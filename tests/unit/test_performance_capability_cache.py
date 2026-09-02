from __future__ import annotations

import asyncio

from persona_continuum.agent.models import ModelCapability
from persona_continuum.agent.protocols.openai_compatible import OpenAICompatibleAPIAdapter
from persona_continuum.performance.capability_cache import (
    ModelCapabilityCache,
    capability_cache_key,
)


class CountingAdapter:
    adapter_id = "counting"
    name = "Counting"

    def __init__(self) -> None:
        self.list_calls = 0

    async def list_models(self) -> list[ModelCapability]:
        self.list_calls += 1
        return [
            ModelCapability(
                id="m1", display_name="M1", provider="x", supported_reasoning_efforts=["high"]
            )
        ]


def test_capability_cache_hits_avoid_repeated_discovery() -> None:
    async def scenario() -> None:
        adapter = CountingAdapter()
        cache = ModelCapabilityCache(ttl_seconds=3600)
        first = await cache.get_models(adapter)
        second = await cache.get_models(adapter)
        third = await cache.get_models(adapter)
        assert len(first) == len(second) == len(third) == 1
        # Normal work: model/list must run once, not per session.
        assert adapter.list_calls == 1
        assert cache.stats.hits == 2
        assert cache.stats.misses == 1

    asyncio.run(scenario())


def test_capability_cache_invalidate_forces_refresh() -> None:
    async def scenario() -> None:
        adapter = CountingAdapter()
        cache = ModelCapabilityCache()
        await cache.get_models(adapter)
        await cache.invalidate(adapter)
        await cache.get_models(adapter)
        assert adapter.list_calls == 2

    asyncio.run(scenario())


def test_capability_cache_disabled_discovers_every_time() -> None:
    async def scenario() -> None:
        adapter = CountingAdapter()
        cache = ModelCapabilityCache(enabled=False)
        await cache.get_models(adapter)
        await cache.get_models(adapter)
        assert adapter.list_calls == 2
        assert cache.peek(adapter) is None

    asyncio.run(scenario())


def test_capability_cache_version_hint_invalidates() -> None:
    async def scenario() -> None:
        adapter = CountingAdapter()
        version_state = {"v": "1.0"}

        async def hint() -> str:
            return version_state["v"]

        adapter.cached_version_hint = hint  # type: ignore[attr-defined]
        cache = ModelCapabilityCache()
        await cache.get_models(adapter)
        version_state["v"] = "2.0"
        await cache.get_models(adapter)
        # Version change forces a re-discovery.
        assert adapter.list_calls == 2

    asyncio.run(scenario())


def test_api_profile_capability_changes_invalidate_cache_identity() -> None:
    before = OpenAICompatibleAPIAdapter(
        adapter_id="api_profile",
        default_model="m1",
        model_capabilities={},
    )
    after = OpenAICompatibleAPIAdapter(
        adapter_id="api_profile",
        default_model="m1",
        model_capabilities={
            "m1": {
                "reasoning_efforts": ["low", "high", "ultra"],
                "default_reasoning_effort": "high",
            }
        },
    )
    assert capability_cache_key(before) != capability_cache_key(after)


def test_clear_counts_removed_entries() -> None:
    async def scenario() -> None:
        adapter = CountingAdapter()
        cache = ModelCapabilityCache()
        await cache.get_models(adapter)
        cache.clear()
        assert cache.populated_keys == 0
        assert cache.stats.invalidations == 1

    asyncio.run(scenario())
