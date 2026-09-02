from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.room.models import ParticipantSlot
from persona_continuum.web.server import create_web_app

STATIC = Path(__file__).resolve().parents[2] / "src" / "persona_continuum" / "web" / "static"


def test_websocket_backend_is_available() -> None:
    from persona_continuum.web.server import websocket_backend_available

    assert websocket_backend_available() is True


def test_index_ships_empty_states_and_stop_button() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'id="btn-stop"' in html
    assert 'id="empty-new-room"' in html
    assert "还没有房间" in html
    assert "onClick(" in js
    assert "UI initialization error" in js
    assert "loadPersonas(), loadRooms(), loadApiProfiles(), loadWorlds()" in js
    assert '$("#btn-stop").onclick' not in js


def test_list_rooms_omits_heavy_transcript_payload(app: PersonaContinuum) -> None:
    app.personas.create_from_manifest(
        {
            "id": "steve_jobs",
            "display_name": "Steve Jobs",
            "persona_type": "historical",
            "run_mode": "continuation",
        }
    )
    room = app.orchestrator.create_room(
        title="Heavy Room",
        topic="Payload trim",
        participants=[
            ParticipantSlot(
                participant_id="slot_1",
                persona_id="steve_jobs",
                runtime_selection="fake_agent",
                model_selection="fake-gpt-5",
            )
        ],
    )
    room.transcript = [{"content": "secret-turn-payload", "speaker_name": "Steve"}]
    room.failed_turn_audits = [{"error": "boom"}]
    room.metadata["progress_events"] = [{"event": "room_started"}]
    app.orchestrator._save_room_state(room)

    web_app = create_web_app(app)
    with TestClient(web_app) as client:
        listed = client.get("/api/rooms").json()["data"]
        match = next(item for item in listed if item["id"] == room.id)
        assert match["title"] == "Heavy Room"
        assert match["transcript"] == []
        assert match["failed_turn_audits"] == []
        assert "progress_events" not in (match.get("metadata") or {})

        detail = client.get(f"/api/rooms/{room.id}").json()["data"]
        assert detail["transcript"][0]["content"] == "secret-turn-payload"
