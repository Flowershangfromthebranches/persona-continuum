from __future__ import annotations

import json
from typing import Any

import pytest

from persona_continuum.agent.context_budget import AgentContextBudgetManager
from persona_continuum.agent.models import EffectiveModelCapabilities
from persona_continuum.application.persona_creation_service import (
    LOCAL_MATERIAL_SYSTEM_PROMPT,
    PersonaCreationJob,
    PersonaCreationOrchestrator,
)
from persona_continuum.compiler.schemas import ArtifactClaim, ResearchArtifact
from persona_continuum.domain.persona import PersonaType


def _dummy_render(item: dict[str, Any]) -> str:
    return (
        "EVIDENCE_ID: {evidence_id}\n"
        "SOURCE_IDS: {source_ids}\n"
        "EVIDENCE_TYPE: {evidence_type}\n"
        "VERBATIM: {verbatim_samples}\n"
        "INTELLIGENCE: {intelligence}\n"
        "CONTENT:\n{content}".format(
            evidence_id=item.get("evidence_id"),
            source_ids=json.dumps(item.get("source_ids") or [], ensure_ascii=False),
            evidence_type=item.get("evidence_type"),
            verbatim_samples=json.dumps(item.get("verbatim_samples") or [], ensure_ascii=False),
            intelligence=json.dumps(
                item.get("intelligence") or {}, ensure_ascii=False, default=str
            ),
            content=item.get("content") or "",
        )
    )


def test_split_oversized_evidence_handles_large_metadata() -> None:
    context_manager = AgentContextBudgetManager()
    capabilities = EffectiveModelCapabilities(
        agent_id="test_agent",
        adapter_id="test_adapter",
        requested_model="gemini-3.8-flash-high",
        effective_model="gemini-3.8-flash-high",
        planning_context_window=32768,
    )

    # Simulate an item with large intelligence list (e.g. 50 items)
    large_intelligence = [
        {"claims": [f"claim {i}"], "emotions": ["attachment"], "quotes": [f"quote {i}"]}
        for i in range(50)
    ]
    item = {
        "evidence_id": "evf_test_large",
        "source_ids": ["src_1"],
        "evidence_type": "fused",
        "verbatim_samples": ["sample quote 1", "sample quote 2"],
        "intelligence": large_intelligence,
        "content": "Short canonical claim describing behavioral pattern.",
    }

    prompt_intro = "为 Persona Continuum 提取维度 interviews_and_dialogue 的 ResearchArtifact。"
    schema = ResearchArtifact.model_json_schema()

    service = PersonaCreationOrchestrator.__new__(PersonaCreationOrchestrator)
    budget = context_manager.budget_for(
        model=capabilities, phase="targeted_repair", expected_output=schema
    )
    base_tokens = context_manager.estimate_tokens(prompt_intro) + context_manager.estimate_tokens(
        LOCAL_MATERIAL_SYSTEM_PROMPT
    )
    available = int((budget.evidence_token_budget - base_tokens) * 0.9)

    split_items = service._split_oversized_evidence(
        [item],
        render=_dummy_render,
        context_manager=context_manager,
        capabilities=capabilities,
        phase="targeted_repair",
        system_prompt=LOCAL_MATERIAL_SYSTEM_PROMPT,
        base_text=prompt_intro,
        expected_output=schema,
    )

    assert len(split_items) >= 1
    for sub in split_items:
        rendered = _dummy_render(sub)
        tokens = context_manager.estimate_tokens(rendered)
        assert tokens <= available, f"Sub-chunk tokens {tokens} exceeds available {available}"

    # Verify that iter_batches can consume these items without ContextBudgetExceededError
    batches = list(
        context_manager.iter_batches(
            split_items,
            item_text=_dummy_render,
            max_items=16,
            phase="targeted_repair",
            model=capabilities,
            system_prompt=LOCAL_MATERIAL_SYSTEM_PROMPT,
            expected_output=schema,
            base_text=prompt_intro,
        )
    )
    assert len(batches) >= 1


@pytest.mark.anyio
async def test_run_dimension_extraction_passes_consistent_phase() -> None:
    context_manager = AgentContextBudgetManager()
    captured_phases: list[str] = []

    original_iter_batches = context_manager.iter_batches

    def recording_iter_batches(*args: Any, **kwargs: Any) -> Any:
        captured_phases.append(str(kwargs.get("phase") or ""))
        return original_iter_batches(*args, **kwargs)

    service = PersonaCreationOrchestrator.__new__(PersonaCreationOrchestrator)
    class DummyConfig:
        persona_dimension_retrieval_items = 48

    class DummyContinuum:
        config = DummyConfig()

    service.continuum = DummyContinuum()  # type: ignore
    service._agent_json = None  # type: ignore

    job = PersonaCreationJob(
        id="pcjob_phase_test",
        display_name="Test Persona",
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        creation_mode="private_materials",
        runtime_source="local_cli",
        agent_id="test_agent",
        model_id="gemini-3.8-flash-high",
    )

    capabilities = EffectiveModelCapabilities(
        agent_id="test_agent",
        adapter_id="test_adapter",
        requested_model="gemini-3.8-flash-high",
        effective_model="gemini-3.8-flash-high",
        planning_context_window=32768,
    )

    service._effective_model_capabilities = lambda j: capabilities  # type: ignore
    service._transport_capped_working_context = lambda j, c: None  # type: ignore
    service._dimension_batch_checkpoints = lambda j, d: {}  # type: ignore
    service._batch_evidence_fingerprint = lambda b: "fp"  # type: ignore
    service._raise_if_pause_requested = lambda j: None  # type: ignore
    service._save = lambda j: None  # type: ignore

    dummy_artifact = ResearchArtifact(
        artifact_id="art_1",
        schema_version="1.1",
        dimension="interviews_and_dialogue",
        source_ids=["src_1"],
        claims=[
            ArtifactClaim(
                content="Valid claim",
                claim_type="historical_self_report",
                source_id="src_1",
                confidence=0.9,
            )
        ],
        memories=[],
        extracted_components={},
        conflicts=[],
        uncertainty={"level": 0.0, "notes": []},
        created_by="test",
        artifact_hash="hash",
    )

    async def fake_agent_json(*args: Any, **kwargs: Any) -> Any:
        return dummy_artifact

    service._agent_json = fake_agent_json  # type: ignore
    service._normalize_batch_artifact_sources = lambda art, batch, s_ids, dimension: art  # type: ignore
    async def fake_emit(*args: Any, **kwargs: Any) -> None:
        pass
    service._emit = fake_emit  # type: ignore
    service._hierarchical_reduce_artifacts = lambda *args, **kwargs: dummy_artifact  # type: ignore

    test_context_manager = AgentContextBudgetManager()
    test_context_manager.iter_batches = recording_iter_batches  # type: ignore

    evidence_items = [
        {
            "evidence_id": "ev_1",
            "source_ids": ["src_1"],
            "evidence_type": "atomic",
            "verbatim_samples": [],
            "intelligence": {},
            "content": "Short test statement",
        }
    ]

    await service._run_dimension_extraction(
        job,
        dimension="interviews_and_dialogue",
        evidence_items=evidence_items,
        participant_id="test_participant",
        mode="targeted_repair",
        source_by_id={"src_1": None},
        context_manager=test_context_manager,
        phase="targeted_repair",
    )

    assert "targeted_repair" in captured_phases
    assert "dimension_extraction" not in captured_phases
