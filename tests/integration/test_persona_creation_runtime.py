from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from persona_continuum.agent.adapters.fake import FakeAgentAdapter
from persona_continuum.agent.response_collector import AgentTimeoutError
from persona_continuum.application.persona_creation_service import (
    PersonaCreationJob,
    ResearchCapabilityError,
)
from persona_continuum.domain.persona import PersonaType


class FakeResearchBroker:
    def __init__(self, count: int = 24) -> None:
        self.count = count
        self.queries: list[str] = []

    def capabilities(self) -> set[str]:
        return {"web_search", "web_fetch", "research"}

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        self.queries.append(query)
        return [
            {
                "url": f"https://example.test/source-{index}",
                "title": f"Source {index}",
                "publisher": f"Publisher {index % 6}",
                "category": ["interview", "speech", "book", "profile", "criticism", "archive"][
                    index % 6
                ],
                "content": f"Evidence for source {index} about {query}.",
            }
            for index in range(min(self.count, limit))
        ]

    async def fetch(self, url: str) -> str:
        return f"Fetched evidence for {url}."


class JsonResearchAgent(FakeAgentAdapter):
    """Deterministic structured-output adapter used only by the written tests."""

    async def send(self, session, turn):  # type: ignore[no-untyped-def]
        prompt = turn.user_message
        if '"required_dimensions"' in prompt:
            payload = {"queries": ["long-term biography interview criticism failure"]}
        elif "ResearchArtifact" in prompt:
            dimension = next(
                (
                    item
                    for item in (
                        "identity_and_timeline",
                        "works_and_views",
                        "interviews_and_dialogue",
                        "expression_dna",
                        "decisions_and_behavior",
                        "third_party_views",
                        "affect_relationship_defense",
                        "values_desires_contradictions",
                    )
                    if item in prompt
                ),
                "identity_and_timeline",
            )
            # The orchestrator test patches this source list after ingestion.
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
                "created_by": "json-test-agent",
                "artifact_hash": f"hash_{dimension}",
            }
        else:
            payload = {
                "question": "请描述压力下的一个真实决定",
                "dimension": "decisions_and_behavior",
            }
        text = json.dumps(payload, ensure_ascii=False)
        from persona_continuum.agent.models import AgentEvent, AgentEventType

        yield AgentEvent(type=AgentEventType.DONE, content=text)


@pytest.mark.anyio
async def test_persona_agent_turn_timeout_covers_entire_call(app, monkeypatch) -> None:
    async def timed_out_call(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AgentTimeoutError(
            "agent_turn_timed_out_after_5s",
            phase="timeout",
            diagnostics={"protocol": "fake_protocol"},
        )

    monkeypatch.setattr(app.persona_creation, "_run_agent_with_heartbeat", timed_out_call)
    job = PersonaCreationJob(
        id="pcjob_timeout",
        display_name="Timeout Persona",
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        creation_mode="fictional",
        runtime_source="test",
        agent_id="fake_agent",
        model_id="fake-gpt-5",
        job_config={"turn_timeout_seconds": 5.0},
    )

    with pytest.raises(AgentTimeoutError, match="agent_turn_timed_out_after_5s") as caught:
        await app.persona_creation._agent_text(
            job,
            user_message="hello",
            system_prompt="system",
            participant_id="timeout",
        )
    assert caught.value.code == "AGENT_TURN_TIMEOUT"
    assert caught.value.phase == "timeout"


@pytest.mark.anyio
async def test_persona_agent_turn_persists_binding_activity_and_attempt_count(app) -> None:
    adapter = FakeAgentAdapter(
        adapter_id="progress_fake_agent",
        chunk_delay_sec=0.005,
    )
    app.agent_registry.register_adapter(adapter)
    await app.agent_discovery.scan(force_refresh=True)
    app.persona_creation.WORKER_HEARTBEAT_INTERVAL_SECONDS = 0.001
    job = PersonaCreationJob(
        id="pcjob_progress_contract",
        display_name="Progress Contract Persona",
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        creation_mode="fictional",
        runtime_source="test",
        agent_id=adapter.adapter_id,
        model_id="fake-gpt-5",
    )

    result = await app.persona_creation._run_agent_with_heartbeat(
        job,
        user_message="Return a short response.",
        system_prompt="You are a test Agent.",
        participant_id="progress_test",
        phase="dimension_extraction",
    )

    assert result.text
    persisted = app.persona_creation.get_job(job.id)
    assert persisted.progress.runtime_binding_snapshot["protocol"] == "fake_protocol"
    assert persisted.progress.activity_tracker
    assert persisted.progress.process_alive is None or isinstance(
        persisted.progress.process_alive, bool
    )
    assert persisted.agent_call_count == 1
    assert persisted.progress.agent_call_count == 1
    assert persisted.progress.agent_call_attempt_count == 1
    assert persisted.progress.agent_call_completed_count == 1
    assert persisted.progress.agent_call_failed_count == 0
    assert "agent_call_in_flight" not in persisted.job_config


@pytest.mark.anyio
async def test_persona_creation_uses_existing_compiler(app) -> None:
    app.agent_registry.register_adapter(JsonResearchAgent())
    app.persona_creation.research_broker = FakeResearchBroker()
    await app.agent_discovery.scan(force_refresh=True)

    job = await app.persona_creation.create_job(
        display_name="Ada Lovelace",
        persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
        creation_mode="public_research",
        runtime_source="test",
        agent_id="fake_agent",
        model_id="fake-gpt-5",
    )
    assert job.compilation_task_id
    assert job.persona_id
    assert app.compilation.get_task(job.compilation_task_id).persona_id == job.persona_id


@pytest.mark.anyio
async def test_public_persona_requires_research_capability(app) -> None:
    await app.agent_discovery.scan(force_refresh=True)
    with pytest.raises(ResearchCapabilityError):
        await app.persona_creation.create_job(
            display_name="Public Person",
            persona_type=PersonaType.PUBLIC_LIVING_PERSON,
            creation_mode="public_research",
            runtime_source="test",
            agent_id="fake_agent",
            model_id="fake-gpt-5",
        )


@pytest.mark.anyio
async def test_public_deep_research_coverage_gate(app) -> None:
    app.persona_creation.research_broker = FakeResearchBroker(count=1)
    await app.agent_discovery.scan(force_refresh=True)
    job = await app.persona_creation.create_job(
        display_name="Sparse Public Record",
        persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
        creation_mode="public_research",
        runtime_source="test",
        agent_id="fake_agent",
        model_id="fake-gpt-5",
    )
    await asyncio.sleep(0)
    current = app.persona_creation.get_job(job.id)
    assert current.research_policy.min_unique_sources == 30
    assert current.status in {"researching", "extracting", "completed_with_gaps", "failed"}


@pytest.mark.anyio
async def test_public_research_sources_have_provenance(app) -> None:
    broker = FakeResearchBroker(count=2)
    app.persona_creation.research_broker = broker
    await app.agent_discovery.scan(force_refresh=True)
    persona = app.personas.create(
        display_name="Provenance Subject",
        aliases=[],
        persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
        run_mode="counterfactual_continuation",
    )
    source = app.personas.add_source_text(
        persona.id,
        title="Archive",
        source_type="web",
        canonical_url="https://example.test/archive",
        publisher="Archive Publisher",
        author="Archive Author",
        published_at="1970-01-01",
        accessed_at="2026-01-01T00:00:00+00:00",
        content="Historical evidence",
        metadata={"provenance": "web_research", "category": "archive"},
    )
    assert source.metadata["canonical_url"] == "https://example.test/archive"
    assert source.metadata["publisher"] == "Archive Publisher"


@pytest.mark.anyio
async def test_private_persona_does_not_auto_web_search(app) -> None:
    broker = FakeResearchBroker()
    app.persona_creation.research_broker = broker
    await app.agent_discovery.scan(force_refresh=True)
    job = await app.persona_creation.create_job(
        display_name="Private Friend",
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        creation_mode="private_materials",
        runtime_source="test",
        agent_id="fake_agent",
        model_id="fake-gpt-5",
    )
    await asyncio.sleep(0)
    assert broker.queries == []
    assert app.persona_creation.get_job(job.id).status in {
        "created",
        "planning",
        "waiting_for_materials",
    }


@pytest.mark.anyio
async def test_private_guided_interview_fills_gaps(app) -> None:
    await app.agent_discovery.scan(force_refresh=True)
    job = await app.persona_creation.create_job(
        display_name="Interview Subject",
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        creation_mode="guided_interview",
        runtime_source="test",
        agent_id="fake_agent",
        model_id="fake-gpt-5",
    )
    await asyncio.sleep(0)
    await app.persona_creation.answer_interview(
        job.id, "我在压力下会先收集事实。", dimension="decisions_and_behavior"
    )
    assert app.personas.source_count(job.persona_id or "") >= 1


@pytest.mark.anyio
async def test_insufficient_private_materials_stays_draft(app) -> None:
    await app.agent_discovery.scan(force_refresh=True)
    job = await app.persona_creation.create_job(
        display_name="Draft Private Subject",
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        creation_mode="private_materials",
        runtime_source="test",
        agent_id="fake_agent",
        model_id="fake-gpt-5",
    )
    await asyncio.sleep(0)
    assert app.personas.get(job.persona_id or "").manifest.compile_state == "draft"


@pytest.mark.anyio
async def test_duplicate_persona_detected(app) -> None:
    app.personas.create(
        display_name="Duplicate Subject",
        aliases=["Alias Subject"],
        persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
        run_mode="counterfactual_continuation",
    )
    await app.agent_discovery.scan(force_refresh=True)
    with pytest.raises(Exception, match="persona_exists"):
        await app.persona_creation.create_job(
            display_name="Alias Subject",
            persona_type=PersonaType.PUBLIC_HISTORICAL_PERSON,
            creation_mode="public_research",
            runtime_source="test",
            agent_id="fake_agent",
            model_id="fake-gpt-5",
            duplicate_action="cancel",
        )


@pytest.mark.anyio
async def test_existing_persona_can_be_enriched(app) -> None:
    existing = app.personas.create(
        display_name="Existing Subject",
        aliases=[],
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        run_mode="digital_continuation",
    )
    await app.agent_discovery.scan(force_refresh=True)
    job = await app.persona_creation.create_job(
        display_name="Existing Subject",
        existing_persona_id=existing.id,
        duplicate_action="enrich",
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        creation_mode="private_materials",
        runtime_source="test",
        agent_id="fake_agent",
        model_id="fake-gpt-5",
    )
    assert job.persona_id == existing.id
