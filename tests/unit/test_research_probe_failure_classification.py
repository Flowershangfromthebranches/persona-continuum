"""Probe failure classification must report the reason the CLI actually gave.

Regression context: ``_classified_probe_error`` used to substring-match the
whole ``diagnostics`` blob.  Every turn's diagnostics carry timeout-budget
keys (``idle_timeout_seconds``) and the sanitized command shape (whose
``--dangerously-skip-permissions`` flag is an expected policy argument), so a
provider region rejection (``FAILED_PRECONDITION: User location is not
supported``) was reported to the operator as "session timeout" plus "blocked
by policy" -- hiding the only actionable fact.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from persona_continuum.agent.adapter import AgentSession
from persona_continuum.agent.models import (
    AgentEvent,
    AgentEventType,
    AgentSessionConfig,
    AgentTurn,
    ResearchCapability,
    ResearchVerificationStatus,
)
from persona_continuum.application.research_backend import (
    AgenticCliResearchBackend,
    ResearchBackendResolver,
)

LOCATION_ERROR = (
    "Gemini API rejected the request: user location is not supported "
    "(FAILED_PRECONDITION). This can be transient."
)

# The diagnostics every real probe turn carries: timeout budget keys and the
# sanitized command shape with the CLI's permission flag.
_REAL_TURN_DIAGNOSTICS: dict[str, Any] = {
    "idle_timeout_seconds": 180.0,
    "hard_timeout_seconds": 1020.0,
    "first_response_timeout_seconds": 1020.0,
    "command_shape": "agy --model gemini-x -p [prompt] --dangerously-skip-permissions",
    "output_streaming_mode": "buffered_final",
}


def _error(message: str, *, code: str | None = None, diagnostics: dict[str, Any] | None = None):
    exc = RuntimeError(message)
    if code is not None:
        exc.code = code  # type: ignore[attr-defined]
    if diagnostics is not None:
        exc.diagnostics = diagnostics  # type: ignore[attr-defined]
    return exc


def _classify(exc: Exception, *, operation: str = "search", cli_name: str = "gemini_cli"):
    return AgenticCliResearchBackend._classified_probe_error(
        exc, operation=operation, cli_name=cli_name
    )


def test_region_rejection_is_not_misreported_as_timeout() -> None:
    exc = _error(
        LOCATION_ERROR,
        code="AGENT_PROCESS_EXITED_WITHOUT_OUTPUT",
        diagnostics={
            **_REAL_TURN_DIAGNOSTICS,
            "cli_failure": LOCATION_ERROR,
            "stderr_tail": "Error: Agent execution terminated due to error.",
        },
    )

    classified = _classify(exc)

    assert classified.code == "WEB_RESEARCH_REGION_BLOCKED"
    assert classified.verification_status == ResearchVerificationStatus.UNAVAILABLE
    assert "User location is not supported" in classified.message


def test_region_rejection_message_is_actionable() -> None:
    classified = _classify(_error(LOCATION_ERROR))

    assert "代理" in classified.message or "Runtime" in classified.message


def test_timeout_budget_keys_in_diagnostics_do_not_force_timeout() -> None:
    exc = _error(
        "connect: connection refused",
        code="AGENT_TRANSPORT_ERROR",
        diagnostics=dict(_REAL_TURN_DIAGNOSTICS),
    )

    classified = _classify(exc)

    assert classified.code != "WEB_RESEARCH_PROBE_TIMEOUT"
    assert classified.code == "WEB_SEARCH_UNAVAILABLE"
    assert "connect: connection refused" in classified.message


def test_skip_permissions_flag_in_diagnostics_is_not_a_policy_denial() -> None:
    exc = _error("CLI exited with status 1", diagnostics=dict(_REAL_TURN_DIAGNOSTICS))

    classified = _classify(exc)

    assert classified.code not in {"WEB_SEARCH_POLICY_BLOCKED", "WEB_FETCH_POLICY_BLOCKED"}
    assert classified.verification_status == ResearchVerificationStatus.UNAVAILABLE


def test_executor_timeout_typed_code_still_classified_as_timeout() -> None:
    for code in ("AGENT_IDLE_TIMEOUT", "AGENT_HARD_TIMEOUT", "AGENT_TURN_TIMEOUT"):
        classified = _classify(
            _error("Agent turn exceeded its hard timeout", code=code, diagnostics={})
        )

        assert classified.code == "WEB_RESEARCH_PROBE_TIMEOUT", code


def test_timeout_without_proxy_env_explains_the_missing_proxy(monkeypatch) -> None:
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(name, raising=False)

    classified = _classify(
        _error("Agent turn exceeded its hard timeout", code="AGENT_HARD_TIMEOUT")
    )

    assert classified.code == "WEB_RESEARCH_PROBE_TIMEOUT"
    assert "HTTP_PROXY" in classified.message


def test_headless_policy_denial_is_still_blocked() -> None:
    classified = _classify(_error("permission denied by headless policy"))

    assert classified.code == "WEB_SEARCH_POLICY_BLOCKED"
    assert classified.verification_status == ResearchVerificationStatus.BLOCKED


def test_fetch_operation_policy_denial_uses_fetch_code() -> None:
    classified = _classify(_error("web_fetch denied by sandbox"), operation="fetch")

    assert classified.code == "WEB_FETCH_POLICY_BLOCKED"
    assert classified.can_read_sources is False


def test_unavailable_failure_surfaces_cli_detail() -> None:
    classified = _classify(
        _error(
            "agent process exited",
            diagnostics={"cli_failure": "calling model: upstream unreachable"},
        )
    )

    assert "upstream unreachable" in classified.message


class _FailingCli:
    adapter_id = "stub_cli"
    name = "Stub CLI"

    async def create_session(self, config: AgentSessionConfig) -> AgentSession:
        return AgentSession(config=config, session_data={})

    def send(self, session: AgentSession, turn: AgentTurn) -> AsyncIterator[AgentEvent]:
        async def stream() -> AsyncIterator[AgentEvent]:
            raise RuntimeError(LOCATION_ERROR)
            yield  # pragma: no cover - keeps this an async generator

        return stream()

    async def close(self, session: AgentSession) -> None:
        session.mark_closed()


class _WorkingCli:
    adapter_id = "stub_cli"
    name = "Stub CLI"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def create_session(self, config: AgentSessionConfig) -> AgentSession:
        return AgentSession(config=config, session_data={})

    def send(self, session: AgentSession, turn: AgentTurn) -> AsyncIterator[AgentEvent]:
        self.prompts.append(turn.user_message)

        async def stream() -> AsyncIterator[AgentEvent]:
            if "PROBE_SEARCH" in turn.user_message:
                payload: dict[str, Any] = {
                    "searched": True,
                    "sources": [{"url": "https://openai.com/", "title": "OpenAI"}],
                }
            else:
                payload = {
                    "fetched": True,
                    "url": "https://openai.com/",
                    "title": "OpenAI",
                    "content": "A separately fetched source excerpt.",
                }
            yield AgentEvent(type=AgentEventType.DONE, content=json.dumps(payload))

        return stream()

    async def close(self, session: AgentSession) -> None:
        session.mark_closed()


def _runtime() -> dict[str, Any]:
    return {
        "runtime_source": "local_cli",
        "runtime_status": "ready",
        "agent_id": "stub_cli",
        "agent_version": "1.0.0",
        "agent_name": "Stub CLI",
        "model_id": "model-a",
        "reasoning_effort": "medium",
    }


def _resolver(app: Any, cli: Any) -> ResearchBackendResolver:
    return ResearchBackendResolver(
        adapter=cli,
        research=ResearchCapability(
            mode="agentic_cli",
            verification_status=ResearchVerificationStatus.UNKNOWN,
            source="stub:unverified",
        ).model_dump(mode="json"),
        capability_cache=app.research_capability_cache,
        url_validator=lambda url: {"url": url, "status_code": 200, "content_length": 42},
    )


@pytest.mark.anyio
async def test_resolver_reports_region_block_instead_of_timeout(app) -> None:
    resolver = _resolver(app, _FailingCli())

    with pytest.raises(RuntimeError, match="WEB_RESEARCH_REGION_BLOCKED"):
        await resolver.resolve(runtime=_runtime(), job_id="region-block")

    assert resolver.last_capability is not None
    assert resolver.last_capability.verification_error_code == "WEB_RESEARCH_REGION_BLOCKED"
    assert resolver.last_capability.verification_status == ResearchVerificationStatus.UNAVAILABLE


@pytest.mark.anyio
async def test_region_block_does_not_block_a_later_successful_probe(app) -> None:
    first = _resolver(app, _FailingCli())
    with pytest.raises(RuntimeError, match="WEB_RESEARCH_REGION_BLOCKED"):
        await first.resolve(runtime=_runtime(), job_id="region-block-cached")

    # The cached UNAVAILABLE result makes the next non-forced resolve fail fast.
    cached_resolver = _resolver(app, _WorkingCli())
    with pytest.raises(RuntimeError, match="WEB_RESEARCH_REGION_BLOCKED"):
        await cached_resolver.resolve(runtime=_runtime(), job_id="region-block-reuse")

    revalidated = _resolver(app, _WorkingCli())
    await revalidated.resolve(
        runtime=_runtime(), job_id="after-region-block", force_revalidate=True
    )

    assert revalidated.last_capability is not None
    assert revalidated.last_capability.verification_status == ResearchVerificationStatus.VERIFIED
