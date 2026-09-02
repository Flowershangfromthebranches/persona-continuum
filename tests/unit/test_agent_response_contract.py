from __future__ import annotations

import pytest

from persona_continuum.agent.models import AgentEvent, AgentEventType
from persona_continuum.agent.response_collector import (
    AgentOutputError,
    AgentPermissionBlockedError,
    AgentResponseCollector,
    AgentRuntimeError,
    AgentStructuredOutputError,
    ReasoningBindingUnverifiedError,
    agent_error_event,
)


async def _events(*events: AgentEvent):
    for event in events:
        yield event


@pytest.mark.anyio
async def test_agent_response_collector_merges_chunk_and_done() -> None:
    collector = AgentResponseCollector(protocol="test")
    response = await collector.collect(
        _events(
            AgentEvent(type=AgentEventType.CHUNK, content='{"summary":"ok"'),
            AgentEvent(
                type=AgentEventType.DONE,
                content='{"summary":"ok"}',
                metadata={"usage": {"total_tokens": 3}},
            ),
        )
    )
    assert response.text == '{"summary":"ok"}'
    assert response.done_received
    assert response.usage["total_tokens"] == 3


@pytest.mark.anyio
async def test_empty_agent_output_is_typed_and_diagnostic() -> None:
    collector = AgentResponseCollector(protocol="plain_cli", stderr="API_KEY=secret")
    await collector.collect(_events(AgentEvent(type=AgentEventType.DONE)))
    with pytest.raises(AgentOutputError) as caught:
        collector.require_text(phase="persona_material_classification", job_id="job_1")
    assert caught.value.diagnostics["code"] == "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT"
    assert "secret" not in str(caught.value.diagnostics)


@pytest.mark.anyio
async def test_reported_process_exit_without_output_is_retriable() -> None:
    collector = AgentResponseCollector(protocol="plain_cli")
    await collector.collect(
        _events(
            AgentEvent(
                type=AgentEventType.ERROR,
                error="AGENT_PROCESS_EXITED_WITHOUT_OUTPUT",
                metadata={
                    "failure_code": "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT",
                    "returncode": 1,
                },
            )
        )
    )

    with pytest.raises(AgentRuntimeError) as caught:
        collector.require_text(phase="material_classification", job_id="job_process_exit")

    assert caught.value.code == "AGENT_PROCESS_EXITED_WITHOUT_OUTPUT"
    assert caught.value.retriable is True


@pytest.mark.anyio
async def test_headless_permission_failure_is_preserved_and_not_retried() -> None:
    collector = AgentResponseCollector(protocol="plain_cli")
    await collector.collect(
        _events(
            agent_error_event(
                AgentPermissionBlockedError(
                    "Headless tool permission was denied",
                    phase="material_classification",
                ),
                protocol="plain_cli",
            )
        )
    )

    with pytest.raises(AgentPermissionBlockedError) as caught:
        collector.require_text(phase="material_classification", job_id="job_permission")

    assert caught.value.code == "AGENT_PERMISSION_BLOCKED"
    assert caught.value.retriable is False


def test_structured_output_error_is_distinct() -> None:
    error = AgentStructuredOutputError("invalid JSON", phase="profile_enrichment")
    assert error.code == "AGENT_STRUCTURED_OUTPUT_INVALID"
    assert error.phase == "profile_enrichment"


@pytest.mark.anyio
async def test_agent_response_collector_records_tool_and_thinking_events() -> None:
    collector = AgentResponseCollector(protocol="acp")
    response = await collector.collect(
        _events(
            AgentEvent(type=AgentEventType.THINKING, thinking="plan"),
            AgentEvent(
                type=AgentEventType.TOOL_CALL,
                tool_call_id="t1",
                tool_name="search",
                tool_arguments={"q": "x"},
            ),
            AgentEvent(
                type=AgentEventType.TOOL_RESULT,
                tool_call_id="t1",
                tool_name="search",
                tool_result="ok",
            ),
            AgentEvent(type=AgentEventType.CHUNK, content="answer"),
            AgentEvent(type=AgentEventType.DONE, content="answer"),
        )
    )
    assert response.text == "answer"
    assert response.thinking == "plan"
    assert response.tool_calls[0]["name"] == "search"
    assert response.tool_results[0]["id"] == "t1"


def test_agent_call_audit_does_not_include_prompt() -> None:
    collector = AgentResponseCollector(protocol="test", diagnostics={"prompt": "private text"})
    audit = collector.response.audit(call_id="call_1", job_id="job_1", phase="summary")
    assert "private text" not in str(audit)


@pytest.mark.anyio
async def test_collector_preserves_reported_reasoning_binding_failure() -> None:
    collector = AgentResponseCollector(protocol="openai_compatible_http")
    await collector.collect(
        _events(
            agent_error_event(
                ReasoningBindingUnverifiedError(
                    "reasoning rejected",
                    phase="session_update",
                    diagnostics={"requested_reasoning": "high"},
                ),
                protocol="openai_compatible_http",
            )
        )
    )
    with pytest.raises(ReasoningBindingUnverifiedError) as caught:
        collector.require_text(phase="classification")
    assert caught.value.code == "REASONING_BINDING_UNVERIFIED"
    assert caught.value.phase == "session_update"
    assert caught.value.diagnostics["failure_code"] == "REASONING_BINDING_UNVERIFIED"
