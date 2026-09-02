from __future__ import annotations

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.room.models import ParticipantSlot, RoomStatus


@pytest.mark.anyio
async def test_room_persistence_and_resume(tmp_path) -> None:
    db_dir = tmp_path / "persist_test"
    config = Config(data_dir=db_dir)

    # 1. Initialize first container instance
    c1 = PersonaContinuum(config, include_fake_agent=True)
    c1.init()

    c1.personas.create_from_manifest(
        {
            "id": "steve_jobs",
            "display_name": "Steve Jobs",
            "persona_type": "historical",
            "run_mode": "continuation",
        }
    )
    c1.personas.create_from_manifest(
        {
            "id": "elon_musk",
            "display_name": "Elon Musk",
            "persona_type": "historical",
            "run_mode": "continuation",
        }
    )

    participants = [
        ParticipantSlot(
            participant_id="slot_1",
            persona_id="steve_jobs",
            runtime_selection="fake_agent",
            model_selection="fake-gpt-5",
        ),
        ParticipantSlot(
            participant_id="slot_2",
            persona_id="elon_musk",
            runtime_selection="fake_agent",
            model_selection="fake-claude-4",
        ),
    ]

    room = c1.orchestrator.create_room(
        title="Persisted Room",
        topic="Architecture",
        participants=participants,
    )
    room_id = room.id

    await c1.orchestrator.start_room(room_id)

    # Run 2 turns
    async for _ in c1.orchestrator.step_turn(room_id):
        pass
    async for _ in c1.orchestrator.step_turn(room_id):
        pass

    room_before_close = c1.orchestrator.get_room(room_id)
    assert room_before_close is not None
    assert room_before_close.turn_index == 2
    assert len(room_before_close.transcript) == 2

    # Close container (simulate restart)
    c1.close()

    # 2. Open new container instance
    c2 = PersonaContinuum(config, include_fake_agent=True)
    c2.init()

    resumed_room = await c2.orchestrator.resume_from_storage(room_id)
    assert resumed_room.id == room_id
    assert resumed_room.status == RoomStatus.ACTIVE
    assert resumed_room.turn_index == 2
    assert len(resumed_room.transcript) == 2
    assert "slot_1" in resumed_room.binding_snapshots
    assert "slot_2" in resumed_room.binding_snapshots

    # Execute a 3rd turn on the resumed room
    events = []
    async for ev in c2.orchestrator.step_turn(room_id):
        events.append(ev)

    assert any(e.get("event") == "turn_completed" and e.get("turn_index") == 3 for e in events)

    final_room = c2.orchestrator.get_room(room_id)
    assert final_room is not None
    assert final_room.turn_index == 3
    assert len(final_room.transcript) == 3

    c2.close()


def test_startup_recovers_orphaned_room_activity(tmp_path) -> None:
    config = Config(data_dir=tmp_path / "orphaned_room")
    first = PersonaContinuum(config, include_fake_agent=True)
    first.init()
    try:
        first.personas.create_from_manifest(
            {
                "id": "orphan_persona",
                "display_name": "Orphan Persona",
                "persona_type": "historical",
                "run_mode": "continuation",
            }
        )
        room = first.orchestrator.create_room(
            title="Interrupted room",
            topic="Recover me",
            participants=[
                ParticipantSlot(
                    participant_id="slot_orphan",
                    persona_id="orphan_persona",
                    runtime_selection="fake_agent",
                    model_selection="fake-gpt-5",
                )
            ],
        )
        room.status = RoomStatus.DISCUSSING
        room.metadata["model_call"] = {
            "status": "calling",
            "participant_id": "slot_orphan",
        }
        first.orchestrator._save_room_state(room, force=True)
        room_id = room.id
    finally:
        first.close()

    restarted = PersonaContinuum(config, include_fake_agent=True)
    restarted.init()
    try:
        recovered = restarted.orchestrator.get_room(room_id)
        assert recovered is not None
        assert recovered.status == RoomStatus.ERROR
        assert recovered.last_error is not None
        assert recovered.metadata["model_call"]["status"] == "interrupted"
        assert recovered.metadata["model_call"]["error"] == "room_activity_interrupted"
    finally:
        restarted.close()
