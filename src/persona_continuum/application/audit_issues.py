"""Data contract for Final Audit issues.

The audit is produced by a model, so its shape is untrusted.  Historically the
repair path did::

    issue.get("dimension") in {dimension, None, "_global", "cross_cutting"}

When a model returned a *cross-dimension* finding as
``{"dimension": ["identity_and_timeline", "works_and_views"]}``, Python tried
to hash a list and raised ``TypeError: unhashable type: 'list'``.  That killed
the whole repair pass and surfaced as ``audit_repair_failed``.

This module owns the normalization so no downstream code has to guess whether a
finding is a string, a list, a tuple, a set, or absent.  The canonical shape is
always ``dimensions: list[str]``.
"""

from __future__ import annotations

from typing import Any

# Finding markers that mean "this applies to every dimension".
GLOBAL_DIMENSION_MARKERS = frozenset(
    {
        "_global",
        "global",
        "cross_cutting",
        "cross-cutting",
        "all",
        "*",
        "__all__",
    }
)

# Legacy single-dimension key plus the canonical list key.
DIMENSION_KEYS = ("dimensions", "dimension")


def _coerce_dimension(value: Any) -> list[str]:
    """Flatten one dimension value into a list of non-empty strings."""

    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set, frozenset)):
        results: list[str] = []
        for item in value:
            results.extend(_coerce_dimension(item))
        return results
    if isinstance(value, dict):
        # Some models emit {"dimension": {"name": "x"}}.
        for key in ("name", "id", "dimension", "value"):
            if key in value:
                return _coerce_dimension(value[key])
        return []
    text = str(value).strip()
    return [text] if text else []


def normalize_audit_issue_dimensions(issue: Any) -> list[str]:
    """Return the dimensions an audit issue applies to, always as ``list[str]``.

    Accepts every shape seen in the wild (and in persisted legacy
    checkpoints):

    - ``None`` / missing  -> ``[]`` (treated as a global finding)
    - ``"identity_and_timeline"``
    - ``["identity_and_timeline", "works_and_views"]``
    - ``("a", "b")`` / ``{"a"}``
    - ``{"dimension": ...}`` (legacy key)
    - ``{"dimensions": [...]}`` (canonical key)
    - nested junk: silently flattened and de-duplicated
    """

    if not isinstance(issue, dict):
        return []
    collected: list[str] = []
    for key in DIMENSION_KEYS:
        if key in issue:
            collected.extend(_coerce_dimension(issue[key]))
    # Deterministic, order-preserving de-duplication.
    seen: set[str] = set()
    ordered: list[str] = []
    for item in collected:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def audit_issue_is_global(issue: Any) -> bool:
    """Whether a finding must be broadcast to every dimension."""

    if not isinstance(issue, dict):
        return False
    dimensions = normalize_audit_issue_dimensions(issue)
    if not dimensions:
        # No dimension at all is a global/cross-cutting finding by contract.
        return True
    return any(str(item).strip().casefold() in GLOBAL_DIMENSION_MARKERS for item in dimensions)


def audit_issue_matches_dimension(issue: Any, dimension: str) -> bool:
    """Whether one finding must reach ``dimension``.

    A cross-dimension finding reaches *every* dimension it names; a
    ``_global``/``cross_cutting`` finding reaches all of them.
    """

    if not isinstance(issue, dict):
        return False
    target = str(dimension or "").strip()
    if not target:
        return False
    if audit_issue_is_global(issue):
        return True
    target_key = target.casefold()
    return any(
        str(item).strip().casefold() == target_key
        for item in normalize_audit_issue_dimensions(issue)
    )


def issues_for_dimension(issues: Any, dimension: str) -> list[dict[str, Any]]:
    """Every finding that must be shown to ``dimension``'s repair pass."""

    if not isinstance(issues, list):
        return []
    return [
        issue
        for issue in issues
        if isinstance(issue, dict) and audit_issue_matches_dimension(issue, dimension)
    ]


def normalize_audit_issue(issue: Any) -> dict[str, Any]:
    """Return a copy of one issue with a canonical ``dimensions`` field.

    The legacy ``dimension`` key is preserved so persisted checkpoints and the
    UI keep reading the original payload, but downstream code only ever has to
    consume ``dimensions``.
    """

    if not isinstance(issue, dict):
        return {}
    normalized = dict(issue)
    normalized["dimensions"] = normalize_audit_issue_dimensions(issue)
    return normalized


__all__ = [
    "DIMENSION_KEYS",
    "GLOBAL_DIMENSION_MARKERS",
    "audit_issue_is_global",
    "audit_issue_matches_dimension",
    "issues_for_dimension",
    "normalize_audit_issue",
    "normalize_audit_issue_dimensions",
]
