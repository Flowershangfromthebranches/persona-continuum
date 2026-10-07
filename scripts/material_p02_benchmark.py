#!/usr/bin/env python3
"""Persisted P0.2 benchmark. Reports contain aggregate counts, never private text.

Default runs synthetic 10k/30k/50k with a latency-controlled mock analyzer.
--real-db reads an existing ledger in read-only mode, copies at most 10k rows
into isolated temporary SQLite, and compares full/balanced on that same subset.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from persona_continuum.agent.prompt_transport import capability_for_mode
from persona_continuum.application.chat_style_profiler import ChatStyleProfiler
from persona_continuum.application.container import PersonaContinuum
from persona_continuum.application.material_intelligence import (
    EVIDENCE_ORDER_POSITION,
    EvidenceUnit,
    MaterialIntelligenceService,
)
from persona_continuum.application.material_pipeline import (
    MaterialPipelineMetrics,
    ResolvedExecutionProfile,
)
from persona_continuum.config import Config
from persona_continuum.domain.persona import PersonaType


def synthetic(count):
    rows = []
    for i in range(count):
        target = i % 2 == 0
        text = ("我决定以后坚持自己的原则" if i % 10 == 0 else "嗯") if target else "今天怎么样"
        rows.append(
            EvidenceUnit(
                id=f"bench_{i:06d}",
                persona_id="fixture",
                source_id="fixture",
                source_locator={"segment_index": i},
                text=text,
                normalized_text=text,
                source_kind="chat_import",
                speaker="target" if target else "exporter",
                speaker_role="target_persona" if target else "exporter",
                timestamp=(datetime(2023, 1, 1, tzinfo=UTC) + timedelta(minutes=i)).isoformat(),
                metadata={"semantic_status": "target_pending" if target else "context_only"},
            )
        )
    return rows


def read_subset(path):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        persona = db.execute(
            "SELECT persona_id FROM persona_evidence_units GROUP BY persona_id "
            "ORDER BY count(*) DESC LIMIT 1"
        ).fetchone()[0]
        rows = db.execute(
            "SELECT * FROM persona_evidence_units WHERE persona_id=? "
            f"ORDER BY source_id, {EVIDENCE_ORDER_POSITION}, id LIMIT 10000",
            (persona,),
        ).fetchall()
        return [MaterialIntelligenceService._unit_from_row(row) for row in rows]


async def run(rows, mode, transport="stdin", analyzer_override=None, legacy_barrier=False):
    with tempfile.TemporaryDirectory(prefix="pc-p02-") as directory:
        app = PersonaContinuum(Config(data_dir=Path(directory), material_semantic_gate_mode=mode))
        app.init()
        try:
            persona = app.personas.create(
                display_name="Isolated benchmark",
                aliases=[],
                persona_type=PersonaType.PRIVATE_LIVING_PERSON,
                run_mode="digital_continuation",
            )
            source = app.personas.add_source_text(
                persona.id,
                title="benchmark",
                source_type="txt",
                canonical_url=None,
                publisher="user",
                author="user",
                published_at=None,
                accessed_at=None,
                content="Isolated benchmark source",
                metadata={},
            )
            units = []
            for row in rows:
                metadata = {
                    key: value
                    for key, value in row.metadata.items()
                    if key
                    not in {
                        "evidence_intelligence",
                        "classification_status",
                        "classification_contract",
                        "intelligence_cache_key",
                        "intelligence_supporting_ids",
                        "intelligence_member_of",
                        "semantic_gate_policy",
                        "semantic_gate_mode",
                        "semantic_gate_reason",
                    }
                }
                metadata["semantic_status"] = (
                    "context_only" if row.speaker_role == "exporter" else "target_pending"
                )
                units.append(
                    row.model_copy(
                        update={
                            "persona_id": persona.id,
                            "source_id": source.id,
                            "metadata": metadata,
                            "extraction_method": "deterministic",
                            "dimension_scores": {},
                            "dimension_candidates": [],
                        }
                    )
                )
            service = app.material_intelligence
            service.in_memory_unit_limit = 1
            service._persist_units(units)
            calls = []
            active = peak = 0
            busy_seconds = 0.0

            async def analyzer(phase, payload):
                calls.append(len(payload["target_units"]))
                nonlocal active, peak, busy_seconds
                tick = time.perf_counter()
                active += 1
                peak = max(peak, active)
                try:
                    if analyzer_override:
                        return await analyzer_override(phase, payload)
                    await asyncio.sleep(0.1)
                    return {"units": []}
                finally:
                    active -= 1
                    busy_seconds += time.perf_counter() - tick

            metrics = MaterialPipelineMetrics()
            started = time.perf_counter()
            profile = ResolvedExecutionProfile(
                parallel_safe=True, context_window=131072, usable_context_budget=100000,
                preferred_working_context=64000, prompt_transport=capability_for_mode(transport),
            )
            if legacy_barrier:
                # Reconstruct the old 1000-raw-row dispatch barrier on the
                # alternating synthetic corpus (no turn crosses a boundary).
                metrics.raw_message_count = len(units)
                for batch in service._iter_units_batched(persona.id):
                    await service._classify_with_agent(
                        batch, analyzer, execution_profile=profile, metrics=metrics,
                    )
            else:
                await service._classify_persisted_with_agent(
                    persona.id, analyzer, execution_profile=profile, metrics=metrics,
                )
            elapsed = time.perf_counter() - started
            stored = [u for batch in service._iter_units_batched(persona.id) for u in batch]
            original = [(u.id, u.text, u.source_locator) for u in units]
            preserved = [(u.id, u.text, u.source_locator) for u in stored] == original
            style = ChatStyleProfiler().profile(persona.id, stored)
            episodes = service._build_episodes_persisted(persona.id)
            episode_ids = {eid for episode in episodes for eid in episode.evidence_unit_ids}
            report = metrics.model_dump(mode="json")
            report.update(
                mode=mode,
                scheduling=(
                    "reconstructed_1000_raw_barrier"
                    if legacy_barrier
                    else "bounded_window_queue"
                ),
                average_active_agent_calls=busy_seconds / elapsed,
                peak_active_agent_calls=peak,
                transport=transport,
                persisted_path_verified=True,
                analyzer="live_existing_runtime"
                if analyzer_override
                else "mock_100ms_no_semantic_output",
                raw_messages=len(rows),
                elapsed_seconds=round(elapsed, 3),
                target_turns_per_hour=round(metrics.target_turns_reviewed * 3600 / elapsed, 2),
                average_target_turns_per_call=sum(calls) / max(1, len(calls)),
                max_target_turns_per_call=max(calls, default=0),
                selection_ratio=metrics.semantic_selected / max(1, metrics.target_turn_count),
                raw_provenance_preserved=preserved,
                style_target_messages=style.corpus_size if style else 0,
                episode_raw_messages=len(episode_ids),
                pending=sum(
                    not service._classification_done(u, ResolvedExecutionProfile()) for u in stored
                ),
            )
            assert preserved and len(episode_ids) == len(rows) and report["pending"] == 0
            evidence = [
                u.model_dump(mode="json") for u in stored if u.metadata.get("evidence_intelligence")
            ]
            style_stats = style.statistics if style else {}
            report["style_statistics_hash"] = hashlib.sha256(
                json.dumps(style_stats, sort_keys=True).encode()
            ).hexdigest()
            return report, evidence, style_stats
        finally:
            app.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-db", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {"synthetic": [], "real_subset": [], "quality_status": "not_live_compiler_validated"}
    for count in (10000, 30000, 50000):
        rows = synthetic(count)
        for mode in ("full", "balanced"):
            report, _, _ = await run(rows, mode)
            result["synthetic"].append(report)
            print(
                json.dumps(
                    {
                        "rows": count,
                        "mode": mode,
                        "calls": report["agent_calls"],
                        "average_workers": report["average_active_classification_workers"],
                    }
                ),
                flush=True,
            )
    # Qwen before/after uses exactly the same synthetic evidence and model budget.
    result["transport_before"], _, _ = await run(synthetic(10000), "full", "unknown")
    result["transport_after"] = result["synthetic"][0]
    if args.real_db:
        rows = read_subset(args.real_db)
        for mode in ("full", "balanced"):
            report, _, _ = await run(rows, mode)
            result["real_subset"].append(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
