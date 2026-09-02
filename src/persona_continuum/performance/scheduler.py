"""Priority-aware bounded execution scheduler.

Persona Creation, Room turns, World ticks, research, and Material
Intelligence all compete for model slots, search/fetch bandwidth, and CPU.
Historically each subsystem ran its own unbounded ``asyncio`` fan-out.  This
scheduler gives every long-running flow one shared place to wait, with:

- a global LLM execution semaphore and per-adapter child semaphores,
- dedicated search / fetch / CPU semaphores,
- priority wake-up so INTERACTIVE room turns never queue behind ten heavy
  BACKGROUND enrichment jobs for the next free slot,
- cooperative cancellation: a cancelled waiter releases nothing it never held
  and a cancelled holder always frees its slot through the context manager.

Quality policy is untouched -- the scheduler decides *when* work runs, never
*what* runs or with which model/reasoning settings.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from persona_continuum.numeric import safe_int


class ExecutionClass:
    INTERACTIVE = 0  # current room turn, persona chat, user-initiated request
    FOREGROUND = 1   # persona creation, parallel world initialization
    BACKGROUND = 2   # enrichment, indexing, cache refresh, health checks

    LEVELS = {"interactive": 0, "foreground": 1, "background": 2}

    @classmethod
    def normalise(cls, priority: str | int | None) -> int:
        if priority is None:
            return cls.FOREGROUND
        if isinstance(priority, bool):
            return cls.FOREGROUND
        if isinstance(priority, int):
            return max(0, min(2, priority))
        return cls.LEVELS.get(str(priority).strip().casefold(), cls.FOREGROUND)


class _Waiter:
    __slots__ = ("priority", "sequence", "future")

    def __init__(self, priority: int, sequence: int) -> None:
        self.priority = priority
        self.sequence = sequence
        self.future: asyncio.Future[None] | None = None

    def order_key(self) -> tuple[int, int]:
        return (self.priority, self.sequence)


class PrioritySemaphore:
    """A semaphore that hands units directly to priority-ordered waiters.

    A woken waiter already owns its unit (the releaser decremented before
    resolving the future); cancellation of a woken waiter returns the unit.

    ``reserved_units`` keeps a slice of the capacity for higher-priority
    callers: BACKGROUND work may never consume the reserved units, so an
    interactive room turn never waits for a background job to release.  The
    reserve degrades automatically when capacity is 1.
    """

    def __init__(self, value: int, *, reserved_units: int = 0) -> None:
        self._capacity = max(1, value)
        self._available = self._capacity
        self._reserved = (
            max(0, min(int(reserved_units), self._capacity - 1))
            if self._capacity > 1
            else 0
        )
        self._waiters: list[_Waiter] = []
        self._counter = itertools.count()

    @property
    def available(self) -> int:
        return self._available

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def reserved_units(self) -> int:
        return self._reserved

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    async def acquire(self, priority: int = ExecutionClass.FOREGROUND) -> None:
        level = ExecutionClass.normalise(priority)
        reserve = self._reserved if level >= ExecutionClass.BACKGROUND else 0
        # Direct grant when units are available (respecting the reserve for
        # background callers) and no lower-priority waiter has been queued
        # for longer; otherwise join the priority-ordered wait queue.
        if self._available > reserve and (
            not self._waiters or self._waiters[0].priority >= level
        ):
            self._available -= 1
            return
        loop = asyncio.get_running_loop()
        waiter = _Waiter(level, next(self._counter))
        waiter.future = loop.create_future()
        self._waiters.append(waiter)
        self._waiters.sort(key=lambda item: item.order_key())
        try:
            await waiter.future
        except asyncio.CancelledError:
            if waiter.future is not None and waiter.future.done():
                # The unit was already handed over; return it to the pool.
                self.release()
            if waiter in self._waiters:
                self._waiters.remove(waiter)
            raise

    def release(self) -> None:
        self._available += 1
        self._handoff()

    def _handoff(self) -> None:
        # Give free units directly to the highest-priority waiters; background
        # waiters are skipped over while only reserved units remain free.
        index = 0
        while index < len(self._waiters) and self._available > 0:
            waiter = self._waiters[index]
            if (
                waiter.priority >= ExecutionClass.BACKGROUND
                and self._available <= self._reserved
            ):
                index += 1
                continue
            self._waiters.pop(index)
            self._available -= 1
            future = waiter.future
            assert future is not None
            if not future.done():
                future.set_result(None)

    def grow(self, delta: int = 1) -> None:
        """Raise capacity (adaptive scheduler reclaiming headroom)."""

        delta = max(0, int(delta))
        if delta == 0:
            return
        self._capacity += delta
        self._available += delta
        self._handoff()

    def shrink(self, delta: int = 1, *, floor: int = 1) -> None:
        """Lower capacity; already-held units are not revoked."""

        delta = max(0, int(delta))
        self._capacity = max(floor, self._capacity - delta)
        self._reserved = min(self._reserved, max(0, self._capacity - 1))
        self._available = max(0, min(self._available, self._capacity))


class AdaptiveConcurrencyController:
    """AIMD-style job concurrency controller driven by runtime feedback.

    It limits whole Persona research jobs while the shared priority scheduler
    continues to arbitrate individual LLM/search/fetch operations.  Rate-limit,
    timeout, crash, queue-delay, and memory-pressure signals reduce the target;
    a sustained healthy streak restores one slot at a time.
    """

    def __init__(
        self,
        *,
        minimum: int = 1,
        initial: int = 3,
        maximum: int = 6,
        recovery_successes: int = 4,
    ) -> None:
        self.minimum = max(1, int(minimum))
        self.maximum = max(self.minimum, int(maximum))
        self.target = max(self.minimum, min(self.maximum, int(initial)))
        self.recovery_successes = max(1, int(recovery_successes))
        self.active = 0
        self.waiting = 0
        self._healthy_streak = 0
        self._last_backoff_reason: str | None = None
        self._latency_ema_ms: float | None = None
        self._condition = asyncio.Condition()

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        async with self._condition:
            self.waiting += 1
            try:
                while self.active >= self.target:
                    await self._condition.wait()
                self.active += 1
            finally:
                self.waiting = max(0, self.waiting - 1)
        try:
            yield
        finally:
            async with self._condition:
                self.active = max(0, self.active - 1)
                self._condition.notify_all()

    async def record_success(self, *, latency_ms: float | None = None) -> None:
        async with self._condition:
            if latency_ms is not None and latency_ms >= 0:
                self._latency_ema_ms = (
                    latency_ms
                    if self._latency_ema_ms is None
                    else self._latency_ema_ms * 0.8 + latency_ms * 0.2
                )
            self._healthy_streak += 1
            if self._healthy_streak >= self.recovery_successes and self.target < self.maximum:
                self.target += 1
                self._healthy_streak = 0
                self._last_backoff_reason = None
                self._condition.notify_all()

    async def record_failure(self, error: BaseException | str) -> None:
        text = str(error).casefold()
        signals = {
            "rate_limit": any(token in text for token in ("429", "rate limit", "rate_limit")),
            "timeout": "timeout" in text or "timed out" in text,
            "transport": any(
                token in text for token in ("transport", "connection reset", "broken pipe")
            ),
            "crash": any(token in text for token in ("crash", "process exited", "exit code")),
            "memory_pressure": any(token in text for token in ("memory pressure", "out of memory")),
        }
        reason = next((name for name, matched in signals.items() if matched), "failure")
        async with self._condition:
            self.target = max(self.minimum, self.target - 1)
            self._healthy_streak = 0
            self._last_backoff_reason = reason
            self._condition.notify_all()

    async def record_pressure(
        self, *, queue_delay_ms: float = 0.0, memory_pressure: bool = False
    ) -> None:
        if memory_pressure or queue_delay_ms >= 30_000:
            await self.record_failure(
                "memory pressure" if memory_pressure else "runtime queue delay timeout"
            )

    def snapshot(self) -> dict[str, Any]:
        return {
            "minimum": self.minimum,
            "target": self.target,
            "maximum": self.maximum,
            "active": self.active,
            "waiting": self.waiting,
            "healthy_streak": self._healthy_streak,
            "last_backoff_reason": self._last_backoff_reason,
            "latency_ema_ms": round(self._latency_ema_ms, 3)
            if self._latency_ema_ms is not None
            else None,
        }


@dataclass(slots=True)
class SchedulerSnapshot:
    llm_available: int
    llm_waiting: int
    search_available: int
    fetch_available: int
    cpu_available: int
    per_adapter: dict[str, dict[str, int]]
    llm_reserved_units: int = 0
    llm_capacity: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "llm_available": self.llm_available,
            "llm_waiting": self.llm_waiting,
            "llm_reserved_units": self.llm_reserved_units,
            "llm_capacity": self.llm_capacity,
            "search_available": self.search_available,
            "fetch_available": self.fetch_available,
            "cpu_available": self.cpu_available,
            "per_adapter": self.per_adapter,
        }


class ExecutionScheduler:
    """One scheduler instance per process, owned by the application container."""

    def __init__(
        self,
        *,
        max_llm_concurrency: int = 4,
        max_search_concurrency: int = 3,
        max_fetch_concurrency: int = 4,
        max_cpu_concurrency: int = 4,
        per_adapter_limits: dict[str, int] | None = None,
        interactive_reserved_units: int | None = None,
    ) -> None:
        llm_capacity = max(1, safe_int(max_llm_concurrency, default=4, minimum=1) or 4)
        if interactive_reserved_units is None:
            # Capacity 1 degrades to no reservation; otherwise keep one model
            # slot permanently available for INTERACTIVE work.
            interactive_reserved_units = 1 if llm_capacity > 1 else 0
        self.llm_semaphore = PrioritySemaphore(
            llm_capacity,
            reserved_units=max(0, safe_int(interactive_reserved_units, default=0, minimum=0) or 0),
        )
        self.search_semaphore = PrioritySemaphore(
            max(1, safe_int(max_search_concurrency, default=3, minimum=1) or 3)
        )
        self.fetch_semaphore = PrioritySemaphore(
            max(1, safe_int(max_fetch_concurrency, default=4, minimum=1) or 4)
        )
        self.cpu_semaphore = PrioritySemaphore(
            max(1, safe_int(max_cpu_concurrency, default=4, minimum=1) or 4)
        )
        self._per_adapter: dict[str, PrioritySemaphore] = {}
        self._per_adapter_limits: dict[str, int] = {
            str(key): max(1, safe_int(value, default=2, minimum=1) or 2)
            for key, value in (per_adapter_limits or {}).items()
        }
        self._lock = asyncio.Lock()

    async def adapter_semaphore(self, adapter_id: str) -> PrioritySemaphore:
        async with self._lock:
            existing = self._per_adapter.get(adapter_id)
            if existing is not None:
                return existing
            limit = self._per_adapter_limits.get(adapter_id, self.llm_semaphore._capacity)
            semaphore = PrioritySemaphore(max(1, min(limit, self.llm_semaphore._capacity)))
            self._per_adapter[adapter_id] = semaphore
            return semaphore

    @asynccontextmanager
    async def llm_slot(
        self,
        *,
        adapter_id: str | None = None,
        priority: str | int | None = None,
    ) -> AsyncIterator[None]:
        level = ExecutionClass.normalise(priority)
        await self.llm_semaphore.acquire(level)
        try:
            adapter_sem = (
                await self.adapter_semaphore(adapter_id) if adapter_id else None
            )
            if adapter_sem is not None:
                await adapter_sem.acquire(level)
            try:
                yield
            finally:
                if adapter_sem is not None:
                    adapter_sem.release()
        finally:
            self.llm_semaphore.release()

    @asynccontextmanager
    async def search_slot(self, *, priority: str | int | None = None) -> AsyncIterator[None]:
        await self.search_semaphore.acquire(ExecutionClass.normalise(priority))
        try:
            yield
        finally:
            self.search_semaphore.release()

    @asynccontextmanager
    async def fetch_slot(self, *, priority: str | int | None = None) -> AsyncIterator[None]:
        await self.fetch_semaphore.acquire(ExecutionClass.normalise(priority))
        try:
            yield
        finally:
            self.fetch_semaphore.release()

    @asynccontextmanager
    async def cpu_slot(self, *, priority: str | int | None = None) -> AsyncIterator[None]:
        await self.cpu_semaphore.acquire(ExecutionClass.normalise(priority))
        try:
            yield
        finally:
            self.cpu_semaphore.release()

    def snapshot(self) -> SchedulerSnapshot:
        return SchedulerSnapshot(
            llm_available=self.llm_semaphore.available,
            llm_waiting=self.llm_semaphore.waiting,
            llm_reserved_units=self.llm_semaphore.reserved_units,
            llm_capacity=self.llm_semaphore.capacity,
            search_available=self.search_semaphore.available,
            fetch_available=self.fetch_semaphore.available,
            cpu_available=self.cpu_semaphore.available,
            per_adapter={
                name: {"available": sem.available, "waiting": sem.waiting}
                for name, sem in sorted(self._per_adapter.items())
            },
        )


_process_default_scheduler: ExecutionScheduler | None = None


def register_default_execution_scheduler(scheduler: ExecutionScheduler) -> ExecutionScheduler:
    """Install the container-owned scheduler as the process default.

    Adapters and back-ends that construct their own ``AgentRuntimeExecutor``
    (research worker sessions, host agent) resolve the singleton through
    ``default_execution_scheduler`` and therefore share one set of bounded
    slots and one priority queue.
    """

    global _process_default_scheduler
    _process_default_scheduler = scheduler
    return scheduler


def default_execution_scheduler(**kwargs: object) -> ExecutionScheduler:
    global _process_default_scheduler
    if _process_default_scheduler is None:
        _process_default_scheduler = ExecutionScheduler(**kwargs)  # type: ignore[arg-type]
    return _process_default_scheduler


__all__ = [
    "AdaptiveConcurrencyController",
    "ExecutionClass",
    "ExecutionScheduler",
    "PrioritySemaphore",
    "SchedulerSnapshot",
    "default_execution_scheduler",
    "register_default_execution_scheduler",
]
