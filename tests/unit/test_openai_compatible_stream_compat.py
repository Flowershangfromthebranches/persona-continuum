from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from persona_continuum.agent.models import AgentEventType, AgentSessionConfig, AgentTurn
from persona_continuum.agent.protocols.openai_compatible import (
    OpenAICompatibleAPIAdapter,
    _http_timeout_for_turn,
)
from persona_continuum.application.container import PersonaContinuum


class MockByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk.encode("utf-8")


class SequenceTransport(httpx.AsyncBaseTransport):
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = list(responses)
        self.payloads: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raw = await request.aread()
        self.payloads.append(json.loads(raw.decode("utf-8")) if raw else {})
        if not self.responses:
            raise AssertionError("Unexpected extra HTTP request")
        return self.responses.pop(0)


async def _collect(adapter: OpenAICompatibleAPIAdapter, body: str | None = None) -> list[Any]:
    session = await adapter.create_session(
        AgentSessionConfig(
            room_id="room_1",
            participant_id="slot_1",
            persona_id="steve_jobs",
            model_id="stealth/ox-alpha",
        )
    )
    turn = AgentTurn(turn_id="turn_1", user_message=body or "Hello")
    events = []
    async for event in adapter.send(session, turn):
        events.append(event)
    return events


@pytest.mark.anyio
async def test_parses_non_sse_json_completion(
    monkeypatch: pytest.MonkeyPatch, app: PersonaContinuum
) -> None:
    app.credentials.create(
        credential_id="compat-json",
        provider="openai_compatible",
        api_key="sk-test-json",
        base_url="http://127.0.0.1:11434/v1",
    )
    adapter = OpenAICompatibleAPIAdapter(
        adapter_id="api_compat-json",
        name="API: 123",
        base_url="http://127.0.0.1:11434/v1",
        credential_manager=app.credentials,
        credential_id="compat-json",
    )
    payload = {
        "choices": [{"message": {"role": "assistant", "content": "模型已经收到议题并开始回应。"}}]
    }
    transport = SequenceTransport(
        [httpx.Response(200, stream=MockByteStream([json.dumps(payload)]))]
    )

    orig = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    events = await _collect(adapter)
    chunks = [e.content for e in events if e.type == AgentEventType.CHUNK]
    assert "模型已经收到议题并开始回应。" in "".join(chunks)
    assert events[-1].type == AgentEventType.DONE


@pytest.mark.anyio
async def test_retries_400_then_reads_completion(
    monkeypatch: pytest.MonkeyPatch, app: PersonaContinuum
) -> None:
    app.credentials.create(
        credential_id="compat-400",
        provider="openai_compatible",
        api_key="sk-test-400",
        base_url="https://openrouter.ai/api/v1",
    )
    adapter = OpenAICompatibleAPIAdapter(
        adapter_id="api_compat-400",
        name="API: 123",
        base_url="https://openrouter.ai/api/v1",
        credential_manager=app.credentials,
        credential_id="compat-400",
    )
    ok = {"choices": [{"delta": {"content": "retry-ok"}}]}
    error_body = '{"error":{"message":"unknown field tools"}}'
    ok_stream = [f"data: {json.dumps(ok)}\n\n", "data: [DONE]\n\n"]
    transport = SequenceTransport(
        [
            httpx.Response(400, stream=MockByteStream([error_body])),
            httpx.Response(200, stream=MockByteStream(ok_stream)),
        ]
    )
    orig = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    adapter_turn = AgentTurn(
        turn_id="turn_tools",
        user_message="Hi",
        tools=[{"type": "function", "function": {"name": "persona_search_memories"}}],
    )
    session = await adapter.create_session(
        AgentSessionConfig(
            room_id="room_1",
            participant_id="slot_1",
            persona_id="steve_jobs",
            model_id="stealth/ox-alpha",
        )
    )
    events = []
    async for event in adapter.send(session, adapter_turn):
        events.append(event)
    assert any(e.type == AgentEventType.CHUNK and "retry-ok" in e.content for e in events)
    assert "tools" not in transport.payloads[1]


@pytest.mark.anyio
async def test_400_does_not_silently_drop_requested_reasoning(
    monkeypatch: pytest.MonkeyPatch, app: PersonaContinuum
) -> None:
    app.credentials.create(
        credential_id="compat-reasoning-400",
        provider="openai_compatible",
        api_key="sk-test-reasoning-400",
        base_url="https://openrouter.ai/api/v1",
    )
    adapter = OpenAICompatibleAPIAdapter(
        adapter_id="api_compat-reasoning-400",
        name="API: reasoning",
        base_url="https://openrouter.ai/api/v1",
        model_capabilities={
            "stealth/ox-alpha": {
                "reasoning_capability": {
                    "mode": "manual_config",
                    "supported_efforts": ["high"],
                    "binding_strategy": "manual_config",
                    "verified": False,
                    "source": "test",
                }
            }
        },
        credential_manager=app.credentials,
        credential_id="compat-reasoning-400",
    )
    transport = SequenceTransport(
        [
            httpx.Response(
                400,
                stream=MockByteStream(['{"error":{"message":"unsupported reasoning"}}']),
            )
        ]
    )
    orig = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    session = await adapter.create_session(
        AgentSessionConfig(
            room_id="room_1",
            participant_id="slot_1",
            persona_id="steve_jobs",
            model_id="stealth/ox-alpha",
            reasoning_effort="high",
        )
    )
    events = [
        event
        async for event in adapter.send(
            session,
            AgentTurn(turn_id="turn_reasoning", user_message="Hi"),
        )
    ]
    assert events[-1].type == AgentEventType.ERROR
    assert events[-1].error == "REASONING_BINDING_REJECTED"
    assert len(transport.payloads) == 1
    assert transport.payloads[0]["reasoning_effort"] == "high"


@pytest.mark.anyio
async def test_unrelated_400_preserves_reasoning_during_compat_retry(
    monkeypatch: pytest.MonkeyPatch, app: PersonaContinuum
) -> None:
    app.credentials.create(
        credential_id="compat-reasoning-tools-400",
        provider="openai_compatible",
        api_key="sk-test-reasoning-tools-400",
        base_url="https://openrouter.ai/api/v1",
    )
    adapter = OpenAICompatibleAPIAdapter(
        adapter_id="api_compat-reasoning-tools-400",
        base_url="https://openrouter.ai/api/v1",
        credential_manager=app.credentials,
        credential_id="compat-reasoning-tools-400",
        model_capabilities={
            "stealth/ox-alpha": {
                "reasoning_efforts": ["high"],
                "default_reasoning_effort": "high",
            }
        },
    )
    ok = {"choices": [{"delta": {"content": "retry-with-reasoning"}}]}
    transport = SequenceTransport(
        [
            httpx.Response(
                400,
                stream=MockByteStream(['{"error":{"message":"unknown field tools"}}']),
            ),
            httpx.Response(
                200,
                stream=MockByteStream(
                    [f"data: {json.dumps(ok)}\n\n", "data: [DONE]\n\n"]
                ),
            ),
        ]
    )
    original_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    session = await adapter.create_session(
        AgentSessionConfig(
            room_id="room_1",
            participant_id="slot_1",
            persona_id="steve_jobs",
            model_id="stealth/ox-alpha",
            reasoning_effort="high",
        )
    )
    events = [
        event
        async for event in adapter.send(
            session,
            AgentTurn(
                turn_id="turn_reasoning_tools",
                user_message="Hi",
                tools=[{"type": "function", "function": {"name": "search"}}],
            ),
        )
    ]
    assert events[-1].type == AgentEventType.DONE
    assert transport.payloads[1]["reasoning_effort"] == "high"
    assert "tools" not in transport.payloads[1]


@pytest.mark.anyio
async def test_http_read_timeout_uses_phase_idle_budget(app: PersonaContinuum) -> None:
    app.credentials.create(
        credential_id="compat-timeout-budget",
        provider="openai_compatible",
        api_key="sk-test-timeout-budget",
        base_url="https://example.invalid/v1",
    )
    adapter = OpenAICompatibleAPIAdapter(
        credential_manager=app.credentials,
        credential_id="compat-timeout-budget",
    )
    session = await adapter.create_session(
        AgentSessionConfig(
            room_id="room_1",
            participant_id="slot_1",
            persona_id="steve_jobs",
        )
    )
    timeout = _http_timeout_for_turn(
        session,
        AgentTurn(user_message="research", metadata={"phase": "public_research"}),
    )
    assert timeout.read == 180.0


@pytest.mark.anyio
async def test_google_system_prompt_is_sent_once(
    monkeypatch: pytest.MonkeyPatch, app: PersonaContinuum
) -> None:
    app.credentials.create(
        credential_id="google-system-once",
        provider="google",
        api_key="google-test-key",
        base_url="https://generativelanguage.googleapis.com/v1beta",
    )
    adapter = OpenAICompatibleAPIAdapter(
        credential_manager=app.credentials,
        credential_id="google-system-once",
    )
    response = {
        "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
    }
    transport = SequenceTransport(
        [
            httpx.Response(
                200,
                stream=MockByteStream([f"data: {json.dumps(response)}\n\n"]),
            )
        ]
    )
    original_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    session = await adapter.create_session(
        AgentSessionConfig(
            room_id="room_1",
            participant_id="slot_1",
            persona_id="steve_jobs",
            model_id="gemini-test",
        )
    )
    events = [
        event
        async for event in adapter.send(
            session,
            AgentTurn(
                system_prompt="SYSTEM_SENTINEL_ONCE",
                user_message="USER_SENTINEL",
            ),
        )
    ]
    assert events[-1].type == AgentEventType.DONE
    payload = transport.payloads[0]
    assert payload["systemInstruction"]["parts"][0]["text"] == "SYSTEM_SENTINEL_ONCE"
    user_text = payload["contents"][0]["parts"][0]["text"]
    assert "SYSTEM_SENTINEL_ONCE" not in user_text
    assert "USER_SENTINEL" in user_text


@pytest.mark.anyio
async def test_reasoning_only_stream_becomes_content(
    monkeypatch: pytest.MonkeyPatch, app: PersonaContinuum
) -> None:
    app.credentials.create(
        credential_id="compat-think",
        provider="openai_compatible",
        api_key="sk-test-think",
        base_url="http://127.0.0.1:1234/v1",
    )
    adapter = OpenAICompatibleAPIAdapter(
        adapter_id="api_compat-think",
        name="API: 123",
        base_url="http://127.0.0.1:1234/v1",
        credential_manager=app.credentials,
        credential_id="compat-think",
    )
    think = {"choices": [{"delta": {"reasoning_content": "先把议题展开成可讨论的问题。"}}]}
    transport = SequenceTransport(
        [
            httpx.Response(
                200,
                stream=MockByteStream([f"data: {json.dumps(think)}\n\n", "data: [DONE]\n\n"]),
            )
        ]
    )
    orig = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    events = await _collect(adapter)
    chunks = [e.content for e in events if e.type == AgentEventType.CHUNK]
    assert "先把议题展开成可讨论的问题。" in "".join(chunks)
