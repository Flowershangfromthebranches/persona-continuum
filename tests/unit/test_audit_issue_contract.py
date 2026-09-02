"""Final Audit issue data contract + retry classification (P0-2).

The audit is model output, so its shape is untrusted.  The historical bug::

    issue.get("dimension") in {dimension, None, "_global", "cross_cutting"}

raised ``TypeError: unhashable type: 'list'`` whenever the model returned a
cross-dimension finding as a list, killing the whole Audit Repair pass.  The
normalizer owns the contract now; these tests pin it.
"""

from __future__ import annotations

import pytest

from persona_continuum.application.audit_issues import (
    audit_issue_is_global,
    audit_issue_matches_dimension,
    normalize_audit_issue_dimensions,
)
from persona_continuum.application.job_progress import (
    PersonaFailureCode,
    classify_persona_failure,
    failure_code_for,
    is_retryable_failure,
    is_retryable_failure_code,
)

# ---------------------------------------------------------------------------
# Dimension normalization
# ---------------------------------------------------------------------------


def test_legacy_single_string_dimension() -> None:
    issue = {"dimension": "identity_and_timeline"}
    assert normalize_audit_issue_dimensions(issue) == ["identity_and_timeline"]


def test_legacy_list_dimension_does_not_raise() -> None:
    # The exact payload shape that historically killed Audit Repair with
    # "unhashable type: 'list'".
    issue = {"dimension": ["identity_and_timeline", "works_and_views"]}
    assert normalize_audit_issue_dimensions(issue) == [
        "identity_and_timeline",
        "works_and_views",
    ]


def test_canonical_dimensions_key() -> None:
    issue = {"dimensions": ["identity_and_timeline"]}
    assert normalize_audit_issue_dimensions(issue) == ["identity_and_timeline"]


@pytest.mark.parametrize("raw", [None, [], (), set(), {}, "", "   "])
def test_missing_or_empty_dimension_is_global(raw: object) -> None:
    issue = {"dimension": raw} if not isinstance(raw, dict) else raw
    assert normalize_audit_issue_dimensions(issue) == []
    assert audit_issue_is_global(issue) is True


def test_tuple_and_set_shapes_flatten() -> None:
    assert normalize_audit_issue_dimensions({"dimension": ("a", "b")}) == ["a", "b"]
    assert normalize_audit_issue_dimensions({"dimensions": {"a", "b"}}) in (
        ["a", "b"],
        ["b", "a"],
    )


def test_nested_junk_is_flattened_deduplicated() -> None:
    issue = {"dimension": ["a", ["a", "b"], None, "", {"name": "c"}]}
    assert normalize_audit_issue_dimensions(issue) == ["a", "b", "c"]


def test_non_dict_issue_is_ignored_not_global() -> None:
    # Non-dict junk cannot be interpreted; it is dropped rather than
    # broadcast.  A dict issue with NO dimension IS global.
    assert normalize_audit_issue_dimensions("junk") == []
    assert audit_issue_is_global("junk") is False
    assert audit_issue_is_global(None) is False
    assert audit_issue_is_global({"severity": "high"}) is True


@pytest.mark.parametrize("marker", ["_global", "global", "cross_cutting", "cross-cutting", "*"])
def test_global_markers_broadcast(marker: str) -> None:
    issue = {"dimension": marker}
    assert audit_issue_is_global(issue) is True
    assert audit_issue_matches_dimension(issue, "identity_and_timeline") is True


# ---------------------------------------------------------------------------
# Repair matching
# ---------------------------------------------------------------------------


def test_cross_dimension_finding_reaches_every_named_dimension() -> None:
    issue = {"dimension": ["identity_and_timeline", "works_and_views"]}
    assert audit_issue_matches_dimension(issue, "identity_and_timeline") is True
    assert audit_issue_matches_dimension(issue, "works_and_views") is True
    assert audit_issue_matches_dimension(issue, "social_web") is False


def test_string_finding_matches_only_its_dimension() -> None:
    issue = {"dimension": "identity_and_timeline"}
    assert audit_issue_matches_dimension(issue, "identity_and_timeline") is True
    assert audit_issue_matches_dimension(issue, "works_and_views") is False


def test_matching_never_raises_on_untrusted_shapes() -> None:
    # Whatever the model emits, matching must never raise TypeError.
    for issue in (
        {"dimension": ["a", "b"]},
        {"dimension": {"nested": ["a"]}},
        {"dimensions": None},
        {"severity": "high"},
        42,
    ):
        assert isinstance(audit_issue_matches_dimension(issue, "a"), bool)


def test_final_audit_schema_declares_dimensions_list() -> None:
    # Structured schema and Python consumer must agree (§23).
    from persona_continuum.application.persona_creation_service import FINAL_AUDIT_ISSUE_SCHEMA

    dimensions = FINAL_AUDIT_ISSUE_SCHEMA["properties"]["dimensions"]
    assert dimensions["type"] == "array"
    assert dimensions["items"] == {"type": "string"}


# ---------------------------------------------------------------------------
# Failure taxonomy / retryability
# ---------------------------------------------------------------------------


def test_audit_repair_failed_is_retryable() -> None:
    assert is_retryable_failure_code("AUDIT_REPAIR_FAILED") is True
    assert is_retryable_failure_code("FINAL_AUDIT_FAILED") is True
    assert is_retryable_failure_code("FINAL_QUALITY_GATE_FAILED") is True
    assert is_retryable_failure_code("MODEL_OUTPUT_INVALID") is True
    assert is_retryable_failure_code("TIMEOUT") is True


def test_classify_maps_phase_prefixes_to_typed_codes() -> None:
    assert classify_persona_failure(
        Exception("audit_repair_failed:identity_and_timeline:boom")
    ) == PersonaFailureCode.AUDIT_REPAIR_FAILED.value
    assert classify_persona_failure(
        Exception("final_quality_gate_failed:identity_and_timeline")
    ) == PersonaFailureCode.FINAL_QUALITY_GATE_FAILED.value


def test_legacy_failed_row_without_code_stays_retryable() -> None:
    # Rows written before the typed taxonomy: retriable=False in the payload
    # must NOT make an audit failure permanent.
    legacy = {
        "message": "audit_repair_failed:identity_and_timeline:unhashable type: 'list'",
        "retriable": False,
    }
    assert is_retryable_failure(legacy) is True


def test_typed_failure_row_is_retryable_even_if_retriable_false() -> None:
    failure = {
        "code": "AUDIT_REPAIR_FAILED",
        "message": "audit_repair_failed:works_and_views:boom",
        "retriable": False,
    }
    assert is_retryable_failure(failure) is True


def test_non_retryable_generic_error_stays_blocked() -> None:
    failure = {"code": "PERSONA_CREATION_ERROR", "message": "something exploded"}
    assert is_retryable_failure(failure) is False


def test_failure_code_for_legacy_message() -> None:
    assert (
        failure_code_for({"message": "audit_repair_failed:x:y"})
        == PersonaFailureCode.AUDIT_REPAIR_FAILED.value
    )
