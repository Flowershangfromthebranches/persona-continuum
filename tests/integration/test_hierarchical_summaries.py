"""Phase 6 acceptance: Hierarchical Summaries (Chapters + Long-term segments).

Scenarios A-L from the Phase 6 brief.  The summariser is scripted so these
tests are about the HIERARCHY (grouping, boundaries, provenance, grounding,
idempotency, deletion) rather than about model quality;
``scripts/phase6_hierarchical_summaries.py`` is the real-model counterpart.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.domain.hierarchical_summary import (
    SummaryReadiness,
    SummaryStatus,
)
from persona_continuum.room.models import ParticipantSlot, RoomMode

PERSONA = "su_he"
BASE = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


class _StructuredResult:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value


def _grounded_summary(payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """A summariser that only repeats what it was given."""

    sources = payload.get("sources") or []
    titles = [
        str(item.get("title") or item.get("summary") or "") for item in sources
    ]
    picked = [title for title in titles if title]
    if not picked:
        # Nothing was summarised yet: borrow grounded wording from the sources
        # that do carry text (chapters point at Episodes, whose text is real).
        picked = [
            str(item.get("summary") or item.get("title") or "")
            for item in (payload.get("facts") or []) + (payload.get("threads") or [])
        ]
        picked = [text for text in picked if text]
    return {
        "title": "、".join(picked[:2]) or "阶段",
        "summary": "、".join(picked[:6]),
        "major_events": picked[:3],
        "relationship_changes": [],
        "resolved_threads": [],
        "ongoing_threads": [],
        "key_entities": [],
        "importance": 0.6,
    }


def _summariser(build: Any = _grounded_summary) -> Any:
    async def summarize(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        return build(payload, schema)

    return summarize


@pytest.fixture()
def app(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Any:
    data_dir = tmp_path / "pc-hierarchy"
    monkeypatch.setenv("PERSONA_CONTINUUM_HOME", str(data_dir))
    config = Config(data_dir=data_dir)
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


def _session(app: PersonaContinuum, counterpart: str = "user", branch: str | None = None) -> str:
    return app.sessions.start_session(
        persona_id=PERSONA,
        title="t",
        counterpart_id=counterpart,
        branch_id=branch,
    ).id


def _commit(
    app: PersonaContinuum,
    session_id: str,
    message: str,
    *,
    minutes: int = 0,
    counterpart: str = "user",
) -> str:
    report = app.sessions.commit_turn(
        persona_id=PERSONA,
        session_id=session_id,
        user_message=message,
        persona_response="苏禾回应",
        occurred_at=BASE + timedelta(minutes=minutes),
        counterpart_id=counterpart,
    )
    return str(report["turn_id"])


def _episodes(app: PersonaContinuum, *, counterpart: str = "user") -> list[Any]:
    return sorted(
        app.episodes.list_episodes(persona_id=PERSONA, counterpart_id=counterpart),
        key=lambda item: item.started_at,
    )


def _assign_all(app: PersonaContinuum, *, counterpart: str = "user") -> list[dict[str, Any]]:
    return [
        app.hierarchies.assign_episode(episode)
        for episode in _episodes(app, counterpart=counterpart)
    ]


def _chapters(app: PersonaContinuum, *, counterpart: str = "user") -> list[Any]:
    return app.hierarchies.list_summaries(
        persona_id=PERSONA, counterpart_id=counterpart, level=1, limit=50
    )


async def _wait(episode_id: str, summarize: Any, *, app: PersonaContinuum) -> dict[str, Any]:
    summary = app.hierarchies.summary_for_source("episode", episode_id)
    assert summary is not None
    return await app.hierarchies.consolidate_summary(summary.id, summarize=summarize)


# --- A. chapter grouping ----------------------------------------------------


@pytest.mark.anyio
async def test_a_related_episodes_form_one_chapter(app: PersonaContinuum) -> None:
    session_id = _session(app)
    for index in range(1, 11):
        _commit(app, session_id, f"考研复习第 {index} 天。", minutes=index * 300)
    reports = _assign_all(app)

    assert reports[0]["action"] == "create"
    assert all(report["action"] == "append" for report in reports[1:8])
    chapters = _chapters(app)
    # Ten episodes, eight per chapter: the first chapter closes on MAX_SOURCES
    # and the rest open a second one.
    assert len(chapters) == 2
    assert chapters[0].source_count == 8
    assert chapters[0].status is SummaryStatus.CLOSED
    assert chapters[1].status is SummaryStatus.OPEN
    assert reports[8]["action"] == "create"
    assert reports[8]["boundary_reason"] == "max_sources"

    finalized = await app.hierarchies.consolidate_summary(
        chapters[0].id, summarize=_summariser()
    )
    assert finalized["summary_status"] == "ready"
    assert finalized["status"] == "closed"


# --- B. boundary ------------------------------------------------------------


@pytest.mark.anyio
async def test_b_a_new_phase_closes_the_previous_chapter(app: PersonaContinuum) -> None:
    session_id = _session(app)
    for index in range(1, 4):
        _commit(app, session_id, f"考研复习第 {index} 天。", minutes=index * 300)
    # A long silence, then a clearly different subject.
    for index in range(1, 4):
        _commit(app, session_id, f"重庆旅行准备第 {index} 步。", minutes=100000 + index * 300)

    reports = _assign_all(app)
    assert [report["boundary_reason"] for report in reports[:3]] == [
        "first_source",
        "append",
        "append",
    ]
    assert reports[3]["boundary_reason"] == "inactivity"
    chapters = _chapters(app)
    assert len(chapters) == 2
    assert chapters[0].status is SummaryStatus.CLOSED
    assert chapters[1].status is SummaryStatus.OPEN
    assert chapters[0].ended_at is not None
    # Closing set the chapter back to "owes a FINAL summary".
    assert chapters[0].summary_status is SummaryReadiness.PENDING


@pytest.mark.anyio
async def test_b2_topic_boundary_is_optional_and_never_blocks(app: PersonaContinuum) -> None:
    """The optional classifier only splits when it says NEW_CHAPTER.

    It is consulted only when no deterministic boundary fired AND the topic
    shift is actually large, and an UNCERTAIN answer or a failed call defaults
    to CONTINUE, so a broken model can never stall grouping.
    """

    session_id = _session(app)
    _commit(app, session_id, "考研的事开始准备了。", minutes=0)
    _commit(app, session_id, "今天去爬山了。", minutes=300)
    for episode in _episodes(app):
        await app.episodes.consolidate_episode(
            episode.id, summarize=_episode_summariser(), allow_open=True
        )
    # The optional classifier is off by default; this test exercises it.
    app.hierarchies.topic_boundary_enabled = True

    calls: list[str] = []

    def classifier(summary: Any, record: Any) -> dict[str, str]:
        calls.append(record.source_id)
        return {"decision": "NEW_CHAPTER"}

    episodes = _episodes(app)
    app.hierarchies.assign_episode(episodes[0], boundary_classifier=classifier)
    assert calls == []  # nothing to compare against yet
    # The first Episode's Chapter is created without consulting anything.
    report = app.hierarchies.assign_episode(episodes[1], boundary_classifier=classifier)
    assert calls == [episodes[1].id]
    assert report["action"] == "create"
    assert report["boundary_reason"] == "topic_shift"
    chapters = _chapters(app)
    assert len(chapters) == 2
    assert chapters[0].status is SummaryStatus.CLOSED

    # ...and a classifier that fails or cannot decide keeps the chapter open.
    _commit(app, session_id, "今天又聊了点别的。", minutes=600)
    fresh = _episodes(app)[-1]
    await app.episodes.consolidate_episode(
        fresh.id, summarize=_episode_summariser(), allow_open=True
    )

    def uncertain(summary: Any, record: Any) -> dict[str, str]:
        return {"decision": "UNCERTAIN"}

    report = app.hierarchies.assign_episode(fresh, boundary_classifier=uncertain)
    assert report["action"] == "append"
    assert len(_chapters(app)) == 2

    _commit(app, session_id, "还有一件事。", minutes=900)
    broken_episode = _episodes(app)[-1]
    await app.episodes.consolidate_episode(
        broken_episode.id, summarize=_episode_summariser(), allow_open=True
    )

    def broken(summary: Any, record: Any) -> dict[str, str]:
        raise RuntimeError("classifier unavailable")

    report = app.hierarchies.assign_episode(broken_episode, boundary_classifier=broken)
    assert report["action"] == "append"
    assert report["error"] is None
    assert len(_chapters(app)) == 2


# --- C. temporal fact evolution --------------------------------------------


@pytest.mark.anyio
async def test_c_chapter_payload_carries_the_fact_timeline(app: PersonaContinuum) -> None:
    session_id = _session(app)
    _commit(app, session_id, "我最喜欢喝茉莉奶绿。", minutes=0)
    _commit(app, session_id, "茉莉奶绿喝腻了，现在只喝美式。", minutes=3 * 24 * 60)
    episodes = _episodes(app)

    async def extract(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        text = " ".join(str(turn.get("text") or "") for turn in payload.get("turns") or [])
        if "只喝美式" in text:
            # The real extractor cites the fact it is replacing; the store then
            # ends the old value even though the new one lands in a new slot.
            existing = payload.get("existing_facts") or []
            cited = next(
                (
                    str(item["fact_id"])
                    for item in existing
                    if "茉莉奶绿" in str(item.get("display_text") or "")
                ),
                None,
            )
            return {
                "facts": [
                    {
                        "category": "preference",
                        "subject": "用户",
                        "predicate": "当前饮品偏好",
                        "value": "美式",
                        "origin": "user_asserted",
                        "confidence": 0.8,
                        "relation": "supersedes",
                        "related_fact_id": cited,
                        "exclusive": True,
                    }
                ]
            }
        if "茉莉奶绿" in text:
            return {
                "facts": [
                    {
                        "category": "preference",
                        "subject": "用户",
                        "predicate": "最喜欢的饮品",
                        "value": "茉莉奶绿",
                        "origin": "user_asserted",
                        "confidence": 0.8,
                        "exclusive": True,
                    }
                ]
            }
        return {"facts": []}

    for episode in episodes:
        await app.facts.extract_episode_facts(episode.id, extract=extract)
    _assign_all(app)
    chapter = _chapters(app)[0]

    payload, corpus = app.hierarchies.build_consolidation_payload(chapter)
    assert payload is not None
    facts = payload["facts"]
    by_value = {item["display_text"]: item for item in facts}
    assert len(facts) >= 2
    active = [item for item in facts if item["status"] == "active"]
    superseded = [item for item in facts if item["status"] == "superseded"]
    assert active and active[0]["valid_until"] is None
    assert superseded and superseded[0]["valid_until"] is not None
    assert superseded[0]["superseded_by"]
    assert by_value  # both ends of the change are in the payload

    # ...and a summary that expresses the change is accepted.
    async def summarize(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        return {
            "title": "饮品偏好变化",
            "summary": "这一阶段早期用户最喜欢喝茉莉奶绿，后期喝腻了改喝美式。",
            "important_preferences_or_fact_changes": [
                "早期最喜欢茉莉奶绿，后期改喝美式"
            ],
            "major_events": ["用户改喝美式"],
            "key_entities": ["茉莉奶绿", "美式"],
        }

    report = await app.hierarchies.consolidate_summary(chapter.id, summarize=summarize)
    # The chapter is still OPEN, so this text is explicitly PROVISIONAL.
    assert report["summary_status"] == "provisional"
    content = app.hierarchies.get_summary(chapter.id).structured
    assert "早期" in content.summary and "后期" in content.summary
    assert content.important_preferences_or_fact_changes

    # Closing regenerates it from the full source set as FINAL.
    app.hierarchies.close_summary(chapter.id)
    final = await app.hierarchies.consolidate_summary(chapter.id, summarize=summarize)
    assert final["summary_status"] == "ready"
    stored = app.hierarchies.get_summary(chapter.id)
    assert stored.status is SummaryStatus.CLOSED
    assert stored.structured.important_preferences_or_fact_changes


# --- D / E. thread lifecycle and relationship arc ---------------------------


@pytest.mark.anyio
async def test_d_chapter_payload_carries_the_whole_thread_lifecycle(
    app: PersonaContinuum,
) -> None:
    session_id = _session(app)
    _commit(app, session_id, "下个月我准备去重庆。", minutes=0)
    _commit(app, session_id, "票我买好了。", minutes=300)
    _commit(app, session_id, "我已经从重庆回来了。", minutes=600)
    episodes = _episodes(app)

    resolve = _resolver_for_threads()
    for episode in episodes:
        await app.threads.resolve_episode_threads(episode.id, resolve=resolve)
    thread = app.threads.list_threads(persona_id=PERSONA)[0]
    assert thread.status.value == "resolved"
    assert thread.milestones

    _assign_all(app)
    chapter = _chapters(app)[0]
    payload, _ = app.hierarchies.build_consolidation_payload(chapter)
    assert payload is not None
    threads = payload["threads"]
    assert len(threads) == 1
    events = [event["event_type"] for event in threads[0]["events"]]
    # planned -> milestone -> resolved, in that order: a summary can describe
    # the process instead of only the starting state.
    assert events[0] == "create"
    assert "milestone" in events
    assert events[-1] == "resolve"
    assert threads[0]["status"] == "resolved"

    async def summarize(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        return {
            "title": "重庆旅行计划与完成",
            "summary": "这一阶段用户完成重庆旅行：先定计划，随后买票，最后从重庆回来。",
            "resolved_threads": ["重庆旅行完成"],
            "major_events": ["票买好了", "从重庆回来了"],
        }

    report = await app.hierarchies.consolidate_summary(chapter.id, summarize=summarize)
    assert report["summary_status"] == "provisional"  # the chapter is still OPEN
    content = app.hierarchies.get_summary(chapter.id).structured
    assert content.resolved_threads == ["重庆旅行完成"]


@pytest.mark.anyio
async def test_e_chapter_payload_keeps_the_relationship_trajectory(
    app: PersonaContinuum,
) -> None:
    session_id = _session(app)
    _commit(app, session_id, "我和小陈吵架了。", minutes=0)
    _commit(app, session_id, "我们已经和好了。", minutes=300)
    _commit(app, session_id, "结果今天我们又吵起来了。", minutes=600)
    for episode in _episodes(app):
        await app.threads.resolve_episode_threads(episode.id, resolve=_resolver_for_threads())

    _assign_all(app)
    chapter = _chapters(app)[0]
    payload, _ = app.hierarchies.build_consolidation_payload(chapter)
    assert payload is not None
    events = [event["event_type"] for event in payload["threads"][0]["events"]]
    assert events == ["create", "resolve", "reopen"]

    async def summarize(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        return {
            "title": "与小陈的一次冲突与修复",
            "summary": "这一阶段两人经历了冲突、和好，随后又发生一次争吵。",
            "relationship_changes": [
                "先与小陈争吵",
                "随后和好",
                "之后又吵起来了",
            ],
        }

    report = await app.hierarchies.consolidate_summary(chapter.id, summarize=summarize)
    assert report["summary_status"] == "provisional"  # the chapter is still OPEN
    content = app.hierarchies.get_summary(chapter.id).structured
    # The trajectory is kept, not only the final state.
    assert len(content.relationship_changes) == 3
    assert "和好" in content.relationship_changes[1]


# --- F. no hallucinated facts ----------------------------------------------


@pytest.mark.anyio
async def test_f_ungrounded_summary_is_refused(app: PersonaContinuum) -> None:
    session_id = _session(app)
    _commit(app, session_id, "今天聊了聊工作。", minutes=0)
    _assign_all(app)
    chapter = _chapters(app)[0]

    async def hallucinating(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        return {
            "title": "关于用户在日本的生活",
            "summary": "用户在东京工作了 7 年，2026年5月搬到柏林。",
            "major_events": ["用户在柏林买房"],
            "key_entities": ["东京", "柏林", "2026年5月"],
        }

    report = await app.hierarchies.consolidate_summary(chapter.id, summarize=hallucinating)
    assert report["error"] == "grounding_failed"
    assert report["grounding_failures"] >= 3
    stored = app.hierarchies.get_summary(chapter.id)
    assert stored.summary_status is SummaryReadiness.FAILED
    # A refused summary is not stored as history.
    assert stored.summary == ""
    assert stored.structured.major_events == []
    failures = stored.metadata["grounding"]["failures"]
    assert {item["kind"] for item in failures} >= {
        "ungrounded_entity",
        "ungrounded_number",
        "ungrounded_statement",
    }


@pytest.mark.anyio
async def test_f2_labelled_inference_is_allowed(app: PersonaContinuum) -> None:
    """Interpretation is fine as long as it is labelled as interpretation."""

    session_id = _session(app)
    _commit(app, session_id, "最近睡不太好。", minutes=0)
    _assign_all(app)
    chapter = _chapters(app)[0]

    async def summarizing(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        return {
            "title": "睡眠状况",
            "summary": "用户提到最近睡不太好。",
            "major_events": ["用户提到睡不好"],
            "inferences": ["可能和工作压力有关"],
        }

    report = await app.hierarchies.consolidate_summary(chapter.id, summarize=summarizing)
    assert report["summary_status"] in {"ready", "provisional"}
    stored = app.hierarchies.get_summary(chapter.id)
    assert stored.structured.inferences == ["可能和工作压力有关"]
    assert stored.metadata["grounding"]["inferences"] == ["可能和工作压力有关"]


# --- G. idempotency --------------------------------------------------------


@pytest.mark.anyio
async def test_g_replaying_the_same_range_changes_nothing(app: PersonaContinuum) -> None:
    session_id = _session(app)
    for index in range(1, 4):
        _commit(app, session_id, f"第 {index} 件事。", minutes=index * 300)
    _assign_all(app)
    before = [(chapter.id, chapter.source_count) for chapter in _chapters(app)]

    replay = _assign_all(app)
    assert all(report["action"] == "noop" for report in replay)
    assert [(chapter.id, chapter.source_count) for chapter in _chapters(app)] == before

    chapter = _chapters(app)[0]
    first = await app.hierarchies.consolidate_summary(chapter.id, summarize=_summariser())
    assert first["summary_status"] == "provisional"  # still OPEN
    app.hierarchies.close_summary(chapter.id)
    final = await app.hierarchies.consolidate_summary(chapter.id, summarize=_summariser())
    assert final["summary_status"] == "ready"
    again = await app.hierarchies.consolidate_summary(chapter.id, summarize=_summariser())
    assert again["action"] == "noop"
    assert len(app.hierarchies.list_summaries(persona_id=PERSONA)) == 1


# --- H. restart ------------------------------------------------------------


@pytest.mark.anyio
async def test_h_restart_resumes_pending_consolidation(tmp_path) -> None:
    data_dir = tmp_path / "pc-hierarchy-restart"
    config = Config(data_dir=data_dir)
    app = PersonaContinuum(config, include_fake_agent=True)
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
    _commit(app, session_id, "考研的事开始准备了。", minutes=0)
    _commit(app, session_id, "买了两本参考资料。", minutes=300)
    _assign_all(app)
    chapter = _chapters(app)[0]
    app.hierarchies.close_summary(chapter.id)
    app.close()

    reopened = PersonaContinuum(config, include_fake_agent=True)
    reopened.init()
    try:
        recovery = reopened.hierarchies.recover_pending()
        assert chapter.id in recovery["pending_summary_ids"]
        report = await reopened.hierarchies.consolidate_summary(
            chapter.id, summarize=_summariser()
        )
        assert report["summary_status"] == "ready"
        assert len(reopened.hierarchies.list_summaries(persona_id=PERSONA, level=1)) == 1
        assert reopened.hierarchies.count_pending(persona_id=PERSONA) == 0
    finally:
        reopened.close()


# --- I. failure ------------------------------------------------------------


@pytest.mark.anyio
async def test_i_a_broken_summariser_leaves_everything_else_alone(
    app: PersonaContinuum,
) -> None:
    session_id = _session(app)
    _commit(app, session_id, "考研的事开始准备了。", minutes=0)
    episodes = _episodes(app)
    await app.facts.extract_episode_facts(
        episodes[0].id,
        extract=_facts_stub(),
    )
    _assign_all(app)
    chapter = _chapters(app)[0]

    async def broken(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        raise RuntimeError("provider down")

    report = await app.hierarchies.consolidate_summary(chapter.id, summarize=broken)
    assert report["error"] == "summarize_failed:RuntimeError"
    assert report["pending"] is True
    stored = app.hierarchies.get_summary(chapter.id)
    assert stored.summary_status is SummaryReadiness.FAILED
    assert stored.last_error == "summarize_failed:RuntimeError"

    # A garbage answer is a failure too, not a quiet success.
    async def garbage(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        return "not json"

    report = await app.hierarchies.consolidate_summary(chapter.id, summarize=garbage)
    assert report["error"] == "invalid_summary_payload"

    # Nothing underneath was touched, and the retry works.
    assert len(app.episodes.list_episodes(persona_id=PERSONA)) == 1
    assert len(app.facts.list_facts(persona_id=PERSONA)) == 1
    assert app.episodes.coverage(persona_id=PERSONA)["orphaned"] == 0
    retry = await app.hierarchies.consolidate_summary(chapter.id, summarize=_summariser())
    assert retry["action"] == "consolidate"


# --- J. scope isolation ----------------------------------------------------


@pytest.mark.anyio
async def test_j_chapters_never_cross_scope(app: PersonaContinuum) -> None:
    session_a = _session(app, counterpart="user_a")
    session_b = _session(app, counterpart="user_b")
    _commit(app, session_a, "考研复习第一天。", counterpart="user_a")
    _commit(app, session_b, "重庆旅行准备中。", counterpart="user_b")
    _assign_all(app, counterpart="user_a")
    _assign_all(app, counterpart="user_b")

    a = _chapters(app, counterpart="user_a")
    b = _chapters(app, counterpart="user_b")
    assert len(a) == 1 and len(b) == 1
    assert a[0].id != b[0].id
    assert a[0].source_count == 1 and b[0].source_count == 1
    assert {item.counterpart_id for item in a} == {"user_a"}

    # A different branch is a different scope even for the same counterpart.
    alt_episode_id = app.episodes.assign_turn(
        persona_id=PERSONA,
        session_id=session_a,
        turn_id="turn_alt_branch",
        counterpart_id="user_a",
        branch_id="shared_pre_divergence",
        room_id=None,
        occurred_at=BASE,
        user_message="另一条分支的第一天。",
        persona_response="嗯",
    )["current_episode_id"]
    alt_episode = app.episodes.get_episode(alt_episode_id)
    assert alt_episode is not None
    app.hierarchies.assign_episode(alt_episode)
    assert len(app.hierarchies.list_summaries(persona_id=PERSONA, level=1)) == 3
    plan = app.hierarchies.plan_long_term(
        persona_id=PERSONA, counterpart_id="user_a", branch_id="main"
    )
    assert plan["chapters_assigned"] == 0  # nothing closed to absorb yet


# --- K. provenance ---------------------------------------------------------


@pytest.mark.anyio
async def test_k_long_term_walks_all_the_way_to_raw_text(app: PersonaContinuum) -> None:
    session_id = _session(app)
    message = "下个月我准备去重庆，票还没定。"
    _commit(app, session_id, message, minutes=0)
    _commit(app, session_id, "复习也要继续。", minutes=300)
    _assign_all(app)
    chapter = _chapters(app)[0]
    app.hierarchies.close_summary(chapter.id)
    await app.hierarchies.consolidate_summary(chapter.id, summarize=_summariser())

    plan = app.hierarchies.plan_long_term(
        persona_id=PERSONA, counterpart_id="user", branch_id="main", close_segment=True
    )
    long_term_id = plan["closed_summary_id"]
    assert long_term_id
    report = await app.hierarchies.consolidate_summary(
        long_term_id, summarize=_summariser()
    )
    assert report["summary_status"] == "ready"

    inspected = app.hierarchies.inspect_summary(long_term_id)
    assert inspected is not None
    assert inspected["summary"]["level"] == 2
    assert inspected["all_sources_resolvable"] is True
    source = inspected["sources"][0]
    assert source["source_type"] == "summary"
    assert source["source_id"] == chapter.id
    assert source["episode_ids"]

    nested = app.hierarchies.inspect_summary(chapter.id)
    assert nested is not None
    episode_source = nested["sources"][0]
    assert episode_source["source_type"] == "episode"
    assert episode_source["resolvable"] is True
    episode = app.episodes.get_episode(episode_source["source_id"])
    assert episode is not None
    turns = app.episodes.episode_turns(episode.id)
    assert message in " ".join(app.episodes.resolve_turn_text(turn) for turn in turns)


# --- L. deleted provenance -------------------------------------------------


@pytest.mark.anyio
async def test_l_deleted_sources_are_visible_not_dangling(app: PersonaContinuum) -> None:
    session_id = _session(app)
    _commit(app, session_id, "考研复习第一天。", minutes=0)
    _commit(app, session_id, "考研复习第二天。", minutes=300)
    _assign_all(app)
    chapter = _chapters(app)[0]
    await app.hierarchies.consolidate_summary(chapter.id, summarize=_summariser())

    assert app.sessions.delete_session(PERSONA, session_id, delete_derived_memories=True)
    stored = app.hierarchies.get_summary(chapter.id)
    assert stored is not None
    # Every source was explicitly deleted: the chapter becomes a tombstone
    # instead of a summary that pretends to still have evidence.
    assert stored.status is SummaryStatus.RETRACTED
    assert stored.metadata["source_availability"] == "deleted"
    assert stored.metadata["retracted_reason"] == "sources_deleted"
    assert app.hierarchies.summary_sources(chapter.id) == []
    inspected = app.hierarchies.inspect_summary(chapter.id)
    assert inspected is not None
    assert inspected["source_availability"] == "deleted"
    # The Episodes themselves are gone; that is visible, not silent.
    assert app.hierarchies.count_available(persona_id=PERSONA) == 0


# --- M. never in the prompt -------------------------------------------------


@pytest.mark.anyio
async def test_m_summaries_are_stored_but_not_injected(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config(data_dir=tmp_path / "pc-hierarchy-room", phase8_context_assembly=False)
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

    async def structured(
        binding: Any,
        *,
        system_prompt: str,
        user_message: str,
        schema: dict[str, Any],
        phase: str,
    ) -> Any:
        if phase == "memory_thread_resolution":
            return _StructuredResult({"threads": []})
        if phase == "memory_fact_extraction":
            return _StructuredResult({"facts": []})
        if phase == "memory_hierarchy_summary":
            payload = json.loads(user_message)
            return _StructuredResult(_grounded_summary(payload, schema))
        return _StructuredResult({"title": "考研复习", "summary": "用户开始准备考研。"})

    monkeypatch.setattr(room_app.orchestrator.runtime_executor, "execute_structured", structured)
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
        assert task is not None
        await task

        # The room pass grouped the Episode into a Chapter and summarised it.
        chapters = room_app.hierarchies.list_summaries(persona_id=PERSONA, level=1)
        assert len(chapters) == 1
        assert chapters[0].summary_status is SummaryReadiness.PROVISIONAL
        assert chapters[0].metadata["open_reason"] == "first_source"

        events = [
            event
            async for event in room_app.orchestrator.step_turn(room.id, user_message="第二句")
        ]
        report = next(event for event in events if event.get("event") == "room_context_report")
        # Stored, real, and contributing exactly nothing to the prompt.
        assert report["hierarchical_summaries_available"] == 1
        assert report["hierarchical_summary_tokens"] == 0
        assert report["summary_injection"] == "phase8_not_enabled"
        # The OPEN chapter already carries a current PROVISIONAL text, so it
        # owes no further work until its range changes or it closes.
        assert report["hierarchical_summaries_pending"] == 0
    finally:
        room_app.close()


# --- N. bounded backfill ----------------------------------------------------


@pytest.mark.anyio
async def test_n_backfill_is_bounded_and_model_free(app: PersonaContinuum) -> None:
    session_id = _session(app)
    for index in range(1, 6):
        _commit(app, session_id, f"第 {index} 天。", minutes=index * 300)
    assert len(app.hierarchies.ungrouped_episodes(limit=50)) == 5

    first = app.hierarchies.backfill(limit=2)
    assert first["processed"] == 2
    assert first["created"] == 1
    assert first["appended"] == 1
    assert first["remaining"] == 3
    assert app.hierarchies.count_pending(persona_id=PERSONA) == 1
    # Only grouping ran: no Chapter was summarised.
    chapter = _chapters(app)[0]
    assert chapter.summary_status is SummaryReadiness.PENDING
    assert chapter.summary == ""


# --- O. long-term increment -------------------------------------------------


@pytest.mark.anyio
async def test_o_long_term_segment_grows_and_then_finalises(app: PersonaContinuum) -> None:
    session_id = _session(app)
    for index in range(1, 4):
        _commit(app, session_id, f"考研第 {index} 天。", minutes=index * 300)
    _assign_all(app)
    first = _chapters(app)[0]
    app.hierarchies.close_summary(first.id)
    await app.hierarchies.consolidate_summary(first.id, summarize=_summariser())
    plan = app.hierarchies.plan_long_term(
        persona_id=PERSONA, counterpart_id="user", branch_id="main"
    )
    assert plan["chapters_assigned"] == 1
    segment = app.hierarchies.get_summary(plan["summary_id"])
    assert segment is not None and segment.level == 2 and segment.is_open

    # A later phase closes another chapter and joins the same segment.
    for index in range(4, 7):
        _commit(app, session_id, f"重庆准备第 {index} 步。", minutes=100000 + index * 300)
    _assign_all(app)
    second = next(chapter for chapter in _chapters(app) if chapter.id != first.id)
    app.hierarchies.close_summary(second.id)
    await app.hierarchies.consolidate_summary(second.id, summarize=_summariser())
    plan = app.hierarchies.plan_long_term(
        persona_id=PERSONA, counterpart_id="user", branch_id="main"
    )
    segment = app.hierarchies.get_summary(plan["summary_id"])
    assert segment is not None
    assert segment.source_count == 2

    closed = app.hierarchies.plan_long_term(
        persona_id=PERSONA,
        counterpart_id="user",
        branch_id="main",
        close_segment=True,
    )
    assert closed["closed_summary_id"] == segment.id
    report = await app.hierarchies.consolidate_summary(
        closed["closed_summary_id"], summarize=_summariser()
    )
    assert report["summary_status"] == "ready"
    assert report["level"] == 2
    assert app.hierarchies.stats(persona_id=PERSONA)["by_level"] == {"1": 2, "2": 1}


# --- P. CLI -----------------------------------------------------------------


@pytest.mark.anyio
async def test_p_cli_inspects_the_hierarchy(app: PersonaContinuum) -> None:
    from typer.testing import CliRunner

    from persona_continuum.cli.app import app as cli_app

    session_id = _session(app)
    _commit(app, session_id, "考研复习第一天。", minutes=0)
    _assign_all(app)
    chapter = _chapters(app)[0]
    await app.hierarchies.consolidate_summary(chapter.id, summarize=_summariser())
    runner = CliRunner()

    listing = runner.invoke(cli_app, ["memory", "summaries", "--persona", PERSONA])
    assert listing.exit_code == 0, listing.output
    assert chapter.id in listing.output
    assert "L1/chapter" in listing.output

    inspected = runner.invoke(
        cli_app, ["memory", "summary", chapter.id, "--sources", "--json"]
    )
    assert inspected.exit_code == 0, inspected.output
    payload = json.loads(inspected.output)
    assert payload["summary"]["level"] == 1
    assert payload["sources"][0]["source_type"] == "episode"

    backfill = runner.invoke(cli_app, ["memory", "hierarchy-backfill", "--limit", "5"])
    assert backfill.exit_code == 0, backfill.output
    assert "remaining=0" in backfill.output


# --- helpers ----------------------------------------------------------------


def _episode_summariser() -> Any:
    """Episode summaries with distinct, topic-bearing titles."""

    async def summarize(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        text = " ".join(str(turn.get("text") or "") for turn in payload.get("turns") or [])
        if "爬山" in text:
            return {
                "title": "爬山",
                "summary": "用户今天去爬山了。",
                "topics": ["爬山"],
                "entities": ["山"],
            }
        if "考研" in text:
            return {
                "title": "考研准备",
                "summary": "用户开始准备考研。",
                "topics": ["考研"],
                "entities": ["考研"],
            }
        return {"title": "闲聊", "summary": "用户随便聊了几句。", "topics": ["闲聊"]}

    return summarize


def _facts_stub() -> Any:
    async def extract(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        return {
            "facts": [
                {
                    "category": "goal",
                    "subject": "用户",
                    "predicate": "当前目标",
                    "value": "准备考研",
                    "origin": "user_asserted",
                    "confidence": 0.7,
                }
            ]
        }

    return extract


def _resolver_for_threads() -> Any:
    """A deterministic stand-in for the thread resolver.

    It decides from the CANDIDATE LIST first, exactly like the real resolver
    must: "票我买好了。" shares no keyword with "重庆旅行".
    """

    async def resolve(payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        text = " ".join(str(turn.get("text") or "") for turn in payload.get("turns") or [])
        candidates = {
            str(item["thread_id"]): item for item in payload.get("existing_threads") or []
        }

        def find(needle: str) -> str | None:
            for thread_id, item in candidates.items():
                if needle in f"{item.get('title')} {item.get('summary')}":
                    return thread_id
            return None

        def first_of_type(thread_type: str) -> str | None:
            for thread_id, item in candidates.items():
                if str(item.get("thread_type")) == thread_type:
                    return thread_id
            return None

        def action(operation: str, thread_id: str | None, **extra: Any) -> dict[str, Any]:
            payload_action: dict[str, Any] = {
                "operation": operation,
                "thread_id": thread_id,
                "confidence": 0.8,
                "reason": "scripted resolver",
            }
            payload_action.update(extra)
            return payload_action

        plan_thread = find("重庆") or first_of_type("plan")
        if plan_thread is not None:
            if "从重庆回来" in text:
                return {"threads": [action("resolve", plan_thread)]}
            if "票" in text:
                return {
                    "threads": [action("milestone", plan_thread, milestone="tickets")]
                }
        relationship_thread = find("小陈") or first_of_type("relationship_issue")
        if relationship_thread is not None:
            if "和好" in text:
                return {"threads": [action("resolve", relationship_thread)]}
            if "又吵" in text:
                return {"threads": [action("reopen", relationship_thread)]}
            if "又联系我" in text:
                return {"threads": [action("update", relationship_thread)]}
        if "重庆" in text:
            return {"threads": [action("create", None, thread_type="plan", title="重庆旅行")]}
        if "小陈" in text:
            return {
                "threads": [
                    action("create", None, thread_type="relationship_issue", title="小陈冲突")
                ]
            }
        return {"threads": []}

    return resolve
