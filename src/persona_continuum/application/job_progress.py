"""Durable progress and failure contracts shared by background jobs."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from persona_continuum.numeric import safe_int


def progress_now() -> str:
    return datetime.now(UTC).isoformat()


class JobVisibility(StrEnum):
    USER = "user"
    INTERNAL = "internal"


class WorkerState(StrEnum):
    """Durable state of the in-process worker, separate from job status."""

    STARTING = "starting"
    RUNNING = "running"
    WAITING_AGENT = "waiting_agent"
    WAITING_IO = "waiting_io"
    PAUSED = "paused"
    FINISHED = "finished"
    FAILED = "failed"
    LOST = "lost"


class JobNotTerminalError(RuntimeError):
    code = "job_is_not_terminal"

    def __init__(self, job_id: str, status: str) -> None:
        self.job_id = str(job_id)
        self.status = str(status)
        super().__init__(self.code)


class JobFailure(BaseModel):
    model_config = ConfigDict(extra="allow")

    code: str
    message: str
    phase: str | None = None
    runtime: dict[str, Any] = Field(default_factory=dict)
    protocol: str | None = None
    last_event_type: str | None = None
    event_counts: dict[str, int] = Field(default_factory=dict)
    stderr_tail: str = ""
    retriable: bool = False
    configured_limit_bytes: int | None = None
    observed_frame_bytes: int | None = None
    frame_bytes: int | None = None
    frame_type: str | None = None
    event_type: str | None = None
    tool_name: str | None = None
    field: str | None = None
    received_type: str | None = None
    child_job_id: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(default_factory=progress_now)

    @model_validator(mode="before")
    @classmethod
    def _normalise_numeric_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        event_counts = data.get("event_counts")
        if isinstance(event_counts, dict):
            data["event_counts"] = {
                str(key): safe_int(item, default=0, minimum=0) or 0
                for key, item in event_counts.items()
            }
        for field in (
            "configured_limit_bytes",
            "observed_frame_bytes",
            "frame_bytes",
        ):
            if field in data and data[field] is not None:
                data[field] = safe_int(data[field], default=0, minimum=0, field=field)
        return data


class JobProgress(BaseModel):
    model_config = ConfigDict(extra="allow")

    stage: str = "queued"
    label: str = "排队中"
    percent: int = 0
    indeterminate: bool = False
    current_item: str | None = None
    current_operation: str | None = None
    # Fine-grained sub-stage visibility: which window/subtask is running, how
    # many completed, and where the current Agent dispatch sits.  Entering a
    # stage percent (e.g. 40%) does NOT mean the model is running.
    current_subtask: str | None = None
    current_window: int | None = None
    windows_completed: int | None = None
    windows_total: int | None = None
    prompt_state: str | None = None
    prompt_state_updated_at: str | None = None
    prompt_info: dict[str, Any] = Field(default_factory=dict)
    dispatch_metrics: dict[str, Any] = Field(default_factory=dict)
    last_progress_at: str | None = None
    completed: int = 0
    total: int | None = None
    message: str = ""
    failure: JobFailure | None = None
    worker_state: WorkerState = WorkerState.STARTING
    worker_started_at: str | None = None
    worker_heartbeat_at: str | None = None
    worker_finished_at: str | None = None
    agent_call_count: int = 0
    agent_call_attempt_count: int = 0
    agent_call_completed_count: int = 0
    agent_call_failed_count: int = 0
    runtime_binding_snapshot: dict[str, Any] = Field(default_factory=dict)
    activity_tracker: dict[str, Any] = Field(default_factory=dict)
    process_alive: bool | None = None
    child_job_id: str | None = None
    child_snapshot: dict[str, Any] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=progress_now)

    @model_validator(mode="before")
    @classmethod
    def _normalise_numeric_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        data["percent"] = safe_int(data.get("percent"), default=0, minimum=0, maximum=100)
        data["completed"] = safe_int(data.get("completed"), default=0, minimum=0)
        data["agent_call_count"] = safe_int(data.get("agent_call_count"), default=0, minimum=0)
        data["agent_call_attempt_count"] = safe_int(
            data.get("agent_call_attempt_count", data.get("agent_call_count")),
            default=0,
            minimum=0,
        )
        data["agent_call_completed_count"] = safe_int(
            data.get("agent_call_completed_count"), default=0, minimum=0
        )
        data["agent_call_failed_count"] = safe_int(
            data.get("agent_call_failed_count"), default=0, minimum=0
        )
        if "total" in data and data["total"] is not None:
            data["total"] = safe_int(data["total"], default=0, minimum=0)
        for field in ("current_window", "windows_completed", "windows_total"):
            if data.get(field) is not None:
                data[field] = safe_int(data[field], default=None, minimum=0)
        return data

    def update_stage(
        self, stage: str, *, label: str | None = None, percent: int | None = None, **values: Any
    ) -> None:
        self.stage = str(stage)
        if label is not None:
            self.label = str(label)
        if percent is not None:
            self.percent = max(0, min(100, int(percent)))
        for key, value in values.items():
            if hasattr(self, key):
                setattr(self, key, value)
        self.updated_at = progress_now()
        self.last_progress_at = self.updated_at

    def set_prompt_state(self, state: str | None) -> None:
        """Record where the current Agent dispatch sits (see MaterialPromptState)."""

        self.prompt_state = str(state) if state else None
        self.prompt_state_updated_at = progress_now()
        self.updated_at = self.prompt_state_updated_at

    def set_operation(self, operation: str | None) -> None:
        """Record the semantic operation without changing the stage percentage."""

        self.current_operation = str(operation) if operation else None
        self.updated_at = progress_now()

    def set_runtime_binding(self, snapshot: dict[str, Any] | None) -> None:
        """Persist the verified runtime binding in the typed progress model."""

        self.runtime_binding_snapshot = dict(snapshot or {})
        self.updated_at = progress_now()

    def set_activity(
        self, diagnostics: dict[str, Any] | None, *, process_alive: bool | None = None
    ) -> None:
        """Persist bounded Agent activity diagnostics without mapping access."""

        self.activity_tracker = dict(diagnostics or {})
        self.process_alive = process_alive
        self.updated_at = progress_now()

    def begin_agent_call(self) -> None:
        """Count a real Agent Turn attempt before waiting for its response."""

        self.agent_call_attempt_count += 1
        self.agent_call_count = self.agent_call_attempt_count
        self.updated_at = progress_now()

    def record_agent_outcome(self, *, failed: bool) -> None:
        """Record a terminal outcome for a previously counted Agent Turn."""

        if failed:
            self.agent_call_failed_count += 1
        else:
            self.agent_call_completed_count += 1
        self.updated_at = progress_now()

    def touch_worker(
        self,
        state: WorkerState | str | None = None,
        *,
        heartbeat_at: str | None = None,
        finished_at: str | None = None,
        agent_call_count: int | None = None,
    ) -> None:
        """Update liveness metadata without changing the semantic job stage."""

        now = heartbeat_at or progress_now()
        if state is not None:
            try:
                self.worker_state = WorkerState(str(state))
            except ValueError:
                self.worker_state = WorkerState.RUNNING
        if self.worker_started_at is None:
            self.worker_started_at = now
        self.worker_heartbeat_at = now
        if finished_at is not None:
            self.worker_finished_at = finished_at
        if agent_call_count is not None:
            self.agent_call_count = max(0, int(agent_call_count))
            self.agent_call_attempt_count = self.agent_call_count
        self.updated_at = now

    def worker_snapshot(self) -> dict[str, Any]:
        return {
            "worker_state": self.worker_state.value,
            "worker_started_at": self.worker_started_at,
            "worker_heartbeat_at": self.worker_heartbeat_at,
            "worker_finished_at": self.worker_finished_at,
            "agent_call_count": self.agent_call_count,
            "agent_call_attempt_count": self.agent_call_attempt_count,
            "agent_call_completed_count": self.agent_call_completed_count,
            "agent_call_failed_count": self.agent_call_failed_count,
            "child_job_id": self.child_job_id,
            "child_snapshot": dict(self.child_snapshot),
        }


PERSONA_LOCAL_STAGE_PERCENT: dict[str, int] = {
    "queued": 2,
    "created": 2,
    "planning": 5,
    "ingesting_sources": 8,
    "parsing": 15,
    "segmenting": 25,
    "material_classification": 40,
    "semantic_relation": 50,
    "evidence_fusion": 60,
    "indexing": 68,
    "gap_analysis": 72,
    "extracting": 75,
    "retry_dimension_reuse": 75,
    "compiling": 96,
    "summary": 99,
    "completed": 100,
    "completed_with_gaps": 100,
    "failed": 0,
    "paused": 0,
}


def percent_for_stage(
    stage: str, *, completed: int = 0, total: int | None = None, web: bool = False
) -> int:
    normalized = str(stage or "queued").lower()
    if normalized in PERSONA_LOCAL_STAGE_PERCENT:
        return PERSONA_LOCAL_STAGE_PERCENT[normalized]
    if web and total and total > 0:
        return max(5, min(94, int(15 + 70 * (completed / total))))
    return 0


class PersonaFailureCode(StrEnum):
    """Typed failure taxonomy.

    Retry policy must never depend on string-matching a message: a code says
    what failed, and the retry policy is derived from the code.
    """

    AUDIT_REPAIR_FAILED = "AUDIT_REPAIR_FAILED"
    FINAL_AUDIT_FAILED = "FINAL_AUDIT_FAILED"
    FINAL_QUALITY_GATE_FAILED = "FINAL_QUALITY_GATE_FAILED"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    STRUCTURED_OUTPUT_FAILED = "STRUCTURED_OUTPUT_FAILED"
    TRANSPORT_FAILED = "TRANSPORT_FAILED"
    TIMEOUT = "TIMEOUT"
    RUNTIME_UNAVAILABLE = "RUNTIME_UNAVAILABLE"
    JOB_PROGRESS_CONTRACT_ERROR = "JOB_PROGRESS_CONTRACT_ERROR"
    JOB_STALLED = "JOB_STALLED"
    PERSONA_CREATION_ERROR = "PERSONA_CREATION_ERROR"
    UNKNOWN = "UNKNOWN"


# Failures that are safe to retry from the last checkpoint: the persona data
# (sources / research / evidence / completed dimensions) is already durable, so
# re-entering the failed phase cannot lose provenance.
RETRYABLE_FAILURE_CODES = frozenset(
    {
        PersonaFailureCode.AUDIT_REPAIR_FAILED,
        PersonaFailureCode.FINAL_AUDIT_FAILED,
        PersonaFailureCode.FINAL_QUALITY_GATE_FAILED,
        PersonaFailureCode.MODEL_OUTPUT_INVALID,
        PersonaFailureCode.STRUCTURED_OUTPUT_FAILED,
        PersonaFailureCode.TRANSPORT_FAILED,
        PersonaFailureCode.TIMEOUT,
        PersonaFailureCode.RUNTIME_UNAVAILABLE,
        PersonaFailureCode.JOB_PROGRESS_CONTRACT_ERROR,
        PersonaFailureCode.JOB_STALLED,
    }
)

# Rows written before the taxonomy existed only carry the generic code plus a
# message.  These prefixes are the historical markers of a retryable phase
# failure and must stay retryable without rewriting the original row.
RETRYABLE_LEGACY_MESSAGE_PREFIXES = (
    "audit_repair_failed:",
    "final_quality_gate_failed:",
    "final_audit_failed:",
    "model_output_invalid:",
    "structured_output_failed:",
)


def is_retryable_failure_code(code: Any) -> bool:
    """Retry policy derived from a typed code, never from message matching."""

    return str(code or "").strip().upper() in RETRYABLE_FAILURE_CODES


def classify_persona_failure(exc: BaseException) -> str:
    """Map an exception onto :class:`PersonaFailureCode`.

    Classified by exception type name (no agent-layer import, so this module
    stays dependency-free) and by the explicit phase prefixes the pipeline
    raises.  Anything unrecognised degrades to ``PERSONA_CREATION_ERROR``
    rather than being guessed at from a free-text message.
    """

    message = str(exc)
    for prefix, code in (
        ("audit_repair_failed:", PersonaFailureCode.AUDIT_REPAIR_FAILED),
        ("final_quality_gate_failed:", PersonaFailureCode.FINAL_QUALITY_GATE_FAILED),
        ("final_audit_failed:", PersonaFailureCode.FINAL_AUDIT_FAILED),
        ("final_evidence_audit_invalid", PersonaFailureCode.FINAL_AUDIT_FAILED),
        ("final_consistency_audit_invalid", PersonaFailureCode.FINAL_AUDIT_FAILED),
    ):
        if message.startswith(prefix):
            return code.value
    if "_audit_invalid" in message:
        return PersonaFailureCode.FINAL_AUDIT_FAILED.value
    type_name = type(exc).__name__
    if "StructuredOutput" in type_name or "Schema" in type_name:
        return PersonaFailureCode.STRUCTURED_OUTPUT_FAILED.value
    if "AgentOutput" in type_name or "OutputError" in type_name:
        return PersonaFailureCode.MODEL_OUTPUT_INVALID.value
    if "Timeout" in type_name:
        return PersonaFailureCode.TIMEOUT.value
    if "Unavailable" in type_name or "SessionStart" in type_name:
        return PersonaFailureCode.RUNTIME_UNAVAILABLE.value
    if "Transport" in type_name or "RuntimeError" in type_name:
        return PersonaFailureCode.TRANSPORT_FAILED.value
    if type_name == "TypeError":
        return PersonaFailureCode.JOB_PROGRESS_CONTRACT_ERROR.value
    return PersonaFailureCode.PERSONA_CREATION_ERROR.value


def failure_code_for(value: Any) -> str:
    """Best-effort extraction of a failure code from any failure payload."""

    if isinstance(value, Mapping):
        code = str(value.get("code") or "").strip().upper()
        if code:
            return code
        # Legacy rows may only carry a diagnostic message; classify it so
        # retry decisions do not depend on the payload shape.
        value = str(value.get("message") or "")
    text = str(value or "").strip()
    for prefix in RETRYABLE_LEGACY_MESSAGE_PREFIXES:
        if text.startswith(prefix):
            return prefix.rstrip(":").upper()
    return PersonaFailureCode.UNKNOWN.value


def is_retryable_failure(failure: Any) -> bool:
    """Return whether a failed job may create a new retry run.

    A failed audit / audit-repair / quality-gate / model-output failure leaves
    the persona checkpoint intact, so it is always retryable.  Compatibility
    prefixes are kept for rows written before the typed taxonomy existed; the
    original row is never rewritten, only a new retry run may be created.
    """

    if not isinstance(failure, Mapping):
        return bool(failure) and failure_code_for(failure) in RETRYABLE_FAILURE_CODES
    if bool(failure.get("retriable", False)):
        return True
    code = str(failure.get("code") or "").strip().upper()
    if code in RETRYABLE_FAILURE_CODES:
        return True
    message = str(failure.get("message") or "")
    if code == "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT":
        # Older rows were persisted before the response collector assigned
        # this output failure its typed retry policy.  Keep those rows usable
        # without rewriting their historical diagnostics.
        return True
    if message.startswith(RETRYABLE_LEGACY_MESSAGE_PREFIXES):
        # Rows persisted before the typed taxonomy: an audit/quality-gate
        # failure must stay retryable after the fix without forcing the user
        # to rebuild the persona from zero.
        return True
    return (
        code == "PERSONA_CREATION_ERROR"
        and "JobProgress" in message
        and "item assignment" in message
    )


__all__ = [
    "JobFailure",
    "JobProgress",
    "JobVisibility",
    "WorkerState",
    "JobNotTerminalError",
    "PERSONA_LOCAL_STAGE_PERCENT",
    "PersonaFailureCode",
    "RETRYABLE_FAILURE_CODES",
    "RETRYABLE_LEGACY_MESSAGE_PREFIXES",
    "classify_persona_failure",
    "failure_code_for",
    "is_retryable_failure",
    "is_retryable_failure_code",
    "percent_for_stage",
]
