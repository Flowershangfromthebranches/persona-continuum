"""Hierarchy rules: levels, content bounds, grounding, identity (pure logic).

Store-level acceptance (grouping, boundaries, provenance, deletion, restart)
lives in ``tests/integration/test_hierarchical_summaries.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from persona_continuum.application.hierarchy_service import (
    HierarchyService,
    _lexical_tokens,
    _overlap_ratio,
)
from persona_continuum.domain.hierarchical_summary import (
    HIERARCHY_CONSOLIDATION_VERSION,
    HierarchicalSummary,
    SummaryContent,
    SummaryReadiness,
    SummarySourceType,
    SummaryStatus,
    SummaryType,
    source_type_for_level,
    summary_range_hash,
    summary_type_for_level,
)


class _Database:
    """Grounding and parsing never touch the database."""

    class _Conn:
        def execute(self, *args, **kwargs):  # pragma: no cover - unused
            raise AssertionError("pure rules must not touch the database")

        def commit(self) -> None:  # pragma: no cover - unused
            raise AssertionError("pure rules must not commit")

    conn = _Conn()


def _service() -> HierarchyService:
    return HierarchyService(_Database(), object())


def _corpus(text: str, **overrides: object) -> dict[str, object]:
    corpus: dict[str, object] = {
        "text": text,
        "tokens": _lexical_tokens(text),
        "date_terms": ["2026-09-03", "2026年9月", "9月3日"],
        "episode_ids": ["episode_a"],
        "summary_ids": ["summary_a"],
        "fact_ids": ["fact_a"],
        "thread_ids": ["thread_a"],
    }
    corpus.update(overrides)
    return corpus


# --- levels -----------------------------------------------------------------


def test_levels_map_to_types_and_source_kinds() -> None:
    assert summary_type_for_level(1) is SummaryType.CHAPTER
    assert summary_type_for_level(2) is SummaryType.LONG_TERM
    # Nothing caps history at two layers.
    assert summary_type_for_level(3) is SummaryType.LONG_TERM
    assert summary_type_for_level(7) is SummaryType.LONG_TERM
    assert source_type_for_level(1) is SummarySourceType.EPISODE
    assert source_type_for_level(2) is SummarySourceType.SUMMARY
    assert source_type_for_level(5) is SummarySourceType.SUMMARY


def test_status_and_readiness_degrade_safely() -> None:
    assert SummaryStatus.from_raw("closed") is SummaryStatus.CLOSED
    assert SummaryStatus.from_raw("nonsense") is SummaryStatus.OPEN
    assert SummaryReadiness.from_raw("provisional") is SummaryReadiness.PROVISIONAL
    assert SummaryReadiness.from_raw("nonsense") is SummaryReadiness.PENDING


def test_provisional_is_not_final_history() -> None:
    base = {
        "id": "summary_1",
        "persona_id": "su_he",
        "counterpart_id": "user",
    }
    provisional = HierarchicalSummary(
        **base, status=SummaryStatus.OPEN, summary_status=SummaryReadiness.PROVISIONAL
    )
    final = HierarchicalSummary(
        **base, status=SummaryStatus.CLOSED, summary_status=SummaryReadiness.READY
    )
    assert provisional.is_provisional and provisional.needs_consolidation is False
    assert final.is_ready and final.needs_consolidation is False
    # A missing or failed text is owed work; a provisional one is current.
    assert (
        HierarchicalSummary(**base, summary_status=SummaryReadiness.FAILED).needs_consolidation
        is True
    )
    assert (
        HierarchicalSummary(**base, summary_status=SummaryReadiness.PENDING).needs_consolidation
        is True
    )
    assert provisional.consolidation_version == HIERARCHY_CONSOLIDATION_VERSION


def test_time_range_label_is_honest_about_an_open_range() -> None:
    summary = HierarchicalSummary(
        id="summary_1",
        persona_id="su_he",
        counterpart_id="user",
        started_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        ended_at=datetime(2026, 9, 28, 10, 0, tzinfo=UTC),
    )
    assert summary.time_range_label() == "2026-09-01 ~ 2026-09-28"
    summary.ended_at = None
    assert summary.time_range_label().endswith("open")


# --- identity ---------------------------------------------------------------


def test_range_hash_is_stable_and_range_sensitive() -> None:
    first = summary_range_hash(
        persona_id="su_he",
        counterpart_id="user",
        branch_id="main",
        level=1,
        source_ids=["e1", "e2"],
    )
    assert first == summary_range_hash(
        persona_id="su_he",
        counterpart_id="user",
        branch_id="main",
        level=1,
        source_ids=["e1", "e2"],
    )
    # A different range, level, or scope is a different summary.
    assert first != summary_range_hash(
        persona_id="su_he",
        counterpart_id="user",
        branch_id="main",
        level=1,
        source_ids=["e1", "e2", "e3"],
    )
    assert first != summary_range_hash(
        persona_id="su_he",
        counterpart_id="user",
        branch_id="main",
        level=2,
        source_ids=["e1", "e2"],
    )
    assert first != summary_range_hash(
        persona_id="su_he",
        counterpart_id="other",
        branch_id="main",
        level=1,
        source_ids=["e1", "e2"],
    )


# --- content ----------------------------------------------------------------


def test_content_bounds_lists_and_text() -> None:
    content = SummaryContent.model_validate(
        {
            "title": "标" * 400,
            "summary": "文" * 20000,
            "major_events": ["事件", "", None, "事件", *[f"e{index}" for index in range(40)]],
            "key_entities": [f"实体{index}" for index in range(80)],
            "importance": 7.5,
            "unknown_field": "dropped",
        }
    )
    assert len(content.title) <= 120
    assert len(content.summary) <= 8000
    assert len(content.major_events) <= 24
    assert len(content.key_entities) <= 32
    assert content.major_events[0] == "事件"
    assert content.importance == 0.5
    assert not hasattr(content, "unknown_field")


def test_grounded_fields_exclude_inferences() -> None:
    content = SummaryContent(
        summary="一段总结",
        major_events=["事件 A"],
        unresolved_topics=["话题"],
        inferences=["我的推断"],
    )
    fields = dict(content.grounded_fields())
    assert "我的推断" not in fields.values()
    assert {"事件 A", "话题", "一段总结"} <= set(fields.values())


def test_parse_content_never_trusts_raw_json() -> None:
    assert HierarchyService.parse_content({"title": "t", "summary": "s"}) is not None
    assert HierarchyService.parse_content("nope") is None
    assert HierarchyService.parse_content(["nope"]) is None
    assert HierarchyService.parse_content(SummaryContent(title="t")) is not None


# --- grounding --------------------------------------------------------------


def test_grounded_summary_passes() -> None:
    service = _service()
    corpus = _corpus("用户开始准备考研。数学复习遇到困难。")
    content = SummaryContent(
        title="考研准备",
        summary="这一阶段用户开始准备考研。数学复习遇到困难。",
        major_events=["用户开始准备考研"],
        key_entities=["考研", "数学"],
    )
    result = service.validate_grounding(content, corpus)
    assert result["ok"] is True
    assert result["failures"] == []


def test_hallucinated_entity_number_and_event_are_rejected() -> None:
    service = _service()
    corpus = _corpus("用户开始准备考研。")
    content = SummaryContent(
        title="在柏林生活",
        summary="住户在柏林工作了 2016 年。",
        major_events=["在柏林买下公寓"],
        key_entities=["柏林"],
    )
    result = service.validate_grounding(content, corpus)
    assert result["ok"] is False
    kinds = {item["kind"] for item in result["failures"]}
    assert "ungrounded_entity" in kinds
    assert "ungrounded_number" in kinds
    assert "ungrounded_statement" in kinds
    assert all(item["kind"] != "weak_grounding" for item in result["failures"])


def test_single_digits_are_not_treated_as_dates() -> None:
    """One digit is usually formatting, not a fabricated number."""

    service = _service()
    corpus = _corpus("用户今天做了三套题。", date_terms=[])
    content = SummaryContent(title="刷题", summary="用户今天做了 3 套题。")
    assert service.validate_grounding(content, corpus)["ok"] is True


def test_dates_are_grounded_through_the_corpus_date_terms() -> None:
    service = _service()
    corpus = _corpus("用户准备考研。", date_terms=["2026-09-03", "2026年9月"])
    content = SummaryContent(
        title="备考阶段",
        summary="这一阶段（2026年9月）用户准备考研。",
    )
    assert service.validate_grounding(content, corpus)["ok"] is True


def test_citations_must_exist_in_the_source_set() -> None:
    service = _service()
    corpus = _corpus("用户准备考研。")
    good = SummaryContent(title="t", summary="用户准备考研。", major_events=["prepare"])
    good.major_events = []
    assert service.validate_grounding(good, corpus)["ok"] is True

    bad = SummaryContent(title="t", summary="用户准备考研。", ongoing_threads=["thread_missing"])
    result = service.validate_grounding(bad, corpus)
    assert result["ok"] is False
    assert any(item["kind"] == "unknown_source_id" for item in result["failures"])


def test_ids_are_not_mistaken_for_numbers() -> None:
    """A cited thread id must not fail the number check on its hex tail."""

    service = _service()
    corpus = _corpus("用户和小陈吵架了。")
    corpus["thread_ids"] = ["thread_3fee7819b1b84cfd"]
    content = SummaryContent(
        title="冲突",
        summary="用户和小陈吵架了。",
        resolved_threads=["小陈冲突（thread_3fee7819b1b84cfd）已经和解"],
    )
    result = service.validate_grounding(content, corpus)
    assert result["ok"] is True, result["failures"]


def test_fact_validity_window_is_grounded() -> None:
    """A fact's dates are part of the payload, so they must be quotable."""

    service = _service()
    facts_corpus = HierarchyService._facts_corpus(
        [
            {
                "display_text": "用户和小陈已和好",
                "status": "active",
                "plan_status": "not_applicable",
                "valid_from": "2026-10-27T10:00:00+00:00",
                "valid_until": "2026-10-28T10:00:00+00:00",
            }
        ]
    )
    corpus = _corpus("用户和小陈吵架了。", date_terms=[])
    corpus["text"] = corpus["text"] + " " + " ".join(facts_corpus)
    corpus["tokens"] = _lexical_tokens(str(corpus["text"]))
    content = SummaryContent(
        title="冲突与和解",
        summary="这一阶段用户和小陈吵架后已和好。",
        important_preferences_or_fact_changes=["「已和好」为持续事实，短时事实于 2026-10-28 到期"],
    )
    result = service.validate_grounding(content, corpus)
    assert result["ok"] is True, result["failures"]


def test_ids_seen_inside_the_sources_are_citable() -> None:
    """A summary may quote an id that its sources themselves show."""

    service = _service()
    corpus = _corpus("这一阶段用户与小陈争吵后和解。")
    corpus["summary_ids"] = ["summary_chapter"]
    corpus["cited_ids"] = ["thread_3fee7819b1b84cfd"]
    content = SummaryContent(
        title="跨阶段",
        summary="冲突与和解的过程见 thread_3fee7819b1b84cfd。",
    )
    assert service.validate_grounding(content, corpus)["ok"] is True
    # An id that appears nowhere is still refused.
    content.summary = "冲突与和解的过程见 thread_deadbeef00000000。"
    refused = service.validate_grounding(content, corpus)
    assert refused["ok"] is False
    assert refused["failures"][0]["kind"] == "unknown_source_id"


def test_thin_paraphrase_is_warned_not_failed() -> None:
    service = _service()
    corpus = _corpus("用户开始准备考研。")
    content = SummaryContent(
        title="备考",
        summary="用户开始准备考研。",
        unresolved_topics=["准备了三天，还没决定报不报班"],
    )
    result = service.validate_grounding(content, corpus)
    # A thin paraphrase shares some vocabulary: flagged, not fatal.
    assert result["ok"] is True
    assert result["warnings"] and result["warnings"][0]["kind"] == "weak_grounding"

    # A statement with NO vocabulary in common with its sources is fatal.
    content.unresolved_topics = ["关于柏林的居住安排"]
    assert service.validate_grounding(content, corpus)["ok"] is False


def test_overlap_ratio_is_containment_friendly() -> None:
    corpus = _lexical_tokens("用户计划去重庆旅行。")
    assert _overlap_ratio("用户计划去重庆旅行", corpus) == 1.0
    assert _overlap_ratio("完全没有关系的一句话", corpus) == 0.0


@pytest.mark.parametrize("field", ["major_events", "relationship_changes", "resolved_threads"])
def test_every_grounded_list_is_checked(field: str) -> None:
    service = _service()
    corpus = _corpus("用户准备考研。")
    content = SummaryContent(title="t", summary="")
    setattr(content, field, ["一个来源里完全没有的说法"])
    assert service.validate_grounding(content, corpus)["ok"] is False
