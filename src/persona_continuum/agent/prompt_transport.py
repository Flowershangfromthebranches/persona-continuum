"""Prompt transport capability: how much prompt an execution chain can carry.

Model context window and prompt transport are two different capabilities and
must never be conflated:

    Model Context      = how much the MODEL can hold (e.g. 1,048,576 tokens)
    Working Context    = how much one phase should handle in a single pass
    Transport Limit    = how much the ADAPTER/PROTOCOL can safely carry

A 1M model behind an ARGV-only CLI that accepts a 100KB shell argument has a
*transport* budget of ~100KB even though its model context is 1M.  Treating
those as one number is what produced single Analysis Windows close to the whole
1M window: the planner saw ``effective_context_window=1048576`` and packed
~780K tokens of evidence into one prompt.

Adapters declare their transport shape once.  The business layer reads the
resolved capability and never branches on an adapter id or tool name:

    if agent == "qoder": batch = ...      # FORBIDDEN
    target = min(usable, preferred, transport)   # correct
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from persona_continuum.numeric import safe_int

# --- Conservative transport ceilings ---------------------------------------
#
# These are NOT model capabilities.  They describe how much data the *carrier*
# (shell argv, stdin pipe, HTTP request body, JSON-RPC frame) can move safely.
# They are deliberately conservative: a prompt that is borderline at the
# transport layer hangs or is truncated instead of failing loudly.

# macOS ARG_MAX is 1MB total for argv+env; Linux commonly 2MB but with a
# 128KB limit for a *single* argument (MAX_ARG_STRLEN).  A prompt passed as
# one argv element must therefore stay well under that single-argument cap.
DEFAULT_ARGV_INLINE_PROMPT_BYTES = 64 * 1024
DEFAULT_ARGV_MAX_PROMPT_BYTES = 64 * 1024
# stdin is a pipe: no single-argument cap, but keep a sane ceiling so a
# runaway prompt is still detected before it is written.
DEFAULT_STDIN_MAX_PROMPT_BYTES = 8 * 1024 * 1024
# A file-based handoff is bounded by the temp filesystem, not by the shell.
DEFAULT_FILE_MAX_PROMPT_BYTES = 64 * 1024 * 1024
# RPC/HTTP request bodies are bounded by the server's request limit.
DEFAULT_RPC_MAX_PROMPT_BYTES = 32 * 1024 * 1024
DEFAULT_STREAM_MAX_PROMPT_BYTES = 32 * 1024 * 1024
# Unknown transport: assume nothing, keep the smallest safe ceiling.
DEFAULT_UNKNOWN_MAX_PROMPT_BYTES = 64 * 1024

# Bytes-per-token used only to translate a *transport byte* ceiling into a
# planning token ceiling.  This is a planning ratio, not a tokenizer: it is
# deliberately pessimistic because CJK material costs more than 1 byte/token.
TRANSPORT_BYTES_PER_TOKEN = 4
# Leave headroom for envelope scaffolding, JSON escaping and envelope headers
# that are added after the evidence payload has been measured.
TRANSPORT_SAFETY_MARGIN_RATIO = 0.10
MIN_TRANSPORT_SAFETY_BYTES = 8 * 1024


class PromptTransportMode(StrEnum):
    """How a prompt physically reaches the model runtime."""

    # In-process HTTP/JSON-RPC/SDK call; no shell argument involved.
    RPC = "rpc"
    # Prompt is written to the child process's stdin pipe.
    STDIN = "stdin"
    # Prompt is handed over through a temporary file path.
    FILE = "file"
    # Prompt is passed as a command-line argument (``-p <prompt>``).
    ARGV = "argv"
    # Long-lived bidirectional stream (ACP / vendor app-server session).
    STREAM = "stream"
    # No adapter declared a transport mode.
    UNKNOWN = "unknown"


#: Transports that are safe for very large prompts.
_LARGE_PROMPT_MODES = frozenset(
    {
        PromptTransportMode.RPC,
        PromptTransportMode.STDIN,
        PromptTransportMode.FILE,
        PromptTransportMode.STREAM,
    }
)

_DEFAULT_MAX_PROMPT_BYTES: dict[str, int] = {
    PromptTransportMode.RPC.value: DEFAULT_RPC_MAX_PROMPT_BYTES,
    PromptTransportMode.STDIN.value: DEFAULT_STDIN_MAX_PROMPT_BYTES,
    PromptTransportMode.FILE.value: DEFAULT_FILE_MAX_PROMPT_BYTES,
    PromptTransportMode.ARGV.value: DEFAULT_ARGV_MAX_PROMPT_BYTES,
    PromptTransportMode.STREAM.value: DEFAULT_STREAM_MAX_PROMPT_BYTES,
    PromptTransportMode.UNKNOWN.value: DEFAULT_UNKNOWN_MAX_PROMPT_BYTES,
}


class PromptTransportCapability(BaseModel):
    """What one Adapter/Protocol can safely carry in a single prompt.

    This is an **Adapter/Protocol** capability, never a business-layer
    constant: the planner reads it and derives the batch target from it.
    """

    model_config = ConfigDict(extra="ignore")

    transport_mode: str = PromptTransportMode.UNKNOWN.value
    max_prompt_bytes: int = DEFAULT_UNKNOWN_MAX_PROMPT_BYTES
    # Ceiling for a prompt embedded directly in the command line.  Only
    # meaningful for ARGV transports; equal to ``max_prompt_bytes`` otherwise.
    max_inline_prompt_bytes: int = DEFAULT_UNKNOWN_MAX_PROMPT_BYTES
    supports_large_prompt: bool = False
    supports_stdin: bool = False
    supports_file_input: bool = False
    supports_streaming: bool = False
    # Where the capability came from (adapter declaration vs. inferred default).
    source: str = "adapter_default"

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        mode = _normalise_mode(data.get("transport_mode"))
        data["transport_mode"] = mode
        default_max = _DEFAULT_MAX_PROMPT_BYTES.get(mode, DEFAULT_UNKNOWN_MAX_PROMPT_BYTES)
        for field in ("max_prompt_bytes", "max_inline_prompt_bytes"):
            parsed = safe_int(data.get(field), default=None, minimum=1)
            data[field] = int(parsed) if parsed is not None else default_max
        return data

    @model_validator(mode="after")
    def _derive(self) -> PromptTransportCapability:
        mode = _normalise_mode(self.transport_mode)
        default_max = _DEFAULT_MAX_PROMPT_BYTES.get(mode, DEFAULT_UNKNOWN_MAX_PROMPT_BYTES)
        # An inline ceiling can never exceed the transport ceiling, and an
        # ARGV transport is always bounded by the single-argument cap.
        effective_max = max(1, int(self.max_prompt_bytes or default_max))
        if mode == PromptTransportMode.ARGV.value:
            effective_max = min(effective_max, DEFAULT_ARGV_MAX_PROMPT_BYTES)
            inline = max(1, int(self.max_inline_prompt_bytes or effective_max))
            self.max_inline_prompt_bytes = min(inline, effective_max)
        else:
            self.max_inline_prompt_bytes = effective_max
        self.max_prompt_bytes = effective_max
        self.transport_mode = mode
        self.supports_large_prompt = mode in _LARGE_PROMPT_MODES
        self.supports_stdin = mode in {
            PromptTransportMode.STDIN.value,
            PromptTransportMode.STREAM.value,
        } or bool(self.supports_stdin)
        self.supports_file_input = mode == PromptTransportMode.FILE.value or bool(
            self.supports_file_input
        )
        self.supports_streaming = mode in {
            PromptTransportMode.STREAM.value,
            PromptTransportMode.RPC.value,
        } or bool(self.supports_streaming)
        return self

    # -- derived planning views ---------------------------------------------
    @property
    def safe_prompt_bytes(self) -> int:
        """The byte ceiling a planner must respect for this transport."""

        ceiling = int(self.max_prompt_bytes)
        margin = max(
            MIN_TRANSPORT_SAFETY_BYTES,
            int(ceiling * TRANSPORT_SAFETY_MARGIN_RATIO),
        )
        return max(1, ceiling - margin)

    def prompt_token_budget(self, *, bytes_per_token: int = TRANSPORT_BYTES_PER_TOKEN) -> int:
        """Translate the byte ceiling into a conservative token ceiling."""

        ratio = max(1, int(bytes_per_token or TRANSPORT_BYTES_PER_TOKEN))
        return max(1, self.safe_prompt_bytes // ratio)

    def allows_bytes(self, value: int) -> bool:
        return int(value) <= self.safe_prompt_bytes

    def as_dict(self) -> dict[str, Any]:
        return {
            "transport_mode": self.transport_mode,
            "max_prompt_bytes": self.max_prompt_bytes,
            "max_inline_prompt_bytes": self.max_inline_prompt_bytes,
            "supports_large_prompt": self.supports_large_prompt,
            "supports_stdin": self.supports_stdin,
            "supports_file_input": self.supports_file_input,
            "supports_streaming": self.supports_streaming,
            "safe_prompt_bytes": self.safe_prompt_bytes,
            "prompt_token_budget": self.prompt_token_budget(),
            "source": self.source,
        }


def _normalise_mode(value: Any) -> str:
    text = str(value or "").strip().casefold()
    if not text:
        return PromptTransportMode.UNKNOWN.value
    aliases = {
        "argv": PromptTransportMode.ARGV.value,
        "arg": PromptTransportMode.ARGV.value,
        "cli_arg": PromptTransportMode.ARGV.value,
        "stdin": PromptTransportMode.STDIN.value,
        "pipe": PromptTransportMode.STDIN.value,
        "file": PromptTransportMode.FILE.value,
        "rpc": PromptTransportMode.RPC.value,
        "http": PromptTransportMode.RPC.value,
        "api": PromptTransportMode.RPC.value,
        "stream": PromptTransportMode.STREAM.value,
        "streaming": PromptTransportMode.STREAM.value,
    }
    if text in aliases:
        return aliases[text]
    try:
        return PromptTransportMode(text).value
    except ValueError:
        return PromptTransportMode.UNKNOWN.value


def capability_for_mode(
    mode: str | PromptTransportMode, *, source: str = "adapter_default", **overrides: Any
) -> PromptTransportCapability:
    """Build a capability for a known transport mode."""

    normalised = _normalise_mode(mode)
    payload: dict[str, Any] = {"transport_mode": normalised, "source": str(source)}
    payload.update(overrides)
    return PromptTransportCapability.model_validate(payload)


def default_prompt_transport_capability() -> PromptTransportCapability:
    """Conservative fallback: assume the weakest carrier until told otherwise."""

    return capability_for_mode(PromptTransportMode.UNKNOWN, source="transport_default")


def resolve_prompt_transport_capability(adapter: Any) -> PromptTransportCapability:
    """Resolve one Adapter's declared prompt transport capability.

    An adapter declares its transport explicitly (``prompt_transport`` /
    ``prompt_transport_mode``).  When it does not, the shape of the adapter is
    inspected *structurally* -- never by name -- so a new CLI adapter is
    classified as ARGV because it carries print-mode flags, not because
    somebody hardcoded its id.
    """

    declared = _declared_capability(adapter)
    if declared is not None:
        return declared
    return _infer_from_adapter_shape(adapter)


def _declared_capability(adapter: Any) -> PromptTransportCapability | None:
    for attribute in (
        "prompt_transport_capability",
        "prompt_transport",
        "prompt_transport_mode",
    ):
        value = getattr(adapter, attribute, None)
        if isinstance(value, PromptTransportCapability):
            return value
        if isinstance(value, dict):
            try:
                return PromptTransportCapability.model_validate(value)
            except (TypeError, ValueError):
                continue
        if isinstance(value, str) and value.strip():
            return capability_for_mode(value, source="adapter_declared")
    return None


def _infer_from_adapter_shape(adapter: Any) -> PromptTransportCapability:
    """Infer a transport mode from structural adapter facts (never from ids)."""

    # An explicit stdin flag means the adapter can take the prompt as a pipe
    # rather than as a shell argument.
    if bool(getattr(adapter, "supports_stdin_prompt", False)):
        return capability_for_mode(
            PromptTransportMode.STDIN, source="adapter_shape_stdin_flag"
        )
    # ``-p``/``--print`` style flags put the prompt on the command line.
    exec_args = getattr(adapter, "exec_args", None)
    if isinstance(exec_args, (list, tuple)):
        flags = {str(arg).strip() for arg in exec_args}
        if flags.intersection({"-p", "--print", "--prompt"}):
            return capability_for_mode(
                PromptTransportMode.ARGV, source="adapter_shape_print_flag"
            )
    # A protocol without a subprocess boundary carries the prompt in-process.
    protocol = str(getattr(adapter, "protocol", "") or "").strip().casefold()
    if protocol in {"acp", "jsonrpc_stdio", "openai_compatible", "vendor_app_server"}:
        return capability_for_mode(
            PromptTransportMode.STREAM
            if protocol in {"acp", "jsonrpc_stdio", "vendor_app_server"}
            else PromptTransportMode.RPC,
            source="adapter_shape_protocol",
        )
    if protocol in {"plain_cli", "streaming_json_cli"}:
        return capability_for_mode(PromptTransportMode.STDIN, source="adapter_shape_protocol")
    return default_prompt_transport_capability()


UNLIMITED_PROMPT_TRANSPORT = capability_for_mode(
    PromptTransportMode.RPC,
    source="explicit_override",
    max_prompt_bytes=DEFAULT_RPC_MAX_PROMPT_BYTES,
)


__all__ = [
    "DEFAULT_ARGV_INLINE_PROMPT_BYTES",
    "DEFAULT_ARGV_MAX_PROMPT_BYTES",
    "DEFAULT_FILE_MAX_PROMPT_BYTES",
    "DEFAULT_RPC_MAX_PROMPT_BYTES",
    "DEFAULT_STDIN_MAX_PROMPT_BYTES",
    "DEFAULT_UNKNOWN_MAX_PROMPT_BYTES",
    "MIN_TRANSPORT_SAFETY_BYTES",
    "TRANSPORT_BYTES_PER_TOKEN",
    "TRANSPORT_SAFETY_MARGIN_RATIO",
    "UNLIMITED_PROMPT_TRANSPORT",
    "PromptTransportCapability",
    "PromptTransportMode",
    "capability_for_mode",
    "default_prompt_transport_capability",
    "resolve_prompt_transport_capability",
]
