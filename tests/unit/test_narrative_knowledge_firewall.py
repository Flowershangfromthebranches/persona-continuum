from __future__ import annotations

from persona_continuum.domain.narrative import (
    AudienceKnowledgeEntry,
    CharacterKnowledgeEntry,
    KnowledgeState,
    NarrativeCharacter,
    NarrativeScene,
    StoryBible,
    StoryFact,
)
from persona_continuum.narrative.knowledge_firewall import NarrativeKnowledgeFirewall

FACT = StoryFact(
    id="fact_email_source",
    project_id="p1",
    text="邮件实际上来自ORACLE模拟中的数字方宁",
)


def _character() -> NarrativeCharacter:
    return NarrativeCharacter(id="char_fang", project_id="p1", name="方宁", persona_id="p_fang")


def test_unknown_fact_is_blocked_from_character_view() -> None:
    fw = NarrativeKnowledgeFirewall()
    view = fw.build_character_view(_character(), [FACT], [])
    assert view.known_facts == []
    assert "fact_email_source" in view.blocked_fact_ids


def test_known_fact_is_visible() -> None:
    fw = NarrativeKnowledgeFirewall()
    entry = CharacterKnowledgeEntry(
        project_id="p1",
        character_id="char_fang",
        fact_id="fact_email_source",
        fact_text=FACT.text,
        state=KnowledgeState.KNOWN,
        learned_episode=31,
    )
    view = fw.build_character_view(_character(), [FACT], [entry], episode_number=31)
    assert view.known_facts == [FACT.text]


def test_future_learning_is_invisible_at_earlier_episode() -> None:
    fw = NarrativeKnowledgeFirewall()
    entry = CharacterKnowledgeEntry(
        project_id="p1",
        character_id="char_fang",
        fact_id="fact_email_source",
        fact_text=FACT.text,
        state=KnowledgeState.KNOWN,
        learned_episode=31,
    )
    view = fw.build_character_view(_character(), [FACT], [entry], episode_number=12)
    assert "fact_email_source" in view.blocked_fact_ids


def test_prompt_block_excludes_story_truth() -> None:
    fw = NarrativeKnowledgeFirewall()
    bible = StoryBible(
        project_id="p1",
        final_truth=["邮件来自数字方宁"],
        author_notes="作者备注：结尾反转",
    )
    context = fw.build_character_context(
        _character(), bible, [FACT], [], [], episode_number=12
    )
    blob = "\n".join(context["prompt_blocks"])
    assert "邮件来自数字方宁" not in blob
    assert "作者备注" not in blob
    assert "ORACLE" not in blob


def test_leak_scan_detects_secret_in_output() -> None:
    fw = NarrativeKnowledgeFirewall()
    leaks = fw.scan_for_leaks(
        "她说：其实邮件实际上来自ORACLE模拟中的数字方宁，我一直都知道。",
        [FACT],
        [],
        "char_fang",
    )
    assert leaks == ["fact_email_source"]


def test_leak_scan_clean_when_unknown() -> None:
    fw = NarrativeKnowledgeFirewall()
    leaks = fw.scan_for_leaks("我只是想知道公司到底发生了什么。", [FACT], [], "char_fang")
    assert leaks == []


def test_scene_must_not_reveal_enters_prompt() -> None:
    fw = NarrativeKnowledgeFirewall()
    scene = NarrativeScene(
        project_id="p1",
        location="地下停车场",
        participants=[
            {
                "character_id": "char_fang",
                "goal": "得到真相",
                "must_not_reveal": [],
            }
        ],
    )
    context = fw.build_character_context(
        _character(), StoryBible(project_id="p1"), [FACT], [], [], scene
    )
    blob = "\n".join(context["prompt_blocks"])
    assert "地下停车场" in blob
    assert "得到真相" in blob


def test_audience_view_hidden_by_default() -> None:
    fw = NarrativeKnowledgeFirewall()
    audience = [
        AudienceKnowledgeEntry(
            project_id="p1", fact_id="fact_email_source", state="partial"
        )
    ]
    view = fw.audience_view([FACT], audience)
    assert view["fact_email_source"] == "partial"
    view_hidden = fw.audience_view([FACT], [])
    assert view_hidden["fact_email_source"] == "hidden"
