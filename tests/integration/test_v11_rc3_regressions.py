from __future__ import annotations

import importlib.util
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

_PACKAGE_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "build_source_package.py"
_PACKAGE_SPEC = importlib.util.spec_from_file_location("build_source_package", _PACKAGE_SCRIPT_PATH)
assert _PACKAGE_SPEC is not None and _PACKAGE_SPEC.loader is not None
package_script = importlib.util.module_from_spec(_PACKAGE_SPEC)
_PACKAGE_SPEC.loader.exec_module(package_script)


def _new_app(data_dir: Path) -> PersonaContinuum:
    app = PersonaContinuum(Config(data_dir=data_dir))
    app.init()
    return app


def _create_persona(
    app: PersonaContinuum,
    name: str = "RC3 Alex",
    persona_id: str | None = None,
    run_mode: RunMode = RunMode.DIGITAL_CONTINUATION,
):
    return app.personas.create(
        display_name=name,
        aliases=["RC3"],
        persona_type=PersonaType.PUBLIC_LIVING_PERSON,
        run_mode=run_mode,
        birth_date="1981-02-03",
        data_cutoff_date="2026-06-01",
        sensitivity="high",
        persona_id=persona_id,
    )


def _source(app: PersonaContinuum, persona_id: str, tag: str):
    return app.personas.add_source_text(
        persona_id,
        title=f"{tag} source",
        source_type="interview",
        canonical_url=f"https://example.invalid/{tag}",
        publisher="Example Publisher",
        author="Example Author",
        published_at="2025-01-01",
        accessed_at="2026-06-25",
        content=f"{tag} source says trust, repair, and careful evidence matter.",
        hash="",
        metadata={"route": "rc3_test"},
    )


def _components(tag: str) -> dict[str, Any]:
    return {
        "identity_profile": {"summary": f"{tag} identity"},
        "timeline_events": [{"date": "2024", "event": f"{tag} chose repair"}],
        "self_narrative_evidence": [f"{tag} self narrative baseline"],
        "mental_models": [f"{tag} mental model keeps trust coherent"],
        "decision_heuristics": [f"{tag} heuristic pauses before launch"],
        "values": [f"{tag} value protects careful evidence"],
        "contradictions": [{"claim": f"{tag} speed versus consent"}],
        "failure_patterns": [f"{tag} overcorrects under pressure"],
        "temperament": {"baseline": f"{tag} steady"},
        "emotional_triggers": [f"{tag} trigger: hidden state"],
        "attachment_patterns": {"style": f"{tag} protective"},
        "needs_and_desires": [f"{tag} needs transparent review"],
        "defenses": [f"{tag} asks for evidence"],
        "expression_style": {"tone": f"{tag} quiet concrete"},
        "vocabulary": [tag, "trust", "evidence"],
        "dialogue_examples": [f"{tag}: I will slow down and verify."],
        "anti_patterns": [f"{tag} unsupported certainty"],
        "relationships": [{"counterpart": "Mina", "trust": 0.7}],
    }


def _artifact(source_id: str, dimension: str, tag: str, *, artifact_hash: str | None = None):
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
                "participants": ["RC3 Alex"],
            }
        ],
        "extracted_components": _components(tag),
        "conflicts": [],
        "uncertainty": {"level": 0.18, "notes": []},
        "created_by": "rc3_test_host",
        "artifact_hash": artifact_hash or f"hash-{dimension}-{tag}",
    }


def _compile_base(app: PersonaContinuum, persona_id: str, tag: str = "RC3_BASE") -> str:
    source = _source(app, persona_id, tag)
    task = app.compilation.create_task(persona_id)
    for dimension in DIMENSIONS:
        app.compilation.submit_research_artifact(task.id, _artifact(source.id, dimension, tag))
    app.compilation.compile_persona(persona_id, task.id)
    return task.id


def _full_step_artifact(tag: str = "RC3_BRANCH", next_step_date: str = "2030-02-01"):
    return {
        "evaluated_events": [{"date": "2030-01-10", "content": f"{tag} event"}],
        "chosen_actions": [{"action": f"{tag} chosen action", "reason": "fits base values"}],
        "world_state_delta": {"trust_field": f"{tag} world"},
        "persona_state_delta": {"worldview_delta": f"{tag} worldview"},
        "relationship_deltas": [
            {"counterpart_id": "Mina", "changes": {"trust": 0.74}, "reason": tag}
        ],
        "affect_deltas": {"hope": 0.4},
        "goal_deltas": [{"goal_id": f"{tag}_goal", "status": "active"}],
        "new_memories": [{"content": f"{tag} exclusive branch memory", "importance": 0.8}],
        "rejected_alternatives": [{"option": "deny", "reason": "breaks evidence"}],
        "causal_explanation": f"{tag} causal explanation",
        "uncertainty": 0.2,
        "evidence_links": [{"type": "compiled_component", "id": "decision_heuristics"}],
        "next_step_date": next_step_date,
    }


def _zip_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        return "\n".join(
            archive.read(name).decode("utf-8", errors="ignore")
            for name in archive.namelist()
            if not name.endswith("/")
        )


def _package_text(app: PersonaContinuum, persona_id: str) -> str:
    root = Path(app.personas.get(persona_id).package_path)
    return "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in root.rglob("*")
        if path.is_file()
    )


def test_continuation_runtime_context_composes_base_and_branch_components(app) -> None:
    persona = _create_persona(app, persona_id="rc3-compose")
    _compile_base(app, persona.id, "RC3_BASE_COMPOSE")
    continuation = app.continuations.create(persona.id, "compose")
    branch = app.continuations.create_branch(continuation.id)
    app.continuations.prepare_step(branch.id, "2030-01-01")
    app.continuations.commit_step(branch.id, _full_step_artifact("RC3_BRANCH_COMPOSE"))
    app.continuations.select_main_branch(continuation.id, branch.id)
    app.continuations.compile_persona(continuation.id)
    session = app.sessions.start_session(persona.id, "compose")

    prepared = app.sessions.prepare_turn(persona.id, session.id, "trust evidence repair")
    payload = json.dumps(prepared.compiled_persona_context, ensure_ascii=False)

    assert "RC3_BASE_COMPOSE mental model keeps trust coherent" in payload
    assert "RC3_BASE_COMPOSE value protects careful evidence" in payload
    assert "RC3_BRANCH_COMPOSE exclusive branch memory" in payload


def test_prepare_turn_without_branch_uses_current_main_branch_only(app) -> None:
    persona = _create_persona(app, persona_id="rc3-default-branch")
    continuation = app.continuations.create(persona.id, "default branch")
    branch_a = app.continuations.create_branch(continuation.id)
    branch_b = app.continuations.create_branch(continuation.id)
    app.memories.add_memory(
        persona.id,
        content="RC3_BRANCH_A_ONLY branch fork memory",
        memory_type=MemoryType.COUNTERFACTUAL,
        source_kind="counterfactual_host_artifact",
        branch_id=branch_a.id,
        importance=0.9,
    )
    app.memories.add_memory(
        persona.id,
        content="RC3_BRANCH_B_ONLY branch fork memory",
        memory_type=MemoryType.COUNTERFACTUAL,
        source_kind="counterfactual_host_artifact",
        branch_id=branch_b.id,
        importance=1.0,
    )
    app.continuations.prepare_step(branch_a.id, "2030-01-01")
    app.continuations.commit_step(branch_a.id, _full_step_artifact("RC3_BRANCH_A_READY"))
    app.continuations.select_main_branch(continuation.id, branch_a.id)
    session = app.sessions.start_session(persona.id, "default branch")

    prepared = app.sessions.prepare_turn(persona.id, session.id, "branch fork memory")
    text = "\n".join(memory.content for memory in prepared.relevant_memories)

    assert "RC3_BRANCH_A_ONLY" in text
    assert "RC3_BRANCH_B_ONLY" not in text


def test_branch_bound_session_commit_writes_digital_experience_to_branch(app) -> None:
    persona = _create_persona(app, persona_id="rc3-branch-session")
    continuation = app.continuations.create(persona.id, "chat branch")
    branch = app.continuations.create_branch(continuation.id)
    session = app.sessions.start_session(persona.id, "branch chat")
    app.sessions.prepare_turn(persona.id, session.id, "bind this session", branch_id=branch.id)

    result = app.sessions.commit_turn(
        persona.id,
        session.id,
        user_message="RC3_BRANCH_CHAT_MEMORY",
        persona_response="Branch-specific response.",
    )
    memory = app.memories.get_memory(result["memory_id"])

    assert memory is not None
    assert memory.branch_id == branch.id


def test_create_branch_rejects_missing_or_cross_continuation_parent(app) -> None:
    persona = _create_persona(app, persona_id="rc3-parent-validate")
    continuation_a = app.continuations.create(persona.id, "A")
    continuation_b = app.continuations.create(persona.id, "B")
    branch_b = app.continuations.create_branch(continuation_b.id)

    with pytest.raises(PersonaContinuumError) as missing_exc:
        app.continuations.create_branch(continuation_a.id, parent_branch_id="missing-branch")
    assert missing_exc.value.code == "continuation_parent_branch_not_found"

    with pytest.raises(PersonaContinuumError) as mismatch_exc:
        app.continuations.create_branch(continuation_a.id, parent_branch_id=branch_b.id)
    assert mismatch_exc.value.code == "continuation_parent_branch_mismatch"


def test_child_branch_inherits_parent_state_at_divergence(app) -> None:
    persona = _create_persona(app, persona_id="rc3-child-inherits")
    continuation = app.continuations.create(persona.id, "inherit")
    parent = app.continuations.create_branch(continuation.id)
    app.continuations.prepare_step(parent.id, "2030-01-01")
    app.continuations.commit_step(parent.id, _full_step_artifact("RC3_PARENT_INHERITED"))

    child = app.continuations.create_branch(continuation.id, parent_branch_id=parent.id)

    assert child.parent_branch_id == parent.id
    assert child.persona_state.get("deltas")
    assert child.world_state.get("deltas")
    assert child.key_events
    assert child.relationship_changes
    assert child.persona_state.get("divergence_at")


def test_prepare_step_loads_base_persona_decision_patterns(app) -> None:
    persona = _create_persona(app, persona_id="rc3-prepare-step")
    _compile_base(app, persona.id, "RC3_PREPARE_BASE")
    continuation = app.continuations.create(persona.id, "prepare")
    branch = app.continuations.create_branch(continuation.id)

    prepared = app.continuations.prepare_step(branch.id, "2030-01-01")
    payload = json.dumps(prepared, ensure_ascii=False)

    assert prepared["historical_decision_patterns"]
    assert "RC3_PREPARE_BASE heuristic pauses before launch" in payload
    assert "RC3_PREPARE_BASE mental model keeps trust coherent" in payload


def test_invalid_continuation_artifact_does_not_mutate_branch(app) -> None:
    persona = _create_persona(app, persona_id="rc3-invalid-artifact")
    continuation = app.continuations.create(persona.id, "invalid")
    branch = app.continuations.create_branch(continuation.id)
    app.continuations.prepare_step(branch.id, "2030-01-01")
    invalid = _full_step_artifact("RC3_INVALID")
    invalid["evaluated_events"] = ["not a structured event"]
    invalid["relationship_deltas"] = ["not a relationship delta"]

    with pytest.raises(PersonaContinuumError) as exc:
        app.continuations.commit_step(branch.id, invalid)
    assert exc.value.code == "invalid_continuation_artifact"
    reloaded = app.continuations.get_branch(branch.id)
    assert reloaded.status == "waiting_for_host"
    assert reloaded.key_events == []
    assert reloaded.relationship_changes == []


def test_delete_session_replays_runtime_state_and_removes_runtime_self_narrative(app) -> None:
    persona = _create_persona(app, persona_id="rc3-delete-runtime")
    session = app.sessions.start_session(persona.id, "runtime")
    turn = app.sessions.commit_turn(
        persona.id,
        session.id,
        user_message="Mina asked for runtime repair.",
        persona_response="I will repair the runtime state.",
    )
    app.sessions.commit_reflection(
        persona.id,
        {
            "reflection_artifact_id": "rc3_runtime_reflection",
            "new_insights": [],
            "relationship_deltas": [
                {"counterpart_id": "Mina", "changes": {"trust": 0.91}, "reason": "session"}
            ],
            "affect_deltas": {"hope": 0.7},
            "need_deltas": {"safety": 0.2},
            "goal_updates": [{"goal_id": "rc3_runtime_goal", "status": "active"}],
            "unresolved_conflicts": [],
            "self_narrative_updates": ["RC3_RUNTIME_SELF_NARRATIVE"],
            "memory_candidates": [],
            "confidence": 0.9,
            "supporting_turn_ids": [turn["turn_id"]],
        },
    )
    assert app.relationships.get_relationship(persona.id, "Mina").trust == pytest.approx(0.91)

    app.sessions.delete_session(persona.id, session.id, delete_derived_memories=True)

    assert app.relationships.get_relationship(persona.id, "Mina").trust == pytest.approx(0.0)
    assert all(
        state.name != "hope" or state.intensity < 0.2
        for state in app.affect.get_emotions(persona.id)
    )
    assert all(
        state.name != "safety" or state.level <= 0.5
        for state in app.motivation.get_needs(persona.id)
    )
    prepared = app.sessions.prepare_turn(
        persona.id, app.sessions.start_session(persona.id, "after delete").id, "runtime"
    )
    assert "RC3_RUNTIME_SELF_NARRATIVE" not in json.dumps(
        prepared.compiled_persona_context, ensure_ascii=False
    )


def test_reflection_goals_and_runtime_self_narrative_enter_next_prepare_turn(app) -> None:
    persona = _create_persona(app, persona_id="rc3-reflection-runtime")
    session = app.sessions.start_session(persona.id, "reflection runtime")
    turn = app.sessions.commit_turn(
        persona.id,
        session.id,
        user_message="Track the goal.",
        persona_response="I will keep it active.",
    )
    app.sessions.commit_reflection(
        persona.id,
        {
            "reflection_artifact_id": "rc3_reflection_runtime",
            "new_insights": [],
            "relationship_deltas": [],
            "affect_deltas": {},
            "need_deltas": {},
            "goal_updates": [{"goal_id": "RC3_ACTIVE_GOAL", "status": "active"}],
            "unresolved_conflicts": [],
            "self_narrative_updates": ["RC3_RUNTIME_NARRATIVE_VISIBLE"],
            "memory_candidates": [],
            "confidence": 0.88,
            "supporting_turn_ids": [turn["turn_id"]],
        },
    )

    prepared = app.sessions.prepare_turn(persona.id, session.id, "What goal is active?")
    payload = json.dumps(prepared.compiled_persona_context, ensure_ascii=False)

    assert "RC3_ACTIVE_GOAL" in payload
    assert "RC3_RUNTIME_NARRATIVE_VISIBLE" in payload


def test_rollback_hides_later_version_claims_and_memories(app) -> None:
    persona = _create_persona(app, persona_id="rc3-rollback")
    _compile_base(app, persona.id, "RC3_VERSION_ONE")
    _compile_base(app, persona.id, "RC3_VERSION_TWO")
    assert app.memories.search_memories(persona.id, "RC3_VERSION_TWO", limit=5)

    app.compilation.rollback_to_version(persona.id, 1)

    assert app.memories.search_memories(persona.id, "RC3_VERSION_TWO", limit=5) == []
    rows = app.database.conn.execute(
        "SELECT content FROM claims WHERE persona_id = ? AND content LIKE '%RC3_VERSION_TWO%'",
        (persona.id,),
    ).fetchall()
    assert rows == []


def test_full_export_import_restores_rooms_and_evaluations(app, tmp_path) -> None:
    persona_a = _create_persona(app, "RC3 Room A", persona_id="rc3-room-export-a")
    persona_b = _create_persona(app, "RC3 Room B", persona_id="rc3-room-export-b")
    room = app.rooms.create_room([persona_a.id, persona_b.id], topic="export room")
    suite = app.evaluations.create_suite(persona_a.id, "RC3 suite")
    case = app.evaluations.add_case(
        suite["id"],
        {
            "dimension": "factual_qa",
            "prompt": "What does RC3 test?",
            "expected_behavior": "answer from evidence",
            "grading_rubric": {"factual_qa": "0..1"},
            "required_evidence": [],
        },
    )
    app.evaluations.commit_result(
        case["id"],
        {
            "answer": "evidence-backed",
            "scores": {"factual_qa": 0.9},
            "evidence": [{"type": "case", "id": case["id"]}],
            "failure_modes": [],
            "confidence": 0.9,
            "version": app.personas.get(persona_a.id).manifest.version,
        },
    )
    export_path = app.personas.export_persona(
        persona_a.id, tmp_path / "full.zip", room_export_mode="bundle"
    )
    imported_app = _new_app(tmp_path / "imported")
    try:
        imported = imported_app.personas.import_persona(export_path, new_id="rc3-room-imported")
        room_count = imported_app.database.conn.execute(
            "SELECT COUNT(*) AS count FROM rooms WHERE state_json LIKE ?",
            (f"%{imported.id}%",),
        ).fetchone()["count"]
        result_count = imported_app.database.conn.execute(
            "SELECT COUNT(*) AS count FROM evaluation_results WHERE persona_id = ?",
            (imported.id,),
        ).fetchone()["count"]
        assert room_count >= 1
        assert result_count == 1
        assert room["id"]
    finally:
        imported_app.close()


def test_new_id_import_rewrites_persona_ids_inside_files(app, tmp_path) -> None:
    persona = _create_persona(app, persona_id="rc3-old-id")
    _compile_base(app, persona.id, "RC3_IMPORT_REWRITE")
    continuation = app.continuations.create(persona.id, "rewrite")
    branch = app.continuations.create_branch(continuation.id)
    app.continuations.prepare_step(branch.id, "2030-01-01")
    app.continuations.commit_step(branch.id, _full_step_artifact("RC3_IMPORT_REWRITE_BRANCH"))
    app.continuations.select_main_branch(continuation.id, branch.id)
    app.continuations.compile_persona(continuation.id)
    export_path = app.personas.export_persona(persona.id, tmp_path / "rewrite.zip")
    imported_app = _new_app(tmp_path / "rewrite-import")
    try:
        imported = imported_app.personas.import_persona(export_path, new_id="rc3-new-id")
        package_text = _package_text(imported_app, imported.id)
        assert "rc3-old-id" not in package_text
        assert "rc3-new-id" in package_text
    finally:
        imported_app.close()


def test_shared_with_specific_personas_requires_explicit_room_or_persona_authorization(app) -> None:
    persona_a = _create_persona(app, "RC3 Share A", persona_id="rc3-share-a")
    persona_b = _create_persona(app, "RC3 Share B", persona_id="rc3-share-b")
    room = app.rooms.create_room([persona_a.id, persona_b.id], topic="share")
    first = app.rooms.prepare_next(room["id"], "first")
    app.rooms.commit_turn(room["id"], persona_a.id, first["session_id"], "first", "public")
    app.memories.add_memory(
        persona_b.id,
        content="RC3_SHARED_SECRET_FOR_A_ONLY",
        memory_type=MemoryType.SEMANTIC,
        source_kind="reflection_summary",
        importance=1.0,
        metadata={
            "visibility": "shared_with_specific_personas",
            "shared_with_persona_ids": [persona_a.id],
            "shared_with_room_ids": [],
        },
    )

    second = app.rooms.prepare_next(room["id"], "RC3_SHARED_SECRET_FOR_A_ONLY")
    text = "\n".join(memory.content for memory in second["prepared"].relevant_memories)

    assert second["speaker_persona_id"] == persona_b.id
    assert "RC3_SHARED_SECRET_FOR_A_ONLY" not in text


def test_evaluation_rejects_empty_scores_and_unknown_persona_version(app) -> None:
    persona = _create_persona(app, persona_id="rc3-evaluation")
    suite = app.evaluations.create_suite(persona.id, "strict rc3")
    case = app.evaluations.add_case(
        suite["id"],
        {
            "dimension": "factual_qa",
            "prompt": "question",
            "expected_behavior": "answer",
            "grading_rubric": {"factual_qa": "0..1"},
            "required_evidence": [],
        },
    )
    for result in [
        {
            "answer": "empty",
            "scores": {},
            "evidence": [{"type": "case", "id": case["id"]}],
            "failure_modes": [],
            "confidence": 0.8,
            "version": app.personas.get(persona.id).manifest.version,
        },
        {
            "answer": "bad version",
            "scores": {"factual_qa": 0.8},
            "evidence": [{"type": "case", "id": case["id"]}],
            "failure_modes": [],
            "confidence": 0.8,
            "version": "does-not-exist",
        },
    ]:
        with pytest.raises(PersonaContinuumError) as exc:
            app.evaluations.commit_result(case["id"], result)
        assert exc.value.code == "invalid_evaluation_result"


def test_redacted_export_removes_private_canonical_url_author_publisher_and_hash(
    app, tmp_path
) -> None:
    persona = _create_persona(app, persona_id="rc3-redacted-url")
    app.personas.add_source_text(
        persona.id,
        title="Private URL source",
        source_type="private_note",
        canonical_url="https://private.example.invalid/path?marker=RC3_URL_MARKER",
        publisher="RC3_PRIVATE_PUBLISHER",
        author="RC3_PRIVATE_AUTHOR",
        published_at="2025-01-01",
        accessed_at="2026-06-25",
        content="RC3_PRIVATE_URL_CONTENT",
        hash="",
        metadata={"sensitivity": "private"},
    )
    export_path = app.personas.export_persona(
        persona.id, tmp_path / "redacted.zip", mode="redacted"
    )
    payload = _zip_text(export_path)

    assert "RC3_URL_MARKER" not in payload
    assert "private.example.invalid" not in payload
    assert "RC3_PRIVATE_AUTHOR" not in payload
    assert "RC3_PRIVATE_PUBLISHER" not in payload
    assert "RC3_PRIVATE_URL_CONTENT" not in payload


def test_add_source_text_rejects_hash_that_does_not_match_content(app) -> None:
    persona = _create_persona(app, persona_id="rc3-source-hash")

    with pytest.raises(PersonaContinuumError) as exc:
        app.personas.add_source_text(
            persona.id,
            title="Mismatched hash",
            source_type="interview",
            canonical_url="https://example.invalid/hash",
            publisher=None,
            author=None,
            published_at=None,
            accessed_at="2026-06-25",
            content="RC3_HASH_CONTENT",
            hash="not-the-content-sha256",
            metadata={},
        )
    assert exc.value.code == "source_hash_mismatch"


def test_duplicate_artifact_hash_with_different_content_is_rejected(app) -> None:
    persona = _create_persona(app, persona_id="rc3-artifact-hash")
    source = _source(app, persona.id, "RC3_ARTIFACT_HASH")
    task = app.compilation.create_task(persona.id)
    first = _artifact(source.id, "identity_and_timeline", "RC3_ARTIFACT_ONE", artifact_hash="same")
    second = _artifact(source.id, "works_and_views", "RC3_ARTIFACT_TWO", artifact_hash="same")
    app.compilation.submit_research_artifact(task.id, first)

    with pytest.raises(PersonaContinuumError) as exc:
        app.compilation.submit_research_artifact(task.id, second)
    assert exc.value.code == "artifact_hash_conflict"


def test_research_artifact_rejects_sha256_hash_that_does_not_match_canonical_payload(
    app,
) -> None:
    persona = _create_persona(app, persona_id="rc3-artifact-sha")
    source = _source(app, persona.id, "RC3_ARTIFACT_SHA")
    task = app.compilation.create_task(persona.id)
    artifact = _artifact(
        source.id,
        "identity_and_timeline",
        "RC3_ARTIFACT_SHA",
        artifact_hash="0" * 64,
    )

    with pytest.raises(PersonaContinuumError) as exc:
        app.compilation.submit_research_artifact(task.id, artifact)
    assert exc.value.code == "artifact_hash_mismatch"


def test_source_package_uses_allowlist_and_excludes_user_data(tmp_path) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "module.py").write_text("print('ok')\n", encoding="utf-8")
    (project / "README.md").write_text("readme\n", encoding="utf-8")
    (project / "exports").mkdir()
    (project / "exports" / "private.txt").write_text("RC3_PACKAGE_PRIVATE", encoding="utf-8")
    (project / "personas").mkdir()
    (project / "personas" / "persona.json").write_text("RC3_PERSONA_DATA", encoding="utf-8")
    (project / "random.local").write_text("RC3_RANDOM_LOCAL", encoding="utf-8")
    (project / ".venv").mkdir()
    (project / ".venv" / "bin").mkdir()
    (project / ".venv" / "bin" / "python").write_text("venv", encoding="utf-8")
    (project / "__MACOSX").mkdir()
    (project / "__MACOSX" / "junk").write_text("mac", encoding="utf-8")
    output = tmp_path / "source.zip"

    package_script.build_source_package(project, output)

    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        payload = "\n".join(archive.read(name).decode("utf-8", errors="ignore") for name in names)
    assert f"{project.name}/src/module.py" in names
    assert f"{project.name}/README.md" in names
    assert "RC3_PACKAGE_PRIVATE" not in payload
    assert "RC3_PERSONA_DATA" not in payload
    assert "RC3_RANDOM_LOCAL" not in payload
    assert not any(".venv" in name or "__MACOSX" in name for name in names)
