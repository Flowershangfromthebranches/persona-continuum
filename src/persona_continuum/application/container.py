from __future__ import annotations

import contextlib
from typing import Any

from persona_continuum.agent.context_budget import AgentContextBudgetManager
from persona_continuum.agent.discovery import AgentDiscoveryService
from persona_continuum.agent.phase_policy import default_phase_context_policy
from persona_continuum.agent.registry import AgentRegistry
from persona_continuum.agent.runtime_executor import AgentRuntimeExecutor
from persona_continuum.application.compilation_service import CompilationService
from persona_continuum.application.compiled_context_service import CompiledPersonaContextService
from persona_continuum.application.continuation_service import ContinuationService
from persona_continuum.application.episode_service import EpisodeService
from persona_continuum.application.evaluation_service import EvaluationService
from persona_continuum.application.fact_service import SemanticFactService
from persona_continuum.application.hierarchy_service import HierarchyService
from persona_continuum.application.material_intelligence import MaterialIntelligenceService
from persona_continuum.application.material_upload_service import MaterialUploadService
from persona_continuum.application.memory_retrieval_planner import MemoryRetrievalPlanner
from persona_continuum.application.memory_service import MemoryService
from persona_continuum.application.narrative_director_service import NarrativeDirectorService
from persona_continuum.application.narrative_service import NarrativeService
from persona_continuum.application.persona_creation_service import PersonaCreationOrchestrator
from persona_continuum.application.persona_patch_service import PersonaPatchService
from persona_continuum.application.persona_service import PersonaService
from persona_continuum.application.profile_enrichment_service import ProfileEnrichmentService
from persona_continuum.application.profile_library_service import ProfileLibraryService
from persona_continuum.application.raw_recall_service import RawRecallService
from persona_continuum.application.research_capability_cache import ResearchCapabilityCache
from persona_continuum.application.room_service import RoomService
from persona_continuum.application.session_service import SessionService
from persona_continuum.application.shooting_service import NarrativeShootingService
from persona_continuum.application.starter_kit_service import StarterKitService
from persona_continuum.application.thread_service import ThreadService
from persona_continuum.application.world.entity_classification_service import (
    WorldEntityClassificationService,
)
from persona_continuum.application.world_service import WorldService
from persona_continuum.auth.credentials import CredentialManager
from persona_continuum.auth.profiles import AuthProfileService
from persona_continuum.config import Config
from persona_continuum.narrative.repository import NarrativeRepository
from persona_continuum.room.orchestrator import MultiAgentOrchestrator
from persona_continuum.room.recall_gate import RecallGate
from persona_continuum.runtime.affect_engine import AffectEngine
from persona_continuum.runtime.motivation_engine import MotivationEngine
from persona_continuum.runtime.relationship_engine import RelationshipEngine
from persona_continuum.storage.database import Database
from persona_continuum.world.repository import WorldRepository


class PersonaContinuum:
    def __init__(
        self,
        config: Config | None = None,
        include_fake_agent: bool = False,
        research_broker: Any | None = None,
    ) -> None:
        self.config = config or Config()
        self.database = Database(self.config.database_path)
        from persona_continuum.performance.capability_cache import (
            configure_default_model_capability_cache,
        )
        from persona_continuum.performance.concurrency_cache import (
            configure_default_runtime_capability_store,
        )
        from persona_continuum.performance.runtime_pool import configure_default_runtime_pool
        from persona_continuum.performance.scheduler import (
            ExecutionScheduler,
            register_default_execution_scheduler,
        )

        configure_default_model_capability_cache(
            enabled=self.config.model_capability_cache_enabled,
            ttl_seconds=self.config.model_capability_cache_ttl_seconds,
        )
        # Bind the persisted independent-session capability store to THIS
        # application's data directory (not a process-wide default), so a probe
        # written against the app data dir is what the app later reads.
        configure_default_runtime_capability_store(
            self.config.database_path,
            ttl_seconds=self.config.runtime_concurrency_capability_ttl_seconds,
        )
        configure_default_runtime_pool(
            enabled=self.config.runtime_pool_enabled,
            max_processes_per_key=self.config.runtime_pool_max_processes_per_adapter,
            idle_timeout_seconds=self.config.runtime_pool_idle_timeout_seconds,
        )
        # One scheduler owns every bounded wait: model execution, search,
        # fetch, and CPU.  Interactive room turns wake ahead of background
        # enrichment so heavy jobs cannot starve live conversation.
        self.execution_scheduler = ExecutionScheduler(
            max_llm_concurrency=self.config.max_llm_concurrency,
            max_search_concurrency=self.config.max_search_concurrency,
            max_fetch_concurrency=self.config.max_fetch_concurrency,
            per_adapter_limits=dict(self.config.per_adapter_llm_limits),
        )
        register_default_execution_scheduler(self.execution_scheduler)
        self.agent_runtime_executor = AgentRuntimeExecutor(
            scheduler=self.execution_scheduler,
            context_budget_manager=AgentContextBudgetManager(
                phase_policy=default_phase_context_policy(self.config.phase_working_ratios)
            ),
        )
        self.personas = PersonaService(self.config, self.database)
        self.starter_kits = StarterKitService(self.config, self.personas)
        self.profile_library = ProfileLibraryService(
            self.database, self.personas, runtime_executor=self.agent_runtime_executor
        )
        self.personas.profile_library = self.profile_library
        self.entity_classifier = WorldEntityClassificationService(self.profile_library)
        self.memories = MemoryService(self.database)
        # Memory Architecture v2 (Phase 3): the Episode ledger.  Constructed
        # before SessionService because a committed turn is placed in it inside
        # the same transaction that writes the turn.
        self.episodes = EpisodeService(self.database, config=self.config)
        # Phase 4: Semantic Facts distilled from Episodes.  Depends on the
        # Episode ledger for provenance, not on any prompt-assembly path.
        self.facts = SemanticFactService(self.database, self.episodes, config=self.config)
        # Phase 5: Active Threads -- what is still in flight.  Depends on the
        # Episode ledger (sources) and, where available, on Facts (lineage); it
        # never depends on prompt assembly.
        self.threads = ThreadService(
            self.database, self.episodes, self.facts, config=self.config
        )
        # Phase 6: Chapters and Long-term segments.  Built from Episodes (level 1)
        # and from lower-level summaries (level 2+), never from a prompt path.
        self.hierarchies = HierarchyService(
            self.database,
            self.episodes,
            self.facts,
            self.threads,
            config=self.config,
        )
        # The Episode layer owns delete semantics for everything derived from its
        # turns; the summary rules live in ONE place and are bound here, so a
        # deleted Episode can never leave a silently dangling Chapter.
        self.episodes.summary_provenance_hook = self.hierarchies.stamp_sources_unavailable
        # Phase 7: provenance-backed raw recall.  Reads the raw transcript
        # through the same provenance the layers above recorded; it never writes
        # memory and never assembles a prompt.
        self.raw_recall = RawRecallService(
            self.database,
            self.episodes,
            self.facts,
            self.threads,
            self.hierarchies,
            config=self.config,
        )
        self.compiled_context = CompiledPersonaContextService(self.database)
        self.affect = AffectEngine(self.database)
        self.motivation = MotivationEngine(self.database)
        self.relationships = RelationshipEngine(self.database)
        self.retrieval_planner = MemoryRetrievalPlanner(
            self.database,
            memories=self.memories,
            episodes=self.episodes,
            facts=self.facts,
            threads=self.threads,
            hierarchies=self.hierarchies,
            raw_recall=self.raw_recall,
            relationships=self.relationships,
            recall_gate=RecallGate(self.memories),
        )
        self.compilation = CompilationService(self.database, self.personas, self.memories)
        self.persona_patches = PersonaPatchService(
            self.database,
            self.personas,
            self.compilation,
            self.memories,
            self.motivation,
            self.relationships,
            self.compiled_context,
        )
        # Raw EvidenceSource records remain authoritative.  The material layer
        # is a rebuildable, provenance-preserving index consumed by private
        # Persona creation and enrichment before the existing compiler.
        self.material_uploads = MaterialUploadService(self.config, self.database)
        self.material_intelligence = MaterialIntelligenceService(
            self.database,
            self.personas,
            context_budget_manager=self.agent_runtime_executor.context_budget_manager,
            batch_size=self.config.material_intelligence_batch_size,
            semantic_gate_mode=self.config.material_semantic_gate_mode,
            max_llm_concurrency=self.config.max_llm_concurrency,
            in_memory_unit_limit=self.config.material_intelligence_in_memory_unit_limit,
            max_source_bytes=self.config.max_source_bytes,
            turn_gap_seconds=self.config.material_chat_turn_gap_seconds,
            analysis_window_max_units=self.config.material_analysis_window_max_units,
            chat_style_profiler_enabled=self.config.material_chat_style_profiler_enabled,
            conversation_episode_gap_seconds=(
                self.config.material_conversation_episode_gap_seconds
            ),
            analysis_window_max_episodes=self.config.material_analysis_window_max_episodes,
            contradiction_max_variants_per_cluster=(
                self.config.material_contradiction_max_variants_per_cluster
            ),
            concurrency_retry_limit=self.config.material_concurrency_retry_limit,
        )
        self.evaluations = EvaluationService(self.database, self.personas)
        self.sessions = SessionService(
            self.database,
            self.personas,
            self.memories,
            self.compiled_context,
            self.affect,
            self.motivation,
            self.relationships,
            config=self.config,
            episodes=self.episodes,
        )
        self.continuations = ContinuationService(
            self.database, self.personas, self.memories, self.compiled_context
        )
        self.rooms = RoomService(self.database, self.personas, self.sessions)
        self.credentials = CredentialManager(self.database, self.config.data_dir / "credential.key")
        self.auth = AuthProfileService(self.database, self.credentials)
        self.research_capability_cache = ResearchCapabilityCache(self.database)
        self.world_repo = WorldRepository(self.database)
        self.worlds = WorldService(self, self.world_repo)
        self.narrative_repo = NarrativeRepository(self.database)
        self.narratives = NarrativeService(self, self.narrative_repo)
        self.narrative_director = NarrativeDirectorService(self, self.narratives)
        self.narrative_shooting = NarrativeShootingService(self, self.narratives)
        self.profile_enrichment = ProfileEnrichmentService(self, self.profile_library)
        self.agent_registry = AgentRegistry(
            include_builtins=True,
            include_fake=include_fake_agent,
            credential_manager=self.credentials,
        )
        self.agent_discovery = AgentDiscoveryService(
            self.agent_registry,
            self.auth,
            research_capability_cache=self.research_capability_cache,
        )
        self.orchestrator = MultiAgentOrchestrator(
            continuum=self,
            registry=self.agent_registry,
            discovery=self.agent_discovery,
            auth_service=self.auth,
        )
        # Persona Creation is an application service over the existing Persona,
        # Evidence and Compilation services.  An external ResearchToolBroker
        # may be injected by an installation; absent one, public deep research
        # fails closed instead of using model memory as fake web research.
        self.persona_creation = PersonaCreationOrchestrator(
            self,
            research_broker=research_broker,
            profile_library=self.profile_library,
            material_intelligence=self.material_intelligence,
            research_capability_cache=self.research_capability_cache,
        )

    def init(self) -> None:
        self.config.ensure_dirs()
        self.database.migrate()
        with contextlib.suppress(Exception):
            self.material_uploads.cleanup_orphans()
        with contextlib.suppress(Exception):
            self.material_intelligence.reclaim_interrupted_jobs()
        # Episodes owed a summary when the process last stopped are put back on
        # the work list; nothing is re-summarised automatically at startup.
        with contextlib.suppress(Exception):
            self.episodes.recover_pending()
        with contextlib.suppress(Exception):
            self.facts.recover_pending()
        # Threads owe resolution for the same reason Episodes owe summaries: the
        # Episode row is the durable work list, and a restart only restores it.
        with contextlib.suppress(Exception):
            self.threads.recover_pending()
        # Chapters / Long-term segments owe the same treatment: the row is the
        # work list, and a restart only restores it -- no model runs at startup.
        with contextlib.suppress(Exception):
            self.hierarchies.recover_pending()
        self.narratives.reclaim_orphaned_jobs()
        self.narrative_director.reclaim_orphaned_sessions()
        self.narrative_shooting.reclaim_orphaned_sessions()
        self.orchestrator.recover_interrupted_rooms()
        # Rooms created by older builds may still list tool names that are
        # no longer granted by any built-in template; strip them before the
        # built-in templates are refreshed so old rooms stay open.
        self.orchestrator.normalize_legacy_room_tool_permissions()
        self.orchestrator.protocol_repository.ensure_builtin_templates()

    def close(self) -> None:
        # Tool providers own child processes and stdio pipes; release them
        # before the database so a dying provider cannot touch a closed DB.
        self.orchestrator.shutdown_sync()
        self.narrative_director.shutdown()
        self.narrative_shooting.shutdown()
        self.narratives.shutdown()
        self.persona_creation.shutdown()
        self.profile_enrichment.shutdown()
        self.credentials.close()
        with contextlib.suppress(Exception):
            from persona_continuum.performance.concurrency_cache import (
                default_runtime_capability_store,
            )

            default_runtime_capability_store().close()
        self.database.close()

    def runtime_state(self, persona_id: str, branch_id: str = "main") -> dict[str, object]:
        from persona_continuum.auth.profiles import redact_secrets
        from persona_continuum.runtime.bond_dynamics import relationship_stance
        from persona_continuum.runtime.core_fidelity import (
            behavioral_implications,
            core_traits,
            render_core,
            render_needs,
        )

        context = self.compiled_context.prepare_context(persona_id, "", branch_id=branch_id)
        components = context.get("core_components", {})
        needs = self.motivation.get_needs(persona_id, branch_id)
        relationships = self.relationships.list_relationships(persona_id, branch_id)
        fidelity = {
            "core_traits": [trait.model_dump() for trait in core_traits(components)],
            "core_components_present": [key for key, value in components.items() if value],
            "static_kernel_preview": render_core(components),
            "current_dominant_needs": render_needs(needs),
            "relationship_modifiers": [
                {"counterpart": state.counterpart, "kind": state.relationship_kind.value,
                 "trust": state.trust, "threat": state.perceived_threat,
                 "stance": relationship_stance(state, {n.name: n.level for n in needs}),
                 "behavioral_implications": behavioral_implications(components, needs, state)}
                for state in relationships
            ],
            "diagnostic_scope": (
                "Current prompt preview; model adherence requires generated-turn review."
            ),
        }
        return {
            "fidelity": redact_secrets(fidelity),
            "persona": self.personas.get(persona_id).manifest.model_dump(mode="json"),
            "branch_id": branch_id,
            "emotions": [
                state.model_dump(mode="json")
                for state in self.affect.get_emotions(persona_id, branch_id)
            ],
            "needs": [
                state.model_dump(mode="json")
                for state in needs
            ],
            "relationships": [
                state.model_dump(mode="json")
                for state in relationships
            ],
        }

    def reset_runtime_state(
        self,
        persona_id: str,
        branch_id: str = "main",
        include_memories: bool = False,
    ) -> dict[str, object]:
        result = self.sessions.reset_runtime_state(
            persona_id, branch_id=branch_id, include_memories=include_memories
        )
        if include_memories:
            for room_id in (result.get("removed") or {}).get("room_ids") or []:
                self.orchestrator.forget_room_runtime(str(room_id))
        return result
