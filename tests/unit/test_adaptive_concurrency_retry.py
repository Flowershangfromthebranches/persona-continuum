"""Adaptive concurrency retry: downgrade ladder, requeue, fail-fast kinds (P0.5 D/M)."""

from __future__ import annotations

import asyncio

import pytest

from persona_continuum.application.classification_dispatch import ClassificationDispatch
from persona_continuum.application.material_pipeline import MaterialPipelineMetrics
from persona_continuum.application.provider_capacity import (
    CapacityFailure,
    classify_capacity_failure,
)


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


# -- error classification (D1) ----------------------------------------------


def test_402_payment_is_never_concurrency() -> None:
    diagnosis = classify_capacity_failure(
        RuntimeError("API error (status 402 Payment Required): balance exhausted")
    )
    assert diagnosis.kind == CapacityFailure.NON_RETRIABLE_PAYMENT
    assert diagnosis.retriable_concurrency is False


def test_429_quota_is_not_concurrency() -> None:
    diagnosis = classify_capacity_failure(RuntimeError("HTTP 429 quota exhausted for today"))
    assert diagnosis.kind == CapacityFailure.NON_RETRIABLE_QUOTA
    assert diagnosis.retriable_concurrency is False


def test_429_too_many_concurrent_is_concurrency() -> None:
    diagnosis = classify_capacity_failure(RuntimeError("HTTP 429 too many concurrent sessions"))
    assert diagnosis.retriable_concurrency is True
    assert diagnosis.kind in {
        CapacityFailure.CONCURRENCY_LIMIT,
        CapacityFailure.TOO_MANY_SESSIONS,
    }


def test_429_generic_rate_limit_is_transient_not_concurrency() -> None:
    diagnosis = classify_capacity_failure(RuntimeError("HTTP 429 rate limit exceeded"))
    assert diagnosis.kind == CapacityFailure.TRANSIENT_RATE_LIMIT
    assert diagnosis.retriable_concurrency is False
    assert diagnosis.retriable is True


# -- provider failure classification table (P0.6 15) ------------------------


@pytest.mark.parametrize(
    ("message", "expected_kind"),
    [
        ("HTTP 429 too many concurrent sessions", CapacityFailure.TOO_MANY_SESSIONS),
        ("HTTP 429 concurrency limit exceeded", CapacityFailure.CONCURRENCY_LIMIT),
        ("HTTP 429 too many requests", CapacityFailure.TRANSIENT_RATE_LIMIT),
        ("HTTP 429 requests per minute exceeded", CapacityFailure.TRANSIENT_RATE_LIMIT),
        ("HTTP 429 quota exhausted", CapacityFailure.QUOTA_EXHAUSTED),
        ("HTTP 429 usage limit reached", CapacityFailure.QUOTA_EXHAUSTED),
        ("HTTP 402 Payment Required", CapacityFailure.PAYMENT_REQUIRED),
        ("HTTP 429", CapacityFailure.UNKNOWN_429),
        ("HTTP 429 unknown provider capacity error", CapacityFailure.UNKNOWN_429),
    ],
)
def test_failure_classification_table(message: str, expected_kind: str) -> None:
    diagnosis = classify_capacity_failure(RuntimeError(message))
    assert diagnosis.kind == expected_kind
    if expected_kind in {
        CapacityFailure.UNKNOWN_429,
        CapacityFailure.QUOTA_EXHAUSTED,
        CapacityFailure.PAYMENT_REQUIRED,
    }:
        assert diagnosis.retriable_concurrency is False


def test_structured_concurrency_code_wins_over_generic_429() -> None:
    class _Error(RuntimeError):
        code = "rate_limit_concurrency"

    diagnosis = classify_capacity_failure(_Error("HTTP 429"))
    assert diagnosis.kind == CapacityFailure.CONCURRENCY_LIMIT
    assert diagnosis.retriable_concurrency is True


def test_unknown_429_never_becomes_concurrency() -> None:
    diagnosis = classify_capacity_failure(RuntimeError("HTTP 429"))
    assert diagnosis.kind == CapacityFailure.UNKNOWN_429
    assert diagnosis.retriable_concurrency is False


# -- adaptive retry state machine (D2/D3/D5, M1-M8) -------------------------


@pytest.mark.anyio
async def test_concurrency_reject_downgrades_and_requeues() -> None:
    metrics = MaterialPipelineMetrics()
    attempts: dict[str, int] = {}
    committed: list[str] = []

    async def work(label: str) -> None:
        attempts[label] = attempts.get(label, 0) + 1
        if label == "w07" and attempts[label] == 1:
            raise RuntimeError("HTTP 429 too many concurrent sessions")
        await asyncio.sleep(0.01)
        committed.append(label)

    dispatch = _dispatch(4, metrics)
    async with dispatch:
        for index in range(20):
            await dispatch.submit(
                (lambda index=index: work(f"w{index:02d}")), label=f"w{index:02d}"
            )
    assert set(committed) == {f"w{index:02d}" for index in range(20)}
    assert len(committed) == 20  # the retried window committed exactly once
    assert attempts["w07"] == 2
    assert metrics.window_retries == 1
    assert dispatch._max == 2
    assert dispatch.downgrade_events[0]["from"] == 4
    assert dispatch.downgrade_events[0]["to"] == 2
    assert dispatch.error is None


@pytest.mark.anyio
async def test_second_reject_downgrades_to_one_then_succeeds() -> None:
    metrics = MaterialPipelineMetrics()
    attempts = {"n": 0}
    committed: list[str] = []

    async def flaky() -> None:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise RuntimeError("429 too many parallel sessions")
        committed.append("ok")

    dispatch = _dispatch(4, metrics)
    async with dispatch:
        await dispatch.submit(flaky, label="x")
    assert committed == ["ok"]
    assert dispatch._max == 1
    assert dispatch.rate_limit_downgrades == 2
    assert dispatch.error is None


@pytest.mark.anyio
async def test_single_worker_permanent_reject_fails_without_downgrade() -> None:
    metrics = MaterialPipelineMetrics()

    async def boom() -> None:
        raise RuntimeError("HTTP 429 too many concurrent sessions")

    dispatch = _dispatch(1, metrics)
    with pytest.raises(RuntimeError):
        async with dispatch:
            await dispatch.submit(boom)
    assert metrics.concurrency_downgrades == 0
    assert dispatch._max == 1


@pytest.mark.anyio
async def test_retry_limit_is_respected() -> None:
    metrics = MaterialPipelineMetrics()

    async def boom() -> None:
        raise RuntimeError("HTTP 429 too many concurrent sessions")

    dispatch = _dispatch(4, metrics, retry_limit=1)
    with pytest.raises(RuntimeError):
        async with dispatch:
            await dispatch.submit(boom)
    assert dispatch.rate_limit_downgrades == 1
    assert dispatch._max == 2


@pytest.mark.anyio
async def test_payment_402_is_not_downgraded() -> None:
    metrics = MaterialPipelineMetrics()

    async def boom() -> None:
        raise RuntimeError("API error (status 402 Payment Required): Grok Build usage exhausted")

    dispatch = _dispatch(4, metrics)
    with pytest.raises(RuntimeError):
        async with dispatch:
            await dispatch.submit(boom)
    assert metrics.concurrency_downgrades == 0
    assert metrics.non_retriable_capacity_failures == 1
    assert metrics.last_concurrency_failure_kind == CapacityFailure.NON_RETRIABLE_PAYMENT


@pytest.mark.anyio
async def test_quota_429_is_not_downgraded() -> None:
    metrics = MaterialPipelineMetrics()

    async def boom() -> None:
        raise RuntimeError("HTTP 429 quota exhausted")

    dispatch = _dispatch(4, metrics)
    with pytest.raises(RuntimeError):
        async with dispatch:
            await dispatch.submit(boom)
    assert metrics.concurrency_downgrades == 0


@pytest.mark.anyio
async def test_completed_windows_are_not_rerun() -> None:
    metrics = MaterialPipelineMetrics()
    attempts: dict[str, int] = {}

    async def work(label: str) -> None:
        attempts[label] = attempts.get(label, 0) + 1
        if label == "w05" and attempts[label] == 1:
            raise RuntimeError("429 too many concurrent requests")
        await asyncio.sleep(0.01)

    dispatch = _dispatch(4, metrics)
    async with dispatch:
        for index in range(12):
            await dispatch.submit(
                (lambda index=index: work(f"w{index:02d}")), label=f"w{index:02d}"
            )
    retried = {label for label, count in attempts.items() if count > 1}
    assert retried == {"w05"}
    assert all(count == 1 for label, count in attempts.items() if label != "w05")
