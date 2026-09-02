from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from persona_continuum.application._utils import new_id
from persona_continuum.room.case_state import (
    MAX_BLOCKING_CLARIFICATIONS,
    RoomCaseState,
    merge_from_host_analysis,
    violates_output_input_boundary,
)
from persona_continuum.room.context_packer import (
    compact_transcript,
    dedupe_public_messages,
)
from persona_continuum.room.models import (
    CrossReview,
    ParticipantSlot,
    ProtocolTask,
    ProtocolTaskStatus,
    RoomProtocolEvent,
    RoomProtocolState,
    RoomProtocolType,
    RoomRunStatus,
    RoomSessionState,
    RoomVote,
    StructuredSubmission,
)
from persona_continuum.room.protocols import (
    ProtocolDefinition,
    ProtocolRegistry,
    ProtocolStageDefinition,
)
from persona_continuum.room.repository import RoomProtocolRepository


class ProtocolActionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    room_id: str
    run_id: str
    task_id: str
    protocol: RoomProtocolType
    stage: str
    action: str
    participant: ParticipantSlot
    question: str
    persona_context: dict[str, Any] = Field(default_factory=dict)
    room_context: dict[str, Any] = Field(default_factory=dict)
    role_context: dict[str, Any] = Field(default_factory=dict)
    task_context: dict[str, Any] = Field(default_factory=dict)
    conversation_context: list[dict[str, Any]] = Field(default_factory=list)
    peer_results: list[dict[str, Any]] = Field(default_factory=list)
    tool_permissions: list[str] = Field(default_factory=list)


ActionExecutor = Callable[[ProtocolActionRequest], Awaitable[dict[str, Any]]]
EventEmitter = Callable[[dict[str, Any]], Awaitable[None]]


# --- role duties ----------------------------------------------------------
#
# These are injected per stage instead of living in the persona material so
# that a persona trained on "check the input package / routing conditions
# first" (a V3 legacy habit) cannot drag the room back into that pattern.
# They are deliberately short: a longer system prompt is not the fix.

HOST_ANALYSIS_DUTY = (
    "你是本次会诊的主持人。你的职责是替用户减少负担，不是把专家缺的信息汇总给用户。\n"
    "判断顺序：1) 哪些信息真的缺且不可替代；2) 哪些你可以自己算；"
    "3) 哪些可以合理默认（例如用户说“最近”默认未来3-6个月，并在结论中说明）；"
    "4) 哪些只是可选细化（先给完整答案，末尾再提一句）。\n"
    "如果已有信息足够，直接 can_proceed=true 进入路由，不得先列半页缺失清单。\n"
    "用户询问的日期、数字、方位、条件、时间窗口属于你要交付的 OUTPUT，"
    "不是必需的 INPUT，禁止以“没有提供日期所以无法给日期”为由阻塞。\n"
    "整个咨询过程最多只能向用户提 1 次阻塞式追问，且必须一次性合并所有真正不可替代的问题。"
)

EXPERT_ANALYSIS_DUTY = (
    "你是独立分析的专家。你必须自己完成专业计算、推演与判读，"
    "不得要求用户补充本应由你计算出的日期、数字、方位或条件。\n"
    "不得向用户发起新的阻塞式追问；只有主持人可以。\n"
    "信息不足时：采用合理默认并明确写出你的默认假设，然后给出完整结论。\n"
    "禁止以接口、路由、启动条件、输入包、交接格式、数据结构、谁先处理为主题输出内容；"
    "这些属于内部编排，用户看不到也不需要看到。\n"
    "独立分析阶段不得参考其他专家结论。"
)

SYNTHESIS_DUTY = (
    "你是主持人，正在给出本轮会诊的最终答复。不要写成流水账，"
    "也不要复述每位专家的完整回答。\n"
    "必须给出：总体判断、倾向性结论（扩张/保守/小额试探/暂缓）、风险、"
    "时间窗口、可执行建议。\n"
    "final_judgment 必须明确果断，禁止“各有道理，建议综合考虑”这类模糊表述。"
)


def _clip_text(value: Any, limit: int = 1200) -> Any:
    """Clip a protocol artifact before it enters a prompt."""

    if value is None:
        return None
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, dict):
        clipped: dict[str, Any] = {}
        used = 0
        for key, item in value.items():
            encoded = len(str(item).encode("utf-8"))
            if used + encoded > limit:
                break
            clipped[key] = item
            used += encoded
        return clipped
    return value


# Public stage → transcript message_kind. Only actions whose structured output
# is user-facing public content produce chat messages; internal bookkeeping
# stages (create_tasks, barrier, vote bookkeeping) never touch the transcript.
PROTOCOL_MESSAGE_KINDS: dict[str, str] = {
    "host_analysis": "protocol_host_analysis",
    "routing": "protocol_routing",
    "analysis": "protocol_analysis",
    "review": "protocol_review",
    "rebuttal": "protocol_rebuttal",
    "synthesis": "protocol_synthesis",
    "finalize": "protocol_final",
}


def render_public_message(
    action: str,
    output: dict[str, Any],
    name_map: dict[str, str] | None = None,
) -> str:
    """Render the model's public structured output as chat text.

    Only explicitly returned public fields are used; hidden reasoning or
    scratchpad content never reaches the transcript.
    """
    allowed = {
        "analysis": {
            "summary",
            "key_findings",
            "evidence",
            "uncertainties",
            "recommendation",
            "confidence",
        },
        "review": {"agree", "disagree", "concerns", "changed_position", "updated_conclusion"},
        "routing": {"selected_participant_ids", "selected_expert_ids", "routing_reason"},
        "synthesis": {
            "summary",
            "participant_positions",
            "consensus",
            "conflicts",
            "final_judgment",
            "uncertainties",
            "recommendations",
        },
    }.get(action, {"summary", "content"})
    public = {key: value for key, value in output.items() if key in allowed}
    if action == "routing":
        selected = public.get("selected_participant_ids") or public.get(
            "selected_expert_ids"
        ) or []
        names = [
            (name_map or {}).get(str(item), str(item))
            for item in selected
            if str(item).strip()
        ]
        lines = []
        if names:
            lines.append("本轮选择：" + "、".join(names))
        reason = str(public.get("routing_reason") or "").strip()
        if reason:
            lines.append(reason)
        return "\n".join(lines)
    if action == "review":
        review_lines: list[str] = []
        for label, key in (("同意", "agree"), ("异议", "disagree"), ("关注点", "concerns")):
            values = [str(item) for item in (public.get(key) or []) if str(item).strip()]
            if values:
                review_lines.append(f"{label}：" + "；".join(values))
        if public.get("changed_position") and public.get("updated_conclusion"):
            review_lines.append(f"更新结论：{public['updated_conclusion']}")
        return "\n".join(review_lines)

    parts: list[str] = []
    summary = str(public.get("summary") or public.get("content") or "").strip()
    if summary:
        parts.append(summary)
    if action == "analysis":
        findings = [str(item) for item in (public.get("key_findings") or []) if str(item).strip()]
        if findings:
            parts.append("关键依据：\n" + "\n".join(f"- {item}" for item in findings))
        recommendation = str(public.get("recommendation") or "").strip()
        if recommendation:
            parts.append(f"建议：{recommendation}")
    if action == "synthesis":
        for label, key in (
            ("共识", "consensus"),
            ("冲突", "conflicts"),
            ("建议", "recommendations"),
        ):
            values = [str(item) for item in (public.get(key) or []) if str(item).strip()]
            if values:
                parts.append(f"{label}：" + "；".join(values))
        judgment = str(public.get("final_judgment") or "").strip()
        if judgment:
            parts.append(f"最终研判：{judgment}")
    return "\n\n".join(part for part in parts if part)


class RoomProtocolRuntime:
    """Deterministic protocol state machine over the existing Agent execution seam."""

    def __init__(
        self,
        repository: RoomProtocolRepository,
        *,
        registry: ProtocolRegistry | None = None,
        event_emitter: EventEmitter | None = None,
        transcript_recorder: EventEmitter | None = None,
    ) -> None:
        self.repository = repository
        self.registry = registry or ProtocolRegistry()
        self.event_emitter = event_emitter
        self.transcript_recorder = transcript_recorder
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._run_tasks: dict[str, asyncio.Task[RoomProtocolState]] = {}
        self._shared_context_cache: dict[str, tuple[str, dict[str, Any]]] = {}

    def start(
        self,
        room: RoomSessionState,
        question: str,
        executor: ActionExecutor,
    ) -> asyncio.Task[RoomProtocolState]:
        existing = self._run_tasks.get(room.id)
        if existing and not existing.done():
            raise ValueError("room_protocol_run_already_active")
        task = asyncio.create_task(self.run(room, question, executor))
        self._run_tasks[room.id] = task
        task.add_done_callback(lambda completed: self._clear_task(room.id, completed))
        return task

    def _clear_task(self, room_id: str, task: asyncio.Task[RoomProtocolState]) -> None:
        if self._run_tasks.get(room_id) is task:
            self._run_tasks.pop(room_id, None)

    async def cancel(self, room_id: str) -> bool:
        event = self._cancel_events.get(room_id)
        task = self._run_tasks.get(room_id)
        if event is None and (task is None or task.done()):
            return False
        if event is not None:
            event.set()
        if task and not task.done():
            task.cancel()
        return True

    async def force_finalize(
        self,
        room: RoomSessionState,
        question: str,
        executor: ActionExecutor,
    ) -> RoomProtocolState:
        previous = self.repository.latest_run(room.id) or room.protocol_state
        definition = self.registry.get(room.protocol, room.protocol_config)
        final_stage = next(
            (
                stage
                for stage in reversed(definition.stages)
                if stage.action in {"synthesis", "finalize"} and stage.allowed_roles
            ),
            None,
        )
        if final_stage is None:
            raise ValueError("protocol_has_no_finalizer")
        run_id = new_id("roomrun")
        root_task_id = new_id("roomtask")
        state = RoomProtocolState(
            run_id=run_id,
            root_task_id=root_task_id,
            status=RoomRunStatus.RUNNING,
            current_stage=final_stage.id,
            submissions=list(previous.submissions),
            reviews=list(previous.reviews),
            votes=list(previous.votes),
            selected_expert_ids=list(previous.selected_expert_ids),
            started_at=datetime.now(UTC),
            source_run_id=previous.run_id,
        )
        self.repository.save_run(room_id=room.id, protocol=room.protocol, state=state)
        root_task = ProtocolTask(
            id=root_task_id,
            run_id=run_id,
            stage="protocol_finalize",
            task_type="room_run",
            status=ProtocolTaskStatus.RUNNING,
            input={"question": question, "source_run_id": previous.run_id},
            started_at=state.started_at,
        )
        self.repository.save_task(room.id, root_task)
        await self._event(
            room,
            state,
            "host_synthesis_started",
            stage=final_stage.id,
            metadata={"source_run_id": previous.run_id},
        )
        results = await self._execute_stage(
            room=room,
            state=state,
            definition=definition,
            stage=final_stage,
            question=question,
            executor=executor,
        )
        state.status = (
            RoomRunStatus.SUCCESS
            if any(success for success, _ in results)
            else RoomRunStatus.FAILED
        )
        state.finished_at = datetime.now(UTC)
        state.current_stage = "waiting_user"
        state.active_participant_ids = []
        root_task.status = (
            ProtocolTaskStatus.SUCCESS
            if state.status == RoomRunStatus.SUCCESS
            else ProtocolTaskStatus.FAILED
        )
        root_task.output = {"run_status": state.status.value}
        root_task.finished_at = state.finished_at
        self.repository.save_task(room.id, root_task)
        self.repository.save_run(room_id=room.id, protocol=room.protocol, state=state)
        await self._record_public_message(
            room,
            state,
            action="finalize",
            stage_id="final",
            participant=None,
            task_id=root_task_id,
            output=state.final_result or {},
        )
        await self._event(
            room,
            state,
            "final_response",
            stage="final",
            status=state.status.value,
            metadata=state.final_result or {},
        )
        room.protocol_state = state
        return state

    async def run(
        self,
        room: RoomSessionState,
        question: str,
        executor: ActionExecutor,
    ) -> RoomProtocolState:
        definition = self.registry.get(room.protocol, room.protocol_config)
        self.registry.validate_participants(definition, room.participants)
        run_id = new_id("roomrun")
        root_task_id = new_id("roomtask")
        cancel_event = asyncio.Event()
        self._cancel_events[room.id] = cancel_event
        state = RoomProtocolState(
            run_id=run_id,
            root_task_id=root_task_id,
            status=RoomRunStatus.RUNNING,
            current_stage=definition.initial_stage,
            started_at=datetime.now(UTC),
        )
        room.protocol_state = state
        self.repository.save_run(room_id=room.id, protocol=room.protocol, state=state)
        root_task = ProtocolTask(
            id=root_task_id,
            run_id=run_id,
            stage="protocol_run",
            task_type="room_run",
            status=ProtocolTaskStatus.RUNNING,
            input={"question": question, "protocol": room.protocol.value},
            started_at=state.started_at,
        )
        self.repository.save_task(room.id, root_task)
        await self._event(room, state, "room_started", metadata={"question": question})

        any_success = False
        any_failure = False
        try:
            for stage in definition.stages:
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                if self._skip_stage(stage, room):
                    continue
                # Clarification gate: the host may stop the run once, with
                # one merged question.  Everything after that must proceed.
                if state.clarification and stage.action != "finalize":
                    break
                state.current_stage = stage.id
                # Activity is stage-scoped.  Carrying participant ids forward
                # made the live panel claim that completed experts were still
                # working during barriers and host synthesis.
                state.active_participant_ids = []
                await self._event(room, state, "stage_changed", stage=stage.id)
                results = await self._execute_stage(
                    room=room,
                    state=state,
                    definition=definition,
                    stage=stage,
                    question=question,
                    executor=executor,
                )
                any_success = any_success or any(result[0] for result in results)
                any_failure = any_failure or any(not result[0] for result in results)
                if any_failure and not room.protocol_config.continue_on_member_failure:
                    raise RuntimeError("protocol_member_failure")

            state.finished_at = datetime.now(UTC)
            if state.clarification:
                # The run ended by asking; that is a completed outcome, not a
                # failure.  The panel must keep showing the run as finished.
                state.status = RoomRunStatus.WAITING_CLARIFICATION
            elif any_failure and any_success:
                state.status = RoomRunStatus.PARTIAL_SUCCESS
            elif any_failure:
                state.status = RoomRunStatus.FAILED
            else:
                state.status = RoomRunStatus.SUCCESS
            state.current_stage = "waiting_user"
            state.active_participant_ids = []
            await self._record_public_message(
                room,
                state,
                action="finalize",
                stage_id="final",
                participant=None,
                task_id=root_task_id,
                output=state.final_result or {},
            )
            await self._event(
                room,
                state,
                "final_response",
                stage="final",
                status=state.status.value,
                metadata=state.final_result or {},
            )
        except asyncio.CancelledError:
            state.status = RoomRunStatus.CANCELLED
            state.finished_at = datetime.now(UTC)
            state.active_participant_ids = []
            await self._event(room, state, "cancelled", status=state.status.value)
        except Exception as exc:
            state.status = RoomRunStatus.PARTIAL_SUCCESS if any_success else RoomRunStatus.FAILED
            state.finished_at = datetime.now(UTC)
            state.active_participant_ids = []
            await self._event(
                room,
                state,
                "error",
                status=state.status.value,
                metadata={"error_type": type(exc).__name__, "error": str(exc)},
            )
        finally:
            room.protocol_state = state
            root_task.status = {
                RoomRunStatus.SUCCESS: ProtocolTaskStatus.SUCCESS,
                RoomRunStatus.PARTIAL_SUCCESS: ProtocolTaskStatus.SUCCESS,
                RoomRunStatus.CANCELLED: ProtocolTaskStatus.CANCELLED,
            }.get(state.status, ProtocolTaskStatus.FAILED)
            root_task.output = {"run_status": state.status.value}
            root_task.finished_at = state.finished_at or datetime.now(UTC)
            self.repository.save_task(room.id, root_task)
            self.repository.save_run(room_id=room.id, protocol=room.protocol, state=state)
            self._cancel_events.pop(room.id, None)
        return state

    def _skip_stage(self, stage: ProtocolStageDefinition, room: RoomSessionState) -> bool:
        config = room.protocol_config
        return (
            (stage.id == "cross_review" and not config.cross_review)
            or (stage.id == "optional_rebuttal" and not config.rebuttal)
            or (stage.id == "cross_examination" and not config.cross_examination)
            or (stage.id == "vote" and not config.voting_enabled)
        )

    async def _execute_stage(
        self,
        *,
        room: RoomSessionState,
        state: RoomProtocolState,
        definition: ProtocolDefinition,
        stage: ProtocolStageDefinition,
        question: str,
        executor: ActionExecutor,
    ) -> list[tuple[bool, dict[str, Any]]]:
        if stage.action in {"wait", "barrier"}:
            return []
        if stage.action == "create_tasks":
            targets = (
                [
                    participant
                    for participant in room.participants
                    if participant.participant_id in set(state.selected_expert_ids)
                ]
                if state.selected_expert_ids
                else []
            )
            state.active_participant_ids = [item.participant_id for item in targets]
            await self._event(
                room,
                state,
                "task_created",
                stage=stage.id,
                metadata={"participant_ids": state.active_participant_ids},
            )
            return []

        targets = self._targets(room, state, definition, stage, question=question)
        if not targets and stage.optional:
            return []
        if not targets and stage.action not in {"finalize"}:
            raise ValueError(f"protocol_stage_has_no_actor:{stage.id}")
        if stage.parallel:
            return await asyncio.gather(
                *[
                    self._execute_participant(room, state, stage, participant, question, executor)
                    for participant in targets
                ]
            )
        results: list[tuple[bool, dict[str, Any]]] = []
        for participant in targets:
            results.append(
                await self._execute_participant(room, state, stage, participant, question, executor)
            )
        if stage.action == "finalize" and not targets and state.final_result is None:
            state.final_result = self._deterministic_final(room, state)
        if stage.action == "finalize" and state.final_result is not None and not targets:
            await self._record_public_message(
                room,
                state,
                action="finalize",
                stage_id=stage.id,
                participant=None,
                task_id=state.root_task_id or "final",
                output=state.final_result,
            )
        return results

    def _targets(
        self,
        room: RoomSessionState,
        state: RoomProtocolState,
        definition: ProtocolDefinition,
        stage: ProtocolStageDefinition,
        *,
        question: str = "",
    ) -> list[ParticipantSlot]:
        enabled = [participant for participant in room.participants if participant.enabled]
        if room.protocol == RoomProtocolType.EXPERT_CONSULTATION and stage.id in {
            "independent_analysis",
            "cross_review",
            "optional_rebuttal",
        }:
            if not state.selected_expert_ids:
                state.selected_expert_ids = self._route_experts(
                    room, self._routing_fallback_text(room, question)
                )
            selected = set(state.selected_expert_ids)
            return [
                participant for participant in enabled if participant.participant_id in selected
            ]
        allowed = set(stage.allowed_roles)
        if not allowed:
            return []
        targets = [participant for participant in enabled if participant.role in allowed]
        if stage.action in {"host_analysis", "routing", "synthesis", "finalize"} and targets:
            return [max(targets, key=lambda participant: participant.authority)]
        return targets

    def _routing_fallback_text(self, room: RoomSessionState, question: str) -> str:
        """Routing fallback anchors on the accumulated case, never one turn."""

        case_state = getattr(room, "case_state", None)
        problem = str(getattr(case_state, "problem_definition", "") or "")
        return problem or str(question or "") or str(room.topic or "")

    def _route_experts(self, room: RoomSessionState, question: str) -> list[str]:
        experts = [
            participant
            for participant in room.participants
            if participant.enabled and participant.role == "expert"
        ]
        config = room.protocol_config
        if config.routing_mode == "all":
            return [item.participant_id for item in experts[: config.max_experts]]
        tokens = set(re.findall(r"[\w\u4e00-\u9fff]+", question.casefold()))
        scored = []
        for index, expert in enumerate(experts):
            specialty_tokens = {
                token
                for specialty in expert.specialties
                for token in re.findall(r"[\w\u4e00-\u9fff]+", specialty.casefold())
            }
            scored.append(
                (len(tokens.intersection(specialty_tokens)), expert.authority, -index, expert)
            )
        scored.sort(reverse=True, key=lambda item: item[:3])
        selected_count = min(config.max_experts, max(config.min_experts, 1))
        return [item[3].participant_id for item in scored[:selected_count]]

    async def _execute_participant(
        self,
        room: RoomSessionState,
        state: RoomProtocolState,
        stage: ProtocolStageDefinition,
        participant: ParticipantSlot,
        question: str,
        executor: ActionExecutor,
    ) -> tuple[bool, dict[str, Any]]:
        assert state.run_id is not None
        task = ProtocolTask(
            id=new_id("roomtask"),
            run_id=state.run_id,
            parent_task_id=state.root_task_id,
            stage=stage.id,
            participant_id=participant.participant_id,
            task_type=stage.action,
            input={"question": question},
            status=ProtocolTaskStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
        state.tasks.append(task)
        state.active_participant_ids = sorted(
            set(state.active_participant_ids + [participant.participant_id])
        )
        self.repository.save_task(room.id, task)
        await self._event(
            room,
            state,
            "task_created",
            stage=stage.id,
            actor_id=(
                None
                if stage.action == "vote" and room.protocol_config.anonymous_voting
                else participant.participant_id
            ),
            task_id=task.id,
            status=task.status.value,
        )
        request = self._action_request(room, state, stage, participant, question, task.id)
        try:
            output = dict(await executor(request))
            task.output = output
            task.status = ProtocolTaskStatus.SUCCESS
            task.finished_at = datetime.now(UTC)
            self._capture_output(room, state, stage, participant, task, output, question=question)
            event_type = {
                "host_analysis": "host_analysis_submitted",
                "analysis": "analysis_submitted",
                "review": "review_submitted",
                "rebuttal": "rebuttal_submitted",
                "vote": "vote_submitted",
                # Synthesis submits its own event; `final_response` is reserved
                # for the run reaching a terminal state exactly once.
                "synthesis": "synthesis_submitted",
                "routing": "expert_selected",
            }.get(stage.action, "task_completed")
            await self._record_public_message(
                room,
                state,
                action=stage.action,
                stage_id=stage.id,
                participant=participant,
                task_id=task.id,
                output=output,
            )
            await self._event(
                room,
                state,
                event_type,
                stage=stage.id,
                actor_id=(
                    None
                    if stage.action == "vote" and room.protocol_config.anonymous_voting
                    else participant.participant_id
                ),
                task_id=task.id,
                status=task.status.value,
                metadata=(
                    {
                        key: value
                        for key, value in self._public_output(stage.action, output).items()
                        if key != "reason"
                    }
                    if stage.action == "vote" and room.protocol_config.anonymous_voting
                    else self._public_output(stage.action, output)
                ),
            )
            return True, output
        except asyncio.CancelledError:
            task.status = ProtocolTaskStatus.CANCELLED
            task.finished_at = datetime.now(UTC)
            raise
        except Exception as exc:
            task.status = ProtocolTaskStatus.FAILED
            task.error_type = type(exc).__name__
            task.error = str(exc)
            task.finished_at = datetime.now(UTC)
            await self._event(
                room,
                state,
                "error",
                stage=stage.id,
                actor_id=participant.participant_id,
                task_id=task.id,
                status=task.status.value,
                metadata={"error_type": task.error_type, "error": task.error},
            )
            return False, {"error_type": task.error_type, "error": task.error}
        finally:
            self.repository.save_task(room.id, task)

    def _action_request(
        self,
        room: RoomSessionState,
        state: RoomProtocolState,
        stage: ProtocolStageDefinition,
        participant: ParticipantSlot,
        question: str,
        task_id: str,
    ) -> ProtocolActionRequest:
        peer_results: list[dict[str, Any]] = []
        if stage.action in {"review", "rebuttal", "synthesis", "finalize"}:
            peer_results = [
                self._compact_peer_result(item.model_dump(mode="json"))
                for item in state.submissions
                if item.participant_id != participant.participant_id
                or stage.action in {"synthesis", "finalize"}
            ]
            if stage.action in {"synthesis", "finalize"}:
                peer_results.extend(
                    self._compact_peer_result(item.model_dump(mode="json"))
                    for item in state.reviews
                )
            peer_results = peer_results[-8:]
        return ProtocolActionRequest(
            room_id=room.id,
            run_id=state.run_id or "",
            task_id=task_id,
            protocol=room.protocol,
            stage=stage.id,
            action=stage.action,
            participant=participant,
            question=question,
            room_context=self.compiled_shared_context(room),
            role_context={"role": participant.role, "authority": participant.authority},
            task_context=self._task_context(room, state, stage, participant),
            # Per-stage, deduplicated, transport-cheap dialogue window.
            conversation_context=self._conversation_context(
                room, state, stage, participant, peer_results
            ),
            peer_results=peer_results,
            tool_permissions=list(participant.tool_permissions),
        )

    # -- per-stage context shaping ----------------------------------------
    #
    # Every stage used to receive ``room.transcript[-12:]`` plus the peer
    # results plus the rendered public messages -- the same information
    # three times.  That is what pushed an ordinary Qoder ARGV prompt past
    # the 64KB ceiling after a handful of turns.  Each stage now declares
    # what it actually needs.

    #: How many recent turns each stage may see (0 = no raw transcript).
    #: Keys are stage ACTIONS (``stage.action``), not stage ids -- the lookup
    # in _conversation_context is ``.get(stage.action)``.  Stage ids such as
    # "expert_routing"/"independent_analysis" never appear as actions, so
    # keying by id silently disabled every minimised config except
    # host_analysis.
    _STAGE_TRANSCRIPT_WINDOW: dict[str, int] = {
        "host_analysis": 6,
        "routing": 0,
        "analysis": 2,
        "review": 0,
        "rebuttal": 4,
        "synthesis": 0,
        "finalize": 0,
    }
    #: Fallback for stages not listed above (debate / committee / custom).
    _DEFAULT_TRANSCRIPT_WINDOW = 4

    def _conversation_context(
        self,
        room: RoomSessionState,
        state: RoomProtocolState,
        stage: ProtocolStageDefinition,
        participant: ParticipantSlot,
        peer_results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        window = self._STAGE_TRANSCRIPT_WINDOW.get(stage.action)
        if window is None:
            window = self._DEFAULT_TRANSCRIPT_WINDOW
        if window <= 0:
            return []
        raw = list(room.transcript[-window:])
        # Expert public prose whose structured conclusion is already carried
        # as a peer result is pure duplication -- drop it.  Independent
        # analysis additionally drops OTHER experts' public analysis prose
        # (its peer-results list is empty by design), so the dedupe is seeded
        # with the peer expert ids directly: one expert's public answer must
        # never leak into another expert's independent window.
        if stage.action in {"review", "rebuttal", "synthesis"}:
            dedupe_seed: list[dict[str, Any]] | None = peer_results
        elif stage.action == "analysis":
            dedupe_seed = [
                {"participant_id": item.participant_id}
                for item in room.participants
                if item.enabled
                and item.role == "expert"
                and item.participant_id != participant.participant_id
            ]
        else:
            dedupe_seed = None
        deduped = dedupe_public_messages(raw, dedupe_seed)
        return compact_transcript(deduped, keep_recent=max(1, window))

    def _task_context(
        self,
        room: RoomSessionState,
        state: RoomProtocolState,
        stage: ProtocolStageDefinition,
        participant: ParticipantSlot,
    ) -> dict[str, Any]:
        instruction = self._stage_instruction(stage)
        base: dict[str, Any] = {
            "stage_instruction": instruction,
            "independent_first": room.protocol_config.independent_first,
        }
        # Accumulated case facts ride into every stage; legacy rooms without
        # one simply send nothing.
        case_state = getattr(room, "case_state", None)
        case_block = (
            _clip_text(case_state.as_prompt_block(), 1200)
            if isinstance(case_state, RoomCaseState)
            else None
        )
        if case_block and any(case_block.values()):
            base["case_state"] = case_block
        if stage.action == "host_analysis":
            # Host needs to understand the case and decide what is missing;
            # it must NOT receive every expert's previous full answer.
            base["participant_specialties"] = [
                {
                    "participant_id": candidate.participant_id,
                    "display_name": candidate.display_name or candidate.persona_id,
                    "specialties": candidate.specialties,
                }
                for candidate in room.participants
                if candidate.enabled and candidate.role == "expert"
            ]
            base["clarification_budget"] = {
                "used": room.case_state.clarification_round_count,
                "max": MAX_BLOCKING_CLARIFICATIONS,
                "exhausted": room.case_state.clarification_budget_exhausted,
            }
            base["host_duty"] = HOST_ANALYSIS_DUTY
        elif stage.action == "routing":
            base["routing_candidates"] = [
                {
                    "participant_id": candidate.participant_id,
                    "display_name": candidate.display_name or candidate.persona_id,
                    "role": candidate.role,
                    "specialties": candidate.specialties,
                }
                for candidate in room.participants
                if candidate.enabled and candidate.role == "expert"
            ]
            base["host_analysis"] = _clip_text(
                state.artifacts.get("host_analysis"), 1200
            )
            base["max_experts"] = room.protocol_config.max_experts
        elif stage.action == "analysis":
            # Independent analysis: peers are deliberately invisible so the
            # cross-review stage has something real to compare.
            base["peer_results"] = "NONE"
            base["expert_duty"] = EXPERT_ANALYSIS_DUTY
        elif stage.action == "review":
            own = next(
                (
                    item
                    for item in reversed(state.submissions)
                    if item.participant_id == participant.participant_id
                ),
                None,
            )
            base["own_conclusion"] = (
                _clip_text(own.model_dump(mode="json"), 1200) if own else None
            )
        elif stage.action in {"synthesis", "finalize"}:
            base["host_prior_analysis"] = _clip_text(
                state.artifacts.get("host_analysis"), 900
            )
            base["synthesis_duty"] = SYNTHESIS_DUTY
        return base

    @staticmethod
    def _compact_peer_result(raw: dict[str, Any]) -> dict[str, Any]:
        """Carry editorial conclusions across stages without replaying full evidence."""

        def clipped(value: Any, limit: int = 420) -> str:
            return str(value or "")[:limit]

        payload = dict(raw.get("payload") or raw.get("output") or raw)
        compact: dict[str, Any] = {
            "participant_id": raw.get("participant_id"),
            "stage": raw.get("stage"),
            "summary": clipped(payload.get("summary"), 600),
            "recommendation": clipped(payload.get("recommendation"), 600),
            "updated_conclusion": clipped(payload.get("updated_conclusion"), 600),
            "confidence": payload.get("confidence"),
        }
        for key in ("key_findings", "agree", "disagree", "concerns", "uncertainties"):
            values = payload.get(key)
            if isinstance(values, list):
                compact[key] = [clipped(item, 260) for item in values[:3]]
        return {key: value for key, value in compact.items() if value not in (None, "", [])}

    def compiled_shared_context(self, room: RoomSessionState) -> dict[str, Any]:
        raw = room.shared_context.model_dump(mode="json")
        fingerprint = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        cached = self._shared_context_cache.get(room.id)
        if cached and cached[0] == fingerprint:
            return cached[1]
        compiled = {
            **raw,
            "compiled_text": "\n".join(
                part
                for part in (
                    raw.get("description", ""),
                    raw.get("background", ""),
                    "\n".join(raw.get("rules") or []),
                    raw.get("task_context", ""),
                    raw.get("custom_instructions", ""),
                )
                if part
            ),
        }
        self._shared_context_cache[room.id] = (fingerprint, compiled)
        return compiled

    @staticmethod
    def _stage_instruction(stage: ProtocolStageDefinition) -> str:
        return {
            "analysis": (
                "独立完成当前任务。输出 summary、key_findings、evidence、uncertainties、"
                "recommendation、confidence。不要假设其他成员结论。"
            ),
            "review": (
                "仅评审提供的同行结论：列出 agree、disagree、concerns，并说明 "
                "changed_position 与 updated_conclusion。"
            ),
            "vote": "提交 vote、reason、confidence。不得替其他成员投票。",
            "routing": (
                "只选择与问题相关的成员，返回 selected_participant_ids 和简短 routing_reason。"
            ),
            "synthesis": (
                "综合所有专家意见形成最终研判，不得机械拼接，密度要求："
                "summary 2-4 句且先给结论；"
                "participant_positions 只列真正参与的专家，每人仅 1 条核心判断加 1 条关键理由，"
                "不得复述完整回答；consensus 最多 5 条，只保留专家独立得出且一致的观点；"
                "conflicts 最多 4 条，说明谁与谁在何处分歧、原因以及主持人如何处置；"
                "final_judgment 必须明确果断，禁止“各有道理，建议综合考虑”一类的模糊表述；"
                "recommendations 给出 1-4 条可执行建议；"
                "uncertainties 只保留会改变结论的不确定性。"
            ),
        }.get(stage.action, f"完成协议阶段 {stage.id} 的公开任务。")

    def _capture_host_analysis(
        self,
        room: RoomSessionState,
        state: RoomProtocolState,
        task: ProtocolTask,
        output: dict[str, Any],
    ) -> None:
        """Fold the host analysis into the case state and apply the gate.

        The gate is enforced here, in code, because a prompt alone cannot
        stop a model that has been trained to ask for its input package
        first.  One case gets at most one blocking clarification, and a
        clarification that asks back for something the user asked *us* to
        compute is always rejected.
        """

        # The budget gate must judge the *pre-merge* counter:
        # merge_from_host_analysis() charges the budget for this very ask
        # (note_clarification), so reading the counter after the merge would
        # already see the budget as spent and suppress the FIRST legal
        # blocking clarification.  Semantic: only the SECOND blocking
        # attempt within the same case is exhausted; the first must reach
        # waiting_clarification (§15/§17/§27).
        budget_exhausted_before_merge = room.case_state.clarification_budget_exhausted
        can_proceed = bool(output.get("can_proceed", True))
        question = str(output.get("clarification_question") or "").strip()
        # The three gates below decide whether this host turn may charge the
        # clarification budget at all: a suppressed ask must not increment
        # the counter, must not enter the asked-questions list and must not
        # leave open questions behind -- otherwise a rejected ask would
        # still poison the case state it was rejected from.
        allow_clarification = (
            bool(question)
            and not can_proceed
            and not budget_exhausted_before_merge
            and not violates_output_input_boundary(question)
        )
        room.case_state = merge_from_host_analysis(
            room.case_state, output, allow_clarification=allow_clarification
        )
        # Ephemeral artifact: never written as persona memory.
        state.artifacts["host_analysis"] = _clip_text(
            {
                "problem_definition": str(output.get("problem_definition") or ""),
                "known_facts_patch": output.get("known_facts_patch") or {},
                "assumptions": output.get("assumptions") or [],
                "can_proceed": can_proceed,
                "missing_indispensable_fields": output.get("missing_indispensable_fields")
                or [],
            },
            2000,
        )

        state.clarification = None
        if can_proceed or not question:
            return
        if budget_exhausted_before_merge:
            # Second attempt in the same case: the budget is spent, so the
            # run must go on with defaults instead of asking again.
            state.artifacts["clarification_suppressed"] = {
                "question": question,
                "reason": "clarification_budget_exhausted",
            }
            return
        if violates_output_input_boundary(question):
            state.artifacts["clarification_suppressed"] = {
                "question": question,
                "reason": "output_asked_back_as_input",
            }
            return
        state.clarification = {
            "task_id": task.id,
            "question": question,
            "missing_fields": [
                str(item)
                for item in (output.get("missing_indispensable_fields") or [])
                if str(item).strip()
            ],
            "round": room.case_state.clarification_round_count,
        }

    def _capture_output(
        self,
        room: RoomSessionState,
        state: RoomProtocolState,
        stage: ProtocolStageDefinition,
        participant: ParticipantSlot,
        task: ProtocolTask,
        output: dict[str, Any],
        *,
        question: str = "",
    ) -> None:
        if stage.action == "host_analysis":
            self._capture_host_analysis(room, state, task, output)
        elif stage.action == "routing":
            raw_selected = output.get("selected_participant_ids") or output.get(
                "selected_expert_ids"
            )
            if isinstance(raw_selected, list):
                allowed = {
                    item.participant_id
                    for item in room.participants
                    if item.enabled and item.role == "expert"
                }
                selected = [str(item) for item in raw_selected if str(item) in allowed]
                if selected:
                    maximum = room.protocol_config.max_experts
                    minimum = room.protocol_config.min_experts
                    fallback = self._route_experts(
                        room, self._routing_fallback_text(room, question)
                    )
                    selected.extend(item for item in fallback if item not in selected)
                    state.selected_expert_ids = selected[
                        : max(minimum, min(maximum, len(selected)))
                    ]
        elif stage.action == "analysis":
            payload = {
                "task_id": task.id,
                "participant_id": participant.participant_id,
                "summary": str(output.get("summary") or output.get("content") or ""),
                "key_findings": output.get("key_findings") or [],
                "evidence": output.get("evidence") or [],
                "uncertainties": output.get("uncertainties") or [],
                "recommendation": str(output.get("recommendation") or ""),
                "confidence": float(output.get("confidence", 0.5)),
                "domain_data": output.get("domain_data") or {},
            }
            state.submissions.append(StructuredSubmission.model_validate(payload))
        elif stage.action == "review":
            state.reviews.append(
                CrossReview.model_validate(
                    {
                        "task_id": task.id,
                        "participant_id": participant.participant_id,
                        "agree": output.get("agree") or [],
                        "disagree": output.get("disagree") or [],
                        "concerns": output.get("concerns") or [],
                        "changed_position": bool(output.get("changed_position")),
                        "updated_conclusion": output.get("updated_conclusion"),
                    }
                )
            )
        elif stage.action == "vote":
            vote = RoomVote.model_validate(
                {
                    "task_id": task.id,
                    "participant_id": participant.participant_id,
                    "vote": output.get("vote") or "abstain",
                    "reason": output.get("reason") or "",
                    "confidence": output.get("confidence", 0.5),
                    "weight": participant.authority / 50.0,
                }
            )
            state.votes.append(vote)
        elif stage.action in {"synthesis", "finalize"}:
            result = dict(output)
            if state.votes:
                result["vote_count"] = self.repository.count_votes(
                    [item.model_dump(mode="json") for item in state.votes],
                    weighted=room.protocol_config.vote_method == "weighted",
                )
                if (
                    result.get("override_majority")
                    and not str(result.get("override_reason") or "").strip()
                ):
                    raise ValueError("chair_majority_override_requires_reason")
            state.final_result = result

    async def _record_public_message(
        self,
        room: RoomSessionState,
        state: RoomProtocolState,
        *,
        action: str,
        stage_id: str,
        participant: ParticipantSlot | None,
        task_id: str,
        output: dict[str, Any],
        kind: str | None = None,
    ) -> None:
        """Hand one public protocol message to the host transcript recorder.

        The runtime only decides *what* is public; persistence, idempotency and
        WebSocket delivery live in the orchestrator recorder.
        """
        if not self.transcript_recorder:
            return
        kind = kind or PROTOCOL_MESSAGE_KINDS.get(action)
        if not kind:
            return
        name_map = {
            item.participant_id: (item.display_name or item.persona_id)
            for item in room.participants
        }
        content = render_public_message(action, output, name_map)
        if not content.strip():
            return
        if participant is None:
            host = next(
                (
                    item
                    for item in room.participants
                    if item.enabled and item.role in {"host", "chair"}
                ),
                None,
            )
            speaker_name = (host.display_name or host.persona_id) if host else "主持人"
            participant_id = host.participant_id if host else ""
            persona_id = host.persona_id if host else ""
        else:
            speaker_name = participant.display_name or participant.persona_id
            participant_id = participant.participant_id
            persona_id = participant.persona_id
        payload = {
            "_room": room,
            "room_id": room.id,
            "run_id": state.run_id or "",
            "task_id": task_id,
            "stage": stage_id,
            "action": action,
            "message_kind": kind,
            "participant_id": participant_id,
            "persona_id": persona_id,
            "speaker_name": speaker_name,
            "content": content,
        }
        if action in {"synthesis", "finalize"}:
            # The run's final answer ships its structured public payload so
            # the frontend can render it without re-parsing the chat text.
            payload["public_payload"] = self._public_output(action, output)
        await self.transcript_recorder(payload)

    def _deterministic_final(
        self, room: RoomSessionState, state: RoomProtocolState
    ) -> dict[str, Any]:
        counts = Counter(item.vote for item in state.votes)
        return {
            "summary": state.submissions[-1].summary if state.submissions else "",
            "vote_count": dict(counts),
            "status": state.status.value,
            "protocol": room.protocol.value,
        }

    @staticmethod
    def _public_output(action: str, output: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "analysis": {
                "summary",
                "key_findings",
                "evidence",
                "uncertainties",
                "recommendation",
                "confidence",
            },
            "review": {"agree", "disagree", "concerns", "changed_position", "updated_conclusion"},
            "vote": {"vote", "reason", "confidence"},
            "routing": {"selected_participant_ids", "selected_expert_ids", "routing_reason"},
            "synthesis": {
                "summary",
                "participant_positions",
                "consensus",
                "conflicts",
                "final_judgment",
                "uncertainties",
                "recommendations",
            },
            # Finalize renders the synthesis-shaped result verbatim.
            "finalize": {
                "summary",
                "participant_positions",
                "consensus",
                "conflicts",
                "final_judgment",
                "uncertainties",
                "recommendations",
            },
        }.get(action, {"summary", "content"})
        return {key: value for key, value in output.items() if key in allowed}

    async def _event(
        self,
        room: RoomSessionState,
        state: RoomProtocolState,
        event_type: str,
        *,
        stage: str | None = None,
        actor_id: str | None = None,
        target_id: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RoomProtocolEvent:
        assert state.run_id is not None
        event = RoomProtocolEvent(
            id=new_id("roomev"),
            room_id=room.id,
            run_id=state.run_id,
            event_type=event_type,
            stage=stage or state.current_stage,
            actor_id=actor_id,
            target_id=target_id,
            task_id=task_id,
            status=status,
            metadata=metadata or {},
        )
        room.protocol_events.append(event)
        if len(room.protocol_events) > 200:
            room.protocol_events = room.protocol_events[-200:]
        # Persist the live state before broadcasting the event.  The web
        # fallback reads the latest run over HTTP, so task progress and the
        # terminal status must never lag behind the WebSocket event that
        # announced them.
        self.repository.save_run(room_id=room.id, protocol=room.protocol, state=state)
        self.repository.save_event(event)
        if self.event_emitter:
            await self.event_emitter(
                {
                    "event": event.event_type,
                    **event.model_dump(mode="json"),
                }
            )
        return event
