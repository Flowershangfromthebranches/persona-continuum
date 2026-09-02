from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.numeric import safe_int
from persona_continuum.room.models import RoomProtocolType
from persona_continuum.web.api import WebAPIHandler


class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Any) -> Response:
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp


def create_web_app(continuum: PersonaContinuum) -> Starlette:
    handler = WebAPIHandler(continuum)
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)

    async def index_endpoint(request: Request) -> Response:
        index_file = static_dir / "index.html"
        if index_file.exists():
            return FileResponse(
                index_file,
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )
        return Response("Persona Continuum Web UI", media_type="text/plain")

    async def websocket_room_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        room_id = websocket.path_params.get("room_id", "")
        orchestrator = continuum.orchestrator
        event_queue = orchestrator.subscribe_events(room_id)
        send_lock = asyncio.Lock()

        async def send_event(event: dict[str, Any]) -> None:
            if websocket.client_state != WebSocketState.CONNECTED:
                return
            async with send_lock:
                if websocket.client_state != WebSocketState.CONNECTED:
                    return
                await websocket.send_text(json.dumps(event, ensure_ascii=False, default=str))

        room_state = orchestrator.get_room(room_id)
        if room_state and room_state.status.value == "initializing":
            progress_events = room_state.metadata.get("progress_events", [])
            if isinstance(progress_events, list):
                latest = progress_events[-1:] if progress_events else []
                for event in latest:
                    if isinstance(event, dict):
                        with contextlib.suppress(Exception):
                            await send_event(event)

        # Drain replayed events on this connection before live forwarding.
        while True:
            try:
                queued = event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            with contextlib.suppress(Exception):
                await send_event(queued)

        async def event_forwarder() -> None:
            try:
                while True:
                    event = await event_queue.get()
                    await send_event(event)
            except (asyncio.CancelledError, WebSocketDisconnect):
                pass
            except Exception:
                pass

        forwarder_task = asyncio.create_task(event_forwarder())

        try:
            while True:
                msg_text = await websocket.receive_text()
                try:
                    cmd_data = json.loads(msg_text)
                    action = cmd_data.get("action")
                    if action in {"ping", "pong"}:
                        await send_event({"event": "pong", "room_id": room_id})
                    elif action == "step":
                        manual_spk = cmd_data.get("manual_speaker_id")
                        usr_msg = cmd_data.get("user_message", "")
                        asyncio.create_task(
                            _run_step_background(orchestrator, room_id, manual_spk, usr_msg)
                        )
                    elif action == "autonomous":
                        room = orchestrator.get_room(room_id)
                        if room is None or room.protocol != RoomProtocolType.FREE_DISCUSSION:
                            # Legacy autonomous discussion is exclusive to
                            # free_discussion rooms; protocol rooms must never
                            # start it from a WebSocket (re)connect.
                            protocol_value = room.protocol.value if room else "missing"
                            await send_event(
                                {
                                    "event": "agent_error",
                                    "room_id": room_id,
                                    "error": (
                                        "autonomous_discussion_only_for_free_discussion_protocol"
                                        f" (protocol: {protocol_value})"
                                    ),
                                }
                            )
                        else:
                            orchestrator.start_autonomous_discussion(
                                room_id,
                                max_turns=safe_int(
                                    cmd_data.get("max_turns", 6),
                                    default=6,
                                    minimum=1,
                                    maximum=100,
                                )
                                or 6,
                            )
                    elif action == "retry":
                        alt_runtime = cmd_data.get("alternate_runtime_id")
                        alt_model = cmd_data.get("alternate_model_id")
                        usr_msg = cmd_data.get("user_message", "")
                        asyncio.create_task(
                            _run_retry_background(
                                orchestrator, room_id, alt_runtime, alt_model, usr_msg
                            )
                        )
                    elif action == "skip":
                        reason = cmd_data.get("reason")
                        await orchestrator.skip_turn(room_id, reason=reason)
                    elif action == "restart_session":
                        participant_id = cmd_data.get("participant_id")
                        if participant_id:
                            await orchestrator.restart_session(room_id, participant_id)
                    elif action == "pause":
                        await orchestrator.pause_room(room_id)
                    elif action == "inject":
                        await orchestrator.inject_message(
                            room_id,
                            content=str(cmd_data.get("content") or ""),
                            injection_type=str(cmd_data.get("type") or "external_information"),
                            client_message_id=str(cmd_data.get("client_message_id") or ""),
                        )
                    elif action == "resume":
                        await orchestrator.resume_room(room_id)
                    elif action == "cancel":
                        await orchestrator.cancel_turn(room_id)
                    elif action == "stop":
                        await orchestrator.stop_room(room_id)
                    else:
                        raise ValueError(f"Unsupported room action: {action}")
                except Exception as exc:
                    with contextlib.suppress(Exception):
                        await send_event(
                            {
                                "event": "agent_error",
                                "room_id": room_id,
                                "error": f"Room command failed: {exc}",
                            }
                        )
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            forwarder_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await forwarder_task
            orchestrator.unsubscribe_events(room_id, event_queue)

    async def websocket_world_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        world_id = websocket.path_params.get("world_id", "")
        world_engine = continuum.worlds
        event_queue = world_engine.subscribe_events(world_id)

        async def event_forwarder() -> None:
            try:
                while True:
                    event = await event_queue.get()
                    await websocket.send_text(json.dumps(event, ensure_ascii=False))
            except (asyncio.CancelledError, WebSocketDisconnect):
                pass
            except Exception:
                pass

        forwarder_task = asyncio.create_task(event_forwarder())

        try:
            while True:
                msg_text = await websocket.receive_text()
                try:
                    cmd_data = json.loads(msg_text)
                    action = cmd_data.get("action")
                    if action == "step":
                        branch_id = cmd_data.get("branch_id", "main")
                        asyncio.create_task(world_engine.step_branch(world_id, branch_id))
                except Exception:
                    pass
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            forwarder_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await forwarder_task
            world_engine.unsubscribe_events(world_id, event_queue)

    async def websocket_persona_creation_endpoint(websocket: WebSocket) -> None:
        """Stream persisted Persona Creation events without exposing secrets."""
        await websocket.accept()
        job_id = websocket.path_params.get("job_id", "")
        service = continuum.persona_creation
        try:
            job = service.get_job(job_id)
        except Exception:
            await websocket.close(code=4404)
            return
        queue = service.subscribe_events(job_id)
        try:
            for event in job.events:
                await websocket.send_text(json.dumps(event, ensure_ascii=False, default=str))
            while True:
                event = await queue.get()
                await websocket.send_text(json.dumps(event, ensure_ascii=False, default=str))
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            service.unsubscribe_events(job_id, queue)

    async def websocket_profile_enrichment_endpoint(websocket: WebSocket) -> None:
        """Push durable enrichment snapshots; polling remains the reconnect fallback."""
        await websocket.accept()
        job_id = websocket.path_params.get("job_id", "")
        service = continuum.profile_enrichment
        try:
            service.profiles.get_enrichment_job(job_id)
        except Exception:
            await websocket.close(code=4404)
            return
        terminal = {"completed", "completed_with_gaps", "failed", "cancelled"}
        try:
            while True:
                job = service.profiles.get_enrichment_job(job_id)
                await websocket.send_text(
                    json.dumps(
                        {
                            "event": "profile_enrichment_progress",
                            "job": job.model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                )
                if job.status.value in terminal:
                    break
                await asyncio.sleep(1.0)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass

    async def websocket_narrative_job_endpoint(websocket: WebSocket) -> None:
        """Stream narrative job progress snapshots until terminal."""
        await websocket.accept()
        job_id = websocket.path_params.get("job_id", "")
        service = continuum.narratives
        try:
            job = service.get_job(job_id)
        except Exception:
            await websocket.close(code=4404)
            return
        queue = service.subscribe_events(job_id)
        terminal = {"completed", "failed", "cancelled"}
        try:
            await websocket.send_text(
                json.dumps(
                    {"event": "narrative_job_progress", "job": job},
                    ensure_ascii=False,
                    default=str,
                )
            )
            if job["status"] not in terminal:
                while True:
                    event = await queue.get()
                    await websocket.send_text(
                        json.dumps(event, ensure_ascii=False, default=str)
                    )
                    if event.get("status") in terminal:
                        break
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            service.unsubscribe_events(job_id, queue)

    routes = [
        Route("/", endpoint=index_endpoint, methods=["GET"]),
        # Agents API
        Route("/api/agents", endpoint=handler.list_agents, methods=["GET"]),
        Route("/api/agents/rescan", endpoint=handler.rescan_agents, methods=["POST"]),
        Route(
            "/api/agents/{agent_id}/research/revalidate",
            endpoint=handler.revalidate_agent_research,
            methods=["POST"],
        ),
        Route("/api/agents/{agent_id}", endpoint=handler.inspect_agent, methods=["GET"]),
        Route("/api/agents/{agent_id}/test", endpoint=handler.test_agent, methods=["POST"]),
        # Performance observability API (read-only)
        Route(
            "/api/performance/summary",
            endpoint=handler.performance_summary,
            methods=["GET"],
        ),
        # Personas API
        Route("/api/personas", endpoint=handler.list_personas, methods=["GET"]),
        Route(
            "/api/personas/{persona_id}/rename",
            endpoint=handler.rename_persona,
            methods=["POST"],
        ),
        Route("/api/profiles", endpoint=handler.list_profiles, methods=["GET"]),
        Route("/api/profiles", endpoint=handler.create_profile, methods=["POST"]),
        Route("/api/profiles/{profile_id}", endpoint=handler.get_profile, methods=["GET"]),
        Route(
            "/api/profiles/{profile_id}/versions",
            endpoint=handler.list_profile_versions,
            methods=["GET"],
        ),
        Route(
            "/api/profiles/{profile_id}/enrich",
            endpoint=handler.enrich_profile,
            methods=["POST"],
        ),
        Route(
            "/api/profiles/{profile_id}/archive",
            endpoint=handler.archive_profile,
            methods=["POST"],
        ),
        Route(
            "/api/profile-enrichment/jobs",
            endpoint=handler.list_profile_enrichment_jobs,
            methods=["GET"],
        ),
        Route(
            "/api/profile-enrichment/jobs/{job_id}",
            endpoint=handler.get_profile_enrichment_job,
            methods=["GET"],
        ),
        Route(
            "/api/profile-enrichment/jobs/{job_id}",
            endpoint=handler.dismiss_profile_enrichment_job,
            methods=["DELETE"],
        ),
        Route(
            "/api/profile-enrichment/jobs/{job_id}/pause",
            endpoint=handler.pause_profile_enrichment_job,
            methods=["POST"],
        ),
        Route(
            "/api/profile-enrichment/jobs/{job_id}/resume",
            endpoint=handler.resume_profile_enrichment_job,
            methods=["POST"],
        ),
        Route(
            "/api/profile-enrichment/jobs/{job_id}/retry",
            endpoint=handler.retry_profile_enrichment_job,
            methods=["POST"],
        ),
        Route(
            "/api/profile-enrichment/jobs/{job_id}/cancel",
            endpoint=handler.cancel_profile_enrichment_job,
            methods=["POST"],
        ),
        Route(
            "/api/persona-creation/jobs",
            endpoint=handler.list_persona_creation_jobs,
            methods=["GET"],
        ),
        Route(
            "/api/persona-creation/jobs",
            endpoint=handler.create_persona_creation_job,
            methods=["POST"],
        ),
        Route(
            "/api/persona-creation/jobs/{job_id}",
            endpoint=handler.get_persona_creation_job,
            methods=["GET"],
        ),
        Route(
            "/api/persona-creation/jobs/{job_id}",
            endpoint=handler.dismiss_persona_creation_job,
            methods=["DELETE"],
        ),
        Route(
            "/api/persona-creation/jobs/{job_id}/pause",
            endpoint=handler.pause_persona_creation_job,
            methods=["POST"],
        ),
        Route(
            "/api/persona-creation/jobs/{job_id}/resume",
            endpoint=handler.resume_persona_creation_job,
            methods=["POST"],
        ),
        Route(
            "/api/persona-creation/jobs/{job_id}/retry",
            endpoint=handler.retry_persona_creation_job,
            methods=["POST"],
        ),
        Route(
            "/api/persona-creation/jobs/{job_id}/continue",
            endpoint=handler.continue_persona_creation_job,
            methods=["POST"],
        ),
        Route(
            "/api/persona-creation/jobs/{job_id}/cancel",
            endpoint=handler.cancel_persona_creation_job,
            methods=["POST"],
        ),
        Route(
            "/api/persona-creation/jobs/{job_id}/materials",
            endpoint=handler.add_persona_creation_materials,
            methods=["POST"],
        ),
        Route(
            "/api/persona-creation/jobs/{job_id}/interview-answer",
            endpoint=handler.answer_persona_creation_interview,
            methods=["POST"],
        ),
        Route(
            "/api/persona-creation/jobs/{job_id}/events",
            endpoint=handler.persona_creation_events,
            methods=["GET"],
        ),
        Route(
            "/api/background-jobs",
            endpoint=handler.list_background_jobs,
            methods=["GET"],
        ),
        Route(
            "/api/background-jobs/cleanup",
            endpoint=handler.cleanup_background_jobs,
            methods=["POST"],
        ),
        Route(
            "/api/personas/{persona_id}/runtime",
            endpoint=handler.get_persona_runtime,
            methods=["GET"],
        ),
        Route(
            "/api/personas/{persona_id}/evidence-index",
            endpoint=handler.get_persona_evidence_index,
            methods=["GET"],
        ),
        Route(
            "/api/personas/{persona_id}/material-analysis",
            endpoint=handler.analyze_persona_materials,
            methods=["POST"],
        ),
        Route(
            "/api/persona-material/jobs",
            endpoint=handler.list_persona_material_jobs,
            methods=["GET"],
        ),
        Route(
            "/api/persona-material/jobs/{job_id}",
            endpoint=handler.get_persona_material_job,
            methods=["GET"],
        ),
        Route(
            "/api/personas/{persona_id}",
            endpoint=handler.delete_persona,
            methods=["DELETE", "POST"],
        ),
        # Auth Profiles API
        Route("/api/auth-profiles", endpoint=handler.list_auth_profiles, methods=["GET"]),
        Route("/api/auth-profiles", endpoint=handler.create_auth_profile, methods=["POST"]),
        Route(
            "/api/auth-profiles/{profile_id}/test",
            endpoint=handler.test_auth_profile,
            methods=["GET", "POST"],
        ),
        Route(
            "/api/providers/{provider_id}/test",
            endpoint=handler.test_provider,
            methods=["POST"],
        ),
        Route(
            "/api/auth-profiles/{profile_id}",
            endpoint=handler.delete_auth_profile,
            methods=["DELETE", "POST"],
        ),
        Route(
            "/api/auth-profiles/{profile_id}",
            endpoint=handler.update_auth_profile,
            methods=["PUT"],
        ),
        # Rooms API
        Route("/api/room-protocols", endpoint=handler.list_room_protocols, methods=["GET"]),
        Route("/api/room-templates", endpoint=handler.list_room_templates, methods=["GET"]),
        Route("/api/room-templates", endpoint=handler.create_room_template, methods=["POST"]),
        Route(
            "/api/room-templates/{template_id}",
            endpoint=handler.update_room_template,
            methods=["PATCH"],
        ),
        Route(
            "/api/room-templates/{template_id}",
            endpoint=handler.delete_room_template,
            methods=["DELETE"],
        ),
        Route("/api/rooms", endpoint=handler.list_rooms, methods=["GET"]),
        Route("/api/rooms", endpoint=handler.create_room, methods=["POST"]),
        Route("/api/rooms/{room_id}", endpoint=handler.get_room, methods=["GET"]),
        Route("/api/rooms/{room_id}", endpoint=handler.patch_room, methods=["PATCH"]),
        Route(
            "/api/rooms/{room_id}/run", endpoint=handler.run_room_protocol, methods=["POST"]
        ),
        Route(
            "/api/rooms/{room_id}/events",
            endpoint=handler.list_room_protocol_events,
            methods=["GET"],
        ),
        Route(
            "/api/rooms/{room_id}/cancel",
            endpoint=handler.cancel_room_protocol,
            methods=["POST"],
        ),
        Route(
            "/api/rooms/{room_id}/finalize",
            endpoint=handler.finalize_room_protocol,
            methods=["POST"],
        ),
        Route(
            "/api/rooms/{room_id}/protocol/convert",
            endpoint=handler.convert_room_protocol,
            methods=["POST"],
        ),
        Route("/api/room-runs", endpoint=handler.list_room_runs, methods=["GET"]),
        Route("/api/room-runs/{run_id}", endpoint=handler.get_room_run, methods=["GET"]),
        Route(
            "/api/room-runs/{run_id}", endpoint=handler.delete_room_run, methods=["DELETE"]
        ),
        Route(
            "/api/rooms/{room_id}/resolve-preview",
            endpoint=handler.resolve_preview,
            methods=["POST"],
        ),
        Route("/api/rooms/{room_id}/start", endpoint=handler.start_room, methods=["POST"]),
        Route("/api/rooms/{room_id}/step", endpoint=handler.step_turn, methods=["POST"]),
        Route(
            "/api/rooms/{room_id}/autonomous",
            endpoint=handler.run_autonomous_room,
            methods=["POST"],
        ),
        Route("/api/rooms/{room_id}/retry", endpoint=handler.retry_turn, methods=["POST"]),
        Route("/api/rooms/{room_id}/skip", endpoint=handler.skip_turn, methods=["POST"]),
        Route(
            "/api/rooms/{room_id}/restart-session",
            endpoint=handler.restart_session,
            methods=["POST"],
        ),
        Route("/api/rooms/{room_id}/pause", endpoint=handler.pause_room, methods=["POST"]),
        Route(
            "/api/rooms/{room_id}/inject",
            endpoint=handler.inject_room_message,
            methods=["POST"],
        ),
        Route("/api/rooms/{room_id}/resume", endpoint=handler.resume_room, methods=["POST"]),
        Route("/api/rooms/{room_id}/cancel-turn", endpoint=handler.cancel_turn, methods=["POST"]),
        Route("/api/rooms/{room_id}/stop", endpoint=handler.stop_room, methods=["POST"]),
        Route("/api/rooms/{room_id}", endpoint=handler.delete_room, methods=["DELETE"]),
        Route(
            "/api/rooms/{room_id}/delete",
            endpoint=handler.delete_room,
            methods=["POST", "DELETE"],
        ),
        Route(
            "/api/rooms/{room_id}/transcripts", endpoint=handler.list_transcripts, methods=["GET"]
        ),
        Route(
            "/api/rooms/{room_id}/transcripts",
            endpoint=handler.clear_transcripts,
            methods=["DELETE"],
        ),
        # Parallel World routes
        Route("/api/worlds", endpoint=handler.list_worlds, methods=["GET"]),
        Route("/api/worlds", endpoint=handler.create_world, methods=["POST"]),
        Route(
            "/api/worlds/entity-classification",
            endpoint=handler.classify_world_entities,
            methods=["POST"],
        ),
        Route(
            "/api/worlds/actor-classification",
            endpoint=handler.classify_world_entities,
            methods=["POST"],
        ),
        Route("/api/worlds/preview-seed", endpoint=handler.preview_world_seed, methods=["POST"]),
        Route(
            "/api/worlds/persona-match",
            endpoint=handler.match_world_personas,
            methods=["POST"],
        ),
        Route(
            "/api/worlds/persona-completion/confirm",
            endpoint=handler.confirm_world_persona_completion,
            methods=["POST"],
        ),
        Route(
            "/api/worlds/actor-completion/confirm",
            endpoint=handler.confirm_world_actor_completion,
            methods=["POST"],
        ),
        Route("/api/worlds/{world_id}", endpoint=handler.get_world, methods=["GET"]),
        Route(
            "/api/worlds/{world_id}/bindings",
            endpoint=handler.list_world_bindings,
            methods=["GET"],
        ),
        Route("/api/worlds/{world_id}/pause", endpoint=handler.pause_world, methods=["POST"]),
        Route("/api/worlds/{world_id}/resume", endpoint=handler.resume_world, methods=["POST"]),
        Route("/api/worlds/{world_id}", endpoint=handler.delete_world, methods=["DELETE"]),
        Route(
            "/api/worlds/{world_id}/delete",
            endpoint=handler.delete_world,
            methods=["POST", "DELETE"],
        ),
        Route(
            "/api/worlds/{world_id}/branches", endpoint=handler.list_world_branches, methods=["GET"]
        ),
        Route(
            "/api/worlds/{world_id}/branches", endpoint=handler.fork_world_branch, methods=["POST"]
        ),
        Route(
            "/api/worlds/{world_id}/branches/{branch_id}",
            endpoint=handler.get_world_branch,
            methods=["GET"],
        ),
        Route(
            "/api/worlds/{world_id}/branches/{branch_id}/step",
            endpoint=handler.step_world_branch,
            methods=["POST"],
        ),
        Route(
            "/api/worlds/{world_id}/branches/{branch_id}/action",
            endpoint=handler.inject_world_action,
            methods=["POST"],
        ),
        Route(
            "/api/worlds/{world_id}/branches/{branch_id}/scene",
            endpoint=handler.trigger_world_scene,
            methods=["POST"],
        ),
        Route(
            "/api/worlds/{world_id}/branches/{branch_id}/timeline",
            endpoint=handler.list_world_events,
            methods=["GET"],
        ),
        Route(
            "/api/worlds/{world_id}/branches/{branch_id}/actors",
            endpoint=handler.list_world_actors,
            methods=["GET"],
        ),
        Route(
            "/api/worlds/{world_id}/branches/{branch_id}/causal",
            endpoint=handler.query_world_causal,
            methods=["GET"],
        ),
        Route(
            "/api/worlds/{world_id}/branches/{branch_id}/replay",
            endpoint=handler.get_world_replay,
            methods=["GET"],
        ),
        Route(
            "/api/worlds/{world_id}/branches/{branch_id}/organizations",
            endpoint=handler.list_world_organizations,
            methods=["GET"],
        ),
        Route(
            "/api/worlds/{world_id}/branches/{branch_id}/technologies",
            endpoint=handler.list_world_technologies,
            methods=["GET"],
        ),
        Route(
            "/api/worlds/{world_id}/branches/{branch_id}/memories",
            endpoint=handler.list_world_memories,
            methods=["GET"],
        ),
        Route("/api/worlds/{world_id}/report", endpoint=handler.get_world_report, methods=["GET"]),
        Route("/api/worlds/{world_id}/evaluate", endpoint=handler.evaluate_world, methods=["POST"]),
        # Narrative Studio (author control layer over Persona / Room / World)
        Route("/api/narratives", endpoint=handler.list_narrative_projects, methods=["GET"]),
        Route("/api/narratives", endpoint=handler.create_narrative_project, methods=["POST"]),
        # Video model profile registry. These literal 3-segment paths MUST be
        # registered BEFORE the catch-all "/api/narratives/{project_id}"
        # routes below, otherwise starlette would match them as a project id.
        Route(
            "/api/narratives/video-model-profiles",
            endpoint=handler.list_video_model_profiles,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/video-model-profiles/{profile_id}",
            endpoint=handler.get_video_model_profile,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}",
            endpoint=handler.get_narrative_project,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}",
            endpoint=handler.patch_narrative_project,
            methods=["PATCH"],
        ),
        Route(
            "/api/narratives/{project_id}",
            endpoint=handler.delete_narrative_project,
            methods=["DELETE"],
        ),
        Route(
            "/api/narratives/{project_id}/bible",
            endpoint=handler.get_narrative_bible,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/bible",
            endpoint=handler.update_narrative_bible,
            methods=["PATCH"],
        ),
        Route(
            "/api/narratives/{project_id}/bible/generate",
            endpoint=handler.generate_narrative_bible,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/bible/versions",
            endpoint=handler.list_narrative_bible_versions,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/characters",
            endpoint=handler.list_narrative_characters,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/characters",
            endpoint=handler.add_narrative_character,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/characters/bind",
            endpoint=handler.bind_narrative_character,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/characters/create-missing",
            endpoint=handler.create_missing_narrative_personas,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/facts",
            endpoint=handler.list_narrative_facts,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/facts",
            endpoint=handler.add_narrative_fact,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/knowledge",
            endpoint=handler.get_narrative_knowledge,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/knowledge",
            endpoint=handler.set_narrative_knowledge,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/audience-knowledge",
            endpoint=handler.get_narrative_audience_knowledge,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/audience-knowledge",
            endpoint=handler.set_narrative_audience_knowledge,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/outline/generate",
            endpoint=handler.generate_narrative_outline,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/episodes",
            endpoint=handler.list_narrative_episodes,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/episodes/{episode_number}",
            endpoint=handler.get_narrative_episode,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/episodes/{episode_number}/prepare",
            endpoint=handler.prepare_narrative_episode,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/episodes/{episode_number}/forecast",
            endpoint=handler.forecast_narrative_episode,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/episodes/{episode_number}/forecast/directions",
            endpoint=handler.generate_narrative_forecast_directions,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/episodes/{episode_number}/scenes",
            endpoint=handler.list_narrative_scenes,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/episodes/{episode_number}/scenes",
            endpoint=handler.create_narrative_scene,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/episodes/{episode_number}/simulate",
            endpoint=handler.simulate_narrative_scene,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/episodes/{episode_number}/draft",
            endpoint=handler.generate_narrative_draft,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/episodes/{episode_number}/audit",
            endpoint=handler.audit_narrative_episode,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/episodes/{episode_number}/commit",
            endpoint=handler.commit_narrative_episode,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/episodes/{episode_number}/production",
            endpoint=handler.generate_narrative_production,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/episodes/{episode_number}/versions",
            endpoint=handler.list_narrative_episode_versions,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/canon",
            endpoint=handler.get_narrative_canon,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/clues",
            endpoint=handler.list_narrative_clues,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/clues",
            endpoint=handler.add_narrative_clue,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/clues/{clue_id}",
            endpoint=handler.update_narrative_clue,
            methods=["PATCH"],
        ),
        Route(
            "/api/narratives/{project_id}/threads",
            endpoint=handler.list_narrative_threads,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/threads",
            endpoint=handler.add_narrative_thread,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/arcs",
            endpoint=handler.list_narrative_arcs,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/forecasts",
            endpoint=handler.list_narrative_forecasts,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/forecasts/{forecast_id}/select",
            endpoint=handler.select_narrative_forecast,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/writer-room",
            endpoint=handler.run_narrative_writer_room,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/episodes/{episode_number}/writer-room",
            endpoint=handler.get_narrative_writer_room,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/production",
            endpoint=handler.list_narrative_production_packages,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/jobs",
            endpoint=handler.list_narrative_jobs,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/jobs",
            endpoint=handler.create_narrative_job,
            methods=["POST"],
        ),
        Route("/api/narrative-jobs/{job_id}", endpoint=handler.get_narrative_job, methods=["GET"]),
        Route(
            "/api/narrative-jobs/{job_id}/pause",
            endpoint=handler.pause_narrative_job,
            methods=["POST"],
        ),
        Route(
            "/api/narrative-jobs/{job_id}/resume",
            endpoint=handler.resume_narrative_job,
            methods=["POST"],
        ),
        Route(
            "/api/narrative-jobs/{job_id}/retry",
            endpoint=handler.retry_narrative_job,
            methods=["POST"],
        ),
        Route(
            "/api/narrative-jobs/{job_id}/cancel",
            endpoint=handler.cancel_narrative_job,
            methods=["POST"],
        ),
        Route(
            "/api/narrative-jobs/{job_id}",
            endpoint=handler.dismiss_narrative_job,
            methods=["DELETE"],
        ),
        Route("/api/narrative-jobs", endpoint=handler.list_all_narrative_jobs, methods=["GET"]),
        # Narrative Director Agent
        Route(
            "/api/narratives/{project_id}/director/sessions",
            endpoint=handler.create_director_session,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/director/sessions",
            endpoint=handler.list_director_sessions,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/director/sessions/{session_id}",
            endpoint=handler.get_director_session,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/director/sessions/{session_id}",
            endpoint=handler.update_director_session,
            methods=["PATCH"],
        ),
        Route(
            "/api/narratives/{project_id}/director/sessions/{session_id}/messages",
            endpoint=handler.send_director_message,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/director/sessions/{session_id}/actions",
            endpoint=handler.list_director_actions,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/director/sessions/{session_id}/pause",
            endpoint=handler.pause_director_session,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/director/sessions/{session_id}/resume",
            endpoint=handler.resume_director_session,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/director/sessions/{session_id}/cancel",
            endpoint=handler.cancel_director_session,
            methods=["POST"],
        ),
        # Narrative Shooting / Video Production pipeline
        Route(
            "/api/narratives/{project_id}/production/{package_id}/prompt-packages",
            endpoint=handler.list_prompt_packages,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/production/{package_id}/prompt-packages",
            endpoint=handler.create_prompt_package_job,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/production/{package_id}/clip-plan",
            endpoint=handler.create_clip_plan_job,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/production/{package_id}/clip-plan",
            endpoint=handler.get_narrative_clip_plan,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/production/{package_id}/prompt-packages/export",
            endpoint=handler.export_prompt_package_zip,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/production/{package_id}/production-guide",
            endpoint=handler.create_production_guide_job,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/production/{package_id}/production-guide",
            endpoint=handler.get_production_guide_for_package,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/production-guides/{guide_id}",
            endpoint=handler.get_production_guide,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/production-guides/{guide_id}/export",
            endpoint=handler.export_production_guide_markdown,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/prompt-packages/{package_id}",
            endpoint=handler.get_prompt_package,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/prompt-packages/{package_id}/compile-prompts",
            endpoint=handler.create_compile_prompts_job,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/prompt-packages/{package_id}/clips/{clip_id}",
            endpoint=handler.patch_prompt_package_clip,
            methods=["PATCH"],
        ),
        Route(
            "/api/narratives/{project_id}/production-assets",
            endpoint=handler.list_narrative_production_assets,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/production-assets",
            endpoint=handler.create_narrative_production_asset,
            methods=["POST"],
        ),
        # Shooting Agent sessions (mirror the Director session group)
        Route(
            "/api/narratives/{project_id}/shooting/sessions",
            endpoint=handler.create_shooting_session,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/shooting/sessions",
            endpoint=handler.list_shooting_sessions,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/shooting/sessions/{session_id}",
            endpoint=handler.get_shooting_session,
            methods=["GET"],
        ),
        Route(
            "/api/narratives/{project_id}/shooting/sessions/{session_id}",
            endpoint=handler.update_shooting_session,
            methods=["PATCH"],
        ),
        Route(
            "/api/narratives/{project_id}/shooting/sessions/{session_id}/messages",
            endpoint=handler.send_shooting_message,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/shooting/sessions/{session_id}/pause",
            endpoint=handler.pause_shooting_session,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/shooting/sessions/{session_id}/resume",
            endpoint=handler.resume_shooting_session,
            methods=["POST"],
        ),
        Route(
            "/api/narratives/{project_id}/shooting/sessions/{session_id}/cancel",
            endpoint=handler.cancel_shooting_session,
            methods=["POST"],
        ),
        # WebSocket routes
        WebSocketRoute("/api/rooms/{room_id}/ws", endpoint=websocket_room_endpoint),
        WebSocketRoute("/api/worlds/{world_id}/ws", endpoint=websocket_world_endpoint),
        WebSocketRoute(
            "/api/persona-creation/jobs/{job_id}/ws",
            endpoint=websocket_persona_creation_endpoint,
        ),
        WebSocketRoute(
            "/api/profile-enrichment/jobs/{job_id}/ws",
            endpoint=websocket_profile_enrichment_endpoint,
        ),
        WebSocketRoute(
            "/api/narrative-jobs/{job_id}/ws",
            endpoint=websocket_narrative_job_endpoint,
        ),
        # Static files mount
        Mount("/static", app=NoCacheStaticFiles(directory=str(static_dir)), name="static"),
    ]

    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
    ]

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        scan_task = asyncio.create_task(continuum.agent_discovery.scan(force_refresh=False))
        resume_task = asyncio.create_task(continuum.persona_creation.resume_pending_jobs())
        profile_resume_task = asyncio.create_task(
            continuum.profile_enrichment.resume_pending_jobs()
        )
        try:
            yield
        finally:
            for task in (scan_task, resume_task, profile_resume_task):
                if not task.done():
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)


async def _run_step_background(
    orchestrator: Any, room_id: str, manual_speaker_id: str | None, user_message: str
) -> None:
    try:
        async for _ in orchestrator.step_turn(
            room_id=room_id,
            manual_speaker_id=manual_speaker_id,
            user_message=user_message,
        ):
            pass
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await orchestrator.mark_background_turn_failed(room_id, exc)


async def _run_retry_background(
    orchestrator: Any,
    room_id: str,
    alternate_runtime_id: str | None,
    alternate_model_id: str | None,
    user_message: str,
) -> None:
    try:
        async for _ in orchestrator.retry_turn(
            room_id=room_id,
            alternate_runtime_id=alternate_runtime_id,
            alternate_model_id=alternate_model_id,
            user_message=user_message,
        ):
            pass
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await orchestrator.mark_background_turn_failed(room_id, exc)


def websocket_backend_available() -> bool:
    for module in ("websockets", "wsproto"):
        try:
            __import__(module)
            return True
        except ImportError:
            continue
    return False


def run_web_server(
    continuum: PersonaContinuum,
    host: str = "127.0.0.1",
    port: int = 8000,
    log_level: str = "info",
) -> None:
    import uvicorn

    if not websocket_backend_available():
        raise RuntimeError(
            "No WebSocket library detected. Install project extras with "
            "`uv sync`, or `uv pip install 'uvicorn[standard]' websockets`."
        )

    app = create_web_app(continuum)
    uvicorn.run(app, host=host, port=port, log_level=log_level, ws="websockets")


def main() -> None:
    from persona_continuum.config import Config

    continuum = PersonaContinuum(Config())
    continuum.init()
    try:
        run_web_server(continuum)
    finally:
        continuum.close()


if __name__ == "__main__":
    main()
