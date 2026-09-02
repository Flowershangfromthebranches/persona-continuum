"""Retrieval policy and knowledge-isolation regression tests (P0 §3 / §4 / §8).

The historical failure: fictional evidence was force-tagged
``counterfactual_simulated`` while retrieval only admitted ``historical*``, so a
fictional persona could compile with an entirely empty biography.  The fix gives
fiction its own provenance family and makes visibility a policy decision driven
by the persona's nature -- never by relabelling fiction as history.
"""

from __future__ import annotations

import pytest

from persona_continuum.domain.persona import PersonaType
from persona_continuum.domain.provenance import (
    AUTHOR_ONLY,
    CHARACTER_VISIBLE,
    EVALUATION_ONLY,
    FICTIONAL_KINDS,
    HISTORICAL_KINDS,
    SIMULATED_KINDS,
    PersonaRetrievalPolicy,
    normalise_fictional_provenance,
    normalise_material_scope,
)


def _policy(persona_type: PersonaType, branch_id: str | None = None) -> PersonaRetrievalPolicy:
    return PersonaRetrievalPolicy(persona_type=persona_type, branch_id=branch_id)


def test_real_persona_reads_historical_only() -> None:
    allowed = _policy(PersonaType.PUBLIC_HISTORICAL_PERSON).allowed_source_kinds()
    assert allowed >= HISTORICAL_KINDS
    assert allowed.isdisjoint(FICTIONAL_KINDS)


def test_fictional_persona_reads_canon_not_history() -> None:
    allowed = _policy(PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON).allowed_source_kinds()
    assert allowed >= FICTIONAL_KINDS
    assert allowed.isdisjoint(HISTORICAL_KINDS)


def test_branch_adds_simulated_kinds() -> None:
    base = _policy(PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON, branch_id="main")
    branch = _policy(PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON, branch_id="branch_future_b")
    assert not (base.allowed_source_kinds() & SIMULATED_KINDS)
    assert branch.allowed_source_kinds() >= SIMULATED_KINDS
    assert branch.allows("counterfactual_simulated")
    assert not base.allows("counterfactual_simulated")


def test_real_branch_reads_history_plus_simulation() -> None:
    branch = _policy(PersonaType.PUBLIC_HISTORICAL_PERSON, branch_id="branch_cf")
    assert branch.allowed_source_kinds() >= HISTORICAL_KINDS
    assert "counterfactual_simulated" in branch.allowed_source_kinds()
    assert branch.allowed_source_kinds().isdisjoint(FICTIONAL_KINDS)


def test_unknown_provenance_is_rejected() -> None:
    policy = _policy(PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON)
    assert not policy.allows("made_up_kind")


def test_rejection_reason_is_human_readable() -> None:
    policy = _policy(PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON)
    reason = policy.rejected_source_kinds("historical_self_report")
    assert "historical_self_report" in reason
    assert "fictional_or_synthetic_person" in reason


# ---------------------------------------------------------------------------
# Provenance normalisation
# ---------------------------------------------------------------------------


def test_fictional_normalisation_uses_fictional_family() -> None:
    artifact = {
        "claims": [
            {"content": "a", "claim_type": "historical_self_report"},
            {"content": "b", "claim_type": "counterfactual_simulated"},
        ],
        "memories": [{"content": "m", "source_kind": "historical_inference"}],
    }
    assert normalise_fictional_provenance(artifact) is True
    kinds = {claim["claim_type"] for claim in artifact["claims"]}
    assert kinds == {"fictional_author_defined"}
    assert artifact["memories"][0]["source_kind"] == "fictional_author_defined"


def test_fictional_normalisation_never_produces_history() -> None:
    artifact = {"claims": [{"content": "a", "claim_type": "historical_self_report"}]}
    normalise_fictional_provenance(artifact)
    assert artifact["claims"][0]["claim_type"].startswith("fictional_")
    assert artifact["claims"][0]["claim_type"] != "counterfactual_simulated"


def test_fictional_normalisation_is_idempotent() -> None:
    artifact = {"claims": [{"content": "a", "claim_type": "fictional_canon"}]}
    assert normalise_fictional_provenance(artifact) is False
    assert artifact["claims"][0]["claim_type"] == "fictional_canon"


# ---------------------------------------------------------------------------
# Material scope
# ---------------------------------------------------------------------------


def test_unknown_scope_fails_closed_to_author_only() -> None:
    """A mislabelled scope must hide content, never expose it."""
    assert normalise_material_scope(None) == AUTHOR_ONLY
    assert normalise_material_scope("") == AUTHOR_ONLY
    assert normalise_material_scope("Project O") == AUTHOR_ONLY
    assert normalise_material_scope(CHARACTER_VISIBLE) == CHARACTER_VISIBLE


def test_evaluation_and_author_scopes_are_never_character_visible() -> None:
    from persona_continuum.domain.provenance import NON_CHARACTER_SCOPES

    assert AUTHOR_ONLY in NON_CHARACTER_SCOPES
    assert EVALUATION_ONLY in NON_CHARACTER_SCOPES
    assert CHARACTER_VISIBLE not in NON_CHARACTER_SCOPES


@pytest.mark.parametrize(
    ("persona_type", "kind", "expected"),
    [
        (PersonaType.PUBLIC_HISTORICAL_PERSON, "historical_self_report", True),
        (PersonaType.PUBLIC_HISTORICAL_PERSON, "fictional_canon", False),
        (PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON, "fictional_author_defined", True),
        (PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON, "historical_self_report", False),
    ],
)
def test_scope_matrix(persona_type: PersonaType, kind: str, expected: bool) -> None:
    assert _policy(persona_type).allows(kind) is expected
