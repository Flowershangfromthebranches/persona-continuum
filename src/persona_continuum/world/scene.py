from __future__ import annotations

from typing import TYPE_CHECKING, Any

from persona_continuum.application._utils import new_id
from persona_continuum.room.models import (
    DirectorConfig,
    DirectorMode,
    ParticipantSlot,
    RoomMode,
)
from persona_continuum.world.firewall import TemporalKnowledgeFirewall
from persona_continuum.world.models import Actor, TimelineEvent, WorldState

if TYPE_CHECKING:
    from persona_continuum.application.container import PersonaContinuum


class SceneEngine:
    """Bridges direct inter-actor dialogues to the Tavern Multi-Agent Room Runtime.

    Reuses existing MultiAgentOrchestrator without modifying Tavern Room core.
    Upon scene completion, writes back memories, relationship updates, and world state.
    """

    def __init__(self, continuum: PersonaContinuum) -> None:
        self.continuum = continuum
        self.firewall = TemporalKnowledgeFirewall()

    async def execute_scene(
        self,
        world_id: str,
        branch_id: str,
        actors: list[Actor],
        topic: str,
        current_world_time: str,
        state: WorldState,
        max_turns: int = 2,
        participant_contexts: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[str, WorldState, list[Actor], TimelineEvent]:
        orchestrator = self.continuum.orchestrator

        # 1. Create participant slots with temporal firewall instructions
        firewall_instruction = self.firewall.build_firewall_prompt_instruction(current_world_time)
        slots: list[ParticipantSlot] = []
        for a in actors:
            # Map persona ID if available or use actor ID
            pid = a.persona_id or f"actor_{a.id}"
            slot = ParticipantSlot(
                participant_id=f"slot_{a.id}",
                persona_id=pid,
                display_name=a.name,
                runtime_selection=(
                    a.runtime_config.agent_id if a.runtime_config else "unconfigured"
                ),
                model_selection=(
                    a.runtime_config.model_id
                    if a.runtime_config
                    and a.runtime_config.model_id not in {"auto", "unconfigured"}
                    else "default"
                ),
                auth_profile_id=(a.runtime_config.credential_id if a.runtime_config else None),
                reasoning_selection=(
                    a.runtime_config.reasoning_effort
                    if a.runtime_config
                    and a.runtime_config.reasoning_effort not in {"", "none", "default"}
                    else "default"
                ),
                allow_manual_model_id=True,
            )
            slots.append(slot)

        # 2. Create room in Tavern orchestrator
        room_state = orchestrator.create_room(
            title=f"Parallel World Scene: {topic}",
            topic=topic,
            participants=slots,
            director_config=DirectorConfig(mode=DirectorMode.DIRECTOR),
            mode=RoomMode.AUTONOMOUS,
            metadata={
                "world_id": world_id,
                "branch_id": branch_id,
                "world_time": current_world_time,
                "mode": "parallel_world_scene",
                "firewall": firewall_instruction,
                "participant_prompt_blocks": dict(participant_contexts or {}),
            },
        )

        # 3. Start the real Tavern runtime and let Host + Director drive the scene.
        transcript_texts: list[str] = []
        await orchestrator.start_room(room_state.id)
        async for room_event in orchestrator.run_autonomous_discussion(
            room_state.id, max_turns=max_turns
        ):
            if room_event.get("event") == "turn_completed":
                turn = dict(room_event.get("turn") or {})
                transcript_texts.append(f"{turn.get('speaker_name')}: {turn.get('content')}")
        completed_room = orchestrator.get_room(room_state.id)
        if not transcript_texts or completed_room is None:
            raise RuntimeError("Parallel World scene produced no real Agent turns")

        scene_summary = (
            f"{', '.join(a.name for a in actors)} 就议题「{topic}」进行会谈。会谈记录：\n"
            + "\n".join(transcript_texts)
        )

        # 4. Write back updates to actors
        updated_actors: list[Actor] = []
        for a in actors:
            cloned = a.model_copy(deep=True)
            other_names = [o.name for o in actors if o.id != a.id]
            others_str = ", ".join(other_names)
            cloned.memory.append(
                {
                    "occurred_at": current_world_time,
                    "type": "scene_dialogue",
                    "topic": topic,
                    "content": f"Held strategic discussion with {others_str} regarding {topic}.",
                }
            )
            # Update affinity
            for other in actors:
                if other.id != a.id:
                    current_rel = cloned.relationships.get(other.id, 0.0)
                    cloned.relationships[other.id] = min(1.0, current_rel + 0.1)
            updated_actors.append(cloned)

        # 5. Write back to world state relationships
        mutated_state = state.model_copy(deep=True)
        if len(actors) >= 2:
            rel_key = f"{actors[0].id}:{actors[1].id}"
            mutated_state.relationships[rel_key] = {
                "affinity": 0.2,
                "status": "scene_concluded",
                "last_scene_topic": topic,
                "timestamp": current_world_time,
            }

        # 6. Emit timeline event
        event_id = new_id("evt")
        timeline_event = TimelineEvent(
            id=event_id,
            event_time=current_world_time,
            actors=[a.id for a in actors],
            cause=f"Direct scene engagement: {topic}",
            effect=f"Completed strategic conference between {', '.join(a.name for a in actors)}.",
            causal_chain=[],
            confidence=1.0,
            data={
                "room_id": room_state.id,
                "summary": scene_summary,
                "decision_source": "llm",
                "token_usage": completed_room.metadata.get("token_usage", {}),
            },
        )
        mutated_state.events.append(event_id)

        # Scene rooms are logical records, not long-lived physical runtime leases.
        # Closing here releases every participant and Host binding after the turn.
        await orchestrator.stop_room(room_state.id)

        return scene_summary, mutated_state, updated_actors, timeline_event
