from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from persona_continuum.agent.context_capability import (
    PLANNING_CONTEXT_WINDOW_TOKENS,
    ContextCapabilityInput,
    ContextCapabilityResolver,
    ContextCapabilitySource,
    ContextWindowMode,
    ModelCapabilityRegistry,
    compute_usable_budget,
)
from persona_continuum.numeric import safe_acp_stream_limit, safe_int, safe_timeout


def _gen_session_id() -> str:
    return "sess_" + uuid.uuid4().hex[:12]


def _gen_turn_id() -> str:
    return "turn_" + uuid.uuid4().hex[:8]


class AgentStatus(StrEnum):
    READY = "ready"
    AUTH_REQUIRED = "auth_required"
    DETECTED = "detected"
    DETECTED_UNCONTROLLABLE = "detected_uncontrollable"
    UNSUPPORTED_VERSION = "unsupported_version"
    BROKEN = "broken"
    DISABLED = "disabled"


class PermissionProfile(StrEnum):
    CHAT_SAFE = "chat_safe"
    READ_ONLY = "read_only"
    RESEARCH_READ_ONLY = "research_read_only"
    AGENT_DEFAULT = "agent_default"
    FULL = "full"
    CUSTOM = "custom"


class AgentEventType(StrEnum):
    CHUNK = "chunk"
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    DONE = "done"
    ERROR = "error"
    RAW_LOG = "raw_log"


class SelectionStrategy(StrEnum):
    STARTUP = "startup"
    RUNTIME = "runtime"
    CONFIG = "config"
    MANUAL = "manual"
    UNSUPPORTED = "unsupported"


class PromptMode(StrEnum):
    """How an adapter carries the two-part Persona Continuum prompt."""

    NATIVE_ROLES = "native_roles"
    INLINE_SYSTEM = "inline_system"
    FULL_PROMPT = "full_prompt"
    PROTOCOL_SPECIFIC = "protocol_specific"


class StructuredOutputMode(StrEnum):
    """The strongest structured-output contract an adapter can provide."""

    NATIVE_SCHEMA = "native_schema"
    JSON_MODE = "json_mode"
    TOOL_SCHEMA = "tool_schema"
    PROMPT_ONLY = "prompt_only"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class OutputStreamingMode(StrEnum):
    """How an adapter emits a turn to the shared runtime executor."""

    STREAMING = "streaming"
    BUFFERED_FINAL = "buffered_final"
    PROTOCOL_STREAM = "protocol_stream"
    UNKNOWN = "unknown"


class ReasoningCapabilityMode(StrEnum):
    NATIVE_EFFORT = "native_effort"
    PROVIDER_SPECIFIC = "provider_specific"
    MANUAL_CONFIG = "manual_config"
    DEFAULT_ONLY = "default_only"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class ReasoningCapability(BaseModel):
    """Evidence-backed reasoning control metadata for one model."""

    model_config = ConfigDict(extra="ignore")

    mode: ReasoningCapabilityMode = ReasoningCapabilityMode.UNKNOWN
    supported_efforts: list[str] = Field(default_factory=list)
    default_effort: str | None = None
    binding_strategy: str = "unknown"
    verified: bool = False
    source: str = "unknown"
    verification_error: str | None = None


class ModelCapability(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    display_name: str
    provider: str = "default"
    supported_reasoning_efforts: list[str] = Field(default_factory=list)
    default_reasoning_effort: str | None = None
    context_window: int | None = None
    selectable: bool = True
    # Evidence provenance, for example dynamic, config, official_cli,
    # protocol_model_list, official_capability_table, or manual.
    source: str = "dynamic"
    verified_for_version: str | None = None
    reasoning_selection: SelectionStrategy = SelectionStrategy.UNSUPPORTED
    reasoning_capability: ReasoningCapability = Field(default_factory=ReasoningCapability)

    @model_validator(mode="before")
    @classmethod
    def _normalise_capabilities(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if data.get("context_window") is not None:
            data["context_window"] = safe_int(
                data.get("context_window"), default=None, minimum=0, field="context_window"
            )

        # Legacy CLI adapters exposed only supported_reasoning_efforts plus a
        # selection strategy.  Provider names cannot decide whether reasoning
        # is bindable: the same Anthropic/Google model may be reached through
        # a CLI flag, ACP, or an HTTP API with entirely different contracts.
        # API adapters therefore provide their own explicit capability, while
        # this compatibility path classifies the evidence reported by CLIs.
        if data.get("reasoning_capability") is None:
            efforts = [
                str(item) for item in (data.get("supported_reasoning_efforts") or []) if item
            ]
            default_effort = data.get("default_reasoning_effort")
            source = str(data.get("source") or "unknown").lower()
            provider = str(data.get("provider") or "").lower()
            selection = str(
                data.get("reasoning_selection") or SelectionStrategy.UNSUPPORTED
            ).lower()
            native_sources = {
                "official_cli",
                "official_capability_table",
                "protocol_model_list",
            }
            if source in native_sources and selection in {
                SelectionStrategy.STARTUP.value,
                SelectionStrategy.RUNTIME.value,
            }:
                mode = (
                    ReasoningCapabilityMode.NATIVE_EFFORT
                    if efforts
                    else ReasoningCapabilityMode.DEFAULT_ONLY
                )
                strategy = (
                    "protocol_reported_reasoning_effort"
                    if source == "protocol_model_list"
                    else "native_cli_flag"
                )
                verified = bool(efforts)
            elif source in {"config", "manual"} and (efforts or default_effort):
                mode = ReasoningCapabilityMode.MANUAL_CONFIG
                strategy = "manual_config"
                verified = False
            elif provider in {"anthropic", "google", "gemini"}:
                # A provider label alone is not evidence of a working
                # provider-specific thinking binding. Explicit API
                # capabilities still fail closed in the API adapter.
                mode = ReasoningCapabilityMode.UNSUPPORTED
                strategy = "provider_specific_unbound"
                verified = False
            else:
                mode = ReasoningCapabilityMode.UNKNOWN
                strategy = "unknown"
                verified = False
            data["reasoning_capability"] = {
                "mode": mode.value,
                "supported_efforts": efforts,
                "default_effort": default_effort,
                "binding_strategy": strategy,
                "verified": verified,
                "source": source,
            }
        elif isinstance(data.get("reasoning_capability"), dict):
            capability = data["reasoning_capability"]
            if not data.get("supported_reasoning_efforts"):
                data["supported_reasoning_efforts"] = list(
                    capability.get("supported_efforts") or []
                )
            if data.get("default_reasoning_effort") is None:
                data["default_reasoning_effort"] = capability.get("default_effort")
        return data


class AgentCapabilityFlags(BaseModel):
    model_config = ConfigDict(extra="ignore")

    streaming: bool = True
    persistent_session: bool = True
    # None means the runtime did not report whether independent logical
    # sessions may execute concurrently.  Pipelines must then fail closed for
    # persistent runtimes; stateless runtimes remain independently callable.
    parallel_safe: bool | None = None
    resume_session: bool = True
    model_discovery: bool = True
    model_selection: SelectionStrategy = SelectionStrategy.STARTUP
    reasoning_discovery: bool = True
    reasoning_selection: SelectionStrategy = SelectionStrategy.STARTUP
    mcp: bool = True
    tool_calls: bool = True
    permission_control: bool = True
    cancel: bool = True
    images: bool = False
    # ``structured_output`` used to default to True, which caused business
    # code to assume every CLI accepted a schema.  Keep the legacy field
    # readable for old snapshots while making the explicit mode authoritative.
    structured_output_mode: StructuredOutputMode = StructuredOutputMode.UNKNOWN
    structured_output: bool | None = None
    auth_detection: bool = True
    # Research capabilities are explicit opt-in signals.  A model's training
    # knowledge is never treated as internet access by Persona Creation.
    web_search: bool = False
    web_fetch: bool = False
    browser: bool = False
    research_mcp: bool = False
    # How this adapter handles a context window.  Adapters declare it here so
    # business code never branches on an adapter id.
    context_window_mode: str = ContextWindowMode.UNKNOWN.value
    # Hard ceiling the adapter/runtime imposes regardless of model capability.
    adapter_context_limit: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalise_context_flags(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        mode = str(data.get("context_window_mode") or "").strip().casefold()
        if mode not in set(ContextWindowMode):
            data["context_window_mode"] = ContextWindowMode.UNKNOWN.value
        if data.get("adapter_context_limit") is not None:
            data["adapter_context_limit"] = safe_int(
                data.get("adapter_context_limit"),
                default=None,
                minimum=1,
                field="adapter_context_limit",
            )
        return data


class ResearchVerificationStatus(StrEnum):
    DECLARED = "declared"
    VERIFIED = "verified"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"


class ResearchCapability(BaseModel):
    """Capability metadata for evidence-backed web research.

    A capability declaration is a hint, not proof that the selected runtime
    can actually search and read the web.  ``verification_status`` keeps that
    distinction explicit so an unreported capability can be behaviorally
    probed instead of being rejected as unavailable.
    """

    model_config = ConfigDict(extra="ignore")

    mode: str = "none"  # native_cli, agentic_cli, broker, mcp, api_native, none
    search: bool = False
    fetch: bool = False
    # These names describe the evidence contract rather than a particular
    # vendor tool.  ``None`` preserves compatibility with older snapshots;
    # callers should use ``discovers_sources``/``reads_sources`` below.
    can_discover_sources: bool | None = None
    can_read_sources: bool | None = None
    browser: bool = False
    citations: bool = False
    live: bool = False
    source: str = "unknown"
    verification_status: ResearchVerificationStatus = ResearchVerificationStatus.UNKNOWN
    verified_at: str | None = None
    verification_method: str | None = None
    verification_error: str | None = None
    verification_error_code: str | None = None

    @property
    def discovers_sources(self) -> bool:
        """Whether this capability can discover at least one real source."""

        if self.can_discover_sources is not None:
            return bool(self.can_discover_sources)
        return bool(self.search or self.browser)

    @property
    def reads_sources(self) -> bool:
        """Whether this capability can read source content."""

        if self.can_read_sources is not None:
            return bool(self.can_read_sources)
        return bool(self.fetch or self.browser)

    @property
    def is_verified(self) -> bool:
        return self.verification_status == "verified"


class AgentProbeResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    status: AgentStatus
    binary_path: str | None = None
    version: str | None = None
    auth_status: str | None = None
    protocols: list[str] = Field(default_factory=list)
    runtime_source: str = "local_cli"  # local_cli, api, test
    definition_source: str = "builtin"  # builtin, manifest, plugin, dynamic_api
    capabilities: AgentCapabilityFlags = Field(default_factory=AgentCapabilityFlags)
    research: ResearchCapability = Field(default_factory=ResearchCapability)
    models: list[ModelCapability] = Field(default_factory=list)
    status_detail: str | None = None
    model_discovery_error: str | None = None
    # Context-window handling declared by the adapter.  Discovery copies these
    # onto the probe so no business code has to branch on an adapter id.
    context_window_mode: str = ContextWindowMode.UNKNOWN.value
    adapter_context_limit: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalise_context_declaration(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        mode = str(data.get("context_window_mode") or "").strip().casefold()
        if mode not in set(ContextWindowMode):
            data["context_window_mode"] = ContextWindowMode.UNKNOWN.value
        if data.get("adapter_context_limit") is not None:
            data["adapter_context_limit"] = safe_int(
                data.get("adapter_context_limit"),
                default=None,
                minimum=1,
                field="adapter_context_limit",
            )
        return data


class PromptEnvelope(BaseModel):
    """Lossless prompt boundary shared by every Agent transport."""

    model_config = ConfigDict(extra="ignore")

    system_prompt: str = ""
    user_message: str = ""
    # Keep the legacy composed prompt as a first-class field.  Renderers may
    # choose how to place it on the wire, but they must never lose it when a
    # caller also supplies the canonical user message.
    full_prompt: str | None = None
    messages: list[dict[str, Any]] = Field(default_factory=list)
    expected_output: Any = None
    context_budget: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalise_legacy_prompt_names(cls, value: Any) -> Any:
        """Accept the old short names without making them canonical."""

        if not isinstance(value, dict):
            return value
        data = dict(value)
        if data.get("system_prompt") is None and "system" in data:
            data["system_prompt"] = data["system"]
        if data.get("user_message") is None and "user" in data:
            data["user_message"] = data["user"]
        return data

    @property
    def system(self) -> str:
        """Compatibility view for callers written against the old envelope."""

        return self.system_prompt

    @property
    def user(self) -> str:
        """Compatibility view for callers written against the old envelope."""

        return self.user_message


class RuntimeBindingSnapshot(BaseModel):
    """What was requested and what the adapter verified at runtime."""

    model_config = ConfigDict(extra="ignore")

    agent_id: str
    protocol: str = "unknown"
    requested_model: str | None = None
    effective_model: str | None = None
    requested_reasoning: str | None = None
    effective_reasoning: str | None = None
    model_verified: bool = False
    reasoning_verified: bool = False
    binding_status: str = "unverified"
    verification_method: str | None = None
    # Context-window capability as reported by the live runtime.  ``None``
    # means "the runtime did not say", never "32768".  A discoverable runtime
    # that reports a value outranks every declarative source.
    context_window: int | None = None
    context_window_source: str | None = None
    context_window_mode: str | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalise_context_window(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if data.get("context_window") is not None:
            data["context_window"] = safe_int(
                data.get("context_window"), default=None, minimum=1, field="context_window"
            )
        return data


class AgentSessionConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str = Field(default_factory=_gen_session_id)
    room_id: str
    participant_id: str
    persona_id: str
    model_id: str | None = None
    reasoning_effort: str | None = None
    auth_profile_id: str | None = None
    auth_env_var: str | None = None
    permission_profile: PermissionProfile = PermissionProfile.CHAT_SAFE
    allow_mcp: bool = True
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
    working_dir: str | None = None
    system_prompt: str | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalise_runtime_limits(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        extra = dict(data.get("extra") or {})
        if "turn_timeout_seconds" in extra:
            if extra["turn_timeout_seconds"] is None:
                extra.pop("turn_timeout_seconds", None)
            else:
                extra["turn_timeout_seconds"] = safe_timeout(extra["turn_timeout_seconds"])
        if "acp_stream_limit_bytes" in extra:
            if extra["acp_stream_limit_bytes"] is None:
                extra.pop("acp_stream_limit_bytes", None)
            else:
                extra["acp_stream_limit_bytes"] = safe_acp_stream_limit(
                    extra["acp_stream_limit_bytes"]
                )
        data["extra"] = extra
        return data


class AgentTurn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    turn_id: str = Field(default_factory=_gen_turn_id)
    user_message: str = ""
    system_prompt: str | None = None
    prompt_mode: PromptMode | None = None
    full_prompt: str | None = None
    messages: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    expected_output: Any = None
    context_budget: dict[str, Any] = Field(default_factory=dict)
    # Resolved Adapter/Protocol transport capability for this turn (mode and
    # byte ceilings).  Kept separate from ``context_budget`` on purpose: the
    # model context window and the prompt transport budget are different
    # capabilities and must never be conflated.
    transport_capability: dict[str, Any] = Field(default_factory=dict)
    stream: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _preserve_legacy_full_prompt(cls, value: Any) -> Any:
        """Normalize legacy prompt names instead of silently discarding them.

        Older callers supplied ``full_prompt`` without a mode.  Treating that
        shape as ``FULL_PROMPT`` keeps the old request lossless while making
        the choice visible to every renderer.  A few older callers also used
        the short ``system``/``user`` names; map those to the canonical fields
        before Pydantic's ``extra=ignore`` handling can drop them.  New callers
        can still select a different mode explicitly.
        """

        if not isinstance(value, dict):
            return value
        data = dict(value)
        if data.get("system_prompt") is None and "system" in data:
            data["system_prompt"] = data["system"]
        if data.get("user_message") is None and "user" in data:
            data["user_message"] = data["user"]
        if data.get("full_prompt") is not None and data.get("prompt_mode") is None:
            data["prompt_mode"] = PromptMode.FULL_PROMPT
        return data


class AgentEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: AgentEventType
    content: str = ""
    thinking: str = ""
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments: dict[str, Any] | None = None
    tool_result: Any = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def _reasoning_mode(selected: dict[str, Any] | None) -> str:
    capability = (selected or {}).get("reasoning_capability")
    if isinstance(capability, dict):
        return str(capability.get("mode") or "unknown")
    return "unknown"


def _first_int(*values: Any) -> int | None:
    for value in values:
        normalized = safe_int(value, default=None, minimum=1)
        if normalized is not None:
            return normalized
    return None


class EffectiveModelCapabilities(BaseModel):
    """One resolved model-capability view shared by every pipeline stage.

    Stages used to guess the model themselves from a raw probe snapshot;
    because the snapshot does not name the selected model, the context
    budget manager fell back to 32K even for 256K+ models.  Resolve the
    effective model once and hand this object to every stage that needs
    context window, reasoning, structured-output, or web facts.

    Context semantics (P0 contract):

    - ``effective_context_window is None`` means **Unknown**, never 32K.
    - ``planning_context_window`` is the only place a fallback number may
      live, and it always carries ``source="fallback_policy"``.
    - ``usable_context_budget`` / ``preferred_working_context`` are computed
      from the effective window, never hardcoded.
    """

    model_config = ConfigDict(extra="ignore")

    agent_id: str | None = None
    adapter_id: str | None = None
    provider: str = "default"
    requested_model: str | None = None
    effective_model: str | None = None
    canonical_model_id: str | None = None
    # -- context capability -------------------------------------------------
    native_context_window: int | None = None
    adapter_context_limit: int | None = None
    requested_context_window: int | None = None
    effective_context_window: int | None = None
    usable_context_budget: int | None = None
    preferred_working_context: int | None = None
    planning_context_window: int | None = None
    context_window_mode: str = ContextWindowMode.UNKNOWN.value
    context_capability_source: str = ContextCapabilitySource.UNKNOWN.value
    context_verified: bool = False
    context_notes: dict[str, Any] = Field(default_factory=dict)
    # -- other capabilities -------------------------------------------------
    reasoning_mode: str = "unknown"
    reasoning_level: str | None = None
    structured_output_mode: str = "unknown"
    streaming_mode: str = "unknown"
    persistent_session: bool = False
    web_capability: bool = False

    # -- compatibility views (never fields: they must stay derived) ---------
    @property
    def context_window(self) -> int | None:
        """Deprecated alias for ``native_context_window``; may be Unknown."""

        return self.native_context_window

    @property
    def context_window_source(self) -> str:
        """Deprecated alias for ``context_capability_source``."""

        return self.context_capability_source

    @property
    def context_unknown(self) -> bool:
        return self.effective_context_window is None

    @property
    def planning_window(self) -> int:
        """A number a planner may always use (never reported as capability)."""

        if self.effective_context_window is not None:
            return int(self.effective_context_window)
        if self.planning_context_window is not None:
            return int(self.planning_context_window)
        return int(PLANNING_CONTEXT_WINDOW_TOKENS)

    def phase_usable_budget(
        self,
        *,
        output_reserve: int = 0,
        reasoning_reserve: int = 0,
        schema_reserve: int = 0,
        protocol_overhead: int = 0,
    ) -> int | None:
        if self.effective_context_window is None:
            return None
        return compute_usable_budget(
            self.effective_context_window,
            output_reserve=output_reserve,
            reasoning_reserve=reasoning_reserve,
            schema_reserve=schema_reserve,
            protocol_overhead=protocol_overhead,
        )

    @classmethod
    def resolve(
        cls,
        snapshot: dict[str, Any] | None,
        *,
        requested_model: str | None = None,
        effective_model: str | None = None,
        agent_id: str | None = None,
        adapter_id: str | None = None,
        reasoning_level: str | None = None,
        requested_context_window: int | None = None,
        runtime_reported_context_window: int | None = None,
        adapter_context_limit: int | None = None,
        user_override_context_window: int | None = None,
        # Kept for call-site compatibility.  It is a PLANNING fallback only:
        # an unresolved context window now stays None instead of becoming 32K.
        default_context_window: int | None = None,
        planning_context_window: int | None = None,
        context_window_mode: str | None = None,
        registry: ModelCapabilityRegistry | None = None,
    ) -> EffectiveModelCapabilities:
        raw = dict(snapshot or {})
        selected_id = str(effective_model or "").strip() or None
        if selected_id is None:
            selected_id = str(requested_model or "").strip() or None
        models = [item for item in (raw.get("models") or []) if isinstance(item, dict)]
        selected = next(
            (
                item
                for item in models
                if str(item.get("id") or item.get("model_id") or "") == str(selected_id or "")
            ),
            None,
        )
        if selected is None and len(models) == 1:
            # The binding may name a model under a provider-qualified id;
            # fall back to a unique declared model rather than guessing.
            selected = models[0]
        capabilities_raw = raw.get("capabilities")
        capabilities = capabilities_raw if isinstance(capabilities_raw, dict) else {}
        research_raw = raw.get("research")
        research = research_raw if isinstance(research_raw, dict) else {}
        web = bool(
            research.get("search")
            or research.get("browser")
            or research.get("fetch")
            or capabilities.get("web_search")
            or capabilities.get("browser")
        )
        binding = raw.get("runtime_binding_snapshot")
        binding_ctx = binding.get("context_window") if isinstance(binding, dict) else None
        resolved = ContextCapabilityResolver(registry).resolve(
            ContextCapabilityInput(
                agent_id=agent_id,
                adapter_id=adapter_id or str(raw.get("agent_id") or "") or None,
                requested_model=requested_model,
                effective_model=selected_id,
                requested_context_window=requested_context_window,
                runtime_reported_context_window=_first_int(
                    runtime_reported_context_window, binding_ctx
                ),
                adapter_context_limit=_first_int(
                    adapter_context_limit,
                    raw.get("adapter_context_limit"),
                    capabilities.get("adapter_context_limit"),
                ),
                context_window_mode=str(
                    context_window_mode
                    or capabilities.get("context_window_mode")
                    or ContextWindowMode.UNKNOWN.value
                ),
                model_capability=selected,
                provider_metadata=raw,
                user_override_context_window=user_override_context_window,
                planning_context_window=int(
                    planning_context_window
                    or default_context_window
                    or PLANNING_CONTEXT_WINDOW_TOKENS
                ),
            )
        )
        return cls(
            agent_id=agent_id,
            adapter_id=adapter_id or str(raw.get("agent_id") or "") or None,
            provider=resolved.provider if resolved.provider != "unknown" else str(
                (selected or {}).get("provider") or "default"
            ),
            requested_model=requested_model,
            effective_model=selected_id,
            canonical_model_id=resolved.canonical_model_id,
            native_context_window=resolved.native_context_window,
            adapter_context_limit=resolved.adapter_context_limit,
            requested_context_window=resolved.requested_context_window,
            effective_context_window=resolved.effective_context_window,
            usable_context_budget=resolved.usable_context_budget,
            preferred_working_context=resolved.preferred_working_context,
            planning_context_window=resolved.planning_context_window,
            context_window_mode=resolved.context_window_mode,
            context_capability_source=resolved.context_capability_source,
            context_verified=resolved.context_verified,
            context_notes=resolved.notes,
            reasoning_mode=_reasoning_mode(selected),
            reasoning_level=reasoning_level,
            structured_output_mode=str(capabilities.get("structured_output_mode") or "unknown"),
            streaming_mode=str(capabilities.get("streaming_mode") or "unknown"),
            persistent_session=bool(capabilities.get("persistent_session")),
            web_capability=web,
        )
