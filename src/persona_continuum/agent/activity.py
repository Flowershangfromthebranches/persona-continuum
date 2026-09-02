"""Bounded, privacy-safe activity accounting for one Agent turn."""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_TRANSPORT_KINDS = {
    "stdout",
    "stderr",
    "stream_bytes",
    "protocol_frame",
    "acp_frame",
    "jsonrpc_frame",
    "http_sse_frame",
    "process",
}
_OUTPUT_KINDS = {"text", "chunk", "output", "done", "thinking", "tool_call", "tool_result"}
_MODEL_KINDS = {"model", "text", "chunk", "thinking", "tool_call", "tool_result", "done"}
_SAFE_METADATA_KEYS = {
    "frame_type",
    "event_type",
    "tool_name",
    "method",
    "stream_mode",
    "returncode",
    "status",
}


class AgentActivityTracker(BaseModel):
    """Telemetry that answers whether a turn is genuinely making progress.

    Timestamps are Unix seconds so the snapshot can cross the persistence and
    HTTP boundaries without depending on a process-local monotonic clock. The
    monotonic companions are intentionally runtime-only and are excluded from
    the public snapshot by ``as_diagnostics``.
    """

    model_config = ConfigDict(extra="allow")

    turn_started_at: float | None = None
    last_transport_activity: float | None = None
    last_model_activity: float | None = None
    last_output_activity: float | None = None
    first_response_at: float | None = None
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    protocol_frames: int = 0
    thinking_events: int = 0
    tool_events: int = 0
    text_events: int = 0
    activity_kind: str = "none"
    last_activity_metadata: dict[str, Any] = Field(default_factory=dict)
    protocol_method_counts: dict[str, int] = Field(default_factory=dict)
    turn_started_monotonic: float | None = Field(default=None, exclude=True)
    last_transport_monotonic: float | None = Field(default=None, exclude=True)

    def begin_turn(self) -> None:
        now = time.time()
        monotonic_now = time.monotonic()
        self.turn_started_at = now
        self.last_transport_activity = now
        self.last_model_activity = None
        self.last_output_activity = None
        self.first_response_at = None
        self.stdout_bytes = 0
        self.stderr_bytes = 0
        self.protocol_frames = 0
        self.thinking_events = 0
        self.tool_events = 0
        self.text_events = 0
        self.activity_kind = "turn_started"
        self.last_activity_metadata = {}
        self.protocol_method_counts = {}
        self.turn_started_monotonic = monotonic_now
        self.last_transport_monotonic = monotonic_now

    def touch(
        self,
        kind: str,
        *,
        byte_count: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record one transport/model/output observation without payload text."""

        normalized = str(kind or "unknown").strip().lower() or "unknown"
        now = time.time()
        monotonic_now = time.monotonic()
        count = max(0, int(byte_count or 0))
        if self.turn_started_at is None:
            self.turn_started_at = now
            self.turn_started_monotonic = monotonic_now

        # A semantic model/tool observation was necessarily delivered by the
        # transport even when its adapter event carries no raw byte count.
        # Treat it as observed activity for idle-time purposes while keeping
        # the byte counters reserved for actual streams.
        is_transport = (
            normalized in _TRANSPORT_KINDS
            or normalized in _MODEL_KINDS
            or count > 0
        )
        if is_transport:
            self.last_transport_activity = now
            self.last_transport_monotonic = monotonic_now
        if normalized == "stdout":
            self.stdout_bytes += count
        elif normalized == "stderr":
            self.stderr_bytes += count

        if normalized in {"protocol_frame", "acp_frame", "jsonrpc_frame", "http_sse_frame"}:
            self.protocol_frames += 1
        if normalized in {"thinking", "model_thinking"}:
            self.thinking_events += 1
        if normalized in {"tool_call", "tool_result", "tool_update", "tool_event"}:
            self.tool_events += 1
        if normalized in {"text", "chunk", "agent_message_chunk", "output", "done"}:
            self.text_events += 1

        if normalized in _MODEL_KINDS or normalized == "agent_message_chunk":
            self.last_model_activity = now
            if self.first_response_at is None:
                self.first_response_at = now
        if normalized in _OUTPUT_KINDS or normalized == "agent_message_chunk":
            self.last_output_activity = now

        self.activity_kind = normalized
        safe = {
            str(key): value
            for key, value in (metadata or {}).items()
            if str(key) in _SAFE_METADATA_KEYS and isinstance(value, (str, int, float, bool))
        }
        if safe:
            self.last_activity_metadata = safe
        method = safe.get("method")
        if method:
            method_key = str(method)[:120]
            self.protocol_method_counts[method_key] = (
                self.protocol_method_counts.get(method_key, 0) + 1
            )

    def transport_age_seconds(self, now: float | None = None) -> float | None:
        timestamp = self.last_transport_activity
        if timestamp is None:
            return None
        return max(0.0, float(now if now is not None else time.time()) - timestamp)

    def as_diagnostics(self) -> dict[str, Any]:
        return {
            "turn_started_at": self.turn_started_at,
            "last_transport_activity": self.last_transport_activity,
            "last_model_activity": self.last_model_activity,
            "last_output_activity": self.last_output_activity,
            "first_response_at": self.first_response_at,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "protocol_frames": self.protocol_frames,
            "thinking_events": self.thinking_events,
            "tool_events": self.tool_events,
            "text_events": self.text_events,
            "activity_kind": self.activity_kind,
            "last_activity_metadata": dict(self.last_activity_metadata),
            "protocol_method_counts": dict(self.protocol_method_counts),
            "process_alive": None,
        }


__all__ = ["AgentActivityTracker"]
