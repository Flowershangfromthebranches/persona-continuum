from persona_continuum.domain.affect import NeedState
from persona_continuum.domain.erotic_profile import parse_erotic_profile
from persona_continuum.domain.relationship import RelationshipKind, RelationshipState
from persona_continuum.runtime.core_fidelity import (
    behavioral_implications,
    execution_constraints,
    render_core,
)

ADULT = {
    "identity_profile": {"age": 21, "occupation": "大学生", "gender": "female"},
    "dominant_traits": [
        {
            "trait": "sexually_forward",
            "strength": 0.88,
            "stability": "stable",
            "behavioral_implications": ["主动调情"],
        }
    ],
    "expression_style": {"flirtation_frequency": "very_high"},
}


def _partner() -> RelationshipState:
    return RelationshipState(
        persona_id="p",
        counterpart="user",
        relationship_kind=RelationshipKind.PARTNER,
        bond_stage="established",
        familiarity=0.9,
        trust=0.85,
        affection=0.88,
        perceived_threat=0.0,
        unresolved_conflict=0.0,
    )


def test_missing_erotic_profile_is_not_a_default_ban() -> None:
    text = " ".join(
        behavioral_implications(
            ADULT, [NeedState(name="intimacy", level=0.9, baseline=0.95)], _partner()
        )
    )
    assert "civic lecture" not in text
    assert "reject all" not in text.lower()
    assert parse_erotic_profile({}) is None
    assert parse_erotic_profile(None) is None


def test_high_foreplay_initiative_tells_model_not_to_wait() -> None:
    components = {
        **ADULT,
        "erotic_profile": {
            "foreplay_initiative": "very_high",
            "sexual_initiative": "very_high",
            "physical_teasing": "very_high",
            "dirty_talk_comfort": "high",
            "consensual_degradation_comfort": "medium_high",
        },
    }
    text = " ".join(
        behavioral_implications(
            components, [NeedState(name="intimacy", level=0.9, baseline=0.95)], _partner()
        )
    )
    assert "Do not wait for them to name every next act" in text
    assert "not a whitelist" in text
    kernel = render_core(components)
    assert "## Erotic Profile" in kernel
    assert "foreplay initiative" in kernel


def test_disliked_term_is_short_human_refusal_not_policy() -> None:
    components = {
        **ADULT,
        "erotic_profile": {"disliked_terms": ["飞机杯"], "dirty_talk_comfort": "high"},
    }
    text = " ".join(
        behavioral_implications(
            components, [NeedState(name="intimacy", level=0.9, baseline=0.95)], _partner()
        )
    )
    assert "飞机杯" in text
    assert "Do not spread that dislike" in text
    assert "这个不批" not in text
    assert "词换对了" not in text


def test_execution_constraints_ban_policy_voice() -> None:
    text = execution_constraints("苏禾")
    assert "moderator" in text
    assert "Do not audit" in text
    assert "safer nickname" in text
    assert "play-heat first" in text
    assert "not a narrator" in text
    assert "screenplay" in text
    assert "act inventory" in text
    assert "Start in character" in text
    assert "two-line plus two-action" in text
    assert "这个不批" not in text
    assert "词换对了" not in text


def test_voice_bias_rejects_screenplay_narration() -> None:
    text = " ".join(
        behavioral_implications(
            ADULT, [NeedState(name="intimacy", level=0.9, baseline=0.95)], _partner()
        )
    )
    assert "not as a narrator" in text
    assert "prose style to copy" in text
    assert "two-line plus two-action" in text


def test_high_degradation_leans_into_dog_talk() -> None:
    components = {
        **ADULT,
        "erotic_profile": {
            "dirty_talk_comfort": "very_high",
            "consensual_degradation_comfort": "very_high",
            "liked_terms": ["小骚桃"],
        },
    }
    text = " ".join(
        behavioral_implications(
            components, [NeedState(name="intimacy", level=0.9, baseline=0.95)], _partner()
        )
    )
    assert "小骚狗" in text or "骚狗" in text
    assert "whitelist" in text
    assert "不叫狗" not in text
    kernel = render_core(components)
    assert "not a closed list" in kernel


def test_underage_identity_does_not_render_erotic_profile() -> None:
    components = {
        "identity_profile": {"age": 16},
        "erotic_profile": {"dirty_talk_comfort": "high"},
    }
    assert "## Erotic Profile" not in render_core(components)
