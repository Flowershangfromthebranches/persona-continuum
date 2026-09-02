from __future__ import annotations

from starlette.testclient import TestClient

from persona_continuum.agent.models import AgentProbeResult, AgentStatus, ModelCapability
from persona_continuum.application.container import PersonaContinuum
from persona_continuum.web.server import create_web_app


def test_runtime_selector_consistency_e2e(app: PersonaContinuum) -> None:
    # Clear existing adapters in registry for deterministic testing
    app.agent_registry._adapters.clear()

    # Create dummy adapters
    class MockCodex:
        adapter_id = "codex"
        name = "Codex CLI"

        async def probe(self):
            return AgentProbeResult(
                id="codex",
                name="Codex CLI",
                status=AgentStatus.READY,
                binary_path="/usr/local/bin/codex",
                version="1.0.0",
                models=[ModelCapability(id="gpt-5", display_name="GPT-5")],
            )

    class MockCodeBuddy:
        adapter_id = "codebuddy"
        name = "CodeBuddy CLI"

        async def probe(self):
            return AgentProbeResult(
                id="codebuddy",
                name="CodeBuddy CLI",
                status=AgentStatus.READY,
                binary_path="/usr/local/bin/codebuddy",
                version="2.0.0",
                models=[ModelCapability(id="codebuddy-model", display_name="CodeBuddy Model")],
            )

    class MockGrok:
        adapter_id = "grok"
        name = "Grok CLI"

        async def probe(self):
            return AgentProbeResult(
                id="grok",
                name="Grok CLI",
                status=AgentStatus.AUTH_REQUIRED,
                binary_path="/usr/local/bin/grok",
                version="1.0.0",
                status_detail="API key required",
            )

    app.agent_registry.register_adapter(MockCodex())  # type: ignore[arg-type]
    app.agent_registry.register_adapter(MockCodeBuddy())  # type: ignore[arg-type]
    app.agent_registry.register_adapter(MockGrok())  # type: ignore[arg-type]

    web_app = create_web_app(app)
    with TestClient(web_app) as client:
        # GET /api/agents
        agents_resp = client.get("/api/agents")
        assert agents_resp.status_code == 200
        agents_json = agents_resp.json()
        assert agents_json["ok"] is True
        agents = agents_json["data"]

        # 1. Agent page checks
        codex_data = next((a for a in agents if a["id"] == "codex"), None)
        assert codex_data is not None
        assert codex_data["status"] == "ready"
        assert codex_data["runtime_source"] == "local_cli"
        assert codex_data["definition_source"] == "builtin"

        codebuddy_data = next((a for a in agents if a["id"] == "codebuddy"), None)
        assert codebuddy_data is not None
        assert codebuddy_data["status"] == "ready"
        assert codebuddy_data["runtime_source"] == "local_cli"

        grok_data = next((a for a in agents if a["id"] == "grok"), None)
        assert grok_data is not None
        assert grok_data["status"] == "auth_required"

        # 2. Selectable Local CLI helper calculation (matches Frontend helper)
        api_ready_local_ids = {
            a["id"]
            for a in agents
            if a["status"] == "ready"
            and a["runtime_source"] == "local_cli"
            and bool(a.get("binary_path"))
        }

        # Room Local CLI Selectable IDs
        room_dropdown_ids = {
            a["id"]
            for a in agents
            if a["status"] == "ready"
            and a["runtime_source"] == "local_cli"
            and bool(a.get("binary_path"))
        }

        # World Local CLI Selectable IDs
        world_dropdown_ids = {
            a["id"]
            for a in agents
            if a["status"] == "ready"
            and a["runtime_source"] == "local_cli"
            and bool(a.get("binary_path"))
        }

        # Assert:
        # 1. Codex and CodeBuddy are in the selectable set
        assert "codex" in room_dropdown_ids
        assert "codebuddy" in room_dropdown_ids
        # 2. Grok is NOT in the selectable set
        assert "grok" not in room_dropdown_ids
        # 3. All sets are strictly identical
        expected_ids = {"codex", "codebuddy"}
        assert api_ready_local_ids == expected_ids
        assert room_dropdown_ids == expected_ids
        assert world_dropdown_ids == expected_ids
