from __future__ import annotations

from persona_continuum.domain.narrative import ClueStatus, NarrativeClue
from persona_continuum.narrative.continuity_auditor import NarrativeContinuityAuditor


def test_clue_lifecycle_statuses() -> None:
    clue = NarrativeClue(project_id="p1", title="桌上的旧照片", planned_reveal_episode=8)
    assert clue.status == ClueStatus.PLANNED
    clue.status = ClueStatus.PLANTED
    clue.status = ClueStatus.REVEALED
    clue.actual_reveal_episode = 8
    assert clue.actual_reveal_episode == clue.planned_reveal_episode


def test_unpaid_clue_flags_warning() -> None:
    auditor = NarrativeContinuityAuditor()
    from persona_continuum.domain.narrative import (
        EpisodePlan,
        EpisodeVersion,
        NarrativeProject,
        StoryBible,
    )

    project = NarrativeProject(id="p1", title="t")
    bible = StoryBible(project_id="p1")
    plan = EpisodePlan(project_id="p1", episode_number=8)
    version = EpisodeVersion(project_id="p1", episode_number=8, screenplay="样稿" * 30)
    clue = NarrativeClue(
        project_id="p1", title="旧照片", planned_reveal_episode=8, status=ClueStatus.PLANTED
    )
    report = auditor.audit_episode(
        project, bible, plan, version, facts=[], knowledge=[], characters=[],
        scenes=[], threads=[], clues=[clue], arcs=[],
    )
    codes = [f.code for f in report.findings]
    assert "CLUE_REVEAL_OVERDUE" in codes


def test_early_revealed_clue_is_blocking() -> None:
    auditor = NarrativeContinuityAuditor()
    from persona_continuum.domain.narrative import (
        EpisodePlan,
        EpisodeVersion,
        NarrativeProject,
        StoryBible,
    )

    project = NarrativeProject(id="p1", title="t")
    bible = StoryBible(project_id="p1")
    plan = EpisodePlan(project_id="p1", episode_number=3)
    version = EpisodeVersion(project_id="p1", episode_number=3, screenplay="样稿" * 30)
    clue = NarrativeClue(
        project_id="p1",
        title="旧照片",
        planned_reveal_episode=30,
        actual_reveal_episode=3,
        status=ClueStatus.REVEALED,
    )
    report = auditor.audit_episode(
        project, bible, plan, version, facts=[], knowledge=[], characters=[],
        scenes=[], threads=[], clues=[clue], arcs=[],
    )
    assert any(
        f.code == "CLUE_REVEALED_EARLY" and f.severity.value == "blocking"
        for f in report.findings
    )
    assert report.passed is False
