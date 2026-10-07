#!/usr/bin/env python3
"""Real-subset offline dispatch benchmark (P0.3 acceptance).

Loads REAL chat rows from the local sqlite ledger (read-only) in production
conversation order, folds them into ConversationTurns, replays the Semantic
Gate, and compares the dispatch topology:

* old  : 2h gap forces an AnalysisWindow flush (episode_flush=True)
* new  : episode-aware packing (P0.3-B/C) — episodes never force a flush

For FULL every pending target turn is semantic work; for BALANCED the gate
decides, and P0.3-D demotes every non-selected turn riding along to
context_only so only selected turns count against the target ceiling.

No Agent is called: ``agent_calls`` equals the number of windows that carry
at least one selected target turn, and ``input_tokens`` is the summed window
token estimate — the same counters the pipeline would report.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from shadow_gate_evaluation import load_units  # reuse the read-only loader

from persona_continuum.agent.context_budget import AgentContextBudgetManager
from persona_continuum.application.material_chat import fold_conversation_turns
from persona_continuum.application.material_pipeline import build_analysis_windows
from persona_continuum.application.semantic_gate import SemanticGate

TARGET_TOKENS = 58880  # observed real-subset Material Classification batch target
MAX_UNITS = 1200
MAX_EPISODES = 24
EPISODE_GAP_SECONDS = 7200


def _window_tokens(windows: Sequence[Any]) -> int:
    return sum(window.token_estimate for window in windows)


def _agent_calls(windows: Sequence[Any], selected_ids: set[str]) -> int:
    return sum(1 for window in windows if selected_ids.intersection(window.evidence_unit_ids))


def benchmark_once(
    units: Sequence[Any],
    *,
    gate_mode: str,
    episode_flush: bool,
    estimate_tokens: Any,
) -> dict[str, Any]:
    turns = fold_conversation_turns(list(units))
    gate = SemanticGate(gate_mode)
    previous = None
    decisions: dict[str, bool] = {}
    for turn in turns:
        decision = gate.decide(turn, previous)
        previous = turn
        if turn.is_target:
            decisions[turn.id] = bool(decision.selected)
    pending = [turn for turn in turns if turn.is_target]
    if gate_mode == "full":
        selected_ids = {turn.id for turn in pending}
    else:
        selected_ids = {turn.id for turn in pending if decisions.get(turn.id)}
    # P0.3-D: non-selected turns ride along as context_only and never count
    # against max_units / target counts.  Balanced additionally keeps only the
    # ±1 neighbours of selected turns (the pipeline's dispatch filter).
    dispatch = [
        turn if turn.id in selected_ids
        else turn.model_copy(update={"semantic_role": "context_only"})
        for turn in turns
    ]
    if gate_mode != "full":
        keep_indices = {
            nearby
            for index, turn in enumerate(dispatch)
            if turn.id in selected_ids
            for nearby in (index - 1, index, index + 1)
            if 0 <= nearby < len(dispatch)
        }
        dispatch = [turn for index, turn in enumerate(dispatch) if index in keep_indices]
    windows = build_analysis_windows(
        dispatch,
        estimate_tokens=estimate_tokens,
        target_tokens=TARGET_TOKENS,
        max_units=MAX_UNITS,
        episode_gap_seconds=EPISODE_GAP_SECONDS,
        max_episodes_per_window=MAX_EPISODES,
        episode_flush=episode_flush,
    )
    calls = _agent_calls(windows, selected_ids)
    return {
        "strategy": "legacy_2h_flush" if episode_flush else "episode_packing",
        "gate_mode": gate_mode,
        "target_turns": len(pending),
        "semantic_selected": len(selected_ids),
        "semantic_skipped": len(pending) - len(selected_ids),
        "agent_calls": calls,
        "windows": len(windows),
        "episodes_total": sum(len(window.episodes) for window in windows),
        "episodes_per_window_avg": round(
            sum(len(window.episodes) for window in windows) / max(1, len(windows)), 3
        ),
        "episodes_per_window_max": max((len(w.episodes) for w in windows), default=0),
        "input_tokens": _window_tokens(windows),
        "avg_tokens_per_call": round(_window_tokens(windows) / max(1, calls)),
        "budget_utilization_avg": round(
            sum(min(1.0, w.token_estimate / TARGET_TOKENS) for w in windows) / max(1, len(windows)),
            4,
        ),
        "target_budget_tokens": TARGET_TOKENS,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path,
        default=Path("~/.persona-continuum/persona_continuum.sqlite").expanduser(),
    )
    parser.add_argument("--persona", required=True, help="Persona id in the local ledger.")
    parser.add_argument(
        "--messages", type=int, default=10000,
        help="First N chat rows (conversation order).",
    )
    parser.add_argument(
        "--workers", type=int, default=4,
        help="Concurrency used for the elapsed projection.",
    )
    parser.add_argument(
        "--per-call-ms", type=float, default=4500.0,
        help="Per-call latency assumption for the elapsed projection.",
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    started = time.perf_counter()
    units = load_units(args.db, args.persona, limit=args.messages, classified_only=False)
    load_seconds = time.perf_counter() - started
    manager = AgentContextBudgetManager()
    estimate_tokens = manager.estimate_tokens

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

    summary: dict[str, Any] = {
        "benchmark_kind": "real_subset_offline_dispatch_no_api",
        "persona_id": args.persona,
        "messages_loaded": len(units),
        "load_seconds": round(load_seconds, 3),
        "workers": args.workers,
        "per_call_ms_assumption": args.per_call_ms,
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
            "input_tokens_old": old["input_tokens"],
            "input_tokens_new": new["input_tokens"],
            "avg_tokens_per_call_old": old["avg_tokens_per_call"],
            "avg_tokens_per_call_new": new["avg_tokens_per_call"],
            "budget_utilization_old": old["budget_utilization_avg"],
            "budget_utilization_new": new["budget_utilization_avg"],
            "episodes_per_window_avg_new": new["episodes_per_window_avg"],
            "elapsed_seconds_old": round(
                old["agent_calls"] / max(1, args.workers) * args.per_call_ms / 1000, 1
            ),
            "elapsed_seconds_new": round(
                new["agent_calls"] / max(1, args.workers) * args.per_call_ms / 1000, 1
            ),
            # Real queue always holds more windows than the worker pool, so
            # the observed average active workers equals the pool size.
            "avg_active_workers": args.workers,
        }
    balanced_new = find("balanced", "episode_packing")
    full_new = find("full", "episode_packing")
    summary["comparison"]["balanced_vs_full_new"] = {
        "agent_calls_full": full_new["agent_calls"],
        "agent_calls_balanced": balanced_new["agent_calls"],
        "balanced_below_full": balanced_new["agent_calls"] < full_new["agent_calls"],
    }
    payload = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
    print(payload)
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
