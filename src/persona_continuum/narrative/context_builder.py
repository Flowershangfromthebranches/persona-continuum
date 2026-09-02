"""NarrativeContextBuilder — task-scoped context assembly.

Forbidden: stuffing the whole novel / all 60 episodes / every persona into a
prompt. The builder selects only the sections a given task needs and reports
what it included so callers (and tests) can verify budget compliance.
"""

from __future__ import annotations

from typing import Any

from persona_continuum.domain.narrative import (
    AudienceKnowledgeEntry,
    CharacterArc,
    CharacterKnowledgeEntry,
    EpisodePlan,
    EpisodeSummary,
    NarrativeAuditReport,
    NarrativeCharacter,
    NarrativeClue,
    NarrativeProject,
    NarrativeScene,
    PlotThread,
    StoryBible,
)


class NarrativeContextBuilder:
    """Builds minimal, role-appropriate contexts for narrative AI tasks."""

    MAX_RECENT_EPISODE_SUMMARIES = 5
    MAX_CHARACTERS_IN_WRITER_CONTEXT = 12

    def build_writer_context(
        self,
        project: NarrativeProject,
        bible: StoryBible,
        plan: EpisodePlan,
        canon_snapshot: list[str],
        characters: list[NarrativeCharacter],
        knowledge: list[CharacterKnowledgeEntry],
        audience: list[AudienceKnowledgeEntry],
        threads: list[PlotThread],
        clues: list[NarrativeClue],
        arcs: list[CharacterArc],
        summaries: list[EpisodeSummary],
        future_plans: list[EpisodePlan] | None = None,
    ) -> dict[str, Any]:
        """Context for the Episode Writer: bible essentials + plan + recent canon.

        Never includes: full transcripts of past episodes, author notes,
        or Story Truth the plan does not explicitly target.
        """
        recent = sorted(summaries, key=lambda s: s.episode_number)[
            -self.MAX_RECENT_EPISODE_SUMMARIES :
        ]
        active_threads = [t for t in threads if t.status in ("active", "planned", "paused")]
        relevant_clues = [
            c
            for c in clues
            if c.status not in ("abandoned",)
            and (
                c.planned_reveal_episode is None
                or c.planned_reveal_episode <= plan.episode_number + 2
                or c.introduced_episode is None
                or c.introduced_episode <= plan.episode_number
            )
        ]
        selected_characters = [
            c for c in characters if c.id in set(plan.required_characters)
        ] or characters[: self.MAX_CHARACTERS_IN_WRITER_CONTEXT]

        bible_block = {
            "premise": bible.premise,
            "core_question": bible.core_question,
            "theme": bible.theme,
            "world_rules": bible.world_rules,
            "narrative_constraints": bible.narrative_constraints,
            "forbidden_shortcuts": bible.forbidden_shortcuts,
            # Story Truth (final_truth) is included ONLY as reveal targets the
            # writer must dramatize, never as facts characters can know.
            "reveal_targets_this_episode": plan.reveal_targets,
            "forbidden_reveals": plan.forbidden_reveals,
        }
        return {
            "task": "episode_writer",
            "project": {
                "title": project.title,
                "format": project.format.value,
                "genre": project.genre,
                "tone": project.tone,
                "episode_duration_seconds": [
                    project.episode_duration_seconds_min,
                    project.episode_duration_seconds_max,
                ],
            },
            "story_bible": bible_block,
            "episode_plan": plan.model_dump(mode="json", exclude={"id", "project_id"}),
            "canon_snapshot": canon_snapshot,
            "characters": [
                {
                    "id": c.id,
                    "name": c.name,
                    "role": c.role,
                    "description": c.description,
                }
                for c in selected_characters
            ],
            "audience_state": {a.fact_id: a.state.value for a in audience},
            "active_plot_threads": [t.title for t in active_threads],
            "relevant_clues": [
                {
                    "id": c.id,
                    "title": c.title,
                    "status": c.status.value,
                    "planned_reveal_episode": c.planned_reveal_episode,
                }
                for c in relevant_clues
            ],
            "character_arcs": [
                {
                    "character_id": a.character_id,
                    "current_phase": a.current_phase,
                    "current_progress": a.current_progress,
                    "target_state": a.target_state,
                }
                for a in arcs
            ],
            "recent_episode_summaries": [
                {"episode": s.episode_number, "summary": s.summary} for s in recent
            ],
            "future_plan_hints": [
                {"episode": p.episode_number, "title": p.title, "goal": p.narrative_goal}
                for p in (future_plans or [])[:3]
            ],
        }

    def build_scene_context(
        self,
        character: NarrativeCharacter,
        scene: NarrativeScene,
        character_view_prompt_blocks: list[str],
        world_state_digest: dict[str, Any] | None = None,
        memories: list[str] | None = None,
        relationship_digest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Context for one Persona actor inside a simulated scene.

        Only the character's own persona, the scene, the world state digest,
        their own knowledge, memories, and relationships.
        """
        return {
            "task": "character_scene",
            "character_id": character.id,
            "persona_id": character.persona_id,
            "prompt_blocks": character_view_prompt_blocks,
            "world_state_digest": world_state_digest or {},
            "own_memories": (memories or [])[-8:],
            "own_relationships": relationship_digest or {},
        }

    def build_auditor_context(
        self,
        project: NarrativeProject,
        bible: StoryBible,
        plan: EpisodePlan,
        draft: str,
        canon_snapshot: list[str],
        knowledge_matrix: dict[str, Any],
        audience_state: dict[str, str],
        threads: list[PlotThread],
        clues: list[NarrativeClue],
        arcs: list[CharacterArc],
        recent_audits: list[NarrativeAuditReport] | None = None,
    ) -> dict[str, Any]:
        """Auditor context: god's-eye view — full story truth is allowed here."""
        return {
            "task": "continuity_auditor",
            "project": {"title": project.title, "format": project.format.value},
            "story_bible_full": bible.model_dump(mode="json"),
            "episode_plan": plan.model_dump(mode="json", exclude={"id", "project_id"}),
            "episode_draft": draft,
            "canon_snapshot": canon_snapshot,
            "knowledge_matrix": knowledge_matrix,
            "audience_state": audience_state,
            "plot_threads": [t.model_dump(mode="json") for t in threads],
            "clues": [c.model_dump(mode="json") for c in clues],
            "character_arcs": [a.model_dump(mode="json") for a in arcs],
            "recent_audit_findings": [
                f.code for report in (recent_audits or [])[:3] for f in report.findings
            ],
        }
