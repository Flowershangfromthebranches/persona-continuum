"""Written API reasoning-capability contracts.

These tests are intentionally not executed during the implementation turn.
They prevent the UI and HTTP adapter from advertising a reasoning control that
is not actually bound to a request field.
"""

from __future__ import annotations

import asyncio
import inspect

from persona_continuum.agent.models import (
    AgentSessionConfig,
    ReasoningCapability,
    ReasoningCapabilityMode,
)
from persona_continuum.agent.protocols.openai_compatible import (
    OpenAICompatibleAPIAdapter,
)


def _adapter(
    *,
    provider_type: str = "openai_compatible",
    capabilities: dict[str, object] | None = None,
) -> OpenAICompatibleAPIAdapter:
    return OpenAICompatibleAPIAdapter(
        adapter_id="api_reasoning_contract",
        name="Reasoning contract API",
        base_url="https://provider.invalid/v1",
        default_model="contract-model",
        provider_type=provider_type,
        model_capabilities=capabilities,
    )


def test_unknown_reasoning_capability_is_not_advertised() -> None:
    capability = _adapter()._reasoning_capability("contract-model")
    assert capability.mode == ReasoningCapabilityMode.UNKNOWN
    assert capability.supported_efforts == []
    assert capability.verified is False


def test_dynamic_api_unknown_reasoning_is_not_fake_verified() -> None:
    capability = _adapter(
        capabilities={
            "contract-model": {
                "supported_reasoning_efforts": [],
                "source": "dynamic",
            }
        }
    )._reasoning_capability("contract-model")
    assert capability.mode == ReasoningCapabilityMode.UNKNOWN
    assert capability.verified is False


def test_protocol_model_list_reasoning_metadata_is_detected() -> None:
    adapter = _adapter()
    capability = adapter._model_list_reasoning_capability(
        {
            "id": "contract-model",
            "reasoning": {
                "supported_efforts": ["none", "low", "high", "ultra"],
                "default_effort": "high",
            },
        }
    )
    assert capability.mode == ReasoningCapabilityMode.NATIVE_EFFORT
    assert capability.supported_efforts == ["none", "low", "high", "ultra"]
    assert capability.default_effort == "high"
    assert capability.binding_strategy == "openai_compatible_reasoning_effort"
    assert capability.source == "protocol_model_list"
    assert capability.verified is True


def test_protocol_model_list_reasoning_object_entries_preserve_future_values() -> None:
    adapter = _adapter()
    capability = adapter._model_list_reasoning_capability(
        {
            "id": "contract-model",
            "supportedReasoningEfforts": [
                {"reasoningEffort": "xhigh"},
                {"reasoningEffort": "future-adaptive"},
            ],
            "defaultReasoningEffort": "future-adaptive",
        }
    )
    assert capability.supported_efforts == ["xhigh", "future-adaptive"]
    assert capability.default_effort == "future-adaptive"


def test_parameter_name_without_allowed_efforts_stays_unknown() -> None:
    capability = _adapter()._model_list_reasoning_capability(
        {"id": "contract-model", "supported_parameters": ["reasoning_effort"]}
    )
    assert capability.mode == ReasoningCapabilityMode.UNKNOWN
    assert capability.supported_efforts == []
    assert capability.verified is False


def test_provider_specific_model_list_reasoning_stays_unbound() -> None:
    capability = _adapter(provider_type="anthropic")._model_list_reasoning_capability(
        {
            "id": "contract-model",
            "reasoning": {"supported_efforts": ["high"]},
        }
    )
    assert capability.mode == ReasoningCapabilityMode.UNSUPPORTED
    assert capability.supported_efforts == []
    assert capability.verified is False


def test_api_manual_reasoning_capability_is_selectable() -> None:
    capability = ReasoningCapability(
        mode=ReasoningCapabilityMode.MANUAL_CONFIG,
        supported_efforts=["none", "low", "medium"],
        default_effort="medium",
        binding_strategy="manual_config",
        verified=False,
        source="manual_ui",
    )
    adapter = _adapter(
        capabilities={
            "contract-model": {"reasoning_capability": capability.model_dump(mode="json")}
        }
    )
    resolved = adapter._reasoning_capability("contract-model")
    assert resolved.mode == ReasoningCapabilityMode.MANUAL_CONFIG
    assert resolved.supported_efforts == ["none", "low", "medium"]
    assert resolved.default_effort == "medium"
    assert resolved.verified is False


def test_api_manual_default_capability_applies_to_every_discovered_model() -> None:
    adapter = _adapter(
        capabilities={
            "default": {
                "reasoning_capability": {
                    "mode": "manual_config",
                    "supported_efforts": ["low", "medium", "custom-deep"],
                    "default_effort": "medium",
                    "binding_strategy": "manual_config",
                    "verified": False,
                    "source": "manual_ui",
                }
            }
        }
    )
    resolved = adapter._reasoning_capability("newly-discovered-model")
    assert resolved.mode == ReasoningCapabilityMode.MANUAL_CONFIG
    assert resolved.supported_efforts == ["low", "medium", "custom-deep"]
    assert resolved.default_effort == "medium"


def test_anthropic_api_reasoning_is_unsupported_without_binding() -> None:
    adapter = _adapter(
        provider_type="anthropic",
        capabilities={
            "contract-model": {
                "reasoning_capability": {
                    "mode": "native_effort",
                    "supported_efforts": ["high"],
                    "default_effort": "high",
                    "binding_strategy": "provider_native",
                    "verified": True,
                    "source": "manual_ui",
                }
            }
        },
    )
    capability = adapter._reasoning_capability("contract-model")
    assert capability.mode == ReasoningCapabilityMode.UNSUPPORTED
    assert capability.verified is False
    assert capability.binding_strategy == "provider_specific_unbound"


def test_anthropic_requested_reasoning_not_marked_verified_when_unbound() -> None:
    adapter = _adapter(provider_type="anthropic")
    session = asyncio.run(
        adapter.create_session(
            AgentSessionConfig(
                room_id="room",
                participant_id="participant",
                persona_id="persona",
                model_id="contract-model",
                reasoning_effort="high",
            )
        )
    )
    snapshot = asyncio.run(adapter.bind_runtime(session))
    assert snapshot.requested_reasoning == "high"
    assert snapshot.effective_reasoning is None
    assert snapshot.reasoning_verified is False


def test_google_requested_reasoning_not_marked_verified_when_unbound() -> None:
    adapter = _adapter(provider_type="google")
    session = asyncio.run(
        adapter.create_session(
            AgentSessionConfig(
                room_id="room",
                participant_id="participant",
                persona_id="persona",
                model_id="contract-model",
                reasoning_effort="high",
            )
        )
    )
    snapshot = asyncio.run(adapter.bind_runtime(session))
    assert snapshot.requested_reasoning == "high"
    assert snapshot.effective_reasoning is None
    assert snapshot.reasoning_verified is False


def test_generic_reasoning_parameter_is_sent_when_supported() -> None:
    source = inspect.getsource(OpenAICompatibleAPIAdapter.send)
    assert 'payload["reasoning_effort"] = session.config.reasoning_effort' in source


def test_reasoning_rejection_fails_closed() -> None:
    adapter = _adapter()
    session = asyncio.run(
        adapter.create_session(
            AgentSessionConfig(
                room_id="room",
                participant_id="participant",
                persona_id="persona",
                model_id="contract-model",
                reasoning_effort="high",
            )
        )
    )
    error = adapter._reasoning_binding_error(session, "openai_compatible")
    assert error is not None
    assert error.code == "REASONING_BINDING_REJECTED"
    assert error.retriable is False


def test_provider_reject_reasoning_fails_closed() -> None:
    source = inspect.getsource(OpenAICompatibleAPIAdapter.send)
    assert "ReasoningBindingRejectedError" in source
    assert "_provider_rejected_reasoning" in source
    assert "compat_retries" in source


def test_api_runtime_snapshot_reports_effective_reasoning() -> None:
    adapter = _adapter(
        capabilities={
            "contract-model": {
                "reasoning_capability": {
                    "mode": "native_effort",
                    "supported_efforts": ["high"],
                    "default_effort": "high",
                    "binding_strategy": "provider_native",
                    "verified": True,
                    "source": "behavioral_probe",
                }
            }
        }
    )
    session = asyncio.run(
        adapter.create_session(
            AgentSessionConfig(
                room_id="room",
                participant_id="participant",
                persona_id="persona",
                model_id="contract-model",
                reasoning_effort="high",
            )
        )
    )
    snapshot = asyncio.run(adapter.bind_runtime(session))
    assert snapshot.requested_reasoning == "high"
    assert snapshot.effective_reasoning == "high"
    assert snapshot.verification_method == "provider_native"
    assert snapshot.reasoning_verified is True
