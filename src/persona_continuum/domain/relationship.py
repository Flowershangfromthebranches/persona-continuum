from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from persona_continuum.domain.persona import utc_now


class RelationshipKind(StrEnum):
    STRANGER = "stranger"
    ACQUAINTANCE = "acquaintance"
    FRIEND = "friend"
    CLOSE_FRIEND = "close_friend"
    FLIRTING = "flirting"
    DATING = "dating"
    PARTNER = "partner"
    FAMILY = "family"
    RIVAL = "rival"
    ENEMY = "enemy"
    CUSTOM = "custom"


class RelationshipState(BaseModel):
    persona_id: str
    counterpart: str
    relationship_kind: RelationshipKind = RelationshipKind.STRANGER
    bond_stage: str = "new"
    trajectory: str = "stable"
    relationship_prior: float = 0.0
    meaningful_interactions: int = 0
    positive_history: float = 0.0
    recent_valence: float = 0.0
    romantic_evidence: int = 0
    recent_acts: list[str] = Field(default_factory=list)
    boundary_explicitness: float = 1.0
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
