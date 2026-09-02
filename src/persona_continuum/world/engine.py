from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from persona_continuum.world.actions import ActionResolver
from persona_continuum.world.actors import ActorManager
from persona_continuum.world.agent_runtime import ActorRuntime
from persona_continuum.world.branch import BranchManager
from persona_continuum.world.builder import WorldBuilderService
from persona_continuum.world.causal import CausalGraphEngine
from persona_continuum.world.clock import AdaptiveWorldClock
from persona_continuum.world.decision import ActorDecisionEngine
from persona_continuum.world.director import WorldDirector
from persona_continuum.world.economy import EconomicSimulationLayer
from persona_continuum.world.evaluator import OutcomeEvaluator
from persona_continuum.world.events import EventEngine
from persona_continuum.world.firewall import TemporalKnowledgeFirewall
from persona_continuum.world.generator import WorldEventGenerator
from persona_continuum.world.llm_builder import LLMWorldBuilder
from persona_continuum.world.models import (
    ActionResolution,
    ActionType,
    Actor,
    ActorAction,
    ActorRuntimeConfig,
    ActorType,
    CausalNodeType,
    CausalRelationType,
    MemoryCategory,
    OrganizationEntity,
    OutcomeEvaluation,
    ReplayTrajectory,
    SimulationBranch,
    TechnologyEntity,
    TimelineEvent,
    WorldDirectorPolicy,
    WorldMemoryRecord,
    WorldRecord,
    WorldRuntimeBinding,
    WorldSeed,
    WorldSimulationReport,
    WorldState,
)
from persona_continuum.world.organization import OrganizationManager
from persona_continuum.world.persona_memory import PersonaMemoryEvolution
from persona_continuum.world.replay import WorldReplayEngine
from persona_continuum.world.report import SimulationReportGenerator
from persona_continuum.world.repository import WorldRepository
from persona_continuum.world.scene import SceneEngine
from persona_continuum.world.simulation_loop import SimulationLoop
from persona_continuum.world.state import WorldStateManager
from persona_continuum.world.technology import TechnologyEvolutionModel

if TYPE_CHECKING:
    from persona_continuum.application.container import PersonaContinuum

logger = logging.getLogger(__name__)


class ParallelWorldEngine:
    """Orchestrator for the Autonomous Parallel World Intelligence Layer runtime."""

    def __init__(self, continuum: PersonaContinuum, repository: WorldRepository) -> None:
        self.continuum = continuum
        self.repo = repository
        self.builder = WorldBuilderService()
        self.llm_builder = LLMWorldBuilder(self.builder)
        self.state_mgr = WorldStateManager()
        self.actor_mgr = ActorManager()
        self.action_resolver = ActionResolver()
        self.clock = AdaptiveWorldClock()
        self.events = EventEngine()
        self.scene_engine = SceneEngine(continuum)
        self.branches = BranchManager()
        self.evaluator = OutcomeEvaluator()
        self.firewall = TemporalKnowledgeFirewall()

        # Intelligence Layer Subsystems
        self.director = WorldDirector()
        self.causal_engine = CausalGraphEngine()
        self.org_mgr = OrganizationManager()
        self.tech_model = TechnologyEvolutionModel()
        self.economy = EconomicSimulationLayer()
        self.memory_evolution = PersonaMemoryEvolution()
        self.actor_runtime = ActorRuntime(continuum)
        self.decision_engine = ActorDecisionEngine(self.actor_runtime)
        _world_cfg = getattr(continuum, "config", None)
        self.simulation_loop = SimulationLoop(
            self.director,
            self.decision_engine,
            max_parallel_actor_proposals=int(
                getattr(_world_cfg, "world_actor_proposal_concurrency", 4) or 4
            ),
            max_active_actors=int(
                getattr(_world_cfg, "world_active_actor_limit", 6) or 6
            ),
            parallel_enabled=bool(
                getattr(_world_cfg, "world_parallel_actor_proposals", True)
            ),
        )
        self.event_gen = WorldEventGenerator()
        self.replay_engine = WorldReplayEngine()
        self.report_gen = SimulationReportGenerator()
        self._event_subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}

    def subscribe_events(self, world_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._event_subscribers.setdefault(world_id, []).append(queue)
        return queue

    def unsubscribe_events(self, world_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        if world_id in self._event_subscribers:
            self._event_subscribers[world_id] = [
                q for q in self._event_subscribers[world_id] if q is not queue
            ]

    async def _broadcast_event(self, world_id: str, event: dict[str, Any]) -> None:
        subscribers = list(self._event_subscribers.get(world_id, []))
        for queue in subscribers:
            with contextlib.suppress(Exception):
                queue.put_nowait(event)

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
        # 1. Build structured seed
        seed_metadata = dict(metadata or {})
        if initial_actors is not None:
            seed_metadata["restrict_actor_runtime"] = True
        if actor_runtime_configs:
            seed_metadata["actor_runtime_configs"] = actor_runtime_configs

        seed = self.builder.build_seed(
            description=description,
            baseline=baseline,
            start_date=start_date,
            simulation_end=simulation_end,
            rules=rules,
            divergence_items=divergence_items,
            immutable_facts=immutable_facts,
            initial_actors=initial_actors,
            metadata=seed_metadata,
        )

        return self.create_world_from_seed(
            seed=seed,
            title=title,
            description=description,
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
        fallback_title = (
            description[:36] + "..." if len(description) > 36 else description
        ) or "Parallel World"
        world_title = title or fallback_title
        world = WorldRecord(
            title=world_title,
            description=description or world_title,
            seed=seed,
            status="active",
            builder_runtime=builder_runtime or {},
            default_actor_runtime=default_actor_runtime or {},
            director_runtime=director_runtime or {},
            evaluator_runtime=evaluator_runtime or {},
        )
        self.repo.save_world(world)

        initial_state = self.state_mgr.initialize_state_from_seed(seed)
        default_runtime_obj: ActorRuntimeConfig | None = None
        if default_actor_runtime and default_actor_runtime.get("agent_id"):
            cred_id = default_actor_runtime.get("auth_profile_id") or default_actor_runtime.get(
                "credential_id"
            )
            default_runtime_obj = ActorRuntimeConfig(
                persona_id="runtime_default",
                agent_id=default_actor_runtime["agent_id"],
                model_id=default_actor_runtime.get("model_id", "default"),
                runtime_source=default_actor_runtime.get("runtime_source", "local_cli"),
                reasoning_effort=default_actor_runtime.get("reasoning_effort", "none"),
                credential_provider=default_actor_runtime.get(
                    "credential_provider", "unconfigured"
                ),
                credential_id=cred_id,
                auth_profile_id=default_actor_runtime.get("auth_profile_id"),
            )
        elif self.continuum.agent_registry.get_adapter("fake_agent") is not None:
            default_runtime_obj = ActorRuntimeConfig(
                persona_id="runtime_test",
                agent_id="fake_agent",
                model_id="fake-gpt-5",
                credential_provider="test",
            )
        else:
            cached = self.continuum.agent_discovery.get_cached_probes()
            ready_probes = [p for p in cached if getattr(p, "status", None) == "ready"]
            if ready_probes:
                first_ready = ready_probes[0]
                first_model = first_ready.models[0].id if first_ready.models else "default"
                default_runtime_obj = ActorRuntimeConfig(
                    persona_id="runtime_default",
                    agent_id=first_ready.id,
                    model_id=first_model,
                    runtime_source=getattr(first_ready, "runtime_source", "local_cli"),
                    reasoning_effort="none",
                )

        if actor_runtime_configs:
            seed.metadata["actor_runtime_configs"] = actor_runtime_configs

        actors = self.actor_mgr.initialize_actors_for_world(seed, default_runtime_obj)
        available_personas = self.continuum.personas.list()
        available_by_id = {persona.id: persona for persona in available_personas}
        for actor in actors.values():
            if actor.actor_type != ActorType.PERSONA_ACTOR or actor.persona_id in available_by_id:
                continue
            actor_key = re.sub(r"[^a-z0-9]", "", actor.id.lower())
            name_key = re.sub(r"[^a-z0-9]", "", actor.name.lower())
            matched = next(
                (
                    persona
                    for persona in available_personas
                    if actor_key == re.sub(r"[^a-z0-9]", "", persona.id.lower())
                    or name_key == re.sub(r"[^a-z0-9]", "", persona.display_name.lower())
                    or name_key in {
                        re.sub(r"[^a-z0-9]", "", alias.lower())
                        for alias in persona.manifest.aliases
                    }
                ),
                None,
            )
            if matched and actor.runtime_config:
                actor.persona_id = matched.id
                actor.runtime_config = actor.runtime_config.model_copy(
                    update={"persona_id": matched.id}
                )

        # Freeze runtime bindings in repository
        bindings: list[WorldRuntimeBinding] = []
        for a in actors.values():
            cfg = a.runtime_config
            bindings.append(
                WorldRuntimeBinding(
                    world_id=world.id,
                    actor_id=a.id,
                    persona_id=a.persona_id,
                    profile_id=a.profile_id,
                    profile_type=a.profile_type,
                    runtime_source=cfg.runtime_source if cfg else "local_cli",
                    agent_id=cfg.agent_id if cfg else "default",
                    model_id=cfg.model_id if cfg else "default",
                    reasoning_effort=cfg.reasoning_effort if cfg else "none",
                    auth_profile_id=cfg.auth_profile_id if cfg else None,
                )
            )
        self.repo.save_runtime_bindings(world.id, bindings)

        # 3. Create root branch (main)
        branch = self.branches.create_root_branch(
            world_id=world.id, name="main", initial_state=initial_state
        )
        self.repo.save_branch(branch)

        # 4. Save initial snapshot
        snapshot = self.state_mgr.snapshot(initial_state, world.id, branch.id)
        self.repo.save_snapshot(snapshot)

        # 5. Save actors to repository
        for a in actors.values():
            self.repo.save_actor_state(world.id, branch.id, a)
            # Initialize identity memory
            self.memory_evolution.add_identity_memory(
                persona_id=a.persona_id or a.id,
                content=f"Core identity and philosophical conviction of {a.name}.",
                importance=0.95,
            )
            self.repo.save_world_memory(
                world.id,
                branch.id,
                self.memory_evolution.get_memories_for_persona(
                    a.persona_id or a.id, MemoryCategory.IDENTITY
                )[-1],
            )

        # 6. Initialize Director & Policies
        director_policy = WorldDirectorPolicy()
        self.repo.save_director(world.id, branch.id, director_policy)

        # 7. Initialize Organizations & Technologies in repository
        for org in self.org_mgr.organizations.values():
            self.repo.save_organization(world.id, branch.id, org)
        for tech in self.tech_model.technologies.values():
            self.repo.save_technology(world.id, branch.id, tech)

        # 8. Record divergence in timeline & causal graph
        div_desc = "; ".join(f"{d.condition} -> {d.consequence}" for d in seed.divergence)
        init_ev = self.events.emit_event(
            event_time=seed.start_date,
            cause=f"Timeline divergence initiated from {seed.baseline_world}",
            effect=div_desc,
            actors=seed.initial_actors,
            confidence=1.0,
            data={"divergence": [d.model_dump() for d in seed.divergence]},
        )
        initial_state.events.append(init_ev.id)
        self.repo.save_event(world.id, branch.id, init_ev)

        # Record root causal node
        root_cnode = self.causal_engine.record_node(
            node_type=CausalNodeType.EVENT,
            name=f"Divergence: {div_desc}",
            timestamp=seed.start_date,
            properties={"divergence": div_desc, "baseline": seed.baseline_world},
            node_id=f"div_root_{world.id}",
        )
        self.causal_engine.record_event(init_ev, antecedent_node_ids=[root_cnode.id])
        self.repo.save_causal_graph(world.id, branch.id, self.causal_engine.graph)

        # Record initial replay step
        self.replay_engine.record_step(
            timestamp=seed.start_date,
            state=initial_state,
            active_events=[init_ev],
            causal_milestones=[f"Divergence @ {seed.start_date}"],
            diff_summary="World simulation initialized from seed divergence.",
        )
        self.repo.save_replay_step(world.id, branch.id, self.replay_engine.steps[-1])

        return world, branch, initial_state

    async def step_branch(
        self,
        world_id: str,
        branch_id: str,
        manual_actions: list[ActorAction] | None = None,
    ) -> tuple[WorldState, list[TimelineEvent], str]:
        branch = self.repo.get_branch(branch_id)
        if not branch:
            raise KeyError(f"Branch not found: {branch_id}")
        world = self.repo.get_world(world_id)
        if world is None:
            raise KeyError(f"World not found: {world_id}")
        if world.status != "active":
            raise RuntimeError(f"World {world_id} is not active (status: {world.status})")

        current_state = branch.current_state or WorldState(timestamp=world.seed.start_date)
        actors_list = self.repo.list_actor_states(world_id, branch_id)
        actors_map = {a.id: a for a in actors_list}
        recent_events = self.repo.list_events(world_id, branch_id)

        # Load organizations & technologies from storage; worlds whose seed
        # provided initial conditions fall back to the branch state copies.
        stored_orgs = self.repo.list_organizations(world_id, branch_id)
        if stored_orgs:
            self.org_mgr.organizations = stored_orgs
        elif current_state.organizations:
            self.org_mgr.organizations = {
                oid: OrganizationEntity.model_validate({"id": oid, **o})
                for oid, o in current_state.organizations.items()
            }
        stored_techs = self.repo.list_technologies(world_id, branch_id)
        if stored_techs:
            self.tech_model.technologies = stored_techs
        elif current_state.technologies:
            self.tech_model.technologies = {
                tid: TechnologyEntity.model_validate({"id": tid, **t})
                for tid, t in current_state.technologies.items()
            }

        # 1. World Director evaluates world pacing & potential milestones
        director_data = self.repo.get_director(world_id, branch_id)
        if director_data:
            self.director.policy = director_data[0]
        self.director.evaluate_next_step(current_state, recent_events)

        # 2. Gather actions (manual interventions or AgentAdapter-backed proposals)
        await self._broadcast_event(
            world_id,
            {
                "event": "world_step_started",
                "world_id": world_id,
                "branch_id": branch_id,
                "timestamp": current_state.timestamp,
            },
        )
        actions_to_run: list[ActorAction] = list(manual_actions or [])
        proposals_by_actor: dict[str, Any] = {}
        runtime_traces: dict[str, dict[str, Any]] = {}
        tick_task_id: str | None = None
        from persona_continuum.performance.tracing import default_tracer

        world_tracer = default_tracer()
        if not actions_to_run:
            tick_task_id = f"world:{world_id}:{branch_id}:{current_state.timestamp}"
            tracer = world_tracer
            tracer.start_task("world_tick", tick_task_id)
            tracer.observe(tick_task_id, "actor_count", len(actors_list))
            with tracer.span(tick_task_id, "proposal_generation"):
                # Heavy memory retrieval is lazy: only the director-selected
                # active actors load their memory, not the whole roster.
                memory_loaded: list[str] = []

                def _load_actor_memories(actor: Actor) -> list[WorldMemoryRecord]:
                    memory_loaded.append(actor.id)
                    return self.repo.list_world_memories(
                        world_id, branch_id, actor.persona_id or actor.id
                    )

                reasoning_batch = await self.simulation_loop.collect_proposals(
                    world_id=world_id,
                    branch_id=branch_id,
                    seed=world.seed,
                    state=current_state,
                    actors=actors_list,
                    recent_events=recent_events,
                    memory_loader=_load_actor_memories,
                )
                tracer.observe(tick_task_id, "memory_loaded_actor_count", len(memory_loaded))
            proposals_by_actor = {p.actor: p for p in reasoning_batch.proposals}
            runtime_traces = reasoning_batch.traces
            tracer.observe(
                tick_task_id,
                "concurrent_actor_count",
                getattr(self.simulation_loop, "concurrency_used", 0),
            )
            for p in reasoning_batch.proposals:
                await self._broadcast_event(
                    world_id,
                    {
                        "event": "world_actor_proposal",
                        "world_id": world_id,
                        "branch_id": branch_id,
                        "actor_id": p.actor,
                        "proposal": p.model_dump(mode="json"),
                        "trace": runtime_traces.get(p.actor, {}),
                    },
                )
            actions_to_run = [
                proposal.to_action(current_state.timestamp)
                for proposal in reasoning_batch.proposals
            ]
            if not actions_to_run:
                details = "; ".join(
                    f"{actor_id}: {error}" for actor_id, error in reasoning_batch.errors.items()
                )
                raise RuntimeError(
                    f"World simulation blocked: no Agent proposal generated. {details}"
                )

        # 3. Compute adaptive timestep
        next_timestamp, step_unit = self.clock.compute_next_timestep(
            current_time_str=current_state.timestamp,
            state=current_state,
            pending_actions=actions_to_run,
            recent_events=recent_events,
        )

        # 4. Resolve actions, organizational dynamics & technology evolution
        # The Director controls pacing; actor proposals remain the origin of world events.
        # All tick persistence below shares ONE transaction: a failed tick can
        # never leave partial events/memories/snapshots behind, and Actor B
        # still sees Actor A's same-tick writes through the same connection.
        resolution_started = time.monotonic()
        new_events: list[TimelineEvent] = []
        mutated_state = current_state.model_copy(deep=True)
        mutated_state.timestamp = next_timestamp
        repo_has_db = hasattr(self.repo, "db")
        tick_transaction = (
            self.repo.db.transaction() if repo_has_db else contextlib.nullcontext()
        )
        with tick_transaction:
            for act in actions_to_run:
                act_actor = actors_map.get(act.actor_id)
                if not act_actor:
                    continue

                await self._broadcast_event(
                    world_id,
                    {
                        "event": "world_action_resolving",
                        "world_id": world_id,
                        "branch_id": branch_id,
                        "actor_id": act.actor_id,
                        "action_type": act.action_type.value,
                        "description": act.description,
                    },
                )
                proposal = proposals_by_actor.get(act.actor_id)
                trace = runtime_traces.get(act.actor_id, {})
                if proposal is not None:
                    resolution, mutated_state, act = self.action_resolver.resolve_proposal(
                        proposal, act_actor, mutated_state
                    )
                else:
                    resolution, mutated_state = self.action_resolver.resolve_action(
                        act, act_actor, mutated_state
                    )

                # Secondary organization and technology models only apply a feasible proposal.
                if resolution.success and act.action_type == ActionType.HIRE:
                    dept = act.parameters.get("department", "Silicon Engineering")
                    headcount = act.parameters.get("headcount")
                    count = int(headcount or act.parameters.get("count") or 50)
                    self.org_mgr.hire(act.actor_id, dept, count)
                elif resolution.success and act.action_type == ActionType.INVEST:
                    target_proj = act.target or "Strategic_Investment"
                    amt = float(act.parameters.get("amount_billions", 1.0))
                    self.org_mgr.invest(act.actor_id, target_proj, amt)
                elif resolution.success and act.action_type == ActionType.RESEARCH:
                    tech_id = act.target or "finfet_lithography"
                    res_out = self.tech_model.advance(
                        tech_id,
                        investment_billions=float(act.parameters.get("investment_billions", 1.5)),
                        engineer_years=int(act.parameters.get("engineer_years", 300)),
                        lead_org_id=act.actor_id,
                    )
                    if res_out.get("success"):
                        tech_obj = self.tech_model.get_technology(tech_id)
                        if tech_obj:
                            tech_ev = self.event_gen.generate_from_tech_breakthrough(
                                tech_id=tech_id,
                                tech_name=tech_obj.name,
                                lead_org_id=act.actor_id,
                                old_maturity=res_out["old_maturity"],
                                new_maturity=res_out["new_maturity"],
                                timestamp=next_timestamp,
                            )
                            new_events.append(tech_ev)

                # Generate strictly grounded TimelineEvent
                ev = self.event_gen.generate_from_action_resolution(act, resolution, next_timestamp)
                if proposal is not None:
                    ev.data["proposal"] = proposal.model_dump(mode="json")
                    ev.data["runtime"] = trace
                    ev.data["decision_source"] = "llm"
                    ev.data["resolution_status"] = (
                        resolution.status.value if resolution.status else "failure"
                    )
                else:
                    ev.data["decision_source"] = "user_injected"
                new_events.append(ev)
                logger.info(
                    "world_actor_step actor=%s model=%s decision_source=%s stage=event event=%s",
                    act_actor.name,
                    trace.get("model_id", "user"),
                    ev.data["decision_source"],
                    ev.id,
                )
                mutated_state.events.append(ev.id)
                self.repo.save_event(world_id, branch_id, ev)
                await self._broadcast_event(
                    world_id,
                    {
                        "event": "world_event_committed",
                        "world_id": world_id,
                        "branch_id": branch_id,
                        "timeline_event": ev.model_dump(mode="json"),
                    },
                )

                # Update Causal Graph
                evt_cnode = self.causal_engine.record_event(ev)
                if act.actor_id:
                    act_node = self.causal_engine.record_node(
                        node_type=CausalNodeType.DECISION,
                        name=f"Decision by {act.actor_id}: {act.description}",
                        timestamp=next_timestamp,
                        node_id=f"dec_{act.id}",
                    )
                    self.causal_engine.link(act_node.id, evt_cnode.id, CausalRelationType.CAUSES)

                # Record and evolve persona memory & beliefs
                mem_content = (
                    f"At {next_timestamp}, executed {act.action_type.value}: "
                    f"{resolution.outcome_description}"
                )
                mem_rec = self.memory_evolution.add_world_experience(
                    persona_id=act_actor.persona_id or act_actor.id,
                    content=mem_content,
                    occurred_at=next_timestamp,
                    emotional_valence=0.4 if resolution.success else -0.3,
                    source_event_id=ev.id,
                )
                self.repo.save_world_memory(world_id, branch_id, mem_rec)
                logger.info(
                    "world_actor_step actor=%s model=%s decision_source=%s stage=memory",
                    act_actor.name,
                    trace.get("model_id", "user"),
                    ev.data["decision_source"],
                )

                if act.action_type == ActionType.NEGOTIATE and act.target:
                    relationship_memory = self.memory_evolution.add_change(
                        persona_id=act_actor.persona_id or act_actor.id,
                        category=MemoryCategory.RELATIONSHIP_CHANGE,
                        content=(
                            f"Relationship with {act.target} changed after negotiation: "
                            f"{resolution.outcome_description}"
                        ),
                        occurred_at=next_timestamp,
                        source_event_id=ev.id,
                        metadata={"target": act.target},
                    )
                    self.repo.save_world_memory(world_id, branch_id, relationship_memory)

                prior_beliefs = dict(act_actor.beliefs)
                act_actor.beliefs = self.memory_evolution.evolve_beliefs(
                    act_actor.persona_id or act_actor.id,
                    act_actor.beliefs,
                    resolution.outcome_description,
                )
                if act_actor.beliefs != prior_beliefs:
                    belief_memory = self.memory_evolution.add_change(
                        persona_id=act_actor.persona_id or act_actor.id,
                        category=MemoryCategory.BELIEF_CHANGE,
                        content=(
                            f"Beliefs evolved after {act.action_type.value}: "
                            f"{prior_beliefs} -> {act_actor.beliefs}"
                        ),
                        occurred_at=next_timestamp,
                        source_event_id=ev.id,
                    )
                    self.repo.save_world_memory(world_id, branch_id, belief_memory)
                self.repo.save_actor_state(world_id, branch_id, act_actor)

            if tick_task_id is not None:
                world_tracer.record(
                    tick_task_id,
                    proposal_resolution_ms=round((time.monotonic() - resolution_started) * 1000, 1),
                )
                world_tracer.finish_task(tick_task_id)
                tick_task_id = None

            # 5. Economic step
            econ_summary = self.economy.step_economy(
                self.org_mgr.organizations,
                self.tech_model.technologies,
                delta_years=1.0 if step_unit == "year" else 0.25,
            )
            mutated_state.economy = econ_summary
            mutated_state.organizations = {
                oid: o.model_dump() for oid, o in self.org_mgr.organizations.items()
            }
            mutated_state.technologies = {
                tid: t.model_dump() for tid, t in self.tech_model.technologies.items()
            }

            # 6. Save organizations & technologies state
            for org in self.org_mgr.organizations.values():
                self.repo.save_organization(world_id, branch_id, org)
            for tech in self.tech_model.technologies.values():
                self.repo.save_technology(world_id, branch_id, tech)
            self.repo.save_causal_graph(world_id, branch_id, self.causal_engine.graph)

            # 7. Record Replay Step
            milestones = [e.effect for e in new_events if e.importance >= 0.7]
            replay_step = self.replay_engine.record_step(
                timestamp=next_timestamp,
                state=mutated_state,
                active_events=new_events,
                causal_milestones=milestones,
                diff_summary=f"Advanced by {step_unit} with {len(new_events)} causal events.",
            )
            self.repo.save_replay_step(world_id, branch_id, replay_step)

            # 8. Save updated state & snapshot
            branch.current_state = mutated_state
            self.repo.save_branch(branch)
            snapshot = self.state_mgr.snapshot(mutated_state, world_id, branch_id)
            self.repo.save_snapshot(snapshot)

        await self._broadcast_event(
            world_id,
            {
                "event": "world_step_completed",
                "world_id": world_id,
                "branch_id": branch_id,
                "timestamp": next_timestamp,
                "state": mutated_state.model_dump(mode="json"),
                "events": [e.model_dump(mode="json") for e in new_events],
            },
        )

        logger.info(
            "world_simulation_step world=%s branch=%s state_timestamp=%s events=%d",
            world_id,
            branch_id,
            mutated_state.timestamp,
            len(new_events),
        )

        return mutated_state, new_events, step_unit

    def inject_action(
        self, world_id: str, branch_id: str, action: ActorAction
    ) -> tuple[ActionResolution, WorldState, TimelineEvent]:
        branch = self.repo.get_branch(branch_id)
        if not branch or not branch.current_state:
            raise KeyError(f"Branch not found or uninitialized: {branch_id}")

        actors = self.repo.list_actor_states(world_id, branch_id)
        actor = next((a for a in actors if a.id == action.actor_id), None)
        if not actor:
            actor = Actor(id=action.actor_id, name=action.actor_id)

        resolution, updated_state = self.action_resolver.resolve_action(
            action, actor, branch.current_state
        )

        ev = self.event_gen.generate_from_action_resolution(
            action, resolution, updated_state.timestamp
        )
        updated_state.events.append(ev.id)

        branch.current_state = updated_state
        self.repo.save_branch(branch)
        self.repo.save_event(world_id, branch_id, ev)

        snapshot = self.state_mgr.snapshot(updated_state, world_id, branch_id)
        self.repo.save_snapshot(snapshot)

        # Update Causal Graph
        self.causal_engine.record_event(ev)
        self.repo.save_causal_graph(world_id, branch_id, self.causal_engine.graph)

        # Record and evolve persona memory
        mem_rec = self.memory_evolution.add_world_experience(
            persona_id=actor.persona_id or actor.id,
            content=f"Injected action {action.action_type.value}: {resolution.outcome_description}",
            occurred_at=updated_state.timestamp,
            emotional_valence=0.5 if resolution.success else -0.3,
            source_event_id=ev.id,
        )
        self.repo.save_world_memory(world_id, branch_id, mem_rec)

        return resolution, updated_state, ev

    async def trigger_scene(
        self, world_id: str, branch_id: str, actor_ids: list[str], topic: str
    ) -> tuple[str, TimelineEvent]:
        branch = self.repo.get_branch(branch_id)
        if not branch or not branch.current_state:
            raise KeyError(f"Branch not found: {branch_id}")

        actors_list = self.repo.list_actor_states(world_id, branch_id)
        actors = [a for a in actors_list if a.id in actor_ids]
        if not actors:
            raise ValueError(f"No matching actors found for IDs: {actor_ids}")

        summary, updated_state, updated_actors, ev = await self.scene_engine.execute_scene(
            world_id=world_id,
            branch_id=branch_id,
            actors=actors,
            topic=topic,
            current_world_time=branch.current_state.timestamp,
            state=branch.current_state,
        )

        # Update branch state and actors in storage
        branch.current_state = updated_state
        self.repo.save_branch(branch)
        self.repo.save_event(world_id, branch_id, ev)
        for a in updated_actors:
            self.repo.save_actor_state(world_id, branch_id, a)

        snapshot = self.state_mgr.snapshot(updated_state, world_id, branch_id)
        self.repo.save_snapshot(snapshot)

        # Update Causal Graph
        self.causal_engine.record_event(ev)
        self.repo.save_causal_graph(world_id, branch_id, self.causal_engine.graph)

        return summary, ev

    def fork_branch(
        self,
        world_id: str,
        source_branch_id: str,
        new_name: str,
        snapshot_id: str | None = None,
    ) -> SimulationBranch:
        if snapshot_id:
            snapshot = self.repo.get_snapshot(snapshot_id)
        else:
            snaps = self.repo.list_snapshots(world_id, source_branch_id)
            snapshot = snaps[-1] if snaps else None

        if not snapshot:
            raise ValueError(f"Cannot fork branch {source_branch_id}: no valid snapshot found")

        new_branch = self.branches.fork_branch(
            world_id=world_id,
            parent_branch_id=source_branch_id,
            snapshot=snapshot,
            name=new_name,
        )
        self.repo.save_branch(new_branch)

        # Clone actors & memories from source branch into new branch
        actors = self.repo.list_actor_states(world_id, source_branch_id)
        for a in actors:
            self.repo.save_actor_state(world_id, new_branch.id, a)
            src_mems = self.repo.list_world_memories(
                world_id, source_branch_id, a.persona_id or a.id
            )
            for m in src_mems:
                self.repo.save_world_memory(world_id, new_branch.id, m)

        # Clone organizations & technologies
        src_orgs = self.repo.list_organizations(world_id, source_branch_id)
        for o in src_orgs.values():
            self.repo.save_organization(world_id, new_branch.id, o)
        src_techs = self.repo.list_technologies(world_id, source_branch_id)
        for t in src_techs.values():
            self.repo.save_technology(world_id, new_branch.id, t)

        # Clone director policy
        src_dir = self.repo.get_director(world_id, source_branch_id)
        if src_dir:
            self.repo.save_director(world_id, new_branch.id, src_dir[0], src_dir[1])

        # Save initial snapshot for new branch
        new_snap = self.state_mgr.snapshot(snapshot.state, world_id, new_branch.id)
        self.repo.save_snapshot(new_snap)

        return new_branch

    def query_causal_chain(
        self, world_id: str, branch_id: str, target: str
    ) -> list[dict[str, Any]]:
        graph = self.repo.get_causal_graph(world_id, branch_id)
        self.causal_engine.graph = graph
        steps = self.causal_engine.query_causal_chain(target)
        return [
            {
                "id": s.node.id,
                "type": s.node.node_type.value,
                "name": s.node.name,
                "time": s.node.timestamp,
                "relation_to_next": s.relation_to_next,
                "properties": s.node.properties,
            }
            for s in steps
        ]

    def get_replay_trajectory(self, world_id: str, branch_id: str) -> ReplayTrajectory:
        snaps = self.repo.list_snapshots(world_id, branch_id)
        events = self.repo.list_events(world_id, branch_id)
        steps = []
        for snap in snaps:
            step_events = [e for e in events if e.event_time == snap.timestamp]
            steps.append(
                self.replay_engine.record_step(
                    timestamp=snap.timestamp,
                    state=snap.state,
                    active_events=step_events,
                    causal_milestones=[e.cause for e in step_events if e.importance >= 0.7],
                    diff_summary=f"Snapshot at {snap.timestamp}",
                )
            )
        return ReplayTrajectory(
            world_id=world_id,
            branch_id=branch_id,
            steps=steps,
            outcome_summary="Historical replay trajectory scrubbed from snapshots.",
        )

    def generate_report(self, world_id: str) -> WorldSimulationReport:
        world = self.repo.get_world(world_id)
        if not world:
            raise KeyError(f"World not found: {world_id}")

        branches = self.repo.list_branches(world_id)
        primary_branch_id = branches[0].id if branches else "main"
        events = self.repo.list_events(world_id, primary_branch_id)
        orgs = self.repo.list_organizations(world_id, primary_branch_id)
        actors = self.repo.list_actor_states(world_id, primary_branch_id)

        branch_comps = []
        for b in branches:
            ts = b.current_state.timestamp if b.current_state else "initial"
            branch_comps.append(
                {
                    "id": b.id,
                    "name": b.name,
                    "status": b.status,
                    "summary": f"Branch state at {ts}",
                }
            )

        # Branch status distribution summary (domain-agnostic)
        status_counts: dict[str, int] = {}
        for b in branches:
            status_counts[b.status] = status_counts.get(b.status, 0) + 1
        eval_dist = {
            "question": "",
            "total_branches": len(branches),
            "distribution": status_counts,
        }

        actor_evos = [
            {
                "id": a.id,
                "name": a.name,
                "belief_shifts": str(a.beliefs),
                "action_count": len(events),
            }
            for a in actors
        ]
        org_evos = [
            {
                "id": o.id,
                "name": o.name,
                "cash_billions": o.cash_reserves_billions,
                "headcount": o.employees_count,
                "focus": o.strategy.primary_goal,
            }
            for o in orgs.values()
        ]

        causal_chains = [
            {
                "target": "2030 Ecosystem Leadership",
                "steps": self.query_causal_chain(world_id, primary_branch_id, "divergence"),
            }
        ]

        return self.report_gen.generate_report(
            world=world,
            timeline_events=events,
            branch_comparisons=branch_comps,
            outcome_distribution=eval_dist,
            actor_evolution=actor_evos,
            organization_evolution=org_evos,
            causal_chains=causal_chains,
        )

    async def run_evaluation(
        self,
        world_id: str,
        question: str,
        branch_count: int = 3,
        metrics: dict[str, float] | None = None,
    ) -> OutcomeEvaluation:
        world = self.repo.get_world(world_id)
        if not world:
            raise KeyError(f"World not found: {world_id}")

        existing_branches = self.repo.list_branches(world_id)
        eval_branches: list[SimulationBranch] = list(existing_branches)

        while len(eval_branches) < branch_count:
            idx = len(eval_branches) + 1
            forked = self.fork_branch(
                world_id=world_id,
                source_branch_id=existing_branches[0].id,
                new_name=f"Branch_{chr(64 + idx)}",
            )
            await self.step_branch(world_id, forked.id)
            await self.step_branch(world_id, forked.id)
            eval_branches.append(self.repo.get_branch(forked.id) or forked)

        evaluation = self.evaluator.evaluate_simulation(
            world_id=world_id,
            question=question,
            branches=eval_branches,
            metrics=metrics,
        )
        self.repo.save_evaluation(evaluation)
        return evaluation
