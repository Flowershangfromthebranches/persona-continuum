"""Bounded persisted window scheduling with generation-scoped concurrency retry.

Independent AnalysisWindows run in parallel.  When the provider rejects the
current width (too many concurrent sessions, parallel-request limit), the
dispatcher halves the effective concurrency once per *concurrency generation*,
backs off briefly, and requeues the affected windows instead of failing the
whole Material task.

A wave of simultaneous rejections is the critical case: four workers in flight
at width 4 may all report congestion at the same instant.  They all observed
the *same* concurrency state, so they must not each lower the width.  Every
attempt records the generation it started under; only the first rejection of
the current generation lowers the limit and advances the generation.  Later
rejections from an older generation are *stale*: they are replayed under the
new width, never counted as terminal failures and never persisted as a
capability downgrade.

Completed windows are never rerun, and their checkpoints stay valid.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import TracebackType

from persona_continuum.application.material_pipeline import MaterialPipelineMetrics
from persona_continuum.application.provider_capacity import (
    CapacityDiagnosis,
    classify_capacity_failure,
)

DEFAULT_RETRY_LIMIT = 2
DEFAULT_STALE_REPLAY_LIMIT = 4
DEFAULT_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_BACKOFF_CAP_SECONDS = 8.0


class ProviderConcurrencyLimitAtSerialExecution(RuntimeError):
    """A window still hit a concurrency limit while running strictly serial.

    Reached only when the window ran at the latest generation with an effective
    concurrency of 1 and the provider still refused it: further retries cannot
    help and the caller must surface a clear, terminal error.
    """

    code = "PROVIDER_CONCURRENCY_LIMIT_AT_SERIAL_EXECUTION"


class ProviderConcurrencyRetryExhausted(RuntimeError):
    code = "PROVIDER_CONCURRENCY_LIMIT_RETRY_EXHAUSTED"


class ProviderRateLimitRetryExhausted(RuntimeError):
    code = "PROVIDER_RATE_LIMIT_RETRY_EXHAUSTED"


@dataclass
class _WindowTask:
    work: Callable[[], Awaitable[None]]
    label: str = ""
    attempt_number: int = 0
    generation: int = 0
    limit: int = 0
    started_at: float = 0.0
    adaptive_retries: int = 0
    stale_replays: int = 0
    rate_limit_replays: int = 0
    last_failure_kind: str | None = None
    diagnosis: CapacityDiagnosis | None = None
    attempt_generation_at_failure: int | None = None
    is_stale_generation: bool = False
    will_requeue: bool = False


@dataclass
class _FailurePlan:
    kind: str
    requeue: bool = False
    stale: bool = False
    downgraded: bool = False
    rate_limited: bool = False
    terminal_error: Exception | None = None
    metadata: dict[str, object] = field(default_factory=dict)


def _default_backoff(attempt: int) -> float:
    value = min(DEFAULT_BACKOFF_BASE_SECONDS * (2**attempt), DEFAULT_BACKOFF_CAP_SECONDS)
    return float(value) + float(random.uniform(0.0, value * 0.25))


def _halved(current: int) -> int:
    """4 -> 2 -> 1 ladder; never 4 -> 3 -> 2 -> 1."""

    return max(1, current // 2)


class ClassificationDispatch:
    def __init__(
        self,
        count: int,
        metrics: MaterialPipelineMetrics,
        *,
        retry_limit: int = DEFAULT_RETRY_LIMIT,
        stale_replay_limit: int = DEFAULT_STALE_REPLAY_LIMIT,
        backoff_seconds: Callable[[int], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        on_downgrade: Callable[[str, int], None] | None = None,
    ) -> None:
        self.metrics = metrics
        self.count = max(1, count)
        self._max = self.count
        self._active = 0
        self._gate = asyncio.Condition()
        # Serializes generation/limit/downgrade mutation across workers.
        self._concurrency_state_lock = asyncio.Lock()
        self._generation = 0
        self.rate_limit_downgrades = 0
        self.retry_limit = max(0, int(retry_limit))
        self.stale_replay_limit = max(0, int(stale_replay_limit))
        self._backoff_seconds = backoff_seconds or _default_backoff
        self._sleep = sleep or asyncio.sleep
        self._on_downgrade = on_downgrade
        self.queue: asyncio.Queue[_WindowTask] = asyncio.Queue(self.count * 2)
        self.tasks: list[asyncio.Task[None]] = []
        self.error: Exception | None = None
        self.terminal_error: Exception | None = None
        self.failed_window: str | None = None
        self.downgrade_events: list[dict[str, object]] = []
        self.window_retries = 0
        self.stale_rejection_count = 0
        self.stale_replayed_windows = 0
        self.terminal_concurrency_failures = 0
        self.started = 0.0

    @property
    def concurrency_generation(self) -> int:
        return self._generation

    @property
    def effective_concurrency(self) -> int:
        return self._max

    async def __aenter__(self) -> ClassificationDispatch:
        self.started = time.perf_counter()
        self.metrics.classification_worker_count = self.count
        self.metrics.effective_classification_workers = self.count
        self.metrics.effective_concurrency = self.count
        self.metrics.concurrency_generation = self._generation
        self.metrics.window_queue_capacity = self.queue.maxsize
        self.metrics.window_retry_limit = self.retry_limit
        self.tasks = [asyncio.create_task(self.worker()) for _ in range(self.count)]
        return self

    async def submit(
        self, work: Callable[[], Awaitable[None]], *, label: str = ""
    ) -> None:
        await self._enqueue(_WindowTask(work=work, label=label))

    async def _enqueue(self, task: _WindowTask) -> None:
        await self.queue.put(task)
        self.metrics.window_queue_depth = self.queue.qsize()
        self.metrics.peak_window_queue_depth = max(
            self.metrics.peak_window_queue_depth, self.queue.qsize()
        )
        self.metrics.classification_windows_dispatched += 1

    def downgrade(self, reason: str = "concurrency_limit", *, window: str = "") -> int:
        """Synchronous helper (tests/compat).  Prefer the locked path in workers."""

        self._downgrade_locked(reason, window=window)
        return self._max

    def _downgrade_locked(self, reason: str, *, window: str = "") -> int:
        previous = self._max
        self._max = _halved(self._max)
        self._generation += 1
        self.rate_limit_downgrades += 1
        self.metrics.classification_worker_count = self._max
        self.metrics.effective_classification_workers = self._max
        self.metrics.effective_concurrency = self._max
        self.metrics.concurrency_generation = self._generation
        self.metrics.concurrency_downgrades += 1
        event: dict[str, object] = {
            "from": previous,
            "to": self._max,
            "reason": reason,
            "window": window,
            "generation": self._generation,
            "at": time.time(),
        }
        self.downgrade_events.append(event)
        self.metrics.concurrency_downgrade_events.append(event)
        self.metrics.last_concurrency_failure_kind = reason
        if self._on_downgrade is not None:
            with contextlib.suppress(Exception):
                self._on_downgrade(reason, self._max)
        return self._max

    @staticmethod
    def _is_concurrency_limit(exc: BaseException) -> bool:
        return classify_capacity_failure(exc).retriable_concurrency

    @classmethod
    def _diagnose(cls, exc: BaseException) -> CapacityDiagnosis:
        return classify_capacity_failure(exc)

    async def _plan_failure(self, task: _WindowTask, exc: Exception) -> _FailurePlan:
        """Decide the outcome of one failed attempt, atomically w.r.t. state."""

        diagnosis = self._diagnose(exc)
        task.diagnosis = diagnosis
        task.last_failure_kind = diagnosis.kind
        task.attempt_generation_at_failure = task.generation
        self.metrics.provider_failure_kind = diagnosis.kind
        self.metrics.last_concurrency_failure_kind = diagnosis.kind

        async with self._concurrency_state_lock:
            if diagnosis.retriable_concurrency:
                if task.generation != self._generation:
                    # This error belongs to a concurrency state that was already
                    # superseded by another window's downgrade.
                    return self._plan_stale(task, diagnosis)
                if self._max <= 1:
                    self.terminal_concurrency_failures += 1
                    self.metrics.terminal_concurrency_failures += 1
                    return _FailurePlan(
                        diagnosis.kind,
                        terminal_error=ProviderConcurrencyLimitAtSerialExecution(
                            "Provider still rejected a concurrency limit while running "
                            "strictly serial (effective concurrency = 1)"
                        ),
                        metadata={"reason": "serial_execution"},
                    )
                if task.adaptive_retries >= self.retry_limit:
                    self.terminal_concurrency_failures += 1
                    self.metrics.terminal_concurrency_failures += 1
                    return _FailurePlan(
                        diagnosis.kind,
                        terminal_error=ProviderConcurrencyRetryExhausted(
                            "Provider concurrency limit persisted across the retry budget"
                        ),
                        metadata={"reason": "retry_exhausted"},
                    )
                self._downgrade_locked(diagnosis.kind, window=task.label)
                task.adaptive_retries += 1
                task.generation = self._generation
                task.limit = self._max
                self.window_retries += 1
                self.metrics.window_retries += 1
                return _FailurePlan(
                    diagnosis.kind,
                    requeue=True,
                    downgraded=True,
                    metadata={"generation": self._generation, "limit": self._max},
                )

            if diagnosis.retriable:
                # Transient throughput limit: bounded replay, never a
                # concurrency downgrade.
                task.rate_limit_replays += 1
                if task.rate_limit_replays > self.retry_limit:
                    return _FailurePlan(
                        diagnosis.kind,
                        terminal_error=ProviderRateLimitRetryExhausted(
                            "Provider transient rate limit persisted across the retry budget"
                        ),
                        metadata={"reason": "rate_limit_exhausted"},
                    )
                self.metrics.rate_limit_replays += 1
                return _FailurePlan(
                    diagnosis.kind,
                    requeue=True,
                    rate_limited=True,
                    metadata={"attempt": task.rate_limit_replays},
                )

            self.metrics.non_retriable_capacity_failures += 1
            return _FailurePlan(diagnosis.kind, terminal_error=exc)

    def _plan_stale(self, task: _WindowTask, diagnosis: CapacityDiagnosis) -> _FailurePlan:
        """A rejection from an old generation: replay, never downgrade or fail."""

        self.stale_rejection_count += 1
        self.metrics.stale_rejection_count += 1
        task.stale_replays += 1
        task.is_stale_generation = True
        if task.stale_replays > self.stale_replay_limit:
            return _FailurePlan(
                diagnosis.kind,
                terminal_error=ProviderConcurrencyRetryExhausted(
                    "Window exceeded its stale-generation replay budget"
                ),
                metadata={"reason": "stale_replay_exhausted"},
            )
        self.stale_replayed_windows += 1
        self.metrics.stale_replayed_windows += 1
        task.generation = self._generation
        task.limit = self._max
        return _FailurePlan(
            diagnosis.kind,
            requeue=True,
            stale=True,
            metadata={"generation": self._generation, "limit": self._max},
        )

    async def worker(self) -> None:
        while True:
            task = await self.queue.get()
            self.metrics.window_queue_depth = self.queue.qsize()
            async with self._gate:
                while self._active >= self._max:
                    await self._gate.wait()
                self._active += 1
                # Capture the concurrency state this attempt actually runs under.
                attempt_generation = self._generation
                attempt_limit = self._max
            task.attempt_number += 1
            task.generation = attempt_generation
            task.limit = attempt_limit
            task.started_at = time.time()
            tick = time.perf_counter()
            self.metrics.active_classification_workers += 1
            self.metrics.active_independent_sessions = self.metrics.active_classification_workers
            self.metrics.peak_active_classification_workers = max(
                self.metrics.peak_active_classification_workers,
                self.metrics.active_classification_workers,
            )
            self.metrics.peak_independent_sessions = max(
                self.metrics.peak_independent_sessions,
                self.metrics.active_independent_sessions,
            )
            requeue = False
            plan: _FailurePlan | None = None
            try:
                await task.work()
            except asyncio.CancelledError:
                current = asyncio.current_task()
                await self._release(tick)
                if current is not None and current.cancelling():
                    raise
                if self.error is None:
                    self.error = RuntimeError("classification_window_cancelled")
                self.queue.task_done()
                continue
            except Exception as exc:
                plan = await self._plan_failure(task, exc)
                requeue = plan.requeue
                task.will_requeue = requeue
                if not requeue and plan.terminal_error is not None and self.error is None:
                    self.error = plan.terminal_error
                    self.terminal_error = plan.terminal_error
                    self.failed_window = task.label or None
            finally:
                await self._release(tick)
            if requeue and plan is not None:
                delay = max(0.0, float(self._backoff_seconds(task.attempt_number - 1)))
                if delay > 0:
                    await self._sleep(delay)
                # Balance the queue's unfinished-task counter before requeueing.
                self.queue.task_done()
                await self._enqueue(task)
            else:
                self.queue.task_done()

    async def _release(self, tick: float) -> None:
        self.metrics.active_classification_workers = max(
            0, self.metrics.active_classification_workers - 1
        )
        self.metrics.active_independent_sessions = max(
            0, self.metrics.active_classification_workers
        )
        self.metrics.classification_worker_seconds += time.perf_counter() - tick
        self.metrics.average_active_classification_workers = (
            self.metrics.classification_worker_seconds
            / max(1e-9, time.perf_counter() - self.started)
        )
        async with self._gate:
            self._active = max(0, self._active - 1)
            self._gate.notify_all()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if exc_type is None:
                await self.queue.join()
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            self.metrics.window_queue_depth = 0
            self.metrics.average_active_classification_workers = (
                self.metrics.classification_worker_seconds
                / max(1e-9, time.perf_counter() - self.started)
            )
        if exc_type is None and self.error is not None:
            raise self.error


__all__ = [
    "DEFAULT_RETRY_LIMIT",
    "ClassificationDispatch",
    "ProviderConcurrencyLimitAtSerialExecution",
    "ProviderConcurrencyRetryExhausted",
    "ProviderRateLimitRetryExhausted",
]
