from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator

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


class FakeAgentAdapter(AgentAdapter):
    """Test adapter providing predictable responses, stream chunks, and tool calls."""

    # Tests exercise both a requested window and a runtime-reported one, so the
    # fake adapter accepts a context parameter and can also report a window.
    context_window_mode = ContextWindowMode.CONFIGURABLE_AND_DISCOVERABLE
    # The fake adapter is an in-process stand-in: it carries prompts like an
    # RPC transport, not through a shell argument, and its transport budget
    # is therefore extremely generous.  Tests that need real transport
    # pressure or trimming must subclass and declare
    # prompt_transport_mode="argv" instead of relying on this base class.
    prompt_transport_mode = "rpc"

    def __init__(
        self,
        adapter_id: str = "fake_agent",
        name: str = "Fake Test Agent",
        status: AgentStatus = AgentStatus.READY,
        models: list[ModelCapability] | None = None,
        chunk_delay_sec: float = 0.005,
        simulate_crash_on_turn: int | None = None,
        context_window: int | None = None,
        adapter_context_limit: int | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.name = name
        self.status = status
        # Reported by the runtime for every session this adapter opens.
        self.reported_context_window = context_window
        # Hard ceiling this adapter imposes regardless of the model.
        self.adapter_context_limit = adapter_context_limit
        self.chunk_delay_sec = chunk_delay_sec
        self.simulate_crash_on_turn = simulate_crash_on_turn
        self._models = models or [
            ModelCapability(
                id="fake-gpt-5",
                display_name="Fake GPT-5 Sol",
                provider=self.adapter_id,
                supported_reasoning_efforts=["none", "low", "medium", "high", "xhigh"],
                default_reasoning_effort="high",
                selectable=True,
                source="dynamic",
                reasoning_capability=ReasoningCapability(
                    mode=ReasoningCapabilityMode.NATIVE_EFFORT,
                    supported_efforts=["low", "medium", "high", "xhigh"],
                    default_effort="high",
                    binding_strategy="test_native_effort",
                    verified=True,
                    source="test_adapter",
                ),
            ),
            ModelCapability(
                id="fake-claude-4",
                display_name="Fake Claude 4 Sonnet",
                provider=self.adapter_id,
                supported_reasoning_efforts=["none", "low", "medium", "high"],
                default_reasoning_effort="medium",
                selectable=True,
                source="dynamic",
                reasoning_capability=ReasoningCapability(
                    mode=ReasoningCapabilityMode.NATIVE_EFFORT,
                    supported_efforts=["low", "medium", "high"],
                    default_effort="medium",
                    binding_strategy="test_native_effort",
                    verified=True,
                    source="test_adapter",
                ),
            ),
            ModelCapability(
                id="fake-grok-4",
                display_name="Fake Grok 4.6",
                provider=self.adapter_id,
                supported_reasoning_efforts=["none", "low", "high", "max"],
                default_reasoning_effort="high",
                selectable=True,
                source="dynamic",
                reasoning_capability=ReasoningCapability(
                    mode=ReasoningCapabilityMode.NATIVE_EFFORT,
                    supported_efforts=["low", "high", "max"],
                    default_effort="high",
                    binding_strategy="test_native_effort",
                    verified=True,
                    source="test_adapter",
                ),
            ),
        ]
        self.active_sessions: dict[str, AgentSession] = {}
        self.sent_turns: list[tuple[str, AgentTurn]] = []
        self.prompt_mode = PromptMode.NATIVE_ROLES
        self.structured_output_mode = StructuredOutputMode.PROMPT_ONLY
        self.output_streaming_mode = OutputStreamingMode.STREAMING
        self.protocols = ["fake_protocol"]

    async def probe(self) -> AgentProbeResult:
        return AgentProbeResult(
            id=self.adapter_id,
            name=self.name,
            status=self.status,
            binary_path="/mock/bin/" + self.adapter_id,
            version="1.0.0-mock",
            auth_status="mock_authenticated",
            protocols=["fake_protocol"],
            capabilities=AgentCapabilityFlags(
                streaming=True,
                persistent_session=True,
                resume_session=True,
                model_discovery=True,
                model_selection=SelectionStrategy.STARTUP,
                reasoning_discovery=True,
                reasoning_selection=SelectionStrategy.STARTUP,
                mcp=True,
                tool_calls=True,
                permission_control=True,
                cancel=True,
                images=False,
                structured_output_mode=self.structured_output_mode,
                auth_detection=True,
            ),
            models=self._models,
            status_detail=None if self.status == AgentStatus.READY else "Mock auth required",
        )

    async def list_models(self) -> list[ModelCapability]:
        return list(self._models)

    async def create_session(self, config: AgentSessionConfig) -> AgentSession:
        session = AgentSession(
            config=config,
            is_active=True,
            session_data={
                "turn_count": 0,
                "crashed": False,
                "protocol": "fake_protocol",
                "effective_model": config.model_id,
                "effective_reasoning": config.reasoning_effort,
                **(
                    {"effective_context_window": self.reported_context_window}
                    if self.reported_context_window is not None
                    else {}
                ),
            },
        )
        session._cancel_event = asyncio.Event()
        self.active_sessions[config.session_id] = session
        return session

    async def bind_runtime(self, session: AgentSession) -> RuntimeBindingSnapshot:
        snapshot = build_runtime_binding_snapshot(
            self,
            session,
            protocol="fake_protocol",
            model_verified=True,
            reasoning_verified=True,
            verification_method="test_adapter",
        )
        if self.reported_context_window is not None:
            snapshot.context_window = int(self.reported_context_window)
            snapshot.context_window_source = "runtime_reported"
            snapshot.context_window_mode = str(self.context_window_mode)
        return snapshot

    def supports_persistent_conversation(self, session: AgentSession) -> bool:
        # The fake adapter regenerates each reply from the current prompt text
        # (keyword-driven) and does not accumulate thread memory the way the
        # Codex/ACP runtimes do.  Declaring persistence would let the room
        # context cursor suppress history this double still depends on.
        return False

    async def send(self, session: AgentSession, turn: AgentTurn) -> AsyncIterator[AgentEvent]:
        self.sent_turns.append((session.config.session_id, turn))
        session.session_data["turn_count"] = session.session_data.get("turn_count", 0) + 1

        turn_count = session.session_data.get("turn_count", 0)
        prompt = AgentPromptRenderer.render_for_single_prompt(turn)

        # Error injection / simulated crash
        if "SIMULATE_CRASH" in prompt or (
            self.simulate_crash_on_turn is not None and turn_count == self.simulate_crash_on_turn
        ):
            session.session_data["crashed"] = True
            yield AgentEvent(
                type=AgentEventType.ERROR,
                error="SimulatedAgentCrashError: Agent process unexpectedly exited (exit code 137)",
            )
            return

        # Thinking simulation
        if session.config.reasoning_effort and session.config.reasoning_effort != "none":
            effort = session.config.reasoning_effort
            yield AgentEvent(
                type=AgentEventType.THINKING,
                thinking=f"[Reasoning ({effort})]: Analyzing persona constraints and context...",
            )
            if self.chunk_delay_sec > 0:
                await asyncio.sleep(self.chunk_delay_sec)

        # Tool calling simulation
        if "TRIGGER_TOOL_CALL" in prompt and turn.tools:
            yield AgentEvent(
                type=AgentEventType.TOOL_CALL,
                tool_call_id="call_mock_123",
                tool_name="persona_search_memories",
                tool_arguments={"query": "historical context", "limit": 4},
            )
            return

        # Regular streaming response
        response_text = self._generate_mock_response(session, turn)
        words = response_text.split(" ")
        for i, word in enumerate(words):
            if session._cancel_event and session._cancel_event.is_set():
                yield AgentEvent(
                    type=AgentEventType.DONE,
                    content=" [Cancelled by user]",
                    metadata={"cancelled": True},
                )
                return

            chunk = word + (" " if i < len(words) - 1 else "")
            yield AgentEvent(type=AgentEventType.CHUNK, content=chunk)
            if self.chunk_delay_sec > 0:
                await asyncio.sleep(self.chunk_delay_sec)

        yield AgentEvent(type=AgentEventType.DONE, content=response_text)

    async def cancel(self, session: AgentSession) -> None:
        session.request_cancel()

    async def close(self, session: AgentSession) -> None:
        session.mark_closed()
        self.active_sessions.pop(session.config.session_id, None)

    def _generate_mock_response(self, session: AgentSession, turn: AgentTurn) -> str:
        prompt = AgentPromptRenderer.render_for_single_prompt(turn)
        system_prompt = turn.system_prompt or ""
        persona = session.config.persona_id
        model = session.config.model_id or "default-model"
        effort = session.config.reasoning_effort or "default"

        if "ROOM PROTOCOL TASK" in prompt:
            required = set((turn.expected_output or {}).get("required") or [])
            if "selected_participant_ids" in required:
                candidates = [
                    value
                    for value in re.findall(r'"participant_id"\s*:\s*"([^"]+)"', prompt)
                    if value != session.config.participant_id
                ]
                return json.dumps(
                    {
                        "selected_participant_ids": list(dict.fromkeys(candidates))[:3],
                        "routing_reason": "Fake adapter selected declared routing candidates.",
                    },
                    ensure_ascii=False,
                )
            if "problem_definition" in required:
                # Host analysis gate: the definition plus the can-proceed
                # decision; never asks a blocking clarification.
                return json.dumps(
                    {
                        "problem_definition": (
                            "Fake host analysis: the stated case is fully specified."
                        ),
                        "known_facts_patch": {},
                        "assumptions": [],
                        "can_proceed": True,
                        "missing_indispensable_fields": [],
                        "clarification_question": None,
                    },
                    ensure_ascii=False,
                )
            if "key_findings" in required:
                return json.dumps(
                    {
                        "summary": f"Independent protocol analysis by {persona}.",
                        "key_findings": ["Structured finding"],
                        "evidence": [],
                        "uncertainties": ["Fake adapter evidence is synthetic test data."],
                        "recommendation": "Continue protocol validation.",
                        "confidence": 0.7,
                        "domain_data": {},
                    },
                    ensure_ascii=False,
                )
            if "changed_position" in required:
                return json.dumps(
                    {
                        "agree": ["The submission follows the requested structure."],
                        "disagree": [],
                        "concerns": [],
                        "changed_position": False,
                        "updated_conclusion": None,
                    },
                    ensure_ascii=False,
                )
            if "vote" in required:
                return json.dumps(
                    {"vote": "approve", "reason": "Fake committee vote.", "confidence": 0.7},
                    ensure_ascii=False,
                )
            if "participant_positions" in required or "final_judgment" in required:
                candidates = [
                    value
                    for value in re.findall(r'"participant_id"\s*:\s*"([^"]+)"', prompt)
                    if value != session.config.participant_id
                ]
                return json.dumps(
                    {
                        "summary": "Protocol synthesis completed by the fake adapter.",
                        "participant_positions": [
                            {
                                "participant_id": candidate,
                                "position": "Proceed with the structured protocol result.",
                                "key_reason": "Synthetic peer evidence is consistent.",
                            }
                            for candidate in list(dict.fromkeys(candidates))[:3]
                        ],
                        "consensus": ["Structured workflow completed."],
                        "conflicts": [],
                        "final_judgment": "Adopt the synthesized recommendation now.",
                        "uncertainties": ["Synthetic integration response."],
                        "recommendations": ["Validate with an explicitly configured real Agent."],
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "summary": "Protocol synthesis completed by the fake adapter.",
                    "consensus": ["Structured workflow completed."],
                    "conflicts": [],
                    "uncertainties": ["Synthetic integration response."],
                    "recommendations": ["Validate with an explicitly configured real Agent."],
                },
                ensure_ascii=False,
            )

        if "严格最终质量门禁" in system_prompt:
            from persona_continuum.application.compilation_service import REQUIRED_DIMENSIONS

            return json.dumps(
                {
                    "status": "pass",
                    "dimensions": {dimension: "pass" for dimension in REQUIRED_DIMENSIONS},
                    "issues": [],
                    "retrieval_source_ids": [],
                    "confidence": 0.95,
                },
                ensure_ascii=False,
            )
        if (
            "私人材料证据分类器" in system_prompt
            or "Evidence Intelligence 分析器" in system_prompt
        ):
            payload = json.loads(turn.user_message)
            return json.dumps(
                {
                    "units": [
                        {
                            "id": item["id"],
                            "dimension_scores": item.get("deterministic_dimensions", {}),
                            "confidence": 0.6,
                        }
                        for item in payload.get("units", [])
                    ]
                },
                ensure_ascii=False,
            )
        if "证据语义关系分析器" in system_prompt:
            return json.dumps({"clusters": [], "contradictions": []}, ensure_ascii=False)
        if "证据融合器" in system_prompt:
            payload = json.loads(turn.user_message)
            return json.dumps(
                {
                    "claims": [
                        {
                            "id": item["id"],
                            "canonical_claim": item["deterministic_claim"],
                        }
                        for item in payload.get("claims", [])
                    ]
                },
                ensure_ascii=False,
            )
        if "ACTION_PROPOSAL_JSON" in prompt:
            action_type = "research" if session.session_data.get("turn_count", 0) % 2 else "invest"
            return json.dumps(
                {
                    "actor": persona,
                    "intent": (
                        "Advance the highest-priority goal using currently available resources."
                    ),
                    "action_type": action_type,
                    "target": "strategic_capability",
                    "reasoning_summary": (
                        "Current goals and visible branch events justify a measured commitment."
                    ),
                    "expected_effect": (
                        "Increase organizational capability without assuming the outcome."
                    ),
                    "confidence": 0.72,
                    "parameters": {
                        "investment_billions": 0.5,
                        "amount_billions": 0.5,
                        "engineer_years": 80,
                        "required_technology_maturity": 0.1,
                    },
                }
            )
        if "火星" in prompt or "Mars" in prompt:
            return (
                f"[{persona}] 建立人类在火星的多行星文明一直是我最坚定的目标。"
                f"（生成自 {model} / effort={effort}）"
            )
        elif "Apple" in prompt or "iPhone" in prompt or "Jobs" in prompt:
            return (
                f"[{persona}] 站在科技与人文的十字路口，我们做产品追求的是追求极致与完美。"
                f"（生成自 {model} / effort={effort}）"
            )
        elif "超人" in prompt or "Nietzsche" in prompt or "上帝" in prompt:
            return (
                f"[{persona}] 凡不能毁灭我的，必使我更强大。生命应当勇于创造与超越。"
                f"（生成自 {model} / effort={effort}）"
            )
        else:
            return (
                f"[{persona}] 关于这个观点，从我的根本视角来看，我们需要深刻理解其本质逻辑。"
                f"这是基于当前对话语境的回应。"
            )
