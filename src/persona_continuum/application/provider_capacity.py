"""Classify provider/runtime capacity failures for adaptive concurrency.

HTTP status alone is not semantics: a bare ``429`` may be a per-second rate
limit, a daily quota, or provider congestion.  Only an *explicit* concurrency
signal (session/parallel-request language or a machine-readable concurrency
code) may lower the effective width and be persisted as a runtime downgrade.

Everything else fails closed:

* ``TRANSIENT_RATE_LIMIT`` — bounded retry, never a capability downgrade.
* ``QUOTA_EXHAUSTED`` / ``PAYMENT_REQUIRED`` — fail fast, no retry ladder.
* ``UNKNOWN_429`` — a 429 with no identifiable semantics: no downgrade, no
  capability change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class CapacityFailure:
    """Kinds of provider/runtime capacity failure."""

    # Explicit concurrency pressure -> downgrade ladder allowed.
    CONCURRENCY_LIMIT = "concurrency_limit"
    TOO_MANY_SESSIONS = "too_many_sessions"
    RUNTIME_POOL_CAPACITY = "runtime_pool_capacity"
    PROVIDER_PARALLELISM_REJECTED = "provider_parallelism_rejected"
    # Transient throughput limit -> bounded retry only.
    TRANSIENT_RATE_LIMIT = "transient_rate_limit"
    # Terminal classes.
    QUOTA_EXHAUSTED = "quota_exhausted"
    PAYMENT_REQUIRED = "payment_required"
    AUTH_FAILURE = "auth_failure"
    UNKNOWN_429 = "unknown_429"
    UNKNOWN_PROVIDER_FAILURE = "unknown_provider_failure"

    # Backwards-compatible aliases (same values as their canonical kinds).
    NON_RETRIABLE_QUOTA = QUOTA_EXHAUSTED
    NON_RETRIABLE_PAYMENT = PAYMENT_REQUIRED
    RATE_LIMIT_CONCURRENCY = TRANSIENT_RATE_LIMIT
    OTHER = UNKNOWN_PROVIDER_FAILURE


_RETRIABLE_CONCURRENCY_KINDS = frozenset(
    {
        CapacityFailure.CONCURRENCY_LIMIT,
        CapacityFailure.TOO_MANY_SESSIONS,
        CapacityFailure.RUNTIME_POOL_CAPACITY,
        CapacityFailure.PROVIDER_PARALLELISM_REJECTED,
    }
)
# Kinds that may be replayed (with backoff) but must never lower the persisted
# independent-session capability.
_RETRIABLE_NON_CONCURRENCY_KINDS = frozenset({CapacityFailure.TRANSIENT_RATE_LIMIT})


# Machine-readable provider codes, checked before text heuristics.  Any of these
# appearing as a structured code is authoritative.
_STRUCTURED_CONCURRENCY_CODES = (
    "concurrency_limit",
    "concurrencylimit",
    "rate_limit_concurrency",
    "too_many_concurrent",
    "parallel_request_limit",
    "simultaneous_session_limit",
    "max_concurrent_requests",
    "resource_exhausted_concurrency",
)
_STRUCTURED_QUOTA_CODES = (
    "insufficient_quota",
    "quota_exhausted",
    "quota_exceeded",
    "billing_hard_limit_reached",
    "usage_limit_reached",
    "monthly_quota_exceeded",
)
_STRUCTURED_PAYMENT_CODES = (
    "payment_required",
    "insufficient_credits",
    "balance_exhausted",
    "billing_required",
)
_STRUCTURED_RATE_CODES = (
    "rate_limit_exceeded",
    "too_many_requests",
    "requests_per_minute",
    "requests_per_second",
    "throttled",
)

# Text heuristics.  Ordered; payment/auth/quota are evaluated before any
# concurrency or rate signal so "429 quota exhausted" is never downgraded.
_PAYMENT_MARKERS = (
    "payment required",
    "payment_required",
    "insufficient credit",
    "insufficient balance",
    "balance exhausted",
    "no credit",
    "billing exhausted",
    "billing_required",
    "402",
)
_AUTH_MARKERS = (
    "401",
    "403",
    "unauthorized",
    "authentication",
    "auth required",
    "not logged in",
    "login required",
    "invalid api key",
    "permission denied",
)
_QUOTA_MARKERS = (
    "quota",
    "insufficient_quota",
    "usage limit",
    "usage balance exhausted",
    "credits exhausted",
    "token quota",
    "daily limit",
    "monthly limit",
    "requests per day",
)
_SESSION_MARKERS = (
    "too many concurrent sessions",
    "too many sessions",
    "too many parallel sessions",
    "too many active sessions",
    "max sessions",
    "session capacity",
    "session limit reached",
    "concurrent session limit",
)
_CONCURRENCY_MARKERS = (
    "too many concurrent",
    "too many parallel",
    "concurrency limit",
    "concurrency_limit",
    "max concurrent",
    "parallel request limit",
    "parallelism limit",
    "max parallel",
    "simultaneous",
    "concurrent request",
)
_RATE_MARKERS = (
    "too many requests",
    "rate limit",
    "rate_limit",
    "retry after",
    "requests per second",
    "requests per minute",
    "request per second",
    "request per minute",
)
_RATE_ABBREVIATIONS = re.compile(r"\b(?:rpm|rps)\b")
_POOL_MARKERS = (
    "runtime pool capacity",
    "runtime_pool",
    "pool capacity",
    "no available runtime",
    "affinity_unavailable",
    "no free lease",
    "lease unavailable",
    "lease timeout",
)

# Broad provider payloads (e.g. a JSON body) may embed a numeric status.
_STATUS_PATTERN = re.compile(r"(?:http[ _]?|status[ :\"=]+)(\d{3})", re.IGNORECASE)


@dataclass(frozen=True)
class CapacityDiagnosis:
    kind: str
    retriable_concurrency: bool
    retriable: bool = False
    http_status: int | None = None
    message: str = ""

    @property
    def is_payment(self) -> bool:
        return self.kind == CapacityFailure.PAYMENT_REQUIRED

    @property
    def is_quota(self) -> bool:
        return self.kind == CapacityFailure.QUOTA_EXHAUSTED

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "retriable_concurrency": self.retriable_concurrency,
            "retriable": self.retriable,
            "http_status": self.http_status,
            "message": self.message[:300],
        }


def _structured_codes(exc: BaseException) -> str:
    """Collect machine-readable error codes from the exception diagnostics."""

    parts: list[str] = []
    diagnostics = getattr(exc, "diagnostics", None)
    if isinstance(diagnostics, dict):
        for key in ("failure_code", "code", "error_code", "provider_code", "reason", "type"):
            value = diagnostics.get(key)
            if value is not None:
                parts.append(str(value))
    for attribute in ("code", "error_code"):
        value = getattr(exc, attribute, None)
        if value is not None:
            parts.append(str(value))
    return " ".join(parts).casefold()


def _harvest_text(exc: BaseException) -> list[str]:
    parts: list[str] = [str(exc)]
    diagnostics = getattr(exc, "diagnostics", None)
    if isinstance(diagnostics, dict):
        for key in (
            "message",
            "error",
            "detail",
            "diagnostic",
            "failure_code",
            "http_status",
            "status_code",
            "status",
        ):
            value = diagnostics.get(key)
            if value is not None:
                parts.append(str(value))
    for attribute in ("error", "detail"):
        value = getattr(exc, attribute, None)
        if value is not None:
            parts.append(str(value))
    return parts


def _detect_status(text: str, exc: BaseException) -> int | None:
    diagnostics = getattr(exc, "diagnostics", None)
    if isinstance(diagnostics, dict):
        for key in ("http_status", "status_code", "status"):
            value = diagnostics.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.strip().isdigit():
                return int(value.strip())
    match = _STATUS_PATTERN.search(text)
    if match:
        return int(match.group(1))
    return None


def _has(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def classify_capacity_failure(exc: BaseException) -> CapacityDiagnosis:
    """Return the dispatcher's diagnosis for one failed window.

    Precedence: structured provider code, then payment, auth, quota, explicit
    concurrency, explicit transient rate, unknown 429, unknown provider failure.
    """

    lowered = "\n".join(_harvest_text(exc)).casefold()
    codes = _structured_codes(exc)
    status = _detect_status(lowered, exc)

    if codes:
        if _has(codes, _STRUCTURED_PAYMENT_CODES):
            return CapacityDiagnosis(
                CapacityFailure.PAYMENT_REQUIRED, False, False, status or 402, lowered
            )
        if _has(codes, _STRUCTURED_QUOTA_CODES):
            return CapacityDiagnosis(
                CapacityFailure.QUOTA_EXHAUSTED, False, False, status, lowered
            )
        if _has(codes, _STRUCTURED_CONCURRENCY_CODES):
            return CapacityDiagnosis(
                CapacityFailure.CONCURRENCY_LIMIT, True, True, status, lowered
            )
        if _has(codes, _STRUCTURED_RATE_CODES):
            return CapacityDiagnosis(
                CapacityFailure.TRANSIENT_RATE_LIMIT, False, True, status or 429, lowered
            )

    if status == 402 or _has(lowered, _PAYMENT_MARKERS):
        return CapacityDiagnosis(
            CapacityFailure.PAYMENT_REQUIRED, False, False, status or 402, lowered
        )
    if _has(lowered, _AUTH_MARKERS):
        return CapacityDiagnosis(CapacityFailure.AUTH_FAILURE, False, False, status, lowered)
    if _has(lowered, _QUOTA_MARKERS):
        return CapacityDiagnosis(CapacityFailure.QUOTA_EXHAUSTED, False, False, status, lowered)
    if _has(lowered, _POOL_MARKERS):
        return CapacityDiagnosis(
            CapacityFailure.RUNTIME_POOL_CAPACITY, True, True, status, lowered
        )
    if _has(lowered, _SESSION_MARKERS):
        return CapacityDiagnosis(CapacityFailure.TOO_MANY_SESSIONS, True, True, status, lowered)
    if _has(lowered, _CONCURRENCY_MARKERS):
        return CapacityDiagnosis(
            CapacityFailure.CONCURRENCY_LIMIT, True, True, status, lowered
        )
    if _has(lowered, _RATE_MARKERS) or _RATE_ABBREVIATIONS.search(lowered):
        return CapacityDiagnosis(
            CapacityFailure.TRANSIENT_RATE_LIMIT, False, True, status or 429, lowered
        )
    if status == 429:
        # A 429 with no identifiable semantics must not be treated as
        # concurrency pressure.
        return CapacityDiagnosis(CapacityFailure.UNKNOWN_429, False, False, 429, lowered)
    return CapacityDiagnosis(
        CapacityFailure.UNKNOWN_PROVIDER_FAILURE, False, False, status, lowered
    )


def is_retriable_concurrency(diagnosis: CapacityDiagnosis) -> bool:
    return diagnosis.kind in _RETRIABLE_CONCURRENCY_KINDS


__all__ = [
    "CapacityDiagnosis",
    "CapacityFailure",
    "classify_capacity_failure",
    "is_retriable_concurrency",
]
