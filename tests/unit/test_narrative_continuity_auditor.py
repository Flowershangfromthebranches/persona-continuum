from __future__ import annotations

from persona_continuum.domain.narrative import (
    EpisodePlan,
    EpisodeVersion,
    KnowledgeState,
    NarrativeProject,
    NarrativeScene,
    StoryBible,
    StoryFact,
)
from persona_continuum.narrative.continuity_auditor import NarrativeContinuityAuditor

PROJECT = NarrativeProject(id="p1", title="t", format="micro_drama")


def _audit(version: EpisodeVersion, scenes=None, facts=None, knowledge=None, plan=None):
    auditor = NarrativeContinuityAuditor()
    return auditor.audit_episode(
        PROJECT,
        StoryBible(project_id="p1"),
        plan or EpisodePlan(project_id="p1", episode_number=12),
        version,
        facts=facts or [],
        knowledge=knowledge or [],
        characters=[],
        scenes=scenes or [],
        threads=[],
        clues=[],
        arcs=[],
    )


def test_draft_too_short_is_blocking() -> None:
    version = EpisodeVersion(project_id="p1", episode_number=1, screenplay="太短")
    report = _audit(version)
    assert report.passed is False
    assert any(f.code == "DRAFT_TOO_SHORT" for f in report.findings)


def test_must_not_happen_violation_blocks() -> None:
    plan = EpisodePlan(
        project_id="p1",
        episode_number=12,
        must_not_happen=["方宁当众崩溃大哭摔门而去"],
    )
    version = EpisodeVersion(
        project_id="p1",
        episode_number=12,
        screenplay="EP12\n方宁当众崩溃大哭摔门而去，走廊一片寂静，无人上前。\n" * 3,
    )
    report = _audit(version, plan=plan)
    codes = [f.code for f in report.findings]
    assert "PLAN_MUST_NOT_HAPPEN_VIOLATED" in codes


def test_character_revealing_unknown_secret_is_blocking() -> None:
    fact = StoryFact(id="fact_1", project_id="p1", text="邮件来自数字方宁模拟体")
    scene = NarrativeScene(
        project_id="p1",
        episode_number=12,
        dialogue=[
            {"speaker": "fang", "text": "我现在确定邮件来自数字方宁模拟体，证据确凿。"}
        ],
    )
    version = EpisodeVersion(
        project_id="p1", episode_number=12, screenplay="EP12 完整草稿" * 20
    )
    report = _audit(version, scenes=[scene], facts=[fact])
    assert any(
        f.code == "NARRATIVE_KNOWLEDGE_LEAK" and f.severity.value == "blocking"
        for f in report.findings
    )
    assert report.passed is False


def test_character_who_knows_fact_is_clean() -> None:
    from persona_continuum.domain.narrative import CharacterKnowledgeEntry

    fact = StoryFact(id="fact_1", project_id="p1", text="邮件来自数字方宁模拟体")
    knowledge = [
        CharacterKnowledgeEntry(
            project_id="p1",
            character_id="fang",
            fact_id="fact_1",
            state=KnowledgeState.KNOWN,
        )
    ]
    scene = NarrativeScene(
        project_id="p1",
        episode_number=12,
        dialogue=[{"speaker": "fang", "text": "邮件来自数字方宁模拟体，我早就确认了。"}],
    )
    version = EpisodeVersion(
        project_id="p1", episode_number=12, screenplay="EP12 完整草稿" * 20
    )
    report = _audit(version, scenes=[scene], facts=[fact], knowledge=knowledge)
    assert not any(f.code == "NARRATIVE_KNOWLEDGE_LEAK" for f in report.findings)


def test_micro_drama_missing_hook_warns() -> None:
    version = EpisodeVersion(project_id="p1", episode_number=1, screenplay="完整草稿内容" * 20)
    report = _audit(version)
    codes = [f.code for f in report.findings]
    assert "MICRO_DRAMA_HOOK_MISSING" in codes
    assert report.blocking_count == 0
