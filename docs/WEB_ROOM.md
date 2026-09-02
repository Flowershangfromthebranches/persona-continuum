# Multi-Agent Web Dashboard & WebSocket Room

## Web UI Architecture

The Persona Continuum Web Dashboard is built on top of Starlette with native WebSocket streaming and REST endpoints. The web process requires a WebSocket backend (`websockets` via `uvicorn[standard]`). If the terminal shows `No supported WebSocket library detected`, run `uv sync` and restart `persona-continuum web`. A missing backend turns `/api/rooms/{id}/ws` into a plain HTTP 404 and the live transcript will not stream.

```
Browser Client <====== WebSocket (/api/rooms/{id}/ws) ======> Starlette Web Server
               <====== HTTP REST (/api/rooms, /api/agents) ===> WebAPIHandler -> MultiAgentOrchestrator
```

## REST API Endpoints

- `GET /api/health` - Server health probe and version status.
- `GET /api/agents` - Scan and list all discovered agent runtimes with readiness status.
- `GET /api/personas` - List local personas with metadata and compiled dimensions.
- `GET /api/auth-profiles` - List configured auth profiles (masked secrets).
- `POST /api/auth-profiles` - Save or update an auth profile.
- `GET /api/rooms` - List all discussion rooms.
- `POST /api/rooms` - Create a new multi-agent room with participants and director config.
- `GET /api/rooms/{id}` - Inspect room details and frozen binding snapshots.
- `POST /api/rooms/{id}/start` - Start discussion room and freeze participant bindings.
- `POST /api/rooms/{id}/pause` - Pause discussion room execution.
- `POST /api/rooms/{id}/resume` - Resume paused discussion room.
- `POST /api/rooms/{id}/stop` - Stop discussion room.
- `POST /api/rooms/{id}/step` - Step single turn with optional human/director intervention.
- `GET /api/rooms/{id}/transcripts` - Fetch full structured room transcript records.
- `POST /api/rooms/{id}/protocol/convert` - Migrate a `free_discussion` room to `expert_consultation`. Protocol migration must NOT go through `PATCH /api/rooms/{id}` (it would bypass the live-run reset and the audit event); always use this endpoint.

  Request:

  ```json
  {
    "target_protocol": "expert_consultation",
    "role_mapping": {
      "slot_host": "host",
      "slot_w1": "expert",
      "slot_w2": "expert"
    }
  }
  ```

  `role_mapping` must cover every enabled participant, map exactly one enabled participant to `host`, and map the rest to `expert`. The response is the full converted room (`{"ok": true, "data": { ...room }}`) with `protocol` switched, roles rewritten, `host_participant_id` updated, and `protocol_state` reset to `waiting_user`; transcript, case state, shared context and persona session bindings are preserved.

  Errors: `404` room not found; `409` a protocol run is active or the room status is not lifecycle-active; `400` unsupported target protocol or invalid role mapping, e.g. `{"ok": false, "error": "role_mapping 必须有且仅有一个启用的 host 映射；映射预览：…", "details": {"code": "invalid_role_mapping"}}`.

## WebSocket Streaming Protocol (`/api/rooms/{id}/ws`)

### Client Commands
```json
{"action": "start"}
{"action": "pause"}
{"action": "resume"}
{"action": "stop"}
{"action": "step", "user_message": "Steve, what do you think?", "next_speaker_id": "slot_steve"}
```

### Server Event Frame Schema
```json
{
  "event": "agent_message_delta",
  "room_id": "room_abc123",
  "turn_index": 1,
  "speaker_id": "slot_steve",
  "speaker_name": "Steve Jobs",
  "text_delta": "Simplicity is the ultimate sophistication.",
  "timestamp": "2026-08-23T15:30:00Z"
}
```
Event lifecycle: `room_started` -> `turn_started` -> `speaker_selected` -> `recall_started` -> `recall_completed` -> `agent_started` -> `agent_message_delta` -> `agent_completed` -> `persona_commit_started` -> `persona_commit_completed` -> `turn_completed`.
# Persona Creation and World Completion

The Personas view exposes **创建人格 / Create Persona**. It uses the shared
READY/Connected runtime selector and shows asynchronous research, source,
dimension, artifact, interview and compilation progress. The same selector is
available as the Parallel World Persona Creation Engine. World Builder output
is matched against Persona ID, name and aliases before a world is persisted;
missing human actors require explicit completion confirmation, while
organization/environment actors remain non-person actors.
