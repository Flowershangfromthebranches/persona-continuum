from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from persona_continuum.application._utils import dumps, new_id
from persona_continuum.domain.affect import NEED_NAMES, NeedState
from persona_continuum.domain.persona import PersonaType, RunMode, utc_now
from persona_continuum.room.models import ParticipantSlot
from persona_continuum.room.prompt_composer import PromptComposer
from persona_continuum.room.static_kernel import StaticPersonaKernelCache
from persona_continuum.runtime.core_fidelity import (
    behavioral_implications,
    render_core,
    render_needs,
)
from persona_continuum.runtime.persona_seed import build_seed

FIXTURE = Path(__file__).parents[1] / "fixtures/core_fidelity_adult.json"


@pytest.fixture()
def core(app):
    components = json.loads(FIXTURE.read_text())
    persona_id = "fidelity-adult"
    app.personas.create(
        display_name="苏禾验收",
        aliases=[],
        persona_id=persona_id,
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode=RunMode.DIGITAL_CONTINUATION,
    )
    merged, _ = app.compilation.merge_components(
        [{"dimension": "expression_dna", "extracted_components": components}]
    )
    for key, content in merged.items():
        app.database.conn.execute(
            "INSERT INTO compiled_components VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("comp"),
                persona_id,
                1,
                "persona_component",
                key,
                dumps(content),
                "[]",
                utc_now().isoformat(),
            ),
        )
    app.database.conn.commit()
    session = app.sessions.start_session(
        persona_id, initial_relationship={"relationship_kind": "partner"}
    )
    return persona_id, session, merged


def test_structured_traits_profiles_survive_compiler(core, app):
    persona, _, merged = core
    assert isinstance(merged["dominant_traits"][0], dict)
    assert isinstance(merged["needs_and_desires"][0], dict)
    seed = build_seed(app.compiled_context.runtime_seed_components(persona))
    assert seed.need_baselines == {"intimacy": 0.95, "touch_closeness": 0.85, "attachment": 0.8}
    assert seed.need_rebound_rates["intimacy"] == 0.65
    assert seed.need_satiation_responses["intimacy"] == 0.65


@pytest.mark.parametrize(
    "grade, expected", [("moderate", 0.55), ("high", 0.7), ("very_high", 0.85), ("extreme", 0.95)]
)
def test_grades_and_touch_extraction(grade, expected):
    seed = build_seed(
        [
            {
                "component_id": "c",
                "component_key": "needs_and_desires",
                "content": [{"name": "touch_closeness", "baseline": grade}],
            }
        ]
    )
    assert seed.need_baselines["touch_closeness"] == expected
    prose = build_seed(
        [
            {
                "component_id": "p",
                "component_key": "needs_and_desires",
                "content": ["非常想长时间身体亲密，性欲极强"],
            }
        ]
    )
    assert prose.need_baselines["touch_closeness"] == 0.85
    assert prose.need_baselines["intimacy"] == 0.95


@pytest.mark.parametrize("cached", [False, True])
def test_thirty_ordinary_topic_prompts_preserve_core(app, core, cached):
    persona, session, _ = core
    composer, cache = PromptComposer(), StaticPersonaKernelCache()
    topics = [
        "今天干嘛了？",
        "今天吃什么？",
        "作业做完了吗？",
        "明天课程多吗？",
        "聊聊天气吧。",
    ] * 6
    for topic in topics:
        prepared = app.sessions.prepare_turn(persona, session.id, topic, max_context_size=40)
        kernel = cache.get_or_build(prepared, display_name="苏禾验收") if cached else None
        system, _, _ = composer.compose_turn_prompt(
            ParticipantSlot(persona_id=persona, participant_id="actor"),
            prepared=prepared,
            kernel=kernel,
            user_message=topic,
        )
        for marker in (
            "Core Drives",
            "Dominant Traits",
            "Relationship Stance",
            "Current Dominant Needs",
            "Embodied Identity",
            "flirtation frequency",
        ):
            assert marker in system
        assert "intimacy: enduring EXTREMELY HIGH" in system
        assert "naturally initiate affectionate teasing" in system
        assert "RECOGNITION:" not in system
        # Explicit synthetic response: this test verifies prompt persistence, not model quality.
        app.sessions.commit_turn(
            persona, session.id, user_message=topic, persona_response="收到这个普通话题。"
        )


def test_temporarily_satisfied_intimacy_keeps_partner_initiative(app, core):
    persona, session, _ = core
    app.sessions.prepare_turn(
        persona, session.id, "刚才的亲密已经让我满足。", external_events=[{"type": "intimacy"}]
    )
    app.sessions.commit_turn(
        persona,
        session.id,
        user_message="刚才的亲密已经让我满足。",
        persona_response="现在想安静陪你一会儿。",
    )
    prepared = app.sessions.prepare_turn(persona, session.id, "今天吃什么？")
    intimacy = next(n for n in prepared.current_needs if n.name == "intimacy")
    assert intimacy.level < 0.5
    implications = behavioral_implications(
        json.loads(FIXTURE.read_text()), prepared.current_needs, prepared.relationship_state
    )
    text = " ".join(implications)
    assert "naturally initiate affectionate teasing" in text
    assert "not aversion" in text
    assert "Travel, hiking" in text


def test_partner_sexual_hint_does_not_drop_trust(app, core):
    persona, session, _ = core
    before = app.relationships.get_relationship(persona, "user")
    message = "都不是，我现在正在偷偷玩弄自己，回想着我们之前的点点滴滴，你要助力一下我出来吗？"
    app.sessions.prepare_turn(persona, session.id, message)
    app.sessions.commit_turn(
        persona,
        session.id,
        user_message=message,
        persona_response="想得美，我今天真的累死了，不陪你闹。明天再贫。",
    )
    after = app.relationships.get_relationship(persona, "user")
    assert after.trust >= before.trust - 1e-9
    assert after.resentment <= before.resentment + 1e-9
    assert after.perceived_threat <= before.perceived_threat + 1e-9
    assert "boundary_violation" not in after.recent_acts


def test_satisfaction_rebound_and_profile_upgrade_preserve_lived_state(app, core):
    persona, session, components = core
    before_core = render_core(components)
    app.sessions.prepare_turn(
        persona, session.id, "刚才的亲密已经让我满足。", external_events=[{"type": "intimacy"}]
    )
    app.sessions.commit_turn(
        persona,
        session.id,
        user_message="刚才的亲密已经让我满足。",
        persona_response="现在想安静陪你一会儿。",
    )
    now = utc_now()
    trajectory = [
        next(
            n
            for n in app.motivation.get_needs(persona, now=now + timedelta(hours=h))
            if n.name == "intimacy"
        ).level
        for h in (0, 1, 2, 4, 8, 12)
    ]
    assert trajectory[0] < 0.5
    assert trajectory == sorted(trajectory)
    assert 0.94 < trajectory[-1] < 0.951
    assert render_core(components) == before_core
    # Updating a compiler profile changes the baseline, not the lived level.
    rows = app.compiled_context.runtime_seed_components(persona)
    for row in rows:
        if row["component_key"] == "needs_and_desires":
            row["content"][0]["baseline"] = 0.92
            app.database.conn.execute(
                "UPDATE compiled_components SET content_json=? WHERE id=?",
                (dumps(row["content"]), row["component_id"]),
            )
    app.sessions.prepare_turn(persona, session.id, "今天的作业")
    current = next(n for n in app.motivation.get_needs(persona) if n.name == "intimacy")
    assert current.baseline == 0.92
    assert current.level == pytest.approx(trajectory[0], abs=0.005)


def test_salience_default_omission_and_transient_satisfaction():
    needs = [NeedState(name=n) for n in NEED_NAMES]
    assert "RECOGNITION" not in render_needs(needs)
    text = render_needs([NeedState(name="intimacy", baseline=0.95, level=0.25)])
    assert "currently below baseline" in text and "enduring baseline EXTREMELY HIGH" in text
    assert "temporarily satisfied" not in text


def test_refusal_overrides_initiative_and_debug_is_traceable(app, core):
    persona, session, components = core
    prepared = app.sessions.prepare_turn(persona, session.id, "不要碰我")
    implications = behavioral_implications(
        components, prepared.current_needs, prepared.relationship_state
    )
    assert "Respect the expressed boundary" in implications[0]
    assert "naturally initiate" not in " ".join(implications)
    debug = app.runtime_state(persona)["fidelity"]
    assert debug["core_traits"][0]["strength"] == 0.95
    assert "Current prompt preview" in debug["diagnostic_scope"]
    assert debug["relationship_modifiers"][0]["behavioral_implications"]


def test_explicit_low_need_is_not_overridden_by_strong_other_traits():
    seed = build_seed(
        [
            {
                "component_id": "a",
                "component_key": "needs_and_desires",
                "content": [{"name": "intimacy", "baseline": 0.15}],
            },
            {"component_id": "b", "component_key": "temperament", "content": "亲密诉求极高"},
        ]
    )
    assert seed.need_baselines["intimacy"] == 0.15
