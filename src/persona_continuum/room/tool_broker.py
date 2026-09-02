"""Backwards-compatible entry point for Room tools.

Historically this module *was* the tool layer: three hardcoded first-party
persona tools, and everything else answered with ``Unknown tool`` — a room's
``tool_permissions`` was never turned into an implementation.

The real implementation now lives in :mod:`persona_continuum.room.tool_providers`,
where one aggregated :class:`RoomToolBroker` fronts every provider
(persona built-ins today, future external sources behind the same contract)
behind a single availability ∩ permission gate.

This module keeps :class:`PersonaToolBroker` as a thin compatibility shim so
existing callers keep working while the orchestrator migrates to the filtered
async API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from persona_continuum.room.tool_providers.broker import RoomToolBroker
from persona_continuum.room.tool_providers.persona_builtin import (
    BUILTIN_TOOL_NAMES,
    PersonaBuiltinToolProvider,
)

if TYPE_CHECKING:
    from persona_continuum.application.container import PersonaContinuum


class PersonaToolBroker:
    """Legacy facade over the aggregated :class:`RoomToolBroker`.

    The no-argument surface (``get_tool_definitions()`` / ``execute_tool()``)
    intentionally reproduces the old *built-ins only* behaviour, so a caller
    that never opted into external tools cannot accidentally gain them.
    """

    def __init__(self, continuum: PersonaContinuum) -> None:
        self.continuum = continuum
        self.broker = RoomToolBroker(continuum)

    # -- legacy surface ----------------------------------------------------
    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Built-in persona tools only (synchronous, legacy compatibility)."""

        return PersonaBuiltinToolProvider.static_definitions()

    # -- filtered surface used by the orchestrator -------------------------
    async def list_tool_definitions(
        self,
        *,
        tool_permissions: list[str] | None = None,
        allow_agent_tools: bool = True,
    ) -> list[dict[str, Any]]:
        """Definitions this participant may call: availability ∩ permission."""

        return await self.broker.list_available_tool_definitions(
            tool_permissions=tool_permissions,
            allow_agent_tools=allow_agent_tools,
        )

    async def execute_tool(
        self,
        persona_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        branch_id: str = "main",
        *,
        tool_permissions: list[str] | None = None,
        allow_agent_tools: bool = True,
        participant_id: str = "",
        room_id: str = "",
    ) -> Any:
        """Execute one tool through the same gate that built the definitions."""

        return await self.broker.execute_tool(
            persona_id,
            tool_name,
            arguments,
            branch_id=branch_id,
            participant_id=participant_id,
            room_id=room_id,
            tool_permissions=tool_permissions,
            allow_agent_tools=allow_agent_tools,
        )

    # -- operational surface -----------------------------------------------
    async def tool_availability_report(
        self,
        *,
        tool_permissions: list[str] | None = None,
        allow_agent_tools: bool = True,
    ) -> dict[str, Any]:
        return await self.broker.tool_availability_report(
            tool_permissions=tool_permissions,
            allow_agent_tools=allow_agent_tools,
        )

    async def compose_tool_block(
        self,
        *,
        tool_permissions: list[str] | None = None,
        allow_agent_tools: bool = True,
    ) -> str:
        return await self.broker.compose_tool_block(
            tool_permissions=tool_permissions,
            allow_agent_tools=allow_agent_tools,
        )

    async def preflight(self) -> dict[str, Any]:
        return await self.broker.preflight()

    async def health_report(self) -> dict[str, Any]:
        return await self.broker.health_report()

    async def warmup(self) -> None:
        await self.broker.warmup()

    async def shutdown(self) -> None:
        await self.broker.shutdown()

    def shutdown_sync(self) -> None:
        self.broker.shutdown_sync()


__all__ = [
    "BUILTIN_TOOL_NAMES",
    "PersonaToolBroker",
    "RoomToolBroker",
]
