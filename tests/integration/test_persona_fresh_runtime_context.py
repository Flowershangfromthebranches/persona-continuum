from __future__ import annotations

from tests.fixtures.base_persona import build_base_persona


def test_persona_fresh_runtime_context_contains_production_components(app) -> None:
    persona_id, _ = build_base_persona(app)
    session = app.sessions.start_session(persona_id, "fresh runtime")

    prepared = app.sessions.prepare_turn(
        persona_id,
        session.id,
        "Should I verify the risk and keep a fallback?",
        max_context_items=20,
    )
    by_key = prepared.compiled_persona_context["by_key"]

    for key in (
        "identity_profile",
        "decision_heuristics",
        "mental_models",
        "temperament",
        "expression_style",
        "attachment_patterns",
        "defenses",
        "values",
        "relationships",
    ):
        assert by_key[key]
    assert prepared.relevant_persona_facts
    assert prepared.relevant_memories
