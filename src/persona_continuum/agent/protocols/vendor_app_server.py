from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from persona_continuum.agent.adapter import (
    AgentAdapter,
    AgentSession,
    build_runtime_binding_snapshot,
)
from persona_continuum.agent.models import (
    AgentCapabilityFlags,
    AgentEvent,
    AgentEventType,
    AgentProbeResult,
    AgentSessionConfig,
    AgentStatus,
    AgentTurn,
    ModelCapability,
    OutputStreamingMode,
    PromptMode,
    RuntimeBindingSnapshot,
    SelectionStrategy,
    StructuredOutputMode,
)


class VendorAppServerAdapter(AgentAdapter):
    def __init__(
        self,
        adapter_id: str,
        name: str,
        server_url: str | None = None,
        socket_path: str | None = None,
        default_models: list[ModelCapability] | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.name = name
        self.server_url = server_url
        self.socket_path = socket_path
        self._default_models = default_models or []
        self.prompt_mode = PromptMode.PROTOCOL_SPECIFIC
        self.structured_output_mode = StructuredOutputMode.UNKNOWN
        self.output_streaming_mode = OutputStreamingMode.PROTOCOL_STREAM

    async def probe(self) -> AgentProbeResult:
        return AgentProbeResult(
            id=self.adapter_id,
            name=self.name,
            status=AgentStatus.DETECTED_UNCONTROLLABLE,
            binary_path=self.socket_path or self.server_url,
            version=None,
            protocols=["vendor_app_server"],
            capabilities=AgentCapabilityFlags(
                streaming=True,
                persistent_session=True,
                model_selection=SelectionStrategy.STARTUP,
                reasoning_selection=SelectionStrategy.STARTUP,
            ),
            models=self._default_models,
            status_detail="Vendor app server detected but direct control surface not connected",
        )

    async def list_models(self) -> list[ModelCapability]:
        return self._default_models

    async def create_session(self, config: AgentSessionConfig) -> AgentSession:
        session = AgentSession(
            config=config,
            is_active=True,
            session_data={},
        )
        session._cancel_event = asyncio.Event()
        return session

    async def bind_runtime(self, session: AgentSession) -> RuntimeBindingSnapshot:
        return build_runtime_binding_snapshot(
            self,
            session,
            protocol="vendor_app_server",
            model_verified=not bool(session.config.model_id),
            reasoning_verified=not bool(session.config.reasoning_effort),
            verification_method="uncontrollable_app_server",
        )

    async def send(self, session: AgentSession, turn: AgentTurn) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(
            type=AgentEventType.ERROR,
            error="VendorAppServerAdapter requires dedicated protocol implementation",
        )

    async def cancel(self, session: AgentSession) -> None:
        session.request_cancel()

    async def close(self, session: AgentSession) -> None:
        session.mark_closed()
