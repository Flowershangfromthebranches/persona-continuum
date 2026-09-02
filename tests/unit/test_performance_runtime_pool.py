from __future__ import annotations

import asyncio

from persona_continuum.performance.runtime_pool import AgentRuntimePool


class FakeTransport:
    """Minimal stand-in for SubprocessAgentTransport used by the pool."""

    def __init__(self, *, alive: bool = True) -> None:
        self.closed = False
        self.force_closed = False
        self._alive = alive

    class _Proc:
        def __init__(self, alive: bool) -> None:
            self.returncode = None if alive else 1

    @property
    def process(self) -> object:
        return self._Proc(self._alive)

    @property
    def process_alive(self) -> bool:
        return self._alive

    async def close(self, *, force: bool = False) -> None:
        self.closed = True
        self.force_closed = force
        self._alive = False

    async def wait(self) -> int:
        return 0

    async def readline(self) -> bytes:
        return b""


def _factory(counter: dict) -> object:
    async def make() -> FakeTransport:
        counter["spawns"] += 1
        return FakeTransport()

    return make


def test_sequential_sessions_reuse_one_physical_runtime() -> None:
    async def scenario() -> None:
        pool = AgentRuntimePool(max_processes_per_key=4)
        counter = {"spawns": 0}
        make = _factory(counter)
        # Persona job: many short logical sessions in sequence.
        leases = []
        for _ in range(5):
            lease = await pool.acquire("codex:x", make)
            leases.append(lease)
            await lease.managed.ensure_initialized(_noop_init)
            await lease.release()
        assert counter["spawns"] == 1  # one process for five sessions
        assert pool.stats.reuses == 4
        # A parked process is healthy and reusable.
        assert pool.snapshot()["parked_processes"] == 1
        await pool.shutdown()

    asyncio.run(scenario())


def test_concurrent_leases_use_distinct_processes_up_to_bound() -> None:
    async def scenario() -> None:
        pool = AgentRuntimePool(max_processes_per_key=2)
        counter = {"spawns": 0}
        make = _factory(counter)
        ready = asyncio.Event()
        release = asyncio.Event()

        async def worker() -> None:
            lease = await pool.acquire("codex:x", make)
            await lease.managed.ensure_initialized(_noop_init)
            ready.set()
            await release.wait()
            await lease.release()

        t1 = asyncio.create_task(worker())
        await ready.wait()
        ready.clear()
        t2 = asyncio.create_task(worker())
        await ready.wait()
        # Two concurrent leases -> two processes (bound=2).
        assert counter["spawns"] == 2
        release.set()
        await asyncio.gather(t1, t2)
        await pool.shutdown()

    asyncio.run(scenario())


def test_third_lease_waits_for_a_free_process() -> None:
    async def scenario() -> None:
        pool = AgentRuntimePool(max_processes_per_key=1)
        counter = {"spawns": 0}
        make = _factory(counter)
        hold = await pool.acquire("codex:x", make)
        await hold.managed.ensure_initialized(_noop_init)

        acquired = asyncio.Event()

        async def waiter() -> None:
            lease = await pool.acquire("codex:x", make)
            acquired.set()
            await lease.release()

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.01)
        assert not acquired.is_set()  # bounded: cannot exceed one process
        assert counter["spawns"] == 1  # second not spawned yet
        await hold.release()
        await asyncio.wait_for(acquired.wait(), timeout=1)
        await task
        # After release, waiter reused the single parked process.
        assert counter["spawns"] == 1
        await pool.shutdown()

    asyncio.run(scenario())


def test_crashed_runtime_is_discarded_and_replaced() -> None:
    async def scenario() -> None:
        pool = AgentRuntimePool(max_processes_per_key=4)
        counter = {"spawns": 0}
        make = _factory(counter)
        lease = await pool.acquire("codex:x", make)
        await lease.managed.ensure_initialized(_noop_init)
        # Simulate transport death.
        lease.managed.transport._alive = False
        lease.mark_unhealthy()
        await lease.release()
        assert pool.snapshot()["parked_processes"] == 0
        fresh = await pool.acquire("codex:x", make)
        assert counter["spawns"] == 2  # replacement process
        await fresh.release()
        await pool.shutdown()

    asyncio.run(scenario())


def test_idle_reaper_closes_stale_processes() -> None:
    async def scenario() -> None:
        pool = AgentRuntimePool(max_processes_per_key=4, idle_timeout_seconds=30)
        counter = {"spawns": 0}
        make = _factory(counter)
        lease = await pool.acquire("codex:x", make)
        await lease.managed.ensure_initialized(_noop_init)
        await lease.release()
        lease.managed.last_used_monotonic -= 1000  # appear idle
        reaped = await pool.reap_idle()
        assert reaped >= 1
        assert pool.snapshot()["parked_processes"] == 0
        await pool.shutdown()

    asyncio.run(scenario())


def test_make_key_separates_auth_profiles_and_redacts_env() -> None:
    key_a = AgentRuntimePool.make_key("codex", ["codex", "app-server"], {"OPENAI_API_KEY": "sk-a"})
    key_b = AgentRuntimePool.make_key("codex", ["codex", "app-server"], {"OPENAI_API_KEY": "sk-b"})
    assert key_a != key_b
    # Secret material must never appear in the key itself.
    assert "sk-a" not in key_a and "sk-b" not in key_b


def test_twenty_logical_sessions_share_four_physical_runtimes_without_starvation() -> None:
    async def scenario() -> None:
        pool = AgentRuntimePool(max_processes_per_key=4)
        counter = {"spawns": 0}
        make = _factory(counter)

        async def open_logical(index: int) -> object:
            lease = await pool.acquire("codex:scale", make)
            await lease.managed.ensure_initialized(_noop_init)
            await pool.retain_logical(lease.managed)
            managed = lease.managed
            await asyncio.sleep(0)
            await lease.release()
            return managed

        logical = await asyncio.wait_for(
            asyncio.gather(*(open_logical(index) for index in range(20))), timeout=2
        )
        assert len(logical) == 20
        assert counter["spawns"] <= 4
        assert pool.snapshot()["logical_sessions"] == 20

        completed: list[int] = []

        async def execute_turn(index: int, managed: object) -> None:
            lease = await pool.acquire_managed(managed)  # type: ignore[arg-type]
            try:
                await asyncio.sleep(0.001)
                completed.append(index)
            finally:
                await lease.release()

        await asyncio.wait_for(
            asyncio.gather(
                *(execute_turn(index, managed) for index, managed in enumerate(logical))
            ),
            timeout=2,
        )
        assert sorted(completed) == list(range(20))
        for managed in logical:
            await pool.release_logical(managed)  # type: ignore[arg-type]
        await pool.shutdown()

    asyncio.run(scenario())


def test_dimension_persona_world_and_room_logical_groups_exceed_pool_capacity() -> None:
    async def scenario() -> None:
        pool = AgentRuntimePool(max_processes_per_key=4)
        counter = {"spawns": 0}
        make = _factory(counter)
        groups = {
            "dimension": 8,
            "persona": 10,
            "world_actor": 10,
            "room_participant": 12,
        }

        async def run_group(label: str, index: int) -> str:
            first = await pool.acquire("codex:groups", make)
            await pool.retain_logical(first.managed)
            managed = first.managed
            await first.release()
            turn = await pool.acquire_managed(managed)
            try:
                await asyncio.sleep(0)
                return f"{label}:{index}"
            finally:
                await turn.release()
                await pool.release_logical(managed)

        expected = sum(groups.values())
        completed = await asyncio.wait_for(
            asyncio.gather(
                *(
                    run_group(label, index)
                    for label, count in groups.items()
                    for index in range(count)
                )
            ),
            timeout=3,
        )
        assert len(completed) == expected
        assert counter["spawns"] <= 4
        assert pool.snapshot()["active_leases"] == 0
        await pool.shutdown()

    asyncio.run(scenario())


async def _noop_init() -> None:
    return None
