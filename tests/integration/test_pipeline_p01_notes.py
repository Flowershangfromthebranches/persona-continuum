from unittest.mock import patch

import pytest

from persona_continuum.application.material_intelligence import (
    EvidenceSimilarityAnalyzer,
    EvidenceUnit,
    PersonaContradictionAnalyzer,
    PersonaEvidenceFusionService,
)
from persona_continuum.application.persona_notes import notes_allow_web, parse_persona_notes
from persona_continuum.domain.persona import PersonaType


def unit(index, text="好的", **kwargs):
    return EvidenceUnit(id=f"u{index:06d}", persona_id="p", source_id="s",
                        text=text, normalized_text=text, **kwargs)


@pytest.mark.parametrize("persisted", [False, True])
def test_context_never_retrieved_as_persona(app, persisted):
    persona = app.personas.create(display_name="Subject", aliases=[],
                                 persona_type=PersonaType.PRIVATE_LIVING_PERSON,
                                 run_mode="digital_continuation")
    source = app.personas.add_source_text(
        persona.id, title="chat", source_type="txt", canonical_url=None,
        publisher="user", author="user", published_at=None, accessed_at=None,
        content="# 我 = 聊天记录导出者\n# 对方 = 目标 Persona\n"
        "2023-04-03 20:00:00 | 对方 | 好的。\n"
        "2023-04-03 20:00:10 | 我 | 我决定选择拒绝这个项目，因为这是我的价值观。",
        metadata={},
    )
    service = app.material_intelligence
    if persisted:
        service.in_memory_unit_limit = service.max_source_bytes = 1
    service.analyze_sources(persona.id, [source.id])
    index = service.get_index(persona.id)
    assert len(index.units()) == 2
    assert any("价值观" in item.text for item in index.units())
    for dimension in ("decisions_and_behavior", "values_desires_contradictions"):
        assert all("价值观" not in row["text"] for row in index.retrieve(dimension))
    clusters = service._cluster_persisted(persona.id)
    exporter = next(item for item in index.units() if "价值观" in item.text)
    assert all(exporter.id not in item.member_evidence_ids for item in clusters)
    assert any(exporter.id in episode.evidence_unit_ids for episode in index.episodes())


def test_exact_duplicate_5000_has_no_pairwise_work():
    units = [unit(i) for i in range(5000)]
    analyzer = PersonaContradictionAnalyzer()
    with patch("persona_continuum.application.material_intelligence._jaccard") as compare:
        assert analyzer.analyze(units, "p") == []
        compare.assert_not_called()


def test_variants_bounded_and_deterministic():
    units = [unit(i, f"项目计划 {i}") for i in range(500)]
    analyzer = PersonaContradictionAnalyzer(8)
    assert analyzer._variants(units) == analyzer._variants(list(reversed(units)))
    assert len(analyzer._variants(units * 10)) == 8


def test_singleton_has_no_fused_row():
    units = [unit(0)]
    clusters = EvidenceSimilarityAnalyzer().cluster(units, "p")
    assert PersonaEvidenceFusionService().fuse(units, clusters, [], "p") == []


def test_notes_separate_guidance_from_facts():
    parsed = parse_persona_notes(
        "重点研究他的关系模式。她大学期间在济南生活三年，这是我确认的信息。"
        "这是某作品中的角色而非现实同名人物。不要联网补充。"
    )
    assert parsed["instruction"] == ["重点研究他的关系模式"]
    assert len(parsed["user_supplied_fact"]) == 1
    assert len(parsed["identity_hint"]) == 1
    assert not notes_allow_web("不要联网补充")
    assert notes_allow_web("请联网补充")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "notes",
    ["", "重点研究他的关系模式", "她大学期间在济南生活三年，这是我确认的信息"],
)
async def test_notes_roundtrip_local_first_and_fact_ingest(app, notes):
    await app.agent_discovery.scan(force_refresh=True)
    job = await app.persona_creation.create_job(
        display_name="Notes Subject", persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        creation_mode="public_research", runtime_source="test", agent_id="fake_agent",
        model_id="fake-gpt-5", persona_notes=notes, materials=[{"content": "好的"}],
        start_worker=False,
    )
    assert job.creation_mode == "private_materials"
    assert job.research_mode == "local"
    loaded = app.persona_creation.get_job(job.id)
    assert loaded.persona_notes == notes
    assert loaded.identity_context is None
    assert loaded.user_defined_facts is None
    loaded.job_config["materials"] = []
    await app.persona_creation._ingest_configured_materials(loaded)
    sources = app.personas.get_sources(job.persona_id)
    assert len(sources) == (1 if "我确认" in notes else 0)
    if sources:
        assert sources[0].metadata["provenance"] == "user_supplied"


@pytest.mark.anyio
async def test_legacy_fields_read_as_notes(app):
    await app.agent_discovery.scan(force_refresh=True)
    job = await app.persona_creation.create_job(
        display_name="Legacy", persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        creation_mode="private_materials", runtime_source="test", agent_id="fake_agent",
        model_id="fake-gpt-5", identity_context="某作品角色", user_defined_facts="生于济南",
        research_instructions="关注关系", start_worker=False,
    )
    loaded = app.persona_creation.get_job(job.id)
    assert loaded.persona_notes == "某作品角色\n生于济南\n关注关系"
    assert loaded.user_defined_facts == "生于济南"
