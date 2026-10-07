"""Phase 7 benchmark: raw recall over a READ-ONLY snapshot of the real database.

What this proves (and what it does not):

* It takes a ``sqlite3`` backup of the production database into a scratch
  directory and only ever opens THE COPY.  The real database is never written.
* It migrates the copy (additive), backfills Episodes over the real committed
  turns (no model) and groups them into Chapters (no model), so every memory
  object it tests carries REAL provenance pointing at REAL conversation.
* It then runs raw recall over >= ``--min-objects`` of them and reports the
  numbers the Phase 7 brief asks for: resolvable / partial / unavailable,
  duplicate excerpts, wrong-scope excerpts, budget violations, and
  message-boundary integrity (every excerpt message must be byte-identical to
  its source row).
* It writes a markdown inspection sample so a human can verify that the
  excerpts really are the original words of the events they claim to describe.

Facts / Threads / Long-term summaries are NOT fabricated here: they require a
model, and inventing them would prove nothing about provenance.  Those paths
are covered by the synthetic suite and by ``phase7_raw_recall_ab.py``.

Usage::

    .venv/bin/python scripts/phase7_raw_recall_snapshot.py            \\
        [--source ~/.persona-continuum/persona_continuum.sqlite]      \\
        [--scratch /tmp/pc-phase7-snapshot] [--objects 24]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.domain.raw_recall import (
    RawMemoryRef,
    RawRecallBudget,
    RawRecallScope,
    SourceAvailability,
)

PRODUCTION_DB = Path.home() / ".persona-continuum" / "persona_continuum.sqlite"
#: Tolerance on the global budget: the estimate is an approximation, so a
#: violation is a REAL overrun, not a one-token rounding difference.
BUDGET_TOLERANCE_TOKENS = 64


def snapshot(source: Path, scratch: Path) -> Path:
    """Backup the production database into the scratch dir (read-only source)."""

    scratch.mkdir(parents=True, exist_ok=True)
    target = scratch / "persona_continuum.sqlite"
    if target.exists():
        target.unlink()
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return target


def _raw_rows(app: PersonaContinuum) -> dict[str, tuple[str, str]]:
    """Every raw message the stores can legitimately provide."""

    rows: dict[str, tuple[str, str]] = {}
    for row in app.database.conn.execute(
        "SELECT id, user_message, persona_response FROM session_turns"
    ).fetchall():
        rows[str(row["id"])] = (str(row["user_message"]), str(row["persona_response"]))
    for row in app.database.conn.execute(
        "SELECT turn_id, content FROM room_transcripts"
    ).fetchall():
        rows[f"room:{row['turn_id']}"] = (str(row["content"]), "")
    return rows


def _scopes(app: PersonaContinuum) -> list[RawRecallScope]:
    """The (persona, counterpart, branch) triples that actually hold Episodes."""

    rows = app.database.conn.execute(
        """
        SELECT persona_id, counterpart_id, branch_id, COUNT(*) AS c
        FROM memory_episodes GROUP BY 1, 2, 3 ORDER BY c DESC
        """
    ).fetchall()
    return [
        RawRecallScope(
            persona_id=str(row["persona_id"]),
            counterpart_id=str(row["counterpart_id"]),
            branch_id=str(row["branch_id"] or "main"),
        )
        for row in rows
    ]


def _query_for(text: str) -> str:
    """A query that a user could plausibly have asked about this memory."""

    words = [word for word in str(text or "").split() if len(word) >= 2]
    return " ".join(words[:8]) or text


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = datetime.now(UTC)
    scratch = Path(args.scratch) if args.scratch else Path(
        tempfile.mkdtemp(prefix="pc-phase7-snapshot-")
    )
    source = Path(args.source)
    if not source.exists():
        raise SystemExit(f"source database not found: {source}")
    copied = snapshot(source, scratch)

    config = Config(data_dir=scratch)
    app = PersonaContinuum(config)
    app.init()

    # Model-free provenance over the real turns: Episodes first, then the
    # deterministic Chapter grouping on top of them.
    episode_report = app.episodes.backfill(limit=args.episode_batch)
    hierarchy_report = app.hierarchies.backfill(limit=args.hierarchy_batch)

    # Real scopes first: a real database has many (persona, counterpart, branch)
    # triples, and raw recall must be asked in the RIGHT one.
    scopes = _scopes(app)
    budget = RawRecallBudget()
    raw_store = _raw_rows(app)

    objects: list[dict[str, Any]] = []
    for scope in scopes:
        if len(objects) >= args.objects:
            break
        remaining = args.objects - len(objects)
        episodes = sorted(
            app.episodes.list_episodes(
                persona_id=scope.persona_id,
                counterpart_id=scope.counterpart_id,
                branch_id=scope.branch_id,
            ),
            key=lambda item: item.started_at,
        )[:remaining]
        for episode in episodes:
            structured = episode.structured_summary()
            objects.append(
                {
                    "kind": "episode",
                    "scope": scope,
                    "id": episode.id,
                    "label": episode.title or structured.title or "未摘要",
                    "query": _query_for(f"{episode.title} {structured.summary}"),
                    "ref": RawMemoryRef(ref_type="episode", ref_id=episode.id),
                }
            )
        chapters = app.hierarchies.list_summaries(
            persona_id=scope.persona_id,
            counterpart_id=scope.counterpart_id,
            branch_id=scope.branch_id,
            level=1,
            limit=max(0, args.objects - len(objects)),
        )
        for chapter in chapters:
            objects.append(
                {
                    "kind": "chapter",
                    "scope": scope,
                    "id": chapter.id,
                    "label": chapter.title or "未总结",
                    "query": _query_for(f"{chapter.title} {chapter.summary}"),
                    "ref": RawMemoryRef(ref_type="summary", ref_id=chapter.id),
                }
            )
    # Lineage objects are EXTRA on purpose: the brief allows Digital Experience
    # only where a reliable lineage already exists, so it must not crowd out the
    # contractually provenance-backed objects above.
    lineage_wanted = args.lineage_count if args.include_lineage else 0
    if lineage_wanted:
        for scope in scopes[:3]:
            rows = app.database.conn.execute(
                "SELECT DISTINCT child_id FROM lineage WHERE parent_type = 'session_turn' "
                "AND child_type = 'episode' LIMIT ?",
                (lineage_wanted,),
            ).fetchall()
            for row in rows:
                objects.append(
                    {
                        "kind": "digital_experience",
                        "scope": scope,
                        "id": str(row["child_id"]),
                        "label": str(row["child_id"]),
                        "query": "",
                        "ref": RawMemoryRef(
                            ref_type="digital_experience", ref_id=str(row["child_id"])
                        ),
                    }
                )
            if sum(1 for item in objects if item["kind"] == "digital_experience") >= lineage_wanted:
                break

    duplicates = 0
    wrong_scope = 0
    budget_violations = 0
    boundary_violations: list[str] = []
    counts = {"resolvable": 0, "partial": 0, "unavailable": 0, "deleted": 0}
    for item in objects:
        scope = item["scope"]
        result = app.raw_recall.recall(
            scope=scope, query=item["query"], memory_refs=[item["ref"]], budget=budget
        )
        if result.excerpts:
            counts["resolvable"] += 1
        elif any(
            ref.availability is SourceAvailability.PARTIAL for ref in result.partial
        ):
            counts["partial"] += 1
        elif any(ref.availability is SourceAvailability.DELETED for ref in result.unavailable):
            counts["deleted"] += 1
        else:
            counts["unavailable"] += 1
        seen: set[str] = set()
        for excerpt in result.excerpts:
            for turn_id in excerpt.turn_ids:
                if turn_id in seen:
                    duplicates += 1
                seen.add(turn_id)
            if (
                excerpt.persona_id != scope.persona_id
                or excerpt.counterpart_id != scope.counterpart_id
                or excerpt.branch_id != scope.branch_id
            ):  # noqa: SIM102 - kept flat so the failure names the field
                wrong_scope += 1
            if result.total_tokens > budget.max_total_tokens + BUDGET_TOLERANCE_TOKENS:
                budget_violations += 1
            for message in excerpt.messages:
                expected = raw_store.get(message.turn_id)
                if expected is None:
                    expected = raw_store.get(f"room:{message.turn_id}")
                candidates = expected or ("", "")
                if message.raw_text not in candidates:
                    boundary_violations.append(f"{item['kind']}:{message.turn_id}")
        item["result"] = {
            "excerpts": len(result.excerpts),
            "tokens": result.total_tokens,
            "anchors": result.anchors_considered,
            "availability": (
                result.excerpts[0].source_availability.value
                if result.excerpts
                else (
                    result.unavailable[0].availability.value
                    if result.unavailable
                    else "none"
                )
            ),
            "unavailable_detail": [
                ref.detail for ref in result.unavailable
            ],
        }

    # Human-verifiable sample: excerpt text next to the row it claims to come from.
    sample: list[dict[str, Any]] = []
    for item in objects:
        if len(sample) >= args.sample:
            break
        result = app.raw_recall.recall(
            scope=scope, query=item["query"], memory_refs=[item["ref"]], budget=budget
        )
        if not result.excerpts:
            continue
        excerpt = result.excerpts[0]
        sample.append(
            {
                "scope": (
                    f"{item['scope'].persona_id}/{item['scope'].counterpart_id}"
                    f"/{item['scope'].branch_id}"
                ),
                "memory": f"{item['kind']}:{item['id']}",
                "query": item["query"],
                "selection_reason": excerpt.selection_reason.value,
                "availability": excerpt.source_availability.value,
                "turn_ids": excerpt.turn_ids,
                "messages": [
                    {
                        "speaker": message.speaker,
                        "turn_id": message.turn_id,
                        "excerpt_text": message.raw_text,
                        "store_row": list(raw_store.get(message.turn_id, ("", ""))),
                    }
                    for message in excerpt.messages
                ],
            }
        )

    tested = len(objects)
    report = {
        "phase": "7 (real-data snapshot)",
        "generated_at": started.isoformat(),
        "source_database": str(source),
        "snapshot": str(copied),
        "scratch": str(scratch),
        "isolation": "sqlite backup copy; the production database is never opened for write",
        "episode_backfill": {
            key: value for key, value in episode_report.items() if key != "actions"
        },
        "hierarchy_backfill": {
            key: value for key, value in hierarchy_report.items() if key != "actions"
        },
        "memory_objects_tested": tested,
        "by_kind": {
            kind: sum(1 for item in objects if item["kind"] == kind)
            for kind in sorted({item["kind"] for item in objects})
        },
        "resolvable": counts["resolvable"],
        "partial": counts["partial"],
        "unavailable": counts["unavailable"],
        "deleted": counts["deleted"],
        "duplicate_excerpts": duplicates,
        "wrong_scope": wrong_scope,
        "budget_violations": budget_violations,
        "message_boundary_violations": len(boundary_violations),
        "boundary_violation_turns": boundary_violations[:20],
        "manual_sample": sample,
        "duration_seconds": round((datetime.now(UTC) - started).total_seconds(), 2),
        "status": "PASS"
        if tested >= args.min_objects
        and not wrong_scope
        and not budget_violations
        and not boundary_violations
        and counts["resolvable"] >= min(10, tested)
        else "PARTIAL",
    }

    app.close()

    out_dir = Path("docs/reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "phase7-raw-recall-snapshot.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out_dir / "phase7-raw-recall-snapshot.md"
    md_path.write_text(_markdown(report), encoding="utf-8")
    report["written"] = [str(json_path), str(md_path)]
    return report


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Phase 7 · Raw Recall 真实数据快照基准",
        "",
        f"- 源库：`{report['source_database']}`（只读 backup 副本，未写真实库）",
        f"- 快照：`{report['snapshot']}`",
        f"- memory objects tested：**{report['memory_objects_tested']}**"
        f"（{json.dumps(report['by_kind'], ensure_ascii=False)}）",
        f"- resolvable / partial / unavailable / deleted："
        f"**{report['resolvable']} / {report['partial']} / "
        f"{report['unavailable']} / {report['deleted']}**",
        f"- duplicate excerpts：{report['duplicate_excerpts']}　"
        f"wrong scope：{report['wrong_scope']}",
        f"- budget violations：{report['budget_violations']}　"
        f"message boundary violations：{report['message_boundary_violations']}",
        f"- 状态：**{report['status']}**　耗时 {report['duration_seconds']}s",
        "",
        "## 人工抽查样本（excerpt 原文 vs 原始存储行）",
        "",
        "每一行都可以对照 `session_turns` / `room_transcripts` 的原行验证：",
        "excerpt 文本必须与存储行逐字一致，没有任何改写。",
        "",
    ]
    for index, item in enumerate(report["manual_sample"], start=1):
        lines.append(f"### 样本 {index} · {item['memory']}")
        lines.append("")
        lines.append(f"- scope：`{item.get('scope', '')}`")
        lines.append(f"- query：`{item['query']}`")
        lines.append(f"- selection_reason：`{item['selection_reason']}`　"
                     f"availability：`{item['availability']}`")
        lines.append("")
        for message in item["messages"]:
            lines.append(f"- **[{message['speaker']}]** `{message['turn_id']}`")
            lines.append(f"  - excerpt：{message['excerpt_text']}")
            store = message["store_row"]
            shown = [part for part in store if part]
            lines.append(f"  - store：{shown if shown else '(行已不存在)'}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(PRODUCTION_DB))
    parser.add_argument("--scratch", default=None)
    parser.add_argument("--objects", type=int, default=24)
    parser.add_argument("--min-objects", type=int, default=20)
    parser.add_argument("--episode-batch", type=int, default=200)
    parser.add_argument("--hierarchy-batch", type=int, default=200)
    parser.add_argument("--sample", type=int, default=12)
    parser.add_argument("--include-lineage", action="store_true")
    parser.add_argument("--lineage-count", type=int, default=4)
    args = parser.parse_args()
    report = run(args)
    summary = {
        key: report[key]
        for key in (
            "memory_objects_tested",
            "resolvable",
            "partial",
            "unavailable",
            "deleted",
            "duplicate_excerpts",
            "wrong_scope",
            "budget_violations",
            "message_boundary_violations",
            "status",
        )
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
