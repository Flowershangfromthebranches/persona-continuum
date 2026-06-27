from __future__ import annotations


def provenance_label(source_kind: str, simulated: bool = False) -> str:
    return "counterfactual_simulated" if simulated else source_kind
