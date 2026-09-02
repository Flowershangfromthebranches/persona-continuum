from __future__ import annotations

from pathlib import Path

from persona_continuum.application.compilation_service import REQUIRED_DIMENSIONS
from tests.fixtures.base_persona import COMPONENTS, build_base_persona


def _counts(app, persona_id: str) -> tuple[int, int, int]:
    claim_count = app.database.conn.execute(
        "SELECT COUNT(*) AS count FROM claims WHERE persona_id = ?", (persona_id,)
    ).fetchone()["count"]
    memory_count = app.database.conn.execute(
        "SELECT COUNT(*) AS count FROM memories WHERE persona_id = ?", (persona_id,)
    ).fetchone()["count"]
    provenance_count = app.database.conn.execute(
        "SELECT COUNT(*) AS count FROM lineage WHERE persona_id = ? "
        "AND child_type IN ('claim', 'memory')",
        (persona_id,),
    ).fetchone()["count"]
    return claim_count, memory_count, provenance_count


def test_persona_recompile_idempotency_preserves_evidence_rows(app) -> None:
    persona_id, task_id = build_base_persona(app)
    before = _counts(app, persona_id)
    task = app.compilation.get_task(task_id)
    task.status = "created"
    app.compilation._save_task(task)

    app.compilation.compile_persona(persona_id, task_id)

    assert _counts(app, persona_id) == before


def test_compile_renders_structured_self_narrative_evidence(app) -> None:
    persona_id, task_id = build_base_persona(app, persona_id="structured-narrative")
    task = app.compilation.get_task(task_id)
    task.artifacts[-1]["extracted_components"]["self_narrative_evidence"] = [
        {"statement": "I verify before committing.", "context": "under uncertainty"}
    ]
    task.status = "created"
    app.compilation._save_task(task)

    app.compilation.compile_persona(persona_id, task_id)

    narrative_path = (
        Path(app.personas.get(persona_id).package_path) / "identity" / "self_narrative.md"
    )
    rendered = narrative_path.read_text(encoding="utf-8")
    assert '"statement":"I verify before committing."' in rendered
    assert '"context":"under uncertainty"' in rendered


def test_component_gaps_use_canonical_compile_mapping(app) -> None:
    artifact = {
        "dimension": "identity_and_timeline",
        "extracted_components": {
            "core_invariants": ["cautious"],
            "timeline": [{"date": "2025", "event": "kept a fallback plan"}],
            "self_narrative_evidence": ["I value choice."],
        },
    }

    assert app.compilation._component_gaps([artifact]) == {}


def test_component_gaps_measure_global_canonical_coverage(app) -> None:
    artifacts = [
        {
            "dimension": "identity_and_timeline",
            "extracted_components": COMPONENTS,
        }
    ]
    artifacts.extend(
        {"dimension": dimension, "extracted_components": {}}
        for dimension in REQUIRED_DIMENSIONS
        if dimension != "identity_and_timeline"
    )

    assert app.compilation._component_gaps(artifacts) == {}
