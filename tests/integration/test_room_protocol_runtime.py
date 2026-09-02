from __future__ import annotations

import pytest

from persona_continuum.room.models import ParticipantSlot, RoomProtocolConfig, RoomProtocolType


def _manifest(persona_id: str) -> dict[str, str]:
    return {
        "id": persona_id,
        "display_name": persona_id,
        "persona_type": "fictional",
        "run_mode": "continuation",
    }


@pytest.mark.anyio
async def test_real_orchestrator_expert_protocol_uses_existing_agent_abstraction(app) -> None:
    for persona_id in ("protocol_host", "protocol_expert_a", "protocol_expert_b"):
        app.personas.create_from_manifest(_manifest(persona_id))
    participants = [
        ParticipantSlot(
            participant_id="host",
            persona_id="protocol_host",
            role="host",
            runtime_selection="fake_agent",
            model_selection="fake-gpt-5",
            reasoning_selection="low",
        ),
        ParticipantSlot(
            participant_id="expert_a",
            persona_id="protocol_expert_a",
            role="expert",
            specialties=["backend"],
            runtime_selection="fake_agent",
            model_selection="fake-gpt-5",
            reasoning_selection="low",
        ),
        ParticipantSlot(
            participant_id="expert_b",
            persona_id="protocol_expert_b",
            role="expert",
            specialties=["frontend"],
            runtime_selection="fake_agent",
            model_selection="fake-gpt-5",
            reasoning_selection="low",
        ),
    ]
    room = app.orchestrator.create_room(
        title="protocol integration",
        topic="backend and frontend architecture",
        participants=participants,
        host_participant_id="host",
        protocol=RoomProtocolType.EXPERT_CONSULTATION,
        protocol_config=RoomProtocolConfig(min_experts=2, max_experts=2),
    )
    await app.orchestrator.start_room(room.id)
    completed = await app.orchestrator.run_protocol(room.id, room.topic or "")

    assert completed.status.value == "ready"
    assert completed.protocol_state.status.value == "success"
    assert set(completed.protocol_state.selected_expert_ids) == {"expert_a", "expert_b"}
    assert len(completed.protocol_state.submissions) == 2
    assert len(completed.protocol_state.reviews) == 2
    assert completed.protocol_state.final_result
    events = app.orchestrator.protocol_repository.list_events(
        room.id, completed.protocol_state.run_id
    )
    event_types = [event.event_type for event in events]
    assert "analysis_submitted" in event_types
    assert "review_submitted" in event_types
    assert event_types[-1] == "final_response"


@pytest.mark.anyio
async def test_ten_logical_protocol_tasks_finish_with_four_scheduler_slots(app) -> None:
    app.execution_scheduler.llm_semaphore.shrink(100, floor=4)
    persona_ids = ["capacity_host", *[f"capacity_{index}" for index in range(10)]]
    for persona_id in persona_ids:
        app.personas.create_from_manifest(_manifest(persona_id))
    participants = [
        ParticipantSlot(
            participant_id="host",
            persona_id="capacity_host",
            role="host",
            runtime_selection="fake_agent",
            model_selection="fake-gpt-5",
        ),
        *[
            ParticipantSlot(
                participant_id=f"expert_{index}",
                persona_id=f"capacity_{index}",
                role="expert",
                specialties=[f"area_{index}"],
                runtime_selection="fake_agent",
                model_selection="fake-gpt-5",
            )
            for index in range(10)
        ],
    ]
    room = app.orchestrator.create_room(
        title="capacity",
        topic="all",
        participants=participants,
        protocol=RoomProtocolType.EXPERT_CONSULTATION,
        protocol_config=RoomProtocolConfig(
            routing_mode="all",
            min_experts=10,
            max_experts=10,
            cross_review=False,
        ),
    )
    await app.orchestrator.start_room(room.id)
    completed = await app.orchestrator.run_protocol(room.id, room.topic or "")
    assert completed.protocol_state.status.value == "success"
    assert len(completed.protocol_state.submissions) == 10
    snapshot = app.execution_scheduler.snapshot()
    assert snapshot.llm_waiting == 0
    assert snapshot.llm_available == snapshot.per_adapter["fake_agent"]["available"] == 4
