"""Phase 6.1 acceptance: parent/child dependency hardening.

Phase 6 left one hole: a Chapter that went ``failed -> ready`` (or whose FINAL
content changed) did not make its Long-term parent notice, so the parent kept
quoting a version of the child that no longer existed.  Phase 6.1 closes it with
a source fingerprint plus a one-level-at-a-time invalidation, and wires
``hierarchy_long_term_min_chapters`` into the automatic close decision.

The tests below are the A5 list from the brief, plus the propagation bound that
makes A2 more than a promise.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.domain.hierarchical_summary import (
    SummaryReadiness,
    SummaryStatus,
)
from persona_continuum.room.context_policy import PROFILES

PERSONA = "su_he"
COUNTERPART = "user"
BASE = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)

#: 200 days.  Past the 7-day Chapter inactivity gap (so each sitting becomes its
#: own Chapter) and past the 180-day Long-term inactivity gap (so the segment
#: boundary genuinely fires), while three turns sixty minutes apart still land in
#: ONE Episode.
FAR_DAYS = 200
FAR = FAR_DAYS * 24 * 60


class _StructuredResult:
    """Placeholder for the room-level structured executor (unused here)."""

    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value


def _episode_summariser(phase: int, sitting: int) -> Any:
    """An Episode summary whose only content is a grounded, distinct title."""

    title = f"第 {phase} 阶段" if sitting == 0 else f"第 {phase} 阶段的后续"

    async def summarize(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        return {"title": title, "summary": f"{title}聊了几件事。", "topics": [title]}

    return summarize


def _grounded(payload: dict[str, Any], *, reverse: bool = False) -> dict[str, Any]:
    """A summariser that only repeats what it was given, in a chosen order.

    ``reverse`` produces genuinely different content that is still fully
    grounded -- the phase-6.1 way to change a child's FINAL without tripping
    the grounding validator (an invented tag would, correctly, be rejected).
    """

    sources = payload.get("sources") or []
    picked = [str(item.get("title") or item.get("summary") or "") for item in sources]
    picked = [text for text in picked if text]
    if reverse:
        picked = list(reversed(picked))
    return {
        "title": "、".join(picked[:2]) or "阶段",
        "summary": "、".join(picked[:4]),
        "major_events": picked[:2],
        "key_entities": [],
        "importance": 0.6,
    }


def _summariser(*, reverse: bool = False) -> Any:
    async def summarize(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        return _grounded(payload, reverse=reverse)

    return summarize


def _broken() -> Any:
    async def summarize(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
        raise RuntimeError("provider down")

    return summarize


def _make_app(data_dir: Any, **overrides: Any) -> PersonaContinuum:
    continuum = PersonaContinuum(
        Config(data_dir=data_dir, **overrides), include_fake_agent=True
    )
    continuum.init()
    continuum.personas.create_from_manifest(
        {
            "id": PERSONA,
            "display_name": "苏禾",
            "persona_type": "fictional",
            "run_mode": "continuation",
        }
    )
    return continuum


@pytest.fixture()
def app(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Any:
    data_dir = tmp_path / "pc-hierarchy-dependency"
    monkeypatch.setenv("PERSONA_CONTINUUM_HOME", str(data_dir))
    continuum = _make_app(data_dir)
    yield continuum
    continuum.close()


# --- scaffolding ------------------------------------------------------------


def _session(app: PersonaContinuum) -> str:
    return app.sessions.start_session(
        persona_id=PERSONA, title="t", counterpart_id=COUNTERPART
    ).id


def _commit(app: PersonaContinuum, session_id: str, message: str, *, minutes: int) -> None:
    app.sessions.commit_turn(
        persona_id=PERSONA,
        session_id=session_id,
        user_message=message,
        persona_response="苏禾回应",
        occurred_at=BASE + timedelta(minutes=minutes),
        counterpart_id=COUNTERPART,
    )


def _assign_all(app: PersonaContinuum) -> None:
    for episode in sorted(
        app.episodes.list_episodes(persona_id=PERSONA, counterpart_id=COUNTERPART),
        key=lambda item: item.started_at,
    ):
        app.hierarchies.assign_episode(episode)


def _chapters(app: PersonaContinuum) -> list[Any]:
    return app.hierarchies.list_summaries(
        persona_id=PERSONA, counterpart_id=COUNTERPART, level=1, limit=50
    )


def _level(app: PersonaContinuum, level: int) -> list[Any]:
    return app.hierarchies.list_summaries(
        persona_id=PERSONA, counterpart_id=COUNTERPART, level=level, limit=50
    )


async def _phase(app: PersonaContinuum, index: int, *, sittings: int = 1) -> None:
    """Sittings -> Episodes -> one Chapter, far from every other phase.

    Turns inside a sitting are an hour apart (one Episode); sittings are ten
    hours apart (past the 180-minute idle gap, so a second Episode) and nowhere
    near the 7-day Chapter gap, so ``sittings`` Episodes land in ONE Chapter.
    """

    session_id = _session(app)
    for sitting in range(sittings):
        for offset in range(3):
            _commit(
                app,
                session_id,
                f"第 {index} 阶段第 {sitting + 1} 轮第 {offset + 1} 次对话。",
                minutes=index * FAR + sitting * 600 + offset * 60,
            )
        # ``sequence`` restarts per session, so the newest Episode of THIS
        # sitting is the one to summarise -- not the global maximum.
        episode = max(
            [
                item
                for item in app.episodes.list_episodes(
                    persona_id=PERSONA, counterpart_id=COUNTERPART
                )
                if item.session_id == session_id
            ],
            key=lambda item: item.sequence,
        )
        await app.episodes.consolidate_episode(
            episode.id, summarize=_episode_summariser(index, sitting), allow_open=True
        )


async def _chapter_ready(app: PersonaContinuum, chapter: Any) -> Any:
    app.hierarchies.close_summary(chapter.id)
    report = await app.hierarchies.consolidate_summary(chapter.id, summarize=_summariser())
    assert report["summary_status"] in {"ready", "provisional"}, report
    return app.hierarchies.get_summary(chapter.id)


async def _two_ready_chapters(app: PersonaContinuum) -> list[Any]:
    # Two sittings in the first phase, so the first Chapter owns TWO Episodes and
    # reversing its sources genuinely changes its content.
    await _phase(app, 0, sittings=2)
    await _phase(app, 1)
    _assign_all(app)
    chapters = _chapters(app)
    assert len(chapters) == 2, [chapter.source_count for chapter in chapters]
    return [await _chapter_ready(app, chapter) for chapter in chapters]


async def _segment_ready(app: PersonaContinuum) -> Any:
    plan = app.hierarchies.plan_long_term(
        persona_id=PERSONA,
        counterpart_id=COUNTERPART,
        branch_id="main",
        close_segment=True,
    )
    segment_id = plan["closed_summary_id"]
    assert segment_id, plan
    report = await app.hierarchies.consolidate_summary(segment_id, summarize=_summariser())
    assert report["summary_status"] == "ready", report
    return app.hierarchies.get_summary(segment_id)


# --- 1. a failed chapter never becomes a parent FINAL -----------------------


@pytest.mark.anyio
async def test_failed_child_blocks_the_parent_final(app: PersonaContinuum) -> None:
    await _phase(app, 0)
    await _phase(app, 1)
    _assign_all(app)
    chapters = _chapters(app)

    # Chapter A never produced text; Chapter B did.
    app.hierarchies.close_summary(chapters[0].id)
    failed = await app.hierarchies.consolidate_summary(chapters[0].id, summarize=_broken())
    assert failed["error"] == "summarize_failed:RuntimeError"
    await _chapter_ready(app, chapters[1])

    plan = app.hierarchies.plan_long_term(
        persona_id=PERSONA,
        counterpart_id=COUNTERPART,
        branch_id="main",
        close_segment=True,
    )
    segment_id = plan["closed_summary_id"]
    assert segment_id
    report = await app.hierarchies.consolidate_summary(segment_id, summarize=_summariser())

    # The parent must NOT produce a FINAL over a Chapter that has no content.
    assert report["error"] == "sources_not_ready"
    assert report["pending"] is True
    assert chapters[0].id in report["blocked_by"]
    blocked = app.hierarchies.get_summary(segment_id)
    assert blocked is not None
    assert blocked.summary == ""
    assert not blocked.is_ready
    assert blocked.metadata["blocked_reason"] == "sources_not_ready"
    assert not app.hierarchies.child_sources_ready(blocked)[0]

    # The retry is what unblocks it, and then the REAL content gets in.
    retried = await app.hierarchies.consolidate_summary(chapters[0].id, summarize=_summariser())
    assert retried["summary_status"] == "ready"
    fresh = app.hierarchies.get_summary(segment_id)
    assert fresh is not None and app.hierarchies.child_sources_ready(fresh)[0]

    final = await app.hierarchies.consolidate_summary(segment_id, summarize=_summariser())
    assert final["action"] == "consolidate"
    assert final["summary_status"] == "ready"
    stored = app.hierarchies.get_summary(segment_id)
    assert stored is not None
    assert stored.summary_status is SummaryReadiness.READY
    assert stored.source_fingerprint == stored.consolidated_fingerprint


# --- 2. a child change puts the parent back on the work list ----------------


@pytest.mark.anyio
async def test_child_change_marks_parent_stale(app: PersonaContinuum) -> None:
    chapters = await _two_ready_chapters(app)
    segment = await _segment_ready(app)
    assert segment.summary_status is SummaryReadiness.READY
    assert segment.source_fingerprint == segment.consolidated_fingerprint
    assert segment.fingerprint_mismatch is False
    assert app.hierarchies.count_stale(persona_id=PERSONA) == 0
    assert app.hierarchies.count_available(persona_id=PERSONA) == 3

    # The child's FINAL content changes (a forced regeneration in place).
    changed = await app.hierarchies.consolidate_summary(
        chapters[0].id, summarize=_summariser(reverse=True), force=True
    )
    assert changed["summary_status"] == "ready"

    stale = app.hierarchies.get_summary(segment.id)
    assert stale is not None
    assert stale.summary_status is SummaryReadiness.STALE
    assert stale.is_stale is True
    assert stale.has_text is True  # readable, but no longer current
    assert stale.fingerprint_mismatch is True
    assert stale.metadata["stale_reason"] == "source_content_changed"
    assert stale.metadata["stale_count"] == 1
    assert stale.metadata["stale_previous_status"] == "ready"
    assert app.hierarchies.count_stale(persona_id=PERSONA) == 1
    old_title = stale.title
    assert old_title == "第 0 阶段、第 0 阶段的后续、第 1 阶段"
    # A stale summary is not available: it no longer describes its sources.
    assert app.hierarchies.count_available(persona_id=PERSONA) == 2
    assert segment.id in [item.id for item in app.hierarchies.pending_summaries(limit=10)]
    assert segment.id in [item.id for item in app.hierarchies.stale_summaries(limit=10)]

    # The refresh pulls the NEW content in and clears the staleness.
    sweep = await app.hierarchies.consolidate_pending(summarize=_summariser())
    assert sweep["succeeded"] == 1
    refreshed = app.hierarchies.get_summary(segment.id)
    assert refreshed is not None
    assert refreshed.summary_status is SummaryReadiness.READY
    assert refreshed.source_fingerprint == refreshed.consolidated_fingerprint
    assert refreshed.metadata.get("stale_reason") is None
    assert refreshed.metadata["last_stale_reason"] == "source_content_changed"
    assert refreshed.title != old_title
    assert "第 0 阶段的后续、第 0 阶段" in refreshed.title
    assert "第 0 阶段的后续、第 0 阶段" in refreshed.summary
    assert app.hierarchies.count_stale(persona_id=PERSONA) == 0
    assert app.hierarchies.count_available(persona_id=PERSONA) == 3


@pytest.mark.anyio
async def test_child_failed_then_retried_ready_invalidates_once(app: PersonaContinuum) -> None:
    """The A1 scenario verbatim: failed -> ready must reach the parent."""

    chapters = await _two_ready_chapters(app)
    segment = await _segment_ready(app)

    # The child breaks (a forced regeneration that fails).
    broken = await app.hierarchies.consolidate_summary(
        chapters[0].id, summarize=_broken(), force=True
    )
    assert broken["error"] == "summarize_failed:RuntimeError"
    stale = app.hierarchies.get_summary(segment.id)
    assert stale is not None and stale.summary_status is SummaryReadiness.STALE
    assert stale.metadata["stale_count"] == 1
    stale_title = stale.title

    # ... then comes back: the parent is already stale and must not double-count.
    again = await app.hierarchies.consolidate_summary(chapters[0].id, summarize=_summariser())
    assert again["summary_status"] == "ready"
    still = app.hierarchies.get_summary(segment.id)
    assert still is not None and still.summary_status is SummaryReadiness.STALE
    assert still.metadata["stale_count"] == 1
    assert still.title == stale_title

    # One refresh reconciles everything.
    await app.hierarchies.consolidate_pending(summarize=_summariser())
    assert app.hierarchies.count_stale(persona_id=PERSONA) == 0
    assert app.hierarchies.count_available(persona_id=PERSONA) == 3


# --- 3. no meaningless recomputation ---------------------------------------


@pytest.mark.anyio
async def test_same_version_ready_does_not_reinvalidate(app: PersonaContinuum) -> None:
    chapters = await _two_ready_chapters(app)
    segment = await _segment_ready(app)
    fingerprint = segment.source_fingerprint

    # Re-running the SAME child with the SAME content changes nothing.
    for _ in range(2):
        report = await app.hierarchies.consolidate_summary(
            chapters[0].id, summarize=_summariser(), force=True
        )
        assert report["summary_status"] == "ready"

    after = app.hierarchies.get_summary(segment.id)
    assert after is not None
    assert after.summary_status is SummaryReadiness.READY
    assert after.source_fingerprint == fingerprint
    assert after.fingerprint_mismatch is False
    assert after.metadata.get("stale_count") is None
    # Nothing is owed, so a sweep attempts nothing at all.
    assert await app.hierarchies.consolidate_pending(summarize=_summariser()) == {
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "reports": [],
    }

    # ... and the fingerprint really is stable arithmetic, not a coincidence.
    assert app.hierarchies.compute_source_fingerprint(after) == fingerprint
    assert app.hierarchies.refresh_source_fingerprint(after.id) == fingerprint
    assert app.hierarchies.invalidate_parents(chapters[0].id, reason="probe") == []


@pytest.mark.anyio
async def test_parent_is_reconciled_without_dropping_the_child(app: PersonaContinuum) -> None:
    """A failed REFRESH must not launder stale text into "no summary at all"."""

    chapters = await _two_ready_chapters(app)
    segment = await _segment_ready(app)
    old_title = app.hierarchies.get_summary(segment.id).title
    await app.hierarchies.consolidate_summary(
        chapters[0].id, summarize=_summariser(reverse=True), force=True
    )
    assert app.hierarchies.get_summary(segment.id).summary_status is SummaryReadiness.STALE

    report = await app.hierarchies.consolidate_summary(segment.id, summarize=_broken())
    assert report["error"] == "summarize_failed:RuntimeError"
    kept = app.hierarchies.get_summary(segment.id)
    assert kept.title == old_title
    assert kept is not None
    # Still stale, still readable, still owed -- never silently emptied.
    assert kept.summary_status is SummaryReadiness.STALE
    assert kept.summary
    assert kept.last_error == "summarize_failed:RuntimeError"
    assert kept.metadata["refresh_failed"] == "summarize_failed:RuntimeError"
    assert segment.id in [item.id for item in app.hierarchies.pending_summaries(limit=10)]

    ok = await app.hierarchies.consolidate_summary(segment.id, summarize=_summariser())
    assert ok["summary_status"] == "ready"
    assert app.hierarchies.count_stale(persona_id=PERSONA) == 0


# --- 4. restart keeps the invalidation -------------------------------------


@pytest.mark.anyio
async def test_restart_keeps_the_stale_state(tmp_path) -> None:
    data_dir = tmp_path / "pc-hierarchy-dep-restart"
    app = _make_app(data_dir)
    chapters = await _two_ready_chapters(app)
    segment = await _segment_ready(app)
    await app.hierarchies.consolidate_summary(
        chapters[0].id, summarize=_summariser(reverse=True), force=True
    )
    assert app.hierarchies.get_summary(segment.id).summary_status is SummaryReadiness.STALE
    app.close()

    reopened = PersonaContinuum(
        Config(data_dir=data_dir), include_fake_agent=True
    )
    reopened.init()
    try:
        recovered = reopened.hierarchies.get_summary(segment.id)
        assert recovered is not None
        assert recovered.summary_status is SummaryReadiness.STALE
        assert recovered.fingerprint_mismatch is True
        assert recovered.metadata["stale_count"] == 1
        # The durable work list survives, so the refresh is not lost.
        assert segment.id in [
            item.id for item in reopened.hierarchies.pending_summaries(limit=10)
        ]
        recovery = reopened.hierarchies.recover_pending()
        assert segment.id in recovery["pending_summary_ids"]
        assert reopened.hierarchies.count_stale(persona_id=PERSONA) == 1

        report = await reopened.hierarchies.consolidate_summary(
            segment.id, summarize=_summariser()
        )
        assert report["summary_status"] == "ready"
        assert reopened.hierarchies.count_stale(persona_id=PERSONA) == 0
    finally:
        reopened.close()


# --- 5. long_term_min_chapters actually governs the automatic close ---------


@pytest.mark.anyio
async def test_long_term_min_chapters_blocks_premature_auto_close(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two chapters must not be allowed to become "a stage of our life".

    Each Chapter closes on its own 7-day inactivity boundary and is propagated
    into the Long-term level by the real path (``_propagate``), so this exercises
    exactly the code a production turn runs.  Chapters sit 200 days apart: past
    the 300-day segment timespan (so the boundary fires) but under the 400-day
    segment inactivity gap (so only ONE boundary kind is in play).
    """

    data_dir = tmp_path / "pc-lt-min"
    monkeypatch.setenv("PERSONA_CONTINUUM_HOME", str(data_dir))
    app = _make_app(
        data_dir,
        hierarchy_long_term_min_chapters=3,
        hierarchy_long_term_max_chapters=12,
        hierarchy_long_term_inactivity_gap_days=400,
        hierarchy_long_term_max_timespan_days=300,
    )
    try:
        for index in range(5):
            await _phase(app, index)
        _assign_all(app)
        chapters = _chapters(app)
        assert len(chapters) == 5, [chapter.source_count for chapter in chapters]
        for chapter in chapters:
            await _chapter_ready(app, chapter)

        # Three chapters did NOT make three segments: the first two boundary
        # firings were deferred, and the segment kept absorbing chapters.
        segments = _level(app, 2)
        assert len(segments) == 2, [
            (item.source_count, item.status.value) for item in segments
        ]
        closed = next(item for item in segments if item.status is SummaryStatus.CLOSED)
        opened = next(item for item in segments if item.status is SummaryStatus.OPEN)
        assert closed.source_count == 3
        assert closed.status is SummaryStatus.CLOSED
        # The last Chapter is still open, so only four of the five were ever
        # propagated: the new segment starts with that one chapter.
        assert opened.source_count == 1

        # The deferral is on the record, not a silent behavioural change.
        inspected = app.hierarchies.inspect_summary(closed.id)
        assert inspected is not None
        assert inspected["deferred_close_reason"] == "max_timespan"
        assert inspected["source_fingerprint"]

        # The closed segment owes a FINAL, and its three Chapters are final.
        report = await app.hierarchies.consolidate_summary(closed.id, summarize=_summariser())
        assert report["summary_status"] == "ready"
        stored = app.hierarchies.get_summary(closed.id)
        assert stored is not None
        assert stored.source_fingerprint == stored.consolidated_fingerprint

        # An explicitly closed one-chapter segment is still allowed: the guard
        # binds automatic closes only, never the operator.
        plan = app.hierarchies.plan_long_term(
            persona_id=PERSONA,
            counterpart_id=COUNTERPART,
            branch_id="main",
            close_segment=True,
        )
        assert plan["chapters_assigned"] == 1
        assert plan["closed_summary_id"] == opened.id
        explicit = app.hierarchies.get_summary(opened.id)
        assert explicit is not None
        assert explicit.status is SummaryStatus.CLOSED
        assert explicit.source_count == 2 < 3
        report = await app.hierarchies.consolidate_summary(
            opened.id, summarize=_summariser()
        )
        assert report["summary_status"] == "ready"
    finally:
        app.close()


@pytest.mark.anyio
async def test_explicit_close_still_overrides_the_minimum(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "pc-lt-explicit"
    monkeypatch.setenv("PERSONA_CONTINUUM_HOME", str(data_dir))
    app = _make_app(data_dir, hierarchy_long_term_min_chapters=5)
    try:
        await _phase(app, 0)
        _assign_all(app)
        chapter = _chapters(app)[0]
        await _chapter_ready(app, chapter)
        plan = app.hierarchies.plan_long_term(
            persona_id=PERSONA,
            counterpart_id=COUNTERPART,
            branch_id="main",
            close_segment=True,
        )
        assert plan["closed_summary_id"]
        segment = app.hierarchies.get_summary(plan["closed_summary_id"])
        assert segment is not None
        assert segment.status is SummaryStatus.CLOSED
        assert segment.source_count == 1 < 5
        report = await app.hierarchies.consolidate_summary(segment.id, summarize=_summariser())
        assert report["summary_status"] == "ready"
    finally:
        app.close()


# --- 6. the propagation is bounded to one level per refresh ----------------


@pytest.fixture()
def deep_app(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Any:
    data_dir = tmp_path / "pc-deep"
    monkeypatch.setenv("PERSONA_CONTINUUM_HOME", str(data_dir))
    continuum = _make_app(
        data_dir, hierarchy_max_level=3, hierarchy_long_term_min_chapters=2
    )
    yield continuum
    continuum.close()


@pytest.mark.anyio
async def test_level_three_dependency_is_bounded_and_safe(deep_app: PersonaContinuum) -> None:
    app = deep_app
    # Two sittings in the first phase, so reversing the first Chapter's sources
    # genuinely changes its content.
    await _phase(app, 0, sittings=2)
    for index in range(1, 3):
        await _phase(app, index)
    _assign_all(app)
    chapters = _chapters(app)
    assert len(chapters) == 3
    for chapter in chapters:
        await _chapter_ready(app, chapter)

    # Chapters 1-2 fill a segment; chapter 3 arrives past the inactivity gap, so
    # the segment closes and propagates into a level-3 seat.
    app.hierarchies.plan_long_term(
        persona_id=PERSONA, counterpart_id=COUNTERPART, branch_id="main"
    )
    level2 = _level(app, 2)
    assert len(level2) == 2, [(item.source_count, item.status.value) for item in level2]
    segment = next(item for item in level2 if item.source_count == 2)
    assert segment.status is SummaryStatus.CLOSED
    await app.hierarchies.consolidate_summary(segment.id, summarize=_summariser())
    stored_segment = app.hierarchies.get_summary(segment.id)
    assert stored_segment is not None
    old_title = stored_segment.title

    level3 = _level(app, 3)
    assert len(level3) == 1
    grandparent_id = level3[0].id
    assert grandparent_id not in {chapter.id for chapter in chapters}
    report = await app.hierarchies.consolidate_summary(grandparent_id, summarize=_summariser())
    assert report["summary_status"] in {"ready", "provisional"}, report
    grandparent = app.hierarchies.get_summary(grandparent_id)
    assert grandparent is not None
    assert grandparent.level == 3
    assert grandparent.source_fingerprint == grandparent.consolidated_fingerprint
    # A level-3 parent's sources are summaries, and their version resolves.
    source = app.hierarchies.summary_sources(grandparent_id)[0]
    assert source.source_type.value == "summary"
    assert app.hierarchies.source_version(source.source_type, source.source_id).startswith(
        "summary:"
    )

    # A change two levels down marks ONLY the direct parent.  This is A2 in
    # arithmetic form: the grandparent is not synchronously rebuilt.
    await app.hierarchies.consolidate_summary(
        chapters[0].id, summarize=_summariser(reverse=True), force=True
    )
    before = app.hierarchies.get_summary(segment.id)
    assert before is not None
    # Stale does not mean rewritten: the old text is still exactly what it was.
    assert before.title == old_title
    assert app.hierarchies.get_summary(segment.id).summary_status is SummaryReadiness.STALE
    untouched = app.hierarchies.get_summary(grandparent_id)
    assert untouched is not None
    # Still current (an OPEN summary carries a PROVISIONAL text) and NOT stale.
    assert untouched.summary_status in {SummaryReadiness.READY, SummaryReadiness.PROVISIONAL}
    assert untouched.metadata.get("stale_count") is None
    assert [item.id for item in app.hierarchies.stale_summaries(limit=10)] == [segment.id]

    # Refreshing the middle level is what reaches the grandparent.
    await app.hierarchies.consolidate_summary(segment.id, summarize=_summariser())
    escalated = app.hierarchies.get_summary(grandparent_id)
    assert escalated is not None
    assert escalated.summary_status is SummaryReadiness.STALE
    assert escalated.metadata["stale_reason"] == "source_content_changed"
    assert escalated.title == old_title

    # And the whole chain reconciles, level by level.
    sweep = await app.hierarchies.consolidate_pending(summarize=_summariser(), limit=5)
    assert sweep["succeeded"] >= 1
    assert app.hierarchies.count_stale(persona_id=PERSONA) == 0
    final = app.hierarchies.get_summary(grandparent_id)
    assert final is not None
    assert "第 0 阶段的后续、第 0 阶段" in final.title
    assert app.hierarchies.count_pending(persona_id=PERSONA) == 0
    assert app.hierarchies.stats(persona_id=PERSONA)["by_level"] == {"1": 3, "2": 2, "3": 1}


# --- 7. context policy is untouched ----------------------------------------


def test_context_policy_numbers_are_unchanged() -> None:
    local = PROFILES["local_constrained"]
    assert (local.prompt_target_tokens, local.prompt_hard_tokens) == (6000, 7000)
    assert local.recent_dialogue_token_budget == 1500
    assert (local.recent_message_window, local.recall_top_k) == (8, 8)
    assert (local.memory_max_tokens, local.memory_budget_tokens) == (400, 2000)
    assert (local.historical_excerpt_token_budget, local.historical_excerpt_messages) == (0, 0)

    balanced = PROFILES["balanced"]
    assert balanced.recent_dialogue_token_budget == 6144
    assert (balanced.historical_excerpt_token_budget, balanced.historical_excerpt_messages) == (
        0,
        0,
    )
    remote = PROFILES["remote_quality"]
    assert remote.recent_dialogue_token_budget == 32_768
    assert (
        remote.historical_excerpt_token_budget,
        remote.historical_excerpt_messages,
    ) == (12_000, 6)