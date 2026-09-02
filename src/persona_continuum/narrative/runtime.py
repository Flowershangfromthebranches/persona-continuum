"""Narrative Agent execution policy and sanitized failure contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from persona_continuum.agent.response_collector import sanitize_diagnostic
from persona_continuum.domain.narrative import GenerationMode

NARRATIVE_AGENT_GENERATION_FAILED = "NARRATIVE_AGENT_GENERATION_FAILED"
NARRATIVE_AGENT_TIMEOUT = "NARRATIVE_AGENT_TIMEOUT"
NARRATIVE_AGENT_STRUCTURED_OUTPUT_INVALID = "NARRATIVE_AGENT_STRUCTURED_OUTPUT_INVALID"
NARRATIVE_AGENT_RUNTIME_UNAVAILABLE = "NARRATIVE_AGENT_RUNTIME_UNAVAILABLE"
# Production/shooting pipeline gates (Task B): production packages are built
# from canon episode versions unless the caller explicitly asks for a preview;
# the shooting pipeline never treats a preview package as a final source.
NARRATIVE_PRODUCTION_CANON_REQUIRED = "NARRATIVE_PRODUCTION_CANON_REQUIRED"
SHOOTING_SOURCE_PREVIEW_NOT_ALLOWED = "SHOOTING_SOURCE_PREVIEW_NOT_ALLOWED"
# Video production guide (copy-ready layer): a clip's location must resolve
# to a real location bible / location list entry before a copy-ready prompt
# is built; the guide layer never falls back to a generic location anchor.
SHOOTING_LOCATION_CONTEXT_MISSING = "SHOOTING_LOCATION_CONTEXT_MISSING"
# Video production guide source gate: the executable guide may only be built
# from a prompt package that is complete (status ready) and fresh (not
# stale); a draft/failed/stale package must be recompiled first.
VIDEO_GUIDE_SOURCE_NOT_READY = "VIDEO_GUIDE_SOURCE_NOT_READY"


class NarrativeAgentError(RuntimeError):
    """A fail-closed narrative generation error safe for API/UI exposure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str,
        runtime: dict[str, Any] | None = None,
        effective_model: str | None = None,
        retryable: bool = False,
        original_error: BaseException | None = None,
    ) -> None:
        safe_runtime = normalize_runtime(runtime)
        safe_message = sanitize_diagnostic(message, limit=2000)
        self.code = code
        self.stage = stage
        self.agent = safe_runtime.get("agent_id")
        self.requested_model = safe_runtime.get("model_id")
        self.effective_model = effective_model or self.requested_model
        self.reasoning = safe_runtime.get("reasoning_effort")
        self.retryable = retryable
        self.original_error_class = (
            type(original_error).__name__ if original_error is not None else None
        )
        self.details = {
            "code": code,
            "message": safe_message,
            "stage": stage,
            "agent": self.agent,
            "requested_model": self.requested_model,
            "effective_model": self.effective_model,
            "reasoning": self.reasoning,
            "retryable": retryable,
            "original_error_class": self.original_error_class,
        }
        super().__init__(f"{code}: {safe_message}")

    def to_dict(self) -> dict[str, Any]:
        return dict(self.details)


def normalize_runtime(runtime: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(runtime or {})
    runtime_source = str(raw.get("runtime_source") or raw.get("source") or "").strip()
    if runtime_source not in {"local_cli", "api"}:
        runtime_source = ""
    return {
        "runtime_source": runtime_source,
        "agent_id": raw.get("agent_id") or raw.get("agent") or "",
        "model_id": raw.get("model_id") or raw.get("model") or "default",
        "reasoning_effort": raw.get("reasoning_effort") or raw.get("reasoning") or "none",
        "auth_profile_id": raw.get("auth_profile_id") or raw.get("credential_id"),
    }


def resolve_generation_mode(
    requested: GenerationMode | str | None, runtime: dict[str, Any] | None
) -> GenerationMode:
    try:
        mode = GenerationMode(requested or GenerationMode.AUTO)
    except ValueError as exc:
        raise ValueError(f"Unknown narrative generation mode: {requested}") from exc
    if mode == GenerationMode.AUTO:
        return (
            GenerationMode.AGENT
            if normalize_runtime(runtime).get("agent_id")
            else GenerationMode.DETERMINISTIC
        )
    return mode


def new_runtime_trace(
    *, stage: str, mode: GenerationMode, runtime: dict[str, Any] | None
) -> dict[str, Any]:
    safe = normalize_runtime(runtime)
    return {
        "execution_id": f"narrative:{stage}:{datetime.now(UTC).timestamp():.6f}",
        "stage": stage,
        "generation_mode": mode.value,
        "agent": safe.get("agent_id") or None,
        "runtime_source": safe.get("runtime_source") or None,
        "requested_model": safe.get("model_id") or None,
        "effective_model": None,
        "reasoning": safe.get("reasoning_effort") or None,
        "started_at": datetime.now(UTC).isoformat(),
        "completed_at": None,
        "duration_ms": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "retry_count": 0,
        "included_sections": [],
        "omitted_sections": [],
        "truncation": False,
        "context_cache_hit": False,
        "status": "running",
        "failure": None,
    }
