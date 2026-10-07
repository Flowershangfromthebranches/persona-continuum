from persona_continuum.domain.affect import NeedState
from persona_continuum.domain.relationship import RelationshipKind, RelationshipState
from persona_continuum.runtime.bond_dynamics import relationship_stance
from persona_continuum.runtime.core_fidelity import (
    behavioral_implications,
    established_adult_partner,
)

COMPONENTS = {
    "dominant_traits": [
        {
            "trait": "exceptionally_high_sex_drive",
            "strength": 0.97,
            "stability": "stable",
            "behavioral_implications": ["性欲是持续驱动力"],
        },
        {
            "trait": "sexually_forward",
            "strength": 0.88,
            "stability": "stable",
            "behavioral_implications": ["主动调情"],
        },
    ],
    "expression_style": {"flirtation_frequency": "very_high", "initiative": "very_high"},
}


def _partner(**overrides: object) -> RelationshipState:
    data = dict(
        persona_id="p",
        counterpart="user",
        relationship_kind=RelationshipKind.PARTNER,
        bond_stage="established",
        familiarity=0.97,
        trust=0.38,
        affection=0.38,
        perceived_threat=0.0,
        unresolved_conflict=0.0,
        boundary_explicitness=0.58,
    )
    data.update(overrides)
    return RelationshipState.model_validate(data)


def test_established_partner_allows_high_familiarity_even_if_trust_is_catching_up() -> None:
    assert established_adult_partner(_partner()) is True
    assert established_adult_partner(_partner(trust=0.38, familiarity=0.2)) is True
    assert established_adult_partner(_partner(relationship_kind=RelationshipKind.FRIEND)) is False
    assert established_adult_partner(_partner(perceived_threat=0.4)) is False


def test_low_current_intimacy_still_asks_for_playful_initiation() -> None:
    needs = [NeedState(name="intimacy", baseline=0.95, level=0.31)]
    text = " ".join(behavioral_implications(COMPONENTS, needs, _partner()))
    assert "naturally initiate affectionate teasing" in text
    assert "not aversion" in text
    assert "Travel, hiking" in text
    assert "coerce" not in text.lower()


def test_explicit_refusal_still_overrides_initiative() -> None:
    state = _partner(recent_acts=["conversation", "refusal"])
    implications = behavioral_implications(
        COMPONENTS, [NeedState(name="intimacy", baseline=0.95, level=0.9)], state
    )
    assert implications[0].startswith("Respect the expressed boundary")
    assert "naturally initiate" not in " ".join(implications)


def test_partner_stance_does_not_equate_adult_hints_with_violation() -> None:
    stance = relationship_stance(_partner(), {"intimacy": 0.31, "attachment": 0.66})
    assert "established intimate partner" in stance
    assert "Respect an explicit refusal." in stance
    assert "coercion" not in stance
    assert "humiliation" not in stance
    assert "reassess" not in stance


def test_anti_patterns_are_kernel_visible() -> None:
    from persona_continuum.runtime.core_fidelity import render_core

    text = render_core(
        {
            **COMPONENTS,
            "anti_patterns": ["不要把旅游默认解释为过夜"],
        }
    )
    assert "## Voice Anti-Patterns" in text
    assert "不要把旅游默认解释为过夜" in text


def test_render_needs_does_not_invent_satiation() -> None:
    from persona_continuum.runtime.core_fidelity import render_needs

    text = render_needs([NeedState(name="intimacy", baseline=0.95, level=0.25)])
    assert "currently below baseline" in text
    assert "temporarily satisfied" not in text
    satiated = render_needs(
        [
            NeedState(
                name="intimacy",
                baseline=0.95,
                level=0.25,
                last_satiated_at=__import__("datetime").datetime.now(
                    __import__("datetime").UTC
                ),
            )
        ]
    )
    assert "temporarily satisfied" in satiated
