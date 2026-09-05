"""NarrativeShootingService — host-owned action loop for the Shooting Agent.

The Shooting model only ever emits a structured decision JSON; THIS service
validates permissions, dispatches actions to the production pipeline,
persists every action, and owns the stop rules:

- Story/canon firewall: the registry contains NO story/canon capability, so
  the agent physically cannot touch the story bible, episode plans, drafts,
  audits, canon or personas (see ``narrative/shooting.py``).
- Circuit breaker: three consecutive failing actions park the session at
  ``NEEDS_HUMAN_GUIDANCE`` instead of burning the turn budget.
- No canon gate / no audit repair loop / no HIGH_IMPACT confirmation: every
  SAFE_WRITE action executes directly and persists.

Sessions are logical conversations persisted in SQLite; every model turn uses
the existing turn-scoped runtime executor (no persistent runtime lease).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

from persona_continuum.application._utils import dumps
from persona_continuum.domain.narrative import (
    GenerationClip,
    GenerationMode,
    ModelPromptPackage,
    NarrativeProject,
    NarrativeShootingAction,
    NarrativeShootingMessage,
    NarrativeShootingSession,
    ProductionAsset,
    ProductionPackage,
    ShootingActionStatus,
    ShootingMessageRole,
    ShootingSessionStatus,
)
from persona_continuum.narrative.clip_planner import plan_clips
from persona_continuum.narrative.repository import NarrativeRepository
from persona_continuum.narrative.runtime import (
    NARRATIVE_AGENT_RUNTIME_UNAVAILABLE,
    NarrativeAgentError,
)
from persona_continuum.narrative.shooting import (
    SHOOTING_ACTION_NOT_ALLOWED,
    SHOOTING_ACTION_REGISTRY,
    SHOOTING_DECISION_SCHEMA,
    SHOOTING_MAX_ACTIONS_PER_TURN,
    SHOOTING_MODE_ALIASES,
    SHOOTING_SESSION_BUSY,
    SHOOTING_SESSION_NOT_FOUND,
    SHOOTING_SYSTEM_PROMPT,
    ShootingActionRisk,
    build_shooting_context,
    missing_shooting_required_arguments,
    normalize_shooting_action_arguments,
)
from persona_continuum.narrative.video_profile_registry import (
    get_profile,
    list_profiles,
    profile_capabilities_digest,
)
from persona_continuum.narrative.video_prompt_compiler import (
    compile_clip_prompt,
    validate_clip_against_profile,
)

_MAX_CONTEXT_MESSAGES = 12
_MAX_CONTEXT_ACTION_RESULTS = 3
_MAX_EPISODE_SCREENPLAY_CHARS = 4000
_MAX_ASSETS_IN_CONTEXT = 24

# READ_ONLY actions that map onto one production-package attribute. Their
# runners share a single compact reader (data-driven dispatch).
_PRODUCTION_SECTION_ATTRS = {
    "get_shot_list": "shot_list",
    "get_character_visual_bible": "character_visual_bible",
    "get_location_visual_bible": "location_visual_bible",
    "get_prop_visual_bible": "prop_visual_bible",
    "get_dialogue_track": "dialogue_track",
    "get_subtitle_track": "subtitle_track",
    "get_sfx_plan": "sound_effect_plan",
    "get_bgm_direction": "bgm_direction",
    "get_continuity_notes": "continuity_notes",
}


def _now() -> datetime:
    return datetime.now(UTC)


class ShootingSessionBusyError(Exception):
    """A turn loop is already running for this session."""


class NarrativeShootingService:
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
        mode: str = "agent",
        runtime: dict[str, Any] | None = None,
    ) -> NarrativeShootingSession:
        self.narratives.get_project(project_id)  # existence check
        session = NarrativeShootingSession(
            project_id=project_id,
            episode_number=episode_number,
            mode=self._normalize_mode(mode),
            runtime=dict(runtime or {}),
        )
        self.repo.save_shooting_session(session)
        return session

    def list_sessions(self, project_id: str) -> list[NarrativeShootingSession]:
        return self.repo.list_shooting_sessions(project_id)

    def reclaim_orphaned_sessions(self) -> int:
        """Recover sessions left RUNNING by a process restart.

        Loop tasks live only in memory, so any RUNNING session at startup is
        orphaned: it would otherwise block every future message forever via
        the busy guard. Park such sessions at WAITING_FOR_USER and cancel
        their in-flight actions.
        """
        recovered = 0
        rows = self.repo.db.conn.execute(
            "SELECT id FROM narrative_shooting_sessions WHERE status = 'running'"
        ).fetchall()
        for row in rows:
            session = self.repo.get_shooting_session(str(row["id"]))
            if session is None:
                continue
            session.status = ShootingSessionStatus.WAITING_FOR_USER
            session.last_error = "Shooting 运行因服务重启中断，可重新发送消息继续。"
            self.repo.save_shooting_session(session)
            self.repo.cancel_running_shooting_actions(session.id)
            self._append_message(
                session.id, "system", "Shooting 运行因服务重启中断，可重新发送消息继续。"
            )
            recovered += 1
        return recovered

    def get_session(self, session_id: str) -> NarrativeShootingSession:
        session = self.repo.get_shooting_session(session_id)
        if session is None:
            raise KeyError(
                f"{SHOOTING_SESSION_NOT_FOUND}: shooting session not found: {session_id}"
            )
        return session

    def session_snapshot(self, session_id: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        return {
            "session": session.model_dump(mode="json"),
            "messages": [
                m.model_dump(mode="json") for m in self.repo.list_shooting_messages(session_id)
            ],
            "actions": [
                a.model_dump(mode="json") for a in self.repo.list_shooting_actions(session_id)
            ],
        }

    def update_mode(
        self,
        session_id: str,
        mode: str | None,
        *,
        runtime: dict[str, Any] | None = None,
    ) -> NarrativeShootingSession:
        session = self.get_session(session_id)
        if mode:
            session.mode = self._normalize_mode(mode)
        if isinstance(runtime, dict) and runtime.get("agent_id"):
            session.runtime = dict(runtime)
        self.repo.save_shooting_session(session)
        return session

    def send_message(
        self,
        session_id: str,
        content: str,
        *,
        mode: str | None = None,
        runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a user message and start (or continue) the action loop."""
        content = (content or "").strip()
        if not content:
            raise ValueError("Message content is required")
        session = self.get_session(session_id)
        if session.status == ShootingSessionStatus.RUNNING:
            task = self._tasks.get(session_id)
            if task is not None and not task.done():
                raise ShootingSessionBusyError(
                    f"{SHOOTING_SESSION_BUSY}: shooting session is already running"
                )
            # Orphaned loop (e.g. the worker task died with the process):
            # recover instead of blocking the session forever.
            session.status = ShootingSessionStatus.WAITING_FOR_USER
            self.repo.save_shooting_session(session)
            self.repo.cancel_running_shooting_actions(session.id)
        self.narratives.get_project(session.project_id)
        self.repo.append_shooting_message(
            NarrativeShootingMessage(
                session_id=session_id, role=ShootingMessageRole.USER, content=content
            )
        )
        if mode:
            session.mode = self._normalize_mode(mode)
        if isinstance(runtime, dict) and runtime.get("agent_id"):
            session.runtime = dict(runtime)
        session.status = ShootingSessionStatus.RUNNING
        session.last_error = ""
        session.updated_at = _now()
        self.repo.save_shooting_session(session)
        self._start_loop(session_id)
        return self.session_snapshot(session_id)

    def pause_session(self, session_id: str) -> NarrativeShootingSession:
        session = self.get_session(session_id)
        if session.status == ShootingSessionStatus.RUNNING:
            session.status = ShootingSessionStatus.PAUSED
            self.repo.save_shooting_session(session)
        return session

    def resume_session(self, session_id: str) -> NarrativeShootingSession:
        session = self.get_session(session_id)
        if session.status == ShootingSessionStatus.PAUSED:
            session.status = ShootingSessionStatus.ACTIVE
            self.repo.save_shooting_session(session)
        return session

    def cancel_session(self, session_id: str) -> NarrativeShootingSession:
        session = self.get_session(session_id)
        session.status = ShootingSessionStatus.CANCELLED
        self.repo.save_shooting_session(session)
        self.repo.cancel_running_shooting_actions(session.id)
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
            self._run_loop(session_id), name=f"narrative-shooting-{session_id}"
        )
        self._tasks[session_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(session_id, None))

    # ------------------------------------------------------------------
    # Action loop
    # ------------------------------------------------------------------
    async def _run_loop(self, session_id: str) -> None:
        consecutive_failures = 0
        turns = 0
        tool_results: list[dict[str, Any]] = []
        try:
            while turns < SHOOTING_MAX_ACTIONS_PER_TURN:
                session = self.get_session(session_id)
                if session.status != ShootingSessionStatus.RUNNING:
                    return  # paused / cancelled between turns
                project = self.narratives.get_project(session.project_id)
                context_message = self._build_context(session, project, tool_results)
                decision = await self._shooting_turn(project, session, context_message)
                turns += 1
                kind = str(decision.get("decision") or "reply")

                if kind == "execute_action":
                    result = await self._execute_action(
                        session,
                        str(decision.get("action") or ""),
                        dict(decision.get("arguments") or {}),
                    )
                    tool_results.append({"action": decision.get("action"), "result": result})
                    if result.get("status") in {"failed", "rejected"}:
                        self._append_message(
                            session_id,
                            "assistant",
                            f"[{decision.get('action')}] 操作未执行："
                            f"{result.get('message') or result.get('code')}",
                        )
                        # Circuit breaker: a repeated failing action means the
                        # model cannot self-correct; stop instead of burning
                        # the whole turn budget on identical errors.
                        consecutive_failures += 1
                        if consecutive_failures >= 3:
                            self._append_message(
                                session_id,
                                "assistant",
                                "连续 3 次操作失败，已停止自动执行，需要你检查：\n"
                                f"最近错误：{result.get('message') or result.get('code')}",
                            )
                            self._transition(
                                session_id, ShootingSessionStatus.NEEDS_HUMAN_GUIDANCE
                            )
                            return
                    else:
                        consecutive_failures = 0
                        summary = str(result.get("summary") or "")
                        if summary:
                            self._append_message(session_id, "assistant", summary)
                    continue

                message = str(decision.get("reply_text") or "")
                if kind == "finish" or not message:
                    message = str(decision.get("finish_summary") or "") or message
                if message:
                    self._append_message(session_id, "assistant", message)
                self._transition(session_id, ShootingSessionStatus.ACTIVE)
                return
            # Turn budget exhausted without reaching a stop decision.
            session = self.get_session(session_id)
            session.last_error = "Shooting action loop turn budget exhausted"
            self.repo.save_shooting_session(session)
            self._append_message(
                session_id,
                "assistant",
                "本轮操作步数已达上限，已暂停等待你的指示。你可以继续补充要求。",
            )
            self._transition(session_id, ShootingSessionStatus.NEEDS_HUMAN_GUIDANCE)
        except asyncio.CancelledError:
            raise
        except NarrativeAgentError as exc:
            session = self.get_session(session_id)
            session.status = ShootingSessionStatus.FAILED
            session.last_error = exc.to_dict().get("message", str(exc))
            self.repo.save_shooting_session(session)
            self._append_message(session_id, "system", f"Shooting 运行失败：{session.last_error}")
        except Exception as exc:  # noqa: BLE001 — the loop owns error recovery.
            session = self.get_session(session_id)
            session.status = ShootingSessionStatus.FAILED
            session.last_error = str(exc)
            self.repo.save_shooting_session(session)
            self._append_message(session_id, "system", f"Shooting 运行失败：{exc}")

    async def _shooting_turn(
        self,
        project: NarrativeProject,
        session: NarrativeShootingSession,
        context_message: str,
    ) -> dict[str, Any]:
        runtime = self.narratives._resolve_stage_runtime(
            project, "shooting_agent", session.runtime or None
        )
        # The shared structured-call helper owns the generic system prompt;
        # the Shooting contract travels with the user message so every
        # transport (CLI harness, API) receives identical instructions.
        value, _trace = await self.narratives._structured_call_async(
            f"{SHOOTING_SYSTEM_PROMPT}\n\n=== CURRENT CONTEXT ===\n{context_message}",
            runtime=runtime,
            phase="shooting_agent",
            mode=GenerationMode.AGENT,
            schema=SHOOTING_DECISION_SCHEMA,
            structured_repair_attempts=2,
        )
        if not isinstance(value, dict):
            raise NarrativeAgentError(
                NARRATIVE_AGENT_RUNTIME_UNAVAILABLE,
                "Shooting Agent requires a configured Agent runtime (no deterministic fallback)",
                stage="shooting_agent",
                runtime=runtime,
            )
        return value

    # ------------------------------------------------------------------
    # Action dispatch
    # ------------------------------------------------------------------
    async def _execute_action(
        self, session: NarrativeShootingSession, action_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        spec = SHOOTING_ACTION_REGISTRY.get(action_name)
        arguments = normalize_shooting_action_arguments(
            action_name,
            arguments,
            session_project_id=session.project_id,
            session_episode_number=session.episode_number,
        )
        record = NarrativeShootingAction(
            session_id=session.id,
            action=action_name or "unknown",
            arguments=arguments,
            status=ShootingActionStatus.RUNNING,
        )
        self.repo.save_shooting_action(record)

        def finish(
            status: ShootingActionStatus,
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
            self.repo.save_shooting_action(record)
            return {"action_id": record.id, "action": record.action, **result}

        try:
            if spec is None:
                # Includes every story/canon capability: they are not registered.
                return finish(
                    ShootingActionStatus.REJECTED,
                    {"status": "rejected", "retryable": False},
                    code=SHOOTING_ACTION_NOT_ALLOWED,
                    message=(
                        f"Action not allowed: {action_name}. The Shooting Agent cannot "
                        "modify story, canon, drafts, or personas."
                    ),
                )
            missing = missing_shooting_required_arguments(spec, arguments)
            if missing:
                return finish(
                    ShootingActionStatus.FAILED,
                    {
                        "status": "failed",
                        "retryable": False,
                        "code": "SHOOTING_ACTION_INVALID_ARGUMENTS",
                        "message": (
                            f"Missing required argument(s): {', '.join(missing)}. "
                            f"Accepted fields for {action_name}: {sorted(spec.arguments)}."
                        ),
                    },
                    code="SHOOTING_ACTION_INVALID_ARGUMENTS",
                    message=f"Missing required argument(s): {', '.join(missing)}",
                )
            if spec.risk != ShootingActionRisk.READ_ONLY and session.mode != "agent":
                return finish(
                    ShootingActionStatus.REJECTED,
                    {"status": "rejected", "retryable": False},
                    code=SHOOTING_ACTION_NOT_ALLOWED,
                    message=(
                        f"Mode '{session.mode}' is read-only; write actions require "
                        "AGENT (代理) mode. Propose a plan instead."
                    ),
                )
            runner = self._runner(action_name)
            summary, artifacts = await runner(session, arguments)
            return finish(
                ShootingActionStatus.SUCCEEDED,
                {"status": "succeeded", "artifacts": artifacts, "summary": summary},
            )
        except NarrativeAgentError as exc:
            return finish(
                ShootingActionStatus.FAILED,
                {
                    "status": "failed",
                    "retryable": bool(exc.retryable),
                    "code": exc.code,
                    "message": str(exc),
                },
                code=exc.code,
                message=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 — action failure is data for the model.
            retryable = isinstance(exc, (TimeoutError, ConnectionError)) or bool(
                getattr(exc, "retryable", False)
            )
            return finish(
                ShootingActionStatus.FAILED,
                {
                    "status": "failed",
                    "retryable": retryable,
                    "code": "SHOOTING_ACTION_FAILED",
                    "message": str(exc),
                },
                code="SHOOTING_ACTION_FAILED",
                message=str(exc),
            )

    def _runner(self, action_name: str) -> Any:
        section = _PRODUCTION_SECTION_ATTRS.get(action_name)
        if section is not None:
            return self._make_section_runner(section)
        mapping: dict[str, Any] = {
            "get_production_package": self._read_production_package,
            "get_video_model_profiles": self._read_video_model_profiles,
            "get_model_prompt_packages": self._read_prompt_packages,
            "get_production_assets": self._read_production_assets,
            "get_canon_episode": self._read_canon_episode,
            "create_clip_plan": self._write_create_clip_plan,
            "revise_clip_plan": self._write_revise_clip_plan,
            "create_model_prompt_package": self._write_create_model_prompt_package,
            "revise_generation_clip_prompt": self._write_revise_clip_prompt,
            "set_clip_reference_assets": self._write_set_reference_assets,
            "set_generation_mode": self._write_set_generation_mode,
            "set_target_video_model": self._write_set_target_model,
            "set_continuity_strategy": self._write_set_continuity_strategy,
            "build_complete_production_guide": self._write_build_complete_production_guide,
        }
        runner = mapping.get(action_name)
        if runner is None:
            raise KeyError(f"Shooting action runner missing: {action_name}")
        return runner

    def _make_section_runner(self, section: str) -> Any:
        async def _runner(
            session: NarrativeShootingSession, args: dict[str, Any]
        ) -> tuple[Any, dict[str, Any]]:
            return await self._read_production_section(session, args, section)

        return _runner

    # ------------------------------------------------------------------
    # Resolution helpers
    # ------------------------------------------------------------------
    def _normalize_mode(self, mode: str | None) -> str:
        value = str(mode or "agent").strip().lower()
        normalized = SHOOTING_MODE_ALIASES.get(value)
        if normalized is None:
            raise ValueError(
                f"Invalid shooting session mode: {mode}. "
                "Expected one of: 讨论/建议/代理 (discuss/advise/agent)."
            )
        return normalized

    def _resolve_production_package(
        self, session: NarrativeShootingSession, args: dict[str, Any]
    ) -> ProductionPackage:
        package_id = str(args.get("production_package_id") or "")
        if package_id:
            package = self.repo.get_production_package(package_id)
            if package is None:
                raise ValueError(f"Production package not found: {package_id}")
            if package.project_id != session.project_id:
                raise ValueError("Production package belongs to another project")
            return package
        package = self._latest_production_package(session)
        if package is None:
            raise ValueError(
                "No production package available for this session; "
                "generate one from the canon episode first"
            )
        return package

    def _latest_production_package(
        self, session: NarrativeShootingSession
    ) -> ProductionPackage | None:
        packages = [
            p
            for p in self.repo.list_production_packages(session.project_id)
            if not p.is_preview
        ]
        if session.episode_number is not None:
            episode_packages = [
                p for p in packages if p.episode_number == session.episode_number
            ]
            if episode_packages:
                return episode_packages[0]
        return packages[0] if packages else None

    def _resolve_prompt_package(
        self, session: NarrativeShootingSession, args: dict[str, Any]
    ) -> ModelPromptPackage:
        package_id = str(args.get("prompt_package_id") or "")
        if package_id:
            package = self.repo.get_model_prompt_package(package_id)
            if package is None:
                raise ValueError(f"Model prompt package not found: {package_id}")
        else:
            packages = [
                p
                for p in self.repo.list_model_prompt_packages(session.project_id)
                if not p.stale and p.status != "failed"
            ]
            if session.episode_number is not None:
                episode_packages = [
                    p for p in packages if p.episode_number == session.episode_number
                ]
                if episode_packages:
                    packages = episode_packages
            if not packages:
                raise ValueError(
                    "No model prompt package available; run create_model_prompt_package first"
                )
            package = packages[0]
        if package.project_id != session.project_id:
            raise ValueError("Model prompt package belongs to another project")
        return package

    def _clip_by_id(self, package: ModelPromptPackage, clip_id: Any) -> GenerationClip:
        key = str(clip_id or "")
        for clip in package.clips:
            if clip.id == key:
                return clip
        if key.isdigit():
            number = int(key)
            for clip in package.clips:
                if clip.clip_number == number:
                    return clip
        raise ValueError(f"Clip not found in package {package.id}: {clip_id}")

    def _assets_by_id(self, project_id: str) -> dict[str, ProductionAsset]:
        return {asset.id: asset for asset in self.repo.list_production_assets(project_id)}

    def _record_revision(
        self,
        package: ModelPromptPackage,
        action: str,
        instruction: str,
        changed: list[str],
    ) -> None:
        """Append a bounded provenance entry for in-place package edits."""
        package.revision_reason = instruction
        trace = dict(package.runtime_trace)
        revisions = list(trace.get("revisions") or [])
        revisions.append(
            {
                "action": action,
                "instruction": instruction[:400],
                "changed": changed,
                "at": _now().isoformat(),
            }
        )
        trace["revisions"] = revisions[-20:]
        package.runtime_trace = trace

    def _require_production(self, package: ModelPromptPackage) -> ProductionPackage:
        production = self.repo.get_production_package(package.production_package_id)
        if production is None:
            raise ValueError(f"Production package not found: {package.production_package_id}")
        return production

    # ------------------------------------------------------------------
    # Read runners
    # ------------------------------------------------------------------
    async def _read_production_section(
        self, session: NarrativeShootingSession, args: dict[str, Any], section: str
    ) -> tuple[Any, dict[str, Any]]:
        package = self._resolve_production_package(session, args)
        if section == "shot_list":
            shots = [
                {
                    "shot_number": shot.shot_number,
                    "duration_seconds": shot.duration_seconds,
                    "shot_size": shot.shot_size,
                    "camera": shot.camera,
                    "movement": shot.movement,
                    "characters": shot.characters,
                    "location": shot.location,
                    "action": shot.action[:300],
                    "dialogue": shot.dialogue[:300],
                    "visual_intent": shot.visual_intent[:300],
                    "transition": shot.transition,
                }
                for shot in package.shot_list
            ]
            return shots, {"count": len(shots)}
        value: Any = getattr(package, section)
        count = len(value) if isinstance(value, list) else 0
        return value, {"count": count}

    async def _read_production_package(
        self, session: NarrativeShootingSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        package = self._resolve_production_package(session, args)
        return self._compact_production_package(package, screenplay_chars=4000), {
            "production_package_id": package.id
        }

    async def _read_video_model_profiles(
        self, session: NarrativeShootingSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        digests = [
            {**profile_capabilities_digest(profile), "official_sources": profile.official_sources}
            for profile in list_profiles()
        ]
        return digests, {"count": len(digests)}

    async def _read_prompt_packages(
        self, session: NarrativeShootingSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        production_package_id = str(args.get("production_package_id") or "") or None
        packages = self.repo.list_model_prompt_packages(
            session.project_id, production_package_id
        )
        payload = [self._compact_prompt_package_summary(p) for p in packages]
        return payload, {"count": len(payload)}

    async def _read_production_assets(
        self, session: NarrativeShootingSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        production_package_id = str(args.get("production_package_id") or "") or None
        assets = self.repo.list_production_assets(session.project_id, production_package_id)
        payload = [self._compact_asset(asset) for asset in assets]
        return payload, {"count": len(payload)}

    async def _read_canon_episode(
        self, session: NarrativeShootingSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        episode_number = int(args.get("episode_number") or session.episode_number or 0)
        if not episode_number:
            raise ValueError(
                "No episode bound to this session; pass episode_number explicitly"
            )
        return self._canon_episode_summary(session.project_id, episode_number), {}

    # ------------------------------------------------------------------
    # Write runners
    # ------------------------------------------------------------------
    async def _write_create_clip_plan(
        self, session: NarrativeShootingSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        package = await self._run_generation_pipeline(session, args)
        return (
            f"已生成 Clip 计划（{package.target_video_model_display_name}，"
            f"{len(package.clips)} 个片段，状态 {package.status}）",
            {
                "prompt_package_id": package.id,
                "status": package.status,
                "clip_count": len(package.clips),
            },
        )

    async def _write_create_model_prompt_package(
        self, session: NarrativeShootingSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        package = await self._run_generation_pipeline(session, args)
        return (
            f"已生成提示词包（{package.target_video_model_display_name}，"
            f"{len(package.clips)} 个片段，状态 {package.status}）",
            {
                "prompt_package_id": package.id,
                "status": package.status,
                "clip_count": len(package.clips),
            },
        )

    async def _run_generation_pipeline(
        self, session: NarrativeShootingSession, args: dict[str, Any]
    ) -> ModelPromptPackage:
        # Direct (job-less) invocation: progress_callback is optional in the
        # Task B pipeline, so None keeps the four-stage execution inline
        # within this action loop without creating a background job row.
        result = await self.narratives.generate_model_prompt_package_async(
            session.project_id,
            str(args.get("production_package_id") or ""),
            str(args.get("profile_id") or ""),
            aspect_ratio=str(args.get("aspect_ratio") or "16:9"),
            quality_priority=str(args.get("quality_priority") or "balanced"),
            generation_strategy=str(args.get("generation_strategy") or "auto"),
            continuity_strategy=str(args.get("continuity_strategy") or "auto"),
            audio_strategy=str(args.get("audio_strategy") or "auto"),
            prompt_language=str(args.get("prompt_language") or "auto"),
            progress_callback=None,
        )
        # The narrative service is typed as Any here; the Task B pipeline
        # contract guarantees a ModelPromptPackage or an exception.
        return cast(ModelPromptPackage, result)

    async def _write_build_complete_production_guide(
        self, session: NarrativeShootingSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        prompt_package = self._resolve_prompt_package(session, args)
        # Direct (job-less) invocation: the guide pipeline reports progress
        # through an optional callback; None keeps execution inline within
        # this action loop. Location-context failures (fail-closed before
        # any LLM call) propagate through the existing NarrativeAgentError
        # finish path as a failed action result.
        guide = await self.narratives.generate_video_production_guide_async(
            session.project_id,
            prompt_package.id,
            progress_callback=None,
        )
        return (
            f"手册已生成：{len(guide.required_assets)} 个素材、"
            f"{len(guide.clip_workflows)} 个 Clip、状态 {guide.status}",
            {
                "production_guide_id": guide.id,
                "status": guide.status,
                "asset_count": len(guide.required_assets),
                "clip_count": len(guide.clip_workflows),
            },
        )

    async def _write_revise_clip_plan(
        self, session: NarrativeShootingSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        package = self._resolve_prompt_package(session, args)
        instruction = str(args.get("instruction") or "")
        aspect_ratio = str(args.get("aspect_ratio") or "")
        quality_priority = str(args.get("quality_priority") or "")
        prompt_language = str(args.get("prompt_language") or "")
        clip_durations = args.get("clip_durations") or {}
        if not isinstance(clip_durations, dict):
            raise ValueError("clip_durations must be an object of clip_id -> seconds")
        changed: list[str] = []
        if aspect_ratio or quality_priority or prompt_language:
            changed.append("replan")
            package = self._replan_clips(
                package,
                aspect_ratio=aspect_ratio,
                quality_priority=quality_priority,
                prompt_language=prompt_language,
                project_id=session.project_id,
            )
        if clip_durations:
            profile = get_profile(package.target_profile_id)
            updated: list[GenerationClip] = []
            for clip in package.clips:
                raw = clip_durations.get(str(clip.id), clip_durations.get(str(clip.clip_number)))
                if raw is None:
                    updated.append(clip)
                    continue
                try:
                    duration = float(raw)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"Invalid duration for clip {clip.clip_number}: {raw!r}"
                    ) from exc
                candidate = clip.model_copy(
                    update={
                        "duration_seconds": duration,
                        "recommended_settings": {
                            **clip.recommended_settings,
                            # Keep the single duration source of truth (task
                            # #29); the legacy duration_seconds key is dropped.
                            "actual_generation_duration": duration,
                        },
                    }
                )
                self._validate_clip_or_raise(candidate, profile)
                updated.append(candidate)
                changed.append(f"clip_{clip.clip_number}_duration")
            package.clips = updated
        if not changed:
            raise ValueError(
                "revise_clip_plan needs at least one of aspect_ratio/quality_priority/"
                "prompt_language/clip_durations to apply"
            )
        self._record_revision(package, "revise_clip_plan", instruction, changed)
        self.repo.save_model_prompt_package(package)
        return (
            f"已修订 Clip 计划（{', '.join(changed)}）",
            {
                "prompt_package_id": package.id,
                "clip_count": len(package.clips),
                "changed": changed,
            },
        )

    def _replan_clips(
        self,
        package: ModelPromptPackage,
        *,
        aspect_ratio: str,
        quality_priority: str,
        prompt_language: str,
        project_id: str,
    ) -> ModelPromptPackage:
        """Deterministic re-plan + recompile for package-level options."""
        production = self._require_production(package)
        profile = get_profile(package.target_profile_id)
        old_clips = package.clips
        clips = plan_clips(
            production.shot_list,
            profile,
            aspect_ratio or package.aspect_ratio,
            quality_priority or package.quality_priority,
        )
        # Pinned reference assets survive a re-plan: each old clip's pinned
        # assets apply to every new clip whose source shots overlap.
        pinned_by_shots = [
            (set(old.source_shot_numbers), list(old.reference_asset_ids))
            for old in old_clips
            if old.reference_asset_ids
        ]
        if pinned_by_shots:
            migrated: list[GenerationClip] = []
            for clip in clips:
                ref_ids = list(clip.reference_asset_ids)
                overlap = set(clip.source_shot_numbers)
                for old_shots, old_ids in pinned_by_shots:
                    if overlap & old_shots:
                        ref_ids.extend(
                            asset_id for asset_id in old_ids if asset_id not in ref_ids
                        )
                migrated.append(
                    clip
                    if ref_ids == clip.reference_asset_ids
                    else clip.model_copy(update={"reference_asset_ids": ref_ids})
                )
            clips = migrated
        for clip in clips:
            self._validate_clip_or_raise(clip, profile)
        assets_by_id = self._assets_by_id(project_id)
        package.clips = [
            compile_clip_prompt(
                clip,
                profile,
                production,
                assets_by_id,
                prompt_language or package.prompt_language,
            )
            for clip in clips
        ]
        if aspect_ratio:
            package.aspect_ratio = aspect_ratio
        if quality_priority:
            package.quality_priority = quality_priority
        if prompt_language:
            package.prompt_language = prompt_language
        # Rebuild asset requirements against the new clip plan (same rule as
        # the recompile path) so entries track the fresh clip numbering
        # instead of the superseded one.
        package.asset_requirements = self._rebuild_asset_requirements(
            package.clips, assets_by_id
        )
        return package

    def _validate_clip_or_raise(self, clip: GenerationClip, profile: Any) -> None:
        violations = validate_clip_against_profile(clip, profile)
        if violations:
            raise NarrativeAgentError(
                violations[0],
                f"Clip {clip.clip_number} violates target video model profile "
                f"'{profile.id}': {', '.join(violations)}",
                stage="shooting_agent",
            )

    async def _write_revise_clip_prompt(
        self, session: NarrativeShootingSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        package = self._resolve_prompt_package(session, args)
        clip = self._clip_by_id(package, args.get("clip_id"))
        instruction = str(args.get("instruction") or "")
        update: dict[str, Any] = {
            "compiler_trace": {
                **clip.compiler_trace,
                "revised_by": "shooting_agent",
                "revision_instruction": instruction[:400],
                "revised_at": _now().isoformat(),
            }
        }
        if args.get("prompt"):
            update["prompt"] = str(args["prompt"])
        if args.get("negative_prompt"):
            update["negative_prompt"] = str(args["negative_prompt"])
        if args.get("audio_prompt"):
            update["audio_prompt"] = str(args["audio_prompt"])
        candidate = clip.model_copy(update=update)
        if not candidate.prompt.strip():
            # Instruction-only revisions recompile from the deterministic
            # compiler so a clip never ends up without a usable prompt.
            profile = get_profile(package.target_profile_id)
            candidate = compile_clip_prompt(
                candidate,
                profile,
                self._require_production(package),
                self._assets_by_id(session.project_id),
                package.prompt_language,
            )
        self._validate_clip_or_raise(candidate, get_profile(package.target_profile_id))
        package.clips = [candidate if c.id == clip.id else c for c in package.clips]
        self._record_revision(
            package, "revise_generation_clip_prompt", instruction,
            [f"clip_{clip.clip_number}_prompt"],
        )
        self.repo.save_model_prompt_package(package)
        return (
            f"已修订 Clip {clip.clip_number} 的提示词",
            {
                "prompt_package_id": package.id,
                "clip_id": candidate.id,
                "clip_number": candidate.clip_number,
            },
        )

    async def _write_set_reference_assets(
        self, session: NarrativeShootingSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        package = self._resolve_prompt_package(session, args)
        clip = self._clip_by_id(package, args.get("clip_id"))
        ids = [str(item) for item in (args.get("reference_asset_ids") or [])]
        assets_by_id = self._assets_by_id(session.project_id)
        missing = [asset_id for asset_id in ids if asset_id not in assets_by_id]
        if missing:
            raise ValueError(
                f"Reference asset(s) not registered: {', '.join(missing)}. "
                "Register production assets first."
            )
        profile = get_profile(package.target_profile_id)
        if profile.supports_reference_images is False:
            raise ValueError(
                "VIDEO_PROFILE_MODE_UNSUPPORTED: profile "
                f"'{profile.id}' documents no reference-image support"
            )
        if profile.max_reference_images is not None and len(ids) > profile.max_reference_images:
            raise ValueError(
                f"Profile '{profile.id}' allows at most {profile.max_reference_images} "
                f"reference images, got {len(ids)}"
            )
        candidate = clip.model_copy(update={"reference_asset_ids": ids})
        self._validate_clip_or_raise(candidate, profile)
        package.clips = [candidate if c.id == clip.id else c for c in package.clips]
        self._record_revision(
            package,
            "set_clip_reference_assets",
            f"set {len(ids)} reference asset(s) on clip {clip.clip_number}",
            [f"clip_{clip.clip_number}_reference_assets"],
        )
        self.repo.save_model_prompt_package(package)
        return (
            f"已设置 Clip {clip.clip_number} 的参考图（{len(ids)} 张）",
            {
                "prompt_package_id": package.id,
                "clip_id": candidate.id,
                "reference_asset_ids": ids,
            },
        )

    async def _write_set_generation_mode(
        self, session: NarrativeShootingSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        package = self._resolve_prompt_package(session, args)
        clip = self._clip_by_id(package, args.get("clip_id"))
        mode = str(args.get("generation_mode") or "")
        profile = get_profile(package.target_profile_id)
        if (
            mode not in ("", "auto")
            and profile.supported_modes
            and mode not in profile.supported_modes
        ):
            raise ValueError(
                f"VIDEO_PROFILE_MODE_UNSUPPORTED: profile '{profile.id}' supports modes "
                f"{profile.supported_modes}, got '{mode}'"
            )
        candidate = clip.model_copy(update={"generation_mode": mode or "auto"})
        self._validate_clip_or_raise(candidate, profile)
        package.clips = [candidate if c.id == clip.id else c for c in package.clips]
        self._record_revision(
            package,
            "set_generation_mode",
            f"clip {clip.clip_number} generation_mode -> {mode or 'auto'}",
            [f"clip_{clip.clip_number}_generation_mode"],
        )
        self.repo.save_model_prompt_package(package)
        return (
            f"已将 Clip {clip.clip_number} 的生成模式设为 {mode or 'auto'}",
            {"prompt_package_id": package.id, "clip_id": candidate.id},
        )

    async def _write_set_target_model(
        self, session: NarrativeShootingSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        package = self._resolve_prompt_package(session, args)
        profile = get_profile(str(args.get("profile_id") or ""))
        production = self._require_production(package)
        assets_by_id = self._assets_by_id(session.project_id)
        recompiled = [
            compile_clip_prompt(clip, profile, production, assets_by_id, package.prompt_language)
            for clip in package.clips
        ]
        for clip in recompiled:
            self._validate_clip_or_raise(clip, profile)
        package.clips = recompiled
        package.target_profile_id = profile.id
        package.target_profile_version = profile.profile_version
        package.target_video_model_display_name = profile.display_name
        package.profile_update_available = False
        package.asset_requirements = self._rebuild_asset_requirements(recompiled, assets_by_id)
        self._record_revision(
            package,
            "set_target_video_model",
            f"switch target model to {profile.id} v{profile.profile_version}",
            ["target_profile", "clips_recompiled"],
        )
        self.repo.save_model_prompt_package(package)
        # Recompiled clips invalidate any guide built on the old prompts.
        self.repo.mark_video_production_guides_stale_for_prompt_package(
            session.project_id, package.id
        )
        return (
            f"已切换目标模型为 {profile.display_name} 并重新编译 {len(recompiled)} 个片段",
            {
                "prompt_package_id": package.id,
                "target_profile_id": profile.id,
                "target_profile_version": profile.profile_version,
                "clip_count": len(recompiled),
            },
        )

    async def _write_set_continuity_strategy(
        self, session: NarrativeShootingSession, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        package = self._resolve_prompt_package(session, args)
        strategy = str(args.get("continuity_strategy") or "")
        if not strategy:
            raise ValueError("continuity_strategy is required")
        package.continuity_strategy = strategy
        self._record_revision(
            package,
            "set_continuity_strategy",
            f"continuity_strategy -> {strategy}",
            ["continuity_strategy"],
        )
        self.repo.save_model_prompt_package(package)
        return (
            f"已将连续性策略设为 {strategy}",
            {"prompt_package_id": package.id, "continuity_strategy": strategy},
        )

    @staticmethod
    def _rebuild_asset_requirements(
        clips: list[GenerationClip], assets_by_id: dict[str, ProductionAsset]
    ) -> list[dict[str, Any]]:
        requirements: list[dict[str, Any]] = []
        for clip in clips:
            pairs = (
                (clip.start_frame_asset_id, "start_frame"),
                (clip.end_frame_asset_id, "end_frame"),
            )
            for asset_id, purpose in pairs:
                if asset_id and asset_id not in assets_by_id:
                    requirements.append(
                        {
                            "asset_id": asset_id,
                            "clip_number": clip.clip_number,
                            "purpose": purpose,
                            "prepared": False,
                        }
                    )
            for asset_id in clip.reference_asset_ids:
                if asset_id not in assets_by_id:
                    requirements.append(
                        {
                            "asset_id": asset_id,
                            "clip_number": clip.clip_number,
                            "purpose": "reference",
                            "prepared": False,
                        }
                    )
        return requirements

    # ------------------------------------------------------------------
    # Host-side clip patch (web PATCH endpoint; no agent involved)
    # ------------------------------------------------------------------
    def patch_clip(
        self, project_id: str, package_id: str, clip_id: str, patch: dict[str, Any]
    ) -> GenerationClip:
        """Apply a validated host-side edit to one clip and return it."""
        package = self.repo.get_model_prompt_package(package_id)
        if package is None or package.project_id != project_id:
            raise KeyError(f"Model prompt package not found: {package_id}")
        clip = self._clip_by_id(package, clip_id)
        profile = get_profile(package.target_profile_id)
        update: dict[str, Any] = {}
        if "prompt" in patch:
            prompt = str(patch["prompt"] or "")
            # Mirror the refinement merge's strip check: an empty prompt is
            # never a valid clip prompt.
            if not prompt.strip():
                raise ValueError("prompt must be a non-empty string when provided")
            update["prompt"] = prompt
        if "negative_prompt" in patch:
            value = str(patch["negative_prompt"]) if patch["negative_prompt"] else None
            # Same profile gate as the LLM refinement merge: models without a
            # separate negative-prompt field (or with a positive_phrasing_only
            # strategy) never receive negative text.
            if (
                profile.supports_negative_prompt is not True
                or profile.negative_prompt_strategy == "positive_phrasing_only"
            ):
                value = None
            update["negative_prompt"] = value
        if "audio_prompt" in patch:
            update["audio_prompt"] = (
                str(patch["audio_prompt"]) if patch["audio_prompt"] else None
            )
        if "generation_mode" in patch:
            mode = str(patch["generation_mode"] or "")
            if (
                mode not in ("", "auto")
                and profile.supported_modes
                and mode not in profile.supported_modes
            ):
                raise ValueError(
                    f"VIDEO_PROFILE_MODE_UNSUPPORTED: profile '{profile.id}' supports modes "
                    f"{profile.supported_modes}, got '{mode}'"
                )
            update["generation_mode"] = mode or "auto"
        if "reference_asset_ids" in patch:
            ids = [str(item) for item in (patch["reference_asset_ids"] or [])]
            assets_by_id = self._assets_by_id(project_id)
            missing = [asset_id for asset_id in ids if asset_id not in assets_by_id]
            if missing:
                raise ValueError(f"Reference asset(s) not registered: {', '.join(missing)}")
            if ids and profile.supports_reference_images is False:
                raise ValueError(
                    "VIDEO_PROFILE_MODE_UNSUPPORTED: profile "
                    f"'{profile.id}' documents no reference-image support"
                )
            if profile.max_reference_images is not None and len(ids) > profile.max_reference_images:
                raise ValueError(
                    f"Profile '{profile.id}' allows at most {profile.max_reference_images} "
                    f"reference images, got {len(ids)}"
                )
            update["reference_asset_ids"] = ids
        if "duration_seconds" in patch:
            duration = float(patch["duration_seconds"])
            update["duration_seconds"] = duration
            update["recommended_settings"] = {
                **clip.recommended_settings,
                "duration_seconds": duration,
            }
        candidate = clip.model_copy(update=update)
        self._validate_clip_or_raise(candidate, profile)
        candidate = candidate.model_copy(
            update={
                "compiler_trace": {
                    **candidate.compiler_trace,
                    "host_patch": {"fields": sorted(update), "at": _now().isoformat()},
                }
            }
        )
        package.clips = [candidate if c.id == clip.id else c for c in package.clips]
        self._record_revision(
            package,
            "host_patch_clip",
            f"manual edit of clip {clip.clip_number}: {', '.join(sorted(update))}",
            [f"clip_{clip.clip_number}_patch"],
        )
        self.repo.save_model_prompt_package(package)
        # A manual clip edit invalidates any guide built on the old prompt.
        self.repo.mark_video_production_guides_stale_for_prompt_package(
            project_id, package.id
        )
        return candidate

    # ------------------------------------------------------------------
    # Context builder (layered, budgeted)
    # ------------------------------------------------------------------
    def _build_context(
        self,
        session: NarrativeShootingSession,
        project: NarrativeProject,
        tool_results: list[dict[str, Any]],
    ) -> str:
        repo = self.repo
        production = self._latest_production_package(session)
        packages = [
            p
            for p in repo.list_model_prompt_packages(session.project_id)
            if not p.stale and p.status != "failed"
        ]
        if session.episode_number is not None:
            episode_packages = [
                p for p in packages if p.episode_number == session.episode_number
            ]
            if episode_packages:
                packages = episode_packages
        package = packages[0] if packages else None

        profile_digest: dict[str, Any] | None = None
        if package is not None:
            try:
                profile = get_profile(package.target_profile_id)
                profile_digest = {
                    "current": profile_capabilities_digest(profile),
                    "available": [
                        {
                            "id": p.id,
                            "display_name": p.display_name,
                            "profile_version": p.profile_version,
                        }
                        for p in list_profiles()
                    ],
                }
            except ValueError:
                profile_digest = None

        messages = repo.list_shooting_messages(session.id)
        user_request = next((m.content for m in reversed(messages) if m.role == "user"), "")
        context_message = build_shooting_context(
            mode=session.mode,
            session={
                "id": session.id,
                "project_id": project.id,
                "project_title": project.title,
                "episode_number": session.episode_number,
                "status": session.status.value,
            },
            user_request=user_request[:1500],
            allowed_actions=[
                {"name": spec.name, "risk": spec.risk.value, "description": spec.description}
                for spec in SHOOTING_ACTION_REGISTRY.values()
            ],
            profile_digest=profile_digest,
            prompt_package=(
                self._compact_prompt_package(package) if package is not None else None
            ),
            production_package=(
                self._compact_production_package(production) if production is not None else None
            ),
            canon_episode=(
                self._canon_episode_summary(project.id, session.episode_number)
                if session.episode_number is not None
                else None
            ),
            clip_hints=self._clip_hints(package) if package is not None else None,
            visual_bibles=(
                self._relevant_visual_bibles(production, package)
                if production is not None
                else None
            ),
            production_assets=[
                self._compact_asset(asset)
                for asset in repo.list_production_assets(project.id)[:_MAX_ASSETS_IN_CONTEXT]
            ],
            recent_messages=[
                {"role": m.role.value, "content": m.content[:600]}
                for m in messages[-_MAX_CONTEXT_MESSAGES:]
            ],
            max_chars=24000,
        )
        if tool_results:
            compact: list[dict[str, Any]] = []
            for row in tool_results[-_MAX_CONTEXT_ACTION_RESULTS:]:
                text = dumps(row)
                compact.append(
                    row if len(text) <= 1800
                    else {"action": row.get("action"), "result_excerpt": text[:1800]}
                )
            context_message += "\n=== RECENT TOOL RESULTS ===\n" + dumps(compact)
        return context_message

    # ------------------------------------------------------------------
    # Compact payload helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _compact_production_package(
        package: ProductionPackage, *, screenplay_chars: int = 2500
    ) -> dict[str, Any]:
        return {
            "id": package.id,
            "episode_number": package.episode_number,
            "episode_version_id": package.episode_version_id,
            "format": package.format.value,
            "is_preview": package.is_preview,
            "stale": package.stale,
            "screenplay_excerpt": package.screenplay[:screenplay_chars],
            "shot_count": len(package.shot_list),
            "shot_list": [
                {
                    "shot_number": shot.shot_number,
                    "duration_seconds": shot.duration_seconds,
                    "shot_size": shot.shot_size,
                    "camera": shot.camera,
                    "movement": shot.movement,
                    "characters": shot.characters,
                    "location": shot.location,
                    "action": shot.action[:160],
                    "dialogue": shot.dialogue[:160],
                    "visual_intent": shot.visual_intent[:160],
                    "transition": shot.transition,
                }
                for shot in package.shot_list[:40]
            ],
            "dialogue_track_count": len(package.dialogue_track),
            "subtitle_track_count": len(package.subtitle_track),
            "sfx_plan_count": len(package.sound_effect_plan),
            "bgm_direction": package.bgm_direction[:300],
            "continuity_notes": package.continuity_notes[:10],
        }

    def _compact_prompt_package(self, package: ModelPromptPackage) -> dict[str, Any]:
        return {
            **self._compact_prompt_package_summary(package),
            "clips": [
                {
                    "id": clip.id,
                    "clip_number": clip.clip_number,
                    "duration_seconds": clip.duration_seconds,
                    "generation_mode": clip.generation_mode,
                    "aspect_ratio": clip.aspect_ratio,
                    "purpose": clip.purpose[:120],
                    "prompt": clip.prompt[:400],
                    "negative_prompt": (clip.negative_prompt or "")[:200],
                    "audio_prompt": (clip.audio_prompt or "")[:200],
                    "reference_asset_ids": clip.reference_asset_ids,
                    "start_frame_asset_id": clip.start_frame_asset_id,
                    "planning_rationale": clip.planning_rationale[:160],
                }
                for clip in package.clips
            ],
            "asset_requirement_count": len(package.asset_requirements),
        }

    @staticmethod
    def _compact_prompt_package_summary(package: ModelPromptPackage) -> dict[str, Any]:
        return {
            "id": package.id,
            "episode_number": package.episode_number,
            "production_package_id": package.production_package_id,
            "status": package.status,
            "target_profile_id": package.target_profile_id,
            "target_profile_version": package.target_profile_version,
            "target_video_model_display_name": package.target_video_model_display_name,
            "aspect_ratio": package.aspect_ratio,
            "quality_priority": package.quality_priority,
            "continuity_strategy": package.continuity_strategy,
            "audio_strategy": package.audio_strategy,
            "prompt_language": package.prompt_language,
            "stale": package.stale,
            "clip_count": len(package.clips),
            "created_at": package.created_at.isoformat(),
        }

    @staticmethod
    def _compact_asset(asset: ProductionAsset) -> dict[str, Any]:
        return {
            "id": asset.id,
            "asset_type": asset.asset_type,
            "name": asset.name,
            "character_id": asset.character_id,
            "location_id": asset.location_id,
            "source_uri": asset.source_uri,
            "local_path": asset.local_path,
            "description": asset.description[:200],
            "episode_number": asset.episode_number,
            "production_package_id": asset.metadata.get("production_package_id"),
        }

    @staticmethod
    def _clip_hints(package: ModelPromptPackage) -> dict[str, Any]:
        chain = [
            {
                "clip_number": clip.clip_number,
                "source_shot_numbers": clip.source_shot_numbers,
                "references_previous_end_frame": (
                    "previous_clip_end_frame" in clip.continuity_constraints
                ),
            }
            for clip in package.clips
        ]
        return {"total": len(chain), "chain": chain}

    @staticmethod
    def _relevant_visual_bibles(
        production: ProductionPackage, package: ModelPromptPackage | None
    ) -> dict[str, list[dict[str, Any]]]:
        """Only bible entries referenced by the current clips (compact)."""
        referenced: set[str] = set()
        shots_by_number = {shot.shot_number: shot for shot in production.shot_list}
        for clip in package.clips if package is not None else []:
            for entry in clip.dialogue:
                speaker = str(entry.get("speaker") or "")
                if speaker:
                    referenced.add(speaker)
            for number in clip.source_shot_numbers:
                shot = shots_by_number.get(number)
                if shot is None:
                    continue
                referenced.update(shot.characters)
                if shot.location:
                    referenced.add(shot.location)

        def _matches(entry: dict[str, Any]) -> bool:
            if not referenced:
                return True
            keys = {
                str(entry.get(key, ""))
                for key in ("name", "id", "character_id", "location_id")
            }
            return bool(keys & referenced)

        def _compact(entries: list[Any], limit: int) -> list[dict[str, Any]]:
            compact: list[dict[str, Any]] = []
            keep = {"name", "id", "character_id", "location_id",
                    "description", "visual_description", "visual", "summary"}
            for entry in entries:
                if not isinstance(entry, dict) or not _matches(entry):
                    continue
                compact.append(
                    {
                        key: value[:400] if isinstance(value, str) else value
                        for key, value in entry.items()
                        if key in keep
                    }
                )
                if len(compact) >= limit:
                    break
            return compact

        return {
            "characters": _compact(production.character_visual_bible, 12),
            "locations": _compact(production.location_visual_bible, 8),
            "props": _compact(production.prop_visual_bible, 8),
        }

    def _canon_episode_summary(self, project_id: str, episode_number: int) -> dict[str, Any]:
        repo = self.repo
        summary: dict[str, Any] = {"episode_number": episode_number}
        plan = repo.get_episode_plan(project_id, episode_number)
        if plan is not None:
            summary["episode_plan"] = {
                "title": plan.title,
                "narrative_goal": plan.narrative_goal[:400],
                "hook": plan.hook[:200],
                "cliffhanger": plan.cliffhanger[:200],
                "status": plan.status.value,
            }
        versions = repo.list_episode_versions(project_id, episode_number)
        canon_version = next((v for v in versions if v.is_canon), None)
        if canon_version is not None:
            summary["canon_version"] = {
                "id": canon_version.id,
                "version": canon_version.version,
                "title": canon_version.title,
                "screenplay_excerpt": canon_version.screenplay[:_MAX_EPISODE_SCREENPLAY_CHARS],
            }
        entries = [
            entry
            for entry in repo.list_canon_entries(project_id)
            if entry.episode_number == episode_number
        ]
        summary["canon_entries"] = [
            {"type": entry.entry_type.value, "text": entry.text[:300]} for entry in entries[:20]
        ]
        return summary

    # ------------------------------------------------------------------
    # Message helpers
    # ------------------------------------------------------------------
    def _append_message(self, session_id: str, role: str, content: str) -> None:
        if not content:
            return
        self.repo.append_shooting_message(
            NarrativeShootingMessage(
                session_id=session_id, role=ShootingMessageRole(role), content=content
            )
        )

    def _transition(self, session_id: str, status: ShootingSessionStatus) -> None:
        session = self.repo.get_shooting_session(session_id)
        if session is None:
            return
        # Pause/cancel always wins over a loop-status transition.
        if session.status in {
            ShootingSessionStatus.PAUSED,
            ShootingSessionStatus.CANCELLED,
        }:
            return
        session.status = status
        session.updated_at = _now()
        self.repo.save_shooting_session(session)
