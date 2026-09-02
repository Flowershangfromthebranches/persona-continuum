from __future__ import annotations

import asyncio

import pytest

from persona_continuum.agent.adapters.fake import FakeAgentAdapter
from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.domain.memory import MemoryRecord, MemoryType
from persona_continuum.room.models import ParticipantSlot, RoomStatus


class CancellingFakeAdapter(FakeAgentAdapter):
    async def send(self, session, turn):
        if False:  # pragma: no cover - keeps this an async generator
            yield None
        raise asyncio.CancelledError


@pytest.mark.anyio
async def test_cancel_completed_room_is_noop_and_does_not_poison_sessions(app) -> None:
    for persona_id in ("cancel-boundary-a", "cancel-boundary-b"):
        app.personas.create_from_manifest(
            {
                "id": persona_id,
                "display_name": persona_id,
                "persona_type": "fictional",
                "run_mode": "continuation",
            }
        )
    participants = [
        ParticipantSlot(
            participant_id=f"slot-{index}",
            persona_id=persona_id,
            runtime_selection="fake_agent",
            model_selection="fake-gpt-5",
        )
        for index, persona_id in enumerate(("cancel-boundary-a", "cancel-boundary-b"))
    ]
    room = app.orchestrator.create_room(
        title="Completed cancel boundary",
        topic="No active turn",
        participants=participants,
    )
    await app.orchestrator.start_room(room.id)
    completed = app.orchestrator.get_room(room.id)
    assert completed is not None
    completed.status = RoomStatus.COMPLETED
    completed.metadata["model_call"] = {"status": "completed"}
    app.orchestrator._save_room_state(completed, force=True)

    cancelled = await app.orchestrator.cancel_turn(room.id)

    persisted = app.orchestrator.get_room(room.id)
    assert cancelled is False
    assert persisted is not None
    assert persisted.status == RoomStatus.COMPLETED
    assert persisted.last_error is None
    assert persisted.metadata["model_call"]["status"] == "completed"
    for session in app.orchestrator._active_agent_sessions[room.id].values():
        assert session._cancel_event is not None
        assert session._cancel_event.is_set() is False

    # Explicitly ending an already-corrupted terminal room repairs the stale
    # transient cancellation marker without deleting its transcript.
    persisted.last_error = "turn_cancelled"
    persisted.metadata["model_call"] = {"status": "cancelled"}
    app.orchestrator._save_room_state(persisted, force=True)
    stopped = await app.orchestrator.stop_room(room.id)
    assert stopped.status == RoomStatus.COMPLETED
    assert stopped.last_error is None
    assert stopped.metadata["model_call"]["status"] == "completed"


@pytest.mark.anyio
async def test_cancel_active_turn_only_signals_current_participant(app) -> None:
    for persona_id in ("active-cancel-a", "active-cancel-b"):
        app.personas.create_from_manifest(
            {
                "id": persona_id,
                "display_name": persona_id,
                "persona_type": "fictional",
                "run_mode": "continuation",
            }
        )
    participants = [
        ParticipantSlot(
            participant_id=f"slot-{index}",
            persona_id=persona_id,
            runtime_selection="fake_agent",
            model_selection="fake-gpt-5",
        )
        for index, persona_id in enumerate(("active-cancel-a", "active-cancel-b"))
    ]
    room = app.orchestrator.create_room(
        title="Active cancel boundary",
        topic="One active participant",
        participants=participants,
    )
    await app.orchestrator.start_room(room.id)
    active = app.orchestrator.get_room(room.id)
    assert active is not None
    active.status = RoomStatus.DISCUSSING
    active.current_speaker_id = "slot-0"
    active.metadata["model_call"] = {
        "status": "calling",
        "participant_id": "slot-0",
    }
    app.orchestrator._save_room_state(active, force=True)

    cancelled = await app.orchestrator.cancel_turn(room.id)

    sessions = app.orchestrator._active_agent_sessions[room.id]
    assert cancelled is True
    assert sessions["slot-0"]._cancel_event is not None
    assert sessions["slot-0"]._cancel_event.is_set() is True
    assert sessions["slot-1"]._cancel_event is not None
    assert sessions["slot-1"]._cancel_event.is_set() is False

    resumed = await app.orchestrator.resume_room(room.id)
    assert resumed.status == RoomStatus.READY
    assert resumed.last_error is None
    assert sessions["slot-0"]._cancel_event.is_set() is False


@pytest.mark.anyio
async def test_completed_room_reopens_ready_with_history_and_runtime_sessions(app) -> None:
    app.personas.create_from_manifest(
        {
            "id": "reopen-completed-room",
            "display_name": "Reopen Completed Room",
            "persona_type": "fictional",
            "run_mode": "continuation",
        }
    )
    room = app.orchestrator.create_room(
        title="Reopen completed room",
        topic="Continue after completion",
        participants=[
            ParticipantSlot(
                participant_id="slot-reopen",
                persona_id="reopen-completed-room",
                runtime_selection="fake_agent",
                model_selection="fake-gpt-5",
            )
        ],
    )
    await app.orchestrator.start_room(room.id)
    async for _ in app.orchestrator.step_turn(room.id):
        pass
    before_stop = app.orchestrator.get_room(room.id)
    assert before_stop is not None
    transcript_before = list(before_stop.transcript)
    turn_index_before = before_stop.turn_index

    stopped = await app.orchestrator.stop_room(room.id)
    assert stopped.status == RoomStatus.COMPLETED
    assert room.id not in app.orchestrator._active_agent_sessions

    resumed = await app.orchestrator.resume_room(room.id)
    assert resumed.status == RoomStatus.READY
    assert resumed.last_error is None
    assert resumed.turn_index == turn_index_before
    assert resumed.transcript == transcript_before
    assert "slot-reopen" in app.orchestrator._active_agent_sessions[room.id]
    assert "slot-reopen" in app.orchestrator._active_agent_bindings[room.id]
    assert room.id in app.orchestrator._host_agent_sessions

    events = [event async for event in app.orchestrator.step_turn(room.id)]
    assert any(event.get("event") == "turn_completed" for event in events)
    continued = app.orchestrator.get_room(room.id)
    assert continued is not None
    assert continued.turn_index == turn_index_before + 1


@pytest.mark.anyio
async def test_failed_turn_transactional_integrity_and_retry(tmp_path) -> None:
    config = Config(data_dir=tmp_path / "test_failed_turn")
    app = PersonaContinuum(config, include_fake_agent=True)
    app.init()
    try:
        # Create persona
        app.personas.create_from_manifest(
            {
                "id": "steve_jobs",
                "display_name": "Steve Jobs",
                "persona_type": "historical",
                "run_mode": "continuation",
            }
        )
        app.memories.add_memory(
            MemoryRecord(
                id="mem_init",
                persona_id="steve_jobs",
                type=MemoryType.EPISODIC,
                source_kind="seed",
                content="Initial memory.",
            )
        )

        initial_memories = app.memories.search_memories("steve_jobs", "", limit=100)
        assert len(initial_memories) == 1

        participants = [
            ParticipantSlot(
                participant_id="slot_steve",
                persona_id="steve_jobs",
                runtime_selection="fake_agent",
                model_selection="fake-gpt-5",
            )
        ]

        room = app.orchestrator.create_room(
            title="Integrity Room",
            topic="Testing Crash Isolation",
            participants=participants,
        )
        await app.orchestrator.start_room(room.id)

        # 1. Simulate Crash on FakeAgent turn 1
        fake_adapter = app.agent_registry.get_adapter("fake_agent")
        assert isinstance(fake_adapter, FakeAgentAdapter)
        fake_adapter.simulate_crash_on_turn = 1

        events = []
        async for ev in app.orchestrator.step_turn(room.id):
            events.append(ev)

        ev_types = [e.get("event") for e in events]
        assert "agent_error" in ev_types
        assert "persona_commit_completed" not in ev_types
        assert "turn_completed" not in ev_types

        # Verify room state transactional integrity
        room_state = app.orchestrator.get_room(room.id)
        assert room_state is not None
        assert room_state.turn_index == 0
        assert len(room_state.transcript) == 0
        assert len(room_state.failed_turn_audits) == 1
        assert room_state.last_error is not None
        assert "SimulatedAgentCrashError" in room_state.last_error

        # Verify persona state completely untouched
        current_memories = app.memories.search_memories("steve_jobs", "", limit=100)
        assert len(current_memories) == 1
        assert current_memories[0].id == "mem_init"

        # 2. Retry the failed turn after clearing crash simulation
        fake_adapter.simulate_crash_on_turn = None

        retry_events = []
        async for ev in app.orchestrator.retry_turn(room.id):
            retry_events.append(ev)

        retry_ev_types = [e.get("event") for e in retry_events]
        assert "agent_completed" in retry_ev_types
        assert "persona_commit_completed" in retry_ev_types
        assert "turn_completed" in retry_ev_types

        # Verify room state after successful retry
        room_after_retry = app.orchestrator.get_room(room.id)
        assert room_after_retry is not None
        assert room_after_retry.turn_index == 1
        assert len(room_after_retry.transcript) == 1
        assert room_after_retry.last_error is None

        # 3. Test Skip Turn and Restart Session
        skip_res = await app.orchestrator.skip_turn(room.id, reason="Testing skip")
        assert skip_res["event"] == "turn_skipped"
        assert skip_res["reason"] == "Testing skip"

        restart_success = await app.orchestrator.restart_session(room.id, "slot_steve")
        assert restart_success is True

    finally:
        app.close()


@pytest.mark.anyio
async def test_cancelled_background_task_does_not_leave_model_call_running(tmp_path) -> None:
    app = PersonaContinuum(
        Config(data_dir=tmp_path / "cancelled_background"), include_fake_agent=True
    )
    app.init()
    try:
        adapter = CancellingFakeAdapter(adapter_id="cancelling_fake")
        app.agent_registry.register_adapter(adapter)
        app.personas.create_from_manifest(
            {
                "id": "cancelled_persona",
                "display_name": "Cancelled Persona",
                "persona_type": "historical",
                "run_mode": "continuation",
            }
        )
        room = app.orchestrator.create_room(
            title="Cancellation cleanup",
            topic="Do not stay busy",
            participants=[
                ParticipantSlot(
                    participant_id="slot_cancelled",
                    persona_id="cancelled_persona",
                    runtime_selection="cancelling_fake",
                    model_selection="fake-gpt-5",
                )
            ],
        )
        await app.orchestrator.start_room(room.id)

        with pytest.raises(asyncio.CancelledError):
            async for _ in app.orchestrator.step_turn(room.id):
                pass

        recovered = app.orchestrator.get_room(room.id)
        assert recovered is not None
        assert recovered.status == RoomStatus.ERROR
        assert recovered.metadata["model_call"]["status"] == "interrupted"
        assert recovered.last_error == "room_turn_task_cancelled"
    finally:
        app.close()


@pytest.mark.anyio
async def test_empty_model_turn_broadcasts_agent_error(app: PersonaContinuum) -> None:
    from collections.abc import AsyncIterator

    from persona_continuum.agent.models import AgentEvent, AgentEventType, AgentTurn
    from persona_continuum.room.models import ParticipantSlot

    app.personas.create_from_manifest(
        {
            "id": "steve_jobs",
            "display_name": "Steve Jobs",
            "persona_type": "historical",
            "run_mode": "continuation",
        }
    )
    room = app.orchestrator.create_room(
        title="Empty reply room",
        topic="Why no answer",
        participants=[
            ParticipantSlot(
                participant_id="slot_steve",
                persona_id="steve_jobs",
                runtime_selection="fake_agent",
                model_selection="fake-gpt-5",
            )
        ],
    )
    await app.orchestrator.start_room(room.id)

    adapter = app.agent_registry.get_adapter("fake_agent")
    assert adapter is not None

    async def empty_send(session: object, turn: AgentTurn) -> AsyncIterator[AgentEvent]:
        del session, turn
        yield AgentEvent(type=AgentEventType.DONE, content="")

    adapter.send = empty_send  # type: ignore[method-assign]
    events = [event async for event in app.orchestrator.step_turn(room.id)]
    assert any(event.get("event") == "agent_error" for event in events)
    assert not any(event.get("event") == "turn_completed" for event in events)
    state = app.orchestrator.get_room(room.id)
    assert state is not None
    assert state.status.value == "error"
    assert "without content" in (state.last_error or "")


@pytest.mark.anyio
async def test_cancel_partial_response_does_not_commit(tmp_path) -> None:
    from collections.abc import AsyncIterator

    from persona_continuum.agent.adapter import AgentAdapter, AgentSession
    from persona_continuum.agent.models import (
        AgentCapabilityFlags,
        AgentEvent,
        AgentEventType,
        AgentProbeResult,
        AgentStatus,
        AgentTurn,
        ModelCapability,
    )

    class MockStreamingCancelAdapter(AgentAdapter):
        adapter_id = "mock_stream_cancel"
        name = "Mock Stream Cancel"

        async def probe(self) -> AgentProbeResult:
            return AgentProbeResult(
                id=self.adapter_id,
                name=self.name,
                status=AgentStatus.READY,
                protocols=["mock"],
                capabilities=AgentCapabilityFlags(streaming=True),
                models=[
                    ModelCapability(
                        id="default-model",
                        display_name="Default",
                        provider="mock",
                        supported_reasoning_efforts=[],
                        default_reasoning_effort="none",
                    )
                ],
            )

        async def create_session(self, config):
            session = AgentSession(config=config, is_active=True, session_data={})
            session._cancel_event = None
            return session

        async def send(self, session: AgentSession, turn: AgentTurn) -> AsyncIterator[AgentEvent]:
            yield AgentEvent(type=AgentEventType.CHUNK, content="partial streamed text ")
            yield AgentEvent(
                type=AgentEventType.DONE,
                content="partial streamed text ",
                metadata={"cancelled": True},
            )

    config = Config(data_dir=tmp_path / "test_cancel_turn")
    app = PersonaContinuum(config, include_fake_agent=True)
    app.init()
    try:
        mock_adapter = MockStreamingCancelAdapter()
        app.agent_registry.register_adapter(mock_adapter)

        app.personas.create_from_manifest(
            {
                "id": "persona_cancel_test",
                "display_name": "Persona Cancel Test",
                "persona_type": "historical",
                "run_mode": "continuation",
            }
        )
        app.memories.add_memory(
            MemoryRecord(
                id="mem_initial_cancel",
                persona_id="persona_cancel_test",
                type=MemoryType.EPISODIC,
                source_kind="seed",
                content="Original memory content before cancel.",
            )
        )

        participants = [
            ParticipantSlot(
                participant_id="slot_cancel",
                persona_id="persona_cancel_test",
                runtime_selection="mock_stream_cancel",
                model_selection="default-model",
            )
        ]

        room = app.orchestrator.create_room(
            title="Cancel Integrity Room",
            topic="Test Cancel Flow",
            participants=participants,
        )
        await app.orchestrator.start_room(room.id)

        events = []
        async for ev in app.orchestrator.step_turn(room.id):
            events.append(ev)

        ev_types = [e.get("event") for e in events]
        assert "agent_message_delta" in ev_types
        assert "turn_cancelled" in ev_types
        assert "agent_completed" not in ev_types
        assert "persona_commit_started" not in ev_types
        assert "persona_commit_completed" not in ev_types
        assert "turn_completed" not in ev_types

        # Verify Room State: turn_index == 0, transcript == [], last_error == turn_cancelled
        room_state = app.orchestrator.get_room(room.id)
        assert room_state is not None
        assert room_state.turn_index == 0
        assert len(room_state.transcript) == 0
        assert room_state.last_error == "turn_cancelled"
        assert len(room_state.failed_turn_audits) == 1
        assert room_state.failed_turn_audits[0]["cancelled"] is True
        assert "partial streamed text" in room_state.failed_turn_audits[0]["partial_content"]

        # Verify Persona State: unchanged
        mems = app.memories.search_memories("persona_cancel_test", "", limit=100)
        assert len(mems) == 1
        assert mems[0].id == "mem_initial_cancel"

    finally:
        app.close()
