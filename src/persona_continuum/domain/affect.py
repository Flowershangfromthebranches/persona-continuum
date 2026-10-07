from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from persona_continuum.domain.persona import utc_now

EMOTION_NAMES = [
    "joy",
    "sadness",
    "anger",
    "fear",
    "disgust",
    "surprise",
    "anxiety",
    "jealousy",
    "shame",
    "guilt",
    "hope",
    "loneliness",
    "affection",
    "frustration",
]

NEED_NAMES = [
    "attachment",
    "recognition",
    "autonomy",
    "control",
    "safety",
    "belonging",
    "achievement",
    "curiosity",
    "continuity",
    "being_understood",
    "intimacy",
    "touch_closeness",
]


NEED_DEFAULT_BASELINES = {name: 0.5 for name in NEED_NAMES}
NEED_DEFAULT_BASELINES.update(intimacy=0.2, touch_closeness=0.2)


class AffectState(BaseModel):
    name: str
    kind: str = "emotion"
    intensity: float = 0.0
    baseline: float = 0.0
    decay_rate: float = 0.08
    updated_at: datetime = Field(default_factory=utc_now)
    triggers: list[str] = Field(default_factory=list)
    confidence: float = 0.5


class NeedState(BaseModel):
    name: str
    level: float = 0.5
    baseline: float = 0.5
    rebound_rate: float = Field(default=0.12, ge=0.0, le=2.0)
    satiation_response: float = Field(default=0.25, ge=0.0, le=1.0)
    updated_at: datetime = Field(default_factory=utc_now)
    confidence: float = 0.5
    reasons: list[str] = Field(default_factory=list)
    last_change_kind: str | None = None
    last_change_reason: str | None = None
    last_satiated_at: datetime | None = None
    last_satiation_event_id: str | None = None
