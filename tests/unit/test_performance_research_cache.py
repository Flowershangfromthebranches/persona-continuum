from __future__ import annotations

import asyncio

from persona_continuum.application.research_backend import CachingResearchBackend
from persona_continuum.performance.research_cache import (
    ResearchQueryCache,
    ResearchSourceCache,
    SourceIntelligenceCache,
    canonical_url_of,
    classify_freshness,
    normalize_query,
)


def test_normalize_and_canonical_helpers() -> None:
    assert normalize_query("  Steve   Jobs  ") == "steve jobs"
    assert canonical_url_of("https://x.test/a/#frag") == "https://x.test/a"
    assert canonical_url_of("https://x.test/a/") == "https://x.test/a"
    assert classify_freshness("latest news about x") == "news"
    assert classify_freshness("early life biography") == "background"


def test_source_cache_fetches_each_url_once_across_personas() -> None:
    async def scenario() -> None:
        cache = ResearchSourceCache()
        calls = {"n": 0}

        async def fetcher(url: str) -> str:
            calls["n"] += 1
            return f"body::{url}"

        # Steve Jobs persona fetches, then Tim Cook persona re-fetches overlap.
        a, hit_a = await cache.fetch("https://x.test/page-1", fetcher)
        b, hit_b = await cache.fetch("https://x.test/page-1", fetcher)
        assert a == b == "body::https://x.test/page-1"
        assert hit_a is False and hit_b is True
        assert calls["n"] == 1
        assert cache.stats.source_hits == 1
        assert cache.stats.source_misses == 1

    asyncio.run(scenario())


def test_source_cache_dedups_concurrent_identical_fetches() -> None:
    async def scenario() -> None:
        cache = ResearchSourceCache()
        calls = {"n": 0}
        gate = asyncio.Event()

        async def slow(url: str) -> str:
            calls["n"] += 1
            await gate.wait()
            return f"body::{url}"

        t1 = asyncio.create_task(cache.fetch("https://x.test/dup", slow))
        await asyncio.sleep(0)
        t2 = asyncio.create_task(cache.fetch("https://x.test/dup", slow))
        await asyncio.sleep(0)
        gate.set()
        (r1, _), (r2, _) = await asyncio.gather(t1, t2)
        assert r1 == r2
        # Two concurrent requests for the same URL trigger a single fetcher call.
        assert calls["n"] == 1

    asyncio.run(scenario())


def test_query_cache_respects_freshness_ttl() -> None:
    async def scenario() -> None:
        cache = ResearchQueryCache(background_ttl_seconds=3600)
        # The constructor floors the news TTL (>=30s); force it to 0 here so a
        # test can prove news results are never served from cache.
        cache.news_ttl_seconds = 0.0
        calls = {"n": 0}

        async def searcher(q: str) -> list[dict[str, object]]:
            calls["n"] += 1
            return [{"url": f"https://x.test/{q}"}]

        # Background query cached.
        r1, hit1 = await cache.search(
            backend_name="cli", query="biography of ada", limit=10, searcher=searcher
        )
        r2, hit2 = await cache.search(
            backend_name="cli", query="  Biography   of  Ada ", limit=10, searcher=searcher
        )
        assert hit1 is False and hit2 is True
        assert calls["n"] == 1
        assert r1 == r2
        # News query (ttl 0) is never served from cache.
        await cache.search(
            backend_name="cli", query="latest news x", limit=10, searcher=searcher
        )
        await cache.search(
            backend_name="cli", query="latest news x", limit=10, searcher=searcher
        )
        assert calls["n"] == 3  # background once + news twice (not cached)

    asyncio.run(scenario())


def test_batch_transport_uses_one_backend_batch_and_populates_caches() -> None:
    class BatchBackend:
        name = "native_cli"

        def __init__(self) -> None:
            self.search_batches = 0
            self.fetch_batches = 0

        async def batch_search(self, queries, limit=10):  # type: ignore[no-untyped-def]
            self.search_batches += 1
            return {
                query: [{"canonical_url": f"https://x.test/{index}", "content": query}]
                for index, query in enumerate(queries)
            }

        async def batch_fetch(self, urls):  # type: ignore[no-untyped-def]
            self.fetch_batches += 1
            return {url: f"body::{url}" for url in urls}

    async def scenario() -> None:
        inner = BatchBackend()
        backend = CachingResearchBackend(
            inner,
            source_cache=ResearchSourceCache(),
            query_cache=ResearchQueryCache(),
        )
        queries = ["alpha", "beta", "gamma"]
        first = await backend.batch_search(queries, limit=5)
        second = await backend.batch_search(queries, limit=5)
        assert first == second
        assert inner.search_batches == 1

        urls = ["https://x.test/a", "https://x.test/b"]
        pages_a = await backend.batch_fetch(urls)
        pages_b = await backend.batch_fetch(urls)
        assert pages_a == pages_b
        assert inner.fetch_batches == 1

    asyncio.run(scenario())


def test_source_intelligence_cache_keys_model_namespace_and_content() -> None:
    cache = SourceIntelligenceCache()
    cache.put("model-a:max", "same page", {"events": ["launch"]})
    assert cache.get("model-a:max", "same page") == {"events": ["launch"]}
    assert cache.get("model-b:max", "same page") is None
