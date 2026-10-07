#!/usr/bin/env python3
"""Mock persisted-classification concurrency benchmark.

Does not read private chat or call an external model.  Uses the same
ClassificationDispatch path as Material Intelligence with a fake 400–800ms
latency per window, sized to the known 10K-turn / 500K-context window count.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from functools import partial
from pathlib import Path
from typing import Any

from persona_continuum.application.classification_dispatch import ClassificationDispatch
from persona_continuum.application.material_pipeline import MaterialPipelineMetrics

# 10K short turns at 500K context previously packed into ~18 windows.
DEFAULT_WINDOWS = 18
DEFAULT_LATENCY = (0.4, 0.8)


async def _run(workers: int, windows: int, latency: tuple[float, float]) -> dict[str, Any]:
    metrics = MaterialPipelineMetrics()
    metrics.workload_context_scope = "per_window"
    metrics.parallel_independent_sessions = True
    metrics.max_parallel_independent_sessions = workers
    metrics.effective_classification_workers = workers
    calls = 0
    latencies: list[float] = []

    async def one() -> None:
        nonlocal calls
        calls += 1
        delay = random.uniform(*latency)
        tick = time.perf_counter()
        await asyncio.sleep(delay)
        latencies.append(time.perf_counter() - tick)

    started = time.perf_counter()
    async with ClassificationDispatch(workers, metrics) as dispatch:
        for _ in range(windows):
            await dispatch.submit(one)
    wall = time.perf_counter() - started
    ordered = sorted(latencies)
    p50 = ordered[len(ordered) // 2] if ordered else 0.0
    p95 = ordered[int(round(0.95 * (len(ordered) - 1)))] if ordered else 0.0
    return {
        "workers": workers,
        "windows": windows,
        "calls": calls,
        "wall_s": round(wall, 4),
        "avg_latency_s": round(sum(latencies) / max(1, len(latencies)), 4),
        "p50_s": round(p50, 4),
        "p95_s": round(p95, 4),
        "peak_active": metrics.peak_active_classification_workers,
        "peak_independent_sessions": metrics.peak_independent_sessions,
        "agent_calls": calls,
    }


async def _noop_sleep(_: float) -> None:
    return None


def _dispatch(workers: int, metrics: MaterialPipelineMetrics) -> ClassificationDispatch:
    return ClassificationDispatch(
        workers,
        metrics,
        retry_limit=2,
        backoff_seconds=lambda _attempt: 0.0,
        sleep=_noop_sleep,
    )


class _Latch:
    """Release every waiter once ``size`` of them have arrived (true in-flight)."""

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


async def _scenario_simultaneous_wave(
    workers: int, windows: int, latency: tuple[float, float]
) -> dict[str, object]:
    """Scenario A: four in-flight windows rejected together (one 4->2)."""

    metrics = MaterialPipelineMetrics()
    calls: dict[str, int] = {}
    committed: list[str] = []
    persistent_downgrades: list[tuple[str, int]] = []
    first_wave = {f"w{index:02d}" for index in range(min(workers, windows))}
    latch = _Latch(len(first_wave))

    async def one(index: int) -> None:
        label = f"w{index:02d}"
        calls[label] = calls.get(label, 0) + 1
        if label in first_wave and calls[label] == 1:
            await latch.wait()
            raise RuntimeError("HTTP 429 too many concurrent sessions")
        await asyncio.sleep(random.uniform(*latency))
        committed.append(label)

    started = time.perf_counter()
    async with _dispatch(workers, metrics) as dispatch:
        def _record_downgrade(reason: str, limit: int) -> None:
            persistent_downgrades.append((reason, limit))

        dispatch._on_downgrade = _record_downgrade
        for index in range(windows):
            await dispatch.submit(partial(one, index), label=f"w{index:02d}")
    wall = time.perf_counter() - started
    return {
        "workers": workers,
        "windows": windows,
        "wall_s": round(wall, 4),
        "successful_windows": len(set(committed)),
        "window_attempts": sum(calls.values()),
        "window_retries": metrics.window_retries,
        "stale_replayed_windows": metrics.stale_replayed_windows,
        "stale_rejection_count": metrics.stale_rejection_count,
        "concurrency_downgrades": metrics.concurrency_downgrades,
        "persistent_downgrades": persistent_downgrades,
        "downgrade_events": metrics.concurrency_downgrade_events,
        "peak_active": metrics.peak_active_classification_workers,
        "checkpoint_duplicates": len(committed) - len(set(committed)),
        "failed_windows": windows - len(set(committed)),
        "terminal_error": None,
    }


async def _scenario_two_level_wave(workers: int, latency: tuple[float, float]) -> dict[str, object]:
    """Scenario B: 4-wide wave rejects, then 2-wide, then succeeds at 1."""

    windows = max(4, workers)
    metrics = MaterialPipelineMetrics()
    committed: list[str] = []
    latch4 = _Latch(4)
    latch2 = _Latch(2)

    async def one(label: str) -> None:
        limit = dispatch.effective_concurrency  # captured width for this attempt
        if limit >= 4:
            await latch4.wait()
            raise RuntimeError("HTTP 429 too many concurrent sessions")
        if limit == 2:
            await latch2.wait()
            raise RuntimeError("HTTP 429 too many concurrent sessions")
        await asyncio.sleep(random.uniform(*latency))
        committed.append(label)

    started = time.perf_counter()
    async with _dispatch(workers, metrics) as dispatch:
        for index in range(windows):
            label = f"w{index:02d}"
            await dispatch.submit(partial(one, label), label=label)
    wall = time.perf_counter() - started
    return {
        "workers": workers,
        "windows": windows,
        "wall_s": round(wall, 4),
        "successful_windows": len(set(committed)),
        "concurrency_downgrades": metrics.concurrency_downgrades,
        "downgrade_events": metrics.concurrency_downgrade_events,
        "final_effective_concurrency": dispatch.effective_concurrency,
        "checkpoint_duplicates": len(committed) - len(set(committed)),
        "failed_windows": windows - len(set(committed)),
        "terminal_error": None,
    }


async def _scenario_serial_failure(workers: int) -> dict[str, object]:
    """Scenario C: a window still rejected at width 1 fails with a clear code."""

    metrics = MaterialPipelineMetrics()
    terminal_error: str | None = None

    async def always_reject() -> None:
        raise RuntimeError("HTTP 429 too many concurrent sessions")

    dispatch = _dispatch(workers, metrics)
    try:
        async with dispatch:
            await dispatch.submit(always_reject, label="serial")
    except Exception as exc:  # raised by __aexit__ after the ladder is exhausted
        terminal_error = getattr(exc, "code", type(exc).__name__)
    return {
        "workers": workers,
        "concurrency_downgrades": metrics.concurrency_downgrades,
        "downgrade_events": metrics.concurrency_downgrade_events,
        "final_effective_concurrency": dispatch.effective_concurrency,
        "terminal_error": terminal_error,
        "terminal_concurrency_failures": metrics.terminal_concurrency_failures,
    }


async def _scenario_generic_429(
    workers: int, windows: int, latency: tuple[float, float]
) -> dict[str, object]:
    """Scenario D: a semantics-free 429 must not lower the verified width."""

    metrics = MaterialPipelineMetrics()
    committed: list[str] = []
    downgrades: list[tuple[str, int]] = []
    terminal_error: str | None = None
    target = "w03"

    async def one(index: int) -> None:
        label = f"w{index:02d}"
        if label == target:
            raise RuntimeError("HTTP 429")
        await asyncio.sleep(random.uniform(*latency))
        committed.append(label)

    dispatch = _dispatch(workers, metrics)
    try:
        async with dispatch:
            def _record_downgrade(reason: str, limit: int) -> None:
                downgrades.append((reason, limit))

            dispatch._on_downgrade = _record_downgrade
            for index in range(windows):
                await dispatch.submit(partial(one, index), label=f"w{index:02d}")
    except Exception as exc:  # unknown 429 fails closed (no downgrade, no retry)
        terminal_error = getattr(exc, "code", type(exc).__name__)
    return {
        "workers": workers,
        "windows": windows,
        "successful_windows": len(set(committed)),
        "concurrency_downgrades": metrics.concurrency_downgrades,
        "persistent_downgrades": downgrades,
        "final_effective_concurrency": dispatch.effective_concurrency,
        "provider_failure_kind": metrics.provider_failure_kind,
        "terminal_error": terminal_error,
        "checkpoint_duplicates": len(committed) - len(set(committed)),
    }


async def main_async(windows: int, seed: int) -> dict[str, object]:
    random.seed(seed)
    rows = []
    for workers in (1, 2, 4):
        rows.append(await _run(workers, windows, DEFAULT_LATENCY))
    serial = float(rows[0]["wall_s"])
    for row in rows:
        row["speedup_vs_serial"] = round(serial / max(1e-9, float(row["wall_s"])), 3)
    return {
        "kind": "mock_persisted_classification_concurrency",
        "target_turns": 10_000,
        "context_tokens": 500_000,
        "windows": windows,
        "latency_s": list(DEFAULT_LATENCY),
        "rows": rows,
        "calls_consistent": len({row["calls"] for row in rows}) == 1,
        "scenario_a_simultaneous_wave": await _scenario_simultaneous_wave(
            4, windows, DEFAULT_LATENCY
        ),
        "scenario_b_two_level_wave": await _scenario_two_level_wave(4, DEFAULT_LATENCY),
        "scenario_c_serial_failure": await _scenario_serial_failure(4),
        "scenario_d_generic_429": await _scenario_generic_429(4, windows, DEFAULT_LATENCY),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--windows", type=int, default=DEFAULT_WINDOWS)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--out",
        default="docs/reports/implementation/mock-classification-concurrency.json",
    )
    args = parser.parse_args()
    payload = asyncio.run(main_async(args.windows, args.seed))
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
