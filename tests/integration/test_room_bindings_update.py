from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.room.models import ParticipantSlot, RoomRunStatus, RoomStatus
from persona_continuum.room.orchestrator import RoomBusyError
from persona_continuum.room.random_resolver import (
    UnknownModelError,
    UnknownRuntimeError,
    UnsupportedReasoningEffortError,
)
from persona_continuum.web.server import create_web_app


def _persona(continuum: PersonaContinuum, persona_id: str) -> None:
    continuum.personas.create_from_manifest(
        {
            "id": persona_id,
            "display_name": persona_id,
            "persona_type": "fictional",
            "run_mode": "continuation",
        }
    )


def _slot(participant_id: str, persona_id: str, **overrides: object) -> ParticipantSlot:
    defaults: dict[str, object] = {
        "participant_id": participant_id,
        "persona_id": persona_id,
        "runtime_selection": "fake_agent",
        "model_selection": "fake-gpt-5",
        "reasoning_selection": "high",
    }
    defaults.update(overrides)
    return ParticipantSlot.model_validate(defaults)


def _two_slot_room(app: PersonaContinuum, **overrides: object) -> str:
    _persona(app, "alice")
    _persona(app, "bob")
    room = app.orchestrator.create_room(
        title="Binding edit room",
        topic="bindings",
        participants=[
            _slot("slot_alice", "alice", **overrides),
            _slot("slot_bob", "bob"),
        ],
    )
    return room.id


@pytest.mark.anyio
async def test_update_room_bindings_rewrites_snapshot_and_slot(app: PersonaContinuum) -> None:
    room_id = _two_slot_room(app)
    await app.orchestrator.start_room(room_id)

    state = await app.orchestrator.update_room_bindings(
        room_id,
        {
            "slot_alice": {
                "agent_runtime_id": "fake_agent",
                "model_id": "fake-claude-4",
                "reasoning_effort": "low",
            }
        },
    )

    snap = state.binding_snapshots["slot_alice"]
    assert snap.model_id == "fake-claude-4"
    assert snap.reasoning_effort == "low"
    slot = next(p for p in state.participants if p.participant_id == "slot_alice")
    assert slot.runtime_selection == "fake_agent"
    assert slot.model_selection == "fake-claude-4"
    assert slot.reasoning_selection == "low"
    # Untouched slots keep their original binding.
    assert state.binding_snapshots["slot_bob"].model_id == "fake-gpt-5"

    # The rewrite is durable: a fresh read (not the returned object) sees it.
    reloaded = app.orchestrator.get_room(room_id)
    assert reloaded is not None
    assert reloaded.binding_snapshots["slot_alice"].model_id == "fake-claude-4"


@pytest.mark.anyio
async def test_updated_binding_is_used_by_the_next_turn(app: PersonaContinuum) -> None:
    room_id = _two_slot_room(app)
    await app.orchestrator.start_room(room_id)

    await app.orchestrator.update_room_bindings(
        room_id,
        {
            "slot_alice": {
                "agent_runtime_id": "fake_agent",
                "model_id": "fake-claude-4",
                "reasoning_effort": "low",
            }
        },
    )

    events = []
    async for ev in app.orchestrator.step_turn(
        room_id, manual_speaker_id="slot_alice", user_message="hello"
    ):
        events.append(ev)

    started = next(e for e in events if e.get("event") == "agent_started")
    assert started.get("model_id") == "fake-claude-4"
    assert started.get("reasoning_effort") == "low"

    transcripts = app.orchestrator.list_room_transcripts(room_id)
    assert transcripts[0].model_id == "fake-claude-4"
    assert transcripts[0].reasoning_effort == "low"


@pytest.mark.anyio
async def test_update_room_bindings_converts_random_pool_to_explicit(
    app: PersonaContinuum,
) -> None:
    _persona(app, "alice")
    _persona(app, "bob")
    room = app.orchestrator.create_room(
        title="Random room",
        topic="random",
        participants=[
            _slot(
                "slot_alice",
                "alice",
                runtime_selection="random",
                model_selection="random",
                reasoning_selection="random",
                runtime_pool=[{"id": "fake_agent", "weight": 1.0}],
            ),
            _slot("slot_bob", "bob"),
        ],
    )
    await app.orchestrator.start_room(room.id)

    state = await app.orchestrator.update_room_bindings(
        room.id,
        {
            "slot_alice": {
                "agent_runtime_id": "fake_agent",
                "model_id": "fake-grok-4",
                "reasoning_effort": "max",
            }
        },
    )

    slot = next(p for p in state.participants if p.participant_id == "slot_alice")
    assert slot.runtime_selection == "fake_agent"
    assert slot.model_selection == "fake-grok-4"
    assert slot.reasoning_selection == "max"
    assert slot.runtime_pool == []
    assert slot.model_pool == []
    assert slot.reasoning_pool == []
    snap = state.binding_snapshots["slot_alice"]
    assert snap.model_id == "fake-grok-4"
    assert snap.reasoning_effort == "max"


@pytest.mark.anyio
async def test_update_room_bindings_rejects_running_room(app: PersonaContinuum) -> None:
    room_id = _two_slot_room(app)
    await app.orchestrator.start_room(room_id)

    state = app.orchestrator.get_room(room_id)
    assert state is not None
    state.status = RoomStatus.DISCUSSING
    app.orchestrator._save_room_state(state, force=True)

    with pytest.raises(RoomBusyError) as excinfo:
        await app.orchestrator.update_room_bindings(
            room_id,
            {
                "slot_alice": {
                    "agent_runtime_id": "fake_agent",
                    "model_id": "fake-claude-4",
                    "reasoning_effort": "low",
                }
            },
        )
    assert excinfo.value.code == "room_bindings_locked_during_run"


@pytest.mark.anyio
async def test_update_room_bindings_rejects_running_protocol(app: PersonaContinuum) -> None:
    room_id = _two_slot_room(app)
    await app.orchestrator.start_room(room_id)

    state = app.orchestrator.get_room(room_id)
    assert state is not None
    state.protocol_state.status = RoomRunStatus.RUNNING
    app.orchestrator._save_room_state(state, force=True)

    with pytest.raises(RoomBusyError):
        await app.orchestrator.update_room_bindings(
            room_id,
            {
                "slot_alice": {
                    "agent_runtime_id": "fake_agent",
                    "model_id": "fake-claude-4",
                    "reasoning_effort": "low",
                }
            },
        )


@pytest.mark.anyio
async def test_update_room_bindings_validation_errors(app: PersonaContinuum) -> None:
    room_id = _two_slot_room(app)
    await app.orchestrator.start_room(room_id)

    with pytest.raises(Exception) as unknown_participant:
        await app.orchestrator.update_room_bindings(
            room_id,
            {
                "slot_ghost": {
                    "agent_runtime_id": "fake_agent",
                    "model_id": "fake-claude-4",
                    "reasoning_effort": "low",
                }
            },
        )
    assert getattr(unknown_participant.value, "code", "") == "participant_not_found"

    # fake-claude-4 does not report the "xhigh" effort.
    with pytest.raises(UnsupportedReasoningEffortError):
        await app.orchestrator.update_room_bindings(
            room_id,
            {
                "slot_alice": {
                    "agent_runtime_id": "fake_agent",
                    "model_id": "fake-claude-4",
                    "reasoning_effort": "xhigh",
                }
            },
        )

    with pytest.raises(UnknownModelError):
        await app.orchestrator.update_room_bindings(
            room_id,
            {
                "slot_alice": {
                    "agent_runtime_id": "fake_agent",
                    "model_id": "not-a-model",
                    "reasoning_effort": "low",
                }
            },
        )

    with pytest.raises(UnknownRuntimeError):
        await app.orchestrator.update_room_bindings(
            room_id,
            {
                "slot_alice": {
                    "agent_runtime_id": "ghost_runtime",
                    "model_id": "fake-claude-4",
                    "reasoning_effort": "low",
                }
            },
        )

    # Failed validation must not mutate the persisted bindings.
    state = app.orchestrator.get_room(room_id)
    assert state is not None
    assert state.binding_snapshots["slot_alice"].model_id == "fake-gpt-5"


def test_room_bindings_update_api(app: PersonaContinuum) -> None:
    room_id = _two_slot_room(app)
    with TestClient(create_web_app(app)) as client:
        started = client.post(f"/api/rooms/{room_id}/start")
        assert started.status_code == 200, started.text

        updated = client.put(
            f"/api/rooms/{room_id}/bindings",
            json={
                "bindings": {
                    "slot_alice": {
                        "agent_runtime_id": "fake_agent",
                        "model_id": "fake-claude-4",
                        "reasoning_effort": "low",
                    }
                }
            },
        )
        assert updated.status_code == 200, updated.text
        data = updated.json()["data"]
        assert data["binding_snapshots"]["slot_alice"]["model_id"] == "fake-claude-4"

        fetched = client.get(f"/api/rooms/{room_id}")
        assert fetched.status_code == 200
        assert (
            fetched.json()["data"]["binding_snapshots"]["slot_alice"]["reasoning_effort"]
            == "low"
        )

        missing = client.put(
            f"/api/rooms/{room_id}/bindings",
            json={"bindings": {"slot_ghost": {"agent_runtime_id": "fake_agent"}}},
        )
        assert missing.status_code == 400

        empty = client.put(f"/api/rooms/{room_id}/bindings", json={"bindings": {}})
        assert empty.status_code == 400

        not_found = client.put(
            "/api/rooms/room_missing/bindings",
            json={"bindings": {"slot_alice": {"agent_runtime_id": "fake_agent"}}},
        )
        assert not_found.status_code == 404
