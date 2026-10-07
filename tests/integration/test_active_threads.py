"""Phase 5 acceptance: Active Threads.

Scenarios A-L from the Phase 5 brief, plus the failure/retry and lineage
contracts.  The resolver is scripted so these tests are about the STORE and the
LIFECYCLE (identity, continuation, resolution, reopen, ambiguity safety,
idempotency, restart, provenance, scope isolation) rather than about model
quality; ``scripts/phase5_active_threads.py`` is the real-model counterpart.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.domain.thread import (
    ThreadEventType,
    ThreadStatus,
    ThreadType,
)
from persona_continuum.room.models import ParticipantSlot, RoomMode

PERSONA = "su_he"
BASE = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


class _StructuredResult:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value


# --- scripted resolver ------------------------------------------------------


def _thread_id(payload: dict[str, Any], needle: str) -> str | None:
    """Stand-in for semantic matching: which live thread is this about?"""

    for thread in payload.get("existing_threads") or []:
        haystack = f"{thread['title']} {thread['summary']} {thread['thread_id']}"
        if needle in haystack:
            return str(thread["thread_id"])
    return None


def _create(thread_type: str, title: str, **overrides: Any) -> dict[str, Any]:
    action: dict[str, Any] = {
        "operation": "create",
        "thread_type": thread_type,
        "title": title,
        "summary": overrides.pop("summary", ""),
        "confidence": 0.8,
        "reason": "resolver decision",
    }
    action.update(overrides)
    return action


def _act(operation: str, thread_id: str | None, **overrides: Any) -> dict[str, Any]:
    action: dict[str, Any] = {
        "operation": operation,
        "thread_id": thread_id,
        "confidence": 0.8,
        "reason": "resolver decision",
    }
    action.update(overrides)
    return action


def _resolver(rules: list[tuple[str, Any]]) -> Any:
    """First marker that appears in the episode decides the whole answer."""

    async def resolve(payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        text = " ".join(str(turn.get("text") or "") for turn in payload.get("turns") or [])
        for marker, build in rules:
            if marker in text:
                return {"threads": build(payload)}
        return {"threads": []}

    return resolve


# --- fixtures / helpers -----------------------------------------------------


@pytest.fixture()
def app(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Any:
    data_dir = tmp_path / "pc-threads"
    # The CLI's ``build()`` reads the same variable, so an operator command and
    # the app under test can never disagree about which store they mean.
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


def _session(app: PersonaContinuum, counterpart: str = "user") -> str:
    return app.sessions.start_session(
        persona_id=PERSONA, title="t", counterpart_id=counterpart
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


async def _resolve_all(
    app: PersonaContinuum, resolve: Any, *, counterpart: str = "user", force: bool = False
) -> list[dict[str, Any]]:
    reports = []
    for episode in _episodes(app, counterpart=counterpart):
        reports.append(
            await app.threads.resolve_episode_threads(
                episode.id, resolve=resolve, force=force
            )
        )
    return reports


def _threads(app: PersonaContinuum, *, counterpart: str = "user") -> list[Any]:
    return app.threads.list_threads(
        persona_id=PERSONA, counterpart_id=counterpart, limit=50
    )


def _only(app: PersonaContinuum, *, counterpart: str = "user") -> Any:
    threads = _threads(app, counterpart=counterpart)
    assert len(threads) == 1, [thread.title for thread in threads]
    return threads[0]


# --- A. weak-keyword continuation across a long gap -------------------------


@pytest.mark.anyio
async def test_a_travel_thread_survives_unrelated_episodes(app: PersonaContinuum) -> None:
    session_id = _session(app)
    _commit(app, session_id, "下个月我准备去重庆。", minutes=0)
    # Many unrelated episodes in between, each in its own sitting.
    unrelated = [
        "今天打游戏输了一晚上。",
        "工作上有点烦。",
        "刚看了一部电影，很好看。",
        "天气突然变冷了。",
        "最近在学做菜。",
        "朋友推荐了一个新的 AI 工具。",
    ]
    for index, message in enumerate(unrelated, start=1):
        _commit(app, session_id, message, minutes=index * 240)
    _commit(app, session_id, "票我买好了。", minutes=len(unrelated) * 240 + 240)

    resolve = _resolver(
        [
            ("重庆", lambda payload: [_create("plan", "重庆旅行", summary="下个月去重庆")]),
            (
                "票我买好了",
                lambda payload: [
                    _act(
                        "milestone",
                        _thread_id(payload, "重庆"),
                        milestone="tickets_purchased",
                        summary="用户已买好票",
                        confidence=0.75,
                    )
                ],
            ),
        ]
    )
    reports = await _resolve_all(app, resolve)

    assert reports[0]["threads_created"] == 1
    assert reports[-1]["threads_milestoned"] == 1
    # The whole point: "票我买好了。" continued 重庆旅行 instead of starting a
    # brand-new "买票" thread.
    assert len(_threads(app)) == 1
    thread = _only(app)
    assert thread.thread_type is ThreadType.PLAN
    assert thread.status is ThreadStatus.ACTIVE
    assert thread.milestones == ["tickets_purchased"]
    # ...and the thread did not lose its origin: it has to resolve back to BOTH
    # episodes, seven unrelated episodes apart.
    inspected = app.threads.inspect_thread(thread.id)
    assert inspected is not None
    assert len(inspected["source_episode_ids"]) == 2
    assert inspected["all_sources_resolvable"] is True
    assert [event["event_type"] for event in inspected["events"]] == [
        "create",
        "milestone",
    ]


# --- B/C/D. relationship thread lifecycle -----------------------------------


@pytest.mark.anyio
async def test_b_c_d_relationship_thread_updates_resolves_and_reopens(
    app: PersonaContinuum,
) -> None:
    session_id = _session(app)
    # Each sitting is its own Episode (idle gap > 180 minutes on purpose).
    _commit(app, session_id, "我和小陈吵架了。", minutes=0)
    _commit(app, session_id, "她今天又联系我了。", minutes=300)
    _commit(app, session_id, "我们已经和好了，这件事算过去了。", minutes=600)

    resolve = _resolver(
        [
            ("吵架了", lambda payload: [_create("relationship_issue", "和小陈的矛盾")]),
            (
                "又联系我了",
                lambda payload: [
                    _act("update", _thread_id(payload, "小陈"), summary="小陈主动联系")
                ],
            ),
            (
                "和好了",
                lambda payload: [
                    _act("resolve", _thread_id(payload, "小陈"), summary="两人和好")
                ],
            ),
        ]
    )
    reports = await _resolve_all(app, resolve)
    assert [report.get("threads_created") for report in reports[:1]] == [1]
    assert any(report.get("threads_updated") for report in reports)
    assert any(report.get("threads_resolved") for report in reports)

    thread = _only(app)
    assert thread.status is ThreadStatus.RESOLVED
    assert thread.resolved_at is not None
    events = app.threads.thread_events(thread.id)
    assert [event.event_type for event in events] == [
        ThreadEventType.CREATE,
        ThreadEventType.UPDATE,
        ThreadEventType.RESOLVE,
    ]

    # D. Reopen: the same matter revives in place, it does not fork #2.
    _commit(app, session_id, "结果今天我们又吵起来了。", minutes=900)
    reopen = _resolver(
        [
            (
                "又吵起来了",
                lambda payload: [
                    _act("reopen", _thread_id(payload, "小陈"), summary="再次争吵")
                ],
            )
        ]
    )
    report = await app.threads.resolve_episode_threads(
        _episodes(app)[-1].id, resolve=reopen
    )
    assert report["threads_reopened"] == 1
    assert len(_threads(app)) == 1
    thread = _only(app)
    assert thread.status is ThreadStatus.ACTIVE
    assert thread.resolved_at is None
    assert [event.event_type for event in app.threads.thread_events(thread.id)][-1] is (
        ThreadEventType.REOPEN
    )


@pytest.mark.anyio
async def test_d2_a_long_dead_thread_revives_as_a_linked_new_thread(
    app: PersonaContinuum,
) -> None:
    """Past the reopen window it is a new episode of the subject -- linked, not lost."""

    session_id = _session(app)
    _commit(app, session_id, "我和小陈吵架了。", minutes=0)
    _commit(app, session_id, "我们已经和好了。", minutes=300)
    resolve = _resolver(
        [
            ("吵架了", lambda payload: [_create("relationship_issue", "和小陈的矛盾")]),
            (
                "和好了",
                lambda payload: [
                    _act("resolve", _thread_id(payload, "小陈"), summary="和好")
                ],
            ),
        ]
    )
    await _resolve_all(app, resolve)
    original = _only(app)
    assert original.status is ThreadStatus.RESOLVED

    # 120 days later the same subject comes back.  It is long outside the
    # reopen window, so the resolver proposes a NEW thread for the same subject.
    _commit(app, session_id, "我和小陈又闹翻了。", minutes=120 * 24 * 60)
    late = _resolver(
        [
            (
                "又闹翻了",
                lambda payload: [
                    _create("relationship_issue", "和小陈的矛盾", summary="再次闹翻")
                ],
            )
        ]
    )
    report = await app.threads.resolve_episode_threads(_episodes(app)[-1].id, resolve=late)
    assert report["threads_created"] == 1
    assert report["threads_merged"] == 0
    threads = _threads(app)
    assert len(threads) == 2
    revived = next(thread for thread in threads if thread.status is ThreadStatus.ACTIVE)
    assert revived.related_previous_thread_id == original.id
    assert app.threads.get_thread(original.id).status is ThreadStatus.RESOLVED
    inspected = app.threads.inspect_thread(revived.id)
    assert inspected is not None
    assert inspected["preceded_by"]["id"] == original.id


# --- E. ambiguous pronoun ---------------------------------------------------


@pytest.mark.anyio
async def test_e_ambiguous_pronoun_records_candidates_instead_of_binding(
    app: PersonaContinuum,
) -> None:
    session_id = _session(app)
    for index, message in enumerate(
        ["我和小陈吵架了。", "小王跟我借钱一直没还。", "前女友的事情让我很烦。"], start=1
    ):
        _commit(app, session_id, message, minutes=index * 240)
    seed = _resolver(
        [
            ("小陈", lambda payload: [_create("relationship_issue", "和小陈的矛盾")]),
            ("小王", lambda payload: [_create("task", "小王借钱")]),
            ("前女友", lambda payload: [_create("relationship_issue", "前女友的事")]),
        ]
    )
    await _resolve_all(app, seed)
    assert len(_threads(app)) == 3

    before = {
        thread.id: (thread.status, thread.updated_at, thread.summary)
        for thread in _threads(app)
    }
    _commit(app, session_id, "她联系我了。", minutes=24 * 60)
    ambiguous = _resolver(
        [
            (
                "她联系我了",
                lambda payload: [
                    _act(
                        "noop",
                        None,
                        confidence=0.7,
                        reason="多个候选同样合理",
                        ambiguous_thread_ids=[
                            str(thread["thread_id"])
                            for thread in payload["existing_threads"]
                        ],
                    )
                ],
            )
        ]
    )
    report = await app.threads.resolve_episode_threads(
        _episodes(app)[-1].id, resolve=ambiguous
    )
    assert report["candidates_recorded"] == 1  # one undecided decision
    assert report["candidate_links"] == 3  # pointing at three plausible threads
    # Nothing was rebound on a guess.
    after = {
        thread.id: (thread.status, thread.updated_at, thread.summary)
        for thread in _threads(app)
    }
    assert after == before
    pending = app.threads.pending_link_candidates()
    assert {row["thread_id"] for row in pending if row["status"] == "pending"} == set(before)


@pytest.mark.anyio
async def test_e2_low_confidence_continuation_becomes_a_candidate(
    app: PersonaContinuum,
) -> None:
    session_id = _session(app)
    _commit(app, session_id, "下个月我准备去重庆。", minutes=0)
    await _resolve_all(app, _resolver([("重庆", lambda payload: [_create("plan", "重庆旅行")])]))
    thread = _only(app)
    before_summary = thread.summary

    _commit(app, session_id, "也许吧。", minutes=300)
    weak = _resolver(
        [
            (
                "也许吧",
                lambda payload: [
                    _act(
                        "update",
                        _thread_id(payload, "重庆"),
                        confidence=0.3,
                        summary="不确定的续接",
                    )
                ],
            )
        ]
    )
    report = await app.threads.resolve_episode_threads(_episodes(app)[-1].id, resolve=weak)
    assert report["candidates_recorded"] == 1
    assert report["threads_updated"] == 0
    assert _only(app).summary == before_summary
    assert app.threads.pending_link_candidates()[0]["thread_id"] == thread.id


# --- F. casual chat ---------------------------------------------------------


@pytest.mark.anyio
async def test_f_casual_chat_creates_no_thread(app: PersonaContinuum) -> None:
    session_id = _session(app)
    _commit(app, session_id, "今天晚饭挺好吃。", minutes=0)
    _commit(app, session_id, "吃完打算早点睡。", minutes=20)
    reports = await _resolve_all(app, _resolver([]))
    assert all(report["action"] == "resolve" for report in reports)
    assert all(report["threads_created"] == 0 for report in reports)
    assert _threads(app) == []
    assert app.threads.stats(persona_id=PERSONA)["threads"] == 0


# --- G. project continuity --------------------------------------------------


@pytest.mark.anyio
async def test_g_project_thread_resolves_when_the_bug_is_fixed(
    app: PersonaContinuum,
) -> None:
    session_id = _session(app)
    _commit(app, session_id, "Persona Continuum 的记忆模块还有 bug。", minutes=0)
    for index in range(1, 5):
        _commit(app, session_id, f"今天聊点别的 {index}。", minutes=index * 300)
    _commit(app, session_id, "之前那个问题已经修掉了。", minutes=5 * 300)

    resolve = _resolver(
        [
            (
                "记忆模块还有 bug",
                lambda payload: [_create("project", "Persona Continuum 记忆模块 bug")],
            ),
            (
                "已经修掉了",
                lambda payload: [
                    _act("resolve", _thread_id(payload, "记忆模块"), summary="bug 已修复")
                ],
            ),
        ]
    )
    reports = await _resolve_all(app, resolve)
    assert reports[0]["threads_created"] == 1
    assert reports[-1]["threads_resolved"] == 1
    thread = _only(app)
    assert thread.status is ThreadStatus.RESOLVED
    assert thread.thread_type is ThreadType.PROJECT
    assert len(app.threads.thread_sources(thread.id)) >= 2


# --- H. idempotency ---------------------------------------------------------


@pytest.mark.anyio
async def test_h_replaying_an_episode_changes_nothing(app: PersonaContinuum) -> None:
    session_id = _session(app)
    _commit(app, session_id, "下个月我准备去重庆。", minutes=0)
    resolve = _resolver(
        [("重庆", lambda payload: [_create("plan", "重庆旅行", summary="下个月去重庆")])]
    )
    episode = _episodes(app)[0]
    first = await app.threads.resolve_episode_threads(episode.id, resolve=resolve)
    assert first["threads_created"] == 1
    thread = _only(app)
    before = {
        "threads": app.threads.stats(persona_id=PERSONA)["threads"],
        "events": len(app.threads.thread_events(thread.id)),
        "sources": len(app.threads.thread_sources(thread.id)),
        "confidence": thread.confidence,
        "activity": thread.last_activity_at,
    }

    second = await app.threads.resolve_episode_threads(episode.id, resolve=resolve, force=True)
    assert second["threads_created"] == 0
    assert second["threads_merged"] == 0
    assert second["replayed"] == 1  # this Episode already produced this thread
    after = {
        "threads": app.threads.stats(persona_id=PERSONA)["threads"],
        "events": len(app.threads.thread_events(thread.id)),
        "sources": len(app.threads.thread_sources(thread.id)),
        "confidence": _only(app).confidence,
        "activity": _only(app).last_activity_at,
    }
    assert after == before
    assert len(_threads(app)) == 1


@pytest.mark.anyio
async def test_h2_replaying_a_milestone_does_not_duplicate_it(app: PersonaContinuum) -> None:
    session_id = _session(app)
    _commit(app, session_id, "下个月我准备去重庆。", minutes=0)
    _commit(app, session_id, "票我买好了。", minutes=300)
    resolve = _resolver(
        [
            ("重庆", lambda payload: [_create("plan", "重庆旅行")]),
            (
                "票我买好了",
                lambda payload: [
                    _act("milestone", _thread_id(payload, "重庆"), milestone="tickets")
                ],
            ),
        ]
    )
    await _resolve_all(app, resolve)
    thread = _only(app)
    assert thread.milestones == ["tickets"]
    events = len(app.threads.thread_events(thread.id))

    await _resolve_all(app, resolve, force=True)
    assert _only(app).milestones == ["tickets"]
    assert len(app.threads.thread_events(thread.id)) == events
    assert len(_threads(app)) == 1


# --- I. restart -------------------------------------------------------------


@pytest.mark.anyio
async def test_i_restart_resumes_pending_resolution_without_duplicates(tmp_path) -> None:
    data_dir = tmp_path / "pc-threads-restart"
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
    _commit(app, session_id, "下个月我准备去重庆。", minutes=0)
    resolve = _resolver(
        [("重庆", lambda payload: [_create("plan", "重庆旅行", summary="下个月去重庆")])]
    )
    episode = _episodes(app)[0]
    await app.threads.resolve_episode_threads(episode.id, resolve=resolve)
    thread = _only(app)
    # Simulate a crash between "thread created" and "episode marked ready".
    app.database.conn.execute(
        "UPDATE memory_episodes SET thread_resolution_status = 'pending' WHERE id = ?",
        (episode.id,),
    )
    app.database.conn.commit()
    app.close()

    reopened = PersonaContinuum(config, include_fake_agent=True)
    reopened.init()
    try:
        recovery = reopened.threads.recover_pending()
        assert episode.id in recovery["pending_episode_ids"]
        report = await reopened.threads.resolve_episode_threads(episode.id, resolve=resolve)
        assert report["threads_created"] == 0
        threads = reopened.threads.list_threads(persona_id=PERSONA)
        assert len(threads) == 1
        assert threads[0].id == thread.id
        assert len(reopened.threads.thread_events(thread.id)) == 1
        assert reopened.threads.pending_resolution_episodes() == []
    finally:
        reopened.close()


# --- J. scope isolation -----------------------------------------------------


@pytest.mark.anyio
async def test_j_scope_isolation_between_counterparts_and_branches(app: PersonaContinuum) -> None:
    session_a = _session(app, counterpart="user_a")
    session_b = _session(app, counterpart="user_b")
    _commit(app, session_a, "下个月我准备去重庆。", counterpart="user_a")
    _commit(app, session_b, "我准备下个月去海南。", counterpart="user_b")

    resolve = _resolver(
        [
            ("重庆", lambda payload: [_create("plan", "重庆旅行")]),
            ("海南", lambda payload: [_create("plan", "海南旅行")]),
        ]
    )
    await _resolve_all(app, resolve, counterpart="user_a")
    await _resolve_all(app, resolve, counterpart="user_b")
    a_threads = _threads(app, counterpart="user_a")
    b_threads = _threads(app, counterpart="user_b")
    assert [thread.title for thread in a_threads] == ["重庆旅行"]
    assert [thread.title for thread in b_threads] == ["海南旅行"]

    # A resolver that cites the OTHER counterpart's thread must not bind it.
    a_thread_id = a_threads[0].id
    _commit(app, session_b, "对了，还想说一件事。", minutes=300, counterpart="user_b")
    cross = _resolver(
        [
            (
                "还想说一件事",
                lambda payload: [_act("update", a_thread_id, summary="越界续接")],
            )
        ]
    )
    report = await app.threads.resolve_episode_threads(
        _episodes(app, counterpart="user_b")[-1].id, resolve=cross
    )
    assert report["skipped_invalid"] == 1
    assert report["threads_updated"] == 0
    assert _threads(app, counterpart="user_a")[0].summary == ""
    assert len(_threads(app, counterpart="user_b")) == 1

    # Branch is part of the scope too: the same counterpart on another branch
    # is a different world, and the store's queries keep them apart.
    assert app.threads.live_threads(
        persona_id=PERSONA, counterpart_id="user_a", branch_id="main"
    )
    assert not app.threads.live_threads(
        persona_id=PERSONA, counterpart_id="user_a", branch_id="shared_pre_divergence"
    )


# --- K. provenance ---------------------------------------------------------


@pytest.mark.anyio
async def test_k_provenance_walks_back_to_the_raw_text(app: PersonaContinuum) -> None:
    session_id = _session(app)
    message = "下个月我准备去重庆，机票还没定。"
    turn_id = _commit(app, session_id, message, minutes=0)
    resolve = _resolver(
        [
            (
                "重庆",
                lambda payload: [
                    _create(
                        "plan",
                        "重庆旅行",
                        source_turn_ids=[payload["turns"][0]["turn_id"]],
                    )
                ],
            )
        ]
    )
    await _resolve_all(app, resolve)
    thread = _only(app)
    inspected = app.threads.inspect_thread(thread.id)
    assert inspected is not None
    event = inspected["events"][0]
    assert event["source_episode_id"] == _episodes(app)[0].id
    assert event["source_turn_id"] == turn_id

    # Thread -> Event -> Episode -> Turn -> the raw committed text.
    sources = [item for item in inspected["sources"] if item["turn_id"] == turn_id]
    assert sources and sources[0]["resolvable"] is True
    episode = app.episodes.get_episode(event["source_episode_id"])
    assert episode is not None
    episode_turn = next(
        item for item in app.episodes.episode_turns(episode.id) if item.turn_id == turn_id
    )
    assert message in app.episodes.resolve_turn_text(episode_turn)


# --- L. deleted provenance --------------------------------------------------


@pytest.mark.anyio
async def test_l_deleted_source_is_stamped_not_silently_dangling(app: PersonaContinuum) -> None:
    session_id = _session(app)
    _commit(app, session_id, "下个月我准备去重庆。", minutes=0)
    await _resolve_all(app, _resolver([("重庆", lambda payload: [_create("plan", "重庆旅行")])]))
    thread = _only(app)

    assert app.sessions.delete_session(PERSONA, session_id, delete_derived_memories=True)

    # The thread and its history survive, and the loss of the raw source is
    # explicitly visible rather than looking healthy until someone clicks.
    surviving = app.threads.get_thread(thread.id)
    assert surviving is not None
    assert surviving.metadata["provenance_availability"] == "deleted"
    inspected = app.threads.inspect_thread(thread.id)
    assert inspected is not None
    assert inspected["events"][0]["source_availability"] == "deleted"
    assert inspected["all_sources_resolvable"] is False
    assert all(item["excerpt_available"] is False for item in inspected["sources"])


# --- M. not in the prompt ---------------------------------------------------


@pytest.mark.anyio
async def test_m_threads_are_stored_but_not_injected_into_the_prompt(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config(data_dir=tmp_path / "pc-threads-room", phase8_context_assembly=False)
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
    phases: list[str] = []

    async def structured(
        binding: Any,
        *,
        system_prompt: str,
        user_message: str,
        schema: dict[str, Any],
        phase: str,
    ) -> Any:
        phases.append(phase)
        if phase == "memory_episode_consolidation":
            return _StructuredResult(
                {"title": "重庆计划", "summary": "用户打算下个月去重庆。", "importance": 0.6}
            )
        if phase == "memory_fact_extraction":
            return _StructuredResult({"facts": []})
        if phase == "memory_hierarchy_summary":
            # Phase 6 groups the Episode into a Chapter; this test is about
            # Threads, so the Chapter text is irrelevant here.
            payload = json.loads(user_message)
            sources = payload.get("sources") or []
            titles = [str(item.get("title") or "") for item in sources]
            return _StructuredResult(
                {
                    "title": "、".join(titles[:2]) or "阶段",
                    "summary": "、".join(title for title in titles if title),
                    "major_events": [title for title in titles[:2] if title],
                }
            )
        assert phase == "memory_thread_resolution"
        return _StructuredResult(
            {
                "threads": [
                    {
                        "operation": "create",
                        "thread_type": "plan",
                        "title": "重庆旅行",
                        "summary": "用户打算下个月去重庆。",
                        "confidence": 0.8,
                        "reason": "明确的未来计划",
                    }
                ]
            }
        )

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
            room.id, user_message="下个月我准备去重庆。"
        ):
            pass
        task = room_app.orchestrator._episode_tasks.get(room.id)
        assert task is not None
        await task
        assert phases == [
            "memory_episode_consolidation",
            "memory_fact_extraction",
            "memory_thread_resolution",
            "memory_hierarchy_summary",
        ]

        events = [
            event
            async for event in room_app.orchestrator.step_turn(room.id, user_message="第二句")
        ]
        report = next(event for event in events if event.get("event") == "room_context_report")
        assert report["threads_selected"] == 0
        assert report["thread_tokens"] == 0
        assert report["thread_injection"] == "phase8_not_enabled"
        # ...while the real store statistic is visible: stored, not injected.
        assert report["threads_available"] == 1
        assert report["threads_pending_resolution"] == 0
        reports = [
            event for event in events if event.get("event") == "memory_thread_report"
        ]
        assert room_app.threads.stats(persona_id=PERSONA)["live"] == 1
        assert reports == []  # nothing left to resolve, so no extra model call
    finally:
        room_app.close()


# --- failure / retry --------------------------------------------------------


@pytest.mark.anyio
async def test_r_room_thread_resolver_failure_does_not_break_chat(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken Thread resolver is background work: the reply still lands."""

    config = Config(data_dir=tmp_path / "pc-threads-broken")
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
            raise RuntimeError("thread provider down")
        if phase == "memory_fact_extraction":
            return _StructuredResult({"facts": []})
        return _StructuredResult({"title": "重庆计划", "summary": "用户打算去重庆。"})

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
        events = [
            event
            async for event in room_app.orchestrator.step_turn(
                room.id, user_message="下个月我准备去重庆。"
            )
        ]
        assert "turn_completed" in [event.get("event") for event in events]
        task = room_app.orchestrator._episode_tasks.get(room.id)
        assert task is not None
        await task

        # The failure is recorded on the Episode and stays retryable.
        episode = room_app.episodes.list_episodes(persona_id=PERSONA, room_id=room.id)[0]
        stored = room_app.episodes.get_episode(episode.id)
        assert stored.thread_resolution_status == "failed"
        assert stored.thread_resolution_error.startswith("resolve_failed")
        assert [item.id for item in room_app.threads.pending_resolution_episodes()] == [episode.id]
        assert room_app.threads.stats(persona_id=PERSONA)["threads"] == 0

        # ...and a later pass picks the same Episode up and succeeds.
        async def ok(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
            return {
                "threads": [
                    {
                        "operation": "create",
                        "thread_type": "plan",
                        "title": "重庆旅行",
                        "confidence": 0.8,
                    }
                ]
            }

        report = await room_app.threads.resolve_episode_threads(episode.id, resolve=ok)
        assert report["threads_created"] == 1
        assert room_app.threads.pending_resolution_episodes() == []
    finally:
        room_app.close()


@pytest.mark.anyio
async def test_s_backfill_is_bounded_and_lazy(app: PersonaContinuum) -> None:
    """A pre-Phase-5 backlog is drained in bounded steps, never all at once."""

    session_id = _session(app)
    for index in range(1, 6):
        _commit(app, session_id, f"第 {index} 件事。", minutes=index * 300)
    assert len(_episodes(app)) == 5

    view = app.threads.backfill(limit=2)
    assert view["batch"] == 2
    assert view["remaining"] == 3
    assert len(view["episode_ids"]) == 2
    assert app.threads.count_pending_resolution(persona_id=PERSONA) == 5
    # Nothing ran a model: the backlog is only reported, and no thread exists.
    assert _threads(app) == []


@pytest.mark.anyio
async def test_t_cli_inspection_reaches_the_raw_conversation(app: PersonaContinuum) -> None:
    """The debug surface an operator actually uses (Phase 5 §32)."""

    from typer.testing import CliRunner

    from persona_continuum.cli.app import app as cli_app

    session_id = _session(app)
    message = "下个月我准备去重庆。"
    _commit(app, session_id, message, minutes=0)
    await _resolve_all(app, _resolver([("重庆", lambda payload: [_create("plan", "重庆旅行")])]))
    thread = _only(app)
    runner = CliRunner()

    listing = runner.invoke(cli_app, ["memory", "threads", "--persona", PERSONA])
    assert listing.exit_code == 0, listing.output
    assert "重庆旅行" in listing.output
    assert thread.id in listing.output
    assert "live=1" in listing.output

    inspected = runner.invoke(cli_app, ["memory", "thread", thread.id, "--events", "--sources"])
    assert inspected.exit_code == 0, inspected.output
    for expected in ("status: active", "plan", thread.id, "all_sources_resolvable: True"):
        assert expected in inspected.output

    backlog = runner.invoke(cli_app, ["memory", "thread-backfill", "--limit", "3"])
    assert backlog.exit_code == 0, backlog.output
    assert "total_pending=0" in backlog.output

    # The provenance chain the inspector reports is real: turn -> raw text.
    episode = app.episodes.get_episode(_episodes(app)[0].id)
    assert episode is not None
    turn = app.episodes.episode_turns(episode.id)[0]
    assert message in app.episodes.resolve_turn_text(turn)


@pytest.mark.anyio
async def test_u_accepting_a_candidate_link_applies_it_once(app: PersonaContinuum) -> None:
    """A human decision on an ambiguous link is itself idempotent."""

    session_id = _session(app)
    _commit(app, session_id, "下个月我准备去重庆。", minutes=0)
    await _resolve_all(app, _resolver([("重庆", lambda payload: [_create("plan", "重庆旅行")])]))
    thread = _only(app)

    _commit(app, session_id, "也许吧。", minutes=300)
    await app.threads.resolve_episode_threads(
        _episodes(app)[-1].id,
        resolve=_resolver(
            [
                (
                    "也许吧",
                    lambda payload: [
                        _act("update", _thread_id(payload, "重庆"), confidence=0.3)
                    ],
                )
            ]
        ),
    )
    pending = app.threads.pending_link_candidates()
    assert len(pending) == 1
    candidate_id = pending[0]["id"]

    events_before = len(app.threads.thread_events(thread.id))
    accepted = app.threads.decide_link_candidate(candidate_id, accept=True)
    assert accepted["status"] == "accepted"
    assert len(app.threads.thread_events(thread.id)) == events_before + 1
    assert app.threads.pending_link_candidates() == []
    # A second decision cannot add a second event.
    app.threads.decide_link_candidate(candidate_id, accept=True)
    assert len(app.threads.thread_events(thread.id)) == events_before + 1

    rejected_id = "tcand_does_not_exist"
    assert app.threads.decide_link_candidate(rejected_id, accept=False)["status"] == "not_found"


@pytest.mark.anyio
async def test_n_failed_resolver_stays_retryable_and_chat_is_unaffected(
    app: PersonaContinuum,
) -> None:
    session_id = _session(app)
    _commit(app, session_id, "下个月我准备去重庆。", minutes=0)
    episode = _episodes(app)[0]

    async def broken(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        raise RuntimeError("provider down")

    report = await app.threads.resolve_episode_threads(episode.id, resolve=broken)
    assert report["error"] == "resolve_failed:RuntimeError"
    assert report["pending"] is True
    stored = app.episodes.get_episode(episode.id)
    assert stored.thread_resolution_status == "failed"
    assert stored.thread_resolution_attempts == 1
    assert _threads(app) == []

    # A garbage answer must not look like a quiet success either.
    async def garbage(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        return {"threads": ["not an object"]}

    report = await app.threads.resolve_episode_threads(episode.id, resolve=garbage)
    assert report["error"] == "invalid_thread_payload"
    assert [episode.id for episode in app.threads.pending_resolution_episodes()] == [episode.id]

    # The retry succeeds and lands on the same Episode row.
    resolve = _resolver([("重庆", lambda payload: [_create("plan", "重庆旅行")])])
    report = await app.threads.resolve_episode_threads(episode.id, resolve=resolve)
    assert report["threads_created"] == 1
    assert app.threads.pending_resolution_episodes() == []
    # Chat itself never noticed: the turn is committed and covered.
    assert app.episodes.coverage(persona_id=PERSONA)["orphaned"] == 0


# --- identity / merge -------------------------------------------------------


@pytest.mark.anyio
async def test_o_same_subject_spelled_differently_merges_instead_of_forking(
    app: PersonaContinuum,
) -> None:
    """Identity is defended twice: deterministically, and by showing candidates."""

    session_id = _session(app)
    _commit(app, session_id, "下个月去重庆。", minutes=0)
    _commit(app, session_id, "重庆旅行安排想再确认一下。", minutes=300)
    _commit(app, session_id, "还是之前那个重庆的事。", minutes=600)
    seen_candidates: list[list[str]] = []

    def _stubborn(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """A resolver that re-proposes CREATE for a longer wording of the same subject."""

        return [_create("plan", "重庆旅行安排")]

    def _semantic(payload: dict[str, Any]) -> list[dict[str, Any]]:
        seen_candidates.append([str(item["title"]) for item in payload["existing_threads"]])
        target = _thread_id(payload, "重庆")
        if target is None:
            return [_create("plan", "重庆旅行")]
        return [_act("update", target, summary="继续推进重庆旅行")]

    resolve = _resolver(
        [
            ("去重庆", _semantic),
            ("重庆旅行安排", _stubborn),
            ("还是之前那个重庆的事", _semantic),
        ]
    )
    reports = await _resolve_all(app, resolve)

    # The server merges a re-wording deterministically, without trusting the
    # model to notice: a CREATE for a longer title of the same subject is a
    # continuation, and the merged wording stays readable on the thread.
    assert reports[0]["threads_created"] == 1
    assert reports[1]["threads_created"] == 0
    assert reports[1]["threads_merged"] == 1
    # The third wording shares neither key nor containment with the original, so
    # it is the CANDIDATE list that makes the continuation possible -- the
    # mechanism this layer exists for ("去重庆" vs "重庆旅行").
    assert "重庆旅行" in seen_candidates[-1]
    assert reports[2]["threads_updated"] == 1
    assert len(_threads(app)) == 1
    thread = _only(app)
    assert len(app.threads.thread_events(thread.id)) == 3
    assert thread.metadata["merged_subjects"] == ["重庆旅行安排"]


@pytest.mark.anyio
async def test_o2_containment_merge_keeps_types_apart(app: PersonaContinuum) -> None:
    """A plan and a project that merely share a word must not be merged."""

    session_id = _session(app)
    _commit(app, session_id, "下个月我准备去重庆。", minutes=0)
    _commit(app, session_id, "Persona Continuum 重庆部署的事情还要推进。", minutes=300)
    resolve = _resolver(
        [
            ("去重庆", lambda payload: [_create("plan", "重庆旅行")]),
            (
                "重庆部署",
                lambda payload: [_create("project", "重庆部署", summary="部署项目")],
            ),
        ]
    )
    await _resolve_all(app, resolve)
    assert sorted(thread.title for thread in _threads(app)) == [
        "重庆旅行",
        "重庆部署",
    ]


# --- thread <-> fact lineage ------------------------------------------------


@pytest.mark.anyio
async def test_p_thread_links_to_facts_without_copying_them(app: PersonaContinuum) -> None:
    session_id = _session(app)
    _commit(app, session_id, "下个月我准备去重庆。", minutes=0)
    episode = _episodes(app)[0]

    async def extract(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        return {
            "facts": [
                {
                    "category": "plan",
                    "subject": "用户",
                    "predicate": "下个月的计划",
                    "value": "去重庆",
                    "origin": "user_asserted",
                    "confidence": 0.8,
                    "plan_status": "planned",
                }
            ]
        }

    await app.facts.extract_episode_facts(episode.id, extract=extract)
    fact_id = app.facts.list_facts(persona_id=PERSONA, status="active")[0].id

    def build(payload: dict[str, Any]) -> list[dict[str, Any]]:
        assert [item["fact_id"] for item in payload["episode_facts"]] == [fact_id]
        return [_create("plan", "重庆旅行", related_fact_ids=[fact_id])]

    await app.threads.resolve_episode_threads(episode.id, resolve=_resolver([("重庆", build)]))
    thread = _only(app)
    linked = app.threads.thread_facts(thread.id)
    assert [item["fact_id"] for item in linked] == [fact_id]
    assert linked[0]["relation"] == "primary"
    # The thread points at the fact; the fact keeps its own text.
    assert linked[0]["display_text"] == app.facts.get_fact(fact_id).display_text

    # A fact id the resolver made up is refused, and never becomes lineage.
    _commit(app, session_id, "顺便说一下重庆的事。", minutes=300)
    bogus = _resolver(
        [
            (
                "顺便说一下",
                lambda payload: [
                    _act(
                        "update",
                        thread.id,
                        related_fact_ids=["fact_made_up_by_the_model"],
                        summary="越界引用",
                    )
                ],
            )
        ]
    )
    await app.threads.resolve_episode_threads(_episodes(app)[-1].id, resolve=bogus)
    assert [item["fact_id"] for item in app.threads.thread_facts(thread.id)] == [fact_id]


# --- stale sweep ------------------------------------------------------------


@pytest.mark.anyio
async def test_q_silence_only_downgrades_to_stale(app: PersonaContinuum) -> None:
    session_id = _session(app)
    _commit(app, session_id, "下个月我准备去重庆。", minutes=0)
    await _resolve_all(app, _resolver([("重庆", lambda payload: [_create("plan", "重庆旅行")])]))
    thread = _only(app)
    assert thread.status is ThreadStatus.ACTIVE

    # Inside the window nothing happens.
    assert app.threads.sweep_stale(limit=10, now=BASE + timedelta(days=10))["stale"] == 0
    # Past it, the thread goes STALE -- never RESOLVED.
    result = app.threads.sweep_stale(limit=10, now=BASE + timedelta(days=120))
    assert result["stale"] == 1
    stale = app.threads.get_thread(thread.id)
    assert stale.status is ThreadStatus.STALE
    assert stale.resolved_at is None
    assert app.threads.thread_events(thread.id)[-1].event_type is ThreadEventType.STALE

    # And a live mention brings it back without inventing a second thread.
    _commit(app, session_id, "重庆那件事继续。", minutes=200 * 24 * 60)
    report = await app.threads.resolve_episode_threads(
        _episodes(app)[-1].id,
        resolve=_resolver(
            [
                (
                    "重庆那件事",
                    lambda payload: [
                        _act("update", _thread_id(payload, "重庆"), summary="继续推进")
                    ],
                )
            ]
        ),
    )
    assert report["threads_updated"] == 1
    assert len(_threads(app)) == 1
    assert _only(app).status is ThreadStatus.ACTIVE
