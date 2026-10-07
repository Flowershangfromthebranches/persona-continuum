from persona_continuum.agent.adapters.codex import CodexAdapter


def test_codex_model_list_parses_context_capability() -> None:
    items = [
        {
            "id": "gpt-5.6-sol",
            "displayName": "GPT-5.6 Sol",
            "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
            "defaultReasoningEffort": "high",
            "contextWindow": 372000,
            "effective_context_window": 372000,
        }
    ]
    parsed = CodexAdapter()._parse_model_list_items(items)
    assert parsed[0].id == "gpt-5.6-sol"
    assert parsed[0].context_window == 372_000
    assert parsed[0].source == "protocol_model_list"
