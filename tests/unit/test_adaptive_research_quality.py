from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from persona_continuum.application.persona_creation_service import ResearchPolicy
from persona_continuum.application.research_quality import (
    AdaptiveResearchStopGate,
    InformationGainSnapshot,
    LifeStageModel,
    MarginalInformationGainTracker,
    PrivatePersonaCoverage,
    PrivatePersonaCoveragePolicy,
    ResearchGap,
    ResearchGapAnalyzer,
    SourceIndependenceAnalyzer,
)

ROOT = Path(__file__).resolve().parents[2]


def _source(
    source_id: str,
    url: str,
    content: str,
    *,
    source_type: str = "profile",
    category: str = "news",
    **meta: object,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=source_id,
        path=url,
        title=source_id,
        hash=f"hash-{source_id}",
        content=content,
        source_type=source_type,
        metadata={"canonical_url": url, "category": category, **meta},
    )


def _full_coverage(policy: ResearchPolicy) -> dict[str, object]:
    return {
        "independent_sources": policy.min_unique_sources,
        "source_category_count": policy.min_source_categories,
        "dimensions_complete": True,
        "dimension_coverage": {
            dimension: {"independent_evidence_count": policy.min_sources_per_dimension}
            for dimension in policy.required_dimensions
        },
        "life_stages_complete": True,
        "primary_secondary_balanced": True,
        "contradiction_search_executed": True,
    }


def test_deep_policy_new_defaults() -> None:
    policy = ResearchPolicy.for_profile("deep")
    assert policy.min_unique_sources == 30
    assert policy.preferred_source_target == 60
    assert policy.soft_max_sources == 100
    assert policy.hard_max_sources == 150
    assert policy.min_source_categories == 6
    assert policy.min_sources_per_dimension == 4


def test_exhaustive_policy_defaults() -> None:
    policy = ResearchPolicy.for_profile("exhaustive")
    assert policy.min_unique_sources == 50
    assert policy.preferred_source_target == 100
    assert policy.soft_max_sources == 150
    assert policy.hard_max_sources == 200
    assert policy.min_source_categories == 7
    assert policy.min_sources_per_dimension == 6


def test_old_job_policy_snapshot_compatible() -> None:
    policy = ResearchPolicy.model_validate(
        {
            "profile": "deep",
            "min_unique_sources": 20,
            "preferred_source_target": 30,
            "max_sources": 40,
        }
    )
    assert policy.min_unique_sources == 20
    assert policy.soft_max_sources == 40
    assert policy.hard_max_sources == 40
    assert policy.require_marginal_gain_gate is False
    assert policy.require_life_stage_coverage is False


def test_raw_reposts_do_not_count_as_independent_sources() -> None:
    analyzer = SourceIndependenceAnalyzer()
    sources = [
        _source("a", "https://one.test/story", "The same original interview text."),
        _source("b", "https://two.test/repost", "The same original interview text."),
    ]
    clusters = analyzer.cluster_sources(sources)
    assert len(clusters) == 1
    assert clusters[0].member_source_ids == ["a", "b"]


def test_many_reposts_cannot_game_deep_source_floor() -> None:
    sources = [
        _source(
            f"source-{index}",
            f"https://publisher-{index}.test/repost",
            f"Original wire story {index % 3}",
        )
        for index in range(70)
    ]
    clusters = SourceIndependenceAnalyzer().cluster_sources(sources)
    assert len(sources) == 70
    assert len(clusters) == 3
    assert len(clusters) < ResearchPolicy.for_profile("deep").min_unique_sources


def test_source_cluster_counts_independent_origins() -> None:
    clusters = SourceIndependenceAnalyzer().cluster_sources(
        [
            _source("a", "https://a.test", "a unique source"),
            _source("b", "https://b.test", "b unique source"),
        ]
    )
    assert len(clusters) == 2


def test_deep_requires_all_dimensions() -> None:
    policy = ResearchPolicy.for_profile("deep")
    coverage = _full_coverage(policy)
    coverage["dimensions_complete"] = False
    result = AdaptiveResearchStopGate().evaluate(
        policy=policy, coverage=coverage, source_count=60, marginal_gain=[], high_priority_gaps=[]
    )
    assert result.continue_research


def test_deep_requires_life_stage_coverage() -> None:
    policy = ResearchPolicy.for_profile("deep")
    coverage = _full_coverage(policy)
    coverage["life_stages_complete"] = False
    result = AdaptiveResearchStopGate().evaluate(
        policy=policy, coverage=coverage, source_count=60, marginal_gain=[], high_priority_gaps=[]
    )
    assert result.continue_research


def test_publication_year_not_equal_life_stage() -> None:
    stages = LifeStageModel.model_validate(
        {
            "life_stages": [
                {"id": "early", "title": "Early work", "start": "1970", "end": "1980"},
                {"id": "late", "title": "Late work", "start": "2020", "end": "2030"},
            ]
        }
    )
    source = _source(
        "s",
        "https://example.test/biography",
        "In 1976 the subject began the company.",
        published_at="2024-01-01",
    )
    assert stages.map_source(source) == ["early"]


def test_primary_secondary_count_is_per_source_not_category() -> None:
    analyzer = SourceIndependenceAnalyzer()
    primary = _source(
        "primary",
        "https://primary.test",
        "First person interview",
        source_type="interview",
        category="web",
    )
    secondary = _source(
        "secondary",
        "https://secondary.test",
        "Independent profile",
        source_type="profile",
        category="web",
        author="Reporter",
    )
    clusters = analyzer.cluster_sources([primary, secondary])
    assert len(clusters) == 2
    assert sum(analyzer._is_primary(item) for item in (primary, secondary)) == 1


def test_contradiction_search_required() -> None:
    policy = ResearchPolicy.for_profile("deep")
    coverage = _full_coverage(policy)
    coverage["contradiction_search_executed"] = False
    result = AdaptiveResearchStopGate().evaluate(
        policy=policy, coverage=coverage, source_count=60, marginal_gain=[], high_priority_gaps=[]
    )
    assert result.continue_research


def test_gap_analyzer_finds_missing_dimension() -> None:
    gaps = ResearchGapAnalyzer().analyze(
        sources=[],
        required_dimensions=["expression_dna"],
        min_sources_per_dimension=4,
        dimension_progress={"expression_dna": 1},
    )
    assert gaps[0].type == "dimension"
    assert gaps[0].target == "expression_dna"


def test_gap_analyzer_finds_missing_life_stage() -> None:
    model = LifeStageModel.model_validate(
        {"life_stages": [{"id": "stage_a", "title": "Stage A", "required_evidence": 2}]}
    )
    gaps = ResearchGapAnalyzer().analyze(
        sources=[],
        required_dimensions=[],
        min_sources_per_dimension=1,
        life_stage_model=model,
        life_stage_progress={"stage_a": 0},
        min_evidence_per_life_stage=2,
    )
    assert any(gap.type == "life_stage" for gap in gaps)


def test_low_yield_queries_are_not_repeated() -> None:
    gap = ResearchGap(
        type="dimension", target="expression_dna", suggested_queries=["{name} expression"]
    )
    queries = ResearchGapAnalyzer().build_gap_queries(
        "Subject",
        [gap],
        query_history={
            "Subject expression": {"attempts": 2, "duplicate_rate": 0.9, "information_gain": 0.01}
        },
    )
    assert queries == []


def test_deep_does_not_stop_at_minimum_source_count() -> None:
    policy = ResearchPolicy.for_profile("deep")
    result = AdaptiveResearchStopGate().evaluate(
        policy=policy,
        coverage=_full_coverage(policy),
        source_count=30,
        marginal_gain=[],
        high_priority_gaps=[],
    )
    assert result.continue_research


def test_deep_can_continue_beyond_preferred_target() -> None:
    policy = ResearchPolicy.for_profile("deep")
    gain = [InformationGainSnapshot(round_index=1, marginal_gain_score=0.8)]
    result = AdaptiveResearchStopGate().evaluate(
        policy=policy,
        coverage=_full_coverage(policy),
        source_count=61,
        marginal_gain=gain,
        high_priority_gaps=[],
    )
    assert result.continue_research


def test_deep_stops_after_low_marginal_gain() -> None:
    policy = ResearchPolicy.for_profile("deep")
    tracker = MarginalInformationGainTracker(window=10, threshold=0.08)
    tracker.snapshots = [
        InformationGainSnapshot(round_index=i, marginal_gain_score=0.01) for i in range(20)
    ]
    result = AdaptiveResearchStopGate().evaluate(
        policy=policy,
        coverage=_full_coverage(policy),
        source_count=60,
        marginal_gain=tracker,
        high_priority_gaps=[],
    )
    assert result.passed
    assert result.stop_reason == "coverage_satisfied_and_low_gain"


def test_deep_high_gain_continues() -> None:
    policy = ResearchPolicy.for_profile("deep")
    result = AdaptiveResearchStopGate().evaluate(
        policy=policy,
        coverage=_full_coverage(policy),
        source_count=60,
        marginal_gain=[InformationGainSnapshot(round_index=1, marginal_gain_score=0.9)],
        high_priority_gaps=[],
    )
    assert result.continue_research


def test_soft_max_can_be_exceeded_for_high_priority_gap() -> None:
    policy = ResearchPolicy.for_profile("deep")
    gap = ResearchGap(type="dimension", target="expression_dna", priority=95, severity="high")
    result = AdaptiveResearchStopGate().evaluate(
        policy=policy,
        coverage=_full_coverage(policy),
        source_count=101,
        marginal_gain=[],
        high_priority_gaps=[gap],
    )
    assert result.continue_research


def test_hard_max_returns_completed_with_gaps() -> None:
    policy = ResearchPolicy.for_profile("deep")
    coverage = _full_coverage(policy)
    coverage["dimensions_complete"] = False
    result = AdaptiveResearchStopGate().evaluate(
        policy=policy, coverage=coverage, source_count=150, marginal_gain=[], high_priority_gaps=[]
    )
    assert result.completed_with_gaps
    assert result.stop_reason == "hard_budget_exhausted"


def test_sparse_person_does_not_loop_forever() -> None:
    policy = ResearchPolicy.for_profile("deep")
    result = AdaptiveResearchStopGate().evaluate(
        policy=policy,
        coverage={"dimensions_complete": False},
        source_count=3,
        marginal_gain=[],
        high_priority_gaps=[],
        source_space_exhausted=True,
    )
    assert not result.continue_research
    assert result.completed_with_gaps


def test_rich_person_adaptive_target_expands() -> None:
    policy = ResearchPolicy.for_profile("deep")
    assert policy.effective_target("very_rich") > policy.preferred_source_target


def test_private_persona_does_not_use_public_source_threshold() -> None:
    coverage = PrivatePersonaCoverage(
        conversation_message_count=120, dimension_coverage={"expression_dna": 1}
    )
    assert coverage.conversation_message_count == 120
    assert not hasattr(coverage, "min_unique_sources")


def test_large_chat_export_counts_information_not_file_count(app) -> None:
    policy = PrivatePersonaCoveragePolicy(message_volume_target=100)
    source = SimpleNamespace(
        content="2019-01-01\n2021-01-01\nUser: 当时面对压力，因为我们决定改变计划。",
        metadata={
            "message_count": 10_000,
            "context": "long-term chat",
            "relationship": "friend",
            "behavioral_episode_count": 12,
        },
    )
    job = SimpleNamespace(
        job_config={},
        dimension_progress={"expression_dna": 4},
        research_policy=ResearchPolicy.for_profile("deep"),
    )
    coverage = app.persona_creation._private_coverage([source], job)
    assert coverage.conversation_message_count == 10_000
    assert coverage.conversation_message_count >= policy.message_volume_target
    assert "message_volume" not in coverage.high_priority_gaps


def test_ui_no_longer_says_20_sources() -> None:
    html = (ROOT / "src/persona_continuum/web/static/index.html").read_text(encoding="utf-8")
    assert "至少 20" not in html
    assert "Exhaustive" in html


def test_progress_reports_independent_source_count() -> None:
    js = (ROOT / "src/persona_continuum/web/static/app.js").read_text(encoding="utf-8")
    assert "Independent:" in js


def test_progress_reports_stop_reason() -> None:
    js = (ROOT / "src/persona_continuum/web/static/app.js").read_text(encoding="utf-8")
    assert "Stop reason:" in js
