from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import TracebackType
from typing import Any

from persona_continuum.storage.migrations import SCHEMA_SQL


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA busy_timeout = 30000")
        return self._conn

    def migrate(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        self._ensure_runtime_branch_columns()
        self._ensure_research_artifact_columns()
        self.conn.commit()

    def _ensure_runtime_branch_columns(self) -> None:
        self._ensure_affect_branch_column()
        self._ensure_needs_branch_column()
        self._ensure_relationships_branch_column()
        self._ensure_change_events_branch_column()
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS change_event_supports (
              event_id TEXT NOT NULL REFERENCES change_events(id) ON DELETE CASCADE,
              session_id TEXT NOT NULL,
              turn_id TEXT NOT NULL,
              support_weight REAL NOT NULL DEFAULT 1.0,
              PRIMARY KEY(event_id, session_id, turn_id)
            )
            """
        )

    def _table_columns(self, table: str) -> set[str]:
        return {
            str(row["name"]) for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _ensure_affect_branch_column(self) -> None:
        if "branch_id" in self._table_columns("affect_states"):
            return
        self.conn.execute("ALTER TABLE affect_states RENAME TO affect_states_legacy_rc5")
        self.conn.execute(
            """
            CREATE TABLE affect_states (
              persona_id TEXT NOT NULL,
              branch_id TEXT NOT NULL DEFAULT 'main',
              name TEXT NOT NULL,
              kind TEXT NOT NULL,
              intensity REAL NOT NULL,
              baseline REAL NOT NULL,
              decay_rate REAL NOT NULL,
              updated_at TEXT NOT NULL,
              triggers_json TEXT NOT NULL,
              confidence REAL NOT NULL,
              PRIMARY KEY(persona_id, branch_id, name, kind)
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO affect_states (
              persona_id, branch_id, name, kind, intensity, baseline, decay_rate,
              updated_at, triggers_json, confidence
            )
            SELECT persona_id, 'main', name, kind, intensity, baseline, decay_rate,
                   updated_at, triggers_json, confidence
            FROM affect_states_legacy_rc5
            """
        )
        self.conn.execute("DROP TABLE affect_states_legacy_rc5")

    def _ensure_needs_branch_column(self) -> None:
        if "branch_id" in self._table_columns("needs"):
            return
        self.conn.execute("ALTER TABLE needs RENAME TO needs_legacy_rc5")
        self.conn.execute(
            """
            CREATE TABLE needs (
              persona_id TEXT NOT NULL,
              branch_id TEXT NOT NULL DEFAULT 'main',
              name TEXT NOT NULL,
              level REAL NOT NULL,
              baseline REAL NOT NULL,
              updated_at TEXT NOT NULL,
              confidence REAL NOT NULL,
              reasons_json TEXT NOT NULL,
              PRIMARY KEY(persona_id, branch_id, name)
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO needs (
              persona_id, branch_id, name, level, baseline, updated_at, confidence,
              reasons_json
            )
            SELECT persona_id, 'main', name, level, baseline, updated_at, confidence,
                   reasons_json
            FROM needs_legacy_rc5
            """
        )
        self.conn.execute("DROP TABLE needs_legacy_rc5")

    def _ensure_relationships_branch_column(self) -> None:
        if "branch_id" in self._table_columns("relationships"):
            return
        self.conn.execute("ALTER TABLE relationships RENAME TO relationships_legacy_rc5")
        self.conn.execute(
            """
            CREATE TABLE relationships (
              persona_id TEXT NOT NULL,
              branch_id TEXT NOT NULL DEFAULT 'main',
              counterpart TEXT NOT NULL,
              state_json TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(persona_id, branch_id, counterpart)
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO relationships (
              persona_id, branch_id, counterpart, state_json, updated_at
            )
            SELECT persona_id, 'main', counterpart, state_json, updated_at
            FROM relationships_legacy_rc5
            """
        )
        self.conn.execute("DROP TABLE relationships_legacy_rc5")

    def _ensure_change_events_branch_column(self) -> None:
        if "branch_id" in self._table_columns("change_events"):
            return
        self.conn.execute("ALTER TABLE change_events RENAME TO change_events_legacy_rc5")
        self.conn.execute(
            """
            CREATE TABLE change_events (
              id TEXT PRIMARY KEY,
              persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
              branch_id TEXT NOT NULL DEFAULT 'main',
              event_type TEXT NOT NULL,
              target_type TEXT NOT NULL,
              target_id TEXT NOT NULL,
              session_id TEXT,
              turn_id TEXT,
              data_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO change_events (
              id, persona_id, branch_id, event_type, target_type, target_id,
              session_id, turn_id, data_json, created_at
            )
            SELECT id, persona_id, 'main', event_type, target_type, target_id,
                   session_id, turn_id, data_json, created_at
            FROM change_events_legacy_rc5
            """
        )
        self.conn.execute("DROP TABLE change_events_legacy_rc5")

    def _ensure_research_artifact_columns(self) -> None:
        columns = self._table_columns("research_artifacts")
        if "artifact_canonical_sha256" not in columns:
            self.conn.execute(
                "ALTER TABLE research_artifacts ADD COLUMN artifact_canonical_sha256 TEXT"
            )
        indexes = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA index_list(research_artifacts)").fetchall()
        }
        if "idx_research_artifacts_canonical_unique" not in indexes:
            self.conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_research_artifacts_canonical_unique
                ON research_artifacts(persona_id, task_id, artifact_canonical_sha256)
                WHERE artifact_canonical_sha256 IS NOT NULL
                """
            )

    def has_fts5(self) -> bool:
        try:
            probe = sqlite3.connect(":memory:")
            probe.execute("CREATE VIRTUAL TABLE fts5_probe USING fts5(x)")
            probe.close()
            return True
        except sqlite3.OperationalError:
            return False

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def execute_write(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
        *,
        retries: int = 3,
    ) -> sqlite3.Cursor:
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(retries):
            try:
                return self.conn.execute(sql, parameters)
            except sqlite3.OperationalError as exc:
                last_error = exc
                if "locked" not in str(exc).lower() or attempt == retries - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))
        raise last_error or sqlite3.OperationalError("write_failed")

    def __enter__(self) -> Database:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
