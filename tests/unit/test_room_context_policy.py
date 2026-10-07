"""Context Policy: profile resolution, budget arithmetic and A/B guarantees.

These tests are the acceptance instrument for the decoupling work: a resource
limit measured on one machine must never decide what another model may see.
Nothing here touches a memory store -- the point is that the policy layer has
no way to.
"""

from __future__ import annotations

import math

from persona_continuum.config import Config
from persona_continuum.room.context_policy import (
    BALANCED_PROFILE,
    LOCAL_CONSTRAINED_PROFILE,
    PROFILE_BALANCED,
    PROFILE_LOCAL_CONSTRAINED,
    PROFILE_REMOTE_QUALITY,
    REMOTE_QUALITY_PROFILE,
    ContextPolicyRequest,
    ContextStrategy,
    endpoint_is_local,
    local_profile_from_config,
    normalize_strategy,
    resolve_context_policy,
)

MB = 1_048_576


def _resolve(**kwargs):
    return resolve_context_policy(ContextPolicyRequest(**kwargs))


# --- endpoint locality -----------------------------------------------------


def test_endpoint_is_local_accepts_loopback_and_private_hosts() -> None:
    for url in (
        "http://127.0.0.1:8080/v1",
        "http://localhost:11434/v1",
        "localhost:8000/v1",
        "http://[::1]:1234/v1",
        "http://192.168.1.9:8080/v1",
        "http://10.0.0.4/v1",
        "http://mac-studio.local:8080/v1",
        "http://host.docker.internal:1234/v1",
    ):
        assert endpoint_is_local(url), url


def test_endpoint_is_local_rejects_public_hosts() -> None:
    for url in (
        "https://api.openai.com/v1",
        "https://api.moonshot.cn/v1",
        "https://generativelanguage.googleapis.com/v1beta",
        None,
        "",
    ):
        assert not endpoint_is_local(url), url


def test_cli_transport_is_never_treated_as_local_memory() -> None:
    """A CLI runs locally but its model usually does not.  CLI != constrained."""

    policy = _resolve(
        strategy="auto",
        runtime_source="local_cli",
        adapter_id="kimi",
        model_id="kimi-k3",
        context_window=MB,
    )
    assert policy.profile_name == PROFILE_REMOTE_QUALITY
    assert policy.local_endpoint is False


# --- auto resolution matrix ------------------------------------------------


def test_local_endpoint_with_bound_window_keeps_the_measured_safe_profile() -> None:
    policy = _resolve(
        strategy="auto",
        base_url="http://127.0.0.1:8080/v1",
        model_id="qwen3.8-27b",
        context_window=16_384,
    )
    assert policy.profile_name == PROFILE_LOCAL_CONSTRAINED
    assert policy.profile is LOCAL_CONSTRAINED_PROFILE
    assert policy.local_safety_ceiling_applied is True
    # The stress-tested numbers must be untouched by this work.
    assert policy.prompt_target_tokens == 6000
    assert policy.prompt_hard_tokens == 7000
    assert policy.effective_context_budget == 7000


def test_remote_million_token_model_is_not_capped_at_7000() -> None:
    policy = _resolve(strategy="auto", base_url="https://api.moonshot.cn/v1", context_window=MB)
    assert policy.profile_name == PROFILE_REMOTE_QUALITY
    assert policy.prompt_hard_tokens > 100_000
    assert policy.profile.recall_top_k > 8
    assert policy.profile.recent_message_window > 8
    assert policy.profile.recent_dialogue_token_budget > 6000
    assert policy.profile.summary_output_max_tokens > 800


def test_remote_128k_endpoint_resolves_to_quality() -> None:
    policy = _resolve(
        strategy="auto", base_url="https://api.anthropic.com/v1", context_window=131_072
    )
    assert policy.profile_name == PROFILE_REMOTE_QUALITY


def test_mid_context_remote_endpoint_resolves_to_balanced() -> None:
    policy = _resolve(strategy="auto", base_url="https://api.example.com/v1", context_window=40_960)
    assert policy.profile_name == PROFILE_BALANCED


def test_unknown_window_defaults_to_balanced_not_to_the_local_profile() -> None:
    policy = _resolve(strategy="auto", base_url="https://api.example.com/v1")
    assert policy.profile_name == PROFILE_BALANCED
    assert policy.context_window is None
    assert policy.context_window_verified is False
    assert "unknown_context_window_defaults_to_balanced" in policy.reasons


def test_small_window_on_a_remote_model_is_attributed_to_capability() -> None:
    policy = _resolve(strategy="auto", base_url="https://api.example.com/v1", context_window=8192)
    assert policy.profile_name == PROFILE_LOCAL_CONSTRAINED
    assert "constrained_by_model_capability_not_local_memory" in policy.reasons


def test_explicit_quality_on_a_small_local_window_is_downgraded() -> None:
    """Forcing quality reserves onto 16K would assemble a smaller prompt."""

    policy = _resolve(
        strategy="quality",
        base_url="http://localhost:8080/v1",
        context_window=16_384,
    )
    assert policy.profile_name == PROFILE_LOCAL_CONSTRAINED
    assert "strategy_override:quality" in policy.reasons
    assert any("quality_requested_but_window" in reason for reason in policy.reasons)


def test_explicit_local_constrained_on_a_huge_window_is_honoured() -> None:
    policy = _resolve(
        strategy="local_constrained",
        base_url="https://api.moonshot.cn/v1",
        context_window=MB,
    )
    assert policy.profile_name == PROFILE_LOCAL_CONSTRAINED
    assert policy.prompt_hard_tokens == 7000


def test_explicit_balanced_on_a_million_token_model_raises_the_budget() -> None:
    policy = _resolve(strategy="balanced", base_url="https://api.moonshot.cn/v1", context_window=MB)
    assert policy.profile_name == PROFILE_BALANCED
    assert 50_000 < policy.prompt_hard_tokens < 700_000


def test_normalize_strategy_accepts_aliases_and_rejects_garbage() -> None:
    assert normalize_strategy(None) == ContextStrategy.AUTO.value
    assert normalize_strategy("QUALITY") == ContextStrategy.QUALITY.value
    assert normalize_strategy("remote_quality") == ContextStrategy.QUALITY.value
    assert normalize_strategy("local") == ContextStrategy.LOCAL_CONSTRAINED.value
    assert normalize_strategy("nonsense") == ContextStrategy.AUTO.value


# --- budget arithmetic -----------------------------------------------------


def test_effective_budget_is_min_of_capability_and_profile() -> None:
    policy = _resolve(strategy="auto", base_url="https://api.example.com/v1", context_window=65_536)
    expected_capability = (
        policy.context_window
        - policy.generation_reserve_tokens
        - policy.reasoning_reserve_tokens
        - policy.safety_reserve_tokens
    )
    assert policy.capability_budget_tokens == expected_capability
    assert policy.profile_budget_tokens == math.ceil(65_536 * BALANCED_PROFILE.prompt_budget_ratio)
    assert policy.effective_context_budget == min(
        policy.capability_budget_tokens, policy.profile_budget_tokens
    )


def test_local_ceiling_wins_over_a_capability_that_would_allow_more() -> None:
    """16384 - 1024 - 0 - 512 = 14848, but the measured safe limit is 7000."""

    policy = _resolve(
        strategy="auto", base_url="http://127.0.0.1:8080/v1", context_window=16_384
    )
    assert policy.capability_budget_tokens == 14_848
    assert policy.profile_budget_tokens == 7000
    assert policy.effective_context_budget == 7000


def test_capability_shrinks_the_budget_on_a_smaller_window() -> None:
    policy = _resolve(
        strategy="local_constrained", base_url="http://127.0.0.1:8080/v1", context_window=8192
    )
    assert policy.prompt_hard_tokens == 8192 - 1024 - 0 - 512
    assert policy.prompt_target_tokens == min(6000, policy.prompt_hard_tokens)


def test_unknown_window_uses_the_explicit_unknown_window_budget() -> None:
    policy = _resolve(
        strategy="quality", base_url="https://api.example.com/v1", context_window=None
    )
    assert policy.context_window is None
    assert policy.profile_budget_tokens == REMOTE_QUALITY_PROFILE.unknown_window_prompt_budget


def test_resolution_is_deterministic_and_side_effect_free() -> None:
    kwargs = dict(
        strategy="auto",
        base_url="https://api.example.com/v1",
        model_id="claude-sonnet-4",
        context_window=200_000,
    )
    first = _resolve(**kwargs).as_dict()
    second = _resolve(**kwargs).as_dict()
    assert first == second
    assert first["context_profile"] == PROFILE_REMOTE_QUALITY


# --- profiles --------------------------------------------------------------


def test_local_profile_from_config_reads_the_configured_tuning() -> None:
    config = Config(
        room_prompt_target_tokens=5500,
        room_prompt_hard_tokens=6500,
        room_raw_message_window=6,
        room_recall_top_k=4,
        room_memory_max_tokens=300,
        room_summary_output_max_tokens=600,
        room_max_generation_tokens=768,
    )
    profile = local_profile_from_config(config)
    assert profile.prompt_target_tokens == 5500
    assert profile.prompt_hard_tokens == 6500
    assert profile.local_measured_safe_prompt_tokens == 6500
    assert profile.recent_message_window == 6
    assert profile.recall_top_k == 4
    assert profile.memory_max_tokens == 300
    assert profile.summary_output_max_tokens == 600
    assert profile.generation_reserve_tokens == 768


def test_default_config_reproduces_the_historical_local_numbers() -> None:
    profile = local_profile_from_config(Config())
    assert profile.prompt_target_tokens == 6000
    assert profile.prompt_hard_tokens == 7000
    assert profile.recent_message_window == 8
    assert profile.recall_top_k == 8
    assert profile.memory_max_tokens == 400
    assert profile.summary_output_max_tokens == 800
    assert profile.generation_reserve_tokens == 1024


def test_local_trim_ladder_is_the_historical_six_rung_ladder() -> None:
    ladder = LOCAL_CONSTRAINED_PROFILE.trim_ladder(
        top_k=8, memory_max_tokens=400, memory_budget_tokens=2000
    )
    assert [(s.memory_limit, s.memory_max_tokens) for s in ladder] == [
        (8, 400),
        (4, 300),
        (3, 200),
        (2, 133),
        (2, 120),
        (1, 100),
    ]
    assert [(s.recent_window, s.summary_max_tokens) for s in ladder] == [
        (None, None),
        (None, None),
        (None, None),
        (6, None),
        (4, 1200),
        (2, 800),
    ]


def test_remote_quality_has_a_single_rung_and_never_degrades_memories() -> None:
    ladder = REMOTE_QUALITY_PROFILE.trim_ladder(
        top_k=48, memory_max_tokens=2000, memory_budget_tokens=24_576
    )
    assert len(ladder) == 1
    assert ladder[0].memory_limit == 48
    assert ladder[0].recent_window is None
    assert ladder[0].summary_max_tokens is None


def test_balanced_ladder_keeps_a_meaningful_memory_floor() -> None:
    ladder = BALANCED_PROFILE.trim_ladder(
        top_k=16, memory_max_tokens=800, memory_budget_tokens=6000
    )
    assert [stage.memory_limit for stage in ladder] == [16, 8, 4]
    assert all((stage.memory_limit or 0) >= 4 for stage in ladder)


def test_memory_budget_never_exceeds_the_message_allowance() -> None:
    assert REMOTE_QUALITY_PROFILE.memory_budget_for(48) == min(24_576, 48 * 2000)
    assert LOCAL_CONSTRAINED_PROFILE.memory_budget_for(8) == 2000


def test_profile_report_is_json_safe() -> None:
    for profile in (LOCAL_CONSTRAINED_PROFILE, BALANCED_PROFILE, REMOTE_QUALITY_PROFILE):
        payload = profile.as_dict()
        assert isinstance(payload, dict)
        assert payload["name"] == profile.name
        assert isinstance(payload["trim_stages"], int)
