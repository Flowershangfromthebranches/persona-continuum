from __future__ import annotations

import asyncio

import pytest

from persona_continuum.room.discussion_director import DiscussionDirector
from persona_continuum.room.models import (
    BindingPreflightError,
    ParticipantSlot,
    RoomMode,
    RoomStatus,
)


def _participants() -> list[ParticipantSlot]:
    return [
        ParticipantSlot(
            participant_id="jobs",
            persona_id="steve_jobs",
            display_name="Jobs",
            runtime_selection="fake_agent",
            model_selection="fake-gpt-5",
            expertise=["product", "Apple"],
            relationships={"jensen": -0.7},
        ),
        ParticipantSlot(
            participant_id="jensen",
            persona_id="jensen_huang",
            display_name="Jensen Huang",
            runtime_selection="fake_agent",
            model_selection="fake-gpt-5",
            expertise=["AI chip", "GPU", "CUDA"],
            relationships={"jobs": -0.7},
        ),
    ]


def _personas(app) -> None:
    for persona_id, name in (("steve_jobs", "Steve Jobs"), ("jensen_huang", "Jensen Huang")):
        app.personas.create_from_manifest(
            {
                "id": persona_id,
                "display_name": name,
                "persona_type": "historical",
                "run_mode": "continuation",
            }
        )


def test_discussion_director_selects_speaker() -> None:
    decision = DiscussionDirector().select_next_speaker(
        conversation=[{"participant_id": "jobs", "content": "Jensen，请回应 AI chip 战略。"}],
        participants=_participants(),
        topic="Apple AI chip strategy",
        relationships={"jobs": {"jensen": -0.8}},
    )
    assert decision.next_speaker == "jensen"
    assert "relationship" in decision.reason or "referenced" in decision.reason


@pytest.mark.anyio
async def test_autonomous_discussion_mode(app) -> None:
    _personas(app)
    room = app.orchestrator.create_room(
        title="Autonomous strategy room",
        topic="Should Apple enter the AI chip market?",
        participants=_participants(),
        mode=RoomMode.AUTONOMOUS,
    )
    await app.orchestrator.start_room(room.id)
    events = [event async for event in app.orchestrator.run_autonomous_discussion(room.id, 3)]
    assert any(event["event"] == "discussion_director_selected" for event in events)
    assert sum(event["event"] == "turn_completed" for event in events) == 3


@pytest.mark.anyio
async def test_autonomous_discussion_generates_three_turns(app) -> None:
    _personas(app)
    room = app.orchestrator.create_room(
        topic="Autonomous three-turn acceptance",
        participants=_participants(),
        mode=RoomMode.AUTONOMOUS,
    )
    await app.orchestrator.start_room(room.id)
    events = [event async for event in app.orchestrator.run_autonomous_discussion(room.id, 3)]
    assert sum(event["event"] == "turn_completed" for event in events) == 3


@pytest.mark.anyio
async def test_room_no_manual_speaker_required(app) -> None:
    _personas(app)
    room = app.orchestrator.create_room(
        topic="Director selects the speaker",
        participants=_participants(),
        mode=RoomMode.AUTONOMOUS,
    )
    await app.orchestrator.start_room(room.id)
    events = [event async for event in app.orchestrator.step_turn(room.id)]
    assert any(event["event"] == "discussion_director_selected" for event in events)
    assert any(event["event"] == "turn_completed" for event in events)


@pytest.mark.anyio
async def test_host_summarizes(app) -> None:
    _personas(app)
    room = app.orchestrator.create_room(
        topic="AI chip strategy",
        participants=_participants(),
        mode=RoomMode.AUTONOMOUS,
    )
    await app.orchestrator.start_room(room.id)
    events = [event async for event in app.orchestrator.run_autonomous_discussion(room.id, 2)]
    summary = next(event for event in events if event["event"] == "host_summarized")
    assert summary["content"]
    assert app.orchestrator.get_room(room.id).metadata["host_summary"]  # type: ignore[union-attr]


def test_room_create_idempotent(app) -> None:
    _personas(app)
    first = app.orchestrator.create_room(
        topic="Idempotency", participants=_participants(), request_id="create-request-1"
    )
    second = app.orchestrator.create_room(
        topic="Should not duplicate", participants=_participants(), request_id="create-request-1"
    )
    assert first.id == second.id
    assert len([room for room in app.orchestrator.list_rooms() if room.id == first.id]) == 1


def test_room_rejects_missing_persona_before_persisting(app) -> None:
    before = len(app.orchestrator.list_rooms())
    missing = ParticipantSlot(
        participant_id="stale-slot",
        persona_id="deleted_persona",
        runtime_selection="fake_agent",
        model_selection="fake-gpt-5",
    )

    with pytest.raises(BindingPreflightError, match="数字人物不存在: deleted_persona") as exc:
        app.orchestrator.create_room(topic="Invalid binding", participants=[missing])

    assert exc.value.code == "persona_not_found"
    assert len(app.orchestrator.list_rooms()) == before


@pytest.mark.anyio
async def test_step_turn_records_model_call_metadata(app) -> None:
    _personas(app)
    room = app.orchestrator.create_room(
        topic="Record model call",
        participants=_participants()[:1],
        mode=RoomMode.AUTONOMOUS,
    )
    await app.orchestrator.start_room(room.id)
    async for _event in app.orchestrator.step_turn(room.id, user_message="hello"):
        pass
    state = app.orchestrator.get_room(room.id)
    assert state is not None
    call = state.metadata.get("model_call") or {}
    assert call.get("status") == "completed"
    assert call.get("model_id")


@pytest.mark.anyio
async def test_user_injection_skips_blocking_host_open(app) -> None:
    _personas(app)
    room = app.orchestrator.create_room(
        topic="Skip host LLM after user speaks",
        participants=_participants(),
        mode=RoomMode.AUTONOMOUS,
    )
    await app.orchestrator.start_room(room.id)
    state = app.orchestrator.get_room(room.id)
    assert state is not None
    state.transcript.append(
        {
            "turn_id": "inj_1",
            "participant_id": "user",
            "speaker_name": "User",
            "content": "开始讨论",
        }
    )
    app.orchestrator._save_room_state(state)

    async def fail_open(_state: object, phase: str) -> str:
        if phase == "open":
            raise AssertionError("host open should be skipped after user injection")
        return "fallback-ok"

    app.orchestrator._generate_host_message = fail_open  # type: ignore[method-assign]
    events = [event async for event in app.orchestrator.run_autonomous_discussion(room.id, 1)]
    assert not any(event["event"] == "host_opened" for event in events)
    assert any(event["event"] == "turn_completed" for event in events)


@pytest.mark.anyio
async def test_host_open_timeout_uses_fallback(app, monkeypatch: pytest.MonkeyPatch) -> None:
    from persona_continuum.room import orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "HOST_GENERATE_TIMEOUT_SECONDS", 0.05)
    _personas(app)
    room = app.orchestrator.create_room(
        topic="Host timeout fallback",
        participants=_participants(),
        mode=RoomMode.AUTONOMOUS,
    )
    await app.orchestrator.start_room(room.id)

    async def hang(_state: object, _phase: str) -> str:
        await asyncio.sleep(2)
        return "too-late"

    app.orchestrator._generate_host_message = hang  # type: ignore[method-assign]
    events = [event async for event in app.orchestrator.run_autonomous_discussion(room.id, 1)]
    opened = next(event for event in events if event["event"] == "host_opened")
    assert "Host timeout fallback" in opened["content"]
    assert any(event["event"] == "turn_completed" for event in events)


@pytest.mark.anyio
async def test_late_websocket_subscriber_replays_room_events(app) -> None:
    _personas(app)
    room = app.orchestrator.create_room(
        topic="Replay missed host open",
        participants=_participants(),
        mode=RoomMode.AUTONOMOUS,
    )
    await app.orchestrator._broadcast_event(
        room.id, {"event": "host_opened", "room_id": room.id, "content": "开始讨论"}
    )
    queue = app.orchestrator.subscribe_events(room.id)
    replayed = queue.get_nowait()
    assert replayed["event"] == "host_opened"
    assert replayed["content"] == "开始讨论"


@pytest.mark.anyio
async def test_autonomous_start_is_single_flight(app) -> None:
    _personas(app)
    room = app.orchestrator.create_room(
        topic="Single autonomous task",
        participants=_participants(),
        mode=RoomMode.AUTONOMOUS,
    )
    await app.orchestrator.start_room(room.id)

    first = app.orchestrator.start_autonomous_discussion(room.id, max_turns=1)
    second = app.orchestrator.start_autonomous_discussion(room.id, max_turns=1)

    assert first is second
    await first
    current = app.orchestrator.get_room(room.id)
    assert current is not None
    assert current.turn_index == 1


@pytest.mark.anyio
async def test_error_room_can_be_reinitialized(app) -> None:
    _personas(app)
    room = app.orchestrator.create_room(
        topic="Recover an existing room",
        participants=_participants(),
        mode=RoomMode.AUTONOMOUS,
    )
    room.status = RoomStatus.ERROR
    room.last_error = "old runtime error"
    app.orchestrator._save_room_state(room)

    recovered = await app.orchestrator.wait_for_initialization(room.id)

    assert recovered.status == RoomStatus.READY
    assert recovered.last_error is None


@pytest.mark.anyio
async def test_legacy_invalid_room_retry_returns_to_error(app) -> None:
    _personas(app)
    room = app.orchestrator.create_room(
        topic="Legacy stale binding",
        participants=_participants(),
        mode=RoomMode.AUTONOMOUS,
    )
    room.status = RoomStatus.ERROR
    room.participants[0].persona_id = "deleted_persona"
    app.orchestrator._save_room_state(room)

    with pytest.raises(BindingPreflightError, match="数字人物不存在: deleted_persona"):
        await app.orchestrator.wait_for_initialization(room.id)

    failed = app.orchestrator.get_room(room.id)
    assert failed is not None
    assert failed.status == RoomStatus.ERROR
    assert failed.initialization_stage == "error"
    assert failed.last_error == "数字人物不存在: deleted_persona"


@pytest.mark.anyio
async def test_room_progress_events(app) -> None:
    _personas(app)
    room = app.orchestrator.create_room(topic="Progress", participants=_participants())
    await app.orchestrator.start_room(room.id)
    current = app.orchestrator.get_room(room.id)
    events = current.metadata["progress_events"]  # type: ignore[union-attr]
    assert [event["progress"] for event in events] == [5, 15, 25, 30, 40, 55, 70, 90, 100]
    assert events[3]["stage"] == "checking_tools"
    listed = next(item for item in app.orchestrator.list_rooms() if item.id == room.id)
    assert listed.status == RoomStatus.READY
    assert listed.initialization_progress == 100
