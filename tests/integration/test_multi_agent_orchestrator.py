from __future__ import annotations

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.domain.memory import MemoryRecord, MemoryType
from persona_continuum.room.models import ParticipantSlot, RoomStatus


@pytest.mark.anyio
async def test_orchestrator_multi_turn_flow(app: PersonaContinuum) -> None:
    # 1. Setup 2 personas
    app.personas.create_from_manifest(
        {
            "id": "steve_jobs",
            "display_name": "Steve Jobs",
            "persona_type": "historical",
            "run_mode": "continuation",
        }
    )
    app.personas.create_from_manifest(
        {
            "id": "elon_musk",
            "display_name": "Elon Musk",
            "persona_type": "historical",
            "run_mode": "continuation",
        }
    )

    # Add some memory
    app.memories.add_memory(
        MemoryRecord(
            id="mem_steve_1",
            persona_id="steve_jobs",
            type=MemoryType.EPISODIC,
            source_kind="seed",
            content="We created the Macintosh with a graphical user interface.",
        )
    )

    # 2. Create room
    participants = [
        ParticipantSlot(
            participant_id="slot_steve",
            persona_id="steve_jobs",
            display_name="Steve",
            runtime_selection="fake_agent",
            model_selection="fake-gpt-5",
            reasoning_selection="high",
        ),
        ParticipantSlot(
            participant_id="slot_elon",
            persona_id="elon_musk",
            display_name="Elon",
            runtime_selection="fake_agent",
            model_selection="fake-claude-4",
            reasoning_selection="medium",
        ),
    ]

    room = app.orchestrator.create_room(
        title="Innovators Roundtable",
        topic="Design and Engineering Fusion",
        participants=participants,
    )
    assert room.status == RoomStatus.LOBBY
    assert len(room.participants) == 2

    # 3. Start room (resolves and freezes bindings)
    active_room = await app.orchestrator.start_room(room.id)
    assert active_room.status == RoomStatus.ACTIVE
    assert "slot_steve" in active_room.binding_snapshots
    assert "slot_elon" in active_room.binding_snapshots

    # 4. Step turn 1 (Auto Director)
    events_t1 = []
    async for ev in app.orchestrator.step_turn(room.id):
        events_t1.append(ev)

    event_names_t1 = [e.get("event") for e in events_t1]
    assert "speaker_selected" in event_names_t1
    assert "agent_started" in event_names_t1
    assert "agent_message_delta" in event_names_t1
    assert "turn_completed" in event_names_t1

    room_after_t1 = app.orchestrator.get_room(room.id)
    assert room_after_t1 is not None
    assert room_after_t1.turn_index == 1
    assert len(room_after_t1.transcript) == 1

    # 5. Step turn 2 with manual speaker override + user message
    events_t2 = []
    async for ev in app.orchestrator.step_turn(
        room.id,
        manual_speaker_id="slot_elon",
        user_message="Elon, what is your perspective on first principles?",
    ):
        events_t2.append(ev)

    assert any(
        e.get("event") == "speaker_selected" and e.get("participant_id") == "slot_elon"
        for e in events_t2
    )
    room_after_t2 = app.orchestrator.get_room(room.id)
    assert room_after_t2 is not None
    assert room_after_t2.turn_index == 2
    assert len(room_after_t2.transcript) == 2

    # 6. Check transcripts in sqlite
    transcripts = app.orchestrator.list_room_transcripts(room.id)
    assert len(transcripts) == 2
    assert transcripts[1].participant_id == "slot_elon"
    assert transcripts[1].agent_runtime_id == "fake_agent"

    # 7. Stop room
    stopped_room = await app.orchestrator.stop_room(room.id)
    assert stopped_room.status == RoomStatus.STOPPED
