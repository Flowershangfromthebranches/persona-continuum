"""Discovery honesty: uninstalled/broken runtimes advertise no models, and
the capability cache must tolerate adapters that re-enter it during discovery.
"""

from __future__ import annotations

import asyncio

import pytest

from persona_continuum.agent.adapters.claude import ClaudeCodeAdapter
from persona_continuum.agent.adapters.codex import CodexAdapter
from persona_continuum.agent.adapters.command_code import CommandCodeAdapter

pytest.importorskip("persona_continuum.agent.adapters.cursor")

from persona_continuum.agent.adapters.cursor import CursorAdapter  # noqa: E402

from persona_continuum.agent.adapters.grok import GrokBuildAdapter
from persona_continuum.agent.adapters.other_vendors import (
    CopilotAdapter,
    KimiAdapter,
    QwenAdapter,
)
from persona_continuum.agent.models import AgentStatus, ModelCapability
from persona_continuum.performance.capability_cache import ModelCapabilityCache


@pytest.mark.anyio
@pytest.mark.parametrize(
    "adapter_factory",
    [
        ClaudeCodeAdapter,
        CodexAdapter,
        CommandCodeAdapter,
        CursorAdapter,
        GrokBuildAdapter,
        QwenAdapter,
        KimiAdapter,
        CopilotAdapter,
    ],
)
async def test_missing_binary_disables_runtime_without_model_list(adapter_factory) -> None:
    adapter = adapter_factory()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(adapter, "_find_binary", lambda: None)
        probe = await adapter.probe()
    assert probe.status == AgentStatus.DISABLED
    assert probe.binary_path is None
    # A runtime that is not installed cannot serve any model; advertising a
    # config scaffold here previously made uninstalled CLIs look usable.
    assert probe.models == []


@pytest.mark.anyio
async def test_capability_cache_allows_reentrant_store_during_discovery() -> None:
    """Codex-style adapters call ``store()`` inside ``list_models()``.

    The cache lock is not reentrant; discovery must run outside it or the
    probe deadlocks and discovery reports a false timeout.
    """

    class ReentrantAdapter:
        adapter_id = "reentrant"
        name = "Reentrant"

        def __init__(self) -> None:
            self.list_calls = 0

        async def list_models(self) -> list[ModelCapability]:
            self.list_calls += 1
            model = ModelCapability(
                id="m1", display_name="M1", provider="x", supported_reasoning_efforts=[]
            )
            await cache.store(self, [model])
            return [model]

    cache = ModelCapabilityCache(ttl_seconds=3600)
    adapter = ReentrantAdapter()
    models = await asyncio.wait_for(cache.get_models(adapter), timeout=2.0)
    assert [m.id for m in models] == ["m1"]
    # The inner store() populated the entry, so the outer write is a no-op.
    peeked = cache.peek(adapter)
    assert peeked is not None and len(peeked) == 1
