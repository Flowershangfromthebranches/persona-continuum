"""Persistent independent-session concurrency capabilities.

A live probe runs in its own process (``scripts/grok_independent_session_probe.py``)
and exits.  Keeping the verified result only in that process' memory meant the
next App start fell back to one worker, so "verified 4-way" never survived a
restart.  This store persists the capability in the project's SQLite database.

Identity is explicit and complete: adapter + CLI binary identity/version +
model + credential identity hash + runtime origin + probe contract version.
Any of those changing produces a different row, so a stale verification can
never be applied to a new CLI/account/model.

Security: only an irreversible ``credential_identity_hash`` is stored.  Raw API
keys, tokens, cookies and account ids never reach this table.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

DEFAULT_CAPABILITY_TTL_SECONDS = 24 * 60 * 60
# A runtime downgrade (temporary provider pressure) expires well before the
# verified capability itself, so recovery does not require a full re-probe.
DEFAULT_DOWNGRADE_TTL_SECONDS = 30 * 60
PROBE_CONTRACT_VERSION = "independent-session-concurrency-v1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_concurrency_capabilities (
  capability_key TEXT PRIMARY KEY,
  adapter_id TEXT NOT NULL,
  binary_identity TEXT NOT NULL DEFAULT '',
  binary_version TEXT NOT NULL DEFAULT '',
  model_id TEXT NOT NULL DEFAULT '',
  credential_identity_hash TEXT NOT NULL DEFAULT '',
  runtime_origin TEXT NOT NULL DEFAULT '',
  probe_version TEXT NOT NULL DEFAULT '',
  max_verified_independent_sessions INTEGER NOT NULL DEFAULT 1,
  parallel_independent_sessions_verified INTEGER NOT NULL DEFAULT 0,
  parallel_same_session_verified INTEGER NOT NULL DEFAULT 0,
  current_recommended INTEGER NOT NULL DEFAULT 1,
  probe_status TEXT NOT NULL DEFAULT 'unverified',
  probe_sample_count INTEGER NOT NULL DEFAULT 0,
  verified_at TEXT,
  expires_at TEXT,
  last_runtime_downgrade_at TEXT,
  downgrade_reason TEXT,
  downgrade_expires_at TEXT,
  last_failure_kind TEXT,
  last_failure_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runtime_concurrency_adapter
  ON runtime_concurrency_capabilities(adapter_id, model_id);
"""


class ProbeStatus:
    """Machine-readable outcome of one synthetic concurrency probe."""

    NOT_RUN = "not_run"
    VERIFIED = "verified"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"
    FAILED_ENVIRONMENT = "failed_environment"
    FAILED_AUTH = "failed_auth"
    FAILED_PAYMENT = "failed_payment"
    FAILED_PROVIDER_QUOTA = "failed_provider_quota"
    FAILED_CORRELATION = "failed_correlation"
    FAILED_RUNTIME = "failed_runtime"


class CapabilityState:
    """Derived state of a stored capability at read time."""

    MISSING = "missing"
    VERIFIED = "verified"
    PARTIAL = "partial"
    STALE = "stale"
    INVALIDATED = "invalidated"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """Everything a concurrency capability is bound to."""

    adapter_id: str
    binary_identity: str = ""
    binary_version: str = ""
    model_id: str = ""
    credential_identity_hash: str = ""
    runtime_origin: str = ""
    probe_version: str = PROBE_CONTRACT_VERSION

    @property
    def key(self) -> str:
        material = "\x1f".join(
            (
                self.adapter_id,
                self.binary_identity,
                self.binary_version,
                self.model_id,
                self.credential_identity_hash,
                self.runtime_origin,
                self.probe_version,
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def as_dict(self, *, include_credential: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "adapter_id": self.adapter_id,
            "binary_identity": self.binary_identity,
            "binary_version": self.binary_version,
            "model_id": self.model_id,
            "runtime_origin": self.runtime_origin,
            "probe_version": self.probe_version,
        }
        # Never echo the credential hash unless a caller explicitly asks;
        # telemetry uses the redacted form.
        if include_credential:
            payload["credential_identity_hash"] = self.credential_identity_hash
        else:
            payload["credential_identity_hash"] = (
                "set" if self.credential_identity_hash else ""
            )
        return payload


@dataclass(frozen=True, slots=True)
class RuntimeConcurrencyCapability:
    identity: RuntimeIdentity
    max_verified: int = 1
    current_recommended: int = 1
    parallel_independent_sessions_verified: bool = False
    parallel_same_session_verified: bool = False
    probe_status: str = ProbeStatus.UNVERIFIED
    probe_sample_count: int = 0
    state: str = CapabilityState.UNVERIFIED
    source: str = "fallback"
    verified_at: str | None = None
    expires_at: str | None = None
    downgrade_reason: str | None = None
    downgrade_expires_at: str | None = None
    last_failure_kind: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.state in {CapabilityState.VERIFIED, CapabilityState.PARTIAL}

    @property
    def effective_limit(self) -> int:
        if not self.verified or not self.parallel_independent_sessions_verified:
            return 1
        return max(1, min(int(self.max_verified), int(self.current_recommended)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "source": self.source,
            "verified": self.verified,
            "max_verified": int(self.max_verified),
            "current_recommended": int(self.current_recommended),
            "effective_limit": self.effective_limit,
            "parallel_independent_sessions_verified": bool(
                self.parallel_independent_sessions_verified
            ),
            "parallel_same_session_verified": bool(self.parallel_same_session_verified),
            "probe_status": self.probe_status,
            "probe_sample_count": int(self.probe_sample_count),
            "verified_at": self.verified_at,
            "expires_at": self.expires_at,
            "downgrade_reason": self.downgrade_reason,
            "downgrade_expires_at": self.downgrade_expires_at,
            "last_failure_kind": self.last_failure_kind,
            "identity": self.identity.as_dict(),
        }


def _now() -> datetime:
    return datetime.now(UTC)


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _expired(value: str | None, *, now: datetime) -> bool:
    stamp = _parse(value)
    return stamp is not None and stamp <= now


class RuntimeCapabilityStore:
    """SQLite-backed store for verified independent-session capabilities."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        connection: sqlite3.Connection | None = None,
        ttl_seconds: float = DEFAULT_CAPABILITY_TTL_SECONDS,
        ensure_schema: bool = True,
    ) -> None:
        self.path = Path(path).expanduser() if path is not None else None
        self._conn: sqlite3.Connection | None = connection
        self._ensure_schema = bool(ensure_schema)
        self.ttl_seconds = max(60.0, float(ttl_seconds))
        self._ensured = False

    @classmethod
    def in_memory(
        cls, *, ttl_seconds: float = DEFAULT_CAPABILITY_TTL_SECONDS
    ) -> RuntimeCapabilityStore:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return cls(connection=conn, ttl_seconds=ttl_seconds)

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            if self.path is None:
                raise RuntimeError("runtime_capability_store_path_required")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA busy_timeout = 30000")
        if not self._ensured and self._ensure_schema:
            self._conn.executescript(_SCHEMA)
            self._ensured = True
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            self._ensured = False

    # -- write -------------------------------------------------------------

    def record_probe(
        self,
        identity: RuntimeIdentity,
        *,
        probe_status: str,
        max_verified: int = 1,
        parallel_independent_sessions_verified: bool = False,
        parallel_same_session_verified: bool = False,
        probe_sample_count: int = 0,
        metadata: dict[str, Any] | None = None,
        ttl_seconds: float | None = None,
    ) -> RuntimeConcurrencyCapability:
        """Persist one probe result.  Only a fully successful probe is verified."""

        now = _now()
        verified = bool(
            parallel_independent_sessions_verified
            and probe_status in {ProbeStatus.VERIFIED, ProbeStatus.PARTIAL}
            and int(max_verified) > 1
        )
        ttl = self.ttl_seconds if ttl_seconds is None else max(60.0, float(ttl_seconds))
        expires_iso = datetime.fromtimestamp(now.timestamp() + ttl, tz=UTC).isoformat()
        capability_key = identity.key
        existing = self._row(capability_key)
        failure_kind = None
        if probe_status.startswith("failed_"):
            failure_kind = probe_status
        self.conn.execute(
            """
            INSERT INTO runtime_concurrency_capabilities (
              capability_key, adapter_id, binary_identity, binary_version, model_id,
              credential_identity_hash, runtime_origin, probe_version,
              max_verified_independent_sessions, parallel_independent_sessions_verified,
              parallel_same_session_verified, current_recommended, probe_status,
              probe_sample_count, verified_at, expires_at, last_runtime_downgrade_at,
              downgrade_reason, downgrade_expires_at, last_failure_kind, last_failure_at,
              metadata_json, updated_at
            ) VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?
            )
            ON CONFLICT(capability_key) DO UPDATE SET
              max_verified_independent_sessions = excluded.max_verified_independent_sessions,
              parallel_independent_sessions_verified =
                excluded.parallel_independent_sessions_verified,
              parallel_same_session_verified = excluded.parallel_same_session_verified,
              current_recommended = excluded.current_recommended,
              probe_status = excluded.probe_status,
              probe_sample_count = excluded.probe_sample_count,
              verified_at = excluded.verified_at,
              expires_at = excluded.expires_at,
              last_failure_kind = excluded.last_failure_kind,
              last_failure_at = excluded.last_failure_at,
              metadata_json = excluded.metadata_json,
              updated_at = excluded.updated_at
            """,
            (
                capability_key,
                identity.adapter_id,
                identity.binary_identity,
                identity.binary_version,
                identity.model_id,
                identity.credential_identity_hash,
                identity.runtime_origin,
                identity.probe_version,
                max(1, int(max_verified)),
                1 if verified else 0,
                1 if parallel_same_session_verified else 0,
                max(1, int(max_verified)) if verified else 1,
                probe_status,
                max(0, int(probe_sample_count)),
                now.isoformat() if verified else (existing["verified_at"] if existing else None),
                expires_iso if verified else None,
                failure_kind,
                now.isoformat() if failure_kind else None,
                json.dumps(metadata or {}, ensure_ascii=False, default=str),
                now.isoformat(),
            ),
        )
        self.conn.commit()
        resolved = self.get(identity)
        assert resolved is not None  # noqa: S101 - just written
        return resolved

    def record_runtime_downgrade(
        self,
        identity: RuntimeIdentity,
        *,
        recommended: int,
        reason: str,
        ttl_seconds: float = DEFAULT_DOWNGRADE_TTL_SECONDS,
    ) -> None:
        """Temporarily lower the recommended concurrency without erasing the probe.

        ``max_verified`` (what the probe proved) is preserved; only
        ``current_recommended`` moves, and only until ``downgrade_expires_at``.
        """

        now = _now()
        expires = datetime.fromtimestamp(now.timestamp() + max(60.0, float(ttl_seconds)), tz=UTC)
        self.conn.execute(
            """
            UPDATE runtime_concurrency_capabilities
               SET current_recommended = ?,
                   last_runtime_downgrade_at = ?,
                   downgrade_reason = ?,
                   downgrade_expires_at = ?,
                   updated_at = ?
             WHERE capability_key = ?
            """,
            (
                max(1, int(recommended)),
                now.isoformat(),
                str(reason)[:200],
                expires.isoformat(),
                now.isoformat(),
                identity.key,
            ),
        )
        self.conn.commit()

    def invalidate(self, identity: RuntimeIdentity, *, reason: str) -> None:
        """Hard corruption: session collisions/cross-talk.  Require a new probe."""

        self._invalidate_where("capability_key = ?", (identity.key,), reason=reason)

    def invalidate_all(self, adapter_id: str, *, reason: str) -> None:
        """Invalidate every stored identity for one adapter."""

        self._invalidate_where("adapter_id = ?", (adapter_id,), reason=reason)

    def _invalidate_where(
        self, clause: str, parameters: tuple[Any, ...], *, reason: str
    ) -> None:
        now = _now()
        self.conn.execute(
            f"""
            UPDATE runtime_concurrency_capabilities
               SET parallel_independent_sessions_verified = 0,
                   current_recommended = 1,
                   probe_status = ?,
                   last_failure_kind = ?,
                   last_failure_at = ?,
                   updated_at = ?
             WHERE {clause}
            """,
            (
                ProbeStatus.FAILED_CORRELATION,
                str(reason)[:80] or ProbeStatus.FAILED_CORRELATION,
                now.isoformat(),
                now.isoformat(),
                *parameters,
            ),
        )
        self.conn.commit()

    # -- read --------------------------------------------------------------

    def _row(self, capability_key: str) -> sqlite3.Row | None:
        return cast(
            "sqlite3.Row | None",
            self.conn.execute(
                "SELECT * FROM runtime_concurrency_capabilities WHERE capability_key = ?",
                (capability_key,),
            ).fetchone(),
        )

    def get(self, identity: RuntimeIdentity) -> RuntimeConcurrencyCapability | None:
        row = self._row(identity.key)
        if row is None:
            return None
        return self._to_capability(row, identity)

    def resolution(self, identity: RuntimeIdentity) -> RuntimeConcurrencyCapability:
        """Capability view, defaulting to fail-closed 1 worker when absent."""

        stored = self.get(identity)
        if stored is not None:
            return stored
        return RuntimeConcurrencyCapability(identity=identity)

    def verified_limit(self, identity: RuntimeIdentity) -> int:
        stored = self.get(identity)
        return stored.effective_limit if stored is not None else 1

    def snapshot(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT capability_key FROM runtime_concurrency_capabilities "
            "ORDER BY updated_at DESC"
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            stored = self._row(str(row["capability_key"]))
            if stored is None:
                continue
            identity = RuntimeIdentity(
                adapter_id=str(stored["adapter_id"]),
                binary_identity=str(stored["binary_identity"]),
                binary_version=str(stored["binary_version"]),
                model_id=str(stored["model_id"]),
                credential_identity_hash=str(stored["credential_identity_hash"]),
                runtime_origin=str(stored["runtime_origin"]),
                probe_version=str(stored["probe_version"]),
            )
            out.append(self._to_capability(stored, identity).as_dict())
        return out

    def _to_capability(
        self, row: sqlite3.Row, identity: RuntimeIdentity
    ) -> RuntimeConcurrencyCapability:
        now = _now()
        stored_identity = RuntimeIdentity(
            adapter_id=str(row["adapter_id"]),
            binary_identity=str(row["binary_identity"]),
            binary_version=str(row["binary_version"]),
            model_id=str(row["model_id"]),
            credential_identity_hash=str(row["credential_identity_hash"]),
            runtime_origin=str(row["runtime_origin"]),
            probe_version=str(row["probe_version"]),
        )
        probe_status = str(row["probe_status"] or ProbeStatus.UNVERIFIED)
        max_verified = int(row["max_verified_independent_sessions"] or 1)
        verified_flag = bool(row["parallel_independent_sessions_verified"])
        current_recommended = int(row["current_recommended"] or 1)
        downgrade_expires = row["downgrade_expires_at"]
        if _expired(downgrade_expires, now=now) and downgrade_expires:
            # Provider pressure cleared: allow recovery to the proven width.
            current_recommended = max_verified
            downgrade_expires = None
        if _expired(row["expires_at"], now=now):
            state = CapabilityState.STALE
        elif not verified_flag:
            state = (
                CapabilityState.INVALIDATED
                if probe_status == ProbeStatus.FAILED_CORRELATION
                else CapabilityState.UNVERIFIED
            )
        elif probe_status == ProbeStatus.PARTIAL:
            state = CapabilityState.PARTIAL
        else:
            state = CapabilityState.VERIFIED
        source = "persistent_cache"
        if row["last_runtime_downgrade_at"] and not _expired(downgrade_expires, now=now):
            source = "runtime_downgrade"
        if state != CapabilityState.VERIFIED and not verified_flag:
            source = "fallback"
        try:
            metadata = dict(json.loads(str(row["metadata_json"] or "{}")))
        except (TypeError, ValueError):
            metadata = {}
        return RuntimeConcurrencyCapability(
            identity=stored_identity,
            max_verified=max_verified,
            current_recommended=current_recommended,
            parallel_independent_sessions_verified=verified_flag,
            parallel_same_session_verified=bool(row["parallel_same_session_verified"]),
            probe_status=probe_status,
            probe_sample_count=int(row["probe_sample_count"] or 0),
            state=state,
            source=source,
            verified_at=row["verified_at"],
            expires_at=row["expires_at"],
            downgrade_reason=row["downgrade_reason"],
            downgrade_expires_at=downgrade_expires,
            last_failure_kind=row["last_failure_kind"],
            metadata=metadata,
        )


__all__ = [
    "DEFAULT_CAPABILITY_TTL_SECONDS",
    "DEFAULT_DOWNGRADE_TTL_SECONDS",
    "PROBE_CONTRACT_VERSION",
    "CapabilityState",
    "ProbeStatus",
    "RuntimeCapabilityStore",
    "RuntimeConcurrencyCapability",
    "RuntimeIdentity",
]
