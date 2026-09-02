from __future__ import annotations

from tests.fixtures.base_persona import build_base_persona


def _state(app, persona_id: str) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    emotions = sorted((item.name, item.intensity) for item in app.affect.get_emotions(persona_id))
    needs = sorted((item.name, item.level) for item in app.motivation.get_needs(persona_id))
    return emotions, needs


def test_persona_validation_state_cleanup_restores_pre_session_state(app) -> None:
    persona_id, _ = build_base_persona(app)
    session = app.sessions.start_session(persona_id, "temporary validation")
    before = _state(app, persona_id)
    app.sessions.prepare_turn(
        persona_id,
        session.id,
        "A death prediction was verified",
        external_events=[{"type": "death_threat", "description": "verified"}],
    )
    committed = app.sessions.commit_turn(
        persona_id,
        session.id,
        user_message="A death prediction was verified",
        persona_response="I will verify exits and timing.",
    )
    assert committed["memory_id"]
    assert _state(app, persona_id) != before

    app.sessions.delete_session(persona_id, session.id, delete_derived_memories=True)

    assert _state(app, persona_id) == before
    assert app.database.conn.execute(
        "SELECT COUNT(*) AS count FROM sessions WHERE id = ?", (session.id,)
    ).fetchone()["count"] == 0
