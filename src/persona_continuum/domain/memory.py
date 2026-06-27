from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from persona_continuum.domain.persona import utc_now


class MemoryType(StrEnum):
    AUTOBIOGRAPHICAL = "autobiographical"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    RELATIONAL = "relational"
    EMOTIONAL = "emotional"
    PROCEDURAL = "procedural"
    COUNTERFACTUAL = "counterfactual"
    DIGITAL_EXPERIENCE = "digital_experience"


class MemoryRecord(BaseModel):
    id: str
    persona_id: str
    content: str
    type: MemoryType
    occurred_at: datetime | None = None
    written_at: datetime = Field(default_factory=utc_now)
    participants: list[str] = Field(default_factory=list)
    emotions: dict[str, float] = Field(default_factory=dict)
    source_id: str | None = None
    source_kind: str
    source_confidence: float = 0.5
    importance: float = 0.5
    validity: str = "valid"
    access_count: int = 0
    last_accessed_at: datetime | None = None
    branch_id: str = "main"
    unresolved: bool = False
    user_corrected: bool = False
    forgettable: bool = True
    supersedes_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
