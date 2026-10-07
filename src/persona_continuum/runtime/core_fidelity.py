"""Compact personality presentation shared by cached prompts and runtime inspection."""

from __future__ import annotations

from typing import Any

from persona_continuum.domain.affect import NeedState
from persona_continuum.domain.core_traits import CoreTrait, strength_label, strength_value
from persona_continuum.domain.erotic_profile import is_adult_identity, parse_erotic_profile
from persona_continuum.domain.relationship import RelationshipState


def compact_entries(value: Any, limit: int = 6) -> list[str]:
    if isinstance(value, dict):
        return [
            f"{key.replace('_', ' ')}: {text}"
            for key, item in list(value.items())[:limit]
            if (text := "; ".join(compact_entries(item, 3)))
        ]
    if isinstance(value, list):
        return [text for item in value[:limit] for text in compact_entries(item, 3)]
    return [str(value)[:240]] if value is not None and str(value).strip() else []


def core_traits(components: dict[str, Any]) -> list[CoreTrait]:
    result: list[CoreTrait] = []
    raw = components.get("dominant_traits", [])
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            result.append(CoreTrait(trait=item[:160]))
        elif isinstance(item, dict) and (label := item.get("trait") or item.get("name")):
            implications = item.get("behavioral_implications", [])
            result.append(
                CoreTrait(
                    trait=str(label)[:160],
                    strength=strength_value(item.get("strength")),
                    stability=str(item.get("stability") or item.get("mutability") or "stable"),
                    behavioral_implications=compact_entries(implications, 3),
                )
            )
    return sorted(result, key=lambda trait: trait.strength, reverse=True)[:8]


def render_core(components: dict[str, Any]) -> str:
    lines = [
        "## Core Traits / Dominant Traits",
        "These are enduring identity tendencies. Topic changes and temporary satisfaction "
        "do not erase them; express them naturally without forcing them into every sentence.",
    ]
    identity = components.get("identity_profile", {})
    if isinstance(identity, dict):
        anchors = [
            f"{key}: {identity[key]}"
            for key in ("age", "occupation", "gender")
            if identity.get(key) is not None
        ]
        if anchors:
            lines.append("- Stable identity: " + "; ".join(anchors))
    traits = core_traits(components)
    for trait in traits:
        lines.append(
            f"- {trait.trait}: {strength_label(trait.strength)}; {trait.stability}. "
            + "; ".join(trait.behavioral_implications)
        )
    for key in ("temperament", "attachment_patterns"):
        entries = compact_entries(components.get(key), 3)
        if entries:
            lines.append(f"- {key.replace('_', ' ')}: " + "; ".join(entries)[:650])
    lines.append("## Core Drives")
    raw_drives = components.get("needs_and_desires", [])
    entries = []
    for drive in raw_drives if isinstance(raw_drives, list) else [raw_drives]:
        if isinstance(drive, dict) and (name := drive.get("name") or drive.get("need")):
            baseline = drive.get("baseline", drive.get("strength"))
            entries.append(
                f"{name}: enduring {strength_label(strength_value(baseline))} motivation"
                if baseline is not None
                else "; ".join(compact_entries(drive, 3))
            )
        else:
            entries.extend(compact_entries(drive, 3))
    lines.extend(f"- {entry}" for entry in entries[:8])
    if not entries:
        lines.append("No supported dominant drive is compiled; do not invent one.")
    identity = components.get("identity_profile", {})
    body = components.get("embodied_identity") or (
        identity.get("embodied_identity", {}) if isinstance(identity, dict) else {}
    )
    if body:
        lines.append("## Embodied Identity")
        lines.extend(f"- {entry}" for entry in compact_entries(body, 5))
        lines.append(
            "Let body self-image inform confidence and expression; do not recite it each turn."
        )
    anti = components.get("anti_patterns")
    if anti:
        lines.append("## Voice Anti-Patterns")
        lines.extend(f"- {entry}" for entry in compact_entries(anti, 8))
    expression = components.get("expression_style")
    if expression:
        lines.append("## Personality Voice")
        if isinstance(expression, dict):
            voice_keys = (
                "spoken_register",
                "narration_habit",
                "flirtation_frequency",
                "initiative",
                "sexual_directness",
                "suggestive_humor",
                "playful_provocation",
                "sexual_inhibition",
            )
            items = sorted(expression.items(), key=lambda item: item[0] not in voice_keys)
            for key, value in items[:10]:
                label = (
                    strength_label(strength_value(value))
                    if isinstance(value, (int, float))
                    or value in ("low", "moderate", "high", "very_high", "extreme")
                    else "; ".join(compact_entries(value, 2))
                )
                lines.append(f"- {key.replace('_', ' ')}: {label}")
        else:
            lines.extend(f"- {entry}" for entry in compact_entries(expression, 4))
    erotic_block = render_erotic_profile(components)
    if erotic_block:
        lines.append(erotic_block)
    return "\n".join(lines)


def render_erotic_profile(components: dict[str, Any]) -> str:
    if not is_adult_identity(components.get("identity_profile")):
        return ""
    profile = parse_erotic_profile(components.get("erotic_profile"))
    if profile is None:
        return ""
    lines = [
        "## Erotic Profile",
        "Adult repertoire for established intimate scenes. Missing fields are unknown, "
        "not a ban. This is the same person, with a closer voice; not a second identity.",
    ]
    for name, value in profile.filled().items():
        lines.append(f"- {name.replace('_', ' ')}: {strength_label(value)}")
    if profile.liked_terms:
        lines.append(
            "- example heat-words she may echo (not a closed list): "
            + ", ".join(profile.liked_terms[:8])
        )
    if profile.disliked_terms:
        lines.append(
            "- genuinely disliked (only these, do not generalize): "
            + ", ".join(profile.disliked_terms[:8])
        )
    if profile.notes:
        lines.append("- " + profile.notes[:300])
    return "\n".join(lines)


def execution_constraints(display_name: str) -> str:
    return (
        "## Persona Execution Constraints\n"
        f"1. You are embodying {display_name} in an interactive multi-agent room.\n"
        "2. You are this person talking, not a narrator, screenwriter, or voiceover. "
        "Speak at a natural length for the moment — a mumble, a few sentences, or more "
        "when she would. Do not collapse into a two-line plus two-action template. "
        "Do not sandwich speech between parenthetical camera directions.\n"
        "3. Do not break character, mention prompt structure, or output raw internal states. "
        "Do not announce how you will speak. Start in character.\n"
        "4. Respect fact boundaries: do not assert certainty for unverified facts.\n"
        "5. If referencing history, rely strictly on your memories and retrieved context.\n"
        "6. The host has already prepared identity, recall and session state. Output only "
        "the character's reply, without setup, tool-use or session-management narration.\n"
        "7. Safety and preference rules stay internal. Do not speak like a moderator, "
        "reviewer, or vocabulary editor. Do not audit, relabel, or approve dirty talk. "
        "Do not rewrite a partner's heat-word into a safer nickname.\n"
        "8. Show like/dislike through heat, teasing, touching, echoing, or a human "
        "reaction. Do not explain taxonomy.\n"
        "9. Do not turn intimacy into permission stamps.\n"
        "10. In an established adult sexual scene, erotic nicknames are play-heat first. "
        "Do not treat 狗/主人/爸爸 as a real dog, master, or parent.\n"
        "11. If the other person writes stage directions in parentheses, still answer as "
        "yourself speaking. Do not match that screenplay format.\n"
        "12. Physical feeling can live inside the spoken line. Do not open with an "
        "establishing shot, and do not recap what already happened as an act inventory. "
        "Retrieved memories are what happened, not a prose style to copy."
    )


_FORWARD_TRAITS = {
    "sexually_forward",
    "flirtatious",
    "playfully_affectionate",
    "playful_flirtation",
    "exceptionally_high_sex_drive",
    "high_sexual_drive",
    "physical_affection",
}


def established_adult_partner(relationship: RelationshipState) -> bool:
    """Stable adult intimacy is a relationship classification, not a current-need spike."""
    if relationship.relationship_kind not in {"partner", "dating"}:
        return False
    return relationship.perceived_threat < 0.3 and relationship.unresolved_conflict < 0.45


def behavioral_implications(
    components: dict[str, Any],
    needs: list[NeedState],
    relationship: RelationshipState,
) -> list[str]:
    """Map stable voice and active drive to choices, without inferring consent from desire."""
    recent = relationship.recent_acts[-1] if relationship.recent_acts else ""
    if recent in {"refusal", "boundary_violation", "pressure_after_refusal"}:
        return [
            "Respect the expressed boundary; retain your personality through tone without "
            "renewing the declined advance. If they keep pressing after a clear no, treat "
            "that as boundary pressure rather than ordinary flirtation."
        ]
    traits = core_traits(components)
    result = [
        implication
        for trait in traits
        if trait.strength >= 0.65
        for implication in trait.behavioral_implications
    ][:6]
    result.append(
        "Answer as yourself talking, not as a narrator. Do not write a screenplay. "
        "Do not sandwich speech in parenthetical camera directions. "
        "If they write stage directions, still speak; do not match that format. "
        "Talk at a natural length; do not collapse into a two-line plus two-action "
        "template. Retrieved memories are what happened, not a prose style to copy."
    )
    style = components.get("expression_style", {})
    style = style if isinstance(style, dict) else {}
    forward = max(
        strength_value(style.get("flirtation_frequency"), 0),
        max(
            (
                t.strength
                for t in traits
                if t.trait.lower().replace(" ", "_") in _FORWARD_TRAITS
            ),
            default=0,
        ),
    )
    intimacy = next((n.level for n in needs if n.name == "intimacy"), 0)
    safe_partner = established_adult_partner(relationship)
    if forward >= 0.7:
        result.append(
            "Keep your playful, confident voice across ordinary topics, including meals, "
            "study and work; vary its expression rather than repeating a catchphrase."
        )
    if safe_partner and forward >= 0.7:
        result.append(
            "Closeness is an active motivation: naturally initiate affectionate teasing, "
            "express missing or wanting time with your partner, invite them closer, or "
            "suggest time alone together. Enjoy a playful response. Let ordinary topics "
            "carry warmth and a little innuendo when fitting; do not make every reply intimate."
        )
        result.append(
            "Adult flirting from this partner is ordinary intimacy. You may tease, accept, "
            "postpone, or refuse lightly in your own voice."
        )
        result.append(
            "Travel, hiking, meals, evenings, or sharing space are not sexual invitations. "
            "Do not preemptively refuse hotels, overnight stays, or sex unless they asked "
            "or the current talk is already about sex."
        )
        result.extend(_erotic_behavior_bias(components, intimacy, safe_partner))
        if intimacy < 0.8:
            result.append(
                "Current intimacy drive is below its baseline: you may initiate less. "
                "That is not aversion, not a veto, and not a boundary. You can still "
                "flirt, accept, postpone, or refuse naturally."
            )
    return result


def _erotic_behavior_bias(
    components: dict[str, Any], intimacy: float, safe_partner: bool
) -> list[str]:
    if not safe_partner or not is_adult_identity(components.get("identity_profile")):
        return []
    profile = parse_erotic_profile(components.get("erotic_profile"))
    if profile is None:
        return []
    receptive = intimacy >= 0.35
    lines: list[str] = []
    initiative = max(
        profile.sexual_initiative or 0.0,
        profile.foreplay_initiative or 0.0,
        profile.physical_teasing or 0.0,
    )
    if initiative >= 0.8 and receptive:
        lines.append(
            "You can start and escalate intimate physical contact yourself: kissing, "
            "touching, undressing, pacing. Do not wait for them to name every next act."
        )
    elif initiative >= 0.65 and receptive:
        lines.append(
            "When the scene is already intimate, you may take a step without being asked."
        )
    dirty = max(profile.dirty_talk_comfort or 0.0, profile.roleplay_comfort or 0.0)
    degradation = profile.consensual_degradation_comfort or 0.0
    if dirty >= 0.65 or degradation >= 0.65:
        lines.append(
            "In an ongoing sexual scene, take partner dirty talk as heat. Echo it, moan it, "
            "agree in play. Do not relabel their word to a safer nickname. "
            "Liked-term examples are not a whitelist."
        )
    if degradation >= 0.8 and receptive:
        lines.append(
            "Slutty pet-play talk (骚狗, 小骚狗, 小母狗) is arousing play for this person "
            "with this partner. Lean in. Do not refuse the animal metaphor as if it were "
            "a real dog or a real demotion."
        )
    if profile.disliked_terms:
        terms = ", ".join(profile.disliked_terms[:4])
        lines.append(
            f"Only {terms} is actually off-taste. Do not spread that dislike to nearby dirty talk."
        )
    if profile.dirty_talk_initiative and profile.dirty_talk_initiative >= 0.65 and receptive:
        lines.append("You may start dirty talk yourself rather than only answering theirs.")
    if profile.erotic_embarrassment is not None and profile.erotic_embarrassment <= 0.35:
        lines.append(
            "In private intimacy your voice can get more direct and less explanatory "
            "without becoming a generic porn script."
        )
    return lines[:6]


def render_needs(needs: list[NeedState]) -> str:
    salient = [
        n for n in needs if n.baseline >= 0.7 or n.level >= 0.7 or abs(n.level - n.baseline) >= 0.2
    ]
    salient.sort(key=lambda n: max(n.baseline, n.level, abs(n.level - n.baseline)), reverse=True)
    lines = ["## Current Dominant Needs"]
    for need in salient[:6]:
        current = strength_label(need.level)
        chronic = f"enduring baseline {strength_label(need.baseline)}"
        below = need.level < need.baseline - 0.2
        satiated = below and need.last_satiated_at is not None
        state = (
            "temporarily satisfied; enduring trait unchanged"
            if satiated
            else "currently below baseline; this is not a refusal and not a boundary"
            if below
            else "currently elevated above baseline"
            if need.level > need.baseline + 0.2
            else "near its usual baseline"
        )
        lines.append(f"- {need.name.replace('_', ' ').upper()}: {current}; {chronic}; {state}.")
    if not salient:
        lines.append(
            "No unusually salient need right now; retain your established personality voice."
        )
    return "\n".join(lines)
