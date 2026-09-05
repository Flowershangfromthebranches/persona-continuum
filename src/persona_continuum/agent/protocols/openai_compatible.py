import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx

from persona_continuum.agent.adapter import (
    AgentAdapter,
    AgentSession,
    build_runtime_binding_snapshot,
)
from persona_continuum.agent.context_capability import ContextWindowMode
from persona_continuum.agent.models import (
    AgentCapabilityFlags,
    AgentEvent,
    AgentEventType,
    AgentProbeResult,
    AgentSessionConfig,
    AgentStatus,
    AgentTurn,
    ModelCapability,
    OutputStreamingMode,
    PromptMode,
    ReasoningCapability,
    ReasoningCapabilityMode,
    RuntimeBindingSnapshot,
    SelectionStrategy,
    StructuredOutputMode,
)
from persona_continuum.agent.prompt import AgentPromptRenderer
from persona_continuum.agent.response_collector import (
    AgentRuntimeError,
    AgentTransportError,
    ReasoningBindingRejectedError,
    agent_error_event,
    compact_tool_result,
    retain_tool_artifact,
)
from persona_continuum.agent.timeout import timeout_budget_for_turn
from persona_continuum.auth.credentials import CredentialManager, CredentialProvider
from persona_continuum.auth.profiles import redact_secrets
from persona_continuum.numeric import safe_int


def _http_timeout_for_turn(session: AgentSession, turn: AgentTurn) -> httpx.Timeout:
    """Keep provider I/O inside the same phase-aware timeout contract."""

    budget = timeout_budget_for_turn(
        session.config,
        turn,
        output_streaming_mode=OutputStreamingMode.PROTOCOL_STREAM,
    )
    return httpx.Timeout(
        connect=min(8.0, budget.idle_timeout_seconds),
        read=budget.idle_timeout_seconds,
        write=min(30.0, budget.idle_timeout_seconds),
        pool=min(8.0, budget.idle_timeout_seconds),
    )


def _provider_rejected_reasoning(error_text: str) -> bool:
    normalized = str(error_text or "").casefold()
    return any(
        marker in normalized
        for marker in (
            "reasoning_effort",
            "reasoning effort",
            "unsupported reasoning",
            "invalid reasoning",
        )
    )


def _choice_text(choice: dict[str, Any]) -> tuple[str, str]:
    """Return (content, thinking) from a streaming or completed chat choice."""
    raw_delta = choice.get("delta")
    raw_message = choice.get("message")
    delta: dict[str, Any] = raw_delta if isinstance(raw_delta, dict) else {}
    message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}
    thinking = (
        delta.get("reasoning_content")
        or delta.get("reasoning")
        or delta.get("thinking")
        or message.get("reasoning_content")
        or message.get("reasoning")
        or ""
    )
    content = delta.get("content")
    if content in (None, ""):
        content = message.get("content")
    if content in (None, ""):
        content = choice.get("text")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        content = "".join(parts)
    return str(content or ""), str(thinking or "")


def _parse_completion_line(line: str) -> dict[str, Any] | None:
    text = line.strip()
    if not text or text.startswith(":"):
        return None
    if text == "data: [DONE]" or text == "[DONE]":
        return {"done": True}
    if text.startswith("data: "):
        text = text[6:].strip()
        if text == "[DONE]":
            return {"done": True}
    if not text.startswith("{") and not text.startswith("["):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _safe_json_dumps(val: Any) -> str:
    def _json_default(obj: Any) -> Any:
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if hasattr(obj, "model_dump"):
            return obj.model_dump(mode="json")
        return str(obj)

    if isinstance(val, (dict, list)):
        try:
            return json.dumps(val, ensure_ascii=False, default=_json_default)
        except Exception:
            return str(val)
    return str(val)


class OpenAICompatibleAPIAdapter(AgentAdapter):
    # The /models listing reports each model's window; the window itself is
    # fixed by the provider and no context parameter is sent.
    context_window_mode = ContextWindowMode.DISCOVERABLE

    def __init__(
        self,
        adapter_id: str = "openai_compatible",
        name: str = "OpenAI Compatible API",
        base_url: str = "https://api.openai.com/v1",
        auth_env_var: str | None = "OPENAI_API_KEY",
        default_model: str = "gpt-4o",
        custom_headers: dict[str, Any] | None = None,
        model_capabilities: dict[str, Any] | None = None,
        provider_type: str | None = None,
        credential_manager: CredentialManager | None = None,
        credential_id: str | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.auth_env_var = auth_env_var
        self.default_model = default_model
        self.custom_headers = custom_headers or {}
        self.model_capabilities = model_capabilities or {}
        self.provider_type = str(provider_type or "openai_compatible").lower()
        self.credential_id = credential_id or adapter_id.removeprefix("api_")
        self.credential_manager = credential_manager
        self.prompt_mode = PromptMode.NATIVE_ROLES
        self.structured_output_mode = StructuredOutputMode.JSON_MODE
        self.output_streaming_mode = OutputStreamingMode.PROTOCOL_STREAM
        self.protocols = ["openai_compatible_http"]

    def capability_cache_identity(self) -> str:
        """Fingerprint non-secret profile data that changes model capabilities."""

        payload = {
            "base_url": self.base_url,
            "credential_id": self.credential_id,
            "default_model": self.default_model,
            "model_capabilities": self.model_capabilities,
            "provider_type": self.provider_type,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return f"api:{hashlib.sha256(encoded).hexdigest()}"

    async def probe(self) -> AgentProbeResult:
        credential_ready = bool(
            self.credential_manager and self.credential_manager.has(self.credential_id)
        )
        if not credential_ready:
            return AgentProbeResult(
                id=self.adapter_id,
                name=self.name,
                status=AgentStatus.AUTH_REQUIRED,
                binary_path=self.base_url,
                version="API Endpoint",
                auth_status="Missing encrypted CredentialManager credential",
                protocols=["openai_compatible_http"],
                capabilities=AgentCapabilityFlags(
                    streaming=True,
                    persistent_session=False,
                    model_selection=SelectionStrategy.STARTUP,
                    reasoning_selection=SelectionStrategy.STARTUP,
                    structured_output_mode=self.structured_output_mode,
                ),
                models=self._configured_models(),
                status_detail="API key is not configured in CredentialManager",
            )

        connection = await self.credential_manager.test_connection(self.credential_id)  # type: ignore[union-attr]
        if not connection["connected"]:
            error = str(connection.get("error") or "Provider connection failed")
            return AgentProbeResult(
                id=self.adapter_id,
                name=self.name,
                status=(AgentStatus.AUTH_REQUIRED if "401" in error else AgentStatus.BROKEN),
                binary_path=self.base_url,
                version="API Endpoint",
                auth_status="credential_rejected",
                protocols=["provider_http"],
                models=self._configured_models(),
                status_detail=error,
            )

        models = await self.list_models()
        return AgentProbeResult(
            id=self.adapter_id,
            name=self.name,
            status=AgentStatus.READY,
            binary_path=self.base_url,
            version="API Endpoint",
            auth_status="configured",
            protocols=["openai_compatible_http"],
            capabilities=AgentCapabilityFlags(
                streaming=True,
                persistent_session=False,
                model_selection=SelectionStrategy.STARTUP,
                reasoning_discovery=True,
                reasoning_selection=SelectionStrategy.STARTUP,
                structured_output_mode=self.structured_output_mode,
            ),
            models=models,
            status_detail=None,
        )

    async def list_models(self) -> list[ModelCapability]:
        try:
            base_url, headers = self._request_context()
        except Exception:
            return self._configured_models()

        url = f"{base_url}/models"
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    raw_items = data.get("data", []) if isinstance(data, dict) else data
                    discovered: list[ModelCapability] = []
                    for item in raw_items:
                        m_id = str(item.get("id") if isinstance(item, dict) else item)
                        if m_id:
                            configured = self._reasoning_capability(m_id)
                            reported = self._model_list_reasoning_capability(item)
                            capability = (
                                reported
                                if configured.mode == ReasoningCapabilityMode.UNKNOWN
                                and reported.mode != ReasoningCapabilityMode.UNKNOWN
                                else configured
                            )
                            efforts = list(capability.supported_efforts)
                            default_effort = capability.default_effort

                            discovered.append(
                                ModelCapability(
                                    id=m_id,
                                    display_name=(
                                        str(item.get("name") or m_id)
                                        if isinstance(item, dict)
                                        else m_id
                                    ),
                                    provider=self.adapter_id,
                                    supported_reasoning_efforts=efforts,
                                    default_reasoning_effort=default_effort,
                                    context_window=(
                                        safe_int(
                                            item.get("context_length")
                                            or item.get("context_window"),
                                            default=None,
                                            minimum=0,
                                        )
                                        if isinstance(item, dict)
                                        else None
                                    ),
                                    source=(
                                        "protocol_model_list"
                                        if capability.source == "protocol_model_list"
                                        else "dynamic"
                                    ),
                                    reasoning_selection=(
                                        SelectionStrategy.STARTUP
                                        if efforts
                                        else SelectionStrategy.UNSUPPORTED
                                    ),
                                    reasoning_capability=capability,
                                )
                            )
                    if discovered:
                        return discovered
        except Exception:
            pass

        return self._configured_models()

    def _configured_models(self) -> list[ModelCapability]:
        capability = self._reasoning_capability(self.default_model)
        return [
            ModelCapability(
                id=self.default_model,
                display_name=self.default_model,
                provider=self.adapter_id,
                supported_reasoning_efforts=list(capability.supported_efforts),
                default_reasoning_effort=capability.default_effort,
                source="config",
                reasoning_capability=capability,
            )
        ]

    async def create_session(self, config: AgentSessionConfig) -> AgentSession:
        session = AgentSession(
            config=config,
            is_active=True,
            session_data={
                "history": [],
                "protocol": "openai_compatible_http",
                "effective_model": config.model_id or self.default_model,
                "effective_reasoning": config.reasoning_effort,
                "model_selection_applied": True,
                "reasoning_selection_applied": True,
            },
        )
        session._cancel_event = asyncio.Event()
        return session

    async def bind_runtime(self, session: AgentSession) -> RuntimeBindingSnapshot:
        model = session.config.model_id or self.default_model
        capability = self._reasoning_capability(model)
        requested = str(session.config.reasoning_effort or "").strip().lower()
        requested = "" if requested in {"", "none", "default", "auto"} else requested
        effective: str | None = None
        verified = not requested
        if requested:
            can_bind = capability.mode in {
                ReasoningCapabilityMode.NATIVE_EFFORT,
                ReasoningCapabilityMode.MANUAL_CONFIG,
            } and requested in {str(item).lower() for item in capability.supported_efforts} and (
                capability.mode == ReasoningCapabilityMode.MANUAL_CONFIG
                or capability.verified
            )
            if can_bind:
                effective = session.config.reasoning_effort
                verified = bool(capability.verified)
        session.session_data["effective_reasoning"] = effective
        return build_runtime_binding_snapshot(
            self,
            session,
            protocol="openai_compatible_http",
            model_verified=True,
            reasoning_verified=verified,
            verification_method=capability.binding_strategy,
        )

    async def send(self, session: AgentSession, turn: AgentTurn) -> AsyncIterator[AgentEvent]:
        try:
            base_url, headers = self._request_context()
            credential = self.credential_manager.get(self.credential_id)  # type: ignore[union-attr]
        except Exception as exc:
            yield agent_error_event(
                AgentTransportError(
                    "Agent credential context could not be resolved",
                    phase="session_prompt",
                    diagnostics={
                        "protocol": "openai_compatible_http",
                        "exception_type": type(exc).__name__,
                    },
                ),
                protocol="openai_compatible_http",
            )
            return
        reasoning_error = self._reasoning_binding_error(session, credential.provider.value)
        if reasoning_error is not None:
            yield agent_error_event(reasoning_error, protocol="openai_compatible_http")
            return
        if credential.provider == CredentialProvider.ANTHROPIC:
            async for event in self._send_anthropic(session, turn, base_url, headers):
                yield event
            return
        if credential.provider == CredentialProvider.GOOGLE:
            async for event in self._send_google(session, turn, base_url, headers):
                yield event
            return
        headers["Content-Type"] = "application/json"

        try:
            self._prepare_inline_attachments(turn)
        except AgentRuntimeError as exc:
            yield agent_error_event(exc, protocol="openai_compatible_http")
            return
        try:
            wire_messages: list[dict[str, Any]] = AgentPromptRenderer.render_for_native_roles(
                turn
            )
            self._apply_provider_image_parts(wire_messages, turn)
            wire_bytes = len(
                json.dumps(wire_messages, ensure_ascii=False, default=str).encode("utf-8")
            )
        except AgentRuntimeError as exc:
            yield agent_error_event(exc, protocol="openai_compatible_http")
            return
        turn.metadata["estimated_wire_bytes"] = wire_bytes
        messages: list[dict[str, Any]] = AgentPromptRenderer.render_for_native_roles(turn)
        self._apply_provider_image_parts(messages, turn)

        model = session.config.model_id or self.default_model
        payload: dict[str, Any] = {
            "model": model,
            "stream": True,
        }
        # Official OpenAI accepts this; many compatible gateways return 400.
        if "openai.com" in base_url:
            payload["stream_options"] = {"include_usage": True}

        if str(session.config.reasoning_effort or "").strip().casefold() not in {
            "",
            "none",
            "default",
            "auto",
        }:
            payload["reasoning_effort"] = session.config.reasoning_effort

        if turn.tools:
            payload["tools"] = turn.tools
            payload["tool_choice"] = "auto"
        self._add_structured_output_request(payload, turn)

        url = f"{base_url}/chat/completions"
        full_content: list[str] = []
        thinking_parts: list[str] = []
        usage: dict[str, int] = {}
        max_tool_iterations = 5
        iteration = 0
        compat_retries = 0

        try:
            while iteration < max_tool_iterations:
                iteration += 1
                payload["messages"] = list(messages)
                round_content: list[str] = []
                # Buffer streamed tool calls: index -> {"id": str, "name": str, "args": str}
                buffered_tools: dict[int, dict[str, str]] = {}
                retry_without_extras = False

                async with (
                    httpx.AsyncClient(timeout=_http_timeout_for_turn(session, turn)) as client,
                    client.stream("POST", url, headers=headers, json=payload) as response,
                ):
                    if response.status_code != 200:
                        err_body = await response.aread()
                        err_text = redact_secrets(err_body.decode("utf-8", errors="replace"))
                        if response.status_code == 400 and compat_retries < 2:
                            requested_reasoning = session.config.reasoning_effort
                            if (
                                requested_reasoning
                                and requested_reasoning != "none"
                                and _provider_rejected_reasoning(err_text)
                            ):
                                yield agent_error_event(
                                    ReasoningBindingRejectedError(
                                        "Provider rejected the selected reasoning effort; "
                                        "refusing a silent downgrade",
                                        phase="session_update",
                                        diagnostics={
                                            "protocol": "openai_compatible_http",
                                            "status_code": response.status_code,
                                            "requested_reasoning": requested_reasoning,
                                            "diagnostic": err_text[-1000:],
                                        },
                                    ),
                                    protocol="openai_compatible_http",
                                )
                                return
                            payload.pop("stream_options", None)
                            payload.pop("tools", None)
                            payload.pop("tool_choice", None)
                            if compat_retries >= 1:
                                payload["stream"] = False
                            compat_retries += 1
                            iteration -= 1
                            retry_without_extras = True
                        else:
                            yield agent_error_event(
                                AgentTransportError(
                                    "Agent HTTP request failed",
                                    phase="session_update",
                                    diagnostics={
                                        "protocol": "openai_compatible_http",
                                        "status_code": response.status_code,
                                        "diagnostic": err_text[-1000:],
                                    },
                                    retriable=response.status_code >= 500
                                    or response.status_code == 429,
                                ),
                                protocol="openai_compatible_http",
                            )
                            return

                    if retry_without_extras:
                        continue

                    async for line in response.aiter_lines():
                        if session._cancel_event and session._cancel_event.is_set():
                            yield AgentEvent(
                                type=AgentEventType.DONE,
                                content="".join(full_content),
                                metadata={"cancelled": True},
                            )
                            return

                        data = _parse_completion_line(line)
                        session.touch_activity(
                            "http_sse_frame",
                            byte_count=len(str(line).encode("utf-8", errors="replace")),
                            metadata={"event_type": "completion_chunk"},
                        )
                        if not data:
                            continue
                        if data.get("done"):
                            break
                        raw_usage = data.get("usage")
                        if isinstance(raw_usage, dict):
                            usage = {
                                "input_tokens": safe_int(
                                    raw_usage.get("prompt_tokens")
                                    or raw_usage.get("input_tokens")
                                    or 0,
                                    default=0,
                                    minimum=0,
                                )
                                or 0,
                                "output_tokens": safe_int(
                                    raw_usage.get("completion_tokens")
                                    or raw_usage.get("output_tokens")
                                    or 0,
                                    default=0,
                                    minimum=0,
                                )
                                or 0,
                                "total_tokens": safe_int(
                                    raw_usage.get("total_tokens"), default=0, minimum=0
                                )
                                or 0,
                            }
                        choices = data.get("choices") or []
                        if not choices or not isinstance(choices[0], dict):
                            continue
                        choice = choices[0]
                        content, reasoning = _choice_text(choice)
                        if reasoning:
                            thinking_parts.append(reasoning)
                            yield AgentEvent(type=AgentEventType.THINKING, thinking=reasoning)
                        if content:
                            yield AgentEvent(type=AgentEventType.CHUNK, content=content)
                            round_content.append(content)
                            full_content.append(content)

                        raw_delta = choice.get("delta")
                        raw_message = choice.get("message")
                        delta: dict[str, Any] = raw_delta if isinstance(raw_delta, dict) else {}
                        message: dict[str, Any] = (
                            raw_message if isinstance(raw_message, dict) else {}
                        )
                        tool_calls = delta.get("tool_calls") or message.get("tool_calls")
                        if tool_calls:
                            for tc in tool_calls:
                                idx = safe_int(tc.get("index", 0), default=0, minimum=0) or 0
                                if idx not in buffered_tools:
                                    buffered_tools[idx] = {
                                        "id": tc.get("id") or "",
                                        "name": "",
                                        "args": "",
                                    }
                                if tc.get("id"):
                                    buffered_tools[idx]["id"] = tc["id"]
                                fn = tc.get("function", {})
                                if fn.get("name"):
                                    buffered_tools[idx]["name"] += fn["name"]
                                if fn.get("arguments"):
                                    buffered_tools[idx]["args"] += fn["arguments"]

                if not full_content and thinking_parts and not buffered_tools:
                    fallback = "".join(thinking_parts).strip()
                    if fallback:
                        yield AgentEvent(type=AgentEventType.CHUNK, content=fallback)
                        round_content.append(fallback)
                        full_content.append(fallback)

                # If no tool calls in this round, generation is complete
                if not buffered_tools:
                    break

                # Process all buffered tool calls
                tool_executor = turn.metadata.get("tool_executor")
                assistant_tool_calls: list[dict[str, Any]] = []

                for idx in sorted(buffered_tools.keys()):
                    t_info = buffered_tools[idx]
                    tc_id_str = str(t_info.get("id") or f"call_{idx}_{iteration}")
                    fn_name_str = str(t_info.get("name") or "")
                    raw_args_str = str(t_info.get("args") or "")
                    try:
                        parsed_args = json.loads(raw_args_str) if raw_args_str else {}
                    except Exception:
                        parsed_args = {}

                    yield AgentEvent(
                        type=AgentEventType.TOOL_CALL,
                        tool_call_id=tc_id_str,
                        tool_name=fn_name_str,
                        tool_arguments=parsed_args,
                    )

                    assistant_tool_calls.append(
                        {
                            "id": tc_id_str,
                            "type": "function",
                            "function": {
                                "name": fn_name_str,
                                "arguments": raw_args_str or "{}",
                            },
                        }
                    )

                # Append assistant message with tool_calls
                messages.append(
                    {
                        "role": "assistant",
                        "content": "".join(round_content) if round_content else None,
                        "tool_calls": assistant_tool_calls,
                    }
                )

                # Execute tools and append tool results
                for tc_dict in assistant_tool_calls:
                    tc_id = str(tc_dict["id"])
                    fn_dict = tc_dict.get("function") or {}
                    fn_name = str(fn_dict.get("name") or "")
                    fn_args_raw = str(fn_dict.get("arguments") or "{}")
                    try:
                        parsed_args = json.loads(fn_args_raw)
                    except Exception:
                        parsed_args = {}

                    tool_result_data: Any = None
                    if callable(tool_executor):
                        try:
                            res = tool_executor(fn_name, parsed_args)
                            if asyncio.iscoroutine(res):
                                res = await res
                            tool_result_data = res
                        except Exception as exc:
                            tool_result_data = {"status": "error", "error": str(exc)}
                    else:
                        tool_result_data = {
                            "status": "success",
                            "executed": fn_name,
                            "args": parsed_args,
                        }

                    compacted_tool_result = compact_tool_result(
                        tool_result_data,
                        tool_name=fn_name,
                        artifact_ref=f"tool-result:{tc_id}",
                    )
                    retain_tool_artifact(
                        session.session_data, compacted_tool_result, tool_result_data
                    )
                    yield AgentEvent(
                        type=AgentEventType.TOOL_RESULT,
                        tool_call_id=tc_id,
                        tool_name=fn_name,
                        tool_result=compacted_tool_result,
                        metadata={"artifact_ref": f"tool-result:{tc_id}"},
                    )

                    tool_res_str = _safe_json_dumps(tool_result_data)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": tool_res_str,
                        }
                    )

            yield AgentEvent(
                type=AgentEventType.DONE,
                content="".join(full_content),
                metadata={
                    "usage": usage,
                    "model": model,
                    "provider": credential.provider.value,
                    "decision_source": "llm",
                },
            )

        except Exception as exc:
            yield agent_error_event(exc, protocol="openai_compatible_http")

    async def _send_anthropic(
        self,
        session: AgentSession,
        turn: AgentTurn,
        base_url: str,
        headers: dict[str, str],
    ) -> AsyncIterator[AgentEvent]:
        headers["Content-Type"] = "application/json"
        model = session.config.model_id or self.default_model
        envelope = AgentPromptRenderer.envelope(turn)
        native = AgentPromptRenderer.render_for_native_roles(turn)
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": 2048,
            "stream": True,
            "messages": [
                self._anthropic_message(message, turn)
                for message in native
                if message.get("role") != "system"
            ],
        }
        system_parts = [
            str(message.get("content") or "")
            for message in AgentPromptRenderer.render_for_native_roles(turn)
            if message.get("role") == "system"
        ]
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if envelope.expected_output is not None:
            payload["system"] = (
                f"{payload.get('system', '')}\n\n"
                "Return exactly one JSON value matching this schema:\n"
                f"{AgentPromptRenderer._serialize_schema(envelope.expected_output)}"
            ).strip()
        full_content: list[str] = []
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        try:
            async with (
                httpx.AsyncClient(timeout=_http_timeout_for_turn(session, turn)) as client,
                client.stream(
                    "POST", f"{base_url}/messages", headers=headers, json=payload
                ) as response,
            ):
                if response.status_code != 200:
                    body = await response.aread()
                    yield AgentEvent(
                        type=AgentEventType.ERROR,
                        error=(
                            f"API Error {response.status_code}: "
                            f"{redact_secrets(body.decode('utf-8', errors='replace'))}"
                        ),
                    )
                    return
                async for line in response.aiter_lines():
                    session.touch_activity(
                        "http_sse_frame",
                        byte_count=len(str(line).encode("utf-8", errors="replace")),
                        metadata={"event_type": "anthropic"},
                    )
                    if session._cancel_event and session._cancel_event.is_set():
                        yield AgentEvent(
                            type=AgentEventType.DONE,
                            content="".join(full_content),
                            metadata={"cancelled": True, "usage": usage},
                        )
                        return
                    if not line.startswith("data: "):
                        continue
                    try:
                        data = json.loads(line[6:])
                    except (TypeError, ValueError):
                        continue
                    event_type = str(data.get("type") or "")
                    if event_type == "message_start":
                        raw = (data.get("message") or {}).get("usage") or {}
                        usage["input_tokens"] = safe_int(
                            raw.get("input_tokens"), default=0, minimum=0
                        ) or 0
                    elif event_type == "content_block_delta":
                        delta = data.get("delta") or {}
                        text = str(delta.get("text") or "")
                        if text:
                            full_content.append(text)
                            yield AgentEvent(type=AgentEventType.CHUNK, content=text)
                    elif event_type == "message_delta":
                        raw = data.get("usage") or {}
                        usage["output_tokens"] = safe_int(
                            raw.get("output_tokens"), default=0, minimum=0
                        ) or 0
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
            yield AgentEvent(
                type=AgentEventType.DONE,
                content="".join(full_content),
                metadata={
                    "usage": usage,
                    "model": model,
                    "provider": CredentialProvider.ANTHROPIC.value,
                    "decision_source": "llm",
                },
            )
        except Exception as exc:
            yield agent_error_event(exc, protocol="anthropic_http")

    async def _send_google(
        self,
        session: AgentSession,
        turn: AgentTurn,
        base_url: str,
        headers: dict[str, str],
    ) -> AsyncIterator[AgentEvent]:
        headers["Content-Type"] = "application/json"
        model = session.config.model_id or self.default_model
        envelope = AgentPromptRenderer.envelope(turn)
        native = AgentPromptRenderer.render_for_native_roles(turn)
        google_parts: list[dict[str, Any]] = []
        for message in native:
            if message.get("role") != "user":
                continue
            google_parts.extend(self._google_parts(message, turn))
        prompt = AgentPromptRenderer.render_for_single_prompt(turn, include_system=False)
        if google_parts:
            text_idx = next(
                (
                    idx
                    for idx, part in enumerate(google_parts)
                    if part.get("text") is not None
                ),
                None,
            )
            if text_idx is not None:
                google_parts[text_idx] = {"text": prompt}
            else:
                google_parts.insert(0, {"text": prompt})
        else:
            google_parts = [{"text": prompt}]
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": google_parts}],
        }
        if envelope.system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": envelope.system_prompt}]}
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        full_content: list[str] = []
        url = f"{base_url}/models/{model}:streamGenerateContent?alt=sse"
        try:
            async with (
                httpx.AsyncClient(timeout=_http_timeout_for_turn(session, turn)) as client,
                client.stream("POST", url, headers=headers, json=payload) as response,
            ):
                if response.status_code != 200:
                    body = await response.aread()
                    yield AgentEvent(
                        type=AgentEventType.ERROR,
                        error=(
                            f"API Error {response.status_code}: "
                            f"{redact_secrets(body.decode('utf-8', errors='replace'))}"
                        ),
                    )
                    return
                async for line in response.aiter_lines():
                    session.touch_activity(
                        "http_sse_frame",
                        byte_count=len(str(line).encode("utf-8", errors="replace")),
                        metadata={"event_type": "google"},
                    )
                    if session._cancel_event and session._cancel_event.is_set():
                        yield AgentEvent(
                            type=AgentEventType.DONE,
                            content="".join(full_content),
                            metadata={"cancelled": True, "usage": usage},
                        )
                        return
                    if not line.startswith("data: "):
                        continue
                    try:
                        data = json.loads(line[6:])
                    except (TypeError, ValueError):
                        continue
                    for candidate in data.get("candidates") or []:
                        content = candidate.get("content") or {}
                        for part in content.get("parts") or []:
                            text = str(part.get("text") or "")
                            if text:
                                full_content.append(text)
                                yield AgentEvent(type=AgentEventType.CHUNK, content=text)
                    raw = data.get("usageMetadata") or {}
                    usage = {
                        "input_tokens": safe_int(
                            raw.get("promptTokenCount"), default=0, minimum=0
                        )
                        or 0,
                        "output_tokens": safe_int(
                            raw.get("candidatesTokenCount"), default=0, minimum=0
                        )
                        or 0,
                        "total_tokens": safe_int(
                            raw.get("totalTokenCount"), default=0, minimum=0
                        )
                        or 0,
                    }
            yield AgentEvent(
                type=AgentEventType.DONE,
                content="".join(full_content),
                metadata={
                    "usage": usage,
                    "model": model,
                    "provider": CredentialProvider.GOOGLE.value,
                    "decision_source": "llm",
                },
            )
        except Exception as exc:
            yield agent_error_event(exc, protocol="google_http")

    async def cancel(self, session: AgentSession) -> None:
        session.request_cancel()

    async def close(self, session: AgentSession) -> None:
        session.mark_closed()

    def _prepare_inline_attachments(self, turn: AgentTurn) -> None:
        """Materialise canonical attachments into inline base64 at the boundary.

        This is the ONLY place where room attachments become base64: the HTTP
        provider boundary.  Bytes are read lazily here (never in the Room
        layer), validated against the inline media budget, and appended to
        ``metadata["inline_images"]`` for the renderer/converters below.
        CLI transports never reach this code path.
        """

        from persona_continuum.agent.media_transport import (
            check_inline_budget,
            media_input_mode_for,
            read_attachment_base64,
            require_consumable_or_raise,
        )
        from persona_continuum.agent.models import AgentAttachment, MediaInputMode

        attachments = [
            AgentAttachment.model_validate(a) if isinstance(a, dict) else a
            for a in (turn.attachments or [])
            if isinstance(a, (dict, AgentAttachment))
        ]
        if not attachments:
            return
        uploads_root = self._attachments_root()
        inline = list(self._inline_images(turn))
        seen = {item["data"] for item in inline}
        inline_texts: list[str] = []
        for attachment in attachments:
            mode = media_input_mode_for(
                self._media_capabilities(), attachment.kind
            )
            if mode == MediaInputMode.EXTRACTED_CONTENT.value:
                from persona_continuum.agent.media_transport import extract_attachment_text

                extracted = attachment.extracted_text or extract_attachment_text(
                    attachment, uploads_root
                )
                if extracted:
                    inline_texts.append(
                        f"[附件 {attachment.filename}（{attachment.mime_type}）提取内容]\n"
                        f"{extracted}"
                    )
                    continue
            if mode != MediaInputMode.INLINE_BASE64.value:
                require_consumable_or_raise(
                    attachment, mode, adapter_id=self.adapter_id
                )
                continue
            check_inline_budget(attachment)
            data = read_attachment_base64(attachment, uploads_root)
            if data in seen:
                continue
            seen.add(data)
            inline.append(
                {"media_type": attachment.mime_type.lower(), "data": data}
            )
        if inline_texts:
            turn.user_message = (
                f"{turn.user_message}\n\n" + "\n\n".join(inline_texts)
                if turn.user_message
                else "\n\n".join(inline_texts)
            )
        metadata = dict(turn.metadata or {})
        metadata["inline_images"] = inline
        turn.metadata = metadata

    def _attachments_root(self) -> Path:
        from persona_continuum.config import Config

        override = getattr(self, "_session_uploads_root", None)
        if override:
            return Path(str(override))
        return Config().room_uploads_dir

    def _media_capabilities(self) -> AgentCapabilityFlags:
        from persona_continuum.agent.models import AgentCapabilityFlags

        return AgentCapabilityFlags(
            images=True,
            media_input_modes={
                "image": "inline_base64",
                "video": "inline_base64",
                "audio": "inline_base64",
                "file": "extracted_content",
            },
        )

    @staticmethod
    def _inline_images(turn: AgentTurn) -> list[dict[str, str]]:
        """Inline image payloads carried on this turn (validated, deduped)."""

        raw = (turn.metadata or {}).get("inline_images")
        if not isinstance(raw, list):
            return []
        images: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            media_type = str(item.get("media_type") or "").strip().lower()
            data = "".join(str(item.get("data") or "").split())
            if not media_type.startswith("image/") or not data:
                continue
            if data in seen:
                continue
            seen.add(data)
            images.append({"media_type": media_type, "data": data})
        return images

    def _apply_provider_image_parts(
        self, messages: list[dict[str, Any]], turn: AgentTurn
    ) -> None:
        """Attach OpenAI-style image parts to the last user message in place.

        The renderer already emits the canonical ``image_url`` content blocks;
        this only keeps provider-specific follow-ups (Google/Anthropic use
        their own converters below) from seeing a shape they cannot parse.
        Plain-text content stays untouched, so CLI transports are unaffected.
        """

    @staticmethod
    def _split_text_and_images(
        message: dict[str, Any],
    ) -> tuple[str, list[dict[str, str]]]:
        """Split one native-role message into text plus inline image parts."""

        content = message.get("content")
        texts: list[str] = []
        images: list[dict[str, str]] = []
        items = content if isinstance(content, list) else [content]
        for item in items:
            if isinstance(item, str):
                if item:
                    texts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text" and item.get("text"):
                    texts.append(str(item["text"]))
                elif item.get("type") == "image_url":
                    url = item.get("image_url") or {}
                    data_url = str(url.get("url") or "")
                    if data_url.startswith("data:"):
                        header, _, data = data_url[5:].partition(";base64,")
                        if header.startswith("image/") and data:
                            images.append(
                                {"media_type": header, "data": "".join(data.split())}
                            )
        return "\n".join(texts), images

    def _anthropic_message(
        self, message: dict[str, Any], turn: AgentTurn
    ) -> dict[str, Any]:
        text, images = self._split_text_and_images(message)
        if not images:
            return message
        # Re-derive from the split images so the Anthropic block always
        # matches the canonical renderer output part-for-part.
        content: list[dict[str, Any]] = ([{"type": "text", "text": text}] if text else []) + [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image["media_type"],
                    "data": image["data"],
                },
            }
            for image in images
        ]
        return {"role": message.get("role", "user"), "content": content}

    def _google_parts(
        self, message: dict[str, Any], turn: AgentTurn
    ) -> list[dict[str, Any]]:
        _text, images = self._split_text_and_images(message)
        parts: list[dict[str, Any]] = []
        for image in images:
            parts.append(
                {
                    "inline_data": {
                        "mime_type": image["media_type"],
                        "data": image["data"],
                    }
                }
            )
        return parts

    def _add_structured_output_request(
        self, payload: dict[str, Any], turn: AgentTurn
    ) -> None:
        if turn.expected_output is None:
            return
        schema = turn.expected_output
        if hasattr(schema, "model_json_schema"):
            schema = schema.model_json_schema()
        if self.structured_output_mode == StructuredOutputMode.NATIVE_SCHEMA:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "persona_continuum_output",
                    "strict": True,
                    "schema": schema,
                },
            }
        elif self.structured_output_mode == StructuredOutputMode.JSON_MODE:
            payload["response_format"] = {"type": "json_object"}

    def _request_context(self) -> tuple[str, dict[str, str]]:
        if not self.credential_manager:
            raise RuntimeError("CredentialManager is required for API adapters")
        return self.credential_manager.request_headers(self.credential_id)

    def _capability_config(self, model_id: str) -> dict[str, Any]:
        raw = self.model_capabilities.get(model_id)
        if raw is None:
            raw = self.model_capabilities.get("default", {})
        if not isinstance(raw, dict):
            return {}
        return dict(raw)

    @staticmethod
    def _parse_reasoning_efforts(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        efforts: list[str] = []
        for item in value:
            raw: Any = item
            if isinstance(item, dict):
                raw = (
                    item.get("reasoningEffort")
                    or item.get("reasoning_effort")
                    or item.get("effort")
                    or item.get("value")
                    or item.get("id")
                )
            text = str(raw or "").strip()
            if text and text not in efforts:
                efforts.append(text)
        return efforts

    def _model_list_reasoning_capability(self, item: Any) -> ReasoningCapability:
        """Parse only explicit reasoning metadata reported by ``GET /models``."""

        if not isinstance(item, dict):
            return ReasoningCapability()
        containers = [item]
        for key in ("reasoning", "reasoning_capability", "capabilities"):
            nested = item.get(key)
            if isinstance(nested, dict):
                containers.append(nested)
                nested_reasoning = nested.get("reasoning")
                if isinstance(nested_reasoning, dict):
                    containers.append(nested_reasoning)

        efforts: list[str] = []
        default_effort: str | None = None
        for container in containers:
            if not efforts:
                for key in (
                    "supported_efforts",
                    "supportedReasoningEfforts",
                    "supported_reasoning_efforts",
                    "reasoning_efforts",
                ):
                    efforts = self._parse_reasoning_efforts(container.get(key))
                    if efforts:
                        break
            if default_effort is None:
                for key in (
                    "default_effort",
                    "defaultReasoningEffort",
                    "default_reasoning_effort",
                ):
                    raw_default = container.get(key)
                    if raw_default not in (None, ""):
                        default_effort = str(raw_default).strip()
                        break

        if not efforts:
            return ReasoningCapability()
        if default_effort not in efforts:
            default_effort = None
        capability = ReasoningCapability(
            mode=ReasoningCapabilityMode.NATIVE_EFFORT,
            supported_efforts=efforts,
            default_effort=default_effort,
            binding_strategy="openai_compatible_reasoning_effort",
            verified=True,
            source="protocol_model_list",
        )
        return self._constrain_provider_reasoning(capability)

    def _constrain_provider_reasoning(
        self, capability: ReasoningCapability
    ) -> ReasoningCapability:
        if self.provider_type not in {"anthropic", "google", "gemini"}:
            return capability
        return capability.model_copy(
            update={
                "mode": ReasoningCapabilityMode.UNSUPPORTED,
                "supported_efforts": [],
                "default_effort": None,
                "verified": False,
                "binding_strategy": "provider_specific_unbound",
                "verification_error": "provider_specific_reasoning_binding_not_implemented",
            }
        )

    def _reasoning_capability(self, model_id: str) -> ReasoningCapability:
        config = self._capability_config(model_id)
        raw = config.get("reasoning_capability")
        capability: ReasoningCapability | None = None
        if isinstance(raw, ReasoningCapability):
            capability = raw
        elif isinstance(raw, dict):
            capability = ReasoningCapability.model_validate(raw)
        if capability is not None:
            # A metadata entry cannot claim a provider-native binding merely
            # because a caller wrote one into the profile.  Anthropic and
            # Google/Gemini still have no request-field binding in this
            # adapter, so keep the capability honest until that path is
            # implemented and behaviorally verified.
            return self._constrain_provider_reasoning(capability)
        efforts = [
            str(item)
            for item in (
                config.get("reasoning_efforts")
                or config.get("supported_reasoning_efforts")
                or []
            )
            if item
        ]
        default_effort = config.get("default_reasoning_effort")
        provider = self.provider_type
        if provider in {"anthropic", "google", "gemini"}:
            # This adapter currently has no provider-specific thinking
            # parameter binding.  Keep the capability explicitly unsupported
            # until a real request/response verification exists.
            mode = ReasoningCapabilityMode.UNSUPPORTED
            strategy = "provider_specific_unbound"
        elif efforts or default_effort:
            mode = ReasoningCapabilityMode.MANUAL_CONFIG
            strategy = "manual_config"
        else:
            mode = ReasoningCapabilityMode.UNKNOWN
            strategy = "unknown"
        capability = ReasoningCapability(
            mode=mode,
            supported_efforts=efforts,
            default_effort=str(default_effort) if default_effort else None,
            binding_strategy=str(config.get("binding_strategy") or strategy),
            verified=bool(config.get("verified", False)),
            source=str(config.get("source") or "api_profile_metadata"),
            verification_error=config.get("verification_error"),
        )
        return self._constrain_provider_reasoning(capability)

    def _reasoning_binding_error(
        self, session: AgentSession, provider: str
    ) -> ReasoningBindingRejectedError | None:
        requested = str(session.config.reasoning_effort or "").strip()
        if requested.casefold() in {"", "none", "default", "auto"}:
            return None
        capability = self._reasoning_capability(session.config.model_id or self.default_model)
        normalized = requested.casefold()
        supported = {str(item).casefold() for item in capability.supported_efforts}
        can_bind = capability.mode in {
            ReasoningCapabilityMode.NATIVE_EFFORT,
            ReasoningCapabilityMode.MANUAL_CONFIG,
        } and normalized in supported and (
            capability.mode == ReasoningCapabilityMode.MANUAL_CONFIG
            or capability.verified
        )
        if provider in {CredentialProvider.ANTHROPIC.value, CredentialProvider.GOOGLE.value}:
            can_bind = False
        if can_bind:
            return None
        return ReasoningBindingRejectedError(
            "Selected reasoning effort has no verified binding for this API runtime",
            phase="session_update",
            diagnostics={
                "protocol": "openai_compatible_http",
                "provider": provider,
                "requested_reasoning": requested,
                "capability_mode": capability.mode.value,
                "supported_efforts": list(capability.supported_efforts),
                "binding_strategy": capability.binding_strategy,
                "verified": capability.verified,
            },
        )
