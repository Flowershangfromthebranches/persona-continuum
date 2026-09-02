from __future__ import annotations

from persona_continuum.world.agent_runtime import ActorRuntime
from persona_continuum.world.models import (
    ActionProposal,
    Actor,
    TimelineEvent,
    WorldMemoryRecord,
    WorldSeed,
    WorldState,
)


class ActorDecisionEngine:
    """Compatibility facade around ActorRuntime; it contains no actor-specific behavior."""

    def __init__(self, runtime: ActorRuntime) -> None:
        self.runtime = runtime

    async def decide(
        self,
        *,
        world_id: str,
        branch_id: str,
        actor: Actor,
        state: WorldState,
        seed: WorldSeed,
        memories: list[WorldMemoryRecord],
        recent_events: list[TimelineEvent],
    ) -> ActionProposal:
        return await self.runtime.reason(
            world_id=world_id,
            branch_id=branch_id,
            actor=actor,
            state=state,
            seed=seed,
            memories=memories,
            recent_events=recent_events,
        )
