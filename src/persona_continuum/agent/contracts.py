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


__all__ = [
    "AgentAdapterContract",
    "AgentAdapterContractError",
    "adapter_prompt_mode",
    "adapter_structured_output_mode",
    "adapter_output_streaming_mode",
]
