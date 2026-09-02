"""Regression tests for the PersonaCompileContract (P0 §2).

These guard the bridge between research-shaped artifact keys and the compiled
persona schema.  The historical failure mode was a bare ``continue`` that
silently discarded unknown keys, so a fully researched persona could compile to
3/18 components without a single warning.
"""

from __future__ import annotations

import pytest

from persona_continuum.compiler.contract import (
    COMPILE_CONTRACT,
    CONTRACT_VERSION,
    CORE_COMPONENTS,
    KEY_MAP,
    CompileCoverage,
    build_coverage,
    normalize_artifact,
)


def test_contract_declares_eighteen_components() -> None:
    assert len(COMPILE_CONTRACT) == 18


def test_contract_is_versioned() -> None:
    assert CONTRACT_VERSION == "1.0"
    assert CompileCoverage().contract_version == CONTRACT_VERSION


def test_core_components_are_a_subset() -> None:
    assert set(COMPILE_CONTRACT) >= CORE_COMPONENTS
    # A persona without these cannot act; everything else may legitimately gap.
    assert "identity_profile" in CORE_COMPONENTS
    assert "decision_heuristics" in CORE_COMPONENTS
    assert "temperament" not in CORE_COMPONENTS


def test_every_contract_key_has_a_declared_shape() -> None:
    for key, spec in COMPILE_CONTRACT.items():
        assert spec.shape in {"list", "dict"}, key


def test_every_contract_key_has_a_direct_compile_mapping() -> None:
    assert set(KEY_MAP) >= set(COMPILE_CONTRACT)


def test_evidence_keys_map_onto_schema_slots() -> None:
    record = normalize_artifact(
        "decisions_and_behavior",
        {
            "decision_style": {"default": "审慎", "crisis": "快速行动"},
            "risk_preparation_behaviors": ["存钱", "学技能"],
        },
    )
    assert record.unused == []
    assert "decision_heuristics" in record.mapped
    heuristics = record.mapped["decision_heuristics"]
    assert any("decision_style.default: 审慎" in str(entry) for entry in heuristics)
    assert any("risk_preparation: 存钱" in str(entry) for entry in heuristics)


def test_schema_keys_pass_through_unchanged() -> None:
    record = normalize_artifact("values_desires_contradictions", {"values": ["安全感"]})
    assert record.unused == []
    assert record.mapped["values"] == ["安全感"]
    assert "values_desires_contradictions.values" in record.aliases_used


def test_self_narrative_evidence_passes_through_unchanged() -> None:
    record = normalize_artifact(
        "identity_and_timeline",
        {"self_narrative_evidence": ["我更看重选择权。"]},
    )
    assert record.unused == []
    assert record.mapped["self_narrative_evidence"] == ["我更看重选择权。"]
    assert "identity_and_timeline.self_narrative_evidence" in record.aliases_used


def test_unknown_field_is_recorded_not_dropped() -> None:
    record = normalize_artifact("identity_and_timeline", {"totally_new_key": ["x"]})
    assert record.unused == ["identity_and_timeline.totally_new_key"]
    assert record.mapped == {}


def test_coverage_counts_mapped_and_unused_fields() -> None:
    artifacts = [
        {
            "dimension": "identity_and_timeline",
            "extracted_components": {"timeline": [{"event": "a"}], "mystery": [1]},
        },
        {
            "dimension": "affect_relationship_defense",
            "extracted_components": {"defense_mechanisms": ["验证优先"]},
        },
    ]
    records = [normalize_artifact(a["dimension"], a["extracted_components"]) for a in artifacts]
    merged = {"timeline_events": [{"event": "a"}], "defenses": ["验证优先"]}
    coverage = build_coverage(records, merged)

    assert coverage.artifact_fields_total == 3
    assert coverage.artifact_fields_mapped == 2
    assert coverage.unused_artifact_fields == ["identity_and_timeline.mystery"]
    assert coverage.compiled_component_coverage == round(2 / 18, 4)


def test_coverage_flags_missing_core_components() -> None:
    coverage = build_coverage([], {})
    assert not coverage.ok
    assert "identity_profile" in coverage.missing_core_components
    assert "temperament" in coverage.missing_optional_components
    assert coverage.missing_optional_components != [] or coverage.missing_core_components != []


def test_full_coverage_is_reported_as_ok() -> None:
    coverage = CompileCoverage(
        artifact_fields_total=18,
        artifact_fields_mapped=18,
        filled_components=sorted(COMPILE_CONTRACT),
    )
    assert coverage.ok
    assert coverage.compiled_component_coverage == 1.0
    assert "OK" in coverage.render_table()


def test_gap_is_a_legal_outcome() -> None:
    """A missing optional component must not be treated as a failure."""
    merged = {
        "identity_profile": {"core_invariants": ["谨慎"]},
        "values": ["选择权"],
        "decision_heuristics": ["评估风险"],
        "mental_models": ["钱 = 安全"],
        "expression_style": {"default_tone": ["简短"]},
        "relationships": ["与母亲亲近"],
        "timeline_events": [{"event": "x"}],
        "contradictions": ["重视钱 vs 不为钱越线"],
    }
    coverage = build_coverage([], merged)
    assert coverage.ok
    assert "temperament" in coverage.missing_optional_components
    assert coverage.compiled_component_coverage == round(8 / 18, 4)


def test_render_table_lists_every_component() -> None:
    table = build_coverage([], {}).render_table()
    for key in COMPILE_CONTRACT:
        assert key in table


@pytest.mark.parametrize("shape", ["list", "dict"])
def test_field_provenance_is_recorded(shape: str) -> None:
    """Traceability lives in the record, not in prefixing every value string."""
    value = {"a": [1]} if shape == "list" else {"a": 1}
    record = normalize_artifact("dim", {"views_on_ai": value})
    assert "dim.views_on_ai" in record.aliases_used
    assert record.mapped["mental_models"]
