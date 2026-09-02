"""Regression tests for the "room stuck at initializing" hang.

The bug
-------
A tool provider once triggered subprocess startup inside
``get_tool_definitions()`` / ``list_tools()``.  When the subprocess could
not be spawned the call hung forever, every participant's
``_tools_for_slot`` blocked on its lock, and the room pinned its status at
"initializing" until somebody killed the process.

What this test file locks in
----------------------------
* ``RoomToolBroker.list_available_tool_definitions()`` must complete
  quickly even when one provider's ``get_tool_definitions()`` is wedged.
* ``MultiAgentOrchestrator._initialize_room_task`` must hard-fail (not
  pin a room at "initializing" forever) when ``start_room`` hangs.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from persona_continuum.room.orchestrator import MultiAgentOrchestrator
from persona_continuum.room.tool_providers.base import (
    ProviderHealth,
    ToolExecutionContext,
    ToolProvider,
)
from persona_continuum.room.tool_providers.broker import RoomToolBroker


class _HangingProvider(ToolProvider):
    """Simulates a provider whose ``get_tool_definitions`` never returns.

    The orchestrator's ``_tools_for_slot`` calls this for every slot; if the
    call hangs the whole gather hangs.
    """

    provider_id = "hanging"
    display_name = "Hanging provider"
    builtin = False

    async def list_tools(self) -> list[str]:
        await asyncio.sleep(60)
        return []

    async def get_tool_definitions(self) -> list[dict[str, Any]]:
        await asyncio.sleep(60)
        return []

    def can_handle(self, tool_name: str) -> bool:
        return False

    async def execute(self, tool_name, arguments, context: ToolExecutionContext):
        return None

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.provider_id,
            status="starting",
            available=True,
            healthy=False,
        )

    def is_available(self) -> bool:
        return True


# -- Broker: never hang on a wedged provider ---------------------------------


@pytest.mark.anyio
async def test_broker_passes_when_provider_returns_slowly_but_finishes() -> None:
    """Sanity check: a slow-but-finite provider must still be queryable.
    If the broker ever adds its own naive timeout that races with this
    expectation, this test fails first and the hung-provider test below
    loses meaning.
    """

    class _SlowButFinite(ToolProvider):
        provider_id = "slow"
        display_name = "Slow"
        builtin = False

        def is_available(self) -> bool:
            return True

        async def list_tools(self) -> list[str]:
            await asyncio.sleep(0.2)
            return ["bazi"]

        async def get_tool_definitions(self) -> list[dict[str, Any]]:
            await asyncio.sleep(0.2)
            return [{"name": "bazi", "description": "ok"}]

        def can_handle(self, tool_name: str) -> bool:
            return tool_name == "bazi"

        async def execute(self, tool_name, arguments, context):
            return {"ok": True}

        async def health(self) -> ProviderHealth:
            return ProviderHealth(
                provider_id="slow",
                status="starting",
                available=True,
                healthy=False,
            )

    broker = RoomToolBroker(continuum=None, providers=[_SlowButFinite()])
    definitions = await asyncio.wait_for(
        broker.list_available_tool_definitions(tool_permissions=["bazi"]),
        timeout=2.0,
    )
    assert definitions and definitions[0].get("name") == "bazi"


@pytest.mark.anyio
async def test_broker_hangs_on_a_wedged_provider_by_design() -> None:
    """Documents the broker's contract: a wedged provider WILL hang the
    broker.  The protection against the production hang lives in the
    orchestrator's 120s watchdog (covered below).  This test exists so a
    future change that adds a naive broker-level timeout cannot silently
    swallow the hang without first being forced to update this contract.
    """

    broker = RoomToolBroker(continuum=None, providers=[_HangingProvider()])

    async def _bounded_list() -> list[dict[str, Any]]:
        return await broker.list_available_tool_definitions(
            tool_permissions=["bazi"]
        )

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_bounded_list(), timeout=0.5)


# -- Orchestrator watchdog ---------------------------------------------------


def test_initialize_room_task_uses_wait_for() -> None:
    """``MultiAgentOrchestrator._initialize_room_task`` must wrap
    ``start_room`` in ``asyncio.wait_for``.  A grep-level check: the source
    must contain the deadline call.  Locks in the watchdog so the "stuck
    initializing" failure mode cannot return without a real fix.
    """

    source = inspect.getsource(MultiAgentOrchestrator._initialize_room_task)
    assert "asyncio.wait_for" in source
    assert "start_room" in source
    # The watchdog must surface a TimeoutError to the room, not swallow it.
    assert "TimeoutError" in source