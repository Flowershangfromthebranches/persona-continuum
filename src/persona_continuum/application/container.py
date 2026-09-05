from __future__ import annotations

from typing import Any

from persona_continuum.agent.discovery import AgentDiscoveryService
from persona_continuum.agent.registry import AgentRegistry
from persona_continuum.agent.runtime_executor import AgentRuntimeExecutor
from persona_continuum.application.compilation_service import CompilationService
from persona_continuum.application.compiled_context_service import CompiledPersonaContextService
from persona_continuum.application.continuation_service import ContinuationService
from persona_continuum.application.evaluation_service import EvaluationService
from persona_continuum.application.material_intelligence import MaterialIntelligenceService
from persona_continuum.application.memory_service import MemoryService
from persona_continuum.application.narrative_director_service import NarrativeDirectorService
from persona_continuum.application.narrative_service import NarrativeService
from persona_continuum.application.persona_creation_service import PersonaCreationOrchestrator
from persona_continuum.application.persona_service import PersonaService
from persona_continuum.application.profile_enrichment_service import ProfileEnrichmentService
from persona_continuum.application.profile_library_service import ProfileLibraryService
from persona_continuum.application.research_capability_cache import ResearchCapabilityCache
from persona_continuum.application.room_service import RoomService
from persona_continuum.application.session_service import SessionService
from persona_continuum.application.shooting_service import NarrativeShootingService
from persona_continuum.application.world.entity_classification_service import (
    WorldEntityClassificationService,
)
from persona_continuum.application.world_service import WorldService
from persona_continuum.auth.credentials import CredentialManager
from persona_continuum.auth.profiles import AuthProfileService
from persona_continuum.config import Config
from persona_continuum.narrative.repository import NarrativeRepository
from persona_continuum.room.orchestrator import MultiAgentOrchestrator
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
        from persona_continuum.performance.runtime_pool import configure_default_runtime_pool
        from persona_continuum.performance.scheduler import (
            ExecutionScheduler,
            register_default_execution_scheduler,
        )

        configure_default_model_capability_cache(
            enabled=self.config.model_capability_cache_enabled,
            ttl_seconds=self.config.model_capability_cache_ttl_seconds,
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
        )
        self.personas = PersonaService(self.config, self.database)
        self.profile_library = ProfileLibraryService(
            self.database, self.personas, runtime_executor=self.agent_runtime_executor
        )
        self.personas.profile_library = self.profile_library
        self.entity_classifier = WorldEntityClassificationService(self.profile_library)
        self.memories = MemoryService(self.database)
        self.compiled_context = CompiledPersonaContextService(self.database)
        self.affect = AffectEngine(self.database)
        self.motivation = MotivationEngine(self.database)
        self.relationships = RelationshipEngine(self.database)
        self.compilation = CompilationService(self.database, self.personas, self.memories)
        # Raw EvidenceSource records remain authoritative.  The material layer
        # is a rebuildable, provenance-preserving index consumed by private
        # Persona creation and enrichment before the existing compiler.
        self.material_intelligence = MaterialIntelligenceService(
            self.database,
            self.personas,
            context_budget_manager=self.agent_runtime_executor.context_budget_manager,
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
        self.database.close()

    def runtime_state(self, persona_id: str, branch_id: str = "main") -> dict[str, object]:
        return {
            "persona": self.personas.get(persona_id).manifest.model_dump(mode="json"),
            "branch_id": branch_id,
            "emotions": [
                state.model_dump(mode="json")
                for state in self.affect.get_emotions(persona_id, branch_id)
            ],
            "needs": [
                state.model_dump(mode="json")
                for state in self.motivation.get_needs(persona_id, branch_id)
            ],
            "relationships": [
                state.model_dump(mode="json")
                for state in self.relationships.list_relationships(persona_id, branch_id)
            ],
        }

    def reset_runtime_state(
        self,
        persona_id: str,
        branch_id: str = "main",
        include_memories: bool = False,
    ) -> dict[str, object]:
        return self.sessions.reset_runtime_state(
            persona_id, branch_id=branch_id, include_memories=include_memories
        )
