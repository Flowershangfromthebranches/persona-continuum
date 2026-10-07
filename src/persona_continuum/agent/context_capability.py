"""Adapter-agnostic context-window capability resolution.

Context window is a capability of the *execution chain* (Agent + Adapter +
Model + Runtime), not a property of one vendor.  Historically an unreported
capability silently collapsed to ``32768``, which made 256K/1M models budget
their evidence as if they were 32K models and shattered the work into dozens of
meaningless batches.

This module resolves the context window from explicitly ranked evidence and
never invents a value:

    Unknown == Unknown          (``effective_context_window is None``)

A caller that must still plan *something* uses ``planning_context_window``,
which carries ``source="fallback_policy"`` and ``verified=False`` so a planning
assumption can never be presented as a measured capability.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from persona_continuum.agent.phase_policy import (
    PREFERRED_WORKING_FLOOR_TOKENS,
    UNVERIFIED_WORKING_RATIO,
    PhaseContextPolicy,
)
from persona_continuum.numeric import safe_int

# A planning-only assumption.  It is NOT a model capability and must never be
# reported as ``effective_context_window`` or ``native_context_window``.
PLANNING_CONTEXT_WINDOW_TOKENS = 32_768
# Backwards-compatible alias for the historical constant name.
DEFAULT_CONTEXT_WINDOW_TOKENS = PLANNING_CONTEXT_WINDOW_TOKENS

# Reserve for envelope scaffolding, tokenizer drift and provider-side counting
# differences.  Scaled with the window, with a fixed floor so tiny windows keep
# a sane headroom.
SAFETY_MARGIN_RATIO = 0.05
MIN_SAFETY_MARGIN_TOKENS = 4_096
# Kept as the *unverified* planning default.  PhaseContextPolicy owns the
# per-phase working ratio; this constant must never be shown as model context.
PREFERRED_WORKING_RATIO = UNVERIFIED_WORKING_RATIO


class ContextWindowMode(StrEnum):
    """What an Adapter can do with a context window.

    Adapters declare this once; the business layer never branches on an
    adapter id.
    """

    # The runtime fixes the window; nothing needs to be passed on the wire.
    FIXED = "fixed"
    # The adapter can map a requested window onto a runtime parameter.
    CONFIGURABLE = "configurable"
    # The runtime reports the window it actually granted.
    DISCOVERABLE = "discoverable"
    CONFIGURABLE_AND_DISCOVERABLE = "configurable_and_discoverable"
    UNKNOWN = "unknown"


class ContextCapabilitySource(StrEnum):
    """Trust-ranked provenance of a resolved context window."""

    RUNTIME_REPORTED = "runtime_reported"
    RUNTIME_DYNAMIC_PROBE = "runtime_dynamic_probe"
    AGENT_MODEL_METADATA = "agent_model_metadata"
    PROVIDER_OFFICIAL_REGISTRY = "provider_official_registry"
    PROJECT_STATIC_REGISTRY = "project_static_registry"
    USER_OVERRIDE = "user_override"
    PLANNING_FALLBACK = "planning_fallback"
    INFERRED_FRESH_SESSION = "inferred_fresh_session"
    ESTIMATED_REMAINING = "estimated_remaining"
    UNKNOWN = "unknown"
    # Back-compat names.  StrEnum values must be unique, so these map through
    # :func:`canonical_context_source` rather than extra enum members.


# Highest trust first.  Never reordered without updating the resolver.
SOURCE_PRIORITY: tuple[ContextCapabilitySource, ...] = (
    ContextCapabilitySource.RUNTIME_REPORTED,
    ContextCapabilitySource.RUNTIME_DYNAMIC_PROBE,
    ContextCapabilitySource.AGENT_MODEL_METADATA,
    ContextCapabilitySource.PROVIDER_OFFICIAL_REGISTRY,
    ContextCapabilitySource.PROJECT_STATIC_REGISTRY,
    ContextCapabilitySource.USER_OVERRIDE,
)

# Only runtime, machine-readable agent metadata, and an explicit official
# registry may be marked verified.  Project guesses and planning fallbacks
# never are.
_VERIFIED_SOURCES = frozenset(
    {
        ContextCapabilitySource.RUNTIME_REPORTED,
        ContextCapabilitySource.RUNTIME_DYNAMIC_PROBE,
        ContextCapabilitySource.AGENT_MODEL_METADATA,
        ContextCapabilitySource.PROVIDER_OFFICIAL_REGISTRY,
    }
)

_SOURCE_ALIASES: dict[str, ContextCapabilitySource] = {
    "adapter_dynamic_probe": ContextCapabilitySource.RUNTIME_DYNAMIC_PROBE,
    "provider_metadata": ContextCapabilitySource.AGENT_MODEL_METADATA,
    "provider_reported": ContextCapabilitySource.AGENT_MODEL_METADATA,
    "adapter_declared": ContextCapabilitySource.AGENT_MODEL_METADATA,
    "model_registry": ContextCapabilitySource.PROJECT_STATIC_REGISTRY,
    "builtin_model_registry": ContextCapabilitySource.PROJECT_STATIC_REGISTRY,
    "fallback_policy": ContextCapabilitySource.PLANNING_FALLBACK,
    "official_capability_table": ContextCapabilitySource.PROVIDER_OFFICIAL_REGISTRY,
    "official": ContextCapabilitySource.PROVIDER_OFFICIAL_REGISTRY,
}

# ``ModelCapability.source`` values that count as evidence measured from a live
# runtime rather than a curated guess.
_PROBE_SOURCE_VALUES = frozenset(
    {"dynamic", "protocol_model_list", "official_cli", "runtime", "runtime_reported"}
)
# Machine-readable catalog / agent metadata (verified when a window is present).
_METADATA_SOURCE_VALUES = frozenset(
    {
        "provider",
        "provider_metadata",
        "agent_model_metadata",
        "protocol_model_list",
        "official_cli",
        "config_file",
    }
)
# Explicit official vendor registry (verified).
_OFFICIAL_REGISTRY_SOURCE_VALUES = frozenset(
    {
        "official",
        "official_capability_table",
        "provider_official_registry",
        "official_versioned_registry",
    }
)
# Project-owned static guesses (never verified).
_PROJECT_REGISTRY_SOURCE_VALUES = frozenset(
    {
        "builtin_model_registry",
        "model_registry",
        "project_static_registry",
        "config",
        "manual",
        "unverified_config",
    }
)


def canonical_context_source(value: Any) -> ContextCapabilitySource:
    text = str(value or "").strip().casefold()
    if not text:
        return ContextCapabilitySource.UNKNOWN
    aliased = _SOURCE_ALIASES.get(text)
    if aliased is not None:
        return aliased
    try:
        return ContextCapabilitySource(text)
    except ValueError:
        return ContextCapabilitySource.UNKNOWN


def source_is_verified(source: Any) -> bool:
    return canonical_context_source(source) in _VERIFIED_SOURCES


def canonical_model_key(model_id: Any) -> str:
    """Normalize a model identifier to one comparable canonical key.

    ``Qwen3.8-Flash``, ``qwen3.8-flash``, ``qoder/qwen3.8-flash`` and
    ``alibaba/qwen3.8-flash`` must all reach the same registry entry; otherwise
    a registry that knows 1M silently degrades to Unknown because of a prefix.
    A trailing runtime tag (``model:latest``) is stripped as well so the same
    model behind different runtime tags stays one entry.
    """

    text = str(model_id or "").strip().casefold()
    if not text:
        return ""
    # Drop any provider/adapter namespace prefix ("provider/model").
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    # Drop a trailing runtime tag ("model:latest"); only when the tag would
    # otherwise leave an empty name ("ns:model" style) keep the tagged part.
    if ":" in text:
        name, _, tag = text.partition(":")
        text = name if name.strip() else tag
    # Compare on alphanumerics only so "_"/"-"/"." spacing cannot split an
    # alias from its canonical id.
    return re.sub(r"[^0-9a-z]+", "", text)


class ModelCapabilityRecord(BaseModel):
    """One canonical model's cross-adapter capability facts.

    Adapters describe *whether they support a model* and *how they map a
    requested context onto their runtime*.  They do not each maintain a private
    copy of the model's own capability table.
    """

    model_config = ConfigDict(extra="ignore")

    canonical_model_id: str
    aliases: list[str] = Field(default_factory=list)
    provider: str = "unknown"
    native_context_window: int | None = None
    max_output_tokens: int | None = None
    supports_reasoning: bool | None = None
    supports_structured_output: bool | None = None
    source: str = "official"
    verified: bool = True
    retired: bool = False

    @model_validator(mode="before")
    @classmethod
    def _normalise(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        for field in ("native_context_window", "max_output_tokens"):
            if data.get(field) is not None:
                data[field] = safe_int(data.get(field), default=None, minimum=1, field=field)
        return data

    @property
    def keys(self) -> set[str]:
        names = {self.canonical_model_id, *self.aliases}
        return {key for key in (canonical_model_key(name) for name in names) if key}


class ModelCapabilityRegistry:
    """Cross-adapter model capability knowledge keyed by canonical model id."""

    def __init__(self) -> None:
        self._by_key: dict[str, ModelCapabilityRecord] = {}
        for record in self._builtin_records():
            self.register(record)

    def register(self, record: ModelCapabilityRecord) -> ModelCapabilityRecord:
        for key in record.keys:
            self._by_key[key] = record
        return record

    def lookup(self, model_id: Any) -> ModelCapabilityRecord | None:
        key = canonical_model_key(model_id)
        if not key:
            return None
        return self._by_key.get(key)

    def canonical_id(self, model_id: Any) -> str:
        record = self.lookup(model_id)
        return record.canonical_model_id if record is not None else str(model_id or "")

    def native_context_window(self, model_id: Any) -> int | None:
        record = self.lookup(model_id)
        return record.native_context_window if record is not None else None

    def snapshot(self) -> dict[str, Any]:
        records = {id(record): record for record in self._by_key.values()}
        return {
            "model_count": len(records),
            "models": sorted(
                {
                    record.canonical_model_id
                    for record in records.values()
                    if record.native_context_window is not None
                }
            ),
        }

    @staticmethod
    def _builtin_records() -> list[ModelCapabilityRecord]:
        """Curated cross-adapter model facts.

        Entries are only here when the model has a documented native context
        window.  Adapters that report a measured value always outrank this
        table, so a curated entry can never mask what a runtime actually
        granted.
        """

        def record(
            canonical: str,
            *aliases: str,
            provider: str,
            window: int,
            max_output: int | None = None,
            reasoning: bool | None = True,
            structured: bool | None = True,
            official: bool = True,
            retired: bool = False,
        ) -> ModelCapabilityRecord:
            return ModelCapabilityRecord(
                canonical_model_id=canonical,
                aliases=list(aliases),
                provider=provider,
                native_context_window=window,
                max_output_tokens=max_output,
                supports_reasoning=reasoning,
                supports_structured_output=structured,
                source="official" if official else "builtin_model_registry",
                verified=bool(official),
                retired=retired,
            )

        return [
            # --- Qoder / Alibaba Qwen catalog -------------------------------
            record(
                "qwen3.8-max",
                "qwen3-8-max",
                "Qwen3.8-Max",
                provider="alibaba",
                window=1_048_576,
                max_output=65_536,
            ),
            record(
                "qwen3.8-flash",
                "qwen3-8-flash",
                "Qwen3.8-Flash",
                provider="alibaba",
                window=1_048_576,
                max_output=65_536,
            ),
            record(
                "qwen3.7-max",
                "Qwen3.7-Max",
                provider="alibaba",
                window=1_048_576,
                max_output=65_536,
            ),
            record(
                "qwen3.7-plus",
                "Qwen3.7-Plus",
                provider="alibaba",
                window=1_048_576,
                max_output=65_536,
            ),
            record(
                "qwen3.7-flash",
                "Qwen3.7-Flash",
                provider="alibaba",
                window=1_048_576,
                max_output=65_536,
            ),
            # --- DeepSeek ----------------------------------------------------
            record(
                "deepseek-v4-pro",
                "DeepSeek-V4-Pro",
                "deepseek-v4-pro",
                provider="deepseek",
                window=1_048_576,
                max_output=65_536,
            ),
            record(
                "deepseek-v4-flash",
                "DeepSeek-V4-Flash",
                provider="deepseek",
                window=1_048_576,
                max_output=65_536,
            ),
            # --- Zhipu GLM ---------------------------------------------------
            record(
                "glm-5.3",
                "GLM-5.3",
                "glm-5-3",
                provider="zhipu",
                window=1_048_576,
                max_output=65_536,
            ),
            record(
                "glm-5.3-flash",
                "GLM-5.3-Flash",
                provider="zhipu",
                window=1_048_576,
                max_output=65_536,
            ),
            record(
                "glm-5.2",
                "GLM-5.2",
                provider="zhipu",
                window=1_048_576,
                max_output=65_536,
            ),
            # --- Moonshot Kimi -----------------------------------------------
            record(
                "kimi-k2.7-code",
                "Kimi-K2.7-Code",
                "kimi-k2-7-code",
                provider="moonshot",
                window=1_048_576,
                max_output=65_536,
            ),
            record(
                "kimi-k2.5",
                "Kimi-K2.5",
                provider="moonshot",
                window=1_048_576,
                max_output=65_536,
            ),
            # --- MiniMax ------------------------------------------------------
            record(
                "minimax-m2.7",
                "MiniMax-M2.7",
                provider="minimax",
                window=1_048_576,
                max_output=65_536,
            ),
            record(
                "minimax-m2",
                "MiniMax-M2",
                provider="minimax",
                window=1_048_576,
                max_output=65_536,
            ),
            # --- Google Gemini ------------------------------------------------
            record(
                "gemini-3-pro",
                "gemini-3-pro-preview",
                provider="google",
                window=1_048_576,
                max_output=65_536,
            ),
            record(
                "gemini-3-flash",
                "gemini-3-flash-preview",
                provider="google",
                window=1_048_576,
                max_output=65_536,
            ),
            record(
                "gemini-2.5-pro",
                "gemini-2.5-pro-preview",
                provider="google",
                window=1_048_576,
                max_output=65_536,
            ),
            record(
                "gemini-2.5-flash",
                "gemini-2.5-flash-preview",
                provider="google",
                window=1_048_576,
                max_output=65_536,
            ),
            # --- xAI Grok -----------------------------------------------------
            # Official Grok 4.6 context is 500K.  Runtime-reported
            # modelUsage.*.contextWindow always outranks this fallback.
            record(
                "grok-4.6",
                "grok-4",
                "ocx-xai-grok-4-6",
                provider="xai",
                window=500_000,
                max_output=32_768,
            ),
            record(
                "grok-4.5",
                provider="xai",
                window=500_000,
                max_output=32_768,
            ),
            record(
                "grok-4-fast",
                provider="xai",
                window=131_072,
                max_output=32_768,
            ),
            record(
                "grok-code-fast-1",
                provider="xai",
                window=131_072,
                max_output=32_768,
            ),
            # --- OpenAI -------------------------------------------------------
            # These are *API native* windows.  Codex / Copilot wrappers must
            # never treat them as the wrapper runtime's effective context.
            record(
                "gpt-5.6-sol",
                "gpt-5-6-sol",
                "ocx-gpt-5-6-sol",
                provider="openai",
                window=1_050_000,
                max_output=128_000,
            ),
            record(
                "gpt-5.6-terra",
                "gpt-5-6-terra",
                "ocx-gpt-5-6-terra",
                provider="openai",
                window=1_050_000,
                max_output=128_000,
            ),
            record(
                "gpt-5.6-luna",
                "gpt-5-6-luna",
                "ocx-gpt-5-6-luna",
                provider="openai",
                window=1_050_000,
                max_output=128_000,
            ),
            record(
                "gpt-5.4",
                provider="openai",
                window=1_050_000,
                max_output=128_000,
            ),
            record(
                "gpt-5",
                "gpt-5.1",
                "gpt-5.2",
                provider="openai",
                window=400_000,
                max_output=128_000,
            ),
            record(
                "gpt-5-codex",
                provider="openai",
                window=400_000,
                max_output=128_000,
            ),
            record(
                "gpt-5-mini",
                provider="openai",
                window=400_000,
                max_output=128_000,
            ),
            # --- Anthropic ----------------------------------------------------
            record(
                "claude-sonnet-5",
                "claude-sonnet-4.6",
                "claude-sonnet-4-6",
                provider="anthropic",
                window=1_000_000,
                max_output=128_000,
            ),
            record(
                "claude-opus-5",
                "claude-opus-4.6",
                "claude-opus-4-6",
                "claude-opus-4-6-thinking",
                provider="anthropic",
                window=1_000_000,
                max_output=128_000,
            ),
            record(
                "claude-haiku-4.5",
                "claude-haiku-4-5",
                provider="anthropic",
                window=200_000,
                max_output=64_000,
            ),
            record(
                "claude-opus-4.5",
                "claude-opus-4-5",
                provider="anthropic",
                window=200_000,
                max_output=64_000,
                retired=True,
            ),
            record(
                "claude-sonnet-4.5",
                "claude-sonnet-4-5",
                provider="anthropic",
                window=200_000,
                max_output=64_000,
                retired=True,
            ),
            record(
                "claude-3-7-sonnet-20250219",
                "claude-3.7-sonnet",
                provider="anthropic",
                window=200_000,
                max_output=64_000,
                retired=True,
            ),
            record(
                "claude-3-5-haiku-20241022",
                "claude-3.5-haiku",
                provider="anthropic",
                window=200_000,
                max_output=8_192,
                retired=True,
            ),
            # --- Moonshot Kimi (K3 advertised 1M) -----------------------------
            record(
                "kimi-k3",
                "kimi-k3-1",
                provider="moonshot",
                window=1_048_576,
                max_output=65_536,
            ),
        ]


_DEFAULT_REGISTRY: ModelCapabilityRegistry | None = None


def default_model_capability_registry() -> ModelCapabilityRegistry:
    """Process-wide shared registry (adapters may register extra models)."""

    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = ModelCapabilityRegistry()
    return _DEFAULT_REGISTRY


class ContextCapabilityInput(BaseModel):
    """Everything the resolver is allowed to look at, in one place."""

    model_config = ConfigDict(extra="ignore")

    agent_id: str | None = None
    adapter_id: str | None = None
    requested_model: str | None = None
    effective_model: str | None = None
    # Explicitly requested window (may be None/Auto).
    requested_context_window: int | None = None
    # What the runtime actually granted for the live session.
    runtime_reported_context_window: int | None = None
    runtime_reported_remaining_context: int | None = None
    runtime_reported_used_context: int | None = None
    # Stored leftover from a previous turn.  Never verified.
    estimated_remaining_context: int | None = None
    runtime_session_id: str | None = None
    # Hard ceiling imposed by the adapter/runtime.
    adapter_context_limit: int | None = None
    # What the adapter declares about the context window.
    context_window_mode: str = ContextWindowMode.UNKNOWN.value
    # One ModelCapability-shaped dict (the selected model).
    model_capability: dict[str, Any] | None = None
    # Whole probe snapshot; used only for adapter-level declarations.
    provider_metadata: dict[str, Any] | None = None
    # A validated user override (e.g. an explicit config setting).
    user_override_context_window: int | None = None
    user_override_verified: bool = False
    persistent_session: bool = False
    session_used_tokens: int | None = None
    max_output_tokens: int | None = None
    upstream_context_is_native_only: bool = False
    # Planning-only assumption; never reported as a capability.
    planning_context_window: int = PLANNING_CONTEXT_WINDOW_TOKENS

    @model_validator(mode="before")
    @classmethod
    def _coerce_windows(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        for field in (
            "requested_context_window",
            "runtime_reported_context_window",
            "adapter_context_limit",
            "user_override_context_window",
            "planning_context_window",
            "max_output_tokens",
        ):
            if data.get(field) is not None:
                data[field] = safe_int(data.get(field), default=None, minimum=1, field=field)
        for field in (
            "runtime_reported_remaining_context",
            "runtime_reported_used_context",
            "estimated_remaining_context",
            "session_used_tokens",
        ):
            if data.get(field) is not None:
                data[field] = safe_int(data.get(field), default=None, minimum=0, field=field)
        return data


class ResolvedContextCapability(BaseModel):
    """The resolved context capability of one execution chain.

    ``effective_context_window is None`` means Unknown.  Callers must never
    coerce it to a number; use ``planning_context_window`` when a plan needs a
    number and surface ``context_verified=False`` to the user.
    """

    model_config = ConfigDict(extra="ignore")

    native_context_window: int | None = None
    adapter_context_limit: int | None = None
    requested_context_window: int | None = None
    effective_context_window: int | None = None
    remaining_context_tokens: int | None = None
    remaining_context_verified: bool = False
    remaining_context_source: str = ContextCapabilitySource.UNKNOWN.value
    max_output_tokens: int | None = None
    usable_context_budget: int | None = None
    preferred_working_context: int | None = None
    phase_working_target: int | None = None
    context_capability_source: str = ContextCapabilitySource.UNKNOWN.value
    context_capability_source_detail: str | None = None
    context_verified: bool = False
    context_window_mode: str = ContextWindowMode.UNKNOWN.value
    planning_context_window: int | None = PLANNING_CONTEXT_WINDOW_TOKENS
    canonical_model_id: str | None = None
    model_id: str | None = None
    adapter: str | None = None
    runtime_session_id: str | None = None
    provider: str = "unknown"
    resolved_at: str | None = None
    notes: dict[str, Any] = Field(default_factory=dict)

    # -- compatibility aliases -------------------------------------------------
    @property
    def context_window(self) -> int | None:
        """Alias for the native/reported model window (may be Unknown)."""

        return self.native_context_window

    @property
    def context_window_source(self) -> str:
        """Alias kept for callers written against the old field name."""

        return self.context_capability_source

    @property
    def is_unknown(self) -> bool:
        return self.effective_context_window is None

    @property
    def planning_window(self) -> int:
        """A usable number for planning, even when the capability is Unknown."""

        if self.effective_context_window is not None:
            return int(self.effective_context_window)
        if self.planning_context_window is not None:
            return int(self.planning_context_window)
        return PLANNING_CONTEXT_WINDOW_TOKENS

    def phase_budget(
        self,
        *,
        output_reserve: int = 0,
        reasoning_reserve: int = 0,
        schema_reserve: int = 0,
        protocol_overhead: int = 0,
    ) -> int | None:
        """Hard usable budget for one phase, or ``None`` when Unknown."""

        if self.effective_context_window is None:
            return None
        return compute_usable_budget(
            self.effective_context_window,
            output_reserve=output_reserve,
            reasoning_reserve=reasoning_reserve,
            schema_reserve=schema_reserve,
            protocol_overhead=protocol_overhead,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "native_context_window": self.native_context_window,
            "adapter_context_limit": self.adapter_context_limit,
            "requested_context_window": self.requested_context_window,
            "effective_context_window": self.effective_context_window,
            "remaining_context_tokens": self.remaining_context_tokens,
            "remaining_context_verified": self.remaining_context_verified,
            "remaining_context_source": self.remaining_context_source,
            "max_output_tokens": self.max_output_tokens,
            "usable_context_budget": self.usable_context_budget,
            "preferred_working_context": self.preferred_working_context,
            "phase_working_target": self.phase_working_target,
            "context_capability_source": self.context_capability_source,
            "context_capability_source_detail": self.context_capability_source_detail,
            "context_verified": self.context_verified,
            "context_window_mode": self.context_window_mode,
            "planning_context_window": self.planning_context_window,
            "canonical_model_id": self.canonical_model_id,
            "model_id": self.model_id,
            "adapter": self.adapter,
            "runtime_session_id": self.runtime_session_id,
            "provider": self.provider,
            "resolved_at": self.resolved_at,
            "notes": dict(self.notes),
        }


def compute_usable_budget(
    effective_context_window: int,
    *,
    output_reserve: int = 0,
    reasoning_reserve: int = 0,
    schema_reserve: int = 0,
    protocol_overhead: int = 0,
) -> int:
    """Hard usable budget = effective - safety margin - phase reserves.

    Deliberately computed rather than hardcoded: a 1M model must land in the
    ~900K+ range while a 32K model keeps a proportionally sane headroom.
    """

    window = max(1, safe_int(effective_context_window, default=1, minimum=1) or 1)
    safety_margin = max(
        MIN_SAFETY_MARGIN_TOKENS,
        int(math.ceil(window * SAFETY_MARGIN_RATIO)),
    )
    reserves = (
        max(0, int(output_reserve))
        + max(0, int(reasoning_reserve))
        + max(0, int(schema_reserve))
        + max(0, int(protocol_overhead))
    )
    return max(0, window - safety_margin - reserves)


def compute_preferred_working_context(
    effective_context_window: int | None,
    usable_context_budget: int | None = None,
    *,
    ratio: float | None = None,
    phase: str | None = None,
    verified: bool = False,
    persistent_session: bool = False,
    fresh_stateless: bool = True,
    policy: PhaseContextPolicy | None = None,
) -> int | None:
    """Phase working target, never a second model context limit.

    When ``ratio`` / ``phase`` are omitted this uses the unverified 50%
    planning default so old callers keep a conservative number.  New
    pipelines must pass a :class:`PhaseContextPolicy`.
    """

    if effective_context_window is None:
        return None
    if policy is not None:
        return policy.working_target(
            effective_context_window,
            phase=phase,
            usable_budget=usable_context_budget,
            verified=verified,
            persistent_session=persistent_session,
            fresh_stateless=fresh_stateless,
        )
    window = max(1, safe_int(effective_context_window, default=1, minimum=1) or 1)
    used_ratio = PREFERRED_WORKING_RATIO if ratio is None else float(ratio)
    preferred = max(
        PREFERRED_WORKING_FLOOR_TOKENS,
        int(math.ceil(window * used_ratio)),
    )
    if usable_context_budget is not None:
        preferred = min(preferred, max(0, int(usable_context_budget)))
    return max(0, preferred)


class ContextCapabilityResolver:
    """Resolve one execution chain's context capability from ranked evidence."""

    def __init__(self, registry: ModelCapabilityRegistry | None = None) -> None:
        self.registry = registry or default_model_capability_registry()

    def resolve(self, data: ContextCapabilityInput) -> ResolvedContextCapability:
        payload = (
            data
            if isinstance(data, ContextCapabilityInput)
            else ContextCapabilityInput.model_validate(dict(data or {}))
        )
        model = payload.model_capability if isinstance(payload.model_capability, dict) else {}
        provider_meta = (
            payload.provider_metadata if isinstance(payload.provider_metadata, dict) else {}
        )
        model_id = str(
            payload.effective_model
            or payload.requested_model
            or model.get("id")
            or model.get("model_id")
            or ""
        ).strip() or None
        record = self.registry.lookup(model_id) if model_id else None
        notes: dict[str, Any] = {}
        if record is not None and record.retired:
            notes["retired_model"] = record.canonical_model_id

        model_source = self._model_window_source(model)
        meta_source = (
            ContextCapabilitySource.RUNTIME_DYNAMIC_PROBE
            if self._metadata_is_probe(provider_meta)
            else self._metadata_source(provider_meta)
        )
        registry_source = (
            self._registry_source(record) if record is not None else ContextCapabilitySource.UNKNOWN
        )
        registry_window = record.native_context_window if record is not None else None
        registry_verified = bool(
            record is not None
            and record.verified
            and registry_source in _VERIFIED_SOURCES
        )

        # Ranked effective-context candidates.  User override is applied as a
        # constraint afterwards and is never allowed to beat a runtime value.
        candidates: list[tuple[ContextCapabilitySource, int | None, bool, str]] = [
            (
                ContextCapabilitySource.RUNTIME_REPORTED,
                payload.runtime_reported_context_window,
                True,
                "runtime_reported_context_window",
            ),
            (
                model_source,
                self._model_reported_window(model),
                model_source in _VERIFIED_SOURCES,
                f"model_capability.source={model.get('source')}",
            ),
            (
                meta_source,
                self._declared_window(provider_meta),
                meta_source in _VERIFIED_SOURCES and self._declared_verified(provider_meta),
                "provider_metadata",
            ),
            (
                registry_source,
                None if payload.upstream_context_is_native_only else registry_window,
                registry_verified,
                f"registry[{record.canonical_model_id}]" if record is not None else "registry",
            ),
        ]

        chosen_source = ContextCapabilitySource.UNKNOWN
        chosen_value: int | None = None
        chosen_verified = False
        source_detail: str | None = None
        for source, value, verified, detail in candidates:
            normalized = safe_int(value, default=None, minimum=1)
            if normalized is None:
                continue
            chosen_source = (
                source if source in SOURCE_PRIORITY else ContextCapabilitySource.UNKNOWN
            )
            chosen_value = normalized
            chosen_verified = bool(verified) and chosen_source in _VERIFIED_SOURCES
            source_detail = detail
            break

        native = self._native_window(model, record, provider_meta, chosen_value, chosen_source)
        effective = chosen_value
        requested = payload.requested_context_window or payload.user_override_context_window
        adapter_limit = payload.adapter_context_limit
        mode = str(payload.context_window_mode or ContextWindowMode.UNKNOWN.value)
        configurable = mode in {
            ContextWindowMode.CONFIGURABLE.value,
            ContextWindowMode.CONFIGURABLE_AND_DISCOVERABLE.value,
        }

        # User / requested window: downscale is always safe.  Upscale is only
        # a request until a CONFIGURABLE runtime verifies the larger window.
        if requested is not None:
            notes["requested_context_window"] = requested
            if effective is not None and requested < effective:
                effective = requested
                notes["requested_context_applied"] = True
                if chosen_source == ContextCapabilitySource.RUNTIME_REPORTED:
                    notes["user_downscale_of_runtime"] = True
            elif effective is not None and requested > effective:
                notes["requested_upscale_ignored"] = {
                    "requested": requested,
                    "runtime_or_resolved": effective,
                    "configurable": configurable,
                }
            elif effective is None and configurable:
                effective = requested
                chosen_source = ContextCapabilitySource.USER_OVERRIDE
                chosen_verified = False
                source_detail = "configurable_requested_unverified"
                notes["requested_context_applied"] = True
            elif effective is None and payload.user_override_context_window is not None:
                effective = payload.user_override_context_window
                chosen_source = ContextCapabilitySource.USER_OVERRIDE
                chosen_verified = False
                source_detail = "user_override_unverified"
                notes["requested_context_applied"] = True

        if effective is not None and adapter_limit is not None and adapter_limit < effective:
            notes["adapter_context_limit_applied"] = {
                "requested_or_resolved": effective,
                "adapter_context_limit": adapter_limit,
            }
            effective = adapter_limit

        if (
            payload.runtime_reported_context_window is not None
            and native is not None
            and payload.runtime_reported_context_window != native
        ):
            notes["runtime_vs_native"] = {
                "native_context_window": native,
                "runtime_reported": payload.runtime_reported_context_window,
            }
            if payload.runtime_reported_context_window < native:
                notes["runtime_narrowed_native"] = notes["runtime_vs_native"]

        remaining, remaining_verified, remaining_source = self._remaining(
            payload, effective, chosen_verified
        )
        hard_limit = remaining if remaining is not None else effective
        usable = compute_usable_budget(hard_limit) if hard_limit is not None else None
        # Capability layer does not invent a phase working target.  Callers
        # that still read preferred_working_context get the unverified
        # conservative default so they cannot mistake it for native context.
        preferred = compute_preferred_working_context(
            hard_limit,
            usable,
            verified=False,
            persistent_session=payload.persistent_session,
        )
        max_output = safe_int(
            payload.max_output_tokens
            or model.get("max_output_tokens")
            or (record.max_output_tokens if record is not None else None),
            default=None,
            minimum=1,
        )
        return ResolvedContextCapability(
            native_context_window=native,
            adapter_context_limit=adapter_limit,
            requested_context_window=requested,
            effective_context_window=effective,
            remaining_context_tokens=remaining,
            remaining_context_verified=remaining_verified,
            remaining_context_source=remaining_source.value,
            max_output_tokens=max_output,
            usable_context_budget=usable,
            preferred_working_context=preferred,
            context_capability_source=chosen_source.value,
            context_capability_source_detail=source_detail,
            context_verified=bool(effective is not None and chosen_verified),
            context_window_mode=mode,
            planning_context_window=(
                int(payload.planning_context_window or PLANNING_CONTEXT_WINDOW_TOKENS)
            ),
            canonical_model_id=record.canonical_model_id if record is not None else model_id,
            model_id=model_id,
            adapter=payload.adapter_id or payload.agent_id,
            runtime_session_id=payload.runtime_session_id,
            provider=str(
                (model.get("provider") if isinstance(model, dict) else "")
                or (record.provider if record is not None else "")
                or "unknown"
            ),
            resolved_at=datetime.now(UTC).isoformat(),
            notes=notes,
        )

    @staticmethod
    def _native_window(
        model: dict[str, Any],
        record: ModelCapabilityRecord | None,
        provider_meta: dict[str, Any],
        chosen: int | None,
        chosen_source: ContextCapabilitySource,
    ) -> int | None:
        """Model-native window, independent of this chain's effective limit.

        Runtime reported values never hide a documented native window, but a
        stale project table cannot keep Native at 128K when the live runtime
        already reported 500K.
        """

        official = record.native_context_window if record is not None else None
        model_window = safe_int(model.get("context_window"), default=None, minimum=1)
        declared = ContextCapabilityResolver._declared_window(provider_meta)
        if chosen_source == ContextCapabilitySource.RUNTIME_REPORTED and chosen is not None:
            if official is None or (
                record is not None
                and canonical_context_source(record.source)
                == ContextCapabilitySource.PROJECT_STATIC_REGISTRY
            ):
                return chosen
            if official is not None and chosen > official:
                return chosen
        native = model_window or official or declared or chosen
        return safe_int(native, default=None, minimum=1)

    @staticmethod
    def _remaining(
        payload: ContextCapabilityInput,
        effective: int | None,
        effective_verified: bool,
    ) -> tuple[int | None, bool, ContextCapabilitySource]:
        reported = safe_int(payload.runtime_reported_remaining_context, default=None, minimum=0)
        if reported is not None:
            return reported, True, ContextCapabilitySource.RUNTIME_REPORTED
        estimated = safe_int(payload.estimated_remaining_context, default=None, minimum=0)
        if estimated is not None:
            return estimated, False, ContextCapabilitySource.ESTIMATED_REMAINING
        used = safe_int(
            payload.runtime_reported_used_context or payload.session_used_tokens,
            default=None,
            minimum=0,
        )
        if effective is not None and used is not None:
            return (
                max(0, int(effective) - int(used)),
                False,
                ContextCapabilitySource.ESTIMATED_REMAINING,
            )
        if effective is not None and not payload.persistent_session:
            return effective, False, ContextCapabilitySource.INFERRED_FRESH_SESSION
        return None, False, ContextCapabilitySource.UNKNOWN

    @staticmethod
    def _model_reported_window(model: dict[str, Any] | None) -> int | None:
        if not isinstance(model, dict):
            return None
        return safe_int(model.get("context_window"), default=None, minimum=1)

    @staticmethod
    def _model_window_source(model: dict[str, Any] | None) -> ContextCapabilitySource:
        """Classify a ModelCapability-reported window by how it was obtained."""

        if not isinstance(model, dict) or model.get("context_window") is None:
            return ContextCapabilitySource.UNKNOWN
        source = str(model.get("source") or "").casefold()
        if source in _PROBE_SOURCE_VALUES:
            return ContextCapabilitySource.RUNTIME_DYNAMIC_PROBE
        if source in _OFFICIAL_REGISTRY_SOURCE_VALUES:
            return ContextCapabilitySource.PROVIDER_OFFICIAL_REGISTRY
        if source in _PROJECT_REGISTRY_SOURCE_VALUES:
            return ContextCapabilitySource.PROJECT_STATIC_REGISTRY
        if source in _METADATA_SOURCE_VALUES:
            return ContextCapabilitySource.AGENT_MODEL_METADATA
        return ContextCapabilitySource.AGENT_MODEL_METADATA

    @staticmethod
    def _registry_source(record: ModelCapabilityRecord) -> ContextCapabilitySource:
        return canonical_context_source(record.source or "official")

    @staticmethod
    def _metadata_source(provider_meta: dict[str, Any]) -> ContextCapabilitySource:
        source = str(
            provider_meta.get("context_window_source")
            or provider_meta.get("context_capability_source")
            or ""
        )
        if source:
            return canonical_context_source(source)
        return ContextCapabilitySource.AGENT_MODEL_METADATA

    @staticmethod
    def _declared_window(provider_meta: dict[str, Any]) -> int | None:
        from persona_continuum.agent.context_fields import extract_context_window

        found = extract_context_window(provider_meta)
        if found is not None:
            return found
        for key in ("context_window", "effective_context_window", "adapter_context_window"):
            value = safe_int(provider_meta.get(key), default=None, minimum=1)
            if value is not None:
                return value
        capabilities = provider_meta.get("capabilities")
        if isinstance(capabilities, dict):
            value = safe_int(capabilities.get("context_window"), default=None, minimum=1)
            if value is not None:
                return value
        return None

    @staticmethod
    def _metadata_is_probe(provider_meta: dict[str, Any]) -> bool:
        source = str(provider_meta.get("context_window_source") or "").casefold()
        return source in _PROBE_SOURCE_VALUES

    @staticmethod
    def _declared_verified(provider_meta: dict[str, Any]) -> bool:
        source = canonical_context_source(
            provider_meta.get("context_window_source")
            or provider_meta.get("context_capability_source")
        )
        return source in _VERIFIED_SOURCES


__all__ = [
    "DEFAULT_CONTEXT_WINDOW_TOKENS",
    "PLANNING_CONTEXT_WINDOW_TOKENS",
    "ContextCapabilityInput",
    "ContextCapabilityResolver",
    "ContextCapabilitySource",
    "ContextWindowMode",
    "ModelCapabilityRecord",
    "ModelCapabilityRegistry",
    "ResolvedContextCapability",
    "SOURCE_PRIORITY",
    "canonical_context_source",
    "canonical_model_key",
    "compute_preferred_working_context",
    "compute_usable_budget",
    "default_model_capability_registry",
    "source_is_verified",
]
