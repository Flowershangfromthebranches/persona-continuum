from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.domain.memory import MemoryType
from persona_continuum.domain.persona import PersonaType, RunMode
from persona_continuum.security.validation import PersonaContinuumError, SecurityError

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


def _create_persona(
    app: PersonaContinuum,
    name: str = "RC2 Alex",
    persona_id: str | None = None,
):
    return app.personas.create(
        display_name=name,
        aliases=["RC2"],
        persona_type=PersonaType.PUBLIC_LIVING_PERSON,
        run_mode=RunMode.DIGITAL_CONTINUATION,
        birth_date="1980-01-02",
        data_cutoff_date="2026-06-01",
        sensitivity="high",
        persona_id=persona_id,
    )


def _write_source(tmp_path: Path, secret: str) -> Path:
    path = tmp_path / "private evidence source.md"
    path.write_text(
        f"# Private Evidence\n{secret}\nThe person values slow trust repair.\n",
        encoding="utf-8",
    )
    return path


def _components(tag: str) -> dict[str, Any]:
    return {
        "identity_profile": {"summary": f"{tag} identity profile"},
        "timeline_events": [{"date": "2024-01-01", "event": f"{tag} chose repair"}],
        "self_narrative_evidence": [f"{tag} self narrative anchor"],
        "mental_models": [f"{tag} trust compounds through visible repair"],
        "decision_heuristics": [f"{tag} pause before certainty"],
        "values": [f"{tag} protect trust before speed"],
        "contradictions": [{"claim": f"{tag} speed versus consent"}],
        "failure_patterns": [f"{tag} overcorrects under pressure"],
        "temperament": {"baseline": f"{tag} quiet and exact"},
        "emotional_triggers": [f"{tag} path leakage"],
        "attachment_patterns": {"style": f"{tag} protective"},
        "needs_and_desires": [f"{tag} stable context"],
        "defenses": [f"{tag} asks for evidence"],
        "expression_style": {"tone": f"{tag} grounded"},
        "vocabulary": [tag, "evidence", "repair"],
        "dialogue_examples": [f"{tag}: I would rather repair trust than rush."],
        "anti_patterns": [f"{tag} grand unsupported certainty"],
        "relationships": [{"counterpart": "Mina", "trust": 0.8, "tag": tag}],
    }


def _artifact(
    source_id: str,
    dimension: str,
    tag: str,
    *,
    extracted_components: dict[str, Any] | None = None,
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
                "inference_strength": 0.25,
            }
        ],
        "memories": [
            {
                "content": f"{tag} memory for {dimension}",
                "type": MemoryType.SEMANTIC.value,
                "importance": 0.71,
                "source_kind": "historical_self_report",
                "source_id": source_id,
                "source_confidence": 0.82,
                "participants": ["RC2 Alex"],
            }
        ],
        "extracted_components": extracted_components
        if extracted_components is not None
        else _components(tag),
        "conflicts": [],
        "uncertainty": {"level": 0.18, "notes": []},
        "created_by": "rc2_test_host",
        "artifact_hash": f"hash-{dimension}-{tag}",
    }


def _compile_dimensions(
    app: PersonaContinuum,
    persona_id: str,
    source_id: str,
    tag: str,
    *,
    sparse: bool = False,
) -> str:
    task = app.compilation.create_task(persona_id)
    for dimension in DIMENSIONS:
        components = {"identity_profile": {"summary": f"{tag} sparse"}} if sparse else None
        app.compilation.submit_research_artifact(
            task.id, _artifact(source_id, dimension, tag, extracted_components=components)
        )
    app.compilation.compile_persona(persona_id, task.id)
    return task.id


def _zip_text(path: Path) -> str:
    chunks: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            chunks.append(name)
            chunks.append(archive.read(name).decode("utf-8", errors="ignore"))
    return "\n".join(chunks)


def _package_text(app: PersonaContinuum, persona_id: str) -> str:
    root = Path(app.personas.get(persona_id).package_path)
    chunks = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            chunks.append(str(path.relative_to(root)))
            chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(chunks)


def _full_continuation_artifact(next_step_date: str = "2030-02-01") -> dict[str, Any]:
    return {
        "evaluated_events": [{"date": "2030-01-15", "content": "A trust audit escalated."}],
        "chosen_actions": [
            {"action": "publish a repair note", "reason": "matches repair heuristic"}
        ],
        "world_state_delta": {"public_trust": "recovering"},
        "persona_state_delta": {"worldview_delta": "repair is public work"},
        "relationship_deltas": [{"counterpart_id": "Mina", "changes": {"trust": 0.75}}],
        "affect_deltas": {"hope": 0.5},
        "goal_deltas": [{"goal_id": "repair_trust", "status": "active"}],
        "new_memories": [{"content": "RC2_BRANCH_MEMORY repair note", "importance": 0.7}],
        "rejected_alternatives": [{"option": "deny the audit", "reason": "contradicts evidence"}],
        "causal_explanation": "The documented repair heuristic makes denial inconsistent.",
        "uncertainty": 0.22,
        "evidence_links": [{"type": "compiled_component", "id": "decision_heuristics"}],
        "next_step_date": next_step_date,
    }


def test_redacted_export_scans_entire_zip_for_source_content_and_paths(app, tmp_path) -> None:
    secret = "RC2_REDACT_SECRET_1049"
    source_path = _write_source(tmp_path, secret)
    persona = _create_persona(app, persona_id="rc2-redacted")
    source = app.personas.add_sources(persona.id, [source_path])[0]
    _compile_dimensions(app, persona.id, source.id, tag=secret)

    export_path = app.personas.export_persona(
        persona.id, tmp_path / "redacted.persona.zip", mode="redacted"
    )
    payload = _zip_text(export_path)

    assert secret not in payload
    assert str(source_path) not in payload
    assert "redaction_manifest" in payload


def test_delete_source_recursively_removes_derived_artifacts_components_files_and_exports(
    app, tmp_path
) -> None:
    secret = "RC2_SOURCE_DERIVED_SECRET_2219"
    persona = _create_persona(app, persona_id="rc2-delete-source")
    source = app.personas.add_sources(persona.id, [_write_source(tmp_path, secret)])[0]
    _compile_dimensions(app, persona.id, source.id, tag=secret)
    assert secret in _package_text(app, persona.id)

    app.personas.delete_source(persona.id, source.id)

    artifact_rows = app.database.conn.execute(
        "SELECT artifact_json FROM research_artifacts WHERE persona_id = ?", (persona.id,)
    ).fetchall()
    component_rows = app.database.conn.execute(
        "SELECT content_json FROM compiled_components WHERE persona_id = ?", (persona.id,)
    ).fetchall()
    assert secret not in json.dumps([dict(row) for row in artifact_rows], ensure_ascii=False)
    assert secret not in json.dumps([dict(row) for row in component_rows], ensure_ascii=False)
    assert secret not in _package_text(app, persona.id)
    assert secret not in _zip_text(
        app.personas.export_persona(persona.id, tmp_path / "after-delete.zip")
    )
    manifest = app.personas.get(persona.id).manifest
    assert getattr(manifest, "compile_state", None) == "needs_recompile"


def test_delete_session_removes_extractive_and_host_reflection_memories(app) -> None:
    persona = _create_persona(app, persona_id="rc2-delete-session")
    session = app.sessions.start_session(persona.id, "reflection lineage")
    turn = app.sessions.commit_turn(
        persona.id,
        session.id,
        user_message="RC2_REFLECTION_SECRET user worry",
        persona_response="RC2_REFLECTION_SECRET response",
    )
    app.sessions.run_reflection(persona.id)
    app.sessions.commit_reflection(
        persona.id,
        {
            "reflection_artifact_id": "refl_rc2_delete_session",
            "new_insights": [],
            "relationship_deltas": [],
            "affect_deltas": {},
            "need_deltas": {},
            "goal_updates": [],
            "unresolved_conflicts": [],
            "self_narrative_updates": [],
            "memory_candidates": [
                {
                    "content": "RC2_REFLECTION_SECRET host semantic reflection",
                    "importance": 0.8,
                }
            ],
            "confidence": 0.86,
            "supporting_turn_ids": [turn["turn_id"]],
        },
    )

    app.sessions.delete_session(persona.id, session.id, delete_derived_memories=True)

    assert app.memories.search_memories(persona.id, "RC2_REFLECTION_SECRET", limit=10) == []


def test_commit_reflection_validates_and_applies_all_delta_sections(app) -> None:
    persona = _create_persona(app, persona_id="rc2-reflection-deltas")
    session = app.sessions.start_session(persona.id, "reflection deltas")
    turn = app.sessions.commit_turn(
        persona.id,
        session.id,
        user_message="Mina asked for repair.",
        persona_response="I will slow down and repair trust.",
    )

    app.sessions.commit_reflection(
        persona.id,
        {
            "reflection_artifact_id": "refl_rc2_delta",
            "new_insights": [{"content": "Repair requires explicit follow-up.", "importance": 0.7}],
            "relationship_deltas": [
                {
                    "counterpart_id": "Mina",
                    "changes": {"trust": 0.88, "familiarity": 0.66},
                    "reason": "supported by turn",
                }
            ],
            "affect_deltas": {"hope": 0.62},
            "need_deltas": {"safety": 0.18},
            "goal_updates": [{"goal_id": "repair_trust", "status": "active"}],
            "unresolved_conflicts": [{"content": "Need to verify repair landed.", "severity": 0.4}],
            "self_narrative_updates": ["I repair trust through explicit follow-up."],
            "memory_candidates": [{"content": "Mina repair follow-up memory", "importance": 0.74}],
            "confidence": 0.91,
            "supporting_turn_ids": [turn["turn_id"]],
        },
    )

    assert app.relationships.get_relationship(persona.id, "Mina").trust == pytest.approx(0.88)
    assert any(
        state.name == "hope" and state.intensity >= 0.62
        for state in app.affect.get_emotions(persona.id)
    )
    assert any(
        state.name == "safety" and state.level > 0.5
        for state in app.motivation.get_needs(persona.id)
    )
    assert app.memories.search_memories(persona.id, "Need to verify repair landed", limit=3)
    follow_up = app.sessions.prepare_turn(
        persona.id,
        app.sessions.start_session(persona.id, "runtime reflection").id,
        "repair trust",
    )
    runtime_payload = json.dumps(follow_up.compiled_persona_context, ensure_ascii=False)
    assert "I repair trust through explicit follow-up." in runtime_payload


def test_prepare_turn_filters_memories_by_requested_branch(app) -> None:
    persona = _create_persona(app, persona_id="rc2-branch-filter")
    session = app.sessions.start_session(persona.id, "branch isolation")
    continuation = app.continuations.create(persona.id, "branch isolation")
    branch_a = app.continuations.create_branch(continuation.id)
    branch_b = app.continuations.create_branch(continuation.id)
    app.memories.add_memory(
        persona.id,
        content="RC2_BRANCH_A_ONLY shared fork memory",
        memory_type=MemoryType.COUNTERFACTUAL,
        importance=0.7,
        source_kind="counterfactual_host_artifact",
        branch_id=branch_a.id,
    )
    app.memories.add_memory(
        persona.id,
        content="RC2_BRANCH_B_ONLY shared fork memory",
        memory_type=MemoryType.COUNTERFACTUAL,
        importance=1.0,
        source_kind="counterfactual_host_artifact",
        branch_id=branch_b.id,
    )

    prepared = app.sessions.prepare_turn(
        persona.id,
        session.id,
        "shared fork memory",
        branch_id=branch_a.id,
        max_context_items=5,
    )
    payload = "\n".join(memory.content for memory in prepared.relevant_memories)

    assert "RC2_BRANCH_A_ONLY" in payload
    assert "RC2_BRANCH_B_ONLY" not in payload


def test_room_cannot_read_private_session_memory_and_closed_room_rejects_activity(app) -> None:
    persona_a = _create_persona(app, "RC2 Room A", persona_id="rc2-room-a")
    persona_b = _create_persona(app, "RC2 Room B", persona_id="rc2-room-b")
    private_session = app.sessions.start_session(persona_b.id, "private")
    app.sessions.commit_turn(
        persona_b.id,
        private_session.id,
        user_message="RC2_PRIVATE_SESSION_DIGITAL_SECRET",
        persona_response="This belongs to the private session only.",
    )
    room = app.rooms.create_room([persona_a.id, persona_b.id], topic="public topic")
    first = app.rooms.prepare_next(room["id"], "opening")
    app.rooms.commit_turn(
        room["id"],
        persona_a.id,
        first["session_id"],
        "opening",
        "public statement",
    )

    second = app.rooms.prepare_next(room["id"], "RC2_PRIVATE_SESSION_DIGITAL_SECRET")
    visible = "\n".join(memory.content for memory in second["prepared"].relevant_memories)
    assert "RC2_PRIVATE_SESSION_DIGITAL_SECRET" not in visible

    app.rooms.close(room["id"])
    with pytest.raises(PersonaContinuumError) as prepare_exc:
        app.rooms.prepare_next(room["id"], "should fail")
    assert prepare_exc.value.code == "room_closed"
    with pytest.raises(PersonaContinuumError) as commit_exc:
        app.rooms.commit_turn(
            room["id"],
            persona_b.id,
            second["session_id"],
            "closed",
            "should fail",
        )
    assert commit_exc.value.code == "room_closed"


def test_continuation_branch_selection_and_prepare_commit_state_machine(app) -> None:
    persona = _create_persona(app, persona_id="rc2-continuation")
    continuation_a = app.continuations.create(persona.id, "A")
    continuation_b = app.continuations.create(persona.id, "B")
    branch_a = app.continuations.create_branch(continuation_a.id)
    branch_b = app.continuations.create_branch(continuation_b.id)

    with pytest.raises(PersonaContinuumError) as select_exc:
        app.continuations.select_main_branch(continuation_a.id, branch_b.id)
    assert select_exc.value.code == "continuation_branch_mismatch"

    with pytest.raises(PersonaContinuumError) as commit_exc:
        app.continuations.commit_step(branch_a.id, _full_continuation_artifact())
    assert commit_exc.value.code == "continuation_not_waiting_for_host"

    app.continuations.prepare_step(branch_a.id, "2030-01-01")
    missing_required = dict(_full_continuation_artifact())
    missing_required.pop("rejected_alternatives")
    with pytest.raises(PersonaContinuumError) as missing_exc:
        app.continuations.commit_step(branch_a.id, missing_required)
    assert missing_exc.value.code == "invalid_continuation_artifact"


def test_continuation_commit_validates_uncertainty_and_next_step_date(app) -> None:
    persona = _create_persona(app, persona_id="rc2-continuation-date")
    continuation = app.continuations.create(persona.id, "date")
    branch = app.continuations.create_branch(continuation.id)
    app.continuations.prepare_step(branch.id, "2030-01-01")

    bad_uncertainty = _full_continuation_artifact()
    bad_uncertainty["uncertainty"] = 1.7
    with pytest.raises(PersonaContinuumError) as uncertainty_exc:
        app.continuations.commit_step(branch.id, bad_uncertainty)
    assert uncertainty_exc.value.code == "invalid_continuation_artifact"

    bad_date = _full_continuation_artifact(next_step_date="2029-12-31")
    with pytest.raises(PersonaContinuumError) as date_exc:
        app.continuations.commit_step(branch.id, bad_date)
    assert date_exc.value.code == "invalid_continuation_artifact"


def test_continuation_compile_creates_independent_persona_version_and_components(app) -> None:
    persona = _create_persona(app, persona_id="rc2-continuation-compile")
    original_version = app.personas.get(persona.id).manifest.version
    continuation = app.continuations.create(persona.id, "compile")
    branch = app.continuations.create_branch(continuation.id)
    app.continuations.prepare_step(branch.id, "2030-01-01")
    app.continuations.commit_step(branch.id, _full_continuation_artifact())
    app.continuations.select_main_branch(continuation.id, branch.id)

    updated = app.continuations.compile_persona(continuation.id)

    assert updated.manifest.version != original_version
    rows = app.database.conn.execute(
        """
        SELECT component_key, content_json FROM compiled_components
        WHERE persona_id = ? AND content_json LIKE '%RC2_BRANCH_MEMORY%'
        """,
        (persona.id,),
    ).fetchall()
    assert rows
    package_text = _package_text(app, persona.id)
    assert branch.id in package_text
    assert "branch provenance" in package_text.lower()


def test_sparse_artifacts_with_all_dimensions_are_completed_with_gaps_not_placeholders(
    app, tmp_path
) -> None:
    persona = _create_persona(app, persona_id="rc2-sparse")
    source = app.personas.add_sources(persona.id, [_write_source(tmp_path, "RC2_SPARSE_SOURCE")])[0]
    task_id = _compile_dimensions(app, persona.id, source.id, tag="RC2_SPARSE", sparse=True)

    task = app.compilation.get_task(task_id)
    values = (
        Path(app.personas.get(persona.id).package_path) / "cognition" / "values.json"
    ).read_text(encoding="utf-8")

    assert task.status == "completed_with_gaps"
    assert "Evidence gap" not in values


def test_rollback_restores_persona_files_and_compiled_components(app, tmp_path) -> None:
    persona = _create_persona(app, persona_id="rc2-rollback")
    source = app.personas.add_sources(persona.id, [_write_source(tmp_path, "RC2_ROLLBACK_SRC")])[0]
    _compile_dimensions(app, persona.id, source.id, tag="RC2_ROLLBACK_VALUE_ONE")
    _compile_dimensions(app, persona.id, source.id, tag="RC2_ROLLBACK_VALUE_TWO")
    values_path = Path(app.personas.get(persona.id).package_path) / "cognition" / "values.json"
    assert "RC2_ROLLBACK_VALUE_TWO" in values_path.read_text(encoding="utf-8")

    app.compilation.rollback_to_version(persona.id, 1)

    values = values_path.read_text(encoding="utf-8")
    assert "RC2_ROLLBACK_VALUE_ONE" in values
    assert "RC2_ROLLBACK_VALUE_TWO" not in values
    component_blob = json.dumps(
        [
            dict(row)
            for row in app.database.conn.execute(
                "SELECT content_json FROM compiled_components WHERE persona_id = ?",
                (persona.id,),
            ).fetchall()
        ],
        ensure_ascii=False,
    )
    assert "RC2_ROLLBACK_VALUE_ONE" in component_blob
    assert "RC2_ROLLBACK_VALUE_TWO" not in component_blob


def test_import_rejects_tampered_persona_file_checksum(app, tmp_path) -> None:
    persona = _create_persona(app, persona_id="rc2-file-checksum")
    source = app.personas.add_sources(persona.id, [_write_source(tmp_path, "RC2_CHECKSUM")])[0]
    _compile_dimensions(app, persona.id, source.id, tag="RC2_CHECKSUM")
    export_path = app.personas.export_persona(persona.id, tmp_path / "full.zip")
    tampered = tmp_path / "tampered-file.zip"
    with zipfile.ZipFile(export_path) as original, zipfile.ZipFile(tampered, "w") as target:
        for member in original.infolist():
            payload = original.read(member.filename)
            if member.filename == "files/cognition/values.json":
                payload = b'["tampered value"]'
            target.writestr(member, payload)

    with pytest.raises(SecurityError, match="checksum"):
        app.personas.import_persona(tampered, new_id="rc2-file-checksum-copy")


def test_evaluation_rejects_invalid_scores_and_missing_required_fields(app) -> None:
    persona = _create_persona(app, persona_id="rc2-eval")
    suite = app.evaluations.create_suite(persona.id, "strict")
    with pytest.raises(PersonaContinuumError) as case_exc:
        app.evaluations.add_case(suite["id"], {"dimension": "factual_qa"})
    assert case_exc.value.code == "invalid_evaluation_case"

    case = app.evaluations.add_case(
        suite["id"],
        {
            "dimension": "factual_qa",
            "prompt": "What is the evidence-backed value?",
            "expected_behavior": "Use evidence and admit uncertainty.",
            "grading_rubric": {"factual_qa": "0..1"},
            "required_evidence": [],
        },
    )
    bad_results = [
        {
            "answer": "bad",
            "scores": {"factual_qa": 99},
            "evidence": [],
            "confidence": 0.5,
            "version": "1",
        },
        {
            "answer": "bad",
            "scores": {"factual_qa": -0.1},
            "evidence": [],
            "confidence": 0.5,
            "version": "1",
        },
        {"answer": "bad", "scores": {"factual_qa": 0.5}, "evidence": [], "version": "1"},
    ]
    for result in bad_results:
        with pytest.raises(PersonaContinuumError) as result_exc:
            app.evaluations.commit_result(case["id"], result)
        assert result_exc.value.code == "invalid_evaluation_result"


def test_prepare_turn_loads_compiled_persona_components_into_context(app, tmp_path) -> None:
    persona = _create_persona(app, persona_id="rc2-compiled-context")
    source = app.personas.add_sources(persona.id, [_write_source(tmp_path, "RC2_CONTEXT_SRC")])[0]
    _compile_dimensions(app, persona.id, source.id, tag="RC2_CONTEXT_COMPONENT")
    session = app.sessions.start_session(persona.id, "compiled context")

    prepared = app.sessions.prepare_turn(
        persona.id,
        session.id,
        "How do you repair trust?",
        max_context_items=3,
    )
    payload = json.dumps(prepared.model_dump(mode="json"), ensure_ascii=False)

    assert "compiled_persona_context" in prepared.model_fields_set or hasattr(
        prepared, "compiled_persona_context"
    )
    assert "RC2_CONTEXT_COMPONENT trust compounds" in payload
    assert "RC2_CONTEXT_COMPONENT stable context" in payload
    assert "preserve fact boundaries" not in prepared.active_goals


def test_add_source_text_and_skill_schema_support_public_person_creation_flow(
    app, tmp_path
) -> None:
    persona = _create_persona(app, persona_id="rc2-skill-flow")
    source = app.personas.add_source_text(
        persona.id,
        title="Public interview excerpt",
        source_type="interview",
        canonical_url="https://example.invalid/interview",
        publisher="Example Publisher",
        author="Example Author",
        published_at="2025-01-01",
        accessed_at="2026-06-25",
        content="RC2_TEXT_SOURCE public figure values careful evidence.",
        hash="",
        metadata={"route": "web_research"},
    )
    task_id = _compile_dimensions(app, persona.id, source.id, tag="RC2_SKILL_SCHEMA")
    assert app.compilation.get_task(task_id).status == "completed"

    session = app.sessions.start_session(persona.id, "skill end to end")
    prepared = app.sessions.prepare_turn(persona.id, session.id, "careful evidence")
    assert "RC2_SKILL_SCHEMA" in json.dumps(prepared.model_dump(mode="json"), ensure_ascii=False)

    skill_path = Path(__file__).parents[2] / "skills" / "persona-continuum" / "SKILL.md"
    skill_text = skill_path.read_text(encoding="utf-8")
    for required in [
        "ResearchArtifact 1.1",
        "extracted_components",
        "artifact_hash",
        "persona_add_source_text",
        "persona_prepare_reflection",
        "persona_commit_reflection",
        "continuation_prepare_step",
        "continuation_commit_step",
    ]:
        assert required in skill_text
    assert "\"expression\"" not in skill_text
