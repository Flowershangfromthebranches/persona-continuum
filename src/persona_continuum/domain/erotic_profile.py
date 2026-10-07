"""Optional adult erotic repertoire. Missing fields are unknown, not refusal."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from persona_continuum.domain.core_traits import strength_value

EROTIC_FIELDS = (
    "sexual_initiative",
    "foreplay_initiative",
    "physical_teasing",
    "verbal_teasing",
    "dirty_talk_comfort",
    "dirty_talk_initiative",
    "roleplay_comfort",
    "power_play_comfort",
    "consensual_degradation_comfort",
    "pet_name_preference",
    "dominance_tendency",
    "submission_tendency",
    "switch_tendency",
    "sexual_directness",
    "sexual_playfulness",
    "sexual_inhibition",
    "erotic_embarrassment",
)


class EroticProfile(BaseModel):
    sexual_initiative: float | None = Field(default=None, ge=0, le=1)
    foreplay_initiative: float | None = Field(default=None, ge=0, le=1)
    physical_teasing: float | None = Field(default=None, ge=0, le=1)
    verbal_teasing: float | None = Field(default=None, ge=0, le=1)
    dirty_talk_comfort: float | None = Field(default=None, ge=0, le=1)
    dirty_talk_initiative: float | None = Field(default=None, ge=0, le=1)
    roleplay_comfort: float | None = Field(default=None, ge=0, le=1)
    power_play_comfort: float | None = Field(default=None, ge=0, le=1)
    consensual_degradation_comfort: float | None = Field(default=None, ge=0, le=1)
    pet_name_preference: float | None = Field(default=None, ge=0, le=1)
    dominance_tendency: float | None = Field(default=None, ge=0, le=1)
    submission_tendency: float | None = Field(default=None, ge=0, le=1)
    switch_tendency: float | None = Field(default=None, ge=0, le=1)
    sexual_directness: float | None = Field(default=None, ge=0, le=1)
    sexual_playfulness: float | None = Field(default=None, ge=0, le=1)
    sexual_inhibition: float | None = Field(default=None, ge=0, le=1)
    erotic_embarrassment: float | None = Field(default=None, ge=0, le=1)
    liked_terms: list[str] = Field(default_factory=list)
    disliked_terms: list[str] = Field(default_factory=list)
    notes: str = ""

    def filled(self) -> dict[str, float]:
        return {
            name: value
            for name in EROTIC_FIELDS
            if (value := getattr(self, name)) is not None
        }


def parse_erotic_profile(value: Any) -> EroticProfile | None:
    if not isinstance(value, dict) or not value:
        return None
    data: dict[str, Any] = {}
    for name in EROTIC_FIELDS:
        if name in value and value[name] not in (None, "", "unknown", "neutral"):
            data[name] = strength_value(value[name], default=0.55)
    for name in ("liked_terms", "disliked_terms"):
        raw = value.get(name) or []
        if isinstance(raw, list):
            data[name] = [str(item)[:80] for item in raw if str(item).strip()]
    if isinstance(value.get("notes"), str):
        data["notes"] = value["notes"][:500]
    profile = EroticProfile.model_validate(data)
    if not profile.filled() and not profile.liked_terms and not profile.disliked_terms:
        return None
    return profile


def is_adult_identity(identity: Any) -> bool:
    if not isinstance(identity, dict):
        return False
    age = identity.get("age")
    return isinstance(age, (int, float)) and not isinstance(age, bool) and age >= 18
