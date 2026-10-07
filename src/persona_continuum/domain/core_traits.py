"""Stable, evidence-backed personality attributes, separate from active drives."""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, Field

STRENGTHS = {
    "low": 0.25,
    "moderate": 0.55,
    "medium_high": 0.68,
    "high": 0.70,
    "very_high": 0.85,
    "extreme": 0.95,
}
CORE_COMPONENT_KEYS = (
    "identity_profile",
    "temperament",
    "attachment_patterns",
    "needs_and_desires",
    "dominant_traits",
    "embodied_identity",
    "expression_style",
    "anti_patterns",
    "decision_heuristics",
    "values",
    "erotic_profile",
)


def strength_value(value: Any, default: float = 0.55) -> float:
    if isinstance(value, str):
        label = value.strip().lower().replace(" ", "_")
        if label in STRENGTHS:
            return STRENGTHS[label]
        try:
            value = float(value)
        except ValueError:
            return default
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return max(0.0, min(1.0, float(value)))
    return default


def strength_label(value: float) -> str:
    return (
        "EXTREMELY HIGH"
        if value >= 0.9
        else "VERY HIGH"
        if value >= 0.8
        else "HIGH"
        if value >= 0.65
        else "MODERATE"
        if value >= 0.4
        else "LOW"
    )


class CoreTrait(BaseModel):
    trait: str = Field(min_length=1, max_length=160)
    strength: float = Field(default=0.55, ge=0, le=1)
    stability: str = "stable"
    behavioral_implications: list[str] = Field(default_factory=list)
    source_component: str = "dominant_traits"


class NeedProfile(BaseModel):
    name: str
    baseline: float = Field(ge=0, le=1)
    rebound_rate: float = Field(default=0.12, ge=0, le=2)
    satiation_response: float = Field(default=0.25, ge=0, le=1)
