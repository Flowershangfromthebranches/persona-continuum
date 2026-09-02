from __future__ import annotations

from pathlib import Path

from persona_continuum.agent.adapter import AgentSession
from persona_continuum.agent.adapters.gemini import (
    LOCATION_UNSUPPORTED_MESSAGE,
    GeminiCliAdapter,
    classify_agy_cli_failure,
)
from persona_continuum.agent.models import AgentSessionConfig, PermissionProfile

AGY_MODELS_OUTPUT = """Fetching available models...
gemini-3.7-flash-highGemini 3.7 Flash (High)
gemini-3.7-flash-mediumGemini 3.7 Flash (Medium)
gemini-3.7-flash-lowGemini 3.7 Flash (Low)
claude-sonnet-4-6Claude Sonnet 4.6 (Thinking)
"""


def test_parse_agy_concatenated_model_listing() -> None:
    models = GeminiCliAdapter._parse_models(AGY_MODELS_OUTPUT)
    by_id = {model.id: model for model in models}
    assert "gemini-3.7-flash-high" in by_id
    assert by_id["gemini-3.7-flash-high"].display_name == "Gemini 3.7 Flash (High)"
    assert by_id["gemini-3.7-flash-high"].supported_reasoning_efforts == ["high"]
    assert by_id["gemini-3.7-flash-high"].context_window == 1_048_576
    assert by_id["gemini-3.7-flash-medium"].supported_reasoning_efforts == ["medium"]
    assert by_id["claude-sonnet-4-6"].display_name.startswith("Claude Sonnet")


def test_parse_tab_separated_models_still_works() -> None:
    models = GeminiCliAdapter._parse_models(
        "gemini-3.7-flash-high\tGemini 3.7 Flash (High)\n"
    )
    assert models[0].id == "gemini-3.7-flash-high"
    assert models[0].supported_reasoning_efforts == ["high"]


def _config(model_id: str, reasoning: str) -> AgentSessionConfig:
    return AgentSessionConfig(
        room_id="narrative:story_bible",
        participant_id="story_bible",
        persona_id="narrative_studio",
        model_id=model_id,
        reasoning_effort=reasoning,
        permission_profile=PermissionProfile.CHAT_SAFE,
    )


def test_suffixed_model_keeps_matching_effort_flag() -> None:
    adapter = GeminiCliAdapter()
    model, reasoning = adapter.resolve_cli_model_and_reasoning(
        _config("gemini-3.7-flash-high", "high")
    )
    assert model == "gemini-3.7-flash-high"
    assert reasoning == "high"


def test_suffixed_model_rewrites_effort_into_model_id() -> None:
    adapter = GeminiCliAdapter()
    model, reasoning = adapter.resolve_cli_model_and_reasoning(
        _config("gemini-3.7-flash-high", "medium")
    )
    assert model == "gemini-3.7-flash-medium"
    assert reasoning == "medium"


def test_unsuffixed_model_still_passes_effort_flag() -> None:
    adapter = GeminiCliAdapter()
    model, reasoning = adapter.resolve_cli_model_and_reasoning(
        _config("gemini-2.5-pro", "high")
    )
    assert model == "gemini-2.5-pro"
    assert reasoning == "high"


def test_classify_agy_location_failure_hides_email_and_explains() -> None:
    log = (
        "OAuth: authenticated successfully as user@example.com\n"
        "agent executor error: calling model: FAILED_PRECONDITION (code 400): "
        "User location is not supported for the API use.\n"
        "Print mode: run ended with error and no response: "
        "Agent execution terminated due to error.\n"
    )
    message = classify_agy_cli_failure(
        "Agent execution terminated due to error.", log
    )
    assert message == LOCATION_UNSUPPORTED_MESSAGE
    assert "example.com" not in message
    assert "user location is not supported" in message.casefold()


def test_agy_print_mode_attaches_private_log_file() -> None:
    adapter = GeminiCliAdapter()
    adapter._resolved_binary = "/opt/local/bin/agy"
    config = _config("gemini-3.7-flash-high", "high")
    args = adapter.build_extra_cli_args(config)
    assert args[:1] == ["--log-file"]
    log_path = Path(args[1])
    assert log_path.exists()
    log_path.write_text(
        "OAuth: authenticated successfully as user@example.com\n"
        "agent executor error: calling model: FAILED_PRECONDITION (code 400): "
        "User location is not supported for the API use.\n"
    )
    session = AgentSession(config=config, session_data={})
    classified, diagnostics = adapter.classify_process_failure(
        session,
        returncode=1,
        stderr_text="Agent execution terminated due to error.",
        diagnostics={"returncode": 1},
    )
    assert classified == LOCATION_UNSUPPORTED_MESSAGE
    assert diagnostics["cli_failure"] == classified
    assert "example.com" not in str(diagnostics)
    assert not log_path.exists()


def test_official_gemini_binary_does_not_add_agy_log_file() -> None:
    adapter = GeminiCliAdapter()
    adapter._resolved_binary = "/usr/local/bin/gemini"
    args = adapter.build_extra_cli_args(_config("gemini-3.7-flash-high", "high"))
    assert args == []
