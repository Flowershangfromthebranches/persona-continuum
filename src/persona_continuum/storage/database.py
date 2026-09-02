from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from collections.abc import Iterator
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
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._conn.execute("PRAGMA busy_timeout = 30000")
        return self._conn

    def migrate(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        self._ensure_runtime_branch_columns()
        self._ensure_research_artifact_columns()
        self._ensure_persona_research_quality_columns()
        self._ensure_world_tables()
        self._ensure_world_runtime_columns()
        self._ensure_world_binding_profile_columns()
        self._ensure_profile_enrichment_columns()
        self._ensure_job_center_columns()
        self._ensure_research_capability_cache_columns()
        self._ensure_room_protocol_state()
        self._ensure_room_transcript_identity()
        self.conn.commit()

    def _ensure_room_transcript_identity(self) -> None:
        """Make (room_id, turn_id) an identity key for replay-safe injection.

        Legacy databases may hold duplicated turn rows from pre-idempotency
        retries; the oldest row wins and the unique index then guarantees the
        protocol/user message dedup keys hold for every future insert.
        """
        if "room_transcripts" not in self._all_tables():
            return
        self.conn.execute(
            """
            DELETE FROM room_transcripts WHERE rowid NOT IN (
              SELECT MIN(rowid) FROM room_transcripts GROUP BY room_id, turn_id
            )
            """
        )
        with contextlib.suppress(Exception):
            self.conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_room_transcripts_room_turn
                ON room_transcripts(room_id, turn_id)
                """
            )

    def _ensure_room_protocol_state(self) -> None:
        """Backfill additive protocol defaults into legacy room JSON in place."""

        if "rooms" not in self._all_tables():
            return
        rows = self.conn.execute("SELECT id, state_json FROM rooms").fetchall()
        for row in rows:
            try:
                state = json.loads(str(row["state_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(state, dict):
                continue
            changed = False
            defaults: dict[str, Any] = {
                "protocol": "free_discussion",
                "protocol_config": {},
                "shared_context": {},
                "protocol_state": {},
                "protocol_events": [],
            }
            for key, value in defaults.items():
                if key not in state:
                    state[key] = value
                    changed = True
            participants = state.get("participants")
            if isinstance(participants, list):
                for participant in participants:
                    if isinstance(participant, dict) and "role" not in participant:
                        participant["role"] = "member"
                        changed = True
            if changed:
                self.conn.execute(
                    "UPDATE rooms SET state_json = ? WHERE id = ?",
                    (json.dumps(state, ensure_ascii=False, separators=(",", ":")), row["id"]),
                )

    def _ensure_world_runtime_columns(self) -> None:
        if "worlds" in self._all_tables():
            info = self.conn.execute("PRAGMA table_info(worlds)").fetchall()
            cols = {str(r["name"]) for r in info}
            for col_name in (
                "builder_runtime_json",
                "default_actor_runtime_json",
                "director_runtime_json",
                "evaluator_runtime_json",
            ):
                if col_name not in cols:
                    with contextlib.suppress(Exception):
                        self.conn.execute(
                            f"ALTER TABLE worlds ADD COLUMN {col_name} TEXT NOT NULL DEFAULT '{{}}'"
                        )

    def _ensure_world_binding_profile_columns(self) -> None:
        if "world_runtime_bindings" not in self._all_tables():
            return
        columns = self._table_columns("world_runtime_bindings")
        for name in ("profile_id", "profile_type"):
            if name not in columns:
                self.conn.execute(f"ALTER TABLE world_runtime_bindings ADD COLUMN {name} TEXT")

    def _ensure_profile_enrichment_columns(self) -> None:
        if "profile_enrichment_jobs" not in self._all_tables():
            return
        columns = self._table_columns("profile_enrichment_jobs")
        additions = {
            "enrichment_input_mode": "TEXT NOT NULL DEFAULT 'local_materials'",
            "parent_job_id": "TEXT",
            "base_persona_version": "INTEGER",
            "input_material_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "input_material_count": "INTEGER NOT NULL DEFAULT 0",
            "new_source_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "failure_json": "TEXT",
            "agent_call_audits_json": "TEXT NOT NULL DEFAULT '[]'",
        }
        for name, definition in additions.items():
            if name not in columns:
                with contextlib.suppress(Exception):
                    self.conn.execute(
                        f"ALTER TABLE profile_enrichment_jobs ADD COLUMN {name} {definition}"
                    )

    def _ensure_job_center_columns(self) -> None:
        """Add task-center lifecycle fields without deleting historical jobs."""

        tables = self._all_tables()
        if "persona_creation_jobs" in tables:
            columns = self._table_columns("persona_creation_jobs")
            additions = {
                "visibility": "TEXT NOT NULL DEFAULT 'user'",
                "dismissed_at": "TEXT",
                "superseded_by": "TEXT",
                "worker_state": "TEXT NOT NULL DEFAULT 'starting'",
                "worker_started_at": "TEXT",
                "worker_heartbeat_at": "TEXT",
                "worker_finished_at": "TEXT",
                "agent_call_count": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in additions.items():
                if name not in columns:
                    self.conn.execute(
                        f"ALTER TABLE persona_creation_jobs ADD COLUMN {name} {definition}"
                    )
            self.conn.execute(
                "UPDATE persona_creation_jobs SET worker_state = 'running' "
                "WHERE worker_state = 'starting' AND status IN "
                "('planning', 'researching', 'ingesting_sources', 'extracting', 'compiling')"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_persona_creation_jobs_task_center "
                "ON persona_creation_jobs(visibility, dismissed_at, status, updated_at)"
            )

        if "profile_enrichment_jobs" in tables:
            columns = self._table_columns("profile_enrichment_jobs")
            additions = {
                "visibility": "TEXT NOT NULL DEFAULT 'user'",
                "dismissed_at": "TEXT",
                "superseded_by": "TEXT",
                "worker_state": "TEXT NOT NULL DEFAULT 'starting'",
                "worker_started_at": "TEXT",
                "worker_heartbeat_at": "TEXT",
                "worker_finished_at": "TEXT",
                "agent_call_count": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in additions.items():
                if name not in columns:
                    self.conn.execute(
                        f"ALTER TABLE profile_enrichment_jobs ADD COLUMN {name} {definition}"
                    )
            self.conn.execute(
                "UPDATE profile_enrichment_jobs SET worker_state = 'running' "
                "WHERE worker_state = 'starting' AND status IN "
                "('researching', 'ingesting', 'compiling')"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_profile_enrichment_jobs_task_center "
                "ON profile_enrichment_jobs(visibility, dismissed_at, status, updated_at)"
            )

        # Historical Profile Enrichment children are internal even if their
        # old row predates the visibility column.  The explicit FK reference
        # is the authoritative detection signal; parent_job_id is checked as
        # an additional guard against unrelated Persona jobs.
        if {"persona_creation_jobs", "profile_enrichment_jobs"} <= tables:
            rows = self.conn.execute(
                """
                SELECT pc.id, pc.job_config_json
                FROM persona_creation_jobs pc
                JOIN profile_enrichment_jobs pe
                  ON pe.persona_creation_job_id = pc.id
                WHERE pe.persona_creation_job_id IS NOT NULL
                """
            ).fetchall()
            for row in rows:
                try:
                    config = json.loads(row["job_config_json"] or "{}")
                except (TypeError, ValueError):
                    config = {}
                if config.get("parent_job_id") or config.get("parent_profile_enrichment_job_id"):
                    self.conn.execute(
                        "UPDATE persona_creation_jobs SET visibility = 'internal' WHERE id = ?",
                        (str(row["id"]),),
                    )

    def _ensure_research_capability_cache_columns(self) -> None:
        """Add TTL/fingerprint fields without invalidating existing probes."""

        if "research_capability_cache" not in self._all_tables():
            return
        columns = self._table_columns("research_capability_cache")
        additions = {
            "configuration_fingerprint": "TEXT NOT NULL DEFAULT ''",
            "expires_at": "TEXT",
            "cache_ttl_seconds": "REAL",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.conn.execute(
                    f"ALTER TABLE research_capability_cache ADD COLUMN {name} {definition}"
                )

    def _all_tables(self) -> set[str]:
        return {
            str(row["name"])
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    def _ensure_persona_research_quality_columns(self) -> None:
        """Backfill adaptive research columns for databases created pre-upgrade."""
        if "persona_creation_jobs" not in self._all_tables():
            return
        info = self.conn.execute("PRAGMA table_info(persona_creation_jobs)").fetchall()
        columns = {str(row["name"]) for row in info}
        additions = {
            "life_stage_progress_json": "TEXT NOT NULL DEFAULT '{}'",
            "life_stages_json": "TEXT NOT NULL DEFAULT '[]'",
            "information_gain_json": "TEXT NOT NULL DEFAULT '[]'",
            "research_gaps_json": "TEXT NOT NULL DEFAULT '[]'",
            "research_checkpoints_json": "TEXT NOT NULL DEFAULT '[]'",
            "research_stop_reason": "TEXT",
            "query_history_json": "TEXT NOT NULL DEFAULT '{}'",
            "private_coverage_json": "TEXT NOT NULL DEFAULT '{}'",
            "progress_json": "TEXT NOT NULL DEFAULT '{}'",
            "failure_json": "TEXT",
            "agent_call_audits_json": "TEXT NOT NULL DEFAULT '[]'",
            "checkpoints_json": "TEXT NOT NULL DEFAULT '[]'",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.conn.execute(
                    f"ALTER TABLE persona_creation_jobs ADD COLUMN {name} {definition}"
                )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS persona_research_checkpoints (
              id TEXT PRIMARY KEY,
              job_id TEXT NOT NULL REFERENCES persona_creation_jobs(id) ON DELETE CASCADE,
              round_index INTEGER NOT NULL,
              raw_source_count INTEGER NOT NULL DEFAULT 0,
              independent_source_count INTEGER NOT NULL DEFAULT 0,
              quality_source_count INTEGER NOT NULL DEFAULT 0,
              coverage_json TEXT NOT NULL DEFAULT '{}',
              information_gain_json TEXT NOT NULL DEFAULT '{}',
              gaps_json TEXT NOT NULL DEFAULT '[]',
              stop_reason TEXT,
              created_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS persona_source_clusters (
              cluster_id TEXT NOT NULL,
              persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
              origin_type TEXT NOT NULL DEFAULT 'unknown',
              origin_identifier TEXT NOT NULL DEFAULT '',
              member_source_ids_json TEXT NOT NULL DEFAULT '[]',
              canonical_source_id TEXT,
              independence_confidence REAL NOT NULL DEFAULT 0.0,
              reason TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(persona_id, cluster_id)
            )
            """
        )

    def _ensure_world_tables(self) -> None:
        if "organizations" in self._all_tables():
            info = self.conn.execute("PRAGMA table_info(organizations)").fetchall()
            pk_cols = [str(r["name"]) for r in info if int(r["pk"]) > 0]
            if pk_cols == ["id"]:
                self.conn.execute("DROP TABLE organizations")
                self.conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS organizations (
                      id TEXT NOT NULL,
                      world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
                      branch_id TEXT NOT NULL REFERENCES world_branches(id) ON DELETE CASCADE,
                      name TEXT NOT NULL,
                      leadership_json TEXT NOT NULL DEFAULT '[]',
                      departments_json TEXT NOT NULL DEFAULT '[]',
                      employees_count INTEGER NOT NULL DEFAULT 0,
                      budget_billions REAL NOT NULL DEFAULT 0.0,
                      cash_reserves_billions REAL NOT NULL DEFAULT 0.0,
                      technology_json TEXT NOT NULL DEFAULT '[]',
                      projects_json TEXT NOT NULL DEFAULT '[]',
                      strategy_json TEXT NOT NULL DEFAULT '{}',
                      culture_json TEXT NOT NULL DEFAULT '{}',
                      updated_at TEXT NOT NULL,
                      PRIMARY KEY(world_id, branch_id, id)
                    )
                    """
                )
        if "technologies" in self._all_tables():
            info = self.conn.execute("PRAGMA table_info(technologies)").fetchall()
            pk_cols = [str(r["name"]) for r in info if int(r["pk"]) > 0]
            if pk_cols == ["id"]:
                self.conn.execute("DROP TABLE technologies")
                self.conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS technologies (
                      id TEXT NOT NULL,
                      world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
                      branch_id TEXT NOT NULL REFERENCES world_branches(id) ON DELETE CASCADE,
                      name TEXT NOT NULL,
                      maturity REAL NOT NULL DEFAULT 0.0,
                      cost REAL NOT NULL DEFAULT 1.0,
                      performance REAL NOT NULL DEFAULT 1.0,
                      adoption REAL NOT NULL DEFAULT 0.0,
                      dependencies_json TEXT NOT NULL DEFAULT '[]',
                      lead_org_id TEXT,
                      updated_at TEXT NOT NULL,
                      PRIMARY KEY(world_id, branch_id, id)
                    )
                    """
                )

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

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """Group writes into one SQLite transaction (nested calls join).

        Repository ``save_*`` helpers commit individually.  Callers that need
        all-or-nothing semantics for a logical unit of work (one World tick)
        wrap them in this context: writes within the block share one
        connection, so uncommitted state stays visible, and any failure rolls
        the whole unit back instead of leaving a partial tick behind.
        """
        conn = self.conn
        if conn.in_transaction:
            yield
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise
        else:
            conn.commit()

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
