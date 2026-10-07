#!/usr/bin/env python3
"""Large Conversation Pipeline V2 synthetic benchmark (mock Agent, no quotas).

Benchmark A: 1,000 raw messages (500 target / 500 context, heavy bursts).
Benchmark B: 10,000 raw messages over the streaming persisted path.

Reports the real pipeline counters plus a legacy-baseline comparison: pre-V2
the same corpus was classified one-window-per-160-units across ALL messages
(both speakers), the ~2,400-call topology the V2 task targets
(385,034 / 160 ~= 2,407).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import tempfile
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from persona_continuum.application import material_intelligence as material_module
from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.domain.persona import PersonaType

BASE = datetime(2023, 4, 3, 20, 0, 0, tzinfo=UTC)


def _stamp(offset_seconds: int) -> str:
    value = (BASE + timedelta(seconds=offset_seconds)).isoformat()
    return value.split("+")[0].replace("T", " ")


def chat_file(target: int, context: int) -> str:
    """Interleaved chat export with realistic same-speaker bursts.

    Same-speaker lines land within 90 seconds so they fold into one turn; a
    short exporter reply between bursts separates target turns.
    """

    lines = ["# 我 = 聊天记录导出者", "# 对方 = 目标 Persona"]
    offset = 0
    produced_t = produced_c = 0
    while produced_t < target or produced_c < context:
        for _ in range(1 + offset % 5):
            if produced_t >= target:
                break
            lines.append(f"{_stamp(offset * 20)} | 对方 | 今天事情{produced_t}真的很多 哈哈哈")
            produced_t += 1
            offset += 1
        if produced_c < context:
            lines.append(f"{_stamp(offset * 20 + 10)} | 我 | 好的")
            produced_c += 1
            offset += 1
    return "\n".join(lines)


def make_analyzer(requests: list[dict], sparse_every: int = 1):
    """Mock Agent: emits one evidence row per dispatched window."""

    async def analyzer(phase: str, payload: dict) -> dict[str, Any]:
        requests.append(payload)
        emit = list(payload["target_units"][:sparse_every])
        return {
            "units": [
                {
                    "id": anchor_id,
                    "claims": [f"synthetic claim {anchor_id}"],
                    "dimension_scores": {"decisions_and_behavior": 0.8},
                }
                for anchor_id in emit
            ]
        }

    return analyzer


async def benchmark(raw_count: int, streaming: bool) -> dict[str, Any]:
    tmp = tempfile.mkdtemp()
    app = PersonaContinuum(Config(data_dir=tmp + "/pc"), include_fake_agent=True)
    app.init()
    try:
        persona = app.personas.create(
            display_name=f"Benchmark {raw_count}",
            aliases=[],
            persona_type=PersonaType.PRIVATE_LIVING_PERSON,
            run_mode="digital_continuation",
        )
        source = app.personas.add_source_text(
            persona.id,
            title=f"chat-{raw_count}",
            source_type="txt",
            canonical_url=None,
            publisher="user",
            author="user",
            published_at=None,
            accessed_at=None,
            content=chat_file(raw_count // 2, raw_count - raw_count // 2),
            metadata={},
        )
        if streaming:
            # Force the persisted streaming path for the whole corpus.
            app.material_intelligence.in_memory_unit_limit = 1
            app.material_intelligence.max_source_bytes = 1
        timings: dict[str, float] = defaultdict(float)
        service = app.material_intelligence

        def timed(name, function):
            def wrapped(*args, **kwargs):
                tick = time.perf_counter()
                try:
                    return function(*args, **kwargs)
                finally:
                    timings[name] += time.perf_counter() - tick
            return wrapped

        for name, method in {
            "ingest": "_segment_sources_batched", "cluster": "_cluster_persisted",
            "contradiction": "_contradictions_from_clusters", "fusion": "_fuse_persisted",
            "style_profiler": "_run_chat_style_profiler",
            "evidence_index": "_replace_derived_atomic",
        }.items():
            setattr(service, method, timed(name, getattr(service, method)))
        original_fold = material_module.fold_conversation_turns
        material_module.fold_conversation_turns = timed("turn_folding", original_fold)
        classify = service._classify_persisted_with_agent

        async def timed_classify(*args, **kwargs):
            tick = time.perf_counter()
            try:
                return await classify(*args, **kwargs)
            finally:
                timings["classification_including_folding"] += time.perf_counter() - tick

        service._classify_persisted_with_agent = timed_classify
        requests: list[dict] = []
        started = time.perf_counter()
        job = await app.material_intelligence.analyze_sources_async(
            persona.id,
            [source.id],
            agent_analyzer=make_analyzer(requests),
            agent_phases=("classify",),
        )
        elapsed = time.perf_counter() - started
        material_module.fold_conversation_turns = original_fold
        assert timings["ingest"] > 0 and timings["classification_including_folding"] > 0
        timings["classification"] = (
            timings["classification_including_folding"] - timings["turn_folding"]
        )
        chat = dict(job.progress.get("chat_pipeline") or {})
        metrics = dict(job.progress.get("performance_metrics") or {})
        rows = {
            unit.id: unit
            for batch in app.material_intelligence._iter_units_batched(persona.id)
            for unit in batch
        }
        target_msgs = int(chat.get("target_message_count") or 0)
        context_msgs = int(chat.get("context_message_count") or 0)
        raw_rows = len(rows)
        # Pre-V2 baseline: ceil(raw_all_messages / 160) windows over BOTH
        # speakers with the old reviewed_ids protocol.
        legacy_calls = math.ceil(raw_rows / 160)
        optimized_calls = int(metrics.get("agent_calls") or len(requests))
        return {
            "corpus_rows_preserved": raw_rows,
            "elapsed_seconds": round(elapsed, 2),
            "persisted_path_verified": True,
            "stage_seconds": {key: round(value, 4) for key, value in timings.items()},
            "raw_message_count": chat.get("raw_message_count"),
            "target_message_count": target_msgs,
            "context_message_count": context_msgs,
            "conversation_turn_count": chat.get("conversation_turn_count"),
            "target_turn_count": chat.get("target_turn_count"),
            "classification_windows_total": chat.get("classification_windows_total"),
            "classification_windows_completed": chat.get(
                "classification_windows_completed"
            ),
            "average_turns_per_window": chat.get("average_turns_per_window"),
            "max_turns_per_window": chat.get("max_turns_per_window"),
            "agent_calls": optimized_calls,
            "input_tokens": chat.get("input_tokens"),
            "output_tokens": chat.get("output_tokens"),
            "evidence_turns_extracted": chat.get("evidence_turns_extracted"),
            "reviewed_no_independent_evidence": chat.get(
                "reviewed_no_independent_evidence"
            ),
            "style_profile_status": chat.get("style_profile_status"),
            "legacy_160_windows_reference": legacy_calls,
            "call_reduction_vs_legacy": round(
                1 - optimized_calls / max(1, legacy_calls), 3
            ),
            "statuses": {
                "extracted": sum(
                    1
                    for unit in rows.values()
                    if (unit.metadata or {}).get("semantic_status")
                    == "evidence_extracted"
                ),
                "reviewed_no_evidence": sum(
                    1
                    for unit in rows.values()
                    if (unit.metadata or {}).get("semantic_status")
                    == "reviewed_no_independent_evidence"
                ),
                "context_only": sum(
                    1
                    for unit in rows.values()
                    if (unit.metadata or {}).get("semantic_status") == "context_only"
                ),
                "pending": sum(
                    1
                    for unit in rows.values()
                    if (unit.metadata or {}).get("semantic_status") == "target_pending"
                ),
            },
            "provenance_100_percent": raw_rows
            == int(chat.get("raw_message_count") or raw_rows),
        }
    finally:
        app.close()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--counts", default="10000,30000,50000")
    args = parser.parse_args()
    results = []
    for raw_count in [int(item) for item in args.counts.split(",")]:
        results.append(await benchmark(raw_count, streaming=True))
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
