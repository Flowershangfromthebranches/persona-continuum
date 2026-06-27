from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from persona_continuum.domain.persona import utc_now


class ContinuationTask(BaseModel):
    id: str
    persona_id: str
    status: str = "created"
    divergence_condition: str
    world_events: list[dict[str, Any]] = Field(default_factory=list)
    main_branch_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ContinuationBranch(BaseModel):
    id: str
    continuation_id: str
    persona_id: str
    parent_branch_id: str | None = None
    divergence_event: str
    world_state: dict[str, Any] = Field(default_factory=dict)
    persona_state: dict[str, Any] = Field(default_factory=dict)
    key_events: list[dict[str, Any]] = Field(default_factory=list)
    relationship_changes: list[dict[str, Any]] = Field(default_factory=list)
    persona_changes: list[str] = Field(default_factory=list)
    credibility: float = 0.5
    eliminated_reason: str | None = None
    random_seed: int = 0
    score: float | None = None
    status: str = "active"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
