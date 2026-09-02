from __future__ import annotations

import asyncio

import pytest

from persona_continuum.agent.adapters.fake import FakeAgentAdapter
from persona_continuum.agent.models import AgentEvent, AgentEventType
from persona_continuum.room.models import ParticipantSlot, RoomMode


class PersistentFastRuntime(FakeAgentAdapter):
    def __init__(self) -> None:
        super().__init__(adapter_id="scale_fake", name="Scale Fake", chunk_delay_sec=0)

    def supports_persistent_conversation(self, session) -> bool:  # type: ignore[no-untyped-def]
        return True


class CrashOncePersistentRuntime(PersistentFastRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.adapter_id = "crash_once_scale"
        self.calls = 0

    async def send(self, session, turn):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == 2:
            yield AgentEvent(
                type=AgentEventType.ERROR,
                error="Agent transport process exited",
                metadata={"failure_code": "AGENT_PROCESS_EXITED"},
            )
            return
        async for event in super().send(session, turn):
            yield event


@pytest.mark.anyio
async def test_room_supports_twelve_persistent_participants(app) -> None:
    runtime = PersistentFastRuntime()
    app.agent_registry.register_adapter(runtime)
    await app.agent_discovery.scan(force_refresh=True)
    participants: list[ParticipantSlot] = []
    for index in range(12):
        persona = app.personas.create_from_manifest(
            {
                "id": f"scale_room_persona_{index}",
                "display_name": f"Scale Room {index}",
                "persona_type": "fictional",
                "run_mode": "continuation",
            }
        )
        participants.append(
            ParticipantSlot(
                participant_id=f"participant_{index}",
                persona_id=persona.id,
                display_name=persona.manifest.display_name,
                runtime_selection=runtime.adapter_id,
                model_selection="fake-gpt-5",
                allow_agent_tools=False,
            )
        )
    room = app.orchestrator.create_room(
        title="12 persistent participants",
        topic="bounded context",
        mode=RoomMode.MANUAL,
        participants=participants,
    )
    await asyncio.wait_for(app.orchestrator.start_room(room.id), timeout=5)
    assert len(app.orchestrator._active_agent_bindings[room.id]) == 12

    for participant in participants:
        events = []
        async for event in app.orchestrator.step_turn(
            room.id,
            manual_speaker_id=participant.participant_id,
            user_message="continue",
        ):
            events.append(event)
        assert any(event.get("event") == "agent_completed" for event in events)

    # A returning persistent participant receives only the transcript delta.
    async for _ in app.orchestrator.step_turn(
        room.id,
        manual_speaker_id=participants[0].participant_id,
        user_message="continue once more",
    ):
        pass
    assert app.orchestrator.context_manager.snapshot()["delta_sends"] >= 1
    await app.orchestrator.stop_room(room.id)


@pytest.mark.anyio
async def test_world_supports_ten_persistent_actor_sessions(app) -> None:
    runtime = PersistentFastRuntime()
    app.agent_registry.register_adapter(runtime)
    await app.agent_discovery.scan(force_refresh=True)
    names: list[str] = []
    for index in range(10):
        name = f"Scale Actor {index}"
        names.append(name)
        app.personas.create_from_manifest(
            {
                "id": f"scale_world_persona_{index}",
                "display_name": name,
                "persona_type": "historical",
                "run_mode": "continuation",
            }
        )
    world, branch, state = app.worlds.create_world(
        description="ten actors independently propose bounded actions",
        start_date="2012-01-01",
        default_actor_runtime={
            "agent_id": runtime.adapter_id,
            "model_id": "fake-gpt-5",
            "reasoning_effort": "high",
            "runtime_source": "test",
        },
        initial_actors=names,
    )
    actors = app.worlds.list_actors(world.id, branch.id)
    assert len(actors) == 10
    app.worlds.engine.simulation_loop.max_active_actors = 10
    batch = await asyncio.wait_for(
        app.worlds.engine.simulation_loop.collect_proposals(
            world_id=world.id,
            branch_id=branch.id,
            seed=world.seed,
            state=state,
            actors=actors,
            recent_events=[],
            memories_by_actor={actor.id: [] for actor in actors},
        ),
        timeout=8,
    )
    assert len(batch.proposals) == 10
    assert app.worlds.engine.simulation_loop.concurrency_used <= 4
    assert len(app.worlds.engine.actor_runtime._sessions) >= 10


@pytest.mark.anyio
async def test_room_runtime_crash_recreates_thread_and_rehydrates_bounded_context(app) -> None:
    runtime = CrashOncePersistentRuntime()
    app.agent_registry.register_adapter(runtime)
    await app.agent_discovery.scan(force_refresh=True)
    persona = app.personas.create_from_manifest(
        {
            "id": "scale_rehydrate_persona",
            "display_name": "Scale Rehydrate",
            "persona_type": "fictional",
            "run_mode": "continuation",
        }
    )
    room = app.orchestrator.create_room(
        title="rehydration",
        topic="runtime recovery",
        mode=RoomMode.MANUAL,
        participants=[
            ParticipantSlot(
                participant_id="speaker",
                persona_id=persona.id,
                display_name="Scale Rehydrate",
                runtime_selection=runtime.adapter_id,
                model_selection="fake-gpt-5",
                allow_agent_tools=False,
            )
        ],
    )
    await app.orchestrator.start_room(room.id)
    async for _ in app.orchestrator.step_turn(
        room.id, manual_speaker_id="speaker", user_message="first"
    ):
        pass
    failed_events = []
    async for event in app.orchestrator.step_turn(
        room.id, manual_speaker_id="speaker", user_message="crash now"
    ):
        failed_events.append(event)
    assert any(event.get("event") == "room_runtime_recovered" for event in failed_events)

    recovered_events = []
    async for event in app.orchestrator.step_turn(
        room.id, manual_speaker_id="speaker", user_message="after restart"
    ):
        recovered_events.append(event)
    assert any(event.get("event") == "room_context_rehydrated" for event in recovered_events)
    assert any(event.get("event") == "agent_completed" for event in recovered_events)
    await app.orchestrator.stop_room(room.id)
