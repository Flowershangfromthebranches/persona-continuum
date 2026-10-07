from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

from persona_continuum.application._utils import dumps, new_id
from persona_continuum.domain.affect import NeedState
from persona_continuum.domain.persona import PersonaType, RunMode, utc_now
from persona_continuum.domain.relationship import RelationshipState
from persona_continuum.runtime.bond_dynamics import appraise_bond, initial_state


@pytest.fixture()
def persona(app):
    app.personas.create(
        display_name="Runtime probe",
        aliases=[],
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode=RunMode.DIGITAL_CONTINUATION,
        persona_id="bond-probe",
    )
    return "bond-probe"


def turn(app, persona, session, message="今天我给你看看路上拍的照片", act=None):
    counterpart = session.metadata["counterpart_id"]
    prepared = app.sessions.prepare_turn(
        persona,
        session.id,
        message,
        counterpart_id=counterpart,
        external_events=[{"type": act}] if act else [],
    )
    result = app.sessions.commit_turn(
        persona,
        session.id,
        user_message=message,
        persona_response="嗯，接着说，我听着。",
        counterpart_id=counterpart,
    )
    return prepared, result


SCENARIOS = {
    "stranger_to_friend": ("stranger", ["sharing", "attention", "shared_activity", "support"] * 25),
    "stranger_to_partner": (
        "stranger",
        ["support"] * 25 + ["mutual_romance"] * 55 + ["commitment"] * 20,
    ),
    "partner_daily_100": (
        "partner",
        ["sharing", "attention", "conversation", "shared_activity"] * 25,
    ),
    "withdrawal_repair": (
        "partner",
        ["sharing"] * 20 + ["withdrawal"] * 8 + ["repair"] * 12 + ["support"] * 60,
    ),
    "refusal_recovery": ("partner", ["sharing"] * 20 + ["refusal"] + ["sharing"] * 79),
    "long_support": ("partner", ["support"] * 100),
    "betrayal": ("partner", ["sharing"] * 20 + ["betrayal"] + ["withdrawal"] * 79),
    "conflict_recovery": ("partner", ["betrayal"] + ["repair"] * 9 + ["support"] * 90),
    "friend_banter_50": ("friend", ["banter"] * 50),
    "partner_plain_100": ("partner", ["conversation"] * 100),
}


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_trajectories(app, persona, scenario):
    kind, acts = SCENARIOS[scenario]
    session = app.sessions.start_session(persona, initial_relationship={"relationship_kind": kind})
    samples = []
    initial = app.relationships.get_relationship(persona, "user")
    for index, act in enumerate(acts, 1):
        prepared, result = turn(app, persona, session, "这是我们今天的日常交流。", act)
        state = app.relationships.get_relationship(persona, "user")
        if index % 10 == 0:
            sample = {
                key: getattr(state, key)
                for key in (
                    "relationship_kind",
                    "bond_stage",
                    "familiarity",
                    "trust",
                    "affection",
                    "dependence",
                    "resentment",
                    "perceived_threat",
                    "trajectory",
                )
            }
            sample.update(
                turn=index,
                active_emotions={
                    e.name: round(e.intensity, 4)
                    for e in app.affect.get_emotions(persona)
                    if e.intensity > 0.02
                },
                active_needs={n.name: round(n.level, 4) for n in app.motivation.get_needs(persona)},
            )
            samples.append(sample)
    if scenario == "stranger_to_friend":
        assert state.relationship_kind in {"friend", "close_friend"}
        assert state.trust > 0.3 and state.affection > 0.3
    if scenario == "stranger_to_partner":
        assert state.relationship_kind == "partner"
    if kind == "partner":
        assert state.relationship_kind == "partner"
    if scenario in {"partner_daily_100", "partner_plain_100", "long_support", "refusal_recovery"}:
        assert state.trust >= initial.trust and state.affection >= initial.affection
        assert "established intimate partner" in prepared.relationship_stance
        assert "Respect an explicit refusal." in prepared.relationship_stance
    if scenario == "betrayal":
        assert state.trust < initial.trust - 0.2 and state.unresolved_conflict > 0.2
    if scenario == "conflict_recovery":
        assert state.trust > initial.trust - 0.05 and state.unresolved_conflict < 0.05
    if scenario == "friend_banter_50":
        assert state.relationship_kind in {"friend", "close_friend"}
        assert state.resentment == 0
    if scenario == "long_support":
        assert state.dependence > initial.dependence
    output = os.getenv("BOND_TRAJECTORY_OUTPUT")
    if output:
        target = Path(output)
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{scenario}.json").write_text(json.dumps(samples, ensure_ascii=False, indent=2))


def test_important_person_amplifies_affect_and_refusal_preserves_trust():
    stranger = initial_state("p", "u", {})
    partner = initial_state("p", "u", {"relationship_kind": "partner"})
    a = appraise_bond(stranger, "今天不去了，你自己睡吧。", [], {"attachment": 0.8}, {})
    b = appraise_bond(partner, "今天不去了，你自己睡吧。", [], {"attachment": 0.8}, {})
    assert b.affect["anxiety"] > a.affect["anxiety"] * 4
    refusal = appraise_bond(partner, "今天不想亲，想独处。", [], {}, {})
    assert refusal.state.trust == partner.trust
    assert refusal.state.relationship_kind == "partner"
    assert refusal.state.boundary_explicitness == 1
    assert refusal.affect["sadness"] > 0


def test_prior_survives_sessions_and_preview_does_not_commit(app, persona):
    session = app.sessions.start_session(
        persona, initial_relationship={"relationship_kind": "partner"}
    )
    original = app.relationships.get_relationship(persona, "user").model_dump()
    prepared = app.sessions.prepare_turn(persona, session.id, "我特别想你")
    assert "partner" in prepared.relationship_stance
    assert app.relationships.get_relationship(persona, "user").model_dump() == original
    app.sessions.end_session(session.id)
    app.sessions.start_session(persona, initial_relationship={"relationship_kind": "stranger"})
    assert app.relationships.get_relationship(persona, "user").relationship_kind == "partner"
    app.sessions.start_session(persona, counterpart_id="other")
    assert app.relationships.get_relationship(persona, "other").relationship_kind == "stranger"


def test_seed_once_and_homeostasis(app, persona):
    for key, content in {
        "temperament": {"emotion_baselines": {"affection": 0.32}, "decay_rate": 0.2},
        "needs_and_desires": [{"name": "intimacy", "baseline": 0.9}],
        "relationships": [{"counterpart_id": "Alex", "relationship_kind": "partner"}],
    }.items():
        app.database.conn.execute(
            "INSERT INTO compiled_components VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("comp"),
                persona,
                1,
                "persona_component",
                key,
                dumps(content),
                "[]",
                utc_now().isoformat(),
            ),
        )
    app.database.conn.commit()
    app.sessions.start_session(persona, counterpart_id="Alex")
    assert app.relationships.get_relationship(persona, "Alex").relationship_kind == "partner"
    affect = {e.name: e for e in app.affect.get_emotions(persona)}
    assert affect["affection"].baseline == 0.32
    needs = {n.name: n for n in app.motivation.get_needs(persona)}
    assert needs["intimacy"].baseline == 0.9
    app.motivation.update_needs(persona, {"intimacy": -0.6}, "satisfied")
    app.sessions.start_session(persona)
    assert {n.name: n.level for n in app.motivation.get_needs(persona)}[
        "intimacy"
    ] == pytest.approx(0.3, abs=1e-4)
    later = {
        n.name: n.level
        for n in app.motivation.get_needs(persona, now=utc_now() + timedelta(hours=24))
    }
    assert 0.8 < later["intimacy"] < 0.9


def test_signed_reflection_and_replay(app, persona):
    session = app.sessions.start_session(
        persona, initial_relationship={"relationship_kind": "partner"}
    )
    _, committed = turn(app, persona, session)
    before = app.relationships.get_relationship(persona, "user").trust
    app.affect.update_emotions(persona, {"anxiety": 0.6}, "test")
    artifact = dict(
        reflection_artifact_id="refl_signed",
        new_insights=[],
        relationship_deltas=[{"counterpart_id": "user", "changes": {"trust": -0.1}}],
        affect_deltas={"anxiety": -0.2},
        need_deltas={},
        goal_updates=[],
        unresolved_conflicts=[],
        self_narrative_updates=[],
        memory_candidates=[],
        confidence=0.8,
        supporting_turn_ids=[committed["turn_id"]],
    )
    app.sessions.commit_reflection(persona, artifact)
    assert app.relationships.get_relationship(persona, "user").trust == pytest.approx(before - 0.1)
    assert {e.name: e.intensity for e in app.affect.get_emotions(persona)}[
        "anxiety"
    ] == pytest.approx(0.4, abs=1e-4)
    other = app.sessions.start_session(persona)
    turn(app, persona, other)
    app.sessions.delete_session(persona, other.id)
    assert app.relationships.get_relationship(persona, "user").trust == pytest.approx(before - 0.1)


def test_legacy_json_and_stranger_confession():
    state = RelationshipState.model_validate(
        {"persona_id": "p", "counterpart": "u", "familiarity": 0.95}
    )
    assert state.relationship_kind == "stranger"
    for _ in range(100):
        result = appraise_bond(state, "ok", [], {}, {})
        state = result.state
    assert state.dependence == 0
    state = appraise_bond(state, "我爱你", [], {}, {}).state
    assert state.relationship_kind != "partner"
    assert state.trust < 0.1


def test_need_rebound_is_read_idempotent(app, persona):
    app.motivation._save(persona, "main", NeedState(name="intimacy", baseline=0.9, level=0.2))
    app.database.conn.commit()
    now = utc_now() + timedelta(hours=8)
    a = app.motivation.get_needs(persona, now=now)
    b = app.motivation.get_needs(persona, now=now)
    assert [n.level for n in a] == [n.level for n in b]


def test_relationship_memory_lineage_and_counterpart_scope(app, persona):
    session = app.sessions.start_session(
        persona, initial_relationship={"relationship_kind": "partner"}
    )
    for _ in range(12):
        turn(app, persona, session, "我陪你", "support")
    memories = app.database.conn.execute(
        "SELECT id, metadata_json FROM memories WHERE persona_id=?", (persona,)
    ).fetchall()
    semantic = [
        (r["id"], json.loads(r["metadata_json"]))
        for r in memories
        if json.loads(r["metadata_json"]).get("relationship_memory")
    ]
    assert len(semantic) == 1
    assert len(semantic[0][1]["supporting_turn_ids"]) == 12
    assert (
        app.database.conn.execute(
            "SELECT COUNT(*) FROM lineage WHERE child_id=?", (semantic[0][0],)
        ).fetchone()[0]
        == 12
    )
    own = app.sessions.prepare_turn(persona, session.id, "support")
    assert any(m.id == semantic[0][0] for m in own.relevant_memories)
    other = app.sessions.start_session(persona, counterpart_id="other")
    foreign = app.sessions.prepare_turn(persona, other.id, "support", counterpart_id="other")
    assert all(m.id != semantic[0][0] for m in foreign.relevant_memories)


def test_additive_patch_and_atomic_failure(app, persona, monkeypatch):
    session = app.sessions.start_session(
        persona, initial_relationship={"relationship_kind": "partner"}
    )
    app.sessions.commit_turn(
        persona,
        session.id,
        user_message="ok",
        persona_response="ok",
        state_patch={"relationships": [{"counterpart_id": "user", "delta": {"trust": -0.1}}]},
    )
    assert app.relationships.get_relationship(persona, "user").trust == pytest.approx(0.7)
    before = app.relationships.get_relationship(persona, "user").model_dump()
    original = app.sessions._insert_change_event

    def fail(*args, **kwargs):
        if args[2] == "relationship_delta":
            raise RuntimeError("injected failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(app.sessions, "_insert_change_event", fail)
    with pytest.raises(RuntimeError, match="injected"):
        turn(app, persona, session, "我陪你")
    assert app.relationships.get_relationship(persona, "user").model_dump() == before


def test_text_seed_and_explicit_low_traits():
    from persona_continuum.runtime.persona_seed import build_seed

    high = build_seed(
        [
            {
                "component_id": "c",
                "component_key": "needs_and_desires",
                "content": ["天然具有建立依恋的驱动力，主动依恋与亲密诉求"],
            }
        ]
    )
    low = build_seed(
        [
            {
                "component_id": "c",
                "component_key": "temperament",
                "content": {"description": "冷淡克制，依恋需求低"},
            }
        ]
    )
    assert high.need_baselines["attachment"] > low.need_baselines["attachment"]
    assert high.emotion_baselines["affection"] > low.emotion_baselines["affection"]


@pytest.mark.anyio
async def test_direct_room_uses_persistent_person_counterpart(app, persona):
    from persona_continuum.room.models import ParticipantSlot, RoomMode
    from persona_continuum.room.prompt_composer import PromptComposer

    slot = ParticipantSlot(
        participant_id="speaker",
        persona_id=persona,
        runtime_selection="fake_agent",
        model_selection="fake-gpt-5",
        initial_relationship={"relationship_kind": "partner"},
    )
    room = app.orchestrator.create_room(mode=RoomMode.DIRECT_CHAT, participants=[slot])
    await app.orchestrator.start_room(room.id)
    session = next(
        s for s in app.sessions.list_sessions(persona) if s.metadata.get("room_id") == room.id
    )
    assert session.metadata["counterpart_id"] == "user"
    prepared = app.sessions.prepare_turn(persona, session.id, "我特别想你")
    active = app.orchestrator.get_room(room.id)
    system, _, _ = PromptComposer().compose_turn_prompt(
        slot=slot,
        binding=active.binding_snapshots["speaker"],
        prepared=prepared,
        dynamic_recall_memories=[],
    )
    assert "Relationship Stance" in system and "partner" in system
    assert "established intimate partner" in system
    assert "Respect an explicit refusal." in system


def test_room_interaction_targets_person_not_room(app, persona):
    app.sessions.initialize_relationship(persona, "Alex", {"relationship_kind": "partner"})
    session = app.sessions.start_session(persona, counterpart_id="room:test", room_id="test")
    prepared = app.sessions.prepare_turn(
        persona,
        session.id,
        "room topic",
        counterpart_id="room:test",
        interaction_counterpart_id="Alex",
        interaction_message="今天不去了，你自己睡吧。",
    )
    assert prepared.relationship_state.counterpart == "Alex"
    assert {e.name: e.intensity for e in prepared.current_emotions}["anxiety"] > 0.05
    app.sessions.commit_turn(
        persona,
        session.id,
        user_message="room topic",
        persona_response="怎么了？",
        counterpart_id="room:test",
    )
    assert app.relationships.get_relationship(persona, "Alex").trajectory == "stable"
    assert app.relationships.get_relationship(persona, "Alex").recent_acts[-1] == "withdrawal"
    assert app.relationships.get_relationship(persona, "room:test").meaningful_interactions == 0


def test_reciprocal_romance_not_unilateral_declaration():
    s = initial_state("p", "u", {"relationship_kind": "friend"})
    for _ in range(8):
        s = appraise_bond(s, "我喜欢你", [], {}, {}, response="我也喜欢你").state
    assert s.relationship_kind in {"flirting", "dating"}
    assert s.romantic_evidence == 8
    unilateral = appraise_bond(
        initial_state("p", "u", {}), "我爱你", [], {}, {}, response="谢谢"
    ).state
    assert unilateral.romantic_evidence == 0 and unilateral.relationship_kind == "stranger"


def test_branch_scoped_extractive_reflection(app, persona):
    main = app.sessions.start_session(persona)
    turn(app, persona, main, "main_only_message")
    continuation = app.continuations.create(persona, "alternate")
    branch = app.continuations.create_branch(continuation.id)
    alternate = app.sessions.start_session(persona, branch_id=branch.id)
    turn(app, persona, alternate, "branch_only_message")
    result = app.sessions.run_reflection(persona, branch_id=branch.id)
    assert "branch_only_message" in result["summary"]
    assert "main_only_message" not in result["summary"]
    assert result["semantic_reflection_required"] is True


def test_legacy_seed_adopts_baseline_without_overwriting_lived_state(app, persona):
    from persona_continuum.domain.affect import AffectState

    app.affect._save(persona, "main", AffectState(name="affection", intensity=0.6))
    app.motivation._save(persona, "main", NeedState(name="attachment", level=0.2))
    app.database.conn.execute(
        "INSERT INTO compiled_components VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id("comp"),
            persona,
            1,
            "persona_component",
            "needs_and_desires",
            dumps(["主动依恋与亲密诉求，具有建立依恋的驱动力"]),
            "[]",
            utc_now().isoformat(),
        ),
    )
    app.database.conn.commit()
    app.sessions.start_session(persona)
    affect = {s.name: s for s in app.affect.get_emotions(persona)}["affection"]
    attachment = {s.name: s for s in app.motivation.get_needs(persona)}["attachment"]
    assert affect.baseline == 0.14 and affect.intensity == pytest.approx(0.6, abs=1e-4)
    assert attachment.baseline == 0.7 and attachment.level == pytest.approx(0.2, abs=1e-4)
