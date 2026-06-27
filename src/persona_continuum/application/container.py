from __future__ import annotations

from persona_continuum.application.compilation_service import CompilationService
from persona_continuum.application.compiled_context_service import CompiledPersonaContextService
from persona_continuum.application.continuation_service import ContinuationService
from persona_continuum.application.evaluation_service import EvaluationService
from persona_continuum.application.memory_service import MemoryService
from persona_continuum.application.persona_service import PersonaService
from persona_continuum.application.room_service import RoomService
from persona_continuum.application.session_service import SessionService
from persona_continuum.config import Config
from persona_continuum.runtime.affect_engine import AffectEngine
from persona_continuum.runtime.motivation_engine import MotivationEngine
from persona_continuum.runtime.relationship_engine import RelationshipEngine
from persona_continuum.storage.database import Database


class PersonaContinuum:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self.database = Database(self.config.database_path)
        self.personas = PersonaService(self.config, self.database)
        self.memories = MemoryService(self.database)
        self.compiled_context = CompiledPersonaContextService(self.database)
        self.affect = AffectEngine(self.database)
        self.motivation = MotivationEngine(self.database)
        self.relationships = RelationshipEngine(self.database)
        self.compilation = CompilationService(self.database, self.personas, self.memories)
        self.evaluations = EvaluationService(self.database, self.personas)
        self.sessions = SessionService(
            self.database,
            self.personas,
            self.memories,
            self.compiled_context,
            self.affect,
            self.motivation,
            self.relationships,
        )
        self.continuations = ContinuationService(
            self.database, self.personas, self.memories, self.compiled_context
        )
        self.rooms = RoomService(self.database, self.personas, self.sessions)

    def init(self) -> None:
        self.config.ensure_dirs()
        self.database.migrate()

    def close(self) -> None:
        self.database.close()

    def runtime_state(self, persona_id: str, branch_id: str = "main") -> dict[str, object]:
        return {
            "persona": self.personas.get(persona_id).manifest.model_dump(),
            "branch_id": branch_id,
            "emotions": [
                state.model_dump() for state in self.affect.get_emotions(persona_id, branch_id)
            ],
            "needs": [
                state.model_dump() for state in self.motivation.get_needs(persona_id, branch_id)
            ],
            "relationships": [
                state.model_dump()
                for state in self.relationships.list_relationships(persona_id, branch_id)
            ],
        }
