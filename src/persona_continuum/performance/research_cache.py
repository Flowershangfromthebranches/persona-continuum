"""Caches shared by public research pipelines.

Two layers avoid repeating identical network work across research rounds,
concurrent persona jobs, and Parallel World multi-persona initialization:

``ResearchSourceCache``
    Caches *fetched pages* keyed by canonical URL (with ETag / Last-Modified
    revalidation hints).  It never caches judgments about what a source means
    for a specific persona -- only the raw text, its hash, and page metadata.
    Per-persona interpretation still runs through model reasoning every time.

``ResearchQueryCache``
    Short-lived cache for *search result lists* keyed by backend + normalized
    query + freshness class + limit.  Freshness matters: news-flavoured
    queries expire quickly while biographical/background queries may be
    reused substantially longer without letting research stagnate.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from persona_continuum.numeric import safe_int

DEFAULT_SOURCE_TTL_SECONDS = 7 * 24 * 60 * 60
BACKGROUND_QUERY_TTL_SECONDS = 12 * 60 * 60
NEWS_QUERY_TTL_SECONDS = 30 * 60

_NEWS_TOKENS = (
    "latest",
    "news",
    "today",
    "2025",
    "2026",
    "最新",
    "今天",
    "新闻",
    "近况",
)

_URL_STRIP = re.compile(r"[#?].*$")


def normalize_query(query: str) -> str:
    return re.sub(r"\s+", " ", str(query or "").strip()).casefold()


def classify_freshness(query: str) -> str:
    lowered = str(query or "").casefold()
    return "news" if any(token in lowered for token in _NEWS_TOKENS) else "background"


def canonical_url_of(url: str) -> str:
    cleaned = _URL_STRIP.sub("", str(url or "").strip())
    return cleaned.rstrip("/")


@dataclass(slots=True)
class _SourceEntry:
    payload: Any
    content_hash: str
    etag: str | None
    last_modified: str | None
    fetched_at_monotonic: float
    ttl_seconds: float


@dataclass(slots=True)
class _QueryEntry:
    results: list[dict[str, Any]]
    created_at_monotonic: float
    ttl_seconds: float


@dataclass(slots=True)
class ResearchCacheStats:
    source_hits: int = 0
    source_misses: int = 0
    query_hits: int = 0
    query_misses: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "source_hits": self.source_hits,
            "source_misses": self.source_misses,
            "query_hits": self.query_hits,
            "query_misses": self.query_misses,
        }


class SourceIntelligenceCache:
    """Cache public source-level factual extraction across Persona jobs.

    Values intentionally exclude Persona-specific motivations, values, and
    dimension judgments.  Private/user-provided material never opts into this
    process-wide cache.
    """

    def __init__(self, *, max_entries: int = 2048) -> None:
        self.max_entries = max(32, int(max_entries))
        self._entries: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self.hits = 0
        self.misses = 0

    @staticmethod
    def make_key(namespace: str, content: str) -> str:
        return hashlib.sha256(
            f"{namespace}\0{content}".encode("utf-8", errors="replace")
        ).hexdigest()

    def get(self, namespace: str, content: str) -> dict[str, Any] | None:
        value = self._entries.get(self.make_key(namespace, content))
        if value is None:
            self.misses += 1
            return None
        self.hits += 1
        return dict(value)

    def put(self, namespace: str, content: str, payload: dict[str, Any]) -> None:
        key = self.make_key(namespace, content)
        if key in self._entries:
            self._order.remove(key)
        self._entries[key] = dict(payload)
        self._order.append(key)
        overflow = len(self._order) - self.max_entries
        if overflow > 0:
            for stale in self._order[:overflow]:
                self._entries.pop(stale, None)
            del self._order[:overflow]

    def snapshot(self) -> dict[str, int]:
        return {
            "entries": len(self._entries),
            "max_entries": self.max_entries,
            "hits": self.hits,
            "misses": self.misses,
        }


class ResearchSourceCache:
    """Bounded in-memory cache of fetched web sources."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_SOURCE_TTL_SECONDS,
        max_entries: int = 512,
    ) -> None:
        self.ttl_seconds = max(60.0, float(ttl_seconds))
        self.max_entries = max(16, safe_int(max_entries, default=512, minimum=16) or 512)
        self.stats = ResearchCacheStats()
        self._entries: dict[str, _SourceEntry] = {}
        self._order: list[str] = []
        self._inflight: dict[str, asyncio.Future[Any]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def content_hash(payload: Any) -> str:
        text = payload if isinstance(payload, str) else repr(
            {k: v for k, v in payload.items()} if isinstance(payload, dict) else payload
        )
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()

    async def fetch(
        self,
        url: str,
        fetcher: Callable[[str], Awaitable[Any] | Any],
    ) -> tuple[Any, bool]:
        """Return ``(payload, from_cache)`` for one canonical URL."""

        url = canonical_url_of(url)
        if not url:
            return await _maybe_await(fetcher("")), False
        async with self._lock:
            entry = self._entries.get(url)
            if (
                entry is not None
                and time.monotonic() - entry.fetched_at_monotonic <= entry.ttl_seconds
            ):
                entry.fetched_at_monotonic = time.monotonic()  # LRU touch (TTL from origin fetch)
                self.stats.source_hits += 1
                return entry.payload, True
            pending = self._inflight.get(url)
            if pending is not None:
                owner = False
            else:
                loop = asyncio.get_running_loop()
                pending = loop.create_future()
                self._inflight[url] = pending
                owner = True
        if not owner:
            # The in-flight owner resolves with the ``(payload, from_cache)``
            # tuple; the waiter adopts the identical result (never a double
            # origin fetch).
            return await asyncio.shield(_await_future(pending))
        try:
            payload = await _maybe_await(fetcher(url))
            digest = self.content_hash(payload)
            headers = payload.get("headers") if isinstance(payload, dict) else None
            async with self._lock:
                self._store(
                    url,
                    _SourceEntry(
                        payload=payload,
                        content_hash=digest,
                        etag=str(headers.get("etag")) if isinstance(headers, dict) else None,
                        last_modified=(
                            str(headers.get("last_modified"))
                            if isinstance(headers, dict)
                            else None
                        ),
                        fetched_at_monotonic=time.monotonic(),
                        ttl_seconds=self.ttl_seconds,
                    ),
                )
                future = self._inflight.pop(url, None)
                if future is not None and not future.done():
                    future.set_result((payload, False))
            self.stats.source_misses += 1
            return payload, False
        except BaseException as exc:
            async with self._lock:
                future = self._inflight.pop(url, None)
                if future is not None and not future.done():
                    future.set_exception(exc)
            raise

    def peek_content_hash(self, url: str) -> str | None:
        entry = self._entries.get(canonical_url_of(url))
        return entry.content_hash if entry else None

    def peek(self, url: str) -> Any | None:
        entry = self._entries.get(canonical_url_of(url))
        if entry is None:
            return None
        if time.monotonic() - entry.fetched_at_monotonic > entry.ttl_seconds:
            return None
        self.stats.source_hits += 1
        return entry.payload

    def _store(self, url: str, entry: _SourceEntry) -> None:
        if url in self._entries:
            self._order.remove(url)
        self._entries[url] = entry
        self._order.append(url)
        overflow = len(self._order) - self.max_entries
        if overflow > 0:
            for stale in self._order[:overflow]:
                self._entries.pop(stale, None)
            del self._order[:overflow]

    def snapshot(self) -> dict[str, Any]:
        return {
            "entries": len(self._entries),
            "max_entries": self.max_entries,
            "ttl_seconds": self.ttl_seconds,
            **self.stats.as_dict(),
        }


class ResearchQueryCache:
    """Short-TTL cache for search result lists."""

    def __init__(
        self,
        *,
        background_ttl_seconds: float = BACKGROUND_QUERY_TTL_SECONDS,
        news_ttl_seconds: float = NEWS_QUERY_TTL_SECONDS,
        max_entries: int = 256,
    ) -> None:
        self.background_ttl_seconds = max(60.0, float(background_ttl_seconds))
        self.news_ttl_seconds = max(30.0, float(news_ttl_seconds))
        self.max_entries = max(16, safe_int(max_entries, default=256, minimum=16) or 256)
        self.stats = ResearchCacheStats()
        self._entries: dict[tuple[str, str, str, int], _QueryEntry] = {}
        self._order: list[tuple[str, str, str, int]] = []

    async def search(
        self,
        *,
        backend_name: str,
        query: str,
        limit: int,
        language: str = "",
        searcher: Callable[[str], Awaitable[list[dict[str, Any]]] | list[dict[str, Any]]],
    ) -> tuple[list[dict[str, Any]], bool]:
        normalized = normalize_query(query)
        freshness = classify_freshness(query)
        key = (backend_name, normalized, freshness, safe_int(limit, default=10, minimum=1) or 10)
        now = time.monotonic()
        entry = self._entries.get(key)
        if entry is not None and now - entry.created_at_monotonic <= entry.ttl_seconds:
            self.stats.query_hits += 1
            return [dict(item) for item in entry.results], True
        results = await _maybe_await(searcher(query))
        normalized_results = [dict(item) for item in results or [] if isinstance(item, dict)]
        ttl = (
            self.news_ttl_seconds
            if freshness == "news"
            else self.background_ttl_seconds
        )
        if key not in self._entries and len(self._entries) >= self.max_entries:
            stale_key = self._order.pop(0) if self._order else None
            if stale_key is not None:
                self._entries.pop(stale_key, None)
        self._entries[key] = _QueryEntry(
            results=normalized_results,
            created_at_monotonic=time.monotonic(),
            ttl_seconds=ttl,
        )
        self._order.append(key)
        self.stats.query_misses += 1
        return normalized_results, False

    def peek(
        self, *, backend_name: str, query: str, limit: int
    ) -> list[dict[str, Any]] | None:
        freshness = classify_freshness(query)
        key = (
            backend_name,
            normalize_query(query),
            freshness,
            safe_int(limit, default=10, minimum=1) or 10,
        )
        entry = self._entries.get(key)
        if entry is None or time.monotonic() - entry.created_at_monotonic > entry.ttl_seconds:
            return None
        self.stats.query_hits += 1
        return [dict(item) for item in entry.results]

    def clear(self) -> None:
        self._entries.clear()
        self._order.clear()

    def snapshot(self) -> dict[str, Any]:
        return {
            "entries": len(self._entries),
            "background_ttl_seconds": self.background_ttl_seconds,
            "news_ttl_seconds": self.news_ttl_seconds,
            **self.stats.as_dict(),
        }


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _await_future(future: asyncio.Future[Any]) -> Any:
    return await asyncio.shield(future)


_default_source_intelligence_cache = SourceIntelligenceCache()


def default_source_intelligence_cache() -> SourceIntelligenceCache:
    return _default_source_intelligence_cache


__all__ = [
    "ResearchCacheStats",
    "ResearchQueryCache",
    "ResearchSourceCache",
    "SourceIntelligenceCache",
    "canonical_url_of",
    "classify_freshness",
    "default_source_intelligence_cache",
    "normalize_query",
]
