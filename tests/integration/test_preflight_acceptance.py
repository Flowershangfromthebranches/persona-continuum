from __future__ import annotations

import asyncio

from persona_continuum.domain.memory import MemoryType
from scripts.preflight_acceptance import run_mcp_flow, run_service_flow


def test_preflight_acceptance_flow(tmp_path) -> None:
    project_root = __import__("pathlib").Path(__file__).resolve().parents[2]
    data_dir = tmp_path / "empty-data"
    result = run_service_flow(project_root, data_dir)
    assert result["persona_id"] == "alex-chen"
    assert result["branch_a"] != result["branch_b"]

    app_db = data_dir / "persona_continuum.sqlite"
    assert app_db.exists()


def test_mcp_stdio_flow_from_empty_data_dir(tmp_path) -> None:
    project_root = __import__("pathlib").Path(__file__).resolve().parents[2]
    data_dir = tmp_path / "mcp-empty-data"
    result = asyncio.run(run_mcp_flow(project_root, data_dir))
    assert result["tool_count"] >= 44


def test_privacy_delete_surfaces_remove_private_data(app, tmp_path) -> None:
    source = tmp_path / "private.md"
    source.write_text("private source secret", encoding="utf-8")
    persona = app.personas.create(
        display_name="Delete Check",
        aliases=[],
        persona_type=__import__(
            "persona_continuum.domain.persona", fromlist=["PersonaType"]
        ).PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode=__import__(
            "persona_continuum.domain.persona", fromlist=["RunMode"]
        ).RunMode.DIGITAL_CONTINUATION,
    )
    added = app.personas.add_sources(persona.id, [source])
    memory = app.memories.add_memory(
        persona.id,
        content="private memory",
        memory_type=MemoryType.SEMANTIC,
        source_kind="historical_self_report",
        source_id=added[0].id,
    )
    session = app.sessions.start_session(persona.id, "private")
    app.sessions.commit_turn(
        persona.id,
        session.id,
        user_message="private question",
        persona_response="private answer",
    )

    assert app.personas.delete_source(persona.id, added[0].id)
    assert app.memories.get_memory(memory.id).validity == "source_deleted"
    assert app.sessions.delete_session(persona.id, session.id)


def test_reflection_and_context_budget_are_real_state_updates(app) -> None:
    persona = app.personas.create(
        display_name="Reflect Check",
        aliases=[],
        persona_type=__import__(
            "persona_continuum.domain.persona", fromlist=["PersonaType"]
        ).PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode=__import__(
            "persona_continuum.domain.persona", fromlist=["RunMode"]
        ).RunMode.DIGITAL_CONTINUATION,
    )
    app.memories.add_memory(
        persona.id,
        content="x" * 400,
        memory_type=MemoryType.SEMANTIC,
        source_kind="historical_inference",
    )
    session = app.sessions.start_session(persona.id, "reflect")
    app.sessions.commit_turn(
        persona.id,
        session.id,
        user_message="What failed?",
        persona_response="I moved too quickly.",
        user_feedback="useful",
    )
    reflection = app.sessions.run_reflection(persona.id)
    assert reflection["memory_id"]
    assert "What failed?" in reflection["summary"]
    prepared = app.sessions.prepare_turn(
        persona.id,
        session.id,
        "budget",
        max_context_items=10,
        max_context_size=120,
    )
    assert sum(len(memory.content) for memory in prepared.relevant_memories) <= 120


def test_continuation_branches_wait_for_host_and_record_structured_paths(app) -> None:
    persona = app.personas.create(
        display_name="Branch Check",
        aliases=[],
        persona_type=__import__(
            "persona_continuum.domain.persona", fromlist=["PersonaType"]
        ).PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode=__import__(
            "persona_continuum.domain.persona", fromlist=["RunMode"]
        ).RunMode.HISTORICAL_SNAPSHOT,
    )
    continuation = app.continuations.create(persona.id, "long_term_survival")
    app.continuations.add_world_events(
        continuation.id,
        [
            {"date": "2026-01-01", "content": "Event A"},
            {"date": "2026-02-01", "content": "Event B"},
        ],
    )
    branch_a = app.continuations.create_branch(continuation.id, seed=0)
    branch_b = app.continuations.create_branch(continuation.id, seed=1)
    advanced_a = app.continuations.advance_branch(branch_a.id, "2026-03-01")
    advanced_b = app.continuations.advance_branch(branch_b.id, "2026-03-01")
    assert advanced_a.status == "waiting_for_host"
    assert advanced_b.status == "waiting_for_host"

    committed_a = app.continuations.commit_step(
        branch_a.id,
        {
            "evaluated_events": [{"date": "2026-03-01", "content": "Event A was prioritized"}],
            "chosen_actions": [
                {"action": "Ask for a small trust audit", "reason": "cautious branch posture"}
            ],
            "world_state_delta": {"audit": "small"},
            "persona_state_delta": {"risk_posture": "cautious"},
            "relationship_deltas": [],
            "affect_deltas": {},
            "goal_deltas": [{"goal_id": "trust", "status": "active", "delta": 0.1}],
            "new_memories": [],
            "rejected_alternatives": [{"option": "Ignore audit", "reason": "unsupported"}],
            "causal_explanation": "Caution follows the branch evidence.",
            "uncertainty": 0.25,
            "evidence_links": [],
            "next_step_date": "2026-04-01",
        },
    )
    committed_b = app.continuations.commit_step(
        branch_b.id,
        {
            "evaluated_events": [{"date": "2026-03-01", "content": "Event B was prioritized"}],
            "chosen_actions": [
                {"action": "Publish a public note", "reason": "transparent branch posture"}
            ],
            "world_state_delta": {"audit": "public"},
            "persona_state_delta": {"risk_posture": "transparent"},
            "relationship_deltas": [],
            "affect_deltas": {},
            "goal_deltas": [{"goal_id": "trust", "status": "active", "delta": 0.2}],
            "new_memories": [],
            "rejected_alternatives": [{"option": "Stay silent", "reason": "unsupported"}],
            "causal_explanation": "Transparency follows the branch evidence.",
            "uncertainty": 0.3,
            "evidence_links": [],
            "next_step_date": "2026-04-01",
        },
    )
    assert committed_a.persona_state["deltas"] != committed_b.persona_state["deltas"]
