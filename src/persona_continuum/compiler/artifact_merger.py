from __future__ import annotations

from typing import Any


def merge_artifacts(artifacts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "claims": [claim for artifact in artifacts for claim in artifact.get("claims", [])],
        "memories": [memory for artifact in artifacts for memory in artifact.get("memories", [])],
    }
