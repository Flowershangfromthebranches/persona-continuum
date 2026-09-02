from __future__ import annotations

import asyncio

from persona_continuum.performance.scheduler import (
    AdaptiveConcurrencyController,
    ExecutionClass,
    ExecutionScheduler,
    PrioritySemaphore,
)


def test_interactive_wakes_before_background() -> None:
    async def scenario() -> None:
        sem = PrioritySemaphore(1)
        order: list[str] = []
        await sem.acquire(ExecutionClass.FOREGROUND)  # hold it

        async def worker(name: str, level: int) -> None:
            await sem.acquire(level)
            order.append(name)
            await asyncio.sleep(0)
            sem.release()

        b1 = asyncio.create_task(worker("bg", ExecutionClass.BACKGROUND))
        b2 = asyncio.create_task(worker("bg2", ExecutionClass.BACKGROUND))
        i1 = asyncio.create_task(worker("int", ExecutionClass.INTERACTIVE))
        await asyncio.sleep(0.02)  # let all three queue
        sem.release()  # free the held unit
        await asyncio.gather(b1, b2, i1)
        # Interactive got the first free unit despite enqueuing last.
        assert order[0] == "int"
        # Background FIFO preserved among equal priorities.
        assert order[1:] == ["bg", "bg2"]

    asyncio.run(scenario())


def test_llm_slot_enforces_global_bound() -> None:
    async def scenario() -> None:
        scheduler = ExecutionScheduler(max_llm_concurrency=2, per_adapter_limits={})
        peak = {"n": 0, "cur": 0}

        async def run() -> None:
            async with scheduler.llm_slot(adapter_id="codex"):
                peak["cur"] += 1
                peak["n"] = max(peak["n"], peak["cur"])
                await asyncio.sleep(0.02)
                peak["cur"] -= 1

        await asyncio.gather(*(run() for _ in range(8)))
        assert peak["n"] <= 2

    asyncio.run(scenario())


def test_per_adapter_limit_is_independent() -> None:
    async def scenario() -> None:
        scheduler = ExecutionScheduler(
            max_llm_concurrency=4, per_adapter_limits={"codex": 1}
        )
        active_codex = {"cur": 0, "max": 0}

        async def codex() -> None:
            async with scheduler.llm_slot(adapter_id="codex"):
                active_codex["cur"] += 1
                active_codex["max"] = max(active_codex["max"], active_codex["cur"])
                await asyncio.sleep(0.02)
                active_codex["cur"] -= 1

        await asyncio.gather(*(codex() for _ in range(5)))
        assert active_codex["max"] == 1  # per-adapter cap

    asyncio.run(scenario())


def test_cancelled_waiter_does_not_leak_slot() -> None:
    async def scenario() -> None:
        sem = PrioritySemaphore(1)
        await sem.acquire(ExecutionClass.FOREGROUND)
        task = asyncio.create_task(sem.acquire(ExecutionClass.FOREGROUND))
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        sem.release()  # original holder leaves
        # A fresh acquire must succeed (capacity not lost to the cancelled one).
        await asyncio.wait_for(sem.acquire(ExecutionClass.FOREGROUND), timeout=1)
        assert sem.available >= 0

    asyncio.run(scenario())


def test_grow_shrink_adjust_capacity() -> None:
    async def scenario() -> None:
        sem = PrioritySemaphore(1)
        sem.grow(2)
        assert sem.capacity == 3
        await sem.acquire()
        await sem.acquire()
        await sem.acquire()
        sem.shrink(1)
        assert sem.capacity >= 1

    asyncio.run(scenario())


def test_adaptive_persona_controller_backs_off_and_recovers() -> None:
    async def scenario() -> None:
        controller = AdaptiveConcurrencyController(
            minimum=1, initial=4, maximum=6, recovery_successes=2
        )
        await controller.record_failure("HTTP 429 rate limit")
        assert controller.snapshot()["target"] == 3
        assert controller.snapshot()["last_backoff_reason"] == "rate_limit"
        await controller.record_failure("transport timeout")
        assert controller.snapshot()["target"] == 2
        await controller.record_success(latency_ms=100)
        await controller.record_success(latency_ms=90)
        assert controller.snapshot()["target"] == 3

    asyncio.run(scenario())


def test_adaptive_persona_controller_enforces_current_target() -> None:
    async def scenario() -> None:
        controller = AdaptiveConcurrencyController(minimum=1, initial=2, maximum=4)
        live = {"current": 0, "peak": 0}

        async def worker() -> None:
            async with controller.slot():
                live["current"] += 1
                live["peak"] = max(live["peak"], live["current"])
                await asyncio.sleep(0.01)
                live["current"] -= 1

        await asyncio.gather(*(worker() for _ in range(10)))
        assert live["peak"] == 2

    asyncio.run(scenario())
