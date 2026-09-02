"""Control-plane state for background jobs.

Pause/cancel requests are control-plane facts, not job data.  They used to live
inside ``job.job_config["pause_requested"]``, which meant two different
``PersonaCreationJob`` Python objects could fight over one flag:

    _run_job()      job = get_job(job_id)   -> holds this object for minutes
    pause_job()     job = get_job(job_id)   -> sets pause_requested on ITS copy
    _save(job)      the worker writes its stale job_config back and
                    silently deletes the pause request

The fix has three parts:

1. Durable control state in its own table (survives restart/multi-worker).
2. An in-process ``asyncio.Event`` per running job, so a worker does not depend
   on a stale job object at all.
3. A one-way mirror: ``job.job_config["pause_requested"]`` is refreshed from
   the control store on every save, so a stale worker can never clobber it.

Safe-pause semantics: an atomic unit that already started may finish and
checkpoint; once a pause is requested no NEW atomic unit may start.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


def control_now() -> str:
    return datetime.now(UTC).isoformat()


class JobControlState(BaseModel):
    """Durable control-plane state for one job."""

    model_config = ConfigDict(extra="ignore")

    job_id: str
    pause_requested: bool = False
    pause_requested_at: str | None = None
    cancel_requested: bool = False
    cancel_requested_at: str | None = None
    # Bumped on every control write so a stale writer can be detected.
    control_version: int = 0
    updated_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "pause_requested": self.pause_requested,
            "pause_requested_at": self.pause_requested_at,
            "cancel_requested": self.cancel_requested,
            "cancel_requested_at": self.cancel_requested_at,
            "control_version": self.control_version,
            "updated_at": self.updated_at,
        }


CONTROL_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS persona_job_control (
  job_id TEXT PRIMARY KEY,
  pause_requested INTEGER NOT NULL DEFAULT 0,
  pause_requested_at TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  cancel_requested_at TEXT,
  control_version INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT
)
"""


class JobControlRegistry:
    """Durable control state plus in-process pause signals.

    Writes go to SQLite first (so a restart or a second worker still honours
    them) and then to the in-memory event.  Reads check the event first and
    fall back to the durable row, so a pause requested by another process is
    still honoured at the next boundary.
    """

    def __init__(self, database: Any) -> None:
        self._database = database
        self._events: dict[str, asyncio.Event] = {}
        self._cache: dict[str, JobControlState] = {}
        self.ensure_schema()

    # -- schema -------------------------------------------------------------
    def ensure_schema(self) -> None:
        conn = self._conn()
        if conn is None:
            return
        with contextlib.suppress(Exception):
            conn.executescript(CONTROL_TABLE_SQL)
            conn.commit()

    def _conn(self) -> sqlite3.Connection | None:
        database = self._database
        conn = getattr(database, "conn", None)
        return conn if isinstance(conn, sqlite3.Connection) else None

    # -- events -------------------------------------------------------------
    def pause_event(self, job_id: str) -> asyncio.Event:
        event = self._events.get(job_id)
        if event is None:
            event = asyncio.Event()
            self._events[job_id] = event
            state = self.get(job_id)
            if state.pause_requested:
                event.set()
        return event

    def has_event(self, job_id: str) -> bool:
        return job_id in self._events

    def release(self, job_id: str) -> None:
        """Forget in-process state for a finished job (durable row remains)."""

        self._events.pop(job_id, None)
        self._cache.pop(job_id, None)

    # -- reads --------------------------------------------------------------
    def get(self, job_id: str) -> JobControlState:
        cached = self._cache.get(job_id)
        if cached is not None:
            return cached
        state = self._load(job_id)
        self._cache[job_id] = state
        return state

    def _load(self, job_id: str) -> JobControlState:
        conn = self._conn()
        if conn is None:
            return JobControlState(job_id=job_id)
        try:
            row = conn.execute(
                "SELECT job_id, pause_requested, pause_requested_at, cancel_requested, "
                "cancel_requested_at, control_version, updated_at "
                "FROM persona_job_control WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        except Exception:
            return JobControlState(job_id=job_id)
        if row is None:
            return JobControlState(job_id=job_id)
        return JobControlState(
            job_id=str(row["job_id"]),
            pause_requested=bool(row["pause_requested"]),
            pause_requested_at=row["pause_requested_at"],
            cancel_requested=bool(row["cancel_requested"]),
            cancel_requested_at=row["cancel_requested_at"],
            control_version=int(row["control_version"] or 0),
            updated_at=row["updated_at"],
        )

    def pause_requested(self, job_id: str) -> bool:
        """Authoritative pause check: in-process signal OR durable state."""

        event = self._events.get(job_id)
        if event is not None and event.is_set():
            return True
        return bool(self.get(job_id).pause_requested)

    def cancel_requested(self, job_id: str) -> bool:
        return bool(self.get(job_id).cancel_requested)

    def control_version(self, job_id: str) -> int:
        return int(self.get(job_id).control_version)

    # -- writes -------------------------------------------------------------
    def _persist(self, state: JobControlState) -> JobControlState:
        state.control_version = int(state.control_version) + 1
        state.updated_at = control_now()
        self._cache[state.job_id] = state
        conn = self._conn()
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.execute(
                    "INSERT INTO persona_job_control (job_id, pause_requested, "
                    "pause_requested_at, cancel_requested, cancel_requested_at, "
                    "control_version, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(job_id) DO UPDATE SET "
                    "pause_requested=excluded.pause_requested, "
                    "pause_requested_at=excluded.pause_requested_at, "
                    "cancel_requested=excluded.cancel_requested, "
                    "cancel_requested_at=excluded.cancel_requested_at, "
                    "control_version=excluded.control_version, "
                    "updated_at=excluded.updated_at",
                    (
                        state.job_id,
                        1 if state.pause_requested else 0,
                        state.pause_requested_at,
                        1 if state.cancel_requested else 0,
                        state.cancel_requested_at,
                        state.control_version,
                        state.updated_at,
                    ),
                )
                conn.commit()
        return state

    def request_pause(self, job_id: str) -> JobControlState:
        state = self.get(job_id)
        state.pause_requested = True
        state.pause_requested_at = state.pause_requested_at or control_now()
        state = self._persist(state)
        self.pause_event(job_id).set()
        return state

    def clear_pause(self, job_id: str) -> JobControlState:
        """Only valid once the worker reached PAUSED or the user resumed."""

        state = self.get(job_id)
        state.pause_requested = False
        state.pause_requested_at = None
        state = self._persist(state)
        event = self._events.get(job_id)
        if event is not None:
            event.clear()
        return state

    def request_cancel(self, job_id: str) -> JobControlState:
        state = self.get(job_id)
        state.cancel_requested = True
        state.cancel_requested_at = state.cancel_requested_at or control_now()
        return self._persist(state)

    def clear_cancel(self, job_id: str) -> JobControlState:
        state = self.get(job_id)
        state.cancel_requested = False
        state.cancel_requested_at = None
        return self._persist(state)

    def clear_all(self, job_id: str) -> JobControlState:
        state = self.clear_pause(job_id)
        return self.clear_cancel(job_id) if not state.cancel_requested else state

    def snapshot(self) -> dict[str, Any]:
        return {
            "tracked_jobs": sorted(self._events),
            "paused_jobs": sorted(
                job_id for job_id in self._events if self.pause_requested(job_id)
            ),
        }


__all__ = [
    "CONTROL_TABLE_SQL",
    "JobControlRegistry",
    "JobControlState",
    "control_now",
]
