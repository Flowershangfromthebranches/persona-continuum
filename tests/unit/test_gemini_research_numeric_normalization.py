"""Regression contracts for Gemini Research permissions and numeric inputs.

The test cases are intentionally supplied as part of the implementation
handoff.  Review approval is required before executing the test suite or any
real Gemini/OpenCode/Web Research operation.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from persona_continuum.agent.adapter import AgentSession
from persona_continuum.agent.adapters.gemini import GeminiCliAdapter
from persona_continuum.agent.adapters.opencode import OpenCodeAdapter
from persona_continuum.agent.models import (
    AgentEvent,
    AgentEventType,
    AgentSessionConfig,
    AgentTurn,
    PermissionProfile,
    ResearchCapability,
    ResearchVerificationStatus,
)
from persona_continuum.application.material_intelligence import MaterialClassificationResult
from persona_continuum.application.persona_creation_service import PersonaCreationJob
from persona_continuum.application.research_backend import ResearchBackendResolver
from persona_continuum.compiler.schemas import ResearchArtifact
from persona_continuum.domain.persona import PersonaType
from persona_continuum.numeric import (
    InvalidNumericFieldError,
    safe_float,
    safe_int,
    safe_probability,
    safe_timeout,
)
from persona_continuum.world.models import ActorRuntimeConfig


def _research_config() -> AgentSessionConfig:
    return AgentSessionConfig(
        room_id="research-room",
        participant_id="research-probe",
        persona_id="research-persona",
        model_id="gemini-test",
        permission_profile=PermissionProfile.RESEARCH_READ_ONLY,
        allow_mcp=False,
        tools=[{"name": "google_web_search"}, {"name": "web_fetch"}],
    )


def test_gemini_research_session_allows_google_web_search() -> None:
    adapter = GeminiCliAdapter()
    adapter._resolved_binary = "/usr/local/bin/gemini"
    args = adapter.build_permission_args(_research_config())
    assert args == ["--allowed-tools", "google_web_search,web_fetch"]
    assert "google_web_search" in args[1]


def test_gemini_research_session_allows_web_fetch() -> None:
    adapter = GeminiCliAdapter()
    adapter._resolved_binary = "/usr/local/bin/gemini"
    args = adapter.build_permission_args(_research_config())
    assert "web_fetch" in args[1]


def test_gemini_research_does_not_use_yolo() -> None:
    adapter = GeminiCliAdapter()
    adapter._resolved_binary = "/opt/local/bin/agy"
    args = adapter.build_permission_args(_research_config())
    assert args == ["--dangerously-skip-permissions"]
    rendered = " ".join(args).casefold()
    assert "--yolo" not in rendered
    assert "shell" not in rendered
    assert "write" not in rendered
    assert "replace" not in rendered


def test_gemini_chat_safe_prompt_forbids_headless_tool_calls() -> None:
    adapter = GeminiCliAdapter()
    config = AgentSessionConfig(
        room_id="persona-room",
        participant_id="persona-job",
        persona_id="persona",
        permission_profile=PermissionProfile.CHAT_SAFE,
        allow_mcp=False,
        tools=[],
    )
    turn = AgentTurn(user_message='{"material": "inline"}')

    prompt = adapter.prepare_prompt(config, turn, "rendered prompt")

    assert "Do not call GrepSearch, read_file" in prompt
    assert prompt.endswith("rendered prompt")


def test_gemini_research_prompt_does_not_disable_declared_tools() -> None:
    adapter = GeminiCliAdapter()
    prompt = adapter.prepare_prompt(
        _research_config(),
        AgentTurn(user_message="research", tools=[{"name": "web_fetch"}]),
        "rendered research prompt",
    )

    assert prompt == "rendered research prompt"


class _ProbeAdapter:
    adapter_id = "gemini_cli"
    name = "Gemini CLI"

    def __init__(self, *, blocked: bool = False) -> None:
        self.blocked = blocked
        self.prompts: list[str] = []
        self.configs: list[AgentSessionConfig] = []

    async def create_session(self, config: AgentSessionConfig) -> AgentSession:
        self.configs.append(config)
        return AgentSession(config=config, session_data={})

    def send(self, session: AgentSession, turn: AgentTurn) -> AsyncIterator[AgentEvent]:
        del session

        async def stream() -> AsyncIterator[AgentEvent]:
            self.prompts.append(turn.user_message)
            if self.blocked:
                raise RuntimeError("permission denied by headless policy")
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


def _resolver(app, adapter: _ProbeAdapter) -> ResearchBackendResolver:
    return ResearchBackendResolver(
        adapter=adapter,
        research=ResearchCapability(
            mode="agentic_cli",
            verification_status=ResearchVerificationStatus.UNKNOWN,
            source="gemini_cli:unverified",
        ).model_dump(mode="json"),
        capability_cache=app.research_capability_cache,
        url_validator=lambda url: {"url": url, "status_code": 200, "content_length": 42},
    )


def _runtime() -> dict[str, Any]:
    return {
        "runtime_source": "local_cli",
        "runtime_status": "ready",
        "agent_id": "gemini_cli",
        "agent_version": "test-version",
        "agent_name": "Gemini CLI",
        "model_id": "gemini-test",
        "reasoning_effort": "medium",
    }


@pytest.mark.anyio
async def test_gemini_search_and_fetch_probed_separately(app) -> None:
    adapter = _ProbeAdapter()
    resolver = _resolver(app, adapter)
    backend = await resolver.resolve(runtime=_runtime(), job_id="probe-separated")

    assert backend.name == "native_cli"
    assert len(adapter.prompts) == 2
    assert "PROBE_SEARCH" in adapter.prompts[0]
    assert "PROBE_FETCH" in adapter.prompts[1]
    assert "google_web_search" in adapter.prompts[0]
    assert "web_fetch" in adapter.prompts[1]
    assert all(
        config.permission_profile == PermissionProfile.RESEARCH_READ_ONLY
        for config in adapter.configs
    )
    assert all(
        {str(tool.get("name")) for tool in config.tools}
        == {"google_web_search", "web_fetch"}
        for config in adapter.configs
    )
    assert resolver.last_capability is not None
    assert resolver.last_capability.verification_status == ResearchVerificationStatus.VERIFIED
    assert resolver.last_capability.can_discover_sources is True
    assert resolver.last_capability.can_read_sources is True


@pytest.mark.anyio
async def test_gemini_headless_policy_blocked_is_not_unavailable(app) -> None:
    resolver = _resolver(app, _ProbeAdapter(blocked=True))

    with pytest.raises(RuntimeError, match="WEB_SEARCH_POLICY_BLOCKED"):
        await resolver.resolve(runtime=_runtime(), job_id="probe-blocked")

    assert resolver.last_capability is not None
    assert resolver.last_capability.verification_status == ResearchVerificationStatus.BLOCKED
    assert resolver.last_capability.verification_error_code == "WEB_SEARCH_POLICY_BLOCKED"


@pytest.mark.anyio
async def test_gemini_blocked_cache_can_be_revalidated(app) -> None:
    blocked = _resolver(app, _ProbeAdapter(blocked=True))
    with pytest.raises(RuntimeError):
        await blocked.resolve(runtime=_runtime(), job_id="probe-blocked-cache")

    good_adapter = _ProbeAdapter()
    resolver = _resolver(app, good_adapter)
    await resolver.resolve(
        runtime=_runtime(), job_id="probe-manual-revalidate", force_revalidate=True
    )

    assert good_adapter.prompts
    assert resolver.last_capability is not None
    assert resolver.last_capability.verification_status == ResearchVerificationStatus.VERIFIED


def test_safe_float_none_uses_default() -> None:
    assert safe_float(None, default=0.5) == 0.5


def test_timeout_none_uses_default() -> None:
    assert safe_timeout(None) == 120.0


@pytest.mark.anyio
async def test_runtime_null_timeout_not_written_to_job_config(app) -> None:
    job = await app.persona_creation.create_job(
        display_name="Null Timeout Persona",
        persona_type=PersonaType.PUBLIC_LIVING_PERSON,
        creation_mode="private_materials",
        runtime_source="test",
        agent_id="fake_agent",
        model_id="fake-gpt-5",
        materials=[],
        enrichment_input_mode="local_materials",
        job_config={"turn_timeout_seconds": None, "acp_stream_limit_bytes": None},
        start_worker=False,
    )
    assert "turn_timeout_seconds" not in job.job_config
    assert "acp_stream_limit_bytes" not in job.job_config


def test_material_dimension_score_null_is_skipped() -> None:
    result = MaterialClassificationResult.model_validate(
        {
            "id": "unit-1",
            "content": "A material excerpt.",
            "source_id": "source-1",
            "dimension_scores": {"identity_and_timeline": None},
        }
    )
    assert result.dimension_scores == {}
    assert "dimension_scores.identity_and_timeline" in result.metadata[
        "invalid_optional_numeric_skipped"
    ]


def test_material_confidence_null_uses_default() -> None:
    result = MaterialClassificationResult.model_validate(
        {
            "id": "unit-2",
            "content": "A material excerpt.",
            "source_id": "source-2",
            "confidence": None,
        }
    )
    assert result.confidence == 0.5
    assert "confidence" in result.metadata["invalid_optional_numeric_skipped"]


def test_open_code_null_optional_numbers_do_not_fail() -> None:
    runtime = ActorRuntimeConfig(
        agent_id=OpenCodeAdapter().adapter_id,
        turn_timeout_seconds=None,
        acp_stream_limit_bytes=None,
    )
    job = PersonaCreationJob(
        id="null-runtime-job",
        display_name="Null Runtime Persona",
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        creation_mode="fictional",
        runtime_source="local_cli",
        agent_id="opencode",
        model_id="opencode-default",
        source_count=None,
        progress={"percent": None, "completed": None},
    )
    assert runtime.turn_timeout_seconds is None
    assert runtime.acp_stream_limit_bytes == 16 * 1024 * 1024
    assert job.source_count == 0
    assert job.progress.percent == 0


def test_numeric_string_is_normalized() -> None:
    result = MaterialClassificationResult.model_validate(
        {
            "id": "unit-3",
            "content": "A material excerpt.",
            "source_id": "source-3",
            "confidence": "0.7",
        }
    )
    assert result.confidence == 0.7


def test_nan_probability_is_rejected_or_defaulted() -> None:
    assert safe_probability(float("nan"), default=0.5) == 0.5


def test_legacy_artifact_null_numeric_does_not_crash_compiler() -> None:
    artifact = ResearchArtifact.model_validate(
        {
            "artifact_id": "artifact-legacy",
            "schema_version": "1.1",
            "dimension": "identity_and_timeline",
            "source_ids": ["source-legacy"],
            "claims": [
                {
                    "content": "A retained claim.",
                    "source_id": "source-legacy",
                    "claim_type": "historical_inference",
                    "confidence": None,
                    "reliability": None,
                    "inference_strength": None,
                }
            ],
            "extracted_components": {"identity_profile": {"name": "Legacy"}},
            "uncertainty": {"level": None},
            "created_by": "legacy_runtime",
            "artifact_hash": "hash-legacy",
        }
    )
    assert artifact.claims[0].confidence == 0.5
    assert artifact.claims[0].reliability == 0.5
    assert artifact.uncertainty["level"] == 0.5


def test_invalid_required_numeric_has_typed_failure() -> None:
    with pytest.raises(InvalidNumericFieldError) as caught:
        safe_int(
            "not-a-number",
            default=None,
            required=True,
            field="required_count",
            phase="researching",
        )
    failure = caught.value.as_failure()
    assert failure["code"] == "INVALID_NUMERIC_FIELD"
    assert failure["phase"] == "researching"
    assert failure["field"] == "required_count"
    assert failure["received_type"] == "str"
