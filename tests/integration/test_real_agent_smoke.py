import os

import pytest

from persona_continuum.agent.adapters.codex import CodexAdapter

try:  # optional adapter module; the smoke test skips when it is absent
    from persona_continuum.agent.adapters.cursor import CursorAdapter
except ImportError:  # pragma: no cover - depends on the adapter tree
    CursorAdapter = None  # type: ignore[assignment, misc]

from persona_continuum.agent.adapters.grok import GrokBuildAdapter
from persona_continuum.agent.models import AgentStatus
from persona_continuum.agent.protocols.openai_compatible import OpenAICompatibleAPIAdapter
from persona_continuum.agent.registry import AgentRegistry
from persona_continuum.application.container import PersonaContinuum


@pytest.mark.anyio
async def test_codex_smoke() -> None:
    adapter = CodexAdapter()
    probe = await adapter.probe()
    if probe.binary_path:
        assert probe.status in {AgentStatus.READY, AgentStatus.AUTH_REQUIRED, AgentStatus.DETECTED}
    else:
        assert probe.status == AgentStatus.DISABLED


@pytest.mark.anyio
async def test_cursor_smoke() -> None:
    if CursorAdapter is None:
        pytest.skip("cursor adapter module is not present in this tree")
    adapter = CursorAdapter()
    probe = await adapter.probe()
    if probe.binary_path:
        assert probe.status in {AgentStatus.READY, AgentStatus.AUTH_REQUIRED, AgentStatus.DETECTED}
    else:
        assert probe.status == AgentStatus.DISABLED


@pytest.mark.anyio
async def test_grok_smoke() -> None:
    adapter = GrokBuildAdapter()
    probe = await adapter.probe()
    if probe.binary_path:
        assert probe.status in {AgentStatus.READY, AgentStatus.AUTH_REQUIRED, AgentStatus.DETECTED}
    else:
        assert probe.status == AgentStatus.DISABLED


@pytest.mark.anyio
async def test_all_builtin_adapters_smoke() -> None:
    registry = AgentRegistry(include_builtins=True, include_fake=False)
    probes = await registry.probe_all()
    assert len(probes) >= 12
    for p in probes:
        assert p.id != "fake_agent"
        assert p.status in {
            AgentStatus.READY,
            AgentStatus.AUTH_REQUIRED,
            AgentStatus.DETECTED,
            AgentStatus.DISABLED,
            AgentStatus.BROKEN,
        }


@pytest.mark.anyio
async def test_openai_compatible_smoke() -> None:
    adapter = OpenAICompatibleAPIAdapter(
        adapter_id="openai_smoke",
        name="OpenAI Smoke",
        base_url="https://api.openai.com/v1",
    )
    probe = await adapter.probe()
    assert probe.status in {AgentStatus.AUTH_REQUIRED, AgentStatus.READY, AgentStatus.DISABLED}


@pytest.mark.anyio
async def test_real_provider_connection() -> None:
    profile_id = os.environ.get("PERSONA_CONTINUUM_REAL_PROVIDER_PROFILE")
    if not profile_id:
        pytest.skip("set PERSONA_CONTINUUM_REAL_PROVIDER_PROFILE to an encrypted profile id")
    app = PersonaContinuum()
    app.init()
    try:
        result = await app.credentials.test_connection(profile_id)
        assert result["connected"] is True, result["error"]
        assert result["models"]
        assert result["latency"] > 0
    finally:
        app.close()
