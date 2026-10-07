from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from persona_continuum.agent.adapter import AgentSession
from persona_continuum.agent.adapters import command_code, gemini, opencode, other_vendors
from persona_continuum.agent.adapters.gemini import GeminiCliAdapter, GoogleGeminiCliAdapter
from persona_continuum.agent.discovery import AgentDiscoveryService
from persona_continuum.agent.models import (
    AgentEventType,
    AgentProbeResult,
    AgentSessionConfig,
    AgentStatus,
    AgentTurn,
    ResearchCapability,
)
from persona_continuum.agent.registry import AgentRegistry
from persona_continuum.agent.response_collector import ReasoningBindingUnverifiedError
from persona_continuum.agent.subprocess_transport import SubprocessAgentTransport
from persona_continuum.application.research_backend import (
    AgenticCliResearchBackend,
    ResearchCapabilityProbeError,
)


def config(**kwargs):
    return AgentSessionConfig(room_id="diagnosis", participant_id="p", persona_id="p", **kwargs)


@pytest.mark.anyio
async def test_google_catalog_and_effort_are_independent_from_agy(tmp_path, monkeypatch) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (tmp_path / "package.json").write_text('{"name":"@google/gemini-cli"}')
    binary = bundle / "gemini.js"
    binary.write_text("// fixture executable")
    (bundle / "chunk-models.js").write_text(
        'var LATEST_GEMINI_FLASH_MODEL = "gemini-3.8-flash";'
        'var DEFAULT_GEMINI_MODEL = "gemini-2.5-pro";'
        '// GEMINI_CLI_HOME customAliases thinkingLevel'
    )
    cli = GoogleGeminiCliAdapter()
    cli._resolved_binary = str(binary)
    never_exec = AsyncMock(side_effect=AssertionError("catalog must not invoke a model prompt"))
    monkeypatch.setattr(gemini, "safe_exec_cmd", never_exec)
    models = await cli.list_models()
    assert {m.id for m in models} == {"gemini-3.8-flash", "gemini-2.5-pro"}
    assert models[0].supported_reasoning_efforts == ["low", "medium", "high"]
    assert models[0].reasoning_capability.verified
    assert models[1].supported_reasoning_efforts == []
    assert cli.resolve_cli_model_and_reasoning(config(model_id="gemini-3.8-flash")) == (
        "gemini-3.8-flash", None
    )
    assert cli.resolve_cli_model_and_reasoning(config(
        model_id="gemini-3.8-flash", reasoning_effort="medium"
    )) == ("gemini-3.8-flash", "medium")
    with pytest.raises(ReasoningBindingUnverifiedError):
        cli.resolve_cli_model_and_reasoning(config(
            model_id="gemini-3.8-flash", reasoning_effort="minimal"
        ))
    with pytest.raises(ReasoningBindingUnverifiedError):
        cli.resolve_cli_model_and_reasoning(config(reasoning_effort="high"))
    original_dir = tmp_path / ".gemini"
    original_dir.mkdir()
    original = original_dir / "settings.json"
    original.write_text('{// retain policy\n"security":{"disableYoloMode":true}}')
    auth = original_dir / "oauth_creds.json"
    auth.write_text('{"fixture":"credential stays local"}')
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path))
    session = await cli.create_session(config(
        model_id="gemini-3.8-flash", reasoning_effort="medium"
    ))
    env = cli.build_cli_environment(session)
    overlay = Path(env["GEMINI_CLI_HOME"]) / ".gemini" / "settings.json"
    try:
        settings = json.loads(overlay.read_text())
        assert settings["security"]["disableYoloMode"] is True
        assert settings["modelConfigs"]["customOverrides"][0] == {
            "match": {"model": session.session_data["google_model_alias"]},
            "modelConfig": {"generateContentConfig": {
                "thinkingConfig": {"thinkingLevel": "MEDIUM"}
            }},
        }
        assert overlay.stat().st_mode & 0o777 == 0o600
        assert (overlay.parent / "oauth_creds.json").is_symlink()
        assert original.read_text().startswith('{// retain policy')
        binding = await cli.bind_runtime(session)
        assert binding.reasoning_verified and binding.effective_reasoning == "medium"
    finally:
        await cli.close(session)
    assert not overlay.exists()
    assert auth.exists()
    assert "gemini" not in GeminiCliAdapter().binary_candidates
    never_exec.assert_not_called()


@pytest.mark.anyio
async def test_opencode_preserves_discovered_catalog_without_guessing_effort(monkeypatch) -> None:
    cli = opencode.OpenCodeAdapter()
    cli._resolved_binary = "/fixture/opencode"
    monkeypatch.setattr(opencode, "safe_exec_cmd", AsyncMock(return_value=(
        0, "google/gemini-3.8-flash\nopenai/gpt-6-sol\n", ""
    )))
    models = await cli.list_models()
    assert [m.id for m in models] == ["google/gemini-3.8-flash", "openai/gpt-6-sol"]
    assert all(m.selectable and not m.supported_reasoning_efforts for m in models)
    assert cli.require_verified_binding is True


@pytest.mark.anyio
async def test_command_catalog_budget_cache_and_no_silent_fallback(monkeypatch) -> None:
    cli = command_code.CommandCodeAdapter()
    cli._resolved_binary = "/fixture/command-code"
    execute = AsyncMock(return_value=(0, "Google\n  google/gemini-3.8-flash Current model\n", ""))
    monkeypatch.setattr(command_code, "safe_exec_cmd", execute)
    assert (await cli.list_models())[0].id == "google/gemini-3.8-flash"
    await cli.list_models()
    assert execute.await_count == 1
    assert execute.call_args.kwargs["env"]["DO_NOT_TRACK"] == "1"
    assert cli.probe_timeout_seconds > cli.model_discovery_timeout_seconds + 11
    cli.invalidate_model_cache()
    execute.return_value = (-1, "", "Command timed out")
    assert await cli.list_models() == []
    assert "timed out" in cli._model_discovery_error


@pytest.mark.anyio
async def test_agy_reuses_a_recent_real_catalog_during_revalidation(monkeypatch) -> None:
    cli = GeminiCliAdapter()
    cli._resolved_binary = "/fixture/agy"
    execute = AsyncMock(return_value=(
        0, "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n", ""
    ))
    monkeypatch.setattr(gemini, "safe_exec_cmd", execute)
    assert (await cli.list_models())[0].id == "gemini-3.8-flash-high"
    execute.return_value = (-1, "", "Transient catalog timeout")
    assert (await cli.list_models())[0].id == "gemini-3.8-flash-high"
    assert execute.await_count == 1
    cli.invalidate_model_cache()
    assert (await cli.list_models())[0].id == "gemini-3.8-flash-high"
    assert "timeout" in cli._model_discovery_error
    assert execute.await_count == 2
    cli._resolved_binary = "/fixture/replacement-agy"
    assert await cli.list_models() == []


def test_agy_only_exposes_efforts_of_actual_catalog_siblings() -> None:
    models = GeminiCliAdapter._parse_models(
        "gemini-3.8-flash-high\tFlash High\n"
        "gemini-3.8-flash-medium\tFlash Medium\n"
        "gemini-3.8-flash-low\tFlash Low\n"
        "gemini-3.1-pro-high\tPro High\n"
        "gemini-3.1-pro-low\tPro Low\n"
    )
    assert models[0].supported_reasoning_efforts == ["high", "medium", "low"]
    assert models[3].supported_reasoning_efforts == ["high", "low"]
    assert "medium" not in models[3].reasoning_capability.supported_efforts


@pytest.mark.anyio
async def test_discovery_retries_empty_agy_catalog_before_completing_scan() -> None:
    cli = GeminiCliAdapter()
    empty = AgentProbeResult(
        id=cli.adapter_id, name=cli.name, status=AgentStatus.READY,
        version="fixture", models=[], model_discovery_error="catalog timeout",
    )
    success = empty.model_copy(update={
        "models": cli._parse_models("gemini-3.8-flash-high\tFlash High\n"),
        "model_discovery_error": None,
    })
    cli.probe = AsyncMock(side_effect=[empty, success])
    registry = AgentRegistry(include_builtins=False)
    registry.register_adapter(cli)
    result = await AgentDiscoveryService(registry).scan(force_refresh=False)
    assert result[0].models[0].id == "gemini-3.8-flash-high"
    assert cli.probe.await_count == 2


@pytest.mark.anyio
async def test_ui_request_waits_for_in_progress_scan_instead_of_partial_cache() -> None:
    service = AgentDiscoveryService(AgentRegistry(include_builtins=False))
    empty = AgentProbeResult(id="gemini_cli", name="agy", status=AgentStatus.READY)
    success = empty.model_copy(update={
        "models": GeminiCliAdapter._parse_models("gemini-3.8-flash-high\tFlash High\n"),
    })
    async with service._scan_lock:
        service._cached_probes["gemini_cli"] = empty
        request = asyncio.create_task(service.scan(force_refresh=False))
        await asyncio.sleep(0)
        assert not request.done()
        service._cached_probes["gemini_cli"] = success
    assert (await request)[0].models[0].id == "gemini-3.8-flash-high"


@pytest.mark.anyio
async def test_qoder_authentication_is_authoritative_for_discovery(monkeypatch) -> None:
    cli = other_vendors.QoderAdapter()
    cli._resolved_binary = "/fixture/qodercli"
    monkeypatch.setattr(cli, "_candidate_binaries", lambda: [cli._resolved_binary])

    async def execute(argv, **kwargs):
        if argv[-1] == "--version":
            return 0, "1.1.41", ""
        return 1, "", "Not logged in. Run qodercli login to authenticate."

    # PlainCliAdapter owns version execution, Qoder owns catalog execution.
    monkeypatch.setattr(other_vendors, "safe_exec_cmd", execute)
    monkeypatch.setattr("persona_continuum.agent.protocols.plain_cli.safe_exec_cmd", execute)
    registry = AgentRegistry(include_builtins=False)
    registry.register_adapter(cli)
    result = await AgentDiscoveryService(registry).probe_adapter(cli.adapter_id)
    assert result is not None
    assert result.status == AgentStatus.AUTH_REQUIRED
    assert result.models == []


@pytest.mark.anyio
async def test_long_help_preserves_cli_argument_failure_and_research_classification() -> None:
    process = AsyncMock()
    process.stderr = None
    session = AgentSession(config=config())
    transport = SubprocessAgentTransport(process, session, stream_limit=1024)
    error = b"Unknown argument: effort\n" + b"help option\n" * 600
    transport._stderr_head.extend(error[:4096])
    transport._stderr_tail.extend(error)
    detail, diagnostic = GeminiCliAdapter().classify_process_failure(
        session, returncode=1, stderr_text=transport.stderr_tail, diagnostics={}
    )
    assert detail == "Unknown argument: effort"
    assert diagnostic["failure_subtype"] == "cli_invalid_argument"
    classified = AgenticCliResearchBackend._classified_probe_error(
        RuntimeError(detail), operation="search", cli_name="Gemini"
    )
    assert classified.code == "WEB_RESEARCH_CLI_INCOMPATIBLE"


@pytest.mark.anyio
async def test_agy_stream_error_preserves_provider_region_rejection(monkeypatch) -> None:
    cli = GeminiCliAdapter()
    cli._resolved_binary = "/fixture/agy"
    transport = AsyncMock()
    transport.readline.return_value = (
        b'{"event":"result","result":{"status":"ERROR","error":'
        b'"Eligibility check failed: Antigravity is not currently available in your location."}}\n'
    )
    monkeypatch.setattr(cli, "_ensure_stream_process", AsyncMock(return_value=transport))
    events = [event async for event in cli.send(
        AgentSession(config=config()), AgentTurn(user_message="Public research probe")
    )]
    assert len(events) == 1 and events[0].type == AgentEventType.ERROR
    assert "location" in events[0].error
    assert events[0].metadata["failure"]["retriable"] is False
    error = AgenticCliResearchBackend._classified_probe_error(
        RuntimeError(events[0].error), operation="search", cli_name="Gemini"
    )
    assert error.code == "WEB_RESEARCH_REGION_BLOCKED"


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["searched_false", "fetched_false", "empty", "wrong_url"])
async def test_research_rejects_declarations_without_matching_fetch_evidence(failure) -> None:
    backend = AgenticCliResearchBackend(
        GeminiCliAdapter(), {"agent_id": "fixture"}, job_id="fixture",
        url_validator=lambda url: {"status_code": 200},
    )
    search = {"searched": True, "sources": [{"url": "https://openai.com/"}]}
    fetch = {"fetched": True, "url": "https://openai.com/", "content": "Page excerpt"}
    if failure == "searched_false":
        search["searched"] = False
    elif failure == "fetched_false":
        fetch["fetched"] = False
    elif failure == "empty":
        fetch["content"] = ""
        fetch["title"] = "A title is not fetched content"
    else:
        fetch["url"] = "https://openai.com/unrelated"
    backend._ask = AsyncMock(side_effect=[search, fetch])
    with pytest.raises(ResearchCapabilityProbeError):
        await backend.verify_cli_research_capability()


def test_old_research_contract_cache_is_not_promoted(app) -> None:
    cache = app.research_capability_cache
    binding = {
        "agent_id": "gemini_cli", "agent_version": "1.2.14",
        "model_id": "gemini-3.8-flash-high", "runtime_source": "local_cli",
    }
    capability = ResearchCapability(verification_status="verified")
    cache.put(**binding, capability=capability, configuration_fingerprint="legacy-contract")
    assert cache.get(**binding) is None
    assert cache.latest_for_runtime(
        agent_id="gemini_cli", agent_version="1.2.14", runtime_source="local_cli"
    ) is None
    cache.put(**binding, capability=capability)
    assert cache.get(**binding) is not None


def test_google_missing_key_is_not_misclassified_by_policy_deprecation_warning() -> None:
    error = AgenticCliResearchBackend._classified_probe_error(
        RuntimeError(
            "Warning: --allowed-tools is deprecated. Migrate to Policy Engine. "
            "When using Gemini API, you must specify the GEMINI_API_KEY environment variable."
        ), operation="search", cli_name="Google Gemini",
    )
    assert error.code == "WEB_RESEARCH_AUTH_REQUIRED"
    warning = AgenticCliResearchBackend._classified_probe_error(
        RuntimeError("Warning: --allowed-tools is deprecated. Migrate to Policy Engine."),
        operation="search", cli_name="Google Gemini",
    )
    assert warning.code == "WEB_SEARCH_UNAVAILABLE"
