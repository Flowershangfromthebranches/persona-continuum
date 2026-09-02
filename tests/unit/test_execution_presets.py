from __future__ import annotations

from persona_continuum.config import Config
from persona_continuum.world.director import WorldDirector
from persona_continuum.world.models import (
    ActionProposal,
    ActionType,
    Actor,
    ActorType,
    WorldSeed,
    WorldState,
)
from persona_continuum.world.simulation_loop import SimulationLoop


def test_optimized_and_legacy_presets_have_identical_quality_policy() -> None:
    opt = Config.optimized()
    legacy = Config.legacy_execution()
    # Quality knobs that are NOT execution-strategy switches stay equal: neither
    # preset can lower research depth, dimensions, or reasoning.
    assert opt.max_source_bytes == legacy.max_source_bytes
    # Execution-strategy switches differ (optimized on, legacy off).
    assert opt.runtime_pool_enabled is True and legacy.runtime_pool_enabled is False
    assert (
        opt.model_capability_cache_enabled is True
        and legacy.model_capability_cache_enabled is False
    )
    assert (
        opt.persona_incremental_extraction is True
        and legacy.persona_incremental_extraction is False
    )
    assert opt.persona_final_full_audit is True and legacy.persona_final_full_audit is False
    assert (
        opt.room_context_cursor_enabled is True and legacy.room_context_cursor_enabled is False
    )
    assert (
        opt.world_parallel_actor_proposals is True
        and legacy.world_parallel_actor_proposals is False
    )


class _SerialEngine:
    class runtime:  # noqa: N801
        @staticmethod
        def get_trace(world_id, branch_id, actor_id):  # type: ignore[no-untyped-def]
            return {}

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def decide(self, **kwargs):  # type: ignore[no-untyped-def]
        actor = kwargs["actor"]
        self.seen.append(actor.id)
        return ActionProposal(
            actor=actor.id,
            action_type=ActionType.OBSERVE,
            intent="t",
            reasoning_summary="r",
            expected_effect="e",
            confidence=0.5,
        )


def _actor(aid: str) -> Actor:
    return Actor(
        id=aid,
        name=aid,
        actor_type=ActorType.PERSONA_ACTOR,
        persona_id=aid,
    )


def test_legacy_loop_disables_parallel_proposals() -> None:
    import asyncio

    engine = _SerialEngine()
    director = WorldDirector()
    loop = SimulationLoop(
        director, engine, parallel_enabled=False  # type: ignore[arg-type]
    )
    assert loop.parallel_enabled is False
    actors = [_actor(f"a{i}") for i in range(4)]
    state = WorldState(timestamp="2012-01-01")

    async def run() -> None:
        batch = await loop.collect_proposals(
            world_id="w",
            branch_id="b",
            seed=WorldSeed(start_date="2012-01-01", simulation_end="2013"),
            state=state,
            actors=actors,
            recent_events=[],
            memories_by_actor={a.id: [] for a in actors},
        )
        # The director may pace selection; assert we got proposals and that
        # serial ordering follows director selection order.
        selected = [a.id for a in director.select_active_actors(actors, state, [])]
        assert [p.actor for p in batch.proposals] == selected
        assert len(batch.proposals) >= 1

    asyncio.run(run())
