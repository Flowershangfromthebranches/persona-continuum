"""Long-TTL cache for Agent model discovery results.

``adapter.list_models()`` is expensive for local CLI runtimes: for Codex it
spawns an app-server process, performs the JSON-RPC initialize handshake, runs
``model/list``, and reaps the process.  Nothing about the answer changes
during normal operation, so every ``open_session`` must hit this cache instead
of re-running discovery.

The cache is only refreshed when something that can change the answer changed:

- program startup (empty cache)
- manual agent rescan
- executable path / file identity change
- adapter-reported version change
- a requested model was not found, or the transport errored (callers call
  :meth:`invalidate` / :meth:`refresh`)
- TTL expiry (deliberately long: discovery data is deployment-stable)
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from persona_continuum.agent.adapter import AgentAdapter
    from persona_continuum.agent.models import ModelCapability

DEFAULT_MODEL_CAPABILITY_TTL_SECONDS = 6 * 60 * 60


def _binary_identity(adapter: AgentAdapter) -> str:
    """Fingerprint the adapter executable so binary swaps invalidate entries."""

    identity = getattr(adapter, "capability_cache_identity", None)
    if callable(identity):
        try:
            value = identity()
            if value:
                return str(value)
        except Exception:
            pass

    path: str | None = None
    finder = getattr(adapter, "_find_binary", None)
    if callable(finder):
        try:
            path = finder()
        except Exception:
            path = None
    if not path:
        probe_binary = getattr(adapter, "binary_path", None)
        if isinstance(probe_binary, str):
            path = probe_binary
    if not path:
        return "api"
    try:
        stat = os.stat(path)
        fingerprint = f"{path}:{stat.st_size}:{int(stat.st_mtime)}"
    except OSError:
        fingerprint = f"{path}:missing"
    return fingerprint


def capability_cache_key(
    adapter: AgentAdapter,
    *,
    auth_profile_id: str | None = None,
) -> tuple[str, str, str]:
    binary_identity = _binary_identity(adapter)
    auth_component = str(auth_profile_id or "default")
    return (str(getattr(adapter, "adapter_id", "unknown")), binary_identity, auth_component)


@dataclass(slots=True)
class ModelCapabilityCacheStats:
    hits: int = 0
    misses: int = 0
    refreshes: int = 0
    invalidations: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "refreshes": self.refreshes,
            "invalidations": self.invalidations,
        }


@dataclass(slots=True)
class _CacheEntry:
    models: list[ModelCapability]
    created_at: float
    version: str | None = None


class ModelCapabilityCache:
    """In-process capability cache shared by executor, room, and world runtimes."""

    def __init__(
        self, *, ttl_seconds: float = DEFAULT_MODEL_CAPABILITY_TTL_SECONDS, enabled: bool = True
    ) -> None:
        self.ttl_seconds = max(30.0, float(ttl_seconds))
        self.enabled = bool(enabled)
        self.stats = ModelCapabilityCacheStats()
        self._entries: dict[tuple[str, str, str], _CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def get_models(
        self,
        adapter: AgentAdapter,
        *,
        auth_profile_id: str | None = None,
    ) -> list[ModelCapability]:
        """Return cached models, refreshing at most once per key under contention."""

        from persona_continuum.performance.tracing import default_tracer

        if not self.enabled:
            # Legacy execution: discover on every call, exactly as before.
            models = await self._discover(adapter)
            default_tracer().incr_global("model_list_count", 1)
            return models

        key = capability_cache_key(adapter, auth_profile_id=auth_profile_id)
        async with self._lock:
            entry = self._entries.get(key)
            now = time.monotonic()
            if entry is not None and now - entry.created_at <= self.ttl_seconds:
                current_version = await self._adapter_version_hint(adapter)
                if current_version and entry.version and current_version != entry.version:
                    self.stats.invalidations += 1
                else:
                    self.stats.hits += 1
                    return list(entry.models)
            self.stats.misses += 1
        # Discovery runs outside the lock: some adapters (Codex) call store()
        # from inside their own list_models() and this lock is not reentrant.
        # A concurrent duplicate discovery is harmless: last writer wins.
        models = await self._discover(adapter)
        version = await self._adapter_version_hint(adapter)
        async with self._lock:
            self._entries[key] = _CacheEntry(models=list(models), created_at=now, version=version)
            self.stats.refreshes += 1
        default_tracer().incr_global("model_list_count", 1)
        return list(models)

    def peek(
        self, adapter: AgentAdapter, *, auth_profile_id: str | None = None
    ) -> list[ModelCapability] | None:
        """Return cached models without ever triggering discovery."""

        if not self.enabled:
            return None
        key = capability_cache_key(adapter, auth_profile_id=auth_profile_id)
        entry = self._entries.get(key)
        if entry is None or time.monotonic() - entry.created_at > self.ttl_seconds:
            return None
        return list(entry.models)

    async def invalidate(
        self, adapter: AgentAdapter, *, auth_profile_id: str | None = None
    ) -> None:
        key = capability_cache_key(adapter, auth_profile_id=auth_profile_id)
        async with self._lock:
            if self._entries.pop(key, None) is not None:
                self.stats.invalidations += 1

    async def store(
        self,
        adapter: AgentAdapter,
        models: list[ModelCapability],
        *,
        auth_profile_id: str | None = None,
    ) -> None:
        """Persist an externally discovered model snapshot (e.g. from probe)."""

        key = capability_cache_key(adapter, auth_profile_id=auth_profile_id)
        version = await self._adapter_version_hint(adapter)
        async with self._lock:
            self._entries[key] = _CacheEntry(
                models=list(models), created_at=time.monotonic(), version=version
            )
            self.stats.refreshes += 1

    def clear(self) -> None:
        removed = len(self._entries)
        self._entries.clear()
        self.stats.invalidations += removed

    @property
    def populated_keys(self) -> int:
        return len(self._entries)

    def snapshot(self) -> dict[str, Any]:
        return {
            "ttl_seconds": self.ttl_seconds,
            "populated_keys": self.populated_keys,
            **self.stats.as_dict(),
        }

    async def _discover(self, adapter: AgentAdapter) -> list[ModelCapability]:
        return list(await adapter.list_models())

    @staticmethod
    async def _adapter_version_hint(adapter: AgentAdapter) -> str | None:
        getter = getattr(adapter, "cached_version_hint", None)
        if callable(getter):
            try:
                result = getter()
                if asyncio.iscoroutine(result):
                    result = await result
                return str(result) if result else None
            except Exception:
                return None
        return None


_process_default: ModelCapabilityCache | None = None


def configure_default_model_capability_cache(
    *, enabled: bool = True, ttl_seconds: float = DEFAULT_MODEL_CAPABILITY_TTL_SECONDS
) -> ModelCapabilityCache:
    """Create or retune the process-wide capability cache from config."""

    global _process_default
    if _process_default is None:
        _process_default = ModelCapabilityCache(enabled=enabled, ttl_seconds=ttl_seconds)
    else:
        _process_default.enabled = bool(enabled)
        _process_default.ttl_seconds = max(30.0, float(ttl_seconds))
    return _process_default


def default_model_capability_cache() -> ModelCapabilityCache:
    global _process_default
    if _process_default is None:
        _process_default = ModelCapabilityCache()
    return _process_default


__all__ = [
    "DEFAULT_MODEL_CAPABILITY_TTL_SECONDS",
    "ModelCapabilityCache",
    "ModelCapabilityCacheStats",
    "capability_cache_key",
    "configure_default_model_capability_cache",
    "default_model_capability_cache",
]
