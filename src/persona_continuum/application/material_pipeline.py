"""Provider-agnostic planning primitives for private-material intelligence.

This module deliberately contains no Adapter or Provider branches.  It reduces
the work presented to an Agent while preserving every atomic evidence row and
its original provenance.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from persona_continuum.agent.context_capability import (
    PLANNING_CONTEXT_WINDOW_TOKENS,
    compute_usable_budget,
    default_model_capability_registry,
)
from persona_continuum.agent.phase_policy import PhaseContextPolicy
from persona_continuum.agent.prompt_transport import (
    PromptTransportCapability,
    capability_for_mode,
)
from persona_continuum.application.material_chat import CHAT_SOURCE_KINDS


class ResolvedExecutionProfile(BaseModel):
    """The runtime/model facts the material pipeline is allowed to depend on.

    ``context_window`` is the *reported* window and stays ``None`` when nothing
    reported one.  Any code that needs a number must read
    ``effective_or_planning_window`` and check ``context_verified``; the
    planning fallback is labelled ``fallback_policy`` so it can never be
    presented as a measured capability.
    """

    adapter_id: str | None = None
    model_id: str | None = None
    context_window: int | None = None
    context_window_source: str = "unknown"
    context_capability_source: str = "unknown"
    context_verified: bool = False
    planning_context_window: int = PLANNING_CONTEXT_WINDOW_TOKENS
    usable_context_budget: int | None = None
    preferred_working_context: int | None = None
    remaining_context_tokens: int | None = None
    remaining_context_verified: bool = False
    remaining_context_source: str | None = None
    phase_working_target: int | None = None
    native_context_window: int | None = None
    runtime_effective_context: int | None = None
    max_output_tokens: int | None = None
    supported_reasoning_efforts: list[str] = Field(default_factory=list)
    selected_reasoning_effort: str | None = None
    # Adapter/Protocol capability: how much prompt the execution chain can
    # safely carry.  This is NOT the model context window and must stay a
    # separate number (a 1M model behind an ARGV-only CLI still has a small
    # transport budget).
    prompt_transport: PromptTransportCapability | None = None
    persistent_session: bool = False
    parallel_safe: bool = False
    adapter_session_mode: str = "unknown"
    workload_context_scope: str = "unknown"
    parallel_turns_same_session: bool = False
    parallel_independent_sessions: bool = False
    max_parallel_independent_sessions: int = 1
    effective_independent_session_concurrency: int = 1
    # Persistent verified capability resolved for this runtime (state, source,
    # max_verified, current_recommended).  Telemetry only; never a model limit.
    independent_session_capability: dict[str, Any] = Field(default_factory=dict)
    structured_output_mode: str = "prompt"
    streaming_mode: str = "unknown"
    model_selection_mode: str = "unknown"
    reasoning_selection_mode: str = "unknown"
    web_search: bool = False
    web_fetch: bool = False
    browser: bool = False
    execution_locality: str = "unknown"

    def classification_worker_count(self, configured_max: int = 4) -> int:
        """Workers for independent AnalysisWindow sessions, never same-session turns."""

        cap = max(1, int(configured_max or 1))
        scope = str(self.workload_context_scope or "").casefold()
        if scope == "persistent" and not self.parallel_turns_same_session:
            return 1
        if scope in {"per_window", "per_request"}:
            if self.parallel_independent_sessions or self.parallel_safe:
                return min(cap, max(1, int(self.effective_independent_session_concurrency or 1)))
            return 1
        return cap if self.parallel_safe else 1

    @property
    def effective_or_planning_window(self) -> int:
        """A number a planner may use; check ``context_verified`` before trusting it."""

        if self.context_window is not None:
            return max(1, int(self.context_window))
        return max(1, int(self.planning_context_window or PLANNING_CONTEXT_WINDOW_TOKENS))

    @classmethod
    def resolve(
        cls,
        snapshot: dict[str, Any] | None,
        *,
        # Planning-only fallback.  An unreported window stays Unknown instead
        # of silently becoming this number.
        fallback_context_window: int | None = None,
        planning_context_window: int | None = None,
    ) -> ResolvedExecutionProfile:
        raw = dict(snapshot or {})
        binding = raw.get("runtime_binding_snapshot")
        if isinstance(binding, dict):
            raw.setdefault("context_window", binding.get("context_window"))
            raw.setdefault("remaining_context_tokens", binding.get("remaining_context_tokens"))
            raw.setdefault(
                "remaining_context_verified", binding.get("remaining_context_verified")
            )
            if binding.get("context_window_source"):
                raw.setdefault("context_window_source", binding.get("context_window_source"))
            raw.setdefault("effective_model", binding.get("effective_model"))
        effective_caps = raw.get("effective_model_capabilities")
        if isinstance(effective_caps, dict):
            for key in (
                "native_context_window",
                "effective_context_window",
                "remaining_context_tokens",
                "remaining_context_verified",
                "remaining_context_source",
                "usable_context_budget",
                "preferred_working_context",
                "phase_working_target",
                "context_capability_source",
                "context_verified",
                "prompt_transport",
            ):
                if effective_caps.get(key) is not None:
                    raw.setdefault(key, effective_caps.get(key))
            if effective_caps.get("effective_context_window") is not None:
                raw.setdefault("context_window", effective_caps.get("effective_context_window"))
        capabilities = raw.get("capabilities")
        if not isinstance(capabilities, dict):
            capabilities = {}
        model = raw.get("selected_model") or raw.get("model_capability")
        if not isinstance(model, dict):
            selected_id = raw.get("effective_model") or raw.get("model_id")
            model = next(
                (
                    item
                    for item in (raw.get("models") or [])
                    if isinstance(item, dict)
                    and str(item.get("id") or item.get("model_id")) == str(selected_id)
                ),
                {},
            )
        context_value = (
            raw.get("effective_context_window")
            or model.get("context_window")
            or raw.get("context_window")
            or capabilities.get("context_window")
        )
        context_window = _positive_int(context_value)
        explicit_source = raw.get("context_window_source") or model.get("context_window_source")
        if explicit_source:
            context_source = str(explicit_source)
        elif model.get("context_window") is not None:
            context_source = "provider_reported"
        elif (
            raw.get("context_window") is not None or capabilities.get("context_window") is not None
        ):
            context_source = "adapter_declared"
        else:
            registry_window = default_model_capability_registry().native_context_window(
                raw.get("effective_model") or raw.get("model_id") or model.get("id")
            )
            if registry_window is not None:
                context_window = registry_window
                context_source = "provider_official_registry"
            else:
                context_window = None
                context_source = "unknown"
        reasoning = raw.get("reasoning_capability")
        if not isinstance(reasoning, dict):
            reasoning = {}
        supported = reasoning.get("supported_efforts") or raw.get("supported_reasoning_efforts")
        if not isinstance(supported, list):
            supported = []
        persistent = bool(
            capabilities.get("persistent_session", raw.get("persistent_session", False))
        )
        workload_scope = str(
            raw.get("workload_context_scope")
            or capabilities.get("workload_context_scope")
            or ""
        ).strip().casefold()
        adapter_mode = str(
            raw.get("adapter_session_mode")
            or capabilities.get("adapter_session_mode")
            or ""
        ).strip().casefold()
        parallel_turns = bool(capabilities.get("parallel_turns_same_session", False))
        parallel_independent = bool(
            raw.get("parallel_independent_sessions")
            or capabilities.get("parallel_independent_sessions")
            or False
        )
        max_independent = (
            _positive_int(
                raw.get("max_parallel_independent_sessions")
                or capabilities.get("max_parallel_independent_sessions")
            )
            or 1
        )
        capability_raw = raw.get("independent_session_capability")
        independent_capability = dict(capability_raw) if isinstance(capability_raw, dict) else {}
        parallel_value = capabilities.get("parallel_safe", raw.get("parallel_safe"))
        if parallel_value is not None:
            parallel_safe = bool(parallel_value)
        elif workload_scope == "persistent":
            parallel_safe = bool(parallel_turns)
        elif workload_scope in {"per_window", "per_request"}:
            parallel_safe = bool(parallel_independent) or (not persistent)
        else:
            # Legacy: a persistent-capable adapter is not parallel unless declared.
            parallel_safe = bool(not persistent)
        if workload_scope == "persistent" and not parallel_turns:
            effective_independent = 1
        elif parallel_independent or parallel_safe:
            effective_independent = max(1, int(max_independent))
        else:
            effective_independent = 1
        raw_research = raw.get("research")
        research: dict[str, Any] = raw_research if isinstance(raw_research, dict) else {}
        # The resolved Adapter/Protocol transport capability travels with the
        # runtime snapshot; the planner only reads it and never branches on an
        # adapter id or tool name.
        transport_raw = raw.get("prompt_transport")
        prompt_transport: PromptTransportCapability | None = None
        if isinstance(transport_raw, PromptTransportCapability):
            prompt_transport = transport_raw
        elif isinstance(transport_raw, dict) and transport_raw:
            try:
                prompt_transport = PromptTransportCapability.model_validate(transport_raw)
            except (TypeError, ValueError):
                prompt_transport = None
        elif isinstance(transport_raw, str) and transport_raw.strip():
            prompt_transport = capability_for_mode(transport_raw, source="snapshot_declared")
        return cls(
            adapter_id=str(raw.get("adapter_id") or raw.get("id") or "") or None,
            model_id=str(raw.get("effective_model") or raw.get("model_id") or model.get("id") or "")
            or None,
            context_window=context_window,
            context_window_source=str(
                raw.get("context_capability_source") or context_source
            ),
            context_capability_source=str(
                raw.get("context_capability_source") or context_source
            ),
            context_verified=bool(
                raw.get("context_verified")
                if "context_verified" in raw
                else (
                    context_window is not None
                    and context_source
                    not in {
                        "unknown",
                        "project_static_registry",
                        "model_registry",
                        "fallback_policy",
                        "planning_fallback",
                    }
                )
            ),
            planning_context_window=max(
                1,
                _positive_int(planning_context_window)
                or _positive_int(fallback_context_window)
                or PLANNING_CONTEXT_WINDOW_TOKENS,
            ),
            usable_context_budget=(
                compute_usable_budget(context_window) if context_window is not None else None
            ),
            remaining_context_tokens=_positive_int(
                raw.get("remaining_context_tokens") or model.get("remaining_context_tokens")
            ),
            remaining_context_source=str(raw.get("remaining_context_source") or "") or None,
            remaining_context_verified=bool(raw.get("remaining_context_verified"))
            and str(raw.get("remaining_context_source") or "runtime_reported")
            in {"", "runtime_reported"},
            native_context_window=_positive_int(
                raw.get("native_context_window") or model.get("native_context_window")
            )
            or context_window,
            runtime_effective_context=_positive_int(
                raw.get("effective_context_window") or raw.get("runtime_effective_context")
            )
            or context_window,
            preferred_working_context=None,
            max_output_tokens=_positive_int(
                model.get("max_output_tokens") or raw.get("max_output_tokens")
            ),
            supported_reasoning_efforts=[str(item) for item in supported],
            selected_reasoning_effort=str(
                raw.get("effective_reasoning") or raw.get("reasoning_effort") or ""
            )
            or None,
            prompt_transport=prompt_transport,
            persistent_session=persistent,
            parallel_safe=parallel_safe,
            adapter_session_mode=adapter_mode or "unknown",
            workload_context_scope=workload_scope or "unknown",
            parallel_turns_same_session=parallel_turns,
            parallel_independent_sessions=parallel_independent,
            max_parallel_independent_sessions=max(1, int(max_independent)),
            effective_independent_session_concurrency=max(1, int(effective_independent)),
            independent_session_capability=independent_capability,
            structured_output_mode=str(
                capabilities.get("structured_output_mode")
                or ("native" if capabilities.get("structured_output") else "prompt")
            ),
            streaming_mode=str(
                capabilities.get("streaming_mode")
                or ("supported" if capabilities.get("streaming") else "unknown")
            ),
            model_selection_mode=str(raw.get("model_selection_mode") or "unknown"),
            reasoning_selection_mode=str(raw.get("reasoning_selection_mode") or "unknown"),
            web_search=bool(research.get("web_search", capabilities.get("web_search", False))),
            web_fetch=bool(research.get("web_fetch", capabilities.get("web_fetch", False))),
            browser=bool(research.get("browser", capabilities.get("browser", False))),
            execution_locality=str(capabilities.get("execution_locality") or "unknown"),
        )


class AnalysisWindow(BaseModel):
    id: str
    evidence_unit_ids: list[str]
    source_ids: list[str]
    text: str
    token_estimate: int
    speaker_context: list[str] = Field(default_factory=list)
    temporal_context: list[str] = Field(default_factory=list)
    conversation_context: dict[str, Any] = Field(default_factory=dict)
    # Episode spans contained in this window (episode-aware packing, P0.3-B).
    # One entry per episode that contributed at least one unit; start/end are
    # the span of THIS window's units, so a budget-split episode reports the
    # contained slice rather than the full original span.
    episodes: list[dict[str, Any]] = Field(default_factory=list)
    flush_reason: str | None = None


class EpisodeSpan(BaseModel):
    """A semantic/context atomic conversation block (P0.3-B).

    Built with the same rules as the persisted ConversationEpisode view
    (source + conversation identity, then a time-gap boundary), but it is a
    *planning* structure: an episode bounds what counts as one continuous
    conversation, never how many model calls are made.  Dispatch packing may
    place many episodes into one AnalysisWindow.
    """

    id: str
    source_id: str
    conversation_id: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    is_chat: bool = True
    unit_ids: list[str] = Field(default_factory=list)


class MaterialPromptState(StrEnum):
    """Where one Agent dispatch currently sits inside the material pipeline.

    Entering a stage percent (e.g. 40% for Material Classification) does NOT
    mean the model is running: the prompt may still be building, queued on the
    scheduler, or blocked before the transport ever received it.
    """

    PREPARING_INPUT = "PREPARING_INPUT"
    PROMPT_BUILDING = "PROMPT_BUILDING"
    WAITING_SCHEDULER = "WAITING_SCHEDULER"
    WAITING_RUNTIME = "WAITING_RUNTIME"
    STARTING_SESSION = "STARTING_SESSION"
    SENDING_PROMPT = "SENDING_PROMPT"
    MODEL_RUNNING = "MODEL_RUNNING"
    VALIDATING_OUTPUT = "VALIDATING_OUTPUT"
    CHECKPOINTING = "CHECKPOINTING"
    PAUSED = "PAUSED"
    FAILED = "FAILED"


class CandidateGroup(BaseModel):
    id: str
    evidence_ids: list[str]
    signals: list[str] = Field(default_factory=list)


class AffectedSet(BaseModel):
    new_evidence_ids: list[str] = Field(default_factory=list)
    affected_cluster_ids: list[str] = Field(default_factory=list)
    affected_contradiction_ids: list[str] = Field(default_factory=list)
    affected_fused_ids: list[str] = Field(default_factory=list)
    affected_dimensions: list[str] = Field(default_factory=list)
    neighbor_evidence_ids: list[str] = Field(default_factory=list)


def material_batch_target_tokens(
    profile: ResolvedExecutionProfile,
    *,
    phase_usable_budget: int,
    serialization_margin_ratio: float = 0.08,
    floor_tokens: int = 256,
    phase_policy: PhaseContextPolicy | None = None,
) -> int:
    """Single Material Classification batch target, in tokens.

    The hard chain is:

        Model Context -> Effective Context -> Usable Hard Budget
            -> Preferred Working Context -> Prompt Transport Budget
            -> Actual Phase Batch Target

    and the actual target is the *minimum* of the three per-pass budgets minus
    a serialization/metadata safety margin.  A 1M model therefore does NOT
    produce a ~800K-token single window unless every one of the three budgets
    explicitly allows it.
    """

    remaining = profile.remaining_context_tokens
    hard = remaining
    if hard is None:
        hard = profile.runtime_effective_context or profile.context_window
    working = profile.phase_working_target
    if working is None and phase_policy is not None:
        working = phase_policy.working_target(
            hard,
            phase="material_classification",
            usable_budget=phase_usable_budget,
            verified=bool(profile.context_verified),
            persistent_session=bool(profile.persistent_session),
            fresh_stateless=not profile.persistent_session,
        )
    if working is not None:
        profile.phase_working_target = working
        profile.preferred_working_context = working
    candidates = [max(0, int(phase_usable_budget))]
    if working is not None:
        candidates.append(max(0, int(working)))
    transport = profile.prompt_transport
    if transport is not None:
        candidates.append(transport.prompt_token_budget())
    target = int(min(candidates) * (1.0 - max(0.0, min(0.5, serialization_margin_ratio))))
    return max(int(floor_tokens), target)


# Envelope keys always present on a classification request.  Counted once as
# base overhead so packing does not wait until Prompt Size Guard to discover
# JSON punctuation, ids, and contracts.
_CLASSIFY_ENVELOPE = {
    "_participant_id": "persona_material_intelligence:aw_xxxxxxxxxxxxxxxx",
    "analysis_window": {"id": "aw_xxxxxxxxxxxxxxxx", "source_ids": [], "episode_count": 0},
    "episodes": [],
    "target_units": [],
    "context_units": [],
    "units": [],
    "episode_contract": (
        "Episodes are independent conversation blocks: never connect "
        "question/answer or reference relations across episodes; "
        "preserve original order inside each episode."
    ),
    "source_context": {},
    "allowed_dimensions": [],
    "output_contract": (
        "Return only target turns that contain independent persona "
        "evidence. Omitted target turns are considered reviewed with "
        "no independent evidence. Do not return reviewed_ids."
    ),
}
_EPISODE_OVERHEAD_SAMPLE = {
    "episode_id": "ep_xxxxxxxxxxxxxxxx",
    "source_id": "source_xxxxxxxxxxxxxxxx",
    "start_time": "2023-05-01T09:00:00+00:00",
    "end_time": "2023-05-01T11:00:00+00:00",
    "unit_ids": [],
}
_EPISODE_MARKERS = (
    "[EPISODE_BEGIN id=ep_xxxxxxxxxxxxxxxx source_id=source_xxxxxxxx "
    "start_time=2023-05-01T09:00:00+00:00 end_time=2023-05-01T11:00:00+00:00]"
    "[EPISODE_END id=ep_xxxxxxxxxxxxxxxx]"
)


@dataclass(slots=True)
class ClassificationRequestTokenEstimator:
    """Approximate the final classification request, not just turn.text.

    Prefer a mild overestimate.  Tokenizer-accurate equality is not required;
    Prompt Size Guard remains the last safety net.
    """

    estimate_tokens: Callable[[Any], int]
    margin_ratio: float = 0.08

    def base_tokens(
        self,
        *,
        system_prompt: str = "",
        schema: Any = None,
        source_context: Any = None,
        extra_contracts: Sequence[str] = (),
        allowed_dimensions: Sequence[str] = (),
    ) -> int:
        parts: list[Any] = [
            system_prompt,
            schema,
            source_context or {},
            dict(_CLASSIFY_ENVELOPE),
            list(allowed_dimensions),
            *list(extra_contracts),
        ]
        raw = sum(max(0, int(self.estimate_tokens(part) or 0)) for part in parts if part)
        return int(raw * (1.0 + max(0.0, min(0.2, self.margin_ratio))))

    def episode_overhead(self) -> int:
        return max(
            1,
            int(self.estimate_tokens(_EPISODE_OVERHEAD_SAMPLE) or 0)
            + int(self.estimate_tokens(_EPISODE_MARKERS) or 0),
        )

    def turn_tokens(self, row: dict[str, Any]) -> int:
        # Row JSON plus the same id in target_units and episode.unit_ids.
        ident = str(row.get("id") or "")
        raw = int(self.estimate_tokens(row) or 0)
        ident_tokens = int(self.estimate_tokens(ident) or 0)
        return max(
            1,
            int((raw + ident_tokens * 2 + 10) * (1.0 + max(0.0, min(0.2, self.margin_ratio)))),
        )

    def estimate_request(
        self,
        *,
        system_prompt: str = "",
        schema: Any = None,
        source_context: Any = None,
        extra_contracts: Sequence[str] = (),
        allowed_dimensions: Sequence[str] = (),
        rows: Sequence[dict[str, Any]] = (),
        episode_count: int = 1,
    ) -> int:
        total = self.base_tokens(
            system_prompt=system_prompt,
            schema=schema,
            source_context=source_context,
            extra_contracts=extra_contracts,
            allowed_dimensions=allowed_dimensions,
        )
        total += self.episode_overhead() * max(0, int(episode_count))
        total += sum(self.turn_tokens(dict(row)) for row in rows)
        return total


class MaterialPipelineMetrics(BaseModel):
    raw_evidence_units: int = 0
    unique_evidence_units: int = 0
    duplicate_evidence_units: int = 0
    analysis_windows: int = 0
    # Large Conversation Pipeline V2 accounting.  These counts describe the
    # chat -> Material Intelligence entry only; raw provenance rows are never
    # merged or removed to make a number look better.
    raw_message_count: int = 0
    target_message_count: int = 0
    context_message_count: int = 0
    conversation_turn_count: int = 0
    target_turn_count: int = 0
    classification_worker_count: int = 0
    effective_classification_workers: int = 0
    active_classification_workers: int = 0
    peak_active_classification_workers: int = 0
    active_independent_sessions: int = 0
    peak_independent_sessions: int = 0
    workload_context_scope: str = "unknown"
    adapter_session_mode: str = "unknown"
    parallel_turns_same_session: bool = False
    parallel_independent_sessions: bool = False
    max_parallel_independent_sessions: int = 1
    runtime_pool_leases: int = 0
    runtime_pool_wait_ms: float = 0.0
    context_capability_revision: int = 0
    context_remaining_revision: int = 0
    remaining_source: str | None = None
    remaining_verified: bool = False
    average_active_classification_workers: float = 0.0
    classification_worker_seconds: float = 0.0
    window_queue_depth: int = 0
    peak_window_queue_depth: int = 0
    window_queue_capacity: int = 0
    classification_windows_dispatched: int = 0
    # Adaptive concurrency retry telemetry.  A concurrency-limit window is
    # requeued after a bounded downgrade; quota/payment failures are never
    # downgraded and are counted separately.
    window_retries: int = 0
    window_retry_limit: int = 0
    concurrency_downgrades: int = 0
    concurrency_downgrade_events: list[dict[str, Any]] = Field(default_factory=list)
    # Concurrency generation/epoch: one downgrade per generation, so a wave of
    # simultaneous rejections cannot walk the ladder more than once.
    concurrency_generation: int = 0
    effective_concurrency: int = 0
    stale_rejection_count: int = 0
    stale_replayed_windows: int = 0
    terminal_concurrency_failures: int = 0
    rate_limit_replays: int = 0
    provider_failure_kind: str | None = None
    last_concurrency_failure_kind: str | None = None
    non_retriable_capacity_failures: int = 0
    independent_session_capability: dict[str, Any] = Field(default_factory=dict)
    semantic_gate_mode: str = "full"
    semantic_selected: int = 0
    semantic_skipped: int = 0
    semantic_skipped_messages: int = 0
    semantic_reserve_selected: int = 0
    semantic_bypass_count: int = 0
    classification_windows_total: int = 0
    classification_windows_completed: int = 0
    target_turns_reviewed: int = 0
    evidence_turns_extracted: int = 0
    reviewed_no_independent_evidence: int = 0
    average_turns_per_window: float = 0.0
    max_turns_per_window: int = 0
    agent_calls: int = 0
    style_profile_status: str = "not_applicable"
    relation_candidate_groups: int = 0
    relation_candidate_evidence: int = 0
    deterministic_fusions: int = 0
    model_assisted_fusions: int = 0
    incremental_affected_units: int = 0
    full_corpus_agent_rescans: int = 0
    agent_turns: dict[str, int] = Field(default_factory=dict)
    input_tokens: dict[str, int] = Field(default_factory=dict)
    output_tokens: dict[str, int] = Field(default_factory=dict)
    cache_hits: dict[str, int] = Field(default_factory=dict)
    elapsed_ms: dict[str, float] = Field(default_factory=dict)
    # Reported window only.  None means Unknown, never 32K.
    context_window: int | None = None
    context_window_source: str = "unknown"
    context_verified: bool = False
    planning_context_window: int | None = None
    usable_context_budget: int | None = None
    preferred_working_context: int | None = None
    # Largest single serialized dispatch actually sent downstream.  Recorded
    # so "额度没变化" can be diagnosed: a dispatch that never happened has no
    # prompt bytes here.
    max_batch_target_tokens: int | None = None
    max_serialized_prompt_tokens: int | None = None
    max_serialized_prompt_bytes: int | None = None
    transport_mode: str | None = None
    transport_max_prompt_bytes: int | None = None
    rebatched_windows: int = 0
    rebatch_reasons: dict[str, int] = Field(default_factory=dict)
    initial_analysis_windows: int = 0
    packing_accuracy: float = 0.0
    rebatched_window_ratio: float = 0.0
    estimated_prompt_tokens_samples: list[int] = Field(default_factory=list, exclude=True)
    actual_prompt_tokens_samples: list[int] = Field(default_factory=list, exclude=True)
    estimation_error_samples: list[float] = Field(default_factory=list, exclude=True)
    estimated_prompt_tokens_p50: float = 0.0
    estimated_prompt_tokens_p95: float = 0.0
    actual_prompt_tokens_p50: float = 0.0
    actual_prompt_tokens_p95: float = 0.0
    estimation_error_p50: float = 0.0
    estimation_error_p95: float = 0.0
    chunked_oversized_units: int = 0
    token_budget_utilization: float = 0.0
    turn_cap_hit_count: int = 0
    episode_cap_hit_count: int = 0
    transport_cap_hit_count: int = 0
    context_cap_hit_count: int = 0
    native_context: int | None = None
    runtime_effective: int | None = None
    usable_context: int | None = None
    phase_working_target: int | None = None
    transport_token_budget: int | None = None
    actual_prompt_tokens: int | None = None
    utilization: float | None = None
    # Episode-aware window packing (P0.3-C).  Averages/percentiles are
    # derived from the raw samples via ``finalize_window_metrics``; samples
    # themselves stay out of serialized payloads.
    episodes_total: int = 0
    episodes_per_window_avg: float = 0.0
    episodes_per_window_max: int = 0
    prompt_budget_utilization_avg: float = 0.0
    prompt_budget_utilization_p50: float = 0.0
    prompt_budget_utilization_p95: float = 0.0
    episodes_per_window_samples: list[int] = Field(default_factory=list, exclude=True)
    prompt_budget_utilization_samples: list[float] = Field(default_factory=list, exclude=True)

    def stamp_execution_profile(
        self, profile: ResolvedExecutionProfile, *, configured_max: int = 4
    ) -> None:
        self.workload_context_scope = str(profile.workload_context_scope or "unknown")
        self.adapter_session_mode = str(profile.adapter_session_mode or "unknown")
        self.parallel_turns_same_session = bool(profile.parallel_turns_same_session)
        self.parallel_independent_sessions = bool(profile.parallel_independent_sessions)
        self.max_parallel_independent_sessions = max(
            1, int(profile.max_parallel_independent_sessions or 1)
        )
        workers = profile.classification_worker_count(configured_max)
        self.classification_worker_count = workers
        self.effective_classification_workers = workers
        if profile.independent_session_capability:
            self.independent_session_capability = dict(profile.independent_session_capability)
        self.remaining_source = profile.remaining_context_source
        self.remaining_verified = bool(profile.remaining_context_verified)
        try:
            from persona_continuum.performance.runtime_pool import default_runtime_pool

            snap = default_runtime_pool().snapshot()
            self.runtime_pool_leases = int(snap.get("active_leases") or 0)
            self.runtime_pool_wait_ms = float(snap.get("wait_ms_total") or 0.0)
        except Exception:
            pass

    def record_analysis_windows(
        self, windows: Sequence[AnalysisWindow], *, target_tokens: int
    ) -> None:
        """Accumulate episode-packing counters for one dispatch batch."""

        stats = window_episode_metrics(windows, target_tokens=target_tokens)
        self.token_budget_utilization = float(stats.get("prompt_budget_utilization_avg") or 0.0)
        for window in windows:
            reason = str(getattr(window, "flush_reason", "") or "")
            if reason == "unit_cap":
                self.turn_cap_hit_count += 1
            elif reason == "episode_cap":
                self.episode_cap_hit_count += 1
            elif reason == "token_budget":
                self.context_cap_hit_count += 1
        self.episodes_total += int(stats["episodes_total"])
        self.episodes_per_window_samples.extend(
            len(window.episodes) for window in windows
        )
        self.prompt_budget_utilization_samples.extend(
            min(1.0, window.token_estimate / max(1, int(target_tokens))) for window in windows
        )

    def finalize_window_metrics(self) -> None:
        """Derive avg/max/percentile aggregates from the raw samples."""

        samples = self.episodes_per_window_samples
        self.episodes_per_window_avg = round(
            sum(samples) / len(samples), 6
        ) if samples else 0.0
        self.episodes_per_window_max = max(samples, default=0)
        utils = sorted(self.prompt_budget_utilization_samples)

        def percentile(fraction: float) -> float:
            if not utils:
                return 0.0
            index = min(len(utils) - 1, max(0, int(round(fraction * (len(utils) - 1)))))
            return round(utils[index], 6)

        self.prompt_budget_utilization_avg = round(
            sum(utils) / len(utils), 6
        ) if utils else 0.0
        self.prompt_budget_utilization_p50 = percentile(0.50)
        self.prompt_budget_utilization_p95 = percentile(0.95)
        self.initial_analysis_windows = max(self.initial_analysis_windows, self.analysis_windows)
        calls = max(1, int(self.agent_calls or 0))
        windows = max(1, int(self.initial_analysis_windows or self.analysis_windows or 0))
        self.packing_accuracy = round(
            (self.initial_analysis_windows or self.analysis_windows) / calls, 6
        ) if (self.initial_analysis_windows or self.analysis_windows) else 0.0
        self.rebatched_window_ratio = round(self.rebatched_windows / windows, 6)

        def token_percentile(values: list[int] | list[float], fraction: float) -> float:
            if not values:
                return 0.0
            ordered = sorted(values)
            index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
            return round(float(ordered[index]), 6)

        self.estimated_prompt_tokens_p50 = token_percentile(
            self.estimated_prompt_tokens_samples, 0.50
        )
        self.estimated_prompt_tokens_p95 = token_percentile(
            self.estimated_prompt_tokens_samples, 0.95
        )
        self.actual_prompt_tokens_p50 = token_percentile(self.actual_prompt_tokens_samples, 0.50)
        self.actual_prompt_tokens_p95 = token_percentile(self.actual_prompt_tokens_samples, 0.95)
        self.estimation_error_p50 = token_percentile(self.estimation_error_samples, 0.50)
        self.estimation_error_p95 = token_percentile(self.estimation_error_samples, 0.95)

    def record_rebatch(self, reason: str) -> None:
        self.rebatched_windows += 1
        key = str(reason or "UNKNOWN")
        self.rebatch_reasons[key] = self.rebatch_reasons.get(key, 0) + 1

    def record_prompt_estimate(self, estimated: int, actual: int) -> None:
        self.estimated_prompt_tokens_samples.append(max(0, int(estimated)))
        self.actual_prompt_tokens_samples.append(max(0, int(actual)))
        if actual > 0:
            self.estimation_error_samples.append((int(estimated) - int(actual)) / float(actual))


class DeduplicationResult(BaseModel):
    units: list[Any]
    canonical_units: list[Any]
    supporting_units: dict[str, list[str]]


class PreLLMDeduplicator:
    """Exact and conservative near-dedup without discarding provenance rows."""

    def deduplicate(self, units: Sequence[Any]) -> DeduplicationResult:
        canonical_by_normalized: dict[str, Any] = {}
        canonical_units: list[Any] = []
        support: dict[str, list[str]] = defaultdict(list)
        token_index: dict[str, list[Any]] = defaultdict(list)
        updated: list[Any] = []
        for unit in units:
            normalized = str(getattr(unit, "normalized_text", "") or "")
            if getattr(unit, "source_kind", None) in {"chat", "chat_import", "guided_interview"}:
                canonical_units.append(unit)
                support[str(unit.id)].append(str(unit.id))
                updated.append(unit)
                continue
            canonical = canonical_by_normalized.get(normalized) if normalized else None
            match_type = "exact" if canonical is not None else None
            if canonical is None and normalized:
                candidates: dict[str, Any] = {}
                for token in _blocking_tokens(normalized):
                    for candidate in token_index.get(token, ())[-64:]:
                        candidates[str(candidate.id)] = candidate
                best: tuple[float, Any] | None = None
                for candidate in candidates.values():
                    score = _near_score(normalized, str(candidate.normalized_text))
                    same_source = str(candidate.source_id) == str(unit.source_id)
                    threshold = 0.86 if same_source else 0.93
                    if score >= threshold and (best is None or score > best[0]):
                        best = (score, candidate)
                if best is not None:
                    canonical = best[1]
                    match_type = (
                        "same_source_overlap"
                        if (str(canonical.source_id) == str(unit.source_id))
                        else "near_duplicate"
                    )
            if canonical is None:
                canonical = unit
                canonical_units.append(unit)
                if normalized:
                    canonical_by_normalized[normalized] = unit
                    for token in _blocking_tokens(normalized):
                        token_index[token].append(unit)
            support[str(canonical.id)].append(str(unit.id))
            metadata = dict(getattr(unit, "metadata", {}) or {})
            metadata["dedup"] = {
                "canonical_evidence_id": str(canonical.id),
                "match_type": match_type or "canonical",
            }
            updated.append(unit.model_copy(update={"metadata": metadata}))
        by_id = {str(item.id): item for item in updated}
        for canonical_id, member_ids in support.items():
            canonical = by_id[canonical_id]
            metadata = dict(canonical.metadata)
            metadata["dedup"] = {
                **dict(metadata.get("dedup") or {}),
                "supporting_evidence_ids": list(member_ids),
            }
            by_id[canonical_id] = canonical.model_copy(update={"metadata": metadata})
        ordered = [by_id[str(item.id)] for item in updated]
        canonicals = [by_id[str(item.id)] for item in canonical_units]
        return DeduplicationResult(
            units=ordered,
            canonical_units=canonicals,
            supporting_units=dict(support),
        )


def build_conversation_episodes(
    units: Sequence[Any],
    *,
    episode_gap_seconds: int = 7200,
) -> list[EpisodeSpan]:
    """Segment chat items into semantic atomic conversation episodes.

    An episode boundary is a real conversation boundary: source identity,
    conversation identity, or a silent gap longer than ``episode_gap_seconds``
    between two chat units.  Non-chat units form same-source spans (document
    structure is bounded by source identity only; the pack step applies the
    legacy half-full source-change rule).  Boundary rules mirror the persisted
    ConversationEpisode view; this function never merges or reorders
    provenance rows.
    """

    spans: list[EpisodeSpan] = []
    current: list[Any] = []
    current_is_chat = True

    def flush() -> None:
        nonlocal current
        if current:
            first = current[0]
            last = current[-1]
            conversation = getattr(first, "conversation_id", None)
            spans.append(
                EpisodeSpan(
                    id=_sha(
                        "episode|"
                        f"{first.source_id}|{conversation}|{getattr(first, 'id', len(spans))}"
                    )[:16],
                    source_id=str(first.source_id),
                    conversation_id=str(conversation) if conversation else None,
                    start_time=_time_str(first),
                    end_time=_time_str(last),
                    is_chat=current_is_chat,
                    unit_ids=[str(item.id) for item in current],
                )
            )
        current = []

    previous_source: str | None = None
    previous_conversation: str | None = None
    previous_time: datetime | None = None
    for unit in units:
        source = str(unit.source_id)
        is_chat = str(getattr(unit, "source_kind", None) or "") in CHAT_SOURCE_KINDS
        conversation_raw = getattr(unit, "conversation_id", None)
        conversation = str(conversation_raw) if conversation_raw else None
        current_time = _parse_time(unit)
        boundary = bool(current) and (
            is_chat != current_is_chat
            or source != previous_source
            or (
                is_chat
                and current_is_chat
                and conversation != previous_conversation
            )
        )
        if (
            not boundary
            and is_chat
            and previous_time is not None
            and current_time is not None
            and previous_time.tzinfo == current_time.tzinfo
        ):
            boundary = (
                current_time - previous_time
            ).total_seconds() > max(0, int(episode_gap_seconds))
        if boundary:
            flush()
        if not current:
            current_is_chat = is_chat
        current.append(unit)
        previous_source = source
        previous_conversation = conversation
        previous_time = current_time or previous_time
    flush()
    return spans


def build_analysis_windows(
    units: Sequence[Any],
    *,
    estimate_tokens: Callable[[str], int],
    target_tokens: int,
    max_units: int | None = None,
    estimate_unit_tokens: Callable[[Any], int] | None = None,
    episode_gap_seconds: int = 7200,
    max_episodes_per_window: int | None = None,
    episode_flush: bool = False,
    base_tokens: int = 0,
    episode_overhead_tokens: int = 0,
) -> list[AnalysisWindow]:
    """Pack analysis items into reasoning windows without merging ledger rows.

    The primary constraints are the token budget and the conversation/source
    boundary; ``max_units`` is only a loose safety ceiling on *target* units.
    ``None`` removes the count cap entirely (the caller must still enforce the
    Prompt Size Guard).  For chat material the sequence may contain both
    target and context ConversationTurns: only ``evidence_source`` turns count
    against the cap, so a long context-only tail never triggers an extra
    flush.

    Episode-aware packing (P0.3-B/C): chat items are first segmented into
    :class:`EpisodeSpan` semantic atomic blocks.  A time-gap episode boundary
    does NOT force a model dispatch flush — multiple episodes are packed into
    the same window until the token budget, the target ceiling, or
    ``max_episodes_per_window`` is reached.  Set ``episode_flush=True`` to
    reproduce the legacy pre-P0.3 behavior (one window flush per gap) for
    A/B benchmarks.  Source/conversation identity changes still flush: that
    boundary protection is semantic, not budget bookkeeping.

    Every window's ``text`` marks each chat episode with
    ``EPISODE_BEGIN``/``EPISODE_END`` so the model can never treat episodes
    as one continuous conversation; ``window.episodes`` carries the same
    structure for the request payload.
    """

    windows: list[AnalysisWindow] = []
    target = max(256, int(target_tokens))
    base = max(0, int(base_tokens or 0))
    episode_overhead = max(0, int(episode_overhead_tokens or 0))
    episode_cap = None if max_episodes_per_window is None else max(1, int(max_episodes_per_window))
    pending_reason = "end"
    spans = build_conversation_episodes(
        units,
        episode_gap_seconds=episode_gap_seconds,
    )
    span_by_id = {span.id: span for span in spans}
    members_by_span: dict[str, list[Any]] = {span.id: [] for span in spans}
    unit_by_id = {str(unit.id): unit for unit in units}

    current: list[Any] = []
    current_tokens = 0
    current_targets = 0
    current_span_ids: list[str] = []

    def flush(reason: str = "end") -> None:
        nonlocal current, current_tokens, current_targets, current_span_ids, pending_reason
        if not current:
            return
        pending_reason = reason
        ids = [str(item.id) for item in current]
        episode_entries: list[dict[str, Any]] = []
        chunks: list[str] = []
        for span_id_value in current_span_ids:
            span = span_by_id.get(span_id_value)
            members = members_by_span.get(span_id_value, [])
            if not members:
                continue
            if span is not None and span.is_chat:
                chunks.append(
                    f"[EPISODE_BEGIN id={span.id} source_id={span.source_id} "
                    f"start_time={_time_str(members[0])} end_time={_time_str(members[-1])}]"
                )
                chunks.extend(
                    f"[EVIDENCE_ID={item.id} SOURCE_ID={item.source_id}] {item.text}"
                    for item in members
                )
                chunks.append(f"[EPISODE_END id={span.id}]")
            else:
                chunks.extend(
                    f"[EVIDENCE_ID={item.id} SOURCE_ID={item.source_id}] {item.text}"
                    for item in members
                )
            episode_entries.append(
                {
                    "episode_id": span_id_value,
                    "source_id": str(members[0].source_id),
                    "start_time": _time_str(members[0]),
                    "end_time": _time_str(members[-1]),
                    "unit_ids": [str(item.id) for item in members],
                }
            )
        text = "\n".join(chunks)
        windows.append(
            AnalysisWindow(
                id=f"aw_{_sha('|'.join(ids))[:16]}",
                evidence_unit_ids=ids,
                source_ids=sorted({str(item.source_id) for item in current}),
                text=text,
                token_estimate=estimate_tokens(text),
                speaker_context=sorted(
                    {str(item.speaker) for item in current if getattr(item, "speaker", None)}
                ),
                temporal_context=sorted(
                    {
                        str(value)
                        for item in current
                        for value in (
                            getattr(item, "timestamp", None),
                            getattr(item, "event_time", None),
                        )
                        if value
                    }
                ),
                conversation_context={
                    "atomic_unit_count": len(current),
                    "episode_count": len(episode_entries),
                },
                episodes=episode_entries,
                flush_reason=pending_reason,
            )
        )
        for span_id_value in current_span_ids:
            members_by_span[span_id_value] = []
        current = []
        current_tokens = base
        current_targets = 0
        current_span_ids = []

    current_tokens = base
    for span in spans:
        span_units = [unit_by_id[item_id] for item_id in span.unit_ids]
        is_chat = span.is_chat
        if current and is_chat:
            # Episode boundary: legacy mode flushed every gap (the P0.3-A
            # behavior being removed); packed mode flushes only when a
            # packing ceiling is already reached.  Episodes from different
            # sources/conversations may share one window — the EPISODE
            # markers carry the identity protection the old flush provided.
            if episode_flush:
                flush("episode_boundary")
            elif max_units is not None and current_targets >= max(int(max_units), 1):
                flush("unit_cap")
            elif episode_cap is not None and len(current_span_ids) >= episode_cap:
                flush("episode_cap")
        elif current and not is_chat and current_tokens >= target // 2:
            # Legacy document behavior: a source change flushes a half-full
            # window so tiny document tails do not each own a dispatch.
            flush("source_boundary")
        for unit in span_units:
            rendered = f"[EVIDENCE_ID={unit.id} SOURCE_ID={unit.source_id}] {unit.text}"
            tokens = (
                estimate_unit_tokens(unit) if estimate_unit_tokens else estimate_tokens(rendered)
            )
            unit_is_target = (
                str(getattr(unit, "semantic_role", "evidence_source")) != "context_only"
            )
            extra_episode = episode_overhead if span.id not in current_span_ids else 0
            if current and (
                max_units is not None
                and unit_is_target
                and current_targets >= max(int(max_units), 1)
            ):
                flush("unit_cap")
                extra_episode = episode_overhead
            elif current and current_tokens + tokens + extra_episode > target:
                flush("token_budget")
                extra_episode = episode_overhead
            members_by_span.setdefault(span.id, []).append(unit)
            if span.id not in current_span_ids:
                current_span_ids.append(span.id)
                current_tokens += extra_episode
            current.append(unit)
            current_targets += int(unit_is_target)
            current_tokens += tokens
    flush()
    return windows


def _parse_time(unit: Any) -> datetime | None:
    raw_time = getattr(unit, "timestamp", None)
    if not raw_time:
        return None
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
    return None


def _time_str(unit: Any) -> str | None:
    value = getattr(unit, "end_time", None) or getattr(unit, "timestamp", None)
    return str(value) if value else None


def window_episode_metrics(
    windows: Sequence[AnalysisWindow],
    *,
    target_tokens: int,
) -> dict[str, Any]:
    """Episode packing quality counters for one set of windows (P0.3-C)."""

    per_window_episodes = [len(window.episodes) for window in windows]
    episodes_total = sum(per_window_episodes)
    budget = max(1, int(target_tokens))
    utilization = [min(1.0, window.token_estimate / budget) for window in windows]

    def percentile(values: list[float], fraction: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
        return round(ordered[index], 6)

    return {
        "windows": len(windows),
        "episodes_total": episodes_total,
        "episodes_per_window_avg": round(episodes_total / len(windows), 6) if windows else 0.0,
        "episodes_per_window_max": max(per_window_episodes, default=0),
        "prompt_budget_utilization_avg": round(sum(utilization) / len(utilization), 6)
        if utilization
        else 0.0,
        "prompt_budget_utilization_p50": percentile(utilization, 0.50),
        "prompt_budget_utilization_p95": percentile(utilization, 0.95),
    }


class GlobalCandidateIndex:
    """Generate relation candidates across the entire corpus using local signals."""

    def groups(
        self,
        units: Sequence[Any],
        *,
        affected_ids: set[str] | None = None,
        max_group_size: int = 12,
    ) -> list[CandidateGroup]:
        buckets: dict[tuple[str, str], set[str]] = defaultdict(set)
        dimension_members: dict[str, set[str]] = defaultdict(set)
        for unit in units:
            unit_id = str(unit.id)
            normalized = str(getattr(unit, "normalized_text", "") or "")
            intelligence = dict(getattr(unit, "metadata", {}) or {}).get(
                "evidence_intelligence", {}
            )
            if not isinstance(intelligence, dict):
                intelligence = {}
            tokens = _blocking_tokens(normalized)
            for token in tokens[:8]:
                buckets[("lexical", token)].add(unit_id)
            for entity in list(getattr(unit, "relationship_entities", ()) or ()) + list(
                intelligence.get("entities") or ()
            ):
                value = str(entity).strip().casefold()
                if value:
                    buckets[("entity", value)].add(unit_id)
            for year in re.findall(r"\b(?:19|20)\d{2}\b", str(getattr(unit, "text", ""))):
                buckets[("temporal", year)].add(unit_id)
            for dimension in getattr(unit, "dimension_candidates", ()) or ():
                dimension_members[str(dimension)].add(unit_id)
        # A dimension by itself is only a useful relation signal for a small
        # corpus.  Treating a 5,000-item dimension as one candidate bucket
        # would recreate the full-corpus rescan this index is meant to avoid.
        for dimension, member_ids in dimension_members.items():
            if 2 <= len(member_ids) <= max_group_size:
                buckets[("dimension", dimension)].update(member_ids)
        combined: dict[frozenset[str], set[str]] = defaultdict(set)
        for (signal, _), member_ids in buckets.items():
            if len(member_ids) < 2 or len(member_ids) > max_group_size * 12:
                continue
            if affected_ids is not None and not member_ids.intersection(affected_ids):
                continue
            members = sorted(member_ids)
            for start in range(0, len(members), max_group_size):
                chunk = frozenset(members[start : start + max_group_size])
                if len(chunk) >= 2 and (affected_ids is None or chunk.intersection(affected_ids)):
                    combined[chunk].add(signal)
        result: list[CandidateGroup] = []
        for ids, signals in sorted(combined.items(), key=lambda item: sorted(item[0])):
            # A large entity-only group says the same person was mentioned; it
            # does not by itself imply duplicate/conflicting claims.  Keep it
            # for incremental neighbor discovery, but do not spend full-run
            # relation turns on that weak signal.
            if affected_ids is None and signals == {"entity"} and len(ids) > 4:
                continue
            result.append(
                CandidateGroup(
                    id=f"rcg_{_sha('|'.join(sorted(ids)))[:16]}",
                    evidence_ids=sorted(ids),
                    signals=sorted(signals),
                )
            )
        return result


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _blocking_tokens(value: str) -> list[str]:
    words = re.findall(r"[a-z0-9_]{3,}|[\u3400-\u9fff]{2,6}", value.casefold())
    if not words and value:
        words = [value[index : index + 3] for index in range(max(0, len(value) - 2))]
    return sorted(set(words), key=lambda item: (-len(item), item))[:16]


def _near_score(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if min(len(left), len(right)) < 24:
        # A one-character difference in a short chat can invert a name,
        # amount, answer, or choice.  Exact dedup remains safe; fuzzy collapse
        # does not.
        return 0.0
    left_numbers = re.findall(r"\d+(?:\.\d+)?", left)
    right_numbers = re.findall(r"\d+(?:\.\d+)?", right)
    if left_numbers != right_numbers:
        # Different dates/counts are relation or contradiction candidates, not
        # safe duplicates.  Never collapse their provenance before analysis.
        return 0.0
    left_tokens, right_tokens = set(_blocking_tokens(left)), set(_blocking_tokens(right))
    union = left_tokens | right_tokens
    token_score = len(left_tokens & right_tokens) / len(union) if union else 0.0
    length_score = min(len(left), len(right)) / max(1, max(len(left), len(right)))
    return token_score * 0.8 + length_score * 0.2


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "AffectedSet",
    "AnalysisWindow",
    "CandidateGroup",
    "DeduplicationResult",
    "EpisodeSpan",
    "GlobalCandidateIndex",
    "MaterialPipelineMetrics",
    "MaterialPromptState",
    "PreLLMDeduplicator",
    "ResolvedExecutionProfile",
    "build_analysis_windows",
    "build_conversation_episodes",
    "material_batch_target_tokens",
    "window_episode_metrics",
]
