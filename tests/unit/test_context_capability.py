"""Context-capability resolution matrix (P0-1).

Every row of the acceptance matrix must hold for ANY adapter, not just one
vendor: the resolver ranks evidence by trust and never invents a number.
``Unknown == Unknown`` -- the historical ``Unknown == 32K`` fallback is gone.
"""

from __future__ import annotations

import pytest

from persona_continuum.agent.context_budget import AgentContextBudgetManager
from persona_continuum.agent.context_capability import (
    PLANNING_CONTEXT_WINDOW_TOKENS,
    ContextCapabilityInput,
    ContextCapabilityResolver,
    ContextCapabilitySource,
    ContextWindowMode,
    ModelCapabilityRecord,
    ModelCapabilityRegistry,
    ResolvedContextCapability,
    canonical_model_key,
    compute_preferred_working_context,
    compute_usable_budget,
    default_model_capability_registry,
)
from persona_continuum.agent.models import EffectiveModelCapabilities

MILLION = 1_048_576


def resolve(**kwargs: object) -> ResolvedContextCapability:
    return ContextCapabilityResolver().resolve(ContextCapabilityInput(**kwargs))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Source priority
# ---------------------------------------------------------------------------


def test_runtime_reported_wins_over_everything() -> None:
    resolved = resolve(
        requested_model="glm-5.3",
        runtime_reported_context_window=400_000,
        model_capability={"id": "glm-5.3", "context_window": MILLION},
        context_window_mode=ContextWindowMode.DISCOVERABLE.value,
    )
    assert resolved.effective_context_window == 400_000
    assert resolved.context_capability_source == "runtime_reported"
    assert resolved.context_verified is True
    assert resolved.native_context_window == MILLION
    assert "runtime_narrowed_native" in resolved.notes


@pytest.mark.parametrize("window", [32_768, 131_072, MILLION])
def test_fixed_runtime_windows_pass_through(window: int) -> None:
    resolved = resolve(
        requested_model="m",
        runtime_reported_context_window=window,
        context_window_mode=ContextWindowMode.FIXED.value,
    )
    assert resolved.effective_context_window == window
    assert resolved.context_capability_source == "runtime_reported"
    assert resolved.context_verified is True


def test_runtime_reported_beats_registry_for_known_model() -> None:
    # Registry knows glm-5.3 == 1M; a live runtime that granted less wins.
    resolved = resolve(
        requested_model="glm-5.3",
        runtime_reported_context_window=200_000,
        context_window_mode=ContextWindowMode.DISCOVERABLE.value,
    )
    assert resolved.effective_context_window == 200_000
    assert resolved.context_capability_source == "runtime_reported"


# ---------------------------------------------------------------------------
# Requested / adapter-limit narrowing (never widening)
# ---------------------------------------------------------------------------


def test_configurable_requested_window_narrows_effective() -> None:
    resolved = resolve(
        requested_model="qwen3.8-max",
        requested_context_window=400_000,
        context_window_mode=ContextWindowMode.CONFIGURABLE.value,
    )
    assert resolved.effective_context_window == 400_000
    assert resolved.requested_context_window == 400_000
    assert resolved.notes.get("requested_context_applied") is True


def test_requested_window_cannot_widen_runtime() -> None:
    # Requested 1M but the runtime only granted 400K: Effective == 400K.
    resolved = resolve(
        requested_model="m",
        requested_context_window=MILLION,
        runtime_reported_context_window=400_000,
        context_window_mode=ContextWindowMode.CONFIGURABLE_AND_DISCOVERABLE.value,
    )
    assert resolved.effective_context_window == 400_000
    assert resolved.requested_context_window == MILLION


def test_adapter_context_limit_caps_effective() -> None:
    # Native 1M, adapter only supports 400K.
    resolved = resolve(
        requested_model="qwen3.8-flash",
        adapter_context_limit=400_000,
        context_window_mode=ContextWindowMode.FIXED.value,
    )
    assert resolved.native_context_window == MILLION
    assert resolved.effective_context_window == 400_000
    assert resolved.adapter_context_limit == 400_000
    assert "adapter_context_limit_applied" in resolved.notes


# ---------------------------------------------------------------------------
# Shared ModelCapabilityRegistry
# ---------------------------------------------------------------------------


def test_registry_resolves_unknown_to_adapter_model() -> None:
    resolved = resolve(requested_model="glm-5.3")
    assert resolved.effective_context_window == MILLION
    assert resolved.context_capability_source == "model_registry"
    assert resolved.canonical_model_id == "glm-5.3"
    # Verified-ness follows the registry record's own verified flag.
    assert resolved.context_verified is True


def test_registry_unverified_record_stays_unverified() -> None:
    registry = ModelCapabilityRegistry()
    registry.register(
        ModelCapabilityRecord(
            canonical_model_id="rumored-model",
            native_context_window=128_000,
            verified=False,
        )
    )
    resolved = ContextCapabilityResolver(registry).resolve(
        ContextCapabilityInput(requested_model="rumored-model")
    )
    assert resolved.effective_context_window == 128_000
    assert resolved.context_capability_source == "model_registry"
    assert resolved.context_verified is False


def test_registry_lookup_via_shared_default() -> None:
    registry = default_model_capability_registry()
    record = registry.lookup("deepseek-v4-pro")
    assert record is not None
    assert record.native_context_window == MILLION


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("qwen3.8-flash", "qwen3.8-flash"),
        ("Qwen3.8-Flash", "qwen3.8-flash"),
        ("qoder/qwen3.8-flash", "qwen3.8-flash"),
        ("provider/qwen3.8-flash", "qwen3.8-flash"),
        ("qwen3-8-flash", "qwen3.8-flash"),
        ("alibaba/qwen3-8-flash:latest", "qwen3.8-flash"),
    ],
)
def test_model_alias_normalization(alias: str, canonical: str) -> None:
    assert canonical_model_key(alias) == canonical_model_key(canonical)
    resolved = resolve(requested_model=alias)
    assert resolved.canonical_model_id == canonical
    assert resolved.effective_context_window == MILLION
    assert resolved.native_context_window == MILLION


def test_registry_custom_registration() -> None:
    registry = ModelCapabilityRegistry()
    registry.register(
        ModelCapabilityRecord(
            canonical_model_id="custom-model",
            aliases=["Custom-Model", "vendor/custom_model"],
            provider="vendor",
            native_context_window=512_000,
        )
    )
    assert registry.native_context_window("vendor/Custom-Model") == 512_000
    assert registry.native_context_window("custom-model") == 512_000
    assert registry.native_context_window("other-model") is None


# ---------------------------------------------------------------------------
# Unknown == Unknown
# ---------------------------------------------------------------------------


def test_unknown_model_stays_unknown() -> None:
    resolved = resolve(requested_model="totally-unknown-model")
    assert resolved.effective_context_window is None
    assert resolved.usable_context_budget is None
    assert resolved.preferred_working_context is None
    assert resolved.context_capability_source == "unknown"
    assert resolved.context_verified is False
    # Planning may still use an explicit fallback, clearly labelled.
    assert resolved.planning_window == PLANNING_CONTEXT_WINDOW_TOKENS


def test_empty_input_stays_unknown() -> None:
    resolved = resolve()
    assert resolved.effective_context_window is None
    assert resolved.context_capability_source == ContextCapabilitySource.UNKNOWN.value


def test_user_override_applies_when_nothing_else_known() -> None:
    resolved = resolve(
        requested_model="mystery-model",
        user_override_context_window=200_000,
        user_override_verified=True,
    )
    assert resolved.effective_context_window == 200_000
    assert resolved.context_capability_source == "user_override"
    assert resolved.context_verified is True


# ---------------------------------------------------------------------------
# Budget math (computed, never hardcoded)
# ---------------------------------------------------------------------------


def test_one_million_model_gets_nine_hundred_k_plus_budget() -> None:
    usable = compute_usable_budget(MILLION)
    assert 900_000 <= usable < MILLION
    preferred = compute_preferred_working_context(MILLION, usable)
    assert 0 < preferred <= usable


def test_budget_scales_with_window_not_constant() -> None:
    assert compute_usable_budget(32_768) < compute_usable_budget(131_072)
    assert compute_usable_budget(131_072) < compute_usable_budget(MILLION)
    # A fixed hardcoded number could never satisfy all three.
    assert compute_usable_budget(MILLION) - MILLION != compute_usable_budget(32_768) - 32_768


def test_preferred_working_context_unknown_is_none() -> None:
    assert compute_preferred_working_context(None) is None


def test_phase_budget_unknown_is_none() -> None:
    resolved = resolve(requested_model="unknown-model")
    assert resolved.phase_budget(output_reserve=1_000) is None
    known = resolve(requested_model="glm-5.3")
    assert known.phase_budget(output_reserve=1_000) == compute_usable_budget(
        MILLION, output_reserve=1_000
    )


# ---------------------------------------------------------------------------
# EffectiveModelCapabilities integration
# ---------------------------------------------------------------------------


def test_effective_model_capabilities_unknown_stays_none() -> None:
    caps = EffectiveModelCapabilities.resolve(
        {"id": "agent", "models": []}, requested_model="nope"
    )
    assert caps.effective_context_window is None
    assert caps.context_unknown is True
    assert caps.context_verified is False
    assert caps.planning_window == PLANNING_CONTEXT_WINDOW_TOKENS
    assert caps.phase_usable_budget() is None


def test_effective_model_capabilities_resolves_via_registry_alias() -> None:
    caps = EffectiveModelCapabilities.resolve(
        {"id": "agent", "models": []}, requested_model="qoder/Qwen3.8-Flash"
    )
    assert caps.effective_context_window == MILLION
    assert caps.usable_context_budget == compute_usable_budget(MILLION)
    assert caps.preferred_working_context is not None
    assert caps.canonical_model_id == "qwen3.8-flash"


def test_effective_model_capabilities_context_window_alias() -> None:
    caps = EffectiveModelCapabilities.resolve(
        {
            "id": "agent",
            "models": [
                {"id": "m1", "context_window": 262_144, "source": "dynamic"}
            ],
        },
        requested_model="m1",
        effective_model="m1",
    )
    # A dynamically probed window is verified and sourced as a probe.
    assert caps.effective_context_window == 262_144
    assert caps.context_capability_source == "adapter_dynamic_probe"
    assert caps.context_verified is True
    assert caps.context_window == 262_144  # deprecated alias still works


# ---------------------------------------------------------------------------
# ContextBudgetManager only consumes resolved capabilities
# ---------------------------------------------------------------------------


def test_budget_manager_consumes_effective_capability() -> None:
    manager = AgentContextBudgetManager()
    caps = EffectiveModelCapabilities.resolve(
        {"id": "agent", "models": []}, requested_model="glm-5.3"
    )
    assert manager.context_window(caps) == MILLION

    unknown_caps = EffectiveModelCapabilities.resolve(
        {"id": "agent", "models": []}, requested_model="nope"
    )
    assert manager.context_window(unknown_caps) is None
    window, source, verified = manager.planning_window(unknown_caps)
    assert window == PLANNING_CONTEXT_WINDOW_TOKENS
    assert source == "fallback_policy"
    assert verified is False


def test_budget_for_flags_unverified_planning_fallback() -> None:
    manager = AgentContextBudgetManager()
    budget = manager.budget_for(
        model={"effective_model": "unknown-model"},
        phase="dimension_extraction",
    )
    assert budget.context_window_tokens == PLANNING_CONTEXT_WINDOW_TOKENS
    assert budget.context_window_source == "fallback_policy"
    assert budget.context_verified is False


def test_batch_count_scales_with_effective_window() -> None:
    manager = AgentContextBudgetManager()
    items = [
        {"evidence_id": f"u{i}", "content": "x" * 4000} for i in range(40)
    ]

    def item_text(item: dict) -> str:
        return str(item["content"])

    small = list(
        manager.iter_batches(
            items,
            item_text=item_text,
            max_items=48,
            phase="dimension_extraction",
            model={"context_window": 32_768, "model_id": "m"},
        )
    )
    large = list(
        manager.iter_batches(
            items,
            item_text=item_text,
            max_items=48,
            phase="dimension_extraction",
            model={"context_window": MILLION, "model_id": "m"},
        )
    )
    assert len(small) > 1
    assert len(large) == 1
