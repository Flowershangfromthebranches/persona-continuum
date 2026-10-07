"""Regression: a stale room snapshot must not revert the rolling summary.

The bug this pins down: a turn reads its room state once at the start and saves
the whole object at the end, while the rolling-summary refresh writes the same
row asynchronously minutes later.  Saving the older snapshot used to replace the
row (and the in-memory cache) wholesale, silently deleting a valid v2 summary.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_continuum.room.models import RoomSessionState  # noqa: E402
from persona_continuum.room.orchestrator import (  # noqa: E402
    MultiAgentOrchestrator,
    _protected_metadata_keys,
)


def _room_state(metadata: dict) -> RoomSessionState:
    return RoomSessionState.model_validate(
        {
            "id": "room_test",
            "title": "t",
            "topic": "",
            "protocol": "free_discussion",
            "participants": [],
            "transcript": [],
            "metadata": metadata,
        }
    )


def _store(conn: sqlite3.Connection, metadata: dict) -> None:
    conn.execute(
        "INSERT INTO rooms "
        "(id, status, persona_ids_json, topic, state_json, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET state_json = excluded.state_json",
        ("room_test", "ready", "[]", "", json.dumps({"metadata": metadata}), "now", "now"),
    )
    conn.commit()


def _load(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT state_json FROM rooms WHERE id='room_test'").fetchone()
    return json.loads(row[0]).get("metadata", {})


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    # The production connection uses sqlite3.Row; the merge reads columns by name.
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE rooms (id TEXT PRIMARY KEY, status TEXT, persona_ids_json TEXT,"
        " topic TEXT, state_json TEXT, created_at TEXT, updated_at TEXT)"
    )
    return c


def _orchestrator_like(conn: sqlite3.Connection) -> SimpleNamespace:
    """Just enough object for the merge helper (it only touches .continuum)."""

    orch = SimpleNamespace(
        continuum=SimpleNamespace(database=SimpleNamespace(conn=conn))
    )
    orch._read_room_metadata = lambda room_id: MultiAgentOrchestrator._read_room_metadata(
        orch, room_id
    )
    return orch


def test_protected_keys_cover_summary_and_archives() -> None:
    keys = _protected_metadata_keys(
        {
            "rolling_summary": "x",
            "rolling_summary_version": 2,
            "rolling_summary_updated_at": "t",
            "raw_archive_legacy_rolling_summary": "y",
            "injections": [],
            "model_call": {},
        }
    )
    assert "rolling_summary" in keys
    assert "raw_archive_legacy_rolling_summary" in keys
    # Ordinary per-turn bookkeeping is NOT protected; it belongs to the snapshot.
    assert "injections" not in keys
    assert "model_call" not in keys


def test_stale_snapshot_adopts_stored_summary(conn: sqlite3.Connection) -> None:
    stored = {
        "rolling_summary": "## Room Long-term Summary\n- Key facts: 去重庆",
        "rolling_summary_version": 2,
        "rolling_summary_updated_at": "2026-09-19T10:00:00+00:00",
        "injections": [],
    }
    _store(conn, stored)

    # A turn's snapshot, taken BEFORE the summary landed.
    state = _room_state({"injections": [], "model_call": {"status": "calling"}})
    MultiAgentOrchestrator._merge_protected_room_metadata(
        _orchestrator_like(conn), state
    )

    assert state.metadata["rolling_summary"] == stored["rolling_summary"]
    assert state.metadata["rolling_summary_version"] == 2
    # The snapshot's own keys survive untouched.
    assert state.metadata["model_call"] == {"status": "calling"}


def test_stale_snapshot_cannot_revert_newer_summary(
    conn: sqlite3.Connection,
) -> None:
    _store(
        conn,
        {
            "rolling_summary": "## Room Long-term Summary\n- Key facts: new",
            "rolling_summary_version": 2,
            "rolling_summary_updated_at": "2026-09-19T12:00:00+00:00",
        },
    )
    # A snapshot that still carries an OLDER summary.
    state = _room_state(
        {
            "rolling_summary": "## Room Long-term Summary\n- Key facts: old",
            "rolling_summary_version": 2,
            "rolling_summary_updated_at": "2026-09-19T09:00:00+00:00",
        }
    )
    MultiAgentOrchestrator._merge_protected_room_metadata(
        _orchestrator_like(conn), state
    )
    assert "new" in state.metadata["rolling_summary"]


def test_fresh_summary_write_is_kept(conn: sqlite3.Connection) -> None:
    _store(
        conn,
        {
            "rolling_summary": "## Room Long-term Summary\n- Key facts: old",
            "rolling_summary_version": 2,
            "rolling_summary_updated_at": "2026-09-19T09:00:00+00:00",
        },
    )
    # The summary writer's own snapshot: strictly newer stamp.
    fresh = "## Room Long-term Summary\n- Key facts: refreshed"
    state = _room_state(
        {
            "rolling_summary": fresh,
            "rolling_summary_version": 2,
            "rolling_summary_updated_at": "2026-09-19T13:00:00+00:00",
        }
    )
    MultiAgentOrchestrator._merge_protected_room_metadata(
        _orchestrator_like(conn), state
    )
    assert state.metadata["rolling_summary"] == fresh


def test_legacy_dump_is_not_resurrected(conn: sqlite3.Connection) -> None:
    """A quarantined legacy blob must not be re-adopted as the summary."""

    _store(conn, {"raw_archive_legacy_rolling_summary": "## Scene / relationship evidence\n{...}"})
    state = _room_state({"injections": []})
    MultiAgentOrchestrator._merge_protected_room_metadata(
        _orchestrator_like(conn), state
    )
    assert "rolling_summary" not in state.metadata
    assert "raw_archive_legacy_rolling_summary" in state.metadata
