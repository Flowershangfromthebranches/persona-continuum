"""Benchmark the resumable Persona Creation / Room / World upgrade.

Produces before/after style measurements for the areas this upgrade touched.
Everything runs against the FakeAgent / deterministic builders so the numbers
measure the CODE paths (batching, checkpoint reuse, persistence size, commit
counts, lazy loads) rather than model latency.

Usage: uv run python scripts/benchmark_resumable_upgrade.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from persona_continuum.agent.context_budget import AgentContextBudgetManager
from persona_continuum.agent.structured_output import StructuredOutputEngine
from persona_continuum.application.container import PersonaContinuum
from persona_continuum.compiler.schemas import ResearchArtifact
from persona_continuum.config import Config
from persona_continuum.performance.runtime_pool import AgentRuntimePool


def section(title: str) -> None:
    print(f"\n## {title}")


def benchmark_context_batching() -> None:
    section("Context window batching (dimension extraction)")
    manager = AgentContextBudgetManager()
    items = [
        {
            "evidence_id": f"u{i}",
            "source_ids": ["s1"],
            "evidence_type": "source",
            "verbatim_samples": [],
            "intelligence": {},
            "content": "x" * 6000,
        }
        for i in range(120)
    ]

    def item_text(item: dict) -> str:
        return str(item["content"])

    schema = ResearchArtifact.model_json_schema()
    print(f"evidence items: {len(items)} (~1500 tokens each)")
    print("window | batches | model calls for full pass")
    for window in (32_768, 131_072, 262_144, 1_048_576):
        batches = list(
            manager.iter_batches(
                items,
                item_text=item_text,
                max_items=48,
                phase="dimension_extraction",
                model={"context_window": window, "model_id": "m"},
                system_prompt="s",
                base_text="b",
                expected_output=schema,
            )
        )
        legacy_default = 32_768
        verdict = (
            "  <- legacy fallback (wrong for big windows)"
            if window == legacy_default
            else ""
        )
        print(f"{window:>7} | {len(batches):>7} | {len(batches)}{verdict}")


def benchmark_batch_checkpoint_reuse() -> None:
    section("Batch checkpoint reuse (model calls avoided)")
    engine = StructuredOutputEngine()
    damaged_samples = [
        '{"claims": [{"content": "a",}],}',
        '```json\n{"claims": [{"content": "b"}]}\n```',
        '{"claims": [{"content": "c"',
        'prose prefix {"claims": [{"content": "d"}]} trailing prose',
    ]
    schema = {"type": "object"}
    local_ok = 0
    started = time.perf_counter()
    for sample in damaged_samples:
        repaired = engine.local_repair(sample)
        try:
            engine.parse_and_validate(repaired, schema, phase="bench")
            local_ok += 1
        except Exception:
            pass
    elapsed_ms = (time.perf_counter() - started) * 1000
    print(
        f"deterministic local repair resolved {local_ok}/{len(damaged_samples)} "
        f"damaged outputs in {elapsed_ms:.2f} ms total "
        "(1 model call saved per case vs the old LLM repair path)"
    )


def benchmark_room_persistence() -> None:
    section("Room state persistence vs transcript length")
    from persona_continuum.room.models import RoomSessionState

    config = Config(data_dir=Path(tempfile.mkdtemp()) / "bench")
    continuum = PersonaContinuum(config, include_fake_agent=True)
    continuum.init()
    try:
        orchestrator = continuum.orchestrator
        state = RoomSessionState(
            id="bench_room",
            title="bench",
            topic="bench",
            participants=[],
        )
        row_sizes: dict[int, float] = {}
        timings: dict[int, float] = {}
        for turn in list(range(1, 11)) + [100, 500, 1000]:
            while len(state.transcript) < turn:
                index = len(state.transcript)
                state.transcript.append(
                    {
                        "turn_id": f"turn_{index}",
                        "participant_id": "host",
                        "persona_id": "room_host",
                        "speaker_name": "Host",
                        "content": "z" * 400,
                        "created_at": "2026-01-01T00:00:00+00:00",
                    }
                )
            state.turn_index = turn
            started = time.perf_counter()
            orchestrator._persist_room_state(state)
            timings[turn] = (time.perf_counter() - started) * 1000
            row = continuum.database.conn.execute(
                "SELECT state_json FROM rooms WHERE id = ?",
                (state.id,),
            ).fetchone()
            row_sizes[turn] = len(row["state_json"]) / 1024.0
        print("turn | persisted state (KB) | ms")
        for turn in (10, 100, 500, 1000):
            print(f"{turn:>4} | {row_sizes[turn]:>20.1f} | {timings[turn]:.1f}")
        print(
            f"growth 10 -> 1000 turns: "
            f"{row_sizes[1000] / max(row_sizes[10], 0.1):.2f}x "
            "(legacy behaviour serialized the FULL transcript every turn: ~100x)"
        )
    finally:
        continuum.close()


def benchmark_runtime_pool_distribution() -> None:
    section("RuntimePool logical-session placement (4 processes, 12 sessions)")

    class FakeTransport:
        class _Proc:
            returncode = None

        @property
        def process(self) -> object:
            return self._Proc()

        async def close(self, *, force: bool = False) -> None:
            return None

    async def scenario() -> list[int]:
        pool = AgentRuntimePool(max_processes_per_key=4)
        counter = {"spawns": 0}

        async def make():
            counter["spawns"] += 1
            return FakeTransport()

        async def noop() -> None:
            return None

        warmups = [await pool.acquire("codex:bench", make) for _ in range(4)]
        for lease in warmups:
            await lease.managed.ensure_initialized(noop)
            await lease.release()
        managed = []
        for _ in range(12):
            lease = await pool.acquire("codex:bench", make)
            await pool.retain_logical(lease.managed)
            managed.append(lease.managed)
            await lease.release()
        distribution = sorted(item.logical_sessions for item in set(managed))
        await pool.shutdown()
        return distribution

    distribution = asyncio.run(scenario())
    print(f"logical sessions per runtime: {distribution}")


def benchmark_world_tick_commits() -> None:
    section("World tick transaction commits")
    config = Config(data_dir=Path(tempfile.mkdtemp()) / "bench")
    continuum = PersonaContinuum(config, include_fake_agent=True)
    continuum.init()
    try:
        db = continuum.database
        # Minimal direct persistence setup through the transaction helper,
        # using a throwaway table (no FK requirements) to count commits.
        db.conn.execute("CREATE TABLE IF NOT EXISTS bench_tick (id TEXT, payload TEXT)")
        db.conn.execute("DELETE FROM bench_tick")
        db.conn.commit()
        statements: list[str] = []
        db.conn.set_trace_callback(statements.append)
        try:
            with db.transaction():
                for i in range(10):
                    db.conn.execute(
                        "INSERT INTO bench_tick VALUES (?, ?)",
                        (f"row_{i}", "x" * 100),
                    )
        finally:
            db.conn.set_trace_callback(None)
        begin_count = statements.count("BEGIN IMMEDIATE")
        commit_count = statements.count("COMMIT")
        print(
            f"transaction block: BEGIN={begin_count}, COMMIT={commit_count} "
            "(legacy: every save_* helper committed individually, ~10 commits/tick)"
        )
    finally:
        continuum.close()


def main() -> None:
    print("# Persona Continuum — resumable-upgrade benchmark\n")
    benchmark_context_batching()
    benchmark_batch_checkpoint_reuse()
    benchmark_room_persistence()
    benchmark_runtime_pool_distribution()
    benchmark_world_tick_commits()
    print("\nAll benchmarks completed.")


if __name__ == "__main__":
    main()
