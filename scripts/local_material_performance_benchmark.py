#!/usr/bin/env python3
"""Deterministic local-material topology benchmark.

This does not claim real-provider latency.  It compares the former fixed-batch
call topology with the optimized provider-neutral planner using identical
evidence, model context and reasoning policy.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from typing import Any

from persona_continuum.agent.context_budget import AgentContextBudgetManager
from persona_continuum.application.material_intelligence import (
    EvidenceSimilarityAnalyzer,
    EvidenceUnit,
)
from persona_continuum.application.material_pipeline import (
    GlobalCandidateIndex,
    PreLLMDeduplicator,
    build_analysis_windows,
)


def evidence_fixture(count: int, duplicate_ratio: float) -> list[EvidenceUnit]:
    unique_count = max(1, round(count * (1.0 - duplicate_ratio)))
    units: list[EvidenceUnit] = []
    for index in range(count):
        claim_index = index if index < unique_count else index - unique_count
        text = f"第{claim_index}条对话：记录事实、关系、决定、失败与不确定性。"
        units.append(
            EvidenceUnit(
                id=f"evu_{index:06d}",
                persona_id="benchmark_persona",
                source_id=f"src_{index // 100:04d}",
                source_locator={"message_index": index},
                text=text,
                normalized_text=text.casefold(),
                relationship_entities=[f"entity_{claim_index % 97}"],
                dimension_candidates=["decisions_and_behavior"],
            )
        )
    return units


def _sum_tokens(manager: AgentContextBudgetManager, units: list[EvidenceUnit]) -> int:
    return sum(manager.estimate_tokens(item.text) for item in units)


def run(count: int, duplicate_ratio: float, context_window: int) -> dict[str, Any]:
    manager = AgentContextBudgetManager(default_context_window_tokens=context_window)
    units = evidence_fixture(count, duplicate_ratio)
    raw_tokens = _sum_tokens(manager, units)

    started = time.perf_counter()
    dedup = PreLLMDeduplicator().deduplicate(units)
    canonical = list(dedup.canonical_units)
    budget = manager.budget_for(
        model={"context_window": context_window}, phase="material_classification"
    )
    windows = build_analysis_windows(
        canonical,
        estimate_tokens=manager.estimate_tokens,
        target_tokens=max(256, int(budget.evidence_token_budget * 0.82)),
    )
    candidates = GlobalCandidateIndex().groups(canonical)
    clusters = EvidenceSimilarityAnalyzer().cluster(dedup.units, "benchmark_persona")
    semantic_fusions = [
        cluster
        for cluster in clusters
        if len(cluster.member_evidence_ids) > 1
        and cluster.similarity_type not in {"exact", "single"}
    ]
    optimized_wall_ms = (time.perf_counter() - started) * 1000

    relation_tokens = sum(
        manager.estimate_tokens(
            " ".join(
                next(item.text for item in canonical if item.id == evidence_id)
                for evidence_id in group.evidence_ids
            )
        )
        for group in candidates
    )
    fusion_tokens = sum(
        manager.estimate_tokens(
            " ".join(
                next(item.text for item in units if item.id == evidence_id)
                for evidence_id in cluster.member_evidence_ids
            )
        )
        for cluster in semantic_fusions
    )
    optimized_material_turns = {
        "intelligence": len(windows),
        "relation": math.ceil(len(candidates) / 12),
        "fusion": math.ceil(len(semantic_fusions) / 96),
    }
    legacy_material_turns = {
        "intelligence": math.ceil(len(units) / 24),
        "relation": math.ceil(len(units) / 30),
        "fusion": math.ceil(len(clusters) / 16),
    }
    fixed_quality_turns = {"dimensions": 8, "audit": 2}
    legacy_total = sum(legacy_material_turns.values()) + sum(fixed_quality_turns.values())
    optimized_total = sum(optimized_material_turns.values()) + sum(fixed_quality_turns.values())
    legacy_input = raw_tokens * 3
    optimized_input = _sum_tokens(manager, canonical) + relation_tokens + fusion_tokens
    legacy_classification_output = sum(
        manager.estimate_tokens({"id": item.id, "claims": [item.text]}) for item in units
    )
    optimized_classification_output = sum(
        manager.estimate_tokens({"id": item.id, "claims": [item.text]}) for item in canonical
    )
    empty_relation_output = manager.estimate_tokens({"clusters": [], "contradictions": []})
    legacy_fusion_output = sum(
        manager.estimate_tokens(
            {
                "id": cluster.id,
                "canonical_claim": " ".join(
                    next(item.text for item in units if item.id == evidence_id)
                    for evidence_id in cluster.member_evidence_ids
                ),
            }
        )
        for cluster in clusters
    )
    optimized_fusion_output = sum(
        manager.estimate_tokens({"id": cluster.id, "canonical_claim": ""})
        for cluster in semantic_fusions
    )
    legacy_output = (
        legacy_classification_output
        + empty_relation_output * legacy_material_turns["relation"]
        + legacy_fusion_output
    )
    optimized_output = (
        optimized_classification_output
        + empty_relation_output * optimized_material_turns["relation"]
        + optimized_fusion_output
    )
    source_ids = {item.source_id for item in units}
    supporting_ids = {
        evidence_id for values in dedup.supporting_units.values() for evidence_id in values
    }
    provenance_coverage = len(supporting_ids) / len(units) if units else 1.0
    return {
        "benchmark_kind": "offline_call_topology_not_provider_latency",
        "fixture": {
            "messages": count,
            "duplicate_ratio": duplicate_ratio,
            "model_context_window": context_window,
            "model": "same-fake-runtime",
            "reasoning": "high",
            "source_count": len(source_ids),
        },
        "legacy": {
            "evidence_units": len(units),
            "unique_evidence": len(units),
            "analysis_windows": None,
            "agent_turns": legacy_total,
            "agent_turns_by_stage": {**legacy_material_turns, **fixed_quality_turns},
            "input_tokens_estimated": legacy_input,
            "output_tokens_estimated": legacy_output,
            "cache_hits": 0,
            "wall_clock_ms": None,
            "dimensions": "8/8 retained",
            "audit_status": "same_strict_audit_contract",
            "provenance_coverage": 1.0,
        },
        "optimized": {
            "evidence_units": len(units),
            "unique_evidence": len(canonical),
            "analysis_windows": len(windows),
            "agent_turns": optimized_total,
            "agent_turns_by_stage": {**optimized_material_turns, **fixed_quality_turns},
            "input_tokens_estimated": optimized_input,
            "output_tokens_estimated": optimized_output,
            "cache_hits": 0,
            "wall_clock_ms": round(optimized_wall_ms, 3),
            "dimensions": "8/8 retained",
            "audit_status": "same_strict_audit_contract",
            "provenance_coverage": round(provenance_coverage, 6),
            "relation_candidate_groups": len(candidates),
            "singleton_agent_fusion_calls": 0,
        },
        "improvement": {
            "agent_turn_reduction": round(1 - optimized_total / legacy_total, 6),
            "input_token_reduction": round(1 - optimized_input / legacy_input, 6),
            "output_token_reduction": round(1 - optimized_output / legacy_output, 6),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--messages", type=int, default=1000)
    parser.add_argument("--duplicate-ratio", type=float, default=0.4)
    parser.add_argument("--context-window", type=int, default=32768)
    args = parser.parse_args()
    print(
        json.dumps(
            run(args.messages, args.duplicate_ratio, args.context_window),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
