from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from persona_continuum.agent.models import ResearchCapability, ResearchVerificationStatus
from persona_continuum.application._utils import dumps, loads
from persona_continuum.numeric import safe_float


class ResearchCapabilityCacheEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cache_key: str
    agent_id: str
    agent_version: str
    model_id: str
    runtime_source: str
    configuration_fingerprint: str = ""
    capability: ResearchCapability
    error: str | None = None
    expires_at: str | None = None
    cache_ttl_seconds: float | None = None
    updated_at: str


class ResearchCapabilityCache:
    """Durable cache for behavioral research verification.

    The cache deliberately stores only capability metadata and diagnostics;
    no prompts, source content, credentials, or Agent output are persisted.
    Version, model, permission policy, and relevant CLI flags are part of the
    key, so a runtime or policy upgrade cannot inherit an old verification
    result.  Denials have a short TTL so a user changing a local policy can
    recover without waiting for a CLI version change.
    """

    VERIFIED_TTL_SECONDS = 24 * 60 * 60.0
    BLOCKED_TTL_SECONDS = 2 * 60.0
    UNAVAILABLE_TTL_SECONDS = 5 * 60.0
    UNKNOWN_TTL_SECONDS = 60.0

    def __init__(self, database: Any) -> None:
        self.database = database

    @staticmethod
    def make_key(
        *,
        agent_id: str,
        agent_version: str | None,
        model_id: str | None,
        runtime_source: str,
        configuration_fingerprint: str | None = None,
    ) -> str:
        base = "|".join(
            (
                str(agent_id or "").strip(),
                str(agent_version or "unknown").strip(),
                str(model_id or "default").strip(),
                str(runtime_source or "local_cli").strip().lower(),
            )
        )
        if configuration_fingerprint is None:
            return base
        return f"{base}|config={configuration_fingerprint}"

    @staticmethod
    def configuration_fingerprint(
        *,
        agent_version: str | None,
        model_id: str | None,
        runtime_source: str,
        permission_profile: str | None = None,
        research_tools: Any = None,
        research_tool_policy: Any = None,
        cli_flags: Any = None,
    ) -> str:
        """Hash only safe runtime policy metadata; never prompt or source text."""

        payload = {
            "agent_version": str(agent_version or "unknown"),
            "model_id": str(model_id or "default"),
            "runtime_source": str(runtime_source or "local_cli").lower(),
            "permission_profile": str(permission_profile or "research_read_only"),
            "research_tools": (
                ["google_web_search", "web_fetch"]
                if research_tools is None
                else research_tools
            ),
            "research_tool_policy": (
                {"allowed_tools": ["google_web_search", "web_fetch"]}
                if research_tool_policy is None
                else research_tool_policy
            ),
            "cli_flags": {} if cli_flags is None else cli_flags,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]

    @classmethod
    def _ttl_seconds(cls, status: ResearchVerificationStatus) -> float:
        if status == ResearchVerificationStatus.VERIFIED:
            return cls.VERIFIED_TTL_SECONDS
        if status == ResearchVerificationStatus.BLOCKED:
            return cls.BLOCKED_TTL_SECONDS
        if status == ResearchVerificationStatus.UNKNOWN:
            return cls.UNKNOWN_TTL_SECONDS
        return cls.UNAVAILABLE_TTL_SECONDS

    @staticmethod
    def _is_expired(expires_at: str | None) -> bool:
        if not expires_at:
            return False
        try:
            expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            return expires <= datetime.now(UTC)
        except (TypeError, ValueError):
            return True

    def get(
        self,
        *,
        agent_id: str,
        agent_version: str | None,
        model_id: str | None,
        runtime_source: str,
        configuration_fingerprint: str | None = None,
        permission_profile: str | None = None,
        research_tools: Any = None,
        research_tool_policy: Any = None,
        cli_flags: Any = None,
    ) -> ResearchCapabilityCacheEntry | None:
        fingerprint = configuration_fingerprint or self.configuration_fingerprint(
            agent_version=agent_version,
            model_id=model_id,
            runtime_source=runtime_source,
            permission_profile=permission_profile,
            research_tools=research_tools,
            research_tool_policy=research_tool_policy,
            cli_flags=cli_flags,
        )
        key = self.make_key(
            agent_id=agent_id,
            agent_version=agent_version,
            model_id=model_id,
            runtime_source=runtime_source,
            configuration_fingerprint=fingerprint,
        )
        row = self.database.conn.execute(
            "SELECT * FROM research_capability_cache WHERE cache_key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        row_keys = set(row.keys())
        if self._is_expired(row["expires_at"] if "expires_at" in row_keys else None):
            return None
        raw_capability = loads(row["capability_json"] or "{}")
        try:
            capability = ResearchCapability.model_validate(raw_capability or {})
        except Exception:
            # A malformed/legacy cache entry must not become a false PASS.
            capability = ResearchCapability(
                mode="agentic_cli" if str(row["runtime_source"]) == "local_cli" else "none",
                verification_status=ResearchVerificationStatus.UNKNOWN,
                source="cache:invalid_entry",
                verification_method="cache_parse_error",
                verification_error="invalid_cached_capability",
            )
        return ResearchCapabilityCacheEntry(
            cache_key=key,
            agent_id=str(row["agent_id"]),
            agent_version=str(row["agent_version"]),
            model_id=str(row["model_id"]),
            runtime_source=str(row["runtime_source"]),
            configuration_fingerprint=str(row["configuration_fingerprint"] or "")
            if "configuration_fingerprint" in row_keys
            else "",
            capability=capability,
            error=row["error"],
            expires_at=(str(row["expires_at"]) if row["expires_at"] else None)
            if "expires_at" in row_keys
            else None,
            cache_ttl_seconds=(
                safe_float(row["cache_ttl_seconds"], default=None, minimum=0.0)
                if row["cache_ttl_seconds"] is not None
                else None
            )
            if "cache_ttl_seconds" in row_keys
            else None,
            updated_at=str(row["updated_at"]),
        )

    def put(
        self,
        *,
        agent_id: str,
        agent_version: str | None,
        model_id: str | None,
        runtime_source: str,
        capability: ResearchCapability,
        error: str | None = None,
        configuration_fingerprint: str | None = None,
        permission_profile: str | None = None,
        research_tools: Any = None,
        research_tool_policy: Any = None,
        cli_flags: Any = None,
    ) -> ResearchCapabilityCacheEntry:
        version = str(agent_version or "unknown")
        model = str(model_id or "default")
        source = str(runtime_source or "local_cli").lower()
        fingerprint = configuration_fingerprint or self.configuration_fingerprint(
            agent_version=version,
            model_id=model,
            runtime_source=source,
            permission_profile=permission_profile,
            research_tools=research_tools,
            research_tool_policy=research_tool_policy,
            cli_flags=cli_flags,
        )
        key = self.make_key(
            agent_id=agent_id,
            agent_version=version,
            model_id=model,
            runtime_source=source,
            configuration_fingerprint=fingerprint,
        )
        now = datetime.now(UTC).isoformat()
        ttl_seconds = self._ttl_seconds(capability.verification_status)
        expires_at = datetime.fromtimestamp(
            datetime.now(UTC).timestamp() + ttl_seconds, UTC
        ).isoformat()
        self.database.conn.execute(
            """
            INSERT INTO research_capability_cache (
              cache_key, agent_id, agent_version, model_id, runtime_source,
              configuration_fingerprint, verification_status, capability_json,
              verification_method, verified_at, error, expires_at,
              cache_ttl_seconds, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
              configuration_fingerprint=excluded.configuration_fingerprint,
              verification_status=excluded.verification_status,
              capability_json=excluded.capability_json,
              verification_method=excluded.verification_method,
              verified_at=excluded.verified_at,
              error=excluded.error,
              expires_at=excluded.expires_at,
              cache_ttl_seconds=excluded.cache_ttl_seconds,
              updated_at=excluded.updated_at
            """,
            (
                key,
                str(agent_id),
                version,
                model,
                source,
                fingerprint,
                capability.verification_status.value,
                dumps(capability.model_dump(mode="json")),
                capability.verification_method,
                capability.verified_at,
                error or capability.verification_error,
                expires_at,
                ttl_seconds,
                now,
            ),
        )
        self.database.conn.commit()
        return self.get(
            agent_id=agent_id,
            agent_version=version,
            model_id=model,
            runtime_source=source,
            configuration_fingerprint=fingerprint,
        ) or ResearchCapabilityCacheEntry(
            cache_key=key,
            agent_id=str(agent_id),
            agent_version=version,
            model_id=model,
            runtime_source=source,
            configuration_fingerprint=fingerprint,
            capability=capability,
            error=error,
            expires_at=expires_at,
            cache_ttl_seconds=ttl_seconds,
            updated_at=now,
        )

    def latest_for_runtime(
        self, *, agent_id: str, agent_version: str | None, runtime_source: str
    ) -> ResearchCapabilityCacheEntry | None:
        """Return the newest model-specific result for UI diagnostics."""

        rows = self.database.conn.execute(
            """
            SELECT * FROM research_capability_cache
            WHERE agent_id = ? AND agent_version = ? AND runtime_source = ?
            ORDER BY updated_at DESC LIMIT 1
            """,
            (str(agent_id), str(agent_version or "unknown"), str(runtime_source or "local_cli")),
        ).fetchone()
        if rows is None:
            return None
        row_keys = set(rows.keys())
        return self.get(
            agent_id=str(rows["agent_id"]),
            agent_version=str(rows["agent_version"]),
            model_id=str(rows["model_id"]),
            runtime_source=str(rows["runtime_source"]),
            configuration_fingerprint=(
                str(rows["configuration_fingerprint"] or "")
                if "configuration_fingerprint" in row_keys
                else None
            ),
        )

    def invalidate_runtime_version(self, *, agent_id: str, agent_version: str) -> int:
        """Remove entries for an older version while retaining audit history.

        Normal resolution does not need this helper because the version is in
        the key.  It is provided for explicit rescan/maintenance flows.
        """

        cursor = self.database.conn.execute(
            "DELETE FROM research_capability_cache WHERE agent_id = ? AND agent_version != ?",
            (agent_id, str(agent_version)),
        )
        self.database.conn.commit()
        return int(cursor.rowcount or 0)
