from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.domain.memory import MemoryType
from persona_continuum.domain.persona import PersonaType, RunMode
from persona_continuum.security.validation import PersonaContinuumError

DIMENSIONS = [
    "identity_and_timeline",
    "works_and_views",
    "interviews_and_dialogue",
    "expression_dna",
    "decisions_and_behavior",
    "third_party_views",
    "affect_relationship_defense",
    "values_desires_contradictions",
]


def _new_app(data_dir: Path) -> PersonaContinuum:
    app = PersonaContinuum(Config(data_dir=data_dir))
    app.init()
    return app


def _create_persona(
    app: PersonaContinuum,
    *,
    persona_id: str,
    name: str = "RC4 Alex",
) -> Any:
    return app.personas.create(
        display_name=name,
        aliases=["RC4"],
        persona_type=PersonaType.PUBLIC_LIVING_PERSON,
        run_mode=RunMode.DIGITAL_CONTINUATION,
        birth_date="1981-02-03",
        data_cutoff_date="2026-06-01",
        sensitivity="high",
        persona_id=persona_id,
    )


def _source(app: PersonaContinuum, persona_id: str, tag: str) -> Any:
    return app.personas.add_source_text(
        persona_id,
        title=f"{tag} source",
        source_type="interview",
        canonical_url=f"https://example.invalid/{tag}",
        publisher="Example",
        author="Researcher",
        published_at="2025-01-01",
        accessed_at="2026-06-26",
        content=f"{tag} source says careful trust repair matters.",
        hash="",
        metadata={},
    )


def _components(tag: str) -> dict[str, Any]:
    return {
        "identity_profile": {"summary": f"{tag} identity"},
        "timeline_events": [{"date": "2024", "event": f"{tag} chose repair"}],
        "self_narrative_evidence": [f"{tag} base self narrative"],
        "mental_models": [f"{tag} mental model"],
        "decision_heuristics": [f"{tag} decision heuristic"],
        "values": [f"{tag} value"],
        "contradictions": [{"claim": f"{tag} contradiction"}],
        "failure_patterns": [f"{tag} failure pattern"],
        "temperament": {"baseline": f"{tag} steady"},
        "emotional_triggers": [f"{tag} hidden state"],
        "attachment_patterns": {"style": f"{tag} protective"},
        "needs_and_desires": [f"{tag} need"],
        "defenses": [f"{tag} defense"],
        "expression_style": {"tone": f"{tag} quiet"},
        "vocabulary": [tag, "trust"],
        "dialogue_examples": [f"{tag}: verify first."],
        "anti_patterns": [f"{tag} unsupported certainty"],
        "relationships": [{"counterpart": "Mina", "trust": 0.7}],
    }


def _artifact(
    source_id: str,
    dimension: str,
    tag: str,
    artifact_hash: str | None = None,
) -> dict[str, Any]:
    return {
        "artifact_id": f"art_{dimension}_{tag}",
        "schema_version": "1.1",
        "dimension": dimension,
        "source_ids": [source_id],
        "claims": [
            {
                "content": f"{tag} claim for {dimension}",
                "source_id": source_id,
                "claim_type": "historical_self_report",
                "confidence": 0.82,
                "reliability": 0.8,
                "inference_strength": 0.2,
            }
        ],
        "memories": [
            {
                "content": f"{tag} memory for {dimension}",
                "type": MemoryType.SEMANTIC.value,
                "importance": 0.7,
                "source_kind": "historical_self_report",
                "source_id": source_id,
                "source_confidence": 0.82,
                "participants": ["RC4"],
            }
        ],
        "extracted_components": _components(tag),
        "conflicts": [],
        "uncertainty": {"level": 0.18, "notes": []},
        "created_by": "rc4_test_host",
        "artifact_hash": artifact_hash or f"hash-{dimension}-{tag}",
    }


def _compile_base(app: PersonaContinuum, persona_id: str, tag: str) -> None:
    source = _source(app, persona_id, tag)
    task = app.compilation.create_task(persona_id)
    for dimension in DIMENSIONS:
        app.compilation.submit_research_artifact(task.id, _artifact(source.id, dimension, tag))
    app.compilation.compile_persona(persona_id, task.id)


def _step(tag: str, next_step_date: str = "2030-02-01") -> dict[str, Any]:
    return {
        "evaluated_events": [{"date": "2030-01-10", "content": f"{tag} event"}],
        "chosen_actions": [{"action": f"{tag} action", "reason": "fits base"}],
        "world_state_delta": {"world": tag},
        "persona_state_delta": {"self_narrative_delta": f"{tag} branch narrative"},
        "relationship_deltas": [
            {"counterpart_id": "Mina", "changes": {"trust": 0.74}, "reason": tag}
        ],
        "affect_deltas": {"hope": 0.4},
        "goal_deltas": [{"goal_id": f"{tag}_goal", "status": "active"}],
        "new_memories": [{"content": f"{tag} branch memory", "importance": 0.8}],
        "rejected_alternatives": [{"option": "deny", "reason": "breaks evidence"}],
        "causal_explanation": f"{tag} causal explanation",
        "uncertainty": 0.2,
        "evidence_links": [{"type": "compiled_component", "id": "decision_heuristics"}],
        "next_step_date": next_step_date,
    }


def _commit_step(
    app: PersonaContinuum, branch_id: str, tag: str, target: str, next_date: str
) -> None:
    app.continuations.prepare_step(branch_id, target)
    app.continuations.commit_step(branch_id, _step(tag, next_date))


def test_delete_persona_removes_database_fts_rooms_and_package(app) -> None:
    persona = _create_persona(app, persona_id="rc4-delete-all")
    other = _create_persona(app, persona_id="rc4-delete-other", name="Other")
    package_path = Path(app.personas.get(persona.id).package_path)
    source = _source(app, persona.id, "RC4_DELETE")
    app.memories.add_memory(
        persona.id,
        content="RC4_DELETE_FTS_SECRET",
        memory_type=MemoryType.SEMANTIC,
        source_kind="manual",
        source_id=source.id,
    )
    session = app.sessions.start_session(persona.id, "delete me", counterpart_id="Alice")
    app.sessions.commit_turn(
        persona.id,
        session.id,
        user_message="trust Alice",
        persona_response="I will verify.",
        counterpart_id="Alice",
    )
    app.affect.update_emotions(persona.id, {"hope": 0.7}, "delete setup")
    app.motivation.update_needs(persona.id, {"safety": 0.2}, "delete setup")
    app.relationships.update_relationship(persona.id, "Alice", {"trust": 0.6}, "delete setup")
    room = app.rooms.create_room([persona.id, other.id], "delete participant")

    app.personas.delete(persona.id)

    for table in [
        "sources",
        "claims",
        "compilation_tasks",
        "research_artifacts",
        "compiled_components",
        "compile_snapshots",
        "memories",
        "memories_fts",
        "affect_states",
        "needs",
        "relationships",
        "sessions",
        "session_turns",
        "continuations",
        "continuation_branches",
        "lineage",
        "change_events",
        "evaluation_suites",
        "evaluation_cases",
        "evaluation_results",
    ]:
        row = app.database.conn.execute(
            f"SELECT COUNT(*) AS count FROM {table} WHERE persona_id = ?",
            (persona.id,),
        ).fetchone()
        assert row["count"] == 0, table
    assert (
        app.database.conn.execute(
            "SELECT COUNT(*) AS count FROM memories_fts WHERE content MATCH ?",
            ("RC4_DELETE_FTS_SECRET",),
        ).fetchone()["count"]
        == 0
    )
    assert not package_path.exists()
    room_state = app.rooms.get_state(room["id"])
    assert persona.id not in room_state["persona_ids"]
    assert persona.id not in room_state.get("room_sessions", {})


def test_delete_room_participant_repairs_room_and_prepare_next(app) -> None:
    persona_a = _create_persona(app, persona_id="rc4-room-a", name="Room A")
    persona_b = _create_persona(app, persona_id="rc4-room-b", name="Room B")
    room = app.rooms.create_room([persona_a.id, persona_b.id], "room repair")
    first = app.rooms.prepare_next(room["id"], "start")
    app.rooms.commit_turn(
        room["id"],
        first["speaker_persona_id"],
        first["session_id"],
        "start",
        "A starts.",
    )

    app.personas.delete(persona_b.id)

    state = app.rooms.get_state(room["id"])
    assert state["status"] == "active"
    assert state["persona_ids"] == [persona_a.id]
    prepared = app.rooms.prepare_next(room["id"], "continue")
    assert prepared["speaker_persona_id"] == persona_a.id


def test_grandchild_branch_inherits_pre_divergence_ancestor_memory_only(app) -> None:
    persona = _create_persona(app, persona_id="rc4-branch-ancestry")
    continuation = app.continuations.create(persona.id, "ancestry")
    root = app.continuations.create_branch(continuation.id)
    sibling = app.continuations.create_branch(continuation.id)
    _commit_step(app, root.id, "RC4_ROOT_PRE", "2030-01-01", "2030-02-01")
    child = app.continuations.create_branch(continuation.id, parent_branch_id=root.id)
    _commit_step(app, root.id, "RC4_ROOT_AFTER", "2030-03-01", "2030-04-01")
    _commit_step(app, child.id, "RC4_CHILD_PRE", "2030-05-01", "2030-06-01")
    grandchild = app.continuations.create_branch(continuation.id, parent_branch_id=child.id)
    _commit_step(app, sibling.id, "RC4_SIBLING", "2030-01-01", "2030-02-01")

    results = app.memories.search_memories(
        persona.id,
        "branch memory",
        limit=20,
        branch_id=grandchild.id,
        include_main_history=True,
        include_shared_pre_divergence=True,
    )
    text = "\n".join(memory.content for memory in results)

    assert "RC4_ROOT_PRE branch memory" in text
    assert "RC4_CHILD_PRE branch memory" in text
    assert "RC4_ROOT_AFTER branch memory" not in text
    assert "RC4_SIBLING branch memory" not in text


def test_delete_session_replays_commit_turn_auto_relationship_and_emotion(app) -> None:
    persona = _create_persona(app, persona_id="rc4-delete-commit-runtime")
    session = app.sessions.start_session(persona.id, "runtime", counterpart_id="Alice")
    app.sessions.commit_turn(
        persona.id,
        session.id,
        user_message="trust Alice",
        persona_response="Thanks, I will verify.",
        counterpart_id="Alice",
    )
    assert app.relationships.get_relationship(persona.id, "Alice").familiarity > 0
    assert any(
        state.name == "hope" and state.intensity >= 0.2
        for state in app.affect.get_emotions(persona.id)
    )

    app.sessions.delete_session(persona.id, session.id, delete_derived_memories=True)

    assert app.relationships.get_relationship(persona.id, "Alice").familiarity == pytest.approx(0)
    assert all(
        state.name != "hope" or state.intensity < 0.2
        for state in app.affect.get_emotions(persona.id)
    )


def test_delete_session_replays_state_patch_affect_need_and_relationship(app) -> None:
    persona = _create_persona(app, persona_id="rc4-delete-state-patch")
    session = app.sessions.start_session(persona.id, "patch", counterpart_id="Alice")
    app.sessions.commit_turn(
        persona.id,
        session.id,
        user_message="patch state",
        persona_response="patched",
        counterpart_id="Alice",
        state_patch={
            "affect": {"hope": 0.8},
            "needs": {"safety": 0.3},
            "relationships": [
                {"counterpart_id": "Alice", "changes": {"trust": 0.77}},
            ],
        },
    )
    assert app.relationships.get_relationship(persona.id, "Alice").trust == pytest.approx(0.77)

    app.sessions.delete_session(persona.id, session.id, delete_derived_memories=True)

    assert app.relationships.get_relationship(persona.id, "Alice").trust == pytest.approx(0)
    assert all(
        state.name != "hope" or state.intensity < 0.2
        for state in app.affect.get_emotions(persona.id)
    )
    assert all(
        state.name != "safety" or state.level <= 0.5
        for state in app.motivation.get_needs(persona.id)
    )


def test_session_rejects_silent_counterpart_switch(app) -> None:
    persona = _create_persona(app, persona_id="rc4-counterpart")
    session = app.sessions.start_session(persona.id, "alice", counterpart_id="Alice")
    app.sessions.prepare_turn(persona.id, session.id, "hello Alice", counterpart_id="Alice")

    with pytest.raises(PersonaContinuumError) as exc:
        app.sessions.prepare_turn(persona.id, session.id, "hello Bob", counterpart_id="Bob")
    assert exc.value.code == "session_counterpart_mismatch"

    with pytest.raises(PersonaContinuumError) as commit_exc:
        app.sessions.commit_turn(
            persona.id,
            session.id,
            user_message="hello Bob",
            persona_response="no switch",
            counterpart_id="Bob",
        )
    assert commit_exc.value.code == "session_counterpart_mismatch"


def test_single_persona_full_export_import_omits_dangling_active_room(app, tmp_path) -> None:
    persona_a = _create_persona(app, persona_id="rc4-export-a", name="Export A")
    persona_b = _create_persona(app, persona_id="rc4-export-b", name="Export B")
    app.rooms.create_room([persona_a.id, persona_b.id], "export room")

    export_path = app.personas.export_persona(persona_a.id, tmp_path / "single.zip")
    imported_app = _new_app(tmp_path / "imported-single")
    try:
        imported = imported_app.personas.import_persona(export_path, new_id="rc4-import-a")
        rooms = imported_app.database.conn.execute("SELECT state_json FROM rooms").fetchall()
        persona_ids = {
            str(row["id"])
            for row in imported_app.database.conn.execute("SELECT id FROM personas").fetchall()
        }
        for row in rooms:
            state = dict(json.loads(row["state_json"]))
            if state.get("status") == "active":
                assert set(map(str, state.get("persona_ids", []))).issubset(persona_ids)
        if rooms:
            room_id = str(json.loads(rooms[0]["state_json"])["id"])
            prepared = imported_app.rooms.prepare_next(room_id)
            assert prepared["speaker_persona_id"] == imported.id
    finally:
        imported_app.close()


def test_room_bundle_export_import_restores_all_personas_and_runs_two_rounds(app, tmp_path) -> None:
    persona_a = _create_persona(app, persona_id="rc4-bundle-a", name="Bundle A")
    persona_b = _create_persona(app, persona_id="rc4-bundle-b", name="Bundle B")
    room = app.rooms.create_room([persona_a.id, persona_b.id], "bundle room")

    export_path = app.personas.export_persona(
        persona_a.id, tmp_path / "bundle.zip", room_export_mode="bundle"
    )
    imported_app = _new_app(tmp_path / "imported-bundle")
    try:
        imported_app.personas.import_persona(export_path, new_id="rc4-bundle-a-imported")
        imported_room = imported_app.rooms.get_state(room["id"])
        assert imported_room["status"] == "active"
        for _ in range(2):
            prepared = imported_app.rooms.prepare_next(room["id"], "bundle turn")
            imported_app.rooms.commit_turn(
                room["id"],
                prepared["speaker_persona_id"],
                prepared["session_id"],
                "bundle turn",
                "bundle response",
            )
    finally:
        imported_app.close()


def test_branch_compile_keeps_base_self_narrative_clean_and_is_idempotent(app) -> None:
    persona = _create_persona(app, persona_id="rc4-branch-files")
    _compile_base(app, persona.id, "RC4_BASE")
    base_path = Path(app.personas.get(persona.id).package_path) / "identity" / "self_narrative.md"
    base_text = base_path.read_text(encoding="utf-8")
    continuation = app.continuations.create(persona.id, "branch files")
    branch_a = app.continuations.create_branch(continuation.id)
    branch_b = app.continuations.create_branch(continuation.id)
    _commit_step(app, branch_a.id, "RC4_BRANCH_A", "2030-01-01", "2030-02-01")
    app.continuations.select_main_branch(continuation.id, branch_a.id)
    first = app.continuations.compile_persona(continuation.id)
    first_version = first.manifest.version
    component_count = app.database.conn.execute(
        "SELECT COUNT(*) AS count FROM compiled_components WHERE persona_id = ?",
        (persona.id,),
    ).fetchone()["count"]
    second = app.continuations.compile_persona(continuation.id)
    assert second.manifest.version == first_version
    assert (
        app.database.conn.execute(
            "SELECT COUNT(*) AS count FROM compiled_components WHERE persona_id = ?",
            (persona.id,),
        ).fetchone()["count"]
        == component_count
    )
    _commit_step(app, branch_b.id, "RC4_BRANCH_B", "2030-01-01", "2030-02-01")
    app.continuations.select_main_branch(continuation.id, branch_b.id)
    app.continuations.compile_persona(continuation.id)

    assert base_path.read_text(encoding="utf-8") == base_text
    branch_root = Path(app.personas.get(persona.id).package_path) / "continuation" / "branches"
    assert (branch_root / branch_a.id / "self_narrative_delta.json").exists()
    assert (branch_root / branch_b.id / "self_narrative_delta.json").exists()


def test_identical_research_artifact_with_different_external_hash_is_deduped(app) -> None:
    persona = _create_persona(app, persona_id="rc4-canonical-dedupe")
    source = _source(app, persona.id, "RC4_CANONICAL")
    task = app.compilation.create_task(persona.id)
    first = _artifact(source.id, "identity_and_timeline", "RC4_CANONICAL", "external-one")
    second = dict(first)
    second["artifact_hash"] = "external-two"
    app.compilation.submit_research_artifact(task.id, first)
    app.compilation.submit_research_artifact(task.id, second)

    reloaded = app.compilation.get_task(task.id)
    assert len(reloaded.artifacts) == 1
    rows = app.database.conn.execute(
        "SELECT artifact_json FROM research_artifacts WHERE persona_id = ? AND task_id = ?",
        (persona.id, task.id),
    ).fetchall()
    assert len(rows) == 1
    assert "artifact_canonical_sha256" in dict(json.loads(rows[0]["artifact_json"]))


def test_advance_branch_and_select_main_respect_continuation_state_machine(app) -> None:
    persona = _create_persona(app, persona_id="rc4-continuation-state")
    continuation = app.continuations.create(persona.id, "state")
    branch = app.continuations.create_branch(continuation.id)
    app.continuations.prepare_step(branch.id, "2030-01-01")

    with pytest.raises(PersonaContinuumError) as advance_exc:
        app.continuations.advance_branch(branch.id, "2030-02-01")
    assert advance_exc.value.code == "continuation_invalid_state"

    with pytest.raises(PersonaContinuumError) as select_exc:
        app.continuations.select_main_branch(continuation.id, branch.id)
    assert select_exc.value.code == "continuation_branch_not_ready"


def test_continuation_step_rejects_empty_nested_deltas(app) -> None:
    persona = _create_persona(app, persona_id="rc4-strict-step")
    continuation = app.continuations.create(persona.id, "strict")
    branch = app.continuations.create_branch(continuation.id)
    app.continuations.prepare_step(branch.id, "2030-01-01")
    artifact = _step("RC4_STRICT")
    artifact["world_state_delta"] = {}
    artifact["persona_state_delta"] = {}
    artifact["rejected_alternatives"] = [{}]
    artifact["evidence_links"] = [{}]

    with pytest.raises(PersonaContinuumError) as exc:
        app.continuations.commit_step(branch.id, artifact)
    assert exc.value.code == "invalid_continuation_artifact"


def test_source_package_excludes_any_venv_even_inside_allowed_directories(tmp_path) -> None:
    import importlib.util

    script_path = Path(__file__).parents[2] / "scripts" / "build_source_package.py"
    spec = importlib.util.spec_from_file_location("build_source_package", script_path)
    assert spec and spec.loader
    package_script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package_script)
    project = tmp_path / "project"
    (project / "src" / ".audit-venv").mkdir(parents=True)
    (project / "src" / ".audit-venv" / "secret.txt").write_text("RC4_VENV_SECRET")
    (project / "src" / "module.py").write_text("print('ok')\n")
    (project / "README.md").write_text("readme\n")
    output = tmp_path / "source.zip"

    package_script.build_source_package(project, output)

    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        payload = "\n".join(archive.read(name).decode("utf-8", errors="ignore") for name in names)
    assert "RC4_VENV_SECRET" not in payload
    assert not any("venv" in name.lower() for name in names)
