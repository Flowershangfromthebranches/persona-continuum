from __future__ import annotations

from persona_continuum.domain.memory import MemoryType
from persona_continuum.domain.persona import PersonaType, RunMode


def test_persona_compile_session_and_export_import_flow(app, tmp_path) -> None:
    source = tmp_path / "alex.md"
    source.write_text(
        "# Alex Chen\nAlex builds calm tools and distrusts vanity metrics.\n",
        encoding="utf-8",
    )
    persona = app.personas.create(
        display_name="Alex Chen",
        aliases=["A. Chen"],
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode=RunMode.DIGITAL_CONTINUATION,
    )
    added = app.personas.add_sources(persona.id, [source])
    assert added[0].hash

    task = app.compilation.create_task(persona.id)
    app.compilation.submit_research_artifact(
        task.id,
        {
            "dimension": "identity_and_timeline",
            "claims": [
                {
                    "content": "Alex Chen builds calm tools and distrusts vanity metrics.",
                    "source_id": added[0].id,
                    "claim_type": "historical_self_report",
                    "confidence": 0.82,
                }
            ],
            "memories": [
                {
                    "content": "Alex once cancelled a launch to protect user trust.",
                    "type": MemoryType.AUTOBIOGRAPHICAL.value,
                    "importance": 0.9,
                    "source_kind": "historical_self_report",
                }
            ],
        },
    )
    compiled = app.compilation.compile_persona(persona.id, task.id)
    evaluation = app.compilation.validate_persona(persona.id)

    assert compiled.manifest.source_count == 1
    assert evaluation.score >= 0.5
    assert (app.config.personas_dir / persona.id / "manifest.yaml").exists()

    session = app.sessions.start_session(persona.id, title="First chat")
    prepared = app.sessions.prepare_turn(
        persona.id,
        session.id,
        "How would you protect user trust before a launch?",
        max_context_items=5,
    )
    assert prepared.identity_anchor.display_name == "Alex Chen"
    assert prepared.relevant_memories
    assert "fact boundaries" in prepared.uncertainty.lower()

    app.sessions.commit_turn(
        persona.id,
        session.id,
        user_message="What do you think about vanity metrics?",
        persona_response="I would rather preserve trust than inflate a dashboard.",
        used_memory_ids=[prepared.relevant_memories[0].id],
    )
    second = app.sessions.prepare_turn(
        persona.id,
        session.id,
        "Do you remember our last topic?",
        max_context_items=5,
    )
    assert any("vanity metrics" in memory.content for memory in second.relevant_memories)

    export_path = app.personas.export_persona(persona.id, tmp_path / "alex.persona.zip")
    imported = app.personas.import_persona(export_path, new_id="alex-chen-imported")
    assert imported.id == "alex-chen-imported"
    assert app.personas.get(imported.id).display_name == "Alex Chen"
