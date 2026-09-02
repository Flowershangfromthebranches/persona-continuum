from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from persona_continuum.agent.discovery import AgentDiscoveryService
from persona_continuum.agent.models import (
    AgentEventType,
    AgentSessionConfig,
    PermissionProfile,
)
from persona_continuum.agent.prompt_transport import (
    PromptTransportCapability,
    resolve_prompt_transport_capability,
)
from persona_continuum.agent.registry import AgentRegistry
from persona_continuum.agent.response_collector import (
    AgentCancelledError,
    AgentResponseCollector,
    AgentRuntimeError,
    PromptTransportLimitExceededError,
    compact_tool_result,
)
from persona_continuum.agent.runtime_executor import RuntimeSessionBinding
from persona_continuum.application._utils import dumps, loads, new_id, parse_dt
from persona_continuum.auth.profiles import AuthProfileService
from persona_continuum.room.case_state import RoomCaseState, merge_case_state

if TYPE_CHECKING:
    from persona_continuum.application.container import PersonaContinuum
from persona_continuum.room.context_manager import RoomContextManager
from persona_continuum.room.context_packer import (
    ABSOLUTE_FLOOR_BYTES,
    ContextPacker,
    budget_for,
    byte_length,
)
from persona_continuum.room.director import SpeakerDirector
from persona_continuum.room.discussion_director import DiscussionDirector
from persona_continuum.room.host_agent import HostAgent
from persona_continuum.room.models import (
    BindingPreflightError,
    DirectorConfig,
    ParticipantSlot,
    RoomMode,
    RoomProtocolConfig,
    RoomProtocolEvent,
    RoomProtocolState,
    RoomProtocolType,
    RoomRunStatus,
    RoomSessionState,
    RoomSharedContext,
    RoomStatus,
    RoomTranscriptRecord,
)
from persona_continuum.room.prompt_composer import PromptComposer
from persona_continuum.room.protocol_runtime import ProtocolActionRequest, RoomProtocolRuntime
from persona_continuum.room.protocols import default_protocol_registry
from persona_continuum.room.random_resolver import RandomBindingResolver
from persona_continuum.room.recall_gate import RecallGate
from persona_continuum.room.repository import DIVINATION_TEMPLATE_ID, RoomProtocolRepository
from persona_continuum.room.static_kernel import default_static_kernel_cache
from persona_continuum.room.tool_broker import PersonaToolBroker, RoomToolBroker

# Test/integration callers may opt into a short fallback timeout explicitly.
# Production host turns use AgentRuntimeExecutor's phase-aware idle/hard policy.
HOST_GENERATE_TIMEOUT_SECONDS: float | None = None

# Tool names that are no longer granted by any built-in template.  Only
# exact matches of these names are stripped from rooms created by older
# builds; any other permission is preserved untouched.
_LEGACY_REMOVED_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "almanac",
        "bazi",
        "bazi_dayun",
        "bazi_pillars_resolve",
        "ziwei",
        "ziwei_horoscope",
        "ziwei_flying_star",
        "liuyao",
        "meihua",
        "xiaoliuren",
        "qimen",
        "daliuren",
        "taiyi",
        "astrology",
        "tarot",
    }
)


def _estimate_tokens(text: str) -> int:
    # Coarse char/4 estimate is sufficient for prompt-size trend metrics.
    return max(0, len(text) // 4)


def _latest_user_injection(state: RoomSessionState) -> str:
    for item in reversed(state.transcript or []):
        if item.get("participant_id") == "user":
            content = str(item.get("content") or "").strip()
            if content:
                return content
    return ""


def _fallback_host_message(state: RoomSessionState, phase: str) -> str:
    topic = state.topic or "当前议题"
    if phase == "open":
        return f"我们开始讨论：{topic}。请第一位参与者先给出观点。"
    if phase == "steer":
        return f"请继续围绕「{topic}」推进，补充证据或反驳上一轮观点。"
    return f"本轮讨论先告一段落。议题是「{topic}」。"


def _coerce_case_state(value: Any) -> RoomCaseState:
    """Old persisted rooms may still carry case_state as a raw dict."""

    if isinstance(value, RoomCaseState):
        return value
    if isinstance(value, dict):
        try:
            return RoomCaseState.model_validate(value)
        except ValueError:
            return RoomCaseState()
    return RoomCaseState()


# Protocol conversion is allowed while the room is still inside its active
# lifecycle; PAUSED/COMPLETED/ERROR rooms must settle first.
_CONVERTIBLE_ROOM_STATUSES: frozenset[RoomStatus] = frozenset(
    {
        RoomStatus.CREATING,
        RoomStatus.INITIALIZING,
        RoomStatus.READY,
        RoomStatus.DISCUSSING,
    }
)


class RoomProtocolConversionError(Exception):
    """Coded guard failure for protocol conversion (no silent fallback).

    ``code`` drives the HTTP mapping in the web layer: room_not_found -> 404,
    protocol_run_active / room_status_not_convertible -> 409, the rest -> 400.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RoomBusyError(Exception):
    """Coded room-busy rejection (protocol run / conversion in flight).

    The web layer maps every RoomBusyError to HTTP 409 with the code in
    ``details``; the caller can poll the room state and retry later.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MultiAgentOrchestrator:
    def __init__(
        self,
        continuum: PersonaContinuum,
        registry: AgentRegistry,
        discovery: AgentDiscoveryService,
        auth_service: AuthProfileService | None = None,
        random_seed: int | None = None,
    ) -> None:
        self.continuum = continuum
        self.registry = registry
        self.discovery = discovery
        self.auth_service = auth_service
        self.resolver = RandomBindingResolver(self.registry, seed=random_seed)
        self.recall_gate = RecallGate(self.continuum.memories)
        self.director = SpeakerDirector(seed=random_seed)
        self.discussion_director = DiscussionDirector()
        self.runtime_executor = getattr(continuum, "agent_runtime_executor", None)
        self.host_agent = HostAgent(self.runtime_executor)
        self.prompt_composer = PromptComposer()
        self.tool_broker = PersonaToolBroker(self.continuum)
        self.protocol_registry = default_protocol_registry()
        # Cached per room so the preflight banner and the UI agree on the same
        # readiness snapshot the participants were actually started with.
        self._tool_preflight: dict[str, dict[str, Any]] = {}
        self._tool_warmup_tasks: dict[str, asyncio.Task[None]] = {}
        # Per-participant prompt transport capability cache (see
        # _resolve_transport_capability); cleared together with room bindings.
        self._transport_capabilities: dict[
            str, dict[str, tuple[str, PromptTransportCapability]]
        ] = {}
        self.protocol_repository = RoomProtocolRepository(self.continuum.database)
        self.protocol_runtime = RoomProtocolRuntime(
            self.protocol_repository,
            registry=self.protocol_registry,
            event_emitter=self._emit_protocol_event,
            transcript_recorder=self._record_protocol_public_message,
        )
        room_cfg = getattr(continuum, "config", None)
        self.context_manager = RoomContextManager(
            cursor_enabled=bool(getattr(room_cfg, "room_context_cursor_enabled", True)),
            raw_window=max(4, int(getattr(room_cfg, "room_raw_turn_window", 8) or 8)),
            summary_every_turns=max(4, int(getattr(room_cfg, "room_raw_turn_window", 8) or 8) * 2),
        )
        self.kernel_cache = (
            default_static_kernel_cache()
            if bool(getattr(room_cfg, "room_static_persona_cache", True))
            else None
        )
        self._debounce_seconds = max(
            0.0, float(getattr(room_cfg, "job_snapshot_debounce_seconds", 0.5) or 0.0)
        )
        # Transcript entries kept inside the persisted state snapshot.  The
        # append-only room_transcripts table stays the authority for full
        # history; the state snapshot keeps only the recent window so turn
        # N+1 persistence never scales with N.
        self._persisted_transcript_window = max(
            32, int(getattr(room_cfg, "room_raw_turn_window", 8) or 8) * 4
        )

        # In-memory active room runtime instances: room_id -> { participant_id: AgentSession }
        self._active_agent_sessions: dict[str, dict[str, Any]] = {}
        self._active_agent_bindings: dict[str, dict[str, RuntimeSessionBinding]] = {}
        # Authoritative room-state mirror (room_id -> latest state snapshot).
        self._room_state_cache: dict[str, RoomSessionState] = {}
        self._room_state_last_commit: dict[str, float] = {}
        self._room_state_dirty: set[str] = set()
        # In-memory session IDs: room_id -> { participant_id: continuum_session_id }
        self._persona_session_ids: dict[str, dict[str, str]] = {}
        # Event subscription queues for WebSockets / CLI streams: room_id -> set of asyncio.Queue
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        # Active turn locks per room to ensure single speaker generation concurrency
        self._room_locks: dict[str, asyncio.Lock] = {}
        self._initialization_locks: dict[str, asyncio.Lock] = {}
        self._initialization_tasks: dict[str, asyncio.Task[RoomSessionState]] = {}
        self._autonomous_tasks: dict[str, asyncio.Task[None]] = {}
        self._protocol_tasks: dict[str, asyncio.Task[RoomSessionState]] = {}
        # Single-flight guards for protocol runs and conversions.  The lock
        # serialises synchronous run_protocol callers (background starts
        # check _protocol_tasks instead); the converting set rejects any
        # state-mutating entry point while a migration is in flight.
        self._protocol_run_locks: dict[str, asyncio.Lock] = {}
        self._converting_rooms: set[str] = set()
        self._host_agent_sessions: dict[str, Any] = {}
        self._host_agent_bindings: dict[str, RuntimeSessionBinding] = {}
        self._event_replay: dict[str, list[dict[str, Any]]] = {}
        # Single-flight background summary refresh per room (room_id -> task).
        self._summary_tasks: dict[str, asyncio.Task[None]] = {}

    async def _run_tool_preflight(self, room_id: str, state: RoomSessionState) -> None:
        """Snapshot generic provider health for the room without failing startup.

        ``preflight()`` only resolves the environment -- it never spawns a
        provider -- so this stays fast and safe.  Tool health is recorded in
        ``state.metadata["tool_preflight"]`` for the 运行详情 debug view and
        broadcast once; it is informational and never gates room startup.
        """

        try:
            report = await self.tool_broker.preflight()
        except Exception as exc:  # pragma: no cover - defensive
            report = {
                "ready": False,
                "total_tools": 0,
                "providers": [],
                "summary": [f"Tool preflight failed: {exc}"],
            }
        self._tool_preflight[room_id] = report
        state.metadata["tool_preflight"] = report
        self._save_room_state(state)
        event = {"event": "tool_preflight", "room_id": room_id, **report}
        await self._broadcast_event(room_id, event)

        # Only pay provider startup cost when the room actually asked for
        # external tools.  Lazy start on first call remains the fallback.
        if any(self._slot_tool_permissions(slot) for slot in state.participants):

            async def _warm() -> None:
                with contextlib.suppress(Exception):
                    await self.tool_broker.warmup()

            self._tool_warmup_tasks[room_id] = asyncio.create_task(_warm())

    def tool_preflight(self, room_id: str) -> dict[str, Any] | None:
        return self._tool_preflight.get(room_id)

    @staticmethod
    def _slot_tool_permissions(slot: ParticipantSlot) -> list[str]:
        """Room policy: which *external* tools this participant may call.

        ``[]`` means "first-party persona tools only"; a non-empty list adds the
        named external capabilities on top of the built-ins.  This is policy,
        not availability -- a permitted tool still needs a live provider.
        """

        return list(getattr(slot, "tool_permissions", None) or [])

    async def _tools_for_slot(self, slot: ParticipantSlot) -> list[dict[str, Any]]:
        """Availability intersected with permission for one participant.

        Every tool path -- ``AgentSessionConfig.tools``, the ``stream_events``
        tool list, and the ``tool_executor`` handed to the adapter -- must see
        this identical set.  Divergence here is what makes a model call a tool
        the broker then rejects.
        """

        return await self.tool_broker.list_tool_definitions(
            tool_permissions=self._slot_tool_permissions(slot),
            allow_agent_tools=bool(slot.allow_agent_tools),
        )

    def create_room(
        self,
        title: str | None = None,
        topic: str | None = None,
        participants: list[ParticipantSlot] | None = None,
        director_config: DirectorConfig | None = None,
        mode: RoomMode = RoomMode.AUTONOMOUS,
        request_id: str | None = None,
        host_participant_id: str | None = None,
        description: str | None = None,
        protocol: RoomProtocolType = RoomProtocolType.FREE_DISCUSSION,
        protocol_config: RoomProtocolConfig | None = None,
        shared_context: RoomSharedContext | None = None,
        template_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RoomSessionState:
        if request_id:
            row = self.continuum.database.conn.execute(
                "SELECT room_id FROM room_create_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row:
                existing = self.get_room(str(row["room_id"]))
                if existing:
                    return existing
        room_participants = participants or []
        self._validate_persona_bindings(room_participants)
        definition = self.protocol_registry.get(protocol, protocol_config)
        self.protocol_registry.validate_participants(definition, room_participants)
        room_id = new_id("room")
        now = datetime.now(UTC)
        state = RoomSessionState(
            id=room_id,
            title=title or f"Room {room_id}",
            description=description,
            topic=topic,
            protocol=protocol,
            protocol_config=protocol_config or RoomProtocolConfig(),
            shared_context=shared_context or RoomSharedContext(),
            template_id=template_id,
            mode=mode,
            status=RoomStatus.CREATING,
            participants=room_participants,
            binding_snapshots={},
            turn_index=0,
            host_participant_id=host_participant_id,
            director_config=director_config or DirectorConfig(),
            transcript=[],
            metadata={**(metadata or {}), **({"request_id": request_id} if request_id else {})},
            created_at=now,
            updated_at=now,
        )
        self._save_room_state(state, force=True)
        if request_id:
            try:
                self.continuum.database.conn.execute(
                    "INSERT INTO room_create_requests (request_id, room_id, created_at) "
                    "VALUES (?, ?, ?)",
                    (request_id, room_id, now.isoformat()),
                )
                self.continuum.database.conn.commit()
            except Exception:
                row = self.continuum.database.conn.execute(
                    "SELECT room_id FROM room_create_requests WHERE request_id = ?", (request_id,)
                ).fetchone()
                if row:
                    existing = self.get_room(str(row["room_id"]))
                    if existing:
                        self.delete_room(room_id)
                        return existing
        return state

    def validate_persona_bindings(self, participants: list[ParticipantSlot]) -> None:
        """Public persona-binding preflight shared with the web API layer.

        The web ``create_room`` handler runs this before its protocol checks
        so a missing persona still surfaces the structured
        ``BindingPreflightError`` payload (``details.code``) instead of the
        generic missing-protocol 400.
        """
        self._validate_persona_bindings(participants)

    def _validate_persona_bindings(self, participants: list[ParticipantSlot]) -> None:
        available_personas = {persona.id for persona in self.continuum.personas.list()}
        for slot in participants:
            if slot.persona_id not in available_personas:
                raise BindingPreflightError(
                    reason=f"数字人物不存在: {slot.persona_id}",
                    participant_id=slot.participant_id,
                    code="persona_not_found",
                )

    def preflight_room_bindings(
        self,
        participants: list[ParticipantSlot],
        probes: list[Any],
    ) -> None:
        probes_by_id = {p.id: p for p in probes}
        self._validate_persona_bindings(participants)

        for slot in participants:
            runtime_id = slot.runtime_selection
            if runtime_id not in {"default", "random", ""}:
                probe = probes_by_id.get(runtime_id)
                if not probe:
                    raise BindingPreflightError(
                        reason=f"指定的 Agent 运行时未被系统发现: {runtime_id}",
                        participant_id=slot.participant_id,
                        runtime_id=runtime_id,
                        code="runtime_not_found",
                    )
                if getattr(probe, "status", None) != "ready":
                    st = getattr(probe, "status", "unknown")
                    raise BindingPreflightError(
                        reason=f"指定的 Agent 运行时未就绪 (当前状态: {st}): {runtime_id}",
                        participant_id=slot.participant_id,
                        runtime_id=runtime_id,
                        code="runtime_not_ready",
                    )
                adapter = self.registry.get_adapter(runtime_id)
                if not adapter:
                    raise BindingPreflightError(
                        reason=f"指定的 Agent Adapter 未注册: {runtime_id}",
                        participant_id=slot.participant_id,
                        runtime_id=runtime_id,
                        code="adapter_not_registered",
                    )

                model_id = slot.model_selection
                if model_id not in {"default", "random", ""}:
                    matched_model = next((m for m in probe.models if m.id == model_id), None)
                    if not matched_model:
                        raise BindingPreflightError(
                            reason=f"Agent 运行时 {runtime_id} 上未找到指定的模型: {model_id}",
                            participant_id=slot.participant_id,
                            runtime_id=runtime_id,
                            model_id=model_id,
                            code="model_not_found",
                        )
                    if not getattr(matched_model, "selectable", True):
                        raise BindingPreflightError(
                            reason=f"模型 {model_id} 在 Agent {runtime_id} 上不可被选择",
                            participant_id=slot.participant_id,
                            runtime_id=runtime_id,
                            model_id=model_id,
                            code="model_not_selectable",
                        )

    async def start_room(self, room_id: str) -> RoomSessionState:
        lock = self._initialization_locks.setdefault(room_id, asyncio.Lock())
        async with lock:
            state = self.get_room(room_id)
            if not state:
                raise KeyError(f"Room not found: {room_id}")
            if state.status == RoomStatus.READY and self._active_agent_sessions.get(room_id):
                return state
            if not state.participants:
                raise ValueError("Cannot start room without participants")
            state.last_error = None

            # 5% Creating
            await self._initialization_progress(state, "creating", 5)

            # 15% Scanning runtimes
            await self._initialization_progress(state, "scanning_runtimes", 15)
            probes = await self.discovery.scan(force_refresh=False)
            explicit_runtime_ids = {
                slot.runtime_selection
                for slot in state.participants
                if slot.runtime_selection not in {"", "default", "random"}
            }
            if explicit_runtime_ids.difference({probe.id for probe in probes}):
                probes = await self.discovery.scan(force_refresh=True)

            # 25% Validating bindings
            await self._initialization_progress(state, "validating_bindings", 25)
            self.preflight_room_bindings(state.participants, probes)

            # 30% Checking tool providers.  Never fatal: tool health is
            # informational for the debug view only.
            await self._initialization_progress(state, "checking_tools", 30)
            await self._run_tool_preflight(room_id, state)

            # 40% Loading personas & resolving bindings
            await self._initialization_progress(state, "loading_personas", 40)
            snapshots = await self.resolver.resolve_all(state.participants, probes)
            state.binding_snapshots = snapshots

            # 55% Loading memories
            await self._initialization_progress(state, "loading_memories", 55)
            for slot in state.participants:
                with contextlib.suppress(Exception):
                    self.continuum.memories.list_memories(slot.persona_id)

            # 70% Connecting agents
            self._active_agent_sessions[room_id] = {}
            self._persona_session_ids[room_id] = {}
            await self._initialization_progress(state, "connecting_agents", 70)

            async def _connect_slot(
                slot: ParticipantSlot,
            ) -> tuple[str, Any, RuntimeSessionBinding, str]:
                snapshot = snapshots[slot.participant_id]
                adapter = self.registry.get_adapter(snapshot.agent_runtime_id)
                if not adapter:
                    raise BindingPreflightError(
                        reason=f"Agent adapter {snapshot.agent_runtime_id} not registered",
                        participant_id=slot.participant_id,
                        runtime_id=snapshot.agent_runtime_id,
                        code="adapter_not_registered",
                    )
                session_cfg = AgentSessionConfig(
                    session_id=f"{room_id}_{slot.participant_id}",
                    room_id=room_id,
                    participant_id=slot.participant_id,
                    persona_id=slot.persona_id,
                    model_id=snapshot.model_id,
                    reasoning_effort=snapshot.reasoning_effort,
                    auth_profile_id=snapshot.auth_profile_id,
                    permission_profile=slot.permission_profile,  # type: ignore[arg-type]
                    allow_mcp=slot.allow_mcp,
                    tools=await self._tools_for_slot(slot),
                )
                if self.runtime_executor is None:
                    raise BindingPreflightError(
                        reason="Agent runtime executor is unavailable",
                        participant_id=slot.participant_id,
                        runtime_id=snapshot.agent_runtime_id,
                        code="runtime_executor_unavailable",
                    )
                binding = await self.runtime_executor.open_session(adapter, session_cfg)
                cont_sess = self.continuum.sessions.start_session(
                    persona_id=slot.persona_id,
                    title=f"Room {room_id} [{slot.participant_id}]",
                    counterpart_id=f"room:{room_id}",
                    session_type="multi_agent_room",
                    room_id=room_id,
                )
                return slot.participant_id, binding.session, binding, cont_sess.id

            results = await asyncio.gather(*[_connect_slot(slot) for slot in state.participants])
            self._active_agent_bindings[room_id] = {}
            for pid, agent_session, binding, cont_sess_id in results:
                self._active_agent_sessions[room_id][pid] = agent_session
                self._active_agent_bindings[room_id][pid] = binding
                self._persona_session_ids[room_id][pid] = cont_sess_id

            # 90% Validating sessions
            await self._initialization_progress(state, "validating_sessions", 90)
            for slot in state.participants:
                sess = self._active_agent_sessions[room_id].get(slot.participant_id)
                if not sess or not getattr(sess, "is_active", True):
                    raise BindingPreflightError(
                        reason=f"Agent session is not active for {slot.participant_id}",
                        participant_id=slot.participant_id,
                        runtime_id=snapshots[slot.participant_id].agent_runtime_id,
                        code="session_inactive",
                    )

            host_slot = next(
                (
                    slot
                    for slot in state.participants
                    if slot.participant_id == state.host_participant_id
                ),
                state.participants[0],
            )
            host_snapshot = snapshots[host_slot.participant_id]
            host_adapter = self.registry.get_adapter(host_snapshot.agent_runtime_id)
            if host_adapter and self.runtime_executor:
                host_binding = await self.runtime_executor.open_session(
                    host_adapter,
                    AgentSessionConfig(
                        session_id=f"{room_id}_host",
                        room_id=room_id,
                        participant_id="host",
                        persona_id="room_host",
                        model_id=host_snapshot.model_id,
                        reasoning_effort=host_snapshot.reasoning_effort,
                        permission_profile=PermissionProfile.CHAT_SAFE,
                        allow_mcp=False,
                        tools=[],
                    )
                )
                self._host_agent_sessions[room_id] = host_binding.session
                self._host_agent_bindings[room_id] = host_binding

            state.status = RoomStatus.READY
            state.updated_at = datetime.now(UTC)
            await self._initialization_progress(state, "ready", 100)
            await self._broadcast_event(
                room_id,
                {
                    "event": "room_started",
                    "room_id": room_id,
                    "status": state.status.value,
                    "binding_snapshots": {
                        key: value.model_dump(mode="json") for key, value in snapshots.items()
                    },
                },
            )

            return state

    def start_autonomous_discussion(
        self, room_id: str, topic: str | None = None, max_turns: int = 6
    ) -> asyncio.Task[None]:
        existing = self._autonomous_tasks.get(room_id)
        if existing and not existing.done():
            return existing
        state = self.get_room(room_id)
        if state and state.protocol != RoomProtocolType.FREE_DISCUSSION:
            raise ValueError("autonomous_discussion_only_for_free_discussion_protocol")

        async def _runner() -> None:
            try:
                async for _event in self.run_autonomous_discussion(room_id, max_turns=max_turns):
                    pass
            except Exception as exc:
                state = self.get_room(room_id)
                if state:
                    state.status = RoomStatus.ERROR
                    state.last_error = str(exc)
                    self._save_room_state(state)
                    await self._broadcast_event(
                        room_id,
                        {"event": "agent_error", "room_id": room_id, "error": str(exc)},
                    )
            finally:
                current = asyncio.current_task()
                if self._autonomous_tasks.get(room_id) is current:
                    self._autonomous_tasks.pop(room_id, None)

        task = asyncio.create_task(_runner())
        self._autonomous_tasks[room_id] = task
        return task

    def initialize_room_background(self, room_id: str) -> asyncio.Task[RoomSessionState]:
        task = self._initialization_tasks.get(room_id)
        if task and not task.done():
            return task
        task = asyncio.create_task(self._initialize_room_task(room_id))
        self._initialization_tasks[room_id] = task
        return task

    async def _initialize_room_task(self, room_id: str) -> RoomSessionState:
        # Hard ceiling so a single slow subprocess cannot pin a room at
        # "initializing" forever.  Anything longer than this surfaces as a
        # proper error with a reason the UI can render.
        deadline_seconds = 120.0
        try:
            return await asyncio.wait_for(
                self.start_room(room_id), timeout=deadline_seconds
            )
        except TimeoutError:
            exc = RuntimeError(
                f"Room initialization timed out after {deadline_seconds:g}s; "
                "the most likely cause is a tool provider subprocess that "
                "failed to start.  Inspect the room log and retry."
            )
            await self._mark_initialization_failed(room_id, exc)
            raise
        except Exception as exc:
            await self._mark_initialization_failed(room_id, exc)
            raise

    async def _mark_initialization_failed(
        self, room_id: str, exc: BaseException
    ) -> None:
        state = self.get_room(room_id)
        if state:
            state.status = RoomStatus.ERROR
            state.last_error = str(exc)
            state.initialization_stage = "error"
            self._save_room_state(state, force=True)
        err_dict = (
            exc.to_dict()
            if isinstance(exc, BindingPreflightError)
            else {"error": str(exc), "code": "room_init_error"}
        )
        await self._broadcast_event(
            room_id,
            {
                "event": "room_initialization_error",
                "room_id": room_id,
                "error": str(exc),
                "details": err_dict,
            },
        )

    def recover_interrupted_rooms(self) -> int:
        """Fail persisted work that cannot still be running after a restart.

        Agent sessions and asyncio tasks are process-local.  A persisted
        ``calling``/``initializing``/``discussing`` state therefore becomes an
        orphan when a new application process starts; presenting it as live
        makes the browser poll forever.  Preserve the room and its transcript,
        but make the interrupted operation explicit and retryable.
        """

        rows = self.continuum.database.conn.execute(
            "SELECT state_json FROM rooms"
        ).fetchall()
        recovered = 0
        for row in rows:
            with contextlib.suppress(Exception):
                state = RoomSessionState.model_validate(loads(row["state_json"]))
                call = state.metadata.get("model_call")
                call_status = str(call.get("status") or "") if isinstance(call, dict) else ""
                protocol_running = state.protocol_state.status.value == "running"
                process_local_status = state.status in {
                    RoomStatus.CREATING,
                    RoomStatus.INITIALIZING,
                    RoomStatus.DISCUSSING,
                }
                if not (process_local_status or call_status == "calling" or protocol_running):
                    continue

                now = datetime.now(UTC)
                reason = (
                    "上一轮房间任务因服务重启或后台任务中断而未完成，"
                    "请重新初始化或重试。"
                )
                state.status = RoomStatus.ERROR
                state.last_error = reason
                if state.initialization_stage not in {"ready", "error"}:
                    state.initialization_stage = "error"
                state.metadata["model_call"] = {
                    **(call if isinstance(call, dict) else {}),
                    "status": "interrupted",
                    "error": "room_activity_interrupted",
                    "finished_at": now.isoformat(),
                }
                if protocol_running:
                    state.protocol_state.status = RoomRunStatus.FAILED
                    state.protocol_state.active_participant_ids = []
                    state.protocol_state.finished_at = now
                    if state.protocol_state.run_id and state.protocol_state.started_at:
                        self.protocol_repository.save_run(
                            room_id=state.id,
                            protocol=state.protocol,
                            state=state.protocol_state,
                        )
                state.updated_at = now
                self._save_room_state(state, force=True)
                recovered += 1
        return recovered

    def normalize_legacy_room_tool_permissions(self) -> int:
        """Strip removed tool names from rooms created by older builds.

        Rooms persisted by older builds may still list tool names that are
        no longer granted by any built-in provider.  Only rooms created
        from the built-in consultation template are touched, and only
        exact matches of the removed names; every other permission, room
        and field is preserved so legacy rooms stay openable.
        """

        rows = self.continuum.database.conn.execute(
            "SELECT state_json FROM rooms"
        ).fetchall()
        normalized = 0
        for row in rows:
            with contextlib.suppress(Exception):
                state = RoomSessionState.model_validate(loads(row["state_json"]))
                if state.template_id != DIVINATION_TEMPLATE_ID:
                    continue
                changed = False
                for participant in state.participants:
                    if not participant.tool_permissions:
                        continue
                    kept = [
                        name
                        for name in participant.tool_permissions
                        if name not in _LEGACY_REMOVED_TOOL_NAMES
                    ]
                    if kept != participant.tool_permissions:
                        participant.tool_permissions = kept
                        changed = True
                if changed:
                    self._save_room_state(state, force=True)
                    normalized += 1
        return normalized

    async def mark_background_turn_failed(
        self, room_id: str, exc: BaseException
    ) -> None:
        """Persist a terminal state when a detached turn task crashes."""

        state = self.get_room(room_id)
        if state is None:
            return
        now = datetime.now(UTC)
        message = str(exc) or type(exc).__name__
        state.status = RoomStatus.ERROR
        state.last_error = message
        call = state.metadata.get("model_call")
        state.metadata["model_call"] = {
            **(call if isinstance(call, dict) else {}),
            "status": "error",
            "error": message,
            "finished_at": now.isoformat(),
        }
        state.updated_at = now
        self._save_room_state(state, force=True)
        await self._broadcast_event(
            room_id,
            {"event": "agent_error", "room_id": room_id, "error": message},
        )

    async def wait_for_initialization(self, room_id: str) -> RoomSessionState:
        task = self._initialization_tasks.get(room_id)
        if task and not task.done():
            return await task
        return await self.initialize_room_background(room_id)

    async def step_turn(
        self,
        room_id: str,
        manual_speaker_id: str | None = None,
        user_message: str = "",
    ) -> AsyncIterator[dict[str, Any]]:
        if room_id in self._converting_rooms:
            # A conversion rewrites roles/protocol and persists the state;
            # a concurrent turn would persist a stale pre-conversion
            # snapshot over it.
            raise RoomBusyError(
                "room_converting",
                "房间正在进行协议迁移，请等待迁移完成后重试。",
            )
        state = self.get_room(room_id)
        if not state or state.status not in {RoomStatus.READY, RoomStatus.DISCUSSING}:
            st_val = state.status.value if state else "None"
            yield {
                "event": "agent_error",
                "error": f"Room {room_id} is not active (status: {st_val})",
            }
            return

        lock = self._room_locks.setdefault(room_id, asyncio.Lock())
        async with lock:
            # 1. Select Speaker
            if manual_speaker_id:
                speaker_id = manual_speaker_id
                director_reason = "Manual override selection"
            elif state.mode == RoomMode.AUTONOMOUS:
                decision = self.discussion_director.select_next_speaker(
                    conversation=state.transcript,
                    participants=state.participants,
                    topic=state.topic or "General discussion",
                    relationships={
                        participant.participant_id: dict(participant.relationships)
                        for participant in state.participants
                    },
                )
                speaker_id = decision.next_speaker
                director_reason = decision.reason
                director_event = {
                    "event": "discussion_director_selected",
                    "room_id": room_id,
                    **decision.model_dump(),
                }
                yield director_event
                await self._broadcast_event(room_id, director_event)
            else:
                self.director.config = state.director_config
                speaker_id, director_reason = self.director.select_next_speaker(
                    state.participants,
                    state.transcript,
                    topic=state.topic,
                    manual_override_id=manual_speaker_id,
                )
            state.status = RoomStatus.DISCUSSING
            state.current_speaker_id = speaker_id
            self._save_room_state(state)

            slot = next((p for p in state.participants if p.participant_id == speaker_id), None)
            if not slot:
                yield {"event": "agent_error", "error": f"Participant not found: {speaker_id}"}
                return

            snapshot = state.binding_snapshots.get(speaker_id)
            if not snapshot:
                yield {"event": "agent_error", "error": f"Snapshot missing for {speaker_id}"}
                return

            speaker_event = {
                "event": "speaker_selected",
                "room_id": room_id,
                "turn_index": state.turn_index,
                "participant_id": speaker_id,
                "persona_id": slot.persona_id,
                "speaker_name": slot.display_name or slot.persona_id,
                "agent_runtime_id": snapshot.agent_runtime_id,
                "model_id": snapshot.model_id,
                "reasoning_effort": snapshot.reasoning_effort,
                "director_reason": director_reason,
            }
            yield speaker_event
            await self._broadcast_event(room_id, speaker_event)

            # 2. Recall Gate Analysis (single shared retrieval for the turn)
            from persona_continuum.performance.tracing import default_tracer

            tracer = default_tracer()
            turn_task_id = f"room:{room_id}:turn:{state.turn_index}:{speaker_id}"
            tracer.start_task("room_turn", turn_task_id)
            tracer.record(
                turn_task_id,
                room_id=room_id,
                participant_id=speaker_id,
                persona_id=slot.persona_id,
            )
            recall_start_event = {
                "event": "recall_started",
                "room_id": room_id,
                "participant_id": speaker_id,
                "persona_id": slot.persona_id,
                "timestamp": datetime.now(UTC).isoformat(),
            }
            yield recall_start_event
            await self._broadcast_event(room_id, recall_start_event)

            with tracer.span(turn_task_id, "prepare_turn"):
                with tracer.span(turn_task_id, "memory_search"):
                    gate_plan = self.recall_gate.plan_turn(
                        persona_id=slot.persona_id,
                        current_speaker_name=slot.display_name or slot.persona_id,
                        user_message=user_message,
                        recent_transcript=state.transcript,
                    )
                    shared_memories: list[Any] | None = None
                    if gate_plan.triggered:
                        # One retrieval serves the gate, the context builder,
                        # and the prompt composer for this turn.
                        shared_memories = self.continuum.memories.search_memories(
                            persona_id=slot.persona_id,
                            query=gate_plan.query,
                            limit=8,
                            branch_id="main",
                            include_main_history=True,
                            include_shared_pre_divergence=True,
                        )
                recall_result = RecallGate.attach_memories(
                    gate_plan, list(shared_memories or [])
                )

            # 3. Context Preparation
            adapter = self.registry.get_adapter(snapshot.agent_runtime_id)
            agent_session = self._active_agent_sessions.get(room_id, {}).get(speaker_id)
            agent_binding = self._active_agent_bindings.get(room_id, {}).get(speaker_id)

            recall_complete_event = {
                "event": "recall_completed",
                "room_id": room_id,
                "participant_id": speaker_id,
                "persona_id": slot.persona_id,
                "triggered": recall_result.triggered,
                "reasons": recall_result.reasons,
                "retrieved_count": len(recall_result.memories),
                "retrieved_memories": [
                    {"id": m.id, "content": m.content, "importance": m.importance}
                    for m in recall_result.memories
                ],
                "timestamp": datetime.now(UTC).isoformat(),
            }
            yield recall_complete_event
            await self._broadcast_event(room_id, recall_complete_event)

            sess_id = self._persona_session_ids.get(room_id, {}).get(speaker_id)
            if not sess_id:
                cont_sess = self.continuum.sessions.start_session(
                    persona_id=slot.persona_id,
                    title=f"Room {room_id} [{slot.participant_id}]",
                    counterpart_id=f"room:{room_id}",
                    session_type="multi_agent_room",
                    room_id=room_id,
                )
                sess_id = cont_sess.id
                self._persona_session_ids.setdefault(room_id, {})[speaker_id] = sess_id

            with tracer.span(turn_task_id, "context_build"):
                prepared = self.continuum.sessions.prepare_turn(
                    persona_id=slot.persona_id,
                    session_id=sess_id,
                    user_message=user_message or state.topic or "Continue the discussion",
                    counterpart_id=f"room:{room_id}",
                    preset_memories=shared_memories,
                )

            persistent = self._participant_is_persistent(
                adapter=adapter,
                session=agent_session,
                snapshot=snapshot,
            )
            stored_summary = state.metadata.get("rolling_summary")
            summary_block = stored_summary if isinstance(stored_summary, str) else None
            transport_capability = self._resolve_transport_capability(
                room_id, speaker_id, adapter, snapshot.agent_runtime_id
            )
            turn_ctx = self.context_manager.prepare(
                room_id=room_id,
                participant_id=speaker_id,
                transcript=list(state.transcript or []),
                persistent=persistent,
                summary_block=summary_block,
                transport_budget_bytes=budget_for(transport_capability),
            )
            if turn_ctx.mode == "rehydrated":
                rehydrate_ev = {
                    "event": "room_context_rehydrated",
                    "room_id": room_id,
                    "participant_id": speaker_id,
                    "turn_index": state.turn_index,
                }
                yield rehydrate_ev
                await self._broadcast_event(room_id, rehydrate_ev)

            with tracer.span(turn_task_id, "prompt_build"):
                kernel = None
                if self.kernel_cache is not None:
                    kernel = self.kernel_cache.get_or_build(
                        prepared,
                        display_name=slot.display_name or slot.persona_id,
                    )
                cursor_span = (
                    (turn_ctx.cursor_before + 1, len(state.transcript))
                    if turn_ctx.mode == "delta" and turn_ctx.turns
                    else None
                )
                system_prompt, composed_user_prompt, _ = self.prompt_composer.compose_turn_prompt(
                    slot=slot,
                    binding=snapshot,
                    prepared=prepared,
                    dynamic_recall_memories=recall_result.memories,
                    recent_transcript=turn_ctx.turns,
                    room_topic=state.topic,
                    user_message=user_message,
                    kernel=kernel,
                    context_mode=turn_ctx.mode,
                    summary_block=(
                        turn_ctx.summary_block
                        if turn_ctx.mode in {"windowed", "rehydrated"}
                        else None
                    ),
                    cursor_span=cursor_span,
                )
                # One computation feeds both the advertised tool list and the
                # prompt block, so they can never drift apart.
                tools_for_turn = await self._tools_for_slot(slot)
                tool_block = RoomToolBroker.render_tool_block(tools_for_turn)
                # Generic room-level and participant-specific safety context.
                # Parallel World supplies the temporal firewall; Narrative
                # Studio adds a separately filtered block per character.
                firewall_instruction = state.metadata.get("firewall")
                participant_blocks = state.metadata.get("participant_prompt_blocks")
                scoped_context = (
                    participant_blocks.get(speaker_id)
                    if isinstance(participant_blocks, dict)
                    else None
                )
                safety_sections = [
                    str(firewall_instruction or ""),
                    json.dumps(scoped_context, ensure_ascii=False, default=str)
                    if scoped_context
                    else "",
                ]
                safety_context = "\n\n".join(item for item in safety_sections if item)
                if safety_context:
                    composed_user_prompt = (
                        f"{composed_user_prompt}\n\nSCOPED RUNTIME CONTEXT:\n{safety_context}"
                    )
                if turn_ctx.mode == "transport_truncated" and turn_ctx.salvaged_facts:
                    # Older user turns fell outside the transported window;
                    # what the user said is the case anchor and rides along
                    # even when the raw transcript cannot.
                    salvaged = "\n".join(f"- {fact}" for fact in turn_ctx.salvaged_facts)
                    composed_user_prompt = (
                        f"Earlier user statements (salvaged):\n{salvaged}\n\n"
                        f"{composed_user_prompt}"
                    )
                # The free-discussion path advertises exactly the tools
                # `tools_for_turn` and `_execute_tool_cb` will accept.
                system_prompt = f"{system_prompt}\n\n{tool_block}"
            tracer.count(
                turn_task_id,
                "transcript_tokens_sent",
                _estimate_tokens(
                    "\n".join(str(t.get("content") or "") for t in turn_ctx.turns)
                ),
            )
            tracer.observe(
                turn_task_id, "memory_results_count", len(prepared.relevant_memories)
            )
            tracer.count(turn_task_id, f"ctx_{turn_ctx.mode}", 1)
            if turn_ctx.mode == "delta":
                tracer.count(turn_task_id, "delta", 1)
            self.context_manager.commit(room_id, speaker_id, turn_ctx)

            if not adapter or not agent_session:
                err_ev: dict[str, Any] = {
                    "event": "agent_error",
                    "room_id": room_id,
                    "participant_id": speaker_id,
                    "error": f"Agent session not available for {snapshot.agent_runtime_id}",
                }
                yield err_ev
                await self._broadcast_event(room_id, err_ev)
                tracer.finish_task(turn_task_id)
                return

            # 4. Agent Invocation & Streaming
            agent_started_event = {
                "event": "agent_started",
                "room_id": room_id,
                "participant_id": speaker_id,
                "persona_id": slot.persona_id,
                "agent_runtime_id": snapshot.agent_runtime_id,
                "model_id": snapshot.model_id,
                "reasoning_effort": snapshot.reasoning_effort,
                "timestamp": datetime.now(UTC).isoformat(),
            }
            yield agent_started_event
            await self._broadcast_event(room_id, agent_started_event)
            state.metadata["model_call"] = {
                "status": "calling",
                "participant_id": speaker_id,
                "display_name": slot.display_name or slot.persona_id,
                "model_id": snapshot.model_id,
                "runtime_id": snapshot.agent_runtime_id,
                "started_at": datetime.now(UTC).isoformat(),
            }
            self._save_room_state(state)

            slot_permissions = self._slot_tool_permissions(slot)

            async def _execute_tool_cb(name: str, args: dict[str, Any]) -> Any:
                # Same gate that produced `tools_for_turn`: a tool the model
                # was never shown must not become callable here.
                return await self.tool_broker.execute_tool(
                    persona_id=slot.persona_id,
                    tool_name=name,
                    arguments=args,
                    participant_id=slot.participant_id,
                    room_id=room_id,
                    tool_permissions=slot_permissions,
                    allow_agent_tools=bool(slot.allow_agent_tools),
                )

            # Tool availability is an explicit room policy.  The adapter owns
            # the protocol mapping, so the room must not infer capabilities
            # from vendor or runtime-id naming conventions.
            use_tools = bool(slot.allow_agent_tools)
            if not agent_binding and self.runtime_executor:
                agent_binding = await self.runtime_executor.bind_existing_session(
                    adapter, agent_session
                )
                self._active_agent_bindings.setdefault(room_id, {})[speaker_id] = agent_binding
            if not agent_binding or not self.runtime_executor:
                raise RuntimeError("Agent runtime binding is unavailable")
            turn_id = new_id("turn")

            full_response_parts: list[str] = []
            turn_failed = False
            turn_cancelled = False
            agent_error_emitted = False
            runtime_recovery_needed = False
            error_message: str | None = None
            completion_metadata: dict[str, Any] = {}
            response_collector = AgentResponseCollector(
                protocol=str(
                    agent_session.session_data.get("protocol")
                    or agent_session.session_data.get("mode")
                    or "unknown"
                ),
                diagnostics={"room_id": room_id, "participant_id": speaker_id},
            )

            stream_started = time.monotonic()
            first_token_at: float | None = None
            try:
                stream_events_agen = self.runtime_executor.stream_events(
                    agent_binding,
                    system_prompt=system_prompt,
                    user_message=(
                        composed_user_prompt
                        or user_message
                        or state.topic
                        or "Continue conversation"
                    ),
                    tools=tools_for_turn if use_tools else [],
                    phase="room_agent_turn",
                    metadata={"turn_id": turn_id, "tool_executor": _execute_tool_cb},
                )
                async with aclosing(stream_events_agen):
                    async for event in stream_events_agen:
                        response_collector.add(event)
                        if event.type == AgentEventType.CHUNK:
                            if first_token_at is None:
                                first_token_at = time.monotonic()
                                tracer.record(
                                    turn_task_id,
                                    agent_ttft_ms=round(
                                    (first_token_at - stream_started) * 1000, 1
                                ),
                                )
                            if event.metadata.get("replace_response"):
                                full_response_parts.clear()
                            full_response_parts.append(event.content)
                            chunk_ev = {
                                "event": "agent_message_delta",
                                "room_id": room_id,
                                "participant_id": speaker_id,
                                "delta": event.content,
                                "replace_response": bool(event.metadata.get("replace_response")),
                            }
                            yield chunk_ev
                            await self._broadcast_event(room_id, chunk_ev)

                        elif event.type == AgentEventType.THINKING:
                            think_ev = {
                                "event": "agent_thinking",
                                "room_id": room_id,
                                "participant_id": speaker_id,
                                "thinking": event.thinking,
                            }
                            yield think_ev
                            await self._broadcast_event(room_id, think_ev)

                        elif event.type == AgentEventType.TOOL_CALL:
                            tool_call_ev = {
                                "event": "agent_tool_call",
                                "room_id": room_id,
                                "participant_id": speaker_id,
                                "tool_call_id": event.tool_call_id,
                                "tool_name": event.tool_name,
                                "tool_arguments": event.tool_arguments,
                            }
                            yield tool_call_ev
                            await self._broadcast_event(room_id, tool_call_ev)

                        elif event.type == AgentEventType.TOOL_RESULT:
                            compact_result = compact_tool_result(
                                event.tool_result or event.content,
                                tool_name=event.tool_name,
                                artifact_ref=f"tool-result:{event.tool_call_id or 'unknown'}",
                            )
                            tool_res_ev = {
                                "event": "agent_tool_result",
                                "room_id": room_id,
                                "participant_id": speaker_id,
                                "tool_call_id": event.tool_call_id,
                                "tool_result": compact_result,
                            }
                            yield tool_res_ev
                            await self._broadcast_event(room_id, tool_res_ev)

                        elif event.type == AgentEventType.ERROR:
                            turn_failed = True
                            error_message = event.error or "Unknown agent error"
                            failure_marker = str(event.metadata.get("failure_code") or "")
                            runtime_recovery_needed = bool(failure_marker) or any(
                                token in error_message.casefold()
                                for token in (
                                    "transport",
                                    "process exited",
                                    "broken pipe",
                                    "pooled_runtime_affinity_unavailable",
                                )
                            )
                            err_ev = {
                                "event": "agent_error",
                                "room_id": room_id,
                                "participant_id": speaker_id,
                                "error": error_message,
                                "failure_code": event.metadata.get("failure_code"),
                                "diagnostics": dict(event.metadata or {}),
                            }
                            yield err_ev
                            await self._broadcast_event(room_id, err_ev)
                            agent_error_emitted = True
                            break

                        elif event.type == AgentEventType.DONE:
                            completion_metadata = dict(event.metadata)
                            if event.metadata.get("cancelled") or (
                                agent_session._cancel_event and agent_session._cancel_event.is_set()
                            ):
                                turn_cancelled = True
                            break

            except asyncio.CancelledError:
                # Cancellation of the detached turn task must never leave the
                # durable room claiming that the model is still running.
                now = datetime.now(UTC)
                error_message = "room_turn_task_cancelled"
                state.status = RoomStatus.ERROR
                state.last_error = error_message
                state.metadata["model_call"] = {
                    **state.metadata.get("model_call", {}),
                    "status": "interrupted",
                    "error": error_message,
                    "finished_at": now.isoformat(),
                }
                state.failed_turn_audits.append(
                    {
                        "turn_index": state.turn_index,
                        "participant_id": speaker_id,
                        "persona_id": slot.persona_id,
                        "error": error_message,
                        "partial_content": "".join(full_response_parts).strip(),
                        "cancelled": True,
                        "timestamp": now.isoformat(),
                        "agent_runtime_id": snapshot.agent_runtime_id,
                        "model_id": snapshot.model_id,
                    }
                )
                self._save_room_state(state, force=True)
                with contextlib.suppress(Exception):
                    await self._broadcast_event(
                        room_id,
                        {
                            "event": "agent_error",
                            "room_id": room_id,
                            "participant_id": speaker_id,
                            "error": error_message,
                            "failure_code": "ROOM_TURN_INTERRUPTED",
                        },
                    )
                tracer.finish_task(turn_task_id)
                raise
            except Exception as exc:
                turn_failed = True
                error_message = str(exc)
                runtime_recovery_needed = isinstance(exc, AgentRuntimeError)
                failure_code = exc.code if isinstance(exc, AgentRuntimeError) else None
                err_ev = {
                    "event": "agent_error",
                    "room_id": room_id,
                    "participant_id": speaker_id,
                    "error": str(exc),
                    "failure_code": failure_code,
                    "diagnostics": (
                        dict(exc.diagnostics) if isinstance(exc, AgentRuntimeError) else {}
                    ),
                }
                yield err_ev
                await self._broadcast_event(room_id, err_ev)
                agent_error_emitted = True

            tracer.record(
                turn_task_id,
                agent_generation_ms=round((time.monotonic() - stream_started) * 1000, 1),
            )
            try:
                final_content = response_collector.require_text(
                    phase="room_agent_turn", job_id=room_id
                )
            except Exception as exc:
                final_content = "".join(full_response_parts).strip()
                if not error_message:
                    error_message = str(exc)
                turn_failed = True
            completion_metadata = {
                **completion_metadata,
                "usage": response_collector.response.usage,
                "audit": response_collector.response.audit(
                    call_id=turn_id, job_id=room_id, phase="room_agent_turn"
                ),
            }
            if (
                agent_session._cancel_event and agent_session._cancel_event.is_set()
            ) or turn_cancelled:
                # Transactional integrity for cancelled turns: abort before commit
                state.last_error = "turn_cancelled"
                state.status = RoomStatus.READY
                failed_audit = {
                    "turn_index": state.turn_index,
                    "participant_id": speaker_id,
                    "persona_id": slot.persona_id,
                    "error": "turn_cancelled",
                    "partial_content": final_content,
                    "cancelled": True,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "agent_runtime_id": snapshot.agent_runtime_id,
                    "model_id": snapshot.model_id,
                }
                state.failed_turn_audits.append(failed_audit)
                self._save_room_state(state, force=True)
                if agent_session._cancel_event:
                    agent_session._cancel_event.clear()

                cancel_ev = {
                    "event": "turn_cancelled",
                    "room_id": room_id,
                    "participant_id": speaker_id,
                    "turn_index": state.turn_index,
                    "partial_content": final_content,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
                yield cancel_ev
                await self._broadcast_event(room_id, cancel_ev)
                tracer.finish_task(turn_task_id)
                return

            if turn_failed or error_message or not final_content:
                # Transactional integrity: never commit failed/empty turns
                state.last_error = error_message or "Turn failed without content"
                state.status = RoomStatus.ERROR
                failed_audit = {
                    "turn_index": state.turn_index,
                    "participant_id": speaker_id,
                    "persona_id": slot.persona_id,
                    "error": state.last_error,
                    "partial_content": final_content,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "agent_runtime_id": snapshot.agent_runtime_id,
                    "model_id": snapshot.model_id,
                }
                state.failed_turn_audits.append(failed_audit)
                state.metadata["model_call"] = {
                    "status": "error",
                    "participant_id": speaker_id,
                    "display_name": slot.display_name or slot.persona_id,
                    "model_id": snapshot.model_id,
                    "runtime_id": snapshot.agent_runtime_id,
                    "error": state.last_error,
                    "finished_at": datetime.now(UTC).isoformat(),
                }
                self._save_room_state(state, force=True)
                if runtime_recovery_needed:
                    restarted = await self.restart_session(room_id, speaker_id)
                    if restarted:
                        state.status = RoomStatus.READY
                        state.metadata["runtime_recovery"] = {
                            "participant_id": speaker_id,
                            "status": "session_recreated",
                            "next_context_mode": "rehydrated",
                            "timestamp": datetime.now(UTC).isoformat(),
                        }
                        self._save_room_state(state, force=True)
                        recovery_ev = {
                            "event": "room_runtime_recovered",
                            "room_id": room_id,
                            "participant_id": speaker_id,
                            "next_context_mode": "rehydrated",
                        }
                        yield recovery_ev
                        await self._broadcast_event(room_id, recovery_ev)
                if not agent_error_emitted:
                    err_ev = {
                        "event": "agent_error",
                        "room_id": room_id,
                        "participant_id": speaker_id,
                        "error": state.last_error,
                    }
                    yield err_ev
                    await self._broadcast_event(room_id, err_ev)
                    agent_error_emitted = True
                tracer.finish_task(turn_task_id)
                return

            state.last_error = None
            agent_completed_ev = {
                "event": "agent_completed",
                "room_id": room_id,
                "participant_id": speaker_id,
                "content": final_content,
                "usage": completion_metadata.get("usage", {}),
                "model_id": snapshot.model_id,
                "agent_runtime_id": snapshot.agent_runtime_id,
                "decision_source": "llm",
                "timestamp": datetime.now(UTC).isoformat(),
            }
            yield agent_completed_ev
            await self._broadcast_event(room_id, agent_completed_ev)

            # 5. Commit Persona State
            commit_start_ev = {
                "event": "persona_commit_started",
                "room_id": room_id,
                "participant_id": speaker_id,
                "persona_id": slot.persona_id,
            }
            yield commit_start_ev
            await self._broadcast_event(room_id, commit_start_ev)

            recall_ids = [m.id for m in recall_result.memories]
            commit_res = self.continuum.sessions.commit_turn(
                persona_id=slot.persona_id,
                session_id=sess_id,
                user_message=user_message or state.topic or "Room discussion turn",
                persona_response=final_content,
                used_memory_ids=recall_ids,
                counterpart_id=f"room:{room_id}",
            )

            commit_complete_ev = {
                "event": "persona_commit_completed",
                "room_id": room_id,
                "participant_id": speaker_id,
                "persona_id": slot.persona_id,
                "turn_id": commit_res.get("turn_id"),
                "memory_id": commit_res.get("memory_id"),
                # Public delta summary ("anxiety 18% -> 27% +9%").  The UI
                # refetches runtime state on this event instead of polling.
                "state_summary": commit_res.get("state_summary") or "",
            }
            yield commit_complete_ev
            await self._broadcast_event(room_id, commit_complete_ev)

            # 6. Save Transcript Record
            now_str = datetime.now(UTC).isoformat()
            transcript_entry = {
                "turn_id": commit_res.get("turn_id") or new_id("turn"),
                "participant_id": speaker_id,
                "persona_id": slot.persona_id,
                "speaker_name": slot.display_name or slot.persona_id,
                "agent_runtime_id": snapshot.agent_runtime_id,
                "model_id": snapshot.model_id,
                "reasoning_effort": snapshot.reasoning_effort,
                "content": final_content,
                "director_reason": director_reason,
                "recall_ids": recall_ids,
                "created_at": now_str,
                "usage": completion_metadata.get("usage", {}),
                "decision_source": "llm",
            }
            state.transcript.append(transcript_entry)
            state.turn_index += 1
            state.metadata["model_call"] = {
                "status": "completed",
                "participant_id": speaker_id,
                "display_name": slot.display_name or slot.persona_id,
                "model_id": snapshot.model_id,
                "runtime_id": snapshot.agent_runtime_id,
                "finished_at": now_str,
            }
            state.status = (
                RoomStatus.DISCUSSING if state.mode == RoomMode.AUTONOMOUS else RoomStatus.READY
            )
            usage = completion_metadata.get("usage") or {}
            if isinstance(usage, dict):
                tracer.observe(
                    turn_task_id, "prompt_tokens", int(usage.get("input_tokens") or 0)
                )
                tracer.observe(
                    turn_task_id, "completion_tokens", int(usage.get("output_tokens") or 0)
                )
            totals = state.metadata.setdefault(
                "token_usage", {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            )
            if isinstance(totals, dict) and isinstance(usage, dict):
                for key in ("input_tokens", "output_tokens", "total_tokens"):
                    totals[key] = int(totals.get(key, 0)) + int(usage.get(key, 0) or 0)
            state.updated_at = datetime.now(UTC)

            persisted_started = time.monotonic()
            self._save_transcript_record(
                RoomTranscriptRecord(
                    id=new_id("rturn"),
                    room_id=room_id,
                    turn_id=str(transcript_entry["turn_id"]),
                    participant_id=speaker_id,
                    persona_id=slot.persona_id,
                    speaker_name=str(transcript_entry["speaker_name"]),
                    agent_runtime_id=snapshot.agent_runtime_id,
                    agent_session_id=agent_session.config.session_id,
                    model_id=snapshot.model_id,
                    reasoning_effort=snapshot.reasoning_effort,
                    content=final_content,
                    director_reason=director_reason,
                    recall_ids=recall_ids,
                    commit_status="committed",
                    metadata={
                        "usage": completion_metadata.get("usage", {}),
                        "decision_source": "llm",
                    },
                )
            )
            self._save_room_state(state, force=True)
            tracer.observe(
                turn_task_id,
                "persistence_ms",
                int(round((time.monotonic() - persisted_started) * 1000, 1)),
            )

            # Summary refresh runs in the background: the turn completes on
            # its reply, and if the refresh is still running on the next
            # turn, that turn proceeds with the old summary + recent raw
            # turns (no context is dropped).
            self._schedule_room_summary(state, persistent=persistent)
            tracer.finish_task(turn_task_id)

            turn_completed_ev = {
                "event": "turn_completed",
                "room_id": room_id,
                "turn_index": state.turn_index,
                "turn": transcript_entry,
            }
            yield turn_completed_ev
            await self._broadcast_event(room_id, turn_completed_ev)

    async def run_autonomous_discussion(
        self, room_id: str, max_turns: int = 6
    ) -> AsyncIterator[dict[str, Any]]:
        state = self.get_room(room_id)
        if not state:
            raise KeyError(room_id)
        if state.mode != RoomMode.AUTONOMOUS:
            raise ValueError("Room is not in autonomous mode")
        if state.protocol != RoomProtocolType.FREE_DISCUSSION:
            raise ValueError("autonomous_discussion_only_for_free_discussion_protocol")
        if state.status in {RoomStatus.CREATING, RoomStatus.INITIALIZING}:
            state = await self.wait_for_initialization(room_id)
        if state.status == RoomStatus.ERROR:
            raise RuntimeError(state.last_error or "Room initialization failed")
        if state.status not in {RoomStatus.READY, RoomStatus.DISCUSSING}:
            raise RuntimeError(f"Room cannot discuss from status {state.status.value}")
        self._clear_session_cancel_events(room_id)
        state.last_error = None
        state.status = RoomStatus.DISCUSSING
        self._save_room_state(state)
        max_turns = max(1, min(max_turns, 20))

        started = {
            "event": "autonomous_discussion_started",
            "room_id": room_id,
            "topic": state.topic,
            "max_turns": max_turns,
        }
        yield started
        await self._broadcast_event(room_id, started)

        user_opening = _latest_user_injection(state)
        if user_opening:
            opening = user_opening
        else:
            opening = await self._host_message_or_fallback(state, "open")
            self._record_host_message(state, opening, "open")
            host_opened = {
                "event": "host_opened",
                "room_id": room_id,
                "content": opening,
            }
            yield host_opened
            await self._broadcast_event(room_id, host_opened)

        for index in range(max_turns):
            state = self.get_room(room_id) or state
            if state.status == RoomStatus.PAUSED:
                return
            before_turn = state.turn_index
            prompt = str(state.transcript[-1].get("content", "")) if state.transcript else opening
            async for event in self.step_turn(
                room_id,
                user_message=prompt,
            ):
                yield event
            state = self.get_room(room_id) or state
            if state.status == RoomStatus.ERROR or state.turn_index == before_turn:
                raise RuntimeError(state.last_error or "Autonomous Agent turn did not complete")

            if max_turns >= 4 and index + 1 == max_turns // 2:
                state = self.get_room(room_id) or state
                guidance = await self._host_message_or_fallback(state, "steer")
                self._record_host_message(state, guidance, "steer")
                steered = {
                    "event": "host_steered",
                    "room_id": room_id,
                    "content": guidance,
                }
                yield steered
                await self._broadcast_event(room_id, steered)

        state = self.get_room(room_id) or state
        summary = await self._host_message_or_fallback(state, "summary")
        self._record_host_message(state, summary, "summary")
        state.metadata["host_summary"] = summary
        state.metadata["autonomous_completed_at"] = datetime.now(UTC).isoformat()
        state.status = RoomStatus.COMPLETED
        self._save_room_state(state)
        completed = {
            "event": "host_summarized",
            "room_id": room_id,
            "content": summary,
            "turns": max_turns,
        }
        yield completed
        await self._broadcast_event(room_id, completed)

    async def run_protocol(self, room_id: str, question: str) -> RoomSessionState:
        # Single-flight guard: synchronous callers (API sync path, narrative
        # service) serialise on a per-room lock so two concurrent runs can
        # never interleave their state transitions.  Background starts keep
        # their immediate-reject _protocol_tasks check on top.
        lock = self._protocol_run_locks.setdefault(room_id, asyncio.Lock())
        async with lock:
            return await self._run_protocol_locked(room_id, question)

    async def _run_protocol_locked(self, room_id: str, question: str) -> RoomSessionState:
        state = self.get_room(room_id)
        if not state:
            raise KeyError(room_id)
        if state.status in {RoomStatus.CREATING, RoomStatus.INITIALIZING}:
            state = await self.wait_for_initialization(room_id)
        if state.status not in {RoomStatus.READY, RoomStatus.DISCUSSING}:
            raise RuntimeError(f"Room cannot run protocol from {state.status.value}")
        if room_id in self._converting_rooms:
            raise RoomBusyError(
                "room_converting",
                "房间正在进行协议迁移，请等待迁移完成后重试。",
            )
        # Validate BEFORE mutating: a participant/runtime pre-check failure
        # must never leave the room stuck in DISCUSSING (fix #4).
        definition = self.protocol_registry.get(state.protocol, state.protocol_config)
        self.protocol_registry.validate_participants(definition, state.participants)
        self._clear_session_cancel_events(room_id)
        # New user input merges into the case state; room.topic stays stable
        # so follow-ups never displace the original case anchor.  An empty
        # question falls back to the topic for EXECUTION only -- the merge
        # still uses the raw question and is skipped entirely when empty.
        if str(question).strip():
            state.case_state = merge_case_state(
                _coerce_case_state(state.case_state), question
            )
        state.status = RoomStatus.DISCUSSING
        self._save_room_state(state, force=True)
        try:
            protocol_state = await self.protocol_runtime.run(
                state,
                question or state.topic or "",
                self._execute_protocol_action,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A crashed run must surface as a retryable room ERROR, never a
            # permanently DISCUSSING room; re-raise so background callers
            # still observe the failure via the task exception.
            state.status = RoomStatus.ERROR
            state.last_error = f"room_protocol_run_failed: {type(exc).__name__}: {exc}"
            self._save_room_state(state, force=True)
            await self._broadcast_event(
                room_id,
                {
                    "event": "room_protocol_error",
                    "room_id": room_id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            raise
        state.protocol_state = protocol_state
        # waiting_clarification is the host asking the user one indispensable
        # question: a finished, successful outcome -- never a room error.
        settled = {"success", "partial_success", "cancelled", "waiting_clarification"}
        state.status = (
            RoomStatus.READY if protocol_state.status.value in settled else RoomStatus.ERROR
        )
        state.last_error = (
            None if protocol_state.status.value in settled else "room_protocol_run_failed"
        )
        self._save_room_state(state, force=True)
        return state

    def start_protocol_background(
        self, room_id: str, question: str
    ) -> asyncio.Task[RoomSessionState]:
        if room_id in self._converting_rooms:
            raise RoomBusyError(
                "room_converting",
                "房间正在进行协议迁移，请等待迁移完成后重试。",
            )
        existing = self._protocol_tasks.get(room_id)
        if existing and not existing.done():
            raise ValueError("room_protocol_run_already_active")
        task = asyncio.create_task(self.run_protocol(room_id, question))
        self._protocol_tasks[room_id] = task

        def _clear(completed: asyncio.Task[RoomSessionState]) -> None:
            if self._protocol_tasks.get(room_id) is completed:
                self._protocol_tasks.pop(room_id, None)
            if completed.cancelled():
                return
            exc = completed.exception()
            if exc is None:
                return
            # Mirror the ERROR marker: a background run that dies before its
            # own terminal bookkeeping must never leave the room stuck in
            # DISCUSSING (mirrors start_autonomous_discussion._runner).
            state = self.get_room(room_id)
            if state is None or state.status == RoomStatus.ERROR:
                return
            state.status = RoomStatus.ERROR
            state.last_error = f"room_protocol_run_failed: {type(exc).__name__}: {exc}"
            self._save_room_state(state, force=True)

        task.add_done_callback(_clear)
        return task

    async def finalize_protocol(self, room_id: str) -> RoomSessionState:
        if room_id in self._converting_rooms:
            raise RoomBusyError(
                "room_converting",
                "房间正在进行协议迁移，请等待迁移完成后重试。",
            )
        await self.protocol_runtime.cancel(room_id)
        active = self._protocol_tasks.get(room_id)
        if active and not active.done():
            await self.cancel_turn(room_id)
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await active
        for session in self._active_agent_sessions.get(room_id, {}).values():
            cancel_event = getattr(session, "_cancel_event", None)
            if cancel_event is not None and cancel_event.is_set():
                cancel_event.clear()
        state = self.get_room(room_id)
        if state is None:
            raise KeyError(room_id)
        state.status = RoomStatus.DISCUSSING
        self._save_room_state(state, force=True)
        try:
            finalized = await self.protocol_runtime.force_finalize(
                state,
                _coerce_case_state(state.case_state).problem_definition
                or "立即总结当前已完成结果",
                self._execute_protocol_action,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Same contract as run_protocol: a crashed finalize must surface
            # as a retryable room ERROR, never a room stuck in DISCUSSING.
            state.status = RoomStatus.ERROR
            state.last_error = f"room_finalize_failed: {type(exc).__name__}: {exc}"
            self._save_room_state(state, force=True)
            await self._broadcast_event(
                room_id,
                {
                    "event": "room_protocol_error",
                    "room_id": room_id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            raise
        state.protocol_state = finalized
        state.status = (
            RoomStatus.READY
            if finalized.status.value == "success"
            else RoomStatus.ERROR
        )
        state.last_error = None if state.status == RoomStatus.READY else "room_finalize_failed"
        self._save_room_state(state, force=True)
        return state

    async def convert_room_protocol(
        self,
        room_id: str,
        target_protocol: str,
        role_mapping: dict[str, str],
    ) -> RoomSessionState:
        """One-shot free_discussion -> expert_consultation migration.

        Guards fail loudly with coded errors; the rewrite (roles, host,
        protocol, live-run reset) is applied to the in-memory state and
        persisted once at the end. Transcript, case_state, shared context,
        bindings and persona sessions are kept untouched; historical runs
        stay in room_runs / room_protocol_events.
        """
        if room_id in self._converting_rooms:
            raise RoomProtocolConversionError(
                "conversion_in_progress",
                "该房间正在进行协议迁移，请勿重复发起。",
            )
        state = self.get_room(room_id)
        if state is None:
            raise RoomProtocolConversionError("room_not_found", f"Room not found: {room_id}")
        # (b) A live protocol run must never be converted mid-flight.  The
        # _protocol_tasks check covers background runs, the run-lock check
        # covers synchronous run_protocol callers holding the per-room lock.
        active_task = self._protocol_tasks.get(room_id)
        if (active_task is not None and not active_task.done()) or (
            state.protocol_state.status == RoomRunStatus.RUNNING
        ):
            raise RoomProtocolConversionError(
                "protocol_run_active",
                "协议正在运行，请先结束或取消当前运行再进行协议迁移。",
            )
        run_lock = self._protocol_run_locks.get(room_id)
        if run_lock is not None and run_lock.locked():
            raise RoomProtocolConversionError(
                "protocol_run_active",
                "协议正在运行，请先结束或取消当前运行再进行协议迁移。",
            )
        # TOCTOU guard: from here until the rewrite completes, every other
        # mutating entry point rejects via the same set.
        self._converting_rooms.add(room_id)
        try:
            return await self._convert_room_protocol_locked(
                room_id, target_protocol, role_mapping
            )
        finally:
            self._converting_rooms.discard(room_id)

    async def _convert_room_protocol_locked(
        self,
        room_id: str,
        target_protocol: str,
        role_mapping: dict[str, str],
    ) -> RoomSessionState:
        state = self.get_room(room_id)
        if state is None:
            raise RoomProtocolConversionError("room_not_found", f"Room not found: {room_id}")
        # (c) Only lifecycle-active rooms can convert.
        if state.status not in _CONVERTIBLE_ROOM_STATUSES:
            raise RoomProtocolConversionError(
                "room_status_not_convertible",
                f"房间状态 {state.status.value} 不允许迁移协议",
            )
        if state.status in {RoomStatus.CREATING, RoomStatus.INITIALIZING}:
            # Serialize against background initialization, mirroring run_protocol.
            state = await self.wait_for_initialization(room_id)
            if state.status not in {RoomStatus.READY, RoomStatus.DISCUSSING}:
                raise RoomProtocolConversionError(
                    "room_status_not_convertible",
                    f"房间状态 {state.status.value} 不允许迁移协议",
                )
            # Re-check (b) after the await: initialization may have started
            # a protocol run or a synchronous caller may have taken the run
            # lock while we were waiting.
            active_task = self._protocol_tasks.get(room_id)
            if (active_task is not None and not active_task.done()) or (
                state.protocol_state.status == RoomRunStatus.RUNNING
            ):
                raise RoomProtocolConversionError(
                    "protocol_run_active",
                    "协议正在运行，请先结束或取消当前运行再进行协议迁移。",
                )
            run_lock = self._protocol_run_locks.get(room_id)
            if run_lock is not None and run_lock.locked():
                raise RoomProtocolConversionError(
                    "protocol_run_active",
                    "协议正在运行，请先结束或取消当前运行再进行协议迁移。",
                )
        # (d) Only free_discussion -> expert_consultation is supported.
        if (
            state.protocol != RoomProtocolType.FREE_DISCUSSION
            or target_protocol != RoomProtocolType.EXPERT_CONSULTATION.value
        ):
            raise RoomProtocolConversionError(
                "unsupported_protocol_conversion",
                "仅支持 free_discussion -> expert_consultation 的协议迁移",
            )
        # (e) Mapping must cover every enabled participant with exactly one host.
        enabled = [p for p in state.participants if p.enabled]
        known_ids = {p.participant_id for p in state.participants}
        name_by_id = {
            p.participant_id: (p.display_name or p.persona_id or p.participant_id)
            for p in state.participants
        }

        def _preview() -> str:
            return "；".join(
                f"{name_by_id.get(pid, pid)}→{role}" for pid, role in role_mapping.items()
            ) or "（空）"

        unknown = sorted(pid for pid in role_mapping if pid not in known_ids)
        if unknown:
            raise RoomProtocolConversionError(
                "invalid_role_mapping",
                f"role_mapping 包含未知参与者：{', '.join(unknown)}；映射预览：{_preview()}",
            )
        invalid_roles = sorted(
            {str(role) for role in role_mapping.values() if role not in {"host", "expert"}}
        )
        if invalid_roles:
            raise RoomProtocolConversionError(
                "invalid_role_mapping",
                f"role_mapping 仅允许 host/expert，收到：{', '.join(invalid_roles)}；"
                f"映射预览：{_preview()}",
            )
        unmapped = [p.participant_id for p in enabled if p.participant_id not in role_mapping]
        if unmapped:
            raise RoomProtocolConversionError(
                "invalid_role_mapping",
                f"role_mapping 未覆盖启用的参与者：{', '.join(unmapped)}；映射预览：{_preview()}",
            )
        host_ids = [pid for pid, role in role_mapping.items() if role == "host"]
        enabled_host_ids = {p.participant_id for p in enabled} & set(host_ids)
        if len(host_ids) != 1 or not enabled_host_ids:
            raise RoomProtocolConversionError(
                "invalid_role_mapping",
                "role_mapping 必须有且仅有一个启用的 host 映射；映射预览：" + _preview(),
            )
        host_pid = host_ids[0]

        # Transactional rewrite: mutate the mirror first, persist once below.
        for participant in state.participants:
            target_role = role_mapping.get(participant.participant_id)
            if target_role:
                participant.role = target_role
        state.host_participant_id = host_pid
        state.protocol = RoomProtocolType.EXPERT_CONSULTATION
        # Final validation against the target definition (1 host + >=1 expert).
        definition = self.protocol_registry.get(state.protocol, state.protocol_config)
        self.protocol_registry.validate_participants(definition, state.participants)
        # Reset live run state; the old run history stays in the event store.
        previous_run_id = state.protocol_state.run_id
        state.protocol_state = RoomProtocolState()
        state.protocol_events = []
        state.updated_at = datetime.now(UTC)
        audit_run_id = previous_run_id
        if not audit_run_id:
            latest = self.protocol_repository.latest_run(room_id)
            audit_run_id = latest.run_id if latest and latest.run_id else None
        if not audit_run_id:
            # room_protocol_events.run_id has an FK to room_runs; a legacy room
            # without any protocol run needs a terminal anchor row so the
            # conversion audit stays referentially valid.
            audit_run_id = new_id("roomrun")
            now = datetime.now(UTC)
            self.protocol_repository.save_run(
                room_id=room_id,
                protocol=RoomProtocolType.FREE_DISCUSSION,
                state=RoomProtocolState(
                    run_id=audit_run_id,
                    status=RoomRunStatus.CANCELLED,
                    current_stage="waiting_user",
                    started_at=now,
                    finished_at=now,
                    final_result={"event": "protocol_converted"},
                ),
            )
        # Persist the converted state FIRST: if the audit write below fails,
        # an audit event pointing at a conversion that never happened is
        # worse than a successful conversion without its audit row -- and
        # the conversion itself is durable and observable either way.
        self._save_room_state(state, force=True)
        self.protocol_repository.save_event(
            RoomProtocolEvent(
                id=new_id("roomev"),
                room_id=room_id,
                run_id=audit_run_id,
                event_type="protocol_converted",
                stage="waiting_user",
                metadata={
                    "from": RoomProtocolType.FREE_DISCUSSION.value,
                    "to": RoomProtocolType.EXPERT_CONSULTATION.value,
                    "role_mapping": dict(role_mapping),
                },
            )
        )
        await self._broadcast_event(
            room_id,
            {
                "event": "protocol_converted",
                "room_id": room_id,
                "from": RoomProtocolType.FREE_DISCUSSION.value,
                "to": RoomProtocolType.EXPERT_CONSULTATION.value,
            },
        )
        return state

    def _resolve_transport_capability(
        self,
        room_id: str,
        participant_id: str,
        adapter: Any,
        runtime_id: str,
    ) -> PromptTransportCapability:
        """Resolve one participant's prompt transport capability per binding.

        Reflection over the adapter shape is not free, so the result is cached
        (mirroring ``_tool_preflight``) keyed by the bound runtime id: a
        re-binding to a different adapter invalidates the entry implicitly.
        """

        cache = self._transport_capabilities.setdefault(room_id, {})
        cached = cache.get(participant_id)
        if cached is not None and cached[0] == runtime_id:
            return cached[1]
        capability = resolve_prompt_transport_capability(adapter)
        cache[participant_id] = (runtime_id, capability)
        return capability

    async def _execute_protocol_action(
        self, request: ProtocolActionRequest
    ) -> dict[str, Any]:
        state = self.get_room(request.room_id)
        if not state:
            raise KeyError(request.room_id)
        slot = request.participant
        snapshot = state.binding_snapshots.get(slot.participant_id)
        if snapshot is None:
            raise RuntimeError(f"protocol_binding_missing:{slot.participant_id}")
        adapter = self.registry.get_adapter(snapshot.agent_runtime_id)
        agent_session = self._active_agent_sessions.get(state.id, {}).get(slot.participant_id)
        binding = self._active_agent_bindings.get(state.id, {}).get(slot.participant_id)
        if not adapter or not agent_session or not self.runtime_executor:
            raise RuntimeError(f"protocol_runtime_unavailable:{slot.participant_id}")
        if binding is None:
            binding = await self.runtime_executor.bind_existing_session(adapter, agent_session)
            self._active_agent_bindings.setdefault(state.id, {})[slot.participant_id] = binding

        recall_started = {
            "event": "recall_started",
            "room_id": state.id,
            "run_id": request.run_id,
            "task_id": request.task_id,
            "participant_id": slot.participant_id,
            "stage": request.stage,
        }
        await self._broadcast_event(state.id, recall_started)
        gate_plan = self.recall_gate.plan_turn(
            persona_id=slot.persona_id,
            current_speaker_name=slot.display_name or slot.persona_id,
            user_message=request.question,
            recent_transcript=state.transcript,
        )
        memories = (
            self.continuum.memories.search_memories(
                persona_id=slot.persona_id,
                query=gate_plan.query,
                limit=8,
                branch_id="main",
                include_main_history=True,
                include_shared_pre_divergence=True,
            )
            if gate_plan.triggered
            else []
        )
        recall_result = RecallGate.attach_memories(gate_plan, list(memories))
        await self._broadcast_event(
            state.id,
            {
                "event": "recall_completed",
                "room_id": state.id,
                "run_id": request.run_id,
                "task_id": request.task_id,
                "participant_id": slot.participant_id,
                "stage": request.stage,
                "triggered": recall_result.triggered,
                "retrieved_count": len(recall_result.memories),
            },
        )
        session_id = self._persona_session_ids.get(state.id, {}).get(slot.participant_id)
        if not session_id:
            continuum_session = self.continuum.sessions.start_session(
                persona_id=slot.persona_id,
                title=f"Room {state.id} [{slot.participant_id}]",
                counterpart_id=f"room:{state.id}",
                session_type="room_protocol",
                room_id=state.id,
            )
            session_id = continuum_session.id
            self._persona_session_ids.setdefault(state.id, {})[slot.participant_id] = session_id
        prepared = self.continuum.sessions.prepare_turn(
            persona_id=slot.persona_id,
            session_id=session_id,
            user_message=request.question,
            counterpart_id=f"room:{state.id}",
            preset_memories=memories,
        )
        kernel = (
            self.kernel_cache.get_or_build(
                prepared, display_name=slot.display_name or slot.persona_id
            )
            if self.kernel_cache is not None
            else None
        )
        system_prompt, base_user_prompt, _ = self.prompt_composer.compose_turn_prompt(
            slot=slot,
            binding=snapshot,
            prepared=prepared,
            dynamic_recall_memories=recall_result.memories,
            # The dialogue window travels as the packed ``recent_dialogue``
            # section below, where the transport budget may trim it first.
            recent_transcript=None,
            room_topic=state.topic,
            kernel=kernel,
            context_mode="windowed",
        )
        # The protocol path advertises exactly what _execute_tool below will
        # accept: availability intersected with this action's permissions.
        tool_block = await self.tool_broker.compose_tool_block(
            tool_permissions=list(request.tool_permissions or []),
            allow_agent_tools=bool(slot.allow_agent_tools),
        )
        system_prompt = f"{system_prompt}\n\n{tool_block}"
        # Pack the protocol context *before* assembly: sections are trimmed
        # in byte-budget priority order so the finished prompt can never
        # exceed the adapter transport ceiling (an ARGV carrier holds ~64KB
        # and _prepare_turn rejects anything larger outright).
        task_context = dict(request.task_context or {})
        case_state_block = task_context.pop("case_state", None)
        stage_instruction = str(task_context.pop("stage_instruction", "") or "")
        special_task = {
            key: value
            for key, value in task_context.items()
            if key != "independent_first"
        }
        capability = self._resolve_transport_capability(
            state.id, slot.participant_id, adapter, snapshot.agent_runtime_id
        )
        packed_budget = budget_for(capability)
        if (
            request.action == "synthesis"
            and request.protocol == RoomProtocolType.EXPERT_CONSULTATION
        ):
            # Reserve headroom for the self-contained repair retry: the
            # packed user prompt is re-sent with the draft JSON + rewrite
            # instruction appended, and that message must stay inside the
            # same transport budget the first call used.
            packed_budget = max(
                ABSOLUTE_FLOOR_BYTES,
                packed_budget - self._SYNTHESIS_REPAIR_RESERVE_BYTES,
            )
        stored_summary = state.metadata.get("rolling_summary")
        packed = ContextPacker(capability, budget_bytes=packed_budget).pack(
            system=system_prompt,
            current_task={
                "room_role": request.role_context,
                "question": request.question,
                "independent_first": bool(task_context.get("independent_first")),
            },
            case_state=case_state_block,
            stage_instruction=stage_instruction,
            expert_task=special_task or None,
            structured_results=request.peer_results,
            shared_context=request.room_context,
            recent_dialogue=request.conversation_context,
            # Durable rolling summary (may be absent); the packer skips the
            # section when empty.
            room_summary=(
                stored_summary if isinstance(stored_summary, str) and stored_summary else ""
            ),
        )
        packed_payload = packed.payload()
        # The system section (identity kernel + constraints + tools) keeps
        # travelling in its own prompt role; adopt the packed copy so a
        # clipped system prompt is what actually gets dispatched.
        system_prompt = str(packed_payload.pop("system", "") or system_prompt)
        user_prompt = (
            f"{base_user_prompt}\n\nROOM PROTOCOL TASK (public structured context):\n"
            f"{json.dumps(packed_payload, ensure_ascii=False, default=str)}"
        )
        await self._broadcast_event(
            state.id,
            {
                "event": "agent_started",
                "room_id": state.id,
                "run_id": request.run_id,
                "task_id": request.task_id,
                "participant_id": slot.participant_id,
                "stage": request.stage,
                "agent_runtime_id": snapshot.agent_runtime_id,
                "model_id": snapshot.model_id,
            },
        )

        async def _execute_tool(name: str, args: dict[str, Any]) -> Any:
            # Distinct failure modes must stay distinct: "no such tool",
            # "not permitted", "provider down" and "call failed" are different
            # problems and the model needs to be able to tell them apart.
            return await self.tool_broker.execute_tool(
                slot.persona_id,
                name,
                args,
                participant_id=slot.participant_id,
                room_id=state.id,
                tool_permissions=list(request.tool_permissions or []),
                allow_agent_tools=bool(slot.allow_agent_tools),
            )

        protocol_call_started = time.monotonic()
        runtime_executor = self.runtime_executor
        if runtime_executor is None:
            raise RuntimeError(f"protocol_runtime_unavailable:{slot.participant_id}")

        async def _run_structured(
            current_binding: RuntimeSessionBinding,
            user_message: str | None = None,
        ) -> Any:
            return await runtime_executor.execute_structured(
                current_binding,
                system_prompt=system_prompt,
                user_message=user_message if user_message is not None else user_prompt,
                schema=self._protocol_output_schema(request.action, request.protocol),
                phase=f"room_protocol_{request.action}",
                metadata={
                    "room_id": state.id,
                    "run_id": request.run_id,
                    "task_id": request.task_id,
                    "tool_executor": _execute_tool,
                },
                max_repair_attempts=1,
            )

        try:
            result = await _run_structured(binding)
        except AgentRuntimeError as exc:
            # Autonomous turns recover a dead runtime via restart_session;
            # protocol actions must do the same or one transient transport
            # failure fails the whole room run.  Cancellations are never
            # retried so a user cancel cannot be swallowed.  A prompt
            # transport limit is deterministic: restarting cannot shrink the
            # prompt, so re-sending the same payload would loop forever.
            if isinstance(exc, PromptTransportLimitExceededError):
                await self._broadcast_event(
                    state.id,
                    {
                        "event": "room_prompt_transport_rejected",
                        "room_id": state.id,
                        "run_id": request.run_id,
                        "task_id": request.task_id,
                        "participant_id": slot.participant_id,
                        "stage": request.stage,
                        "diagnostics": exc.diagnostics,
                    },
                )
                raise
            if isinstance(exc, AgentCancelledError) or not exc.retriable:
                raise
            restarted = await self.restart_session(state.id, slot.participant_id)
            retry_binding = self._active_agent_bindings.get(state.id, {}).get(
                slot.participant_id
            )
            if not restarted or retry_binding is None:
                raise
            await self._broadcast_event(
                state.id,
                {
                    "event": "room_runtime_recovered",
                    "room_id": state.id,
                    "run_id": request.run_id,
                    "task_id": request.task_id,
                    "participant_id": slot.participant_id,
                    "stage": request.stage,
                    "next_context_mode": "protocol_task_context",
                },
            )
            binding = retry_binding
            result = await _run_structured(retry_binding)
        output = dict(result.value)
        retry_response = None
        if (
            request.action == "synthesis"
            and request.protocol == RoomProtocolType.EXPERT_CONSULTATION
        ):
            # Host synthesis is the user-facing final answer: enforce the
            # density contract once, keep the better draft, never crash.
            # Committee (chair_synthesis) and host_moderated (host_final)
            # reuse the action and are exempt from the dense contract.
            # Stateless repair: the retry call must be self-contained -- the
            # packed user prompt (case state, expert submissions, reviews)
            # plus the current draft plus the density instruction.  The
            # packing pass reserved _SYNTHESIS_REPAIR_RESERVE_BYTES for this
            # retry and the draft is clipped to the remaining headroom, so
            # the repair message stays within the same transport budget the
            # first call used.
            output, retry_response = await self._ensure_synthesis_density(
                output,
                retry=lambda instruction: _run_structured(
                    binding,
                    user_message=self._repair_user_message(user_prompt, output, instruction),
                ),
            )
        usage = getattr(result.response, "usage", {}) if result.response else {}
        if retry_response is not None:
            # Metrics must cover the whole exchange: merge the repair call's
            # token usage onto the first call.  duration_ms below already
            # spans both calls because its clock starts before the first one.
            retry_usage = getattr(retry_response, "usage", None)
            if isinstance(retry_usage, dict) and retry_usage:
                base = usage if isinstance(usage, dict) else {}
                input_tokens = int(base.get("input_tokens") or 0) + int(
                    retry_usage.get("input_tokens") or 0
                )
                output_tokens = int(base.get("output_tokens") or 0) + int(
                    retry_usage.get("output_tokens") or 0
                )
                if input_tokens or output_tokens:
                    usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
        output["_execution"] = {
            "room_id": state.id,
            "run_id": request.run_id,
            "protocol": request.protocol.value,
            "stage": request.stage,
            "participant": slot.participant_id,
            "agent": snapshot.agent_runtime_id,
            "model": snapshot.model_id,
            "task_id": request.task_id,
            "duration_ms": round((time.monotonic() - protocol_call_started) * 1000, 1),
            "input_tokens": int(usage.get("input_tokens") or 0) if isinstance(usage, dict) else 0,
            "output_tokens": int(usage.get("output_tokens") or 0) if isinstance(usage, dict) else 0,
            "status": "success",
            "packed": packed.report(),
        }
        if request.action == "host_analysis":
            # The host schema carries no summary: the public card text comes
            # from the clarification ask, else from the problem definition.
            output.setdefault(
                "content",
                str(
                    output.get("clarification_question")
                    or output.get("problem_definition")
                    or ""
                ),
            )
        # Only analysis/synthesis are persona-facing public answers worth
        # remembering; routing/review/rebuttal/host_analysis/vote live on as
        # protocol state artifacts and never enter persona session memory.
        public_text = str(output.get("summary") or output.get("content") or "")
        if request.action in {"analysis", "synthesis"} and public_text:
            commit = self.continuum.sessions.commit_turn(
                persona_id=slot.persona_id,
                session_id=session_id,
                user_message=request.question,
                persona_response=public_text,
                used_memory_ids=[memory.id for memory in recall_result.memories],
                counterpart_id=f"room:{state.id}",
            )
            output.setdefault("turn_id", commit.get("turn_id"))
        await self._broadcast_event(
            state.id,
            {
                "event": "agent_completed",
                "room_id": state.id,
                "run_id": request.run_id,
                "task_id": request.task_id,
                "participant_id": slot.participant_id,
                "stage": request.stage,
                "usage": usage,
            },
        )
        return output

    @staticmethod
    def _synthesis_density_issues(output: dict[str, Any]) -> list[str]:
        """Quality-gate checks applied to synthesis payloads only."""

        issues: list[str] = []
        summary = str(output.get("summary") or "").strip()
        if not summary:
            issues.append("summary_missing")
        elif len(summary) > 400:
            issues.append("summary_too_long")
        positions = output.get("participant_positions")
        if not isinstance(positions, list) or not positions:
            issues.append("participant_positions_empty")
        if not str(output.get("final_judgment") or "").strip():
            issues.append("final_judgment_empty")
        return issues

    _SYNTHESIS_REPAIR_INSTRUCTION = (
        "上一稿综合结论不达标，请重写并严格遵守以下密度要求："
        "summary 2-4 句、先给结论、总长不超过400字；"
        "participant_positions 只列真正参与的专家，每人仅 1 条核心判断加 1 条关键理由，"
        "不得复述完整回答；consensus 最多 5 条，只保留专家独立得出且一致的观点；"
        "conflicts 最多 4 条，说明谁与谁在何处分歧、原因以及主持人如何处置；"
        "final_judgment 必须明确果断，禁止“各有道理，建议综合考虑”一类的模糊表述；"
        "recommendations 给出 1-4 条可执行建议；"
        "uncertainties 只保留会改变结论的不确定性。"
    )

    #: Packing headroom reserved for the synthesis repair retry message.
    _SYNTHESIS_REPAIR_RESERVE_BYTES = 4 * 1024

    def _repair_user_message(
        self,
        base_prompt: str,
        draft: dict[str, Any],
        instruction: str,
    ) -> str:
        """Assemble the repair retry message within the reserved headroom.

        The packed user prompt was built with _SYNTHESIS_REPAIR_RESERVE_BYTES
        unused; the draft JSON is clipped to what is left after the labels
        and the rewrite instruction so the repair message can never exceed
        the transport budget the first call packed against.
        """

        draft_json = json.dumps(draft, ensure_ascii=False, default=str)
        labels = "\n\nCURRENT SYNTHESIS DRAFT (to rewrite):\n\n\nREWRITE INSTRUCTION:\n"
        allowance = (
            self._SYNTHESIS_REPAIR_RESERVE_BYTES
            - byte_length(instruction)
            - byte_length(labels)
            - 256  # safety margin for scaffolding around the draft block
        )
        if allowance > 0:
            raw = draft_json.encode("utf-8")[:allowance]
            draft_json = raw.decode("utf-8", errors="ignore")
        return (
            f"{base_prompt}\n\nCURRENT SYNTHESIS DRAFT (to rewrite):\n{draft_json}\n\n"
            f"REWRITE INSTRUCTION:\n{instruction}"
        )

    async def _ensure_synthesis_density(
        self,
        output: dict[str, Any],
        retry: Callable[[str], Awaitable[Any]],
    ) -> tuple[dict[str, Any], Any]:
        """Retry a low-density synthesis once; keep the better draft, never crash.

        Returns the adopted payload plus the structured result behind it
        (``None`` when the original draft stays) so the caller can merge
        usage metrics from the repair call.
        """

        if not self._synthesis_density_issues(output):
            return output, None
        try:
            retried = await retry(self._SYNTHESIS_REPAIR_INSTRUCTION)
            retry_output = dict(retried.value)
        except (asyncio.CancelledError, AgentCancelledError):
            # Cancellations are never retried or swallowed: a user cancel
            # must surface exactly like the first-call path does.
            raise
        except Exception:
            # A failed repair must never fail the run: the original draft is
            # still a usable synthesis.
            return output, None
        # An empty retry summary would degrade the frontend to plain text;
        # the original draft always beats an empty repair.
        if not str(retry_output.get("summary") or "").strip():
            return output, None
        if not self._synthesis_density_issues(retry_output):
            return retry_output, retried.response
        if len(self._synthesis_density_issues(retry_output)) < len(
            self._synthesis_density_issues(output)
        ):
            return retry_output, retried.response
        return output, None

    @staticmethod
    def _protocol_output_schema(
        action: str,
        protocol: RoomProtocolType | None = None,
    ) -> dict[str, Any]:
        schemas: dict[str, dict[str, Any]] = {
            "analysis": {
                "type": "object",
                "required": [
                    "summary",
                    "key_findings",
                    "evidence",
                    "uncertainties",
                    "recommendation",
                    "confidence",
                ],
                "properties": {
                    "summary": {"type": "string"},
                    "key_findings": {"type": "array", "items": {"type": "string"}},
                    "evidence": {"type": "array", "items": {}},
                    "uncertainties": {"type": "array", "items": {"type": "string"}},
                    "recommendation": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "domain_data": {"type": "object"},
                },
            },
            "review": {
                "type": "object",
                "required": [
                    "agree",
                    "disagree",
                    "concerns",
                    "changed_position",
                    "updated_conclusion",
                ],
                "properties": {
                    "agree": {"type": "array", "items": {"type": "string"}},
                    "disagree": {"type": "array", "items": {"type": "string"}},
                    "concerns": {"type": "array", "items": {"type": "string"}},
                    "changed_position": {"type": "boolean"},
                    "updated_conclusion": {"type": ["string", "null"]},
                },
            },
            "vote": {
                "type": "object",
                "required": ["vote", "reason", "confidence"],
                "properties": {
                    "vote": {"type": "string"},
                    "reason": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
            "routing": {
                "type": "object",
                "required": ["selected_participant_ids", "routing_reason"],
                "properties": {
                    "selected_participant_ids": {"type": "array", "items": {"type": "string"}},
                    "routing_reason": {"type": "string"},
                },
            },
            # Host synthesis is the run's public final answer.  Only
            # expert_consultation runs reach this dense entry; committee
            # (chair_synthesis) and host_moderated (host_final) synthesis
            # calls are routed to the default shape below instead.
            "synthesis": {
                "type": "object",
                "required": ["summary", "participant_positions", "final_judgment"],
                "properties": {
                    "summary": {"type": "string"},
                    "participant_positions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["participant_id", "position", "key_reason"],
                            "properties": {
                                "participant_id": {"type": "string"},
                                "position": {"type": "string"},
                                "key_reason": {"type": "string"},
                            },
                        },
                    },
                    "consensus": {"type": "array", "items": {"type": "string"}},
                    "conflicts": {"type": "array", "items": {"type": "string"}},
                    "final_judgment": {"type": "string"},
                    "uncertainties": {"type": "array", "items": {"type": "string"}},
                    "recommendations": {"type": "array", "items": {"type": "string"}},
                },
            },
            # Host analysis decides whether the case can proceed at all; the
            # only mandatory fields are the definition and the gate decision.
            "host_analysis": {
                "type": "object",
                "required": ["problem_definition", "can_proceed"],
                "properties": {
                    "problem_definition": {"type": "string"},
                    "known_facts_patch": {"type": "object"},
                    "assumptions": {"type": "array", "items": {"type": "string"}},
                    "can_proceed": {"type": "boolean"},
                    "missing_indispensable_fields": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "clarification_question": {"type": ["string", "null"]},
                },
            },
        }
        default = {
            "type": "object",
            "required": ["summary"],
            "properties": {
                "summary": {"type": "string"},
                "consensus": {"type": "array", "items": {"type": "string"}},
                "conflicts": {"type": "array", "items": {"type": "string"}},
                "uncertainties": {"type": "array", "items": {"type": "string"}},
                "recommendations": {"type": "array", "items": {"type": "string"}},
            },
        }
        if action == "synthesis" and protocol != RoomProtocolType.EXPERT_CONSULTATION:
            # The dense synthesis contract belongs to expert_consultation
            # host synthesis only; every other protocol keeps the permissive
            # default shape and skips the density retry entirely.
            return default
        return schemas.get(action, default)

    async def _host_message_or_fallback(self, state: RoomSessionState, phase: str) -> str:
        try:
            generation = self._generate_host_message(state, phase)
            if HOST_GENERATE_TIMEOUT_SECONDS is not None:
                return await asyncio.wait_for(generation, timeout=HOST_GENERATE_TIMEOUT_SECONDS)
            return await generation
        except Exception as exc:
            fallback = _fallback_host_message(state, phase)
            state.metadata.setdefault("host_generations", []).append(
                {
                    "phase": phase,
                    "decision_source": "fallback",
                    "error": str(exc)[:400],
                }
            )
            self._save_room_state(state)
            return fallback

    async def _generate_host_message(self, state: RoomSessionState, phase: str) -> str:
        host_slot = next(
            (
                slot
                for slot in state.participants
                if slot.participant_id == state.host_participant_id
            ),
            state.participants[0],
        )
        snapshot = state.binding_snapshots[host_slot.participant_id]
        adapter = self.registry.get_adapter(snapshot.agent_runtime_id)
        session = self._host_agent_sessions.get(state.id)
        if not adapter or not session:
            raise RuntimeError("Host Agent session is unavailable")
        content = await self.host_agent.generate(
            adapter=adapter,
            session=session,
            topic=state.topic or "General discussion",
            transcript=state.transcript,
            phase=phase,
        )
        metadata = dict(session.session_data.get("last_completion_metadata") or {})
        usage = metadata.get("usage") or {}
        totals = state.metadata.setdefault(
            "token_usage", {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        )
        if isinstance(totals, dict) and isinstance(usage, dict):
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                totals[key] = int(totals.get(key, 0)) + int(usage.get(key, 0) or 0)
        state.metadata.setdefault("host_generations", []).append(
            {
                "phase": phase,
                "agent_runtime_id": snapshot.agent_runtime_id,
                "model_id": snapshot.model_id,
                "usage": usage,
                "decision_source": "llm",
            }
        )
        self._save_room_state(state)
        return content

    def _record_host_message(self, state: RoomSessionState, content: str, phase: str) -> None:
        entry = {
            "turn_id": new_id("host_message"),
            "participant_id": "host",
            "persona_id": "room_host",
            "speaker_name": "Host",
            "content": content,
            "phase": phase,
            "created_at": datetime.now(UTC).isoformat(),
        }
        state.transcript.append(entry)
        # Host messages live in the append-only event store too, so a room
        # restored from the truncated state snapshot keeps them.
        self._save_transcript_record(
            RoomTranscriptRecord(
                id=new_id("rturn"),
                room_id=state.id,
                turn_id=str(entry["turn_id"]),
                participant_id="host",
                persona_id="room_host",
                speaker_name="Host",
                agent_runtime_id="",
                content=content,
                commit_status="host_message",
                metadata={"phase": phase},
                created_at=datetime.now(UTC),
            )
        )
        state.updated_at = datetime.now(UTC)
        self._save_room_state(state)

    async def inject_message(
        self,
        room_id: str,
        content: str,
        injection_type: str = "external_information",
        client_message_id: str | None = None,
    ) -> dict[str, Any]:
        state = self.get_room(room_id)
        if not state:
            raise KeyError(room_id)
        if state.status not in {RoomStatus.READY, RoomStatus.DISCUSSING, RoomStatus.PAUSED}:
            raise RuntimeError(
                f"Room injection requires ready/discussing/paused status, got {state.status.value}"
            )
        message = content.strip()
        if not message:
            raise ValueError("Injected room message cannot be empty")

        # Transactional semantics: the user message is the unit of work.  A
        # repeated submission with the same client_message_id is a retry and
        # must return the saved message instead of appending a duplicate.
        client_id = (client_message_id or "").strip()
        turn_id = f"user_msg:{client_id}" if client_id else new_id("room_injection")
        if client_id:
            existing_entry = next(
                (entry for entry in state.transcript if entry.get("turn_id") == turn_id),
                None,
            )
            if existing_entry is None:
                row = self.continuum.database.conn.execute(
                    "SELECT participant_id, persona_id, speaker_name, content, "
                    "metadata_json, created_at FROM room_transcripts "
                    "WHERE room_id = ? AND turn_id = ? LIMIT 1",
                    (room_id, turn_id),
                ).fetchone()
                if row:
                    existing_entry = {
                        "turn_id": turn_id,
                        "participant_id": str(row["participant_id"]),
                        "persona_id": str(row["persona_id"]),
                        "speaker_name": str(row["speaker_name"]),
                        "content": str(row["content"]),
                        "injection_type": injection_type,
                        "commit_status": "user_injected",
                        "metadata": dict(loads(str(row["metadata_json"]))),
                        "created_at": str(row["created_at"]),
                    }
            if existing_entry is not None:
                return {
                    "event": "room_message_injected",
                    "room_id": room_id,
                    "message": existing_entry,
                    "duplicate": True,
                }

        entry = {
            "turn_id": turn_id,
            "participant_id": "user",
            "persona_id": "user",
            "speaker_name": "User",
            "content": message,
            "injection_type": injection_type,
            "commit_status": "user_injected",
            "created_at": datetime.now(UTC).isoformat(),
        }
        state.transcript.append(entry)
        state.metadata.setdefault("injections", []).append(entry)
        state.updated_at = datetime.now(UTC)
        # Persist the injection into the event store as well, so history
        # survives the state-snapshot transcript window.
        with contextlib.suppress(Exception):
            self._save_transcript_record(
                RoomTranscriptRecord(
                    id=new_id("rturn"),
                    room_id=room_id,
                    turn_id=turn_id,
                    participant_id="user",
                    persona_id="user",
                    speaker_name="User",
                    agent_runtime_id="",
                    content=message,
                    commit_status="user_injected",
                    metadata={"injection_type": injection_type},
                    created_at=datetime.now(UTC),
                )
            )
        self._save_room_state(state, force=True)
        event = {"event": "room_message_injected", "room_id": room_id, "message": entry}
        await self._broadcast_event(room_id, event)

        # Legacy autonomous discussion is exclusive to free_discussion rooms;
        # protocol rooms advance through RoomProtocolRuntime only.  Injection
        # has already been persisted, so a failure to start a run must never
        # be reported as a failed message save.
        if state.status == RoomStatus.READY:
            if state.protocol == RoomProtocolType.FREE_DISCUSSION:
                if state.mode == RoomMode.AUTONOMOUS:
                    with contextlib.suppress(Exception):
                        self.start_autonomous_discussion(room_id, max_turns=6)
            else:
                with contextlib.suppress(Exception):
                    self.start_protocol_background(room_id, message)

        return event

    async def pause_room(self, room_id: str) -> RoomSessionState:
        state = self.get_room(room_id)
        if not state:
            raise KeyError(room_id)
        state.status = RoomStatus.PAUSED
        state.updated_at = datetime.now(UTC)
        self._save_room_state(state)
        await self._broadcast_event(
            room_id,
            {"event": "room_paused", "room_id": room_id, "status": state.status.value},
        )
        return state

    async def resume_room(self, room_id: str) -> RoomSessionState:
        state = self.get_room(room_id)
        if not state:
            raise KeyError(room_id)

        participant_ids = {slot.participant_id for slot in state.participants if slot.enabled}
        active_session_ids = set(self._active_agent_sessions.get(room_id, {}))
        active_binding_ids = set(self._active_agent_bindings.get(room_id, {}))
        if not participant_ids.issubset(active_session_ids & active_binding_ids):
            # A completed room has released its physical Agent sessions.  Reopen
            # them from the frozen bindings before advertising the room as ready;
            # otherwise the first resumed turn fails after Recall Gate.
            state = await self.resume_from_storage(room_id)
        self._clear_session_cancel_events(room_id)
        state.status = RoomStatus.READY
        state.last_error = None
        state.updated_at = datetime.now(UTC)
        self._save_room_state(state)
        await self._broadcast_event(
            room_id,
            {"event": "room_resumed", "room_id": room_id, "status": state.status.value},
        )
        return state

    async def cancel_turn(self, room_id: str) -> bool:
        state = self.get_room(room_id)
        if state is None:
            return False

        call = state.metadata.get("model_call")
        call_data = call if isinstance(call, dict) else {}
        call_status = str(call_data.get("status") or "")
        autonomous_task = self._autonomous_tasks.get(room_id)
        protocol_task = self._protocol_tasks.get(room_id)
        autonomous_active = bool(autonomous_task and not autonomous_task.done())
        protocol_active = bool(protocol_task and not protocol_task.done())
        protocol_running = state.protocol_state.status == RoomRunStatus.RUNNING
        turn_active = (
            state.status == RoomStatus.DISCUSSING
            or call_status == "calling"
            or autonomous_active
            or protocol_active
            or protocol_running
        )
        # A completed/ready room has no current turn to cancel.  Treating this
        # button as successful used to poison every participant session and
        # replace a valid completed state with ``turn_cancelled``.
        if not turn_active:
            return False

        target_ids: set[str] = set()
        if call_status == "calling" and call_data.get("participant_id"):
            target_ids.add(str(call_data["participant_id"]))
        if protocol_running:
            target_ids.update(state.protocol_state.active_participant_ids)
        if (
            not target_ids
            and not autonomous_active
            and not protocol_active
            and state.current_speaker_id
        ):
            # Direct/manual step during recall has not written model_call yet.
            target_ids.add(state.current_speaker_id)

        sessions = self._active_agent_sessions.get(room_id, {})
        for p_id in target_ids:
            sess = sessions.get(p_id)
            if sess is None:
                continue
            sess.request_cancel()
            snapshot = state.binding_snapshots.get(p_id)
            if snapshot:
                adapter = self.registry.get_adapter(snapshot.agent_runtime_id)
                if adapter:
                    with contextlib.suppress(Exception):
                        await adapter.cancel(sess)

        # Host generation has no participant session.  Cancelling the
        # autonomous task propagates cancellation through the host runtime.
        if autonomous_active and not target_ids and autonomous_task is not None:
            autonomous_task.cancel()

        now = datetime.now(UTC)
        state.metadata["model_call"] = {
            **call_data,
            "status": "cancelled",
            "finished_at": now.isoformat(),
        }
        if state.status in {RoomStatus.DISCUSSING, RoomStatus.INITIALIZING}:
            state.status = RoomStatus.READY
        state.last_error = "turn_cancelled"
        state.updated_at = now
        self._save_room_state(state, force=True)
        await self._broadcast_event(
            room_id,
            {"event": "turn_cancelled", "room_id": room_id, "timestamp": now.isoformat()},
        )
        return True

    def _clear_session_cancel_events(self, room_id: str) -> None:
        """Clear prior-turn cancellation only at an explicit new-run boundary."""

        for session in self._active_agent_sessions.get(room_id, {}).values():
            cancel_event = getattr(session, "_cancel_event", None)
            if cancel_event is not None and cancel_event.is_set():
                cancel_event.clear()

    async def retry_turn(
        self,
        room_id: str,
        alternate_runtime_id: str | None = None,
        alternate_model_id: str | None = None,
        user_message: str = "",
    ) -> AsyncIterator[dict[str, Any]]:
        state = self.get_room(room_id)
        if not state:
            raise KeyError(room_id)
        self._clear_session_cancel_events(room_id)
        state.status = RoomStatus.READY
        state.last_error = None
        self._save_room_state(state)
        speaker_id = state.current_speaker_id or (
            state.participants[0].participant_id if state.participants else None
        )
        if speaker_id and alternate_runtime_id:
            snap = state.binding_snapshots.get(speaker_id)
            if snap:
                snap.agent_runtime_id = alternate_runtime_id
                if alternate_model_id:
                    snap.model_id = alternate_model_id
                state.binding_snapshots[speaker_id] = snap
                self._save_room_state(state)
                # Restart agent session for this slot
                await self.restart_session(room_id, speaker_id)

        async for ev in self.step_turn(
            room_id=room_id,
            manual_speaker_id=speaker_id if state.mode == RoomMode.MANUAL else None,
            user_message=user_message,
        ):
            yield ev

    async def skip_turn(
        self,
        room_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        state = self.get_room(room_id)
        if not state:
            raise KeyError(room_id)
        skipped_speaker = state.current_speaker_id
        state.current_speaker_id = None
        state.last_error = None
        state.updated_at = datetime.now(UTC)
        self._save_room_state(state)
        skip_ev = {
            "event": "turn_skipped",
            "room_id": room_id,
            "skipped_speaker_id": skipped_speaker,
            "reason": reason or "Turn skipped by operator",
            "timestamp": datetime.now(UTC).isoformat(),
        }
        await self._broadcast_event(room_id, skip_ev)
        return skip_ev

    async def restart_session(
        self,
        room_id: str,
        participant_id: str,
    ) -> bool:
        state = self.get_room(room_id)
        if not state:
            raise KeyError(room_id)
        # 1. Close existing agent session
        self._transport_capabilities.get(room_id, {}).pop(participant_id, None)
        agent_sess = self._active_agent_sessions.get(room_id, {}).pop(participant_id, None)
        if agent_sess:
            snapshot = state.binding_snapshots.get(participant_id)
            if snapshot:
                adapter = self.registry.get_adapter(snapshot.agent_runtime_id)
                if adapter:
                    with contextlib.suppress(Exception):
                        binding = self._active_agent_bindings.get(room_id, {}).pop(
                            participant_id, None
                        )
                        if binding and self.runtime_executor:
                            await self.runtime_executor.close(binding)
                        else:
                            await adapter.close(agent_sess)
        # 2. Reset persona session id
        self._persona_session_ids.get(room_id, {}).pop(participant_id, None)
        # The native thread was destroyed; next turn must rehydrate context.
        self.context_manager.reset_cursor(room_id, participant_id)
        # 3. Create fresh agent session
        slot = next((p for p in state.participants if p.participant_id == participant_id), None)
        snapshot = state.binding_snapshots.get(participant_id)
        if slot and snapshot:
            adapter = self.registry.get_adapter(snapshot.agent_runtime_id)
            if adapter:
                session_cfg = AgentSessionConfig(
                    session_id=new_id("asess"),
                    room_id=room_id,
                    participant_id=slot.participant_id,
                    persona_id=slot.persona_id,
                    model_id=snapshot.model_id,
                    reasoning_effort=snapshot.reasoning_effort,
                    auth_profile_id=snapshot.auth_profile_id,
                    permission_profile=slot.permission_profile,  # type: ignore[arg-type]
                    allow_mcp=slot.allow_mcp,
                    tools=await self._tools_for_slot(slot),
                )
                if not self.runtime_executor:
                    return False
                binding = await self.runtime_executor.open_session(adapter, session_cfg)
                new_sess = binding.session
                self._active_agent_sessions.setdefault(room_id, {})[participant_id] = new_sess
                self._active_agent_bindings.setdefault(room_id, {})[participant_id] = binding
        return True

    async def stop_room(self, room_id: str) -> RoomSessionState:
        state = self.get_room(room_id)
        if not state:
            raise KeyError(room_id)
        state.status = RoomStatus.COMPLETED
        state.last_error = None
        state.updated_at = datetime.now(UTC)
        call = state.metadata.get("model_call")
        if isinstance(call, dict) and str(call.get("status") or "") in {
            "calling",
            "cancelled",
            "interrupted",
        }:
            state.metadata["model_call"] = {
                **call,
                "status": "completed",
                "finished_at": state.updated_at.isoformat(),
            }
        self._clear_session_cancel_events(room_id)

        autonomous_task = self._autonomous_tasks.pop(room_id, None)
        if autonomous_task and autonomous_task is not asyncio.current_task():
            autonomous_task.cancel()

        warmup_task = self._tool_warmup_tasks.pop(room_id, None)
        if warmup_task and not warmup_task.done():
            warmup_task.cancel()
        self._tool_preflight.pop(room_id, None)
        self._transport_capabilities.pop(room_id, None)

        # Close all active sessions
        sessions = self._active_agent_sessions.pop(room_id, {})
        for p_id, sess in sessions.items():
            snapshot = state.binding_snapshots.get(p_id)
            if snapshot:
                adapter = self.registry.get_adapter(snapshot.agent_runtime_id)
                if adapter:
                    with contextlib.suppress(Exception):
                        binding = self._active_agent_bindings.get(room_id, {}).get(p_id)
                        if binding and self.runtime_executor:
                            await self.runtime_executor.close(binding)
                        else:
                            await adapter.close(sess)
        self._active_agent_bindings.pop(room_id, None)
        self.context_manager.clear_room(room_id)
        host_session = self._host_agent_sessions.pop(room_id, None)
        if host_session:
            host_slot = next(
                (
                    slot
                    for slot in state.participants
                    if slot.participant_id == state.host_participant_id
                ),
                state.participants[0] if state.participants else None,
            )
            if host_slot:
                snapshot = state.binding_snapshots.get(host_slot.participant_id)
                adapter = self.registry.get_adapter(snapshot.agent_runtime_id) if snapshot else None
                if adapter:
                    with contextlib.suppress(Exception):
                        host_binding = self._host_agent_bindings.pop(room_id, None)
                        if host_binding and self.runtime_executor:
                            await self.runtime_executor.close(host_binding)
                        else:
                            await adapter.close(host_session)

        self._save_room_state(state)
        await self._broadcast_event(
            room_id,
            {"event": "room_stopped", "room_id": room_id, "status": state.status.value},
        )
        return state

    async def resume_from_storage(self, room_id: str) -> RoomSessionState:
        """Reconstruct native agent sessions and continue a room after app restart."""
        state = self.get_room(room_id)
        if not state:
            raise KeyError(room_id)

        # Rebuild full history: persisted state holds a recent window; the
        # room_transcripts event store is the authority for the rest.
        rehydrate_data = state.model_dump(mode="json")
        self._rehydrate_transcript(rehydrate_data)
        if len(rehydrate_data.get("transcript") or []) != len(state.transcript):
            state = RoomSessionState.model_validate(rehydrate_data)

        # Re-initialize sessions if needed
        self._active_agent_sessions[room_id] = {}
        self._active_agent_bindings[room_id] = {}
        self._persona_session_ids[room_id] = {}
        # Fresh native threads after a restart carry no history: every
        # participant rehydrates from kernel + summary + recent window.
        self.context_manager.clear_room(room_id)

        for slot in state.participants:
            snapshot = state.binding_snapshots.get(slot.participant_id)
            if snapshot:
                adapter = self.registry.get_adapter(snapshot.agent_runtime_id)
                if adapter:
                    native_session_id = f"{room_id}_{slot.participant_id}"
                    session_cfg = AgentSessionConfig(
                        session_id=native_session_id,
                        room_id=room_id,
                        participant_id=slot.participant_id,
                        persona_id=slot.persona_id,
                        model_id=snapshot.model_id,
                        reasoning_effort=snapshot.reasoning_effort,
                        auth_profile_id=snapshot.auth_profile_id,
                        permission_profile=slot.permission_profile,  # type: ignore[arg-type]
                        allow_mcp=slot.allow_mcp,
                        tools=await self._tools_for_slot(slot),
                    )
                    if self.runtime_executor:
                        binding = await self.runtime_executor.open_session(adapter, session_cfg)
                        self._active_agent_sessions[room_id][slot.participant_id] = binding.session
                        self._active_agent_bindings[room_id][slot.participant_id] = binding

            # Resume or locate persona session
            cont_sessions = self.continuum.sessions.list_sessions(slot.persona_id)
            room_sess = next(
                (s for s in cont_sessions if s.metadata.get("room_id") == room_id),
                None,
            )
            if room_sess:
                self._persona_session_ids[room_id][slot.participant_id] = room_sess.id
            else:
                new_sess = self.continuum.sessions.start_session(
                    persona_id=slot.persona_id,
                    title=f"Room {room_id} [{slot.participant_id}]",
                    counterpart_id=f"room:{room_id}",
                    session_type="multi_agent_room",
                    room_id=room_id,
                )
                self._persona_session_ids[room_id][slot.participant_id] = new_sess.id

        host_slot = next(
            (
                slot
                for slot in state.participants
                if slot.participant_id == state.host_participant_id
            ),
            state.participants[0] if state.participants else None,
        )
        if host_slot is not None:
            host_snapshot = state.binding_snapshots.get(host_slot.participant_id)
            host_adapter = (
                self.registry.get_adapter(host_snapshot.agent_runtime_id)
                if host_snapshot
                else None
            )
            if host_snapshot and host_adapter and self.runtime_executor:
                host_binding = await self.runtime_executor.open_session(
                    host_adapter,
                    AgentSessionConfig(
                        session_id=f"{room_id}_host",
                        room_id=room_id,
                        participant_id="host",
                        persona_id="room_host",
                        model_id=host_snapshot.model_id,
                        reasoning_effort=host_snapshot.reasoning_effort,
                        permission_profile=PermissionProfile.CHAT_SAFE,
                        allow_mcp=False,
                        tools=[],
                    ),
                )
                self._host_agent_sessions[room_id] = host_binding.session
                self._host_agent_bindings[room_id] = host_binding

        state.status = RoomStatus.READY
        self._save_room_state(state)
        return state

    def get_room(self, room_id: str) -> RoomSessionState | None:
        # In-process rooms read their authoritative mirror; the database row
        # receives debounced snapshots (forced at every transaction point).
        # This removes repeated full-transcript JSON commits from the per-turn
        # latency path without weakening durability guarantees at commits.
        cached = self._room_state_cache.get(room_id)
        if cached is not None:
            try:
                return cached.model_copy(deep=True)
            except Exception:
                pass
        row = self.continuum.database.conn.execute(
            "SELECT state_json FROM rooms WHERE id = ?", (room_id,)
        ).fetchone()
        if not row:
            return None
        data = loads(row["state_json"])
        # The persisted snapshot keeps only a recent transcript window; the
        # event store rebuilds the full history before the state is used.
        self._rehydrate_transcript(data)
        return RoomSessionState.model_validate(data)

    # PATCH must never touch these: swapping the protocol without the
    # runtime reset, final participant validation and protocol_converted
    # audit of convert_room_protocol breaks the migration contract.  Any
    # change goes through POST /api/rooms/{id}/protocol/convert instead.
    _NON_PATCHABLE_ROOM_FIELDS: frozenset[str] = frozenset(
        {"protocol", "protocol_config"}
    )

    def update_room(self, room_id: str, patch: dict[str, Any]) -> RoomSessionState:
        rejected = sorted(set(patch) & self._NON_PATCHABLE_ROOM_FIELDS)
        if rejected:
            raise RoomProtocolConversionError(
                "protocol_change_requires_convert",
                "协议变更必须走 POST /api/rooms/{id}/protocol/convert；"
                f"PATCH 不允许修改字段：{', '.join(rejected)}",
            )
        state = self.get_room(room_id)
        if state is None:
            raise KeyError(room_id)
        if state.protocol_state.status.value == "running":
            raise ValueError("cannot_change_room_definition_during_run")
        data = state.model_dump(mode="python")
        mutable = {
            "title",
            "description",
            "topic",
            "mode",
            "shared_context",
            "participants",
            "host_participant_id",
        }
        for key in mutable:
            if key in patch:
                data[key] = patch[key]
        updated = RoomSessionState.model_validate(data)
        self._validate_persona_bindings(updated.participants)
        definition = self.protocol_registry.get(updated.protocol, updated.protocol_config)
        self.protocol_registry.validate_participants(definition, updated.participants)
        updated.updated_at = datetime.now(UTC)
        self._save_room_state(updated, force=True)
        return updated

    def list_rooms(self) -> list[RoomSessionState]:
        rows = self.continuum.database.conn.execute(
            "SELECT id, state_json FROM rooms ORDER BY created_at DESC"
        ).fetchall()
        rooms: list[RoomSessionState] = []
        for r in rows:
            with contextlib.suppress(Exception):
                cached = self._room_state_cache.get(str(r["id"]))
                rooms.append(
                    cached.model_copy(deep=True)
                    if cached is not None
                    else RoomSessionState.model_validate(loads(r["state_json"]))
                )
        return rooms

    def delete_room(self, room_id: str) -> bool:
        self.continuum.database.conn.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
        self.continuum.database.conn.execute(
            "DELETE FROM room_transcripts WHERE room_id = ?", (room_id,)
        )
        self.continuum.database.conn.commit()
        self._active_agent_sessions.pop(room_id, None)
        self._active_agent_bindings.pop(room_id, None)
        self._persona_session_ids.pop(room_id, None)
        self._room_state_cache.pop(room_id, None)
        self._room_state_last_commit.pop(room_id, None)
        self._room_state_dirty.discard(room_id)
        self.context_manager.clear_room(room_id)
        protocol_task = self._protocol_tasks.pop(room_id, None)
        if protocol_task and not protocol_task.done():
            protocol_task.cancel()
        self._converting_rooms.discard(room_id)
        self._protocol_run_locks.pop(room_id, None)
        autonomous_task = self._autonomous_tasks.pop(room_id, None)
        if autonomous_task:
            autonomous_task.cancel()
        summary_task = self._summary_tasks.pop(room_id, None)
        if summary_task and not summary_task.done():
            summary_task.cancel()
        warmup_task = self._tool_warmup_tasks.pop(room_id, None)
        if warmup_task and not warmup_task.done():
            warmup_task.cancel()
        self._tool_preflight.pop(room_id, None)
        self._transport_capabilities.pop(room_id, None)
        return True

    async def shutdown(self) -> None:
        """Release every provider process this orchestrator started.

        Tool providers own child processes and stdio pipes; without this they
        would outlive the application.
        """

        for task in list(self._tool_warmup_tasks.values()):
            if not task.done():
                task.cancel()
        self._tool_warmup_tasks.clear()
        self._tool_preflight.clear()
        await self.tool_broker.shutdown()

    def shutdown_sync(self) -> None:
        """Best-effort teardown when no event loop is running."""

        for task in list(self._tool_warmup_tasks.values()):
            if not task.done():
                task.cancel()
        self._tool_warmup_tasks.clear()
        self._tool_preflight.clear()
        self.tool_broker.shutdown_sync()

    def list_room_transcripts(self, room_id: str) -> list[RoomTranscriptRecord]:
        rows = self.continuum.database.conn.execute(
            "SELECT * FROM room_transcripts WHERE room_id = ? ORDER BY created_at ASC",
            (room_id,),
        ).fetchall()
        return [
            RoomTranscriptRecord(
                id=str(r["id"]),
                room_id=str(r["room_id"]),
                turn_id=str(r["turn_id"]),
                participant_id=str(r["participant_id"]),
                persona_id=str(r["persona_id"]),
                speaker_name=str(r["speaker_name"]),
                agent_runtime_id=str(r["agent_runtime_id"]),
                agent_session_id=r["agent_session_id"],
                model_id=r["model_id"],
                reasoning_effort=r["reasoning_effort"],
                content=str(r["content"]),
                director_reason=r["director_reason"],
                recall_ids=list(loads(r["recall_ids_json"])),
                commit_status=str(r["commit_status"]),
                metadata=dict(loads(r["metadata_json"])),
                created_at=parse_dt(r["created_at"]) or datetime.now(UTC),
            )
            for r in rows
        ]

    async def clear_room_transcripts(self, room_id: str) -> int:
        """Delete every transcript row for ``room_id`` and reset the in-memory
        window.  Persona state, memories, binding snapshots and turn counters
        are intentionally untouched -- only the visible conversation is wiped,
        so a host that wants to start a new question in the same room can do
        so without losing accumulated Affect / Need / Relationship updates.

        Returns the number of rows that were removed so the API can echo a
        useful confirmation back to the client.
        """

        conn = self.continuum.database.conn
        cursor = conn.execute(
            "DELETE FROM room_transcripts WHERE room_id = ?", (room_id,)
        )
        removed = int(cursor.rowcount or 0)
        state = self.get_room(room_id)
        if state is not None:
            state.transcript = []
            self._save_room_state(state, force=True)
        conn.commit()
        if removed or state is not None:
            # Broadcast only when something actually changed -- a no-op clear
            # must not race a connected client into re-rendering on its own.
            await self._broadcast_event(
                room_id,
                {
                    "event": "transcript_cleared",
                    "room_id": room_id,
                    "removed": removed,
                },
            )
        return removed

    def _participant_is_persistent(
        self,
        *,
        adapter: Any,
        session: Any,
        snapshot: Any,
    ) -> bool:
        """Whether the participant's runtime genuinely remembers turns.

        Capability flags declare the intent; runtime mode proves it.  A Codex
        session that fell back to ``codex exec`` per turn has no thread memory
        despite the adapter advertising persistence, so those sessions use the
        stateless layered context instead of a transcript cursor.
        """

        capabilities = getattr(snapshot, "capabilities", None) or {}
        if not bool(capabilities.get("persistent_session", False)):
            return False
        if adapter is None or session is None:
            return False
        hook = getattr(adapter, "supports_persistent_conversation", None)
        if callable(hook):
            with contextlib.suppress(Exception):
                result = hook(session)
                # A coroutine hook would signal misuse; fall back to safe mode.
                if not hasattr(result, "__await__"):
                    return bool(result)
            return False
        mode = str(session.session_data.get("mode") or "")
        return mode not in {"cli_exec_fallback", "exec", "oneshot"}

    def _schedule_room_summary(self, state: RoomSessionState, *, persistent: bool) -> None:
        """Queue a background rolling-summary refresh (never blocks a turn).

        Persistent-thread rooms already own exact history in their threads and
        never need this model call.  At most one refresh per room is in
        flight; a next turn that starts before the refresh lands simply keeps
        using the previous summary plus its recent raw turns.
        """
        if persistent:
            return
        if not self.context_manager.should_update_summary(state.turn_index):
            return
        existing = self._summary_tasks.get(state.id)
        if existing is not None and not existing.done():
            return
        self._summary_tasks[state.id] = asyncio.create_task(
            self._maybe_update_room_summary(state),
            name=f"room-summary-{state.id}",
        )

    async def _maybe_update_room_summary(
        self, state: RoomSessionState, *, persistent: bool = False
    ) -> None:
        """Regenerate the durable rolling summary for stateless rooms.

        Persistent-thread rooms already own exact history in their threads, so
        they never need this extra model call.  Stateless participants get a
        fidelity-preserving long-term summary (facts, relationships, promises,
        conflicts, positions, events, open topics, goals, user info) refreshed
        every ``summary_every_turns`` raw turns.
        """

        if persistent:
            return
        if not self.context_manager.should_update_summary(state.turn_index):
            return
        host_session = self._host_agent_sessions.get(state.id)
        host_binding = self._host_agent_bindings.get(state.id)
        if host_session is None or host_binding is None or self.runtime_executor is None:
            return
        from persona_continuum.room.context_manager import SUMMARY_PROMPT_INTRO

        executor = self.runtime_executor
        if executor is None:
            return

        async def _summarize(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
            result = await executor.execute_structured(
                host_binding,
                system_prompt=SUMMARY_PROMPT_INTRO,
                user_message=json.dumps(payload, ensure_ascii=False),
                schema=schema,
                phase="room_summary_refresh",
            )
            return result.value

        try:
            previous = state.metadata.get("rolling_summary")
            summary = await self.context_manager.update_summary(
                transcript=list(state.transcript or []),
                previous_summary=previous if isinstance(previous, str) else None,
                summarize=_summarize,
            )
        except Exception:
            return
        if summary:
            state.metadata["rolling_summary"] = summary
            self._save_room_state(state, force=True)

    def subscribe_events(self, room_id: str) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for event in self._event_replay.get(room_id, []):
            q.put_nowait(event)
        self._subscribers.setdefault(room_id, set()).add(q)
        return q

    async def _record_protocol_public_message(self, payload: dict[str, Any]) -> None:
        """Persist one protocol public message into the room transcript.

        The room object is the live run-state copy owned by ``run_protocol``:
        appending here keeps GET /room, WebSocket replay and the final state
        save consistent.  Idempotency key is run_id + task_id + message_kind,
        so WebSocket reconnects or repeated events never duplicate a message.
        """
        room = payload.pop("_room", None)
        room_id = str(payload.get("room_id") or "")
        run_id = str(payload.get("run_id") or "")
        task_id = str(payload.get("task_id") or "")
        message_kind = str(payload.get("message_kind") or "")
        content = str(payload.get("content") or "").strip()
        if not room_id or not run_id or not message_kind or not content:
            return
        if room is None:
            room = self.get_room(room_id)
        if room is None:
            return
        turn_id = f"protocol:{run_id}:{task_id}:{message_kind}"
        if any(entry.get("turn_id") == turn_id for entry in room.transcript):
            return
        row = self.continuum.database.conn.execute(
            "SELECT 1 FROM room_transcripts WHERE room_id = ? AND turn_id = ? LIMIT 1",
            (room_id, turn_id),
        ).fetchone()
        if row:
            return
        # The synthesis answer is the run's final answer.  Terminal finalize
        # may render a shorter summary than synthesis, so content equality is
        # not a valid dedup key: once this run has a synthesis message, never
        # append a second protocol_final card.  Debate's finalize stage also
        # records protocol_final under its stage task_id before run() repeats
        # it under the root task_id, so ANY persisted protocol_final for this
        # run suppresses later duplicates as well.
        if message_kind == "protocol_final":
            already_final = any(
                entry.get("commit_status") == "protocol_public"
                and entry.get("metadata", {}).get("run_id") == run_id
                and entry.get("metadata", {}).get("message_kind")
                in {"protocol_synthesis", "protocol_final"}
                for entry in room.transcript
            )
            if already_final:
                return
        now = datetime.now(UTC)
        metadata = {
            "run_id": run_id,
            "task_id": task_id,
            "stage": payload.get("stage"),
            "action": payload.get("action"),
            "message_kind": message_kind,
            "protocol": room.protocol.value,
        }
        # Synthesis/finalize cards carry the run's structured final answer so
        # the frontend can render it straight from the transcript metadata.
        if "public_payload" in payload:
            metadata["public_payload"] = payload.get("public_payload")
        participant_id = str(payload.get("participant_id") or "")
        persona_id = str(payload.get("persona_id") or "")
        speaker_name = str(payload.get("speaker_name") or "")
        entry = {
            "turn_id": turn_id,
            "participant_id": participant_id,
            "persona_id": persona_id,
            "speaker_name": speaker_name,
            "content": content,
            "commit_status": "protocol_public",
            "metadata": metadata,
            "created_at": now.isoformat(),
        }
        room.transcript.append(entry)
        room.updated_at = now
        # The append-only transcript store keeps protocol messages durable
        # even when the state snapshot truncates its transcript window.
        with contextlib.suppress(Exception):
            self._save_transcript_record(
                RoomTranscriptRecord(
                    id=new_id("rturn"),
                    room_id=room_id,
                    turn_id=turn_id,
                    participant_id=participant_id,
                    persona_id=persona_id,
                    speaker_name=speaker_name,
                    agent_runtime_id="",
                    content=content,
                    commit_status="protocol_public",
                    metadata=metadata,
                    created_at=now,
                )
            )
        self._save_room_state(room, force=True)
        await self._broadcast_event(
            room_id,
            {"event": "protocol_message", "room_id": room_id, "message": entry},
        )

    async def _emit_protocol_event(self, event: dict[str, Any]) -> None:
        room_id = str(event.get("room_id") or "")
        if room_id:
            state = self.get_room(room_id)
            if state:
                latest = self.protocol_repository.latest_run(room_id)
                if latest:
                    state.protocol_state = latest
                    self._save_room_state(state)
            await self._broadcast_event(room_id, event)

    def unsubscribe_events(self, room_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        subs = self._subscribers.get(room_id)
        if subs:
            subs.discard(q)

    async def _initialization_progress(
        self, state: RoomSessionState, stage: str, progress: int
    ) -> None:
        state.status = RoomStatus.READY if progress >= 100 else RoomStatus.INITIALIZING
        state.initialization_stage = stage
        state.initialization_progress = progress
        state.updated_at = datetime.now(UTC)
        progress_events = state.metadata.setdefault("progress_events", [])
        if isinstance(progress_events, list):
            progress_events.append(
                {
                    "event": "room_initialization_progress",
                    "room_id": state.id,
                    "stage": stage,
                    "progress": progress,
                }
            )
        self._save_room_state(state, force=progress >= 100)
        await self._broadcast_event(
            state.id,
            {
                "event": "room_initialization_progress",
                "room_id": state.id,
                "stage": stage,
                "progress": progress,
            },
        )

    async def _broadcast_event(self, room_id: str, event: dict[str, Any]) -> None:
        event_name = str(event.get("event") or "")
        if event_name not in {"agent_message_delta", "agent_thinking"}:
            buf = self._event_replay.setdefault(room_id, [])
            buf.append(event)
            overflow = len(buf) - 40
            if overflow > 0:
                del buf[:overflow]
        subs = list(self._subscribers.get(room_id, set()))
        for q in subs:
            with contextlib.suppress(Exception):
                q.put_nowait(event)

    def _save_room_state(self, state: RoomSessionState, *, force: bool = False) -> None:
        # Always keep the in-process mirror authoritative for reads.
        try:
            self._room_state_cache[state.id] = state.model_copy(deep=True)
        except Exception:
            self._room_state_cache[state.id] = state
        try:
            now_monotonic = asyncio.get_running_loop().time()
        except RuntimeError:
            now_monotonic = 0.0
            self._persist_room_state(state)
            return
        last = self._room_state_last_commit.get(state.id, 0.0)
        if (
            not force
            and self._debounce_seconds > 0
            and now_monotonic - last < self._debounce_seconds
        ):
            self._room_state_dirty.add(state.id)
            return
        self._persist_room_state(state)
        self._room_state_last_commit[state.id] = now_monotonic
        self._room_state_dirty.discard(state.id)

    def flush_room_state(self, room_id: str) -> None:
        """Force a durable write for one room (terminal transitions)."""

        if room_id not in self._room_state_dirty:
            # Still write the authoritative copy so callers can rely on it.
            cached = self._room_state_cache.get(room_id)
            if cached is not None:
                self._persist_room_state(cached)
            return
        cached = self._room_state_cache.get(room_id)
        if cached is not None:
            self._persist_room_state(cached)
        self._room_state_dirty.discard(room_id)
        with contextlib.suppress(RuntimeError):
            self._room_state_last_commit[room_id] = asyncio.get_running_loop().time()

    def _persist_room_state(self, state: RoomSessionState) -> None:
        persona_ids = [p.persona_id for p in state.participants]
        state_dict = state.model_dump(mode="json")
        # The append-only room_transcripts table is the authority for full
        # history; the state snapshot carries only the recent transcript
        # window (plus a truncation marker) so persistence cost per turn no
        # longer scales with the total conversation length.  In-memory state
        # keeps the full transcript for prompt building.
        transcript = state_dict.get("transcript")
        if isinstance(transcript, list) and len(transcript) > self._persisted_transcript_window:
            metadata = state_dict.setdefault("metadata", {})
            if isinstance(metadata, dict):
                metadata["transcript_total_entries"] = len(transcript)
                metadata["transcript_truncated_in_state"] = True
            state_dict["transcript"] = transcript[-self._persisted_transcript_window :]
        now_str = datetime.now(UTC).isoformat()
        self.continuum.database.conn.execute(
            """
            INSERT INTO rooms (
                id, status, persona_ids_json, topic, state_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                persona_ids_json = excluded.persona_ids_json,
                topic = excluded.topic,
                state_json = excluded.state_json,
                updated_at = excluded.updated_at
            """,
            (
                state.id,
                state.status.value,
                dumps(persona_ids),
                state.topic,
                dumps(state_dict),
                state.created_at.isoformat(),
                now_str,
            ),
        )
        self.continuum.database.conn.commit()

    def _rehydrate_transcript(self, data: dict[str, Any]) -> None:
        """Rebuild a full transcript from the event store after a restart.

        Turn records come from ``room_transcripts``; state-only entries that
        were kept in the recent window (host messages, user injections) are
        merged back in chronological order, so no history is lost by the
        state-snapshot truncation.
        """
        metadata = data.get("metadata")
        if not isinstance(metadata, dict) or not metadata.get("transcript_truncated_in_state"):
            return
        room_id = str(data.get("id") or "")
        records = self.list_room_transcripts(room_id) if room_id else []
        if not records:
            return
        retained = [entry for entry in data.get("transcript") or [] if isinstance(entry, dict)]
        known_turn_ids = {
            str(t.turn_id)
            for t in records
        }
        state_only = [
            entry for entry in retained if str(entry.get("turn_id") or "") not in known_turn_ids
        ]
        rebuilt: list[dict[str, Any]] = [
            {
                "turn_id": t.turn_id,
                "participant_id": t.participant_id,
                "persona_id": t.persona_id,
                "speaker_name": t.speaker_name,
                "agent_runtime_id": t.agent_runtime_id,
                "model_id": t.model_id,
                "reasoning_effort": t.reasoning_effort,
                "content": t.content,
                "director_reason": t.director_reason,
                "recall_ids": t.recall_ids,
                "commit_status": t.commit_status,
                "metadata": t.metadata,
                "created_at": t.created_at.isoformat(),
            }
            for t in records
        ]
        merged = sorted(
            [*rebuilt, *state_only],
            key=lambda entry: str(entry.get("created_at") or ""),
        )
        data["transcript"] = merged

    def _save_transcript_record(self, record: RoomTranscriptRecord) -> None:
        self.continuum.database.conn.execute(
            """
            INSERT INTO room_transcripts (
                id, room_id, turn_id, participant_id, persona_id, speaker_name,
                agent_runtime_id, agent_session_id, model_id, reasoning_effort,
                content, director_reason, recall_ids_json, commit_status,
                metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.room_id,
                record.turn_id,
                record.participant_id,
                record.persona_id,
                record.speaker_name,
                record.agent_runtime_id,
                record.agent_session_id,
                record.model_id,
                record.reasoning_effort,
                record.content,
                record.director_reason,
                dumps(record.recall_ids),
                record.commit_status,
                dumps(record.metadata),
                record.created_at.isoformat(),
            ),
        )
        self.continuum.database.conn.commit()
