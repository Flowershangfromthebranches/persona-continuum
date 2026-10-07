"""Probe report validity: no speedup from a failed wave (P0.5 F/G/N)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from persona_continuum.performance.concurrency_cache import (
    IndependentSessionConcurrencyCache,
)
from persona_continuum.performance.provider_failure import (
    classify_probe_failure,
    derive_max_verified,
    derive_probe_status,
    speedup_or_none,
)
from persona_continuum.performance.runtime_capability_store import (
    CapabilityState,
    ProbeStatus,
    RuntimeCapabilityStore,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_probe_module():
    path = REPO_ROOT / "scripts" / "grok_independent_session_probe.py"
    spec = importlib.util.spec_from_file_location("grok_probe_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_speedup_is_null_when_wave_incomplete() -> None:
    wave = {
        "correlation_ok": False,
        "success_count": 0,
        "requested_width": 4,
        "wall_s": 12.5,
    }
    assert speedup_or_none(10.0, 4, wave) is None


def test_speedup_computed_only_for_full_success() -> None:
    wave = {
        "correlation_ok": True,
        "success_count": 4,
        "requested_width": 4,
        "wall_s": 10.0,
    }
    assert speedup_or_none(10.0, 4, wave) == 4.0


def test_payment_failure_classification() -> None:
    waves = {
        "serial": {
            "correlation_ok": False,
            "errors": ["Error: API error (status 402 Payment Required): balance exhausted"],
        }
    }
    assert classify_probe_failure(waves) == ProbeStatus.FAILED_PAYMENT


def test_status_and_max_verified_rules() -> None:
    partial = {
        "serial": {"correlation_ok": True},
        "2_way": {"correlation_ok": True},
        "4_way": {"correlation_ok": False, "errors": ["429 too many concurrent sessions"]},
    }
    assert derive_max_verified(partial, [1, 2, 4]) == 2
    assert derive_probe_status(partial, [1, 2, 4], correlation_failure=False) == (
        ProbeStatus.PARTIAL
    )

    complete = {
        "serial": {"correlation_ok": True},
        "2_way": {"correlation_ok": True},
        "4_way": {"correlation_ok": True},
    }
    assert derive_max_verified(complete, [1, 2, 4]) == 4
    assert derive_probe_status(complete, [1, 2, 4], correlation_failure=False) == (
        ProbeStatus.VERIFIED
    )

    corrupted = dict(complete)
    assert derive_probe_status(corrupted, [1, 2, 4], correlation_failure=True) == (
        ProbeStatus.FAILED_CORRELATION
    )


class _Session:
    def __init__(self, config) -> None:
        self.config = config
        self.is_active = True
        self.session_data: dict = {}


class _FakeGrok:
    adapter_id = "grok"
    name = "Grok Build"
    runtime_origin = "xai"
    binary_version = "1.0.25"
    credential_manager = None

    def __init__(
        self, *, error_text: str | None = None, fail_labels: set[str] | None = None
    ) -> None:
        self.error_text = error_text
        self.fail_labels = set(fail_labels or ())

    def _find_binary(self) -> str:
        return "/tmp/fake-grok"

    def credential_identity_hash(self) -> str:
        return "cred-1"

    async def create_session(self, config):
        return _Session(config)

    async def send(self, session, turn):
        from persona_continuum.agent.models import AgentEvent, AgentEventType

        label = turn.user_message.replace("Reply with exactly: ", "")
        if self.error_text is not None:
            yield AgentEvent(type=AgentEventType.ERROR, error=self.error_text)
            return
        if label in self.fail_labels:
            yield AgentEvent(
                type=AgentEventType.ERROR, error="HTTP 429 too many concurrent sessions"
            )
            return
        yield AgentEvent(type=AgentEventType.CHUNK, content=label)
        yield AgentEvent(type=AgentEventType.DONE)

    async def close(self, session) -> None:
        session.is_active = False


def _wire(monkeypatch, module, adapter: _FakeGrok, tmp_path):
    cache = IndependentSessionConcurrencyCache(
        store=RuntimeCapabilityStore(tmp_path / "runtime.sqlite")
    )
    monkeypatch.setattr(module, "GrokBuildAdapter", lambda: adapter)
    monkeypatch.setattr(module, "_grok_binary", lambda _adapter: "/tmp/fake-grok")
    monkeypatch.setattr(module, "_load_credential_manager", lambda: None)
    monkeypatch.setattr(module, "default_concurrency_cache", lambda: cache)

    async def _version(_adapter, _binary):
        return "1.0.25"

    monkeypatch.setattr(module, "_binary_version", _version)
    return cache


def test_probe_end_to_end_success_writes_persistent_capability(monkeypatch, tmp_path) -> None:
    module = _load_probe_module()
    cache = _wire(monkeypatch, module, _FakeGrok(), tmp_path)
    import asyncio

    payload = asyncio.run(
        module.run_probe({"model": "grok-4.6", "max_concurrency": 4, "force": True})
    )
    assert payload["probe_status"] == ProbeStatus.VERIFIED
    assert payload["max_verified"] == 4
    assert payload["speedup_valid"] is True
    identity = cache.identity(
        "grok",
        binary_identity=payload["identity"]["binary_identity"],
        binary_version="1.0.25",
        model_id="grok-4.6",
        credential_identity_hash="cred-1",
        runtime_origin="xai",
    )
    assert cache.store.verified_limit(identity) == 4
    assert cache.store.get(identity).state == CapabilityState.VERIFIED


def test_probe_end_to_end_payment_failure_has_no_speedup(monkeypatch, tmp_path) -> None:
    module = _load_probe_module()
    adapter = _FakeGrok(
        error_text=(
            'Error: API error (status 402 Payment Required): Grok Build usage balance exhausted'
        )
    )
    cache = _wire(monkeypatch, module, adapter, tmp_path)
    import asyncio

    payload = asyncio.run(
        module.run_probe({"model": "grok-4.6", "max_concurrency": 4, "force": True})
    )
    assert payload["probe_status"] == ProbeStatus.FAILED_PAYMENT
    assert payload["max_verified"] == 1
    assert payload["two_speedup"] is None
    assert payload["four_speedup"] is None
    assert payload["speedup_valid"] is False
    identity = cache.identity(
        "grok",
        binary_identity=payload["identity"]["binary_identity"],
        binary_version="1.0.25",
        model_id="grok-4.6",
        credential_identity_hash="cred-1",
        runtime_origin="xai",
    )
    assert cache.store.verified_limit(identity) == 1
    assert cache.store.get(identity).state != CapabilityState.VERIFIED


def test_partial_probe_keeps_two_way_speedup(monkeypatch, tmp_path) -> None:
    """2-way success + 4-way failure => verified 2, two_speedup preserved."""

    module = _load_probe_module()
    # 4-way labels are TASK-05..TASK-08; 2-way labels TASK-03/TASK-04 succeed.
    adapter = _FakeGrok(fail_labels={"TASK-05", "TASK-06", "TASK-07", "TASK-08"})
    cache = _wire(monkeypatch, module, adapter, tmp_path)
    import asyncio

    payload = asyncio.run(
        module.run_probe({"model": "grok-4.6", "max_concurrency": 4, "force": True})
    )
    assert payload["probe_status"] == ProbeStatus.PARTIAL
    assert payload["max_verified"] == 2
    assert payload["overall_probe_complete"] is False
    # The valid 2-way result must survive the failed 4-way wave.
    assert payload["two_speedup_valid"] is True
    assert payload["two_speedup"] is not None
    assert payload["four_speedup_valid"] is False
    assert payload["four_speedup"] is None
    identity = cache.identity(
        "grok",
        binary_identity=payload["identity"]["binary_identity"],
        binary_version="1.0.25",
        model_id="grok-4.6",
        credential_identity_hash="cred-1",
        runtime_origin="xai",
    )
    assert cache.store.verified_limit(identity) == 2
