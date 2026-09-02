"""NarrativeContinuityAuditor.

Runs before any Canon Commit. Deterministic rule checks always run; an
optional LLM review pass (persona consistency, causality, world rules) can be
injected by the service when an agent runtime is available. Findings carry a
uniform severity: BLOCKING / WARNING / INFO. Any BLOCKING finding blocks the
default commit path (explicit force-commit with override reason is the only
bypass, and it is recorded).
"""

from __future__ import annotations

import re
from typing import Any

from persona_continuum.domain.narrative import (
    AuditFinding,
    AuditSeverity,
    CharacterArc,
    ClueStatus,
    EpisodePlan,
    EpisodeVersion,
    KnowledgeState,
    NarrativeAuditReport,
    NarrativeCharacter,
    NarrativeClue,
    NarrativeProject,
    NarrativeScene,
    PlotThread,
    PlotThreadStatus,
    StoryBible,
    StoryFact,
)

MICRO_HOOK_MIN_SECONDS = 15


class NarrativeContinuityAuditor:
    def audit_episode(
        self,
        project: NarrativeProject,
        bible: StoryBible,
        plan: EpisodePlan,
        version: EpisodeVersion,
        *,
        facts: list[StoryFact],
        knowledge: list[CharacterKnowledgeEntryAlias | Any],
        characters: list[NarrativeCharacter],
        scenes: list[NarrativeScene],
        threads: list[PlotThread],
        clues: list[NarrativeClue],
        arcs: list[CharacterArc],
        previous_versions: list[EpisodeVersion] | None = None,
        audience_state: dict[str, str] | None = None,
        context_fingerprint: str = "",
    ) -> NarrativeAuditReport:
        findings: list[AuditFinding] = []
        draft = version.screenplay
        draft_lower = draft.lower()

        # --- Canon: must_happen / must_not_happen -------------------------
        for item in plan.must_happen:
            if self._fact_missing(item, draft_lower):
                findings.append(
                    AuditFinding(
                        severity=AuditSeverity.WARNING,
                        code="PLAN_MUST_HAPPEN_MISSING",
                        message=f"Planned beat not clearly present in draft: {item}",
                        evidence={"beat": item},
                    )
                )
        for item in plan.must_not_happen:
            if self._fact_missing(item, draft_lower):
                continue
            findings.append(
                AuditFinding(
                    severity=AuditSeverity.BLOCKING,
                    code="PLAN_MUST_NOT_HAPPEN_VIOLATED",
                    message=f"Draft contains forbidden content: {item}",
                    evidence={"forbidden": item},
                )
            )

        # --- Knowledge: characters must not reveal what they cannot know ---
        knowledge_by_character: dict[str, set[str]] = {}
        for entry in knowledge:
            if entry.state == KnowledgeState.KNOWN:
                knowledge_by_character.setdefault(entry.character_id, set()).add(entry.fact_id)

        for scene in scenes:
            for participant in scene.participants:
                for hidden in participant.must_not_reveal:
                    speaker_lines = self._character_lines(scene, participant.character_id)
                    if any(self._mentions(line, hidden) for line in speaker_lines):
                        findings.append(
                            AuditFinding(
                                severity=AuditSeverity.BLOCKING,
                                code="NARRATIVE_KNOWLEDGE_LEAK",
                                message=(
                                    f"{participant.name or participant.character_id} revealed "
                                    f"forbidden information in scene {scene.id}: {hidden}"
                                ),
                                evidence={"scene_id": scene.id, "fact": hidden},
                            )
                        )
            # Scene-level dialogue scan against the knowledge firewall.
            for line in scene.dialogue:
                speaker = str(line.get("speaker", ""))
                text = str(line.get("text", ""))
                known = knowledge_by_character.get(speaker, set())
                for fact in facts:
                    if not fact.secret or fact.id in known:
                        continue
                    if self._mentions(text, fact.text) or f"[fact:{fact.id}]" in text.lower():
                        findings.append(
                            AuditFinding(
                                severity=AuditSeverity.BLOCKING,
                                code="NARRATIVE_KNOWLEDGE_LEAK",
                                message=(
                                    f"Character {speaker} referenced story truth "
                                    f"'{fact.id}' they do not know."
                                ),
                                evidence={"scene_id": scene.id, "fact_id": fact.id},
                            )
                        )

        # --- Audience: secrets leaked ahead of their reveal ----------------
        for fact in facts:
            if not fact.secret:
                continue
            state = (audience_state or {}).get(fact.id, "hidden")
            if state in ("hidden", "teased") and self._mentions(draft, fact.text):
                findings.append(
                    AuditFinding(
                        severity=AuditSeverity.BLOCKING,
                        code="AUDIENCE_SECRET_LEAKED",
                        message=(
                            f"Audience-only secret '{fact.id}' appears openly in the "
                            "draft before its reveal."
                        ),
                        evidence={"fact_id": fact.id, "audience_state": state},
                    )
                )

        # --- Timeline: scene ordering --------------------------------------
        scene_times = [s.time for s in scenes if s.time]
        if scene_times != sorted(scene_times):
            findings.append(
                AuditFinding(
                    severity=AuditSeverity.WARNING,
                    code="TIMELINE_ORDER_SUSPECT",
                    message="Scene times are not in chronological order.",
                    evidence={"times": scene_times},
                )
            )

        # --- Foreshadowing ---------------------------------------------------
        for clue in clues:
            early_reveal = (
                clue.status == ClueStatus.REVEALED
                and clue.actual_reveal_episode is not None
                and clue.planned_reveal_episode is not None
                and clue.actual_reveal_episode < clue.planned_reveal_episode
            )
            if early_reveal:
                    findings.append(
                        AuditFinding(
                            severity=AuditSeverity.BLOCKING,
                            code="CLUE_REVEALED_EARLY",
                            message=(
                                f"Clue '{clue.title}' revealed at EP{clue.actual_reveal_episode} "
                                f"but planned for EP{clue.planned_reveal_episode}."
                            ),
                            evidence={"clue_id": clue.id},
                        )
                    )
            if (
                clue.planned_reveal_episode is not None
                and clue.planned_reveal_episode <= plan.episode_number
                and clue.status
                in (ClueStatus.PLANNED, ClueStatus.PLANTED, ClueStatus.ECHOED)
            ):
                findings.append(
                    AuditFinding(
                        severity=AuditSeverity.WARNING,
                        code="CLUE_REVEAL_OVERDUE",
                        message=(
                            f"Clue '{clue.title}' is due for reveal by EP"
                            f"{clue.planned_reveal_episode} but not yet revealed."
                        ),
                        evidence={"clue_id": clue.id},
                    )
                )

        # --- Plot progress ----------------------------------------------------
        progressed = [
            t
            for t in threads
            if t.status == PlotThreadStatus.ACTIVE
            and any(note for note in t.progress_notes)
        ]
        if threads and not progressed:
            findings.append(
                AuditFinding(
                    severity=AuditSeverity.WARNING,
                    code="PLOT_THREAD_STALLED",
                    message="No active plot thread progressed in this episode.",
                )
            )

        # --- Character arcs -----------------------------------------------------
        for arc in arcs:
            if arc.current_progress <= 0.0 and plan.episode_number > 1:
                findings.append(
                    AuditFinding(
                        severity=AuditSeverity.INFO,
                        code="CHARACTER_ARC_STATIC",
                        message=(
                            f"Character {arc.character_id} arc shows no progress "
                            f"through EP{plan.episode_number}."
                        ),
                    )
                )

        # --- Repetition vs previous episodes -----------------------------------
        if previous_versions:
            current_beats = [b.title for b in version.beat_sheet if b.title]
            overlap: float = 0.0
            for prev in previous_versions[:3]:
                prev_titles = {b.title for b in prev.beat_sheet if b.title}
                if not prev_titles:
                    continue
                overlap = max(
                    overlap,
                    len(set(current_beats) & prev_titles) / max(len(prev_titles), 1),
                )
            if overlap > 0.6:
                findings.append(
                    AuditFinding(
                        severity=AuditSeverity.WARNING,
                        code="REPETITIVE_STRUCTURE",
                        message=(
                            "Episode beat structure heavily repeats a previous "
                            f"episode ({overlap:.0%} overlap)."
                        ),
                    )
                )

        # --- Micro drama pacing --------------------------------------------------
        if project.format.value == "micro_drama":
            if not plan.hook.strip():
                findings.append(
                    AuditFinding(
                        severity=AuditSeverity.WARNING,
                        code="MICRO_DRAMA_HOOK_MISSING",
                        message="Micro drama episode lacks an opening hook.",
                    )
                )
            if not plan.cliffhanger.strip():
                findings.append(
                    AuditFinding(
                        severity=AuditSeverity.WARNING,
                        code="MICRO_DRAMA_CLIFFHANGER_MISSING",
                        message="Micro drama episode lacks an ending cliffhanger.",
                    )
                )

        # --- Draft completeness ---------------------------------------------------
        if len(draft.strip()) < 40:
            findings.append(
                AuditFinding(
                    severity=AuditSeverity.BLOCKING,
                    code="DRAFT_TOO_SHORT",
                    message="Episode draft is effectively empty.",
                )
            )

        blocking = sum(1 for f in findings if f.severity == AuditSeverity.BLOCKING)
        warning = sum(1 for f in findings if f.severity == AuditSeverity.WARNING)
        info = sum(1 for f in findings if f.severity == AuditSeverity.INFO)
        return NarrativeAuditReport(
            project_id=project.id,
            episode_number=plan.episode_number,
            episode_version_id=version.id,
            findings=findings,
            blocking_count=blocking,
            warning_count=warning,
            info_count=info,
            passed=blocking == 0,
            context_fingerprint=context_fingerprint,
            story_bible_version=bible.version,
            project_revision=project.revision,
        )

    # ------------------------------------------------------------------
    def _fact_missing(self, needle: str, haystack_lower: str) -> bool:
        probe = needle.strip().lower()
        if len(probe) < 4:
            return False
        tokens = [t for t in re.split(r"\s+", probe) if len(t) > 2]
        if not tokens:
            return False
        hits = sum(1 for t in tokens if t in haystack_lower)
        return hits < max(1, len(tokens) // 2)

    def _mentions(self, text: str, fact_text: str) -> bool:
        probe = fact_text.strip().lower()
        if len(probe) < 8:
            return False
        return probe in text.lower()

    def _character_lines(self, scene: NarrativeScene, character_id: str) -> list[str]:
        return [
            str(line.get("text", ""))
            for line in scene.dialogue
            if str(line.get("speaker", "")) == character_id
        ]


# Alias kept for the type hint above without a circular import concern.
CharacterKnowledgeEntryAlias = Any
