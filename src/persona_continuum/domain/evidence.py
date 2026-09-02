from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from persona_continuum.domain.persona import utc_now
from persona_continuum.numeric import append_skipped_numeric_metadata, safe_probability


def _normalise_probability_fields(value: Any, fields: tuple[str, ...]) -> Any:
    if not isinstance(value, dict):
        return value
    data = dict(value)
    skipped: list[str] = []
    for field in fields:
        if field not in data:
            continue
        normalized = safe_probability(data.get(field), default=None, field=field)
        if normalized is None:
            data[field] = 0.5
            skipped.append(field)
        else:
            data[field] = normalized
    if skipped:
        data["metadata"] = append_skipped_numeric_metadata(data.get("metadata"), skipped, field="")
    return data


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

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        return _normalise_probability_fields(
            value, ("reliability", "inference_strength", "confidence")
        )


class ConflictRecord(BaseModel):
    id: str
    persona_id: str
    claim_ids: list[str]
    summary: str
    severity: float = 0.5
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        normalized = safe_probability(data.get("severity"), default=0.5)
        data["severity"] = normalized if normalized is not None else 0.5
        return data
