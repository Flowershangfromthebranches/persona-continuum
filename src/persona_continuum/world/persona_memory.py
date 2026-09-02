from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel, Field

from persona_continuum.application._utils import new_id
from persona_continuum.world.models import MemoryCategory, WorldMemoryRecord


class PersonaMemoryState(BaseModel):
    identity_memory: list[WorldMemoryRecord] = Field(default_factory=list)
    world_memory: list[WorldMemoryRecord] = Field(default_factory=list)
    belief_changes: list[WorldMemoryRecord] = Field(default_factory=list)
    relationship_changes: list[WorldMemoryRecord] = Field(default_factory=list)


class PersonaMemoryEvolution:
    """Manages continuous memory evolution for personas in parallel worlds,

    strictly distinguishing core Identity Memory from branch-specific World Experience Memory.
    """

    def __init__(self, memories: list[WorldMemoryRecord] | None = None) -> None:
        self.memories: list[WorldMemoryRecord] = memories or []

    def add_identity_memory(
        self,
        persona_id: str,
        content: str,
        importance: float = 0.9,
    ) -> WorldMemoryRecord:
        rec = WorldMemoryRecord(
            id=new_id("wmem_id"),
            persona_id=persona_id,
            memory_type=MemoryCategory.IDENTITY,
            content=content,
            occurred_at="historical_baseline",
            importance=importance,
            emotional_valence=0.0,
            metadata={"origin": "core_identity"},
        )
        self.memories.append(rec)
        return rec

    def add_world_experience(
        self,
        persona_id: str,
        content: str,
        occurred_at: str,
        emotional_valence: float = 0.0,
        importance: float = 0.6,
        source_event_id: str | None = None,
    ) -> WorldMemoryRecord:
        rec = WorldMemoryRecord(
            id=new_id("wmem_exp"),
            persona_id=persona_id,
            memory_type=MemoryCategory.WORLD_EXPERIENCE,
            content=content,
            occurred_at=occurred_at,
            importance=importance,
            emotional_valence=emotional_valence,
            source_event_id=source_event_id,
            metadata={"origin": "counterfactual_experience"},
        )
        self.memories.append(rec)
        return rec

    def add_change(
        self,
        *,
        persona_id: str,
        category: MemoryCategory,
        content: str,
        occurred_at: str,
        source_event_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> WorldMemoryRecord:
        if category not in {MemoryCategory.BELIEF_CHANGE, MemoryCategory.RELATIONSHIP_CHANGE}:
            raise ValueError("change memory must be belief_change or relationship_change")
        record = WorldMemoryRecord(
            persona_id=persona_id,
            memory_type=category,
            content=content,
            occurred_at=occurred_at,
            importance=0.7,
            source_event_id=source_event_id,
            metadata={"origin": "counterfactual_evolution", **(metadata or {})},
        )
        self.memories.append(record)
        return record

    def state_for_persona(self, persona_id: str) -> PersonaMemoryState:
        records = self.get_memories_for_persona(persona_id)
        return PersonaMemoryState(
            identity_memory=[m for m in records if m.memory_type == MemoryCategory.IDENTITY],
            world_memory=[m for m in records if m.memory_type == MemoryCategory.WORLD_EXPERIENCE],
            belief_changes=[m for m in records if m.memory_type == MemoryCategory.BELIEF_CHANGE],
            relationship_changes=[
                m for m in records if m.memory_type == MemoryCategory.RELATIONSHIP_CHANGE
            ],
        )

    def get_memories_for_persona(
        self,
        persona_id: str,
        category: MemoryCategory | None = None,
    ) -> list[WorldMemoryRecord]:
        results = [m for m in self.memories if m.persona_id == persona_id]
        if category:
            results = [m for m in results if m.memory_type == category]
        return results

    def evolve_beliefs(
        self,
        persona_id: str,
        current_beliefs: dict[str, Any],
        recent_experience: str,
    ) -> dict[str, Any]:
        """Evolves persona beliefs and strategic heuristics based on counterfactual experiences.

        Uses generic, domain-neutral belief dimensions (vigilance, patience,
        confidence, investment appetite, ecosystem respect) so evolution works
        for any world rather than a specific industry.
        """
        evolved = dict(current_beliefs)
        exp_lower = recent_experience.lower()

        if "failure" in exp_lower or "delayed" in exp_lower or "setback" in exp_lower:
            evolved["risk_vigilance"] = min(
                1.0, float(evolved.get("risk_vigilance", 0.5)) + 0.25
            )
            evolved["strategic_patience"] = max(
                0.2, float(evolved.get("strategic_patience", 0.7)) - 0.2
            )
        elif "breakthrough" in exp_lower or "success" in exp_lower:
            evolved["confidence_in_strategy"] = min(
                1.0, float(evolved.get("confidence_in_strategy", 0.6)) + 0.2
            )
            evolved["investment_appetite"] = min(
                1.0, float(evolved.get("investment_appetite", 0.5)) + 0.15
            )
        elif "competitor" in exp_lower or "platform" in exp_lower or "ecosystem" in exp_lower:
            evolved["respect_for_platform_ecosystems"] = min(
                1.0, float(evolved.get("respect_for_platform_ecosystems", 0.4)) + 0.3
            )

        return evolved

    def clone_for_branch(self) -> PersonaMemoryEvolution:
        return PersonaMemoryEvolution(memories=copy.deepcopy(self.memories))

    def export_list(self) -> list[dict[str, Any]]:
        return [m.model_dump() for m in self.memories]

    def import_list(self, data: list[dict[str, Any]]) -> None:
        self.memories = [WorldMemoryRecord.model_validate(m) for m in data]
