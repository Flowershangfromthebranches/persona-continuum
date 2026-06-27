from __future__ import annotations


def rank_score(fts_score: float, importance: float, confidence: float) -> float:
    return importance * 0.5 + confidence * 0.3 - fts_score * 0.2
