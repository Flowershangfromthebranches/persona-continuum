from __future__ import annotations

import json

from starlette.testclient import TestClient

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.web.server import create_web_app


def test_web_app_rest_and_websocket_e2e(app: PersonaContinuum) -> None:
    # 1. Setup persona
    app.personas.create_from_manifest(
        {
            "id": "steve_jobs",
            "display_name": "Steve Jobs",
            "persona_type": "historical",
            "run_mode": "continuation",
        }
    )

    web_app = create_web_app(app)
    with TestClient(web_app) as client:
        root_resp = client.get("/")
        assert root_resp.status_code == 200

        css_resp = client.get("/static/app.css")
        assert css_resp.status_code == 200

        js_resp = client.get("/static/app.js")
        assert js_resp.status_code == 200
        assert 'persona_id: "alex_chen"' not in js_resp.text
        assert 'id: "persona_extra"' not in js_resp.text
        assert "Promise.all([loadPersonas(), loadAgents(false)])" in js_resp.text
        assert "retryRoomInitialization" in js_resp.text
        assert 'id="btn-stop"' in root_resp.text
        assert "还没有房间" in root_resp.text
        assert "UI initialization error" in js_resp.text

        # 3. Test Agents API
        agents_resp = client.get("/api/agents")
        assert agents_resp.status_code == 200
        agents_data = agents_resp.json()
        assert agents_data.get("ok") is True
        assert len(agents_data.get("data", [])) > 0

        # 4. Test Personas API
        personas_resp = client.get("/api/personas")
        assert personas_resp.status_code == 200
        personas_data = personas_resp.json()
        assert personas_data.get("ok") is True
        assert len(personas_data.get("data", [])) >= 1

        rooms_before = len(client.get("/api/rooms").json()["data"])
        invalid_room_resp = client.post(
            "/api/rooms",
            json={
                "topic": "Stale browser state",
                "participants": [
                    {
                        "participant_id": "stale-slot",
                        "persona_id": "deleted_persona",
                        "runtime_selection": "fake_agent",
                        "model_selection": "fake-gpt-5",
                    }
                ],
            },
        )
        assert invalid_room_resp.status_code == 400
        invalid_room_data = invalid_room_resp.json()
        assert invalid_room_data["details"]["code"] == "persona_not_found"
        assert len(client.get("/api/rooms").json()["data"]) == rooms_before

        # 5. Test Auth Profiles API
        auth_create_resp = client.post(
            "/api/auth-profiles",
            json={
                "name": "Local Ollama",
                "base_url": "http://localhost:11434/v1",
                "provider_type": "openai_compatible",
            },
        )
        assert auth_create_resp.status_code == 201
        auth_data = auth_create_resp.json()
        prof_id = auth_data["data"]["id"]

        auth_list_resp = client.get("/api/auth-profiles")
        assert len(auth_list_resp.json()["data"]) >= 1

        # 6. Test Room Creation and Step Turn REST API
        room_create_resp = client.post(
            "/api/rooms",
            json={
                "title": "Web Test Room",
                "topic": "Testing Web Interface",
                # The web UI always submits an explicit protocol (app.js
                # defaults the lobby selector to free_discussion); the API
                # rejects protocol-less creation.
                "protocol": "free_discussion",
                "participants": [
                    {
                        "participant_id": "slot_w1",
                        "persona_id": "steve_jobs",
                        "runtime_selection": "fake_agent",
                        "model_selection": "fake-gpt-5",
                    }
                ],
            },
        )
        assert room_create_resp.status_code == 201
        room_id = room_create_resp.json()["data"]["id"]

        # Start room
        start_resp = client.post(f"/api/rooms/{room_id}/start")
        assert start_resp.status_code == 200
        assert start_resp.json()["data"]["status"] == "ready"

        # Step room via REST
        step_resp = client.post(f"/api/rooms/{room_id}/step", json={})
        assert step_resp.status_code == 200
        events = step_resp.json()["data"]["events"]
        assert any(e.get("event") == "turn_completed" for e in events)

        # 7. Test WebSocket room interaction
        with client.websocket_connect(f"/api/rooms/{room_id}/ws") as websocket:
            websocket.send_text(json.dumps({"action": "ping"}))
            pong = False
            for _ in range(40):
                msg = websocket.receive_text()
                if "pong" in msg:
                    pong = True
                    break
            assert pong, "websocket did not answer ping"
            websocket.send_text(json.dumps({"action": "pause"}))
            paused = False
            for _ in range(40):
                msg = websocket.receive_text()
                if "room_paused" in msg:
                    paused = True
                    break
            assert paused, "websocket did not receive room_paused after replayed events"

        # 8. Test Parallel World Creation and Deletion
        # The generic engine never infers a default actor roster, so the test
        # supplies the human actors explicitly; the Persona Match gate must
        # still stop creation until completion is confirmed.
        world_payload = {
            "description": "Steve Jobs survives and invests in Apple Neural Silicon",
            "baseline": "real_world",
            "start_date": "2011-10-05",
            "simulation_end": "2030",
            "raw_actors": [
                {"id": "steve_jobs", "name": "Steve Jobs"},
                {"id": "jensen_huang", "name": "Jensen Huang"},
            ],
        }
        world_preflight_resp = client.post("/api/worlds", json=world_payload)
        assert world_preflight_resp.status_code == 409
        preflight = world_preflight_resp.json()["details"]
        missing_profiles = preflight["missing_profiles"]
        world_create_resp = client.post(
            "/api/worlds",
            json={
                **world_payload,
                "raw_actors": preflight["raw_actors"],
                "generated_actor_ids": [item["id"] for item in missing_profiles],
                "actor_completion_confirmed": True,
            },
        )
        assert world_create_resp.status_code == 201
        world_data = world_create_resp.json()["data"]
        world_id = world_data["world"]["id"]

        world_get_resp = client.get(f"/api/worlds/{world_id}")
        assert world_get_resp.status_code == 200
        assert world_get_resp.json()["data"]["world"]["id"] == world_id

        # Delete world
        del_world = client.delete(f"/api/worlds/{world_id}")
        assert del_world.status_code == 200
        assert del_world.json()["data"]["deleted"] is True

        # Verify deleted
        world_after_del = client.get(f"/api/worlds/{world_id}")
        assert world_after_del.status_code == 404

        # Clean up
        del_auth = client.delete(f"/api/auth-profiles/{prof_id}")
        assert del_auth.status_code == 200

        del_room = client.delete(f"/api/rooms/{room_id}")
        assert del_room.status_code == 200

        # 9. Test Persona Deletion API
        del_persona_resp = client.delete("/api/personas/steve_jobs")
        assert del_persona_resp.status_code == 200
        assert del_persona_resp.json()["data"]["deleted"] is True
