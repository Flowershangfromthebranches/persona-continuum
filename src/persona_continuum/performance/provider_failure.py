"""Classify a synthetic concurrency probe's failure reason.

The probe records *why* it could not verify a width so the caller can report
``FAILED_PAYMENT`` / ``FAILED_AUTH`` / ``FAILED_PROVIDER_QUOTA`` instead of a
meaningless "unverified" with a fabricated speedup.
"""

from __future__ import annotations

from typing import Any

from persona_continuum.performance.runtime_capability_store import ProbeStatus

_PAYMENT_MARKERS = (
    "402",
    "payment required",
    "balance exhausted",
    "insufficient balance",
    "insufficient credit",
    "billing",
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
)
_QUOTA_MARKERS = (
    "quota",
    "rate limit exceeded",
    "requests per day",
    "daily limit",
    "monthly limit",
)


def _collect_errors(waves: dict[str, Any]) -> str:
    parts: list[str] = []
    for wave in waves.values():
        if not isinstance(wave, dict):
            continue
        parts.extend(str(item) for item in (wave.get("errors") or []))
        for row in wave.get("rows") or []:
            if isinstance(row, dict) and row.get("error"):
                parts.append(str(row["error"]))
    return "\n".join(parts).casefold()


def classify_probe_failure(waves: dict[str, Any]) -> str:
    text = _collect_errors(waves)
    if not text:
        return ProbeStatus.FAILED_RUNTIME
    if any(marker in text for marker in _PAYMENT_MARKERS):
        return ProbeStatus.FAILED_PAYMENT
    if any(marker in text for marker in _AUTH_MARKERS):
        return ProbeStatus.FAILED_AUTH
    if any(marker in text for marker in _QUOTA_MARKERS):
        return ProbeStatus.FAILED_PROVIDER_QUOTA
    return ProbeStatus.FAILED_RUNTIME


def speedup_or_none(serial_wall: float, width: int, wave: dict[str, Any]) -> float | None:
    """Speedup is only meaningful for a fully successful, well-correlated wave."""

    if not wave.get("correlation_ok"):
        return None
    if int(wave.get("success_count") or 0) != int(wave.get("requested_width") or width):
        return None
    wall = float(wave.get("wall_s") or 0.0)
    if wall <= 0 or serial_wall <= 0:
        return None
    return round((serial_wall * width) / wall, 3)


def derive_max_verified(waves: dict[str, Any], widths: list[int]) -> int:
    """Only widths that fully succeeded and correlated may raise the limit."""

    if not waves.get("serial", {}).get("correlation_ok"):
        return 1
    verified = 1
    for width in sorted(widths):
        if width <= 1:
            continue
        wave = waves.get(f"{width}_way")
        if isinstance(wave, dict) and wave.get("correlation_ok"):
            verified = width
        else:
            break
    return verified


def derive_probe_status(
    waves: dict[str, Any],
    widths: list[int],
    *,
    correlation_failure: bool,
) -> str:
    if correlation_failure:
        return ProbeStatus.FAILED_CORRELATION
    if not waves.get("serial", {}).get("correlation_ok"):
        return classify_probe_failure(waves)
    verified = derive_max_verified(waves, widths)
    if verified >= max(widths or [1]):
        return ProbeStatus.VERIFIED
    if verified >= 2:
        return ProbeStatus.PARTIAL
    return classify_probe_failure(waves)


__all__ = [
    "classify_probe_failure",
    "derive_max_verified",
    "derive_probe_status",
    "speedup_or_none",
]
