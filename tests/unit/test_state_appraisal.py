"""Persona state appraisal: legality, bounded movement, and the §17 regression.

The bug this module guards against: the old code ended with
``return observations or {"curiosity": 0.1}``.  ``curiosity`` is a NEED, so
``AffectEngine.update_emotions`` dropped it via ``if name not in EMOTION_NAMES``
and every "state update" was silently a no-op.
"""

from __future__ import annotations

from typing import Any

from persona_continuum.application.state_appraisal import (
    DEFAULT_MODE,
    AppraisalLimits,
    AppraisalRequest,
    PersonaStateAppraisalService,
)
from persona_continuum.domain.affect import EMOTION_NAMES, NEED_NAMES
from persona_continuum.domain.persona import PersonaType, RunMode
from persona_continuum.runtime.relationship_engine import RELATIONSHIP_FIELDS


class _ScriptedAppraiser:
    def __init__(self, patch: dict[str, Any]) -> None:
        self.patch = patch
        self.seen: list[dict[str, Any]] = []

    def appraise(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        self.seen.append(payload)
        return self.patch


def _service(**kwargs: Any) -> PersonaStateAppraisalService:
    mode = kwargs.pop("mode", DEFAULT_MODE)
    return PersonaStateAppraisalService(mode, **kwargs)


# -- §17 regression: the curiosity leak -------------------------------------


def test_curiosity_is_appraised_as_a_need_never_as_an_emotion() -> None:
    result = _service().appraise(AppraisalRequest(user_message="这是为什么？我很好奇"))

    assert result.needs, "a curiosity cue must move the curiosity need"
    assert "curiosity" in result.needs
    assert "curiosity" not in result.affect


def test_affect_keys_are_always_legal_emotion_names() -> None:
    service = _service()
    messages = [
        "我很好奇为什么",
        "非常担心、焦虑、紧张、不安、愤怒、讨厌、难过、害怕、羞愧、内疚、孤独、嫉妒",
        "太好了，很喜欢，充满希望，非常惊讶",
        "谢谢你的帮助，我完全不信任你，我们和解了",
    ]
    for message in messages:
        result = service.appraise(AppraisalRequest(user_message=message, persona_response=message))
        assert result.affect.keys() <= set(EMOTION_NAMES), message
        assert result.needs.keys() <= set(NEED_NAMES), message


def test_need_keys_are_always_legal_need_names() -> None:
    result = _service().appraise(
        AppraisalRequest(
            user_message="为什么？我自己决定，谢谢你认可，我完成了任务，我们一起，掌控局面"
        )
    )
    assert result.needs.keys() <= set(NEED_NAMES)
    assert result.needs, "the message carries several need cues"


def test_no_cues_means_no_affect_or_need_write() -> None:
    """The old fallback wrote a phantom value here; now nothing is invented.

    Familiarity still drifts, because a turn really did happen, but no emotion
    and no need is manufactured out of an uneventful sentence.
    """

    result = _service().appraise(AppraisalRequest(user_message="今天天气是多云。"))

    assert result.affect == {}
    assert result.needs == {}
    assert set(result.relationships[0]["changes"]) == {"familiarity"}


def test_emotion_observations_view_never_contains_a_need(app) -> None:
    observations = app.sessions._emotion_observations("", "", None)

    assert observations == {}
    assert "curiosity" not in observations


# -- §18 heuristic behaviour ------------------------------------------------


def test_worry_raises_anxiety() -> None:
    result = _service().appraise(AppraisalRequest(user_message="我最近很担心项目进度"))

    assert "anxiety" in result.affect
    assert result.affect["anxiety"] > 0.0


def test_gratitude_moves_trust_and_affection() -> None:
    result = _service().appraise(AppraisalRequest(user_message="谢谢你，我明白了"))

    changes = result.relationships[0]["changes"]
    assert changes["trust"] > 0
    assert changes["affection"] > 0


def test_hostility_moves_trust_down_and_resentment_up() -> None:
    # Relationship values are absolute and clamped to [0, 1], so a baseline is
    # required to observe erosion: from zero, trust can only stay at zero.
    result = _service().appraise(
        AppraisalRequest(
            user_message="你是个骗子，我不信任你",
            current_relationship={"trust": 0.50},
        )
    )

    changes = result.relationships[0]["changes"]
    assert changes["trust"] < 0.50, "hostility must erode trust"
    assert changes["resentment"] > 0


def test_ambivalence_is_detected_and_damped() -> None:
    mixed = _service().appraise(AppraisalRequest(user_message="我很开心，但同时也非常担心"))
    single = _service().appraise(AppraisalRequest(user_message="我非常担心"))

    assert "ambivalent" in mixed.signals
    assert "ambivalent" not in single.signals
    # Both pulls are present, but neither hits its un-damped strength.
    assert "joy" in mixed.affect and "anxiety" in mixed.affect


def test_goal_completion_moves_achievement_and_joy() -> None:
    result = _service().appraise(
        AppraisalRequest(user_message="终于做完了", goal_completed=True)
    )

    assert result.needs.get("achievement", 0.0) > 0
    assert result.affect.get("joy", 0.0) > 0
    assert "goal_completed" in result.signals


def test_explicit_external_event_moves_state_without_any_text() -> None:
    result = _service().appraise(
        AppraisalRequest(external_events=[{"type": "loss", "valence": -0.8}])
    )

    assert "anxiety" in result.affect
    assert result.needs.get("safety", 0.0) < 0


def test_relationship_fields_are_always_legal() -> None:
    result = _service().appraise(AppraisalRequest(user_message="谢谢你，我们不信任他"))

    changes = result.relationships[0]["changes"]
    assert changes.keys() <= RELATIONSHIP_FIELDS


# -- §19 conservative, multi-timescale movement -----------------------------


def test_affect_movement_is_clamped_per_turn() -> None:
    service = _service(limits=AppraisalLimits(affect=0.15))

    result = service.appraise(
        AppraisalRequest(
            user_message="非常担心、极其焦虑、特别紧张、十分不安、真的很害怕"
        )
    )

    assert result.affect["anxiety"] <= 0.15 + 1e-9


def test_need_movement_is_clamped_per_turn() -> None:
    service = _service(limits=AppraisalLimits(need=0.10))

    result = service.appraise(
        AppraisalRequest(user_message="为什么怎么会好奇想了解，我自己来，让我决定")
    )

    assert all(abs(delta) <= 0.10 + 1e-9 for delta in result.needs.values())


def test_needs_are_additive_deltas_not_absolute_levels() -> None:
    """MotivationEngine applies ``level + delta``, so small values are correct."""

    result = _service().appraise(AppraisalRequest(user_message="为什么？我很好奇"))

    assert all(abs(delta) < 0.5 for delta in result.needs.values())


def test_relationship_cannot_teleport_in_one_turn() -> None:
    service = _service(limits=AppraisalLimits(relationship=0.08))

    result = service.appraise(
        AppraisalRequest(
            user_message="谢谢谢谢谢谢谢谢谢谢，我太感激你了，非常感动",
            current_relationship={"trust": 0.20},
        )
    )

    trust = result.relationships[0]["changes"]["trust"]
    assert 0.20 < trust < 0.30, "trust must drift, not jump to 90%"


def test_relationship_values_stay_within_the_unit_interval() -> None:
    service = _service()
    low = service.appraise(
        AppraisalRequest(user_message="我完全不信任你，闭嘴", current_relationship={"trust": 0.02})
    )
    high = service.appraise(
        AppraisalRequest(user_message="谢谢你", current_relationship={"trust": 0.99})
    )

    assert low.relationships[0]["changes"]["trust"] >= 0.0
    assert high.relationships[0]["changes"]["trust"] <= 1.0


def test_timescales_differ_emotion_moves_more_than_relationship() -> None:
    limits = AppraisalLimits()
    assert limits.affect > limits.need > limits.relationship


# -- §18 modes --------------------------------------------------------------


def test_default_mode_is_heuristic_and_adds_no_model_call() -> None:
    assert DEFAULT_MODE == "heuristic"
    assert _service().mode == "heuristic"
    assert _service(mode="not-a-mode").mode == "heuristic"


def test_app_wires_the_heuristic_default(app) -> None:
    assert app.config.persona_state_appraisal_mode == "heuristic"
    assert app.sessions.state_appraisal.mode == "heuristic"
    assert app.sessions.state_appraisal.model_appraiser is None


def test_model_mode_without_an_appraiser_degrades_to_heuristic() -> None:
    service = PersonaStateAppraisalService("model")

    result = service.appraise(AppraisalRequest(user_message="我最近很担心项目进度"))

    assert "anxiety" in result.affect
    assert "model appraiser unavailable" in result.summary


def test_hybrid_mode_merges_and_reclamps_model_output() -> None:
    """A model patch obeys the same ceilings as the heuristic one (§19)."""

    appraiser = _ScriptedAppraiser({"affect": {"joy": 1.0}, "needs": {"curiosity": 5.0}})
    service = PersonaStateAppraisalService("hybrid", model_appraiser=appraiser)

    result = service.appraise(AppraisalRequest(user_message="我最近很担心项目进度"))

    assert result.affect["joy"] <= 0.15 + 1e-9
    assert abs(result.needs["curiosity"]) <= 0.10 + 1e-9
    assert "model_refined" in result.signals
    assert appraiser.seen, "the appraiser must receive the heuristic baseline"


def test_hybrid_mode_ignores_illegal_model_keys() -> None:
    appraiser = _ScriptedAppraiser({"affect": {"curiosity": 0.9}, "needs": {"joy": 0.9}})
    service = PersonaStateAppraisalService("hybrid", model_appraiser=appraiser)

    result = service.appraise(AppraisalRequest(user_message="我最近很担心项目进度"))

    assert "curiosity" not in result.affect
    assert "joy" not in result.needs


def test_a_raising_appraiser_does_not_break_the_turn() -> None:
    class _Broken:
        def appraise(self, payload: dict[str, Any]) -> dict[str, Any] | None:
            raise RuntimeError("model down")

    service = PersonaStateAppraisalService("hybrid", model_appraiser=_Broken())

    result = service.appraise(AppraisalRequest(user_message="我最近很担心项目进度"))

    assert "anxiety" in result.affect


# -- §20 public summary -----------------------------------------------------


def test_summary_shows_what_moved_without_revealing_reasoning() -> None:
    result = _service().appraise(
        AppraisalRequest(user_message="我最近很担心项目进度", current_affect={"anxiety": 0.18})
    )

    assert "anxiety" in result.summary
    assert "→" in result.summary
    assert "%" in result.summary


def test_summary_reports_only_what_actually_moved() -> None:
    """§20: the public summary lists movements, never private reasoning."""

    result = _service().appraise(AppraisalRequest(user_message="今天天气是多云。"))

    assert "rel familiarity" in result.summary
    assert "need " not in result.summary
    assert not any(name in result.summary for name in EMOTION_NAMES)


# -- end to end through SessionService --------------------------------------


def _new_persona(app) -> str:  # type: ignore[no-untyped-def]
    persona = app.personas.create(
        display_name="Alex Chen",
        aliases=[],
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode=RunMode.DIGITAL_CONTINUATION,
    )
    return str(persona.id)


def test_commit_turn_moves_real_runtime_state(app) -> None:
    persona_id = _new_persona(app)
    session = app.sessions.start_session(persona_id, "state-flow", counterpart_id="user")

    result = app.sessions.commit_turn(
        persona_id,
        session.id,
        user_message="我最近非常担心项目进度，压力很大",
        persona_response="我理解这份压力，我们一起拆解一下。",
        counterpart_id="user",
    )

    assert "state_summary" in result
    states = {state.name: state for state in app.affect.get_emotions(persona_id)}
    assert states["anxiety"].intensity > 0.0
    assert "anxiety" in result["state_summary"]


def test_next_turn_prompt_reads_the_updated_state(app) -> None:
    """§21: the prompt must be built from state the previous turn actually wrote."""

    persona_id = _new_persona(app)
    session = app.sessions.start_session(persona_id, "prompt-flow", counterpart_id="user")

    before = {
        state.name: state.intensity
        for state in app.sessions.prepare_turn(persona_id, session.id, "开场").current_emotions
    }

    app.sessions.commit_turn(
        persona_id,
        session.id,
        user_message="我最近非常担心项目进度，也很好奇你的判断",
        persona_response="我理解这份压力。",
        counterpart_id="user",
    )

    prepared = app.sessions.prepare_turn(persona_id, session.id, "那接下来呢？")
    after = {state.name: state.intensity for state in prepared.current_emotions}
    needs = {state.name: state.level for state in prepared.current_needs}

    assert after["anxiety"] > before["anxiety"]
    assert needs["curiosity"] > 0.0
    # The prompt carries real state, not a frozen snapshot.
    assert prepared.current_mood or any(
        state.name == "anxiety" for state in prepared.current_emotions
    )


def test_relationship_state_accumulates_conservatively(app) -> None:
    persona_id = _new_persona(app)
    session = app.sessions.start_session(persona_id, "rel-flow", counterpart_id="Jordan")

    trust_values = []
    for _ in range(3):
        app.sessions.commit_turn(
            persona_id,
            session.id,
            user_message="谢谢你的帮助，我明白了",
            persona_response="不客气。",
            counterpart_id="Jordan",
        )
        trust_values.append(
            app.relationships.get_relationship(persona_id, "Jordan").trust
        )

    assert trust_values[0] < trust_values[-1]
    # Three grateful turns must still leave trust far from saturation.
    assert trust_values[-1] < 0.35
    for previous, current in zip(trust_values, trust_values[1:], strict=False):
        assert current - previous <= 0.08 + 1e-6


def test_state_survives_a_read_through_the_runtime_endpoint(app) -> None:
    """The Inspector reads exactly what the appraisal wrote (§13-§15)."""

    persona_id = _new_persona(app)
    session = app.sessions.start_session(persona_id, "runtime-flow", counterpart_id="user")
    app.sessions.commit_turn(
        persona_id,
        session.id,
        user_message="我最近非常担心项目进度",
        persona_response="我理解这份压力。",
        counterpart_id="user",
    )

    runtime = app.runtime_state(persona_id, "main")

    assert {item["name"] for item in runtime["emotions"]} == set(EMOTION_NAMES)
    assert {item["name"] for item in runtime["needs"]} == set(NEED_NAMES)
    anxiety = next(item for item in runtime["emotions"] if item["name"] == "anxiety")
    assert anxiety["intensity"] > 0.0
    assert anxiety["triggers"], "every movement must record a public trigger"
