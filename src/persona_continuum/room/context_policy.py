"""Context Policy: how much of a model's capability one room turn may use.

Memory semantics decide **what a persona remembers**; context policy decides
**what this particular model can see this turn**.  The two used to be the same
constants: budgets measured on a 24 GiB M3 running a local 27B were applied to
every provider, so a 1M-context API model was assembled a ~6K prompt and then
*refused* above 7K.  A resource limit of one machine had become a product-wide
quality policy.

This module owns the policy half only:

* :class:`ContextProfile` -- a named, model-independent budget shape.
* :data:`LOCAL_CONSTRAINED_PROFILE` -- the measured-safe profile for locally
  hosted, memory-bound inference.  Its numbers stay exactly what the previous
  global constants were, and they are still read from ``Config`` so the local
  tuning remains the user's.
* :data:`BALANCED_PROFILE` / :data:`REMOTE_QUALITY_PROFILE` -- profiles whose
  budgets scale with the model's real capability instead of with this laptop.
* :func:`resolve_context_policy` -- ``auto`` resolution from provider,
  context window, endpoint locality and model capability.

Nothing here reads or writes a memory store.  Switching profiles changes only
the assembly of the current prompt.
"""

from __future__ import annotations

import ipaddress
import math
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from persona_continuum.numeric import safe_int

# --- strategy surface ------------------------------------------------------

PROFILE_LOCAL_CONSTRAINED = "local_constrained"
PROFILE_BALANCED = "balanced"
PROFILE_REMOTE_QUALITY = "remote_quality"


class ContextStrategy(StrEnum):
    """User-facing strategy selector.  ``AUTO`` is the default."""

    AUTO = "auto"
    QUALITY = "quality"
    BALANCED = "balanced"
    LOCAL_CONSTRAINED = "local_constrained"


STRATEGY_TO_PROFILE: dict[str, str] = {
    ContextStrategy.QUALITY.value: PROFILE_REMOTE_QUALITY,
    ContextStrategy.BALANCED.value: PROFILE_BALANCED,
    ContextStrategy.LOCAL_CONSTRAINED.value: PROFILE_LOCAL_CONSTRAINED,
}

#: Windows at or above this are treated as "remote-quality capable".
REMOTE_QUALITY_FLOOR_TOKENS = 131_072
#: Windows at or below this on a locally hosted endpoint stay constrained.
LOCAL_ENDPOINT_CEILING_TOKENS = 32_768
#: Below this a window cannot carry a balanced prompt at all.
BALANCED_FLOOR_TOKENS = 32_768

DEFAULT_LOCAL_GENERATION_RESERVE_TOKENS = 1024
DEFAULT_LOCAL_SUMMARY_OUTPUT_MAX_TOKENS = 800

_LOCAL_HOSTNAMES = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "::1",
        "0.0.0.0",
        "host.docker.internal",
        "host.containers.internal",
    }
)
_LOCAL_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".lan")


def endpoint_is_local(base_url: str | None) -> bool:
    """Whether an API base URL points at this machine (or the local network).

    Only an endpoint can be "local" in the memory-bound sense.  A CLI adapter
    runs locally but usually talks to a remote model, so CLI-ness is never a
    locality signal -- that mistake is what made a 1M-context CLI model look
    like a 24 GiB Mac.
    """

    if not base_url:
        return False
    raw = str(base_url).strip()
    if not raw:
        return False
    if "://" not in raw:
        raw = f"http://{raw}"
    try:
        host = urlsplit(raw).hostname
    except ValueError:
        return False
    if not host:
        return False
    host = host.strip().strip("[]").casefold()
    if not host:
        return False
    if host in _LOCAL_HOSTNAMES:
        return True
    if host.endswith(_LOCAL_HOST_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(address.is_loopback or address.is_private or address.is_link_local)


# --- profile shape ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrimStage:
    """One rung of the degradation ladder.

    A stage is only ever used when the assembled prompt still exceeds the
    profile's soft target.  ``local_constrained`` keeps the historical six-rung
    ladder verbatim; ``remote_quality`` has a single rung, so a large model is
    never degraded for being large.
    """

    memory_limit: int | None
    memory_max_tokens: int
    recent_window: int | None = None
    summary_max_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ContextProfile:
    """A budget shape keyed to a model tier, never to an individual model."""

    name: str
    description: str
    #: Soft planning target.  ``None`` means "the whole effective budget".
    prompt_target_tokens: int | None
    #: Explicit hard ceiling.  ``None`` means "the effective budget".
    prompt_hard_tokens: int | None
    #: Share of the context window one turn may use when no explicit ceiling.
    prompt_budget_ratio: float
    #: Floor for a ratio-derived budget (never below this for a capable model).
    min_prompt_floor_tokens: int
    #: Budget used when the window is Unknown and the strategy was explicit.
    unknown_window_prompt_budget: int
    #: PRIMARY working-memory unit.  A one-word "嗯" and a 3000-character essay
    #: are both one message, so message counts cannot be the main cap.
    recent_dialogue_token_budget: int
    #: Secondary cap / fallback for the message-count contract.
    recent_message_window: int
    recall_top_k: int
    #: Per-memory render cap.
    memory_max_tokens: int
    #: Total budget for the whole retrieved-memory block.
    memory_budget_tokens: int
    summary_max_chars: int
    summary_output_max_tokens: int
    summary_input_max_tokens: int
    generation_reserve_tokens: int
    reasoning_reserve_tokens: int
    #: Provenance-backed raw excerpt expansion (memory -> source_turn_ids).
    historical_excerpt_token_budget: int
    historical_excerpt_messages: int
    #: True when this profile exists because of *this machine's* memory.
    local_resource_bound: bool
    #: Measured safe prompt size for locally hosted inference (Metal OOM).
    local_measured_safe_prompt_tokens: int | None
    trim_stages: tuple[TrimStage, ...] = field(default=())

    def trim_ladder(
        self, *, top_k: int, memory_max_tokens: int, memory_budget_tokens: int
    ) -> tuple[TrimStage, ...]:
        """Degradation ladder for this profile.

        ``local_constrained`` returns the historical ladder unchanged.  Wider
        profiles return far fewer rungs, and their ``memory_limit`` never drops
        below a floor that keeps the memory system meaningful.
        """

        if self.name == PROFILE_LOCAL_CONSTRAINED:
            return _local_trim_ladder(top_k, memory_max_tokens)
        if self.name == PROFILE_BALANCED:
            return (
                TrimStage(top_k, memory_max_tokens),
                TrimStage(
                    max(4, top_k // 2),
                    max(400, memory_max_tokens // 2),
                ),
                TrimStage(
                    max(2, top_k // 4),
                    max(300, memory_max_tokens // 3),
                ),
            )
        return (TrimStage(top_k, memory_max_tokens),)

    def memory_budget_for(self, top_k: int) -> int:
        """Total memory-block budget, never above one message-count allowance."""

        return max(0, min(self.memory_budget_tokens, top_k * self.memory_max_tokens))

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "prompt_target_tokens": self.prompt_target_tokens,
            "prompt_hard_tokens": self.prompt_hard_tokens,
            "prompt_budget_ratio": self.prompt_budget_ratio,
            "recent_dialogue_token_budget": self.recent_dialogue_token_budget,
            "recent_message_window": self.recent_message_window,
            "recall_top_k": self.recall_top_k,
            "memory_max_tokens": self.memory_max_tokens,
            "memory_budget_tokens": self.memory_budget_tokens,
            "summary_max_chars": self.summary_max_chars,
            "summary_output_max_tokens": self.summary_output_max_tokens,
            "summary_input_max_tokens": self.summary_input_max_tokens,
            "generation_reserve_tokens": self.generation_reserve_tokens,
            "reasoning_reserve_tokens": self.reasoning_reserve_tokens,
            "historical_excerpt_token_budget": self.historical_excerpt_token_budget,
            "historical_excerpt_messages": self.historical_excerpt_messages,
            "local_resource_bound": self.local_resource_bound,
            "local_measured_safe_prompt_tokens": self.local_measured_safe_prompt_tokens,
            "trim_stages": len(
                self.trim_ladder(
                    top_k=self.recall_top_k,
                    memory_max_tokens=self.memory_max_tokens,
                    memory_budget_tokens=self.memory_budget_tokens,
                )
            ),
        }


def _local_trim_ladder(top_k: int, memory_max_tokens: int) -> tuple[TrimStage, ...]:
    """The historical ladder, preserved exactly.

    These literals are what the local 27B was stress-tested against; changing
    them would change the only configuration currently known to be Metal-safe.
    """

    return (
        TrimStage(top_k, memory_max_tokens),
        TrimStage(max(3, top_k // 2), max(200, memory_max_tokens * 3 // 4)),
        TrimStage(3, max(160, memory_max_tokens // 2)),
        TrimStage(2, max(120, memory_max_tokens // 3), recent_window=6),
        TrimStage(2, 120, recent_window=4, summary_max_tokens=1200),
        TrimStage(1, 100, recent_window=2, summary_max_tokens=800),
    )


# --- profile catalogue -----------------------------------------------------


LOCAL_CONSTRAINED_PROFILE = ContextProfile(
    name=PROFILE_LOCAL_CONSTRAINED,
    description=(
        "Locally hosted, memory-bound inference (MLX / llama.cpp / Ollama on "
        "unified memory).  Prompt size is limited by Metal allocation, not by "
        "the model's declared context window."
    ),
    prompt_target_tokens=6000,
    prompt_hard_tokens=7000,
    prompt_budget_ratio=0.45,
    min_prompt_floor_tokens=4096,
    unknown_window_prompt_budget=7000,
    recent_dialogue_token_budget=1500,
    recent_message_window=8,
    recall_top_k=8,
    memory_max_tokens=400,
    memory_budget_tokens=2000,
    summary_max_chars=2400,
    summary_output_max_tokens=DEFAULT_LOCAL_SUMMARY_OUTPUT_MAX_TOKENS,
    summary_input_max_tokens=4096,
    generation_reserve_tokens=DEFAULT_LOCAL_GENERATION_RESERVE_TOKENS,
    reasoning_reserve_tokens=0,
    historical_excerpt_token_budget=0,
    historical_excerpt_messages=0,
    local_resource_bound=True,
    local_measured_safe_prompt_tokens=7000,
)

BALANCED_PROFILE = ContextProfile(
    name=PROFILE_BALANCED,
    description=(
        "Mid-tier models: 14B-class local inference, machines with headroom, "
        "ordinary API models and medium-context CLIs.  Comfortably wider than "
        "the local survival profile without assuming a frontier window."
    ),
    prompt_target_tokens=None,
    prompt_hard_tokens=None,
    prompt_budget_ratio=0.55,
    min_prompt_floor_tokens=16_384,
    unknown_window_prompt_budget=32_768,
    recent_dialogue_token_budget=6144,
    recent_message_window=32,
    recall_top_k=16,
    memory_max_tokens=800,
    memory_budget_tokens=6000,
    summary_max_chars=8000,
    summary_output_max_tokens=2000,
    summary_input_max_tokens=16_384,
    generation_reserve_tokens=2048,
    reasoning_reserve_tokens=2048,
    historical_excerpt_token_budget=0,
    historical_excerpt_messages=0,
    local_resource_bound=False,
    local_measured_safe_prompt_tokens=None,
)

REMOTE_QUALITY_PROFILE = ContextProfile(
    name=PROFILE_REMOTE_QUALITY,
    description=(
        "128K / 256K / 1M remote models that are not bound by this machine's "
        "unified memory.  Quality first: long recent dialogue, many memories, "
        "rich summaries and provenance-backed raw excerpts."
    ),
    prompt_target_tokens=None,
    prompt_hard_tokens=None,
    prompt_budget_ratio=0.70,
    # A remote_quality prompt is never assembled small just because 6000/7000
    # used to be a global constant.
    min_prompt_floor_tokens=65_536,
    unknown_window_prompt_budget=131_072,
    recent_dialogue_token_budget=32_768,
    recent_message_window=256,
    recall_top_k=48,
    memory_max_tokens=2000,
    memory_budget_tokens=24_576,
    summary_max_chars=32_000,
    summary_output_max_tokens=8000,
    summary_input_max_tokens=98_304,
    generation_reserve_tokens=4096,
    reasoning_reserve_tokens=8192,
    historical_excerpt_token_budget=12_000,
    historical_excerpt_messages=6,
    local_resource_bound=False,
    local_measured_safe_prompt_tokens=None,
)

PROFILES: dict[str, ContextProfile] = {
    PROFILE_LOCAL_CONSTRAINED: LOCAL_CONSTRAINED_PROFILE,
    PROFILE_BALANCED: BALANCED_PROFILE,
    PROFILE_REMOTE_QUALITY: REMOTE_QUALITY_PROFILE,
}


def _config_int(config: Any, name: str, *, default: int, minimum: int) -> int:
    """Read one optional ``Config`` knob; a missing/invalid value uses default."""

    raw = getattr(config, name, None)
    if raw is None:
        return default
    converted = safe_int(raw, default=None, minimum=minimum)
    return default if converted is None else int(converted)


def local_profile_from_config(config: Any) -> ContextProfile:
    """Local profile whose numbers still come from ``Config``.

    The local tuning is the only configuration that has been stress-tested on
    the 24 GiB M3, so it must stay the user's to set -- it just stops being
    everyone else's default.
    """

    if config is None:
        return LOCAL_CONSTRAINED_PROFILE
    base = LOCAL_CONSTRAINED_PROFILE
    target = _config_int(
        config,
        "room_prompt_target_tokens",
        default=base.prompt_target_tokens or 6000,
        minimum=1024,
    )
    hard = max(
        target,
        _config_int(
            config,
            "room_prompt_hard_tokens",
            default=base.prompt_hard_tokens or 7000,
            minimum=1024,
        ),
    )
    return replace(
        base,
        prompt_target_tokens=target,
        prompt_hard_tokens=hard,
        unknown_window_prompt_budget=hard,
        local_measured_safe_prompt_tokens=hard,
        recent_message_window=_config_int(
            config,
            "room_raw_message_window",
            default=base.recent_message_window,
            minimum=4,
        ),
        recall_top_k=_config_int(
            config, "room_recall_top_k", default=base.recall_top_k, minimum=1
        ),
        memory_max_tokens=_config_int(
            config,
            "room_memory_max_tokens",
            default=base.memory_max_tokens,
            minimum=0,
        ),
        summary_max_chars=_config_int(
            config,
            "room_summary_max_chars",
            default=base.summary_max_chars,
            minimum=400,
        ),
        summary_output_max_tokens=_config_int(
            config,
            "room_summary_output_max_tokens",
            default=base.summary_output_max_tokens,
            minimum=100,
        ),
        summary_input_max_tokens=_config_int(
            config,
            "room_summary_input_max_tokens",
            default=base.summary_input_max_tokens,
            minimum=512,
        ),
        generation_reserve_tokens=_config_int(
            config,
            "room_max_generation_tokens",
            default=base.generation_reserve_tokens,
            minimum=1,
        ),
    )


# --- resolution ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContextPolicyRequest:
    """Everything the resolver is allowed to know.  No memory, no transcript."""

    strategy: str = ContextStrategy.AUTO.value
    adapter_id: str = ""
    #: ``openai_compatible_api`` / ``local_cli`` / ``test`` / ``unknown``.
    runtime_source: str = "unknown"
    base_url: str | None = None
    model_id: str | None = None
    context_window: int | None = None
    context_window_source: str = "unknown"
    context_verified: bool = False
    #: ``True`` when the operator declared this endpoint memory-bound even
    #: though the base URL is not obviously loopback (e.g. a LAN inference box).
    local_memory_constrained: bool | None = None
    local_profile: ContextProfile = LOCAL_CONSTRAINED_PROFILE


@dataclass(frozen=True, slots=True)
class ResolvedContextPolicy:
    """The policy actually used for one turn, plus why it was chosen."""

    requested_strategy: str
    profile: ContextProfile
    reasons: tuple[str, ...]
    local_endpoint: bool
    context_window: int | None
    context_window_source: str
    context_window_verified: bool
    generation_reserve_tokens: int
    reasoning_reserve_tokens: int
    safety_reserve_tokens: int
    capability_budget_tokens: int | None
    profile_budget_tokens: int
    effective_context_budget: int
    prompt_target_tokens: int
    prompt_hard_tokens: int
    local_safety_ceiling_applied: bool
    context_capacity_ceiling: int = 7000
    context_target_budget: int = 6000
    context_hard_budget: int = 7000

    @property
    def profile_name(self) -> str:
        return self.profile.name

    @property
    def capacity_ceiling(self) -> int:
        return self.context_capacity_ceiling

    @property
    def target_budget(self) -> int:
        return self.context_target_budget

    @property
    def hard_budget(self) -> int:
        return self.context_hard_budget

    def as_dict(self) -> dict[str, Any]:
        return {
            "context_strategy_requested": self.requested_strategy,
            "context_profile": self.profile.name,
            "context_profile_reasons": list(self.reasons),
            "local_endpoint": self.local_endpoint,
            "context_window": self.context_window,
            "context_window_source": self.context_window_source,
            "context_window_verified": self.context_window_verified,
            "generation_reserve_tokens": self.generation_reserve_tokens,
            "reasoning_reserve_tokens": self.reasoning_reserve_tokens,
            "safety_reserve_tokens": self.safety_reserve_tokens,
            "capability_budget_tokens": self.capability_budget_tokens,
            "profile_budget_tokens": self.profile_budget_tokens,
            "effective_context_budget": self.effective_context_budget,
            "prompt_target_tokens": self.prompt_target_tokens,
            "prompt_hard_tokens": self.prompt_hard_tokens,
            "local_safety_ceiling_applied": self.local_safety_ceiling_applied,
            "context_capacity_ceiling": self.context_capacity_ceiling,
            "context_target_budget": self.context_target_budget,
            "context_hard_budget": self.context_hard_budget,
            "capacity_ceiling": self.context_capacity_ceiling,
            "target_budget": self.context_target_budget,
            "hard_budget": self.context_hard_budget,
        }


def normalize_strategy(value: Any) -> str:
    """Accept the four public names plus a few historical aliases."""

    raw = str(value or "").strip().casefold()
    aliases = {
        "": ContextStrategy.AUTO.value,
        "none": ContextStrategy.AUTO.value,
        "default": ContextStrategy.AUTO.value,
        "remote_quality": ContextStrategy.QUALITY.value,
        "remote": ContextStrategy.QUALITY.value,
        "q": ContextStrategy.QUALITY.value,
        "local": ContextStrategy.LOCAL_CONSTRAINED.value,
        "constrained": ContextStrategy.LOCAL_CONSTRAINED.value,
    }
    raw = aliases.get(raw, raw)
    if raw in {
        ContextStrategy.AUTO.value,
        ContextStrategy.QUALITY.value,
        ContextStrategy.BALANCED.value,
        ContextStrategy.LOCAL_CONSTRAINED.value,
    }:
        return raw
    return ContextStrategy.AUTO.value


def _registry_window(model_id: str | None) -> int | None:
    if not model_id:
        return None
    try:
        from persona_continuum.agent.context_capability import (
            default_model_capability_registry,
        )

        return default_model_capability_registry().native_context_window(model_id)
    except Exception:  # noqa: BLE001 - a missing registry must not change policy
        return None


def _safety_reserve(window: int | None) -> int:
    if window is None:
        return 1024
    return max(512, min(32_768, window // 40))


def _reserves(profile: ContextProfile, window: int | None) -> tuple[int, int]:
    """Generation/reasoning reserves, never larger than the window can host."""

    generation = max(0, profile.generation_reserve_tokens)
    reasoning = max(0, profile.reasoning_reserve_tokens)
    if window is not None:
        share = max(256, window // 4)
        generation = min(generation, share)
        reasoning = min(reasoning, share)
    return generation, reasoning


def _auto_profile(
    *, window: int | None, local_endpoint: bool
) -> tuple[ContextProfile, list[str]]:
    if window is None:
        if local_endpoint:
            return LOCAL_CONSTRAINED_PROFILE, [
                "local_endpoint_with_unknown_context_window"
            ]
        return BALANCED_PROFILE, ["unknown_context_window_defaults_to_balanced"]
    if local_endpoint and window <= LOCAL_ENDPOINT_CEILING_TOKENS:
        return LOCAL_CONSTRAINED_PROFILE, [
            f"local_endpoint_and_window_{window}_at_or_below_"
            f"{LOCAL_ENDPOINT_CEILING_TOKENS}"
        ]
    if window >= REMOTE_QUALITY_FLOOR_TOKENS:
        return REMOTE_QUALITY_PROFILE, [
            f"context_window_{window}_at_or_above_{REMOTE_QUALITY_FLOOR_TOKENS}"
        ]
    if local_endpoint:
        return (
            BALANCED_PROFILE,
            [f"local_endpoint_with_large_window_{window}"],
        )
    if window >= BALANCED_FLOOR_TOKENS:
        return BALANCED_PROFILE, [f"context_window_{window}"]
    return LOCAL_CONSTRAINED_PROFILE, [
        f"context_window_{window}_below_{BALANCED_FLOOR_TOKENS}"
    ]


def _clamp_requested(
    profile: ContextProfile,
    *,
    window: int | None,
    local_endpoint: bool,
) -> tuple[ContextProfile, list[str]]:
    """Downgrade an explicit request that the capability cannot honour.

    Upward moves are always honoured when the window supports them; a request
    that would assemble a *smaller* prompt than the constrained profile (e.g.
    forcing quality reserves of 4K+8K onto a 16K window) is downgraded instead,
    with the reason recorded.
    """

    if profile.name == PROFILE_LOCAL_CONSTRAINED:
        return profile, []
    if window is None:
        if local_endpoint:
            return LOCAL_CONSTRAINED_PROFILE, [
                f"{profile.name}_requested_on_local_endpoint_with_unknown_window"
            ]
        return profile, []
    if profile.name == PROFILE_REMOTE_QUALITY and window < REMOTE_QUALITY_FLOOR_TOKENS:
        if window >= BALANCED_FLOOR_TOKENS:
            return BALANCED_PROFILE, [
                f"quality_requested_but_window_{window}_below_{REMOTE_QUALITY_FLOOR_TOKENS}"
            ]
        return LOCAL_CONSTRAINED_PROFILE, [
            f"quality_requested_but_window_{window}_below_{BALANCED_FLOOR_TOKENS}"
        ]
    if profile.name == PROFILE_BALANCED and window < BALANCED_FLOOR_TOKENS:
        return LOCAL_CONSTRAINED_PROFILE, [
            f"balanced_requested_but_window_{window}_below_{BALANCED_FLOOR_TOKENS}"
        ]
    return profile, []


def resolve_context_policy(request: ContextPolicyRequest) -> ResolvedContextPolicy:
    """Resolve one turn's context policy.  Pure function, no side effects."""

    requested = normalize_strategy(request.strategy)
    window = safe_int(request.context_window, default=None, minimum=1)
    window_source = str(request.context_window_source or "unknown")
    verified = bool(request.context_verified) and window is not None
    if window is None:
        registry_window = _registry_window(request.model_id)
        if registry_window is not None:
            window = registry_window
            window_source = "provider_official_registry"
            verified = True
        else:
            window_source = "unknown"

    local_endpoint = bool(
        request.local_memory_constrained is True
        or (request.local_memory_constrained is not False and endpoint_is_local(request.base_url))
    )

    reasons: list[str] = []
    if requested == ContextStrategy.AUTO.value:
        profile, auto_reasons = _auto_profile(window=window, local_endpoint=local_endpoint)
        reasons.extend(auto_reasons)
    else:
        profile = PROFILES[STRATEGY_TO_PROFILE[requested]]
        reasons.append(f"strategy_override:{requested}")
        profile, downgrade_reasons = _clamp_requested(
            profile, window=window, local_endpoint=local_endpoint
        )
        reasons.extend(downgrade_reasons)
        if profile.name == PROFILE_LOCAL_CONSTRAINED and requested != (
            ContextStrategy.LOCAL_CONSTRAINED.value
        ):
            reasons.append("downgraded_to_local_constrained")

    if profile.name == PROFILE_LOCAL_CONSTRAINED and request.local_profile is not None:
        # The local tuning is the stress-tested one and stays the operator's to
        # set, so the config-derived profile replaces the module constant.
        profile = request.local_profile

    if profile.name == PROFILE_LOCAL_CONSTRAINED and (
        local_endpoint is False and requested == ContextStrategy.AUTO.value
    ):
        # A small window on a remote model is a capability limit, not this
        # machine's memory limit.  Recorded so the report never blames the Mac.
        reasons.append("constrained_by_model_capability_not_local_memory")

    generation_reserve, reasoning_reserve = _reserves(profile, window)
    safety_reserve = _safety_reserve(window)

    if window is None:
        capability_budget: int | None = None
        profile_budget = max(0, profile.unknown_window_prompt_budget)
    else:
        capability_budget = max(
            0, window - generation_reserve - reasoning_reserve - safety_reserve
        )
        if profile.prompt_hard_tokens is not None:
            profile_budget = max(0, profile.prompt_hard_tokens)
        else:
            profile_budget = max(
                profile.min_prompt_floor_tokens,
                int(math.ceil(window * profile.prompt_budget_ratio)),
            )
            profile_budget = min(profile_budget, max(0, window))

    budget = profile_budget if capability_budget is None else min(profile_budget, capability_budget)

    local_ceiling_applied = False
    ceiling = profile.local_measured_safe_prompt_tokens
    if profile.name == PROFILE_LOCAL_CONSTRAINED and ceiling is not None:
        budget = min(budget, ceiling)
        local_ceiling_applied = True

    budget = max(0, budget)
    target = budget if profile.prompt_target_tokens is None else min(
        profile.prompt_target_tokens, budget
    )

    # Phase 8: Capacity Ceiling vs Target Budget formal separation
    if profile.name == PROFILE_LOCAL_CONSTRAINED:
        capacity_ceiling = ceiling or 7000
        hard_budget = budget
        target_budget = max(0, target)
    elif profile.name == PROFILE_REMOTE_QUALITY:
        capacity_ceiling = capability_budget if capability_budget is not None else 700_000
        hard_budget = capacity_ceiling
        target_budget = min(hard_budget, max(32_768, profile.min_prompt_floor_tokens))
    else:  # BALANCED
        capacity_ceiling = capability_budget if capability_budget is not None else 65_536
        hard_budget = budget
        target_budget = min(hard_budget, max(16_384, int(math.ceil(hard_budget * 0.7))))

    return ResolvedContextPolicy(
        requested_strategy=requested,
        profile=profile,
        reasons=tuple(reasons),
        local_endpoint=local_endpoint,
        context_window=window,
        context_window_source=window_source,
        context_window_verified=verified,
        generation_reserve_tokens=generation_reserve,
        reasoning_reserve_tokens=reasoning_reserve,
        safety_reserve_tokens=safety_reserve,
        capability_budget_tokens=capability_budget,
        profile_budget_tokens=profile_budget,
        effective_context_budget=budget,
        prompt_target_tokens=max(0, target),
        prompt_hard_tokens=budget,
        local_safety_ceiling_applied=local_ceiling_applied,
        context_capacity_ceiling=capacity_ceiling,
        context_target_budget=target_budget,
        context_hard_budget=hard_budget,
    )


__all__ = [
    "BALANCED_FLOOR_TOKENS",
    "BALANCED_PROFILE",
    "ContextPolicyRequest",
    "ContextProfile",
    "ContextStrategy",
    "LOCAL_CONSTRAINED_PROFILE",
    "LOCAL_ENDPOINT_CEILING_TOKENS",
    "PROFILES",
    "PROFILE_BALANCED",
    "PROFILE_LOCAL_CONSTRAINED",
    "PROFILE_REMOTE_QUALITY",
    "REMOTE_QUALITY_FLOOR_TOKENS",
    "REMOTE_QUALITY_PROFILE",
    "ResolvedContextPolicy",
    "STRATEGY_TO_PROFILE",
    "TrimStage",
    "endpoint_is_local",
    "local_profile_from_config",
    "normalize_strategy",
    "resolve_context_policy",
]
