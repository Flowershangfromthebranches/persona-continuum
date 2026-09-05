from persona_continuum.application.persona_creation_service import PersonaCreationOrchestrator
from persona_continuum.compiler.schemas import ResearchArtifact


def test_merge_targeted_repair_artifact_includes_new_source_ids():
    current = {
        "artifact_id": "art_1",
        "dimension": "works_and_views",
        "source_ids": ["src_1"],
        "claims": [
            {
                "content": "Existing claim",
                "source_id": "src_1",
                "claim_type": "historical_inference",
                "confidence": 0.9,
            }
        ],
        "memories": [],
        "extracted_components": {"core_work": "Book 1"},
        "uncertainty": {"level": 0.2, "reasons": []},
        "created_by": "test_agent",
        "schema_version": "1.1",
    }
    repaired = {
        "artifact_id": "art_2",
        "dimension": "works_and_views",
        "source_ids": ["src_2"],
        "claims": [
            {
                "content": "New repaired claim",
                "source_id": "src_2",
                "claim_type": "historical_inference",
                "confidence": 0.85,
            }
        ],
        "memories": [],
        "extracted_components": {"core_work": "Book 1", "key_impact": "Major"},
        "uncertainty": {"level": 0.2, "reasons": []},
        "created_by": "test_agent",
        "schema_version": "1.1",
    }
    merged, outcomes = PersonaCreationOrchestrator._merge_targeted_repair_artifact(
        current,
        repaired,
        target_components=["key_impact"],
        allowed_source_ids={"src_2"},
    )
    assert merged is not None
    assert "src_2" in merged["source_ids"]
    assert "src_1" in merged["source_ids"]
    # Pydantic validation must pass without claim_source_id_not_in_artifact
    artifact = ResearchArtifact.model_validate(merged)
    assert len(artifact.claims) == 2
