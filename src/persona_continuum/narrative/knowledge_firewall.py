"""Narrative Knowledge Firewall.

Core principle: a Persona actor may only ever see information it is allowed
to know under the current Canon / Branch / Episode / Scene. Character prompts
must never contain:

  * StoryBible.final_truth (Story Truth) unless already in that character's
    CharacterKnowledge state
  * other characters' secrets / future reveals
  * Audience-only information
  * author notes
  * future episode plans

The firewall is enforced on prompt assembly (build_character_context) and
verified on outputs (scan_for_leaks), and is covered by dedicated tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from persona_continuum.domain.narrative import (
    AudienceKnowledgeEntry,
    CharacterKnowledgeEntry,
    KnowledgeState,
    NarrativeCharacter,
    NarrativeScene,
    StoryBible,
    StoryFact,
)


@dataclass
class CharacterView:
    """The world as one character is allowed to see it."""

    character_id: str
    known_facts: list[str] = field(default_factory=list)
    suspected_facts: list[str] = field(default_factory=list)
    misbeliefs: list[str] = field(default_factory=list)
    blocked_fact_ids: list[str] = field(default_factory=list)
    instructions: list[str] = field(default_factory=list)

    def to_prompt_block(self) -> str:
        lines = ["### CHARACTER KNOWLEDGE (you know ONLY the following)"]
        if self.known_facts:
            for fact in self.known_facts:
                lines.append(f"- KNOWN: {fact}")
        for fact in self.suspected_facts:
            lines.append(f"- SUSPECTED (not confirmed, do not treat as fact): {fact}")
        for fact in self.misbeliefs:
            lines.append(f"- YOU BELIEVE (possibly wrong): {fact}")
        if not self.known_facts and not self.suspected_facts and not self.misbeliefs:
            lines.append("- (no special story knowledge)")
        lines.append(
            "- You must NOT reference, hint at, or reason about any story fact "
            "not listed above. Author notes and story truth are invisible to you."
        )
        return "\n".join(lines)


class NarrativeKnowledgeFirewall:
    """Filters story truth through each character's knowledge state."""

    def build_character_view(
        self,
        character: NarrativeCharacter,
        facts: list[StoryFact],
        knowledge: list[CharacterKnowledgeEntry],
        *,
        episode_number: int | None = None,
    ) -> CharacterView:
        view = CharacterView(character_id=character.id)
        # CRITICAL: only this character's own knowledge entries may be read.
        # Another character's KNOWN entry must never leak into this view.
        own_knowledge = [k for k in knowledge if k.character_id == character.id]
        knowledge_by_fact = {k.fact_id: k for k in own_knowledge}
        for fact in facts:
            entry = knowledge_by_fact.get(fact.id)
            if entry is None or entry.state == KnowledgeState.UNKNOWN:
                view.blocked_fact_ids.append(fact.id)
                continue
            if (
                episode_number is not None
                and entry.learned_episode is not None
                and entry.learned_episode > episode_number
            ):
                view.blocked_fact_ids.append(fact.id)
                continue
            if entry.state == KnowledgeState.KNOWN:
                view.known_facts.append(entry.fact_text or fact.text)
            elif entry.state == KnowledgeState.SUSPECTED:
                view.suspected_facts.append(entry.fact_text or fact.text)
            elif entry.state == KnowledgeState.MISBELIEVED:
                view.misbeliefs.append(entry.fact_text or fact.text)
        return view

    def build_character_context(
        self,
        character: NarrativeCharacter,
        bible: StoryBible,
        facts: list[StoryFact],
        knowledge: list[CharacterKnowledgeEntry],
        audience: list[AudienceKnowledgeEntry],
        scene: NarrativeScene | None = None,
        *,
        episode_number: int | None = None,
    ) -> dict[str, Any]:
        """Assemble the character-safe prompt context.

        Returns a dict whose ``prompt_blocks`` are safe to inject into a Persona
        actor prompt. Deliberately excludes: bible.final_truth, bible.author_notes,
        other characters' knowledge entries, audience-only state, future plans.
        """
        view = self.build_character_view(
            character, facts, knowledge, episode_number=episode_number
        )
        blocked_texts = [
            fact.text
            for fact in facts
            if fact.id in set(view.blocked_fact_ids) and fact.secret
        ] + list(bible.final_truth)

        def safe(value: str) -> str:
            sanitized = value
            for blocked in blocked_texts:
                probe = blocked.strip()
                if probe:
                    sanitized = sanitized.replace(probe, "[REDACTED STORY TRUTH]")
            if bible.author_notes:
                sanitized = sanitized.replace(
                    bible.author_notes.strip(), "[REDACTED AUTHOR NOTES]"
                )
            return sanitized

        participant = None
        if scene is not None:
            participant = next(
                (p for p in scene.participants if p.character_id == character.id), None
            )

        scene_block: list[str] = []
        if scene is not None:
            scene_block.append(f"### SCENE: {safe(scene.location) or 'unspecified location'}")
            if scene.time:
                scene_block.append(f"Time: {safe(scene.time)}")
            if scene.scene_goal:
                scene_block.append(f"Scene goal: {safe(scene.scene_goal)}")
            if participant is not None and participant.goal:
                scene_block.append(f"Your goal: {safe(participant.goal)}")
            if participant is not None and participant.must_not_reveal:
                scene_block.append(
                    "You MUST NOT reveal: "
                    + "; ".join(safe(item) for item in participant.must_not_reveal)
                )
            if scene.knowledge_constraints:
                scene_block.append(
                    "Knowledge constraints: "
                    + "; ".join(safe(item) for item in scene.knowledge_constraints)
                )

        # Self-owned backstory and dialogue style only — never the bible's
        # author-only fields.
        personal_block = [
            f"### CHARACTER: {character.name}",
            safe(character.description or ""),
        ]
        if character.backstory:
            personal_block.append(f"Backstory: {safe(character.backstory)}")
        if character.dialogue_samples:
            personal_block.append(
                "Speech style samples: " + " | ".join(character.dialogue_samples[:3])
            )

        return {
            "character_id": character.id,
            "persona_id": character.persona_id,
            "prompt_blocks": [
                "\n".join(personal_block),
                view.to_prompt_block(),
                "\n".join(scene_block),
            ],
            "allowed_fact_ids": [
                fact.id
                for fact in facts
                if fact.id not in view.blocked_fact_ids
            ],
            "blocked_fact_ids": list(view.blocked_fact_ids),
        }

    def scan_for_leaks(
        self,
        text: str,
        facts: list[StoryFact],
        knowledge: list[CharacterKnowledgeEntry],
        character_id: str,
        *,
        bible: StoryBible | None = None,
    ) -> list[str]:
        """Detect story-truth leaks in a character's output/prompt text.

        A leak is a secret fact the character does not KNOW appearing in the
        text (by explicit fact marker ``[fact:<id>]`` or by distinctive fact
        text match).
        """
        leaks: list[str] = []
        known_ids = {
            k.fact_id
            for k in knowledge
            if k.character_id == character_id and k.state == KnowledgeState.KNOWN
        }
        known_texts = {
            (entry.fact_text or fact.text).strip().lower()
            for entry in knowledge
            for fact in facts
            if entry.character_id == character_id
            and entry.fact_id == fact.id
            and entry.state == KnowledgeState.KNOWN
        }
        lowered = text.lower()
        for fact in facts:
            if not fact.secret or fact.id in known_ids:
                continue
            marker = f"[fact:{fact.id}]"
            if marker.lower() in lowered:
                leaks.append(fact.id)
                continue
            probe = fact.text.strip().lower()
            if len(probe) >= 12 and probe in lowered:
                leaks.append(fact.id)
        if bible is not None:
            for truth in bible.final_truth:
                probe = truth.strip().lower()
                if len(probe) >= 12 and probe in lowered and probe not in known_texts:
                    leaks.append(f"bible_final_truth::{truth[:40]}")
            if bible.author_notes and bible.author_notes.strip().lower() in lowered:
                leaks.append("bible_author_notes")
        return leaks

    def audience_view(
        self,
        facts: list[StoryFact],
        audience: list[AudienceKnowledgeEntry],
        *,
        episode_number: int | None = None,
    ) -> dict[str, str]:
        """Map fact_id -> audience state for the information-gap matrix."""
        audience_by_fact = {a.fact_id: a for a in audience}
        result: dict[str, str] = {}
        for fact in facts:
            entry = audience_by_fact.get(fact.id)
            if entry is None:
                result[fact.id] = "hidden"
                continue
            if (
                episode_number is not None
                and entry.revealed_episode is not None
                and entry.revealed_episode > episode_number
            ):
                result[fact.id] = "hidden"
                continue
            result[fact.id] = entry.state.value
        return result


def _compact_scene_lines(scene: NarrativeScene) -> str:
    """A compact, character-safe scene description line."""
    parts = [f"Scene at {scene.location or 'unknown location'}"]
    if scene.time:
        parts.append(f"time {scene.time}")
    names = [p.name or p.character_id for p in scene.participants]
    if names:
        parts.append("present: " + ", ".join(names))
    return "### SITUATION\n" + ". ".join(parts) + "."
