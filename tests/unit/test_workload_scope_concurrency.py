"""Adapter persistence vs workload scope and independent-session workers."""

from __future__ import annotations

import asyncio
import time

import pytest

from persona_continuum.agent.adapters.fake import FakeAgentAdapter
from persona_continuum.agent.adapters.grok import GrokBuildAdapter
from persona_continuum.agent.discovery import AgentDiscoveryService
from persona_continuum.agent.models import AgentProbeResult, AgentStatus
from persona_continuum.application.classification_dispatch import ClassificationDispatch
from persona_continuum.application.material_pipeline import (
    MaterialPipelineMetrics,
    ResolvedExecutionProfile,
)
from persona_continuum.performance.concurrency_cache import (
    IndependentSessionConcurrencyCache,
    IndependentSessionProbe,
)
from persona_continuum.performance.runtime_pool import AgentRuntimePool


def _declared_probe(adapter) -> AgentProbeResult:
    probe = AgentProbeResult(
        id=adapter.adapter_id, name=adapter.name, status=AgentStatus.DETECTED
    )
    AgentDiscoveryService._apply_context_capability_declaration(adapter, probe)
    return probe


def test_adapter_declaration_flows_into_material_profile() -> None:
    probe = _declared_probe(FakeAgentAdapter())
    assert probe.capabilities.adapter_session_mode == "per_request"
    assert probe.capabilities.parallel_independent_sessions is True
    assert probe.capabilities.max_parallel_independent_sessions == 4
    snapshot = probe.model_dump(mode="json")
    snapshot["workload_context_scope"] = "per_window"
    profile = ResolvedExecutionProfile.resolve(snapshot)
    assert profile.workload_context_scope == "per_window"
    assert profile.classification_worker_count(4) == 4


def test_unverified_persistent_capable_adapter_stays_serial() -> None:
    probe = _declared_probe(GrokBuildAdapter())
    assert probe.capabilities.adapter_session_mode == "persistent_capable"
    assert probe.capabilities.parallel_turns_same_session is False
    assert probe.capabilities.parallel_independent_sessions is False
    snapshot = probe.model_dump(mode="json")
    snapshot["workload_context_scope"] = "per_window"
    profile = ResolvedExecutionProfile.resolve(snapshot)
    assert profile.classification_worker_count(4) == 1


def test_verified_cache_unlocks_independent_session_workers() -> None:
    cache = IndependentSessionConcurrencyCache()
    cache.store_probe(
        IndependentSessionProbe(adapter_id="grok", max_verified=4, status="verified")
    )
    snapshot = _declared_probe(GrokBuildAdapter()).model_dump(mode="json")
    snapshot["workload_context_scope"] = "per_window"
    verified = cache.verified_limit("grok")
    assert verified == 4
    snapshot["parallel_independent_sessions"] = verified > 1
    snapshot["max_parallel_independent_sessions"] = verified
    profile = ResolvedExecutionProfile.resolve(snapshot)
    assert profile.classification_worker_count(4) == 4


def test_persistent_adapter_per_window_can_use_independent_sessions() -> None:
    profile = ResolvedExecutionProfile.resolve(
        {
            "persistent_session": True,
            "workload_context_scope": "per_window",
            "parallel_independent_sessions": True,
            "max_parallel_independent_sessions": 4,
        }
    )
    assert profile.workload_context_scope == "per_window"
    assert profile.classification_worker_count(4) == 4


def test_same_persistent_session_stays_serial() -> None:
    profile = ResolvedExecutionProfile.resolve(
        {
            "persistent_session": True,
            "workload_context_scope": "persistent",
            "parallel_independent_sessions": True,
            "max_parallel_independent_sessions": 4,
        }
    )
    assert profile.classification_worker_count(4) == 1


def test_material_default_snapshot_is_per_window() -> None:
    profile = ResolvedExecutionProfile.resolve(
        {"persistent_session": True, "workload_context_scope": "per_window"}
    )
    assert profile.workload_context_scope == "per_window"


def test_concurrency_cache_unverified_is_one() -> None:
    cache = IndependentSessionConcurrencyCache()
    assert cache.verified_limit("grok") == 1
    cache.store_probe(
        IndependentSessionProbe(adapter_id="grok", max_verified=4, status="verified")
    )
    assert cache.verified_limit("grok") == 4


@pytest.mark.anyio
async def test_dispatch_independent_workers_run_four_wide() -> None:
    metrics = MaterialPipelineMetrics()
    started: list[float] = []
    finished: list[float] = []

    async def one() -> None:
        started.append(time.perf_counter())
        await asyncio.sleep(0.15)
        finished.append(time.perf_counter())

    async with ClassificationDispatch(4, metrics) as dispatch:
        for _ in range(4):
            await dispatch.submit(one)
    wall = max(finished) - min(started)
    assert wall < 0.4
    assert metrics.peak_active_classification_workers == 4


@pytest.mark.anyio
async def test_rate_limit_downgrades_dispatch() -> None:
    metrics = MaterialPipelineMetrics()

    async def boom() -> None:
        raise RuntimeError("HTTP 429 too many concurrent sessions")

    async def noop(_: float) -> None:
        return None

    dispatch = ClassificationDispatch(
        4, metrics, retry_limit=2, backoff_seconds=lambda _a: 0.0, sleep=noop
    )
    with pytest.raises(RuntimeError):
        async with dispatch:
            await dispatch.submit(boom)
    # Permanent congestion walks the 4 -> 2 -> 1 ladder, then fails.
    assert dispatch.rate_limit_downgrades == 2
    assert dispatch._max == 1


@pytest.mark.anyio
async def test_runtime_pool_four_leases_then_release() -> None:
    pool = AgentRuntimePool(max_processes_per_key=4)
    counter = {"spawns": 0}

    async def factory():
        class _T:
            closed = False

            class _Proc:
                returncode = None

            process = _Proc()
            process_alive = True

            async def close(self, *, force: bool = False) -> None:
                self.closed = True

            async def wait(self) -> int:
                return 0

            async def readline(self) -> bytes:
                return b""

        counter["spawns"] += 1
        return _T()

    leases = [await pool.acquire("grok:x", factory) for _ in range(4)]
    assert counter["spawns"] == 4
    waiter_got = asyncio.Event()

    async def fifth() -> None:
        lease = await pool.acquire("grok:x", factory)
        waiter_got.set()
        await lease.release()

    task = asyncio.create_task(fifth())
    await asyncio.sleep(0.05)
    assert not waiter_got.is_set()
    await leases[0].release()
    await asyncio.wait_for(task, timeout=1.0)
    assert waiter_got.is_set()
    for lease in leases[1:]:
        await lease.release()
    await pool.shutdown()


def test_v4_contract_versions_stay_frozen() -> None:
    from persona_continuum.application.material_intelligence import (
        CLASSIFICATION_CONTRACT_V4,
        TURN_POLICY_VERSION,
    )

    assert CLASSIFICATION_CONTRACT_V4 == "conversation-evidence-v4"
    assert TURN_POLICY_VERSION == "conversation-turn-v2"


@pytest.mark.anyio
async def test_mock_workers_reduce_wall_clock() -> None:
    walls: dict[int, float] = {}
    calls: dict[int, int] = {}
    for workers in (1, 2, 4):
        metrics = MaterialPipelineMetrics()
        started = time.perf_counter()
        counter = {"n": 0}

        async def one(bucket: dict[str, int] = counter) -> None:
            bucket["n"] += 1
            await asyncio.sleep(0.12)

        async with ClassificationDispatch(workers, metrics) as dispatch:
            for _ in range(8):
                await dispatch.submit(one)
        walls[workers] = time.perf_counter() - started
        calls[workers] = counter["n"]
        assert metrics.peak_active_classification_workers == workers
    assert calls[1] == calls[2] == calls[4] == 8
    assert walls[2] < walls[1] * 0.75
    assert walls[4] < walls[2] * 0.75
