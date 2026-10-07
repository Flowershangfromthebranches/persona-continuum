"""Sparse conversation-evidence-v4 integrity tests (Large Conversation Pipeline V2).

The V4 contract replaces reviewed_ids/missing-id retries: a structurally
successful window means every target turn inside was reviewed; absent turns
become reviewed_no_independent_evidence locally.  These tests pin that
semantics plus provenance/validation failures.
"""

from __future__ import annotations

import asyncio

import pytest

from persona_continuum.agent.response_collector import AgentStructuredOutputError
from persona_continuum.agent.structured_output import (
    StructuredOutputEngine,
    StructuredOutputParseError,
)
from persona_continuum.application.material_chat import (
    SEMANTIC_EVIDENCE_EXTRACTED,
    SEMANTIC_REVIEWED_NO_EVIDENCE,
)
from persona_continuum.application.material_intelligence import (
    EvidenceUnit,
    MaterialClassificationResult,
)
from persona_continuum.application.material_pipeline import (
    MaterialPipelineMetrics,
    PreLLMDeduplicator,
    ResolvedExecutionProfile,
)
from persona_continuum.domain.persona import PersonaType


def _fixture(app, count=12):
    persona = app.personas.create(
        display_name="Synthetic integrity subject",
        aliases=[],
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        run_mode="digital_continuation",
    )
    source = app.personas.add_source_text(
        persona.id,
        title="synthetic-chat",
        source_type="txt",
        canonical_url=None,
        publisher="user",
        author="user",
        published_at=None,
        accessed_at=None,
        content="Synthetic source only",
        metadata={},
    )
    units = [
        EvidenceUnit(
            id=f"evu_{index:04d}",
            persona_id=persona.id,
            source_id=source.id,
            source_locator={"segment_index": index},
            text=f"synthetic {index}",
            normalized_text=f"synthetic {index}",
            source_kind="chat_import",
            speaker="target" if index % 2 else "exporter",
        )
        for index in range(count)
    ]
    service = app.material_intelligence
    service.batch_size = 3
    service.analysis_window_max_units = 3
    service.semantic_gate_mode = "full"
    service._persist_units(list(reversed(units)))
    return service, persona, units


@pytest.mark.anyio
async def test_stable_pagination_sparse_coverage_and_resume(app):
    """Streaming folds every raw unit exactly once; resume never re-bills."""

    service, persona, units = _fixture(app)
    profile = ResolvedExecutionProfile()
    visited: list[str] = []
    events = []

    async def analyzer(phase, request):
        ids = list(request["target_units"])
        visited.extend(ids)
        first = next(row for row in request["units"] if row["id"] == ids[0])
        return {
            "units": [
                {
                    "id": ids[0],
                    "claims": ["One shared observation"],
                    "supporting_evidence_ids": first["evidence_ids"],
                }
            ]
        }

    async def report(event):
        events.append(event)

    await service._classify_persisted_with_agent(
        persona.id,
        analyzer,
        execution_profile=profile,
        metrics=MaterialPipelineMetrics(),
        window_progress=report,
    )
    assert visited == [unit.id for unit in units]
    stored = [unit for batch in service._iter_units_batched(persona.id) for unit in batch]
    assert all(service._has_classification(unit, profile) for unit in stored)
    assert sum(bool(unit.metadata.get("evidence_intelligence")) for unit in stored) == 4
    assert events[-1]["classification_completed"] == 12
    assert events[-1]["classification_total"] == 12
    assert events[-1]["classification_pending"] == 0
    service._persist_units(units)  # Re-segmentation must not erase review state.
    visited.clear()
    await service._classify_persisted_with_agent(
        persona.id,
        analyzer,
        execution_profile=profile,
        metrics=MaterialPipelineMetrics(),
    )
    assert not visited


@pytest.mark.anyio
async def test_sparse_output_never_triggers_missing_id_retry(app):
    """Partial output = successful sparse review, never a missing-id re-request."""

    service, persona, units = _fixture(app, 3)
    calls: list[list[str]] = []

    async def analyzer(phase, request):
        ids = list(request["target_units"])
        calls.append(ids)
        if len(calls) == 1:
            return {"units": [{"id": ids[0], "claims": ["first fact"]}]}
        raise RuntimeError("must not be reached")

    await service._classify_with_agent(units, analyzer)
    assert len(calls) == 1  # sparse success: no retry, no re-splitting
    stored = {unit.id: unit for batch in service._iter_units_batched(persona.id) for unit in batch}
    assert service._classification_done(stored[units[0].id], ResolvedExecutionProfile())
    assert service._classification_done(stored[units[1].id], ResolvedExecutionProfile())


@pytest.mark.anyio
async def test_checkpoint_preserves_completed_after_runtime_failure(app, monkeypatch):
    """A mid-run failure keeps earlier window checkpoints; later units resume."""

    from persona_continuum.application.material_pipeline import AnalysisWindow

    service, persona, units = _fixture(app, 3)

    def one_window_per_unit(items, **kwargs):
        return [
            AnalysisWindow(
                id=f"aw-{index}",
                evidence_unit_ids=[turn.id],
                source_ids=[turn.source_id],
                text=turn.text,
                token_estimate=16,
            )
            for index, turn in enumerate(items)
        ]

    monkeypatch.setattr(
        "persona_continuum.application.material_intelligence.build_analysis_windows",
        one_window_per_unit,
    )
    calls: list[list[str]] = []

    async def analyzer(phase, request):
        ids = list(request["target_units"])
        calls.append(ids)
        if len(calls) == 2:
            raise RuntimeError("synthetic interruption")
        if len(calls) == 1:
            return {"units": [{"id": ids[0], "claims": ["first fact"]}]}
        return {"units": []}

    with pytest.raises(RuntimeError, match="synthetic interruption"):
        await service._classify_with_agent(units, analyzer)
    profile = ResolvedExecutionProfile()
    stored = {unit.id: unit for batch in service._iter_units_batched(persona.id) for unit in batch}
    # Window 1 checkpoint survived the failure of window 2.
    assert service._classification_done(stored[units[0].id], profile)
    # A failed window does not prevent healthy windows from checkpointing.
    assert service._classification_done(stored[units[2].id], profile)
    # A resume run (fresh rows from the ledger) only revisits what never
    # completed.
    fresh = {
        unit.id: unit
        for batch in service._iter_units_batched(persona.id)
        for unit in batch
    }
    visited: list[str] = []

    async def resume_analyzer(phase, request):
        visited.extend(request["target_units"])
        return {"units": []}

    await service._classify_with_agent(
        [fresh[unit.id] for unit in units], resume_analyzer
    )
    assert units[0].id not in visited
    assert units[2].id not in visited
    assert units[1].id in visited


@pytest.mark.anyio
async def test_empty_result_is_a_valid_full_review(app):
    """V4: empty units = reviewed_no_independent_evidence for the whole window."""

    service, persona, units = _fixture(app, 2)

    async def analyzer(phase, request):
        return {"units": []}

    await service._classify_with_agent(units, analyzer)
    profile = ResolvedExecutionProfile()
    stored = {unit.id: unit for batch in service._iter_units_batched(persona.id) for unit in batch}
    for unit in units:
        row = stored[unit.id]
        assert service._classification_done(row, profile)
        assert row.metadata["semantic_status"] == SEMANTIC_REVIEWED_NO_EVIDENCE
        assert row.metadata["classification_contract"] == "conversation-evidence-v4"


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "evu_0000"},
        {"units": [{"id": "foreign"}]},
        {"units": [{"id": "evu_0000"}, {"id": "evu_0000"}]},
        {"units": [{"id": "evu_0000", "supporting_evidence_ids": ["foreign"]}]},
    ],
)
def test_invalid_envelopes_and_provenance_are_rejected(app, payload):
    service, _, units = _fixture(app, 1)
    with pytest.raises(AgentStructuredOutputError):
        service._classification_values(payload, units)


def test_stray_reviewed_ids_are_ignored_not_required(app):
    """V4 parsers must neither require nor validate a stray reviewed_ids field."""

    service, _, units = _fixture(app, 1)
    values = service._classification_values({"units": [], "reviewed_ids": ["foreign"]}, units)
    assert values == []


def test_gemini_object_and_scalar_fields_are_coerced(app):
    """Prompt-only Gemini output uses objects/scalars where the envelope wants lists."""

    result = MaterialClassificationResult.model_validate(
        {
            "id": "evu_0000",
            "content": "synthetic",
            "source_id": "src",
            "entities": [{"name": "室友", "type": "person"}],
            "dates": [{"date": "2023-02-14", "event": "过节"}],
            "claims": "想回家",
            "relationship_entities": [{"name": "室友"}],
            "life_stage": ["university"],
            "emotions": [{"emotion": "lonely", "trigger": "室友回家"}],
            "dimension_scores": [{"dimension": "affect_relationship_defense", "score": 0.8}],
            "quotes": None,
            "metadata": ["not-a-dict"],
            "speaker_role": ["target"],
        }
    )
    assert result.quotes == []
    assert result.entities == ["室友"]
    assert result.dates == ["2023-02-14"]
    assert result.claims == ["想回家"]
    assert result.relationship_entities == ["室友"]
    assert result.life_stage == "university"
    assert result.speaker_role == "target"
    assert result.dimension_scores["affect_relationship_defense"] == 0.8
    assert result.emotions == [{"emotion": "lonely", "trigger": "室友回家"}]
    assert result.metadata == {}

    service, _, units = _fixture(app, 1)
    values = service._classification_values(
        {
            "units": [
                {
                    "id": units[0].id,
                    "entities": [{"name": "室友", "type": "person"}],
                    "dates": "2023-02-14",
                    "claims": {"text": "想回家"},
                    "relationship_entities": [{"entity": "室友"}],
                    "life_stage": {"stage": "university"},
                    "context_tags": [{"tag": "dorm"}],
                }
            ]
        },
        units,
    )
    assert values[0]["entities"] == ["室友"]
    assert values[0]["dates"] == ["2023-02-14"]
    assert values[0]["claims"] == [{"text": "想回家"}]
    assert values[0]["relationship_entities"] == ["室友"]
    assert values[0]["life_stage"] == "university"
    assert values[0]["context_tags"] == ["dorm"]


@pytest.mark.anyio
async def test_gemini_shaped_window_is_persisted_not_failed(app):
    """A window of Gemini-shaped records must checkpoint, not raise invalid_fields."""

    service, persona, units = _fixture(app, 2)

    async def analyzer(phase, request):
        ids = list(request["target_units"])
        return {
            "units": [
                {
                    "id": ids[0],
                    "entities": [{"name": "室友"}],
                    "dates": [{"date": "2023-02-14"}],
                    "claims": "想回家过节",
                    "relationships": [{"target": "室友", "relation": "roommate"}],
                    "relationship_entities": [{"name": "室友"}],
                    "emotions": [{"emotion": "lonely"}],
                    "life_stage": ["university"],
                }
            ]
        }

    await service._classify_with_agent(units, analyzer)
    stored = {unit.id: unit for batch in service._iter_units_batched(persona.id) for unit in batch}
    extracted = stored[units[0].id]
    intelligence = extracted.metadata["evidence_intelligence"]
    assert intelligence["entities"] == ["室友"]
    assert intelligence["dates"] == ["2023-02-14"]
    assert intelligence["claims"] == ["想回家过节"]
    assert extracted.relationship_entities == ["室友"]
    assert extracted.metadata["semantic_status"] == SEMANTIC_EVIDENCE_EXTRACTED
    assert service._classification_done(stored[units[1].id], ResolvedExecutionProfile())


def test_truncated_outer_json_cannot_become_inner_result():
    raw = '{"units":[{"id":"evu_1","claims":["synthetic"]},{"id":"unfinished'
    with pytest.raises(StructuredOutputParseError):
        StructuredOutputEngine().parse_and_validate(raw, {"type": "object"}, phase="classification")


def test_legacy_success_reused_but_empty_legacy_failure_not_reused(app):
    service, _, units = _fixture(app, 1)
    profile = ResolvedExecutionProfile()
    unit = units[0].model_copy(update={"extraction_method": "deterministic_plus_agent"})
    unit.metadata = {
        "intelligence_cache_key": service._material_intelligence_cache_key(
            unit, profile, legacy=True
        ),
        "evidence_intelligence": {"claims": ["verified legacy fact"]},
    }
    assert service._classification_done(unit, profile)
    unit.metadata["evidence_intelligence"] = {}
    assert not service._classification_done(unit, profile)


def test_v3_success_is_reused_not_rebilled(app):
    """A persisted conversation-evidence-v3 success migrates by reading."""

    service, _, units = _fixture(app, 1)
    profile = ResolvedExecutionProfile()
    unit = units[0].model_copy(update={"extraction_method": "deterministic_plus_agent"})
    unit.metadata = {
        "intelligence_cache_key": service._material_intelligence_cache_key(
            unit, profile, contract="v3"
        ),
        "classification_status": "completed",
        "classification_contract": "conversation-evidence-v3",
        "evidence_intelligence": {"claims": ["verified v3 fact"]},
    }
    assert service._classification_done(unit, profile)
    # v3 and v4 stay separate namespaces: v3 is never silently treated as a
    # v4 stamp, so contract accounting remains auditable.
    v4_key = service._material_intelligence_cache_key(unit, profile)
    assert v4_key != unit.metadata["intelligence_cache_key"]


def test_chat_short_replies_are_not_deduplicated_across_speakers(app):
    _, _, units = _fixture(app, 2)
    repeated = [unit.model_copy(update={"text": "好", "normalized_text": "好"}) for unit in units]
    dedup = PreLLMDeduplicator().deduplicate(repeated)
    assert len(dedup.canonical_units) == 2


@pytest.mark.anyio
async def test_cancellation_drains_children(app):
    service, _, units = _fixture(app, 200)
    active = 0
    started = asyncio.Event()

    async def analyzer(phase, request):
        nonlocal active
        active += 1
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    task = asyncio.create_task(service._classify_with_agent(units, analyzer))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0


@pytest.mark.anyio
async def test_file_role_context_and_batch_neighbors_survive(app, tmp_path):
    service, persona, units = _fixture(app, 6)
    path = tmp_path / "chat.txt"
    path.write_text(
        '# 对方 = 目标 Persona\n# <voice> automatic transcript\n# <emoji name="X"/>\n'
        '2024-01-01 00:00:00 | 对方 | synthetic\n'
    )
    for unit in units:
        unit.metadata["source_path"] = str(path)
    service._persist_units(units)
    requests = []

    async def analyzer(phase, request):
        requests.append(request)
        return {"units": []}

    await service._classify_persisted_with_agent(
        persona.id,
        analyzer,
        execution_profile=ResolvedExecutionProfile(),
        metrics=MaterialPipelineMetrics(),
    )
    assert (
        requests[0]["source_context"][units[0].source_id]["speaker_roles"]["对方"]
        == "target_persona"
    )
    # Batch look-ahead: the first turns of the next group ride along as
    # context rows and are billed later as their own targets (P0-F).
    assert units[3].id in requests[0]["context_units"]
    assert units[3].id not in requests[0]["target_units"]
    billed: list[str] = []
    for request in requests:
        billed.extend(request["target_units"])
    assert billed == [unit.id for unit in units]
