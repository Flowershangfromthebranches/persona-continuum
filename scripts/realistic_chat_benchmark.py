#!/usr/bin/env python3
"""Realistic synthetic chat benchmark (P0.3-E) — old 2h flush vs new packing.

The synthetic ``timestamp += 1 minute`` benchmarks could never expose the
dominant real-world cost: chats happen a few times a day with hours of
silence between bursts, across years.  This generator produces that shape:

* several chat sessions per day (episode gaps 2h..24h);
* multi-year span;
* some contiguous bursts (same-speaker runs below the 90s turn gap);
* long quiet stretches (days) between active periods.

Both strategies run the identical turn fold + Semantic Gate; only the
window dispatch differs:

* old: a >2h gap forces an AnalysisWindow flush (``episode_flush=True``);
* new: episode-aware packing fills the token budget (P0.3-B/C).

No Agent is called; agent_calls/windows/episodes/tokens/utilization are the
pipeline-equivalent counters, elapsed is a worker-pool projection plus the
measured planning time.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from real_subset_benchmark import (
    benchmark_once,
)

from persona_continuum.agent.context_budget import AgentContextBudgetManager
from persona_continuum.application.material_chat import fold_conversation_turns
from persona_continuum.application.semantic_gate import SemanticGate

BURST_LINES = (
    "我今天加了一整天班，方案改了四版，领导还是不满意。",
    "你觉得我要不要辞职？我其实已经想了很久了。",
    "周末我们去看电影吧，最近那部评分很高。",
    "我妈今天又问我相亲的事，我真的没办法跟她解释。",
    "我决定报个夜校班，把证书考下来，以后换工作也有底气。",
    "工资到账了，这个月存了八千，比上个月多一点。",
    "今天跑步五公里，坚持第三十天了。",
    "跟同事闹了点矛盾，他说我不配合，其实我已经让步很多了。",
    "我最近睡不着，压力有点大，凌晨三点还醒着。",
    "谢谢你今天听我说这么多，感觉好多了。",
)

CONTEXT_LINES = (
    "嗯嗯，然后呢？",
    "哈哈，可以啊",
    "好的，我知道了",
    "在忙，晚点回你",
)


def generate_realistic_chat(
    *,
    sessions: int,
    seed: int = 20260909,
) -> list[dict[str, Any]]:
    """Chat export lines with realistic temporal structure."""

    rng = random.Random(seed)
    start = datetime(2021, 3, 14, 21, 30, tzinfo=UTC)
    rows: list[dict[str, Any]] = []
    stamp = start
    for session_index in range(sessions):
        # Session starts: evenings/afternoons, gaps 2h..24h between sessions
        # (occasionally days of silence) so every session is its own episode.
        if session_index:
            gap_hours = rng.choice([2.5, 4, 6, 9, 14, 22, 26, 40, 60])
            stamp = stamp + timedelta(hours=gap_hours)
        session_lines = rng.randint(8, 40)
        turn_time = stamp
        for _line_index in range(session_lines):
            is_target = rng.random() < 0.6
            text = rng.choice(BURST_LINES if is_target else CONTEXT_LINES)
            if rng.random() < 0.15:
                text = f"{text}（第{rng.randint(1, 9)}次）"
            rows.append(
                {
                    "speaker": "对方" if is_target else "我",
                    "text": text,
                    "timestamp": turn_time.isoformat(),
                }
            )
            # Bursts: consecutive lines land within 90s ~30% of the time,
            # otherwise a short pause keeps them separate turns.
            turn_time += timedelta(seconds=rng.choice([15, 40, 95, 240, 900]))
    return rows


def to_units(rows: list[dict[str, Any]]) -> list[Any]:
    from persona_continuum.application.material_intelligence import EvidenceUnit

    units: list[Any] = []
    for index, row in enumerate(rows):
        text = row["text"]
        units.append(
            EvidenceUnit(
                id=f"evu_{index:06d}",
                persona_id="benchmark_persona",
                source_id="src_realistic",
                source_locator={"segment_index": index},
                speaker=row["speaker"],
                speaker_role="target_persona" if row["speaker"] == "对方" else "exporter",
                timestamp=row["timestamp"],
                text=text,
                normalized_text=text.casefold(),
                source_kind="chat_import",
                confidence=0.5,
            )
        )
    return units


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=600, help="Chat sessions (2h..60h apart).")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--per-call-ms", type=float, default=4500.0)
    parser.add_argument("--seed", type=int, default=20260909)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    started = time.perf_counter()
    rows = generate_realistic_chat(sessions=args.sessions, seed=args.seed)
    units = to_units(rows)
    manager = AgentContextBudgetManager()
    estimate_tokens = manager.estimate_tokens
    generate_seconds = time.perf_counter() - started

    fold_started = time.perf_counter()
    turns = fold_conversation_turns(list(units))
    fold_seconds = time.perf_counter() - fold_started
    gate = SemanticGate("balanced")
    previous = None
    for turn in turns:
        gate.decide(turn, previous)
        previous = turn

    results = []
    for gate_mode in ("full", "balanced"):
        for episode_flush in (True, False):
            run_started = time.perf_counter()
            result = benchmark_once(
                units,
                gate_mode=gate_mode,
                episode_flush=episode_flush,
                estimate_tokens=estimate_tokens,
            )
            result["build_seconds"] = round(time.perf_counter() - run_started, 3)
            results.append(result)

    def find(gate_mode: str, strategy: str) -> dict[str, Any]:
        return next(r for r in results if r["gate_mode"] == gate_mode and r["strategy"] == strategy)

    started_at = datetime.fromisoformat(rows[0]["timestamp"])
    ended_at = datetime.fromisoformat(rows[-1]["timestamp"])
    span_days = (ended_at - started_at).total_seconds() / 86400
    summary = {
        "benchmark_kind": "realistic_synthetic_dispatch_no_api",
        "sessions": args.sessions,
        "messages": len(rows),
        "conversation_turns": len(turns),
        "span_days": round(span_days, 1),
        "workers": args.workers,
        "generate_seconds": round(generate_seconds, 3),
        "fold_seconds": round(fold_seconds, 3),
        "runs": results,
        "comparison": {},
    }
    for gate_mode in ("full", "balanced"):
        old = find(gate_mode, "legacy_2h_flush")
        new = find(gate_mode, "episode_packing")
        summary["comparison"][gate_mode] = {
            "agent_calls_old": old["agent_calls"],
            "agent_calls_new": new["agent_calls"],
            "call_reduction": round(1 - new["agent_calls"] / max(1, old["agent_calls"]), 4),
            "windows_old": old["windows"],
            "windows_new": new["windows"],
            "episodes_total_old": old["episodes_total"],
            "episodes_total_new": new["episodes_total"],
            "episodes_per_window_avg_new": new["episodes_per_window_avg"],
            "tokens_per_window_avg_old": round(old["input_tokens"] / max(1, old["windows"])),
            "tokens_per_window_avg_new": round(new["input_tokens"] / max(1, new["windows"])),
            "budget_utilization_old": old["budget_utilization_avg"],
            "budget_utilization_new": new["budget_utilization_avg"],
            "elapsed_seconds_old": round(
                old["agent_calls"] / max(1, args.workers) * args.per_call_ms / 1000, 1
            ),
            "elapsed_seconds_new": round(
                new["agent_calls"] / max(1, args.workers) * args.per_call_ms / 1000, 1
            ),
        }
    payload = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
    print(payload)
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
