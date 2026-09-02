from __future__ import annotations

import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any

from persona_continuum.agent.adapter import AgentAdapter
from persona_continuum.agent.models import (
    AgentSessionConfig,
    AgentStatus,
    PermissionProfile,
)
from persona_continuum.agent.response_collector import AgentRuntimeError, RuntimeUnavailableError
from persona_continuum.agent.runtime_executor import AgentRuntimeExecutor, RuntimeSessionBinding
from persona_continuum.agent.structured_output import (
    StructuredOutputEngine,
    StructuredOutputSchemaError,
    StructuredResult,
)
from persona_continuum.security.validation import NotFoundError
from persona_continuum.world.context_builder import WorldAgentContext, WorldContextBuilder
from persona_continuum.world.models import (
    ActionProposal,
    ActionType,
    Actor,
    TimelineEvent,
    WorldMemoryRecord,
    WorldSeed,
    WorldState,
)

if TYPE_CHECKING:
    from persona_continuum.application.container import PersonaContinuum

logger = logging.getLogger(__name__)


class AgentRuntimeUnavailableError(RuntimeUnavailableError):
    pass


class InvalidActionProposalError(ValueError):
    pass


class ActorRuntime:
    """The only autonomous actor decision path: context -> AgentAdapter -> proposal."""

    def __init__(
        self,
        continuum: PersonaContinuum,
        context_builder: WorldContextBuilder | None = None,
    ) -> None:
        self.continuum = continuum
        self.context_builder = context_builder or WorldContextBuilder()
        self.runtime_executor = (
            getattr(continuum, "agent_runtime_executor", None) or AgentRuntimeExecutor()
        )
        self._sessions: dict[tuple[str, str, str], RuntimeSessionBinding] = {}
        self._traces: dict[tuple[str, str, str], dict[str, Any]] = {}

    async def reason(
        self,
        *,
        world_id: str,
        branch_id: str,
        actor: Actor,
        state: WorldState,
        seed: WorldSeed,
        memories: list[WorldMemoryRecord],
        recent_events: list[TimelineEvent],
    ) -> ActionProposal:
        adapter = await self._resolve_adapter(actor)
        persona_profile = self._load_persona_binding(actor)
        organization_profile = self._load_organization_binding(actor, state)
        actor_profile = self._load_actor_profile_binding(actor)
        context = self.context_builder.build(
            actor=actor,
            state=state,
            seed=seed,
            memories=memories,
            recent_events=recent_events,
            persona_profile=persona_profile,
            organization_profile=organization_profile,
            actor_profile=actor_profile,
        )
        session_binding = await self._session_for(
            adapter=adapter,
            world_id=world_id,
            branch_id=branch_id,
            actor=actor,
        )
        logger.info(
            "world_actor_step actor=%s model=%s decision_source=llm stage=observe",
            actor.name,
            session_binding.session.config.model_id,
        )
        try:
            response, completion_metadata = await self._invoke(session_binding, context)
        except AgentRuntimeError:
            key = (world_id, branch_id, actor.id)
            broken = self._sessions.pop(key, None)
            if broken is not None:
                with contextlib.suppress(Exception):
                    await self.runtime_executor.close(broken)
            raise
        proposal = self._parse_proposal(response, actor.id)
        violations = self.context_builder.firewall.check_knowledge_violation(
            json.dumps(proposal.model_dump(mode="json"), ensure_ascii=False), state.timestamp
        )
        if violations:
            raise InvalidActionProposalError("; ".join(violations))
        trace = {
            "actor_id": actor.id,
            "actor_name": actor.name,
            "agent_id": adapter.adapter_id,
            "model_id": session_binding.session.config.model_id,
            "credential_provider": (
                actor.runtime_config.credential_provider if actor.runtime_config else "unconfigured"
            ),
            "decision_source": "llm",
            "usage": dict(completion_metadata.get("usage") or {}),
        }
        self._traces[(world_id, branch_id, actor.id)] = trace
        logger.info(
            "world_actor_step actor=%s model=%s decision_source=llm stage=proposal",
            actor.name,
            session_binding.session.config.model_id,
        )
        return proposal

    def get_trace(self, world_id: str, branch_id: str, actor_id: str) -> dict[str, Any]:
        return dict(self._traces.get((world_id, branch_id, actor_id), {}))

    async def _resolve_adapter(self, actor: Actor) -> AgentAdapter:
        # Normal world ticks must never probe (a probe spawns CLI processes).
        # Discovery results are cached at startup / manual rescan; we consult
        # that cache and only rescan (without force) when the agent is unknown.
        runtime = actor.runtime_config
        agent_id = runtime.agent_id if runtime else actor.agent_runtime_id
        if not agent_id or agent_id == "unconfigured":
            raise AgentRuntimeUnavailableError(
                f"Actor {actor.id} has no configured LLM Agent runtime"
            )
        selected = self.continuum.agent_registry.get_adapter(agent_id)
        if selected is None:
            await self.continuum.agent_discovery.scan(force_refresh=False)
            selected = self.continuum.agent_registry.get_adapter(agent_id)
        if selected is None:
            raise AgentRuntimeUnavailableError(f"Configured Agent Adapter not found: {agent_id}")
        from persona_continuum.performance.tracing import default_tracer

        default_tracer().incr_global("world_actor_adapter_resolve_count", 1)
        probes = {
            str(probe.id): probe
            for probe in self.continuum.agent_discovery.get_cached_probes()
        }
        probe = probes.get(agent_id)
        if probe is None:
            await self.continuum.agent_discovery.scan(force_refresh=False)
            probe = next(
                (
                    item
                    for item in self.continuum.agent_discovery.get_cached_probes()
                    if str(item.id) == agent_id
                ),
                None,
            )
        if probe is not None and probe.status != AgentStatus.READY:
            # One authoritative re-probe when discovery believes the runtime
            # went away; healthy ticks never reach this branch.
            fresh_probe = await self.continuum.agent_discovery.probe_adapter(agent_id)
            if fresh_probe is not None:
                probe = fresh_probe
        if probe is not None and probe.status != AgentStatus.READY:
            raise AgentRuntimeUnavailableError(
                f"Configured Agent Adapter is not ready: {agent_id} ({probe.status.value})"
            )
        return selected

    async def _session_for(
        self,
        *,
        adapter: AgentAdapter,
        world_id: str,
        branch_id: str,
        actor: Actor,
    ) -> RuntimeSessionBinding:
        key = (world_id, branch_id, actor.id)
        existing = self._sessions.get(key)
        if existing and existing.session.is_active:
            return existing
        runtime = actor.runtime_config
        # Model discovery is served from the shared capability cache; calling
        # adapter.list_models() here would spawn a fresh discovery process per
        # actor session.
        models = await self.runtime_executor.model_capability_cache.get_models(
            adapter,
            auth_profile_id=runtime.credential_id if runtime else None,
        )
        requested_model = runtime.model_id if runtime else actor.model_id
        model_id = (
            models[0].id
            if requested_model in {None, "", "auto", "default", "unconfigured"} and models
            else requested_model
        )
        if not model_id:
            raise AgentRuntimeUnavailableError(
                f"Actor {actor.id} has no model available on {adapter.adapter_id}"
            )
        selected_model = next((item for item in models if item.id == model_id), None)
        reasoning_effort = (
            selected_model.default_reasoning_effort if selected_model is not None else None
        )
        if reasoning_effort in {"none", "default", ""}:
            reasoning_effort = None
        session_binding = await self.runtime_executor.open_session(
            adapter,
            AgentSessionConfig(
                session_id=f"world_{world_id}_{branch_id}_{actor.id}",
                room_id=f"world:{world_id}:{branch_id}",
                participant_id=actor.id,
                persona_id=actor.persona_id or actor.id,
                model_id=model_id,
                auth_profile_id=runtime.credential_id if runtime else None,
                reasoning_effort=reasoning_effort,
                permission_profile=PermissionProfile.CHAT_SAFE,
                allow_mcp=False,
                tools=[],
                extra={
                    "idle_timeout_seconds": runtime.idle_timeout_seconds if runtime else None,
                    "hard_timeout_seconds": runtime.hard_timeout_seconds if runtime else None,
                    # Compatibility input; policy defaults are used when it
                    # is absent rather than injecting a 120-second default.
                    "turn_timeout_seconds": runtime.turn_timeout_seconds if runtime else None,
                    "acp_stream_limit_bytes": runtime.acp_stream_limit_bytes
                    if runtime
                    else 16 * 1024 * 1024,
                },
            )
        )
        self._sessions[key] = session_binding
        return session_binding

    async def _invoke(
        self, session_binding: RuntimeSessionBinding, context: WorldAgentContext
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        session = session_binding.session
        actor_id = session.config.participant_id or session.config.persona_id or "unknown_actor"
        result = await self.runtime_executor.execute_structured(
            session_binding,
            system_prompt=context.system_prompt,
            user_message=context.decision_prompt,
            schema={"type": "object"},
            phase="actor_decision",
            metadata={"actor_id": actor_id},
        )
        if not isinstance(result, StructuredResult) or not isinstance(result.value, dict):
            raise InvalidActionProposalError("actor_decision_output_invalid")
        return dict(result.value), {
            "usage": result.response.usage if result.response is not None else {},
            "audit": (
                result.response.audit(
                    call_id=f"world_turn_{actor_id}",
                    job_id=actor_id,
                    phase="actor_decision",
                )
                if result.response is not None
                else {},
            ),
        }

    def _parse_proposal(self, text: str | dict[str, Any], actor_id: str) -> ActionProposal:
        if isinstance(text, dict):
            candidate: Any = dict(text)
            data: Any = candidate
        else:
            candidate = text.strip()
            try:
                data = StructuredOutputEngine.parse_json(candidate)
            except StructuredOutputSchemaError:
                data = None
            except Exception:
                data = None
        try:
            if isinstance(data, dict):
                data["actor"] = actor_id
                valid_types = {t.value for t in ActionType}
                if "action_type" not in data or data["action_type"] not in valid_types:
                    data["action_type"] = "observe"
                data.setdefault("intent", f"{actor_id} assesses timeline situation")
                data.setdefault(
                    "reasoning_summary",
                    str(candidate)[:150],
                )
                data.setdefault("expected_effect", "Assess current state")
                data.setdefault("confidence", 0.8)
                return ActionProposal.model_validate(data)
        except Exception:
            pass

        raise StructuredOutputSchemaError(
            "agent_action_proposal_invalid",
            phase="actor_decision",
            diagnostics={"actor_id": actor_id, "response_chars": len(str(candidate))},
        )

    def _load_persona_binding(self, actor: Actor) -> dict[str, Any]:
        if not actor.persona_id:
            return {}
        try:
            persona = self.continuum.personas.get(actor.persona_id)
        except (KeyError, NotFoundError, ValueError):
            return {}
        return {
            "id": persona.id,
            "display_name": persona.display_name,
            "manifest": persona.manifest.model_dump(mode="json"),
            "metadata": persona.metadata,
        }

    def _load_organization_binding(self, actor: Actor, state: WorldState) -> dict[str, Any]:
        organization_id = actor.organization_id or actor.identity.get("organization")
        if not organization_id and actor.actor_type.value in {"organization", "organization_actor"}:
            organization_id = actor.id
        return dict(state.organizations.get(str(organization_id), {})) if organization_id else {}

    def _load_actor_profile_binding(self, actor: Actor) -> dict[str, Any]:
        profile_id = actor.profile_id or actor.persona_id
        try:
            profile = self.continuum.profile_library.get_profile(profile_id) if profile_id else None
            if profile is None and actor.profile_type:
                profile = self.continuum.profile_library.find_by_name(
                    actor.name, profile_type=actor.profile_type
                )
            if profile is None:
                return {}
        except Exception:
            return {}
        return profile.model_dump(mode="json")
