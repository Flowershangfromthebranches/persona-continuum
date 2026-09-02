"""Persona State Appraisal Service.

Turns the raw material of one turn -- what the user said, what the persona
replied, any explicit user feedback, and any explicit external events -- into a
*legal* ``state_patch`` that :class:`SessionService` can apply.

Why this exists
---------------
The previous implementation was a handful of ``if token in text`` checks that
ended with::

    return observations or {"curiosity": 0.1}

Two problems.  First, ``curiosity`` is a **need**, not an emotion, so
``AffectEngine.update_emotions`` silently dropped it via
``if name not in EMOTION_NAMES: continue`` -- the fallback was a no-op that
looked like an update.  Second, keyword hits produced fixed magnitudes with no
notion of intensity, direction, or ambivalence.

Modes
-----
``heuristic`` (default)
    Deterministic, multilingual (zh + en), zero extra model calls.  This is the
    default precisely because a second LLM call per turn is unacceptable for
    latency and cost.
``hybrid``
    Heuristic baseline plus an optional model-produced refinement patch, which
    is validated against the same schema and clamped by the same ceilings.
``model``
    Reserved for a host-injected appraiser.  Falls back to the heuristic when
    no appraiser is configured, so a misconfiguration degrades instead of
    raising.

Engine contracts this service must respect
------------------------------------------
* affect  -> absolute target intensity; ``AffectEngine`` applies
  ``intensity = clamp(max(intensity, target))``.  Emotions can therefore only
  *rise* here; cooling is the job of the engine's exponential decay.
* needs   -> additive delta; ``MotivationEngine`` applies ``level + delta``.
* relationship -> absolute set; ``RelationshipEngine`` applies ``clamp(value)``.

Because relationship values are set absolutely, an unclamped appraisal is how
you get "trust 20% -> 90%" in one turn.  Every movement is capped below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from persona_continuum.domain.affect import EMOTION_NAMES, NEED_NAMES
from persona_continuum.runtime.relationship_engine import RELATIONSHIP_FIELDS

AppraisalMode = Literal["heuristic", "hybrid", "model"]

DEFAULT_MODE: AppraisalMode = "heuristic"


class ModelAppraiser(Protocol):
    """Optional external appraiser used by the ``hybrid`` / ``model`` modes."""

    def appraise(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Return a partial ``state_patch`` or ``None`` to defer to heuristic."""


# -- lexicons ---------------------------------------------------------------
# Cues are matched case-insensitively as plain substrings.  Deliberately
# bilingual: a persona worked in Chinese must not silently stop updating.

_NEGATIVE_CUES: dict[str, tuple[str, ...]] = {
    "anxiety": ("担心", "担忧", "焦虑", "紧张", "不安", "worried", "anxious", "nervous", "uneasy"),
    "frustration": ("烦", "挫败", "受阻", "卡住", "无奈", "frustrated", "stuck", "annoying"),
    "anger": ("生气", "愤怒", "气死", "讨厌", "angry", "furious", "outrage", "hate"),
    "sadness": ("难过", "伤心", "失落", "沮丧", "sad", "sorrow", "depressed", "grief"),
    "fear": ("恐惧", "惊恐", "害怕", "terrified", "panic", "frightened", "scared"),
    "disgust": ("恶心", "厌恶", "反感", "disgust", "revolting", "sick of"),
    "shame": ("羞愧", "丢脸", "尴尬", "难堪", "ashamed", "embarrassed", "humiliated"),
    "guilt": ("内疚", "愧疚", "自责", "后悔", "guilty", "regret"),
    "loneliness": ("孤独", "寂寞", "lonely", "alone", "isolated"),
    "jealousy": ("嫉妒", "吃醋", "羡慕", "jealous", "envious"),
}

_POSITIVE_CUES: dict[str, tuple[str, ...]] = {
    "joy": ("开心", "高兴", "快乐", "太好了", "很好", "happy", "glad", "delighted", "great"),
    "hope": ("希望", "期待", "有信心", "乐观", "hope", "hopeful", "optimistic", "look forward"),
    "affection": ("喜欢", "爱", "温暖", "感动", "贴心", "love", "affection", "fond", "care about"),
    "surprise": ("惊讶", "意外", "没想到", "居然", "surprised", "astonished", "unexpected"),
}

_NEED_CUES: dict[str, tuple[str, ...]] = {
    "curiosity": (
        "为什么",
        "怎么会",
        "好奇",
        "想了解",
        "是什么",
        "why",
        "how come",
        "curious",
        "wonder",
    ),
    "being_understood": (
        "你不懂",
        "不明白我的意思",
        "误解",
        "没听懂",
        "misunderstand",
        "you don't get",
        "not what i meant",
    ),
    "autonomy": (
        "我自己",
        "别管",
        "让我决定",
        "我决定",
        "let me",
        "my own",
        "i decide",
        "i'll decide",
    ),
    "recognition": ("厉害", "真棒", "佩服", "认可", "不错", "impressive", "well done", "recogni"),
    "safety": ("安全", "稳定", "可靠", "safe", "stable", "secure", "reliable"),
    "achievement": ("完成", "成功", "做到了", "解决", "finished", "accomplished", "succeeded"),
    "belonging": ("一起", "我们", "陪伴", "together", "belong", "with us"),
    "control": ("掌控", "控制", "安排", "in control", "manage", "organize"),
    "continuity": ("一直", "还是", "照旧", "as always", "consistent", "keep going"),
    "attachment": ("想念", "离不开", "你在吗", "miss you", "stay with", "are you there"),
}

_HOSTILE_CUES: tuple[str, ...] = (
    "骗",
    "不信任",
    "你不配",
    "闭嘴",
    "背叛",
    "liar",
    "don't trust",
    "betray",
    "shut up",
)
_GRATITUDE_CUES: tuple[str, ...] = (
    "谢谢",
    "感谢",
    "多谢",
    "感激",
    "thanks",
    "thank you",
    "appreciate",
    "grateful",
)
_RESOLUTION_CUES: tuple[str, ...] = (
    "明白了",
    "解决了",
    "和解",
    "清楚了",
    "resolved",
    "settled",
    "cleared up",
    "makes sense now",
)

# Intensity multipliers.  "!!!" and 非常 both mean the cue fired harder.
_INTENSIFIERS: tuple[str, ...] = (
    "非常",
    "特别",
    "极其",
    "太",
    "超级",
    "真的",
    "extremely",
    "really",
    "very",
    "so ",
    "deeply",
    "absolutely",
)
_DIMINISHERS: tuple[str, ...] = (
    "有点",
    "稍微",
    "一点点",
    "还算",
    "kind of",
    "sort of",
    "a bit",
    "slightly",
    "somewhat",
)

_EXCLAMATION_RE = re.compile(r"[!！]{2,}")
_PUNCT_REPEAT_RE = re.compile(r"([?!。？])\1{1,}")


def _contains(text: str, cues: tuple[str, ...]) -> bool:
    return any(cue in text for cue in cues)


def _count_hits(text: str, cues: tuple[str, ...]) -> int:
    return sum(1 for cue in cues if cue in text)


def _intensity_factor(text: str) -> float:
    """How strongly the text is expressed, as a multiplier around 1.0."""

    factor = 1.0
    if _contains(text, _INTENSIFIERS):
        factor += 0.3
    if _contains(text, _DIMINISHERS):
        factor -= 0.3
    if _EXCLAMATION_RE.search(text):
        factor += 0.2
    if _PUNCT_REPEAT_RE.search(text):
        factor += 0.1
    # Very long messages usually dilute any single cue.
    if len(text) > 600:
        factor -= 0.15
    return max(0.4, min(1.6, factor))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class AppraisalLimits:
    """Per-turn movement ceilings (§19).

    Emotions are allowed the largest step because they decay; relationships get
    the smallest because they are the slowest-moving, most consequential state.
    """

    affect: float = 0.15
    need: float = 0.10
    relationship: float = 0.08
    # Discrete world events get their own, much larger ceilings: a lethal
    # threat must be able to move fear visibly in a single turn, which the
    # conversational-tone cap would forbid.
    event_affect: float = 0.85
    event_need: float = 0.60
    event_relationship: float = 0.45


@dataclass
class AppraisalRequest:
    """Everything one turn knows that could move internal state."""

    user_message: str = ""
    persona_response: str = ""
    user_feedback: str | None = None
    external_events: list[dict[str, Any]] = field(default_factory=list)
    goal_completed: bool = False
    counterpart_id: str = "user"
    # Current values, required to cap movement rather than absolute position.
    current_affect: dict[str, float] = field(default_factory=dict)
    current_needs: dict[str, float] = field(default_factory=dict)
    current_relationship: dict[str, float] = field(default_factory=dict)


@dataclass
class AppraisalResult:
    """A legal, bounded state_patch plus a public explanation of it."""

    affect: dict[str, float] = field(default_factory=dict)
    needs: dict[str, float] = field(default_factory=dict)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    mode: str = DEFAULT_MODE
    signals: list[str] = field(default_factory=list)

    @property
    def state_patch(self) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        if self.affect:
            patch["affect"] = dict(self.affect)
        if self.needs:
            patch["needs"] = dict(self.needs)
        if self.relationships:
            patch["relationships"] = list(self.relationships)
        return patch

    def is_empty(self) -> bool:
        return not (self.affect or self.needs or self.relationships)


def _event_valence(event: dict[str, Any]) -> float:
    """Signed valence from an explicit event, tolerating several shapes."""

    for key in ("valence", "weight", "impact"):
        value = event.get(key)
        if isinstance(value, (int, float)):
            return _clamp(float(value), -1.0, 1.0)
    kind = str(event.get("type") or event.get("kind") or "").lower()
    if any(token in kind for token in ("loss", "conflict", "threat", "betray", "失败", "冲突")):
        return -0.6
    if any(token in kind for token in ("support", "gift", "success", "reunion", "支持", "成功")):
        return 0.6
    return 0.0


#: Event families that map onto specific emotions rather than a generic
#: anxiety bump.  A death threat and a missed deadline are not the same event,
#: and before this table existed both produced "anxiety +0.05".
EVENT_FAMILIES: tuple[
    tuple[tuple[str, ...], dict[str, float], dict[str, float], dict[str, float]], ...
] = (
    # (tokens, affect deltas, need deltas, relationship deltas at full intensity)
    (
        ("death", "peril", "threat", "danger", "lethal", "死亡", "致命", "威胁", "危险", "遇险"),
        {"fear": 0.55, "anxiety": 0.30},
        {"safety": -0.35},
        {"perceived_threat": 0.20},
    ),
    (
        ("betray", "lie", "deceit", "deception", "撒谎", "说谎", "欺骗", "背叛", "隐瞒"),
        {"anger": 0.35, "frustration": 0.20},
        {"being_understood": -0.20, "control": 0.10},
        {"trust": -0.30, "resentment": 0.20, "perceived_threat": 0.10},
    ),
    (
        ("job_loss", "laid_off", "fired", "裁员", "解雇", "被裁"),
        {"sadness": 0.30, "frustration": 0.30, "anxiety": 0.25},
        {"safety": -0.25},
        {},
    ),
    (
        # Deliberately not bare "loss": that stays on the generic valence path
        # so a generic loss reads as anxiety while a bereavement reads as grief.
        ("bereavement", "grief", "mourn", "loss_of_person", "丧失", "去世", "过世"),
        {"sadness": 0.45},
        {"safety": -0.10},
        {},
    ),
    (
        ("rescue", "saved", "protect", "救命", "救下", "保护"),
        {"affection": 0.20, "hope": 0.15},
        {"safety": 0.20},
        {"trust": 0.25, "dependence": 0.10},
    ),
    (
        ("accident", "near_miss", "car", "车祸", "事故", "险情"),
        {"fear": 0.45, "anxiety": 0.25},
        {"safety": -0.30},
        {},
    ),
)

#: Routine messages should barely register.  This is the ceiling the *event*
#: path may produce before the per-turn appraisal cap is applied, so a lethal
#: threat can be clearly visible while a status update stays quiet.
ROUTINE_EVENT_TOKENS: tuple[str, ...] = (
    "status", "update", "meeting", "reminder", "日常", "例行", "通知", "会议", "汇报",
)


def _event_family(
    event: dict[str, Any],
) -> tuple[dict[str, float], dict[str, float], dict[str, float]] | None:
    """Map one event onto a concrete affect/need response, or None."""
    kind = str(event.get("type") or event.get("kind") or "").lower()
    summary = str(event.get("summary") or event.get("content") or "").lower()
    haystack = f"{kind} {summary}"
    if not haystack.strip():
        return None
    for tokens, affect, needs, relationship in EVENT_FAMILIES:
        if any(token in haystack for token in tokens):
            intensity = _clamp(float(event.get("intensity", 1.0)) or 1.0, 0.0, 1.5)
            scaled_affect = {name: value * intensity for name, value in affect.items()}
            scaled_needs = {name: value * intensity for name, value in needs.items()}
            scaled_rel = {name: value * intensity for name, value in relationship.items()}
            return scaled_affect, scaled_needs, scaled_rel
    if any(token in haystack for token in ROUTINE_EVENT_TOKENS):
        # Explicitly quiet: a routine message is not an emotional event.
        return {"surprise": 0.02}, {}, {}
    return None


class PersonaStateAppraisalService:
    """Appraise one turn into a bounded, schema-legal state_patch.

    The default ``heuristic`` mode is deterministic and performs no model
    call, so enabling appraisal never doubles a turn's latency.
    """

    def __init__(
        self,
        mode: AppraisalMode | str = DEFAULT_MODE,
        *,
        limits: AppraisalLimits | None = None,
        model_appraiser: ModelAppraiser | None = None,
    ) -> None:
        normalized = str(mode or DEFAULT_MODE).lower()
        if normalized not in {"heuristic", "hybrid", "model"}:
            normalized = DEFAULT_MODE
        self.mode: AppraisalMode = normalized  # type: ignore[assignment]
        self.limits = limits or AppraisalLimits()
        self.model_appraiser = model_appraiser

    # -- public API ---------------------------------------------------------
    def appraise(self, request: AppraisalRequest) -> AppraisalResult:
        result = self._heuristic(request)
        if self.mode in {"hybrid", "model"} and self.model_appraiser is not None:
            result = self._merge_model(result, request)
        elif self.mode == "model" and self.model_appraiser is None:
            # Configured for a model but none was injected: degrade to the
            # heuristic rather than raising mid-turn.
            result.summary = f"{result.summary} (model appraiser unavailable)".strip()
        return result

    def affect_observations(self, request: AppraisalRequest) -> dict[str, float]:
        """Backwards-compatible view: affect targets only (never needs)."""

        return self.appraise(request).affect

    # -- heuristic ----------------------------------------------------------
    def _heuristic(self, request: AppraisalRequest) -> AppraisalResult:
        user_text = (request.user_message or "").strip()
        persona_text = (request.persona_response or "").strip()
        feedback_text = (request.user_feedback or "").strip()
        combined = f"{user_text} {persona_text} {feedback_text}".strip()
        if not combined and not request.external_events and not request.goal_completed:
            return AppraisalResult(mode=self.mode)

        lowered = combined.lower()
        # Explicit feedback is the strongest signal a turn can carry: the user
        # is literally telling us how that exchange landed.
        feedback_weight = 1.6 if feedback_text else 1.0
        factor = _intensity_factor(lowered) * feedback_weight

        affect_delta: dict[str, float] = {}
        need_delta: dict[str, float] = {}
        # Event-sourced movement is kept apart from cue-sourced movement:
        # a lethal threat is a discrete world change and must not be capped by
        # the conversational-tone ceiling, or every event degrades to +0.04.
        event_affect: dict[str, float] = {}
        event_needs: dict[str, float] = {}
        event_relationship: dict[str, float] = {}
        signals: list[str] = []

        for emotion, cues in _NEGATIVE_CUES.items():
            hits = _count_hits(lowered, cues)
            if hits:
                affect_delta[emotion] = affect_delta.get(emotion, 0.0) + min(hits, 3) * 0.06
                signals.append(f"negative:{emotion}")

        for emotion, cues in _POSITIVE_CUES.items():
            hits = _count_hits(lowered, cues)
            if hits:
                affect_delta[emotion] = affect_delta.get(emotion, 0.0) + min(hits, 3) * 0.06
                signals.append(f"positive:{emotion}")

        # Ambivalence is normal, but two opposing pulls should not both hit
        # full strength -- otherwise every mixed message maxes everything out.
        if affect_delta and _has_opposing_pulls(affect_delta):
            for name in list(affect_delta):
                affect_delta[name] *= 0.7
            signals.append("ambivalent")

        for need, cues in _NEED_CUES.items():
            hits = _count_hits(lowered, cues)
            if hits:
                need_delta[need] = need_delta.get(need, 0.0) + min(hits, 2) * 0.04 * factor
                signals.append(f"need:{need}")

        for event in request.external_events or []:
            family = _event_family(event)
            if family is not None:
                affect_part, need_part, relationship_part = family
                for name, value in affect_part.items():
                    event_affect[name] = event_affect.get(name, 0.0) + value
                for name, value in need_part.items():
                    event_needs[name] = event_needs.get(name, 0.0) + value
                for name, value in relationship_part.items():
                    event_relationship[name] = event_relationship.get(name, 0.0) + value
                signals.append(f"event:{str(event.get('type') or event.get('kind') or 'signal')}")
                continue
            valence = _event_valence(event)
            if valence == 0.0:
                continue
            if valence < 0:
                event_affect["anxiety"] = event_affect.get("anxiety", 0.0) + 0.08 * abs(valence)
                event_needs["safety"] = event_needs.get("safety", 0.0) - 0.05 * abs(valence)
            else:
                event_affect["hope"] = event_affect.get("hope", 0.0) + 0.08 * valence
                event_needs["safety"] = event_needs.get("safety", 0.0) + 0.05 * valence
            signals.append(f"event:{valence:+.1f}")

        if request.goal_completed:
            # Goal completion is the one place a slightly larger step is
            # warranted: finishing something is a real, discrete life event.
            need_delta["achievement"] = need_delta.get("achievement", 0.0) + 0.05
            affect_delta["joy"] = affect_delta.get("joy", 0.0) + 0.06
            affect_delta["hope"] = affect_delta.get("hope", 0.0) + 0.04
            signals.append("goal_completed")

        relationship = self._relationship_changes(request, lowered, factor, signals)
        for name, value in event_relationship.items():
            base = float((request.current_relationship or {}).get(name, 0.0))
            # Relationship values are written absolutely, so express the event
            # as a capped movement from where the pair actually stands.
            cap = self.limits.event_relationship
            relationship[name] = _clamp(base + _clamp(value, -cap, cap), 0.0, 1.0)
        return self._finalize(
            request, affect_delta, need_delta, relationship, signals, event_affect, event_needs
        )

    def _relationship_changes(
        self,
        request: AppraisalRequest,
        lowered: str,
        factor: float,
        signals: list[str],
    ) -> dict[str, float]:
        current = dict(request.current_relationship or {})
        changes: dict[str, float] = {}

        # Every exchange makes the pair a little more familiar, but the step
        # shrinks as familiarity grows: a first meeting teaches you a lot, the
        # hundredth almost nothing.  A flat step would saturate familiarity
        # after twenty turns, which models a counter, not a relationship.
        familiarity = _clamp(float(current.get("familiarity", 0.0)), 0.0, 1.0)
        changes["familiarity"] = 0.06 * (1.0 - familiarity)

        if _contains(lowered, _GRATITUDE_CUES):
            changes["trust"] = changes.get("trust", 0.0) + 0.04 * factor
            changes["affection"] = changes.get("affection", 0.0) + 0.03 * factor
            signals.append("gratitude")
        if _contains(lowered, _HOSTILE_CUES):
            changes["trust"] = changes.get("trust", 0.0) - 0.06 * factor
            changes["resentment"] = changes.get("resentment", 0.0) + 0.05 * factor
            changes["perceived_threat"] = changes.get("perceived_threat", 0.0) + 0.04 * factor
            signals.append("hostile")
        if _contains(lowered, _RESOLUTION_CUES):
            changes["unresolved_conflict"] = changes.get("unresolved_conflict", 0.0) - 0.05 * factor
            signals.append("resolution")
        if request.goal_completed:
            changes["respect"] = changes.get("respect", 0.0) + 0.03
            signals.append("goal_completed_respect")

        # Convert movement into absolute targets the engine can set, capped so
        # trust cannot teleport across the scale in a single turn.
        ceiling = self.limits.relationship
        absolute: dict[str, float] = {}
        for field_name, delta in changes.items():
            if field_name not in RELATIONSHIP_FIELDS:
                continue
            base = float(current.get(field_name, 0.0))
            absolute[field_name] = _clamp(base + _clamp(delta, -ceiling, ceiling), 0.0, 1.0)
        return absolute

    def _finalize(
        self,
        request: AppraisalRequest,
        affect_delta: dict[str, float],
        need_delta: dict[str, float],
        relationship: dict[str, float],
        signals: list[str],
        event_affect: dict[str, float] | None = None,
        event_needs: dict[str, float] | None = None,
    ) -> AppraisalResult:
        affect_ceiling = self.limits.affect
        need_ceiling = self.limits.need
        event_affect = event_affect or {}
        event_needs = event_needs or {}
        event_ceiling = self.limits.event_affect

        # Drop anything illegal before it can reach an engine.  This is what
        # makes "curiosity leaked into affect" impossible by construction.
        affect: dict[str, float] = {}
        for name, delta in event_affect.items():
            if name not in EMOTION_NAMES:
                continue
            movement = _clamp(delta, -event_ceiling, event_ceiling)
            if movement > 0:
                affect[name] = movement
            elif movement < 0:
                # Only the additive patch can cool an emotion; the floor
                # engine expresses a decrease as "no change this turn".
                event_needs.setdefault("_affect_decrease_", 0.0)
        for name, delta in affect_delta.items():
            if name not in EMOTION_NAMES:
                continue
            movement = _clamp(delta, -affect_ceiling, affect_ceiling)
            # AffectEngine applies max(intensity, target): only rises survive,
            # so a negative movement is expressed as "no change this turn" and
            # cooling is left to the engine's exponential decay.
            if movement <= 0:
                continue
            current = float((request.current_affect or {}).get(name, 0.0))
            affect[name] = _clamp(current + movement, 0.0, 1.0)

        needs: dict[str, float] = {}
        for name, delta in event_needs.items():
            if name == "_affect_decrease_" or name not in NEED_NAMES:
                continue
            movement = _clamp(delta, -self.limits.event_need, self.limits.event_need)
            if abs(movement) >= 1e-6:
                needs[name] = movement
        for name, delta in need_delta.items():
            if name not in NEED_NAMES:
                continue
            movement = _clamp(delta, -need_ceiling, need_ceiling)
            if abs(movement) < 1e-6:
                continue
            needs[name] = movement

        relationships: list[dict[str, Any]] = []
        if relationship and request.counterpart_id:
            relationships.append(
                {
                    "counterpart_id": request.counterpart_id,
                    "changes": {k: v for k, v in relationship.items() if k in RELATIONSHIP_FIELDS},
                }
            )
        result = AppraisalResult(
            affect=affect,
            needs=needs,
            relationships=relationships,
            mode=self.mode,
            signals=signals,
        )
        result.summary = self._public_summary(request, result)
        return result

    @staticmethod
    def _public_summary(request: AppraisalRequest, result: AppraisalResult) -> str:
        """A short, public reason string.

        Shows what moved and by how much, never why the model thought so: no
        chain-of-thought and no private reasoning is ever exposed here.
        """

        parts: list[str] = []
        for name, target in sorted(result.affect.items()):
            current = float((request.current_affect or {}).get(name, 0.0))
            delta = target - current
            if abs(delta) < 0.005:
                continue
            parts.append(f"{name} {current:.0%} → {target:.0%} {delta:+.0%}")
        for name, delta in sorted(result.needs.items()):
            parts.append(f"need {name} {delta:+.0%}")
        for entry in result.relationships:
            changes = entry.get("changes") or {}
            moved = {
                k: v - float((request.current_relationship or {}).get(k, 0.0))
                for k, v in changes.items()
            }
            for name, delta in sorted(moved.items()):
                if abs(delta) < 0.005:
                    continue
                parts.append(f"rel {name} {delta:+.0%}")
        return ", ".join(parts)

    # -- model modes --------------------------------------------------------
    def _merge_model(
        self, result: AppraisalResult, request: AppraisalRequest
    ) -> AppraisalResult:
        assert self.model_appraiser is not None
        try:
            proposed = self.model_appraiser.appraise(
                {
                    "user_message": request.user_message,
                    "persona_response": request.persona_response,
                    "user_feedback": request.user_feedback,
                    "external_events": request.external_events,
                    "goal_completed": request.goal_completed,
                    "heuristic": result.state_patch,
                }
            )
        except Exception:
            return result
        if not isinstance(proposed, dict) or not proposed:
            return result

        merged = AppraisalResult(
            affect=dict(result.affect),
            needs=dict(result.needs),
            relationships=list(result.relationships),
            mode=self.mode,
            signals=list(result.signals) + ["model_refined"],
        )
        for name, value in (proposed.get("affect") or {}).items():
            if name in EMOTION_NAMES and isinstance(value, (int, float)):
                merged.affect[str(name)] = _clamp(float(value), 0.0, 1.0)
        for name, value in (proposed.get("needs") or {}).items():
            if name in NEED_NAMES and isinstance(value, (int, float)):
                merged.needs[str(name)] = _clamp(float(value), -1.0, 1.0)
        if isinstance(proposed.get("relationships"), list):
            merged.relationships = [
                entry
                for entry in proposed["relationships"]
                if isinstance(entry, dict) and str(entry.get("counterpart_id", ""))
            ] or merged.relationships
        # Re-finalize so a model-produced patch obeys the same ceilings.
        rebuilt = self._finalize(
            request,
            {
                name: value - float((request.current_affect or {}).get(name, 0.0))
                for name, value in merged.affect.items()
            },
            dict(merged.needs),
            (merged.relationships[0]["changes"] if merged.relationships else {}),
            merged.signals,
        )
        return rebuilt


def _has_opposing_pulls(affect_delta: dict[str, float]) -> bool:
    negative = any(name in _NEGATIVE_CUES for name in affect_delta)
    positive = any(name in _POSITIVE_CUES for name in affect_delta)
    return negative and positive


DEFAULT_LIMITS = AppraisalLimits()


__all__ = [
    "AppraisalLimits",
    "AppraisalMode",
    "AppraisalRequest",
    "AppraisalResult",
    "DEFAULT_LIMITS",
    "DEFAULT_MODE",
    "ModelAppraiser",
    "PersonaStateAppraisalService",
]
