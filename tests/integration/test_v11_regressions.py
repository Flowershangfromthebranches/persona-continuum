from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from docx import Document

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
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


def _new_app(data_dir: Path) -> PersonaContinuum:
    continuum = PersonaContinuum(Config(data_dir=data_dir))
    continuum.init()
    return continuum


def _create_persona(app: PersonaContinuum, name: str = "V11 Alex", persona_id: str | None = None):
    return app.personas.create(
        display_name=name,
        aliases=["V11"],
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode=RunMode.DIGITAL_CONTINUATION,
        birth_date="1990-01-02",
        data_cutoff_date="2026-06-01",
        persona_id=persona_id,
    )


def _write_source(tmp_path: Path, content: str = "PRIVATE_SOURCE_SECRET_917") -> Path:
    path = tmp_path / "source.md"
    path.write_text(
        f"# Source\n{content}\nAlex protects user trust and rejects vanity metrics.\n",
        encoding="utf-8",
    )
    return path


def _artifact(source_id: str, dimension: str, index: int = 0) -> dict[str, object]:
    return {
        "artifact_id": f"art_{dimension}",
        "schema_version": "1.1",
        "dimension": dimension,
        "source_ids": [source_id],
        "claims": [
            {
                "content": f"{dimension} claim {index}: Alex protects user trust.",
                "source_id": source_id,
                "claim_type": "historical_self_report",
                "confidence": 0.82,
                "reliability": 0.8,
                "inference_strength": 0.35,
            }
        ],
        "memories": [
            {
                "content": f"{dimension} memory {index}: trust beat vanity metrics.",
                "type": MemoryType.SEMANTIC.value,
                "importance": 0.72,
                "source_kind": "historical_self_report",
                "source_id": source_id,
                "source_confidence": 0.82,
                "participants": ["Alex"],
            }
        ],
        "extracted_components": {
            "identity_profile": {"summary": "Alex is a calm product builder."},
            "timeline_events": [{"date": "2020", "event": "Chose trust over growth."}],
            "self_narrative_evidence": ["I slow down when trust is at risk."],
            "mental_models": ["Trust compounds slower than metrics."],
            "decision_heuristics": ["Delay launches when evidence is weak."],
            "values": ["trust", "careful observation"],
            "contradictions": [{"claim": "fast shipping vs careful consent"}],
            "failure_patterns": ["over-caution under ambiguity"],
            "temperament": {"baseline": "quiet and concrete"},
            "emotional_triggers": ["vanity metrics"],
            "attachment_patterns": ["protective toward collaborators"],
            "needs_and_desires": ["clear evidence", "stable trust"],
            "defenses": ["asks for concrete examples"],
            "expression_style": {"tone": "quiet, specific"},
            "vocabulary": ["trust", "evidence", "slow down"],
            "dialogue_examples": ["I would rather protect trust than inflate a number."],
            "anti_patterns": ["grand unsupported claims"],
            "relationships": [{"counterpart": "Mina", "trust": 0.7}],
        },
        "conflicts": [],
        "uncertainty": {"level": 0.18, "notes": []},
        "created_by": "test_host_agent",
        "artifact_hash": f"hash-{dimension}",
    }


def _compile_all_dimensions(app: PersonaContinuum, persona_id: str, source_id: str) -> str:
    task = app.compilation.create_task(persona_id)
    for index, dimension in enumerate(DIMENSIONS):
        app.compilation.submit_research_artifact(task.id, _artifact(source_id, dimension, index))
    app.compilation.compile_persona(persona_id, task.id)
    return task.id


def _seed_full_persona(app: PersonaContinuum, tmp_path: Path):
    persona = _create_persona(app, persona_id="v11-alex")
    source = app.personas.add_sources(persona.id, [_write_source(tmp_path)])[0]
    task_id = _compile_all_dimensions(app, persona.id, source.id)
    session = app.sessions.start_session(persona.id, "V1.1 export session")
    prepared = app.sessions.prepare_turn(persona.id, session.id, "Do you remember Mina?")
    turn = app.sessions.commit_turn(
        persona.id,
        session.id,
        user_message="Do you remember Mina?",
        persona_response="I remember slowing down when Mina raised a trust concern.",
        used_memory_ids=[memory.id for memory in prepared.relevant_memories],
        user_feedback="good recall",
        goal_completed=True,
    )
    app.affect.update_emotions(persona.id, {"hope": 0.6}, "seed export data")
    app.motivation.update_needs(persona.id, {"achievement": 0.2}, "seed export data")
    app.relationships.update_relationship(
        persona.id, "Mina", {"trust": 0.7, "familiarity": 0.8}, "seed export data"
    )
    continuation = app.continuations.create(persona.id, "V1.1 divergence")
    app.continuations.add_world_events(
        continuation.id, [{"date": "2028-01-01", "content": "A public trust audit landed."}]
    )
    branch = app.continuations.create_branch(continuation.id, seed=7)
    return persona, source, task_id, session, turn, continuation, branch


def _count(app: PersonaContinuum, table: str, persona_id: str) -> int:
    row = app.database.conn.execute(
        f"SELECT COUNT(*) AS count FROM {table} WHERE persona_id = ?", (persona_id,)
    ).fetchone()
    return int(row["count"])


def _minimal_text_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()

    def obj(number: int, body: bytes) -> bytes:
        return f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    objects = [
        obj(1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
        obj(
            3,
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        ),
        obj(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"),
        obj(5, f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"),
    ]
    header = b"%PDF-1.4\n"
    body = b""
    offsets = []
    pos = len(header)
    for item in objects:
        offsets.append(pos)
        body += item
        pos += len(item)
    xref_start = len(header) + len(body)
    xref = b"xref\n0 6\n0000000000 65535 f \n" + b"".join(
        f"{offset:010d} 00000 n \n".encode() for offset in offsets
    )
    trailer = f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF\n".encode()
    return header + body + xref + trailer


def _docx_bytes(text: str) -> bytes:
    handle = BytesIO()
    doc = Document()
    doc.add_paragraph(text)
    doc.save(handle)
    return handle.getvalue()


def test_full_export_import_restores_all_database_data_with_new_id(app, tmp_path) -> None:
    persona, *_ = _seed_full_persona(app, tmp_path)
    export_path = app.personas.export_persona(persona.id, tmp_path / "full.persona.zip")

    imported_app = _new_app(tmp_path / "imported")
    try:
        imported = imported_app.personas.import_persona(export_path, new_id="v11-alex-copy")
        expected_tables = [
            "sources",
            "claims",
            "memories",
            "affect_states",
            "needs",
            "relationships",
            "sessions",
            "session_turns",
            "compilation_tasks",
            "continuations",
            "continuation_branches",
            "lineage",
            "research_artifacts",
            "compiled_components",
            "compile_snapshots",
        ]
        for table in expected_tables:
            assert _count(imported_app, table, imported.id) == _count(app, table, persona.id), table
        assert imported.id == "v11-alex-copy"
        assert imported_app.memories.search_memories(imported.id, "Mina trust", limit=3)
    finally:
        imported_app.close()


def test_import_rejects_checksum_tampering_and_rolls_back(app, tmp_path) -> None:
    persona, *_ = _seed_full_persona(app, tmp_path)
    export_path = app.personas.export_persona(persona.id, tmp_path / "tamper.persona.zip")
    with zipfile.ZipFile(export_path, "a") as archive:
        archive.writestr("data/sources.jsonl", "tampered source data\n")

    imported_app = _new_app(tmp_path / "tampered-import")
    try:
        with pytest.raises(SecurityError, match="checksum"):
            imported_app.personas.import_persona(export_path, new_id="tampered-copy")
        assert imported_app.personas.list(include_archived=True) == []
        assert not (imported_app.config.personas_dir / "tampered-copy").exists()
    finally:
        imported_app.close()


def test_cross_persona_session_operations_are_rejected(app) -> None:
    persona_a = _create_persona(app, "Persona A", persona_id="persona-a")
    persona_b = _create_persona(app, "Persona B", persona_id="persona-b")
    session = app.sessions.start_session(persona_a.id, "A only")

    for operation in (
        lambda: app.sessions.prepare_turn(persona_b.id, session.id, "hello"),
        lambda: app.sessions.commit_turn(
            persona_b.id,
            session.id,
            user_message="hello",
            persona_response="wrong persona",
        ),
    ):
        with pytest.raises(PersonaContinuumError) as exc:
            operation()
        assert exc.value.code == "session_persona_mismatch"


def test_ended_session_cannot_accept_new_turns(app) -> None:
    persona = _create_persona(app)
    session = app.sessions.start_session(persona.id, "ended")
    app.sessions.end_session(session.id)

    with pytest.raises(PersonaContinuumError) as exc:
        app.sessions.commit_turn(
            persona.id,
            session.id,
            user_message="late",
            persona_response="should not persist",
        )
    assert exc.value.code == "session_not_active"


def test_delete_session_removes_derived_digital_experience_memory(app) -> None:
    persona = _create_persona(app)
    session = app.sessions.start_session(persona.id, "derived")
    result = app.sessions.commit_turn(
        persona.id,
        session.id,
        user_message="PRIVATE_SESSION_SECRET_551",
        persona_response="I will not keep this after deletion.",
    )
    memory = app.memories.get_memory(result["memory_id"])
    assert memory is not None
    assert memory.metadata["session_id"] == session.id
    assert memory.metadata["turn_id"] == result["turn_id"]

    app.sessions.delete_session(persona.id, session.id, delete_derived_memories=True)

    assert app.memories.search_memories(persona.id, "PRIVATE_SESSION_SECRET_551", limit=3) == []
    assert app.memories.get_memory(result["memory_id"]) is None


def test_delete_source_removes_file_contents_exports_and_lineage_derivatives(app, tmp_path) -> None:
    secret = "PRIVATE_SOURCE_SECRET_917"
    persona = _create_persona(app)
    source = app.personas.add_sources(persona.id, [_write_source(tmp_path, secret)])[0]
    _compile_all_dimensions(app, persona.id, source.id)
    package_root = Path(app.personas.get(persona.id).package_path)
    assert secret in (package_root / "evidence" / "sources.jsonl").read_text(encoding="utf-8")

    app.personas.delete_source(persona.id, source.id)

    assert app.personas.get_sources(persona.id) == []
    assert secret not in (package_root / "evidence" / "sources.jsonl").read_text(encoding="utf-8")
    assert secret not in (package_root / "evidence" / "claims.jsonl").read_text(encoding="utf-8")
    assert app.memories.search_memories(persona.id, secret, limit=5) == []
    export_path = app.personas.export_persona(persona.id, tmp_path / "after-delete.zip")
    with zipfile.ZipFile(export_path) as archive:
        for member in archive.namelist():
            if member.endswith((".json", ".jsonl", ".yaml", ".md", ".txt")):
                assert secret not in archive.read(member).decode("utf-8", errors="ignore")


def test_zip_members_use_format_specific_parsers(app, tmp_path) -> None:
    persona = _create_persona(app)
    zip_path = tmp_path / "mixed.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("note.txt", "TXT visible text")
        archive.writestr("note.md", "# Markdown visible text")
        archive.writestr("note.json", json.dumps({"message": "JSON visible text"}))
        archive.writestr("note.csv", "kind,value\ncsv,CSV visible text\n")
        archive.writestr("note.docx", _docx_bytes("DOCX visible text"))
        archive.writestr("note.pdf", _minimal_text_pdf("PDF visible text"))

    docs = app.personas.add_sources(persona.id, [zip_path])
    contents = "\n".join(source.content for source in docs)

    assert len(docs) == 6
    assert "TXT visible text" in contents
    assert "Markdown visible text" in contents
    assert "JSON visible text" in contents
    assert "CSV visible text" in contents
    assert "DOCX visible text" in contents
    assert "PDF visible text" in contents


def test_zip_bomb_limits_reject_oversized_total_and_ratio(app, tmp_path) -> None:
    persona = _create_persona(app)
    app.personas.loader.max_bytes = 1024
    zip_path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("huge.txt", "A" * 20_000)

    with pytest.raises(SecurityError):
        app.personas.add_sources(persona.id, [zip_path])


def test_compile_persona_is_idempotent_for_completed_task(app, tmp_path) -> None:
    persona = _create_persona(app)
    source = app.personas.add_sources(persona.id, [_write_source(tmp_path)])[0]
    task_id = _compile_all_dimensions(app, persona.id, source.id)
    first_counts = (_count(app, "claims", persona.id), _count(app, "memories", persona.id))

    app.compilation.compile_persona(persona.id, task_id)

    assert (_count(app, "claims", persona.id), _count(app, "memories", persona.id)) == first_counts
    assert app.compilation.get_task(task_id).status in {"completed", "completed_with_gaps"}


def test_compile_writes_non_empty_eight_layer_persona_files(app, tmp_path) -> None:
    persona = _create_persona(app)
    source = app.personas.add_sources(persona.id, [_write_source(tmp_path)])[0]
    _compile_all_dimensions(app, persona.id, source.id)
    package_root = Path(app.personas.get(persona.id).package_path)
    expected_files = [
        "identity/profile.json",
        "identity/timeline.jsonl",
        "identity/self_narrative.md",
        "cognition/mental_models.json",
        "cognition/decision_heuristics.json",
        "cognition/values.json",
        "cognition/contradictions.json",
        "cognition/failure_patterns.json",
        "affect/temperament.json",
        "affect/emotional_triggers.json",
        "affect/attachment.json",
        "affect/needs.json",
        "affect/defenses.json",
        "expression/style.json",
        "expression/vocabulary.json",
        "expression/dialogue_examples.jsonl",
        "expression/anti_patterns.json",
        "relationships/relationships.json",
    ]

    for relative in expected_files:
        value = (package_root / relative).read_text(encoding="utf-8").strip()
        assert value not in {"", "{}", "[]"}, relative


def test_invalid_artifact_schema_is_rejected(app, tmp_path) -> None:
    persona = _create_persona(app)
    source = app.personas.add_sources(persona.id, [_write_source(tmp_path)])[0]
    task = app.compilation.create_task(persona.id)

    with pytest.raises(PersonaContinuumError) as exc:
        app.compilation.submit_research_artifact(
            task.id,
            {
                "artifact_id": "bad",
                "schema_version": "1.1",
                "dimension": "identity_and_timeline",
                "source_ids": [source.id],
                "claims": [{"content": "missing claim_type and confidence"}],
                "memories": [],
                "extracted_components": {},
                "conflicts": [],
                "uncertainty": {"level": 1.4},
                "created_by": "test",
                "artifact_hash": "bad",
            },
        )
    assert exc.value.code == "invalid_artifact"


def test_missing_compile_dimensions_returns_completed_with_gaps(app, tmp_path) -> None:
    persona = _create_persona(app)
    source = app.personas.add_sources(persona.id, [_write_source(tmp_path)])[0]
    task = app.compilation.create_task(persona.id)
    app.compilation.submit_research_artifact(task.id, _artifact(source.id, "identity_and_timeline"))

    app.compilation.compile_persona(persona.id, task.id)

    saved = app.compilation.get_task(task.id)
    assert saved.status == "completed_with_gaps"
    assert set(saved.plan["missing_dimensions"]) == set(DIMENSIONS) - {"identity_and_timeline"}


def test_chinese_memory_retrieval_returns_relevant_match_with_score(app) -> None:
    persona = _create_persona(app)
    relevant = app.memories.add_memory(
        persona.id,
        content="阿历克斯在米娜担心时选择暂停发布，优先保护用户信任。",
        memory_type=MemoryType.SEMANTIC,
        importance=0.4,
        source_kind="historical_self_report",
    )
    app.memories.add_memory(
        persona.id,
        content="Alex enjoys unrelated office snacks.",
        memory_type=MemoryType.SEMANTIC,
        importance=0.99,
        source_kind="historical_self_report",
    )

    results = app.memories.search_memories(persona.id, "用户信任", limit=2)

    assert results
    assert results[0].id == relevant.id
    assert results[0].metadata["score"] > 0
    assert "score_breakdown" in results[0].metadata
    assert results[0].metadata["retrieval_reason"] != "importance_fallback"


def test_continuation_branch_waits_for_host_artifact_before_advancing(app) -> None:
    persona = _create_persona(app)
    continuation = app.continuations.create(persona.id, "host must reason")
    branch = app.continuations.create_branch(continuation.id, seed=3)

    prepared = app.continuations.prepare_step(branch.id, target_date="2030-01-01")
    with pytest.raises(PersonaContinuumError) as exc:
        app.continuations.advance_branch(branch.id, target_date="2030-01-01")
    waiting = app.continuations.get_branch(branch.id)

    assert prepared["output_artifact_schema"]["required"]
    assert exc.value.code == "continuation_invalid_state"
    assert waiting.status == "waiting_for_host"
    assert waiting.key_events == []
    assert app.memories.search_memories(persona.id, "Branch seed", limit=3) == []


def test_room_reuses_persistent_sessions_and_preserves_private_boundaries(app) -> None:
    persona_a = _create_persona(app, "Room A", persona_id="room-a")
    persona_b = _create_persona(app, "Room B", persona_id="room-b")
    private = app.memories.add_memory(
        persona_b.id,
        content="PRIVATE_ROOM_SECRET_331",
        memory_type=MemoryType.SEMANTIC,
        importance=1.0,
        source_kind="private_note",
    )
    room = app.rooms.create_room([persona_a.id, persona_b.id], topic="Trust review")
    first = app.rooms.prepare_next(room["id"], "Opening prompt")
    app.rooms.commit_turn(
        room["id"],
        persona_a.id,
        first["session_id"],
        "Opening prompt",
        "Public room statement from A",
    )
    second = app.rooms.prepare_next(room["id"], "What did A say?")

    assert second["prepared"].session_id != first["session_id"]
    assert second["room"]["transcript"][-1]["persona_response"] == "Public room statement from A"
    assert private.id not in [memory.id for memory in second["prepared"].relevant_memories]


def test_room_add_persona_validates_persona_exists(app) -> None:
    persona = _create_persona(app)
    room = app.rooms.create_room([persona.id], topic="Validation")

    with pytest.raises(PersonaContinuumError) as exc:
        app.rooms.add_persona(room["id"], "missing-persona")
    assert exc.value.code == "not_found"
