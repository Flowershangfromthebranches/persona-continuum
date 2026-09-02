from __future__ import annotations

import asyncio

import pytest

from persona_continuum.agent import discovery as discovery_mod

pytest.importorskip("persona_continuum.agent.adapters.cursor")

from persona_continuum.agent.adapters.cursor import CursorAdapter  # noqa: E402

from persona_continuum.agent.adapters.opencode import OpenCodeAdapter
from persona_continuum.agent.adapters.other_vendors import CodeBuddyAdapter
from persona_continuum.agent.discovery import AgentDiscoveryService
from persona_continuum.agent.models import AgentProbeResult, AgentStatus
from persona_continuum.agent.registry import AgentRegistry
from persona_continuum.auth.profiles import AuthProfileService
from persona_continuum.storage.database import Database


@pytest.fixture(autouse=True)
def isolate_external_agent_definitions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        AgentRegistry,
        "load_manifests_from_dir",
        lambda self, directory=None: [],
    )
    monkeypatch.setattr(
        AgentRegistry,
        "load_plugin_entrypoints",
        lambda self: [],
    )


@pytest.mark.anyio
async def test_agent_discovery_basic(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    db.migrate()
    auth_service = AuthProfileService(db)
    registry = AgentRegistry(include_builtins=True, include_fake=True)
    discovery = AgentDiscoveryService(registry, auth_service)

    for adapter in registry.list_adapters():
        if adapter.adapter_id == "fake_agent":
            continue

        async def disabled_probe(current=adapter):
            return AgentProbeResult(
                id=current.adapter_id,
                name=current.name,
                status=AgentStatus.DISABLED,
            )

        adapter.probe = disabled_probe  # type: ignore[method-assign]

    probes = await discovery.scan(force_refresh=True)
    assert len(probes) > 0

    fake_probe = next((p for p in probes if p.id == "fake_agent"), None)
    assert fake_probe is not None
    assert fake_probe.status == AgentStatus.READY
    assert len(fake_probe.models) >= 2
    assert "fake-gpt-5" in [m.id for m in fake_probe.models]

    # Verify Command Code adapter is registered and discovered
    cmd_probe = next((p for p in probes if p.id == "command_code"), None)
    assert cmd_probe is not None
    assert cmd_probe.name == "Command Code"

    # Test cache hit
    probes_cached = await discovery.scan(force_refresh=False)
    assert len(probes_cached) == len(probes)

    # Test probe specific adapter
    single_probe = await discovery.probe_adapter("fake_agent")
    assert single_probe is not None
    assert single_probe.id == "fake_agent"

    ready_agents = await discovery.get_ready_agents()
    assert any(a.id == "fake_agent" for a in ready_agents)


@pytest.mark.anyio
async def test_discovery_reuses_in_flight_scan() -> None:
    calls = {"n": 0}

    class CountingAdapter:
        adapter_id = "counter"
        name = "Counter"

        async def probe(self) -> AgentProbeResult:
            calls["n"] += 1
            await asyncio.sleep(0.05)
            return AgentProbeResult(id="counter", name="Counter", status=AgentStatus.READY)

    registry = AgentRegistry(include_builtins=False, include_fake=False)
    registry.register_adapter(CountingAdapter())  # type: ignore[arg-type]
    discovery = AgentDiscoveryService(registry)

    first, second = await asyncio.gather(
        discovery.scan(force_refresh=False),
        discovery.scan(force_refresh=False),
    )
    assert calls["n"] == 1
    assert len(first) == 1
    assert len(second) == 1


@pytest.mark.anyio
async def test_discovery_timeout_marks_detected_not_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(discovery_mod, "PROBE_TIMEOUT_SECONDS", 0.05)

    class SlowAdapter:
        adapter_id = "slow"
        name = "Slow"

        async def probe(self) -> AgentProbeResult:
            await asyncio.sleep(2)
            return AgentProbeResult(id="slow", name="Slow", status=AgentStatus.READY)

    registry = AgentRegistry(include_builtins=False, include_fake=False)
    registry.register_adapter(SlowAdapter())  # type: ignore[arg-type]
    discovery = AgentDiscoveryService(registry)
    probes = await discovery.scan(force_refresh=True)
    slow = next(item for item in probes if item.id == "slow")
    assert slow.status == AgentStatus.DETECTED
    assert "timed out" in (slow.status_detail or "").lower()


def test_codebuddy_dynamic_model_parsing() -> None:
    adapter = CodeBuddyAdapter()
    sample_help = (
        "Options:\n"
        "  --model <model>  Model for the current session. Currently supported: "
        "(hy3, hy3-x, glm-5.3, glm-5.2, glm-5.1, glm-5v-turbo, minimax-m3, "
        "minimax-m2.7, kimi-k3-1, kimi-k2.7, kimi-k2.6, deepseek-v4-pro, deepseek-v4-flash)\n"
        "  -h, --help       display help\n"
    )
    models = adapter._parse_models_from_help(sample_help)
    assert len(models) == 13
    model_ids = {m.id for m in models}
    assert "hy3" in model_ids
    assert "glm-5.3" in model_ids
    assert "kimi-k3-1" in model_ids
    assert "deepseek-v4-pro" in model_ids

    ds_pro = next(m for m in models if m.id == "deepseek-v4-pro")
    assert ds_pro.provider == "deepseek"
    assert "max" in ds_pro.supported_reasoning_efforts


def test_opencode_dynamic_model_parsing() -> None:
    adapter = OpenCodeAdapter()
    sample_output = """
xai:
  grok-4.5 * (500k)
  grok-4.6 (500k)

alibailian:
  deepseek-v4-flash-0731 *

openrouter:
  anthropic/claude-sonnet-5 (1000k)
  openai/gpt-5.6-sol (1050k)

* = default model for provider
Note: providers with liveModels may have additional models at runtime.
"""
    models = adapter._parse_models(sample_output)
    assert len(models) == 5
    model_ids = {m.id for m in models}
    assert "grok-4.6" in model_ids
    assert "deepseek-v4-flash-0731" in model_ids
    assert "anthropic/claude-sonnet-5" in model_ids
    assert "openai/gpt-5.6-sol" in model_ids


def test_cursor_binary_candidates_excludes_generic_agent() -> None:
    adapter = CursorAdapter()
    assert "agent" not in adapter.binary_candidates
    assert "cursor-agent" in adapter.binary_candidates
    assert "cursor" in adapter.binary_candidates
