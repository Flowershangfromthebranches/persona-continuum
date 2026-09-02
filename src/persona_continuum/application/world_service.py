from __future__ import annotations

from typing import TYPE_CHECKING, Any

from persona_continuum.application.world.entity_classification_service import (
    WorldEntityClassificationResult,
)
from persona_continuum.world.engine import ParallelWorldEngine
from persona_continuum.world.llm_builder import LLMWorldBuilder
from persona_continuum.world.models import (
    ActionResolution,
    Actor,
    ActorAction,
    OrganizationEntity,
    OutcomeEvaluation,
    ReplayTrajectory,
    SimulationBranch,
    TechnologyEntity,
    TimelineEvent,
    WorldMemoryRecord,
    WorldRecord,
    WorldRuntimeBinding,
    WorldSeed,
    WorldSimulationReport,
    WorldSnapshot,
    WorldState,
)
from persona_continuum.world.repository import WorldRepository

if TYPE_CHECKING:
    import asyncio

    from persona_continuum.application.container import PersonaContinuum


class WorldService:
    """Application facade for Parallel World simulation workflows and intelligence operations."""

    def __init__(self, continuum: PersonaContinuum, repository: WorldRepository) -> None:
        self.continuum = continuum
        self.repo = repository
        self.engine = ParallelWorldEngine(continuum, repository)

    @property
    def llm_builder(self) -> LLMWorldBuilder:
        return self.engine.llm_builder

    def subscribe_events(self, world_id: str) -> asyncio.Queue[dict[str, Any]]:
        return self.engine.subscribe_events(world_id)

    def unsubscribe_events(self, world_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self.engine.unsubscribe_events(world_id, queue)

    def create_world(
        self,
        description: str,
        title: str | None = None,
        baseline: str = "real_world",
        start_date: str | None = None,
        simulation_end: str | int = "2030",
        rules: list[str] | None = None,
        divergence_items: list[dict[str, str]] | None = None,
        immutable_facts: list[str] | None = None,
        initial_actors: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        builder_runtime: dict[str, Any] | None = None,
        default_actor_runtime: dict[str, Any] | None = None,
        actor_runtime_configs: dict[str, Any] | None = None,
        director_runtime: dict[str, Any] | None = None,
        evaluator_runtime: dict[str, Any] | None = None,
    ) -> tuple[WorldRecord, SimulationBranch, WorldState]:
        return self.engine.create_world(
            description=description,
            title=title,
            baseline=baseline,
            start_date=start_date,
            simulation_end=simulation_end,
            rules=rules,
            divergence_items=divergence_items,
            immutable_facts=immutable_facts,
            initial_actors=initial_actors,
            metadata=metadata,
            builder_runtime=builder_runtime,
            default_actor_runtime=default_actor_runtime,
            actor_runtime_configs=actor_runtime_configs,
            director_runtime=director_runtime,
            evaluator_runtime=evaluator_runtime,
        )

    def create_world_from_seed(
        self,
        seed: WorldSeed,
        title: str | None = None,
        description: str = "",
        builder_runtime: dict[str, Any] | None = None,
        default_actor_runtime: dict[str, Any] | None = None,
        actor_runtime_configs: dict[str, Any] | None = None,
        director_runtime: dict[str, Any] | None = None,
        evaluator_runtime: dict[str, Any] | None = None,
    ) -> tuple[WorldRecord, SimulationBranch, WorldState]:
        return self.engine.create_world_from_seed(
            seed=seed,
            title=title,
            description=description,
            builder_runtime=builder_runtime,
            default_actor_runtime=default_actor_runtime,
            actor_runtime_configs=actor_runtime_configs,
            director_runtime=director_runtime,
            evaluator_runtime=evaluator_runtime,
        )

    def list_runtime_bindings(self, world_id: str) -> list[WorldRuntimeBinding]:
        return self.repo.list_runtime_bindings(world_id)

    def get_runtime_binding(self, world_id: str, actor_id: str) -> WorldRuntimeBinding | None:
        return self.repo.get_runtime_binding(world_id, actor_id)

    def get_world(self, world_id: str) -> WorldRecord | None:
        return self.repo.get_world(world_id)

    def pause_world(self, world_id: str) -> WorldRecord:
        world = self.repo.get_world(world_id)
        if not world:
            raise KeyError(f"World not found: {world_id}")
        world.status = "paused"
        self.repo.save_world(world)
        return world

    def resume_world(self, world_id: str) -> WorldRecord:
        world = self.repo.get_world(world_id)
        if not world:
            raise KeyError(f"World not found: {world_id}")
        world.status = "active"
        self.repo.save_world(world)
        return world

    def list_worlds(self) -> list[WorldRecord]:
        return self.repo.list_worlds()

    def delete_world(self, world_id: str) -> bool:
        return self.repo.delete_world(world_id)

    def list_branches(self, world_id: str) -> list[SimulationBranch]:
        return self.repo.list_branches(world_id)

    def get_branch(self, branch_id: str) -> SimulationBranch | None:
        return self.repo.get_branch(branch_id)

    async def step_branch(
        self,
        world_id: str,
        branch_id: str,
        manual_actions: list[ActorAction] | None = None,
    ) -> tuple[WorldState, list[TimelineEvent], str]:
        return await self.engine.step_branch(world_id, branch_id, manual_actions)

    def inject_action(
        self, world_id: str, branch_id: str, action: ActorAction
    ) -> tuple[ActionResolution, WorldState, TimelineEvent]:
        return self.engine.inject_action(world_id, branch_id, action)

    async def trigger_scene(
        self, world_id: str, branch_id: str, actor_ids: list[str], topic: str
    ) -> tuple[str, TimelineEvent]:
        return await self.engine.trigger_scene(world_id, branch_id, actor_ids, topic)

    def fork_branch(
        self,
        world_id: str,
        source_branch_id: str,
        new_name: str,
        snapshot_id: str | None = None,
    ) -> SimulationBranch:
        return self.engine.fork_branch(world_id, source_branch_id, new_name, snapshot_id)

    async def evaluate_question(
        self,
        world_id: str,
        question: str,
        branch_count: int = 3,
        metrics: dict[str, float] | None = None,
    ) -> OutcomeEvaluation:
        return await self.engine.run_evaluation(world_id, question, branch_count, metrics)

    def query_causal_chain(
        self, world_id: str, branch_id: str, target: str
    ) -> list[dict[str, Any]]:
        return self.engine.query_causal_chain(world_id, branch_id, target)

    def get_replay_trajectory(self, world_id: str, branch_id: str) -> ReplayTrajectory:
        return self.engine.get_replay_trajectory(world_id, branch_id)

    def generate_report(self, world_id: str) -> WorldSimulationReport:
        return self.engine.generate_report(world_id)

    def list_events(self, world_id: str, branch_id: str) -> list[TimelineEvent]:
        return self.repo.list_events(world_id, branch_id)

    def list_snapshots(self, world_id: str, branch_id: str) -> list[WorldSnapshot]:
        return self.repo.list_snapshots(world_id, branch_id)

    def list_actors(self, world_id: str, branch_id: str) -> list[Actor]:
        return self.repo.list_actor_states(world_id, branch_id)

    def list_organizations(self, world_id: str, branch_id: str) -> dict[str, OrganizationEntity]:
        return self.repo.list_organizations(world_id, branch_id)

    def list_technologies(self, world_id: str, branch_id: str) -> dict[str, TechnologyEntity]:
        return self.repo.list_technologies(world_id, branch_id)

    def list_memories(
        self, world_id: str, branch_id: str, persona_id: str | None = None
    ) -> list[WorldMemoryRecord]:
        return self.repo.list_world_memories(world_id, branch_id, persona_id)

    async def classify_entities(
        self,
        *,
        description: str,
        entities: list[dict[str, Any]],
        runtime: dict[str, Any] | None = None,
        require_llm: bool = True,
    ) -> WorldEntityClassificationResult:
        runtime = dict(runtime or {})
        adapter_id = str(runtime.get("agent_id") or "")
        adapter = self.continuum.agent_registry.get_adapter(adapter_id) if adapter_id else None
        return await self.continuum.entity_classifier.classify(
            description=description,
            entities=entities,
            adapter=adapter,
            runtime=runtime,
            require_llm=require_llm,
        )
