from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from persona_continuum.domain.persona import utc_now


class EvidenceSource(BaseModel):
    id: str
    persona_id: str
    source_type: str
    path: str
    title: str
    hash: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class EvidenceClaim(BaseModel):
    id: str
    persona_id: str
    content: str
    dimension: str
    source_id: str | None = None
    claim_type: str
    raw_location: str | None = None
    event_time: str | None = None
    reliability: float = 0.5
    is_self_report: bool = False
    is_third_party_report: bool = False
    has_counter_evidence: bool = False
    inference_strength: float = 0.5
    confidence: float = 0.5
    created_by: str = "agent_artifact"
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConflictRecord(BaseModel):
    id: str
    persona_id: str
    claim_ids: list[str]
    summary: str
    severity: float = 0.5
    created_at: datetime = Field(default_factory=utc_now)
