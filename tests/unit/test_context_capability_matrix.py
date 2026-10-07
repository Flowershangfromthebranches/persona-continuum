"""Acceptance matrix for context capability discovery (runtime-first)."""

from __future__ import annotations

from persona_continuum.agent.adapter import AgentSession
from persona_continuum.agent.adapters.claude import ClaudeCodeAdapter
from persona_continuum.agent.adapters.codex import CodexAdapter
from persona_continuum.agent.adapters.command_code import CommandCodeAdapter
from persona_continuum.agent.adapters.fake import FakeAgentAdapter
from persona_continuum.agent.adapters.gemini import GeminiCliAdapter
from persona_continuum.agent.adapters.grok import GrokBuildAdapter
from persona_continuum.agent.adapters.other_vendors import QoderAdapter
from persona_continuum.agent.context_capability import (
    PLANNING_CONTEXT_WINDOW_TOKENS,
    ContextCapabilityInput,
    ContextCapabilityResolver,
    ContextWindowMode,
    default_model_capability_registry,
)
from persona_continuum.agent.context_fields import ContextScope
from persona_continuum.agent.models import (
    AgentSessionConfig,
    EffectiveModelCapabilities,
    RuntimeBindingSnapshot,
)
from persona_continuum.agent.prompt_transport import resolve_prompt_transport_capability
from persona_continuum.agent.protocols.acp import ACPAdapter
from persona_continuum.agent.protocols.streaming_json_cli import StreamingJsonCliAdapter
from persona_continuum.agent.runtime_executor import AgentRuntimeExecutor, RuntimeSessionBinding


def _resolve(**kwargs: object):
    return ContextCapabilityResolver().resolve(ContextCapabilityInput(**kwargs))  # type: ignore[arg-type]


def test_grok_runtime_500k_beats_stale_128k_table() -> None:
    resolved = _resolve(
        requested_model="grok-4.6",
        adapter_id="grok",
        runtime_reported_context_window=500_000,
        context_window_mode=ContextWindowMode.DISCOVERABLE.value,
    )
    assert resolved.effective_context_window == 500_000
    assert resolved.native_context_window == 500_000
    assert resolved.context_capability_source == "runtime_reported"
    assert resolved.context_verified is True


def test_grok_fallback_table_is_500k_not_128k() -> None:
    model = {item.id: item for item in GrokBuildAdapter()._fallback_models()}["grok-4.6"]
    assert model.context_window == 500_000
    registry = default_model_capability_registry().lookup("grok-4.6")
    assert registry is not None
    assert registry.native_context_window == 500_000
    assert GrokBuildAdapter.context_window_mode == ContextWindowMode.DISCOVERABLE


def test_claude_streaming_json_transport_is_stdin() -> None:
    capability = resolve_prompt_transport_capability(ClaudeCodeAdapter())
    assert capability.transport_mode == "stdin"
    assert capability.supports_large_prompt is True
    assert StreamingJsonCliAdapter.prompt_transport_mode == "stdin"


def test_claude_sonnet5_and_haiku_official_fallback() -> None:
    sonnet = _resolve(requested_model="claude-sonnet-5")
    haiku = _resolve(requested_model="claude-haiku-4-5")
    opus = _resolve(requested_model="claude-opus-5")
    assert sonnet.native_context_window == 1_000_000
    assert sonnet.effective_context_window == 1_000_000
    assert haiku.native_context_window == 200_000
    assert opus.native_context_window == 1_000_000
    assert sonnet.context_capability_source == "provider_official_registry"


def test_codex_runtime_beats_openai_api_native() -> None:
    resolved = _resolve(
        requested_model="gpt-5.6-sol",
        adapter_id="codex",
        runtime_reported_context_window=372_000,
        upstream_context_is_native_only=True,
        context_window_mode=ContextWindowMode.DISCOVERABLE.value,
    )
    assert resolved.effective_context_window == 372_000
    assert resolved.native_context_window == 1_050_000
    assert resolved.context_capability_source == "runtime_reported"
    assert CodexAdapter.upstream_context_is_native_only is True


def test_codex_without_runtime_does_not_force_api_native_as_effective() -> None:
    resolved = _resolve(
        requested_model="gpt-5.6-sol",
        adapter_id="codex",
        upstream_context_is_native_only=True,
    )
    assert resolved.native_context_window == 1_050_000
    assert resolved.effective_context_window is None
    assert resolved.planning_window == PLANNING_CONTEXT_WINDOW_TOKENS
    assert resolved.context_verified is False


def test_gemini_25_pro_is_1m_not_2m() -> None:
    official = {item.id: item for item in GeminiCliAdapter._OFFICIAL_MODELS}["gemini-2.5-pro"]
    assert official.context_window == 1_048_576
    resolved = _resolve(requested_model="gemini-2.5-pro")
    assert resolved.native_context_window == 1_048_576
    assert resolved.effective_context_window == 1_048_576


def test_qoder_requested_1m_runtime_400k_effective_400k() -> None:
    resolved = _resolve(
        requested_model="qwen3.8-max",
        adapter_id="qoder",
        requested_context_window=1_000_000,
        runtime_reported_context_window=400_000,
        context_window_mode=ContextWindowMode.CONFIGURABLE_AND_DISCOVERABLE.value,
    )
    assert resolved.requested_context_window == 1_000_000
    assert resolved.effective_context_window == 400_000
    assert resolved.notes.get("requested_upscale_ignored")
    assert QoderAdapter.context_window_mode == ContextWindowMode.CONFIGURABLE_AND_DISCOVERABLE


def test_command_code_runtime_active_model_overrides_registry() -> None:
    resolved = _resolve(
        requested_model="meta/muse-spark-1.3-contributor",
        adapter_id="command_code",
        runtime_reported_context_window=1_048_576,
        upstream_context_is_native_only=True,
        context_window_mode=ContextWindowMode.DISCOVERABLE.value,
    )
    assert resolved.effective_context_window == 1_048_576
    assert resolved.context_capability_source == "runtime_reported"
    assert CommandCodeAdapter.upstream_context_is_native_only is True


def test_opencode_acp_session_context_window_propagates() -> None:
    caps = EffectiveModelCapabilities.resolve(
        {
            "id": "opencode",
            "capabilities": {"context_window_mode": "discoverable", "persistent_session": True},
            "runtime_binding_snapshot": {
                "context_window": 262_144,
                "context_window_source": "runtime_reported",
            },
            "models": [{"id": "opencode/big-pickle", "source": "protocol_model_list"}],
        },
        requested_model="opencode/big-pickle",
        effective_model="opencode/big-pickle",
        adapter_id="opencode",
    )
    assert caps.effective_context_window == 262_144
    assert caps.context_capability_source == "runtime_reported"
    assert caps.context_verified is True
    assert ACPAdapter.context_window_mode == ContextWindowMode.DISCOVERABLE


def test_unknown_model_native_is_unknown_not_32k() -> None:
    resolved = _resolve(requested_model="brand-new-unlisted-model")
    assert resolved.native_context_window is None
    assert resolved.effective_context_window is None
    assert resolved.context_verified is False
    assert resolved.planning_context_window == PLANNING_CONTEXT_WINDOW_TOKENS
    assert resolved.planning_window == PLANNING_CONTEXT_WINDOW_TOKENS


def test_user_cannot_upscale_verified_runtime() -> None:
    resolved = _resolve(
        requested_model="m",
        runtime_reported_context_window=200_000,
        user_override_context_window=1_000_000,
        context_window_mode=ContextWindowMode.CONFIGURABLE_AND_DISCOVERABLE.value,
    )
    assert resolved.effective_context_window == 200_000
    assert resolved.requested_context_window == 1_000_000


def test_user_can_downscale() -> None:
    resolved = _resolve(
        requested_model="glm-5.3",
        runtime_reported_context_window=1_000_000,
        requested_context_window=400_000,
        context_window_mode=ContextWindowMode.CONFIGURABLE.value,
    )
    assert resolved.effective_context_window == 400_000


def test_remaining_context_from_runtime() -> None:
    resolved = _resolve(
        requested_model="grok-4.6",
        runtime_reported_context_window=500_000,
        runtime_reported_remaining_context=370_000,
        persistent_session=True,
    )
    assert resolved.remaining_context_tokens == 370_000
    assert resolved.remaining_context_verified is True
    assert resolved.remaining_context_source == "runtime_reported"


def test_remaining_estimated_when_unreported() -> None:
    resolved = _resolve(
        requested_model="grok-4.6",
        runtime_reported_context_window=500_000,
        session_used_tokens=130_000,
        persistent_session=True,
    )
    assert resolved.remaining_context_tokens == 370_000
    assert resolved.remaining_context_verified is False
    assert resolved.remaining_context_source == "estimated_remaining"


def test_inferred_fresh_remaining_is_not_verified() -> None:
    resolved = _resolve(
        requested_model="grok-4.6",
        runtime_reported_context_window=500_000,
        persistent_session=False,
    )
    assert resolved.remaining_context_tokens == 500_000
    assert resolved.remaining_context_verified is False
    assert resolved.remaining_context_source == "inferred_fresh_session"


def test_persistent_unknown_remaining_stays_unknown() -> None:
    resolved = _resolve(
        requested_model="grok-4.6",
        runtime_reported_context_window=500_000,
        persistent_session=True,
    )
    assert resolved.remaining_context_tokens is None
    assert resolved.remaining_context_verified is False
    assert resolved.remaining_context_source == "unknown"


def test_registry_grok45_luna_and_claude_max_output() -> None:
    from persona_continuum.agent.context_capability import ModelCapabilityRegistry

    registry = ModelCapabilityRegistry()
    grok45 = registry.lookup("grok-4.5")
    luna = registry.lookup("gpt-5.6-luna")
    sonnet = registry.lookup("claude-sonnet-5")
    opus = registry.lookup("claude-opus-5")
    assert grok45 is not None and grok45.native_context_window == 500_000
    assert luna is not None and luna.native_context_window == 1_050_000
    assert sonnet is not None and sonnet.max_output_tokens == 128_000
    assert opus is not None and opus.max_output_tokens == 128_000


def test_runtime_metadata_covers_static_registry() -> None:
    resolved = _resolve(
        requested_model="grok-4.5",
        runtime_reported_context_window=262_144,
        context_window_mode=ContextWindowMode.DISCOVERABLE.value,
    )
    assert resolved.native_context_window == 500_000
    assert resolved.effective_context_window == 262_144
    assert resolved.context_capability_source == "runtime_reported"


class _PersistentFake(FakeAgentAdapter):
    context_scope = ContextScope.PERSISTENT

    def supports_persistent_conversation(self, session: AgentSession) -> bool:
        return True


def _binding(session_data: dict) -> RuntimeSessionBinding:
    adapter = _PersistentFake(context_window=500_000)
    session = AgentSession(
        config=AgentSessionConfig(
            room_id="r",
            participant_id="p",
            persona_id="x",
            model_id="grok-4.5",
        ),
        session_data=session_data,
    )
    return RuntimeSessionBinding(
        adapter=adapter,
        session=session,
        snapshot=RuntimeBindingSnapshot(
            agent_id="fake_agent",
            protocol="fake",
            effective_model="grok-4.5",
        ),
    )


def test_executor_planning_uses_runtime_remaining_as_hard_budget() -> None:
    executor = AgentRuntimeExecutor()
    caps = executor._planning_capability(
        _binding(
            {
                "effective_context_window": 500_000,
                "remaining_context_tokens": 80_000,
                "remaining_context_verified": True,
                "remaining_context_source": "runtime_reported",
                "effective_model": "grok-4.5",
            }
        ),
        phase="material_classification",
    )
    assert caps.remaining_context_tokens == 80_000
    assert caps.remaining_context_verified is True
    assert caps.remaining_context_source == "runtime_reported"
    budget = executor.context_budget_manager.budget_for(
        model=caps, phase="material_classification"
    )
    assert budget.usable_context_budget is not None
    assert budget.usable_context_budget <= 80_000
    assert budget.phase_working_target is not None
    assert budget.phase_working_target <= 80_000


def test_executor_planning_uses_estimated_remaining_not_native() -> None:
    executor = AgentRuntimeExecutor()
    caps = executor._planning_capability(
        _binding(
            {
                "effective_context_window": 500_000,
                "remaining_context_tokens": 80_000,
                "remaining_context_verified": False,
                "remaining_context_source": "estimated_remaining",
                "session_used_tokens": 10_000,
                "effective_model": "grok-4.5",
            }
        ),
        phase="material_classification",
    )
    assert caps.remaining_context_tokens == 80_000
    assert caps.remaining_context_verified is False
    assert caps.remaining_context_source == "estimated_remaining"
    budget = executor.context_budget_manager.budget_for(
        model=caps, phase="material_classification"
    )
    assert budget.usable_context_budget is not None
    assert budget.usable_context_budget <= 80_000


def test_unknown_remaining_is_not_verified() -> None:
    resolved = _resolve(
        requested_model="grok-4.6",
        runtime_reported_context_window=500_000,
        persistent_session=True,
    )
    assert resolved.remaining_context_source == "unknown"
    assert resolved.remaining_context_verified is False
    assert resolved.remaining_context_tokens is None
