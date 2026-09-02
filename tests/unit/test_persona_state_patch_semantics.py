"""State-patch semantics and runtime affect response tests (P0 §9 / §10).

Before these tests existed, one field named ``affect`` meant two different
things: the engine treats it as an absolute floor (``max(current, amount)``)
while the name reads like a delta, and relationship ``changes`` were actually
absolute sets.  A caller who wrote ``{"affect": {"fear": -0.3}}`` expecting a
cool-down got a silent no-op.

The contract now separates:
* ``affect_set``    -> floor, raises only
* ``affect_delta``  -> additive, raises or lowers
* ``affect``        -> deprecated alias for ``affect_set``
* relationships ``set`` -> absolute write (``changes`` kept as an alias)
"""

from __future__ import annotations

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.domain.persona import PersonaType, RunMode
from persona_continuum.security.validation import CodedError


@pytest.fixture()
def app(tmp_path) -> PersonaContinuum:
    instance = PersonaContinuum(Config(data_dir=tmp_path / "data"))
    instance.init()
    try:
        instance.personas.create(
            display_name="Probe",
            aliases=["Probe"],
            persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
            run_mode=RunMode.DIGITAL_CONTINUATION,
            persona_id="probe",
        )
        instance.personas.activate("probe")
        yield instance
    finally:
        instance.database.conn.close()


def _session(app: PersonaContinuum) -> str:
    record = app.sessions.start_session("probe", title="probe")
    return record["session_id"] if isinstance(record, dict) else record.id


# ---------------------------------------------------------------------------
# Affect patch semantics
# ---------------------------------------------------------------------------


def test_affect_set_can_raise_but_not_lower(app: PersonaContinuum) -> None:
    session_id = _session(app)
    app.sessions.commit_turn(
        "probe",
        session_id,
        user_message="a",
        persona_response="b",
        state_patch={"affect_set": {"fear": 0.5}},
    )
    assert app.affect.get_emotions("probe")[0].name  # sanity: engine reachable
    fear = {s.name: s.intensity for s in app.affect.get_emotions("probe")}["fear"]
    assert fear == pytest.approx(0.5)

    app.sessions.commit_turn(
        "probe",
        session_id,
        user_message="c",
        persona_response="d",
        state_patch={"affect_set": {"fear": 0.1}},
    )
    fear = {s.name: s.intensity for s in app.affect.get_emotions("probe")}["fear"]
    assert fear == pytest.approx(0.5, abs=1e-6)


def test_affect_delta_can_lower(app: PersonaContinuum) -> None:
    session_id = _session(app)
    app.sessions.commit_turn(
        "probe",
        session_id,
        user_message="a",
        persona_response="b",
        state_patch={"affect_set": {"fear": 0.6}},
    )
    app.sessions.commit_turn(
        "probe",
        session_id,
        user_message="c",
        persona_response="d",
        state_patch={"affect_delta": {"fear": -0.4}},
    )
    fear = {s.name: s.intensity for s in app.affect.get_emotions("probe")}["fear"]
    assert fear == pytest.approx(0.2, abs=1e-6)


def test_legacy_affect_key_behaves_as_set(app: PersonaContinuum) -> None:
    session_id = _session(app)
    app.sessions.commit_turn(
        "probe",
        session_id,
        user_message="a",
        persona_response="b",
        state_patch={"affect": {"anger": 0.4}},
    )
    anger = {s.name: s.intensity for s in app.affect.get_emotions("probe")}["anger"]
    assert anger == pytest.approx(0.4)


def test_affect_alias_conflict_is_rejected(app: PersonaContinuum) -> None:
    session_id = _session(app)
    with pytest.raises(CodedError) as error:
        app.sessions.commit_turn(
            "probe",
            session_id,
            user_message="a",
            persona_response="b",
            state_patch={"affect": {"anger": 0.1}, "affect_set": {"anger": 0.2}},
        )
    assert error.value.code == "invalid_state_patch"


def test_affect_delta_rejects_out_of_range(app: PersonaContinuum) -> None:
    session_id = _session(app)
    with pytest.raises(CodedError):
        app.sessions.commit_turn(
            "probe",
            session_id,
            user_message="a",
            persona_response="b",
            state_patch={"affect_delta": {"fear": -2.0}},
        )


# ---------------------------------------------------------------------------
# Relationship patch semantics
# ---------------------------------------------------------------------------


def test_relationship_set_is_absolute(app: PersonaContinuum) -> None:
    session_id = _session(app)
    app.sessions.commit_turn(
        "probe",
        session_id,
        user_message="a",
        persona_response="b",
        state_patch={"relationships": [{"counterpart_id": "user", "set": {"trust": 0.6}}]},
    )
    state = app.relationships.get_relationship("probe", "user")
    assert state.trust == pytest.approx(0.6)

    app.sessions.commit_turn(
        "probe",
        session_id,
        user_message="c",
        persona_response="d",
        state_patch={"relationships": [{"counterpart_id": "user", "set": {"trust": 0.12}}]},
    )
    assert app.relationships.get_relationship("probe", "user").trust == pytest.approx(0.12)


def test_relationship_changes_alias_still_works(app: PersonaContinuum) -> None:
    session_id = _session(app)
    app.sessions.commit_turn(
        "probe",
        session_id,
        user_message="a",
        persona_response="b",
        state_patch={"relationships": [{"counterpart_id": "user", "changes": {"trust": 0.3}}]},
    )
    assert app.relationships.get_relationship("probe", "user").trust == pytest.approx(0.3)


def test_relationship_set_and_changes_conflict_is_rejected(app: PersonaContinuum) -> None:
    session_id = _session(app)
    with pytest.raises(CodedError):
        app.sessions.commit_turn(
            "probe",
            session_id,
            user_message="a",
            persona_response="b",
            state_patch={
                "relationships": [
                    {"counterpart_id": "user", "set": {"trust": 0.1}, "changes": {"trust": 0.2}}
                ]
            },
        )


def test_relationship_rejects_negative_values(app: PersonaContinuum) -> None:
    session_id = _session(app)
    with pytest.raises(CodedError) as error:
        app.sessions.commit_turn(
            "probe",
            session_id,
            user_message="a",
            persona_response="b",
            state_patch={"relationships": [{"counterpart_id": "user", "set": {"trust": -0.25}}]},
        )
    assert "relationships" in str(error.value.code) or error.value.code == "invalid_state_patch"


# ---------------------------------------------------------------------------
# §10.1 minimum event response
# ---------------------------------------------------------------------------


def _emotions(app: PersonaContinuum) -> dict[str, float]:
    return {s.name: round(s.intensity, 3) for s in app.affect.get_emotions("probe")}


def test_routine_event_barely_moves_state(app: PersonaContinuum) -> None:
    session_id = _session(app)
    app.sessions.prepare_turn(
        "probe", session_id, "在吗", external_events=[{"type": "status", "summary": "例行同步消息"}]
    )
    app.sessions.commit_turn(
        "probe",
        session_id,
        user_message="在吗",
        persona_response="在。",
    )
    emotions = _emotions(app)
    assert max(emotions.values(), default=0.0) < 0.1


def test_death_threat_raises_fear_clearly(app: PersonaContinuum) -> None:
    session_id = _session(app)
    app.sessions.prepare_turn(
        "probe",
        session_id,
        "什么情况",
        external_events=[
            {"type": "death_threat", "summary": "收到准确的死亡预告", "intensity": 1.0}
        ],
    )
    app.sessions.commit_turn(
        "probe",
        session_id,
        user_message="什么情况",
        persona_response="……我知道了。",
    )
    emotions = _emotions(app)
    assert emotions.get("fear", 0.0) >= 0.30
    assert emotions.get("fear", 0.0) > emotions.get("joy", 0.0)


def test_betrayal_lowers_trust(app: PersonaContinuum) -> None:
    session_id = _session(app)
    app.relationships.update_relationship(
        "probe", "陈默", {"trust": 0.55}, "baseline", commit=False
    )
    app.database.conn.commit()
    app.sessions.end_session(session_id)
    started = app.sessions.start_session("probe", title="betray", counterpart_id="陈默")
    session_id = started["session_id"] if isinstance(started, dict) else started.id
    app.sessions.prepare_turn(
        "probe",
        session_id,
        "你说",
        counterpart_id="陈默",
        external_events=[{"type": "betrayal", "summary": "陈默被发现撒谎"}],
    )
    app.sessions.commit_turn(
        "probe",
        session_id,
        user_message="你说",
        persona_response="……",
        counterpart_id="陈默",
    )
    state = app.relationships.get_relationship("probe", "陈默")
    assert state.trust < 0.55


def test_event_movement_exceeds_the_conversational_cap(app: PersonaContinuum) -> None:
    """A discrete world event must not be capped at the tone ceiling."""
    from persona_continuum.application.state_appraisal import (
        AppraisalLimits,
        AppraisalRequest,
        PersonaStateAppraisalService,
    )

    service = PersonaStateAppraisalService(
        mode="heuristic", limits=AppraisalLimits(affect=0.15)
    )
    result = service.appraise(
        AppraisalRequest(
            external_events=[{"type": "death_threat", "summary": "19:17 你会死"}]
        )
    )
    assert result.affect.get("fear", 0.0) >= 0.30
