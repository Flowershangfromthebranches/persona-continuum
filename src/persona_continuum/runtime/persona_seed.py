"""Conservative, one-time baseline extraction from compiled persona components."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from persona_continuum.domain.affect import EMOTION_NAMES, NEED_NAMES
from persona_continuum.domain.core_traits import strength_value

NEED_ALIASES = {
    "attachment": ("依恋", "黏人", "attachment"),
    "intimacy": ("亲密", "性欲", "intimacy"),
    "touch_closeness": ("身体亲密", "身体接触", "身体靠近", "拥抱", "physical_touch", "touch"),
    "belonging": ("归属", "belonging"),
    "being_understood": ("被理解", "being_understood"),
}


def text_strength(text: str) -> float | None:
    """Conservative clause-level fallback; structured compiler values take precedence."""
    grades = (
        (0.95, ("extreme", "极强", "极高", "欲求不满", "几乎每天")),
        (0.85, ("very_high", "very high", "非常强", "非常想", "强烈", "特别高")),
        (0.25, ("low", "低", "冷淡", "克制", "不喜欢", "不需要")),
        (0.70, ("high", "高", "喜欢", "黏人", "主动依恋", "驱动力", "渴望", "享受")),
        (0.55, ("moderate", "适中", "中等")),
    )
    for strength, cues in grades:
        if any(cue in text for cue in cues):
            return strength
    return None


class PersonaRuntimeSeed(BaseModel):
    version: int = 2
    emotion_baselines: dict[str, float] = Field(default_factory=dict)
    need_baselines: dict[str, float] = Field(default_factory=dict)
    need_rebound_rates: dict[str, float] = Field(default_factory=dict)
    need_satiation_responses: dict[str, float] = Field(default_factory=dict)
    decay_rate: float = 0.08
    source_component_ids: list[str] = Field(default_factory=list)
    relationship_priors: dict[str, dict[str, Any]] = Field(default_factory=dict)


def build_seed(components: list[dict[str, Any]]) -> PersonaRuntimeSeed:
    seed = PersonaRuntimeSeed()
    explicit: set[str] = set()
    for component in components:
        key, content = component["component_key"], component["content"]
        if key not in {
            "temperament",
            "attachment_patterns",
            "needs_and_desires",
            "relationships",
            "dominant_traits",
            "embodied_identity",
        }:
            continue
        seed.source_component_ids.append(component["component_id"])
        if key == "relationships":
            for item in content if isinstance(content, list) else []:
                if not isinstance(item, dict):
                    continue
                counterpart = (
                    item.get("counterpart_id") or item.get("counterpart") or item.get("name")
                )
                kind = item.get("relationship_kind") or item.get("kind") or item.get("relationship")
                aliases = {
                    "恋人": "partner",
                    "伴侣": "partner",
                    "好友": "close_friend",
                    "朋友": "friend",
                    "家人": "family",
                }
                if counterpart and isinstance(kind, str):
                    from persona_continuum.runtime.bond_dynamics import PRIORS

                    kind = aliases.get(kind, kind)
                    if kind in PRIORS:
                        seed.relationship_priors[str(counterpart)] = {"relationship_kind": kind}
            continue
        # Prefer explicit numbers; prose is a narrow fallback within the relevant component.
        if isinstance(content, dict):
            for name, value in (content.get("emotion_baselines") or {}).items():
                if name in EMOTION_NAMES and isinstance(value, int | float):
                    seed.emotion_baselines[name] = max(0.0, min(0.8, float(value)))
            for name, value in (content.get("need_baselines") or {}).items():
                if name in NEED_NAMES:
                    seed.need_baselines[name] = strength_value(value)
                    explicit.add(name)
            decay = content.get("decay_rate")
            if isinstance(decay, int | float):
                seed.decay_rate = max(0.01, min(1.0, float(decay)))
        entries = content if isinstance(content, list) else [content]
        for entry in entries:
            if isinstance(entry, dict):
                name = entry.get("name") or entry.get("need") or entry.get("trait")
                value = entry.get("baseline", entry.get("level", entry.get("strength")))
                if name in NEED_NAMES and value is not None:
                    name = str(name)
                    seed.need_baselines[name] = strength_value(value)
                    explicit.add(name)
                    rate = entry.get("rebound_rate", 0.12)
                    response = entry.get("satiation_response", 0.25)
                    if isinstance(rate, (int, float)):
                        seed.need_rebound_rates[name] = max(0.0, min(2.0, float(rate)))
                    seed.need_satiation_responses[name] = strength_value(response, 0.25)
            text = json.dumps(entry, ensure_ascii=False).lower()
            for clause in re.split(r"[。；;，,]", text):
                strength = text_strength(clause)
                if strength is None:
                    continue
                for name, cues in NEED_ALIASES.items():
                    if name not in explicit and any(cue in clause for cue in cues):
                        seed.need_baselines[name] = strength
                        if name == "attachment" and strength >= 0.7:
                            seed.emotion_baselines.setdefault("affection", 0.14)
            if key == "temperament":
                if any(cue in text for cue in ("外向", "extravert", "outgoing", "情绪丰富")):
                    seed.emotion_baselines.setdefault("joy", 0.18)
                    seed.emotion_baselines.setdefault("affection", 0.16)
                if any(cue in text for cue in ("冷淡", "克制", "reserved")):
                    seed.emotion_baselines.setdefault("joy", 0.03)
                    seed.emotion_baselines.setdefault("affection", 0.02)
    return seed
