"""Static and fake-runtime regression contracts for the unified Agent layer.

These tests are delivered with the refactor and are intentionally not run in
the implementation turn; review approval is the next validation gate.
"""

from __future__ import annotations

import pytest

from persona_continuum.agent.adapter import AgentSession
from persona_continuum.agent.adapters import (
    ClaudeCodeAdapter,
    CodeBuddyAdapter,
    CodexAdapter,
    CommandCodeAdapter,
    GeminiCliAdapter,
    GrokBuildAdapter,
    KimiAdapter,
    OpenCodeAdapter,
    QoderAdapter,
    QwenAdapter,
    WorkBuddyAdapter,
)

try:
    from persona_continuum.agent.adapters import CursorAdapter
except ImportError:  # Cursor adapter module not present yet.
    CursorAdapter = None  # type: ignore[assignment, misc]

from persona_continuum.agent.adapters.fake import FakeAgentAdapter
from persona_continuum.agent.context_budget import AgentContextBudgetManager
from persona_continuum.agent.contracts import (
    AgentAdapterContract,
    adapter_prompt_mode,
    adapter_structured_output_mode,
)
from persona_continuum.agent.models import (
    AgentCapabilityFlags,
    AgentEvent,
    AgentEventType,
    AgentSessionConfig,
    AgentStatus,
    AgentTurn,
    ModelCapability,
    PromptEnvelope,
    PromptMode,
    StructuredOutputMode,
)
from persona_continuum.agent.prompt import AgentPromptRenderer
from persona_continuum.agent.protocols.plain_cli import PlainCliAdapter
from persona_continuum.agent.response_collector import (
    AgentTransportError,
    ContextBudgetExceededError,
    ModelBindingUnverifiedError,
    ReasoningBindingUnverifiedError,
    compact_tool_result,
)
from persona_continuum.agent.runtime_executor import AgentRuntimeExecutor
from persona_continuum.agent.structured_output import (
    StructuredOutputEngine,
    StructuredOutputSchemaError,
)
from persona_continuum.agent.timeout import AgentTimeoutPolicy


def _config(**extra: object) -> AgentSessionConfig:
    system_prompt = extra.pop("system_prompt", None)
    return AgentSessionConfig(
        room_id="contract-room",
        participant_id="contract-participant",
        persona_id="contract-persona",
        model_id="fake-gpt-5",
        reasoning_effort="high",
        system_prompt=str(system_prompt) if system_prompt is not None else None,
        extra=dict(extra),
    )


def test_prompt_contract_preserves_system_and_user_for_both_wire_shapes() -> None:
    turn = AgentTurn(
        system_prompt="SYSTEM_SENTINEL_ABC123",
        user_message="USER_SENTINEL_XYZ789",
        messages=[{"role": "user", "content": "CONTEXT_SENTINEL"}],
    )

    single_prompt = AgentPromptRenderer.render_for_single_prompt(turn)
    native_messages = AgentPromptRenderer.render_for_native_roles(turn)

    assert "SYSTEM_SENTINEL_ABC123" in single_prompt
    assert "USER_SENTINEL_XYZ789" in single_prompt
    assert any(
        message.get("role") == "system" and message.get("content") == "SYSTEM_SENTINEL_ABC123"
        for message in native_messages
    )
    assert any(
        message.get("role") == "user" and message.get("content") == "USER_SENTINEL_XYZ789"
        for message in native_messages
    )
    assert any(message.get("content") == "CONTEXT_SENTINEL" for message in native_messages)


def test_prompt_envelope_uses_canonical_names_and_accepts_legacy_aliases() -> None:
    canonical = PromptEnvelope(system_prompt="SYSTEM", user_message="USER")
    legacy = PromptEnvelope(system="SYSTEM", user="USER")
    legacy_turn = AgentTurn(system="SYSTEM", user="USER")
    assert canonical.system_prompt == legacy.system_prompt == "SYSTEM"
    assert canonical.user_message == legacy.user_message == "USER"
    assert canonical.system == legacy.system == "SYSTEM"
    assert canonical.user == legacy.user == "USER"
    assert legacy_turn.system_prompt == "SYSTEM"
    assert legacy_turn.user_message == "USER"


def test_prompt_renderer_keeps_explicit_legacy_full_prompt() -> None:
    turn = AgentTurn(
        system_prompt="SYSTEM_SENTINEL_ABC123",
        user_message="USER_SENTINEL_XYZ789",
        full_prompt="FULL_PROMPT_SENTINEL",
        prompt_mode=PromptMode.FULL_PROMPT,
    )
    rendered = AgentPromptRenderer.render_for_single_prompt(turn)
    assert "SYSTEM_SENTINEL_ABC123" in rendered
    assert "USER_SENTINEL_XYZ789" in rendered
    assert "FULL_PROMPT_SENTINEL" in rendered


def test_structured_output_engine_accepts_fences_prose_arrays_and_null() -> None:
    engine = StructuredOutputEngine()
    assert engine.parse_json("before ```json\n{\"ok\": true}\n``` after") == {"ok": True}
    assert engine.parse_json("explanation [1, null, 3] trailing") == [1, None, 3]
    assert engine.parse_json("null") is None
    assert engine.validate(None, {"type": ["string", "null"]}) is None


def test_structured_output_engine_reports_schema_failure() -> None:
    with pytest.raises(StructuredOutputSchemaError) as caught:
        StructuredOutputEngine().parse_and_validate(
            '{"summary": 42}',
            {
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
            },
            phase="profile_summary",
            adapter="fake_agent",
            protocol="fake_protocol",
            structured_output_mode=StructuredOutputMode.PROMPT_ONLY,
        )
    assert caught.value.code == "STRUCTURED_OUTPUT_SCHEMA_FAILED"
    assert caught.value.diagnostics["phase"] == "profile_summary"
    assert caught.value.diagnostics["structured_output_mode"] == "prompt_only"


def test_context_budget_batches_by_items_and_prompt_size_without_dropping_items() -> None:
    manager = AgentContextBudgetManager(default_context_window_tokens=12_000)
    items = [f"evidence-{index}-" + ("x" * 5_000) for index in range(8)]
    batches = list(
        manager.iter_batches(
            items,
            item_text=lambda item: item,
            max_items=20,
            phase="classification",
            model=ModelCapability(
                id="bounded",
                display_name="Bounded",
                context_window=12_000,
            ),
        )
    )
    assert [item for batch in batches for item in batch] == items
    assert len(batches) > 1


def test_context_budget_fails_closed_for_one_oversized_item() -> None:
    manager = AgentContextBudgetManager(default_context_window_tokens=7_000)
    with pytest.raises(ContextBudgetExceededError) as caught:
        list(
            manager.iter_batches(
                ["x" * 10_000],
                item_text=lambda item: item,
                max_items=24,
                phase="material_classification",
            )
        )
    assert caught.value.code == "CONTEXT_BUDGET_EXCEEDED"


def test_timeout_policy_uses_phase_defaults_and_separate_idle_hard_limits() -> None:
    policy = AgentTimeoutPolicy()
    budget = policy.resolve(phase="dimension", reasoning_effort="high")
    assert budget.idle_timeout_seconds >= 180
    assert budget.hard_timeout_seconds >= 480
    assert policy.resolve(phase="research").hard_timeout_seconds >= 900


def test_structured_output_capability_is_not_true_by_default() -> None:
    flags = AgentCapabilityFlags()
    assert flags.structured_output is None
    assert flags.structured_output_mode == StructuredOutputMode.UNKNOWN


def test_all_builtin_adapters_declare_prompt_and_structured_modes() -> None:
    adapters = [
        GeminiCliAdapter(),
        OpenCodeAdapter(),
        CodexAdapter(),
        GrokBuildAdapter(),
        CommandCodeAdapter(),
        ClaudeCodeAdapter(),
        QoderAdapter(),
        WorkBuddyAdapter(),
        CodeBuddyAdapter(),
        KimiAdapter(),
        QwenAdapter(),
    ]
    if CursorAdapter is not None:
        adapters.append(CursorAdapter())
    assert all(adapter_prompt_mode(adapter) for adapter in adapters)
    assert all(adapter_structured_output_mode(adapter) for adapter in adapters)
    assert all(callable(getattr(adapter, "bind_runtime", None)) for adapter in adapters)
    assert all(callable(getattr(adapter, "capabilities", None)) for adapter in adapters)
    assert all(isinstance(adapter, AgentAdapterContract) for adapter in adapters)


def test_opencode_binding_fails_closed_when_session_does_not_confirm_model() -> None:
    adapter = OpenCodeAdapter()
    with pytest.raises(ModelBindingUnverifiedError) as caught:
        adapter._runtime_binding_from_session_result(
            _config(),
            {"sessionId": "unverified-session"},
        )
    assert caught.value.code == "MODEL_BINDING_UNVERIFIED"


def test_opencode_default_binding_does_not_claim_an_unverified_model() -> None:
    adapter = OpenCodeAdapter()
    config = AgentSessionConfig(
        room_id="contract-room",
        participant_id="contract-participant",
        persona_id="contract-persona",
        model_id="opencode-default",
    )

    params = adapter.build_session_new_params(config)
    snapshot = adapter._runtime_binding_from_session_result(
        config, {"sessionId": "default-session"}
    )

    assert "model" not in params
    assert snapshot.requested_model is None
    assert snapshot.effective_model is None
    assert snapshot.model_verified is True
    assert snapshot.verification_method == "acp_session_new_default"


def test_opencode_fallback_only_exposes_the_default_without_binding_proof() -> None:
    models = {model.id: model for model in OpenCodeAdapter._fallback_models()}

    assert models["opencode-default"].selectable is True
    assert models["opencode-default"].supported_reasoning_efforts == []
    assert models["grok-4.6"].selectable is False


@pytest.mark.anyio
async def test_codex_exec_fallback_fails_closed_for_requested_reasoning() -> None:
    adapter = CodexAdapter()
    fallback = AgentSession(
        config=_config(),
        session_data={
            "mode": "cli_exec_fallback",
            "protocol": "codex_exec_json",
            "effective_model": "fake-gpt-5",
            "effective_reasoning": None,
        },
    )
    with pytest.raises(ReasoningBindingUnverifiedError) as caught:
        await adapter.bind_runtime(fallback)
    assert caught.value.code == "REASONING_BINDING_UNVERIFIED"


@pytest.mark.anyio
async def test_codex_mid_turn_exec_fallback_fails_closed_for_reasoning() -> None:
    class _EmptyStdout:
        async def readline(self) -> bytes:
            return b""

    class _LiveAppServer:
        returncode = None
        stdin = None
        stdout = _EmptyStdout()

    adapter = CodexAdapter()
    session = AgentSession(
        config=_config(),
        session_data={
            "mode": "app_server",
            "protocol": "codex_app_server",
            "proc": _LiveAppServer(),
            "thread_id": "thread-test",
            "binary": "codex-must-not-run",
        },
    )
    events = [
        event async for event in adapter.send(session, AgentTurn(user_message="hello"))
    ]
    assert events[-1].type == AgentEventType.ERROR
    assert events[-1].error == "REASONING_BINDING_UNVERIFIED"


def test_large_tool_result_has_reference_preview_and_size_but_not_full_body() -> None:
    result = compact_tool_result(
        {"evidence_id": "ev-1", "content": "private-" + ("x" * 20_000)},
        tool_name="evidence_search",
        artifact_ref="artifact:ev-1",
    )
    assert result["artifact_ref"] == "artifact:ev-1"
    assert result["character_count"] > 12_000
    assert result["evidence_refs"] == ["ev-1"]
    assert "private-" not in result["preview"] or len(result["preview"]) < result["character_count"]


@pytest.mark.anyio
async def test_runtime_executor_preserves_config_system_prompt_and_records_budget() -> None:
    adapter = FakeAgentAdapter(chunk_delay_sec=0.0)
    executor = AgentRuntimeExecutor()
    binding = await executor.open_session(
        adapter,
        _config(system_prompt="CONFIG_SYSTEM_SENTINEL"),
    )
    try:
        result = await executor.execute_text(
            binding,
            system_prompt=None,
            user_message="USER_RUNTIME_SENTINEL",
            phase="classification",
        )
        sent = adapter.sent_turns[-1][1]
        assert sent.system_prompt == "CONFIG_SYSTEM_SENTINEL"
        assert sent.user_message == "USER_RUNTIME_SENTINEL"
        assert result.context_budget.estimated_prompt_tokens > 0
        assert result.response.raw_diagnostics["prompt_mode"] == "native_roles"
    finally:
        await executor.close(binding)


class _RepairFakeAdapter(FakeAgentAdapter):
    def _generate_mock_response(self, session, turn) -> str:  # type: ignore[no-untyped-def]
        if session.session_data.get("turn_count", 0) == 1:
            return "not-json"
        return '{"ok": true}'


@pytest.mark.anyio
async def test_runtime_executor_allows_one_bounded_structured_repair() -> None:
    adapter = _RepairFakeAdapter(chunk_delay_sec=0.0)
    executor = AgentRuntimeExecutor(max_repair_attempts=1)
    binding = await executor.open_session(adapter, _config())
    try:
        result = await executor.execute_structured(
            binding,
            system_prompt="Return a JSON object.",
            user_message="repair this",
            schema={"type": "object", "required": ["ok"]},
            phase="classification",
        )
        assert result.value == {"ok": True}
        assert result.attempts == 2
        assert result.repair_attempted is True
    finally:
        await executor.close(binding)


@pytest.mark.anyio
async def test_runtime_executor_can_use_two_bounded_structured_repairs() -> None:
    class _TwoRepairFakeAdapter(FakeAgentAdapter):
        def _generate_mock_response(self, session, turn):  # type: ignore[no-untyped-def]
            if session.session_data.get("turn_count", 0) < 3:
                return "not-json"
            return '{"ok": true}'

    adapter = _TwoRepairFakeAdapter(chunk_delay_sec=0.0)
    executor = AgentRuntimeExecutor(max_repair_attempts=2)
    binding = await executor.open_session(adapter, _config())
    try:
        result = await executor.execute_structured(
            binding,
            system_prompt="Return a JSON object.",
            user_message="repair twice",
            schema={"type": "object", "required": ["ok"]},
            phase="classification",
        )
        assert result.value == {"ok": True}
        assert result.attempts == 3
        assert result.diagnostics["repair_attempts"] == 2
    finally:
        await executor.close(binding)


@pytest.mark.anyio
async def test_runtime_public_stream_compacts_large_tool_result() -> None:
    class _LargeToolResultAdapter(FakeAgentAdapter):
        async def send(self, session, turn):  # type: ignore[no-untyped-def]
            yield AgentEvent(
                type=AgentEventType.TOOL_RESULT,
                tool_call_id="call-large",
                tool_name="evidence_search",
                tool_result={"evidence_id": "ev-large", "content": "x" * 20_000},
            )
            yield AgentEvent(type=AgentEventType.CHUNK, content="done")
            yield AgentEvent(type=AgentEventType.DONE, content="done")

    adapter = _LargeToolResultAdapter(chunk_delay_sec=0.0)
    executor = AgentRuntimeExecutor()
    binding = await executor.open_session(adapter, _config())
    try:
        events = [
            event
            async for event in executor.stream_events(
                binding,
                system_prompt="system",
                user_message="user",
                phase="classification",
            )
        ]
        tool_event = next(event for event in events if event.type == AgentEventType.TOOL_RESULT)
        assert tool_event.tool_result["artifact_ref"]
        assert tool_event.tool_result["character_count"] > 12_000
        assert "x" * 20_000 not in str(tool_event.tool_result)
        artifact_ref = tool_event.tool_result["artifact_ref"]
        assert binding.session.session_data["_agent_tool_artifacts"][artifact_ref]["content"] == (
            "x" * 20_000
        )
    finally:
        await executor.close(binding)


@pytest.mark.anyio
async def test_runtime_transport_failure_is_not_relabelled_as_structured_repair() -> None:
    adapter = FakeAgentAdapter(chunk_delay_sec=0.0, simulate_crash_on_turn=1)
    executor = AgentRuntimeExecutor(max_repair_attempts=1)
    binding = await executor.open_session(adapter, _config())
    try:
        with pytest.raises(AgentTransportError):
            await executor.execute_structured(
                binding,
                system_prompt="Return JSON.",
                user_message="transport failure",
                schema={"type": "object"},
                phase="classification",
            )
        assert len(adapter.sent_turns) == 1
    finally:
        await executor.close(binding)


def test_plain_cli_classifies_auth_and_quota_errors() -> None:
    adapter = PlainCliAdapter("test_cli", "Test CLI", ["test"])
    session = AgentSession(config=_config())

    msg, _ = adapter.classify_process_failure(
        session,
        returncode=0,
        stderr_text="Authentication required. Please use /login command to sign in",
        diagnostics={},
    )
    assert msg is not None
    assert "CLI authentication required" in msg

    msg, _ = adapter.classify_process_failure(
        session,
        returncode=1,
        stderr_text="You've reached your credit usage limit.",
        diagnostics={},
    )
    assert msg is not None
    assert "credit usage limit" in msg


@pytest.mark.anyio
async def test_codebuddy_probe_marks_auth_required_when_unauthenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = CodeBuddyAdapter()
    monkeypatch.setattr(adapter, "_find_binary", lambda: "/usr/local/bin/codebuddy")

    async def mock_exec(cmd: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
        if "--version" in cmd:
            return 0, "2.137.1", ""
        if "status" in cmd:
            return (
                0,
                "",
                "Authentication required. Please use /login command to sign in to your account",
            )
        if "--help" in cmd:
            return 0, "Currently supported: (hy3)", ""
        return 0, "", ""

    monkeypatch.setattr("persona_continuum.agent.adapters.other_vendors.safe_exec_cmd", mock_exec)
    monkeypatch.setattr("persona_continuum.agent.protocols.plain_cli.safe_exec_cmd", mock_exec)

    probe = await adapter.probe()
    assert probe.status == AgentStatus.AUTH_REQUIRED
    assert probe.auth_status == "auth_required"
    assert "Authentication required" in str(probe.status_detail)

