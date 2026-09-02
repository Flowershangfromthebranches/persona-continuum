from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from persona_continuum.agent.models import (
    AgentEventType,
    AgentSessionConfig,
    AgentTurn,
)
from persona_continuum.agent.protocols.openai_compatible import OpenAICompatibleAPIAdapter


class MockByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for c in self.chunks:
            yield c.encode("utf-8")


class MockStreamingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.request_count = 0
        self.received_payloads: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request_count += 1
        raw_body = await request.aread()
        body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        self.received_payloads.append(body)

        messages = body.get("messages", [])

        # Round 1: Assistant calls tool "search_persona_memory" with fragmented arguments
        if self.request_count == 1:
            chunk1 = {
                "choices": [
                    {
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_abc123",
                                    "type": "function",
                                    "function": {
                                        "name": "search_persona_memory",
                                        "arguments": '{"qu',
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
            chunk2 = {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {
                                        "arguments": 'ery": "Macintosh"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
            chunks = [
                f"data: {json.dumps(chunk1)}\n\n",
                f"data: {json.dumps(chunk2)}\n\n",
                "data: [DONE]\n\n",
            ]
        # Round 2: Assistant uses tool result to produce final answer
        else:
            # Verify the tool message is in the conversation
            if not any(m.get("role") == "tool" for m in messages):
                raise ValueError(f"No tool message found in messages: {messages}")
            chunk_ans1 = {"choices": [{"delta": {"content": "Based on the retrieved memory, "}}]}
            chunk_ans2 = {
                "choices": [{"delta": {"content": "the Macintosh was launched in 1984."}}]
            }
            chunks = [
                f"data: {json.dumps(chunk_ans1)}\n\n",
                f"data: {json.dumps(chunk_ans2)}\n\n",
                "data: [DONE]\n\n",
            ]

        return httpx.Response(200, stream=MockByteStream(chunks))


@pytest.mark.anyio
async def test_openai_compatible_multi_round_tool_calling(monkeypatch, app) -> None:
    mock_transport = MockStreamingTransport()

    # Patch httpx.AsyncClient to use mock transport
    orig_async_client = httpx.AsyncClient

    def mock_client_factory(*args, **kwargs):
        kwargs["transport"] = mock_transport
        return orig_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client_factory)

    app.credentials.create(
        credential_id="mock-openai-tool-loop",
        provider="openai",
        api_key="sk-mock-tool-loop-secret-123456",
    )
    adapter = OpenAICompatibleAPIAdapter(
        adapter_id="mock_openai",
        name="Mock OpenAI",
        base_url="https://api.openai.com/v1",
        credential_manager=app.credentials,
        credential_id="mock-openai-tool-loop",
    )

    cfg = AgentSessionConfig(
        room_id="room_1",
        participant_id="slot_1",
        persona_id="steve_jobs",
        model_id="gpt-4o",
        auth_profile_id="mock-openai-tool-loop",
    )
    session = await adapter.create_session(cfg)

    # Tool executor mock
    async def mock_tool_executor(name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name != "search_persona_memory" or args != {"query": "Macintosh"}:
            raise ValueError(f"mock_tool_executor received invalid args: name={name}, args={args}")
        return {"status": "success", "results": ["Macintosh team launched in 1984"]}

    turn = AgentTurn(
        turn_id="turn_101",
        user_message="When was the Macintosh launched?",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "search_persona_memory",
                    "description": "Search memory records",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                },
            }
        ],
        metadata={"tool_executor": mock_tool_executor},
    )

    events = []
    async for ev in adapter.send(session, turn):
        events.append(ev)

    if events and events[0].type == AgentEventType.ERROR:
        pytest.fail(f"Adapter returned error: {events[0].error}")

    ev_types = [e.type for e in events]
    assert AgentEventType.TOOL_CALL in ev_types
    assert AgentEventType.TOOL_RESULT in ev_types
    assert AgentEventType.CHUNK in ev_types
    assert AgentEventType.DONE in ev_types

    # Find the tool call event and tool result event
    tool_call_ev = next(e for e in events if e.type == AgentEventType.TOOL_CALL)
    assert tool_call_ev.tool_name == "search_persona_memory"
    assert tool_call_ev.tool_arguments == {"query": "Macintosh"}
    assert tool_call_ev.tool_call_id == "call_abc123"

    tool_result_ev = next(e for e in events if e.type == AgentEventType.TOOL_RESULT)
    assert tool_result_ev.tool_result == {
        "status": "success",
        "results": ["Macintosh team launched in 1984"],
    }

    # Verify final content
    done_ev = next(e for e in events if e.type == AgentEventType.DONE)
    assert done_ev.content == "Based on the retrieved memory, the Macintosh was launched in 1984."
    assert mock_transport.request_count == 2


@pytest.mark.anyio
async def test_openai_compatible_tool_calling_with_datetime_and_model_objects(
    monkeypatch, app
) -> None:
    from datetime import UTC, datetime

    mock_transport = MockStreamingTransport()
    orig_async_client = httpx.AsyncClient

    def mock_client_factory(*args, **kwargs):
        kwargs["transport"] = mock_transport
        return orig_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client_factory)

    app.credentials.create(
        credential_id="mock-openai-datetime-tool",
        provider="openai",
        api_key="sk-mock-datetime-tool-secret",
    )
    adapter = OpenAICompatibleAPIAdapter(
        adapter_id="mock_openai",
        name="Mock OpenAI",
        base_url="https://api.openai.com/v1",
        credential_manager=app.credentials,
        credential_id="mock-openai-datetime-tool",
    )

    cfg = AgentSessionConfig(
        room_id="room_dt_1",
        participant_id="slot_1",
        persona_id="steve_jobs",
        model_id="gpt-4o",
        auth_profile_id="mock-openai-datetime-tool",
    )
    session = await adapter.create_session(cfg)

    # Tool executor mock returning datetime objects
    async def mock_tool_executor(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "success",
            "timestamp": datetime(2026, 8, 24, 21, 0, 0, tzinfo=UTC),
            "nested": {
                "created_at": datetime(1984, 1, 24, 10, 0, 0, tzinfo=UTC),
                "event": "Macintosh Launch",
            },
        }

    turn = AgentTurn(
        turn_id="turn_dt_1",
        user_message="Check launch timestamp",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "search_persona_memory",
                    "description": "Search memory records",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                },
            }
        ],
        metadata={"tool_executor": mock_tool_executor},
    )

    events = []
    async for ev in adapter.send(session, turn):
        events.append(ev)

    # Must not contain error
    error_events = [e for e in events if e.type == AgentEventType.ERROR]
    err_msg = error_events[0].error if error_events else ""
    assert not error_events, f"Unexpected error event: {err_msg}"

    ev_types = [e.type for e in events]
    assert AgentEventType.TOOL_CALL in ev_types
    assert AgentEventType.TOOL_RESULT in ev_types
    assert AgentEventType.DONE in ev_types
    assert mock_transport.request_count == 2
