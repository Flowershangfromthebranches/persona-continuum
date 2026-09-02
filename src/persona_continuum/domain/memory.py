from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from persona_continuum.domain.persona import utc_now
from persona_continuum.numeric import normalize_probability_map, safe_int, safe_probability


class MemoryType(StrEnum):
    AUTOBIOGRAPHICAL = "autobiographical"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    RELATIONAL = "relational"
    EMOTIONAL = "emotional"
    PROCEDURAL = "procedural"
    COUNTERFACTUAL = "counterfactual"
    DIGITAL_EXPERIENCE = "digital_experience"

    @classmethod
    def from_raw(cls, value: object, default: MemoryType | str = "semantic") -> MemoryType:
        default_type = default if isinstance(default, cls) else cls(str(default))
        if isinstance(value, cls):
            return value
        raw = str(value or "").strip().lower()
        if not raw:
            return default_type
        try:
            return cls(raw)
        except ValueError:
            pass
        if any(
            k in raw
            for k in (
                "reflect",
                "thought",
                "insight",
                "belief",
                "view",
                "opinion",
                "philosoph",
                "trait",
                "value",
                "semantic",
            )
        ):
            return cls.SEMANTIC
        if any(
            k in raw
            for k in (
                "event",
                "episode",
                "story",
                "incident",
                "experience",
                "action",
                "episodic",
            )
        ):
            return cls.EPISODIC
        if any(k in raw for k in ("feel", "emotion", "affect", "mood")):
            return cls.EMOTIONAL
        if any(
            k in raw
            for k in ("relation", "interpersonal", "friend", "colleague", "family", "social")
        ):
            return cls.RELATIONAL
        if any(k in raw for k in ("auto", "bio", "life", "self", "identity")):
            return cls.AUTOBIOGRAPHICAL
        if any(k in raw for k in ("skill", "habit", "method", "procedure", "process")):
            return cls.PROCEDURAL
        if any(k in raw for k in ("counter", "whatif", "simulat", "hypothetical")):
            return cls.COUNTERFACTUAL
        if any(k in raw for k in ("digital", "session", "chat", "turn")):
            return cls.DIGITAL_EXPERIENCE
        return default_type


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

    @model_validator(mode="before")
    @classmethod
    def _normalise_numbers(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        for field in ("source_confidence", "importance"):
            normalized = safe_probability(data.get(field), default=0.5)
            data[field] = normalized if normalized is not None else 0.5
        emotions, _ = normalize_probability_map(data.get("emotions"), field="emotions")
        data["emotions"] = emotions
        data["access_count"] = safe_int(data.get("access_count"), default=0, minimum=0)
        return data
