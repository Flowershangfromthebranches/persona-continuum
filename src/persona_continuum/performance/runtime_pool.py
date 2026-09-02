"""Bounded pool of long-lived physical Agent runtime processes.

Adapters whose protocol keeps one OS process per native session (Codex
app-server, ACP agent processes) previously paid a full process spawn +
protocol handshake for every logical session, then destroyed the process.

The pool changes the unit of reuse: one *physical* runtime process hosts many
sequential *logical* sessions.  Logical sessions retain an affinity to a
physical process, but only an executing protocol operation holds the exclusive
lease.  This preserves stdio framing while allowing far more persistent
logical threads than physical processes.

Persona isolation is structural, not incidental:

- the physical process is stateless between turns (Codex keeps all
  conversational state in per-thread ids; ACP in per-session ids);
- every logical session performs its own ``thread/start`` / ``session/new``
  and only ever addresses its own thread/session id;
- no prompt content is carried by the pool itself.

Bounding model: every managed process has at most one active execution lease.
The pool creates at most ``max_processes_per_key`` processes for a key and
waiters are notified whenever any process becomes executable.  Failures are
contained per process: when a transport dies, its manager is marked unhealthy,
discarded, and replaced on the next acquire.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from persona_continuum.agent.subprocess_transport import SubprocessAgentTransport
from persona_continuum.numeric import safe_int

DEFAULT_IDLE_TIMEOUT_SECONDS = 15 * 60
REAP_INTERVAL_SECONDS = 60.0


class _Releasable(Protocol):
    """Structural type: anything with a ``release()`` (semaphore or noop)."""

    def release(self) -> None: ...


class _NoopSlot:
    def release(self) -> None:
        return None


@dataclass(slots=True)
class RuntimePoolStats:
    spawns: int = 0
    reuses: int = 0
    crashes: int = 0
    reaped_idle: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "spawns": self.spawns,
            "reuses": self.reuses,
            "crashes": self.crashes,
            "reaped_idle": self.reaped_idle,
        }


class ManagedRuntime:
    """One persistent child process plus its serialized initialization."""

    def __init__(
        self,
        *,
        key: str,
        transport: SubprocessAgentTransport,
        max_idle_seconds: float,
    ) -> None:
        self.key = key
        self.transport = transport
        self.created_at_monotonic = time.monotonic()
        self.last_used_monotonic = time.monotonic()
        self.max_idle_seconds = max_idle_seconds
        self.healthy = True
        self.shared_state: dict[str, Any] = {}
        self.initialized = False
        self._init_lock = asyncio.Lock()
        self.in_use = False
        self.logical_sessions = 0
        self.execution_count = 0

    @property
    def process(self) -> asyncio.subprocess.Process | None:
        return self.transport.process

    @property
    def alive(self) -> bool:
        proc = self.transport.process
        return proc is not None and proc.returncode is None

    @property
    def idle_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.last_used_monotonic)

    async def ensure_initialized(
        self, initializer: Callable[[], Awaitable[None]]
    ) -> None:
        """Run the one-time protocol handshake once per physical process."""

        if self.initialized:
            return
        async with self._init_lock:
            if self.initialized:
                return
            await initializer()
            self.initialized = True

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.transport.close(force=True)


class PoolAcquiredRuntime:
    """An exclusive lease over one managed physical runtime."""

    def __init__(
        self,
        managed: ManagedRuntime,
        *,
        pool: AgentRuntimePool,
        slot: _Releasable,
    ) -> None:
        self.managed = managed
        self._pool = pool
        self._slot = slot
        self._released = False

    @property
    def transport(self) -> SubprocessAgentTransport:
        return self.managed.transport

    @property
    def shared_state(self) -> dict[str, Any]:
        return self.managed.shared_state

    def touch(self) -> None:
        self.managed.last_used_monotonic = time.monotonic()

    def mark_unhealthy(self) -> None:
        self.managed.healthy = False

    async def release(self, *, kill: bool = False) -> None:
        """Return the physical runtime.  ``kill`` tears the process down."""

        if self._released:
            return
        self._released = True
        self.touch()
        await self._pool.release(self.managed, kill=kill)
        self._slot.release()


class AgentRuntimePool:
    """Per-key bounded physical runtimes with turn-scoped leases."""

    def __init__(
        self,
        *,
        max_processes_per_key: int = 4,
        idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
        enabled: bool = True,
    ) -> None:
        self.enabled = bool(enabled)
        self.max_processes_per_key = max(
            1, safe_int(max_processes_per_key, default=4, minimum=1) or 4
        )
        self.idle_timeout_seconds = max(30.0, float(idle_timeout_seconds))
        self.stats = RuntimePoolStats()
        self._entries: dict[str, list[ManagedRuntime]] = {}
        self._creating: dict[str, int] = {}
        self._active_leases: int = 0
        self._lock = asyncio.Lock()
        self._condition = asyncio.Condition(self._lock)
        self._reaper_task: asyncio.Task[None] | None = None

    @staticmethod
    def make_key(adapter_id: str, command: list[str], env: dict[str, str] | None) -> str:
        # The env fingerprint covers auth/credential differences without
        # storing any secret material beyond its digest.
        interesting = {
            k: v
            for k, v in (env or {}).items()
            if k.upper() in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"}
            or k.startswith(("CODEX_", "ACP_", "CLAUDE_"))
        }
        digest = hashlib.sha256(
            repr(sorted(interesting.items())).encode("utf-8")
        ).hexdigest()[:16]
        return f"{adapter_id}:{' '.join(command)}:{digest}"

    async def acquire(
        self,
        key: str,
        factory: Callable[[], Awaitable[SubprocessAgentTransport]],
        *,
        max_idle_seconds: float | None = None,
    ) -> PoolAcquiredRuntime:
        """Lease an existing healthy process or spawn one within bounds."""

        if not self.enabled:
            transport = await factory()
            managed = ManagedRuntime(
                key=key,
                transport=transport,
                max_idle_seconds=DEFAULT_IDLE_TIMEOUT_SECONDS,
            )
            managed.initialized = True  # Disabled pools do not manage handshake state.
            return PoolAcquiredRuntime(managed, pool=self, slot=_NoopSlot())
        self._ensure_reaper()
        idle_budget = (
            self.idle_timeout_seconds
            if max_idle_seconds is None
            else max(30.0, float(max_idle_seconds))
        )
        while True:
            async with self._condition:
                bucket = self._entries.setdefault(key, [])
                dead = [item for item in bucket if not item.healthy or not item.alive]
                for item in dead:
                    bucket.remove(item)
                    if not item.alive:
                        self.stats.crashes += 1
                    asyncio.create_task(_discard(item))
                idle = [item for item in bucket if not item.in_use]
                # Least-loaded placement: a new persistent logical session
                # goes to the parked process with the fewest logical sessions
                # (most idle first among equals), so twelve sessions over
                # four processes land ~3/3/3/3 instead of 12/0/0/0.
                candidate = (
                    min(idle, key=lambda item: (item.logical_sessions, -item.idle_seconds))
                    if idle
                    else None
                )
                if candidate is not None:
                    candidate.in_use = True
                    candidate.execution_count += 1
                    self.stats.reuses += 1
                    self._active_leases += 1
                    lease = PoolAcquiredRuntime(candidate, pool=self, slot=_NoopSlot())
                    lease.touch()
                    return lease
                creating = self._creating.get(key, 0)
                if len(bucket) + creating < self.max_processes_per_key:
                    self._creating[key] = creating + 1
                    break
                await self._condition.wait()

        try:
            transport = await factory()
        except BaseException:
            async with self._condition:
                self._creating[key] = max(0, self._creating.get(key, 1) - 1)
                self._condition.notify_all()
            raise
        managed = ManagedRuntime(key=key, transport=transport, max_idle_seconds=idle_budget)
        managed.in_use = True
        managed.execution_count = 1
        async with self._condition:
            self._creating[key] = max(0, self._creating.get(key, 1) - 1)
            self._entries.setdefault(key, []).append(managed)
            self.stats.spawns += 1
            from persona_continuum.performance.tracing import default_tracer

            default_tracer().incr_global("physical_process_spawn_count", 1)
            self._active_leases += 1
            self._condition.notify_all()
        return PoolAcquiredRuntime(managed, pool=self, slot=_NoopSlot())

    async def acquire_managed(self, managed: ManagedRuntime) -> PoolAcquiredRuntime:
        """Acquire the execution lease for a logical session's affinity."""

        async with self._condition:
            while True:
                bucket = self._entries.get(managed.key, [])
                if managed not in bucket or not managed.healthy or not managed.alive:
                    raise RuntimeError("pooled_runtime_affinity_unavailable")
                if not managed.in_use:
                    managed.in_use = True
                    managed.execution_count += 1
                    self.stats.reuses += 1
                    self._active_leases += 1
                    lease = PoolAcquiredRuntime(managed, pool=self, slot=_NoopSlot())
                    lease.touch()
                    return lease
                await self._condition.wait()

    async def retain_logical(self, managed: ManagedRuntime) -> None:
        """Keep thread affinity without consuming an execution lease."""

        async with self._condition:
            if managed in self._entries.get(managed.key, []):
                managed.logical_sessions += 1

    async def release_logical(self, managed: ManagedRuntime) -> None:
        async with self._condition:
            managed.logical_sessions = max(0, managed.logical_sessions - 1)
            managed.last_used_monotonic = time.monotonic()
            self._condition.notify_all()

    async def release(self, managed: ManagedRuntime, *, kill: bool = False) -> None:
        victim: ManagedRuntime | None = None
        async with self._condition:
            self._active_leases = max(0, self._active_leases - 1)
            managed.in_use = False
            if kill or not managed.healthy or not managed.alive:
                bucket = self._entries.get(managed.key, [])
                if managed in bucket:
                    bucket.remove(managed)
                victim = managed
            else:
                managed.last_used_monotonic = time.monotonic()
            self._condition.notify_all()
        if victim is not None:
            asyncio.create_task(_discard(victim))

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_processes_per_key": self.max_processes_per_key,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "physical_processes": sum(len(v) for v in self._entries.values()),
            "parked_processes": sum(
                1 for bucket in self._entries.values() for item in bucket if not item.in_use
            ),
            "logical_sessions": sum(
                item.logical_sessions for bucket in self._entries.values() for item in bucket
            ),
            "logical_sessions_per_runtime": sorted(
                (
                    item.logical_sessions
                    for bucket in self._entries.values()
                    for item in bucket
                ),
                reverse=True,
            ),
            "active_leases": self._active_leases,
            **self.stats.as_dict(),
        }

    async def reap_idle(self) -> int:
        reaped = 0
        victims: list[ManagedRuntime] = []
        async with self._condition:
            for key, bucket in list(self._entries.items()):
                kept: list[ManagedRuntime] = []
                for managed in bucket:
                    if (
                        not managed.in_use
                        and managed.logical_sessions == 0
                        and (managed.idle_seconds >= managed.max_idle_seconds or not managed.alive)
                    ):
                        reaped += 1
                        self.stats.reaped_idle += 1
                        victims.append(managed)
                    else:
                        kept.append(managed)
                if kept:
                    self._entries[key] = kept
                else:
                    self._entries.pop(key, None)
            self._condition.notify_all()
        for managed in victims:
            asyncio.create_task(_discard(managed))
        return reaped

    async def shutdown(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper_task
            self._reaper_task = None
        async with self._condition:
            buckets = list(self._entries.values())
            self._entries.clear()
            victims = [m for bucket in buckets for m in bucket]
            for victim in victims:
                victim.in_use = False
            self._active_leases = 0
            self._condition.notify_all()
        await asyncio.gather(*(_discard(m) for m in victims), return_exceptions=True)

    def _ensure_reaper(self) -> None:
        if self._reaper_task is not None and not self._reaper_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._reaper_task = loop.create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(REAP_INTERVAL_SECONDS)
            try:
                await self.reap_idle()
            except asyncio.CancelledError:
                raise
            except Exception:
                continue


async def _discard(managed: ManagedRuntime) -> None:
    with contextlib.suppress(Exception):
        await managed.close()


_process_default_pool: AgentRuntimePool | None = None


def configure_default_runtime_pool(
    *,
    enabled: bool = True,
    max_processes_per_key: int = 4,
    idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
) -> AgentRuntimePool:
    """Create (or retune) the process-wide pool from application config."""

    global _process_default_pool
    if _process_default_pool is None:
        _process_default_pool = AgentRuntimePool(
            enabled=enabled,
            max_processes_per_key=max_processes_per_key,
            idle_timeout_seconds=idle_timeout_seconds,
        )
    else:
        _process_default_pool.enabled = bool(enabled)
        _process_default_pool.max_processes_per_key = max(1, int(max_processes_per_key))
        _process_default_pool.idle_timeout_seconds = max(30.0, float(idle_timeout_seconds))
    return _process_default_pool


def default_runtime_pool(**kwargs: Any) -> AgentRuntimePool:
    global _process_default_pool
    if _process_default_pool is None:
        _process_default_pool = AgentRuntimePool(**kwargs)
    return _process_default_pool


__all__ = [
    "AgentRuntimePool",
    "ManagedRuntime",
    "PoolAcquiredRuntime",
    "RuntimePoolStats",
    "configure_default_runtime_pool",
    "default_runtime_pool",
]
