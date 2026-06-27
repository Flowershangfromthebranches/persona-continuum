from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from persona_continuum.domain.affect import AffectState, NeedState
from persona_continuum.domain.memory import MemoryRecord
from persona_continuum.domain.persona import PersonaManifest, utc_now
from persona_continuum.domain.relationship import RelationshipState


class SessionRecord(BaseModel):
    id: str
    persona_id: str
    title: str | None = None
    status: str = "active"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PreparedTurn(BaseModel):
    persona_id: str
    session_id: str
    identity_anchor: PersonaManifest
    current_run_mode: str
    relevant_historical_facts: list[str]
    relevant_memories: list[MemoryRecord]
    activated_emotional_memories: list[MemoryRecord]
    current_emotions: list[AffectState]
    current_mood: dict[str, float]
    current_needs: list[NeedState]
    active_goals: list[str]
    relationship_state: RelationshipState
    mental_models: list[str]
    decision_patterns: list[str]
    contradictions: list[str]
    event_appraisal: dict[str, Any]
    expression_intent: str
    expression_parameters: dict[str, Any]
    compiled_persona_context: dict[str, Any] = Field(default_factory=dict)
    uncertainty: str
    suggested_memory_candidates: list[str]


class SessionTurn(BaseModel):
    id: str
    session_id: str
    persona_id: str
    user_message: str
    persona_response: str
    used_memory_ids: list[str] = Field(default_factory=list)
    user_feedback: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
