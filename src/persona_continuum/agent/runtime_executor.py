"""Unified Agent execution entry point for text, structured, and research turns."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from persona_continuum.agent.adapter import AgentAdapter, AgentSession
from persona_continuum.agent.context_budget import AgentContextBudgetManager, ContextBudget
from persona_continuum.agent.contracts import (
    adapter_output_streaming_mode,
    adapter_prompt_mode,
    adapter_structured_output_mode,
)
from persona_continuum.agent.models import (
    AgentEvent,
    AgentEventType,
    AgentSessionConfig,
    AgentTurn,
    ModelCapability,
    RuntimeBindingSnapshot,
)
from persona_continuum.agent.prompt_transport import resolve_prompt_transport_capability
from persona_continuum.agent.response_collector import (
    AgentHardTimeoutError,
    AgentIdleTimeoutError,
    AgentResponse,
    AgentResponseCollector,
    AgentRuntimeError,
    AgentTransportError,
    ModelBindingUnverifiedError,
    PromptTransportLimitExceededError,
    ReasoningBindingUnverifiedError,
    compact_tool_result,
    retain_tool_artifact,
    sanitize_diagnostic,
)
from persona_continuum.agent.structured_output import (
    StructuredOutputEngine,
    StructuredOutputParseError,
    StructuredOutputRepairError,
    StructuredOutputSchemaError,
    StructuredResult,
    schema_as_json,
)
from persona_continuum.agent.timeout import AgentTimeoutPolicy, TimeoutBudget
from persona_continuum.numeric import safe_int

if TYPE_CHECKING:
    from persona_continuum.performance.capability_cache import ModelCapabilityCache

# One pending ``__anext__`` is kept alive while this fixed interval is used
# only to observe the executor-owned budget.  An activity tick must never
# cancel an adapter read and thereby corrupt a protocol generator.
ACTIVITY_CHECK_INTERVAL_SECONDS = 1.0


def _default_capability_cache() -> ModelCapabilityCache:
    # Imported lazily: the performance package touches agent models only for
    # typing, and a module-level import would create an import cycle through
    # the agent package's eager __init__.
    from persona_continuum.performance.capability_cache import (
        default_model_capability_cache,
    )

    return default_model_capability_cache()


def _default_scheduler() -> Any:
    from persona_continuum.performance.scheduler import default_execution_scheduler

    return default_execution_scheduler()


# Phase -> scheduler priority.  Interactive room/chat turns win the next free
# model slot ahead of background enrichment; this changes only WHEN a slot is
# granted, never the model, reasoning effort, prompt, or evidence.
_INTERACTIVE_PHASES = frozenset(
    {"room_agent_turn", "room_turn", "persona_chat", "session_turn", "continuation"}
)
_BACKGROUND_PHASES = frozenset(
    {"profile_enrichment", "material_indexing", "cache_refresh", "health_check"}
)


def _priority_for_phase(phase: str | None) -> Any:
    from persona_continuum.performance.scheduler import ExecutionClass

    if phase is None:
        return ExecutionClass.FOREGROUND
    normalized = str(phase).strip().casefold()
    if normalized in _INTERACTIVE_PHASES:
        return ExecutionClass.INTERACTIVE
    if normalized in _BACKGROUND_PHASES:
        return ExecutionClass.BACKGROUND
    return ExecutionClass.FOREGROUND


@dataclass(slots=True)
class RuntimeSessionBinding:
    adapter: AgentAdapter
    session: AgentSession
    snapshot: RuntimeBindingSnapshot


@dataclass(slots=True)
class AgentExecutionResult:
    text: str
    response: AgentResponse
    binding: RuntimeBindingSnapshot
    timeout_budget: TimeoutBudget
    context_budget: ContextBudget


class AgentRuntimeExecutor:
    """Own prompt, timeout, capability, and response semantics for Agent turns."""

    def __init__(
        self,
        *,
        context_budget_manager: AgentContextBudgetManager | None = None,
        timeout_policy: AgentTimeoutPolicy | None = None,
        structured_output_engine: StructuredOutputEngine | None = None,
        max_repair_attempts: int = 1,
        model_capability_cache: ModelCapabilityCache | None = None,
        scheduler: Any | None = None,
    ) -> None:
        self.context_budget_manager = context_budget_manager or AgentContextBudgetManager()
        self.timeout_policy = timeout_policy or AgentTimeoutPolicy()
        self.structured_output_engine = structured_output_engine or StructuredOutputEngine()
        self.max_repair_attempts = max(
            0, min(2, safe_int(max_repair_attempts, default=1, minimum=0, maximum=2) or 0)
        )
        self.model_capability_cache = model_capability_cache or _default_capability_cache()
        self._scheduler = scheduler

    @property
    def scheduler(self) -> Any:
        if self._scheduler is None:
            self._scheduler = _default_scheduler()
        return self._scheduler

    def _model_slot(self, adapter: AgentAdapter, phase: str | None) -> Any:
        return self.scheduler.llm_slot(
            adapter_id=getattr(adapter, "adapter_id", None),
            priority=_priority_for_phase(phase),
        )

    async def open_session(
        self, adapter: AgentAdapter, config: AgentSessionConfig
    ) -> RuntimeSessionBinding:
        from persona_continuum.performance.tracing import default_tracer

        default_tracer().incr_global("agent_session_open_count", 1)
        session: AgentSession | None = None
        try:
            session = await adapter.create_session(config)
            snapshot = await self._resolve_binding(adapter, session)
            self._validate_binding(adapter, snapshot, config)
            await self._attach_model_capability(adapter, session, snapshot)
            session.session_data["runtime_binding"] = snapshot.model_dump(mode="json")
            return RuntimeSessionBinding(adapter=adapter, session=session, snapshot=snapshot)
        except BaseException:
            if session is not None:
                with contextlib.suppress(Exception):
                    await adapter.close(session)
            raise

    async def bind_existing_session(
        self, adapter: AgentAdapter, session: AgentSession
    ) -> RuntimeSessionBinding:
        """Attach the shared contract to a session created by a legacy caller."""

        snapshot = await self._resolve_binding(adapter, session)
        self._validate_binding(adapter, snapshot, session.config)
        await self._attach_model_capability(adapter, session, snapshot)
        session.session_data["runtime_binding"] = snapshot.model_dump(mode="json")
        return RuntimeSessionBinding(adapter=adapter, session=session, snapshot=snapshot)

    async def _attach_model_capability(
        self,
        adapter: AgentAdapter,
        session: AgentSession,
        snapshot: RuntimeBindingSnapshot,
    ) -> None:
        # Model discovery must not run per session: for local CLI runtimes it
        # spawns and reaps a whole process.  The cache refreshes only when the
        # executable, version, or auth identity changes (or TTL expiry).
        with contextlib.suppress(Exception):
            try:
                models = await self.model_capability_cache.get_models(
                    adapter, auth_profile_id=session.config.auth_profile_id
                )
            except Exception:
                models = await adapter.list_models()
            selected_id = snapshot.effective_model or session.config.model_id
            selected = next((model for model in models if model.id == selected_id), None)
            if selected is not None:
                session.session_data["model_capability"] = selected.model_dump(mode="json")

    async def execute_text(
        self,
        session_binding: RuntimeSessionBinding,
        *,
        system_prompt: str | None,
        user_message: str,
        messages: list[dict[str, Any]] | None = None,
        expected_output: Any = None,
        phase: str = "agent_turn",
        stream: bool = False,
        metadata: dict[str, Any] | None = None,
        state_callback: Callable[[str], None] | None = None,
    ) -> AgentExecutionResult:
        turn = AgentTurn(
            user_message=user_message,
            system_prompt=(
                session_binding.session.config.system_prompt
                if system_prompt is None
                else system_prompt
            ),
            messages=list(messages or []),
            expected_output=schema_as_json(expected_output),
            stream=stream,
            metadata={"phase": phase, **dict(metadata or {})},
        )
        return await self._execute_turn(
            session_binding, turn, phase=phase, state_callback=state_callback
        )

    async def execute_structured(
        self,
        session_binding: RuntimeSessionBinding,
        *,
        system_prompt: str | None,
        user_message: str,
        schema: Any,
        phase: str,
        messages: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        max_repair_attempts: int | None = None,
        state_callback: Callable[[str], None] | None = None,
    ) -> StructuredResult:
        first = await self.execute_text(
            session_binding,
            system_prompt=system_prompt,
            user_message=user_message,
            messages=messages,
            expected_output=schema,
            phase=phase,
            stream=False,
            metadata=metadata,
            state_callback=state_callback,
        )
        mode = adapter_structured_output_mode(session_binding.adapter)
        try:
            parsed = self.structured_output_engine.parse_and_validate(
                first.text,
                schema,
                phase=phase,
                adapter=session_binding.adapter.adapter_id,
                protocol=self._protocol(session_binding.session),
                structured_output_mode=mode,
            )
            parsed.attempts = 1
            parsed.diagnostics.update(first.response.raw_diagnostics)
            parsed.response = first.response
            return parsed
        except (StructuredOutputParseError, StructuredOutputSchemaError) as first_error:
            # Deterministic local repair first: fence/prose damage, trailing
            # commas, and truncation must not cost a full model call.  The
            # locally repaired value still passes full schema validation.
            from persona_continuum.performance.tracing import default_tracer

            local_text = self.structured_output_engine.local_repair(first.text)
            if local_text != first.text:
                try:
                    parsed = self.structured_output_engine.parse_and_validate(
                        local_text,
                        schema,
                        phase=phase,
                        adapter=session_binding.adapter.adapter_id,
                        protocol=self._protocol(session_binding.session),
                        structured_output_mode=mode,
                    )
                    parsed.attempts = 1
                    parsed.diagnostics.update(first.response.raw_diagnostics)
                    parsed.diagnostics["local_repair"] = True
                    parsed.response = first.response
                    default_tracer().incr_global("structured_local_repair_count", 1)
                    return parsed
                except (StructuredOutputParseError, StructuredOutputSchemaError):
                    pass
            repair_limit = self.max_repair_attempts
            if max_repair_attempts is not None:
                repair_limit = max(0, min(2, int(max_repair_attempts)))
            if repair_limit < 1:
                raise
            repair_text = first.text
            repair_failure: StructuredOutputParseError | StructuredOutputSchemaError = first_error
            repair_diagnostics = dict(first.response.raw_diagnostics)
            last_error: StructuredOutputParseError | StructuredOutputSchemaError = first_error
            for repair_attempt in range(1, repair_limit + 1):
                repair_turn = self.structured_output_engine.repair_turn(
                    original_text=repair_text,
                    schema=schema,
                    parser_failure=str(repair_failure),
                    phase=phase,
                )
                repair_turn.metadata.update(dict(metadata or {}))
                try:
                    repaired = await self._execute_turn(
                        session_binding,
                        repair_turn,
                        phase="structured_output_repair",
                        state_callback=state_callback,
                    )
                    repair_diagnostics.update(repaired.response.raw_diagnostics)
                    parsed = self.structured_output_engine.parse_and_validate(
                        repaired.text,
                        schema,
                        phase=phase,
                        adapter=session_binding.adapter.adapter_id,
                        protocol=self._protocol(session_binding.session),
                        structured_output_mode=mode,
                        repair_attempted=True,
                    )
                    parsed.attempts = repair_attempt + 1
                    parsed.repair_attempted = True
                    parsed.diagnostics.update(
                        {
                            **repair_diagnostics,
                            "repair_attempts": repair_attempt,
                        }
                    )
                    parsed.response = repaired.response
                    default_tracer().incr_global("structured_llm_repair_count", 1)
                    return parsed
                except (StructuredOutputParseError, StructuredOutputSchemaError) as repair_error:
                    last_error = repair_error
                    repair_text = repaired.text
                    repair_failure = repair_error
                    if repair_attempt >= repair_limit:
                        break
                except AgentRuntimeError:
                    # A failed repair transport is still a transport failure.
                    # Do not relabel it as malformed JSON and lose the root cause.
                    raise
            raise StructuredOutputRepairError(
                "Structured output repair failed",
                phase=phase,
                diagnostics={
                    "adapter": session_binding.adapter.adapter_id,
                    "protocol": self._protocol(session_binding.session),
                    "structured_output_mode": mode.value,
                    "raw_chars": len(repair_text),
                    "parser_failure": str(first_error)[:1000],
                    "schema_failure": str(last_error)[:1000],
                    "repair_attempted": True,
                    "repair_attempts": repair_limit,
                },
            ) from last_error

    async def execute_research(
        self,
        session_binding: RuntimeSessionBinding,
        *,
        system_prompt: str | None,
        user_message: str,
        phase: str = "public_research",
        schema: Any = None,
    ) -> AgentExecutionResult | StructuredResult:
        if schema is None:
            return await self.execute_text(
                session_binding,
                system_prompt=system_prompt,
                user_message=user_message,
                phase=phase,
                stream=False,
            )
        return await self.execute_structured(
            session_binding,
            system_prompt=system_prompt,
            user_message=user_message,
            schema=schema,
            phase=phase,
        )

    async def close(self, session_binding: RuntimeSessionBinding) -> None:
        await session_binding.adapter.close(session_binding.session)

    async def _execute_turn(
        self,
        session_binding: RuntimeSessionBinding,
        turn: AgentTurn,
        *,
        phase: str,
        state_callback: Callable[[str], None] | None = None,
    ) -> AgentExecutionResult:
        # One model-execution slot per turn; queued behind higher-priority
        # work only when the scheduler is saturated.  Occupying the slot is
        # timed exactly against the physical send/collect window.
        from persona_continuum.performance.tracing import default_tracer

        tracer = default_tracer()
        trace_task_id = self._trace_task_id(turn)
        category = self._trace_phase_category(phase)
        tracer.incr_global("model_call_count", 1)
        tracer.count(trace_task_id, "llm_turn_count", 1)
        tracer.count(trace_task_id, f"llm_turn_{category}", 1)
        wait_started = time.monotonic()
        if state_callback is not None:
            with contextlib.suppress(Exception):
                state_callback("WAITING_SCHEDULER")
        async with self._model_slot(session_binding.adapter, phase):
            if state_callback is not None:
                with contextlib.suppress(Exception):
                    state_callback("SENDING_PROMPT")
            tracer.observe_value(
                trace_task_id,
                "scheduler_queue_wait_ms",
                (time.monotonic() - wait_started) * 1000.0,
            )
            turn_started = time.monotonic()
            try:
                return await self._execute_turn_locked(
                    session_binding, turn, phase=phase, state_callback=state_callback
                )
            finally:
                tracer.observe_value(
                    trace_task_id,
                    "llm_turn_latency_ms",
                    (time.monotonic() - turn_started) * 1000.0,
                )

    @staticmethod
    def _trace_task_id(turn: AgentTurn) -> str | None:
        job_id = turn.metadata.get("persona_creation_job_id")
        if job_id:
            return f"job:{job_id}"
        task_id = turn.metadata.get("performance_task_id")
        return str(task_id) if task_id else None

    @staticmethod
    def _trace_phase_category(phase: str) -> str:
        lowered = str(phase or "agent_turn").casefold()
        mappings = (
            ("research_plan", "research_plan"),
            ("gap", "research_gap_analysis"),
            ("search", "search_agent"),
            ("fetch", "fetch_agent"),
            ("material_classification", "evidence_intelligence"),
            ("evidence_intelligence", "evidence_intelligence"),
            ("dimension", "dimension_synthesis"),
            ("final_", "final_audit"),
            ("targeted_repair", "repair"),
            ("compile", "compile"),
        )
        return next((label for token, label in mappings if token in lowered), "other")

    async def _execute_turn_locked(
        self,
        session_binding: RuntimeSessionBinding,
        turn: AgentTurn,
        *,
        phase: str,
        state_callback: Callable[[str], None] | None = None,
    ) -> AgentExecutionResult:
        context_budget, timeout_budget, collector = self._prepare_turn(
            session_binding, turn, phase=phase
        )
        adapter = session_binding.adapter
        session = session_binding.session
        started = time.monotonic()
        self._mark_turn_started(session)
        stream = adapter.send(session, turn)
        # MODEL_RUNNING is only honest once the Adapter dispatch has actually
        # completed; everything before this point is still preparation.
        if state_callback is not None:
            with contextlib.suppress(Exception):
                state_callback("MODEL_RUNNING")
        model_started_at = time.monotonic()
        try:
            await self._collect_with_timeouts(
                stream,
                collector,
                session_binding,
                timeout_budget,
                started=started,
                phase=phase,
            )
            text = collector.require_text(phase=phase, job_id=session.config.participant_id)
            trace_task_id = self._trace_task_id(turn)
            from persona_continuum.performance.tracing import default_tracer

            tracer = default_tracer()
            generation_ms = (time.monotonic() - model_started_at) * 1000.0
            tracer.observe_value(trace_task_id, "generation_ms", generation_ms)
            collector.response.raw_diagnostics["model_started_at"] = (
                session.session_data.get("agent_turn_started_at")
            )
            collector.response.raw_diagnostics["model_completed_at"] = (
                time.time()
            )
            collector.response.raw_diagnostics["generation_ms"] = generation_ms
            tracer.observe_value(
                trace_task_id,
                "prompt_tokens",
                float(context_budget.estimated_prompt_tokens),
            )
            usage = dict(collector.response.usage or {})
            for key in ("input_tokens", "prompt_tokens"):
                if usage.get(key) is not None:
                    with contextlib.suppress(TypeError, ValueError):
                        tracer.observe_value(
                            trace_task_id, "reported_prompt_tokens", float(usage[key])
                        )
                    break
            for key in ("output_tokens", "completion_tokens"):
                if usage.get(key) is not None:
                    with contextlib.suppress(TypeError, ValueError):
                        tracer.observe_value(
                            trace_task_id, "reported_output_tokens", float(usage[key])
                        )
                    break
            collector.response.raw_diagnostics["last_activity_at"] = (
                session.session_data.get("last_agent_activity_at")
            )
            collector.response.raw_diagnostics["activity_tracker"] = (
                session.activity_tracker.as_diagnostics()
            )
            collector.response.raw_diagnostics["process_alive"] = self._process_alive(session)
            return AgentExecutionResult(
                text=text,
                response=collector.response,
                binding=session_binding.snapshot,
                timeout_budget=timeout_budget,
                context_budget=context_budget,
            )
        except AgentRuntimeError as exc:
            exc.response = collector.response
            collector.response.raw_diagnostics.update(dict(exc.diagnostics or {}))
            collector.response.raw_diagnostics["last_activity_at"] = (
                session.session_data.get("last_agent_activity_at")
            )
            collector.response.raw_diagnostics["activity_tracker"] = (
                session.activity_tracker.as_diagnostics()
            )
            collector.response.raw_diagnostics["process_alive"] = self._process_alive(session)
            raise
        except Exception as exc:
            wrapped = AgentTransportError(
                "Agent transport failed",
                phase=phase,
                diagnostics={
                    "protocol": self._protocol(session),
                    "adapter": adapter.adapter_id,
                    "exception_type": type(exc).__name__,
                    "diagnostic": sanitize_diagnostic(exc),
                },
            )
            wrapped.response = collector.response
            collector.response.raw_diagnostics["last_activity_at"] = (
                session.session_data.get("last_agent_activity_at")
            )
            collector.response.raw_diagnostics["activity_tracker"] = (
                session.activity_tracker.as_diagnostics()
            )
            collector.response.raw_diagnostics["process_alive"] = self._process_alive(session)
            raise wrapped from exc
        finally:
            close_stream = getattr(stream, "aclose", None)
            if callable(close_stream):
                with contextlib.suppress(Exception):
                    await close_stream()

    async def stream_events(
        self,
        session_binding: RuntimeSessionBinding,
        *,
        system_prompt: str | None,
        user_message: str,
        messages: list[dict[str, Any]] | None = None,
        expected_output: Any = None,
        tools: list[dict[str, Any]] | None = None,
        phase: str = "agent_turn",
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Stream events through the same bounded runtime as non-streaming turns.

        A single model-execution slot is held for the whole streamed turn.
        Callers MUST close this generator (the room uses ``contextlib.aclosing``)
        so the slot releases deterministically even when the consumer breaks on
        the DONE event; relying on garbage collection could strand a slot.
        """

        turn = AgentTurn(
            user_message=user_message,
            system_prompt=(
                session_binding.session.config.system_prompt
                if system_prompt is None
                else system_prompt
            ),
            messages=list(messages or []),
            tools=list(tools or []),
            expected_output=schema_as_json(expected_output),
            stream=True,
            metadata={"phase": phase, **dict(metadata or {})},
        )
        async with self._model_slot(session_binding.adapter, phase):
            async for event in self._stream_events_locked(
                session_binding, turn, phase=phase
            ):
                yield event

    async def _stream_events_locked(
        self,
        session_binding: RuntimeSessionBinding,
        turn: AgentTurn,
        *,
        phase: str,
    ) -> AsyncIterator[AgentEvent]:
        from persona_continuum.performance.tracing import default_tracer

        default_tracer().incr_global("model_call_count", 1)
        context_budget, timeout_budget, collector = self._prepare_turn(
            session_binding, turn, phase=phase
        )
        self._mark_turn_started(session_binding.session)
        stream = session_binding.adapter.send(session_binding.session, turn)
        started = time.monotonic()
        try:
            async for event in self._iter_events_with_timeouts(
                stream,
                session_binding,
                timeout_budget,
                started=started,
                phase=phase,
            ):
                collector.add(event)
                yield event
            collector.require_text(
                phase=phase, job_id=session_binding.session.config.participant_id
            )
            collector.response.raw_diagnostics["last_activity_at"] = (
                session_binding.session.session_data.get("last_agent_activity_at")
            )
            collector.response.raw_diagnostics["activity_tracker"] = (
                session_binding.session.activity_tracker.as_diagnostics()
            )
            collector.response.raw_diagnostics["process_alive"] = self._process_alive(
                session_binding.session
            )
        except AgentRuntimeError as exc:
            exc.response = collector.response
            collector.response.raw_diagnostics.update(dict(exc.diagnostics or {}))
            collector.response.raw_diagnostics["last_activity_at"] = (
                session_binding.session.session_data.get("last_agent_activity_at")
            )
            collector.response.raw_diagnostics["activity_tracker"] = (
                session_binding.session.activity_tracker.as_diagnostics()
            )
            collector.response.raw_diagnostics["process_alive"] = self._process_alive(
                session_binding.session
            )
            raise
        except Exception as exc:
            wrapped = AgentTransportError(
                "Agent transport failed",
                phase=phase,
                diagnostics={
                    "protocol": self._protocol(session_binding.session),
                    "adapter": session_binding.adapter.adapter_id,
                    "exception_type": type(exc).__name__,
                    "diagnostic": sanitize_diagnostic(exc),
                },
            )
            wrapped.response = collector.response
            collector.response.raw_diagnostics["last_activity_at"] = (
                session_binding.session.session_data.get("last_agent_activity_at")
            )
            collector.response.raw_diagnostics["activity_tracker"] = (
                session_binding.session.activity_tracker.as_diagnostics()
            )
            collector.response.raw_diagnostics["process_alive"] = self._process_alive(
                session_binding.session
            )
            raise wrapped from exc
        finally:
            close_stream = getattr(stream, "aclose", None)
            if callable(close_stream):
                with contextlib.suppress(Exception):
                    await close_stream()

    def _prepare_turn(
        self,
        session_binding: RuntimeSessionBinding,
        turn: AgentTurn,
        *,
        phase: str,
    ) -> tuple[ContextBudget, TimeoutBudget, AgentResponseCollector]:
        adapter = session_binding.adapter
        session = session_binding.session
        if turn.prompt_mode is None:
            turn.prompt_mode = adapter_prompt_mode(adapter)
        # Older callers inspect ``full_prompt`` after a turn has been sent.
        # Keep that compatibility view populated without using it as the
        # transport source unless FULL_PROMPT was explicitly requested.
        if turn.full_prompt is None:
            turn.full_prompt = turn.user_message
        model = self._model_capability(session)
        prepare_started = time.monotonic()
        context_budget = self.context_budget_manager.plan(
            turn,
            model=model,
            phase=phase,
            enforce=True,
        )
        # Prompt Transport Guard: model context and prompt transport are two
        # different capabilities.  A prompt that the transport cannot carry
        # must fail loudly here instead of hanging or truncating downstream.
        transport = resolve_prompt_transport_capability(adapter)
        estimated_prompt_bytes = len(
            ((turn.system_prompt or "") + (turn.full_prompt or turn.user_message or "")).encode(
                "utf-8"
            )
        )
        for message in turn.messages or []:
            estimated_prompt_bytes += len(
                json.dumps(message, ensure_ascii=False, default=str).encode("utf-8")
            )
        if not transport.allows_bytes(estimated_prompt_bytes):
            raise PromptTransportLimitExceededError(
                "PROMPT_TRANSPORT_LIMIT_EXCEEDED",
                phase=phase,
                diagnostics={
                    "adapter": adapter.adapter_id,
                    "protocol": self._protocol(session),
                    "transport_mode": transport.transport_mode,
                    "transport_max_prompt_bytes": transport.max_prompt_bytes,
                    "transport_safe_prompt_bytes": transport.safe_prompt_bytes,
                    "estimated_prompt_bytes": estimated_prompt_bytes,
                    "estimated_prompt_tokens": context_budget.estimated_prompt_tokens,
                    "model_context_limit": context_budget.context_window_tokens,
                },
            )
        from persona_continuum.performance.tracing import default_tracer

        tracer = default_tracer()
        trace_task_id = self._trace_task_id(turn)
        tracer.observe_value(
            trace_task_id,
            "prompt_build_ms",
            (time.monotonic() - prepare_started) * 1000.0,
        )
        tracer.observe_value(
            trace_task_id,
            "estimated_prompt_bytes",
            float(estimated_prompt_bytes),
        )
        turn.transport_capability = transport.as_dict()
        turn.metadata.update(
            {
                "transport_mode": transport.transport_mode,
                "transport_max_prompt_bytes": transport.max_prompt_bytes,
                "estimated_prompt_bytes": estimated_prompt_bytes,
            }
        )
        if turn.prompt_mode is None:
            turn.prompt_mode = adapter_prompt_mode(adapter)
        turn.context_budget = context_budget.as_dict()
        turn.metadata.update(
            {
                "phase": phase,
                "prompt_mode": adapter_prompt_mode(adapter).value,
                "structured_output_mode": adapter_structured_output_mode(adapter).value,
                "input_token_estimate": context_budget.estimated_prompt_tokens,
            }
        )
        timeout_budget = self.timeout_policy.resolve(
            phase=phase,
            model_id=session.config.model_id,
            reasoning_effort=session.config.reasoning_effort,
            prompt_chars=context_budget.estimated_prompt_chars,
            tool_usage=bool(turn.tools),
            extra=session.config.extra,
            output_streaming_mode=adapter_output_streaming_mode(adapter),
        )
        session.config.extra.update(
            {
                "idle_timeout_seconds": timeout_budget.idle_timeout_seconds,
                "hard_timeout_seconds": timeout_budget.hard_timeout_seconds,
                "first_response_timeout_seconds": timeout_budget.first_response_timeout_seconds,
                "runtime_phase": phase,
            }
        )
        protocol = self._protocol(session)
        collector = AgentResponseCollector(
            protocol=protocol,
            diagnostics={
                "adapter": adapter.adapter_id,
                "protocol": protocol,
                "prompt_mode": adapter_prompt_mode(adapter).value,
                "structured_output_mode": adapter_structured_output_mode(adapter).value,
                "output_streaming_mode": adapter_output_streaming_mode(adapter).value,
                "input_token_estimate": context_budget.estimated_prompt_tokens,
                "requested_model": session_binding.snapshot.requested_model,
                "effective_model": session_binding.snapshot.effective_model,
                "requested_reasoning": session_binding.snapshot.requested_reasoning,
                "effective_reasoning": session_binding.snapshot.effective_reasoning,
                "runtime_binding_snapshot": session_binding.snapshot.model_dump(mode="json"),
                "idle_timeout_seconds": timeout_budget.idle_timeout_seconds,
                "hard_timeout_seconds": timeout_budget.hard_timeout_seconds,
                "first_response_timeout_seconds": timeout_budget.first_response_timeout_seconds,
            },
        )
        return context_budget, timeout_budget, collector

    async def _collect_with_timeouts(
        self,
        stream: AsyncIterator[AgentEvent],
        collector: Any,
        session_binding: RuntimeSessionBinding,
        timeout_budget: TimeoutBudget,
        *,
        started: float,
        phase: str,
    ) -> None:
        async for event in self._iter_events_with_timeouts(
            stream,
            session_binding,
            timeout_budget,
            started=started,
            phase=phase,
        ):
            collector.add(event)

    async def _iter_events_with_timeouts(
        self,
        stream: AsyncIterator[AgentEvent],
        session_binding: RuntimeSessionBinding,
        timeout_budget: TimeoutBudget,
        *,
        started: float,
        phase: str,
    ) -> AsyncIterator[AgentEvent]:
        iterator = stream.__aiter__()
        next_event_task: asyncio.Task[Any] | None = None
        session = session_binding.session

        async def read_next_event() -> AgentEvent:
            return await iterator.__anext__()

        try:
            while True:
                now = time.monotonic()
                hard_remaining = timeout_budget.hard_timeout_seconds - (now - started)
                tracker = session.activity_tracker
                transport_started = tracker.last_transport_monotonic or started
                transport_idle = now - transport_started
                if hard_remaining <= 0:
                    raise await self._timeout_error(
                        AgentHardTimeoutError,
                        session_binding,
                        timeout_budget,
                        phase,
                        transport_started,
                    )
                if tracker.first_response_at is None:
                    first_remaining = timeout_budget.first_response_timeout_seconds - (
                        now - started
                    )
                    if first_remaining <= 0:
                        raise await self._timeout_error(
                            AgentIdleTimeoutError,
                            session_binding,
                            timeout_budget,
                            phase,
                            transport_started,
                            timeout_reason="first_response",
                        )
                    wait_for = min(hard_remaining, first_remaining)
                else:
                    idle_remaining = timeout_budget.idle_timeout_seconds - transport_idle
                    if idle_remaining <= 0:
                        raise await self._timeout_error(
                            AgentIdleTimeoutError,
                            session_binding,
                            timeout_budget,
                            phase,
                            transport_started,
                        )
                    wait_for = min(hard_remaining, idle_remaining)

                # Keep one pending __anext__ task alive while the executor
                # checks the budget.  Repeated wait_for cancellation can leave
                # protocol generators in an indeterminate state and can drop
                # a frame that arrived during the cancellation window.
                if next_event_task is None:
                    next_event_task = asyncio.create_task(read_next_event())
                done, _ = await asyncio.wait(
                    {next_event_task},
                    timeout=min(wait_for, ACTIVITY_CHECK_INTERVAL_SECONDS),
                )
                if not done:
                    current = time.monotonic()
                    hard_expired = (
                        current - started >= timeout_budget.hard_timeout_seconds
                    )
                    first_expired = (
                        tracker.first_response_at is None
                        and current - started
                        >= timeout_budget.first_response_timeout_seconds
                    )
                    idle_expired = (
                        tracker.first_response_at is not None
                        and current - (tracker.last_transport_monotonic or started)
                        >= timeout_budget.idle_timeout_seconds
                    )
                    if not (hard_expired or first_expired or idle_expired):
                        # This was only an observation tick.  Keep the
                        # adapter's pending read alive for the next loop.
                        continue
                    timed_out_task = next_event_task
                    next_event_task = None
                    timed_out_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await timed_out_task
                    error_type = AgentHardTimeoutError if hard_expired else AgentIdleTimeoutError
                    raise await self._timeout_error(
                        error_type,
                        session_binding,
                        timeout_budget,
                        phase,
                        tracker.last_transport_monotonic or started,
                        timeout_reason=(
                            "first_response"
                            if first_expired
                            else None
                        ),
                    )

                completed_task = next_event_task
                next_event_task = None
                try:
                    event = completed_task.result()
                except StopAsyncIteration:
                    return
                if not isinstance(event, AgentEvent):
                    event = AgentEvent.model_validate(event)
                self._touch_event_activity(session, event)
                event = self._public_event(event, session=session)
                yield event
        except asyncio.CancelledError:
            if next_event_task is not None and not next_event_task.done():
                next_event_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await next_event_task
            raise
        finally:
            if next_event_task is not None and not next_event_task.done():
                # This is only reached for generator shutdown/cancellation;
                # timeout cancellation is handled at the exact deadline above.
                next_event_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await next_event_task

    @staticmethod
    def _mark_turn_started(session: AgentSession) -> None:
        session.begin_turn()

    @staticmethod
    def _touch_event_activity(session: AgentSession, event: AgentEvent) -> None:
        metadata = dict(event.metadata or {})
        frame_bytes = metadata.get("frame_bytes") or metadata.get("frame_bytes_count") or 0
        if not metadata.get("activity_recorded") and (
            metadata.get("frame_type") or metadata.get("protocol") or metadata.get("method")
        ):
            session.touch_activity(
                "protocol_frame",
                byte_count=safe_int(frame_bytes, default=0, minimum=0) or 0,
                metadata=metadata,
            )
        kind = event.type.value if isinstance(event.type, AgentEventType) else str(event.type)
        if not metadata.get("activity_recorded") or session.activity_tracker.activity_kind != kind:
            session.touch_activity(kind, metadata=metadata)

    @classmethod
    def _public_event(cls, event: AgentEvent, *, session: AgentSession | None = None) -> AgentEvent:
        """Keep large tool payloads out of every application-facing stream.

        Adapters have already used the complete tool result when this boundary
        is reached.  From here on, Collector, Room, WebSocket, and UI consumers
        receive only the artifact-shaped observation.  The raw value is kept in
        the non-public session artifact map so an evidence reference can still
        resolve to the original result without putting that body in an event or
        diagnostic payload.
        """

        if event.type != AgentEventType.TOOL_RESULT:
            return event
        payload = event.tool_result if event.tool_result is not None else event.content
        compacted = compact_tool_result(
            payload,
            tool_name=event.tool_name,
            artifact_ref=(event.metadata or {}).get("artifact_ref") or None,
        )
        if compacted is payload:
            return event
        if session is not None:
            cls._retain_tool_artifact(session, compacted, payload)
        metadata = dict(event.metadata or {})
        metadata["tool_result_compacted"] = True
        if isinstance(compacted, dict):
            metadata["artifact_ref"] = compacted.get("artifact_ref")
            metadata["tool_result_character_count"] = compacted.get("character_count")
        # A content-only tool event can carry the same private body twice.  A
        # separate short human-readable summary is safe to retain, but a large
        # content field is removed together with the raw tool value.
        content = event.content
        if event.tool_result is None or compact_tool_result(
            event.content,
            tool_name=event.tool_name,
            artifact_ref=(metadata.get("artifact_ref") or None),
        ) is not event.content:
            content = ""
        return event.model_copy(
            update={"content": content, "tool_result": compacted, "metadata": metadata}
        )

    @staticmethod
    def _retain_tool_artifact(session: AgentSession, compacted: Any, raw_value: Any) -> None:
        """Retain raw tool evidence behind a bounded, non-public session map."""

        retain_tool_artifact(session.session_data, compacted, raw_value)

    async def _timeout_error(
        self,
        error_type: type[AgentRuntimeError],
        session_binding: RuntimeSessionBinding,
        budget: TimeoutBudget,
        phase: str,
        last_activity: float,
        timeout_reason: str | None = None,
    ) -> AgentRuntimeError:
        with contextlib.suppress(Exception):
            await session_binding.adapter.cancel(session_binding.session)
        diagnostics = {
            "protocol": self._protocol(session_binding.session),
            "adapter": session_binding.adapter.adapter_id,
            "idle_timeout_seconds": budget.idle_timeout_seconds,
            "hard_timeout_seconds": budget.hard_timeout_seconds,
            "last_activity_age_seconds": max(0.0, time.monotonic() - last_activity),
            "last_activity_at": session_binding.session.session_data.get(
                "last_agent_activity_at"
            ),
            "output_streaming_mode": adapter_output_streaming_mode(
                session_binding.adapter
            ).value,
            "first_response_timeout_seconds": budget.first_response_timeout_seconds,
            "runtime_binding_snapshot": session_binding.snapshot.model_dump(mode="json"),
            "activity_tracker": session_binding.session.activity_tracker.as_diagnostics(),
            "process_alive": self._process_alive(session_binding.session),
        }
        if timeout_reason:
            diagnostics["timeout_reason"] = timeout_reason
        if error_type is AgentIdleTimeoutError and timeout_reason == "first_response":
            message = "Agent turn exceeded its first-response timeout"
        elif error_type is AgentIdleTimeoutError:
            message = "Agent turn exceeded its idle timeout"
        else:
            message = "Agent turn exceeded its hard timeout"
        return error_type(message, phase=phase, diagnostics=diagnostics)

    @staticmethod
    def _process_alive(session: AgentSession) -> bool | None:
        process = session.session_data.get("active_proc") or session.session_data.get("proc")
        if process is None:
            return None
        return getattr(process, "returncode", None) is None

    async def _resolve_binding(
        self, adapter: AgentAdapter, session: AgentSession
    ) -> RuntimeBindingSnapshot:
        raw = session.session_data.get("runtime_binding")
        if isinstance(raw, RuntimeBindingSnapshot):
            return raw
        if isinstance(raw, dict):
            return RuntimeBindingSnapshot.model_validate(raw)
        hook = getattr(adapter, "bind_runtime", None)
        if callable(hook):
            result = hook(session)
            if hasattr(result, "__await__"):
                result = await result
            if isinstance(result, RuntimeBindingSnapshot):
                return result
            if isinstance(result, dict):
                return RuntimeBindingSnapshot.model_validate(result)
        config = session.config
        requested_model = self._requested_value(config.model_id)
        requested_reasoning = self._requested_value(config.reasoning_effort)
        effective_model = session.session_data.get("effective_model") or requested_model
        effective_reasoning = session.session_data.get("effective_reasoning") or requested_reasoning
        model_verified = bool(
            not requested_model
            or session.session_data.get("model_selection_applied", True)
            and effective_model == requested_model
        )
        reasoning_verified = bool(
            not requested_reasoning
            or session.session_data.get("reasoning_selection_applied", True)
            and effective_reasoning == requested_reasoning
        )
        return RuntimeBindingSnapshot(
            agent_id=adapter.adapter_id,
            protocol=self._protocol(session),
            requested_model=requested_model,
            effective_model=str(effective_model) if effective_model else None,
            requested_reasoning=requested_reasoning,
            effective_reasoning=str(effective_reasoning) if effective_reasoning else None,
            model_verified=model_verified,
            reasoning_verified=reasoning_verified,
            binding_status="verified" if model_verified and reasoning_verified else "unverified",
            verification_method="adapter_session_config",
        )

    @staticmethod
    def _validate_binding(
        adapter: AgentAdapter,
        snapshot: RuntimeBindingSnapshot,
        config: AgentSessionConfig,
    ) -> None:
        strict = bool(getattr(adapter, "require_verified_binding", False))
        if not strict:
            return
        requested_model = AgentRuntimeExecutor._requested_value(config.model_id)
        requested_reasoning = AgentRuntimeExecutor._requested_value(config.reasoning_effort)
        if requested_model and not snapshot.model_verified:
            raise ModelBindingUnverifiedError(
                "Selected model could not be verified by the ACP runtime",
                phase="session_binding",
                diagnostics={
                    "protocol": "acp",
                    "requested_model": requested_model,
                    "effective_model": snapshot.effective_model,
                },
            )
        if requested_reasoning and not snapshot.reasoning_verified:
            raise ReasoningBindingUnverifiedError(
                "Selected reasoning effort could not be verified by the ACP runtime",
                phase="session_binding",
                diagnostics={
                    "protocol": "acp",
                    "requested_reasoning": requested_reasoning,
                    "effective_reasoning": snapshot.effective_reasoning,
                },
            )

    @staticmethod
    def _requested_value(value: Any) -> str | None:
        text = str(value).strip() if value is not None else ""
        return text if text and text.lower() not in {"default", "auto", "none"} else None

    @staticmethod
    def _protocol(session: AgentSession) -> str:
        return str(
            session.session_data.get("protocol")
            or session.session_data.get("mode")
            or "unknown"
        )

    @staticmethod
    def _model_capability(session: AgentSession) -> ModelCapability | None:
        raw = session.session_data.get("model_capability")
        if isinstance(raw, ModelCapability):
            return raw
        if isinstance(raw, dict):
            with contextlib.suppress(Exception):
                return ModelCapability.model_validate(raw)
        return None


__all__ = [
    "AgentExecutionResult",
    "AgentRuntimeExecutor",
    "RuntimeSessionBinding",
]
