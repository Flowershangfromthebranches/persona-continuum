"""NarrativeDirectorService — host-owned action loop for the Director Agent.

The Director model only ever emits a structured decision JSON; THIS service
validates permissions, enforces optimistic concurrency, dispatches actions to
``NarrativeService``, persists every action, and owns the stop rules:

- Canon Gate: ``audit_episode`` with BLOCKING == 0 forces
  ``WAITING_FOR_CANON_APPROVAL`` and ends the loop. The Director has no
  commit/production capability at all (see ``narrative/director.py``).
- Audit Repair Loop: after a failed audit the model may keep revising, but at
  most ``DIRECTOR_MAX_AUTO_AUDIT_REPAIR_ROUNDS`` automatic repair rounds;
  beyond that the session enters ``NEEDS_HUMAN_GUIDANCE``.
- WARNINGs never trigger further automatic revision.

Sessions are logical conversations persisted in SQLite; every model turn uses
the existing turn-scoped runtime executor (no persistent runtime lease).
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

from persona_continuum.application._utils import dumps
from persona_continuum.domain.narrative import (
    EpisodeStatus,
    ForecastDirection,
    GenerationMode,
    NarrativeDirectorAction,
    NarrativeDirectorActionStatus,
    NarrativeDirectorMessage,
    NarrativeDirectorMode,
    NarrativeDirectorSession,
    NarrativeDirectorSessionStatus,
)
from persona_continuum.narrative.director import (
    ACTION_REGISTRY,
    DIRECTOR_DECISION_SCHEMA,
    DIRECTOR_MAX_ACTIONS_PER_TURN,
    DIRECTOR_MAX_AUTO_AUDIT_REPAIR_ROUNDS,
    DIRECTOR_SYSTEM_PROMPT,
    DirectorActionRisk,
    NarrativeDirectorStateConflict,
    missing_required_arguments,
    normalize_action_arguments,
)
from persona_continuum.narrative.repository import NarrativeRepository
from persona_continuum.narrative.runtime import (
    NARRATIVE_AGENT_RUNTIME_UNAVAILABLE,
    NarrativeAgentError,
)

# An affirmative reply grants confirmation for the pending HIGH_IMPACT action.
_CONFIRM_PATTERN = re.compile(
    r"^\s*(确认|同意|确认执行|执行吧|可以|是的?|嗯+|好的?|ok|yes|confirm|approve)[!！。.]*\s*$",
    re.IGNORECASE,
)
_REJECT_PATTERN = re.compile(
    r"^\s*(不|不要|不用|取消|先不|否|no|cancel|stop|don't|do not)[!！。.]*\s*$",
    re.IGNORECASE,
)

_MAX_CONTEXT_MESSAGES = 12
_MAX_CONTEXT_ACTION_RESULTS = 3


def _now() -> datetime:
    return datetime.now(UTC)


class DirectorSessionBusyError(Exception):
    """A turn loop is already running for this session."""


class NarrativeDirectorService:
    def __init__(self, continuum: Any, narratives: Any) -> None:
        self.continuum = continuum
        self.narratives = narratives
        self.repo: NarrativeRepository = narratives.repo
        self._tasks: dict[str, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------
    def create_session(
        self,
        project_id: str,
        *,
        episode_number: int | None = None,
        mode: str = NarrativeDirectorMode.AGENT.value,
        runtime: dict[str, Any] | None = None,
    ) -> NarrativeDirectorSession:
        self.narratives.get_project(project_id)  # existence check
        session = NarrativeDirectorSession(
            project_id=project_id,
            episode_number=episode_number,
            mode=NarrativeDirectorMode(mode),
            runtime=dict(runtime or {}),
        )
        self.repo.save_director_session(session)
        return session

    def list_sessions(self, project_id: str) -> list[NarrativeDirectorSession]:
        return self.repo.list_director_sessions(project_id)

    def reclaim_orphaned_sessions(self) -> int:
        """Recover sessions left RUNNING by a process restart.

        Loop tasks live only in memory, so any RUNNING session at startup is
        orphaned: it would otherwise block every future message forever via
        the busy guard. Park such sessions at WAITING_FOR_USER (history is
        kept, the author simply continues) and cancel their in-flight actions.
        """
        recovered = 0
        for session in self.repo.list_director_sessions_in_status(
            NarrativeDirectorSessionStatus.RUNNING.value
        ):
            session.status = NarrativeDirectorSessionStatus.WAITING_FOR_USER
            session.last_error = "Director 运行因服务重启中断，可重新发送消息继续。"
            self.repo.save_director_session(session)
            self.repo.cancel_running_director_actions(
                session.id, "Director 会话中断（服务重启）"
            )
            self.repo.append_director_message(
                NarrativeDirectorMessage(
                    session_id=session.id,
                    role="system",
                    content="Director 运行因服务重启中断，可重新发送消息继续。",
                )
            )
            recovered += 1
        return recovered

    def get_session(self, session_id: str) -> NarrativeDirectorSession:
        session = self.repo.get_director_session(session_id)
        if session is None:
            raise KeyError(f"Director session not found: {session_id}")
        return session

    def session_snapshot(self, session_id: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        return {
            "session": session.model_dump(mode="json"),
            "messages": [
                m.model_dump(mode="json") for m in self.repo.list_director_messages(session_id)
            ],
            "actions": [
                a.model_dump(mode="json") for a in self.repo.list_director_actions(session_id)
            ],
        }

    def update_mode(
        self,
        session_id: str,
        mode: str | None,
        *,
        runtime: dict[str, Any] | None = None,
    ) -> NarrativeDirectorSession:
        session = self.get_session(session_id)
        if mode:
            session.mode = NarrativeDirectorMode(mode)
        if isinstance(runtime, dict) and runtime.get("agent_id"):
            session.runtime = dict(runtime)
        self.repo.save_director_session(session)
        return session

    def send_message(
        self,
        session_id: str,
        content: str,
        *,
        runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a user message and start (or continue) the action loop.

        ``runtime`` optionally overrides the Director runtime for this and
        following turns (UI runtime selector); it is persisted on the session.
        """
        content = (content or "").strip()
        if not content:
            raise ValueError("Message content is required")
        session = self.get_session(session_id)
        if session.status == NarrativeDirectorSessionStatus.RUNNING:
            task = self._tasks.get(session_id)
            if task is not None and not task.done():
                raise DirectorSessionBusyError("Director session is already running")
            # Orphaned loop (e.g. the worker task died with the process):
            # recover instead of blocking the session forever.
            session.status = NarrativeDirectorSessionStatus.WAITING_FOR_USER
            self.repo.save_director_session(session)
            self.repo.cancel_running_director_actions(session.id, "Director 会话中断")
        project = self.narratives.get_project(session.project_id)
        # Confirmation handshake for a pending HIGH_IMPACT action.
        if session.pending_action:
            if _CONFIRM_PATTERN.match(content):
                session.pending_action["user_confirmed"] = True
                self.repo.append_director_message(
                    NarrativeDirectorMessage(
                        session_id=session_id, role="system",
                        content="用户已确认待执行的高影响操作。",
                    )
                )
            elif _REJECT_PATTERN.match(content):
                session.pending_action = {}
                self.repo.append_director_message(
                    NarrativeDirectorMessage(
                        session_id=session_id, role="system",
                        content="用户拒绝了待执行的高影响操作。",
                    )
                )
        self.repo.append_director_message(
            NarrativeDirectorMessage(session_id=session_id, role="user", content=content)
        )
        if isinstance(runtime, dict) and runtime.get("agent_id"):
            session.runtime = dict(runtime)
        session.status = NarrativeDirectorSessionStatus.RUNNING
        session.project_revision = project.revision
        session.story_bible_version = project.story_bible_version
        session.last_error = ""
        self.repo.save_director_session(session)
        self._start_loop(session_id)
        return self.session_snapshot(session_id)

    def pause_session(self, session_id: str) -> NarrativeDirectorSession:
        session = self.get_session(session_id)
        if session.status == NarrativeDirectorSessionStatus.RUNNING:
            session.status = NarrativeDirectorSessionStatus.PAUSED
            self.repo.save_director_session(session)
        return session

    def resume_session(self, session_id: str) -> NarrativeDirectorSession:
        session = self.get_session(session_id)
        if session.status == NarrativeDirectorSessionStatus.PAUSED:
            session.status = NarrativeDirectorSessionStatus.ACTIVE
            self.repo.save_director_session(session)
        return session

    def cancel_session(self, session_id: str) -> NarrativeDirectorSession:
        session = self.get_session(session_id)
        session.status = NarrativeDirectorSessionStatus.CANCELLED
        self.repo.save_director_session(session)
        task = self._tasks.get(session_id)
        if task is not None and not task.done():
            task.cancel()
        return session

    def shutdown(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        self._tasks.clear()

    def _start_loop(self, session_id: str) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._run_loop(session_id))
            return
        task = asyncio.get_running_loop().create_task(
            self._run_loop(session_id), name=f"narrative-director-{session_id}"
        )
        self._tasks[session_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(session_id, None))

    # ------------------------------------------------------------------
    # Action loop
    # ------------------------------------------------------------------
    async def _run_loop(self, session_id: str) -> None:
        failed_audits = 0
        consecutive_failures = 0
        turns = 0
        tool_results: list[dict[str, Any]] = []
        try:
            while turns < DIRECTOR_MAX_ACTIONS_PER_TURN:
                session = self.get_session(session_id)
                if session.status != NarrativeDirectorSessionStatus.RUNNING:
                    return  # paused / cancelled between turns
                project = self.narratives.get_project(session.project_id)
                context_message = self._build_context(session, project, tool_results)
                decision = await self._director_turn(project, session, context_message)
                turns += 1
                kind = str(decision.get("decision") or "answer")

                if kind == "execute_action":
                    result = await self._execute_action(
                        session,
                        str(decision.get("action") or ""),
                        dict(decision.get("arguments") or {}),
                    )
                    tool_results.append({"action": decision.get("action"), "result": result})
                    # Circuit breaker: a repeated failing action means the
                    # model cannot self-correct; stop instead of burning the
                    # whole turn budget on identical errors.
                    if result.get("status") in {"failed", "rejected"}:
                        consecutive_failures += 1
                        if consecutive_failures >= 3:
                            self._append_message(
                                session_id,
                                "assistant",
                                "同一操作连续失败 3 次，已停止自动执行，需要你检查：\n"
                                f"最近错误：{result.get('message') or result.get('code')}",
                            )
                            self._transition(
                                session_id,
                                NarrativeDirectorSessionStatus.NEEDS_HUMAN_GUIDANCE,
                            )
                            return
                    else:
                        consecutive_failures = 0
                    if result.get("status") == "awaiting_confirmation":
                        self._append_message(
                            session_id,
                            "assistant",
                            str(
                                decision.get("message")
                                or result.get("message")
                                or "该操作影响较大，需要你明确确认后才会执行。"
                            ),
                        )
                        self._transition(
                            session_id, NarrativeDirectorSessionStatus.WAITING_FOR_USER
                        )
                        return
                    if (
                        result.get("action") == "audit_episode"
                        and result.get("status") == "succeeded"
                    ):
                        blocking = int(
                            (result.get("artifacts") or {}).get("blocking_count") or 0
                        )
                        if blocking == 0:
                            # Canon Gate: hard stop before human approval.
                            self._append_message(
                                session_id,
                                "assistant",
                                str(decision.get("message") or result.get("summary") or "")
                                + "\n\n连续性审核已通过（BLOCKING 0）。Director 已停止自动执行，"
                                "请你在「单集创作 → 提交正史」手动确认提交。",
                            )
                            self._transition(
                                session_id,
                                NarrativeDirectorSessionStatus.WAITING_FOR_CANON_APPROVAL,
                            )
                            return
                        failed_audits += 1
                        # Initial audit + at most N automatic repair rounds.
                        if failed_audits >= 1 + DIRECTOR_MAX_AUTO_AUDIT_REPAIR_ROUNDS:
                            self._append_message(
                                session_id,
                                "assistant",
                                f"自动修复 {DIRECTOR_MAX_AUTO_AUDIT_REPAIR_ROUNDS} 轮后仍存在 "
                                f"BLOCKING 问题，需要人工判断：\n{result.get('summary', '')}",
                            )
                            self._transition(
                                session_id,
                                NarrativeDirectorSessionStatus.NEEDS_HUMAN_GUIDANCE,
                            )
                            return
                    continue

                message = str(decision.get("message") or "")
                if message:
                    self._append_message(session_id, "assistant", message)
                if kind == "request_user_input":
                    self._transition(session_id, NarrativeDirectorSessionStatus.WAITING_FOR_USER)
                    return
                if kind == "stop":
                    self._transition(
                        session_id, self._gate_status(session.project_id, session.episode_number)
                    )
                    return
                # Plain answer.
                self._transition(
                    session_id, self._gate_status(session.project_id, session.episode_number)
                )
                return
            # Turn budget exhausted without reaching a stop decision.
            session = self.get_session(session_id)
            session.last_error = "Director action loop turn budget exhausted"
            self.repo.save_director_session(session)
            self._append_message(
                session_id,
                "assistant",
                "本轮操作步数已达上限，已暂停等待你的指示。你可以继续补充要求。",
            )
            self._transition(session_id, NarrativeDirectorSessionStatus.NEEDS_HUMAN_GUIDANCE)
        except asyncio.CancelledError:
            raise
        except NarrativeAgentError as exc:
            session = self.get_session(session_id)
            session.status = NarrativeDirectorSessionStatus.FAILED
            session.last_error = exc.to_dict().get("message", str(exc))
            self.repo.save_director_session(session)
            self._append_message(session_id, "system", f"Director 运行失败：{session.last_error}")
        except Exception as exc:  # noqa: BLE001 — the loop owns error recovery.
            session = self.get_session(session_id)
            session.status = NarrativeDirectorSessionStatus.FAILED
            session.last_error = str(exc)
            self.repo.save_director_session(session)
            self._append_message(session_id, "system", f"Director 运行失败：{exc}")

    async def _director_turn(
        self, project: Any, session: NarrativeDirectorSession, context_message: str
    ) -> dict[str, Any]:
        runtime = self.narratives._resolve_stage_runtime(
            project, "director", session.runtime or None
        )
        # The shared structured-call helper owns the generic system prompt;
        # the Director contract travels with the user message so every
        # transport (CLI harness, API) receives identical instructions.
        value, _trace = await self.narratives._structured_call_async(
            f"{DIRECTOR_SYSTEM_PROMPT}\n\n=== CURRENT CONTEXT ===\n{context_message}",
            runtime=runtime,
            phase="narrative_director",
            mode=GenerationMode.AGENT,
            schema=DIRECTOR_DECISION_SCHEMA,
            structured_repair_attempts=2,
        )
        if not isinstance(value, dict):
            raise NarrativeAgentError(
                NARRATIVE_AGENT_RUNTIME_UNAVAILABLE,
                "Director requires a configured Agent runtime (no deterministic fallback)",
                stage="narrative_director",
                runtime=runtime,
            )
        return value

    # ------------------------------------------------------------------
    # Action dispatch
    # ------------------------------------------------------------------
    async def _execute_action(
        self, session: NarrativeDirectorSession, action_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        spec = ACTION_REGISTRY.get(action_name)
        arguments = normalize_action_arguments(
            action_name,
            arguments,
            session_project_id=session.project_id,
            session_episode_number=session.episode_number,
            session_project_revision=session.project_revision or None,
        )
        record = NarrativeDirectorAction(
            session_id=session.id,
            action=action_name or "unknown",
            arguments=arguments,
            status=NarrativeDirectorActionStatus.RUNNING,
        )
        self.repo.save_director_action(record)

        def finish(
            status: NarrativeDirectorActionStatus,
            result: dict[str, Any],
            *,
            code: str = "",
            message: str = "",
        ) -> dict[str, Any]:
            record.status = status
            record.result = result
            record.error_code = code
            record.error_message = message
            record.completed_at = _now()
            self.repo.save_director_action(record)
            payload = {"action_id": record.id, "action": record.action, **result}
            return payload

        try:
            if spec is None:
                # Includes every HUMAN_ONLY capability: they are not registered.
                return finish(
                    NarrativeDirectorActionStatus.REJECTED,
                    {"status": "rejected", "retryable": False},
                    code="DIRECTOR_ACTION_NOT_ALLOWED",
                    message=f"Action not allowed: {action_name}. Human approval required.",
                )
            missing = missing_required_arguments(spec, arguments)
            if missing:
                return finish(
                    NarrativeDirectorActionStatus.FAILED,
                    {
                        "status": "failed",
                        "retryable": False,
                        "code": "DIRECTOR_ACTION_INVALID_ARGUMENTS",
                        "message": (
                            f"Missing required argument(s): {', '.join(missing)}. "
                            f"Accepted fields for {action_name}: {sorted(spec.arguments)}."
                        ),
                    },
                    code="DIRECTOR_ACTION_INVALID_ARGUMENTS",
                    message=f"Missing required argument(s): {', '.join(missing)}",
                )
            if (
                spec.risk != DirectorActionRisk.READ_ONLY
                and session.mode != NarrativeDirectorMode.AGENT
            ):
                return finish(
                    NarrativeDirectorActionStatus.REJECTED,
                    {"status": "rejected", "retryable": False},
                    code="DIRECTOR_ACTION_NOT_ALLOWED",
                    message=(
                        f"Mode '{session.mode.value}' is read-only; write actions "
                        "require AGENT mode. Propose a plan instead."
                    ),
                )
            if spec.risk == DirectorActionRisk.HIGH_IMPACT_WRITE:
                allowed = bool(
                    session.pending_action
                    and session.pending_action.get("action") == action_name
                    and session.pending_action.get("user_confirmed") is True
                    and arguments.get("user_confirmed") is True
                )
                if not allowed:
                    pending = {
                        "action": action_name,
                        "arguments": {k: v for k, v in arguments.items() if k != "user_confirmed"},
                        "requested_at": _now().isoformat(),
                    }
                    session.pending_action = pending
                    self.repo.save_director_session(session)
                    return finish(
                        NarrativeDirectorActionStatus.PENDING,
                        {
                            "status": "awaiting_confirmation",
                            "retryable": False,
                            "code": "DIRECTOR_CONFIRMATION_REQUIRED",
                            "message": (
                                "This is a high-impact write. Ask the user to confirm explicitly; "
                                "it will only run after the user confirms in chat."
                            ),
                        },
                        code="DIRECTOR_CONFIRMATION_REQUIRED",
                        message="High-impact write requires explicit user confirmation.",
                    )
                session.pending_action = {}
                self.repo.save_director_session(session)

            runner = self._runner(action_name)
            result, artifacts = await runner(session, arguments)
            if action_name in {"patch_episode_plan", "patch_story_bible", "change_project_settings",
                               "generate_episode_draft", "revise_episode_draft", "run_forecast",
                               "select_forecast_direction", "run_persona_rehearsal"}:
                project = self.narratives.get_project(session.project_id)
                session.project_revision = project.revision
                session.story_bible_version = project.story_bible_version
                self.repo.save_director_session(session)
            return finish(
                NarrativeDirectorActionStatus.SUCCEEDED,
                {"status": "succeeded", "artifacts": artifacts, "summary": result,
                 "project_revision": session.project_revision},
            )
        except NarrativeDirectorStateConflict as exc:
            return finish(
                NarrativeDirectorActionStatus.FAILED,
                {"status": "failed", "retryable": False, "code": exc.code,
                 "message": str(exc)},
                code=exc.code,
                message=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 — action failure is data for the model.
            retryable = isinstance(exc, (TimeoutError, ConnectionError)) or bool(
                getattr(exc, "retryable", False)
            )
            return finish(
                NarrativeDirectorActionStatus.FAILED,
                {"status": "failed", "retryable": retryable, "code": "DIRECTOR_ACTION_FAILED",
                 "message": str(exc)},
                code="DIRECTOR_ACTION_FAILED",
                message=str(exc),
            )

    def _runner(self, action_name: str) -> Any:
        mapping = {
            "get_project": self._read_project,
            "get_story_bible": self._read_story_bible,
            "get_story_bible_versions": self._read_bible_versions,
            "get_characters": self._read_characters,
            "get_character_bindings": self._read_bindings,
            "get_episode_plan": self._read_episode_plan,
            "get_episode_plans": self._read_episode_plans,
            "get_episode_versions": self._read_episode_versions,
            "get_episode_version": self._read_episode_version,
            "get_episode_audits": self._read_episode_audits,
            "get_forecasts": self._read_forecasts,
            "get_selected_forecast": self._read_selected_forecast,
            "get_scenes": self._read_scenes,
            "get_rehearsal_results": self._read_rehearsal_results,
            "get_knowledge_matrix": self._read_knowledge_matrix,
            "get_audience_knowledge": self._read_audience_knowledge,
            "get_clues": self._read_clues,
            "get_plot_threads": self._read_plot_threads,
            "get_character_arcs": self._read_character_arcs,
            "get_canon": self._read_canon,
            "get_pipeline_state": self._read_pipeline_state,
            "patch_episode_plan": self._write_patch_episode_plan,
            "generate_forecast_directions": self._write_forecast_directions,
            "run_forecast": self._write_run_forecast,
            "select_forecast_direction": self._write_select_forecast,
            "run_persona_rehearsal": self._write_persona_rehearsal,
            "generate_episode_draft": self._write_generate_draft,
            "revise_episode_draft": self._write_revise_draft,
            "audit_episode": self._write_audit_episode,
            "patch_story_bible": self._write_patch_story_bible,
            "change_project_settings": self._write_project_settings,
        }
        runner = mapping.get(action_name)
        if runner is None:
            raise KeyError(f"Director action runner missing: {action_name}")
        return runner

    # -- read runners --------------------------------------------------
    async def _read_project(
        self, session: NarrativeDirectorSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        project = self.narratives.get_project(session.project_id)
        return project.model_dump(mode="json"), {}

    async def _read_story_bible(
        self, session: NarrativeDirectorSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        bible = self.repo.get_bible(session.project_id)
        if bible is None:
            raise ValueError("Story bible missing")
        return bible.model_dump(mode="json"), {"version": bible.version}

    async def _read_bible_versions(
        self, session: NarrativeDirectorSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        return self.narratives.list_bible_versions(session.project_id), {}

    async def _read_characters(
        self, session: NarrativeDirectorSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        chars = self.repo.list_characters(session.project_id)
        return [c.model_dump(mode="json") for c in chars], {"count": len(chars)}

    async def _read_bindings(
        self, session: NarrativeDirectorSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        bindings = [
            {"character_id": c.id, "name": c.name, "persona_id": c.persona_id}
            for c in self.repo.list_characters(session.project_id)
            if c.persona_id
        ]
        return bindings, {"count": len(bindings)}

    async def _read_episode_plan(
        self, session: NarrativeDirectorSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        plan = self.narratives.get_episode_plan(
            session.project_id, int(args.get("episode_number") or 0)
        )
        return plan.model_dump(mode="json"), {}

    async def _read_episode_plans(
        self, session: NarrativeDirectorSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        plans = self.repo.list_episode_plans(session.project_id)
        return [p.model_dump(mode="json") for p in plans], {"count": len(plans)}

    async def _read_episode_versions(
        self, session: NarrativeDirectorSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        versions = self.narratives.get_episode_versions(
            session.project_id, int(args.get("episode_number") or 0)
        )
        return [v.model_dump(mode="json") for v in versions], {"count": len(versions)}

    async def _read_episode_version(
        self, session: NarrativeDirectorSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        version = self.narratives.get_episode_version(str(args.get("version_id") or ""))
        return version.model_dump(mode="json"), {"version": version.version}

    async def _read_episode_audits(
        self, session: NarrativeDirectorSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        audits = self.narratives.list_audits(
            session.project_id, int(args.get("episode_number") or 0)
        )
        return [a.model_dump(mode="json") for a in audits], {"count": len(audits)}

    async def _read_forecasts(
        self, session: NarrativeDirectorSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        forecasts = self.repo.list_forecasts(session.project_id)
        return [f.model_dump(mode="json") for f in forecasts], {"count": len(forecasts)}

    async def _read_selected_forecast(
        self, session: NarrativeDirectorSession, args: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        direction = self.narratives._selected_forecast(
            session.project_id, int(args.get("episode_number") or 0)
        )
        data = (
            direction.model_dump(mode="json")
            if isinstance(direction, ForecastDirection)
            else None
        )
        return data, {}

    async def _read_scenes(
        self, session: NarrativeDirectorSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        episode = args.get("episode_number")
        scenes = self.repo.list_scenes(
            session.project_id, int(episode) if episode else None
        )
        return [s.model_dump(mode="json") for s in scenes], {"count": len(scenes)}

    async def _read_rehearsal_results(
        self, session: NarrativeDirectorSession, args: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        scenes = self.repo.list_scenes(
            session.project_id, int(args.get("episode_number") or 0)
        )
        rehearsals = [s.model_dump(mode="json") for s in scenes if s.dialogue]
        return rehearsals, {"count": len(rehearsals)}

    async def _read_knowledge_matrix(
        self, session: NarrativeDirectorSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        episode = args.get("episode_number")
        data = self.narratives.get_knowledge_matrix(
            session.project_id, int(episode) if episode else None
        )
        return data, {}

    async def _read_audience_knowledge(
        self, session: NarrativeDirectorSession, args: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        view = self.narratives.firewall.audience_view(
            self.repo.list_facts(session.project_id),
            self.repo.list_audience_knowledge(session.project_id),
        )
        return view, {}

    async def _read_clues(
        self, session: NarrativeDirectorSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        clues = self.repo.list_clues(session.project_id)
        return [c.model_dump(mode="json") for c in clues], {"count": len(clues)}

    async def _read_plot_threads(
        self, session: NarrativeDirectorSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        threads = self.repo.list_plot_threads(session.project_id)
        return [t.model_dump(mode="json") for t in threads], {"count": len(threads)}

    async def _read_character_arcs(
        self, session: NarrativeDirectorSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        arcs = self.repo.list_arcs(session.project_id)
        return [a.model_dump(mode="json") for a in arcs], {"count": len(arcs)}

    async def _read_canon(
        self, session: NarrativeDirectorSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        entries = self.repo.list_canon_entries(session.project_id)
        return [e.model_dump(mode="json") for e in entries], {"count": len(entries)}

    async def _read_pipeline_state(
        self, session: NarrativeDirectorSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        return self._pipeline_state(
            session.project_id, int(args.get("episode_number") or session.episode_number or 0)
        ), {}

    # -- write runners --------------------------------------------------
    async def _write_patch_episode_plan(
        self, session: NarrativeDirectorSession, args: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        project = self.narratives.get_project(session.project_id)
        plan = self.narratives.patch_episode_plan(
            session.project_id,
            int(args.get("episode_number") or session.episode_number or 0),
            dict(args.get("patch") or {}),
            expected_project_revision=int(args.get("expected_project_revision") or 0)
            or project.revision,
            reason=str(args.get("reason") or "director patch"),
        )
        fields = sorted((args.get("patch") or {}).keys())
        return f"已修改 EP{plan.episode_number} 剧集计划（{', '.join(fields)}）", {
            "episode_plan_id": plan.id,
            "episode_number": plan.episode_number,
            "patched_fields": fields,
        }

    async def _write_forecast_directions(
        self, session: NarrativeDirectorSession, args: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        data = await self.narratives.generate_forecast_directions(
            session.project_id,
            int(args.get("episode_number") or 0),
            count=int(args.get("count") or 3),
        )
        return (
            f"已生成 {len(data.get('directions') or [])} 条候选预测方向",
            {"directions": data.get("directions") or []},
        )

    async def _write_run_forecast(
        self, session: NarrativeDirectorSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        forecast = await self.narratives.forecast_episode_async(
            session.project_id,
            int(args.get("episode_number") or 0),
            list(args.get("directions") or []),
            horizon_episodes=int(args.get("horizon_episodes") or 3),
        )
        return (
            f"Forecast（NON-CANON）完成：{len(forecast.directions)} 个方向",
            {"forecast_id": forecast.id, "direction_ids": [d.id for d in forecast.directions]},
        )

    async def _write_select_forecast(
        self, session: NarrativeDirectorSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        forecast = self.narratives.select_forecast_direction(
            str(args.get("forecast_id") or ""), str(args.get("direction_id") or "")
        )
        return "已选择创作方向（尚未提交正史）", {"forecast_id": forecast.id}

    async def _write_persona_rehearsal(
        self, session: NarrativeDirectorSession, args: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        project_id = session.project_id
        episode_number = int(args.get("episode_number") or 0)
        characters = self.repo.list_characters(project_id)
        wanted = set(args.get("character_ids") or [])
        participants = [
            {"character_id": c.id, "name": c.name}
            for c in characters
            if c.persona_id and (not wanted or c.id in wanted)
        ]
        if len(participants) < 1:
            raise ValueError("Persona rehearsal requires characters bound to Personas")
        self.narratives.create_missing_personas(project_id)
        scene = self.narratives.create_scene(
            project_id,
            {
                "episode_number": episode_number,
                "order": len(self.repo.list_scenes(project_id, episode_number)) + 1,
                "location": str(args.get("location") or ""),
                "scene_goal": str(args.get("goal") or "Director rehearsal (NON-CANON)"),
                "background": str(args.get("background") or ""),
                "participants": participants,
            },
        )
        simulated = await self.narratives.simulate_scene(
            project_id, scene, max_turns=int(args.get("max_turns") or 2)
        )
        return (
            f"Persona 排练（NON-CANON）完成：{len(simulated.dialogue)} 句对白",
            {"scene_id": simulated.id, "dialogue_count": len(simulated.dialogue)},
        )

    async def _write_generate_draft(
        self, session: NarrativeDirectorSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        version = await self.narratives.generate_episode_draft(
            session.project_id, int(args.get("episode_number") or 0)
        )
        return (
            f"已生成剧本 {version.title} V{version.version}",
            {"episode_version_id": version.id, "version": version.version},
        )

    async def _write_revise_draft(
        self, session: NarrativeDirectorSession, args: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        episode_number = int(args.get("episode_number") or session.episode_number or 0)
        base_version_id = str(args.get("base_version_id") or "")
        if not base_version_id:
            # Default: revise from the latest draft of the bound episode.
            versions = self.repo.list_episode_versions(session.project_id, episode_number)
            if not versions:
                raise ValueError(
                    f"No existing draft to revise for EP{episode_number}; "
                    "run generate_episode_draft first"
                )
            base_version_id = versions[0].id
        runtime = self.narratives._resolve_stage_runtime(
            self.narratives.get_project(session.project_id), "director", session.runtime or None
        )
        version = await self.narratives.revise_episode_draft(
            session.project_id,
            episode_number,
            base_version_id=base_version_id,
            instructions=[str(item) for item in (args.get("instructions") or [])],
            revision_mode=str(args.get("revision_mode") or "medium"),
            created_by=f"director:{runtime.get('agent_id') or 'agent'}",
            reason=str(args.get("reason") or ""),
        )
        return (
            f"已生成修订版 V{version.version}（基于 {version.parent_version_id}）",
            {
                "episode_version_id": version.id,
                "version": version.version,
                "parent_version_id": version.parent_version_id,
            },
        )

    async def _write_audit_episode(
        self, session: NarrativeDirectorSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        version_id = str(args.get("version_id") or "")
        if not version_id:
            versions = self.repo.list_episode_versions(
                session.project_id, int(args.get("episode_number") or session.episode_number or 0)
            )
            if not versions:
                raise ValueError("No draft version to audit")
            version_id = versions[0].id
        report = await self.narratives.audit_episode_async(session.project_id, version_id)
        blocking = [f.message for f in report.findings if f.severity.value == "blocking"]
        summary = (
            f"连续性审核：{'通过' if report.passed else '未通过'} · "
            f"BLOCKING {report.blocking_count} · WARNING {report.warning_count}"
        )
        if blocking:
            summary += "\n" + "\n".join(f"- {m}" for m in blocking[:6])
        return summary, {
            "audit_id": report.id,
            "episode_version_id": report.episode_version_id,
            "passed": report.passed,
            "blocking_count": report.blocking_count,
            "warning_count": report.warning_count,
        }

    async def _write_patch_story_bible(
        self, session: NarrativeDirectorSession, args: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        bible = self.narratives.save_bible(
            session.project_id,
            dict(args.get("patch") or {}),
        )
        return (
            f"Story Bible 已更新到 V{bible.version}（下游产物已标记 stale）",
            {"story_bible_version": bible.version},
        )

    async def _write_project_settings(
        self, session: NarrativeDirectorSession, args: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        project = self.narratives.update_project(
            session.project_id, dict(args.get("updates") or {})
        )
        return "作品设定已更新", {"project_revision": project.revision}

    # ------------------------------------------------------------------
    # Context builder (layered, budgeted)
    # ------------------------------------------------------------------
    def _build_context(
        self,
        session: NarrativeDirectorSession,
        project: Any,
        tool_results: list[dict[str, Any]],
    ) -> str:
        n = self.narratives
        repo = self.repo
        ctx: dict[str, Any] = {
            "task": "narrative_director",
            "mode": session.mode.value,
            "status": session.status.value,
            "project": {
                "id": project.id,
                "title": project.title,
                "logline": project.logline,
                "format": project.format.value,
                "status": project.status.value,
                "revision": project.revision,
                "story_bible_version": project.story_bible_version,
                "planned_episode_count": project.planned_episode_count,
                "episode_bound": session.episode_number,
            },
            "allowed_actions": [
                {"name": spec.name, "risk": spec.risk.value, "description": spec.description}
                for spec in ACTION_REGISTRY.values()
            ],
        }
        bible = repo.get_bible(project.id)
        if bible is not None:
            # Author/Director layer: Story Truth is visible here, but it must
            # never flow into character-facing rehearsal prompts (the rehearsal
            # runner goes through the existing knowledge firewall).
            ctx["story_bible_summary"] = {
                "version": bible.version,
                "premise": bible.premise[:1200],
                "core_question": bible.core_question[:600],
                "theme": bible.theme[:400],
                "world_rules": bible.world_rules[:8],
                "final_truth": bible.final_truth[:8],
                "narrative_constraints": bible.narrative_constraints[:8],
            }
        if session.episode_number:
            ctx["episode"] = self._episode_state(project.id, session.episode_number)
        ctx["characters"] = [
            {"id": c.id, "name": c.name, "role": c.role, "persona_id": c.persona_id}
            for c in repo.list_characters(project.id)
        ]
        messages = repo.list_director_messages(session.id)
        if session.pending_action:
            ctx["pending_confirmation"] = session.pending_action
        ctx["recent_conversation"] = [
            {"role": m.role, "content": m.content[:800]} for m in messages[-_MAX_CONTEXT_MESSAGES:]
        ]
        ctx["recent_tool_results"] = tool_results[-_MAX_CONTEXT_ACTION_RESULTS:]
        ctx["context_fingerprint"] = n.context_fingerprint(project)
        return dumps(ctx)

    def _episode_state(self, project_id: str, episode_number: int) -> dict[str, Any]:
        state = self._pipeline_state(project_id, episode_number)
        plan = self.repo.get_episode_plan(project_id, episode_number)
        if plan is not None:
            dump = plan.model_dump(mode="json")
            dump.pop("runtime_trace", None)
            state["episode_plan"] = dump
        return state

    def _pipeline_state(self, project_id: str, episode_number: int) -> dict[str, Any]:
        repo = self.repo
        state: dict[str, Any] = {"episode_number": episode_number}
        plan = repo.get_episode_plan(project_id, episode_number) if episode_number else None
        state["has_plan"] = plan is not None
        state["plan_status"] = plan.status.value if plan else None
        versions = (
            repo.list_episode_versions(project_id, episode_number) if episode_number else []
        )
        latest = versions[0] if versions else None
        state["latest_version"] = (
            {
                "id": latest.id,
                "version": latest.version,
                "title": latest.title,
                "stale": latest.stale,
                "is_canon": latest.is_canon,
                "parent_version_id": latest.parent_version_id,
                "revision_mode": latest.revision_mode,
                "created_by": latest.created_by,
            }
            if latest
            else None
        )
        audits = repo.list_audits(project_id, episode_number) if episode_number else []
        latest_audit = audits[0] if audits else None
        state["latest_audit"] = (
            {
                "id": latest_audit.id,
                "episode_version_id": latest_audit.episode_version_id,
                "passed": latest_audit.passed,
                "blocking_count": latest_audit.blocking_count,
                "warning_count": latest_audit.warning_count,
                "blocking": [
                    f.message for f in latest_audit.findings if f.severity.value == "blocking"
                ][:6],
            }
            if latest_audit
            else None
        )
        direction = (
            self.narratives._selected_forecast(project_id, episode_number)
            if episode_number
            else None
        )
        state["forecast_selected"] = (
            {"label": direction.label, "description": direction.description[:400]}
            if isinstance(direction, ForecastDirection)
            else None
        )
        state["pipeline_step_done"] = {
            "plan": plan is not None,
            "draft": latest is not None,
            "audit": bool(latest_audit and latest_audit.passed),
            "canon": bool(latest and latest.is_canon),
        }
        if plan and plan.status == EpisodeStatus.CANON:
            state["committed"] = True
        return state

    def _gate_status(
        self, project_id: str, episode_number: int | None
    ) -> NarrativeDirectorSessionStatus:
        """Status after a conversational turn: keep the canon gate visible."""
        if episode_number:
            state = self._pipeline_state(project_id, episode_number)
            audit = state.get("latest_audit") or {}
            latest = state.get("latest_version") or {}
            if audit.get("passed") and not latest.get("is_canon"):
                return NarrativeDirectorSessionStatus.WAITING_FOR_CANON_APPROVAL
            if latest.get("is_canon"):
                return NarrativeDirectorSessionStatus.COMPLETED
        return NarrativeDirectorSessionStatus.ACTIVE

    def _append_message(self, session_id: str, role: str, content: str) -> None:
        if not content:
            return
        self.repo.append_director_message(
            NarrativeDirectorMessage(session_id=session_id, role=role, content=content)
        )

    def _transition(
        self, session_id: str, status: NarrativeDirectorSessionStatus
    ) -> None:
        session = self.repo.get_director_session(session_id)
        if session is None:
            return
        # Pause/cancel always wins over a loop-status transition.
        if session.status in {
            NarrativeDirectorSessionStatus.PAUSED,
            NarrativeDirectorSessionStatus.CANCELLED,
        }:
            return
        session.status = status
        session.updated_at = _now()
        self.repo.save_director_session(session)
