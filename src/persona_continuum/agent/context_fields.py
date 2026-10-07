"""Extract context-window facts from nested runtime / catalog payloads.

Adapter protocols disagree on field names.  Persona Continuum never treats a
model's native window, a runtime's effective window, remaining capacity, or a
prompt-transport ceiling as the same number.  This helper only *finds* the
numbers; :class:`ContextCapabilityResolver` ranks them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from persona_continuum.numeric import safe_int

CONTEXT_WINDOW_KEYS: tuple[str, ...] = (
    "contextWindow",
    "context_window",
    "contextLength",
    "context_length",
    "maxContextTokens",
    "max_context_tokens",
    "totalContextTokens",
    "total_context_tokens",
    "effective_context_window",
    "effectiveContextWindow",
    "native_context_window",
    "nativeContextWindow",
    "max_context",
    "maxContext",
    "maxContextWindow",
    "max_context_window",
    "model_context_window",
    "modelContextWindow",
)

REMAINING_CONTEXT_KEYS: tuple[str, ...] = (
    "remainingContextTokens",
    "remaining_context_tokens",
    "remainingContext",
    "remaining_context",
    "contextRemaining",
    "context_remaining",
    "tokensRemaining",
    "tokens_remaining",
)

# Occupancy of the live context window.  Per-turn billing fields such as
# ``inputTokens`` / ``input_tokens`` are NOT occupancy and must not be used
# to derive remaining unless an adapter explicitly opts in.
OCCUPANCY_CONTEXT_KEYS: tuple[str, ...] = (
    "usedContextTokens",
    "used_context_tokens",
    "contextUsed",
    "context_used",
    "tokensUsed",
    "tokens_used",
    "totalContextTokensUsed",
    "total_context_tokens_used",
)
USED_CONTEXT_KEYS = OCCUPANCY_CONTEXT_KEYS


class ContextUsageKind(StrEnum):
    """What one protocol's usage numbers actually mean."""

    CURRENT_PROMPT_USAGE = "current_prompt_usage"
    CUMULATIVE_CONTEXT_USAGE = "cumulative_context_usage"
    BILLING_INPUT_USAGE = "billing_input_usage"
    CACHE_EXCLUDED_INPUT_USAGE = "cache_excluded_input_usage"
    UNKNOWN = "unknown"


class ContextScope(StrEnum):
    """Whether model context occupancy carries across turns.

    This is the *workload* remaining-accounting scope, not whether the
    adapter is physically able to keep a process alive.
    """

    PERSISTENT = "persistent"
    PER_REQUEST = "per_request"
    PER_WINDOW = "per_window"
    UNKNOWN = "unknown"


class AdapterSessionMode(StrEnum):
    """What kind of native session the adapter can host."""

    NONE = "none"
    PER_REQUEST = "per_request"
    PERSISTENT_CAPABLE = "persistent_capable"
    UNKNOWN = "unknown"


class WorkloadContextScope(StrEnum):
    """How the current pipeline actually reuses model context."""

    PER_REQUEST = "per_request"
    PER_WINDOW = "per_window"
    PERSISTENT = "persistent"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ContextUsageSemantics:
    """How one adapter/protocol reports live context occupancy.

    Default is fail-closed: remaining is only taken from explicit remaining
    fields.  ``inputTokens`` is never occupancy unless the adapter lists it
    in ``occupancy_keys``, sets ``derive_remaining_from_occupancy``, and
    declares ``CUMULATIVE_CONTEXT_USAGE``.
    """

    remaining_keys: tuple[str, ...] = REMAINING_CONTEXT_KEYS
    occupancy_keys: tuple[str, ...] = OCCUPANCY_CONTEXT_KEYS
    derive_remaining_from_occupancy: bool = False
    usage_kind: str = ContextUsageKind.UNKNOWN.value


# Fail-closed default.  Adapters must opt in before occupancy can derive remaining.
CONSERVATIVE_USAGE_SEMANTICS = ContextUsageSemantics()
# ACP (Grok headless, OpenCode, and other JSON-RPC session runtimes):
# modelUsage.contextWindow is the window; per-turn inputTokens is billing.
ACP_USAGE_SEMANTICS = ContextUsageSemantics(usage_kind=ContextUsageKind.BILLING_INPUT_USAGE.value)
GROK_ACP_USAGE_SEMANTICS = ACP_USAGE_SEMANTICS
# Codex app-server: occupancy only from remaining*/usedContext* fields.
CODEX_USAGE_SEMANTICS = ContextUsageSemantics(
    usage_kind=ContextUsageKind.BILLING_INPUT_USAGE.value
)
# Claude streaming-json: remaining only from explicit remaining* fields.
CLAUDE_USAGE_SEMANTICS = ContextUsageSemantics(
    usage_kind=ContextUsageKind.BILLING_INPUT_USAGE.value
)
# Gemini / agy stream-json: remaining only from explicit remaining* fields.
GEMINI_USAGE_SEMANTICS = ContextUsageSemantics(
    usage_kind=ContextUsageKind.BILLING_INPUT_USAGE.value
)

CONTEXT_USAGE_STALE_SECONDS = 30 * 60


def remaining_is_runtime_verified(facts: dict[str, Any]) -> bool:
    """True only for an explicit remaining reading, never an inferred estimate."""

    source = str(facts.get("remaining_source") or "")
    return bool(
        facts.get("remaining_context_tokens") is not None
        and not facts.get("remaining_derived")
        and source == "runtime_reported"
    )


def remaining_usage_is_fresh(session_data: dict[str, Any], *, now: float | None = None) -> bool:
    """Freshness of remaining, never of a capability-only window update."""

    raw = session_data.get("remaining_updated_at")
    if raw is None:
        return False
    try:
        if isinstance(raw, (int, float)):
            stamp = float(raw)
        else:
            stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else float(now)
    return (current - stamp) <= CONTEXT_USAGE_STALE_SECONDS


def consume_pending_runtime_context(
    session_data: dict[str, Any],
    *,
    semantics: ContextUsageSemantics | None = None,
) -> bool:
    """Apply already-buffered usage facts.  Never waits on the network."""

    pending = session_data.pop("_pending_runtime_context", None)
    if not isinstance(pending, dict) or not pending:
        return False
    return apply_runtime_context_to_session(session_data, pending, semantics=semantics)


def _stamp_usage(session_data: dict[str, Any], *, source: str, verified: bool) -> None:
    session_data["context_usage_revision"] = (
        int(session_data.get("context_usage_revision") or 0) + 1
    )
    session_data["context_usage_updated_at"] = datetime.now(UTC).isoformat()
    session_data["context_usage_source"] = source
    session_data["context_usage_verified"] = bool(verified)


def bump_context_capability_revision(session_data: dict[str, Any], *, source: str) -> int:
    revision = int(session_data.get("context_capability_revision") or 0) + 1
    session_data["context_capability_revision"] = revision
    _stamp_usage(session_data, source=source, verified=False)
    return revision


def bump_context_remaining_revision(
    session_data: dict[str, Any],
    *,
    source: str,
    verified: bool,
) -> int:
    revision = int(session_data.get("context_remaining_revision") or 0) + 1
    session_data["context_remaining_revision"] = revision
    session_data["remaining_updated_at"] = datetime.now(UTC).isoformat()
    _stamp_usage(session_data, source=source, verified=verified)
    return revision


def bump_context_usage_revision(
    session_data: dict[str, Any],
    *,
    source: str,
    verified: bool,
) -> int:
    """Telemetry total.  Never used to decide remaining estimate-skip."""

    _stamp_usage(session_data, source=source, verified=verified)
    return int(session_data.get("context_usage_revision") or 0)


def apply_runtime_context_to_session(
    session_data: dict[str, Any],
    facts: dict[str, Any],
    *,
    semantics: ContextUsageSemantics | None = None,
) -> bool:
    """Write runtime window/remaining/used.  Remaining revision is independent.

    A context_window-only frame advances capability revision, not remaining
    revision, so a later local estimate is still allowed.
    """

    rules = semantics or CONSERVATIVE_USAGE_SEMANTICS
    capability_updated = False
    remaining_updated = False
    if facts.get("context_window") is not None:
        session_data["effective_context_window"] = facts["context_window"]
        session_data["effective_context_window_source"] = "runtime_reported"
        capability_updated = True
    if facts.get("max_output_tokens") is not None:
        session_data["max_output_tokens"] = facts["max_output_tokens"]
        capability_updated = True
    if facts.get("remaining_context_tokens") is not None:
        session_data["remaining_context_tokens"] = facts["remaining_context_tokens"]
        source = str(facts.get("remaining_source") or "runtime_reported")
        session_data["remaining_context_source"] = source
        session_data["remaining_context_verified"] = remaining_is_runtime_verified(facts)
        remaining_updated = True
    if facts.get("used_context_tokens") is not None:
        session_data["used_context_tokens"] = facts["used_context_tokens"]
        kind = str(rules.usage_kind or ContextUsageKind.UNKNOWN.value)
        if (
            not remaining_updated
            and rules.derive_remaining_from_occupancy
            and kind == ContextUsageKind.CUMULATIVE_CONTEXT_USAGE.value
        ):
            window = safe_int(
                session_data.get("effective_context_window") or facts.get("context_window"),
                default=None,
                minimum=1,
            )
            used = safe_int(facts.get("used_context_tokens"), default=None, minimum=0)
            if window is not None and used is not None:
                session_data["remaining_context_tokens"] = max(0, int(window) - int(used))
                session_data["remaining_context_source"] = "estimated_remaining"
                session_data["remaining_context_verified"] = False
                remaining_updated = True
    if facts.get("compaction"):
        session_data["auto_compaction_detected"] = facts["compaction"]
        binding = session_data.get("runtime_binding")
        if isinstance(binding, dict):
            binding["auto_compaction_detected"] = facts["compaction"]
    if capability_updated:
        bump_context_capability_revision(session_data, source="runtime_reported")
    if remaining_updated:
        bump_context_remaining_revision(
            session_data,
            source=str(session_data.get("remaining_context_source") or "runtime_reported"),
            verified=bool(session_data.get("remaining_context_verified")),
        )
    return capability_updated or remaining_updated


def invalidate_runtime_context(
    session_data: dict[str, Any], *, reason: str
) -> None:
    """Drop leftover remaining/used after a model switch or rebind."""

    session_data["remaining_context_tokens"] = None
    session_data["remaining_context_verified"] = False
    session_data["remaining_context_source"] = "unknown"
    session_data["used_context_tokens"] = None
    session_data["session_used_tokens"] = 0
    session_data["context_usage_invalidated_reason"] = reason
    bump_context_remaining_revision(session_data, source="unknown", verified=False)
    bump_context_capability_revision(session_data, source="unknown")


def workload_context_scope_for_phase(phase: str | None) -> WorkloadContextScope:
    text = str(phase or "").strip().casefold()
    if any(token in text for token in ("material", "classif")):
        return WorkloadContextScope.PER_WINDOW
    if text in {
        "room_agent_turn",
        "room_turn",
        "persona_chat",
        "session_turn",
        "continuation",
        "room",
    }:
        return WorkloadContextScope.PERSISTENT
    if any(token in text for token in ("research", "compile", "dimension", "audit", "fusion")):
        return WorkloadContextScope.PER_REQUEST
    return WorkloadContextScope.UNKNOWN


def resolve_context_scope(adapter: Any, session: Any | None = None) -> ContextScope:
    raw = None
    extra = None
    if session is not None:
        data = getattr(session, "session_data", None)
        if isinstance(data, dict):
            raw = data.get("workload_context_scope") or data.get("context_scope")
        config = getattr(session, "config", None)
        extra = getattr(config, "extra", None) if config is not None else None
        if raw is None and isinstance(extra, dict):
            raw = extra.get("workload_context_scope") or extra.get("context_scope")
    if raw is None:
        raw = getattr(adapter, "context_scope", None)
    text = str(raw or "").strip().casefold().replace("-", "_")
    for item in ContextScope:
        if item.value == text:
            return item
    supports = getattr(adapter, "supports_persistent_conversation", None)
    if callable(supports) and session is not None:
        try:
            if bool(supports(session)):
                return ContextScope.PERSISTENT
        except Exception:
            pass
    return ContextScope.UNKNOWN


def context_usage_is_fresh(session_data: dict[str, Any], *, now: float | None = None) -> bool:
    raw = session_data.get("context_usage_updated_at")
    if raw is None:
        return False
    try:
        if isinstance(raw, (int, float)):
            stamp = float(raw)
        else:
            stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else float(now)
    return (current - stamp) <= CONTEXT_USAGE_STALE_SECONDS

MAX_OUTPUT_KEYS: tuple[str, ...] = (
    "maxOutputTokens",
    "max_output_tokens",
    "maxOutput",
    "max_output",
    "outputTokenLimit",
    "output_token_limit",
)

COMPACTION_MARKERS: tuple[str, ...] = (
    "auto_compact",
    "autocompact",
    "auto-compact",
    "context_summary",
    "contextsummary",
    "compaction",
    "compacted",
    "truncation",
    "truncated",
    "context_window_compression",
    "contextwindowcompression",
)

_NESTED_USAGE_KEYS: tuple[str, ...] = (
    "modelUsage",
    "model_usage",
    "usage",
    "tokenUsage",
    "token_usage",
    "contextUsage",
    "context_usage",
    "contextWindowMetadata",
    "context_window_metadata",
    "_meta",
    "meta",
    "sampling",
    "samplingConfig",
    "capabilities",
    "limits",
    "window",
    "params",
    "update",
    "result",
    "session",
)


def _positive(value: Any) -> int | None:
    return safe_int(value, default=None, minimum=1)


def extract_first_int(payload: Any, keys: tuple[str, ...], *, depth: int = 0) -> int | None:
    """Return the first positive int for ``keys`` in a nested mapping."""

    if depth > 6 or payload is None:
        return None
    if isinstance(payload, (int, float, str)):
        return _positive(payload)
    if isinstance(payload, list):
        for item in payload[:32]:
            found = extract_first_int(item, keys, depth=depth + 1)
            if found is not None:
                return found
        return None
    if not isinstance(payload, dict):
        return None
    for key in keys:
        if payload.get(key) is not None:
            found = _positive(payload.get(key))
            if found is not None:
                return found
    for nested_key in _NESTED_USAGE_KEYS:
        nested = payload.get(nested_key)
        if nested is None:
            continue
        if isinstance(nested, dict) and nested_key in {"modelUsage", "model_usage"}:
            # Grok Build: modelUsage.<model>.contextWindow
            for item in nested.values():
                found = extract_first_int(item, keys, depth=depth + 1)
                if found is not None:
                    return found
            continue
        found = extract_first_int(nested, keys, depth=depth + 1)
        if found is not None:
            return found
    session = payload.get("session")
    if isinstance(session, dict):
        found = extract_first_int(session, keys, depth=depth + 1)
        if found is not None:
            return found
    return None


def extract_context_window(payload: Any) -> int | None:
    return extract_first_int(payload, CONTEXT_WINDOW_KEYS)


def extract_remaining_context(payload: Any) -> int | None:
    return extract_first_int(payload, REMAINING_CONTEXT_KEYS)


def extract_used_context(payload: Any, *, keys: tuple[str, ...] | None = None) -> int | None:
    return extract_first_int(payload, keys or OCCUPANCY_CONTEXT_KEYS)


def extract_max_output_tokens(payload: Any) -> int | None:
    return extract_first_int(payload, MAX_OUTPUT_KEYS)


def detect_context_compaction(payload: Any, *, depth: int = 0) -> str | None:
    """Return a compaction/truncation marker if the runtime reported one."""

    if depth > 5 or payload is None:
        return None
    if isinstance(payload, str):
        lowered = payload.casefold().replace(" ", "").replace("-", "_")
        for marker in COMPACTION_MARKERS:
            if marker.replace("-", "_") in lowered:
                return marker
        return None
    if isinstance(payload, list):
        for item in payload[:24]:
            found = detect_context_compaction(item, depth=depth + 1)
            if found:
                return found
        return None
    if not isinstance(payload, dict):
        return None
    for key, value in list(payload.items())[:48]:
        key_text = str(key or "").casefold().replace("-", "_")
        for marker in COMPACTION_MARKERS:
            if marker.replace("-", "_") in key_text and value not in {None, False, ""}:
                return marker
        found = detect_context_compaction(value, depth=depth + 1)
        if found:
            return found
    return None


def extract_runtime_context_facts(
    payload: Any,
    *,
    semantics: ContextUsageSemantics | None = None,
) -> dict[str, Any]:
    """Collect context numbers the runtime volunteered under adapter semantics."""

    rules = semantics or CONSERVATIVE_USAGE_SEMANTICS
    window = extract_context_window(payload)
    remaining = extract_first_int(payload, rules.remaining_keys)
    occupancy = (
        extract_first_int(payload, rules.occupancy_keys) if rules.occupancy_keys else None
    )
    remaining_source = "runtime_reported" if remaining is not None else None
    remaining_derived = False
    if (
        remaining is None
        and rules.derive_remaining_from_occupancy
        and str(rules.usage_kind) == ContextUsageKind.CUMULATIVE_CONTEXT_USAGE.value
        and window is not None
        and occupancy is not None
    ):
        remaining = max(0, int(window) - int(occupancy))
        remaining_source = "estimated_remaining"
        remaining_derived = True
    return {
        "context_window": window,
        "remaining_context_tokens": remaining,
        "used_context_tokens": occupancy,
        "max_output_tokens": extract_max_output_tokens(payload),
        "compaction": detect_context_compaction(payload),
        "remaining_source": remaining_source,
        "remaining_derived": remaining_derived,
    }
