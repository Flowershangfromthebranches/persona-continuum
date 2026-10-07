from persona_continuum.agent.adapter import AgentSession
from persona_continuum.agent.adapters.codex import CodexAdapter
from persona_continuum.agent.adapters.gemini import GeminiCliAdapter
from persona_continuum.agent.adapters.grok import GrokBuildAdapter
from persona_continuum.agent.context_fields import (
    CLAUDE_USAGE_SEMANTICS,
    CODEX_USAGE_SEMANTICS,
    GEMINI_USAGE_SEMANTICS,
    GROK_ACP_USAGE_SEMANTICS,
    extract_runtime_context_facts,
)
from persona_continuum.agent.models import AgentSessionConfig
from persona_continuum.agent.protocols.acp import ACPAdapter
from persona_continuum.agent.protocols.streaming_json_cli import StreamingJsonCliAdapter


def test_acp_session_new_extracts_context_window() -> None:
    adapter = ACPAdapter(
        adapter_id="opencode",
        name="OpenCode",
        binary_candidates=["/nonexistent"],
    )
    snapshot = adapter._runtime_binding_from_session_result(
        AgentSessionConfig(room_id="r", participant_id="p", persona_id="x"),
        {
            "sessionId": "sess_1",
            "contextWindow": 262144,
            "remainingContextTokens": 200000,
            "model": "opencode/big-pickle",
        },
    )
    assert snapshot.context_window == 262_144
    assert snapshot.remaining_context_tokens == 200_000
    assert snapshot.context_window_source == "runtime_reported"
    assert snapshot.effective_model == "opencode/big-pickle"


def test_acp_model_usage_frame_updates_session() -> None:
    adapter = ACPAdapter(
        adapter_id="grok",
        name="Grok",
        binary_candidates=["/nonexistent"],
    )
    session = AgentSession(
        config=AgentSessionConfig(room_id="r", participant_id="p", persona_id="x"),
        session_data={},
    )
    adapter._ingest_runtime_context(
        session,
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "usage",
                    "modelUsage": {"grok-4.6": {"contextWindow": 500000, "inputTokens": 130000}},
                }
            },
        },
    )
    assert session.session_data["effective_context_window"] == 500_000
    assert session.session_data["effective_context_window_source"] == "runtime_reported"
    assert session.session_data.get("remaining_context_tokens") is None
    assert session.session_data.get("remaining_context_verified") in {None, False}


def test_input_tokens_are_not_occupancy_by_default() -> None:
    payload = {"modelUsage": {"grok-4.6": {"contextWindow": 500000, "inputTokens": 130000}}}
    for semantics in (
        GROK_ACP_USAGE_SEMANTICS,
        CODEX_USAGE_SEMANTICS,
        CLAUDE_USAGE_SEMANTICS,
        GEMINI_USAGE_SEMANTICS,
    ):
        facts = extract_runtime_context_facts(payload, semantics=semantics)
        assert facts["context_window"] == 500_000
        assert facts["remaining_context_tokens"] is None
        assert facts["used_context_tokens"] is None
        assert facts["remaining_source"] is None


def test_protocol_adapters_declare_usage_semantics() -> None:
    assert GrokBuildAdapter.usage_context_semantics is GROK_ACP_USAGE_SEMANTICS
    assert CodexAdapter.usage_context_semantics is CODEX_USAGE_SEMANTICS
    assert StreamingJsonCliAdapter.usage_context_semantics is CLAUDE_USAGE_SEMANTICS
    assert GeminiCliAdapter.usage_context_semantics is GEMINI_USAGE_SEMANTICS
    assert GrokBuildAdapter.usage_context_semantics.derive_remaining_from_occupancy is False
    assert CodexAdapter.usage_context_semantics.derive_remaining_from_occupancy is False
