from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
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
    AgentTransportError,
    PromptTransportLimitExceededError,
    compact_tool_result,
)
from persona_continuum.agent.runtime_executor import RuntimeSessionBinding
from persona_continuum.application._utils import dumps, loads, new_id, parse_dt
from persona_continuum.auth.profiles import AuthProfileService, redact_secrets
from persona_continuum.domain.episode import EpisodeStatus
from persona_continuum.domain.scene import RoomSceneState, SceneEvent, SceneOutput
from persona_continuum.room.attachments import (
    attachment_public_url,
    decode_base64_payload,
    store_room_attachment,
)
from persona_continuum.room.case_state import RoomCaseState, merge_case_state
from persona_continuum.runtime.scene_runtime import SceneRuntime
from persona_continuum.runtime.temporal_parser import parse_temporal_input
from persona_continuum.runtime.turn_normalizer import normalize_turn

if TYPE_CHECKING:
    from persona_continuum.application.container import PersonaContinuum
from persona_continuum.domain.memory_bundle import MemoryBundle, RetrievalMode
from persona_continuum.room.context_manager import (
    CANONICAL_SUMMARY_VERSION,
    RoomContextManager,
    is_canonical_summary,
)
from persona_continuum.room.context_packer import (
    ABSOLUTE_FLOOR_BYTES,
    ContextPacker,
    budget_for,
    byte_length,
)
from persona_continuum.room.context_policy import (
    ContextPolicyRequest,
    ContextProfile,
    ContextStrategy,
    ResolvedContextPolicy,
    local_profile_from_config,
    normalize_strategy,
    resolve_context_policy,
)
from persona_continuum.room.director import SpeakerDirector
from persona_continuum.room.discussion_director import DiscussionDirector
from persona_continuum.room.host_agent import HostAgent
from persona_continuum.room.models import (
    BindingPreflightError,
    DirectorConfig,
    ParticipantSlot,
    ProtocolTaskStatus,
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
from persona_continuum.room.prompt_composer import PromptComposer, _clip_to_tokens
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
    """Coarse token estimate used for prompt-size trend metrics.

    CJK is counted per character and everything else at ~4 chars/token: the old
    flat ``len // 4`` under-counted Chinese persona prompts by roughly 4x, which
    made the context report useless for exactly the rooms that matter here.
    """

    if not text:
        return 0
    cjk = len(re.findall(r"[\u3400-\u9fff\uf900-\ufaff]", text))
    return cjk + max(0, len(text) - cjk) // 4


#: Room-metadata keys owned by *asynchronous* writers (the rolling-summary
#: refresh, and the one-off scene/style migrations) rather than by the turn that
#: happens to be holding an in-memory snapshot.
#:
#: A turn reads its room state once at the start and saves the whole object at
#: the end.  A summary refresh runs in the background and takes minutes, so the
#: next turn's snapshot almost always predates it.  Saving that snapshot used to
#: whole-object replace the cache and the row, silently reverting the summary --
#: which is exactly how a valid v2 summary disappeared at the next turn.
#:
#: These keys are therefore merged from the database instead of being taken from
#: the snapshot (see ``_merge_protected_room_metadata``).
_PROTECTED_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "rolling_summary",
        "rolling_summary_version",
        "rolling_summary_updated_at",
        "rolling_summary_through_message_index",
        "rolling_summary_quarantined_at",
    }
)
_PROTECTED_METADATA_PREFIXES: tuple[str, ...] = ("rolling_summary", "raw_archive_")

#: Temporary write tracing, off unless explicitly enabled:
#:   PERSONA_CONTINUUM_DEBUG_METADATA_WRITES=1
_METADATA_WRITE_TRACE = os.environ.get(
    "PERSONA_CONTINUUM_DEBUG_METADATA_WRITES", ""
).lower() in {"1", "true", "yes", "on"}
_metadata_trace_logger = logging.getLogger("persona_continuum.room.metadata")


def _protected_metadata_keys(metadata: dict[str, Any]) -> list[str]:
    return [
        key
        for key in metadata
        if key in _PROTECTED_METADATA_KEYS
        or key.startswith(_PROTECTED_METADATA_PREFIXES)
    ]


def _summary_fingerprint(metadata: dict[str, Any]) -> tuple[Any, int]:
    """(version, length) fingerprint -- never the summary body."""

    value = metadata.get("rolling_summary")
    return metadata.get("rolling_summary_version"), (
        len(value) if isinstance(value, str) else 0
    )


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


def _trim_bundle_for_stage(
    bundle: MemoryBundle | None, stage_index: int, mem_limit: int | None
) -> MemoryBundle | None:
    """Trim memory bundle components according to the profile degradation ladder."""
    if bundle is None or stage_index == 0:
        return bundle

    # Step 1: trim low-relevance summaries
    trimmed_summaries = list(bundle.hierarchical_summaries)
    if stage_index >= 1:
        trimmed_summaries = [s for s in trimmed_summaries if s.relevance_score >= 0.5][:1]
    if stage_index >= 3:
        trimmed_summaries = []

    # Step 2: trim low-relevance episodes
    trimmed_episodes = list(bundle.relevant_episodes)
    if stage_index >= 2:
        trimmed_episodes = [e for e in trimmed_episodes if e.relevance_score >= 0.5][:1]
    if stage_index >= 4:
        trimmed_episodes = []

    # Step 3: trim facts
    trimmed_facts = list(bundle.semantic_facts)
    if stage_index >= 3:
        limit = mem_limit or 2
        trimmed_facts = trimmed_facts[:limit]
    if stage_index >= 5:
        trimmed_facts = trimmed_facts[:1]

    # Step 4: trim raw excerpts
    trimmed_excerpts = list(bundle.historical_excerpts)
    if stage_index >= 3:
        trimmed_excerpts = trimmed_excerpts[:1]
    if stage_index >= 4:
        trimmed_excerpts = []

    # Step 5: live threads
    trimmed_threads = list(bundle.active_threads)
    if stage_index >= 5:
        trimmed_threads = [t for t in trimmed_threads if t.is_live][:1]

    return MemoryBundle(
        persona_id=bundle.persona_id,
        counterpart_id=bundle.counterpart_id,
        branch_id=bundle.branch_id,
        current_arc=bundle.current_arc,
        current_arc_tokens=bundle.current_arc_tokens,
        recent_dialogue=bundle.recent_dialogue,
        recent_dialogue_tokens=bundle.recent_dialogue_tokens,
        semantic_facts=trimmed_facts,
        active_threads=trimmed_threads,
        relevant_episodes=trimmed_episodes,
        relationship_context=bundle.relationship_context,
        hierarchical_summaries=trimmed_summaries,
        historical_excerpts=trimmed_excerpts,
        retrieval_metadata=bundle.retrieval_metadata,
    )


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
        self.retrieval_planner = getattr(continuum, "retrieval_planner", None)
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
        # Window semantics: a transcript ENTRY is one message -- a user message
        # and a persona reply are two entries -- so a window of 8 is roughly
        # four exchanges.  ``room_raw_message_window`` is the explicit name;
        # ``room_raw_turn_window`` remains a fallback for older deployments.
        raw_window = max(
            4,
            int(
                getattr(room_cfg, "room_raw_message_window", None)
                or getattr(room_cfg, "room_raw_turn_window", 8)
                or 8
            ),
        )
        self.context_manager = RoomContextManager(
            cursor_enabled=bool(getattr(room_cfg, "room_context_cursor_enabled", True)),
            raw_window=raw_window,
            summary_every_turns=max(4, raw_window * 2),
            summary_refresh_every_turns=max(
                2, int(getattr(room_cfg, "room_summary_refresh_every_turns", 4) or 4)
            ),
            summary_max_chars=max(
                400, int(getattr(room_cfg, "room_summary_max_chars", 2400) or 2400)
            ),
            summary_output_max_tokens=max(
                100, int(getattr(room_cfg, "room_summary_output_max_tokens", 800) or 800)
            ),
            summary_input_max_tokens=max(
                512, int(getattr(room_cfg, "room_summary_input_max_tokens", 4096) or 4096)
            ),
        )
        # Long-term memories retrieved/injected per room turn.  This is the
        # *fallback* value now: every turn resolves a ContextProfile and uses
        # that profile's ``recall_top_k`` (see ``resolve_turn_context_policy``).
        self._room_recall_top_k = max(
            1, int(getattr(room_cfg, "room_recall_top_k", 8) or 8)
        )
        # Conservative prompt budget for THIS machine (see Config for why it is
        # far below the model's 16384 context).  These stay the LOCAL profile's
        # numbers and are no longer a global ceiling: they are only applied when
        # the resolved ContextProfile is ``local_constrained``.
        self._room_prompt_target_tokens = max(
            1024, int(getattr(room_cfg, "room_prompt_target_tokens", 6000) or 6000)
        )
        self._room_prompt_hard_tokens = max(
            self._room_prompt_target_tokens,
            int(getattr(room_cfg, "room_prompt_hard_tokens", 7000) or 7000),
        )
        self._room_memory_max_tokens = max(
            0, int(getattr(room_cfg, "room_memory_max_tokens", 400) or 400)
        )
        # Context strategy policy: the local profile keeps reading Config (so
        # the stress-tested numbers stay the user's), while balanced /
        # remote_quality scale with the model's real capability.
        self._local_context_profile = local_profile_from_config(room_cfg)
        self._default_context_strategy = normalize_strategy(
            getattr(room_cfg, "room_context_strategy", ContextStrategy.AUTO.value)
        )
        self._local_memory_constrained = getattr(
            room_cfg, "room_context_local_memory_constrained", None
        )
        # Per-participant resolved policy, keyed by the capability signature it
        # was resolved from, so a re-bind to another model re-resolves instead
        # of reusing a stale budget.  Keys are ``(room_id, participant_id)``.
        self._turn_context_policies: dict[
            tuple[str, str], tuple[tuple[Any, ...], ResolvedContextPolicy]
        ] = {}
        self._last_context_policy: dict[tuple[str, str], ResolvedContextPolicy] = {}
        # Prompt token ceiling for one room turn (None = derive from provider).
        self._room_prompt_max_tokens = getattr(room_cfg, "room_prompt_max_tokens", None)
        # Single-flight for local inference.  A rolling-summary refresh is a full
        # model pass and must never overlap a live turn: two concurrent 27B
        # prefills on a 24 GiB machine is exactly the Metal OOM this work is
        # trying to avoid (observed: summary 8.2k + turn 4.4k prefill together
        # pushed a turn past its first-response deadline).
        self._summary_inference_lock = asyncio.Lock()
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
        self._direct_reply_tasks: dict[str, asyncio.Task[None]] = {}
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
        # Single-flight background Episode consolidation per room.
        self._episode_tasks: dict[str, asyncio.Task[None]] = {}
        # Uploaded room attachments: attachment_id -> attachment metadata
        # (room_id, stored_name, filename, mime, size, kind).  Files live
        # under config.room_uploads_dir/{room_id}/; the registry only maps
        # ids so download routes can resolve them without scanning the disk.
        self._room_attachments: dict[str, dict[str, Any]] = {}

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
        scene_state: RoomSceneState | None = None,
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
        if mode == RoomMode.DIRECT_CHAT:
            for slot in room_participants:
                slot.allow_mcp = False
                slot.allow_agent_tools = False
                slot.counterpart_id = slot.counterpart_id or "user"
                if not slot.initial_relationship:
                    slot.initial_relationship = {"relationship_kind": "partner"}
        if mode == RoomMode.DIRECT_CHAT:
            if protocol != RoomProtocolType.FREE_DISCUSSION:
                raise ValueError("direct_chat_requires_free_discussion_protocol")
            if len(room_participants) != 1:
                raise ValueError("direct_chat_requires_exactly_one_participant")
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
            scene_state=scene_state or RoomSceneState(scene_time=now, clock_wall_time=now),
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

    def _runtime_counterpart(self, state: RoomSessionState, slot: ParticipantSlot) -> str:
        session_id = self._persona_session_ids.get(state.id, {}).get(slot.participant_id)
        if session_id:
            # Keep existing session bindings valid when opening an older room.
            for session in self.continuum.sessions.list_sessions(slot.persona_id):
                if session.id == session_id:
                    return str(session.metadata.get("counterpart_id", f"room:{state.id}"))
        return slot.counterpart_id or (
            "user" if state.mode == RoomMode.DIRECT_CHAT else f"room:{state.id}"
        )

    def _attach_relationship_context(
        self, state: RoomSessionState, slot: ParticipantSlot, prepared: Any
    ) -> None:
        from persona_continuum.runtime.bond_dynamics import relationship_stance

        branch = str(
            prepared.compiled_persona_context.get("runtime_version", {}).get("active_branch_id")
            or "main"
        )
        needs = {need.name: need.level for need in prepared.current_needs}
        for other in state.participants:
            if other.persona_id == slot.persona_id:
                continue
            prior = slot.relationship_priors.get(other.persona_id, {})
            shared = state.shared_context.relationships.get(slot.participant_id, {})
            if not prior and isinstance(shared, dict):
                prior = shared.get(other.participant_id, {})
            if isinstance(prior, str):
                prior = {"relationship_kind": prior}
            if prior:
                self.continuum.sessions.initialize_relationship(
                    slot.persona_id, other.persona_id, prior, branch
                )
            relationship = self.continuum.relationships.get_relationship(
                slot.persona_id, other.persona_id, branch
            )
            prepared.relationship_stance += "\n\n" + relationship_stance(relationship, needs)

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
                    counterpart_id=self._runtime_counterpart(state, slot),
                    initial_relationship=slot.initial_relationship,
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

            await self._open_host_session(room_id, state)

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
            # Opening a room is the natural moment to work through Episode
            # summaries it still owes -- e.g. history folded in by a lazy
            # backfill, which has no live turn to piggyback on.
            self._schedule_episode_consolidation(state)

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

    def start_direct_reply(self, room_id: str, user_message: str) -> asyncio.Task[None]:
        """Run exactly one persona turn for a user message in direct-chat mode."""

        existing = self._direct_reply_tasks.get(room_id)
        if existing and not existing.done():
            return existing
        state = self.get_room(room_id)
        if state is None:
            raise KeyError(room_id)
        if state.mode != RoomMode.DIRECT_CHAT:
            raise ValueError("Room is not in direct chat mode")
        if state.protocol != RoomProtocolType.FREE_DISCUSSION:
            raise ValueError("direct_chat_requires_free_discussion_protocol")
        if len(state.participants) != 1:
            raise ValueError("direct_chat_requires_exactly_one_participant")

        participant_id = state.participants[0].participant_id
        state.status = RoomStatus.DISCUSSING
        state.last_error = None
        self._save_room_state(state, force=True)

        async def _runner() -> None:
            try:
                async for _event in self.step_turn(
                    room_id,
                    manual_speaker_id=participant_id,
                    user_message=user_message,
                ):
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.mark_background_turn_failed(room_id, exc)
            finally:
                current = asyncio.current_task()
                if self._direct_reply_tasks.get(room_id) is current:
                    self._direct_reply_tasks.pop(room_id, None)

        task = asyncio.create_task(_runner())
        self._direct_reply_tasks[room_id] = task
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
            return await asyncio.wait_for(self.start_room(room_id), timeout=deadline_seconds)
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

    async def _mark_initialization_failed(self, room_id: str, exc: BaseException) -> None:
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

        rows = self.continuum.database.conn.execute("SELECT state_json FROM rooms").fetchall()
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
                reason = "上一轮房间任务因服务重启或后台任务中断而未完成，请重新初始化或重试。"
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

        rows = self.continuum.database.conn.execute("SELECT state_json FROM rooms").fetchall()
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

    async def mark_background_turn_failed(self, room_id: str, exc: BaseException) -> None:
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
            if (
                not user_message
                and state.transcript
                and state.transcript[-1].get("participant_id") == "user"
            ):
                user_message = str(
                    state.transcript[-1].get("raw_content")
                    or state.transcript[-1].get("content")
                    or ""
                )
            if user_message and not (
                state.transcript
                and state.transcript[-1].get("participant_id") == "user"
                and (
                    state.transcript[-1].get("raw_content") == user_message
                    or state.transcript[-1].get("content") == user_message
                )
            ):
                injected_event = await self._inject_message_locked(
                    room_id, user_message, _schedule_reply=False
                )
                state = self.get_room(room_id) or state
                yield injected_event
            else:
                SceneRuntime.tick(state.scene_state)
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

            resets = state.metadata.get("voice_reset_participants", [])
            if speaker_id in resets:
                logical_session = self._persona_session_ids.get(room_id, {}).get(speaker_id)
                if not await self.restart_session(room_id, speaker_id):
                    raise RuntimeError("style_firewall_runtime_reset_failed")
                if logical_session:
                    self._persona_session_ids.setdefault(room_id, {})[speaker_id] = logical_session
                resets.remove(speaker_id)
                self._save_room_state(state, force=True)

            # 2. Context Policy Resolution (before any retrieval)
            #
            # The profile decides how many memories this turn may retrieve and
            # how much of them it may show; retrieval itself must therefore
            # happen after it.  Resolving it here also keeps the local survival
            # profile from dictating what a 1M-context model is allowed to see.
            adapter = self.registry.get_adapter(snapshot.agent_runtime_id)
            agent_session = self._active_agent_sessions.get(room_id, {}).get(speaker_id)
            agent_binding = self._active_agent_bindings.get(room_id, {}).get(speaker_id)
            context_policy = self.resolve_turn_context_policy(
                state, slot, agent_session=agent_session, snapshot=snapshot, adapter=adapter
            )
            policy_report = context_policy.as_dict()
            policy_event = {
                "event": "room_context_policy",
                "room_id": room_id,
                "participant_id": speaker_id,
                "persona_id": slot.persona_id,
                "turn_index": state.turn_index,
                "adapter_id": snapshot.agent_runtime_id,
                "model_id": snapshot.model_id,
                **policy_report,
            }
            yield policy_event
            await self._broadcast_event(room_id, policy_event)

            # 3. Recall Gate Analysis (single shared retrieval for the turn)
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

            phase8_active = bool(
                state.metadata.get(
                    "phase8_context_assembly",
                    getattr(self.continuum.config, "phase8_context_assembly", True),
                )
            )

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
                            limit=context_policy.profile.recall_top_k,
                            branch_id="main",
                            include_main_history=True,
                            include_shared_pre_divergence=True,
                        )
                recall_result = RecallGate.attach_memories(gate_plan, list(shared_memories or []))

                memory_bundle: MemoryBundle | None = None
                if phase8_active and self.retrieval_planner is not None:
                    with tracer.span(turn_task_id, "bundle_retrieval"):
                        memory_bundle = self.retrieval_planner.plan_and_retrieve(
                            persona_id=slot.persona_id,
                            counterpart_id=self._runtime_counterpart(state, slot),
                            branch_id="main",
                            user_message=user_message or "",
                            recent_transcript=list(state.transcript or []),
                            room_topic=state.topic,
                            stored_rolling_summary=state.metadata.get("rolling_summary"),
                            context_profile=context_policy.profile,
                            recall_gate_signal=gate_plan,
                        )

            # 4. Context Preparation
            recall_complete_event = {
                "event": "recall_completed",
                "room_id": room_id,
                "participant_id": speaker_id,
                "persona_id": slot.persona_id,
                "triggered": recall_result.triggered or bool(
                    memory_bundle
                    and memory_bundle.retrieval_metadata.mode != RetrievalMode.BASE
                ),
                "reasons": (
                    memory_bundle.retrieval_metadata.reasons
                    if memory_bundle
                    else recall_result.reasons
                ),
                "retrieved_count": (
                    (
                        len(memory_bundle.semantic_facts)
                        + len(memory_bundle.active_threads)
                        + len(memory_bundle.relevant_episodes)
                        + len(memory_bundle.historical_excerpts)
                        + len(memory_bundle.hierarchical_summaries)
                    )
                    if memory_bundle
                    else len(recall_result.memories)
                ),
                "retrieved_memories": [
                    {"id": m.id, "content": m.content, "importance": m.importance}
                    for m in recall_result.memories
                ],
                "retrieval_mode": (
                    memory_bundle.retrieval_metadata.mode.value if memory_bundle else "base"
                ),
                "timestamp": datetime.now(UTC).isoformat(),
            }
            yield recall_complete_event
            await self._broadcast_event(room_id, recall_complete_event)

            sess_id = self._persona_session_ids.get(room_id, {}).get(speaker_id)
            if not sess_id:
                cont_sess = self.continuum.sessions.start_session(
                    persona_id=slot.persona_id,
                    title=f"Room {room_id} [{slot.participant_id}]",
                    counterpart_id=self._runtime_counterpart(state, slot),
                    initial_relationship=slot.initial_relationship,
                    session_type="multi_agent_room",
                    room_id=room_id,
                )
                sess_id = cont_sess.id
                self._persona_session_ids.setdefault(room_id, {})[speaker_id] = sess_id

            interaction_counterpart = None
            interaction_message = None
            if state.mode != RoomMode.DIRECT_CHAT:
                if user_message:
                    interaction_counterpart, interaction_message = "user", user_message
                elif state.transcript:
                    previous = state.transcript[-1]
                    previous_persona = previous.get("persona_id")
                    if previous_persona and previous_persona != slot.persona_id:
                        interaction_counterpart = str(previous_persona)
                        interaction_message = str(previous.get("content") or "")
                if interaction_counterpart:
                    prior = slot.relationship_priors.get(interaction_counterpart, {})
                    if not prior:
                        shared = state.shared_context.relationships.get(slot.participant_id, {})
                        other_slot = next(
                            (
                                p
                                for p in state.participants
                                if p.persona_id == interaction_counterpart
                            ),
                            None,
                        )
                        if isinstance(shared, dict) and other_slot:
                            prior = shared.get(other_slot.participant_id, {})
                    if isinstance(prior, str):
                        prior = {"relationship_kind": prior}
                    self.continuum.sessions.initialize_relationship(
                        slot.persona_id, interaction_counterpart, prior
                    )
            with tracer.span(turn_task_id, "context_build"):
                prepared = self.continuum.sessions.prepare_turn(
                    persona_id=slot.persona_id,
                    session_id=sess_id,
                    user_message=user_message or state.topic or "Continue the discussion",
                    counterpart_id=self._runtime_counterpart(state, slot),
                    preset_memories=shared_memories,
                    interaction_counterpart_id=interaction_counterpart,
                    interaction_message=interaction_message,
                    current_time=state.scene_state.scene_time,
                    # Without this the recalled set is truncated a SECOND time
                    # to prepare_turn's own default of 8, which silently
                    # defeated any profile that retrieves more.
                    max_context_items=context_policy.profile.recall_top_k,
                )

            self._attach_relationship_context(state, slot, prepared)
            persistent = self._participant_is_persistent(
                adapter=adapter,
                session=agent_session,
                snapshot=snapshot,
            )
            stored_summary = state.metadata.get("rolling_summary")
            # Only a canonical summary may be injected.  A missing, failed or
            # in-flight refresh must NOT fall back to the full transcript (the
            # context manager bounds the window unconditionally); it simply
            # means this turn has no summary block.
            summary_block = (
                stored_summary
                if is_canonical_summary(
                    stored_summary, state.metadata.get("rolling_summary_version")
                )
                else None
            )
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
                # Working memory is sized by a TOKEN budget; the message count
                # is only a secondary cap.  Both come from this turn's profile,
                # so a 24 GiB Mac no longer decides what a 1M model may read.
                message_window=context_policy.profile.recent_message_window,
                recent_token_budget=context_policy.profile.recent_dialogue_token_budget,
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
                # --- component-aware prompt budget -----------------------
                # Measured with the real Qwen tokenizer on a live 8756-token
                # turn: retrieved memories 3454 (39%!), persona kernel ~3300,
                # scene facts 827, recent dialogue 817, current message 17.
                # Memories are the only large block that can be given back
                # without changing who the persona is or what the user just
                # said, so they go first; then the oldest raw dialogue; then the
                # summary.  Identity, constraints, the output contract and the
                # current message are never trimmed.
                #
                # Which rungs exist, how far memories may be cut and how much
                # summary survives all come from this turn's ContextProfile.
                # ``local_constrained`` reproduces the historical six-rung
                # ladder exactly; ``remote_quality`` has a single rung, so a
                # large model is never degraded for being large.
                summary_for_prompt = (
                    turn_ctx.summary_block
                    if turn_ctx.mode in {"windowed", "rehydrated", "full"}
                    else None
                )
                base_transcript = list(turn_ctx.turns)
                active_profile = context_policy.profile
                budget_stages: list[tuple[int, int, int | None, int | None]] = [
                    (stage.memory_limit or 0, stage.memory_max_tokens, stage.recent_window,
                     stage.summary_max_tokens)
                    for stage in active_profile.trim_ladder(
                        top_k=active_profile.recall_top_k,
                        memory_max_tokens=active_profile.memory_max_tokens,
                        memory_budget_tokens=active_profile.memory_budget_tokens,
                    )
                ]
                prompt_target_tokens = context_policy.prompt_target_tokens
                prompt_hard_tokens = context_policy.prompt_hard_tokens
                prompt_tokens = 0
                budget_stage_used = -1
                budget_trimmed = False
                # Filled by the composer for the prompt that is finally kept,
                # so the Context Assembly Report can name the block that
                # consumed the budget instead of only reporting a total.
                assembly_report: dict[str, Any] = {}
                for stage_index, (mem_limit, mem_tokens, keep_raw, sum_cap) in enumerate(
                    budget_stages
                ):
                    transcript_for_prompt = (
                        base_transcript if keep_raw is None else base_transcript[-keep_raw:]
                    )
                    summary_value = summary_for_prompt
                    if summary_value and sum_cap:
                        clipped = _clip_to_tokens(summary_value, sum_cap)
                        summary_value = clipped if clipped == summary_value else clipped + "…"
                    stage_bundle = (
                        _trim_bundle_for_stage(memory_bundle, stage_index, mem_limit)
                        if (phase8_active and memory_bundle is not None)
                        else None
                    )
                    system_prompt, composed_user_prompt, _ = (
                        self.prompt_composer.compose_turn_prompt(
                            slot=slot,
                            binding=snapshot,
                            prepared=prepared,
                            dynamic_recall_memories=recall_result.memories,
                            recent_transcript=transcript_for_prompt,
                            room_topic=state.topic,
                            user_message=user_message,
                            kernel=kernel,
                            context_mode=turn_ctx.mode,
                            summary_block=summary_value,
                            cursor_span=cursor_span,
                            scene_state=state.scene_state,
                            structured_actions=True,
                            memory_limit=mem_limit,
                            memory_max_tokens=mem_tokens,
                            memory_bundle=stage_bundle,
                            report=assembly_report,
                        )
                    )
                    prompt_tokens = _estimate_tokens(system_prompt) + _estimate_tokens(
                        composed_user_prompt
                    )
                    budget_stage_used = stage_index
                    if stage_index > 0:
                        budget_trimmed = True
                    if prompt_tokens <= prompt_target_tokens:
                        break
                if prompt_tokens > prompt_hard_tokens:
                    # Refuse rather than hand a prompt larger than the model's
                    # own effective budget to a provider that will reject it.
                    # Reaching this means the fixed blocks alone (identity +
                    # constraints + contract + current message) already blew
                    # the budget -- for a remote_quality profile that is the
                    # capability-derived ceiling, not a 7000-token constant.
                    budget_error = AgentTransportError(
                        "Room prompt exceeds the hard token budget",
                        phase="session_prompt",
                        diagnostics={
                            "protocol": "openai_compatible_http",
                            "failure_code": "ROOM_CONTEXT_BUDGET_EXCEEDED",
                            "context_profile": active_profile.name,
                            "estimated_prompt_tokens": prompt_tokens,
                            "hard_limit_tokens": prompt_hard_tokens,
                            "target_tokens": prompt_target_tokens,
                            "budget_stage": budget_stage_used,
                            "room_id": room_id,
                            "turn_index": state.turn_index,
                        },
                    )
                    yield {
                        "event": "agent_error",
                        "room_id": room_id,
                        "participant_id": speaker_id,
                        "error": "Room prompt exceeds the hard token budget",
                        "failure_code": "ROOM_CONTEXT_BUDGET_EXCEEDED",
                        "diagnostics": dict(budget_error.diagnostics),
                    }
                    return
                if prompt_tokens > prompt_target_tokens:
                    # Sent, but recorded: the trim stages could not get under the
                    # soft target without harming the persona.
                    _metadata_trace_logger.warning(
                        "ROOM_PROMPT_SOFT_LIMIT_EXCEEDED room=%s turn=%s profile=%s "
                        "estimated_tokens=%s target=%s stage=%s",
                        room_id,
                        state.turn_index,
                        active_profile.name,
                        prompt_tokens,
                        prompt_target_tokens,
                        budget_stage_used,
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
                        f"Earlier user statements (salvaged):\n{salvaged}\n\n{composed_user_prompt}"
                    )
                # The free-discussion path advertises exactly the tools
                # `tools_for_turn` and `_execute_tool_cb` will accept.
                system_prompt = f"{system_prompt}\n\n{tool_block}"
            tracer.count(
                turn_task_id,
                "transcript_tokens_sent",
                _estimate_tokens("\n".join(str(t.get("content") or "") for t in turn_ctx.turns)),
            )
            tracer.observe(turn_task_id, "memory_results_count", len(prepared.relevant_memories))
            tracer.count(turn_task_id, f"ctx_{turn_ctx.mode}", 1)
            if turn_ctx.mode == "delta":
                tracer.count(turn_task_id, "delta", 1)

            # --- Context Assembly Report --------------------------------------
            # A compact, machine-readable record of what actually reaches the
            # model this turn.  This is the acceptance instrument: it proves the
            # prompt stays bounded as turns accumulate, and it makes a
            # full-transcript regression immediately visible.  It is emitted as
            # a room event (never into the model prompt) and deliberately does
            # not carry the raw recall query text or any dialogue body.
            stored_summary = state.metadata.get("rolling_summary")
            memory_candidates = len(getattr(recall_result, "memories", []) or [])
            episodes = getattr(self.continuum, "episodes", None)
            facts = getattr(self.continuum, "facts", None)
            threads = getattr(self.continuum, "threads", None)
            context_report = {
                "event": "room_context_report",
                "room_id": room_id,
                "participant_id": speaker_id,
                "persona_id": slot.persona_id,
                "context_mode": turn_ctx.mode,
                "turn_index": state.turn_index,
                # -- policy --------------------------------------------------
                "context_profile": context_policy.profile_name,
                "context_strategy_requested": context_policy.requested_strategy,
                "context_profile_reasons": list(context_policy.reasons),
                "adapter_id": snapshot.agent_runtime_id,
                "model_id": snapshot.model_id,
                "context_window": context_policy.context_window,
                "context_window_source": context_policy.context_window_source,
                "context_window_verified": context_policy.context_window_verified,
                "local_endpoint": context_policy.local_endpoint,
                "local_safety_ceiling_applied": context_policy.local_safety_ceiling_applied,
                "effective_context_budget": context_policy.effective_context_budget,
                "capability_budget_tokens": context_policy.capability_budget_tokens,
                "profile_budget_tokens": context_policy.profile_budget_tokens,
                "context_capacity_ceiling": context_policy.context_capacity_ceiling,
                "context_target_budget": context_policy.context_target_budget,
                "context_hard_budget": context_policy.context_hard_budget,
                "generation_reserve_tokens": context_policy.generation_reserve_tokens,
                "reasoning_reserve_tokens": context_policy.reasoning_reserve_tokens,
                "safety_reserve_tokens": context_policy.safety_reserve_tokens,
                # -- what entered the prompt ---------------------------------
                "transcript_total_entries": len(state.transcript or []),
                "transcript_in_prompt": len(turn_ctx.turns),
                "raw_window": context_policy.profile.recent_message_window,
                "recent_dialogue_token_budget": (
                    context_policy.profile.recent_dialogue_token_budget
                ),
                "recent_dialogue_tokens": assembly_report.get("recent_dialogue_tokens", 0),
                "persona_tokens": assembly_report.get("persona_identity_tokens", 0)
                + assembly_report.get("persona_constraints_tokens", 0),
                "dynamic_state_tokens": assembly_report.get("dynamic_state_tokens", 0),
                "voice_exemplar_tokens": assembly_report.get("voice_exemplar_tokens", 0),
                "memory_tokens": assembly_report.get("memory_tokens", 0),
                "summary_tokens": assembly_report.get("summary_tokens", 0),
                "scene_facts_tokens": assembly_report.get("scene_facts_tokens", 0),
                "current_message_tokens": assembly_report.get("current_message_tokens", 0),
                "static_system_tokens": _estimate_tokens(system_prompt),
                "estimated_prompt_tokens": _estimate_tokens(system_prompt)
                + _estimate_tokens(composed_user_prompt),
                "prompt_target_tokens": prompt_target_tokens,
                "prompt_hard_tokens": prompt_hard_tokens,
                "prompt_budget_stage": budget_stage_used,
                "prompt_budget_trimmed": budget_trimmed,
                "prompt_budget_stages_available": len(budget_stages),
                # -- memory ---------------------------------------------------
                "rolling_summary_version": state.metadata.get("rolling_summary_version"),
                "rolling_summary_canonical": is_canonical_summary(
                    stored_summary, state.metadata.get("rolling_summary_version")
                ),
                "recall_triggered": bool(getattr(recall_result, "triggered", False)),
                "recall_reasons": list(getattr(recall_result, "reasons", []) or []),
                "recall_query_chars": len(str(getattr(recall_result, "query", "") or "")),
                "memory_top_k": context_policy.profile.recall_top_k,
                "memory_candidates": memory_candidates,
                "memory_retrieved": memory_candidates,
                "memory_selected": assembly_report.get("memory_lines_rendered", 0),
                "memory_injected": len(prepared.relevant_memories),
                "memory_max_tokens": context_policy.profile.memory_max_tokens,
                "retrieval_mode": (
                    memory_bundle.retrieval_metadata.mode.value if memory_bundle else "base"
                ),
                "retrieval_timings_ms": (
                    memory_bundle.retrieval_metadata.timings.model_dump()
                    if memory_bundle
                    else None
                ),
                "episodes_selected": (
                    assembly_report.get("episodes_rendered", 0) if phase8_active else 0
                ),
                "episode_tokens": (
                    assembly_report.get("episode_tokens", 0) if phase8_active else 0
                ),
                "episode_injection": "phase8_active" if phase8_active else "phase8_not_enabled",
                "episodes_available": (
                    episodes.count_episodes(room_id=room_id)
                    if episodes is not None
                    else None
                ),
                "semantic_facts_available": (
                    facts.count_active(persona_id=slot.persona_id) if facts is not None else None
                ),
                "semantic_memory_tokens": (
                    assembly_report.get("semantic_fact_tokens", 0) if phase8_active else 0
                ),
                "facts_selected": (
                    assembly_report.get("facts_rendered", 0) if phase8_active else 0
                ),
                "fact_injection": "phase8_active" if phase8_active else "phase8_not_enabled",
                "threads_available": (
                    threads.count_live(persona_id=slot.persona_id)
                    if threads is not None
                    else None
                ),
                "threads_selected": (
                    assembly_report.get("threads_rendered", 0) if phase8_active else 0
                ),
                "thread_tokens": (
                    assembly_report.get("thread_tokens", 0) if phase8_active else 0
                ),
                "thread_injection": "phase8_active" if phase8_active else "phase8_not_enabled",
                "threads_pending_resolution": (
                    threads.count_pending_resolution(persona_id=slot.persona_id)
                    if threads is not None
                    else None
                ),
                "relationship_event_tokens": None,
            }
            raw_recall = getattr(self.continuum, "raw_recall", None)
            context_report.update(
                {
                    "historical_excerpt_tokens": (
                        assembly_report.get("historical_excerpt_tokens", 0)
                        if phase8_active
                        else 0
                    ),
                    "raw_excerpts_selected": (
                        assembly_report.get("raw_excerpts_rendered", 0)
                        if phase8_active
                        else 0
                    ),
                    "raw_excerpts_expanded": (
                        assembly_report.get("raw_excerpts_rendered", 0)
                        if phase8_active
                        else 0
                    ),
                    "raw_excerpt_injection": (
                        "phase8_active" if phase8_active else "phase8_not_enabled"
                    ),
                    "raw_recall_available": bool(raw_recall is not None),
                }
            )
            hierarchies = getattr(self.continuum, "hierarchies", None)
            context_report.update(
                {
                    "hierarchical_summaries_available": (
                        hierarchies.count_available(persona_id=slot.persona_id)
                        if hierarchies is not None
                        else None
                    ),
                    "hierarchical_summary_tokens": (
                        assembly_report.get("hierarchical_summary_tokens", 0)
                        if phase8_active
                        else 0
                    ),
                    "summary_injection": (
                        "phase8_active" if phase8_active else "phase8_not_enabled"
                    ),
                    "hierarchical_summaries_pending": (
                        hierarchies.count_pending(persona_id=slot.persona_id)
                        if hierarchies is not None
                        else None
                    ),
                }
            )
            tracer.count(
                turn_task_id,
                "prompt_tokens_sent",
                context_report["estimated_prompt_tokens"],
            )
            tracer.count(
                turn_task_id,
                "transcript_entries_sent",
                context_report["transcript_in_prompt"],
            )
            yield context_report
            await self._broadcast_event(room_id, context_report)

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

            # A live turn takes precedence over background summarisation: if a
            # summary prefill is in flight, let it finish before starting ours
            # rather than running two model passes at once.
            if self._summary_inference_lock.locked():
                await self._summary_inference_lock.acquire()
                self._summary_inference_lock.release()

            stream_started = time.monotonic()
            first_token_at: float | None = None
            turn_attachments = self._turn_attachments(state)
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
                    attachments=turn_attachments or None,
                    expected_output=(
                        SceneOutput.model_json_schema()
                        if snapshot.capabilities.get("structured_output_mode")
                        in {"native_schema", "tool_schema"}
                        else None
                    ),
                    tools=tools_for_turn if use_tools else [],
                    phase="room_agent_turn",
                    metadata={
                        "turn_id": turn_id,
                        "tool_executor": _execute_tool_cb,
                    },
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

            raw_final_content = str(redact_secrets(final_content))
            channels = normalize_turn(raw_final_content, actor=slot.persona_id)
            next_scene = state.scene_state.model_copy(deep=True)
            channels, scene_events = SceneRuntime().accept_turn(
                next_scene,
                room_id=room_id,
                turn_id=turn_id,
                actor=slot.persona_id,
                raw_content=raw_final_content,
                channels=channels,
                advance_clock=False,
            )
            final_content = channels.spoken_text
            speech_event = {
                "event": "agent_message_delta",
                "room_id": room_id,
                "participant_id": speaker_id,
                "delta": final_content,
                "replace_response": True,
            }
            yield speech_event
            await self._broadcast_event(room_id, speech_event)
            state.last_error = None
            agent_completed_ev = {
                "event": "agent_completed",
                "room_id": room_id,
                "participant_id": speaker_id,
                "content": final_content,
                "actions": channels.actions,
                "scene_events": channels.scene_events,
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
            shared_user_turn = self._shared_user_turn(state)
            commit_res = self.continuum.sessions.commit_turn(
                persona_id=slot.persona_id,
                session_id=sess_id,
                user_message=user_message or state.topic or "Room discussion turn",
                persona_response=raw_final_content,
                occurred_at=state.scene_state.scene_time,
                source_turn_id=turn_id,
                scene_events=channels.scene_events,
                used_memory_ids=recall_ids,
                counterpart_id=self._runtime_counterpart(state, slot),
                shared_user_turn=shared_user_turn,
            )

            state.scene_state = next_scene
            if channels.non_voice_context or (
                "（" in raw_final_content or "(" in raw_final_content
            ):
                state.metadata.setdefault("voice_reset_participants", []).append(speaker_id)
            commit_complete_ev = {
                "event": "persona_commit_completed",
                "room_id": room_id,
                "participant_id": speaker_id,
                "persona_id": slot.persona_id,
                "turn_id": commit_res.get("turn_id"),
                "memory_id": commit_res.get("memory_id"),
                "episode_id": commit_res.get("episode_id"),
                "reflection_due": commit_res.get("reflection_due", False),
                # Public delta summary ("anxiety 18% -> 27% +9%").  The UI
                # refetches runtime state on this event instead of polling.
                "state_summary": commit_res.get("state_summary") or "",
            }
            yield commit_complete_ev
            await self._broadcast_event(room_id, commit_complete_ev)

            # 5b. Memory Episodes: the turn is already durable, so this only
            # queues the description of what was just recorded.  The reply the
            # user is waiting for is never delayed by it.
            self._schedule_episode_consolidation(state, commit_res)

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
                **channels.model_dump(mode="json"),
                "scene_time": state.scene_state.scene_time.isoformat(),
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
                tracer.observe(turn_task_id, "prompt_tokens", int(usage.get("input_tokens") or 0))
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
                        "channels": {
                            **channels.model_dump(mode="json"),
                            "scene_time": state.scene_state.scene_time.isoformat(),
                        },
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
        SceneRuntime.tick(state.scene_state)
        if question and not (
            state.transcript
            and state.transcript[-1].get("participant_id") == "user"
            and state.transcript[-1].get("content") == question
        ):
            await self._inject_message_locked(room_id, question, _schedule_reply=False)
            state = self.get_room(room_id) or state
        # Validate BEFORE mutating: a participant/runtime pre-check failure
        # must never leave the room stuck in DISCUSSING (fix #4).
        definition = self.protocol_registry.get(state.protocol, state.protocol_config)
        self.protocol_registry.validate_participants(definition, state.participants)
        self._clear_session_cancel_events(room_id)
        question = normalize_turn(question).spoken_text
        # New user input merges into the case state; room.topic stays stable
        # so follow-ups never displace the original case anchor.  An empty
        # question falls back to the topic for EXECUTION only -- the merge
        # still uses the raw question and is skipped entirely when empty.
        if str(question).strip():
            state.case_state = merge_case_state(_coerce_case_state(state.case_state), question)
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
        if protocol_state.status.value in settled:
            state.status = RoomStatus.READY
            state.last_error = None
        else:
            state.status = RoomStatus.ERROR
            detail = ""
            for task in getattr(protocol_state, "tasks", []):
                if getattr(task, "status", None) == ProtocolTaskStatus.FAILED and getattr(
                    task, "error", None
                ):
                    detail = f": {task.error}"
                    break
            state.last_error = f"room_protocol_run_failed{detail}"
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
                _coerce_case_state(state.case_state).problem_definition or "立即总结当前已完成结果",
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
        state.status = RoomStatus.READY if finalized.status.value == "success" else RoomStatus.ERROR
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
            return await self._convert_room_protocol_locked(room_id, target_protocol, role_mapping)
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
            return (
                "；".join(
                    f"{name_by_id.get(pid, pid)}→{role}" for pid, role in role_mapping.items()
                )
                or "（空）"
            )

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

    # --- Context policy -----------------------------------------------------

    @staticmethod
    def _session_context_facts(
        agent_session: Any,
        snapshot: Any,
    ) -> tuple[str | None, int | None, str, bool]:
        """``(model_id, context_window, source, verified)`` for one participant.

        Reads only capabilities the runtime actually reported.  An unknown
        window stays ``None`` -- the resolver treats Unknown as Unknown instead
        of assuming 32K, which is what used to mis-size large models.
        """

        from persona_continuum.agent.context_capability import source_is_verified
        from persona_continuum.numeric import safe_int

        data = getattr(agent_session, "session_data", None)
        data = data if isinstance(data, dict) else {}
        snapshot_model = str(getattr(snapshot, "model_id", "") or "") or None
        model_id: str | None = None
        capability = data.get("model_capability")
        if isinstance(capability, dict):
            model_id = (
                str(capability.get("id") or capability.get("model_id") or "") or snapshot_model
            )
            window = safe_int(capability.get("context_window"), default=None, minimum=1)
            if window is not None:
                source = str(
                    capability.get("source")
                    or data.get("effective_context_window_source")
                    or "agent_model_metadata"
                )
                return model_id, window, source, source_is_verified(source)
        for key, default_source in (
            ("effective_context_window", "agent_model_metadata"),
            ("native_context_window", "agent_model_metadata"),
        ):
            window = safe_int(data.get(key), default=None, minimum=1)
            if window is not None:
                source = str(
                    data.get("effective_context_window_source")
                    or data.get("context_window_source")
                    or default_source
                )
                verified = bool(data.get("context_verified")) or source_is_verified(source)
                return model_id or snapshot_model, window, source, verified
        return model_id or snapshot_model, None, "unknown", False

    def resolve_turn_context_policy(
        self,
        state: RoomSessionState,
        slot: ParticipantSlot,
        *,
        agent_session: Any = None,
        snapshot: Any = None,
        adapter: Any = None,
        strategy: str | None = None,
    ) -> ResolvedContextPolicy:
        """Resolve how much of THIS participant's model one turn may use.

        Resolution order for the strategy: explicit argument, participant
        slot, room metadata, global config default.  ``auto`` then decides from
        the provider, the reported context window, endpoint locality and the
        model capability.  The result is cached per participant keyed by the
        capability signature, so a re-bind to another model re-resolves.
        """

        model_id, window, source, verified = self._session_context_facts(
            agent_session, snapshot
        )
        # Resolution order: explicit argument, participant slot, room metadata,
        # global config default.  ``auto`` means "no opinion", so it falls
        # through to the next level instead of pinning the whole room.
        resolved_strategy = ContextStrategy.AUTO.value
        for candidate in (
            strategy,
            getattr(slot, "context_strategy", None),
            (state.metadata or {}).get("context_strategy"),
            self._default_context_strategy,
        ):
            normalized = normalize_strategy(candidate)
            if normalized != ContextStrategy.AUTO.value:
                resolved_strategy = normalized
                break
        base_url = getattr(adapter, "base_url", None) if adapter is not None else None
        if base_url is None:
            data = getattr(agent_session, "session_data", None)
            if isinstance(data, dict):
                base_url = data.get("base_url")
        runtime_source = (
            "openai_compatible_api" if base_url else ("local_cli" if adapter else "unknown")
        )
        cache_key = (
            resolved_strategy,
            model_id,
            window,
            source,
            bool(base_url),
            self._local_memory_constrained,
        )
        policy_key = (state.id, slot.participant_id)
        cached = self._turn_context_policies.get(policy_key)
        if cached is not None and cached[0] == cache_key:
            return cached[1]
        policy = resolve_context_policy(
            ContextPolicyRequest(
                strategy=resolved_strategy,
                adapter_id=str(getattr(adapter, "adapter_id", "") or ""),
                runtime_source=runtime_source,
                base_url=str(base_url) if base_url else None,
                model_id=model_id,
                context_window=window,
                context_window_source=source,
                context_verified=verified,
                local_memory_constrained=self._local_memory_constrained,
                local_profile=self._local_context_profile,
            )
        )
        self._turn_context_policies[policy_key] = (cache_key, policy)
        self._last_context_policy[policy_key] = policy
        return policy

    def _room_summary_boundary(self, state: RoomSessionState) -> int:
        """Conservative eviction boundary for the shared rolling summary.

        The summary is shared by every participant, so it must cover whatever
        has left the SMALLEST window in the room.  Taking the minimum over the
        participants' resolved policies is what guarantees no committed turn
        becomes an orphan (gone from the prompt and never summarised).  With no
        resolved policy yet, the manager's own (local, smallest) window is the
        conservative answer.
        """

        transcript = list(state.transcript or [])
        boundaries: list[int] = []
        for slot in state.participants:
            policy = self._last_context_policy.get((state.id, slot.participant_id))
            if policy is None:
                continue
            boundaries.append(
                self.context_manager.eviction_boundary(
                    transcript,
                    message_window=policy.profile.recent_message_window,
                    recent_token_budget=policy.profile.recent_dialogue_token_budget,
                )
            )
        if boundaries:
            return min(boundaries)
        return self.context_manager.eviction_boundary(transcript)

    def _room_summary_profile(self, state: RoomSessionState) -> ContextProfile | None:
        """Profile of the *summariser* (the host agent), if it is resolved yet."""

        host_id = state.host_participant_id
        if host_id:
            policy = self._last_context_policy.get((state.id, host_id))
            if policy is not None:
                return policy.profile
        room_policies = [
            policy
            for (room_id, _), policy in self._last_context_policy.items()
            if room_id == state.id
        ]
        if len(room_policies) == 1:
            return room_policies[0].profile
        return None

    async def _execute_protocol_action(self, request: ProtocolActionRequest) -> dict[str, Any]:
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
        # Protocol turns resolve the same ContextProfile as free turns: a
        # protocol step on a 1M-context model must not be capped by the local
        # survival profile either.
        protocol_policy = self.resolve_turn_context_policy(
            state, slot, agent_session=agent_session, snapshot=snapshot, adapter=adapter
        )

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
        # ``None`` -- not ``[]`` -- means "no preset memories".  prepare_turn
        # treats ``None`` as "run your own search" but ``[]`` as "use zero
        # memories", so the old ``else []`` silently disabled long-term recall
        # for every protocol turn the gate did not trigger on.
        memories: list[Any] | None = None
        if gate_plan.triggered:
            memories = self.continuum.memories.search_memories(
                persona_id=slot.persona_id,
                query=gate_plan.query,
                limit=protocol_policy.profile.recall_top_k,
                branch_id="main",
                include_main_history=True,
                include_shared_pre_divergence=True,
            )
        recall_result = RecallGate.attach_memories(gate_plan, list(memories or []))
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
                counterpart_id=self._runtime_counterpart(state, slot),
                initial_relationship=slot.initial_relationship,
                session_type="room_protocol",
                room_id=state.id,
            )
            session_id = continuum_session.id
            self._persona_session_ids.setdefault(state.id, {})[slot.participant_id] = session_id
        prepared = self.continuum.sessions.prepare_turn(
            persona_id=slot.persona_id,
            session_id=session_id,
            user_message=request.question,
            current_time=state.scene_state.scene_time,
            counterpart_id=self._runtime_counterpart(state, slot),
            preset_memories=memories,
            max_context_items=protocol_policy.profile.recall_top_k,
        )
        self._attach_relationship_context(state, slot, prepared)
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
            scene_state=state.scene_state,
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
            key: value for key, value in task_context.items() if key != "independent_first"
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
        # ``is_canonical_summary`` only ever accepts a non-empty string, so the
        # coercion is a no-op that keeps the packed section typed as ``str``.
        canonical_summary = (
            str(stored_summary)
            if is_canonical_summary(
                stored_summary, state.metadata.get("rolling_summary_version")
            )
            else ""
        )
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
            # section when empty.  Legacy evidence dumps are not canonical and
            # are excluded above.
            room_summary=canonical_summary,
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
            protocol_attachments = self._turn_attachments(state)
            return await runtime_executor.execute_structured(
                current_binding,
                system_prompt=system_prompt,
                user_message=user_message if user_message is not None else user_prompt,
                schema=self._protocol_output_schema(request.action, request.protocol),
                phase=f"room_protocol_{request.action}",
                attachments=protocol_attachments or None,
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
            retry_binding = self._active_agent_bindings.get(state.id, {}).get(slot.participant_id)
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
                str(output.get("clarification_question") or output.get("problem_definition") or ""),
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
                occurred_at=prepared.current_time,
                used_memory_ids=[memory.id for memory in recall_result.memories],
                counterpart_id=self._runtime_counterpart(state, slot),
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
        attachment_ids: list[str] | None = None,
        input_mode: str = "speech",
    ) -> dict[str, Any]:
        lock = self._room_locks.setdefault(room_id, asyncio.Lock())
        async with lock:
            return await self._inject_message_locked(
                room_id,
                content,
                injection_type,
                client_message_id,
                attachment_ids,
                input_mode=input_mode,
            )

    async def _inject_message_locked(
        self,
        room_id: str,
        content: str,
        injection_type: str = "external_information",
        client_message_id: str | None = None,
        attachment_ids: list[str] | None = None,
        _schedule_reply: bool = True,
        input_mode: str = "speech",
    ) -> dict[str, Any]:
        state = self.get_room(room_id)
        if not state:
            raise KeyError(room_id)
        if state.status not in {RoomStatus.READY, RoomStatus.DISCUSSING, RoomStatus.PAUSED}:
            raise RuntimeError(
                f"Room injection requires ready/discussing/paused status, got {state.status.value}"
            )
        attachments = self._resolve_inject_attachments(room_id, attachment_ids or [])
        message = str(redact_secrets(content)).strip()
        if not message and not attachments:
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

        entry: dict[str, Any] = {
            "turn_id": turn_id,
            "participant_id": "user",
            "persona_id": "user",
            "speaker_name": "User",
            "content": message,
            "injection_type": injection_type,
            "commit_status": "user_injected",
            "created_at": datetime.now(UTC).isoformat(),
        }
        if input_mode == "time":
            parsed_time = parse_temporal_input(
                message,
                reference_time=state.scene_state.scene_time,
                timezone_str=state.scene_state.timezone,
            )
            old_time = state.scene_state.scene_time
            state.scene_state.scene_time = parsed_time.target_scene_time or (
                old_time + timedelta(seconds=parsed_time.seconds)
            )
            state.scene_state.elapsed_since_last_interaction = max(
                0.0, (state.scene_state.scene_time - old_time).total_seconds()
            )
            state.scene_state.last_interaction_scene_time = state.scene_state.scene_time
            state.scene_state.last_interaction_wall_time = datetime.now(UTC)

            for p in state.participants:
                with contextlib.suppress(Exception):
                    self.continuum.affect.get_emotions(
                        p.persona_id, now=state.scene_state.scene_time
                    )
                    self.continuum.motivation.get_needs(
                        p.persona_id, now=state.scene_state.scene_time
                    )
                    self.continuum.motivation.update_needs(
                        p.persona_id,
                        {},
                        "scene_time_elapsed",
                        now=state.scene_state.scene_time,
                        commit=False,
                    )
            self.continuum.database.conn.commit()

            time_event = SceneEvent(
                id=f"scene:{room_id}:{turn_id}:tadv",
                room_id=room_id,
                source_turn_id=turn_id,
                actor="user",
                type="time_advance",
                payload={
                    "intent": parsed_time.kind,
                    "seconds": parsed_time.seconds,
                    "previous_scene_time": old_time.isoformat(),
                    "current_scene_time": state.scene_state.scene_time.isoformat(),
                    "description": parsed_time.description,
                },
                scene_time=state.scene_state.scene_time,
                created_at=datetime.now(UTC),
            )
            state.scene_state.recent_events = (state.scene_state.recent_events + [time_event])[-30:]
            entry["content"] = parsed_time.description
            entry["spoken_text"] = ""
            entry["input_mode"] = "time"
            entry["actions"] = []
            entry["scene_events"] = [time_event.model_dump(mode="json")]
            entry["scene_time"] = state.scene_state.scene_time.isoformat()
            state.transcript.append(entry)
            state.metadata.setdefault("injections", []).append(entry)
            state.updated_at = datetime.now(UTC)
            self._save_transcript_record(
                RoomTranscriptRecord(
                    id=new_id("rturn"),
                    room_id=room_id,
                    turn_id=turn_id,
                    participant_id="user",
                    persona_id="user",
                    speaker_name="User",
                    agent_runtime_id="",
                    content=parsed_time.description,
                    commit_status="user_injected",
                    metadata={
                        "injection_type": injection_type,
                        "channels": {
                            "raw_content": message,
                            "spoken_text": "",
                            "input_mode": "time",
                            "actions": [],
                            "scene_events": [time_event.model_dump(mode="json")],
                            "scene_time": state.scene_state.scene_time.isoformat(),
                        },
                    },
                    created_at=datetime.now(UTC),
                )
            )
            self._save_room_state(state, force=True)
            event = {"event": "room_message_injected", "room_id": room_id, "message": entry}
            await self._broadcast_event(room_id, event)
            return event
        else:
            channels, scene_events = SceneRuntime().accept_turn(
                state.scene_state,
                room_id=room_id,
                turn_id=turn_id,
                actor="user",
                raw_content=message,
                input_mode=input_mode,
            )
            entry.update(channels.model_dump(mode="json"))
            entry["scene_time"] = state.scene_state.scene_time.isoformat()
            if attachments:
                entry["attachments"] = attachments
                entry["metadata"] = {"injection_type": injection_type, "attachments": attachments}
            state.transcript.append(entry)
            state.metadata.setdefault("injections", []).append(entry)
            state.updated_at = datetime.now(UTC)
        # Persist the injection into the event store as well, so history
        # survives the state-snapshot transcript window.
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
                metadata=(
                    {
                        "injection_type": injection_type,
                        "attachments": attachments,
                        "channels": {
                            **channels.model_dump(mode="json"),
                            "scene_time": state.scene_state.scene_time.isoformat(),
                        },
                    }
                ),
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
        if _schedule_reply and state.status == RoomStatus.READY:
            if state.protocol == RoomProtocolType.FREE_DISCUSSION:
                if state.mode == RoomMode.AUTONOMOUS:
                    with contextlib.suppress(Exception):
                        self.start_autonomous_discussion(room_id, max_turns=6)
                elif state.mode == RoomMode.DIRECT_CHAT:
                    with contextlib.suppress(Exception):
                        self.start_direct_reply(room_id, message)
            else:
                with contextlib.suppress(Exception):
                    self.start_protocol_background(room_id, message)

        return event

    def upload_room_attachment(
        self,
        room_id: str,
        filename: str,
        mime: str,
        content_base64: str,
    ) -> dict[str, Any]:
        """Persist one uploaded file for a room and register its metadata.

        No type or size gate by design (per product decision); the only hard
        rules are valid base64 and path-traversal safety via
        ``ensure_child_path``.  The returned dict is the transcript-facing
        attachment record (no ``stored_name`` leak).
        """

        state = self.get_room(room_id)
        if state is None:
            raise KeyError(room_id)
        raw_bytes = decode_base64_payload(content_base64)
        uploads_root = self.continuum.config.room_uploads_dir
        record = store_room_attachment(uploads_root, room_id, filename, mime, raw_bytes)
        full = {
            **record,
            "room_id": room_id,
            "url": attachment_public_url(record["id"]),
        }
        self._room_attachments[record["id"]] = full
        return self._public_attachment(full)

    def get_room_attachment(self, attachment_id: str) -> dict[str, Any] | None:
        full = self._room_attachments.get(attachment_id)
        if full is not None:
            return full
        return self._scan_room_attachment(attachment_id)

    def get_room_attachment_by_stored_name(
        self, room_id: str, stored_name: str
    ) -> dict[str, Any] | None:
        for full in self._room_attachments.values():
            if str(full.get("room_id")) == room_id and full.get("stored_name") == stored_name:
                return full
        return None

    def _scan_room_attachment(self, attachment_id: str) -> dict[str, Any] | None:
        """Recover an attachment record after a process restart.

        The in-memory registry is rebuilt lazily: transcript metadata and the
        upload directory are the authority, so a restart never orphans files.
        """

        uploads_root = self.continuum.config.room_uploads_dir
        if not uploads_root.exists():
            return None
        for room_dir in uploads_root.iterdir():
            if not room_dir.is_dir():
                continue
            for path in room_dir.iterdir():
                name = path.name
                if "_" not in name:
                    continue
                prefix, _, _original = name.partition("_")
                if len(prefix) != 32:
                    continue
                rows = self.continuum.database.conn.execute(
                    "SELECT metadata_json FROM room_transcripts "
                    "WHERE room_id = ? AND metadata_json LIKE ? LIMIT 20",
                    (room_dir.name, f"%{attachment_id}%"),
                ).fetchall()
                for row in rows:
                    try:
                        metadata = loads(str(row["metadata_json"]))
                    except Exception:
                        continue
                    for item in metadata.get("attachments") or []:
                        if (
                            isinstance(item, dict)
                            and item.get("id") == attachment_id
                            and item.get("stored_name") == name
                            and room_dir.name == str(item.get("room_id") or room_dir.name)
                        ):
                            full = {**item, "room_id": room_dir.name}
                            self._room_attachments[attachment_id] = full
                            return full
        return None

    @staticmethod
    def _public_attachment(full: dict[str, Any]) -> dict[str, Any]:
        return {
            key: full[key]
            for key in ("id", "filename", "mime", "size", "kind", "room_id", "url")
            if key in full
        }

    def _resolve_inject_attachments(
        self, room_id: str, attachment_ids: list[str]
    ) -> list[dict[str, Any]]:
        resolved: list[dict[str, Any]] = []
        for attachment_id in attachment_ids:
            full = self.get_room_attachment(str(attachment_id))
            if full is None or str(full.get("room_id")) != room_id:
                raise ValueError(f"attachment_not_found:{attachment_id}")
            resolved.append(self._public_attachment(full))
        return resolved

    @staticmethod
    def latest_user_attachments(
        transcript: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """Newest user message's attachments (text + files travel together)."""

        for item in reversed(transcript or []):
            if item.get("participant_id") != "user":
                continue
            attachments = item.get("attachments") or (item.get("metadata") or {}).get("attachments")
            if attachments:
                return [dict(a) for a in attachments if isinstance(a, dict)]
            if str(item.get("content") or "").strip():
                return []
        return []

    def _turn_attachments(self, state: RoomSessionState) -> list[dict[str, Any]]:
        """Canonical attachments for the current turn (metadata only).

        Returns the newest user message's attachments as ``AgentAttachment``
        dicts: id/kind/mime/filename/size + the store-local path.  No bytes
        are read here -- each adapter materialises the carrier (local path
        reference, native protocol item, inline base64) at its own boundary.
        Only the current turn's attachments travel; history keeps the
        transcript reference without re-sending binaries.
        """

        from persona_continuum.agent.models import AgentAttachment

        attachments = self.latest_user_attachments(state.transcript)
        canonical: list[dict[str, Any]] = []
        for item in attachments:
            full = self.get_room_attachment(str(item.get("id") or ""))
            if full is None:
                continue
            stored = str(full.get("stored_name") or "")
            if not stored:
                continue
            room_id = str(full.get("room_id") or state.id)
            record = AgentAttachment(
                id=str(item.get("id")),
                kind=str(item.get("kind") or "file"),
                mime_type=str(item.get("mime") or "application/octet-stream"),
                filename=str(item.get("filename") or "attachment"),
                size_bytes=int(item.get("size") or 0),
                local_path=str(self.continuum.config.room_uploads_dir / room_id / stored),
                url=str(item.get("url") or ""),
            )
            canonical.append(record.model_dump(mode="python"))
        return canonical

    def _turn_image_messages(
        self, state: RoomSessionState
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Legacy inline-image builder (kept for backward compatibility).

        The canonical path is now ``_turn_attachments`` + adapter-side
        carriers.  This helper is retained so existing callers/tests keep
        working; new code must not build data-URL messages in the Room layer.
        """

        from persona_continuum.room.attachments import attachment_supports_inline_vision

        attachments = self.latest_user_attachments(state.transcript)
        inline: list[dict[str, str]] = []
        for item in attachments:
            mime = str(item.get("mime") or "")
            if not attachment_supports_inline_vision(mime):
                continue
            full = self.get_room_attachment(str(item.get("id") or ""))
            stored = (full or {}).get("stored_name")
            if not full or not stored:
                continue
            try:
                path = (
                    self.continuum.config.room_uploads_dir
                    / str(full.get("room_id") or state.id)
                    / str(stored)
                )
                from persona_continuum.security.paths import ensure_child_path

                data = ensure_child_path(self.continuum.config.room_uploads_dir, path).read_bytes()
            except Exception:
                continue
            import base64

            inline.append(
                {
                    "media_type": mime.lower().split(";")[0].strip(),
                    "data": base64.b64encode(data).decode("ascii"),
                }
            )
        if not inline:
            return [], []
        content: list[dict[str, Any]] = [{"type": "text", "text": ""}]
        for image in inline:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{image['media_type']};base64,{image['data']}"},
                }
            )
        return [{"role": "user", "content": content}], inline

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
        direct_reply_task = self._direct_reply_tasks.get(room_id)
        protocol_task = self._protocol_tasks.get(room_id)
        autonomous_active = bool(autonomous_task and not autonomous_task.done())
        direct_reply_active = bool(direct_reply_task and not direct_reply_task.done())
        protocol_active = bool(protocol_task and not protocol_task.done())
        protocol_running = state.protocol_state.status == RoomRunStatus.RUNNING
        turn_active = (
            state.status == RoomStatus.DISCUSSING
            or call_status == "calling"
            or autonomous_active
            or direct_reply_active
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
            and not direct_reply_active
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
        if direct_reply_active and not target_ids and direct_reply_task is not None:
            direct_reply_task.cancel()

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

    async def _open_host_session(self, room_id: str, state: RoomSessionState) -> bool:
        """(Re)open the host agent session from the current binding snapshot.

        Shared by start_room, resume_from_storage and update_room_bindings:
        the host session is separate from participant sessions and must be
        rebuilt whenever the host binding changes, because the reasoning
        effort / model are consumed when the session opens.
        """
        host_slot = next(
            (
                slot
                for slot in state.participants
                if slot.participant_id == state.host_participant_id
            ),
            state.participants[0] if state.participants else None,
        )
        if host_slot is None or not self.runtime_executor:
            return False
        host_snapshot = state.binding_snapshots.get(host_slot.participant_id)
        host_adapter = (
            self.registry.get_adapter(host_snapshot.agent_runtime_id) if host_snapshot else None
        )
        if not (host_snapshot and host_adapter):
            return False
        stale_session = self._host_agent_sessions.pop(room_id, None)
        if stale_session is not None:
            with contextlib.suppress(Exception):
                stale_binding = self._host_agent_bindings.pop(room_id, None)
                if stale_binding and self.runtime_executor:
                    await self.runtime_executor.close(stale_binding)
                else:
                    await host_adapter.close(stale_session)
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
        return True

    async def update_room_bindings(
        self,
        room_id: str,
        bindings: dict[str, dict[str, Any]],
    ) -> RoomSessionState:
        """Re-resolve and apply explicit provider/model/reasoning bindings.

        Allowed only while the room is not generating (no protocol run, no
        autonomous discussion, no conversion in flight, no turn generating).
        Each entry replaces a slot's random pools with explicit selections,
        re-resolves the binding snapshot through the resolver (which validates
        runtime readiness, model membership and reasoning-effort support) and
        reopens any live agent session for that slot so the new binding takes
        effect on the next turn.
        """
        state = self.get_room(room_id)
        if state is None:
            raise KeyError(room_id)
        if room_id in self._converting_rooms:
            raise RoomBusyError(
                "room_converting",
                "房间正在进行协议迁移，请等待迁移完成后重试。",
            )
        if state.protocol_state.status.value == "running":
            raise RoomBusyError(
                "room_bindings_locked_during_run",
                "房间正在运行协议流程，不能修改模型绑定；请先停止当前运行。",
            )
        tasks_in_flight = [
            task
            for task in (
                self._protocol_tasks.get(room_id),
                self._autonomous_tasks.get(room_id),
                self._direct_reply_tasks.get(room_id),
            )
            if task is not None and not task.done()
        ]
        if tasks_in_flight or state.status == RoomStatus.DISCUSSING:
            raise RoomBusyError(
                "room_bindings_locked_during_run",
                "房间正在生成回合，不能修改模型绑定；请先暂停或停止房间。",
            )
        if not bindings:
            return state

        known = {slot.participant_id: slot for slot in state.participants}
        unknown = sorted(set(bindings) - known.keys())
        if unknown:
            raise BindingPreflightError(
                reason=f"房间中不存在以下席位: {', '.join(unknown)}",
                participant_id=unknown[0],
                code="participant_not_found",
            )

        # Serialize against step_turn so a live turn can never observe a
        # half-updated binding set.
        lock = self._room_locks.setdefault(room_id, asyncio.Lock())
        async with lock:
            probes = await self.discovery.scan(force_refresh=False)
            for participant_id, binding in bindings.items():
                slot = known[participant_id]
                runtime_id = str(binding.get("agent_runtime_id") or "").strip() or "default"
                model_id = str(binding.get("model_id") or "").strip() or "default"
                effort = str(binding.get("reasoning_effort") or "").strip() or "default"
                slot_data = slot.model_dump(mode="python")
                slot_data.update(
                    {
                        "runtime_selection": runtime_id,
                        "model_selection": model_id,
                        "reasoning_selection": effort,
                        "runtime_pool": [],
                        "model_pool": [],
                        "reasoning_pool": [],
                        "runtime_candidate_pool": [],
                        "model_candidate_pool": [],
                        "reasoning_candidate_pool": [],
                    }
                )
                edited = ParticipantSlot.model_validate(slot_data)
                # resolve_participant validates the runtime, the model
                # membership and the reasoning-effort support; failures raise
                # coded resolver errors before any state mutation.
                snapshot = await self.resolver.resolve_participant(edited, probes)
                slot.runtime_selection = runtime_id
                slot.model_selection = model_id
                slot.reasoning_selection = effort
                slot.runtime_pool = []
                slot.model_pool = []
                slot.reasoning_pool = []
                slot.runtime_candidate_pool = []
                slot.model_candidate_pool = []
                slot.reasoning_candidate_pool = []
                state.binding_snapshots[participant_id] = snapshot

            state.updated_at = datetime.now(UTC)
            self._save_room_state(state, force=True)

            # Reopen only sessions that are currently live; a room that has
            # not started yet simply picks the new snapshots up at
            # start/resume time.
            live_sessions = self._active_agent_sessions.get(room_id) or {}
            host_slot_id = next(
                (
                    slot.participant_id
                    for slot in state.participants
                    if slot.participant_id == state.host_participant_id
                ),
                state.participants[0].participant_id if state.participants else None,
            )
            reopened: list[str] = []
            for participant_id in bindings:
                if participant_id in live_sessions:
                    await self.restart_session(room_id, participant_id)
                    reopened.append(participant_id)
                if participant_id == host_slot_id and self._host_agent_sessions.get(room_id):
                    await self._open_host_session(room_id, state)
                    reopened.append(f"{participant_id}:host")

        await self._broadcast_event(
            room_id,
            {
                "event": "room_bindings_updated",
                "room_id": room_id,
                "binding_snapshots": {
                    key: value.model_dump(mode="json")
                    for key, value in state.binding_snapshots.items()
                },
                "reopened": reopened,
            },
        )
        return self.get_room(room_id) or state

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
        direct_reply_task = self._direct_reply_tasks.pop(room_id, None)
        if direct_reply_task and direct_reply_task is not asyncio.current_task():
            direct_reply_task.cancel()

        warmup_task = self._tool_warmup_tasks.pop(room_id, None)
        if warmup_task and not warmup_task.done():
            warmup_task.cancel()
        self._tool_preflight.pop(room_id, None)
        self._transport_capabilities.pop(room_id, None)
        for key in [k for k in self._turn_context_policies if k[0] == room_id]:
            self._turn_context_policies.pop(key, None)
        for key in [k for k in self._last_context_policy if k[0] == room_id]:
            self._last_context_policy.pop(key, None)

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
                    counterpart_id=self._runtime_counterpart(state, slot),
                    initial_relationship=slot.initial_relationship,
                    session_type="multi_agent_room",
                    room_id=room_id,
                )
                self._persona_session_ids[room_id][slot.participant_id] = new_sess.id

        await self._open_host_session(room_id, state)

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
    _NON_PATCHABLE_ROOM_FIELDS: frozenset[str] = frozenset({"protocol", "protocol_config"})

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

    def forget_room_runtime(self, room_id: str) -> None:
        """Drop in-process room handles after the SQL row is already gone."""
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
        direct_reply_task = self._direct_reply_tasks.pop(room_id, None)
        if direct_reply_task:
            direct_reply_task.cancel()
        summary_task = self._summary_tasks.pop(room_id, None)
        if summary_task and not summary_task.done():
            summary_task.cancel()
        # Episode rows are memory, not runtime: cancelling the task must never
        # delete them.  A cancelled consolidation stays PENDING and is resumed
        # by the next schedule, a restart, or the backfill sweep.
        episode_task = self._episode_tasks.pop(room_id, None)
        if episode_task and not episode_task.done():
            episode_task.cancel()
        warmup_task = self._tool_warmup_tasks.pop(room_id, None)
        if warmup_task and not warmup_task.done():
            warmup_task.cancel()
        self._tool_preflight.pop(room_id, None)
        self._transport_capabilities.pop(room_id, None)
        for key in [k for k in self._turn_context_policies if k[0] == room_id]:
            self._turn_context_policies.pop(key, None)
        for key in [k for k in self._last_context_policy if k[0] == room_id]:
            self._last_context_policy.pop(key, None)

    def delete_room(self, room_id: str) -> bool:
        self.continuum.database.conn.execute(
            "DELETE FROM room_scene_events WHERE room_id = ?", (room_id,)
        )
        self.continuum.database.conn.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
        self.continuum.database.conn.execute(
            "DELETE FROM room_transcripts WHERE room_id = ?", (room_id,)
        )
        self.continuum.database.conn.commit()
        # Episodes are persona memory and survive a room deletion, but the
        # shared-user provenance they recorded inside this room is gone.  Say so
        # explicitly instead of leaving it silently unresolvable.
        episodes = getattr(self.continuum, "episodes", None)
        if episodes is not None:
            with contextlib.suppress(Exception):
                episodes.mark_room_sources_unavailable(room_id)
        self.forget_room_runtime(room_id)
        return True

    async def shutdown(self) -> None:
        """Release every provider process this orchestrator started.

        Tool providers own child processes and stdio pipes; without this they
        would outlive the application.
        """

        for task in list(self._direct_reply_tasks.values()):
            if not task.done():
                task.cancel()
        self._direct_reply_tasks.clear()
        for task in list(self._episode_tasks.values()):
            if not task.done():
                task.cancel()
        self._episode_tasks.clear()
        for task in list(self._tool_warmup_tasks.values()):
            if not task.done():
                task.cancel()
        self._tool_warmup_tasks.clear()
        self._tool_preflight.clear()
        await self.tool_broker.shutdown()

    def shutdown_sync(self) -> None:
        """Best-effort teardown when no event loop is running."""

        for task in list(self._direct_reply_tasks.values()):
            if not task.done():
                task.cancel()
        self._direct_reply_tasks.clear()
        for task in list(self._episode_tasks.values()):
            if not task.done():
                task.cancel()
        self._episode_tasks.clear()
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
        cursor = conn.execute("DELETE FROM room_transcripts WHERE room_id = ?", (room_id,))
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

    def _should_refresh_room_summary(self, state: RoomSessionState) -> bool:
        """Whether this room needs a (re)built rolling summary right now.

        The first summary is produced as soon as dialogue leaves the recent
        window -- waiting for ``2 * raw_window`` turns left the prompt without a
        summary for the whole gap.  Afterwards it refreshes on the configured
        cadence.  A legacy or non-canonical stored value does not count as
        "already has one", so it gets replaced instead of trusted.

        The boundary is the SMALLEST window start in the room (token-budget
        based), so no committed turn can leave every participant's window
        without being folded into the shared summary.
        """

        stored = state.metadata.get("rolling_summary")
        has_summary = is_canonical_summary(
            stored, state.metadata.get("rolling_summary_version")
        )
        return self.context_manager.should_update_summary(
            state.turn_index,
            transcript_len=len(state.transcript or []),
            has_summary=has_summary,
            eviction_boundary=self._room_summary_boundary(state),
        )

    def _schedule_room_summary(self, state: RoomSessionState, *, persistent: bool) -> None:
        """Queue a background rolling-summary refresh (never blocks a turn).

        Persistent-thread rooms already own exact history in their threads and
        never need this model call.  At most one refresh per room is in
        flight; a next turn that starts before the refresh lands simply keeps
        using the previous summary plus its recent raw turns.
        """
        if persistent:
            return
        if not self._should_refresh_room_summary(state):
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
        if not self._should_refresh_room_summary(state):
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
            # Last check before spending a full model pass: if a turn went live
            # while we were queuing, back off rather than compete for the GPU.
            latest = self.get_room(state.id)
            if latest is not None and latest.status == RoomStatus.DISCUSSING:
                raise RuntimeError("room busy; deferring summary refresh")
            result = await executor.execute_structured(
                host_binding,
                system_prompt=SUMMARY_PROMPT_INTRO,
                user_message=json.dumps(payload, ensure_ascii=False),
                schema=schema,
                phase="room_summary_refresh",
            )
            return result.value

        # Hold the single-flight lock for the whole summary pass so a live turn
        # cannot start its own prefill mid-summary.
        async with self._summary_inference_lock:
            # Re-read the room INSIDE the lock: the snapshot this task was
            # created with may be many turns old (the call takes minutes, and a
            # turn preempts it), and folding a stale eviction boundary would
            # summarise the wrong slice and then overwrite newer state.
            latest = self.get_room(state.id)
            if latest is None or latest.status == RoomStatus.DISCUSSING:
                return
            previous = latest.metadata.get("rolling_summary")
            checkpoint = latest.metadata.get("rolling_summary_through_message_index")
            summary_profile = self._room_summary_profile(latest)
            try:
                summary, new_checkpoint, summary_report = (
                    await self.context_manager.update_summary(
                        transcript=list(latest.transcript or []),
                        previous_summary=previous if isinstance(previous, str) else None,
                        summarize=_summarize,
                        checkpoint=(
                            int(checkpoint) if isinstance(checkpoint, int) else None
                        ),
                        # The eviction boundary is the smallest window start in
                        # the room; the summary caps belong to the summariser
                        # (the host agent), not to whoever spoke last.
                        eviction_boundary=self._room_summary_boundary(latest),
                        summary_max_chars=(
                            summary_profile.summary_max_chars if summary_profile else None
                        ),
                        summary_output_max_tokens=(
                            summary_profile.summary_output_max_tokens
                            if summary_profile
                            else None
                        ),
                        summary_input_max_tokens=(
                            summary_profile.summary_input_max_tokens
                            if summary_profile
                            else None
                        ),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                summary_report = {"reason": f"update_summary_failed:{type(exc).__name__}"}
                summary, new_checkpoint = None, None
            summary_report.update(
                {
                    "event": "room_summary_report",
                    "room_id": state.id,
                    "turn_index": latest.turn_index,
                }
            )
            _metadata_trace_logger.warning(
                "room_summary_report %s",
                json.dumps(
                    {k: v for k, v in summary_report.items() if k != "event"},
                    ensure_ascii=False,
                ),
            )
            await self._broadcast_event(state.id, summary_report)
            # Persist inside the lock: writing after releasing it lets a turn's
            # own state save land in between and drop the freshly built summary
            # (observed: a valid v2 summary disappeared at the next turn).
            if summary:
                # A background summary must not overwrite newer dialogue or scene time.
                latest = self.get_room(state.id)
                if latest is not None:
                    latest.metadata["rolling_summary"] = summary
                    latest.metadata["rolling_summary_version"] = CANONICAL_SUMMARY_VERSION
                    # Stamp the write so the merge in _save_room_state can tell a
                    # fresh summary from one an older snapshot is reverting.
                    latest.metadata["rolling_summary_updated_at"] = datetime.now(
                        UTC
                    ).isoformat()
                    if isinstance(new_checkpoint, int):
                        latest.metadata[
                            "rolling_summary_through_message_index"
                        ] = new_checkpoint
                    self._save_room_state(latest, force=True)

    # --- Memory Episodes (Phase 3) ------------------------------------------

    @staticmethod
    def _shared_user_turn(state: RoomSessionState) -> dict[str, Any] | None:
        """The room-level user message this turn is answering, if any.

        In a multi-persona room the user speaks ONCE and several personas may
        answer.  That single raw turn must be reachable from every persona's
        Episode instead of being invisible to all of them -- without copying it
        per persona.  The newest user entry is the shared trigger until the user
        speaks again.
        """

        for entry in reversed(state.transcript or []):
            if not isinstance(entry, dict):
                continue
            if str(entry.get("participant_id") or "") != "user":
                continue
            turn_id = str(entry.get("turn_id") or "").strip()
            if not turn_id:
                continue
            created = entry.get("scene_time") or entry.get("created_at")
            occurred = parse_dt(str(created)) if created else None
            return {
                "turn_id": turn_id,
                "text": str(entry.get("content") or ""),
                "occurred_at": occurred,
            }
        return None

    def _episode_summariser_binding(self, state: RoomSessionState) -> Any:
        """A model binding that may summarise this room's Episodes.

        The host agent is the neutral summariser and is preferred.  A
        direct-chat room has no host, so the single participant's binding is
        used instead -- the same persona that lived the episode is the one
        asking itself what happened, which is exactly what a person does.
        """

        host_binding = self._host_agent_bindings.get(state.id)
        if host_binding is not None:
            return host_binding
        bindings = self._active_agent_bindings.get(state.id, {})
        for participant_id in sorted(bindings):
            return bindings[participant_id]
        return None

    def _memory_backlog_pending(self, room_id: str) -> bool:
        """Whether ANY memory layer still owes work for this room.

        Opening a room is the natural moment to work through a backlog that was
        recorded in an earlier process, and the backlog is now three layers
        deep: Episodes, Facts, and Active Threads.  Checking only Episodes would
        leave already-summarised Episodes owing facts/threads untouched forever.
        """

        episodes = getattr(self.continuum, "episodes", None)
        facts = getattr(self.continuum, "facts", None)
        threads = getattr(self.continuum, "threads", None)
        if episodes is not None and episodes.pending_episodes(room_id=room_id, limit=1):
            return True
        if facts is not None and facts.pending_extraction_episodes(room_id=room_id, limit=1):
            return True
        return bool(
            threads is not None
            and threads.pending_resolution_episodes(room_id=room_id, limit=1)
        )

    def _schedule_episode_consolidation(
        self, state: RoomSessionState, commit_res: dict[str, Any] | None = None
    ) -> None:
        """Queue Episode summarisation for this room.  Never blocks the reply.

        The turn is already committed and the Episode row already exists, so
        this only decides whether to *describe* what was just recorded.  A
        failure here cannot lose anything: the Episode keeps
        ``pending_consolidation`` and the next schedule (or a restart, or the
        backfill sweep) retries it.

        Called with ``commit_res=None`` when a room is reopened: an old room can
        owe summaries for episodes recorded in an earlier process, and opening
        it is the natural moment to work through them.
        """

        config = getattr(self.continuum, "config", None)
        if not bool(getattr(config, "memory_episode_consolidation_enabled", True)):
            return
        episodes = getattr(self.continuum, "episodes", None)
        if episodes is None or self.runtime_executor is None:
            return
        if commit_res is not None:
            report = commit_res.get("episode") if isinstance(commit_res, dict) else None
            if not isinstance(report, dict) or not report.get("current_episode_id"):
                return
        elif not self._memory_backlog_pending(state.id):
            return
        existing = self._episode_tasks.get(state.id)
        if existing is not None and not existing.done():
            return
        self._episode_tasks[state.id] = asyncio.create_task(
            self._maybe_consolidate_episodes(state),
            name=f"room-episodes-{state.id}",
        )

    async def _maybe_consolidate_episodes(self, state: RoomSessionState) -> None:
        """One bounded background pass over this room's memory layer.

        Two derived layers are produced here, in order, each a separate model
        call: the Episode summary (Phase 3) and then Semantic Facts (Phase 4).
        Both are one-inference-at-a-time on purpose -- a memory-bound local
        runtime must never see two concurrent prefills.
        """

        episodes = getattr(self.continuum, "episodes", None)
        if episodes is None or self.runtime_executor is None:
            return
        binding = self._episode_summariser_binding(state)
        if binding is None:
            return
        executor = self.runtime_executor
        config = getattr(self.continuum, "config", None)
        batch = max(1, int(getattr(config, "memory_episode_consolidation_batch", 3) or 3))

        async def _summarize(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
            from persona_continuum.application.episode_service import EPISODE_SUMMARY_INTRO

            result = await executor.execute_structured(
                binding,
                system_prompt=EPISODE_SUMMARY_INTRO,
                user_message=json.dumps(payload, ensure_ascii=False),
                schema=schema,
                phase="memory_episode_consolidation",
            )
            return result.value

        for episode in episodes.pending_episodes(room_id=state.id, limit=batch):
            # Same rule as the rolling summary: never compete with a live turn
            # for the model.  On a memory-bound local runtime two concurrent
            # prefills are exactly the failure mode this must avoid.
            latest = self.get_room(state.id)
            if latest is not None and latest.status == RoomStatus.DISCUSSING:
                return
            async with self._summary_inference_lock:
                try:
                    report = await episodes.consolidate_episode(
                        episode.id,
                        summarize=_summarize,
                        allow_open=episode.status is EpisodeStatus.OPEN,
                    )
                except Exception as exc:  # noqa: BLE001 - background work
                    report = {
                        "episode_id": episode.id,
                        "action": "noop",
                        "error": f"consolidation_failed:{type(exc).__name__}",
                        "pending": True,
                    }
            report.update(
                {
                    "event": "memory_consolidation_report",
                    "room_id": state.id,
                    "turn_index": state.turn_index,
                    "episode_status": str(
                        getattr(episodes.get_episode(episode.id), "status", "")
                    ),
                }
            )
            _metadata_trace_logger.warning(
                "memory_consolidation_report %s",
                json.dumps(
                    {k: v for k, v in report.items() if k != "event"},
                    ensure_ascii=False,
                ),
            )
            await self._broadcast_event(state.id, report)

        # Phase 4: distill Semantic Facts from the same raw sources.  This runs
        # even when the summary is still pending -- facts come from the
        # transcript, not from a summary of it.
        facts = getattr(self.continuum, "facts", None)
        if facts is None or not bool(
            getattr(config, "memory_fact_extraction_enabled", True)
        ):
            return
        fact_batch = max(1, int(getattr(config, "memory_fact_extraction_batch", 3) or 3))

        async def _extract(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
            from persona_continuum.application.fact_service import FACT_EXTRACTION_INTRO

            result = await executor.execute_structured(
                binding,
                system_prompt=FACT_EXTRACTION_INTRO,
                user_message=json.dumps(payload, ensure_ascii=False),
                schema=schema,
                phase="memory_fact_extraction",
            )
            return result.value

        for episode in facts.pending_extraction_episodes(room_id=state.id, limit=fact_batch):
            latest = self.get_room(state.id)
            if latest is not None and latest.status == RoomStatus.DISCUSSING:
                return
            async with self._summary_inference_lock:
                try:
                    fact_report = await facts.extract_episode_facts(
                        episode.id, extract=_extract
                    )
                except Exception as exc:  # noqa: BLE001 - background work
                    fact_report = {
                        "episode_id": episode.id,
                        "action": "noop",
                        "error": f"fact_extraction_failed:{type(exc).__name__}",
                        "pending": True,
                    }
            fact_report.update(
                {
                    "event": "memory_fact_report",
                    "room_id": state.id,
                    "turn_index": state.turn_index,
                }
            )
            _metadata_trace_logger.warning(
                "memory_fact_report %s",
                json.dumps(
                    {k: v for k, v in fact_report.items() if k != "event"},
                    ensure_ascii=False,
                ),
            )
            await self._broadcast_event(state.id, fact_report)

        # Phase 5: resolve what is still in flight.  Runs LAST because it reads
        # both the Episode's raw sources and the Facts just distilled from them.
        # Like the layers above it, it never blocks a reply: a failure leaves the
        # Episode's durable status pending and the next pass retries it.
        threads = getattr(self.continuum, "threads", None)
        if threads is None or not bool(
            getattr(config, "memory_thread_resolution_enabled", True)
        ):
            return
        thread_batch = max(
            1, int(getattr(config, "memory_thread_resolution_batch", 2) or 2)
        )

        async def _resolve_threads(payload: dict[str, Any], schema: dict[str, Any]) -> Any:
            from persona_continuum.application.thread_service import (
                THREAD_RESOLUTION_INTRO,
            )

            result = await executor.execute_structured(
                binding,
                system_prompt=THREAD_RESOLUTION_INTRO,
                user_message=json.dumps(payload, ensure_ascii=False),
                schema=schema,
                phase="memory_thread_resolution",
            )
            return result.value

        for episode in threads.pending_resolution_episodes(
            room_id=state.id, limit=thread_batch
        ):
            latest = self.get_room(state.id)
            if latest is not None and latest.status == RoomStatus.DISCUSSING:
                return
            async with self._summary_inference_lock:
                try:
                    thread_report = await threads.resolve_episode_threads(
                        episode.id, resolve=_resolve_threads
                    )
                except Exception as exc:  # noqa: BLE001 - background work
                    thread_report = {
                        "episode_id": episode.id,
                        "action": "noop",
                        "error": f"thread_resolution_failed:{type(exc).__name__}",
                        "pending": True,
                    }
            thread_report.update(
                {
                    "event": "memory_thread_report",
                    "room_id": state.id,
                    "turn_index": state.turn_index,
                }
            )
            _metadata_trace_logger.warning(
                "memory_thread_report %s",
                json.dumps(
                    {k: v for k, v in thread_report.items() if k != "event"},
                    ensure_ascii=False,
                ),
            )
            await self._broadcast_event(state.id, thread_report)
        # Silence downgrades a thread to STALE and never resolves it, so this
        # sweep is a bounded, model-free UPDATE and not a judgment call.
        threads.sweep_stale(limit=50)

        # Phase 6: fold the Episodes just resolved into Chapters, and work the
        # Chapter / Long-term backlog.  Grouping is deterministic and cheap;
        # only the consolidation below spends a model call, and only on a
        # summary that is actually owed.
        hierarchies = getattr(self.continuum, "hierarchies", None)
        if hierarchies is None or not bool(
            getattr(config, "hierarchy_summary_enabled", True)
        ):
            return
        # Grouping only touches Episodes this room owns, and only those no
        # Chapter owns yet: a reopened room works its own backlog instead of
        # re-scanning the database.
        scopes: set[tuple[str, str, str]] = set()
        for source_episode in episodes.list_episodes(
            room_id=state.id, limit=max(8, batch * 8)
        ):
            scopes.add(
                (
                    source_episode.persona_id,
                    source_episode.counterpart_id,
                    source_episode.branch_id,
                )
            )
            hierarchies.assign_episode(source_episode)
        for persona_id, counterpart_id, branch_id in sorted(scopes):
            hierarchies.plan_long_term(
                persona_id=persona_id,
                counterpart_id=counterpart_id,
                branch_id=branch_id,
            )

        async def _summarize_hierarchy(
            payload: dict[str, Any], schema: dict[str, Any]
        ) -> Any:
            from persona_continuum.application.hierarchy_service import (
                CHAPTER_SUMMARY_INTRO,
                LONG_TERM_SUMMARY_INTRO,
            )

            intro = (
                LONG_TERM_SUMMARY_INTRO
                if int((payload.get("summary") or {}).get("level") or 1) > 1
                else CHAPTER_SUMMARY_INTRO
            )
            result = await executor.execute_structured(
                binding,
                system_prompt=intro,
                user_message=json.dumps(payload, ensure_ascii=False),
                schema=schema,
                phase="memory_hierarchy_summary",
            )
            return result.value

        hierarchy_batch = max(
            1, int(getattr(config, "hierarchy_consolidation_batch", 1) or 1)
        )
        for summary in hierarchies.pending_summaries(room_id=state.id, limit=hierarchy_batch):
            latest = self.get_room(state.id)
            if latest is not None and latest.status == RoomStatus.DISCUSSING:
                return
            async with self._summary_inference_lock:
                try:
                    hierarchy_report = await hierarchies.consolidate_summary(
                        summary.id, summarize=_summarize_hierarchy
                    )
                except Exception as exc:  # noqa: BLE001 - background work
                    hierarchy_report = {
                        "summary_id": summary.id,
                        "action": "noop",
                        "error": f"hierarchy_summary_failed:{type(exc).__name__}",
                        "pending": True,
                    }
            hierarchy_report.update(
                {
                    "event": "memory_hierarchy_report",
                    "room_id": state.id,
                    "turn_index": state.turn_index,
                }
            )
            _metadata_trace_logger.warning(
                "memory_hierarchy_report %s",
                json.dumps(
                    {k: v for k, v in hierarchy_report.items() if k != "event"},
                    ensure_ascii=False,
                ),
            )
            await self._broadcast_event(state.id, hierarchy_report)

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

    def _read_room_metadata(self, room_id: str) -> dict[str, Any]:
        row = self.continuum.database.conn.execute(
            "SELECT state_json FROM rooms WHERE id = ?", (room_id,)
        ).fetchone()
        if not row:
            return {}
        try:
            metadata = (loads(row["state_json"]) or {}).get("metadata")
        except Exception:
            return {}
        return dict(metadata) if isinstance(metadata, dict) else {}

    def _merge_protected_room_metadata(self, state: RoomSessionState) -> None:
        """Fold asynchronous writers' metadata keys back into this snapshot.

        The database row is the authority for keys owned by background writers
        (the rolling summary, migration archives).  A snapshot taken before one
        of those writes must not revert it, so every protected key this snapshot
        does not carry -- or carries an older revision of -- is adopted from the
        stored row.  This is a field-level merge, never a whole-object replace.
        """

        current_metadata = self._read_room_metadata(state.id)
        if not current_metadata:
            return
        incoming = state.metadata if isinstance(state.metadata, dict) else {}
        protected = _protected_metadata_keys(current_metadata)
        if not protected:
            return

        current_stamp = str(current_metadata.get("rolling_summary_updated_at") or "")
        incoming_stamp = str(incoming.get("rolling_summary_updated_at") or "")

        for key in protected:
            db_value = current_metadata[key]
            if key not in incoming:
                # This snapshot predates the writer that owns the key.
                incoming[key] = db_value
                continue
            if incoming[key] == db_value:
                continue
            # Both sides carry a value and they differ: the newer stamp wins,
            # and a missing stamp counts as older.
            if current_stamp > incoming_stamp:
                incoming[key] = db_value
        state.metadata = incoming

    def _trace_metadata_write(
        self,
        state: RoomSessionState,
        before: dict[str, Any],
        *,
        writer: str,
    ) -> None:
        """Temporary observability for room-metadata writers (debug only).

        Logs key sets and a (version, length) fingerprint of the rolling
        summary -- never the summary body.  A version going to None is logged at
        ERROR because that is a lost-update regression.
        """

        if not _METADATA_WRITE_TRACE:
            return
        after = state.metadata if isinstance(state.metadata, dict) else {}
        b_ver, b_len = _summary_fingerprint(before)
        a_ver, a_len = _summary_fingerprint(after)
        keys_before = sorted(_protected_metadata_keys(before))
        keys_after = sorted(_protected_metadata_keys(after))
        if b_ver == a_ver and b_len == a_len and keys_before == keys_after:
            return
        _metadata_trace_logger.warning(
            "room_metadata_write writer=%s room=%s turn_index=%s "
            "summary_version=%r->%r summary_len=%s->%s protected_keys=%s->%s",
            writer,
            state.id,
            getattr(state, "turn_index", None),
            b_ver,
            a_ver,
            b_len,
            a_len,
            keys_before,
            keys_after,
        )
        if b_ver is not None and a_ver is None:
            _metadata_trace_logger.error(
                "room_metadata_write LOST SUMMARY writer=%s room=%s turn_index=%s "
                "(rolling_summary_version %r -> None)",
                writer,
                state.id,
                getattr(state, "turn_index", None),
                b_ver,
            )

    def _save_room_state(self, state: RoomSessionState, *, force: bool = False) -> None:
        # Merge-safe: adopt any protected key a background writer has stored
        # since this snapshot was taken, BEFORE it reaches the cache or the row.
        self._merge_protected_room_metadata(state)
        # Always keep the in-process mirror authoritative for reads.
        try:
            self._room_state_cache[state.id] = state.model_copy(deep=True)
        except Exception:
            self._room_state_cache[state.id] = state
        try:
            now_monotonic = asyncio.get_running_loop().time()
        except RuntimeError:
            now_monotonic = 0.0
            self._persist_room_state(state, writer="save_room_state_sync", merged=True)
            return
        last = self._room_state_last_commit.get(state.id, 0.0)
        if (
            not force
            and self._debounce_seconds > 0
            and now_monotonic - last < self._debounce_seconds
        ):
            self._room_state_dirty.add(state.id)
            return
        self._persist_room_state(state, writer="save_room_state", merged=True)
        self._room_state_last_commit[state.id] = now_monotonic
        self._room_state_dirty.discard(state.id)

    def flush_room_state(self, room_id: str) -> None:
        """Force a durable write for one room (terminal transitions)."""

        cached = self._room_state_cache.get(room_id)
        if cached is None:
            return
        self._merge_protected_room_metadata(cached)
        self._persist_room_state(cached, writer="flush_room_state", merged=True)
        self._room_state_dirty.discard(room_id)
        with contextlib.suppress(RuntimeError):
            self._room_state_last_commit[room_id] = asyncio.get_running_loop().time()

    def _persist_room_state(
        self,
        state: RoomSessionState,
        *,
        writer: str = "unknown",
        merged: bool = False,
    ) -> None:
        if not merged:
            # Direct callers must get the same protection as _save_room_state.
            before = self._read_room_metadata(state.id)
            self._merge_protected_room_metadata(state)
            self._trace_metadata_write(state, before, writer=writer)
        elif _METADATA_WRITE_TRACE:
            self._trace_metadata_write(
                state, self._read_room_metadata(state.id), writer=writer
            )
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
        known_turn_ids = {str(t.turn_id) for t in records}
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
                **(t.metadata.get("channels") or {}),
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
        channels = record.metadata.get("channels") or {}
        for event in channels.get("scene_events", []):
            if not event.get("id"):
                continue
            self.continuum.database.conn.execute(
                "INSERT OR IGNORE INTO room_scene_events "
                "(id, room_id, source_turn_id, event_json) VALUES (?, ?, ?, ?)",
                (event["id"], record.room_id, record.turn_id, dumps(redact_secrets(event))),
            )
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
