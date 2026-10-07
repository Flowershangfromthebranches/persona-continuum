"""Semantic Fact rules: identity, validation, bounds (pure logic)."""

from __future__ import annotations

import pytest

from persona_continuum.domain.semantic_fact import (
    FACT_EXTRACTION_VERSION,
    FactCandidate,
    FactCategory,
    FactDurability,
    FactExtractionPayload,
    FactOrigin,
    FactRelation,
    FactStatus,
    PlanStatus,
    SemanticFact,
    canonical_fact_key,
    canonical_value_key,
)


def _candidate(**overrides) -> dict:
    payload = {
        "category": "preference",
        "subject": "用户",
        "predicate": "最喜欢的饮品",
        "value": "茉莉奶绿",
        "origin": "user_asserted",
        "confidence": 0.8,
    }
    payload.update(overrides)
    return payload


def test_fact_key_ignores_case_and_spacing() -> None:
    first = canonical_fact_key(FactCategory.PREFERENCE, " 用户 ", "最喜欢的饮品")
    second = canonical_fact_key("preference", "用户", "最喜欢的饮品 ")
    assert first == second
    assert first == "preference|用户|最喜欢的饮品"
    # Category is part of the identity: the same words in another category are
    # a different fact.
    assert canonical_fact_key("habit", "用户", "最喜欢的饮品") != first


def test_value_key_is_canonical() -> None:
    assert canonical_value_key({"text": " 美式 "}) == canonical_value_key({"text": "美式"})
    assert canonical_value_key({"text": "美式"}) != canonical_value_key({"text": "茉莉奶绿"})
    # A structured value keeps its keys, order-independent.
    assert canonical_value_key({"a": 1, "b": 2}) == canonical_value_key({"b": 2, "a": 1})


def test_candidate_normalises_enums_and_text() -> None:
    candidate = FactCandidate.model_validate(
        _candidate(
            category="PREFERENCE",
            origin="USER_ASSERTED",
            durability="temporary",
            relation="SUPERSEDES",
            plan_status="planned",
            subject="  用户  ",
            value="美式",
            confidence=1.4,
            unknown_field="dropped",
        )
    )
    assert candidate.category is FactCategory.PREFERENCE
    assert candidate.origin is FactOrigin.USER_ASSERTED
    assert candidate.durability is FactDurability.TEMPORARY
    assert candidate.relation is FactRelation.SUPERSEDES
    assert candidate.plan_status is PlanStatus.PLANNED
    assert candidate.subject == "用户"
    assert candidate.confidence == 0.5  # out of range -> default, never 1.4
    assert "unknown_field" not in candidate.model_dump()


def test_unknown_enum_values_degrade_safely() -> None:
    candidate = FactCandidate.model_validate(
        _candidate(category="nonsense", origin="nonsense", relation="nonsense")
    )
    assert candidate.category is FactCategory.OTHER
    # An unrecognised origin must not become "the user said it".
    assert candidate.origin is FactOrigin.INFERRED
    assert candidate.relation is FactRelation.UNRELATED


def test_candidate_is_unusable_without_subject_predicate_value() -> None:
    assert FactCandidate.model_validate(_candidate()).is_usable
    assert not FactCandidate.model_validate(_candidate(value="")).is_usable
    assert not FactCandidate.model_validate(_candidate(predicate="")).is_usable
    assert not FactCandidate.model_validate(_candidate(subject="")).is_usable


def test_display_text_falls_back_to_a_structured_rendering() -> None:
    candidate = FactCandidate.model_validate(_candidate())
    assert candidate.display_text == ""
    assert candidate.display() == "用户 最喜欢的饮品 = 茉莉奶绿"
    explicit = FactCandidate.model_validate(_candidate(display_text="用户最爱茉莉奶绿"))
    assert explicit.display() == "用户最爱茉莉奶绿"


def test_payload_requires_a_facts_key_and_bounds_the_list() -> None:
    assert FactExtractionPayload.model_validate({"facts": []}).facts == []
    assert FactExtractionPayload.model_validate({"nope": 1}).facts == []
    bounded = FactExtractionPayload.model_validate(
        {"facts": [_candidate(value=f"值{i}") for i in range(200)]}
    )
    assert len(bounded.facts) <= 32
    assert len(bounded.usable()) == len(bounded.facts)


def test_payload_drops_junk_entries_and_keeps_the_good_ones() -> None:
    payload = FactExtractionPayload.model_validate(
        {"facts": ["不是对象", None, 42, _candidate()]}
    )
    # One malformed entry must not cost the caller the valid ones next to it.
    assert len(payload.facts) == 1
    assert payload.facts[0].value == "茉莉奶绿"


def test_payload_rejects_a_non_list_facts_value() -> None:
    from persona_continuum.application.fact_service import SemanticFactService

    assert SemanticFactService.parse_extraction({"facts": "不是数组"}) is None
    assert SemanticFactService.parse_extraction("不是对象") is None
    assert SemanticFactService.parse_extraction({"nope": 1}) is None
    # A legitimate "nothing to remember" answer is not a failure.
    empty = SemanticFactService.parse_extraction({"facts": []})
    assert empty is not None and empty.facts == []
    # Claiming facts but yielding none usable IS a failure.
    assert SemanticFactService.parse_extraction({"facts": ["垃圾"]}) is None
    # One good entry among junk survives.
    good = SemanticFactService.parse_extraction({"facts": ["垃圾", _candidate()]})
    assert good is not None and len(good.facts) == 1


def test_candidate_value_json_shapes() -> None:
    assert FactCandidate.model_validate(_candidate()).value_json == {"text": "茉莉奶绿"}
    assert FactCandidate.model_validate(_candidate(value="")).value_json == {}


def test_default_extraction_version_is_stamped() -> None:
    fact = SemanticFact(
        id="f1", persona_id="p", counterpart_id="c", subject="用户",
        predicate="最喜欢", value_json={"text": "美式"},
    )
    assert fact.extraction_version == FACT_EXTRACTION_VERSION
    assert fact.status is FactStatus.ACTIVE
    assert fact.is_active
    assert fact.is_valid_now


def test_validity_window_is_respected() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    expired = SemanticFact(
        id="f2", persona_id="p", counterpart_id="c", subject="用户", predicate="喜欢",
        value_json={"text": "旧"}, valid_until=now - timedelta(hours=1),
    )
    assert not expired.is_valid_now
    future = SemanticFact(
        id="f3", persona_id="p", counterpart_id="c", subject="用户", predicate="喜欢",
        value_json={"text": "新"}, valid_from=now + timedelta(hours=1),
    )
    assert not future.is_valid_now
    superseded = SemanticFact(
        id="f4", persona_id="p", counterpart_id="c", subject="用户", predicate="喜欢",
        value_json={"text": "旧"}, status=FactStatus.SUPERSEDED,
    )
    assert not superseded.is_valid_now


@pytest.mark.parametrize(
    "category",
    [
        "identity",
        "preference",
        "plan",
        "project",
        "relation",
        "habit",
        "possession",
        "location",
        "goal",
        "commitment",
        "other",
    ],
)
def test_required_categories_exist(category: str) -> None:
    assert FactCategory.from_raw(category).value == category
