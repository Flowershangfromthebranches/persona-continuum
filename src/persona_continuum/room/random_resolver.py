from __future__ import annotations

import random

from persona_continuum.agent.models import AgentProbeResult, AgentStatus, ModelCapability
from persona_continuum.agent.registry import AgentRegistry
from persona_continuum.room.models import ParticipantSlot, ResolvedBindingSnapshot


class ResolverError(Exception):
    """Base error for binding resolution."""


class NoReadyAgentError(ResolverError):
    """Raised when no agent in READY status is available for selection."""


class UnknownRuntimeError(ResolverError):
    """Raised when an explicit or resolved runtime is not registered or not found."""


class UnknownModelError(ResolverError):
    """Raised when an explicit or requested model is not found on the runtime."""


class UnsupportedReasoningEffortError(ResolverError):
    """Raised when an explicit reasoning effort is not supported by the model."""


class RandomBindingResolver:
    def __init__(self, registry: AgentRegistry, seed: int | None = None) -> None:
        self.registry = registry
        self._rng = random.Random(seed) if seed is not None else random.Random()

    def set_seed(self, seed: int) -> None:
        self._rng = random.Random(seed)

    async def resolve_participant(
        self,
        slot: ParticipantSlot,
        probes: list[AgentProbeResult],
    ) -> ResolvedBindingSnapshot:
        probes_by_id = {p.id: p for p in probes}

        # 1. Resolve Agent Runtime
        runtime_id = self._resolve_runtime(slot, probes)
        probe = probes_by_id.get(runtime_id)
        if not probe:
            raise UnknownRuntimeError(f"Runtime '{runtime_id}' not found among probed agents")

        if probe.status != AgentStatus.READY and probe.status.value != "ready":
            raise NoReadyAgentError(
                f"Runtime '{runtime_id}' is not in READY status (current: {probe.status.value})"
            )

        adapter = self.registry.get_adapter(runtime_id)
        if not adapter:
            raise UnknownRuntimeError(f"Runtime '{runtime_id}' has no registered adapter")

        # 2. Resolve Model
        model_cap = self._resolve_model(slot, probe)

        # 3. Resolve Reasoning Effort with Capability Validation
        reasoning_effort = self._resolve_reasoning(slot, model_cap)

        runtime_name = adapter.name if adapter else (probe.name or runtime_id)

        return ResolvedBindingSnapshot(
            participant_id=slot.participant_id,
            persona_id=slot.persona_id,
            display_name=slot.display_name or slot.persona_id,
            agent_runtime_id=runtime_id,
            agent_runtime_name=runtime_name,
            model_id=model_cap.id,
            model_name=model_cap.display_name,
            reasoning_effort=reasoning_effort,
            runtime_candidate_pool=slot.runtime_pool,
            model_candidate_pool=slot.model_pool,
            reasoning_candidate_pool=slot.reasoning_pool,
            auth_profile_id=slot.auth_profile_id,
            adapter_version="0.1.0",
            agent_version=probe.version if probe else "unknown",
            capabilities=(
                probe.capabilities.model_dump(mode="json") if probe else {"streaming": True}
            ),
        )

    resolve_slot = resolve_participant

    async def resolve_all(
        self,
        slots: list[ParticipantSlot],
        probes: list[AgentProbeResult],
    ) -> dict[str, ResolvedBindingSnapshot]:
        snapshots: dict[str, ResolvedBindingSnapshot] = {}
        for slot in slots:
            snapshot = await self.resolve_participant(slot, probes)
            snapshots[slot.participant_id] = snapshot
        return snapshots

    def _resolve_runtime(self, slot: ParticipantSlot, probes: list[AgentProbeResult]) -> str:
        selection = slot.runtime_selection.strip()
        ready_ids = [
            p.id for p in probes if (p.status == AgentStatus.READY or p.status.value == "ready")
        ]

        if selection == "random":
            if not ready_ids:
                raise NoReadyAgentError(
                    "No agent in READY status is available for random selection"
                )
            if slot.runtime_pool:
                candidates: list[str] = []
                weights: list[float] = []
                for item in slot.runtime_pool:
                    r_id = str(item.get("id") or item.get("agent_id", ""))
                    w = float(item.get("weight", 1.0))
                    if r_id in ready_ids:
                        candidates.append(r_id)
                        weights.append(max(0.1, w))
                if candidates:
                    return self._rng.choices(candidates, weights=weights, k=1)[0]
                raise NoReadyAgentError(
                    f"No candidates in runtime_pool are in READY status. Ready agents: {ready_ids}"
                )
            return self._rng.choice(ready_ids)

        if selection == "default" or not selection:
            if not ready_ids:
                raise NoReadyAgentError(
                    "No agent in READY status is available for default selection"
                )
            return ready_ids[0]

        # Explicit selection
        probe = next((p for p in probes if p.id == selection), None)
        if not probe:
            raise UnknownRuntimeError(f"Unknown runtime '{selection}'")
        if probe.status != AgentStatus.READY and probe.status.value != "ready":
            st_val = probe.status.value
            raise NoReadyAgentError(
                f"Selected runtime '{selection}' is not in READY status (current: {st_val})"
            )
        if not self.registry.get_adapter(selection):
            raise UnknownRuntimeError(f"Runtime '{selection}' has no registered adapter")

        return selection

    def _resolve_model(self, slot: ParticipantSlot, probe: AgentProbeResult) -> ModelCapability:
        available_models = probe.models or []
        selection = slot.model_selection.strip()

        if selection == "random":
            if not available_models:
                raise UnknownModelError(f"No available models discovered on runtime '{probe.id}'")
            if slot.model_pool:
                candidates: list[ModelCapability] = []
                weights: list[float] = []
                avail_map = {m.id: m for m in available_models}
                for item in slot.model_pool:
                    m_id = str(item.get("id") or item.get("model_id", ""))
                    w = float(item.get("weight", 1.0))
                    if m_id in avail_map:
                        candidates.append(avail_map[m_id])
                        weights.append(max(0.1, w))
                if candidates:
                    return self._rng.choices(candidates, weights=weights, k=1)[0]
                raise UnknownModelError(
                    f"No candidates in model_pool match available models on runtime '{probe.id}'"
                )
            return self._rng.choice(available_models)

        if selection == "default" or not selection:
            if available_models:
                return available_models[0]
            raise UnknownModelError(f"No default model available for runtime '{probe.id}'")

        # Explicit model selection
        for m in available_models:
            if m.id == selection:
                return m

        if slot.allow_manual_model_id:
            return ModelCapability(
                id=selection,
                display_name=selection,
                provider="custom",
                supported_reasoning_efforts=[],
                default_reasoning_effort="none",
                source="manual",
            )

        raise UnknownModelError(
            f"Model '{selection}' is not supported by runtime '{probe.id}'. "
            f"Available models: {[m.id for m in available_models]}. "
            f"Set allow_manual_model_id=True on ParticipantSlot to permit custom models."
        )

    def _resolve_reasoning(self, slot: ParticipantSlot, model_cap: ModelCapability) -> str:
        supported = model_cap.supported_reasoning_efforts or []
        default_effort = model_cap.default_reasoning_effort or (
            supported[0] if supported else "none"
        )

        selection = slot.reasoning_selection.strip()
        if selection == "random":
            if supported:
                if slot.reasoning_pool:
                    candidates: list[str] = []
                    weights: list[float] = []
                    for item in slot.reasoning_pool:
                        effort = str(item.get("id") or item.get("effort", ""))
                        w = float(item.get("weight", 1.0))
                        if effort in supported:
                            candidates.append(effort)
                            weights.append(max(0.1, w))
                    if candidates:
                        return self._rng.choices(candidates, weights=weights, k=1)[0]
                return self._rng.choice(supported)
            return "none"

        if selection == "default" or not selection:
            return default_effort

        if selection in supported:
            return selection

        if not supported:
            return selection or "none"

        raise UnsupportedReasoningEffortError(
            f"Reasoning effort '{selection}' is not supported by model '{model_cap.id}'. "
            f"Supported reasoning efforts: {supported}"
        )
