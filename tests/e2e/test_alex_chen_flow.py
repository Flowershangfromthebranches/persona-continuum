from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from persona_continuum.cli.app import app as cli_app
from persona_continuum.domain.persona import PersonaType, RunMode
from persona_continuum.mcp.server import registered_tool_names


def _continuation_step_artifact(tag: str) -> dict:
    return {
        "evaluated_events": [{"date": "2027-03-01", "content": f"{tag} public adoption"}],
        "chosen_actions": [{"action": f"{tag} writes the checklist", "reason": "fits calm design"}],
        "world_state_delta": {"adoption": f"{tag} checklist adopted"},
        "persona_state_delta": {"worldview_delta": f"{tag} confidence with caution"},
        "relationship_deltas": [
            {"counterpart_id": "Mina", "changes": {"trust": 0.08}, "reason": "shared repair"}
        ],
        "affect_deltas": {"hope": 0.2},
        "goal_deltas": [{"goal_id": "calm_design", "status": "active"}],
        "new_memories": [
            {
                "content": f"{tag} treated adoption as a reason to keep evidence visible.",
                "importance": 0.72,
            }
        ],
        "rejected_alternatives": [{"option": "overclaim impact", "reason": "too certain"}],
        "causal_explanation": f"{tag} follows from cautious product habits.",
        "uncertainty": 0.25,
        "evidence_links": [{"type": "world_event", "id": "school_adoption"}],
        "next_step_date": "2027-09-01",
    }


def test_alex_chen_end_to_end(app, tmp_path, monkeypatch) -> None:
    fixture_dir = Path(__file__).parents[1] / "fixtures" / "alex_chen"
    persona = app.personas.create(
        display_name="Alex Chen",
        aliases=["Alex"],
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode=RunMode.HISTORICAL_SNAPSHOT,
        birth_date="1988-04-12",
        data_cutoff_date="2026-01-01",
    )
    sources = app.personas.add_sources(
        persona.id,
        [fixture_dir / "biography.md", fixture_dir / "chat.jsonl", fixture_dir / "decisions.csv"],
    )
    task = app.compilation.create_task(persona.id)
    artifact = {
        "dimension": "eight_layer_profile",
        "claims": [
            {
                "content": "Alex prefers slow research loops before product decisions.",
                "source_id": sources[0].id,
                "claim_type": "historical_self_report",
                "confidence": 0.86,
            },
            {
                "content": "Some teammates considered Alex too cautious.",
                "source_id": sources[2].id,
                "claim_type": "historical_third_party_report",
                "confidence": 0.72,
            },
        ],
        "memories": [
            {
                "content": "Alex apologized to Mina after ignoring an accessibility warning.",
                "type": "episodic",
                "importance": 0.88,
                "participants": ["Alex", "Mina"],
                "source_kind": "historical_self_report",
            }
        ],
        "expression": {"tone": "quiet, concrete, reflective", "avoid": ["grand claims"]},
    }
    app.compilation.submit_research_artifact(task.id, artifact)
    app.compilation.compile_persona(persona.id, task.id)
    app.compilation.validate_persona(persona.id)
    app.personas.activate(persona.id)

    session = app.sessions.start_session(persona.id, "E2E")
    prepared = app.sessions.prepare_turn(persona.id, session.id, "Mina is worried again.")
    app.sessions.commit_turn(
        persona.id,
        session.id,
        user_message="Mina is worried again.",
        persona_response="I should slow down and ask what I missed before defending the plan.",
        used_memory_ids=[memory.id for memory in prepared.relevant_memories],
        user_feedback="good recall",
        goal_completed=False,
    )
    prepared_again = app.sessions.prepare_turn(persona.id, session.id, "What changed?")
    assert any("Mina" in memory.content for memory in prepared_again.relevant_memories)

    continuation = app.continuations.create(persona.id, "digital_persona_activated")
    app.continuations.add_world_events(
        continuation.id,
        [{"date": "2027-03-01", "content": "A school adopted Alex's calm design checklist."}],
    )
    branch = app.continuations.create_branch(continuation.id, seed=42)
    app.continuations.prepare_step(branch.id, target_date="2027-06-01")
    advanced = app.continuations.commit_step(branch.id, _continuation_step_artifact("Alex"))
    scored = app.continuations.score_branch(advanced.id)
    app.continuations.select_main_branch(continuation.id, scored.id)
    continued = app.continuations.compile_persona(continuation.id)
    assert continued.manifest.run_mode == RunMode.COUNTERFACTUAL_CONTINUATION

    export_path = app.personas.export_persona(persona.id, tmp_path / "alex.zip")
    assert export_path.exists()

    monkeypatch.setenv("PERSONA_CONTINUUM_HOME", str(app.config.data_dir))
    result = CliRunner().invoke(cli_app, ["doctor", "--json"])
    assert result.exit_code == 0
    assert '"sqlite_fts5":true' in result.stdout.replace(" ", "")

    tools = registered_tool_names()
    assert "persona_prepare_turn" in tools
    assert "continuation_advance_branch" in tools
    assert "persona_create_room" in tools
