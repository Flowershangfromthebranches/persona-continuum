from __future__ import annotations

import json
from typing import Any

from persona_continuum.domain.persona import PersonaType, RunMode

COMPONENTS: dict[str, Any] = {
    "identity_profile": {"name": "Lin", "role": "operations lead"},
    "timeline_events": [{"date": "2025", "event": "kept a fallback plan"}],
    "self_narrative_evidence": ["I prefer to verify before committing."],
    "mental_models": ["Evidence should change confidence gradually."],
    "decision_heuristics": ["Keep a reversible option under uncertainty."],
    "values": ["safety", "autonomy", "care"],
    "contradictions": ["wants certainty but resists being controlled"],
    "failure_patterns": ["overchecks when pressure stays high"],
    "temperament": {"baseline": "calm and cautious"},
    "emotional_triggers": ["coercion", "unexplained betrayal"],
    "attachment_patterns": {"style": "slow trust"},
    "needs_and_desires": ["economic safety", "choice"],
    "defenses": ["asks for verifiable details"],
    "expression_style": {"tone": "brief, concrete"},
    "vocabulary": ["verify", "fallback"],
    "dialogue_examples": ["Let me verify that first."],
    "anti_patterns": ["grand heroic declarations"],
    "relationships": [{"counterpart": "mother", "stance": "protective"}],
}


def build_base_persona(app, *, persona_id: str = "base-cert") -> tuple[str, str]:
    persona = app.personas.create(
        display_name="Lin",
        aliases=[],
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode=RunMode.DIGITAL_CONTINUATION,
        data_cutoff_date="2026-01-01",
        persona_id=persona_id,
    )
    source = app.personas.add_source_text(
        persona.id,
        title="character visible",
        source_type="md",
        canonical_url=None,
        publisher=None,
        author=None,
        published_at=None,
        accessed_at=None,
        content=json.dumps(COMPONENTS, ensure_ascii=False),
        metadata={"material_scope": "character_visible"},
    )
    task = app.compilation.create_task(persona.id)
    app.compilation.submit_research_artifact(
        task.id,
        {
            "artifact_id": f"art_{persona.id}",
            "schema_version": "1.1",
            "dimension": "identity_and_timeline",
            "source_ids": [source.id],
            "claims": [
                {
                    "content": "Lin verifies risk before acting.",
                    "source_id": source.id,
                    "claim_type": "fictional_author_defined",
                    "confidence": 0.9,
                    "reliability": 0.9,
                    "inference_strength": 0.1,
                    "metadata": {"material_scope": "character_visible"},
                }
            ],
            "memories": [
                {
                    "content": "Lin keeps a fallback for economic safety.",
                    "type": "semantic",
                    "importance": 0.8,
                    "source_kind": "fictional_author_defined",
                    "source_id": source.id,
                    "source_confidence": 0.9,
                    "participants": [],
                    "metadata": {"material_scope": "character_visible"},
                }
            ],
            "extracted_components": COMPONENTS,
            "conflicts": [],
            "uncertainty": {"level": 0.1, "notes": []},
            "created_by": "test_host",
            "artifact_hash": f"hash-{persona.id}",
        },
    )
    app.compilation.compile_persona(persona.id, task.id)
    app.personas.activate(persona.id)
    return persona.id, task.id
