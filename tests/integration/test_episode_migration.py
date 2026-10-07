"""Episode migration: additive, idempotent, reversible, non-destructive."""

from __future__ import annotations

from datetime import UTC, datetime

from persona_continuum.domain.memory import MemoryRecord, MemoryType
from persona_continuum.storage.database import Database
from persona_continuum.storage.migrations import (
    MEMORY_EPISODES_DOWN_SQL,
    MEMORY_EPISODES_MIGRATION_ID,
)


def _table_names(database: Database) -> set[str]:
    return {
        str(row["name"])
        for row in database.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _seed(database: Database) -> None:
    database.conn.execute(
        "INSERT INTO personas (id, manifest_json, package_path, archived, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("p1", "{}", "", 0, datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
    )
    database.conn.execute(
        "INSERT INTO sessions (id, persona_id, title, status, created_at, updated_at, "
        "metadata_json)"
        " VALUES (?,?,?,?,?,?,?)",
        (
            "sess1",
            "p1",
            "t",
            "active",
            datetime.now(UTC).isoformat(),
            datetime.now(UTC).isoformat(),
            "{}",
        ),
    )
    database.conn.execute(
        "INSERT INTO session_turns VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "turn1",
            "sess1",
            "p1",
            "u",
            "p",
            "[]",
            None,
            "{}",
            datetime.now(UTC).isoformat(),
        ),
    )
    database.conn.execute(
        "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "mem1",
            "p1",
            "content",
            "semantic",
            None,
            datetime.now(UTC).isoformat(),
            "[]",
            "{}",
            None,
            "seed",
            0.5,
            0.5,
            "valid",
            0,
            None,
            "main",
            0,
            0,
            1,
            None,
            "{}",
        ),
    )
    database.conn.commit()


def test_migration_creates_the_episode_tables_and_records_a_version(tmp_path) -> None:
    database = Database(tmp_path / "db.sqlite")
    database.migrate()
    tables = _table_names(database)
    assert {"memory_episodes", "memory_episode_turns", "schema_migrations"} <= tables
    row = database.conn.execute(
        "SELECT migration_id FROM schema_migrations WHERE migration_id = ?",
        (MEMORY_EPISODES_MIGRATION_ID,),
    ).fetchone()
    assert row is not None
    database.close()


def test_migration_is_idempotent_and_preserves_existing_data(tmp_path) -> None:
    database = Database(tmp_path / "db.sqlite")
    database.migrate()
    _seed(database)
    memories_before = database.conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
    turns_before = database.conn.execute(
        "SELECT COUNT(*) AS c FROM session_turns"
    ).fetchone()["c"]

    for _ in range(3):
        database.migrate()

    assert database.conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"] == (
        memories_before
    )
    assert (
        database.conn.execute("SELECT COUNT(*) AS c FROM session_turns").fetchone()["c"]
        == turns_before
    )
    versions = database.conn.execute(
        "SELECT COUNT(*) AS c FROM schema_migrations WHERE migration_id = ?",
        (MEMORY_EPISODES_MIGRATION_ID,),
    ).fetchone()["c"]
    assert versions == 1
    database.close()


def test_indexes_exist_for_the_required_lookups(tmp_path) -> None:
    database = Database(tmp_path / "db.sqlite")
    database.migrate()
    indexes = {
        str(row["name"])
        for row in database.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    for expected in (
        "idx_memory_episodes_scope_sequence",
        "idx_memory_episodes_scope_status",
        "idx_memory_episodes_session_status",
        "idx_memory_episodes_persona_time",
        "idx_memory_episodes_pending",
        "idx_memory_episode_turns_turn",
        "idx_memory_episode_turns_session",
        "idx_memory_episode_turns_episode_position",
    ):
        assert expected in indexes, expected
    database.close()


def test_scope_sequence_is_unique(tmp_path) -> None:
    import sqlite3

    database = Database(tmp_path / "db.sqlite")
    database.migrate()
    now = datetime.now(UTC).isoformat()
    insert = (
        "INSERT INTO memory_episodes (id, persona_id, counterpart_id, branch_id, session_id, "
        "sequence, started_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
    )
    database.conn.execute(insert, ("e1", "p", "c", "main", "s", 1, now, now, now))
    database.conn.commit()
    try:
        database.conn.execute(insert, ("e2", "p", "c", "main", "s", 1, now, now, now))
        database.conn.commit()
        raise AssertionError("duplicate (scope, sequence) must be rejected")
    except sqlite3.IntegrityError:
        database.conn.rollback()
    # A different scope with the same sequence is fine.
    database.conn.execute(insert, ("e3", "p", "c", "main", "s2", 1, now, now, now))
    database.conn.commit()
    database.close()


def test_episode_turns_cascade_when_an_episode_is_removed(tmp_path) -> None:
    database = Database(tmp_path / "db.sqlite")
    database.migrate()
    now = datetime.now(UTC).isoformat()
    database.conn.execute(
        "INSERT INTO memory_episodes (id, persona_id, counterpart_id, branch_id, session_id, "
        "sequence, started_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("e1", "p", "c", "main", "s", 1, now, now, now),
    )
    database.conn.execute(
        "INSERT INTO memory_episode_turns (episode_id, turn_id, position, created_at) "
        "VALUES (?,?,?,?)",
        ("e1", "turn1", 0, now),
    )
    database.conn.commit()
    database.conn.execute("DELETE FROM memory_episodes WHERE id = 'e1'")
    database.conn.commit()
    assert (
        database.conn.execute("SELECT COUNT(*) AS c FROM memory_episode_turns").fetchone()["c"]
        == 0
    )
    database.close()


def test_rollback_drops_only_the_episode_tables(tmp_path) -> None:
    database = Database(tmp_path / "db.sqlite")
    database.migrate()
    _seed(database)
    assert "memory_episodes" in _table_names(database)

    database.rollback_memory_episodes()
    tables = _table_names(database)
    assert "memory_episodes" not in tables
    assert "memory_episode_turns" not in tables
    assert (
        database.conn.execute(
            "SELECT COUNT(*) AS c FROM schema_migrations WHERE migration_id = ?",
            (MEMORY_EPISODES_MIGRATION_ID,),
        ).fetchone()["c"]
        == 0
    )
    # Raw history is untouched by the rollback.
    assert database.conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"] == 1
    assert database.conn.execute("SELECT COUNT(*) AS c FROM session_turns").fetchone()["c"] == 1

    # And the migration can be re-applied.
    database.migrate()
    assert "memory_episodes" in _table_names(database)
    assert database.conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"] == 1
    database.close()


def test_down_sql_only_targets_episode_tables() -> None:
    lowered = MEMORY_EPISODES_DOWN_SQL.casefold()
    assert "drop table if exists memory_episode_turns" in lowered
    assert "drop table if exists memory_episodes" in lowered
    for forbidden in ("session_turns", "room_transcripts", "memories", "lineage", "rooms"):
        assert f"drop table if exists {forbidden}" not in lowered


def test_app_migration_through_the_container(tmp_path) -> None:
    """The real upgrade path: init() must migrate without touching history."""

    from persona_continuum.application.container import PersonaContinuum
    from persona_continuum.config import Config

    app = PersonaContinuum(Config(data_dir=tmp_path / "pc"), include_fake_agent=True)
    app.init()
    app.personas.create_from_manifest(
        {"id": "p1", "display_name": "P", "persona_type": "fictional", "run_mode": "continuation"}
    )
    app.memories.add_memory(
        MemoryRecord(
            id="mem_keep",
            persona_id="p1",
            type=MemoryType.SEMANTIC,
            source_kind="seed",
            content="不能被迁移删除",
        )
    )
    app.database.migrate()  # a second startup on an already-upgraded directory
    assert app.memories.get_memory("mem_keep") is not None
    assert app.episodes.coverage()["orphaned"] == 0
    app.close()
