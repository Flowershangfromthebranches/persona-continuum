"""Phase 7 acceptance: Provenance-backed Raw Recall.

Scenarios A-O from the Phase 7 brief.  The model side is always scripted (these
tests are about RESOLUTION, ranking, budgets and honesty, not about model
quality); ``scripts/phase7_raw_recall_ab.py`` is the real-model counterpart.

Every excerpt in these tests is compared against the actual ``session_turns``
row it claims to come from -- that is the whole point of the phase.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.domain.raw_recall import (
    RawMemoryRef,
    RawRecallBudget,
    RawRecallScope,
    SelectionReason,
    SourceAvailability,
)
from persona_continuum.room.models import ParticipantSlot, RoomMode

PERSONA = "su_he"
COUNTERPART = "user"
BASE = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
SCOPE = RawRecallScope(persona_id=PERSONA, counterpart_id=COUNTERPART)


@pytest.fixture()
def app(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Any:
    data_dir = tmp_path / "pc-raw-recall"
    monkeypatch.setenv("PERSONA_CONTINUUM_HOME", str(data_dir))
    continuum = PersonaContinuum(Config(data_dir=data_dir), include_fake_agent=True)
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


# --- scaffolding ------------------------------------------------------------


def _session(app: PersonaContinuum, counterpart: str = COUNTERPART) -> str:
    return app.sessions.start_session(
        persona_id=PERSONA, title="t", counterpart_id=counterpart
    ).id


def _commit(
    app: PersonaContinuum,
    session_id: str,
    message: str,
    *,
    minutes: int = 0,
    reply: str = "苏禾回应。",
    counterpart: str = COUNTERPART,
) -> str:
    report = app.sessions.commit_turn(
        persona_id=PERSONA,
        session_id=session_id,
        user_message=message,
        persona_response=reply,
        occurred_at=BASE + timedelta(minutes=minutes),
        counterpart_id=counterpart,
    )
    return str(report["turn_id"])


def _episodes(app: PersonaContinuum, counterpart: str = COUNTERPART) -> list[Any]:
    return sorted(
        app.episodes.list_episodes(persona_id=PERSONA, counterpart_id=counterpart),
        key=lambda item: item.started_at,
    )


def _raw_row(app: PersonaContinuum, turn_id: str) -> dict[str, str]:
    row = app.database.conn.execute(
        "SELECT user_message, persona_response FROM session_turns WHERE id = ?", (turn_id,)
    ).fetchone()
    assert row is not None, turn_id
    return {"user": str(row["user_message"]), "persona": str(row["persona_response"])}


def _excerpt_text(result: Any) -> str:
    return "\n".join(
        message.raw_text for excerpt in result.excerpts for message in excerpt.messages
    )


def _turn_ids(result: Any) -> list[str]:
    return [turn_id for excerpt in result.excerpts for turn_id in excerpt.turn_ids]


def _facts_stub(*claims: dict[str, Any]) -> Any:
    async def extract(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        return {"facts": list(claims)}

    return extract


def _preference(
    value: str, *, turn_id: str | None = None, confidence: float = 0.8
) -> dict[str, Any]:
    claim: dict[str, Any] = {
        "category": "preference",
        "subject": "用户",
        "predicate": "喜欢的饮品",
        "value": value,
        "display_text": f"用户喜欢的饮品是{value}",
        "origin": "user_asserted",
        "confidence": confidence,
    }
    if turn_id:
        claim["evidence_turn_id"] = turn_id
    return claim


def _threads_stub() -> Any:
    """A deterministic resolver keyed on the turn text, like the real one."""

    async def resolve(payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        text = " ".join(str(turn.get("text") or "") for turn in payload.get("turns") or [])
        turn_ids = [str(turn.get("turn_id") or "") for turn in payload.get("turns") or []]
        candidates = {
            str(item["thread_id"]): item for item in payload.get("existing_threads") or []
        }
        plan_thread = next(
            (key for key, item in candidates.items() if "重庆" in f"{item.get('title')}"), None
        )

        def action(operation: str, thread_id: str | None, **extra: Any) -> dict[str, Any]:
            payload_action: dict[str, Any] = {
                "operation": operation,
                "thread_id": thread_id,
                "confidence": 0.85,
                "reason": "scripted resolver",
            }
            payload_action.update(extra)
            return payload_action

        if "票" in text and plan_thread is not None:
            return {
                "threads": [
                    action(
                        "milestone",
                        plan_thread,
                        milestone="tickets",
                        source_turn_ids=turn_ids,
                    )
                ]
            }
        if "重庆" in text:
            return {
                "threads": [
                    action(
                        "create",
                        None,
                        thread_type="plan",
                        title="重庆旅行",
                        source_turn_ids=turn_ids[:1],
                    )
                ]
            }
        return {"threads": []}

    return resolve


def _episode_summary_stub() -> Any:
    """Summaries that deliberately DROP the details raw recall exists for."""

    async def summarize(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        text = " ".join(str(turn.get("text") or "") for turn in payload.get("turns") or [])
        if "票" in text or "车" in text:
            return {"title": "购票", "summary": "用户已购票。", "topics": ["购票"]}
        if "生气" in text or "和好" in text:
            return {"title": "冲突与和好", "summary": "发生冲突并和好。", "topics": ["关系"]}
        return {"title": "闲聊", "summary": "用户随便聊了几句。", "topics": ["闲聊"]}

    return summarize


async def _blurry_summariser(payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """A chapter summary that says a conflict happened, and nothing more."""

    return {"title": "冲突与和好", "summary": "发生冲突并和好。", "key_entities": []}


async def _plain_summariser(payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """A grounded summariser for chapters / long-term segments."""

    picked = [str(item.get("title") or "") for item in payload.get("sources") or []]
    picked = [text for text in picked if text]
    return {
        "title": picked[0] if picked else "阶段",
        "summary": "、".join(str(item.get("summary") or "") for item in payload["sources"]),
        "key_entities": [],
    }


async def _consolidate_episodes(app: PersonaContinuum) -> None:
    for episode in _episodes(app):
        await app.episodes.consolidate_episode(
            episode.id, summarize=_episode_summary_stub(), allow_open=True
        )


# --- A. fact -> raw ---------------------------------------------------------


@pytest.mark.anyio
async def test_a_fact_recall_returns_the_exact_user_sentence(app: PersonaContinuum) -> None:
    session_id = _session(app)
    liked = _commit(app, session_id, "我最喜欢喝茉莉奶绿。", minutes=0)
    _commit(app, session_id, "今天天气真不错。", minutes=60)
    episode = _episodes(app)[0]
    await app.facts.extract_episode_facts(
        episode.id, extract=_facts_stub(_preference("茉莉奶绿", turn_id=liked))
    )
    fact_id = app.facts.list_facts(persona_id=PERSONA)[0].id

    result = app.raw_recall.recall(
        scope=SCOPE,
        query="我喜欢喝什么？",
        memory_refs=[RawMemoryRef(ref_type="fact", ref_id=fact_id)],
    )

    assert not result.is_empty
    assert result.excerpts[0].selection_reason is SelectionReason.FACT_DIRECT_EVIDENCE
    assert "我最喜欢喝茉莉奶绿。" in _excerpt_text(result)
    # The evidence message is the RAW row, not a paraphrase of the fact text.
    message = result.excerpts[0].messages[0]
    assert message.raw_text == _raw_row(app, liked)["user"]
    assert message.speaker == "user"
    assert message.turn_id == liked
    assert result.total_tokens <= 4000


# --- B. superseded fact -----------------------------------------------------


@pytest.mark.anyio
async def test_b_superseded_preference_returns_both_sides(app: PersonaContinuum) -> None:
    first_session = _session(app)
    liked = _commit(app, first_session, "我最喜欢喝茉莉奶绿。", minutes=0)
    later_session = _session(app)
    switched = _commit(app, later_session, "现在基本只喝美式了。", minutes=1500)
    for episode in _episodes(app):
        await app.facts.extract_episode_facts(
            episode.id,
            extract=_facts_stub(
                _preference("茉莉奶绿" if episode.session_id == first_session else "美式",
                            turn_id=liked if episode.session_id == first_session else switched)
            ),
        )
    facts = app.facts.list_facts(persona_id=PERSONA)
    assert len(facts) == 2

    result = app.raw_recall.recall(
        scope=SCOPE,
        query="我的饮料口味是什么时候变的？",
        memory_refs=[RawMemoryRef(ref_type="fact", ref_id=fact.id) for fact in facts],
    )

    text = _excerpt_text(result)
    # BOTH the old preference and the supersession evidence come back.  A raw
    # recall that only returned the current fact could not answer "when".
    assert "我最喜欢喝茉莉奶绿。" in text
    assert "现在基本只喝美式了。" in text
    assert {liked, switched} <= set(_turn_ids(result))


# --- C. thread -> raw -------------------------------------------------------


@pytest.mark.anyio
async def test_c_thread_milestone_beats_the_whole_thread(app: PersonaContinuum) -> None:
    session_id = _session(app)
    planned = _commit(app, session_id, "我准备去重庆旅行。", minutes=0)
    bought = _commit(app, session_id, "票我买好了，晚上 8 点那班。", minutes=200)
    for episode in _episodes(app):
        await app.threads.resolve_episode_threads(episode.id, resolve=_threads_stub())
    threads = app.threads.list_threads(persona_id=PERSONA)
    assert len(threads) == 1
    thread_id = threads[0].id

    result = app.raw_recall.recall(
        scope=SCOPE,
        query="我重庆那次票是什么时候买的？",
        memory_refs=[RawMemoryRef(ref_type="thread", ref_id=thread_id)],
    )

    text = _excerpt_text(result)
    assert "票我买好了，晚上 8 点那班。" in text
    # The MILESTONE turn is what the query is about, so it outranks the opener.
    assert result.excerpts[0].selection_reason in {
        SelectionReason.THREAD_EVENT_SOURCE,
        SelectionReason.THREAD_SOURCE,
    }
    assert bought in _turn_ids(result)
    assert planned in _turn_ids(result) or planned == result.excerpts[0].turn_ids[0]


# --- D. detail missing from the summary -------------------------------------


@pytest.mark.anyio
async def test_d_detail_missing_from_summary_is_recovered(app: PersonaContinuum) -> None:
    session_id = _session(app)
    ticket = _commit(app, session_id, "我订的是晚上 8 点那班车。", minutes=0)
    await _consolidate_episodes(app)
    episode = _episodes(app)[0]
    # The summary really did drop the detail.
    assert "晚上 8 点" not in (episode.summary or "")

    result = app.raw_recall.recall(
        scope=SCOPE,
        query="我当时买的几点的票？",
        memory_refs=[RawMemoryRef(ref_type="episode", ref_id=episode.id)],
    )

    # Only raw recall can answer this; the summary cannot.
    assert "我订的是晚上 8 点那班车。" in _excerpt_text(result)
    assert ticket in _turn_ids(result)


# --- E. long-term -> chapter -> episode -> raw ------------------------------


@pytest.mark.anyio
async def test_e_long_term_walks_all_the_way_to_raw(app: PersonaContinuum) -> None:
    first = _session(app)
    planted = _commit(app, first, "下个月我准备去重庆，想看看晚上的洪崖洞。", minutes=0)
    second = _session(app)
    _commit(app, second, "最近在忙别的事。", minutes=300_000)
    await _consolidate_episodes(app)
    for episode in _episodes(app):
        app.hierarchies.assign_episode(episode)
    chapters = app.hierarchies.list_summaries(persona_id=PERSONA, level=1, limit=10)
    for chapter in chapters:
        app.hierarchies.close_summary(chapter.id)
        report = await app.hierarchies.consolidate_summary(
            chapter.id, summarize=_plain_summariser
        )
        assert report["summary_status"] == "ready", report
    plan = app.hierarchies.plan_long_term(
        persona_id=PERSONA, counterpart_id=COUNTERPART, branch_id="main", close_segment=True
    )
    long_term_id = plan["closed_summary_id"]
    assert long_term_id
    await app.hierarchies.consolidate_summary(
        long_term_id, summarize=_plain_summariser
    )

    result = app.raw_recall.recall(
        scope=SCOPE,
        query="我那次重庆住宿为什么换了？",
        memory_refs=[RawMemoryRef(ref_type="summary", ref_id=long_term_id)],
    )

    assert result.excerpts, result.unavailable
    assert result.excerpts[0].selection_reason is SelectionReason.LONG_TERM_DESCENDANT
    assert "下个月我准备去重庆，想看看晚上的洪崖洞。" in _excerpt_text(result)
    assert planted in _turn_ids(result)
    # A summary is an entry point, never the source of the "raw" text.
    stored_summary = app.hierarchies.get_summary(long_term_id)
    assert stored_summary is not None
    for excerpt in result.excerpts:
        for message in excerpt.messages:
            assert message.raw_text != stored_summary.summary


# --- F. relationship detail -------------------------------------------------


@pytest.mark.anyio
async def test_f_relationship_detail_survives_a_blurry_summary(app: PersonaContinuum) -> None:
    session_id = _session(app)
    reason = _commit(app, session_id, "我今天很生气，因为你答应了又反悔。", minutes=0)
    _commit(app, session_id, "好吧，和好了。", minutes=120)
    await _consolidate_episodes(app)
    chapter_report = None
    for episode in _episodes(app):
        app.hierarchies.assign_episode(episode)
    chapter = app.hierarchies.list_summaries(persona_id=PERSONA, level=1, limit=5)[0]
    app.hierarchies.close_summary(chapter.id)
    chapter_report = await app.hierarchies.consolidate_summary(
        chapter.id,
        summarize=_blurry_summariser,
    )
    assert chapter_report["summary_status"] == "ready"

    result = app.raw_recall.recall(
        scope=SCOPE,
        query="我当时到底为什么生气？",
        memory_refs=[RawMemoryRef(ref_type="summary", ref_id=chapter.id)],
    )

    # The chapter only says "conflict"; the REASON exists only in the raw turn.
    assert "你答应了又反悔" in _excerpt_text(result)
    assert reason in _turn_ids(result)


# --- G. ambiguous memories --------------------------------------------------


@pytest.mark.anyio
async def test_g_ambiguous_memories_return_two_candidates(app: PersonaContinuum) -> None:
    first = _session(app)
    _commit(app, first, "重庆的票我买好了。", minutes=0)
    second = _session(app)
    _commit(app, second, "演唱会的票我也买好了。", minutes=60)
    episodes = _episodes(app)
    assert len(episodes) == 2

    result = app.raw_recall.recall(
        scope=SCOPE,
        query="我之前票买好了的那次",
        memory_refs=[RawMemoryRef(ref_type="episode", ref_id=episode.id) for episode in episodes],
        budget=RawRecallBudget(max_total_tokens=4000, max_excerpts=4),
    )

    assert len(result.excerpts) == 2
    assert {excerpt.episode_ids[0] for excerpt in result.excerpts} == {
        episode.id for episode in episodes
    }


# --- H. dedup ---------------------------------------------------------------


@pytest.mark.anyio
async def test_h_one_turn_is_never_returned_twice(app: PersonaContinuum) -> None:
    session_id = _session(app)
    _commit(app, session_id, "我准备去重庆旅行。", minutes=0)
    bought = _commit(app, session_id, "票我买好了，晚上 8 点那班。", minutes=100)
    episode = _episodes(app)[0]
    await app.facts.extract_episode_facts(
        episode.id, extract=_facts_stub(_preference("晚上 8 点的车票", turn_id=bought))
    )
    # First pass creates the thread; the second pass sees it and records the
    # milestone against the ticket turn -- exactly how the room pass converges.
    await app.threads.resolve_episode_threads(episode.id, resolve=_threads_stub())
    await app.threads.resolve_episode_threads(episode.id, resolve=_threads_stub(), force=True)
    fact_id = app.facts.list_facts(persona_id=PERSONA)[0].id
    thread_id = app.threads.list_threads(persona_id=PERSONA)[0].id

    result = app.raw_recall.recall(
        scope=SCOPE,
        query="票买好了吗？",
        memory_refs=[
            RawMemoryRef(ref_type="fact", ref_id=fact_id),
            RawMemoryRef(ref_type="thread", ref_id=thread_id),
        ],
        budget=RawRecallBudget(max_total_tokens=4000, max_excerpts=4),
    )

    assert _turn_ids(result).count(bought) == 1
    assert _excerpt_text(result).count("票我买好了，晚上 8 点那班。") == 1


# --- I. overlapping windows merge ------------------------------------------


@pytest.mark.anyio
async def test_i_adjacent_anchors_merge_into_one_excerpt(app: PersonaContinuum) -> None:
    session_id = _session(app)
    first = _commit(app, session_id, "酒店我订好了，在解放碑。", minutes=0)
    second = _commit(app, session_id, "后来换成江北区了，那边便宜。", minutes=60)
    third = _commit(app, session_id, "今天去吃了火锅。", minutes=120)
    episode = _episodes(app)[0]
    await app.facts.extract_episode_facts(
        episode.id,
        extract=_facts_stub(
            _preference("解放碑的酒店", turn_id=first),
            _preference("江北区的酒店", turn_id=second),
        ),
    )
    fact_ids = [fact.id for fact in app.facts.list_facts(persona_id=PERSONA)]

    result = app.raw_recall.recall(
        scope=SCOPE,
        query="住宿后来怎么处理的？",
        memory_refs=[RawMemoryRef(ref_type="fact", ref_id=fact_id) for fact_id in fact_ids],
        # No context expansion: the two anchors are still adjacent, so this is a
        # pure overlap/adjacency merge, not an expansion artefact.
        budget=RawRecallBudget(max_total_tokens=4000, max_excerpts=4, max_context_window=0),
    )

    assert len(result.excerpts) == 1
    turns = result.excerpts[0].turn_ids
    assert first in turns and second in turns
    # Contiguous and in conversation order: the two anchors did not come back
    # as two separate fragments, and nothing was re-ordered by score.
    positions = [
        turn.position
        for turn in app.episodes.episode_turns(result.excerpts[0].episode_ids[0])
        if turn.turn_id in turns
    ]
    assert positions == sorted(positions)
    assert third not in turns


def test_merge_windows_joins_adjacent_and_overlapping_spans() -> None:
    """The merge rule itself, as pure arithmetic (no store involved)."""

    from persona_continuum.application.raw_recall_service import _Window, merge_windows

    def window(positions: list[int], anchor: int) -> Any:
        return _Window(
            episode_id="episode_a",
            positions=list(positions),
            anchor_turn_id=f"turn_{anchor}",
            anchor_position=anchor,
            strength=0.5,
            reason=SelectionReason.EPISODE_ANCHOR,
            ref=RawMemoryRef(ref_type="episode", ref_id="episode_a"),
            memory_relevance=0.5,
        )

    # [0] and [1] are adjacent -> one excerpt.
    merged, count = merge_windows([window([0], 0), window([1], 1)])
    assert count == 1 and merged[0].positions == [0, 1]
    # [100] and [102] are NOT adjacent -> two excerpts, no hole stitched over.
    merged, count = merge_windows([window([100], 100), window([102], 102)])
    assert count == 0 and len(merged) == 2
    # Overlapping spans keep the strongest claim.
    strong = window([4, 5], 5)
    strong.strength = 1.0
    merged, count = merge_windows([window([3, 4], 4), strong])
    assert count == 1
    assert merged[0].positions == [3, 4, 5]
    assert merged[0].strength == 1.0
    # Different episodes never merge.
    other = window([0], 0)
    other.episode_id = "episode_b"
    merged, count = merge_windows([window([0], 0), other])
    assert count == 0 and len(merged) == 2


# --- J. budget --------------------------------------------------------------


@pytest.mark.anyio
async def test_j_total_budget_is_respected_at_message_boundaries(
    app: PersonaContinuum,
) -> None:
    session_id = _session(app)
    for index in range(20):
        _commit(
            app,
            session_id,
            f"第 {index} 天我去了一趟很远的地方，聊了很久很久的事情，说了很多很多的话。",
            minutes=index * 5,
            reply="苏禾也说了很长很长的一段回应，内容同样非常充实，字数不少，足够撑起一个片段。",
        )
    episode = _episodes(app)[0]

    result = app.raw_recall.recall(
        scope=SCOPE,
        query="第 3 天",
        memory_refs=[RawMemoryRef(ref_type="episode", ref_id=episode.id)],
        budget=RawRecallBudget(
            max_total_tokens=2000,
            max_excerpts=10,
            max_tokens_per_excerpt=400,
            max_turns_per_excerpt=50,
        ),
    )

    assert result.excerpts
    assert result.total_tokens <= 2000
    for excerpt in result.excerpts:
        for message in excerpt.messages:
            row = _raw_row(app, message.turn_id)
            whole = row["user"] if message.speaker == "user" else row["persona"]
            # A message boundary, never a chopped string.
            assert message.raw_text == whole


# --- K. a large budget is not clamped by Context Policy ---------------------


@pytest.mark.anyio
async def test_k_large_budget_is_not_clamped(app: PersonaContinuum) -> None:
    from persona_continuum.application import raw_recall_service

    source = raw_recall_service.__file__ or ""
    assert "context_policy" not in source
    assert "6000" not in source and "7000" not in source

    session_id = _session(app)
    # 20 turns x 2 messages, comfortably inside one Episode's 24-turn boundary.
    for index in range(20):
        _commit(app, session_id, f"第 {index} 天的对话内容。", minutes=index * 5)
    episode = _episodes(app)[0]

    result = app.raw_recall.recall(
        scope=SCOPE,
        query="",
        memory_refs=[RawMemoryRef(ref_type="episode", ref_id=episode.id)],
        budget=RawRecallBudget(
            max_total_tokens=200_000,
            max_excerpts=50,
            max_tokens_per_excerpt=20_000,
            max_turns_per_excerpt=1000,
        ),
    )

    # 20 turns x 2 messages: far past any "8 messages" habit, and nothing in the
    # service silently re-applied a local profile.
    assert len(result.excerpts) >= 1
    assert sum(len(excerpt.messages) for excerpt in result.excerpts) == 40
    assert result.total_tokens <= 200_000


# --- L. shared user provenance ---------------------------------------------


@pytest.mark.anyio
async def test_l_shared_user_message_resolves_once(app: PersonaContinuum, tmp_path) -> None:
    config = Config(data_dir=tmp_path / "pc-raw-recall-room")
    room_app = PersonaContinuum(config, include_fake_agent=True)
    room_app.init()
    room_app.personas.create_from_manifest(
        {
            "id": PERSONA,
            "display_name": "苏禾",
            "persona_type": "fictional",
            "run_mode": "continuation",
        }
    )
    room_app.personas.create_from_manifest(
        {
            "id": "su_he_b",
            "display_name": "苏禾乙",
            "persona_type": "fictional",
            "run_mode": "continuation",
        }
    )

    class _StructuredResult:
        def __init__(self, value: dict[str, Any]) -> None:
            self.value = value

    async def structured(
        binding: Any, *, system_prompt: str, user_message: str, schema: dict[str, Any], phase: str
    ) -> Any:
        if phase == "memory_thread_resolution":
            return _StructuredResult({"threads": []})
        if phase == "memory_fact_extraction":
            return _StructuredResult({"facts": []})
        if phase == "memory_hierarchy_summary":
            return _StructuredResult({"title": "阶段", "summary": "阶段摘要。"})
        return _StructuredResult({"title": "闲聊", "summary": "用户随便聊了几句。"})

    room_app.orchestrator.runtime_executor.execute_structured = structured
    try:
        room = room_app.orchestrator.create_room(
            title="双人",
            topic="日常",
            participants=[
                ParticipantSlot(
                    participant_id="slot_a",
                    persona_id=PERSONA,
                    display_name="苏禾",
                    runtime_selection="fake_agent",
                    model_selection="fake-gpt-5",
                    reasoning_selection="none",
                ),
                ParticipantSlot(
                    participant_id="slot_b",
                    persona_id="su_he_b",
                    display_name="苏禾乙",
                    runtime_selection="fake_agent",
                    model_selection="fake-gpt-5",
                    reasoning_selection="none",
                ),
            ],
            mode=RoomMode.AUTONOMOUS,
        )
        await room_app.orchestrator.start_room(room.id)
        user_message = "我们俩今晚一起去吃火锅吧。"
        events = [
            event
            async for event in room_app.orchestrator.step_turn(room.id, user_message=user_message)
        ]
        task = room_app.orchestrator._episode_tasks.get(room.id)
        if task is not None:
            await task
        transcripts = [
            str(row["content"])
            for row in room_app.database.conn.execute(
                "SELECT content FROM room_transcripts WHERE room_id = ?", (room.id,)
            ).fetchall()
        ]
        episodes = room_app.episodes.list_episodes(persona_id=PERSONA)
        assert episodes, [event.get("event") for event in events]
        stored = episodes[0]

        scope = RawRecallScope(
            persona_id=PERSONA,
            counterpart_id=stored.counterpart_id,
            branch_id=stored.branch_id,
        )
        result = room_app.raw_recall.recall(
            scope=scope,
            query="火锅",
            memory_refs=[RawMemoryRef(ref_type="episode", ref_id=stored.id)],
        )

        # The user's message is ONE raw event, resolved from the transcript, and
        # never duplicated as a fake per-persona turn.
        assert user_message in transcripts
        assert result.excerpts, result.unavailable
        text = _excerpt_text(result)
        assert user_message in text
        # Exactly ONE message IS the shared user turn: the persona's own reply
        # may quote it, but it is never re-emitted as a second copy of the user.
        shared = [
            message
            for excerpt in result.excerpts
            for message in excerpt.messages
            if message.source_kind == "shared_user"
        ]
        assert len(shared) == 1
        assert shared[0].raw_text == user_message
        speakers = {message.speaker for excerpt in result.excerpts for message in excerpt.messages}
        # The shared event keeps the transcript's real speaker ("User"); the
        # persona's own reply is labelled by the canonical resolver, never
        # relabelled as the user.
        assert speakers <= {"User", "user", "persona", "苏禾", "苏禾乙"}
        assert all(
            message.speaker != "苏禾"
            for excerpt in result.excerpts
            for message in excerpt.messages
            if message.source_kind == "shared_user"
        )
    finally:
        room_app.close()


# --- M. deleted / partial provenance ---------------------------------------


@pytest.mark.anyio
async def test_m_deleted_and_partial_sources_are_reported_not_faked(
    app: PersonaContinuum,
) -> None:
    session_id = _session(app)
    first = _commit(app, session_id, "第一段会被删除的原话。", minutes=0)
    second = _commit(app, session_id, "第二段保留下来的原话。", minutes=60)
    episode = _episodes(app)[0]
    await _consolidate_episodes(app)
    stored_before = app.episodes.get_episode(episode.id)
    assert stored_before is not None and stored_before.summary

    # PARTIAL: one of the two turns is explicitly removed.
    app.database.conn.execute("DELETE FROM session_turns WHERE id = ?", (first,))
    app.database.conn.commit()
    app.episodes.refresh_source_availability([episode.id])

    result = app.raw_recall.recall(
        scope=SCOPE,
        query="原话",
        memory_refs=[RawMemoryRef(ref_type="episode", ref_id=episode.id)],
    )

    assert result.excerpts, "the surviving turn must still be reachable"
    assert result.excerpts[0].source_availability is SourceAvailability.PARTIAL
    assert "第二段保留下来的原话。" in _excerpt_text(result)
    assert first not in _turn_ids(result)
    assert second in _turn_ids(result)

    # DELETED: everything is gone.  Nothing may be invented, not even from a
    # summary that happens to exist.
    app.database.conn.execute("DELETE FROM session_turns WHERE id = ?", (second,))
    app.database.conn.commit()
    app.episodes.refresh_source_availability([episode.id])
    empty = app.raw_recall.recall(
        scope=SCOPE,
        query="原话",
        memory_refs=[RawMemoryRef(ref_type="episode", ref_id=episode.id)],
    )
    assert empty.excerpts == []
    assert empty.unavailable
    assert empty.unavailable[0].availability in {
        SourceAvailability.DELETED,
        SourceAvailability.UNAVAILABLE,
    }
    stored = app.episodes.get_episode(episode.id)
    assert stored is not None
    # The summary text is still on disk -- and it is still NOT raw text.
    assert stored.summary
    assert "第一段会被删除的原话" not in _excerpt_text(empty)
    assert "第二段保留下来的原话" not in _excerpt_text(empty)


# --- N. scope isolation ----------------------------------------------------


@pytest.mark.anyio
async def test_n_recall_never_crosses_scope(app: PersonaContinuum) -> None:
    session_a = _session(app, counterpart="user_a")
    _commit(app, session_a, "和 A 说的秘密。", minutes=0, counterpart="user_a")
    episode_a = _episodes(app, counterpart="user_a")[0]

    result = app.raw_recall.recall(
        scope=RawRecallScope(persona_id=PERSONA, counterpart_id="user_b"),
        query="秘密",
        memory_refs=[RawMemoryRef(ref_type="episode", ref_id=episode_a.id)],
    )

    assert result.excerpts == []
    assert result.unavailable
    assert result.unavailable[0].detail == "scope_mismatch"
    # A different branch is a different scope too.
    branched = app.raw_recall.recall(
        scope=RawRecallScope(persona_id=PERSONA, counterpart_id="user_a", branch_id="fork"),
        query="秘密",
        memory_refs=[RawMemoryRef(ref_type="episode", ref_id=episode_a.id)],
    )
    assert branched.excerpts == []
    assert branched.unavailable[0].detail == "scope_mismatch"


# --- O. restart / statelessness --------------------------------------------


@pytest.mark.anyio
async def test_o_recall_is_stateless_across_a_restart(tmp_path) -> None:
    data_dir = tmp_path / "pc-raw-recall-restart"
    app = PersonaContinuum(Config(data_dir=data_dir), include_fake_agent=True)
    app.init()
    app.personas.create_from_manifest(
        {
            "id": PERSONA,
            "display_name": "苏禾",
            "persona_type": "fictional",
            "run_mode": "continuation",
        }
    )
    session_id = _session(app)
    ticket = _commit(app, session_id, "我订的是晚上 8 点那班车。", minutes=0)
    episode = _episodes(app)[0]
    first = app.raw_recall.recall(
        scope=SCOPE,
        query="几点的票",
        memory_refs=[RawMemoryRef(ref_type="episode", ref_id=episode.id)],
    )
    app.close()

    reopened = PersonaContinuum(Config(data_dir=data_dir), include_fake_agent=True)
    reopened.init()
    try:
        again = reopened.raw_recall.recall(
            scope=SCOPE,
            query="几点的票",
            memory_refs=[RawMemoryRef(ref_type="episode", ref_id=episode.id)],
        )
        # Excerpt ids are fresh; the CONTENT is identical, because recall is a
        # stateless query over the raw stores and nothing was cached.
        assert _excerpt_text(first) == _excerpt_text(again)
        assert _turn_ids(first) == _turn_ids(again)
        assert len(first.excerpts) == len(again.excerpts)
        assert first.total_tokens == again.total_tokens
        assert "我订的是晚上 8 点那班车。" in _excerpt_text(again)
        assert ticket in _turn_ids(again)
        third = reopened.raw_recall.recall(
            scope=SCOPE,
            query="几点的票",
            memory_refs=[RawMemoryRef(ref_type="episode", ref_id=episode.id)],
        )
        assert _excerpt_text(third) == _excerpt_text(again)
    finally:
        reopened.close()


# --- speaker correctness / no rewriting ------------------------------------


@pytest.mark.anyio
async def test_speaker_labels_come_from_the_canonical_resolver(app: PersonaContinuum) -> None:
    session_id = _session(app)
    _commit(app, session_id, "用户的问题。", minutes=0, reply="苏禾的回答。")
    episode = _episodes(app)[0]
    turns = app.episodes.episode_turns(episode.id)

    messages = app.episodes.resolve_turn_messages(turns[0])

    # Phase 4.1 fixed the "whole turn labelled persona" bug; raw recall reuses
    # that resolver instead of growing a third opinion.
    assert [message.speaker for message in messages] == ["user", "persona"]
    assert [message.raw_text for message in messages] == ["用户的问题。", "苏禾的回答。"]
    assert app.episodes.turn_speaker_label(turns[0]) == "user+persona"

    result = app.raw_recall.recall(
        scope=SCOPE,
        query="问题",
        memory_refs=[RawMemoryRef(ref_type="episode", ref_id=episode.id)],
        budget=RawRecallBudget(max_total_tokens=4000, max_excerpts=1),
    )
    speakers = [message.speaker for excerpt in result.excerpts for message in excerpt.messages]
    assert speakers == ["user", "persona"]
    for excerpt in result.excerpts:
        for message in excerpt.messages:
            row = _raw_row(app, message.turn_id)
            assert message.raw_text in (row["user"], row["persona"])


# --- CLI --------------------------------------------------------------------


@pytest.mark.anyio
async def test_cli_recall_raw_reports_scores_and_reasons(app: PersonaContinuum) -> None:
    from typer.testing import CliRunner

    from persona_continuum.cli.app import app as cli_app

    session_id = _session(app)
    _commit(app, session_id, "我最喜欢喝茉莉奶绿。", minutes=0)
    episode = _episodes(app)[0]
    runner = CliRunner()

    listed = runner.invoke(
        cli_app,
        ["recall", "raw", PERSONA, "--episode", episode.id, "--query", "喝什么", "--json"],
    )
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    assert payload["excerpts"], payload["unavailable"]
    assert payload["excerpts"][0]["selection_reason"]
    assert "<" in payload["excerpts"][0]["messages"][0]["raw_text"]  # text redacted

    shown = runner.invoke(
        cli_app,
        [
            "recall",
            "raw",
            PERSONA,
            "--episode",
            episode.id,
            "--query",
            "喝什么",
            "--text",
        ],
    )
    assert shown.exit_code == 0, shown.output
    assert "茉莉奶绿" in shown.output
    assert "reason:" in shown.output
    assert "availability:" in shown.output


# --- prompt injection stays off --------------------------------------------


@pytest.mark.anyio
async def test_raw_recall_is_stored_but_not_injected(tmp_path, monkeypatch) -> None:
    config = Config(data_dir=tmp_path / "pc-raw-recall-room2", phase8_context_assembly=False)
    room_app = PersonaContinuum(config, include_fake_agent=True)
    room_app.init()
    room_app.personas.create_from_manifest(
        {
            "id": PERSONA,
            "display_name": "苏禾",
            "persona_type": "fictional",
            "run_mode": "continuation",
        }
    )

    class _StructuredResult:
        def __init__(self, value: dict[str, Any]) -> None:
            self.value = value

    async def structured(
        binding: Any, *, system_prompt: str, user_message: str, schema: dict[str, Any], phase: str
    ) -> Any:
        if phase == "memory_thread_resolution":
            return _StructuredResult({"threads": []})
        if phase == "memory_fact_extraction":
            return _StructuredResult({"facts": []})
        if phase == "memory_hierarchy_summary":
            return _StructuredResult({"title": "阶段", "summary": "阶段摘要。"})
        return _StructuredResult({"title": "闲聊", "summary": "用户随便聊了几句。"})

    room_app.orchestrator.runtime_executor.execute_structured = structured
    try:
        room = room_app.orchestrator.create_room(
            title="苏禾",
            topic="日常",
            participants=[
                ParticipantSlot(
                    participant_id="slot_su_he",
                    persona_id=PERSONA,
                    display_name="苏禾",
                    runtime_selection="fake_agent",
                    model_selection="fake-gpt-5",
                    reasoning_selection="none",
                )
            ],
            mode=RoomMode.DIRECT_CHAT,
        )
        await room_app.orchestrator.start_room(room.id)
        async for _event in room_app.orchestrator.step_turn(
            room.id, user_message="今天开始准备考研。"
        ):
            pass
        task = room_app.orchestrator._episode_tasks.get(room.id)
        if task is not None:
            await task
        events = [
            event
            async for event in room_app.orchestrator.step_turn(room.id, user_message="第二句")
        ]
        report = next(event for event in events if event.get("event") == "room_context_report")
        assert report["historical_excerpt_tokens"] == 0
        assert report["raw_excerpts_selected"] == 0
        assert report["raw_excerpts_expanded"] == 0
        assert report["raw_excerpt_injection"] == "phase8_not_enabled"
        assert report["raw_recall_available"] is True
    finally:
        room_app.close()
