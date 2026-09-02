from __future__ import annotations

from persona_continuum.domain.narrative import (
    EpisodePlan,
    EpisodeSummary,
    NarrativeCharacter,
    NarrativeProject,
    NarrativeScene,
    StoryBible,
)
from persona_continuum.narrative.context_builder import NarrativeContextBuilder


def _fixtures():
    project = NarrativeProject(id="p1", title="t", planned_episode_count=60)
    bible = StoryBible(
        project_id="p1",
        premise="前提",
        final_truth=["真相只有作者知道"],
        author_notes="作者备注",
    )
    plan = EpisodePlan(project_id="p1", episode_number=30, required_characters=["c1"])
    characters = [
        NarrativeCharacter(id="c1", project_id="p1", name="方宁"),
        NarrativeCharacter(id="c2", project_id="p1", name="陈默"),
    ]
    return project, bible, plan, characters


def test_writer_context_is_task_scoped() -> None:
    builder = NarrativeContextBuilder()
    project, bible, plan, characters = _fixtures()
    summaries = [
        EpisodeSummary(project_id="p1", episode_number=n, summary=f"EP{n}")
        for n in range(1, 60)
    ]
    context = builder.build_writer_context(
        project, bible, plan, ["canon-1"], characters, [], [], [], [], [], summaries
    )
    # Only the last 5 summaries included, never all 59.
    assert len(context["recent_episode_summaries"]) == 5
    assert context["recent_episode_summaries"][-1]["episode"] == 59
    # Only required characters selected.
    assert [c["id"] for c in context["characters"]] == ["c1"]
    # Story truth exposed only as reveal targets, not as known facts.
    assert "reveal_targets_this_episode" in context["story_bible"]
    assert context["story_bible"].get("final_truth") is None


def test_scene_context_limits_to_own_data() -> None:
    builder = NarrativeContextBuilder()
    character = NarrativeCharacter(id="c1", project_id="p1", name="方宁", persona_id="p1")
    scene = NarrativeScene(project_id="p1", location="办公室")
    context = builder.build_scene_context(
        character, scene, ["block"], world_state_digest={"k": "v"}, memories=["m1", "m2"]
    )
    assert context["character_id"] == "c1"
    assert context["own_memories"] == ["m1", "m2"]
    assert context["prompt_blocks"] == ["block"]


def test_auditor_context_contains_full_story_truth() -> None:
    builder = NarrativeContextBuilder()
    project, bible, plan, characters = _fixtures()
    context = builder.build_auditor_context(
        project, bible, plan, "草稿", ["canon"], {}, {}, [], [], []
    )
    assert context["task"] == "continuity_auditor"
    assert "story_bible_full" in context  # god's-eye view is allowed
