"""Web runtime hygiene: keep the lobby create-room path explicit.

These checks parse the frontend bundle as text to guarantee that protocol
selection never silently falls back to free_discussion and that role
mappings stay protocol-accurate.
"""

from __future__ import annotations

import re
from pathlib import Path

APP_JS_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "persona_continuum"
    / "web"
    / "static"
    / "app.js"
)


def _app_js_text() -> str:
    return APP_JS_PATH.read_text(encoding="utf-8")


def _protocol_role_mapping(protocol_key: str) -> list[str]:
    """Parse the literal roles array for one protocol from the mapping."""

    matches = re.findall(rf"{protocol_key}:\s*\[([^\]]*)\]", _app_js_text())
    assert matches, f"roles array for {protocol_key} not found in app.js"
    roles: list[str] = []
    for entry in matches:
        roles.extend(re.findall(r'"([^"]+)"', entry))
    return roles


def test_role_options_expert_consultation_includes_expert() -> None:
    roles = _protocol_role_mapping("expert_consultation")
    assert "expert" in roles
    assert "host" in roles


def test_role_options_free_discussion_excludes_expert() -> None:
    roles = _protocol_role_mapping("free_discussion")
    assert "expert" not in roles
    assert "member" in roles


def test_create_payload_has_no_silent_protocol_fallback() -> None:
    assert 'value || "free_discussion"' not in _app_js_text()


def test_opening_room_lobby_keeps_configuration_blank() -> None:
    text = _app_js_text()
    start = text.index("function openRoomLobby()")
    end = text.index("// ─── Lobby: simple vs advanced mode", start)
    block = text[start:end]
    assert '$("#lobby-title").value = ""' in block
    assert '$("#lobby-protocol").value = ""' in block
    assert "state.lobbySlots = []" in block
    assert "多人哲学与技术探索" not in block
    assert "Multi-Persona Exploration" not in block
