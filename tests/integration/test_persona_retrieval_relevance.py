from __future__ import annotations

from persona_continuum.application.session_service import EVALUATION_CONTEXT_TAGS
from persona_continuum.domain.memory import MemoryType
from tests.fixtures.base_persona import build_base_persona


def test_persona_retrieval_relevance_prefers_query_matched_fact(app) -> None:
    persona_id, _ = build_base_persona(app)
    app.memories.add_memory(
        persona_id,
        content="失业时优先保住房租和经济安全缓冲。",
        memory_type=MemoryType.SEMANTIC,
        importance=0.9,
        source_kind="fictional_author_defined",
        metadata={"material_scope": "character_visible"},
    )
    app.memories.add_memory(
        persona_id,
        content="面对陌生人的异常信息，先谨慎验证，不轻信。",
        memory_type=MemoryType.SEMANTIC,
        importance=0.8,
        source_kind="fictional_author_defined",
        metadata={"material_scope": "character_visible"},
    )

    session = app.sessions.start_session(persona_id, "retrieval")
    prepared = app.sessions.prepare_turn(persona_id, session.id, "失业 金钱安全")

    assert "房租" in prepared.relevant_persona_facts[0]
    assert "经济安全" in prepared.relevant_persona_facts[0]


def test_persona_retrieval_backfills_visible_compiled_claims(app) -> None:
    persona_id, _ = build_base_persona(app, persona_id="claim-retrieval")
    session = app.sessions.start_session(persona_id, "claim retrieval")

    prepared = app.sessions.prepare_turn(persona_id, session.id, "verifies risk")

    assert "Lin verifies risk before acting." in prepared.relevant_persona_facts


def test_evaluation_context_tags_are_explicitly_non_runtime() -> None:
    assert {"evaluation_only", "test_fixture", "验收题"} <= EVALUATION_CONTEXT_TAGS
