from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from persona_continuum.agent.adapter import AgentSession
from persona_continuum.agent.adapters.codex import CodexAdapter
from persona_continuum.agent.adapters.other_vendors import QoderAdapter, WorkBuddyAdapter
from persona_continuum.agent.models import (
    AgentEvent,
    AgentEventType,
    AgentSessionConfig,
    AgentTurn,
    ResearchCapability,
)
from persona_continuum.agent.protocols.plain_cli import PlainCliAdapter
from persona_continuum.application.research_backend import (
    ResearchBackendResolver,
    ResearchCapabilityProbeError,
)
from persona_continuum.domain.persona import PersonaType


class StubResearchCli:
    adapter_id = "stub_cli"
    name = "Stub CLI"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.prompts: list[str] = []

    async def create_session(self, config: AgentSessionConfig) -> AgentSession:
        return AgentSession(config=config, session_data={})

    def send(self, session: AgentSession, turn: AgentTurn) -> AsyncIterator[AgentEvent]:
        async def stream() -> AsyncIterator[AgentEvent]:
            self.prompts.append(turn.user_message)
            yield AgentEvent(
                type=AgentEventType.DONE,
                content=json.dumps(self.payload, ensure_ascii=False),
            )

        return stream()

    async def close(self, session: AgentSession) -> None:
        session.mark_closed()


async def _resolve_unknown_cli(app, cli: StubResearchCli):
    resolver = ResearchBackendResolver(
        adapter=cli,
        research=ResearchCapability(
            mode="agentic_cli",
            verification_status="unknown",
            source="test:unreported",
        ).model_dump(mode="json"),
        capability_cache=app.research_capability_cache,
        url_validator=lambda url: {"status_code": 200, "content_length": 42},
    )
    backend = await resolver.resolve(
        runtime={
            "runtime_source": "local_cli",
            "runtime_status": "ready",
            "agent_id": cli.adapter_id,
            "agent_version": "1.0.0",
            "model_id": "model-a",
            "reasoning_effort": "medium",
        },
        job_id="probe-test",
    )
    return resolver, backend


@pytest.mark.anyio
async def test_unknown_ready_cli_not_rejected_before_probe(app) -> None:
    cli = StubResearchCli(
        {"searched": True, "sources": [{"url": "https://openai.com/", "title": "OpenAI"}]}
    )
    resolver, backend = await _resolve_unknown_cli(app, cli)
    assert backend.name == "native_cli"
    assert resolver.last_capability is not None
    assert resolver.last_capability.verification_status == "verified"


@pytest.mark.anyio
async def test_unknown_cli_runs_behavioral_research_probe(app) -> None:
    cli = StubResearchCli(
        {"searched": True, "sources": [{"url": "https://openai.com/", "title": "OpenAI"}]}
    )
    await _resolve_unknown_cli(app, cli)
    assert cli.prompts
    assert "native Web Research" in cli.prompts[0]
    assert "openai.com" in cli.prompts[0]


@pytest.mark.anyio
async def test_successful_cli_probe_marks_verified(app) -> None:
    cli = StubResearchCli(
        {"searched": True, "sources": [{"url": "https://openai.com/", "title": "OpenAI"}]}
    )
    await _resolve_unknown_cli(app, cli)
    entry = app.research_capability_cache.get(
        agent_id="stub_cli",
        agent_version="1.0.0",
        model_id="model-a",
        runtime_source="local_cli",
    )
    assert entry is not None
    assert entry.capability.verification_status == "verified"


@pytest.mark.anyio
async def test_cli_probe_requires_verifiable_url(app) -> None:
    cli = StubResearchCli(
        {"searched": True, "sources": [{"url": "https://example.invalid/", "title": "fake"}]}
    )
    resolver = ResearchBackendResolver(
        adapter=cli,
        research=ResearchCapability(mode="agentic_cli").model_dump(mode="json"),
        capability_cache=app.research_capability_cache,
        url_validator=lambda url: (_ for _ in ()).throw(
            ResearchCapabilityProbeError("invalid", "unreachable")
        ),
    )
    with pytest.raises(RuntimeError, match="未能验证其 URL"):
        await resolver.resolve(
            runtime={
                "runtime_source": "local_cli",
                "runtime_status": "ready",
                "agent_id": "stub_cli",
                "agent_version": "1.0.0",
                "model_id": "model-a",
            },
            job_id="invalid-url",
        )


@pytest.mark.anyio
async def test_cli_claim_without_real_url_fails(app) -> None:
    cli = StubResearchCli({"searched": True, "sources": [{"title": "memory only"}]})
    resolver = ResearchBackendResolver(
        adapter=cli,
        research=ResearchCapability(mode="agentic_cli").model_dump(mode="json"),
        capability_cache=app.research_capability_cache,
        url_validator=lambda url: True,
    )
    with pytest.raises(RuntimeError, match="没有执行任何可验证的 Web Search"):
        await resolver.resolve(
            runtime={
                "runtime_source": "local_cli",
                "runtime_status": "ready",
                "agent_id": "stub_cli",
                "agent_version": "1.0.0",
                "model_id": "model-a",
            },
            job_id="missing-url",
        )


def test_cli_version_change_invalidates_research_cache(app) -> None:
    verified = ResearchCapability(
        mode="native_cli",
        search=True,
        fetch=True,
        verification_status="verified",
        verification_method="behavioral_probe+http_validation",
    )
    app.research_capability_cache.put(
        agent_id="codex",
        agent_version="1.0.0",
        model_id="gpt-x",
        runtime_source="local_cli",
        capability=verified,
    )
    assert app.research_capability_cache.get(
        agent_id="codex",
        agent_version="2.0.0",
        model_id="gpt-x",
        runtime_source="local_cli",
    ) is None


def test_codex_without_help_token_is_unknown_not_unavailable() -> None:
    capability = CodexAdapter._probe_research_capability("1.0.0", "Codex CLI help")
    assert capability.verification_status == "unknown"
    assert capability.mode == "agentic_cli"


def test_plain_cli_defaults_to_agentic_unknown() -> None:
    adapter = PlainCliAdapter(
        adapter_id="unreported_cli",
        name="Unreported CLI",
        binary_candidates=["unreported-cli"],
    )
    assert adapter._research_capability.mode == "agentic_cli"
    assert adapter._research_capability.verification_status == "unknown"


@pytest.mark.anyio
async def test_qoder_ready_cli_can_attempt_research_probe(app) -> None:
    cli = StubResearchCli(
        {"searched": True, "sources": [{"url": "https://openai.com/", "title": "OpenAI"}]}
    )
    cli.adapter_id = QoderAdapter().adapter_id
    await _resolve_unknown_cli(app, cli)
    assert cli.prompts


@pytest.mark.anyio
async def test_workbuddy_ready_cli_can_attempt_research_probe(app) -> None:
    cli = StubResearchCli(
        {"searched": True, "sources": [{"url": "https://openai.com/", "title": "OpenAI"}]}
    )
    cli.adapter_id = WorkBuddyAdapter().adapter_id
    await _resolve_unknown_cli(app, cli)
    assert cli.prompts


@pytest.mark.anyio
async def test_unknown_api_does_not_get_cli_probe(app) -> None:
    cli = StubResearchCli(
        {"searched": True, "sources": [{"url": "https://openai.com/", "title": "OpenAI"}]}
    )
    resolver = ResearchBackendResolver(
        adapter=cli,
        research=ResearchCapability(mode="none", verification_status="unknown").model_dump(
            mode="json"
        ),
        capability_cache=app.research_capability_cache,
        url_validator=lambda url: {"status_code": 200},
    )
    with pytest.raises(RuntimeError, match="research_capability_unavailable"):
        await resolver.resolve(
            runtime={
                "runtime_source": "api",
                "runtime_status": "ready",
                "agent_id": "api_unknown",
                "agent_version": "1.0.0",
                "model_id": "model-a",
            },
            job_id="api-no-cli-probe",
        )
    assert cli.prompts == []


@pytest.mark.anyio
async def test_local_materials_never_checks_web_research(app, monkeypatch) -> None:
    async def forbidden(**kwargs: Any) -> Any:
        raise AssertionError("local_materials must not resolve web research")

    monkeypatch.setattr(app.persona_creation, "_resolve_or_verify_research_runtime", forbidden)
    job = await app.persona_creation.create_job(
        display_name="Public Local Materials Subject",
        persona_type=PersonaType.PUBLIC_LIVING_PERSON,
        creation_mode="public_research",
        runtime_source="test",
        agent_id="fake_agent",
        model_id="fake-gpt-5",
        materials=[{"id": "note-1", "content": "A locally provided note."}],
        enrichment_input_mode="local_materials",
        start_worker=False,
    )
    assert job.job_config["enrichment_input_mode"] == "local_materials"


def test_public_person_local_materials_bypass_is_explicit() -> None:
    from persona_continuum.application.persona_creation_service import PersonaCreationOrchestrator

    assert not PersonaCreationOrchestrator._requires_web_research(
        "public_research", "local_materials"
    )
    assert PersonaCreationOrchestrator._requires_web_research("public_research", "web_research")


def test_public_person_local_materials_never_checks_web_research() -> None:
    from persona_continuum.application.persona_creation_service import PersonaCreationOrchestrator

    assert not PersonaCreationOrchestrator._requires_web_research(
        "public_research", "local_materials"
    )


def test_frontend_unknown_research_does_not_show_fail_closed() -> None:
    source = Path(__file__).resolve().parents[2] / "src/persona_continuum/web/static/app.js"
    text = source.read_text(encoding="utf-8")
    assert "联网研究能力：尚未验证" in text
    assert "未报告 Web Research 研究能力；公共联网研究将 fail-closed。" not in text
