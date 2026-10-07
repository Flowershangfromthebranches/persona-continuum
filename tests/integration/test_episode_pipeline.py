"""Phase 3 acceptance: turn -> Episode -> provenance (integration).

Covers the eight required behaviours: basic grouping, boundary, provenance,
no-orphan, restart, idempotency, scope isolation, and failed consolidation.
Every assertion here is about *organisation*, never about how a prompt is
assembled -- Context Policy (Phase 1/2) is untouched by this layer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.domain.episode import EpisodeStatus

PERSONA = "su_he"
BRANCH = "main"


@pytest.fixture()
def episode_app(tmp_path) -> Any:
    config = Config(data_dir=tmp_path / "pc-data", memory_episode_max_turns=4)
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
    yield app
    app.close()


@pytest.fixture()
def wide_app(tmp_path) -> Any:
    """Default thresholds (24 turns) -- for the 50-turn coverage check."""

    config = Config(data_dir=tmp_path / "pc-wide")
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
    yield app
    app.close()


def _session(app: PersonaContinuum, counterpart: str = "user") -> str:
    return app.sessions.start_session(
        persona_id=PERSONA, title="chat", counterpart_id=counterpart
    ).id


def _commit(
    app: PersonaContinuum,
    session_id: str,
    message: str,
    *,
    reply: str | None = None,
    occurred_at: datetime | None = None,
    counterpart: str = "user",
) -> dict[str, Any]:
    return app.sessions.commit_turn(
        persona_id=PERSONA,
        session_id=session_id,
        user_message=message,
        persona_response=reply or f"苏禾回应：{message}",
        occurred_at=occurred_at,
        counterpart_id=counterpart,
    )


def _summariser(title: str = "讨论考研与重庆理工") -> Any:
    async def summarize(payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        # Echo one real turn id so provenance round-trips through the summary.
        first = str((payload.get("turns") or [{}])[0].get("turn_id") or "")
        return {
            "title": title,
            "summary": "用户正在考虑重庆理工大学人工智能专硕，目前主要担心数学成绩。",
            "important_events": ["用户表达数学焦虑", "苏禾进行了安慰"],
            "commitments": ["用户计划第二天开始复习"],
            "unresolved": ["尚未确定完整复习安排"],
            "emotional_arc": ["焦虑", "被理解", "形成计划"],
            "topics": ["考研", "重庆理工"],
            "entities": ["重庆理工大学"],
            "user_stated": ["担心数学成绩"],
            "persona_stated": ["苏禾安慰了用户"],
            "inferred_context": [],
            "importance": 0.7,
            "confidence": 0.8,
            "_first_turn": first,
        }

    return summarize


# --- A. basic grouping ------------------------------------------------------


def test_consecutive_turns_form_one_episode(episode_app: PersonaContinuum) -> None:
    session_id = _session(episode_app)
    reports = [
        _commit(episode_app, session_id, f"第{i}轮：我在想考研的事") for i in range(4)
    ]
    episode_ids = {report["episode_id"] for report in reports}
    assert len(episode_ids) == 1
    episode = episode_app.episodes.get_episode(next(iter(episode_ids)))
    assert episode is not None
    assert episode.turn_count == 4
    assert episode.status is EpisodeStatus.OPEN
    assert episode.boundary_reason == "first_turn"
    assert len(episode_app.episodes.list_episodes(persona_id=PERSONA)) == 1


# --- B. boundary ------------------------------------------------------------


def test_boundary_closes_one_episode_and_opens_the_next(episode_app: PersonaContinuum) -> None:
    session_id = _session(episode_app)
    for index in range(4):
        _commit(episode_app, session_id, f"考研话题第{index}轮")
    report = _commit(episode_app, session_id, "换个话题：重庆那家火锅店")
    assert report["episode"]["boundary_reason"] == "max_turns"
    assert report["episode"]["closed_episode_id"]

    episodes = episode_app.episodes.list_episodes(persona_id=PERSONA)
    assert len(episodes) == 2
    closed = next(item for item in episodes if item.sequence == 1)
    opened = next(item for item in episodes if item.sequence == 2)
    assert closed.turn_count == 4
    assert closed.status is EpisodeStatus.PENDING_CONSOLIDATION
    assert opened.turn_count == 1
    assert opened.status is EpisodeStatus.OPEN
    assert opened.boundary_reason == "max_turns"


def test_idle_gap_closes_the_episode(episode_app: PersonaContinuum) -> None:
    session_id = _session(episode_app)
    start = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)
    _commit(episode_app, session_id, "早上的第一句", occurred_at=start)
    report = _commit(
        episode_app, session_id, "第二天再说", occurred_at=start + timedelta(hours=6)
    )
    assert report["episode"]["boundary_reason"] == "idle_gap"
    assert report["episode"]["closed_episode_id"]
    assert len(episode_app.episodes.list_episodes(persona_id=PERSONA)) == 2


# --- C. provenance ----------------------------------------------------------


@pytest.mark.anyio
async def test_episode_owns_resolvable_source_turns(episode_app: PersonaContinuum) -> None:
    session_id = _session(episode_app)
    turn_ids = [
        _commit(episode_app, session_id, f"第{i}轮原文内容", reply=f"第{i}轮苏禾原文")[
            "turn_id"
        ]
        for i in range(3)
    ]
    episode = episode_app.episodes.list_episodes(persona_id=PERSONA)[0]
    sources = episode_app.episodes.episode_turns(episode.id)
    assert [item.turn_id for item in sources] == turn_ids
    assert [item.position for item in sources] == [0, 1, 2]
    assert all(item.source_kind == "session_turn" for item in sources)

    # Provenance walks back to the RAW committed text, not to a copy.
    for index, item in enumerate(sources):
        text = episode_app.episodes.resolve_turn_text(item)
        assert f"第{index}轮原文内容" in text
        assert f"第{index}轮苏禾原文" in text

    # The Episode table itself stores no transcript body.
    row = episode_app.database.conn.execute(
        "SELECT summary, summary_json, metadata_json FROM memory_episodes WHERE id = ?",
        (episode.id,),
    ).fetchone()
    assert "第0轮原文内容" not in str(tuple(row))

    # Consolidation keeps provenance: the summary never replaces the turns.
    report = await episode_app.episodes.consolidate_episode(
        episode.id, summarize=_summariser(), allow_open=True
    )
    assert report["summary_created"] is True
    refreshed = episode_app.episodes.get_episode(episode.id)
    assert refreshed is not None
    assert refreshed.turn_count == 3
    assert [item.turn_id for item in episode_app.episodes.episode_turns(episode.id)] == turn_ids
    assert refreshed.structured_summary().commitments == ["用户计划第二天开始复习"]


def test_lineage_links_episode_to_every_source_turn(episode_app: PersonaContinuum) -> None:
    session_id = _session(episode_app)
    for index in range(3):
        _commit(episode_app, session_id, f"第{index}轮")
    rows = episode_app.database.conn.execute(
        "SELECT child_id, parent_id, relation FROM lineage "
        "WHERE child_type = 'episode' AND relation = 'episode_contains_turn'"
    ).fetchall()
    assert len(rows) == 3
    assert all(str(row["parent_id"]).startswith("turn_") for row in rows)


# --- D. no orphan -----------------------------------------------------------


def test_fifty_turns_have_no_orphans(wide_app: PersonaContinuum) -> None:
    session_id = _session(wide_app)
    for index in range(50):
        _commit(wide_app, session_id, f"第{index}轮：继续聊")
    coverage = wide_app.episodes.coverage(persona_id=PERSONA)
    assert coverage["committed_turns"] == 50
    assert coverage["assigned_to_episode"] == 50
    assert coverage["orphaned"] == 0
    assert coverage["dangling_provenance"] == 0
    assert (
        coverage["assigned_to_episode"] + coverage["unassigned_pending_backfill"]
        == coverage["committed_turns"]
    )
    # Everything is assigned but nothing is summarised yet: that is PENDING.
    assert coverage["pending_consolidation"] == 50
    assert coverage["episodes"] == 3  # 24 + 24 + 2


def test_legacy_turns_are_unassigned_and_backfillable(wide_app: PersonaContinuum) -> None:
    """Turns committed before Phase 3 are pending, not lost."""

    session_id = _session(wide_app)
    _commit(wide_app, session_id, "第一句")
    # Simulate a pre-Phase-3 turn: the committed row exists, but the Episode
    # layer has never seen it (no mapping AND no Episode row).
    wide_app.database.conn.execute(
        "DELETE FROM memory_episode_turns WHERE turn_id IN (SELECT id FROM session_turns)"
    )
    wide_app.database.conn.execute("DELETE FROM memory_episodes")
    wide_app.database.conn.commit()
    coverage = wide_app.episodes.coverage(persona_id=PERSONA)
    assert coverage["committed_turns"] == 1
    assert coverage["assigned_to_episode"] == 0
    assert coverage["unassigned_pending_backfill"] == 1
    assert coverage["orphaned"] == 0  # unaccounted, but reachable

    result = wide_app.episodes.backfill()
    assert result["processed"] == 1
    assert result["created"] == 1
    after = wide_app.episodes.coverage(persona_id=PERSONA)
    assert after["orphaned"] == 0
    assert after["assigned_to_episode"] == 1
    assert after["unassigned_pending_backfill"] == 0
    # Re-running backfill is a no-op: nothing is duplicated.
    again = wide_app.episodes.backfill()
    assert again["processed"] == 0
    assert len(wide_app.episodes.list_episodes(persona_id=PERSONA)) == 1


# --- E. restart -------------------------------------------------------------


def test_open_episode_survives_a_restart(tmp_path) -> None:
    config = Config(data_dir=tmp_path / "pc-restart", memory_episode_max_turns=6)
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
    for index in range(3):
        _commit(app, session_id, f"重启前第{index}轮")
    before = app.episodes.list_episodes(persona_id=PERSONA)
    assert len(before) == 1
    episode_id = before[0].id
    app.close()

    reopened = PersonaContinuum(config, include_fake_agent=True)
    reopened.init()
    after_restart = reopened.episodes.list_episodes(persona_id=PERSONA)
    assert [item.id for item in after_restart] == [episode_id]
    assert after_restart[0].turn_count == 3

    report = _commit(reopened, session_id, "重启后的下一轮")
    assert report["episode_id"] == episode_id  # same OPEN episode, no duplicate
    assert report["episode"]["action"] == "append"
    assert len(reopened.episodes.list_episodes(persona_id=PERSONA)) == 1
    assert reopened.episodes.coverage(persona_id=PERSONA)["orphaned"] == 0
    reopened.close()


def test_restart_across_an_idle_gap_closes_the_old_episode(tmp_path) -> None:
    config = Config(data_dir=tmp_path / "pc-gap", memory_episode_max_turns=24)
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
    start = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)
    _commit(app, session_id, "早上聊的", occurred_at=start)
    app.close()

    reopened = PersonaContinuum(config, include_fake_agent=True)
    reopened.init()
    report = _commit(
        reopened, session_id, "隔天再聊", occurred_at=start + timedelta(hours=8)
    )
    assert report["episode"]["boundary_reason"] == "idle_gap"
    episodes = reopened.episodes.list_episodes(persona_id=PERSONA)
    assert len(episodes) == 2
    closed = next(item for item in episodes if item.sequence == 1)
    assert closed.status is EpisodeStatus.PENDING_CONSOLIDATION
    assert closed.ended_at is not None
    assert reopened.episodes.coverage(persona_id=PERSONA)["orphaned"] == 0
    reopened.close()


# --- F. idempotency ---------------------------------------------------------


@pytest.mark.anyio
async def test_consolidation_is_idempotent(episode_app: PersonaContinuum) -> None:
    session_id = _session(episode_app)
    for index in range(4):
        _commit(episode_app, session_id, f"第{index}轮")
    episode = episode_app.episodes.list_episodes(persona_id=PERSONA)[0]

    first = await episode_app.episodes.consolidate_episode(
        episode.id, summarize=_summariser(), allow_open=True
    )
    assert first["summary_created"] is True
    second = await episode_app.episodes.consolidate_episode(
        episode.id, summarize=_summariser(title="重复摘要"), allow_open=True
    )
    assert second["action"] == "noop"
    assert second["summary_created"] is False
    assert len(episode_app.episodes.list_episodes(persona_id=PERSONA)) == 1
    stored = episode_app.episodes.get_episode(episode.id)
    assert stored is not None
    assert stored.title == "讨论考研与重庆理工"  # the forced re-run did not rewrite it


def test_replayed_assignment_never_duplicates_a_turn(episode_app: PersonaContinuum) -> None:
    session_id = _session(episode_app)
    report = _commit(episode_app, session_id, "第一句")
    turn_id = report["turn_id"]
    episode_id = report["episode_id"]
    replay = episode_app.episodes.assign_turn(
        persona_id=PERSONA,
        session_id=session_id,
        turn_id=turn_id,
        counterpart_id="user",
        branch_id=BRANCH,
        room_id=None,
        occurred_at=datetime.now(UTC),
        user_message="第一句",
        persona_response="重复投递",
    )
    assert replay["action"] == "noop"
    assert replay["error"] == "turn_already_assigned"
    assert replay["current_episode_id"] == episode_id
    episode = episode_app.episodes.get_episode(episode_id)
    assert episode is not None
    assert episode.turn_count == 1
    assert len(episode_app.episodes.episode_turns(episode_id)) == 1


def test_boundary_decisions_are_reproducible(episode_app: PersonaContinuum) -> None:
    """The same turn sequence must always produce the same Episode layout."""

    session_id = _session(episode_app)
    timestamps = [datetime(2026, 9, 19, 10, 0, tzinfo=UTC) + timedelta(minutes=i) for i in range(9)]
    for index, moment in enumerate(timestamps):
        _commit(episode_app, session_id, f"第{index}轮", occurred_at=moment)
    first = [
        (item.sequence, item.turn_count)
        for item in episode_app.episodes.list_episodes(persona_id=PERSONA)
    ]
    assert first == [(3, 1), (2, 4), (1, 4)]

    other_session = _session(episode_app, counterpart="user_b")
    for index, moment in enumerate(timestamps):
        _commit(
            episode_app, other_session, f"第{index}轮", occurred_at=moment,
            counterpart="user_b",
        )
    second = [
        (item.sequence, item.turn_count)
        for item in episode_app.episodes.list_episodes(persona_id=PERSONA)
        if item.counterpart_id == "user_b"
    ]
    assert second == first


# --- G. scope isolation -----------------------------------------------------


def test_counterparts_never_share_an_episode(episode_app: PersonaContinuum) -> None:
    session_a = _session(episode_app, counterpart="user_a")
    session_b = _session(episode_app, counterpart="user_b")
    for index in range(3):
        _commit(episode_app, session_a, f"A 的第{index}轮", counterpart="user_a")
    for index in range(2):
        _commit(episode_app, session_b, f"B 的第{index}轮", counterpart="user_b")

    a_episodes = episode_app.episodes.list_episodes(persona_id=PERSONA, counterpart_id="user_a")
    b_episodes = episode_app.episodes.list_episodes(persona_id=PERSONA, counterpart_id="user_b")
    assert len(a_episodes) == 1 and len(b_episodes) == 1
    assert a_episodes[0].id != b_episodes[0].id
    assert a_episodes[0].turn_count == 3
    assert b_episodes[0].turn_count == 2
    # A's episode never saw B's turns.
    a_turns = {item.turn_id for item in episode_app.episodes.episode_turns(a_episodes[0].id)}
    b_turns = {item.turn_id for item in episode_app.episodes.episode_turns(b_episodes[0].id)}
    assert not a_turns & b_turns


def test_branches_never_share_an_episode(episode_app: PersonaContinuum) -> None:
    session_id = _session(episode_app)
    now = datetime.now(UTC)
    for branch in ("main", "diverged"):
        for index in range(2):
            episode_app.episodes.assign_turn(
                persona_id=PERSONA,
                session_id=session_id,
                turn_id=f"turn_{branch}_{index}",
                counterpart_id="user",
                branch_id=branch,
                room_id=None,
                occurred_at=now + timedelta(minutes=index),
                user_message=f"{branch} 第{index}轮",
                persona_response=f"{branch} 回应",
            )
    main_episodes = episode_app.episodes.list_episodes(persona_id=PERSONA, branch_id="main")
    diverged = episode_app.episodes.list_episodes(persona_id=PERSONA, branch_id="diverged")
    assert len(main_episodes) == 1 and len(diverged) == 1
    assert main_episodes[0].id != diverged[0].id
    assert main_episodes[0].turn_count == 2
    assert diverged[0].turn_count == 2


def test_sessions_of_the_same_counterpart_stay_separate_episodes(
    episode_app: PersonaContinuum,
) -> None:
    """A new conversation container starts a new Episode (never a continuation)."""

    first = _session(episode_app)
    second = _session(episode_app)  # a new room/session with the same counterpart
    assert first != second
    _commit(episode_app, first, "第一段对话")
    _commit(episode_app, second, "第二段对话")
    episodes = episode_app.episodes.list_episodes(persona_id=PERSONA)
    assert len(episodes) == 2
    assert {item.session_id for item in episodes} == {first, second}


# --- H. failed consolidation ------------------------------------------------


@pytest.mark.anyio
async def test_consolidation_failure_keeps_everything_retryable(
    episode_app: PersonaContinuum,
) -> None:
    async def failing(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        raise TimeoutError("provider timed out")

    session_id = _session(episode_app)
    for index in range(4):
        _commit(episode_app, session_id, f"第{index}轮")
    episode = episode_app.episodes.list_episodes(persona_id=PERSONA)[0]

    report = await episode_app.episodes.consolidate_episode(
        episode.id, summarize=failing, allow_open=True
    )
    assert report["error"] == "summarize_failed:TimeoutError"
    assert report["pending"] is True
    failed = episode_app.episodes.get_episode(episode.id)
    assert failed is not None
    assert failed.status is EpisodeStatus.FAILED
    assert failed.summary_status == "failed"
    assert failed.last_error == "TimeoutError"
    assert failed.consolidation_attempts == 1

    # Raw history is untouched and the reply path never saw the failure.
    assert (
        episode_app.database.conn.execute("SELECT COUNT(*) AS c FROM session_turns").fetchone()[
            "c"
        ]
        == 4
    )
    assert len(episode_app.episodes.episode_turns(episode.id)) == 4
    assert episode_app.episodes.coverage(persona_id=PERSONA)["orphaned"] == 0

    # Chat keeps working while consolidation is failing.
    follow_up = _commit(episode_app, session_id, "失败期间的下一轮")
    assert follow_up["turn_id"]

    # A retry succeeds and the row recovers.
    retry = await episode_app.episodes.consolidate_episode(
        episode.id, summarize=_summariser(), force=True
    )
    assert retry["summary_created"] is True
    recovered = episode_app.episodes.get_episode(episode.id)
    assert recovered is not None
    assert recovered.summary_status == "ready"
    assert recovered.last_error is None


@pytest.mark.anyio
async def test_invalid_summary_payload_is_rejected(episode_app: PersonaContinuum) -> None:
    """A model that ignores the schema must not poison the ledger."""

    async def garbage(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        return "这不是一个结构化摘要"

    session_id = _session(episode_app)
    _commit(episode_app, session_id, "第一轮")
    episode = episode_app.episodes.list_episodes(persona_id=PERSONA)[0]
    report = await episode_app.episodes.consolidate_episode(
        episode.id, summarize=garbage, allow_open=True
    )
    assert report["error"] == "invalid_summary_payload"
    stored = episode_app.episodes.get_episode(episode.id)
    assert stored is not None
    assert stored.summary == ""
    assert stored.status is EpisodeStatus.FAILED


def test_recover_pending_restores_the_work_list(tmp_path) -> None:
    config = Config(data_dir=tmp_path / "pc-pending", memory_episode_max_turns=2)
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
    for index in range(3):
        _commit(app, session_id, f"第{index}轮")
    pending = app.episodes.pending_episodes()
    assert len(pending) == 2
    assert all(item.summary_status == "pending" for item in pending)
    recovery = app.episodes.recover_pending()
    assert recovery["pending"] == len(pending)
    assert app.episodes.coverage(persona_id=PERSONA)["orphaned"] == 0
    app.close()


def test_episodes_do_not_replace_digital_experience(episode_app: PersonaContinuum) -> None:
    """Phase 3 adds a higher layer; the per-turn memory stays."""

    session_id = _session(episode_app)
    for index in range(3):
        _commit(episode_app, session_id, f"第{index}轮")
    experience = episode_app.database.conn.execute(
        "SELECT COUNT(*) AS c FROM memories WHERE source_kind = 'digital_experience'"
    ).fetchone()["c"]
    assert experience == 3
    episode = episode_app.episodes.list_episodes(persona_id=PERSONA)[0]
    assert episode.turn_count == 3  # 3 experiences -> 1 episode


def test_deleting_a_room_does_not_delete_episodes(episode_app: PersonaContinuum) -> None:
    session_id = _session(episode_app)
    _commit(episode_app, session_id, "房间里的第一句")
    episode = episode_app.episodes.list_episodes(persona_id=PERSONA)[0]
    # Episodes are persona memory; only runtime caches may be forgotten.
    episode_app.orchestrator.forget_room_runtime("room_does_not_exist")
    assert episode_app.episodes.get_episode(episode.id) is not None
    assert episode_app.episodes.coverage(persona_id=PERSONA)["orphaned"] == 0


def test_room_deletion_keeps_episode_provenance_resolvable(
    episode_app: PersonaContinuum,
) -> None:
    """A room is a runtime container: deleting it must not erase memory.

    Episode provenance points at ``session_turns`` (which outlive the room), so
    even after ``room_transcripts`` is cleared for that room, every source turn
    still resolves back to the committed dialogue.
    """

    room = episode_app.orchestrator.create_room(
        title="临时房间", topic="临时", participants=[]
    )
    session_id = _session(episode_app)
    _commit(episode_app, session_id, "这个房间之后会被删掉")
    episode = episode_app.episodes.list_episodes(persona_id=PERSONA)[0]
    assert episode_app.episodes.inspect_episode(episode.id)["sources"][0]["resolvable"]

    assert episode_app.orchestrator.delete_room(room.id) is True
    assert episode_app.episodes.get_episode(episode.id) is not None
    sources = episode_app.episodes.episode_turns(episode.id)
    assert all(episode_app.episodes.resolve_turn_text(item) for item in sources)
    assert episode_app.episodes.coverage(persona_id=PERSONA)["orphaned"] == 0


def test_context_policy_is_not_involved(episode_app: PersonaContinuum) -> None:
    """Phase 3 must not touch the Phase 1/2 budget surface at all."""

    config = episode_app.config
    assert config.room_prompt_target_tokens == 6000
    assert config.room_prompt_hard_tokens == 7000
    assert config.room_context_strategy == "auto"
    session_id = _session(episode_app)
    _commit(episode_app, session_id, "第一句")
    episode = episode_app.episodes.list_episodes(persona_id=PERSONA)[0]
    # Episode size is bounded by its own thresholds, never by a prompt budget.
    assert episode.source_token_estimate < config.memory_episode_max_tokens
    assert config.memory_episode_max_tokens != config.room_prompt_hard_tokens
