from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from persona_continuum.domain.persona import utc_now


class RelationshipState(BaseModel):
    persona_id: str
    counterpart: str
    familiarity: float = 0.0
    trust: float = 0.0
    affection: float = 0.0
    respect: float = 0.0
    dependence: float = 0.0
    resentment: float = 0.0
    jealousy: float = 0.0
    perceived_threat: float = 0.0
    unresolved_conflict: float = 0.0
    updated_at: datetime = Field(default_factory=utc_now)
    reasons: list[str] = Field(default_factory=list)
