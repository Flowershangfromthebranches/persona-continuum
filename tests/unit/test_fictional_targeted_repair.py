from __future__ import annotations

from persona_continuum.application.persona_creation_service import (
    PersonaCreationOrchestrator,
)


def _claim(content: str, source_id: str, scope: str) -> dict[str, object]:
    return {
        "content": content,
        "source_id": source_id,
        "claim_type": "fictional_author_defined",
        "metadata": {"material_scope": scope},
    }


def test_fictional_targeted_repair_reads_only_character_visible_claims() -> None:
    artifact = {
        "claims": [
            _claim("visible stress response", "src_visible", "character_visible"),
            _claim("author secret", "src_author", "author_only"),
            _claim("expected answer", "src_eval", "evaluation_only"),
        ]
    }

    items = PersonaCreationOrchestrator._targeted_repair_evidence_items(
        artifact,
        known_source_ids={"src_visible", "src_author", "src_eval"},
    )

    assert [item["content"] for item in items] == ["visible stress response"]
    assert items[0]["intelligence"] == {"material_scope": "character_visible"}


def test_targeted_repair_slice_contains_no_legacy_artifact_content() -> None:
    artifact = {
        "dimension": "expression_dna",
        "claims": [_claim("short phrasing", "src_visible", "character_visible")],
        "extracted_components": {
            "quotes": ["Still here."],
            "default_tone": ["brief"],
            "branch_architecture": {"secret": True},
        },
    }

    sliced = PersonaCreationOrchestrator._targeted_repair_artifact_slice(
        artifact, ["vocabulary"]
    )

    assert sliced["extracted_components"] == {}
    assert sliced["conflicts"] == []
    assert sliced["target_components"] == ["vocabulary"]
    assert "branch_architecture" not in str(sliced)
    assert "Still here" not in str(sliced)
