from __future__ import annotations


def summarize_reflection(turns: list[str]) -> str:
    return "\n".join(turns[-5:])
