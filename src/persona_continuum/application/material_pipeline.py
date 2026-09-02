"""Provider-agnostic planning primitives for private-material intelligence.

This module deliberately contains no Adapter or Provider branches.  It reduces
the work presented to an Agent while preserving every atomic evidence row and
its original provenance.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from persona_continuum.agent.context_capability import (
    PLANNING_CONTEXT_WINDOW_TOKENS,
    compute_preferred_working_context,
    compute_usable_budget,
    default_model_capability_registry,
)
from persona_continuum.agent.prompt_transport import (
    PromptTransportCapability,
    capability_for_mode,
)


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
    structured_output_mode: str = "prompt"
    streaming_mode: str = "unknown"
    model_selection_mode: str = "unknown"
    reasoning_selection_mode: str = "unknown"
    web_search: bool = False
    web_fetch: bool = False
    browser: bool = False
    execution_locality: str = "unknown"

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
            model.get("context_window")
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
                context_source = "model_registry"
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
        parallel_value = capabilities.get("parallel_safe", raw.get("parallel_safe"))
        # Stateless execution is independently callable unless explicitly denied.
        parallel_safe = bool(not persistent if parallel_value is None else parallel_value)
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
            prompt_transport = capability_for_mode(
                transport_raw, source="snapshot_declared"
            )
        return cls(
            adapter_id=str(raw.get("adapter_id") or raw.get("id") or "") or None,
            model_id=str(raw.get("effective_model") or raw.get("model_id") or model.get("id") or "")
            or None,
            context_window=context_window,
            context_window_source=context_source,
            context_capability_source=context_source,
            context_verified=context_window is not None and context_source != "unknown",
            planning_context_window=max(
                1,
                _positive_int(planning_context_window)
                or _positive_int(fallback_context_window)
                or PLANNING_CONTEXT_WINDOW_TOKENS,
            ),
            usable_context_budget=(
                compute_usable_budget(context_window) if context_window is not None else None
            ),
            preferred_working_context=compute_preferred_working_context(
                context_window,
                compute_usable_budget(context_window) if context_window is not None else None,
            ),
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

    candidates = [max(0, int(phase_usable_budget))]
    preferred = profile.preferred_working_context
    if preferred is not None:
        candidates.append(max(0, int(preferred)))
    transport = profile.prompt_transport
    if transport is not None:
        candidates.append(transport.prompt_token_budget())
    target = int(min(candidates) * (1.0 - max(0.0, min(0.5, serialization_margin_ratio))))
    return max(int(floor_tokens), target)


class MaterialPipelineMetrics(BaseModel):
    raw_evidence_units: int = 0
    unique_evidence_units: int = 0
    duplicate_evidence_units: int = 0
    analysis_windows: int = 0
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
    chunked_oversized_units: int = 0


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


def build_analysis_windows(
    units: Sequence[Any],
    *,
    estimate_tokens: Callable[[str], int],
    target_tokens: int,
    max_units: int = 160,
) -> list[AnalysisWindow]:
    """Pack atomic evidence into reasoning windows without merging ledger rows."""

    windows: list[AnalysisWindow] = []
    current: list[Any] = []
    current_tokens = 0
    target = max(256, int(target_tokens))

    def flush() -> None:
        nonlocal current, current_tokens
        if not current:
            return
        ids = [str(item.id) for item in current]
        text = "\n".join(
            f"[EVIDENCE_ID={item.id} SOURCE_ID={item.source_id}] {item.text}" for item in current
        )
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
                conversation_context={"atomic_unit_count": len(current)},
            )
        )
        current = []
        current_tokens = 0

    previous_source: str | None = None
    for unit in units:
        rendered = f"[EVIDENCE_ID={unit.id} SOURCE_ID={unit.source_id}] {unit.text}"
        tokens = estimate_tokens(rendered)
        source = str(unit.source_id)
        source_break = (
            previous_source is not None
            and source != previous_source
            and current_tokens >= target // 2
        )
        if current and (
            len(current) >= max_units or current_tokens + tokens > target or source_break
        ):
            flush()
        current.append(unit)
        current_tokens += tokens
        previous_source = source
    flush()
    return windows


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
    "GlobalCandidateIndex",
    "MaterialPipelineMetrics",
    "MaterialPromptState",
    "PreLLMDeduplicator",
    "ResolvedExecutionProfile",
    "build_analysis_windows",
    "material_batch_target_tokens",
]
