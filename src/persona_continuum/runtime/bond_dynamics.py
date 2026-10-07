"""Local interaction appraisal. Identity changes require sustained or explicit evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from persona_continuum.domain.persona import utc_now
from persona_continuum.domain.relationship import RelationshipKind, RelationshipState
from persona_continuum.security.validation import clamp

PRIORS: dict[str, tuple[float, float, float, float]] = {
    "stranger": (0, 0, 0, 0),
    "acquaintance": (0.25, 0.15, 0.12, 0),
    "friend": (0.55, 0.5, 0.45, 0.1),
    "close_friend": (0.8, 0.75, 0.7, 0.3),
    "flirting": (0.45, 0.35, 0.5, 0.05),
    "dating": (0.65, 0.6, 0.7, 0.25),
    "partner": (0.9, 0.8, 0.85, 0.55),
    "family": (0.9, 0.65, 0.7, 0.4),
    "rival": (0.5, 0.15, 0.05, 0),
    "enemy": (0.5, 0.02, 0, 0),
    "custom": (0.2, 0.1, 0.1, 0),
}


def initial_state(persona_id: str, counterpart: str, value: dict[str, Any]) -> RelationshipState:
    kind = RelationshipKind(value.get("relationship_kind", "stranger"))
    f, t, a, d = PRIORS[kind]
    data: dict[str, Any] = dict(
        familiarity=f,
        trust=t,
        affection=a,
        dependence=d,
        relationship_prior=min(t, a),
        bond_stage="established" if f >= 0.55 else "new",
    )
    data.update(value)
    data.update(persona_id=persona_id, counterpart=counterpart, relationship_kind=kind)
    state = RelationshipState.model_validate(data)
    state.boundary_explicitness = boundary_level(state)
    return state


def bond_strength(s: RelationshipState) -> float:
    identity = {
        "partner": 0.28,
        "family": 0.22,
        "dating": 0.15,
        "close_friend": 0.12,
        "friend": 0.06,
    }.get(s.relationship_kind, 0)
    # Familiarity alone contributes almost nothing; hurt does not erase importance.
    return clamp(
        identity
        + 0.3 * s.affection
        + 0.22 * s.trust
        + 0.18 * s.dependence
        + 0.04 * s.familiarity
        + 0.12 * s.relationship_prior
        - 0.06 * s.resentment
        - 0.04 * s.unresolved_conflict
    )


def boundary_level(s: RelationshipState, act: str = "") -> float:
    if act in {"boundary_violation", "refusal"}:
        return 1.0
    return clamp(1 - 0.65 * s.trust - 0.45 * s.affection)


# Acts describe what happened, rather than naming the emotion it should cause.
ACT_CUES: dict[str, tuple[str, ...]] = {
    "conflict": ("我恨你", "你真恶心", "i hate you"),
    "boundary_violation": ("不管你愿不愿意", "强迫你"),
    "refusal": (
        "今天不想亲",
        "不要碰我",
        "不想抱",
        "不想做",
        "想独处",
        "need some space",
        "not tonight",
    ),
    "repair": (
        "说开了",
        "和好了",
        "对不起",
        "刚才在忙",
        "手机没电",
        "不是不理你",
        "i am sorry",
        "i was busy",
    ),
    "betrayal": ("一直在骗你", "背叛了你", "出轨了", "i lied to you", "i cheated on you"),
    "withdrawal": ("你自己睡吧", "今天不去了", "别找我", "不想理你", "leave me alone"),
    "absence": ("一天没回复", "没有回我", "没回消息", "no reply all day"),
    "support": ("我陪你", "我在这里", "慢慢说", "辛苦了", "听你说", "here for you"),
    "affection": ("想你", "想念你", "喜欢你", "我爱你", "miss you", "love you"),
    "shared_activity": ("一起", "还记得", "老地方", "我们的梗", "together", "remember when"),
    "sharing": ("今天我", "跟你说", "给你看", "告诉你", "my day", "let me tell you"),
    "attention": ("晚安", "早安", "吃饭了吗", "回来了", "good morning", "good night"),
}
ALIASES = {
    "comfort": "support",
    "reunion": "affection",
    "promise_broken": "betrayal",
    "conflict_repair": "repair",
    "intimacy_rejected": "refusal",
    "rejection": "refusal",
    "deception": "betrayal",
    "romantic_mutual": "mutual_romance",
}


def interaction_act(message: str, events: list[dict[str, Any]]) -> str:
    for event in events:
        raw = str(event.get("type") or event.get("kind") or "").lower()
        act = ALIASES.get(raw, raw)
        if (
            act in ACT_CUES
            or act in {"conversation", "neutral"}
            or act
            in {
                "promise_kept",
                "mutual_romance",
                "commitment",
                "conflict",
                "boundary_violation",
                "intimacy",
                "touch",
                "banter",
            }
        ):
            return act
    text = message.lower()
    for act, cues in ACT_CUES.items():
        if any(cue in text for cue in cues):
            return act
    return "conversation" if len(text.strip()) >= 4 else "neutral"


@dataclass
class BondAppraisal:
    state: RelationshipState
    act: str
    affect: dict[str, float] = field(default_factory=dict)
    needs: dict[str, float] = field(default_factory=dict)
    salient: bool = False


def appraise_bond(
    s: RelationshipState,
    message: str,
    events: list[dict[str, Any]],
    needs: dict[str, float],
    emotions: dict[str, float],
    response: str = "",
) -> BondAppraisal:
    act = interaction_act(message, events)
    reciprocal = any(
        cue in response.lower()
        for cue in (
            "我也喜欢你",
            "我也爱你",
            "我愿意",
            "我们在一起",
            "i love you too",
            "yes, let's date",
        )
    )
    if reciprocal and act == "affection":
        act = "mutual_romance"
    if reciprocal and any(
        cue in message.lower()
        for cue in ("我们交往", "在一起吧", "做我女朋友", "做我男朋友", "let's date")
    ):
        act = "commitment"
    result = BondAppraisal(s.model_copy(deep=True), act)
    n = result.state
    n.updated_at = utc_now()
    n.reasons = (s.reasons + [f"interaction:{act}"])[-32:]
    importance = bond_strength(s)
    activation = 0.15 + importance * (1 + 0.5 * needs.get("attachment", 0.5))
    positive = {
        "conversation": 0.22,
        "attention": 0.45,
        "sharing": 0.6,
        "shared_activity": 0.7,
        "support": 1.0,
        "affection": 0.85,
        "promise_kept": 1.0,
        "repair": 0.7,
        "mutual_romance": 0.9,
        "commitment": 0.9,
        "intimacy": 0.9,
        "touch": 0.7,
        "banter": 0.4,
    }.get(act, 0.0)
    negative = {
        "withdrawal": -0.45,
        "absence": -0.3,
        "conflict": -0.7,
        "betrayal": -1.0,
        "boundary_violation": -1.0,
    }.get(act, 0.0)
    valence = positive or negative
    n.meaningful_interactions += int(act != "neutral")
    n.recent_valence = 0.8 * s.recent_valence + 0.2 * valence
    n.recent_acts = (s.recent_acts + [act])[-12:]
    if act != "neutral":
        n.familiarity = clamp(s.familiarity + 0.045 * (1 - s.familiarity) * (0.5 + abs(valence)))
    if positive:
        n.positive_history += positive
        reliability = min(1.0, n.positive_history / 10)
        safety = (1 - s.unresolved_conflict) * (1 - 0.8 * s.perceived_threat)
        growth = positive * reliability * safety * (0.65 + 0.35 * max(0.0, s.recent_valence))
        n.trust = clamp(s.trust + 0.025 * growth * (1 - s.trust))
        n.affection = clamp(s.affection + 0.025 * growth * (1 - s.affection))
        if s.trust > 0.4 and s.affection > 0.4:
            n.dependence = clamp(
                s.dependence
                + 0.012 * growth * needs.get("_attachment_baseline", 0.5) * (1 - s.dependence)
            )
        # Repair needs evidence; time alone does not make betrayal disappear.
        repair = 0.08 if act == "repair" else 0.008 * positive * reliability
        n.unresolved_conflict = clamp(s.unresolved_conflict - repair)
        n.resentment = clamp(s.resentment - repair * 0.7)
        n.perceived_threat = clamp(s.perceived_threat - repair * 0.6)
        result.affect = {
            "joy": 0.045 * activation * positive,
            "affection": 0.035 * activation * positive,
            "hope": 0.025 * activation * positive,
            "anxiety": -0.07 * activation * positive,
            "loneliness": -0.09 * activation * positive,
            "frustration": -0.06 * activation * positive,
        }
        if act in {"support", "repair", "affection", "intimacy", "touch"}:
            result.needs = {
                "attachment": -0.07 * activation,
                "safety": -0.06 * activation,
                "being_understood": -0.05 * activation,
            }
        if act == "shared_activity":
            result.needs["belonging"] = -0.07 * activation
        if act in {"intimacy", "touch"}:
            result.needs["intimacy" if act == "intimacy" else "touch_closeness"] = (
                -needs.get(
                    "_satiation_intimacy" if act == "intimacy" else "_satiation_touch_closeness",
                    0.25,
                ) * importance
            )
    if act in {"withdrawal", "absence", "conflict", "betrayal", "boundary_violation"}:
        magnitude = abs(negative) * activation * (1 + max(0.0, -s.recent_valence))
        result.affect = {
            "anxiety": 0.18 * magnitude,
            "loneliness": 0.2 * magnitude,
            "frustration": 0.14 * magnitude,
            "joy": -0.12 * magnitude,
        }
        result.needs = {"attachment": 0.09 * magnitude, "safety": 0.06 * magnitude}
        if act in {"conflict", "betrayal", "boundary_violation"}:
            harm = 0.3 if act != "conflict" else 0.04 * (1 - 0.5 * s.relationship_prior)
            n.trust = clamp(s.trust - harm)
            n.affection = clamp(s.affection - harm * 0.45)
            n.resentment = clamp(s.resentment + harm * 0.8)
            n.unresolved_conflict = clamp(s.unresolved_conflict + harm)
            n.perceived_threat = clamp(s.perceived_threat + harm * 0.6)
            result.affect["anger"] = 0.25 * magnitude
    if act == "refusal":
        result.affect = {"sadness": 0.035 * importance, "frustration": 0.015 * importance}
    if act in {"mutual_romance", "commitment"}:
        n.romantic_evidence += 1
    if s.relationship_kind in {
        "stranger",
        "acquaintance",
        "friend",
        "close_friend",
        "flirting",
        "dating",
    }:
        if act == "commitment" and n.romantic_evidence >= 3 and n.trust > 0.5:
            n.relationship_kind = RelationshipKind.PARTNER
        elif n.romantic_evidence >= 4 and n.trust > 0.4 and n.affection > 0.4:
            n.relationship_kind = RelationshipKind.DATING
        elif n.romantic_evidence >= 2 and n.affection > 0.2:
            n.relationship_kind = RelationshipKind.FLIRTING
        elif n.romantic_evidence == 0:
            if n.trust > 0.65 and n.affection > 0.6:
                n.relationship_kind = RelationshipKind.CLOSE_FRIEND
            elif n.trust > 0.3 and n.affection > 0.28 and n.positive_history >= 10:
                n.relationship_kind = RelationshipKind.FRIEND
            elif n.familiarity > 0.2:
                n.relationship_kind = RelationshipKind.ACQUAINTANCE
    n.relationship_prior = max(s.relationship_prior, min(n.trust, n.affection) * 0.85)
    n.bond_stage = (
        "established" if n.relationship_prior >= 0.5 else ("forming" if n.trust > 0.2 else "new")
    )
    n.trajectory = (
        "repairing"
        if positive and s.unresolved_conflict > 0.05
        else "strained"
        if n.unresolved_conflict > 0.2
        else "cooling"
        if n.recent_valence < -0.15
        else "deepening"
        if n.recent_valence > 0.5 and n.trust > 0.4
        else "warming"
        if n.recent_valence > 0.15
        else "stable"
    )
    n.boundary_explicitness = boundary_level(n, act)
    result.salient = (
        s.relationship_kind != n.relationship_kind
        or act in {"betrayal", "boundary_violation", "commitment"}
        or (act == "repair" and s.unresolved_conflict > 0.1)
        or (n.meaningful_interactions % 12 == 0 and n.recent_acts.count(act) >= 4)
    )
    # Saturation and recovery depend on the present state, never on a random roll.
    result.affect = {
        k: v * (1 - emotions.get(k, 0.0) if v > 0 else emotions.get(k, 0.0))
        for k, v in result.affect.items()
    }
    return result


def relationship_stance(s: RelationshipState, needs: dict[str, float]) -> str:
    def level(value: float) -> str:
        return (
            "very high"
            if value >= 0.8
            else "high"
            if value >= 0.6
            else "moderate"
            if value >= 0.3
            else "low"
        )

    lines = [
        f"Counterpart: {s.counterpart}",
        f"Relationship: {s.relationship_kind.value} ({s.bond_stage})",
        f"Bond: {level(bond_strength(s))}; Trust: {level(s.trust)}; "
        f"Affection: {level(s.affection)}",
        f"Attachment: {level(s.dependence)}; Current tension: {level(s.unresolved_conflict)}",
        f"Recent trajectory: {s.trajectory}",
    ]
    recent = s.recent_acts[-1] if s.recent_acts else ""
    safe_established = (
        s.relationship_kind in {"partner", "dating"}
        and s.perceived_threat < 0.3
        and s.unresolved_conflict < 0.45
    )
    if recent in {"refusal", "boundary_violation", "pressure_after_refusal"}:
        lines.append("Respect an explicit refusal.")
        lines.append(
            "They already heard a clear no. If they keep pressing, treat that as "
            "boundary pressure. Do not relaunch the declined advance."
        )
        return "\n".join(lines)
    if safe_established:
        lines.append(
            "This is your established intimate partner. Respond with familiar warmth, "
            "trust, playfulness and natural intimacy. Adult flirting and intimacy are "
            "ordinary parts of this relationship."
        )
        lines.append("Respect an explicit refusal.")
    else:
        if bond_strength(s) > 0.5:
            lines.append(
                "This person matters emotionally. Let shared familiarity, playfulness and "
                "vulnerability shape your voice."
            )
        lines.append("Respect an explicit refusal.")
    safety = s.trust * (1 - s.perceived_threat)
    if needs.get("attachment", 0) * s.affection > 0.3:
        lines.append(
            "You may want to initiate contact, express missing them, or seek "
            "companionship and reassurance."
        )
    if needs.get("belonging", 0) > 0.65:
        lines.append("Shared activities and inclusion are currently appealing.")
    if needs.get("being_understood", 0) > 0.65:
        lines.append(
            "Being misunderstood matters more right now; seek clarification in your own voice."
        )
    if needs.get("intimacy", 0) * s.affection * safety > 0.3:
        lines.append(
            "Consensual closeness may feel motivating; express it according to your "
            "personality and the other person's response."
        )
    if needs.get("intimacy", 0) > 0.65 and safety < 0.3:
        lines.append("Desire may be present while low relational safety inhibits acting on it.")
    return "\n".join(lines)
