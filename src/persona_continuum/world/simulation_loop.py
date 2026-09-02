from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from persona_continuum.world.decision import ActorDecisionEngine
from persona_continuum.world.director import WorldDirector
from persona_continuum.world.models import (
    ActionProposal,
    Actor,
    TimelineEvent,
    WorldMemoryRecord,
    WorldSeed,
    WorldState,
)


@dataclass
class SimulationReasoningBatch:
    proposals: list[ActionProposal] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    traces: dict[str, dict[str, Any]] = field(default_factory=dict)


class SimulationLoop:
    """Runs observe -> reason -> proposal for director-selected active actors.

    Actor proposals are generated under a bounded semaphore against the same
    immutable world snapshot (parallel), while proposal RESOLUTION stays fully
    serial in the engine (causal order unchanged).  No actor can observe
    another actor's same-tick proposal: decisions receive only the pre-tick
    snapshot they always received.
    """

    def __init__(
        self,
        director: WorldDirector,
        decision_engine: ActorDecisionEngine,
        *,
        max_parallel_actor_proposals: int = 4,
        max_active_actors: int = 6,
        parallel_enabled: bool = True,
    ) -> None:
        self.director = director
        self.decision_engine = decision_engine
        self.parallel_enabled = bool(parallel_enabled)
        self.max_active_actors = max(1, int(max_active_actors or 6))
        self._semaphore = asyncio.Semaphore(
            max(1, min(8, int(max_parallel_actor_proposals) if max_parallel_actor_proposals else 4))
        )
        self.concurrency_used = 0
        self._in_flight = 0
        self._peak_in_flight = 0

    async def _decide_one(
        self,
        *,
        world_id: str,
        branch_id: str,
        actor: Actor,
        state: WorldState,
        seed: WorldSeed,
        recent_events: list[TimelineEvent],
        memories: list[WorldMemoryRecord] | None = None,
        memory_loader: Callable[[Actor], list[WorldMemoryRecord]] | None = None,
    ) -> ActionProposal:
        async with self._semaphore:
            self._in_flight += 1
            self._peak_in_flight = max(self._peak_in_flight, self._in_flight)
            try:
                # Heavy memory retrieval happens here, for active actors
                # only, so a 50-actor world loads memories for the ~6
                # selected actors instead of all of them.
                if memories is None:
                    memories = memory_loader(actor) if memory_loader is not None else []
                return await self.decision_engine.decide(
                    world_id=world_id,
                    branch_id=branch_id,
                    actor=actor,
                    state=state,
                    seed=seed,
                    memories=memories,
                    recent_events=recent_events,
                )
            finally:
                self._in_flight -= 1

    async def collect_proposals(
        self,
        *,
        world_id: str,
        branch_id: str,
        seed: WorldSeed,
        state: WorldState,
        actors: list[Actor],
        recent_events: list[TimelineEvent],
        memories_by_actor: dict[str, list[WorldMemoryRecord]] | None = None,
        memory_loader: Callable[[Actor], list[WorldMemoryRecord]] | None = None,
    ) -> SimulationReasoningBatch:
        """Collect proposals for director-selected active actors.

        ``memory_loader`` (preferred) loads one actor's heavy memory lazily at
        decision time; ``memories_by_actor`` remains as a preloaded fallback
        for existing callers.
        """
        batch = SimulationReasoningBatch()
        active_actors = self.director.select_active_actors(
            actors,
            state,
            recent_events,
            limit=self.max_active_actors,
        )
        if not active_actors:
            return batch

        def memories_for(actor: Actor) -> list[WorldMemoryRecord]:
            if memory_loader is not None:
                return memory_loader(actor)
            return list((memories_by_actor or {}).get(actor.id) or [])

        if not self.parallel_enabled or len(active_actors) <= 1:
            for actor in active_actors:
                try:
                    proposal = await self.decision_engine.decide(
                        world_id=world_id,
                        branch_id=branch_id,
                        actor=actor,
                        state=state,
                        seed=seed,
                        memories=memories_for(actor),
                        recent_events=recent_events,
                    )
                    batch.proposals.append(proposal)
                    batch.traces[actor.id] = self.decision_engine.runtime.get_trace(
                        world_id, branch_id, actor.id
                    )
                except Exception as exc:
                    batch.errors[actor.id] = str(exc)
                    raise RuntimeError(
                        f"World simulation failed for actor {actor.id}: "
                        f"no LLM decision: {exc}"
                    ) from exc
            return batch

        # Deterministic order: results are appended in director order even
        # though decisions were produced concurrently.
        self._peak_in_flight = 0
        outcomes = await asyncio.gather(
            *(
                self._decide_one(
                    world_id=world_id,
                    branch_id=branch_id,
                    actor=actor,
                    state=state,
                    seed=seed,
                    recent_events=recent_events,
                    memory_loader=memory_loader,
                    memories=None if memory_loader is not None else memories_for(actor),
                )
                for actor in active_actors
            ),
            return_exceptions=True,
        )
        self.concurrency_used = self._peak_in_flight
        for actor, outcome in zip(active_actors, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                # Mirror serial semantics: any actor failure aborts the tick
                # deterministically by director order.
                batch.errors[actor.id] = str(outcome)
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                raise RuntimeError(
                    f"World simulation failed for actor {actor.id}: "
                    f"no LLM decision: {outcome}"
                ) from outcome
            batch.proposals.append(outcome)
            batch.traces[actor.id] = self.decision_engine.runtime.get_trace(
                world_id, branch_id, actor.id
            )
        return batch
