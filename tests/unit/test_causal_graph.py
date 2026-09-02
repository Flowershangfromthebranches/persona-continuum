from __future__ import annotations

from persona_continuum.world.causal import CausalGraphEngine
from persona_continuum.world.models import (
    CausalNodeType,
    CausalRelationType,
)


def test_event_creates_causal_chain() -> None:
    engine = CausalGraphEngine()

    # 1. Divergence Root Event
    root_node = engine.record_node(
        node_type=CausalNodeType.EVENT,
        name="Jobs survives pancreatic cancer in 2011",
        timestamp="2011-10-05",
        node_id="node_jobs_survives",
    )

    # 2. Decision: Apple AI chip investment
    dec_node = engine.record_node(
        node_type=CausalNodeType.DECISION,
        name="Apple launches custom silicon Neural Engine strategy",
        timestamp="2012-03-01",
        node_id="node_apple_chip_decision",
    )
    engine.link(root_node.id, dec_node.id, CausalRelationType.CAUSES)

    # 3. Technology Advance
    tech_node = engine.record_node(
        node_type=CausalNodeType.TECHNOLOGY,
        name="Neural Engine V1 tapeout at TSMC",
        timestamp="2014-09-01",
        node_id="node_neural_engine_tapeout",
    )
    engine.link(dec_node.id, tech_node.id, CausalRelationType.INFLUENCES)

    # 4. Final outcome in 2030
    outcome_node = engine.record_node(
        node_type=CausalNodeType.EVENT,
        name="Apple datacenter independence and on-device AI ecosystem lead",
        timestamp="2030-01-01",
        node_id="node_2030_apple_lead",
    )
    engine.link(tech_node.id, outcome_node.id, CausalRelationType.CAUSES)

    # 5. Query backward causal chain from 2030 outcome
    chain = engine.query_causal_chain("node_2030_apple_lead")
    assert len(chain) == 4
    chain_names = [step.node.name for step in chain]
    assert "Apple datacenter independence" in chain_names[0]
    assert "Neural Engine V1 tapeout" in chain_names[1]
    assert "Apple launches custom silicon" in chain_names[2]
    assert "Jobs survives" in chain_names[3]
