"""Small, fail-closed numeric boundaries shared by runtime and model inputs.

Numbers in Persona Continuum can arrive from LLM JSON, persisted snapshots,
HTTP payloads, or CLI metadata.  This module keeps those boundaries explicit so
that a nullable optional field never becomes a raw ``float(None)`` traceback,
while genuinely required numeric fields still produce a typed failure.
"""

from __future__ import annotations

from math import isfinite
from typing import Any

NULL_NUMERIC_VALUES = {"", "null", "none", "nil", "n/a", "na"}
DEFAULT_TIMEOUT_SECONDS = 120.0
MIN_TIMEOUT_SECONDS = 5.0
MAX_TIMEOUT_SECONDS = 3600.0
DEFAULT_ACP_STREAM_LIMIT_BYTES = 16 * 1024 * 1024
MIN_ACP_STREAM_LIMIT_BYTES = 1 * 1024 * 1024
MAX_ACP_STREAM_LIMIT_BYTES = 32 * 1024 * 1024


class InvalidNumericFieldError(ValueError):
    """A required numeric field could not be recovered safely."""

    code = "INVALID_NUMERIC_FIELD"

    def __init__(self, field: str, value: Any, *, phase: str | None = None) -> None:
        self.field = str(field)
        self.received_type = type(value).__name__
        self.phase = phase
        super().__init__(f"{self.code}:{self.field}")

    def as_failure(self, *, phase: str | None = None) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "phase": phase or self.phase,
            "field": self.field,
            "received_type": self.received_type,
            "retriable": False,
        }


def _is_null(value: Any) -> bool:
    return value is None or (
        isinstance(value, str) and value.strip().casefold() in NULL_NUMERIC_VALUES
    )


def _invalid[T](
    value: Any,
    *,
    default: T,
    field: str | None,
    required: bool,
    phase: str | None,
) -> T:
    if required:
        raise InvalidNumericFieldError(field or "numeric", value, phase=phase)
    return default


def safe_float(
    value: Any,
    default: float | None = 0.0,
    minimum: float | None = None,
    maximum: float | None = None,
    *,
    field: str | None = None,
    required: bool = False,
    phase: str | None = None,
) -> float | None:
    """Convert a finite number without allowing nullable/invalid input to leak.

    ``required=True`` is reserved for fields whose absence makes the record
    unusable.  Optional fields return ``default`` for null, non-finite, or
    out-of-range values.
    """

    if _is_null(value):
        return _invalid(
            value, default=default, field=field, required=required, phase=phase
        )
    if isinstance(value, bool):
        return _invalid(
            value, default=default, field=field, required=required, phase=phase
        )
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError):
        return _invalid(
            value, default=default, field=field, required=required, phase=phase
        )
    if not isfinite(converted):
        return _invalid(
            value, default=default, field=field, required=required, phase=phase
        )
    if minimum is not None and converted < minimum:
        return _invalid(
            value, default=default, field=field, required=required, phase=phase
        )
    if maximum is not None and converted > maximum:
        return _invalid(
            value, default=default, field=field, required=required, phase=phase
        )
    return converted


def safe_int(
    value: Any,
    default: int | None = 0,
    minimum: int | None = None,
    maximum: int | None = None,
    *,
    field: str | None = None,
    required: bool = False,
    phase: str | None = None,
) -> int | None:
    """Convert an integral numeric value through the same finite checks."""

    converted = safe_float(
        value,
        default=None,
        minimum=float(minimum) if minimum is not None else None,
        maximum=float(maximum) if maximum is not None else None,
        field=field,
        required=required,
        phase=phase,
    )
    if converted is None:
        return _invalid(value, default=default, field=field, required=required, phase=phase)
    if not converted.is_integer():
        return _invalid(value, default=default, field=field, required=required, phase=phase)
    return int(converted)


def safe_probability(
    value: Any,
    default: float | None = 0.0,
    *,
    field: str | None = None,
    required: bool = False,
    phase: str | None = None,
) -> float | None:
    """Normalize a probability-like value to the closed interval [0, 1]."""

    return safe_float(
        value,
        default=default,
        minimum=0.0,
        maximum=1.0,
        field=field,
        required=required,
        phase=phase,
    )


def safe_timeout(value: Any, default: float = DEFAULT_TIMEOUT_SECONDS) -> float:
    """Return a bounded per-turn timeout; null means the documented default."""

    converted = safe_float(value, default=None, field="turn_timeout_seconds")
    fallback = safe_float(
        default,
        default=DEFAULT_TIMEOUT_SECONDS,
        minimum=MIN_TIMEOUT_SECONDS,
        maximum=MAX_TIMEOUT_SECONDS,
        field="turn_timeout_seconds",
    )
    bounded_default = float(fallback if fallback is not None else DEFAULT_TIMEOUT_SECONDS)
    if converted is None:
        return bounded_default
    return float(max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, converted)))


def safe_acp_stream_limit(value: Any, default: int = DEFAULT_ACP_STREAM_LIMIT_BYTES) -> int:
    """Return a finite ACP reader limit between 1 MiB and 32 MiB."""

    converted = safe_float(value, default=None, field="acp_stream_limit_bytes")
    fallback = safe_float(
        default,
        default=float(DEFAULT_ACP_STREAM_LIMIT_BYTES),
        minimum=float(MIN_ACP_STREAM_LIMIT_BYTES),
        maximum=float(MAX_ACP_STREAM_LIMIT_BYTES),
        field="acp_stream_limit_bytes",
    )
    bounded_default = int(fallback if fallback is not None else DEFAULT_ACP_STREAM_LIMIT_BYTES)
    if converted is None:
        return bounded_default
    return int(max(MIN_ACP_STREAM_LIMIT_BYTES, min(MAX_ACP_STREAM_LIMIT_BYTES, converted)))


def normalize_probability_map(
    value: Any,
    *,
    field: str = "dimension_scores",
    allowed_keys: set[str] | None = None,
) -> tuple[dict[str, float], list[str]]:
    """Normalize an optional probability map and report skipped entries.

    Null, non-finite, invalid, and out-of-range entries are omitted instead of
    being converted to 0.0.  That distinction is important for dimension
    coverage: an omitted score means "not provided", not negative evidence.
    """

    if not isinstance(value, dict):
        return {}, []
    normalized: dict[str, float] = {}
    skipped: list[str] = []
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if allowed_keys is not None and key not in allowed_keys:
            continue
        score = safe_probability(raw_value, default=None, field=f"{field}.{key}")
        if score is None:
            skipped.append(key)
        else:
            normalized[key] = float(score)
    return normalized, skipped


def append_skipped_numeric_metadata(
    metadata: Any, skipped: list[str], *, field: str = "dimension_scores"
) -> dict[str, Any]:
    """Return metadata with bounded diagnostics for omitted optional numbers."""

    result = dict(metadata) if isinstance(metadata, dict) else {}
    if skipped:
        previous = result.get("invalid_optional_numeric_skipped", [])
        values = list(previous) if isinstance(previous, list) else []
        values.extend(f"{field}.{key}" if field else str(key) for key in skipped)
        result["invalid_optional_numeric_skipped"] = list(dict.fromkeys(values))
    return result


__all__ = [
    "DEFAULT_ACP_STREAM_LIMIT_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "InvalidNumericFieldError",
    "MAX_ACP_STREAM_LIMIT_BYTES",
    "MAX_TIMEOUT_SECONDS",
    "MIN_ACP_STREAM_LIMIT_BYTES",
    "MIN_TIMEOUT_SECONDS",
    "append_skipped_numeric_metadata",
    "normalize_probability_map",
    "safe_acp_stream_limit",
    "safe_float",
    "safe_int",
    "safe_probability",
    "safe_timeout",
]
