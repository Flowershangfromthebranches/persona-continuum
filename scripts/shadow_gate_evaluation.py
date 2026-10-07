#!/usr/bin/env python3
"""Offline ShadowGateEvaluator (P1-A/B) — local, no API, no DB writes.

Replays SemanticGate("balanced") over REAL chat target turns that already
carry a classification outcome (evidence_extracted /
reviewed_no_independent_evidence) and scores gate quality:

* overall / high-confidence evidence recall
* per-dimension recall
* relationship / life-event / contradiction-support recall
* temporal coverage, selected ratio
* aggregate false-negative categories (never message text)

The report is pure aggregate JSON.  With ``--write-acceptance`` a small
acceptance record is stored under the data dir so the per-task "auto"
Semantic Gate resolver (P1-C) can enable balanced for large chats.

Privacy: read-only sqlite connection (``mode=ro``); nothing is sent
anywhere; report fields are counts and ratios only.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from persona_continuum.application.material_intelligence import EvidenceUnit
from persona_continuum.application.shadow_gate import evaluate_shadow_gate

DEFAULT_DB = Path("~/.persona-continuum/persona_continuum.sqlite").expanduser()
EVIDENCE_ORDER_POSITION = (
    "COALESCE(CAST(json_extract(source_locator_json, '$.segment_index') AS INTEGER), 0)"
)
CLASSIFIED_STATUSES = (
    "evidence_extracted",
    "reviewed_no_independent_evidence",
)


def load_units(
    db_path: Path,
    persona_id: str,
    *,
    limit: int | None = None,
    classified_only: bool = True,
) -> list[EvidenceUnit]:
    """Read chat rows in production conversation order (read-only)."""

    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        status_filter = ""
        params: list[object] = [persona_id]
        if classified_only:
            placeholders = ",".join("?" for _ in CLASSIFIED_STATUSES)
            status_filter = (
                " AND COALESCE(json_extract(metadata_json, '$.semantic_status'), '') IN "
                f"({placeholders}) "
            )
            params.extend(CLASSIFIED_STATUSES)
        limit_sql = ""
        if limit:
            limit_sql = " LIMIT ? "
            params.append(int(limit))
        rows = conn.execute(
            "SELECT * FROM persona_evidence_units WHERE persona_id = ? "
            "AND source_kind IN ('chat', 'chat_import', 'guided_interview') "
            f"{status_filter}"
            f"ORDER BY source_id, {EVIDENCE_ORDER_POSITION}, id"
            f"{limit_sql}",
            params,
        ).fetchall()
    finally:
        conn.close()
    units: list[EvidenceUnit] = []
    for row in rows:
        metadata = json.loads(row["metadata_json"] or "{}")
        units.append(
            EvidenceUnit(
                id=row["id"],
                persona_id=row["persona_id"],
                source_id=row["source_id"],
                source_locator=json.loads(row["source_locator_json"] or "{}"),
                speaker=row["speaker"],
                speaker_role=row["speaker_role"],
                timestamp=row["timestamp"],
                text=row["text"],
                normalized_text=row["normalized_text"] or "",
                evidence_type=row["evidence_type"] or "behavioral_observation",
                dimension_candidates=json.loads(row["dimension_candidates_json"] or "[]"),
                dimension_scores=json.loads(row["dimension_scores_json"] or "{}"),
                life_stage_candidates=json.loads(row["life_stage_candidates_json"] or "[]"),
                relationship_entities=json.loads(row["relationship_entities_json"] or "[]"),
                context_tags=json.loads(row["context_tags_json"] or "[]"),
                confidence=float(row["confidence"] or 0.0),
                extraction_method=row["extraction_method"] or "deterministic_segmenter",
                source_kind=row["source_kind"] or "user_provided",
                event_time=row["event_time"],
                metadata=metadata,
            )
        )
    return units


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--persona", required=True, help="Persona id in the local ledger.")
    parser.add_argument(
        "--limit", type=int, default=None, help="Cap loaded rows (conversation order)."
    )
    parser.add_argument(
        "--include-unclassified",
        action="store_true",
        help="Also feed target_pending/context rows through the gate sketch.",
    )
    parser.add_argument("--modes", default="balanced", help="Comma-separated gate modes.")
    parser.add_argument(
        "--write-acceptance",
        action="store_true",
        help="Persist the balanced acceptance record for the P1-C auto resolver.",
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    started = time.perf_counter()
    units = load_units(
        args.db,
        args.persona,
        limit=args.limit,
        classified_only=not args.include_unclassified,
    )
    load_seconds = time.perf_counter() - started
    if not units:
        print(
            json.dumps(
                {"error": "no_classified_chat_units", "persona": args.persona}, ensure_ascii=False
            )
        )
        return 1

    reports = {}
    for mode in [mode.strip() for mode in args.modes.split(",") if mode.strip()]:
        reports[mode] = evaluate_shadow_gate(units, mode=mode).model_dump(mode="json")
    payload = {
        "benchmark_kind": "shadow_gate_offline_replay_no_api_no_db_writes",
        "persona_id": args.persona,
        "db_path": str(args.db),
        "units_loaded": len(units),
        "load_seconds": round(load_seconds, 3),
        "evaluated_at": datetime.now(UTC).isoformat(),
        "reports": reports,
    }
    line = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    print(line)
    if args.output:
        args.output.write_text(line, encoding="utf-8")
    if args.write_acceptance:
        balanced = reports.get("balanced")
        if balanced is None:
            print(json.dumps({"error": "acceptance_requires_balanced_mode"}))
            return 1
        acceptance = {
            "kind": "shadow_gate_acceptance_v1",
            "mode": "balanced",
            "accepted": bool(balanced["accepted"]),
            "evaluated_at": payload["evaluated_at"],
            "units_loaded": len(units),
            "evidence_recall_overall": balanced["evidence_recall_overall"],
            "evidence_recall_high_confidence": balanced["evidence_recall_high_confidence"],
            "dimension_recall": balanced["dimension_recall"],
            "acceptance_reasons": balanced["acceptance_reasons"],
        }
        target = Path(
            args.output.parent if args.output else DEFAULT_DB.parent,
            "shadow_gate_acceptance.json",
        )
        target.write_text(
            json.dumps(acceptance, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(f"acceptance record: {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
