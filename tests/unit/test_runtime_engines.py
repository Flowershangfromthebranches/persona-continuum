from __future__ import annotations

from datetime import UTC, datetime, timedelta

from persona_continuum.domain.affect import EMOTION_NAMES
from persona_continuum.domain.persona import PersonaType, RunMode


def test_affect_engine_decays_and_records_reason(app) -> None:
    persona = app.personas.create(
        display_name="Alex Chen",
        aliases=[],
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode=RunMode.DIGITAL_CONTINUATION,
    )
    now = datetime.now(UTC)
    app.affect.update_emotions(
        persona.id,
        observations={"frustration": 0.7, "hope": 0.4},
        reason="User challenged Alex's abandoned prototype.",
        now=now,
    )

    later = now + timedelta(hours=6)
    states = app.affect.get_emotions(persona.id, now=later)

    assert set(EMOTION_NAMES).issuperset({state.name for state in states})
    frustration = next(state for state in states if state.name == "frustration")
    assert 0 < frustration.intensity < 0.7
    assert "challenged" in frustration.triggers[-1]


def test_relationship_engine_allows_contradictory_state(app) -> None:
    persona = app.personas.create(
        display_name="Alex Chen",
        aliases=[],
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode=RunMode.DIGITAL_CONTINUATION,
    )

    relationship = app.relationships.update_relationship(
        persona.id,
        "Jordan",
        {"affection": 0.8, "resentment": 0.5, "trust": 0.3},
        reason="Jordan supported Alex but leaked an early memo.",
    )

    assert relationship.affection == 0.8
    assert relationship.resentment == 0.5
    assert relationship.trust == 0.3
    assert relationship.reasons[-1].startswith("Jordan supported")
