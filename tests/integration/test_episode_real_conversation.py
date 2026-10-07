"""One synthetic 18-turn conversation: does the ledger match what happened?

The conversation is scripted (topic A -> a real silence -> topic B) so the
expected organisation is known in advance, and a deterministic summariser keeps
the test about ORGANISATION rather than about model quality.

The semantic judgement -- "does this Episode actually correspond to a stretch
of things that happened" -- needs a real model and a human reader.  Run
``scripts/phase3_su_he_conversation.py`` for that; this file pins the
structure it must produce.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.domain.episode import EpisodeStatus

PERSONA = "su_he"
START = datetime(2026, 9, 19, 14, 0, tzinfo=UTC)

TOPIC_A = "考研"
TOPIC_B = "小陈"

CONVERSATION: list[tuple[str, str, str]] = [
    # (topic, user_message, persona_reply)
    (
        "A",
        "我最近在准备考研，想考重庆理工的人工智能专硕",
        "重庆理工的 AI 方向挺好的，你是看中它的导师还是地理位置？",
    ),
    (
        "A",
        "主要是离家近。但我数学太差了，一想到数学就焦虑",
        "数学焦虑很正常，先把焦虑和分数分开看：你现在模考大概什么水平？",
    ),
    (
        "A",
        "上次模考才 60 分，我觉得自己肯定考不上",
        "60 分是起点不是结论。你哪几个模块丢分最多？",
    ),
    (
        "A",
        "高数下和概率论几乎全错",
        "那就先补这两块。要不要我帮你排一个两周的补漏计划？",
    ),
    (
        "A",
        "好，但是我怕坚持不下来",
        "坚持不下来通常不是意志问题，是计划太大。我们把每天的量压到两小时。",
    ),
    (
        "A",
        "那具体怎么安排？",
        "早上四十分钟高数下，晚上四十分钟概率论，剩下时间只做复盘，不做新题。",
    ),
    (
        "A",
        "这样听起来可行多了",
        "先跑一周。一周后我们再看哪里需要调。",
    ),
    (
        "A",
        "谢谢你，我感觉没那么慌了",
        "慌是因为没有路径，现在有路径了。",
    ),
    (
        "A",
        "那我明天就开始",
        "好，明天开始，我们一周后对一次进度。",
    ),
    (
        "B",
        "刚才小陈跟我吵架了",
        "因为什么？你们平时很少吵。",
    ),
    (
        "B",
        "她说我总是敷衍她，回消息很慢",
        "她说的这几件事，你自己怎么看？",
    ),
    (
        "B",
        "我承认我最近确实回得慢，但我真的是在复习",
        "那她知道你在考研这件事吗？",
    ),
    (
        "B",
        "知道，但她觉得我把她排在后面",
        "这两件事不一定冲突，只是你没告诉她你的时间表。",
    ),
    (
        "B",
        "那我该怎么跟她说？",
        "先把你的复习安排摊开给她看，再问她需要你哪几个时间段。",
    ),
    (
        "B",
        "听你这么说，好像是我没沟通清楚",
        "你不是不在乎她，你是没把在乎说出来。",
    ),
    (
        "B",
        "我现在就去找她聊",
        "去吧。聊完回来告诉我怎么样。",
    ),
    (
        "B",
        "我们和好了，她说她也太急了",
        "那就好。你们这次其实是把话说开了。",
    ),
    (
        "B",
        "嗯，谢谢你",
        "不用谢，记得明天开始复习。",
    ),
]

def _summariser() -> Any:
    async def summarize(payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        text = " ".join(str(turn.get("text") or "") for turn in payload.get("turns") or [])
        if TOPIC_B in text:
            return {
                "title": "与小陈的争吵与和好",
                "summary": "用户与小陈因回消息慢发生争吵，讨论后用户主动沟通，两人和好。",
                "important_events": ["小陈指责用户敷衍", "用户主动找小陈沟通", "两人和好"],
                "commitments": ["用户把复习安排告诉小陈"],
                "unresolved": [],
                "emotional_arc": ["委屈", "被点醒", "和好"],
                "topics": ["关系", "沟通"],
                "entities": ["小陈"],
                "user_stated": ["她总是说我敷衍她"],
                "persona_stated": ["你不是不在乎她，你是没把在乎说出来"],
                "inferred_context": [],
                "importance": 0.8,
                "confidence": 0.75,
            }
        return {
            "title": "讨论考研与重庆理工",
            "summary": "用户准备考研重庆理工人工智能专硕，担心数学，与苏禾一起制定了复习计划。",
            "important_events": ["用户表达数学焦虑", "苏禾帮助制定复习计划"],
            "commitments": ["用户计划明天开始复习", "一周后回顾进度"],
            "unresolved": ["复习计划尚未经过验证"],
            "emotional_arc": ["焦虑", "被理解", "形成计划"],
            "topics": ["考研", "数学"],
            "entities": ["重庆理工大学"],
            "user_stated": ["数学太差了", "模考 60 分"],
            "persona_stated": ["60 分是起点不是结论"],
            "inferred_context": [],
            "importance": 0.75,
            "confidence": 0.8,
        }

    return summarize


@pytest.fixture()
def app(tmp_path) -> Any:
    config = Config(data_dir=tmp_path / "pc-conversation")
    continuum = PersonaContinuum(config, include_fake_agent=True)
    continuum.init()
    continuum.personas.create_from_manifest(
        {
            "id": PERSONA,
            "display_name": "苏禾",
            "persona_type": "fictional",
            "run_mode": "continuation",
        }
    )
    yield continuum
    continuum.close()


def _run_conversation(app: PersonaContinuum) -> tuple[str, list[str]]:
    session_id = app.sessions.start_session(
        persona_id=PERSONA, title="考研与生活", counterpart_id="user"
    ).id
    turn_ids: list[str] = []
    elapsed = timedelta(0)
    seen_topic_b = False
    for topic, message, reply in CONVERSATION:
        if topic == "B" and not seen_topic_b:
            seen_topic_b = True
            elapsed += timedelta(hours=6)  # the silence between the two sittings
        elapsed += timedelta(minutes=3)
        report = app.sessions.commit_turn(
            persona_id=PERSONA,
            session_id=session_id,
            user_message=message,
            persona_response=reply,
            occurred_at=START + elapsed,
        )
        turn_ids.append(str(report["turn_id"]))
    return session_id, turn_ids


@pytest.mark.anyio
async def test_eighteen_turn_conversation_organises_into_two_episodes(app: Any) -> None:
    _, turn_ids = _run_conversation(app)
    assert len(turn_ids) == 18

    episodes = sorted(
        app.episodes.list_episodes(persona_id=PERSONA), key=lambda item: item.sequence
    )
    assert len(episodes) == 2

    first, second = episodes
    # boundary_reason on a CLOSED episode is why it ended; on the OPEN one it is
    # why it started.  metadata carries both halves explicitly.
    assert first.status is EpisodeStatus.PENDING_CONSOLIDATION
    assert first.boundary_reason == "idle_gap"
    assert first.metadata["start_reason"] == "first_turn"
    assert first.metadata["close_reason"] == "idle_gap"
    assert second.status is EpisodeStatus.OPEN
    assert second.boundary_reason == "idle_gap"
    assert second.metadata["start_reason"] == "idle_gap"
    assert first.turn_count == 9
    assert second.turn_count == 9

    # Each Episode owns exactly the turns of its own stretch, in order.
    first_turns = [item.turn_id for item in app.episodes.episode_turns(first.id)]
    second_turns = [item.turn_id for item in app.episodes.episode_turns(second.id)]
    assert first_turns == turn_ids[:9]
    assert second_turns == turn_ids[9:]
    assert not set(first_turns) & set(second_turns)

    # Provenance resolves to the raw committed text for every source turn.
    for index, turn in enumerate(app.episodes.episode_turns(first.id)):
        text = app.episodes.resolve_turn_text(turn)
        assert CONVERSATION[index][1] in text
        assert CONVERSATION[index][2] in text

    # Consolidation describes each stretch separately, and never blocks a turn.
    summarize = _summariser()
    for episode in episodes:
        report = await app.episodes.consolidate_episode(
            episode.id, summarize=summarize, allow_open=True
        )
        assert report["summary_created"] is True

    refreshed = [
        app.episodes.get_episode(item.id) for item in episodes
    ]
    assert all(item is not None for item in refreshed)
    titles = [str(item.title) for item in refreshed if item]  # type: ignore[union-attr]
    assert titles == ["讨论考研与重庆理工", "与小陈的争吵与和好"]
    assert all(item.summary_status == "ready" for item in refreshed if item)  # type: ignore[union-attr]
    assert (refreshed[0].structured_summary().commitments if refreshed[0] else []) == [
        "用户计划明天开始复习",
        "一周后回顾进度",
    ]

    coverage = app.episodes.coverage(persona_id=PERSONA)
    assert coverage["committed_turns"] == 18
    assert coverage["assigned_to_episode"] == 18
    assert coverage["pending_consolidation"] == 0
    assert coverage["orphaned"] == 0
    assert coverage["episodes"] == 2

    # Human-readable dump (visible with `pytest -s`): the point of this test is
    # that the grouping matches the conversation, not merely that numbers add up.
    print("\n=== Phase 3 Episode report (synthetic conversation) ===")
    for episode in refreshed:
        assert episode is not None
        window = f"{episode.started_at:%H:%M}"
        if episode.ended_at:
            window += f"-{episode.ended_at:%H:%M}"
        print(
            f"[{episode.sequence}] {episode.title} | {episode.status.value} | "
            f"{episode.turn_count} turns | {window} | boundary={episode.boundary_reason}"
        )
        print(f"    {episode.summary}")
        summary = episode.structured_summary()
        print(f"    commitments={summary.commitments} unresolved={summary.unresolved}")
        print(f"    sources={len(app.episodes.episode_turns(episode.id))}")


def test_an_idle_gap_inside_a_topic_still_boundaries(app: Any) -> None:
    """Boundary is about the sitting, not about whether the subject changed."""

    session_id = app.sessions.start_session(
        persona_id=PERSONA, title="同一话题两次对话", counterpart_id="user"
    ).id
    app.sessions.commit_turn(
        persona_id=PERSONA,
        session_id=session_id,
        user_message="考研的事，早上聊",
        persona_response="早上好",
        occurred_at=START,
    )
    app.sessions.commit_turn(
        persona_id=PERSONA,
        session_id=session_id,
        user_message="考研的事，晚上接着聊",
        persona_response="晚上好",
        occurred_at=START + timedelta(hours=7),
    )
    episodes = app.episodes.list_episodes(persona_id=PERSONA)
    assert len(episodes) == 2
    closed = next(item for item in episodes if item.sequence == 1)
    opened = next(item for item in episodes if item.sequence == 2)
    assert closed.status is EpisodeStatus.PENDING_CONSOLIDATION
    assert closed.metadata["close_reason"] == "idle_gap"
    assert opened.status is EpisodeStatus.OPEN
