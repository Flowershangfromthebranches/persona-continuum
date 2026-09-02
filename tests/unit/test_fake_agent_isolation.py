from __future__ import annotations

import pytest

from persona_continuum.agent.models import AgentProbeResult, AgentStatus
from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config


@pytest.mark.anyio
async def test_production_registry_excludes_fake_agent(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Production default initialization (include_fake_agent=False)
    prod_config = Config(data_dir=tmp_path / "prod-data")
    prod_app = PersonaContinuum(prod_config)
    prod_app.init()
    try:
        monkeypatch.setattr(
            prod_app.agent_registry,
            "load_manifests_from_dir",
            lambda *args, **kwargs: [],
        )
        monkeypatch.setattr(
            prod_app.agent_registry,
            "load_plugin_entrypoints",
            lambda: [],
        )
        # Registry check
        assert prod_app.agent_registry.get_adapter("fake_agent") is None
        assert "fake_agent" not in prod_app.agent_registry.list_adapters()

        # Discovery isolation is independent from the health of locally
        # installed CLIs; stub their probes so this unit test never launches
        # user runtimes merely to prove Fake Agent is absent.
        for adapter in prod_app.agent_registry.list_adapters():
            async def disabled_probe(current=adapter):
                return AgentProbeResult(
                    id=current.adapter_id,
                    name=current.name,
                    status=AgentStatus.DISABLED,
                )

            adapter.probe = disabled_probe  # type: ignore[method-assign]

        probes = await prod_app.agent_discovery.scan(force_refresh=True)
        probe_ids = [p.id for p in probes]
        assert "fake_agent" not in probe_ids

        # Ready agents check
        ready_agents = await prod_app.agent_discovery.get_ready_agents()
        ready_ids = [a.id for a in ready_agents]
        assert "fake_agent" not in ready_ids
    finally:
        prod_app.close()


@pytest.mark.anyio
async def test_debug_mode_allows_fake_agent(tmp_path) -> None:
    # Explicit debug / test mode
    debug_config = Config(data_dir=tmp_path / "debug-data")
    debug_app = PersonaContinuum(debug_config, include_fake_agent=True)
    debug_app.init()
    try:
        adapter_ids = [a.adapter_id for a in debug_app.agent_registry.list_adapters()]
        assert "fake_agent" in adapter_ids
    finally:
        debug_app.close()
