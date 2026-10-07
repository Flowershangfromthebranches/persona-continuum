#!/usr/bin/env python3
"""Persisted production-path context scale benchmark.

SQLite stream -> ConversationTurn -> episode packing -> dynamic planner ->
producer queue -> classification windows.

Uses mock execution profiles and a no-op analyzer.  Never sends private chat
or calls an external model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from persona_continuum.agent.prompt_transport import capability_for_mode
from persona_continuum.application.container import PersonaContinuum
from persona_continuum.application.material_intelligence import EvidenceUnit
from persona_continuum.application.material_pipeline import (
    MaterialPipelineMetrics,
    ResolvedExecutionProfile,
)
from persona_continuum.config import Config
from persona_continuum.domain.persona import PersonaType

WINDOWS = (65_536, 131_072, 200_000, 500_000, 1_000_000)
BASE = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)


def _profile(window: int) -> ResolvedExecutionProfile:
    return ResolvedExecutionProfile(
        adapter_id="fake",
        model_id="synthetic",
        context_window=window,
        native_context_window=window,
        runtime_effective_context=window,
        remaining_context_tokens=window,
        remaining_context_verified=True,
        remaining_context_source="runtime_reported",
        context_verified=True,
        context_capability_source="runtime_reported",
        context_window_source="runtime_reported",
        usable_context_budget=max(1, int(window * 0.9)),
        phase_working_target=max(1, int(window * 0.8)),
        parallel_safe=True,
        prompt_transport=capability_for_mode("stdin"),
        persistent_session=False,
    )


def _units(persona_id: str, source_id: str, count: int, text: str) -> list[EvidenceUnit]:
    return [
        EvidenceUnit(
            id=f"scale_{index:05d}",
            persona_id=persona_id,
            source_id=source_id,
            source_locator={"segment_index": index},
            speaker="对方",
            speaker_role="target_persona",
            timestamp=(BASE + timedelta(minutes=index * 3)).isoformat(),
            text=text,
            normalized_text=text,
            source_kind="chat_import",
            metadata={"semantic_status": "target_pending"},
        )
        for index in range(count)
    ]


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return int(ordered[index])


def _reset_pending(service, persona_id: str) -> None:
    for batch in service._iter_units_batched(persona_id):
        changed = []
        for unit in batch:
            metadata = dict(unit.metadata)
            metadata["semantic_status"] = "target_pending"
            metadata.pop("classification_contract", None)
            metadata.pop("classification_status", None)
            metadata.pop("intelligence_cache_key", None)
            changed.append(
                unit.model_copy(
                    update={
                        "metadata": metadata,
                        "extraction_method": "deterministic_plus_agent",
                    }
                )
            )
        service._persist_units(changed)


async def run(count: int) -> list[dict[str, object]]:
    long_text = "这是一条合成目标人格发言，用来填满分析窗口。" * 12
    with tempfile.TemporaryDirectory(prefix="pc-context-scale-") as directory:
        app = PersonaContinuum(Config(data_dir=Path(directory)))
        app.init()
        try:
            persona = app.personas.create(
                display_name="Scale path",
                aliases=[],
                persona_type=PersonaType.PRIVATE_LIVING_PERSON,
                run_mode="digital_continuation",
            )
            source = app.personas.add_source_text(
                persona.id,
                title="synthetic",
                source_type="txt",
                canonical_url=None,
                publisher="user",
                author="user",
                published_at=None,
                accessed_at=None,
                content="synthetic",
                metadata={},
            )
            service = app.material_intelligence
            service.semantic_gate_mode = "full"
            service.turn_gap_seconds = 0
            service.analysis_window_max_units = None
            service.analysis_window_max_episodes = None
            service._persist_units(_units(persona.id, source.id, count, long_text))
            estimate = service.context_budget_manager.estimate_tokens
            rows: list[dict[str, object]] = []
            for window in WINDOWS:
                prompt_tokens: list[int] = []

                async def analyzer(phase, payload, tokens=prompt_tokens):
                    tokens.append(estimate(payload))
                    return {"units": []}

                metrics = MaterialPipelineMetrics()
                started = datetime.now(UTC)
                await service._classify_persisted_with_agent(
                    persona.id,
                    analyzer,
                    execution_profile=_profile(window),
                    metrics=metrics,
                )
                elapsed = (datetime.now(UTC) - started).total_seconds()
                rows.append(
                    {
                        "runtime_context": window,
                        "target_turns": count,
                        "windows": metrics.analysis_windows,
                        "initial_windows": (
                            metrics.initial_analysis_windows or metrics.analysis_windows
                        ),
                        "agent_calls": metrics.agent_calls,
                        "rebatched_windows": metrics.rebatched_windows,
                        "packing_accuracy": metrics.packing_accuracy,
                        "rebatched_window_ratio": metrics.rebatched_window_ratio,
                        "rebatch_reasons": dict(metrics.rebatch_reasons),
                        "avg_prompt_tokens": round(
                            sum(prompt_tokens) / max(1, len(prompt_tokens))
                        )
                        if prompt_tokens
                        else None,
                        "p50_prompt_tokens": _percentile(prompt_tokens, 0.50),
                        "p95_prompt_tokens": _percentile(prompt_tokens, 0.95),
                        "p50_utilization": metrics.prompt_budget_utilization_p50,
                        "p95_utilization": metrics.prompt_budget_utilization_p95,
                        "budget_utilization": metrics.prompt_budget_utilization_avg,
                        "estimation_error_p50": metrics.estimation_error_p50,
                        "estimation_error_p95": metrics.estimation_error_p95,
                        "turns_per_window": round(count / max(1, metrics.analysis_windows), 2),
                        "episodes_per_window": metrics.episodes_per_window_avg,
                        "elapsed": elapsed,
                        "cap_trigger_reason": {
                            "token_budget": metrics.context_cap_hit_count,
                            "unit_cap": metrics.turn_cap_hit_count,
                            "episode_cap": metrics.episode_cap_hit_count,
                            "transport_cap": metrics.transport_cap_hit_count,
                        },
                        "max_batch_target_tokens": metrics.max_batch_target_tokens,
                    }
                )
                _reset_pending(service, persona.id)
            return rows
        finally:
            app.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=2_000)
    args = parser.parse_args()
    rows = asyncio.run(run(max(1, int(args.count))))
    payload = {
        "path": "sqlite_stream -> ConversationTurn -> episode packing -> "
        "dynamic planner -> producer queue -> classification windows",
        "rows": rows,
    }
    out = Path("docs/reports/implementation/context-window-scale-benchmark.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
