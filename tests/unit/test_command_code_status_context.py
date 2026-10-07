from persona_continuum.agent.adapters.command_code import CommandCodeAdapter


def test_command_code_status_json_exposes_runtime_context() -> None:
    facts = CommandCodeAdapter._parse_status_json(
        '{"authenticated":true,"model":"meta/muse-spark-1.3-contributor","context_window":1048576}'
    )
    assert facts["context_window"] == 1_048_576
    assert facts["model"] == "meta/muse-spark-1.3-contributor"
