"""Verified independent-session concurrency, never a guess.

Live probes write here.  Unverified adapters stay at 1.  The capability is
bound to the full runtime identity (adapter, CLI binary identity/version,
model, credential identity hash, runtime origin, probe contract version) and
persisted in the project's SQLite database so a probe run in one process is
visible to the next App start.

``IndependentSessionConcurrencyCache()`` constructed bare is an in-memory store
for unit tests; ``default_concurrency_cache()`` is the persistent, app-wide one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from persona_continuum.performance.runtime_capability_store import (
    DEFAULT_CAPABILITY_TTL_SECONDS,
    CapabilityState,
    ProbeStatus,
    RuntimeCapabilityStore,
    RuntimeConcurrencyCapability,
    RuntimeIdentity,
)

__all__ = [
    "DEFAULT_CAPABILITY_TTL_SECONDS",
    "CapabilityState",
    "IndependentSessionConcurrencyCache",
    "IndependentSessionProbe",
    "ProbeStatus",
    "RuntimeConcurrencyCapability",
    "RuntimeIdentity",
    "configure_default_runtime_capability_store",
    "default_concurrency_cache",
    "default_runtime_capability_store",
    "resolve_independent_session_capability",
    "verified_independent_session_limit",
]


@dataclass
class IndependentSessionProbe:
    """One probe result (identity + outcome).  Compatibility shape for callers."""

    adapter_id: str
    max_verified: int = 1
    status: str = ProbeStatus.UNVERIFIED
    probed_at: float | None = None
    binary_identity: str = ""
    binary_version: str = ""
    model_id: str = ""
    credential_identity_hash: str = ""
    runtime_origin: str = ""
    probe_version: str = ""
    probe_sample_count: int = 0
    parallel_same_session_verified: bool = False
    speedup_valid: bool = False
    two_speedup: float | None = None
    four_speedup: float | None = None
    last_failure_kind: str | None = None
    serial_s: float | None = None
    two_way_s: float | None = None
    four_way_s: float | None = None
    notes: dict[str, Any] = field(default_factory=dict)

    def identity(self) -> RuntimeIdentity:
        return RuntimeIdentity(
            adapter_id=self.adapter_id,
            binary_identity=self.binary_identity,
            binary_version=self.binary_version,
            model_id=self.model_id,
            credential_identity_hash=self.credential_identity_hash,
            runtime_origin=self.runtime_origin or self.adapter_id,
            probe_version=self.probe_version or "independent-session-concurrency-v1",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "max_verified": self.max_verified,
            "status": self.status,
            "probed_at": self.probed_at,
            "binary_identity": self.binary_identity,
            "binary_version": self.binary_version,
            "model_id": self.model_id,
            "credential_identity_hash": "set" if self.credential_identity_hash else "",
            "runtime_origin": self.runtime_origin,
            "probe_version": self.probe_version,
            "probe_sample_count": self.probe_sample_count,
            "parallel_same_session_verified": self.parallel_same_session_verified,
            "speedup_valid": self.speedup_valid,
            "two_speedup": self.two_speedup,
            "four_speedup": self.four_speedup,
            "last_failure_kind": self.last_failure_kind,
            "serial_s": self.serial_s,
            "two_way_s": self.two_way_s,
            "four_way_s": self.four_way_s,
            "notes": dict(self.notes),
        }


class IndependentSessionConcurrencyCache:
    """Identity-bound capability cache over a :class:`RuntimeCapabilityStore`.

    Bare construction uses an in-memory store (unit tests).  Production uses the
    persistent store created by :func:`default_concurrency_cache`.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_CAPABILITY_TTL_SECONDS,
        store: RuntimeCapabilityStore | None = None,
        path: Path | str | None = None,
    ) -> None:
        if store is not None:
            self._store = store
        elif path is not None:
            self._store = RuntimeCapabilityStore(path, ttl_seconds=ttl_seconds)
        else:
            self._store = RuntimeCapabilityStore.in_memory(ttl_seconds=ttl_seconds)
        self.ttl_seconds = max(60.0, float(ttl_seconds))

    @property
    def store(self) -> RuntimeCapabilityStore:
        """Underlying persistent store (also supports record_runtime_downgrade)."""

        return self._store

    def identity(
        self,
        adapter_id: str,
        *,
        binary_identity: str = "",
        binary_version: str = "",
        model_id: str = "",
        credential_identity_hash: str = "",
        runtime_origin: str = "",
        probe_version: str = "",
    ) -> RuntimeIdentity:
        return RuntimeIdentity(
            adapter_id=adapter_id,
            binary_identity=binary_identity,
            binary_version=binary_version,
            model_id=model_id,
            credential_identity_hash=credential_identity_hash,
            runtime_origin=runtime_origin or adapter_id,
            probe_version=probe_version or "independent-session-concurrency-v1",
        )

    def store_probe(self, probe: IndependentSessionProbe) -> RuntimeConcurrencyCapability:
        return self._store.record_probe(
            probe.identity(),
            probe_status=probe.status,
            max_verified=probe.max_verified,
            parallel_independent_sessions_verified=(
                probe.status in {ProbeStatus.VERIFIED, ProbeStatus.PARTIAL}
                and probe.max_verified > 1
            ),
            parallel_same_session_verified=probe.parallel_same_session_verified,
            probe_sample_count=probe.probe_sample_count,
            metadata={
                "speedup_valid": probe.speedup_valid,
                "two_speedup": probe.two_speedup,
                "four_speedup": probe.four_speedup,
                "serial_s": probe.serial_s,
                "two_way_s": probe.two_way_s,
                "four_way_s": probe.four_way_s,
                "notes": dict(probe.notes),
            },
        )

    # -- compatibility facade ------------------------------------------------

    def get(
        self,
        adapter_id: str,
        *,
        binary_identity: str = "",
        binary_version: str = "",
        model_id: str = "",
        credential_identity_hash: str = "",
        runtime_origin: str = "",
    ) -> RuntimeConcurrencyCapability | None:
        return self.store.get(
            self.identity(
                adapter_id,
                binary_identity=binary_identity,
                binary_version=binary_version,
                model_id=model_id,
                credential_identity_hash=credential_identity_hash,
                runtime_origin=runtime_origin,
            )
        )

    def invalidate(self, adapter_id: str, *, reason: str = "corruption") -> None:
        self.store.invalidate_all(adapter_id, reason=reason)

    def verified_limit(
        self,
        adapter_id: str,
        *,
        binary_identity: str = "",
        binary_version: str = "",
        model_id: str = "",
        credential_identity_hash: str = "",
        runtime_origin: str = "",
    ) -> int:
        entry = self.get(
            adapter_id,
            binary_identity=binary_identity,
            binary_version=binary_version,
            model_id=model_id,
            credential_identity_hash=credential_identity_hash,
            runtime_origin=runtime_origin,
        )
        if entry is None:
            return 1
        return entry.effective_limit

    def resolution_for(self, identity: RuntimeIdentity) -> RuntimeConcurrencyCapability:
        return self.store.resolution(identity)

    def snapshot(self) -> list[dict[str, Any]]:
        return self.store.snapshot()


_DEFAULT_CACHE: IndependentSessionConcurrencyCache | None = None
_DEFAULT_STORE: RuntimeCapabilityStore | None = None


def default_runtime_capability_store() -> RuntimeCapabilityStore:
    """Process-wide persistent store in the app data directory."""

    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        from persona_continuum.config import Config

        config = Config()
        _DEFAULT_STORE = RuntimeCapabilityStore(
            config.database_path,
            ttl_seconds=getattr(
                config, "runtime_concurrency_capability_ttl_seconds", DEFAULT_CAPABILITY_TTL_SECONDS
            ),
        )
    return _DEFAULT_STORE


def configure_default_runtime_capability_store(
    path: Path | str | None = None,
    *,
    ttl_seconds: float | None = None,
) -> IndependentSessionConcurrencyCache:
    """Bind the process-wide capability cache to the application data directory."""

    global _DEFAULT_STORE, _DEFAULT_CACHE
    if path is None:
        from persona_continuum.config import Config

        config = Config()
        path = config.database_path
        if ttl_seconds is None:
            ttl_seconds = getattr(
                config,
                "runtime_concurrency_capability_ttl_seconds",
                DEFAULT_CAPABILITY_TTL_SECONDS,
            )
    _DEFAULT_STORE = RuntimeCapabilityStore(
        path,
        ttl_seconds=DEFAULT_CAPABILITY_TTL_SECONDS if ttl_seconds is None else ttl_seconds,
    )
    _DEFAULT_CACHE = IndependentSessionConcurrencyCache(store=_DEFAULT_STORE)
    return _DEFAULT_CACHE


def default_concurrency_cache() -> IndependentSessionConcurrencyCache:
    global _DEFAULT_CACHE
    if _DEFAULT_CACHE is None:
        _DEFAULT_CACHE = IndependentSessionConcurrencyCache(
            store=default_runtime_capability_store()
        )
    return _DEFAULT_CACHE


def resolve_independent_session_capability(
    adapter_id: str | None,
    *,
    binary_identity: str = "",
    binary_version: str = "",
    model_id: str = "",
    credential_identity_hash: str = "",
    runtime_origin: str = "",
) -> RuntimeConcurrencyCapability:
    if not adapter_id:
        return RuntimeConcurrencyCapability(identity=RuntimeIdentity(adapter_id=""))
    return default_concurrency_cache().resolution_for(
        RuntimeIdentity(
            adapter_id=str(adapter_id),
            binary_identity=binary_identity,
            binary_version=binary_version,
            model_id=model_id,
            credential_identity_hash=credential_identity_hash,
            runtime_origin=runtime_origin or str(adapter_id),
        )
    )


def verified_independent_session_limit(adapter_id: str | None, **kwargs: str) -> int:
    if not adapter_id:
        return 1
    return resolve_independent_session_capability(str(adapter_id), **kwargs).effective_limit
