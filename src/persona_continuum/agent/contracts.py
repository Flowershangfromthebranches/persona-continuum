"""Structural contract shared by built-in and manifest Agent adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from persona_continuum.agent.adapter import AgentSession
from persona_continuum.agent.models import (
    AgentCapabilityFlags,
    AgentEvent,
    AgentProbeResult,
    AgentSessionConfig,
    AgentTurn,
    ModelCapability,
    OutputStreamingMode,
    PromptMode,
    RuntimeBindingSnapshot,
    StructuredOutputMode,
)


@runtime_checkable
class AgentAdapterContract(Protocol):
    """Minimum semantic surface expected by ``AgentRuntimeExecutor``."""

    adapter_id: str
    name: str
    prompt_mode: PromptMode | str
    structured_output_mode: StructuredOutputMode | str
    output_streaming_mode: OutputStreamingMode | str

    async def probe(self) -> AgentProbeResult: ...

    async def capabilities(self) -> AgentCapabilityFlags: ...

    async def list_models(self) -> list[ModelCapability]: ...

    async def create_session(self, config: AgentSessionConfig) -> AgentSession: ...

    async def bind_runtime(self, session: AgentSession) -> RuntimeBindingSnapshot: ...

    def send(self, session: AgentSession, turn: AgentTurn) -> AsyncIterator[AgentEvent]: ...

    async def cancel(self, session: AgentSession) -> None: ...

    async def close(self, session: AgentSession) -> None: ...


class AgentAdapterContractError(RuntimeError):
    """Raised when an adapter cannot satisfy the shared runtime contract."""


def adapter_prompt_mode(adapter: Any) -> PromptMode:
    raw = getattr(adapter, "prompt_mode", PromptMode.PROTOCOL_SPECIFIC)
    try:
        return raw if isinstance(raw, PromptMode) else PromptMode(str(raw))
    except ValueError:
        return PromptMode.PROTOCOL_SPECIFIC


def adapter_structured_output_mode(adapter: Any) -> StructuredOutputMode:
    raw = getattr(adapter, "structured_output_mode", StructuredOutputMode.UNKNOWN)
    try:
        return raw if isinstance(raw, StructuredOutputMode) else StructuredOutputMode(str(raw))
    except ValueError:
        return StructuredOutputMode.UNKNOWN


def adapter_output_streaming_mode(adapter: Any) -> OutputStreamingMode:
    raw = getattr(adapter, "output_streaming_mode", OutputStreamingMode.UNKNOWN)
    try:
        return raw if isinstance(raw, OutputStreamingMode) else OutputStreamingMode(str(raw))
    except ValueError:
        return OutputStreamingMode.UNKNOWN


def adapter_wire_text(adapter: Any, turn: AgentTurn) -> str:
    """Best-effort rendering of what this adapter actually puts on the wire.

    The Prompt Transport Guard must measure the real carrier text, not the
    canonical turn: an image that travels as ``@/path`` costs ~100 bytes on
    a CLI transport, while the same image inlined as base64 costs megabytes
    on an HTTP transport.  Adapters that append an attachment block expose
    it through ``attachment_prompt_block`` (plain CLI family) or consume
    attachments natively (app-server / HTTP family, no extra wire text).
    """

    from persona_continuum.agent.prompt import AgentPromptRenderer

    base = AgentPromptRenderer.render_for_single_prompt(turn)
    block_fn = getattr(adapter, "attachment_prompt_block", None)
    if callable(block_fn):
        try:
            block = block_fn(turn)
        except Exception:
            block = ""
        if block:
            return f"{base}\n\n{block}"
    block_fn = getattr(adapter, "_attachment_prompt_block", None)
    if callable(block_fn):
        try:
            block = block_fn(turn)
        except Exception:
            block = ""
        if block:
            return f"{base}\n\n{block}"
    return base


__all__ = [
    "AgentAdapterContract",
    "AgentAdapterContractError",
    "adapter_prompt_mode",
    "adapter_structured_output_mode",
    "adapter_output_streaming_mode",
    "adapter_wire_text",
]
