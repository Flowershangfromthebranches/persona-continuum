#!/usr/bin/env python3
"""Local historical V4 retention audit, explicitly not fresh compiler validation."""

import argparse
import json
from pathlib import Path

from material_p02_benchmark import read_subset

from persona_continuum.application.material_chat import fold_conversation_turns
from persona_continuum.application.semantic_gate import SemanticGate


def audit(rows):
    gate = SemanticGate("balanced")
    selected = set()
    previous = None
    target_months, selected_months = set(), set()
    for turn in fold_conversation_turns(rows):
        decision = gate.decide(turn, previous)
        previous = turn
        if not turn.is_target:
            continue
        month = (turn.start_time or "undated")[:7]
        target_months.add(month)
        if decision.selected:
            selected.update(turn.evidence_unit_ids)
            selected_months.add(month)
    axes = {
        "personality_core": ["claims", "beliefs", "behaviors"],
        "values_desires": ["values", "motivations", "beliefs"],
        "decisions_behavior": ["decisions", "behaviors"],
        "relationships": ["relationships"],
        "emotional_patterns": ["emotions"],
        "communication_style": ["communication_patterns", "quotes"],
        "contradictions": ["contradictions", "negative_evidence"],
        "life_facts_temporal": ["events", "dates", "entities", "life_stage"],
    }
    result = {}
    for axis, fields in axes.items():
        reference = [
            row
            for row in rows
            if row.speaker_role != "exporter"
            and any(row.metadata.get("evidence_intelligence", {}).get(field) for field in fields)
        ]
        retained = sum(row.id in selected for row in reference)
        result[axis] = {
            "historically_extracted_rows": len(reference),
            "gate_retained": retained,
            "gate_excluded": len(reference) - retained,
            "retention_ratio": retained / len(reference) if reference else None,
        }
    return {
        "status": "historical_evidence_retention_only",
        "fresh_full_balanced_compiler_quality": "not_run_requires_external_transfer_approval",
        "raw_rows": len(rows),
        "dimensions": result,
        "time_months": sorted(target_months),
        "selected_months": sorted(selected_months),
        "temporal_coverage_preserved": target_months <= selected_months,
        "interpretation": (
            "Excluded historical rows are review candidates, not proven quality loss. "
            "Existing V4 completed rows are never gated in production."
        ),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(audit(read_subset(args.real_db)), ensure_ascii=False, indent=2)
    )
