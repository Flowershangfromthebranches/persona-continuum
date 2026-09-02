"""Written regression contracts for the Agent Runtime activity refactor.

The implementation turn deliberately does not execute these tests.  They are
the review/verification gate for the next phase and use only fake streams,
static adapter contracts, and bounded in-process objects.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from persona_continuum.agent.activity import AgentActivityTracker
from persona_continuum.agent.adapter import AgentSession
from persona_continuum.agent.adapters.codex import CodexAdapter
from persona_continuum.agent.adapters.command_code import CommandCodeAdapter
from persona_continuum.agent.models import (
    AgentEvent,
    AgentEventType,
    AgentSessionConfig,
    OutputStreamingMode,
    RuntimeBindingSnapshot,
)
from persona_continuum.agent.protocols.acp import ACPAdapter
from persona_continuum.agent.protocols.plain_cli import PlainCliAdapter
from persona_continuum.agent.runtime_executor import (
    AgentRuntimeExecutor,
    RuntimeSessionBinding,
)
from persona_continuum.agent.timeout import TimeoutBudget


def _session() -> AgentSession:
    return AgentSession(
        config=AgentSessionConfig(
            room_id="activity-room",
            participant_id="activity-participant",
            persona_id="activity-persona",
        )
    )


def test_agent_activity_tracker_counts_stdout_stderr_and_protocol() -> None:
    tracker = AgentActivityTracker()
    tracker.begin_turn()
    tracker.touch("stdout", byte_count=512)
    tracker.touch("stderr", byte_count=128)
    tracker.touch(
        "protocol_frame",
        byte_count=2048,
        metadata={"method": "session/update", "event_type": "agent_message_chunk"},
    )
    tracker.touch("thinking")
    tracker.touch("tool_call")
    tracker.touch("chunk")

    diagnostics = tracker.as_diagnostics()
    assert diagnostics["stdout_bytes"] == 512
    assert diagnostics["stderr_bytes"] == 128
    assert diagnostics["protocol_frames"] == 1
    assert diagnostics["thinking_events"] == 1
    assert diagnostics["tool_events"] == 1
    assert diagnostics["text_events"] == 1
    assert diagnostics["last_transport_activity"] is not None
    assert diagnostics["protocol_method_counts"]["session/update"] == 1


class _DelayedEventStream:
    def __init__(self) -> None:
        self.emitted = False
        self.cancelled = False

    def __aiter__(self) -> _DelayedEventStream:
        return self

    async def __anext__(self) -> AgentEvent:
        if self.emitted:
            raise StopAsyncIteration
        try:
            await asyncio.sleep(0.025)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.emitted = True
        return AgentEvent(type=AgentEventType.DONE, content="bounded response")


class _RuntimeAdapter:
    adapter_id = "runtime-test"
    name = "Runtime test"
    output_streaming_mode = OutputStreamingMode.STREAMING

    async def cancel(self, session: AgentSession) -> None:
        session.request_cancel()


@pytest.mark.anyio
async def test_runtime_timeout_keeps_one_pending_read_until_deadline(monkeypatch) -> None:
    import persona_continuum.agent.runtime_executor as runtime_module

    monkeypatch.setattr(runtime_module, "ACTIVITY_CHECK_INTERVAL_SECONDS", 0.01)
    session = _session()
    session.begin_turn()
    adapter = _RuntimeAdapter()
    binding = RuntimeSessionBinding(
        adapter=adapter,  # type: ignore[arg-type]
        session=session,
        snapshot=RuntimeBindingSnapshot(agent_id=adapter.adapter_id),
    )
    stream = _DelayedEventStream()
    executor = AgentRuntimeExecutor()
    events = [
        event
        async for event in executor._iter_events_with_timeouts(
            stream,
            binding,
            TimeoutBudget(
                first_response_timeout_seconds=0.10,
                idle_timeout_seconds=0.10,
                hard_timeout_seconds=0.20,
                phase="activity_test",
            ),
            started=__import__("time").monotonic(),
            phase="activity_test",
        )
    ]
    assert events[-1].content == "bounded response"
    assert stream.cancelled is False


def test_buffered_cli_declares_buffered_mode() -> None:
    adapter = PlainCliAdapter("plain-test", "Plain test", ["missing-plain-test"])
    assert adapter.output_streaming_mode == OutputStreamingMode.BUFFERED_FINAL


def test_plain_cli_buffered_final_uses_first_response_policy() -> None:
    source = inspect.getsource(PlainCliAdapter.send)
    assert "SubprocessAgentTransport" in source
    assert "transport.wait()" in source
    assert "output_streaming_mode" in source


def test_plain_cli_stderr_does_not_deadlock() -> None:
    source = inspect.getsource(PlainCliAdapter.send)
    transport_source = inspect.getsource(
        __import__(
            "persona_continuum.agent.subprocess_transport",
            fromlist=["SubprocessAgentTransport"],
        ).SubprocessAgentTransport
    )
    assert "stderr_tail" in source
    assert "_drain_stderr" in transport_source
    assert "create_task" in transport_source


def test_command_code_stderr_is_drained() -> None:
    source = inspect.getsource(CommandCodeAdapter.send)
    assert "SubprocessAgentTransport" in source
    assert "stderr_tail" in source


def test_command_code_buffered_output_not_idle_timed_out() -> None:
    adapter = CommandCodeAdapter()
    assert adapter.output_streaming_mode == OutputStreamingMode.BUFFERED_FINAL
    from persona_continuum.agent.timeout import AgentTimeoutPolicy

    budget = AgentTimeoutPolicy().resolve(
        phase="material_classification",
        output_streaming_mode=adapter.output_streaming_mode,
    )
    assert budget.first_response_timeout_seconds == budget.hard_timeout_seconds
    assert budget.hard_timeout_seconds >= 900


def test_command_code_process_activity_is_tracked() -> None:
    source = inspect.getsource(CommandCodeAdapter.send)
    transport_source = inspect.getsource(
        __import__(
            "persona_continuum.agent.subprocess_transport",
            fromlist=["SubprocessAgentTransport"],
        ).SubprocessAgentTransport
    )
    assert "transport.read" in source
    assert "touch_activity" in transport_source


def test_codex_unknown_jsonrpc_frame_counts_as_activity() -> None:
    source = inspect.getsource(CodexAdapter.send)
    assert '"frame_type"' in source
    assert '"unknown"' in source
    assert "touch_activity" in source


def test_codex_reasoning_frame_resets_idle_activity() -> None:
    source = inspect.getsource(CodexAdapter.send)
    assert "turn/thinking" in source
    assert "item/reasoning/delta" in source
    assert "touch_activity" in source


def test_codex_silent_text_but_active_protocol_not_timed_out() -> None:
    adapter = CodexAdapter()
    assert adapter.output_streaming_mode == OutputStreamingMode.PROTOCOL_STREAM
    source = inspect.getsource(CodexAdapter.send)
    assert "protocol_frame" in source
    assert "full_content" in source


def test_acp_frame_activity_reaches_runtime_tracker() -> None:
    source = inspect.getsource(ACPAdapter._send_acp)
    assert "touch_activity" in source
    assert '"acp_frame"' in source
    assert "frame.frame_bytes" in source


def test_acp_has_no_duplicate_turn_timeout_owner() -> None:
    source = inspect.getsource(ACPAdapter._send_acp)
    assert "wait_for" not in source
    executor_source = inspect.getsource(AgentRuntimeExecutor._iter_events_with_timeouts)
    assert "ACTIVITY_CHECK_INTERVAL_SECONDS" in executor_source
    assert "next_event_task" in executor_source


def test_phase_does_not_overwrite_participant_id() -> None:
    from persona_continuum.application.persona_creation_service import (
        PersonaCreationOrchestrator,
    )

    source = inspect.getsource(PersonaCreationOrchestrator._run_agent)
    assert 'participant_id=participant_id' in source
    assert 'phase=effective_phase' in source


def test_material_classify_uses_material_classification_phase() -> None:
    from persona_continuum.application.persona_creation_service import (
        PersonaCreationOrchestrator,
    )

    source = inspect.getsource(PersonaCreationOrchestrator._analyze_private_materials)
    assert '"classify": "material_classification"' in source


def test_material_relate_uses_semantic_relation_phase() -> None:
    from persona_continuum.application.persona_creation_service import (
        PersonaCreationOrchestrator,
    )

    source = inspect.getsource(PersonaCreationOrchestrator._analyze_private_materials)
    assert '"relate": "semantic_relation"' in source


def test_material_fuse_uses_evidence_fusion_phase() -> None:
    from persona_continuum.application.persona_creation_service import (
        PersonaCreationOrchestrator,
    )

    source = inspect.getsource(PersonaCreationOrchestrator._analyze_private_materials)
    assert '"fuse": "evidence_fusion"' in source


def test_dimension_participant_id_is_not_used_as_phase() -> None:
    from persona_continuum.application.persona_creation_service import (
        PersonaCreationOrchestrator,
    )

    source = inspect.getsource(PersonaCreationOrchestrator._extract_dimensions)
    assert 'phase="dimension_extraction"' in source
