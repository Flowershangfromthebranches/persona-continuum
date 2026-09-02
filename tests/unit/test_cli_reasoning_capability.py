from __future__ import annotations

from persona_continuum.agent.adapters.codex import CodexAdapter
from persona_continuum.agent.adapters.command_code import CommandCodeAdapter
from persona_continuum.agent.adapters.gemini import GeminiCliAdapter
from persona_continuum.agent.adapters.grok import GrokBuildAdapter
from persona_continuum.agent.adapters.opencode import OpenCodeAdapter
from persona_continuum.agent.adapters.other_vendors import CodeBuddyAdapter, WorkBuddyAdapter
from persona_continuum.agent.models import (
    ModelCapability,
    ReasoningCapabilityMode,
    SelectionStrategy,
)


def test_provider_name_does_not_override_official_cli_binding() -> None:
    model = ModelCapability(
        id="claude-cli-model",
        display_name="Claude CLI model",
        provider="anthropic",
        supported_reasoning_efforts=["low", "high"],
        default_reasoning_effort="high",
        source="official_cli",
        reasoning_selection=SelectionStrategy.STARTUP,
    )

    assert model.reasoning_capability.mode == ReasoningCapabilityMode.NATIVE_EFFORT
    assert model.reasoning_capability.supported_efforts == ["low", "high"]
    assert model.reasoning_capability.verified is True


def test_provider_api_like_unknown_still_fails_closed() -> None:
    model = ModelCapability(
        id="gemini-unknown",
        display_name="Gemini unknown",
        provider="google",
        supported_reasoning_efforts=["high"],
        source="dynamic",
        reasoning_selection=SelectionStrategy.UNSUPPORTED,
    )

    assert model.reasoning_capability.mode == ReasoningCapabilityMode.UNSUPPORTED
    assert model.reasoning_capability.binding_strategy == "provider_specific_unbound"
    assert model.reasoning_capability.verified is False


def test_manual_cli_config_is_not_misclassified_as_unbound_api() -> None:
    model = ModelCapability(
        id="claude-configured",
        display_name="Claude configured",
        provider="anthropic",
        supported_reasoning_efforts=["low", "medium", "high"],
        default_reasoning_effort="medium",
        source="config",
        reasoning_selection=SelectionStrategy.STARTUP,
    )

    assert model.reasoning_capability.mode == ReasoningCapabilityMode.MANUAL_CONFIG
    assert model.reasoning_capability.verified is False


def test_codex_protocol_model_list_exposes_native_reasoning() -> None:
    model = CodexAdapter()._parse_model_list_items(
        [
            {
                "id": "gpt-protocol",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "medium"},
                    {"reasoningEffort": "high"},
                ],
                "defaultReasoningEffort": "medium",
            }
        ]
    )[0]

    assert model.source == "protocol_model_list"
    assert model.reasoning_capability.mode == ReasoningCapabilityMode.NATIVE_EFFORT
    assert model.reasoning_capability.verified is True


def test_gemini_cli_reported_effort_is_not_treated_as_google_api() -> None:
    model = GeminiCliAdapter._parse_models("gemini-3-pro-high  Gemini 3 Pro High")[0]

    assert model.source == "official_cli"
    assert model.reasoning_capability.mode == ReasoningCapabilityMode.NATIVE_EFFORT
    assert model.reasoning_capability.supported_efforts == ["high"]
    assert model.reasoning_capability.verified is True


def test_grok_protocol_and_official_models_expose_native_reasoning() -> None:
    adapter = GrokBuildAdapter()
    protocol_model = adapter._parse_machine_model(
        {
            "id": "grok-protocol",
            "supportedReasoningEfforts": ["low", "high"],
            "defaultReasoningEffort": "high",
        }
    )
    official_model = adapter._capability_from_official_table("grok-4.6")

    assert protocol_model.source == "protocol_model_list"
    assert protocol_model.reasoning_capability.mode == ReasoningCapabilityMode.NATIVE_EFFORT
    assert official_model.reasoning_capability.mode == ReasoningCapabilityMode.NATIVE_EFFORT
    assert protocol_model.reasoning_capability.verified is True
    assert official_model.reasoning_capability.verified is True


def test_command_code_cli_flag_binding_exposes_reasoning() -> None:
    model = CommandCodeAdapter()._parse_models(
        "OpenAI\n\n  gpt-reasoning  reasoning model\n"
    )[0]

    assert model.source == "official_cli"
    assert model.reasoning_capability.mode == ReasoningCapabilityMode.NATIVE_EFFORT
    assert model.reasoning_capability.verified is True


def test_opencode_model_config_is_selectable_but_session_verified() -> None:
    adapter = OpenCodeAdapter()
    model = adapter._parse_models(
        "google:\n  google/gemini-reasoning\n"
    )[0]

    assert adapter.require_verified_binding is True
    assert model.source == "config"
    assert model.reasoning_capability.mode == ReasoningCapabilityMode.MANUAL_CONFIG
    assert model.reasoning_capability.verified is False


def test_vendor_cli_listings_expose_reasoning_instead_of_failing_closed() -> None:
    """Qoder/CodeBuddy/WorkBuddy parse official CLI output and bind an effort
    flag at startup; their parsed models must classify as native evidence so
    the runtime selector shows the effort ladder instead of reporting that
    the agent did not report any reasoning options."""

    help_listing = "Currently supported: (hy3, glm-5.3, kimi-k3-1)"
    codebuddy_models = CodeBuddyAdapter()._parse_models_from_help(help_listing)
    workbuddy_models = WorkBuddyAdapter._parse_models_from_help(help_listing)

    for models in (codebuddy_models, workbuddy_models):
        assert models
        assert all(m.source == "official_cli" for m in models)
        assert all(
            m.reasoning_capability.mode == ReasoningCapabilityMode.NATIVE_EFFORT
            and m.reasoning_capability.verified
            and m.reasoning_capability.supported_efforts
            for m in models
        )
