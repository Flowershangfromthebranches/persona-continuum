from __future__ import annotations

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.domain.memory import MemoryRecord, MemoryType
from persona_continuum.room.models import DirectorConfig, DirectorMode, ParticipantSlot, RoomStatus


@pytest.mark.anyio
async def test_e2e_multi_persona_room_debate(app: PersonaContinuum) -> None:
    # 1. Setup 3 distinct personas
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
            "aliases": ["Musk"],
            "persona_type": "historical",
            "run_mode": "continuation",
        }
    )
    app.personas.create_from_manifest(
        {
            "id": "friedrich_nietzsche",
            "display_name": "Friedrich Nietzsche",
            "persona_type": "historical",
            "run_mode": "continuation",
        }
    )

    # Add memories
    app.memories.add_memory(
        MemoryRecord(
            id="mem_musk_mars",
            persona_id="elon_musk",
            type=MemoryType.EPISODIC,
            source_kind="seed",
            content="I believe becoming a multiplanetary species is humanity's highest calling.",
            importance=0.9,
        )
    )
    app.memories.add_memory(
        MemoryRecord(
            id="mem_steve_mac",
            persona_id="steve_jobs",
            type=MemoryType.EPISODIC,
            source_kind="seed",
            content="We put an incredible amount of care into the typography and aesthetics.",
            importance=0.85,
        )
    )

    # 2. Configure Participant Slots with random and explicit selections
    participants = [
        ParticipantSlot(
            participant_id="slot_steve",
            persona_id="steve_jobs",
            runtime_selection="fake_agent",
            model_selection="fake-gpt-5",
            reasoning_selection="high",
        ),
        ParticipantSlot(
            participant_id="slot_elon",
            persona_id="elon_musk",
            runtime_selection="fake_agent",
            model_selection="random",
            reasoning_selection="random",
        ),
        ParticipantSlot(
            participant_id="slot_nietzsche",
            persona_id="friedrich_nietzsche",
            runtime_selection="fake_agent",
            model_selection="default",
            reasoning_selection="none",
        ),
    ]

    dir_cfg = DirectorConfig(mode=DirectorMode.DIRECTOR)
    room = app.orchestrator.create_room(
        title="Debate on Technology, Will, and Aesthetics",
        topic="What is the ultimate purpose of human technological creation?",
        participants=participants,
        director_config=dir_cfg,
    )

    # 3. Start room (freezes bindings)
    active_room = await app.orchestrator.start_room(room.id)
    assert active_room.status == RoomStatus.ACTIVE
    assert len(active_room.binding_snapshots) == 3

    # 4. Run Turn 1 (Auto Director)
    turn1_events = []
    async for ev in app.orchestrator.step_turn(room.id):
        turn1_events.append(ev)

    ev_types_t1 = [e.get("event") for e in turn1_events]
    assert "speaker_selected" in ev_types_t1
    assert "agent_started" in ev_types_t1
    assert "agent_message_delta" in ev_types_t1
    assert "persona_commit_completed" in ev_types_t1
    assert "turn_completed" in ev_types_t1

    # Verify event ordering
    spk_idx = ev_types_t1.index("speaker_selected")
    ag_start_idx = ev_types_t1.index("agent_started")
    done_idx = ev_types_t1.index("turn_completed")
    assert spk_idx < ag_start_idx < done_idx

    # 5. Run Turn 2 with a temporal inquiry targeting Musk to verify Recall Gate ordering
    turn2_events = []
    async for ev in app.orchestrator.step_turn(
        room.id,
        manual_speaker_id="slot_elon",
        user_message="以前 Musk 关于火星说过什么？",
    ):
        turn2_events.append(ev)

    ev_types_t2 = [e.get("event") for e in turn2_events]
    assert "recall_started" in ev_types_t2
    assert "recall_completed" in ev_types_t2
    assert "agent_started" in ev_types_t2

    # Strict Recall Gate ordering check: recall_started < recall_completed < agent_started
    r_start_idx = ev_types_t2.index("recall_started")
    r_done_idx = ev_types_t2.index("recall_completed")
    ag_start_idx_t2 = ev_types_t2.index("agent_started")
    assert r_start_idx < r_done_idx < ag_start_idx_t2

    # 6. Check state and transcripts
    final_room = app.orchestrator.get_room(room.id)
    assert final_room is not None
    assert final_room.turn_index == 2
    assert len(final_room.transcript) == 2

    # Transcripts in database
    transcripts = app.orchestrator.list_room_transcripts(room.id)
    assert len(transcripts) == 2
    assert len(transcripts[1].recall_ids) >= 1

    # Stop room
    await app.orchestrator.stop_room(room.id)
