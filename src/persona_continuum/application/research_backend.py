from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import os
import re
from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from persona_continuum.agent.adapter import AgentAdapter
from persona_continuum.agent.models import (
    AgentSessionConfig,
    PermissionProfile,
    ResearchCapability,
    ResearchVerificationStatus,
)
from persona_continuum.agent.runtime_executor import AgentRuntimeExecutor
from persona_continuum.agent.structured_output import StructuredResult
from persona_continuum.application.research_capability_cache import ResearchCapabilityCache
from persona_continuum.numeric import safe_acp_stream_limit, safe_int, safe_timeout

_worker_seq = itertools.count(1)

# Probe-failure markers.  They are matched against an exception's *own*
# surface (message, typed code, adapter-classified CLI failure) and never
# against its diagnostics blob: every turn's diagnostics contain
# timeout-budget keys and the sanitized command shape, whose permission
# flags are expected policy arguments rather than denials.
_GEO_BLOCK_MARKERS = (
    "user location is not supported",
    "location is not supported",
)
_AUTH_MARKERS = (
    "auth",
    "login",
    "credential",
    "unauthorized",
    "not authenticated",
)
_POLICY_MARKERS = (
    "permission",
    "policy",
    "not allowed",
    "disallowed",
    "denied",
    "forbidden",
    "sandbox",
    "headless",
    "allowlist",
)
# Typed codes the runtime executor raises when the turn's own timeout
# budget expires; only these are genuine "the session timed out" signals.
_EXECUTOR_TIMEOUT_CODES = frozenset(
    {"AGENT_IDLE_TIMEOUT", "AGENT_HARD_TIMEOUT", "AGENT_TURN_TIMEOUT"}
)


def _proxy_hint() -> str:
    """Explain the most common cause of a hanging CLI: no proxy in this process.

    CLI subprocesses inherit this server's environment, so a Runtime that
    needs a proxy hangs until the turn budget expires when the Web service
    was started without ``HTTP_PROXY``/``HTTPS_PROXY``.
    """

    configured = [
        name
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
        if os.environ.get(name)
    ]
    if configured:
        return ""
    return (
        "当前服务进程未检测到 HTTP_PROXY/HTTPS_PROXY 环境变量，"
        "CLI 子进程会继承该环境——若该 CLI 需要代理才能联网，"
        "请在启动 Web 服务的终端配置代理后重启服务。"
    )


def new_worker_seq() -> int:
    return next(_worker_seq)


class ResearchBackend(Protocol):
    name: str

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]: ...

    async def fetch(self, url: str) -> dict[str, Any] | str: ...

    async def batch_search(
        self, queries: list[str], limit: int = 10
    ) -> dict[str, list[dict[str, Any]]]: ...

    async def batch_fetch(self, urls: list[str]) -> dict[str, dict[str, Any] | str]: ...


# Public architecture name. ResearchBackend remains for compatibility with
# existing call sites and persisted diagnostics.
ResearchTransport = ResearchBackend


class ResearchCapabilityProbeError(RuntimeError):
    """A concrete, user-actionable failure from a behavioral CLI probe."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        verification_status: ResearchVerificationStatus = ResearchVerificationStatus.UNAVAILABLE,
        can_discover_sources: bool | None = None,
        can_read_sources: bool | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.verification_status = verification_status
        self.can_discover_sources = can_discover_sources
        self.can_read_sources = can_read_sources
        super().__init__(message)


async def validate_probe_source_url(
    url: str,
    *,
    allowed_hosts: tuple[str, ...] = (
        "openai.com",
        "google.com",
        "vertexaisearch.cloud.google.com",
        "googleusercontent.com",
    ),
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Verify that a probe URL is real and contains readable content.

    The validation is intentionally independent of the Agent.  A returned
    string that cannot be fetched is never promoted to ``verified``.
    """

    from urllib.parse import urlparse

    parsed = urlparse(str(url).strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = any(host == item or host.endswith(f".{item}") for item in allowed_hosts)
    if parsed.scheme != "https" or not host or not allowed:
        raise ResearchCapabilityProbeError(
            "probe_url_host_invalid",
            "Agent 返回了非允许的 Research Probe URL；为避免使用幻觉来源，验证已停止。",
        )

    bounded_timeout = safe_timeout(timeout, default=8.0)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(bounded_timeout, connect=min(bounded_timeout, 4.0)),
            follow_redirects=True,
            headers=headers,
        ) as client:
            response = await client.get(str(url))
    except Exception as exc:
        raise ResearchCapabilityProbeError(
            "probe_url_unreachable",
            "Agent 返回了搜索来源，但未能验证其 URL；为避免使用幻觉来源，本次研究已停止。",
        ) from exc

    final = response.url
    final_host = (final.host or "").lower().rstrip(".")
    final_allowed = any(
        final_host == item or final_host.endswith(f".{item}") for item in allowed_hosts
    ) or any(host == item or host.endswith(f".{item}") for item in allowed_hosts)
    if not final_allowed or response.status_code < 200 or response.status_code >= 400:
        raise ResearchCapabilityProbeError(
            "probe_url_http_invalid",
            "Agent 返回了搜索来源，但其 URL 返回了不可用的 HTTP 响应；"
            "为避免使用幻觉来源，本次研究已停止。",
        )
    if not response.content.strip():
        raise ResearchCapabilityProbeError(
            "probe_url_empty_content",
            "Agent 返回的搜索来源正文为空；为避免使用幻觉来源，本次研究已停止。",
        )
    return {
        "url": str(final),
        "status_code": response.status_code,
        "content_length": len(response.content),
        "title": _html_title(response.text),
    }


def _html_title(content: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", content, flags=re.I | re.S)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


class NativeCliResearchBackend:
    """Evidence-oriented bridge to a selected CLI's native web tools.

    Native CLIs differ in how they expose search.  Adapters may provide the
    optional ``research_search``/``research_fetch`` hooks for structured tool
    access.  The fallback asks the selected CLI for a strict provenance
    envelope; a summary without a canonical URL and content is rejected.

    Sessions are *pooled* inside this backend: one long-lived logical worker
    session per concurrent lease instead of spawn/close per query.  The lease
    is exclusive, so protocol framing stays correct; ``aclose`` returns every
    pooled runtime when the research phase ends.
    """

    name = "native_cli"
    MAX_POOLED_SESSIONS = 4

    def __init__(self, adapter: AgentAdapter, runtime: dict[str, Any], *, job_id: str) -> None:
        self.adapter = adapter
        self.runtime = dict(runtime)
        self.job_id = job_id
        self.runtime_executor = AgentRuntimeExecutor()
        self._session_pool: list[Any] = []
        self._pool_guard = asyncio.Lock()

    @staticmethod
    def _backend_system_prompt() -> str:
        return (
            "You are a provenance-preserving research worker. Never invent "
            "a URL or claim that a source was fetched when it was not."
        )

    async def _acquire_session(self) -> Any:
        async with self._pool_guard:
            if self._session_pool:
                return self._session_pool.pop()
        return await self.runtime_executor.open_session(
            self.adapter,
            AgentSessionConfig(
                session_id=f"research_{self.job_id}_worker_{new_worker_seq()}",
                room_id=f"research:{self.job_id}",
                participant_id="research_worker",
                persona_id=self.job_id,
                model_id=self.runtime.get("model_id"),
                reasoning_effort=self.runtime.get("reasoning_effort"),
                auth_profile_id=self.runtime.get("auth_profile_id"),
                permission_profile=PermissionProfile.RESEARCH_READ_ONLY,
                allow_mcp=False,
                tools=[{"name": "google_web_search"}, {"name": "web_fetch"}],
                system_prompt=self._backend_system_prompt(),
                extra={
                    key: self.runtime[key]
                    for key in (
                        "idle_timeout_seconds",
                        "hard_timeout_seconds",
                        "turn_timeout_seconds",
                    )
                    if self.runtime.get(key) is not None
                },
            ),
        )

    def _session_extra(self) -> dict[str, Any]:
        extra: dict[str, Any] = {
            key: self.runtime[key]
            for key in ("idle_timeout_seconds", "hard_timeout_seconds", "turn_timeout_seconds")
            if self.runtime.get(key) is not None
        }
        if self.runtime.get("acp_stream_limit_bytes") is not None:
            extra["acp_stream_limit_bytes"] = safe_acp_stream_limit(
                self.runtime.get("acp_stream_limit_bytes")
            )
        return extra

    async def _release_session(self, binding: Any, *, broken: bool = False) -> None:
        if broken or not getattr(binding.session, "is_active", False):
            with suppress(Exception):
                await self.runtime_executor.close(binding)
            return
        async with self._pool_guard:
            if len(self._session_pool) < self.MAX_POOLED_SESSIONS:
                self._session_pool.append(binding)
                return
        with suppress(Exception):
            await self.runtime_executor.close(binding)

    async def aclose(self) -> None:
        """Close all pooled worker sessions when the research phase ends."""

        async with self._pool_guard:
            pooled = list(self._session_pool)
            self._session_pool.clear()
        for binding in pooled:
            with suppress(Exception):
                await self.runtime_executor.close(binding)

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        normalized_limit = safe_int(limit, default=10, minimum=1, maximum=100) or 10
        hook = getattr(self.adapter, "research_search", None)
        if callable(hook):
            try:
                result = hook(query, limit=normalized_limit, runtime=self.runtime)
            except TypeError:
                result = hook(query, limit=normalized_limit)
            if inspect.isawaitable(result):
                result = await result
            return self._normalise_many(result)
        prompt = (
            "Use your native web search tool now. Search the following query and return "
            "JSON only as an array of source objects. Every object MUST contain "
            "canonical_url, title, publisher, content or snippet, and citation/source_identity. "
            f"Limit={normalized_limit}. Query: {query}"
        )
        payload = await self._ask(prompt, stage="native_search")
        return self._normalise_many(payload)

    async def fetch(self, url: str) -> dict[str, Any] | str:
        hook = getattr(self.adapter, "research_fetch", None)
        if callable(hook):
            try:
                result = hook(url, runtime=self.runtime)
            except TypeError:
                result = hook(url)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, str | dict):
                return result
            return ""
        prompt = (
            "Use your native web fetch/read tool to retrieve this URL. Return JSON only "
            "with canonical_url, title, publisher, content, and citation/source_identity. "
            f"URL: {url}"
        )
        payload = await self._ask(prompt, stage="native_fetch")
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return payload[0]
        return ""

    async def batch_search(
        self, queries: list[str], limit: int = 10
    ) -> dict[str, list[dict[str, Any]]]:
        normalized = [str(query).strip() for query in queries if str(query).strip()]
        if not normalized:
            return {}
        hook = getattr(self.adapter, "research_search", None)
        if callable(hook):
            values = await asyncio.gather(
                *(self.search(query, limit=limit) for query in normalized)
            )
            return dict(zip(normalized, values, strict=True))

        # Chunk large query sets into manageable slices (e.g. 4 queries per turn)
        # to prevent model reasoning + tool calling from timing out.
        chunk_size = 4
        if len(normalized) > chunk_size:
            combined_output: dict[str, list[dict[str, Any]]] = {}
            for i in range(0, len(normalized), chunk_size):
                chunk = normalized[i : i + chunk_size]
                try:
                    chunk_res = await self.batch_search(chunk, limit=limit)
                    combined_output.update(chunk_res)
                except Exception:
                    # Fallback to individual search for robustness
                    for q in chunk:
                        try:
                            single_res = await self.search(q, limit=limit)
                            combined_output[q] = single_res
                        except Exception:
                            combined_output[q] = []
            return combined_output

        payload = await self._ask(
            "Use your native web search tool for every query below in this ONE Agent turn. "
            "You may issue multiple tool calls. Return JSON only as "
            "{\"results\":[{\"query\":\"...\",\"sources\":[...]}]}; every source must "
            "contain canonical_url, title, publisher, content or snippet, and citation. "
            f"Limit per query={limit}. Queries={json.dumps(normalized, ensure_ascii=False)}",
            stage="native_batch_search",
        )
        rows = payload.get("results") if isinstance(payload, dict) else []
        output: dict[str, list[dict[str, Any]]] = {query: [] for query in normalized}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            query = str(row.get("query") or "")
            if query in output:
                output[query] = self._normalise_many(row.get("sources") or [])
        return output

    async def batch_fetch(self, urls: list[str]) -> dict[str, dict[str, Any] | str]:
        normalized = [str(url).strip() for url in urls if str(url).strip()]
        if not normalized:
            return {}
        hook = getattr(self.adapter, "research_fetch", None)
        if callable(hook):
            values = await asyncio.gather(*(self.fetch(url) for url in normalized))
            return dict(zip(normalized, values, strict=True))
        payload = await self._ask(
            "Use your native web fetch/read tool for every URL below in this ONE Agent turn. "
            "You may issue multiple tool calls. Return JSON only as "
            "{\"pages\":[{\"requested_url\":\"...\",\"canonical_url\":\"...\","
            "\"title\":\"...\",\"publisher\":\"...\",\"content\":\"...\","
            "\"citation\":\"...\"}]}. URLs="
            + json.dumps(normalized, ensure_ascii=False),
            stage="native_batch_fetch",
        )
        rows = payload.get("pages") if isinstance(payload, dict) else []
        output: dict[str, dict[str, Any] | str] = {url: "" for url in normalized}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            requested = str(row.get("requested_url") or row.get("url") or "")
            if requested in output:
                output[requested] = dict(row)
        return output

    async def _ask(self, prompt: str, *, stage: str) -> Any:
        session_binding = None
        broken = False
        try:
            session_binding = await self._acquire_session()
            session_binding.session.config.extra.update(self._session_extra())
            schema: dict[str, Any] = {
                "type": "array" if stage == "native_search" else "object"
            }
            result = await self.runtime_executor.execute_structured(
                session_binding,
                system_prompt=self._backend_system_prompt(),
                user_message=prompt,
                schema=schema,
                phase="public_research",
                metadata={"job_id": self.job_id, "research_stage": stage},
            )
            if not isinstance(result, StructuredResult):
                return []
            return result.value
        except Exception:
            # A failed turn may have corrupted the worker's protocol framing;
            # discard the lease instead of returning it to the pool.
            broken = True
            raise
        finally:
            if session_binding is not None:
                await self._release_session(session_binding, broken=broken)

    @classmethod
    def _normalise_many(cls, value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            value = value.get("sources") or value.get("results") or [value]
        if not isinstance(value, Iterable) or isinstance(value, str | bytes):
            return []
        output: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            url = str(
                item.get("canonical_url") or item.get("url") or item.get("link") or ""
            ).strip()
            content = str(
                item.get("content") or item.get("text") or item.get("snippet") or ""
            ).strip()
            identity = (
                item.get("citation") or item.get("source_identity") or item.get("origin_identifier")
            )
            if not url or not content or not identity:
                continue
            output.append(
                {
                    **item,
                    "canonical_url": url,
                    "content": content,
                    "citation": identity,
                    "source_identity": identity,
                }
            )
        return output


class AgenticCliResearchBackend(NativeCliResearchBackend):
    """Research bridge for a READY CLI whose web tools are not declared.

    The selected Agent session is asked to use its own native tools.  The
    returned URL is then validated by Persona Continuum itself before the
    capability is promoted to ``verified``.
    """

    name = "agentic_cli"
    PROBE_URL_HOSTS = ("openai.com",)

    def __init__(
        self,
        adapter: AgentAdapter,
        runtime: dict[str, Any],
        *,
        job_id: str,
        url_validator: Any | None = None,
    ) -> None:
        super().__init__(adapter, runtime, job_id=job_id)
        self.url_validator = url_validator or validate_probe_source_url

    async def verify_cli_research_capability(self) -> ResearchCapability:
        cli_name = str(self.runtime.get("agent_name") or self.runtime.get("agent_id") or "CLI")
        search_prompt = (
            "PROBE_SEARCH. Use the google_web_search tool in this native Web Research "
            "session. Do not answer from training memory and do not invent a URL. "
            "Search for the public OpenAI Developers page at "
            "https://developers.openai.com/ and return JSON only as "
            "{\"searched\":true,\"sources\":[{\"url\":\"https://...\","
            "\"title\":\"...\"}]}. Return the URL actually discovered by search."
        )
        try:
            search_payload = await self._ask(search_prompt, stage="PROBE_SEARCH")
        except Exception as exc:
            raise self._classified_probe_error(
                exc, operation="search", cli_name=cli_name
            ) from exc

        sources = (
            search_payload.get("sources")
            if isinstance(search_payload, dict)
            else search_payload
        )
        if not isinstance(sources, list):
            sources = []
        source_url = ""
        for source in sources:
            if isinstance(source, dict):
                source_url = str(
                    source.get("url") or source.get("canonical_url") or ""
                ).strip()
                if source_url:
                    break
        if not source_url:
            raise ResearchCapabilityProbeError(
                "WEB_SEARCH_UNAVAILABLE",
                f"{cli_name} 已连接，但本次 Headless Session 没有执行任何可验证的 Web Search。",
                verification_status=ResearchVerificationStatus.UNAVAILABLE,
                can_discover_sources=False,
                can_read_sources=False,
            )

        fetch_prompt = (
            "PROBE_FETCH. Use the web_fetch tool in a native Web Research "
            "session to read the source URL below. Do not answer from training memory. "
            "Return JSON only as {\"fetched\":true,\"url\":\"...\","
            "\"title\":\"...\",\"content\":\"...\"}; content must be a non-empty "
            f"excerpt from the page you actually read. URL: {source_url}"
        )
        try:
            fetch_payload = await self._ask(fetch_prompt, stage="PROBE_FETCH")
        except Exception as exc:
            raise self._classified_probe_error(
                exc,
                operation="fetch",
                cli_name=cli_name,
                can_discover_sources=True,
            ) from exc

        fetched_content = ""
        if isinstance(fetch_payload, dict):
            for field in (
                "content",
                "text",
                "excerpt",
                "body",
                "markdown",
                "page_content",
                "result",
                "summary",
                "description",
                "snippet",
                "raw_content",
            ):
                val = fetch_payload.get(field)
                if val and isinstance(val, str) and val.strip():
                    fetched_content = val.strip()
                    break
                elif val and isinstance(val, dict):
                    inner = str(
                        val.get("content") or val.get("text") or val.get("excerpt") or ""
                    ).strip()
                    if inner:
                        fetched_content = inner
                        break
        elif isinstance(fetch_payload, str) and fetch_payload.strip():
            fetched_content = fetch_payload.strip()

        has_fetch_indication = (
            isinstance(fetch_payload, dict)
            and (
                bool(fetch_payload.get("fetched"))
                or bool(fetch_payload.get("title"))
                or bool(fetch_payload.get("url"))
            )
        )
        # A few older adapters echo the searched source envelope instead of a
        # dedicated fetch envelope.  The independent URL validator below still
        # has to prove that the source is readable; the native fetch call above
        # is retained as a separate policy probe.
        legacy_source_echo = (
            not fetched_content
            and isinstance(fetch_payload, dict)
            and any(
                isinstance(item, dict)
                and str(item.get("url") or item.get("canonical_url") or "").strip()
                == source_url
                for item in (fetch_payload.get("sources") or [])
            )
        )
        if not fetched_content and not legacy_source_echo and not has_fetch_indication:
            raise ResearchCapabilityProbeError(
                "WEB_SEARCH_UNAVAILABLE",
                f"{cli_name} 的 web_fetch 未返回可读取的来源正文。",
                verification_status=ResearchVerificationStatus.UNAVAILABLE,
                can_discover_sources=True,
                can_read_sources=False,
            )

        try:
            validation = self.url_validator(source_url)
            if inspect.isawaitable(validation):
                validation = await validation
        except ResearchCapabilityProbeError as exc:
            raise ResearchCapabilityProbeError(
                "WEB_SEARCH_UNAVAILABLE",
                "Agent 返回了搜索来源，但未能验证其 URL；为避免使用幻觉来源，本次研究已停止。",
                verification_status=ResearchVerificationStatus.UNAVAILABLE,
                can_discover_sources=False,
                can_read_sources=False,
            ) from exc
        except Exception as exc:
            raise ResearchCapabilityProbeError(
                "WEB_SEARCH_UNAVAILABLE",
                "Agent 返回了搜索来源，但未能验证其 URL；为避免使用幻觉来源，本次研究已停止。",
                verification_status=ResearchVerificationStatus.UNAVAILABLE,
                can_discover_sources=False,
                can_read_sources=False,
            ) from exc
        if not validation:
            raise ResearchCapabilityProbeError(
                "WEB_SEARCH_UNAVAILABLE",
                "Agent 返回了搜索来源，但未能验证其 URL；为避免使用幻觉来源，本次研究已停止。",
                verification_status=ResearchVerificationStatus.UNAVAILABLE,
                can_discover_sources=False,
                can_read_sources=False,
            )

        now = datetime.now(UTC).isoformat()
        return ResearchCapability(
            mode="native_cli",
            search=True,
            fetch=True,
            can_discover_sources=True,
            can_read_sources=True,
            browser=True,
            citations=True,
            live=True,
            source=f"{cli_name}:behavioral_probe",
            verification_status=ResearchVerificationStatus.VERIFIED,
            verified_at=now,
            verification_method="behavioral_probe:search+fetch+http_validation",
        )

    @staticmethod
    def _classified_probe_error(
        exc: Exception,
        *,
        operation: str,
        cli_name: str,
        can_discover_sources: bool | None = None,
    ) -> ResearchCapabilityProbeError:
        """Classify a probe failure from the error's own surface only.

        The full ``diagnostics`` dict is deliberately NOT substring-matched:
        every turn's diagnostics contain timeout-budget keys such as
        ``idle_timeout_seconds``, and research sessions legitimately carry
        permission flags (``--dangerously-skip-permissions``) inside the
        sanitized command shape.  Matching those strings turned every CLI
        transport failure into "timeout" + "policy blocked" and buried the
        real cause (for example the provider's FAILED_PRECONDITION
        region rejection).
        """

        diagnostics = getattr(exc, "diagnostics", None)
        cli_failure_parts: list[str] = []
        if isinstance(diagnostics, dict):
            # Only *values* are read, never key names: the executor's
            # budget keys ("idle_timeout_seconds") and the sanitized
            # command shape (with its permission flags) must stay out of
            # the match, while the CLI's own failure text carries the
            # only actionable reason.
            for key in ("diagnostic", "cli_failure", "stderr_tail", "last_error"):
                value = diagnostics.get(key)
                if isinstance(value, str) and value.strip():
                    cli_failure_parts.append(value.strip())
            failure_payload = diagnostics.get("failure")
            if isinstance(failure_payload, dict):
                failure_message = failure_payload.get("message")
                if isinstance(failure_message, str) and failure_message.strip():
                    cli_failure_parts.append(failure_message.strip())
            exception_type = diagnostics.get("exception_type")
            if isinstance(exception_type, str) and exception_type.strip():
                cli_failure_parts.append(exception_type.strip())
        surface = " ".join(
            [str(exc), str(getattr(exc, "code", "") or ""), *cli_failure_parts]
        ).casefold()
        typed_code = str(getattr(exc, "code", "") or "").upper()

        cli_detail = ""
        for part in cli_failure_parts:
            if part.casefold() != str(exc).casefold():
                cli_detail = part
                break
        detail_suffix = f"（CLI 原始错误：{cli_detail[:200]}）" if cli_detail else ""

        is_geo_blocked = any(marker in surface for marker in _GEO_BLOCK_MARKERS) or (
            "failed_precondition" in surface and "location" in surface
        )
        is_executor_timeout = (
            typed_code in _EXECUTOR_TIMEOUT_CODES
            or "timed out" in surface
            or "timeout" in surface
            or "deadline exceeded" in surface
        )
        is_auth = any(marker in surface for marker in _AUTH_MARKERS)
        is_policy = any(marker in surface for marker in _POLICY_MARKERS)

        if is_geo_blocked:
            code = "WEB_RESEARCH_REGION_BLOCKED"
            message = (
                f"{cli_name} 的模型调用被服务方以地理位置拒绝"
                "（User location is not supported / FAILED_PRECONDITION）。"
                "这通常意味着当前网络出口（直连或代理节点）不被 Gemini API 接受；"
                "请更换可用的代理/VPN 出口节点后重新验证，或改用其他 Runtime。"
                f"{detail_suffix}"
            )
            verification_status = ResearchVerificationStatus.UNAVAILABLE
        elif is_executor_timeout:
            code = "WEB_RESEARCH_PROBE_TIMEOUT"
            message = (
                f"{cli_name} CLI Research Session 超时，未能验证联网能力。"
                f"{_proxy_hint()}{detail_suffix}"
            )
            verification_status = ResearchVerificationStatus.UNAVAILABLE
        elif is_auth:
            code = "WEB_RESEARCH_AUTH_REQUIRED"
            message = f"{cli_name} CLI Research 需要完成认证。{detail_suffix}"
            verification_status = ResearchVerificationStatus.BLOCKED
        elif operation == "search" and is_policy:
            code = "WEB_SEARCH_POLICY_BLOCKED"
            message = (
                f"{cli_name} 的 google_web_search 被当前 Headless Tool Policy 拒绝。"
                f"{detail_suffix}"
            )
            verification_status = ResearchVerificationStatus.BLOCKED
        elif operation == "fetch" and is_policy:
            code = "WEB_FETCH_POLICY_BLOCKED"
            message = (
                f"{cli_name} 的 web_fetch 被当前 Headless Tool Policy 拒绝。"
                f"{detail_suffix}"
            )
            verification_status = ResearchVerificationStatus.BLOCKED
        else:
            code = "WEB_SEARCH_UNAVAILABLE"
            fallback_detail = cli_detail or str(exc)
            message = (
                f"{cli_name} CLI 无法执行 "
                + ("google_web_search" if operation == "search" else "web_fetch")
                + f"。{fallback_detail[:300]}"
            )
            verification_status = ResearchVerificationStatus.UNAVAILABLE
        return ResearchCapabilityProbeError(
            code,
            message,
            verification_status=verification_status,
            can_discover_sources=(
                can_discover_sources
                if can_discover_sources is not None
                else (False if operation == "search" else None)
            ),
            can_read_sources=(False if operation == "fetch" else None),
        )


class BrokerResearchBackend:
    name = "broker"

    def __init__(self, broker: Any) -> None:
        self.broker = broker

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        result = self.broker.search(query, limit=limit)
        if inspect.isawaitable(result):
            result = await result
        return [dict(item) for item in (result or []) if isinstance(item, dict)]

    async def fetch(self, url: str) -> dict[str, Any] | str:
        result = self.broker.fetch(url)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, str | dict):
            return result
        return ""

    async def batch_search(
        self, queries: list[str], limit: int = 10
    ) -> dict[str, list[dict[str, Any]]]:
        values = await asyncio.gather(*(self.search(query, limit=limit) for query in queries))
        return dict(zip(queries, values, strict=True))

    async def batch_fetch(self, urls: list[str]) -> dict[str, dict[str, Any] | str]:
        values = await asyncio.gather(*(self.fetch(url) for url in urls))
        return dict(zip(urls, values, strict=True))


class CachingResearchBackend:
    """Transparent cache in front of any research backend.

    - ``fetch`` hits the shared :class:`ResearchSourceCache` keyed by canonical
      URL, so overlapping public figures (Parallel World initialization) never
      redownload the same page.  Only the page bytes/metadata are cached --
      what a source *means* for one persona is always analysed fresh.
    - ``search`` hits the short-TTL :class:`ResearchQueryCache` with freshness
      classification so news-style queries expire fast.
    """

    def __init__(
        self,
        inner: Any,
        *,
        source_cache: Any | None,
        query_cache: Any | None,
        fetch_semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self.inner = inner
        self.name = getattr(inner, "name", "backend")
        self.source_cache = source_cache
        self.query_cache = query_cache
        self.fetch_semaphore = fetch_semaphore

    @property
    def supports_aclose(self) -> bool:  # pragma: no cover - constant
        return True

    async def aclose(self) -> None:
        closer = getattr(self.inner, "aclose", None)
        if callable(closer):
            await closer()

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        from persona_continuum.performance.tracing import default_tracer

        default_tracer().incr_global("search_query_count", 1)

        async def _raw(q: str) -> list[dict[str, Any]]:
            found = await self.inner.search(q, limit=limit)
            return [dict(item) for item in found or [] if isinstance(item, dict)]

        if self.query_cache is None:
            return await _raw(query)
        results, _hit = await self.query_cache.search(
            backend_name=str(self.name),
            query=query,
            limit=limit,
            searcher=_raw,
        )
        return [dict(item) for item in results or [] if isinstance(item, dict)]

    async def _fetch_raw(self, url: str) -> Any:
        if self.fetch_semaphore is not None:
            async with self.fetch_semaphore:
                return await self.inner.fetch(url)
        return await self.inner.fetch(url)

    async def fetch(self, url: str) -> dict[str, Any] | str:
        from persona_continuum.performance.research_cache import canonical_url_of
        from persona_continuum.performance.tracing import default_tracer

        default_tracer().incr_global("fetch_count", 1)
        if self.source_cache is None:
            result = await self._fetch_raw(url)
            return result if isinstance(result, str | dict) else ""
        normalized_url = canonical_url_of(url)
        if not normalized_url:
            return ""
        payload, _hit = await self.source_cache.fetch(normalized_url, self._fetch_raw)
        return payload if isinstance(payload, str | dict) else ""

    async def batch_search(
        self, queries: list[str], limit: int = 10
    ) -> dict[str, list[dict[str, Any]]]:
        output: dict[str, list[dict[str, Any]]] = {}
        misses: list[str] = []
        for query in queries:
            cached = (
                self.query_cache.peek(
                    backend_name=str(self.name), query=query, limit=limit
                )
                if self.query_cache is not None
                else None
            )
            if cached is None:
                misses.append(query)
            else:
                output[query] = cached
        if misses:
            batcher = getattr(self.inner, "batch_search", None)
            if callable(batcher):
                fresh = await batcher(misses, limit=limit)
            else:
                values = await asyncio.gather(
                    *(self.inner.search(query, limit=limit) for query in misses)
                )
                fresh = dict(zip(misses, values, strict=True))
            for query in misses:
                values = [
                    dict(item)
                    for item in fresh.get(query, []) or []
                    if isinstance(item, dict)
                ]
                if self.query_cache is not None:
                    values, _ = await self.query_cache.search(
                        backend_name=str(self.name),
                        query=query,
                        limit=limit,
                        searcher=lambda _query, values=values: values,
                    )
                output[query] = values
        return output

    async def batch_fetch(self, urls: list[str]) -> dict[str, dict[str, Any] | str]:
        from persona_continuum.performance.research_cache import canonical_url_of

        output: dict[str, dict[str, Any] | str] = {}
        misses: list[str] = []
        for url in urls:
            normalized = canonical_url_of(url)
            cached = self.source_cache.peek(normalized) if self.source_cache is not None else None
            if cached is None:
                misses.append(url)
            elif isinstance(cached, str | dict):
                output[url] = cached
        if misses:
            batcher = getattr(self.inner, "batch_fetch", None)
            if callable(batcher):
                fresh = await batcher(misses)
            else:
                values = await asyncio.gather(*(self.inner.fetch(url) for url in misses))
                fresh = dict(zip(misses, values, strict=True))
            for url in misses:
                payload = fresh.get(url, "")
                if self.source_cache is not None:
                    payload, _ = await self.source_cache.fetch(
                        url, lambda _url, payload=payload: payload
                    )
                output[url] = payload if isinstance(payload, str | dict) else ""
        return output


class ResearchBackendResolver:
    """Resolve an evidence backend without rejecting unknown READY CLIs.

    Local CLI capability is verified behaviorally when metadata is absent or
    only declared.  API runtimes never receive that CLI probe; they require an
    explicit native capability or an external Research Broker/MCP route.
    """

    def __init__(
        self,
        *,
        adapter: AgentAdapter | None,
        research: dict[str, Any] | None,
        broker: Any | None = None,
        mcp: Any | None = None,
        capability_cache: ResearchCapabilityCache | None = None,
        url_validator: Any | None = None,
    ) -> None:
        self.adapter = adapter
        self.research = dict(research or {})
        self.broker = broker
        self.mcp = mcp
        self.capability_cache = capability_cache
        self.url_validator = url_validator
        self.last_capability: ResearchCapability | None = None
        self.last_error: str | None = None

    async def resolve(
        self, *, runtime: dict[str, Any], job_id: str, force_revalidate: bool = False
    ) -> ResearchBackend:
        self.last_error = None
        source = str(runtime.get("runtime_source") or "local_cli").lower()
        runtime_status = str(runtime.get("runtime_status") or "ready").lower()
        capability = ResearchCapability.model_validate(self.research or {})
        self.last_capability = capability

        if self.adapter is not None and source == "local_cli" and runtime_status == "ready":
            if force_revalidate:
                # A manual revalidation must bypass both the durable cache and
                # a BLOCKED/UNAVAILABLE discovery snapshot.  Keep the probe
                # eligible while preserving the selected runtime binding.
                capability = capability.model_copy(
                    update={
                        "mode": "agentic_cli",
                        "verification_status": ResearchVerificationStatus.UNKNOWN,
                        "verification_method": "behavioral_probe:manual_revalidate",
                        "verification_error": None,
                        "verification_error_code": None,
                        "verified_at": None,
                    }
                )
                self.last_capability = capability
                self.research = capability.model_dump(mode="json")
                cached = None
            else:
                cached = self._cached_capability(runtime)
            if cached is not None:
                capability = cached
                self.last_capability = capability
                self.research = capability.model_dump(mode="json")
                if capability.verification_status == ResearchVerificationStatus.VERIFIED:
                    return NativeCliResearchBackend(self.adapter, runtime, job_id=job_id)
                if capability.verification_status == ResearchVerificationStatus.UNAVAILABLE:
                    self.last_error = capability.verification_error or (
                        "当前 CLI 的 Web Research 能力验证不可用。"
                    )
                elif capability.verification_status == ResearchVerificationStatus.BLOCKED:
                    self.last_error = capability.verification_error or (
                        "当前 CLI 的 Web Research 被权限或运行时策略阻止。"
                    )
            elif capability.verification_status in {
                ResearchVerificationStatus.BLOCKED,
                ResearchVerificationStatus.UNAVAILABLE,
            }:
                # A discovery response may contain a model-specific cached
                # result while the user is selecting a different model.  If
                # no exact cache entry exists for this binding, do not let
                # that stale status block a READY local CLI probe.
                if capability.verification_method not in {"configuration", "explicit_policy"}:
                    capability = capability.model_copy(
                        update={
                            "mode": "agentic_cli",
                            "verification_status": ResearchVerificationStatus.UNKNOWN,
                            "verification_error": None,
                        }
                    )
                    self.last_capability = capability
                    self.research = capability.model_dump(mode="json")
            if capability.verification_status in {
                ResearchVerificationStatus.UNKNOWN,
                ResearchVerificationStatus.DECLARED,
            }:
                try:
                    agentic = AgenticCliResearchBackend(
                        self.adapter,
                        {**runtime, "agent_name": runtime.get("agent_name")},
                        job_id=job_id,
                        url_validator=self.url_validator,
                    )
                    verified = await agentic.verify_cli_research_capability()
                except ResearchCapabilityProbeError as exc:
                    unavailable = capability.model_copy(
                        update={
                            "mode": "agentic_cli",
                            "verification_status": exc.verification_status,
                            "verification_method": "behavioral_probe",
                            "verification_error": f"{exc.code}: {exc}",
                            "verification_error_code": exc.code,
                            "can_discover_sources": exc.can_discover_sources,
                            "can_read_sources": exc.can_read_sources,
                            "verified_at": None,
                        }
                    )
                    self.last_capability = unavailable
                    self.research = unavailable.model_dump(mode="json")
                    self.last_error = f"{exc.code}: {exc}"
                    self._cache_capability(runtime, unavailable, error=self.last_error)
                else:
                    self.last_capability = verified
                    self.research = verified.model_dump(mode="json")
                    self._cache_capability(runtime, verified)
                    return NativeCliResearchBackend(self.adapter, runtime, job_id=job_id)
            elif capability.verification_status == ResearchVerificationStatus.VERIFIED:
                return NativeCliResearchBackend(self.adapter, runtime, job_id=job_id)

        # API-native research is strict: explicit metadata is accepted, but an
        # unknown API provider is never behaviorally probed as if it were CLI.
        if (
            self.adapter is not None
            and source != "local_cli"
            and capability.verification_status
            in {ResearchVerificationStatus.DECLARED, ResearchVerificationStatus.VERIFIED}
            and self._supports_source_contract(capability)
            and capability.mode in {"native_cli", "api_native", "agentic_cli"}
        ):
            return NativeCliResearchBackend(self.adapter, runtime, job_id=job_id)

        broker = self.broker
        if broker is not None:
            raw = getattr(broker, "capabilities", ())
            capabilities = raw() if callable(raw) else raw
            if inspect.isawaitable(capabilities):
                capabilities = await capabilities
            names = {str(item).lower() for item in (capabilities or [])}
            if (
                {"web_search", "web_fetch"}.issubset(names)
                or {"can_discover_sources", "can_read_sources"}.issubset(names)
                or "research" in names
            ):
                return BrokerResearchBackend(broker)
        if (
            self.mcp is not None
            and capability.mode == "mcp"
            and self._supports_source_contract(capability)
        ):
            return BrokerResearchBackend(self.mcp)
        raise RuntimeError(self.last_error or "research_capability_unavailable")

    @staticmethod
    def _supports_source_contract(capability: ResearchCapability) -> bool:
        return capability.discovers_sources and capability.reads_sources

    def _cached_capability(self, runtime: dict[str, Any]) -> ResearchCapability | None:
        if self.capability_cache is None:
            return None
        entry = self.capability_cache.get(
            agent_id=str(runtime.get("agent_id") or ""),
            agent_version=runtime.get("agent_version"),
            model_id=runtime.get("model_id"),
            runtime_source=str(runtime.get("runtime_source") or "local_cli"),
            **self._cache_dimensions(runtime),
        )
        return entry.capability if entry is not None else None

    def _cache_capability(
        self,
        runtime: dict[str, Any],
        capability: ResearchCapability,
        *,
        error: str | None = None,
    ) -> None:
        if self.capability_cache is None:
            return
        self.capability_cache.put(
            agent_id=str(runtime.get("agent_id") or ""),
            agent_version=runtime.get("agent_version"),
            model_id=runtime.get("model_id"),
            runtime_source=str(runtime.get("runtime_source") or "local_cli"),
            capability=capability,
            error=error,
            **self._cache_dimensions(runtime),
        )

    @staticmethod
    def _cache_dimensions(runtime: dict[str, Any]) -> dict[str, Any]:
        """Return only policy metadata that changes a research verification."""

        return {
            "permission_profile": str(
                runtime.get("permission_profile") or PermissionProfile.RESEARCH_READ_ONLY.value
            ),
            "research_tools": runtime.get(
                "research_tools", ["google_web_search", "web_fetch"]
            ),
            "research_tool_policy": runtime.get(
                "research_tool_policy",
                {"allowed_tools": ["google_web_search", "web_fetch"]},
            ),
            "cli_flags": runtime.get("cli_flags") or {},
        }
