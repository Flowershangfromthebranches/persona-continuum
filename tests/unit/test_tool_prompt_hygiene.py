"""Prompt hygiene: the injected tool block must stay neutral.

The tool layer once appended a divination-specific "tool unavailable /
knowledge-explanation mode" note to every room prompt, leaking task-specific
instructions into rooms that never asked for them.  These tests lock in the
generic contract: the block lists exactly the callable tools and nothing else.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from persona_continuum.room.tool_providers.broker import RoomToolBroker
from persona_continuum.room.tool_providers.persona_builtin import (
    BUILTIN_TOOL_NAMES,
    PersonaBuiltinToolProvider,
)

FORBIDDEN_PROMPT_TERMS = ("术数", "排盘", "知识解释", "降级", "degraded", "divination")


class _StubContinuum:
    """Discovery-only stand-in for the application container."""


def _default_broker() -> RoomToolBroker:
    continuum = cast("Any", _StubContinuum())
    return RoomToolBroker(continuum)


def test_default_registration_contains_only_builtin_provider() -> None:
    """With no explicit providers, only the first-party provider is registered."""

    broker = _default_broker()

    ids = [provider.provider_id for provider in broker.providers]
    assert ids == [PersonaBuiltinToolProvider.provider_id]
    assert all(provider.builtin for provider in broker.providers)


@pytest.mark.anyio
async def test_render_tool_block_lists_only_callable_tools() -> None:
    """The prompt block is a bare header plus the tool list -- nothing else."""

    broker = _default_broker()

    block = await broker.compose_tool_block()

    assert block.startswith("AVAILABLE TOOLS FOR THIS TURN:")
    assert len(block.splitlines()) == 1 + len(BUILTIN_TOOL_NAMES)
    for term in FORBIDDEN_PROMPT_TERMS:
        assert term not in block


def test_render_tool_block_with_no_tools_says_none() -> None:
    block = RoomToolBroker.render_tool_block([])

    assert block == "AVAILABLE TOOLS FOR THIS TURN:\n- none"


def test_tool_unavailable_prompt_note_is_gone() -> None:
    """The divination-specific prompt note must not exist anywhere."""

    import persona_continuum.room.tool_providers.broker as broker_module

    assert not hasattr(broker_module, "TOOL_UNAVAILABLE_PROMPT_NOTE")
