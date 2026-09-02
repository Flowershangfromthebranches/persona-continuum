from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.narrative_live_acceptance import run_live_acceptance


@pytest.mark.anyio
async def test_narrative_live_runtime(tmp_path: Path) -> None:
    """Explicitly opt-in: discovers READY Agents and skips with a clear reason."""
    if os.environ.get("RUN_NARRATIVE_LIVE") != "1":
        pytest.skip("set RUN_NARRATIVE_LIVE=1 to authorize real Agent calls")
    result = await run_live_acceptance(
        data_dir=tmp_path / "narrative-live",
        requested_agent=os.environ.get("NARRATIVE_LIVE_AGENT"),
        requested_model=os.environ.get("NARRATIVE_LIVE_MODEL", "gpt-5.6-sol"),
        requested_reasoning=os.environ.get("NARRATIVE_LIVE_REASONING", "low"),
        branch_count=3,
        horizon=2,
    )
    if result.get("error") == "No non-test READY Agent with a discovered model is available":
        pytest.skip(result["error"])
    assert result["status"] == "passed", result
    assert all(result["checks"].values()), result["checks"]
