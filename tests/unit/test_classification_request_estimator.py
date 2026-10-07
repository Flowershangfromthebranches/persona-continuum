"""Classification request estimator includes schema/system/envelope overhead."""

from __future__ import annotations

import json

from persona_continuum.agent.context_budget import AgentContextBudgetManager
from persona_continuum.application.material_intelligence import (
    MATERIAL_AGENT_OUTPUT_SCHEMAS,
    MATERIAL_AGENT_SYSTEM_PROMPTS,
    REQUIRED_DIMENSIONS,
)
from persona_continuum.application.material_pipeline import ClassificationRequestTokenEstimator


def test_estimator_counts_schema_and_system_overhead() -> None:
    manager = AgentContextBudgetManager()
    estimator = ClassificationRequestTokenEstimator(manager.estimate_tokens)
    empty = estimator.base_tokens()
    with_prompt = estimator.base_tokens(
        system_prompt=MATERIAL_AGENT_SYSTEM_PROMPTS["classify"],
        schema=MATERIAL_AGENT_OUTPUT_SCHEMAS["classify"],
        extra_contracts=["output contract"],
        allowed_dimensions=REQUIRED_DIMENSIONS,
    )
    assert with_prompt > empty
    row = {
        "id": "t1",
        "evidence_ids": ["t1"],
        "text": "短句",
        "source_id": "s1",
        "semantic_role": "target",
    }
    turn_only = estimator.turn_tokens(row)
    full = estimator.estimate_request(
        system_prompt=MATERIAL_AGENT_SYSTEM_PROMPTS["classify"],
        schema=MATERIAL_AGENT_OUTPUT_SCHEMAS["classify"],
        extra_contracts=["contract"],
        allowed_dimensions=REQUIRED_DIMENSIONS,
        rows=[row] * 10,
        episode_count=1,
    )
    assert full > turn_only * 10
    assert full > with_prompt


def test_estimator_prefers_overestimate_on_serialized_request() -> None:
    manager = AgentContextBudgetManager()
    estimator = ClassificationRequestTokenEstimator(manager.estimate_tokens, margin_ratio=0.08)
    rows = [
        {
            "id": f"t{index}",
            "evidence_ids": [f"t{index}"],
            "text": "这是一条合成目标人格发言，用来填满分析窗口。" * 4,
            "source_id": "s1",
            "semantic_role": "target",
            "speaker": "对方",
            "speaker_role": "target_persona",
            "timestamp": "2023-05-01T09:00:00+00:00",
            "source_kind": "chat_import",
        }
        for index in range(40)
    ]
    request = {
        "_participant_id": "persona_material_intelligence:aw_test",
        "analysis_window": {"id": "aw_test", "source_ids": ["s1"], "episode_count": 1},
        "episodes": [
            {"episode_id": "ep1", "source_id": "s1", "unit_ids": [row["id"] for row in rows]}
        ],
        "target_units": [row["id"] for row in rows],
        "context_units": [],
        "units": rows,
        "episode_contract": "Episodes are independent",
        "source_context": {},
        "allowed_dimensions": list(REQUIRED_DIMENSIONS),
        "output_contract": "Return only target turns",
    }
    estimated = estimator.estimate_request(
        system_prompt=MATERIAL_AGENT_SYSTEM_PROMPTS["classify"],
        schema=MATERIAL_AGENT_OUTPUT_SCHEMAS["classify"],
        source_context={},
        extra_contracts=["Episodes are independent", "Return only target turns"],
        allowed_dimensions=REQUIRED_DIMENSIONS,
        rows=rows,
        episode_count=1,
    )
    actual = manager.estimate_tokens(json.dumps(request, ensure_ascii=False))
    actual += manager.estimate_tokens(MATERIAL_AGENT_SYSTEM_PROMPTS["classify"])
    actual += manager.estimate_tokens(MATERIAL_AGENT_OUTPUT_SCHEMAS["classify"])
    error = (estimated - actual) / max(1, actual)
    assert error >= -0.10
