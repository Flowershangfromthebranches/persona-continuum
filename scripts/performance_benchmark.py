"""Before/after performance benchmark (Legacy Execution vs Optimized Execution).

Both runs use an IDENTICAL Quality Policy (same dimensions, same reasoning,
same model, same source requirements, same prompt semantics).  Only the
execution strategy differs, selected through the two ``Config`` presets.  It
uses a deterministic fake runtime + fake research broker so it runs offline and
reproducibly.  The process-wide tracer counters are reset between runs so each
column shows only that run's work.

Run:  uv run python scripts/performance_benchmark.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from persona_continuum.agent.adapters.fake import FakeAgentAdapter  # noqa: E402
from persona_continuum.agent.models import AgentEvent, AgentEventType  # noqa: E402
from persona_continuum.application.container import PersonaContinuum  # noqa: E402
from persona_continuum.config import Config  # noqa: E402
from persona_continuum.domain.persona import PersonaType  # noqa: E402
from persona_continuum.performance import default_tracer  # noqa: E402
from persona_continuum.performance.capability_cache import (  # noqa: E402
    default_model_capability_cache,
)
from persona_continuum.performance.runtime_pool import default_runtime_pool  # noqa: E402

_DIMENSIONS = [
    "identity_and_timeline",
    "works_and_views",
    "interviews_and_dialogue",
    "expression_dna",
    "decisions_and_behavior",
    "third_party_views",
    "affect_relationship_defense",
    "values_desires_contradictions",
]

# A shared pool of web pages that overlapping public figures all cite.  Three
# personas researched back-to-back re-visit these pages: exactly the Parallel
# World "Steve Jobs / Tim Cook / Jony Ive" initialization case.
PAGE_POOL = 20
SIMULATED_MODEL_TURN_SECONDS = 0.2


class JsonResearchAgent(FakeAgentAdapter):
    """Deterministic structured-output runtime for the benchmark."""

    async def send(self, session, turn):  # type: ignore[no-untyped-def]
        prompt = turn.user_message
        system_prompt = turn.system_prompt or ""
        self.sent_turns.append((session.config.session_id, turn))
        if "严格最终质量门禁" in system_prompt:
            payload = {
                "status": "pass",
                "dimensions": {dimension: "pass" for dimension in _DIMENSIONS},
                "issues": [],
                "confidence": 0.95,
            }
        elif "Evidence Intelligence 分析器" in system_prompt:
            request = json.loads(prompt)
            payload = {
                "units": [
                    {
                        "id": item["id"],
                        "dimension_scores": item.get("deterministic_dimensions")
                        or {dimension: 0.8 for dimension in _DIMENSIONS},
                        "claims": [item.get("text", "")],
                        "events": [{"content": item.get("text", "")}],
                        "confidence": 0.8,
                    }
                    for item in request.get("units", [])
                ]
            }
        elif '"queries"' in prompt or (
            "life_stages" in prompt and "ResearchArtifact" not in prompt
        ):
            payload: dict[str, Any] = {"queries": [f"biography {i}" for i in range(3)]}
        elif "ResearchArtifact" in prompt:
            dimension = next(
                (d for d in _DIMENSIONS if f"提取维度 {d}" in prompt),
                _DIMENSIONS[0],
            )
            source_id = (session.config.extra.get("source_ids") or ["src_test"])[0]
            payload = {
                "artifact_id": f"art_{dimension}",
                "schema_version": "1.1",
                "dimension": dimension,
                "source_ids": [source_id],
                "claims": [
                    {
                        "content": f"Sourced claim for {dimension}",
                        "source_id": source_id,
                        "claim_type": "historical_inference",
                        "confidence": 0.7,
                    }
                ],
                "memories": [],
                "extracted_components": {"identity_profile": {"summary": dimension}},
                "conflicts": [],
                "uncertainty": {"level": 0.3, "notes": []},
                "created_by": "bench",
                "artifact_hash": f"hash_{dimension}_{source_id}",
            }
        elif "key_facts" in prompt or "长期摘要" in prompt:
            payload = {"key_facts": ["bench"], "open_topics": ["bench"]}
        else:
            payload = {"question": "bench?", "dimension": _DIMENSIONS[4]}
        # Keep the benchmark offline while assigning every model turn the
        # same deterministic latency.  Without this, wall time mostly measures
        # SQLite/JSON overhead and cannot represent the production bottleneck.
        await asyncio.sleep(SIMULATED_MODEL_TURN_SECONDS)
        yield AgentEvent(
            type=AgentEventType.DONE, content=json.dumps(payload, ensure_ascii=False)
        )


class OverlappingBroker:
    """Returns partially-overlapping pages across queries and personas."""

    def __init__(self, pages: int = PAGE_POOL) -> None:
        self.pages = pages
        self.search_calls = 0
        self.fetch_calls = 0
        self._fetch_cache: set[str] = set()

    def capabilities(self) -> set[str]:
        return {"web_search", "web_fetch", "research"}

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        self.search_calls += 1
        # Deterministic overlap keyed by query so different queries (and
        # different personas) return many of the SAME canonical URLs.
        seed = sum(ord(ch) for ch in query) % self.pages
        out = []
        for index in range(min(self.pages, limit + 6)):
            page = (seed + index) % self.pages
            out.append(
                {
                    "url": f"https://overlap.test/page-{page}",
                    "title": f"Shared source {page}",
                    "publisher": f"Pub {page % 5}",
                    "category": ["interview", "criticism", "archive", "profile"][page % 4],
                    "content": "",
                }
            )
        return out

    async def fetch(self, url: str) -> str:
        # A real network fetch.  The source cache is what prevents this from
        # being called again for a page another persona already downloaded.
        self.fetch_calls += 1
        await asyncio.sleep(0)
        self._fetch_cache.add(url)
        return f"Full page evidence body for {url} describing decisions and style."


# Same Quality Policy in both runs; only execution strategy changes.
_SHARED_POLICY = {
    "profile": "deep",
    "min_unique_sources": 8,
    "preferred_source_target": 12,
    "soft_max_sources": 40,
    "hard_max_sources": 40,
    "min_source_categories": 2,
    "min_sources_per_dimension": 1,
    "require_life_stage_coverage": False,
    "require_contradiction_search": False,
    "require_negative_evidence": False,
    "require_marginal_gain_gate": False,
    "adaptive_target": False,
    "quality_floor": 0.0,
    "max_research_rounds": 2,
}

_BENCH_PERSONAS = ["Ada Lovelace", "Charles Babbage", "Alan Turing"]


class PersistentFakeAgent(FakeAgentAdapter):
    """A fake runtime that advertises persistent threads (Codex/ACP-like).

    Only used to exercise the Room transcript cursor deterministically
    offline; it produces the same keyword-driven replies as the plain fake.
    """

    def __init__(self) -> None:
        super().__init__(adapter_id="persist_fake", name="Persistent Fake")

    def supports_persistent_conversation(self, session: Any) -> bool:
        return True


def _fresh_continuum(config: Config) -> PersonaContinuum:
    continuum = PersonaContinuum(config=config, include_fake_agent=True)
    continuum.init()
    return continuum


async def _drive_personas(continuum: PersonaContinuum, broker: OverlappingBroker) -> int:
    """Create three overlapping public personas (Parallel World init pattern)."""

    continuum.persona_creation.research_broker = broker
    await continuum.agent_discovery.scan(force_refresh=True)
    total_sources = 0
    for name in _BENCH_PERSONAS:
        job = await continuum.persona_creation.create_job(
            display_name=name,
            persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
            creation_mode="public_research",
            runtime_source="test",
            agent_id="fake_agent",
            model_id="fake-gpt-5",
            research_policy=dict(_SHARED_POLICY),
            start_worker=False,
        )
        await continuum.persona_creation._run_job(job.id)
        final = continuum.persona_creation.get_job(job.id)
        assert final.status in {"completed", "completed_with_gaps"}, (
            f"benchmark Persona must reach compilation, got {final.status}: "
            f"{final.failure_json or final.error}"
        )
        total_sources += final.source_count
    assert total_sources > 0, "benchmark research must ingest evidence"
    return total_sources


async def _drive_room(continuum: PersonaContinuum, turns: int) -> int:
    from persona_continuum.room.models import ParticipantSlot

    ids = []
    for tag in ("A", "B"):
        persona = continuum.personas.create(
            display_name=f"Room {tag}",
            aliases=[],
            persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
            run_mode="counterfactual_continuation",
        )
        ids.append(persona.id)
    # Pin to the persistent fake runtime so no real CLI is contacted and the
    # transcript-cursor path is the one under measurement.
    room = continuum.orchestrator.create_room(
        title="bench",
        topic="设计哲学与产品取舍",
        participants=[
            ParticipantSlot(
                participant_id="a", persona_id=ids[0], display_name="Room A",
                runtime_selection="persist_fake", model_selection="fake-gpt-5",
                allow_agent_tools=False,
            ),
            ParticipantSlot(
                participant_id="b", persona_id=ids[1], display_name="Room B",
                runtime_selection="persist_fake", model_selection="fake-gpt-5",
                allow_agent_tools=False,
            ),
        ],
    )
    await continuum.orchestrator.start_room(room.id)
    sent_chars = 0
    for _ in range(turns):
        async for event in continuum.orchestrator.step_turn(
            room.id, user_message="继续讨论设计与工程之间的取舍"
        ):
            if event.get("event") == "agent_message_delta":
                sent_chars += len(event.get("delta", ""))
    await continuum.orchestrator.stop_room(room.id)
    return sent_chars


async def _drive_world(continuum: PersonaContinuum, ticks: int) -> int:
    for tag in range(2):
        continuum.personas.create_from_manifest(
            {
                "id": f"bench_actor_{tag}",
                "display_name": f"Bench Actor {tag}",
                "persona_type": "historical",
                "run_mode": "continuation",
            }
        )
    world, branch, _state = continuum.worlds.create_world(
        description="Bench actors negotiate a technology roadmap.",
        start_date="2012-03-01",
        default_actor_runtime={
            "agent_id": "fake_agent",
            "model_id": "fake-gpt-5",
            "reasoning_effort": "high",
            "runtime_source": "test",
        },
        initial_actors=["Bench Actor 0", "Bench Actor 1"],
    )
    for _ in range(ticks):
        await continuum.worlds.step_branch(world.id, branch.id)
    return len(continuum.worlds.list_actors(world.id, branch.id))


async def _run_config(label: str, config: Config) -> dict[str, Any]:
    default_tracer().reset()
    default_model_capability_cache().clear()
    await default_runtime_pool().shutdown()
    broker = OverlappingBroker()
    with tempfile.TemporaryDirectory() as tmp:
        config.data_dir = Path(tmp)
        continuum = _fresh_continuum(config)
        try:
            continuum.agent_registry.register_adapter(JsonResearchAgent())
            continuum.agent_registry.register_adapter(PersistentFakeAgent())
            await continuum.agent_discovery.scan(force_refresh=True)
            default_tracer().reset()
            started = time.perf_counter()
            sources = await _drive_personas(continuum, broker)
            persona_ms = (time.perf_counter() - started) * 1000.0
            room_started = time.perf_counter()
            await _drive_room(continuum, turns=8)
            room_ms = (time.perf_counter() - room_started) * 1000.0
            world_started = time.perf_counter()
            await _drive_world(continuum, ticks=4)
            world_ms = (time.perf_counter() - world_started) * 1000.0
            wall_ms = (time.perf_counter() - started) * 1000.0
            counters = default_tracer().global_snapshot()
            room_turns = [
                item
                for item in default_tracer().recent(limit=200)
                if item.get("kind") == "room_turn"
            ]
            prepare_values = sorted(
                t.get("prepare_turn_ms", 0.0) for t in room_turns if "prepare_turn_ms" in t
            )
            delta_turns = sum(1 for t in room_turns if t.get("counters", {}).get("delta"))
            summary = {
                "label": label,
                "wall_ms": round(wall_ms),
                "persona_total_ms": round(persona_ms),
                "room_total_ms": round(room_ms),
                "world_total_ms": round(world_ms),
                "research_sources": sources,
                "broker_fetch_calls": broker.fetch_calls,
                "model_call_count": counters.get("model_call_count", 0),
                "model_list_count": counters.get("model_list_count", 0),
                "agent_session_open_count": counters.get("agent_session_open_count", 0),
                "agent_probe_count": counters.get("agent_probe_count", 0),
                "world_actor_resolve_count": counters.get(
                    "world_actor_adapter_resolve_count", 0
                ),
                "room_turns_recorded": len(room_turns),
                "room_prepare_ms_p50": round(prepare_values[len(prepare_values) // 2])
                if prepare_values
                else 0,
                "room_delta_turns": delta_turns,
            }
        finally:
            continuum.close()
    return summary


def _print_table(optimized: dict[str, Any], legacy: dict[str, Any]) -> None:
    keys = [k for k in optimized if k != "label"]
    width = max(len(k) for k in keys) + 2
    print("\n=== Persona Continuum performance benchmark (identical Quality Policy) ===")
    print(f"{'metric':<{width}}{'optimized':>16}{'legacy':>16}")
    print("-" * (width + 32))
    for key in keys:
        print(f"{key:<{width}}{str(optimized[key]):>16}{str(legacy[key]):>16}")
    print("\nNotes:")
    print(
        f"- each fake model turn has a fixed {SIMULATED_MODEL_TURN_SECONDS:.1f}s "
        "latency; compare topology, not real-provider throughput."
    )
    print("- physical_process_spawn_count is 0 for both runs here because the")
    print("  benchmark uses the in-process fake runtime; with a real Codex/ACP")
    print("  CLI the Optimized column collapses spawn+model/list churn toward 0.")
    print("- broker_fetch_calls: legacy re-downloads shared pages per persona")
    print(f"  ({legacy['broker_fetch_calls']}), optimized shares the source cache")
    print(f"  across personas ({optimized['broker_fetch_calls']}).")


async def main() -> None:
    optimized_config = Config.optimized()
    legacy_config = Config.legacy_execution()
    # Final quality gates are policy, not an execution optimization.  Keep
    # them identical in both columns even though the compatibility preset
    # normally models the pre-refactor implementation where they did not exist.
    legacy_config.persona_final_full_audit = True
    legacy_config.persona_global_audit = True
    optimized = await _run_config("Optimized Execution", optimized_config)
    legacy = await _run_config("Legacy Execution", legacy_config)
    _print_table(optimized, legacy)


if __name__ == "__main__":
    asyncio.run(main())
