"""Offline ShadowGateEvaluator (P1-A/B): local Semantic Gate quality replay.

This module is deliberately side-effect free:

* it never calls an Agent, an API, or any network transport;
* it never mutates storage — the caller passes already-classified rows and
  the gate is replayed on in-memory views only;
* it never emits message text — every report field is an aggregate over
  turn-level outcomes, so private chat bodies cannot leak into reports.

Ground truth comes from the persisted classification states of real target
turns (``evidence_extracted`` / ``reviewed_no_independent_evidence``).  The
gate is re-run in the requested mode over the same conversation order and
compared against those outcomes, producing recall-style quality metrics that
decide whether the balanced gate may be enabled by default for large chats
(P1-B acceptance).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from pydantic import BaseModel, Field

from persona_continuum.application.material_chat import (
    CHAT_SOURCE_KINDS,
    fold_conversation_turns,
)
from persona_continuum.application.semantic_gate import SemanticGate

DEFAULT_HIGH_CONFIDENCE_THRESHOLD = 0.7
DEFAULT_HIGH_VALUE_RECALL_THRESHOLD = 0.95
DEFAULT_DIMENSION_RECALL_THRESHOLD = 0.90


class ShadowGateReport(BaseModel):
    """Aggregate gate-quality report.  Contains no message text by contract."""

    mode: str = "balanced"
    units_total: int = 0
    target_turns_total: int = 0
    selected_total: int = 0
    selected_ratio: float = 0.0
    evidence_turns_total: int = 0
    reviewed_turns_total: int = 0
    unknown_outcome_turns: int = 0
    evidence_recall_overall: float = 0.0
    high_confidence_threshold: float = DEFAULT_HIGH_CONFIDENCE_THRESHOLD
    evidence_turns_high_confidence: int = 0
    evidence_recall_high_confidence: float = 0.0
    dimension_recall: dict[str, float] = Field(default_factory=dict)
    dimension_evidence_turns: dict[str, int] = Field(default_factory=dict)
    relationship_evidence_turns: int = 0
    relationship_recall: float = 0.0
    life_event_evidence_turns: int = 0
    life_event_recall: float = 0.0
    contradiction_support_evidence_turns: int = 0
    contradiction_support_recall: float = 0.0
    temporal_coverage: float = 0.0
    temporal_months_total: int = 0
    temporal_months_covered: int = 0
    false_negative_categories: dict[str, int] = Field(default_factory=dict)
    accepted: bool = False
    acceptance_reasons: list[str] = Field(default_factory=list)
    # Acceptance thresholds actually applied (echoed for auditability).
    high_value_recall_threshold: float = DEFAULT_HIGH_VALUE_RECALL_THRESHOLD
    dimension_recall_threshold: float = DEFAULT_DIMENSION_RECALL_THRESHOLD


def _member_status(unit: Any) -> str:
    metadata = getattr(unit, "metadata", None) or {}
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get("semantic_status") or "")


def _turn_confidence(members: list[Any]) -> float:
    values = [float(getattr(unit, "confidence", None) or 0.0) for unit in members]
    return max(values) if values else 0.0


def _turn_dimensions(members: list[Any]) -> set[str]:
    dimensions: set[str] = set()
    for unit in members:
        for candidate in getattr(unit, "dimension_candidates", None) or ():
            dimensions.add(str(candidate))
    return dimensions


def _turn_relationships(members: list[Any]) -> bool:
    return any(bool(getattr(unit, "relationship_entities", None)) for unit in members)


def _turn_life_events(members: list[Any]) -> bool:
    return any(bool(getattr(unit, "life_stage_candidates", None)) for unit in members)


def _turn_contradiction_support(members: list[Any]) -> bool:
    for unit in members:
        metadata = getattr(unit, "metadata", None) or {}
        intelligence = metadata.get("evidence_intelligence") if isinstance(metadata, dict) else None
        if not isinstance(intelligence, dict):
            continue
        for key in ("contradictions", "negative_evidence"):
            if bool(intelligence.get(key)):
                return True
    return False


def _categorize_false_negative(members: list[Any], text: str) -> list[str]:
    """Aggregate feature categories only — never message content."""

    categories: list[str] = []
    if len(text) >= 240:
        categories.append("long_expression")
    elif len(text) <= 16:
        categories.append("very_short")
    if re.search(r"\d", text):
        categories.append("contains_numbers")
    if _turn_relationships(members):
        categories.append("relationship_signal")
    if _turn_life_events(members):
        categories.append("life_event_signal")
    if _turn_contradiction_support(members):
        categories.append("contradiction_support_signal")
    if _turn_confidence(members) >= DEFAULT_HIGH_CONFIDENCE_THRESHOLD:
        categories.append("high_confidence")
    for dimension in sorted(_turn_dimensions(members))[:3]:
        categories.append(f"dimension:{dimension}")
    return categories or ["no_local_signal"]


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 1.0


def load_shadow_gate_acceptance(data_dir: Any) -> dict[str, Any] | None:
    """Read the local acceptance record written by the shadow evaluation script.

    The record is produced offline (scripts/shadow_gate_evaluation.py
    --write-acceptance) and is the ONLY input the P1-C "auto" resolver uses
    to enable balanced for large chats.  Missing/corrupt file means "no
    acceptance", which keeps auto on full fidelity.
    """

    from pathlib import Path

    path = Path(str(data_dir)) / "shadow_gate_acceptance.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) and "accepted" in payload else None


def evaluate_shadow_gate(
    units: Any,
    *,
    mode: str = "balanced",
    high_confidence_threshold: float = DEFAULT_HIGH_CONFIDENCE_THRESHOLD,
    high_value_recall_threshold: float = DEFAULT_HIGH_VALUE_RECALL_THRESHOLD,
    dimension_recall_threshold: float = DEFAULT_DIMENSION_RECALL_THRESHOLD,
) -> ShadowGateReport:
    """Replay the Semantic Gate over already-classified turns and score it.

    ``units`` must be chat EvidenceUnit-like rows in conversation order with
    their persisted ``semantic_status`` metadata intact.  Nothing here writes
    storage or contacts a model.
    """

    rows = list(units)
    report = ShadowGateReport(
        mode=mode,
        units_total=len(rows),
        high_confidence_threshold=high_confidence_threshold,
        high_value_recall_threshold=high_value_recall_threshold,
        dimension_recall_threshold=dimension_recall_threshold,
    )
    by_id: dict[str, Any] = {str(unit.id): unit for unit in rows}
    gate = SemanticGate(mode)
    previous = None

    # Turn-level outcome buckets.  "evidence" turns are positives (gate must
    # select them); "reviewed" turns are true negatives (skipping them is
    # correct); anything else has no known outcome and is excluded from
    # recall denominators while still driving the gate's sketch state.
    evidence_turns: list[dict[str, Any]] = []
    reviewed_count = 0
    unknown_count = 0
    selected_total = 0
    months_total: set[str] = set()
    months_covered: set[str] = set()
    dimension_hits: dict[str, int] = defaultdict(int)
    dimension_selected: dict[str, int] = defaultdict(int)
    relationship_total = relationship_selected = 0
    life_event_total = life_event_selected = 0
    contradiction_total = contradiction_selected = 0
    high_confidence_total = high_confidence_selected = 0
    false_negatives: list[dict[str, Any]] = []

    for turn in fold_conversation_turns(rows):
        decision = gate.decide(turn, previous)
        previous = turn
        if not turn.is_target or turn.source_kind not in CHAT_SOURCE_KINDS:
            continue
        members = [by_id[mid] for mid in turn.evidence_unit_ids if mid in by_id]
        selected = bool(decision.selected)
        selected_total += int(selected)
        report.target_turns_total += 1
        statuses = {_member_status(unit) for unit in members}
        if "evidence_extracted" in statuses:
            bucket = "evidence"
        elif "reviewed_no_independent_evidence" in statuses or statuses == {
            "semantic_gate_skipped"
        }:
            bucket = "reviewed"
        else:
            bucket = "unknown"
        if bucket == "reviewed":
            reviewed_count += 1
            continue
        if bucket == "unknown":
            unknown_count += 1
            continue
        month = (turn.start_time or "undated")[:7]
        months_total.add(month)
        record = {
            "members": members,
            "text": turn.text,
            "selected": selected,
            "month": month,
        }
        evidence_turns.append(record)
        if selected:
            months_covered.add(month)
        for dimension in _turn_dimensions(members):
            dimension_hits[dimension] += 1
            dimension_selected[dimension] += int(selected)
        if _turn_relationships(members):
            relationship_total += 1
            relationship_selected += int(selected)
        if _turn_life_events(members):
            life_event_total += 1
            life_event_selected += int(selected)
        if _turn_contradiction_support(members):
            contradiction_total += 1
            contradiction_selected += int(selected)
        if _turn_confidence(members) >= high_confidence_threshold:
            high_confidence_total += 1
            high_confidence_selected += int(selected)
            record["high_confidence"] = True
        if not selected:
            false_negatives.append(record)

    evidence_total = len(evidence_turns)
    evidence_selected = sum(1 for record in evidence_turns if record["selected"])
    report.selected_total = selected_total
    report.selected_ratio = _ratio(selected_total, report.target_turns_total)
    report.evidence_turns_total = evidence_total
    report.reviewed_turns_total = reviewed_count
    report.unknown_outcome_turns = unknown_count
    report.evidence_recall_overall = _ratio(evidence_selected, evidence_total)
    report.evidence_turns_high_confidence = high_confidence_total
    report.evidence_recall_high_confidence = _ratio(
        high_confidence_selected, high_confidence_total
    )
    report.dimension_evidence_turns = dict(sorted(dimension_hits.items()))
    report.dimension_recall = {
        dimension: _ratio(dimension_selected[dimension], hits)
        for dimension, hits in sorted(dimension_hits.items())
    }
    report.relationship_evidence_turns = relationship_total
    report.relationship_recall = _ratio(relationship_selected, relationship_total)
    report.life_event_evidence_turns = life_event_total
    report.life_event_recall = _ratio(life_event_selected, life_event_total)
    report.contradiction_support_evidence_turns = contradiction_total
    report.contradiction_support_recall = _ratio(
        contradiction_selected, contradiction_total
    )
    report.temporal_months_total = len(months_total)
    report.temporal_months_covered = len(months_covered)
    report.temporal_coverage = _ratio(len(months_covered), len(months_total))

    categories: dict[str, int] = defaultdict(int)
    false_negative: dict[str, Any]
    for false_negative in false_negatives:
        for category in _categorize_false_negative(
            false_negative["members"], false_negative["text"]
        ):
            categories[category] += 1
    report.false_negative_categories = dict(sorted(categories.items()))

    # P1-B acceptance: high-value evidence recall must clear the threshold
    # and every observed dimension must clear its own floor.  Failures list
    # concrete reasons so the gate can be improved deterministically (local
    # features / bypass) instead of widening the random reserve.
    reasons: list[str] = []
    if report.evidence_recall_overall < high_value_recall_threshold:
        reasons.append(
            f"evidence_recall_overall {report.evidence_recall_overall:.4f} "
            f"< {high_value_recall_threshold:.2f}"
        )
    if report.evidence_recall_high_confidence < high_value_recall_threshold:
        reasons.append(
            f"evidence_recall_high_confidence {report.evidence_recall_high_confidence:.4f} "
            f"< {high_value_recall_threshold:.2f}"
        )
    for dimension, recall in report.dimension_recall.items():
        if recall < dimension_recall_threshold:
            reasons.append(
                f"dimension_recall[{dimension}] {recall:.4f} < {dimension_recall_threshold:.2f}"
            )
    report.acceptance_reasons = reasons
    report.accepted = not reasons
    return report
