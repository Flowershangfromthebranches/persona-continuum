"""Common response collection and diagnostics for every Agent transport.

The adapters are deliberately small protocol bridges.  This module is the
single application-facing contract that turns their event streams into a
bounded response, while retaining enough telemetry to explain an empty turn
without leaking prompts, credentials, or private material.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any

from persona_continuum.agent.models import AgentEvent, AgentEventType
from persona_continuum.numeric import safe_acp_stream_limit, safe_int

_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|secret|authorization|password)([=: ]+)\S+"),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{12,}|xox[baprs]-[A-Za-z0-9-]{12,})\b"),
)


def sanitize_diagnostic(value: object, *, limit: int = 2000) -> str:
    """Redact common secret forms and cap diagnostic size."""

    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(
            lambda match: f"{match.group(1) if match.lastindex else 'secret'}=<redacted>", text
        )
    return text[-limit:]


TOOL_RESULT_INLINE_LIMIT_CHARS = 12_000
TOOL_RESULT_PREVIEW_CHARS = 1_200
DEFAULT_ACP_STREAM_LIMIT_BYTES = 16 * 1024 * 1024


def _safe_tool_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    except Exception:
        return str(value)


def _collect_evidence_refs(value: Any, *, limit: int = 256) -> list[str]:
    """Collect identifiers without retaining the evidence body in diagnostics."""

    refs: list[str] = []

    def visit(item: Any) -> None:
        if len(refs) >= limit:
            return
        if isinstance(item, dict):
            for key, child in item.items():
                key_text = str(key).casefold()
                if key_text in {
                    "id",
                    "source_id",
                    "source_ids",
                    "evidence_id",
                    "evidence_ids",
                    "memory_id",
                    "memory_ids",
                }:
                    if isinstance(child, (list, tuple, set)):
                        for ref in child:
                            if ref and len(refs) < limit:
                                refs.append(sanitize_diagnostic(ref, limit=200))
                    elif child:
                        refs.append(sanitize_diagnostic(child, limit=200))
                elif isinstance(child, (dict, list, tuple)):
                    visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return list(dict.fromkeys(refs))[:limit]


def compact_tool_result(
    value: Any,
    *,
    tool_name: str | None = None,
    artifact_ref: str | None = None,
    inline_limit_chars: int = TOOL_RESULT_INLINE_LIMIT_CHARS,
) -> Any:
    """Keep large internal tool results out of events, sockets, and UI.

    The complete value remains available to the adapter's model request and
    the authoritative Persona/Evidence store. Only the application-facing
    observation is compacted to a reference, preview, size, and source IDs.
    """

    serialized = _safe_tool_json(value)
    if len(serialized) <= inline_limit_chars:
        return value
    safe_tool_name = sanitize_diagnostic(tool_name, limit=120) if tool_name else None
    safe_artifact_ref = sanitize_diagnostic(artifact_ref, limit=200) if artifact_ref else None
    fallback_ref = (
        f"tool-result:{safe_tool_name or 'unknown'}:"
        f"{hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:16]}"
    )
    return {
        "artifact_ref": safe_artifact_ref or fallback_ref,
        "preview": sanitize_diagnostic(serialized, limit=TOOL_RESULT_PREVIEW_CHARS),
        "character_count": len(serialized),
        "evidence_refs": _collect_evidence_refs(value),
        "tool_name": safe_tool_name,
    }


def retain_tool_artifact(
    session_data: dict[str, Any], compacted: Any, raw_value: Any, *, max_items: int = 64
) -> None:
    """Keep a bounded live-turn lookup for a compacted tool result.

    Durable evidence remains in Persona Continuum's evidence store. This
    private map is the short-lived bridge for tools that return an already
    materialized result without a durable ID; it is never copied into an
    ``AgentEvent``, diagnostic, WebSocket payload, or UI model.
    """

    if not isinstance(compacted, dict):
        return
    artifact_ref = str(compacted.get("artifact_ref") or "").strip()
    if not artifact_ref:
        return
    artifacts = session_data.setdefault("_agent_tool_artifacts", {})
    if not isinstance(artifacts, dict):
        artifacts = {}
        session_data["_agent_tool_artifacts"] = artifacts
    bounded_limit = max(1, safe_int(max_items, default=64, minimum=1, maximum=256) or 64)
    if artifact_ref not in artifacts and len(artifacts) >= bounded_limit:
        oldest = next(iter(artifacts), None)
        if oldest is not None:
            artifacts.pop(oldest, None)
    artifacts[artifact_ref] = raw_value


def sanitize_command(argv: Iterable[object]) -> list[str]:
    """Return command shape without prompt values or credential material."""

    values = [str(item) for item in argv]
    result: list[str] = []
    for index, value in enumerate(values):
        if index and (values[index - 1] in {"-p", "--prompt", "--message"}):
            result.append("<prompt>")
        elif any(
            marker in value.casefold() for marker in ("key=", "token=", "secret=", "password=")
        ):
            result.append("<credential>")
        else:
            result.append(value)
    return result


class AgentRuntimeError(RuntimeError):
    """Base typed error that may safely cross the application boundary."""

    code = "AGENT_RUNTIME_ERROR"
    retriable = False
    response: AgentResponse | None = None

    def __init__(
        self,
        message: str,
        *,
        phase: str | None = None,
        diagnostics: dict[str, Any] | None = None,
        retriable: bool | None = None,
    ) -> None:
        self.phase = phase
        self.diagnostics = dict(diagnostics or {})
        if retriable is not None:
            self.retriable = bool(retriable)
        super().__init__(message)

    def as_failure(self) -> dict[str, Any]:
        failure = {
            "code": self.code,
            "message": str(self),
            "phase": self.phase,
            "retriable": bool(self.retriable),
            "diagnostics": self.diagnostics,
        }
        for key in (
            "protocol",
            "configured_limit_bytes",
            "observed_frame_bytes",
            "frame_bytes",
            "frame_type",
            "event_type",
            "tool_name",
            "phase",
            "adapter",
            "structured_output_mode",
            "output_streaming_mode",
            "first_response_timeout_seconds",
            "raw_chars",
            "parser_failure",
            "schema_failure",
            "repair_attempted",
            "repair_attempts",
            "idle_timeout_seconds",
            "hard_timeout_seconds",
            "last_activity_at",
            "requested_model",
            "effective_model",
            "requested_reasoning",
            "effective_reasoning",
            "runtime_binding_snapshot",
            "binding_status",
            "verification_method",
            "reasoning_verified",
            "model_verified",
        ):
            if key in self.diagnostics:
                failure[key] = self.diagnostics[key]
        return failure


class RuntimeUnavailableError(AgentRuntimeError):
    code = "RUNTIME_UNAVAILABLE"
    retriable = True


class AgentTransportError(AgentRuntimeError):
    code = "AGENT_TRANSPORT_ERROR"
    retriable = True


class AgentProtocolError(AgentRuntimeError):
    code = "AGENT_PROTOCOL_ERROR"
    retriable = False


class SystemPromptDroppedError(AgentProtocolError):
    """The adapter reported that a canonical system prompt was not carried."""

    code = "SYSTEM_PROMPT_DROPPED"


class AgentProcessExitError(AgentTransportError):
    code = "AGENT_PROCESS_EXITED"


class AgentOutputError(AgentRuntimeError):
    code = "AGENT_OUTPUT_ERROR"
    retriable = True


class AgentPermissionBlockedError(AgentRuntimeError):
    """A headless Agent could not ask the user to approve a tool call."""

    code = "AGENT_PERMISSION_BLOCKED"
    retriable = False


class AgentStructuredOutputError(AgentOutputError):
    code = "AGENT_STRUCTURED_OUTPUT_INVALID"


class AgentTimeoutError(AgentTransportError):
    code = "AGENT_TURN_TIMEOUT"


class AgentIdleTimeoutError(AgentTimeoutError):
    code = "AGENT_IDLE_TIMEOUT"


class AgentHardTimeoutError(AgentTimeoutError):
    code = "AGENT_HARD_TIMEOUT"


class ModelBindingUnverifiedError(AgentRuntimeError):
    code = "MODEL_BINDING_UNVERIFIED"
    retriable = False


class ReasoningBindingUnverifiedError(AgentRuntimeError):
    code = "REASONING_BINDING_UNVERIFIED"
    retriable = False


class ReasoningBindingRejectedError(AgentRuntimeError):
    code = "REASONING_BINDING_REJECTED"
    retriable = False


class ContextBudgetExceededError(AgentRuntimeError):
    code = "CONTEXT_BUDGET_EXCEEDED"
    retriable = False


class PromptTransportLimitExceededError(AgentTransportError):
    """A prompt exceeded the Adapter/Protocol transport budget before dispatch.

    This is a transport failure, not a model-capability failure: the model may
    hold 1M tokens while an ARGV-only CLI can safely carry far less.  Failing
    loudly here replaces the silent hang a giant prompt used to cause.
    """

    code = "PROMPT_TRANSPORT_LIMIT_EXCEEDED"
    retriable = True


class ACPFrameTooLargeError(AgentTransportError):
    """An ACP JSONL frame exceeded the bounded subprocess stream limit."""

    code = "AGENT_TRANSPORT_FRAME_TOO_LARGE"
    retriable = True

    def __init__(
        self,
        *,
        configured_limit_bytes: int,
        observed_frame_bytes: int | None = None,
        frame_type: str = "unknown",
        event_type: str = "unknown",
        tool_name: str | None = None,
        phase: str = "session_update",
    ) -> None:
        self.configured_limit_bytes = safe_acp_stream_limit(configured_limit_bytes)
        self.observed_frame_bytes = (
            safe_int(observed_frame_bytes, default=None, minimum=0)
            if observed_frame_bytes is not None
            else None
        )
        self.frame_type = str(frame_type or "unknown")
        self.event_type = str(event_type or "unknown")
        self.tool_name = str(tool_name) if tool_name else None
        diagnostics: dict[str, Any] = {
            "protocol": "acp",
            "configured_limit_bytes": self.configured_limit_bytes,
            "frame_type": self.frame_type,
            "event_type": self.event_type,
        }
        if self.observed_frame_bytes is not None:
            diagnostics["observed_frame_bytes"] = self.observed_frame_bytes
        if self.tool_name:
            diagnostics["tool_name"] = self.tool_name
        super().__init__(
            "ACP JSON frame exceeded the configured stream limit",
            phase=phase,
            diagnostics=diagnostics,
            retriable=True,
        )


class AgentSessionStartError(AgentTransportError):
    code = "AGENT_SESSION_START_FAILED"


class AgentProtocolOutputError(AgentOutputError):
    code = "AGENT_PROTOCOL_RETURNED_NO_FINAL_TEXT"


class AgentCancelledError(AgentRuntimeError):
    code = "AGENT_CANCELLED"


class AgentReportedError(AgentRuntimeError):
    """Preserve an adapter-reported failure code not known by this client version."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        phase: str | None = None,
        diagnostics: dict[str, Any] | None = None,
        retriable: bool | None = None,
    ) -> None:
        self.code = str(code or AgentRuntimeError.code)
        super().__init__(
            message,
            phase=phase,
            diagnostics=diagnostics,
            retriable=retriable,
        )


RETRIABLE_REPORTED_FAILURE_CODES = {
    # Plain/streaming CLI adapters report this precise wire code instead of
    # AgentOutputError.code.  Preserve the precise code while retaining the
    # retry policy of the typed output-error family.
    "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT",
}


def agent_error_event(exc: BaseException, *, protocol: str) -> AgentEvent:
    """Convert an adapter exception into one typed, privacy-safe error event."""

    if isinstance(exc, AgentRuntimeError):
        typed = exc
    elif isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.casefold():
        # A low-level timeout without phase information is treated as an idle
        # timeout. Callers with a hard deadline use AgentHardTimeoutError
        # explicitly, so no new event is emitted with the legacy blanket code.
        typed = AgentIdleTimeoutError(
            "Agent transport timed out",
            phase="agent_turn",
            diagnostics={"protocol": protocol, "exception_type": type(exc).__name__},
        )
    else:
        typed = AgentTransportError(
            "Agent transport failed",
            phase="agent_turn",
            diagnostics={"protocol": protocol, "exception_type": type(exc).__name__},
        )
    diagnostics = dict(typed.diagnostics)
    metadata: dict[str, Any] = {
        "protocol": protocol,
        "failure_code": typed.code,
        "failure": typed.as_failure(),
        "exception_type": type(exc).__name__,
    }
    for key in (
        "configured_limit_bytes",
        "observed_frame_bytes",
        "frame_bytes",
        "frame_type",
        "event_type",
        "tool_name",
        "idle_timeout_seconds",
        "hard_timeout_seconds",
        "last_activity_at",
    ):
        if key in diagnostics:
            metadata[key] = diagnostics[key]
    return AgentEvent(type=AgentEventType.ERROR, error=typed.code, metadata=metadata)


@dataclass(slots=True)
class AgentResponse:
    """Collected response plus protocol and observability metadata."""

    text: str = ""
    thinking: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    event_counts: Counter[str] = field(default_factory=Counter)
    done_received: bool = False
    last_event_type: str | None = None
    protocol: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    sanitized_stderr: str = ""
    raw_diagnostics: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float | None = None

    @property
    def duration_ms(self) -> int:
        end = self.ended_at if self.ended_at is not None else time.monotonic()
        return max(0, int((end - self.started_at) * 1000))

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def has_output(self) -> bool:
        return bool(self.text.strip())

    def audit(self, *, call_id: str, job_id: str | None, phase: str) -> dict[str, Any]:
        return {
            "call_id": call_id,
            "job_id": job_id,
            "phase": phase,
            "protocol": self.protocol,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "event_counts": dict(self.event_counts),
            "chunks": int(self.event_counts.get(AgentEventType.CHUNK.value, 0)),
            "chars": self.chars,
            "done_received": self.done_received,
            "last_event_type": self.last_event_type,
            "usage": dict(self.usage),
            "runtime_diagnostics": {
                key: value
                for key, value in self.raw_diagnostics.items()
                if key
                in {
                    "adapter",
                    "prompt_mode",
                    "structured_output_mode",
                    "requested_model",
                    "effective_model",
                    "requested_reasoning",
                    "effective_reasoning",
                    "runtime_binding_snapshot",
                    "input_token_estimate",
                    "idle_timeout_seconds",
                    "hard_timeout_seconds",
                    "first_response_timeout_seconds",
                    "last_activity_at",
                    "output_streaming_mode",
                    "activity_tracker",
                    "process_alive",
                }
            },
            "status": "ok" if self.has_output else "empty",
            "failure_code": None,
        }


class AgentResponseCollector:
    """Consume an adapter stream exactly once and produce a safe response."""

    def __init__(
        self,
        *,
        protocol: str | None = None,
        stderr: str = "",
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        self.response = AgentResponse(
            protocol=protocol,
            sanitized_stderr=sanitize_diagnostic(stderr),
            raw_diagnostics={
                str(k): sanitize_diagnostic(v) if isinstance(v, str) else v
                for k, v in (diagnostics or {}).items()
            },
        )
        self._parts: list[str] = []

    async def collect(self, events: AsyncIterator[AgentEvent]) -> AgentResponse:
        async for event in events:
            self.add(event)
        self.response.text = "".join(self._parts).strip()
        self.response.ended_at = time.monotonic()
        return self.response

    def add(self, event: AgentEvent) -> None:
        event_type = event.type.value if isinstance(event.type, AgentEventType) else str(event.type)
        self.response.event_counts[event_type] += 1
        self.response.last_event_type = event_type
        metadata = dict(event.metadata or {})
        if metadata.get("protocol"):
            self.response.protocol = str(metadata["protocol"])
        if metadata.get("usage") and isinstance(metadata["usage"], dict):
            self.response.usage.update(metadata["usage"])
        for key in (
            "stderr_tail",
            "failure_code",
            "returncode",
            "command_shape",
            "fallback",
            "failure",
            "configured_limit_bytes",
            "observed_frame_bytes",
            "frame_bytes",
            "frame_type",
            "event_type",
            "tool_name",
            "idle_timeout_seconds",
            "hard_timeout_seconds",
            "first_response_timeout_seconds",
            "last_activity_at",
            "output_streaming_mode",
            "activity_tracker",
            "process_alive",
            "last_activity_age_seconds",
        ):
            if key in metadata:
                value = metadata[key]
                self.response.raw_diagnostics[key] = (
                    sanitize_diagnostic(value) if isinstance(value, str) else value
                )
        if event_type in {AgentEventType.CHUNK.value, AgentEventType.DONE.value}:
            if event_type == AgentEventType.CHUNK.value and metadata.get("replace_response"):
                self._parts.clear()
            if event.content:
                current = "".join(self._parts)
                if event_type == AgentEventType.DONE.value:
                    # DONE may be a terminal marker, the complete response,
                    # or only a final delta. Normalize all three shapes.
                    if not current:
                        self._parts.append(event.content)
                    elif event.content == current or current.endswith(event.content):
                        pass
                    elif event.content.startswith(current):
                        self._parts = [event.content]
                    else:
                        self._parts.append(event.content)
                else:
                    self._parts.append(event.content)
            if event_type == AgentEventType.DONE.value:
                self.response.done_received = True
        elif event_type == AgentEventType.THINKING.value:
            self.response.thinking += event.thinking or event.content
        elif event_type == AgentEventType.TOOL_CALL.value:
            self.response.tool_calls.append(
                {
                    "id": event.tool_call_id,
                    "name": event.tool_name,
                    "arguments": event.tool_arguments,
                }
            )
        elif event_type == AgentEventType.TOOL_RESULT.value:
            compact = compact_tool_result(
                event.tool_result,
                tool_name=event.tool_name,
                artifact_ref=(metadata.get("artifact_ref") or None),
            )
            self.response.tool_results.append(
                {
                    "id": event.tool_call_id,
                    "name": event.tool_name,
                    "result": compact,
                }
            )
        elif event_type == AgentEventType.ERROR.value:
            self.response.raw_diagnostics["last_error"] = sanitize_diagnostic(
                event.error or event.content
            )

    def require_text(self, *, phase: str, job_id: str | None = None) -> str:
        self.response.text = "".join(self._parts).strip()
        self.response.ended_at = self.response.ended_at or time.monotonic()
        if self.response.event_counts.get(AgentEventType.ERROR.value, 0):
            failure_payload = self.response.raw_diagnostics.get("failure")
            failure_code = str(
                self.response.raw_diagnostics.get("failure_code")
                or (failure_payload.get("code") if isinstance(failure_payload, dict) else "")
                or ""
            )
            typed_diagnostics = {
                "job_id": job_id,
                "event_counts": dict(self.response.event_counts),
                "last_event_type": self.response.last_event_type,
                "protocol": self.response.protocol,
                "stderr_tail": self.response.sanitized_stderr,
                **self.response.raw_diagnostics,
            }
            if failure_code == ACPFrameTooLargeError.code:
                raw_failure_diagnostics = (
                    failure_payload.get("diagnostics")
                    if isinstance(failure_payload, dict)
                    else None
                )
                failure_diagnostics = (
                    dict(raw_failure_diagnostics)
                    if isinstance(raw_failure_diagnostics, dict)
                    else {}
                )
                failure_phase = str(
                    (
                        failure_payload.get("phase")
                        if isinstance(failure_payload, dict)
                        else None
                    )
                    or failure_diagnostics.get("phase")
                    or phase
                )
                configured_limit_value: Any = (
                    self.response.raw_diagnostics.get("configured_limit_bytes")
                    or (
                        failure_payload.get("configured_limit_bytes")
                        if isinstance(failure_payload, dict)
                        else None
                    )
                    or failure_diagnostics.get("configured_limit_bytes")
                    or DEFAULT_ACP_STREAM_LIMIT_BYTES
                )
                observed_frame_value: Any = (
                    self.response.raw_diagnostics.get("observed_frame_bytes")
                    if self.response.raw_diagnostics.get("observed_frame_bytes") is not None
                    else (
                        failure_payload.get("observed_frame_bytes")
                        if isinstance(failure_payload, dict)
                        and failure_payload.get("observed_frame_bytes") is not None
                        else failure_diagnostics.get("observed_frame_bytes")
                    )
                )
                frame_error = ACPFrameTooLargeError(
                    configured_limit_bytes=safe_acp_stream_limit(configured_limit_value),
                    observed_frame_bytes=(
                        safe_int(observed_frame_value, default=None, minimum=0)
                        if observed_frame_value is not None
                        else None
                    ),
                    frame_type=str(
                        self.response.raw_diagnostics.get("frame_type")
                        or (
                            failure_payload.get("frame_type")
                            if isinstance(failure_payload, dict)
                            else None
                        )
                        or failure_diagnostics.get("frame_type")
                        or "unknown"
                    ),
                    event_type=str(
                        self.response.raw_diagnostics.get("event_type")
                        or (
                            failure_payload.get("event_type")
                            if isinstance(failure_payload, dict)
                            else None
                        )
                        or failure_diagnostics.get("event_type")
                        or "unknown"
                    ),
                    tool_name=(
                        self.response.raw_diagnostics.get("tool_name")
                        or (
                            failure_payload.get("tool_name")
                            if isinstance(failure_payload, dict)
                            else None
                        )
                        or failure_diagnostics.get("tool_name")
                    ),
                    phase=failure_phase,
                )
                frame_error.diagnostics.update(
                    {
                        key: typed_diagnostics[key]
                        for key in ("job_id", "event_counts", "last_event_type", "stderr_tail")
                        if typed_diagnostics.get(key) is not None
                    }
                )
                frame_error.response = self.response
                raise frame_error
            if failure_code == AgentProtocolError.code:
                failure_phase = str(
                    (failure_payload.get("phase") if isinstance(failure_payload, dict) else None)
                    or phase
                )
                protocol_error = AgentProtocolError(
                    str(self.response.raw_diagnostics.get("last_error") or "agent_protocol_error"),
                    phase=failure_phase,
                    diagnostics=typed_diagnostics,
                )
                protocol_error.response = self.response
                raise protocol_error
            if failure_code == AgentProcessExitError.code:
                failure_phase = str(
                    (failure_payload.get("phase") if isinstance(failure_payload, dict) else None)
                    or phase
                )
                process_error = AgentProcessExitError(
                    str(self.response.raw_diagnostics.get("last_error") or "agent_process_exited"),
                    phase=failure_phase,
                    diagnostics=typed_diagnostics,
                )
                process_error.response = self.response
                raise process_error
            if failure_code in {
                AgentTimeoutError.code,
                AgentIdleTimeoutError.code,
                AgentHardTimeoutError.code,
            }:
                failure_phase = str(
                    (failure_payload.get("phase") if isinstance(failure_payload, dict) else None)
                    or phase
                )
                timeout_type: type[AgentTimeoutError] = {
                    AgentIdleTimeoutError.code: AgentIdleTimeoutError,
                    AgentHardTimeoutError.code: AgentHardTimeoutError,
                }.get(failure_code, AgentTimeoutError)
                timeout_error = timeout_type(
                    str(self.response.raw_diagnostics.get("last_error") or "agent_turn_timeout"),
                    phase=failure_phase,
                    diagnostics=typed_diagnostics,
                )
                timeout_error.response = self.response
                raise timeout_error
            preserved_error_types: dict[str, type[AgentRuntimeError]] = {
                RuntimeUnavailableError.code: RuntimeUnavailableError,
                AgentTransportError.code: AgentTransportError,
                AgentPermissionBlockedError.code: AgentPermissionBlockedError,
                SystemPromptDroppedError.code: SystemPromptDroppedError,
                AgentOutputError.code: AgentOutputError,
                AgentStructuredOutputError.code: AgentStructuredOutputError,
                ModelBindingUnverifiedError.code: ModelBindingUnverifiedError,
                ReasoningBindingUnverifiedError.code: ReasoningBindingUnverifiedError,
                ReasoningBindingRejectedError.code: ReasoningBindingRejectedError,
                ContextBudgetExceededError.code: ContextBudgetExceededError,
                PromptTransportLimitExceededError.code: PromptTransportLimitExceededError,
                AgentSessionStartError.code: AgentSessionStartError,
                AgentProtocolOutputError.code: AgentProtocolOutputError,
                AgentCancelledError.code: AgentCancelledError,
            }
            if failure_code:
                failure_phase = str(
                    (failure_payload.get("phase") if isinstance(failure_payload, dict) else None)
                    or phase
                )
                failure_message = str(
                    (failure_payload.get("message") if isinstance(failure_payload, dict) else None)
                    or self.response.raw_diagnostics.get("last_error")
                    or failure_code
                )
                failure_retriable = (
                    bool(failure_payload.get("retriable"))
                    if isinstance(failure_payload, dict)
                    and failure_payload.get("retriable") is not None
                    else (
                        True
                        if failure_code in RETRIABLE_REPORTED_FAILURE_CODES
                        else None
                    )
                )
                preserved_type = preserved_error_types.get(failure_code)
                if preserved_type is not None:
                    preserved_error = preserved_type(
                        failure_message,
                        phase=failure_phase,
                        diagnostics=typed_diagnostics,
                        retriable=failure_retriable,
                    )
                else:
                    preserved_error = AgentReportedError(
                        failure_message,
                        code=failure_code,
                        phase=failure_phase,
                        diagnostics=typed_diagnostics,
                        retriable=failure_retriable,
                    )
                preserved_error.response = self.response
                raise preserved_error
            error_type = (
                AgentOutputError
                if self.response.raw_diagnostics.get("failure_code")
                in {
                    "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT",
                    "AGENT_PROTOCOL_RETURNED_NO_FINAL_TEXT",
                }
                else AgentTransportError
            )
            runtime_error = error_type(
                str(self.response.raw_diagnostics.get("last_error") or "agent_stream_error"),
                phase=phase,
                diagnostics={
                    **typed_diagnostics,
                },
            )
            runtime_error.response = self.response
            raise runtime_error
        if not self.response.has_output:
            code = str(
                self.response.raw_diagnostics.get("failure_code")
                or (
                    "AGENT_PROTOCOL_RETURNED_NO_FINAL_TEXT"
                    if self.response.done_received
                    and self.response.protocol not in {"plain_cli", "cli"}
                    else "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT"
                )
            )
            error_type = (
                AgentProtocolOutputError
                if code == "AGENT_PROTOCOL_RETURNED_NO_FINAL_TEXT"
                else AgentOutputError
            )
            output_error = error_type(
                str(
                    self.response.raw_diagnostics.get("last_error")
                    or "model session ended without content: no final text was captured"
                ),
                phase=phase,
                diagnostics={
                    "code": code,
                    "job_id": job_id,
                    "event_counts": dict(self.response.event_counts),
                    "last_event_type": self.response.last_event_type,
                    "protocol": self.response.protocol,
                    "stderr_tail": self.response.sanitized_stderr,
                    "raw_diagnostics": dict(self.response.raw_diagnostics),
                },
            )
            output_error.response = self.response
            raise output_error
        return self.response.text


__all__ = [
    "AgentResponse",
    "AgentResponseCollector",
    "AgentRuntimeError",
    "RuntimeUnavailableError",
    "AgentTransportError",
    "AgentProtocolError",
    "SystemPromptDroppedError",
    "AgentProcessExitError",
    "AgentOutputError",
    "AgentPermissionBlockedError",
    "AgentStructuredOutputError",
    "AgentTimeoutError",
    "AgentIdleTimeoutError",
    "AgentHardTimeoutError",
    "ModelBindingUnverifiedError",
    "ReasoningBindingUnverifiedError",
    "ReasoningBindingRejectedError",
    "ContextBudgetExceededError",
    "PromptTransportLimitExceededError",
    "AgentSessionStartError",
    "AgentProtocolOutputError",
    "AgentCancelledError",
    "AgentReportedError",
    "agent_error_event",
    "ACPFrameTooLargeError",
    "compact_tool_result",
    "retain_tool_artifact",
    "sanitize_command",
    "sanitize_diagnostic",
]
