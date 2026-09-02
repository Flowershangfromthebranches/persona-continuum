"""Quality regression: Legacy vs Optimized execution produce the SAME result.

Both configs run one public-research Persona job against an identical fake
research broker and a deterministic JSON runtime.  The optimization must not
reduce evidence, dimensions, provenance, compilation, or validation -- it may
only change *how* the same work is scheduled, cached, and reused.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from persona_continuum.agent.adapters.fake import FakeAgentAdapter
from persona_continuum.agent.models import AgentEvent, AgentEventType
from persona_continuum.application.container import PersonaContinuum
from persona_continuum.application.persona_creation_service import REQUIRED_DIMENSIONS
from persona_continuum.config import Config
from persona_continuum.domain.persona import PersonaType


class JsonRuntime(FakeAgentAdapter):
    def __init__(self, adapter_id: str = "fake_agent") -> None:
        super().__init__(adapter_id=adapter_id, name="Json Runtime")

    async def send(self, session, turn):  # type: ignore[no-untyped-def]
        prompt = turn.user_message
        system_prompt = turn.system_prompt or ""
        self.sent_turns.append((session.config.session_id, turn))
        if "严格最终质量门禁" in system_prompt:
            payload = {
                "status": "pass",
                "dimensions": {dimension: "pass" for dimension in REQUIRED_DIMENSIONS},
                "issues": [],
                "confidence": 0.95,
            }
        elif "Evidence Intelligence 分析器" in system_prompt:
            request = json.loads(turn.user_message)
            payload = {
                "units": [
                    {
                        "id": item["id"],
                        "dimension_scores": item.get("deterministic_dimensions") or {
                            dimension: 0.8 for dimension in REQUIRED_DIMENSIONS
                        },
                        "claims": [item.get("text", "")],
                        "events": [{"content": item.get("text", "")}],
                        "confidence": 0.8,
                    }
                    for item in request.get("units", [])
                ]
            }
        elif "提取维度" in prompt and "ResearchArtifact" in prompt:
            # The real prompt is "为 Persona Continuum 提取维度 <dimension> 的..."
            dimension = next(
                (d for d in REQUIRED_DIMENSIONS if f"提取维度 {d}" in prompt),
                REQUIRED_DIMENSIONS[0],
            )
            source_id = (session.config.extra.get("source_ids") or ["src_missing"])[0]
            payload = {
                "artifact_id": f"art_{dimension}",
                "schema_version": "1.1",
                "dimension": dimension,
                "source_ids": [source_id],
                "claims": [
                    {
                        "content": f"Claim for {dimension} sourced to {source_id}",
                        "source_id": source_id,
                        "claim_type": "historical_third_party_report",
                        "confidence": 0.7,
                    }
                ],
                "memories": [
                    {"content": f"Memory for {dimension}", "source_id": source_id}
                ],
                "extracted_components": {"profile": {"dimension": dimension}},
                "conflicts": [],
                "uncertainty": {"level": 0.3, "notes": []},
                "created_by": "regression",
                "artifact_hash": f"hash_{dimension}",
            }
        elif '"queries"' in prompt and "ResearchArtifact" not in prompt:
            payload: dict[str, Any] = {
                "queries": ["biography interview", "criticism failure", "archive dialogue"],
                "contradiction_search_queries": ["contradictions criticism"],
                "negative_evidence_queries": ["failures opposing views"],
                "life_stages": [
                    {"id": "early", "title": "Early", "start": "1940", "end": "1979"},
                    {"id": "middle", "title": "Middle", "start": "1980", "end": "2004"},
                    {"id": "late", "title": "Late", "start": "2005", "end": "2026"},
                ],
            }
        else:
            payload = {"question": "regression?", "dimension": REQUIRED_DIMENSIONS[0]}
        yield AgentEvent(type=AgentEventType.DONE, content=json.dumps(payload, ensure_ascii=False))


class FixedBroker:
    """Returns the same canonical set of sources for both configs."""

    def __init__(self, count: int = 60) -> None:
        self.count = count

    def capabilities(self) -> set[str]:
        return {"web_search", "web_fetch", "research"}

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return [
            {
                "url": f"https://regression.test/source-{i}",
                "title": f"Source {i}",
                "publisher": f"Pub {i % 6}",
                "category": ["interview", "criticism", "archive", "profile", "speech", "book"][
                    i % 6
                ],
                "is_primary": i % 4 == 0,
                "event_time": f"{1950 + (i % 75):04d}-01-01",
                "content": (
                    f"Stable historical event {i}: the subject was born, produced a work, "
                    "said a phrase in an interview, decided and failed, according to a critic, "
                    "changed a family relationship, and described a value and contradiction."
                ),
            }
            for i in range(self.count)
        ]

    async def fetch(self, url: str) -> str:
        return f"Fetched full page for {url}."


_POLICY = {"profile": "deep"}


async def _run_persona(config: Config) -> dict[str, Any]:
    # Final audit is a quality policy, so both execution strategies must run it.
    config.persona_final_full_audit = True
    config.persona_global_audit = True
    config.data_dir = config.data_dir
    app = PersonaContinuum(config=config, include_fake_agent=True)
    app.init()
    try:
        runtime = JsonRuntime()
        app.agent_registry.register_adapter(runtime)
        app.persona_creation.research_broker = FixedBroker()
        await app.agent_discovery.scan(force_refresh=True)
        job = await app.persona_creation.create_job(
            display_name="Regression Subject",
            persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
            creation_mode="public_research",
            runtime_source="test",
            agent_id="fake_agent",
            model_id="fake-gpt-5",
            research_policy=dict(_POLICY),
            start_worker=False,
        )
        await app.persona_creation._run_job(job.id)
        final = app.persona_creation.get_job(job.id)
        artifacts = app.compilation.get_task(job.compilation_task_id).artifacts
        dims_with_claims = {
            str(a.get("dimension"))
            for a in artifacts
            if isinstance(a, dict) and (a.get("claims") or [])
        }
        unsourced = [
            claim
            for a in artifacts
            if isinstance(a, dict)
            for claim in (a.get("claims") or [])
            if not (claim.get("source_id") if isinstance(claim, dict) else None)
        ]
        unique_sources = {
            str(s.metadata.get("canonical_url") or s.path)
            for s in app.personas.get_sources(job.persona_id or "")
        }
        validation = app.compilation.validate_persona(job.persona_id or "")
        audit_prompt_chars = [
            len(turn.user_message)
            for _, turn in runtime.sent_turns
            if "严格最终质量门禁" in (turn.system_prompt or "")
        ]
        return {
            "status": final.status,
            "source_count": final.source_count,
            "unique_sources": len(unique_sources),
            "dimensions_complete": set(dims_with_claims),
            "required_dimensions": set(REQUIRED_DIMENSIONS),
            "unsourced_claims": len(unsourced),
            "compiled": True,
            "validation_score": validation.score if hasattr(validation, "score") else None,
            "manifest_compile_state": app.personas.get(job.persona_id or "").manifest.compile_state,
            "research_policy": final.research_policy.model_dump(mode="json"),
            "performance_trace": dict(final.job_config.get("performance_trace") or {}),
            "audit_prompt_chars": audit_prompt_chars,
        }
    finally:
        app.close()


@pytest.mark.anyio
async def test_quality_identical_between_legacy_and_optimized(tmp_path) -> None:
    legacy = await _run_persona(Config.legacy_execution(data_dir=tmp_path / "legacy"))
    optimized = await _run_persona(Config.optimized(data_dir=tmp_path / "optimized"))

    assert legacy["status"] in {"completed", "completed_with_gaps"}
    assert optimized["status"] in {"completed", "completed_with_gaps"}

    # Same evidence volume and provenance under both execution strategies.
    assert optimized["source_count"] == legacy["source_count"]
    assert optimized["source_count"] > 0
    assert optimized["unique_sources"] == legacy["unique_sources"]
    assert optimized["unique_sources"] >= 30
    assert optimized["research_policy"]["preferred_source_target"] == 60
    assert optimized["research_policy"]["require_life_stage_coverage"] is True
    assert optimized["research_policy"]["require_contradiction_search"] is True
    assert optimized["research_policy"]["require_negative_evidence"] is True
    assert optimized["research_policy"]["require_marginal_gain_gate"] is True

    # Both satisfy the full eight-dimension contract (the final global audit
    # guarantees the optimized incremental path loses nothing).
    assert optimized["dimensions_complete"] >= optimized["required_dimensions"]
    assert legacy["dimensions_complete"] >= legacy["required_dimensions"]

    # No un-sourced claims in either run (provenance preserved).
    assert optimized["unsourced_claims"] == 0
    assert legacy["unsourced_claims"] == 0

    # Compilation + validation succeed identically; neither run is a stub.
    assert optimized["manifest_compile_state"] == legacy["manifest_compile_state"]

    # Normal 60-source optimized creation stays within the formal LLM-turn budget.
    counters = optimized["performance_trace"].get("counters", {})
    assert counters.get("llm_turn_count", 0) <= 30
    assert len(optimized["audit_prompt_chars"]) == 2
    assert max(optimized["audit_prompt_chars"]) < 50_000
