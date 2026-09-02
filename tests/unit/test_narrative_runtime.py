from persona_continuum.narrative.runtime import normalize_runtime


def test_normalize_runtime_keeps_runtime_source_and_legacy_source() -> None:
    api = normalize_runtime(
        {
            "runtime_source": "api",
            "agent_id": "api_x",
            "model_id": "model_x",
            "reasoning_effort": "high",
        }
    )
    assert api["runtime_source"] == "api"
    assert api["agent_id"] == "api_x"
    legacy = normalize_runtime({"source": "local_cli", "agent": "qoder", "model": "glm"})
    assert legacy["runtime_source"] == "local_cli"
    assert legacy["agent_id"] == "qoder"
    assert legacy["model_id"] == "glm"
    empty = normalize_runtime({"agent_id": "qoder"})
    assert empty["runtime_source"] == ""
