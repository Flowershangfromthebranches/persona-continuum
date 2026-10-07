"""Decimal token notation used by the frontend (500K = 500,000)."""

from __future__ import annotations

from pathlib import Path


def test_app_js_uses_decimal_token_notation() -> None:
    text = Path("src/persona_continuum/web/static/app.js").read_text(encoding="utf-8")
    assert "function formatDecimalTokens" in text
    assert "1_000_000" in text
    format_tokens = text.split("function formatTokens")[1].split("function formatContextTag")[0]
    assert "n / 1024" not in format_tokens
    assert "(Estimate)" in text
    assert 'remainingTrust === "verified"' in text
    assert 'remainingSource === "runtime_reported"' in text
    assert "Context Scope:" in text
    assert "Fresh Session Assumption" in text
    assert "Window packing underestimated request overhead" in text
    detail = text.split("function formatContextDetail")[1].split("function loadRooms")[0]
    assert "remainingUnknown" in detail
    assert 'remainingTrust === "verified" ? "yes" : "no"' in detail
