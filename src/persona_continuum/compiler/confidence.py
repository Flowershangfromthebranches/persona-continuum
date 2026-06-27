from __future__ import annotations


def weighted_confidence(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
