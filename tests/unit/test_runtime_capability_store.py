"""Persistent independent-session concurrency capability (P0.5 A/B/C/E/L)."""

from __future__ import annotations

import hashlib

from persona_continuum.performance.concurrency_cache import (
    IndependentSessionConcurrencyCache,
    IndependentSessionProbe,
)
from persona_continuum.performance.runtime_capability_store import (
    CapabilityState,
    ProbeStatus,
    RuntimeCapabilityStore,
    RuntimeIdentity,
)


def _identity(
    *,
    binary: str = "bin-a",
    version: str = "1.0.25",
    model: str = "grok-4.6",
    credential: str = "cred-1",
    origin: str = "xai",
) -> RuntimeIdentity:
    return RuntimeIdentity(
        adapter_id="grok",
        binary_identity=binary,
        binary_version=version,
        model_id=model,
        credential_identity_hash=credential,
        runtime_origin=origin,
    )


def _verify(store: RuntimeCapabilityStore, identity: RuntimeIdentity, width: int = 4):
    return store.record_probe(
        identity,
        probe_status=ProbeStatus.VERIFIED,
        max_verified=width,
        parallel_independent_sessions_verified=True,
        probe_sample_count=7,
    )


def test_probe_survives_process_restart(tmp_path) -> None:
    path = tmp_path / "runtime.sqlite"
    identity = _identity()
    writer = RuntimeCapabilityStore(path)
    _verify(writer, identity, 4)
    writer.close()
    # A separate store instance is what a later App start would create.
    reader = RuntimeCapabilityStore(path)
    assert reader.verified_limit(identity) == 4
    assert reader.get(identity).state == CapabilityState.VERIFIED
    reader.close()


def test_ttl_expiry_falls_back_to_one(tmp_path) -> None:
    store = RuntimeCapabilityStore(tmp_path / "runtime.sqlite")
    identity = _identity()
    _verify(store, identity, 4)
    store.conn.execute(
        "UPDATE runtime_concurrency_capabilities SET expires_at = '2000-01-01T00:00:00+00:00'"
    )
    store.conn.commit()
    assert store.get(identity).state == CapabilityState.STALE
    assert store.verified_limit(identity) == 1


def test_binary_version_change_does_not_reuse(tmp_path) -> None:
    store = RuntimeCapabilityStore(tmp_path / "runtime.sqlite")
    _verify(store, _identity(version="1.0.25"), 4)
    assert store.verified_limit(_identity(version="1.1.0")) == 1


def test_model_change_does_not_reuse(tmp_path) -> None:
    store = RuntimeCapabilityStore(tmp_path / "runtime.sqlite")
    _verify(store, _identity(model="grok-4.6"), 4)
    assert store.verified_limit(_identity(model="grok-4.7")) == 1


def test_credential_change_does_not_reuse(tmp_path) -> None:
    store = RuntimeCapabilityStore(tmp_path / "runtime.sqlite")
    _verify(store, _identity(credential="account-a"), 4)
    assert store.verified_limit(_identity(credential="account-b")) == 1
    assert store.verified_limit(_identity(credential="account-a")) == 4


def test_runtime_downgrade_keeps_verified_max(tmp_path) -> None:
    store = RuntimeCapabilityStore(tmp_path / "runtime.sqlite")
    identity = _identity()
    _verify(store, identity, 4)
    store.record_runtime_downgrade(identity, recommended=2, reason="provider_concurrency_limit")
    capability = store.get(identity)
    assert capability.max_verified == 4
    assert capability.current_recommended == 2
    assert store.verified_limit(identity) == 2
    assert capability.source == "runtime_downgrade"


def test_correlation_corruption_invalidates(tmp_path) -> None:
    store = RuntimeCapabilityStore(tmp_path / "runtime.sqlite")
    identity = _identity()
    _verify(store, identity, 4)
    store.invalidate(identity, reason="session_correlation_failure")
    capability = store.get(identity)
    assert capability.state == CapabilityState.INVALIDATED
    assert store.verified_limit(identity) == 1


def test_no_raw_credential_is_persisted(tmp_path) -> None:
    path = tmp_path / "runtime.sqlite"
    store = RuntimeCapabilityStore(path)
    secret = "xai-super-secret-token-value"
    digest = hashlib.sha256(secret.encode()).hexdigest()[:32]
    identity = _identity(credential=digest)
    _verify(store, identity, 4)
    raw_db = path.read_bytes()
    assert secret.encode() not in raw_db
    # Redacted by default when the identity is echoed to telemetry.
    assert identity.as_dict()["credential_identity_hash"] == "set"
    assert identity.as_dict(include_credential=True)["credential_identity_hash"] == digest


def test_failed_payment_probe_is_not_verified(tmp_path) -> None:
    store = RuntimeCapabilityStore(tmp_path / "runtime.sqlite")
    identity = _identity()
    capability = store.record_probe(
        identity,
        probe_status=ProbeStatus.FAILED_PAYMENT,
        max_verified=1,
        parallel_independent_sessions_verified=False,
    )
    assert capability.state == CapabilityState.UNVERIFIED
    assert capability.last_failure_kind == ProbeStatus.FAILED_PAYMENT
    assert store.verified_limit(identity) == 1


def test_cache_facade_is_persistent_backed(tmp_path) -> None:
    cache = IndependentSessionConcurrencyCache(
        store=RuntimeCapabilityStore(tmp_path / "runtime.sqlite")
    )
    cache.store_probe(
        IndependentSessionProbe(
            adapter_id="grok",
            max_verified=4,
            status=ProbeStatus.VERIFIED,
            model_id="grok-4.6",
        )
    )
    assert cache.verified_limit("grok", model_id="grok-4.6") == 4
    assert cache.verified_limit("grok", model_id="grok-4.7") == 1
