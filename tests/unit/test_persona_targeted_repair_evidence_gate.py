from __future__ import annotations

from persona_continuum.application.persona_creation_service import (
    PersonaCreationOrchestrator,
)
from persona_continuum.compiler.schemas import ResearchArtifact


def test_persona_targeted_repair_keeps_gap_without_evidence() -> None:
    merged, outcomes = PersonaCreationOrchestrator._merge_targeted_repair_artifact(
        {"claims": [], "extracted_components": {}},
        {"claims": [], "extracted_components": {"vocabulary": ["invented"]}},
        target_components=["vocabulary"],
        allowed_source_ids=set(),
    )

    assert merged is None
    assert outcomes == {"vocabulary": "gap_insufficient_evidence"}


def test_persona_targeted_repair_changes_only_requested_component() -> None:
    current = {
        "artifact_id": "old",
        "claims": [],
        "extracted_components": {"values": ["care"], "vocabulary": []},
    }
    merged, outcomes = PersonaCreationOrchestrator._merge_targeted_repair_artifact(
        current,
        {
            "claims": [
                {
                    "content": "Uses short operational phrases.",
                    "source_id": "src_1",
                    "claim_type": "fictional_author_defined",
                }
            ],
            "extracted_components": {
                "values": ["overwritten"],
                "vocabulary": ["let me verify"],
            },
        },
        target_components=["vocabulary"],
        allowed_source_ids={"src_1"},
    )

    assert merged is not None
    assert merged["extracted_components"]["values"] == ["care"]
    assert merged["extracted_components"]["vocabulary"] == ["let me verify"]
    assert merged["claims"][0]["metadata"]["material_scope"] == "character_visible"
    assert outcomes == {"vocabulary": "repaired"}


def test_repair_hash_is_computed_after_schema_normalization(app) -> None:
    payload = {
        "artifact_id": "art_repair",
        "schema_version": "1.1",
        "dimension": "expression_dna",
        "source_ids": ["src_1"],
        "claims": [],
        "memories": [
            {
                "content": "supported memory",
                "type": "long_term",
                "source_kind": "fictional_author_defined",
                "source_id": "src_1",
            }
        ],
        "extracted_components": {"vocabulary": ["verify"]},
        "conflicts": [],
        "uncertainty": {"level": 0.2},
        "created_by": "test",
        "artifact_hash": "legacy-placeholder",
    }

    validated = ResearchArtifact.model_validate(payload).model_dump(mode="json")
    validated["artifact_hash"] = app.compilation._canonical_artifact_sha256(validated)
    submitted = ResearchArtifact.model_validate(validated).model_dump(mode="json")

    assert validated["artifact_hash"] == app.compilation._canonical_artifact_sha256(
        submitted
    )
