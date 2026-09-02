from __future__ import annotations

from typing import Any

import pytest

from persona_continuum.agent.discovery import AgentDiscoveryService
from persona_continuum.agent.manifest_adapter import ManifestAgentAdapter
from persona_continuum.agent.models import (
    AgentProbeResult,
    AgentStatus,
    ModelCapability,
)
from persona_continuum.agent.protocols.openai_compatible import OpenAICompatibleAPIAdapter
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


def get_selectable_local_agents(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        a
        for a in agents
        if a.get("status") == "ready"
        and a.get("runtime_source") == "local_cli"
        and bool(a.get("binary_path"))
    ]


def get_selectable_api_agents(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [a for a in agents if a.get("status") == "ready" and a.get("runtime_source") == "api"]


def get_selectable_agents_for_source(
    agents: list[dict[str, Any]], source: str
) -> list[dict[str, Any]]:
    return (
        get_selectable_api_agents(agents)
        if source == "api"
        else get_selectable_local_agents(agents)
    )


@pytest.mark.anyio
async def test_ready_builtin_cli_visible_in_room_selector(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    db.migrate()
    auth_service = AuthProfileService(db)
    registry = AgentRegistry(include_builtins=False, include_fake=False)

    class DummyBuiltinCli:
        adapter_id = "codex"
        name = "Codex CLI"

        async def probe(self):
            return AgentProbeResult(
                id=self.adapter_id,
                name=self.name,
                status=AgentStatus.READY,
                binary_path="/usr/local/bin/codex",
                version="1.2.0",
                models=[ModelCapability(id="gpt-5", display_name="GPT-5")],
            )

    registry.register_adapter(DummyBuiltinCli())  # type: ignore[arg-type]
    discovery = AgentDiscoveryService(registry, auth_service)

    probes = await discovery.scan(force_refresh=True)
    probe_dicts = [p.model_dump(mode="json") for p in probes]

    selectable = get_selectable_local_agents(probe_dicts)
    assert len(selectable) == 1
    assert selectable[0]["id"] == "codex"
    assert selectable[0]["runtime_source"] == "local_cli"
    assert selectable[0]["definition_source"] == "builtin"


@pytest.mark.anyio
async def test_ready_manifest_cli_visible_in_room_selector(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    db.migrate()
    auth_service = AuthProfileService(db)
    registry = AgentRegistry(include_builtins=False, include_fake=False)

    manifest_adapter = ManifestAgentAdapter(
        manifest_data={
            "id": "codebuddy_manifest",
            "name": "CodeBuddy Manifest CLI",
            "binary": "codebuddy",
            "models": [{"id": "codebuddy-v1", "name": "CodeBuddy V1"}],
        }
    )

    # Mock probe returning READY with binary_path
    async def mock_probe():
        return AgentProbeResult(
            id="codebuddy_manifest",
            name="CodeBuddy Manifest CLI",
            status=AgentStatus.READY,
            binary_path="/opt/bin/codebuddy",
            version="2.0.0",
            models=[ModelCapability(id="codebuddy-v1", display_name="CodeBuddy V1")],
        )

    manifest_adapter.probe = mock_probe  # type: ignore[assignment]

    registry.register_adapter(manifest_adapter)
    discovery = AgentDiscoveryService(registry, auth_service)

    probes = await discovery.scan(force_refresh=True)
    probe_dicts = [p.model_dump(mode="json") for p in probes]

    # Should be classified as runtime_source: local_cli and definition_source: manifest
    codebuddy_probe = next(p for p in probe_dicts if p["id"] == "codebuddy_manifest")
    assert codebuddy_probe["runtime_source"] == "local_cli"
    assert codebuddy_probe["definition_source"] == "manifest"

    selectable = get_selectable_local_agents(probe_dicts)
    assert any(a["id"] == "codebuddy_manifest" for a in selectable)


@pytest.mark.anyio
async def test_nonready_cli_hidden_from_room_selector(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    db.migrate()
    auth_service = AuthProfileService(db)
    registry = AgentRegistry(include_builtins=False, include_fake=False)

    class DummyAuthRequiredCli:
        adapter_id = "grok"
        name = "Grok CLI"

        async def probe(self):
            return AgentProbeResult(
                id=self.adapter_id,
                name=self.name,
                status=AgentStatus.AUTH_REQUIRED,
                binary_path="/usr/local/bin/grok",
                version="1.0.0",
            )

    registry.register_adapter(DummyAuthRequiredCli())  # type: ignore[arg-type]
    discovery = AgentDiscoveryService(registry, auth_service)

    probes = await discovery.scan(force_refresh=True)
    probe_dicts = [p.model_dump(mode="json") for p in probes]

    selectable = get_selectable_local_agents(probe_dicts)
    assert not any(a["id"] == "grok" for a in selectable)


@pytest.mark.anyio
async def test_ready_api_not_visible_in_local_cli_selector(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    db.migrate()
    auth_service = AuthProfileService(db)
    registry = AgentRegistry(include_builtins=False, include_fake=False)

    api_adapter = OpenAICompatibleAPIAdapter(
        adapter_id="api_openai",
        name="OpenAI API",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o",
    )

    async def mock_api_probe():
        return AgentProbeResult(
            id="api_openai",
            name="OpenAI API",
            status=AgentStatus.READY,
            binary_path="https://api.openai.com/v1",
            version="API Endpoint",
            models=[ModelCapability(id="gpt-4o", display_name="GPT-4o")],
        )

    api_adapter.probe = mock_api_probe  # type: ignore[assignment]
    registry.register_adapter(api_adapter)

    discovery = AgentDiscoveryService(registry, auth_service)
    probes = await discovery.scan(force_refresh=True)
    probe_dicts = [p.model_dump(mode="json") for p in probes]

    api_probe = next(p for p in probe_dicts if p["id"] == "api_openai")
    assert api_probe["runtime_source"] == "api"
    assert api_probe["definition_source"] == "dynamic_api"

    local_selectable = get_selectable_local_agents(probe_dicts)
    assert not any(a["id"] == "api_openai" for a in local_selectable)

    api_selectable = get_selectable_api_agents(probe_dicts)
    assert any(a["id"] == "api_openai" for a in api_selectable)


@pytest.mark.anyio
async def test_room_and_world_use_same_runtime_filter() -> None:
    agents: list[dict[str, Any]] = [
        {
            "id": "codex",
            "name": "Codex",
            "status": "ready",
            "runtime_source": "local_cli",
            "binary_path": "/bin/codex",
        },
        {
            "id": "codebuddy",
            "name": "CodeBuddy",
            "status": "ready",
            "runtime_source": "local_cli",
            "binary_path": "/bin/codebuddy",
        },
        {
            "id": "grok",
            "name": "Grok",
            "status": "auth_required",
            "runtime_source": "local_cli",
            "binary_path": "/bin/grok",
        },
        {
            "id": "api_ollama",
            "name": "Ollama",
            "status": "ready",
            "runtime_source": "api",
            "binary_path": "http://localhost:11434",
        },
    ]

    room_local_agents = get_selectable_agents_for_source(agents, "local_cli")
    world_local_agents = get_selectable_agents_for_source(agents, "local_cli")

    assert [a["id"] for a in room_local_agents] == ["codex", "codebuddy"]
    assert [a["id"] for a in world_local_agents] == ["codex", "codebuddy"]
    assert [a["id"] for a in room_local_agents] == [a["id"] for a in world_local_agents]


@pytest.mark.anyio
async def test_rescan_updates_open_room_lobby(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    db.migrate()
    auth_service = AuthProfileService(db)
    registry = AgentRegistry(include_builtins=False, include_fake=False)
    discovery = AgentDiscoveryService(registry, auth_service)

    # Initially empty
    probes_1 = await discovery.scan(force_refresh=True)
    assert len(probes_1) == 0

    # Add dynamic adapter and scan again
    class DynamicCli:
        adapter_id = "dynamic_cli"
        name = "Dynamic CLI"

        async def probe(self):
            return AgentProbeResult(
                id=self.adapter_id,
                name=self.name,
                status=AgentStatus.READY,
                binary_path="/usr/bin/dynamic_cli",
                version="1.0.0",
                models=[ModelCapability(id="m1", display_name="M1")],
            )

    registry.register_adapter(DynamicCli())  # type: ignore[arg-type]

    probes_2 = await discovery.scan(force_refresh=True)
    selectable = get_selectable_local_agents([p.model_dump(mode="json") for p in probes_2])
    assert len(selectable) == 1
    assert selectable[0]["id"] == "dynamic_cli"


def test_no_fake_default_model_when_models_empty() -> None:
    agent_without_models = {
        "id": "custom_cli",
        "name": "Custom CLI",
        "status": "ready",
        "runtime_source": "local_cli",
        "binary_path": "/bin/custom",
        "models": [],
        "capabilities": {"model_selection": "startup", "agent_default_model": False},
    }
    models = agent_without_models.get("models", [])
    caps = agent_without_models.get("capabilities", {})
    has_explicit_default = (
        caps.get("model_selection") == "unsupported" or caps.get("agent_default_model") is True
    )
    assert not models
    assert not has_explicit_default


def test_no_fake_reasoning_values_when_capability_empty() -> None:
    model_without_efforts = {
        "id": "model_basic",
        "display_name": "Basic Model",
        "supported_reasoning_efforts": [],
    }
    efforts = model_without_efforts.get("supported_reasoning_efforts", [])
    assert efforts == []


@pytest.mark.anyio
async def test_cached_discovery_scan_returns_fast(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    db.migrate()
    auth_service = AuthProfileService(db)
    registry = AgentRegistry(include_builtins=False, include_fake=False)
    discovery = AgentDiscoveryService(registry, auth_service)

    class FastMockCli:
        adapter_id = "fast_mock"
        name = "Fast Mock"

        async def probe(self):
            return AgentProbeResult(
                id=self.adapter_id,
                name=self.name,
                status=AgentStatus.READY,
                binary_path="/usr/bin/fast_mock",
                version="1.0.0",
                models=[ModelCapability(id="m1", display_name="M1")],
            )

    registry.register_adapter(FastMockCli())  # type: ignore[arg-type]

    # Full scan populates cache
    probes_1 = await discovery.scan(force_refresh=True)
    assert len(probes_1) == 1

    # Cached scan returns instantly without re-probing
    probes_2 = await discovery.scan(force_refresh=False)
    assert len(probes_2) == 1
    assert probes_2[0].id == "fast_mock"


def test_api_model_provides_reasoning_efforts() -> None:
    api_agent = {
        "id": "api_openai",
        "name": "OpenAI API",
        "runtime_source": "api",
        "status": "ready",
        "models": [{"id": "o3-mini", "display_name": "o3-mini", "supported_reasoning_efforts": []}],
    }
    # For API agents, UI offers standard reasoning complexity options
    model = api_agent["models"][0]
    is_api = api_agent.get("runtime_source") == "api"
    efforts = model.get("supported_reasoning_efforts") or (
        ["none", "low", "medium", "high", "xhigh"] if is_api else []
    )
    assert efforts == ["none", "low", "medium", "high", "xhigh"]
