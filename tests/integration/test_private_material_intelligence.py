from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from persona_continuum.application.material_intelligence import EvidenceUnit, MaterialJobStatus
from persona_continuum.application.material_pipeline import (
    AnalysisWindow,
    ResolvedExecutionProfile,
)
from persona_continuum.application.persona_creation_service import PersonaCreationJob
from persona_continuum.domain.persona import PersonaType


def _persona(app, name: str = "Private Evidence Subject"):
    return app.personas.create(
        display_name=name,
        aliases=[],
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        run_mode="digital_continuation",
    )


def _source(app, persona_id: str, text: str, *, source_type: str = "txt", metadata=None):
    return app.personas.add_source_text(
        persona_id,
        title=f"material-{len(text)}",
        source_type=source_type,
        canonical_url=None,
        publisher="user",
        author="user",
        published_at=None,
        accessed_at="2026-08-25T00:00:00+00:00",
        content=text,
        metadata={
            "provenance": "user_provided",
            "source_kind": "user_provided",
            **(metadata or {}),
        },
    )


@pytest.mark.anyio
async def test_material_classification_cancels_sibling_windows_after_failure(
    app, monkeypatch
) -> None:
    units = [
        EvidenceUnit(
            id=f"evidence-{index}",
            persona_id="persona-test",
            source_id=f"source-{index}",
            text=f"material {index}",
            normalized_text=f"material {index}",
        )
        for index in range(2)
    ]
    windows = [
        AnalysisWindow(
            id=f"window-{index}",
            evidence_unit_ids=[unit.id],
            source_ids=[unit.source_id],
            text=unit.text,
            token_estimate=10,
        )
        for index, unit in enumerate(units)
    ]
    monkeypatch.setattr(
        "persona_continuum.application.material_intelligence.build_analysis_windows",
        lambda *_args, **_kwargs: windows,
    )
    both_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    calls = 0

    async def analyzer(_phase, request):
        nonlocal calls
        calls += 1
        if calls == 2:
            both_started.set()
        await both_started.wait()
        if request["_participant_id"].endswith(":0"):
            raise RuntimeError("classification_timeout")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise

    with pytest.raises(RuntimeError, match="classification_timeout"):
        await app.material_intelligence._classify_with_agent(
            units,
            analyzer,
            execution_profile=ResolvedExecutionProfile(parallel_safe=True),
        )

    await asyncio.wait_for(sibling_cancelled.wait(), timeout=0.2)
    assert calls == 2


def test_many_mixed_files_are_all_ingested(app) -> None:
    persona = _persona(app)
    sources = [
        _source(app, persona.id, "小时候我住在上海。", source_type="txt"),
        _source(app, persona.id, "sender: me\ncontent: 我喜欢把复杂问题讲清楚。", source_type="md"),
        _source(
            app,
            persona.id,
            '{"messages":[{"sender":"me","content":"今天做了一个决定。"}]}',
            source_type="json",
        ),
    ]
    job = app.material_intelligence.analyze_sources(persona.id, [item.id for item in sources])
    assert job.status == MaterialJobStatus.READY_FOR_COMPILATION
    assert job.progress["source_count"] == 3


def test_real_csv_loader_preserves_header_and_messages(app, tmp_path: Path) -> None:
    persona = _persona(app)
    path = tmp_path / "chat.csv"
    path.write_text(
        "timestamp,sender,content\n"
        "2024-01-01T10:00:00Z,me,第一条\n"
        "2024-01-01T10:01:00Z,friend,第二条\n",
        encoding="utf-8",
    )
    source = app.personas.add_sources(persona.id, [path])[0]
    app.material_intelligence.analyze_sources(persona.id, [source.id])
    units = app.material_intelligence.get_index(persona.id).units()
    assert [item.text for item in units] == ["第一条", "第二条"]
    assert [item.speaker for item in units] == ["me", "friend"]


def test_chat_file_creates_multiple_evidence_units(app) -> None:
    persona = _persona(app)
    source = _source(
        app,
        persona.id,
        '{"timestamp":"2020-01-01T10:00:00Z","sender":"me","content":"第一条"}\n'
        '{"timestamp":"2020-01-01T10:05:00Z","sender":"friend","content":"第二条"}',
        source_type="jsonl",
    )
    app.material_intelligence.analyze_sources(persona.id, [source.id])
    units = app.material_intelligence.get_index(persona.id).units()
    assert len(units) == 2
    assert all(item.source_locator.get("segment_index") is not None for item in units)


def test_source_locator_preserved(app) -> None:
    persona = _persona(app)
    source = _source(app, persona.id, "Me: 一段可定位的原话。", source_type="md")
    app.material_intelligence.analyze_sources(persona.id, [source.id])
    unit = app.material_intelligence.get_index(persona.id).units()[0]
    assert unit.source_id == source.id
    assert unit.source_locator["source_path"] == source.path


def test_exact_duplicate_evidence_clusters(app) -> None:
    persona = _persona(app)
    first = _source(app, persona.id, "我在压力下会先写出约束。")
    second = _source(app, persona.id, "我在压力下会先写出约束。 ")
    # Source hashes are exact-content based; the trailing space remains a
    # separate raw source and should collapse at the evidence layer.
    app.material_intelligence.analyze_sources(persona.id, [first.id, second.id])
    clusters = (
        app.material_intelligence.get_index(persona.id)
        .database.conn.execute(
            "SELECT cluster_type FROM persona_evidence_clusters WHERE persona_id = ?", (persona.id,)
        )
        .fetchall()
    )
    assert any(row["cluster_type"] == "exact_duplicate" for row in clusters)


def test_near_duplicate_evidence_clusters(app) -> None:
    persona = _persona(app)
    first = _source(app, persona.id, "遇到冲突时我会先写出约束和目标。")
    second = _source(app, persona.id, "遇到冲突时我会先写出约束、目标和边界。")
    app.material_intelligence.analyze_sources(persona.id, [first.id, second.id])
    clusters = (
        app.material_intelligence.get_index(persona.id)
        .database.conn.execute(
            "SELECT cluster_type FROM persona_evidence_clusters WHERE persona_id = ?", (persona.id,)
        )
        .fetchall()
    )
    assert any(row["cluster_type"] == "near_duplicate" for row in clusters)


def test_duplicate_source_does_not_increase_independence(app) -> None:
    persona = _persona(app)
    first = _source(app, persona.id, "我重视长期关系和清晰表达。")
    second = _source(app, persona.id, "我重视长期关系和清晰表达。 ")
    app.material_intelligence.analyze_sources(persona.id, [first.id, second.id])
    coverage = app.material_intelligence.coverage(persona.id)
    assert coverage.evidence_unit_count == 2
    assert coverage.fused_evidence_count == 1


def test_similar_files_are_semantically_unioned(app) -> None:
    persona = _persona(app)
    first = _source(app, persona.id, "我会先听完对方，再做决定。")
    second = _source(app, persona.id, "我会先听完对方，再做决定；也会记录风险。")
    app.material_intelligence.analyze_sources(persona.id, [first.id, second.id])
    fused = app.material_intelligence.get_index(persona.id).fused()
    assert fused and "记录风险" in fused[0].canonical_claim


def test_shared_information_only_once(app) -> None:
    persona = _persona(app)
    first = _source(app, persona.id, "我每天写下目标。")
    second = _source(app, persona.id, "我每天写下目标。 ")
    app.material_intelligence.analyze_sources(persona.id, [first.id, second.id])
    fused = app.material_intelligence.get_index(persona.id).fused()
    assert len(fused) == 1
    assert fused[0].canonical_claim.count("我每天写下目标") == 1


def test_unique_information_from_both_files_preserved(app) -> None:
    persona = _persona(app)
    first = _source(app, persona.id, "我每天写下目标。")
    second = _source(app, persona.id, "我每天写下目标，也会记录风险。")
    app.material_intelligence.analyze_sources(persona.id, [first.id, second.id])
    claim = app.material_intelligence.get_index(persona.id).fused()[0].canonical_claim
    assert "目标" in claim and "风险" in claim


def test_fused_evidence_keeps_all_provenance(app) -> None:
    persona = _persona(app)
    first = _source(app, persona.id, "冲突时我会先听完对方。")
    second = _source(app, persona.id, "冲突时我会先听完对方，再记录风险。")
    app.material_intelligence.analyze_sources(persona.id, [first.id, second.id])
    fused = app.material_intelligence.get_index(persona.id).fused()[0]
    evidence_ids = {item.id for item in app.material_intelligence.get_index(persona.id).units()}
    assert set(fused.source_ids) == {first.id, second.id}
    assert set(fused.supporting_evidence_ids) == evidence_ids
    assert set(fused.unique_evidence_ids).issubset(evidence_ids)


def test_conflicting_claims_are_not_silently_overwritten(app) -> None:
    persona = _persona(app)
    first = _source(app, persona.id, "我每天工作 5 小时。")
    second = _source(app, persona.id, "我每天工作 8 小时。")
    app.material_intelligence.analyze_sources(persona.id, [first.id, second.id])
    contradictions = app.material_intelligence.get_index(persona.id).contradictions()
    assert contradictions and contradictions[0].contradiction_type == "FACT_CONFLICT"


def test_temporal_change_not_treated_as_simple_error(app) -> None:
    persona = _persona(app)
    first = _source(
        app, persona.id, "2018 年我每天工作 5 小时。", metadata={"event_time": "2018-01-01"}
    )
    second = _source(
        app, persona.id, "2024 年我每天工作 8 小时。", metadata={"event_time": "2024-01-01"}
    )
    app.material_intelligence.analyze_sources(persona.id, [first.id, second.id])
    contradictions = app.material_intelligence.get_index(persona.id).contradictions()
    assert any(item.contradiction_type == "TEMPORAL_CHANGE" for item in contradictions)


def test_context_dependent_behavior_preserved(app) -> None:
    persona = _persona(app)
    first = _source(app, persona.id, "我每天工作 5 小时。", metadata={"context": "家庭"})
    second = _source(app, persona.id, "我每天工作 8 小时。", metadata={"context": "创业"})
    app.material_intelligence.analyze_sources(persona.id, [first.id, second.id])
    contradictions = app.material_intelligence.get_index(persona.id).contradictions()
    assert any(item.contradiction_type == "CONTEXT_DEPENDENT" for item in contradictions)


def test_aliases_can_resolve_same_private_person(app) -> None:
    persona = _persona(app)
    alias = app.material_intelligence.add_identity_alias(
        persona.id, "小张", confidence=0.9, status="confirmed"
    )
    assert app.material_intelligence.resolve_identity_alias(persona.id, "小张") == alias


def test_uncertain_identity_requires_user_confirmation(app) -> None:
    persona = _persona(app)
    alias = app.material_intelligence.add_identity_alias(
        persona.id, "可能是小张", status="requires_confirmation"
    )
    assert alias.status == "requires_confirmation"


def test_dimension_retrieval_searches_full_corpus(app) -> None:
    persona = _persona(app)
    sources = [
        _source(
            app,
            persona.id,
            f"早期作品观点 {index}。",
            metadata={"event_time": f"20{index:02d}-01-01"},
        )
        for index in range(14)
    ]
    app.material_intelligence.analyze_sources(persona.id, [item.id for item in sources])
    results = app.material_intelligence.get_index(persona.id).retrieve(
        "works_and_views", top_k=24, diversity=False
    )
    assert len(results) >= 14


def test_dimension_retrieval_not_limited_to_last_12_sources(app) -> None:
    persona = _persona(app)
    sources = [
        _source(app, persona.id, f"identity timeline event {index}。") for index in range(14)
    ]
    app.material_intelligence.analyze_sources(persona.id, [item.id for item in sources])
    results = app.material_intelligence.get_index(persona.id).retrieve(
        "identity_and_timeline", top_k=24, diversity=False
    )
    assert sources[0].id in {value for item in results for value in item["source_ids"]}


def test_retrieval_preserves_source_diversity(app) -> None:
    persona = _persona(app)
    sources = [_source(app, persona.id, f"作品观点与决定 {index}。") for index in range(3)]
    app.material_intelligence.analyze_sources(persona.id, [item.id for item in sources])
    results = app.material_intelligence.get_index(persona.id).retrieve("works_and_views", top_k=3)
    assert len({value for item in results for value in item["source_ids"]}) >= 3


def test_large_chat_file_not_counted_as_only_one_information_unit(app) -> None:
    persona = _persona(app)
    content = "\n".join(
        json.dumps(
            {
                "timestamp": f"2022-01-01T10:{index:02d}:00Z",
                "sender": "me",
                "content": f"消息 {index}",
            },
            ensure_ascii=False,
        )
        for index in range(20)
    )
    source = _source(app, persona.id, content, source_type="jsonl")
    app.material_intelligence.analyze_sources(persona.id, [source.id])
    assert app.material_intelligence.coverage(persona.id).evidence_unit_count == 20


def test_private_coverage_uses_messages_and_episodes(app) -> None:
    persona = _persona(app)
    source = _source(
        app,
        persona.id,
        '{"conversation_id":"c","sender":"me","content":"你好"}\n{"conversation_id":"c","sender":"friend","content":"你好"}',
        source_type="jsonl",
    )
    app.material_intelligence.analyze_sources(persona.id, [source.id])
    coverage = app.material_intelligence.coverage(persona.id)
    assert coverage.message_count == 2 and coverage.episode_count == 1


def test_plain_document_does_not_count_as_conversation_episode(app) -> None:
    persona = _persona(app)
    source = _source(app, persona.id, "一篇普通的个人说明文档。", source_type="txt")
    app.material_intelligence.analyze_sources(persona.id, [source.id])
    coverage = app.material_intelligence.coverage(persona.id)
    assert coverage.message_count == 0
    assert coverage.episode_count == 0


def test_guided_interview_counts_as_message_and_episode(app) -> None:
    persona = _persona(app)
    source = _source(app, persona.id, "压力下我会先收集事实。", source_type="guided_interview")
    app.material_intelligence.analyze_sources(persona.id, [source.id])
    coverage = app.material_intelligence.coverage(persona.id)
    assert coverage.message_count == 1
    assert coverage.episode_count == 1


def test_guided_interview_targets_remaining_gaps(app) -> None:
    persona = _persona(app)
    source = _source(app, persona.id, "我喜欢阅读。", source_type="guided_interview")
    app.material_intelligence.analyze_sources(persona.id, [source.id])
    gaps = app.material_intelligence.gap_analysis(persona.id)
    assert gaps["gaps"]
    assert "question" in gaps["gaps"][0]


@pytest.mark.anyio
async def test_compiler_uses_fused_evidence(app, monkeypatch) -> None:
    persona = _persona(app)
    source = _source(app, persona.id, "我会记录风险并做决定。")
    app.material_intelligence.analyze_sources(persona.id, [source.id])
    task = app.compilation.create_task(persona.id)
    job = PersonaCreationJob(
        id="pcjob_compiler_fused",
        display_name=persona.display_name,
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        creation_mode="private_materials",
        runtime_source="test",
        agent_id="fake_agent",
        model_id="fake-gpt-5",
        persona_id=persona.id,
        compilation_task_id=task.id,
        source_ids=[source.id],
    )
    prompts: list[tuple[str, str]] = []

    async def artifact(*args, **kwargs):
        prompts.append((str(kwargs["user_message"]), str(kwargs["system_prompt"])))
        dimension = str(kwargs["participant_id"])
        return {
            "artifact_id": f"art_{dimension}",
            "schema_version": "1.1",
            "dimension": dimension,
            "source_ids": [source.id],
            "claims": [
                {
                    "content": "只来自融合证据的声明",
                    "source_id": source.id,
                    "claim_type": "historical_self_report",
                    "confidence": 0.7,
                }
            ],
            "memories": [],
            "extracted_components": {},
            "conflicts": [],
            "uncertainty": {"level": 0.3, "notes": []},
            "created_by": "test",
            "artifact_hash": f"hash_{dimension}",
        }

    monkeypatch.setattr(app.persona_creation, "_agent_json", artifact)
    await app.persona_creation._extract_dimensions(job)
    assert any("EVIDENCE_TYPE: fused" in user_prompt for user_prompt, _ in prompts)
    assert prompts and all("禁止联网" in system_prompt for _, system_prompt in prompts)


@pytest.mark.anyio
async def test_async_material_analysis_calls_agent_for_hybrid_semantics(app) -> None:
    persona = _persona(app)
    first = _source(app, persona.id, "遇到压力时，我先把事实列出来。")
    second = _source(app, persona.id, "困难出现后，我的第一步是整理客观信息。")
    phases: list[str] = []

    async def analyzer(phase: str, payload: dict):
        phases.append(phase)
        if phase == "classify":
            return {
                "units": [
                    {
                        "id": item["id"],
                        "dimension_scores": {"decisions_and_behavior": 0.9},
                        "evidence_type": "decision_example",
                        "confidence": 0.8,
                    }
                    for item in payload["units"]
                ]
            }
        if phase == "relate":
            return {
                "clusters": [{"evidence_ids": [item["id"] for item in payload["units"]]}],
                "contradictions": [],
            }
        return {
            "claims": [
                {
                    "id": item["id"],
                    "canonical_claim": item["deterministic_claim"],
                }
                for item in payload["claims"]
            ]
        }

    job = await app.material_intelligence.analyze_sources_async(
        persona.id, [first.id, second.id], agent_analyzer=analyzer
    )
    assert job.status == MaterialJobStatus.READY_FOR_COMPILATION
    assert {"classify", "relate", "fuse"}.issubset(phases)
    fused = app.material_intelligence.get_index(persona.id).fused()
    assert any(set(item.source_ids) == {first.id, second.id} for item in fused)


def test_failed_derived_rebuild_preserves_previous_index(app, monkeypatch) -> None:
    persona = _persona(app)
    first = _source(app, persona.id, "我每天都会记录目标。")
    app.material_intelligence.analyze_sources(persona.id, [first.id])
    before = app.material_intelligence.get_index(persona.id).fused()

    def fail_persist(*args, **kwargs):
        raise RuntimeError("simulated_fusion_write_failure")

    monkeypatch.setattr(app.material_intelligence, "_persist_fused", fail_persist)
    second = _source(app, persona.id, "我也会记录风险。")
    with pytest.raises(RuntimeError, match="simulated_fusion_write_failure"):
        app.material_intelligence.analyze_sources(
            persona.id, [first.id, second.id], incremental=True
        )
    after = app.material_intelligence.get_index(persona.id).fused()
    assert [item.model_dump() for item in after] == [item.model_dump() for item in before]


def test_compiler_keeps_contradictions(app) -> None:
    persona = _persona(app)
    first = _source(app, persona.id, "我每天工作 5 小时。")
    second = _source(app, persona.id, "我每天工作 8 小时。")
    app.material_intelligence.analyze_sources(persona.id, [first.id, second.id])
    assert app.material_intelligence.get_index(persona.id).contradictions()


def test_compiler_keeps_conditional_patterns(app) -> None:
    persona = _persona(app)
    first = _source(app, persona.id, "在家庭场景我每天工作 5 小时。", metadata={"context": "家庭"})
    second = _source(app, persona.id, "在创业场景我每天工作 8 小时。", metadata={"context": "创业"})
    app.material_intelligence.analyze_sources(persona.id, [first.id, second.id])
    assert any(
        item.conditions for item in app.material_intelligence.get_index(persona.id).contradictions()
    )


def test_expression_dna_preserves_verbatim_samples(app) -> None:
    persona = _persona(app)
    source = _source(app, persona.id, "我常说：把复杂问题讲清楚。", source_type="guided_interview")
    app.material_intelligence.analyze_sources(persona.id, [source.id])
    assert (
        "我常说：把复杂问题讲清楚。"
        in app.material_intelligence.get_index(persona.id).get_expression_samples()[0]["text"]
    )


def test_new_material_incrementally_enriches_existing_persona(app) -> None:
    persona = _persona(app)
    first = _source(app, persona.id, "早期我喜欢阅读。")
    app.material_intelligence.analyze_sources(persona.id, [first.id])
    second = _source(app, persona.id, "后来我开始记录风险。")
    app.material_intelligence.analyze_sources(persona.id, [first.id, second.id], incremental=True)
    assert app.material_intelligence.coverage(persona.id).source_count == 2


def test_old_persona_version_preserved(app) -> None:
    persona = _persona(app)
    profile = app.profile_library.sync_persona(persona)
    app.profile_library.update_profile(
        profile.id, summary="补充材料后的简介。", payload={"new": True}
    )
    versions = app.profile_library.list_versions(profile.id)
    assert versions and versions[0].version == profile.version
