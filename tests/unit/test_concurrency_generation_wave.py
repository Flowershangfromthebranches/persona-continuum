"""Concurrency generation: simultaneous rejection waves (P0.6).

A wave of windows rejected at the same instant all observed the same
concurrency state.  Only the first rejection may lower the width; the rest are
stale and must be replayed, never failed and never persisted as a downgrade.
"""

from __future__ import annotations

import asyncio

import pytest

from persona_continuum.application.classification_dispatch import (
    ClassificationDispatch,
    ProviderConcurrencyLimitAtSerialExecution,
    ProviderRateLimitRetryExhausted,
)
from persona_continuum.application.material_pipeline import MaterialPipelineMetrics
from persona_continuum.application.provider_capacity import CapacityFailure


async def _noop(_: float) -> None:
    return None


def _dispatch(count: int, metrics: MaterialPipelineMetrics, retry_limit: int = 2):
    return ClassificationDispatch(
        count,
        metrics,
        retry_limit=retry_limit,
        backoff_seconds=lambda _a: 0.0,
        sleep=_noop,
    )


class _Latch:
    """Release every waiter once ``size`` of them have arrived."""

    def __init__(self, size: int) -> None:
        self.size = size
        self._count = 0
        self._lock = asyncio.Lock()
        self._event = asyncio.Event()

    async def wait(self) -> None:
        async with self._lock:
            self._count += 1
            if self._count >= self.size:
                self._event.set()
        await self._event.wait()


@pytest.mark.anyio
async def test_simultaneous_concurrency_wave_downgrades_once() -> None:
    """Four in-flight windows rejected together must produce exactly one 4->2."""

    metrics = MaterialPipelineMetrics()
    dispatch = _dispatch(4, metrics)
    attempts: dict[str, int] = {}
    committed: list[str] = []
    first_wave = {f"w{index:02d}" for index in range(4)}
    latch = _Latch(4)

    async def work(label: str) -> None:
        attempts[label] = attempts.get(label, 0) + 1
        if label in first_wave and attempts[label] == 1:
            await latch.wait()
            raise RuntimeError("HTTP 429 too many concurrent sessions")
        await asyncio.sleep(0.01)
        committed.append(label)

    async with dispatch:
        for index in range(12):
            label = f"w{index:02d}"
            await dispatch.submit(lambda label=label: work(label), label=label)

    assert sorted(committed) == [f"w{index:02d}" for index in range(12)]
    assert len(committed) == 12  # no duplicate commits
    assert dispatch.error is None
    assert dispatch.terminal_error is None
    assert metrics.concurrency_downgrades == 1
    assert [(event["from"], event["to"]) for event in dispatch.downgrade_events] == [(4, 2)]
    # Exactly one window triggered the downgrade; the other three were stale.
    assert dispatch.stale_replayed_windows == 3
    assert metrics.stale_rejection_count == 3
    assert all(attempts[label] == 2 for label in first_wave)


@pytest.mark.anyio
async def test_two_level_wave_downgrades_twice_then_succeeds() -> None:
    """A new rejection at width 2 may lower to 1; the same wave may not."""

    metrics = MaterialPipelineMetrics()
    dispatch = _dispatch(4, metrics)
    committed: list[str] = []
    latch4 = _Latch(4)
    latch2 = _Latch(2)

    async def work(label: str) -> None:
        # No await has run since the worker captured this attempt's width.
        limit = dispatch.effective_concurrency
        if limit >= 4:
            await latch4.wait()
            raise RuntimeError("HTTP 429 too many concurrent sessions")
        if limit == 2:
            await latch2.wait()
            raise RuntimeError("HTTP 429 too many concurrent sessions")
        await asyncio.sleep(0.01)
        committed.append(label)

    async with dispatch:
        for index in range(4):
            label = f"w{index:02d}"
            await dispatch.submit(lambda label=label: work(label), label=label)

    assert sorted(committed) == [f"w{index:02d}" for index in range(4)]
    assert dispatch.error is None
    assert [(event["from"], event["to"]) for event in dispatch.downgrade_events] == [
        (4, 2),
        (2, 1),
    ]
    assert dispatch.effective_concurrency == 1
    assert metrics.concurrency_downgrades == 2


@pytest.mark.anyio
async def test_serial_execution_concurrency_rejection_is_terminal() -> None:
    """Only a genuine rejection at width 1 fails the task, with a clear code."""

    metrics = MaterialPipelineMetrics()
    dispatch = _dispatch(4, metrics)

    async def always_reject() -> None:
        raise RuntimeError("HTTP 429 too many concurrent sessions")

    with pytest.raises(ProviderConcurrencyLimitAtSerialExecution) as excinfo:
        async with dispatch:
            await dispatch.submit(always_reject, label="serial")

    assert excinfo.value.code == "PROVIDER_CONCURRENCY_LIMIT_AT_SERIAL_EXECUTION"
    assert dispatch.effective_concurrency == 1
    assert [(event["from"], event["to"]) for event in dispatch.downgrade_events] == [
        (4, 2),
        (2, 1),
    ]
    assert metrics.terminal_concurrency_failures >= 1


@pytest.mark.anyio
async def test_generic_429_does_not_downgrade_capability() -> None:
    """A semantics-free HTTP 429 fails closed: no downgrade, no persistence."""

    metrics = MaterialPipelineMetrics()
    dispatch = _dispatch(4, metrics)
    downgrades: list[tuple[str, int]] = []
    dispatch._on_downgrade = lambda reason, limit: downgrades.append((reason, limit))

    async def boom() -> None:
        raise RuntimeError("HTTP 429")

    with pytest.raises(RuntimeError):
        async with dispatch:
            await dispatch.submit(boom, label="w")

    assert metrics.concurrency_downgrades == 0
    assert dispatch.effective_concurrency == 4
    assert downgrades == []
    assert metrics.provider_failure_kind == CapacityFailure.UNKNOWN_429


@pytest.mark.anyio
async def test_transient_rate_limit_retries_without_downgrade() -> None:
    """Generic RPM/RPS limits may retry, but never lower the verified width."""

    metrics = MaterialPipelineMetrics()
    dispatch = _dispatch(4, metrics)
    attempts = {"n": 0}
    committed: list[str] = []

    async def flaky() -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("HTTP 429 too many requests")
        committed.append("ok")

    async with dispatch:
        await dispatch.submit(flaky, label="w")

    assert committed == ["ok"]
    assert metrics.concurrency_downgrades == 0
    assert dispatch.effective_concurrency == 4
    assert metrics.rate_limit_replays == 1


@pytest.mark.anyio
async def test_transient_rate_limit_exhaustion_is_terminal() -> None:
    metrics = MaterialPipelineMetrics()
    dispatch = _dispatch(4, metrics)

    async def boom() -> None:
        raise RuntimeError("HTTP 429 requests per minute exceeded")

    with pytest.raises(ProviderRateLimitRetryExhausted):
        async with dispatch:
            await dispatch.submit(boom, label="w")
    assert metrics.concurrency_downgrades == 0
    assert dispatch.effective_concurrency == 4


@pytest.mark.anyio
async def test_stale_rejections_do_not_consume_adaptive_budget() -> None:
    """Stale replays must not exhaust a window's own adaptive retry budget."""

    metrics = MaterialPipelineMetrics()
    dispatch = _dispatch(4, metrics, retry_limit=2)
    latch = _Latch(4)
    attempts: dict[str, int] = {}
    committed: list[str] = []
    first_wave = {f"w{index:02d}" for index in range(4)}

    async def work(label: str) -> None:
        attempts[label] = attempts.get(label, 0) + 1
        if label in first_wave and attempts[label] == 1:
            await latch.wait()
            raise RuntimeError("HTTP 429 too many concurrent sessions")
        await asyncio.sleep(0.01)
        committed.append(label)

    async with dispatch:
        for index in range(4):
            label = f"w{index:02d}"
            await dispatch.submit(lambda label=label: work(label), label=label)

    # The three stale windows only needed their one stale replay each.
    assert sorted(committed) == [f"w{index:02d}" for index in range(4)]
    assert dispatch.error is None
    assert all(attempts[label] == 2 for label in first_wave)
