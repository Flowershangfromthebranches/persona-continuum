from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from persona_continuum.application._utils import dumps, loads, new_id
from persona_continuum.application.persona_service import PersonaService
from persona_continuum.application.session_service import SessionService
from persona_continuum.auth.profiles import redact_secrets
from persona_continuum.domain.scene import RoomSceneState
from persona_continuum.runtime.scene_runtime import SceneRuntime
from persona_continuum.runtime.turn_normalizer import normalize_turn, normalize_turn_for_prompt
from persona_continuum.security.validation import CodedError
from persona_continuum.storage.database import Database


class RoomService:
    def __init__(
        self, database: Database, personas: PersonaService, sessions: SessionService
    ) -> None:
        self.database = database
        self.personas = personas
        self.sessions = sessions

    def create_room(
        self,
        persona_ids: list[str],
        topic: str | None = None,
        initial_relationships: dict[str, dict[str, dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        room_id = new_id("room")
        room_sessions: dict[str, str] = {}
        for persona_id in persona_ids:
            self.personas.get(persona_id)
            for counterpart, prior in (initial_relationships or {}).get(persona_id, {}).items():
                self.sessions.initialize_relationship(persona_id, counterpart, prior)
            session = self.sessions.start_session(
                persona_id,
                title=f"Room {room_id}",
                counterpart_id=f"room:{room_id}",
                session_type="room_session",
                room_id=room_id,
            )
            room_sessions[persona_id] = session.id
        room = {
            "id": room_id,
            "status": "active",
            "persona_ids": persona_ids,
            "topic": topic,
            "turn_index": 0,
            "room_sessions": room_sessions,
            "transcript": [],
            "scene_state": RoomSceneState().model_dump(mode="json"),
            "shared_public_memory": [],
            "speaker_selection_state": {"previous_speaker_id": None},
            "previous_speaker_id": None,
            "previous_content": None,
            "previous_evaluation": None,
            "room_goal": topic,
            "unresolved_disagreements": [],
        }
        now = datetime.now(UTC).isoformat()
        self.database.conn.execute(
            "INSERT INTO rooms VALUES (?, ?, ?, ?, ?, ?, ?)",
            (room_id, "active", dumps(persona_ids), topic, dumps(room), now, now),
        )
        self.database.conn.commit()
        return room

    def add_persona(self, room_id: str, persona_id: str) -> dict[str, Any]:
        self.personas.get(persona_id)
        room = self.get_state(room_id)
        self._require_active(room)
        if persona_id not in room["persona_ids"]:
            room["persona_ids"].append(persona_id)
            session = self.sessions.start_session(
                persona_id,
                title=f"Room {room_id}",
                counterpart_id=f"room:{room_id}",
                session_type="room_session",
                room_id=room_id,
            )
            room.setdefault("room_sessions", {})[persona_id] = session.id
        self._save(room)
        return room

    def prepare_next(self, room_id: str, message: str = "") -> dict[str, Any]:
        room = self.get_state(room_id)
        self._require_active(room)
        persona_ids: list[str] = room["persona_ids"]
        if not persona_ids:
            raise ValueError("room_has_no_personas")
        persona_id = persona_ids[room["turn_index"] % len(persona_ids)]
        session_id = room.setdefault("room_sessions", {}).get(persona_id)
        if session_id is None:
            session = self.sessions.start_session(
                persona_id,
                title=f"Room {room_id}",
                counterpart_id=f"room:{room_id}",
                session_type="room_session",
                room_id=room_id,
            )
            session_id = session.id
            room["room_sessions"][persona_id] = session_id
            self._save(room)
        scene = RoomSceneState.model_validate(room.get("scene_state") or {})
        if message and room.get("pending_scene_message") != message:
            input_id = new_id("room_input")
            channels, scene_events = SceneRuntime().accept_turn(
                scene,
                room_id=room_id,
                turn_id=input_id,
                actor="user",
                raw_content=str(redact_secrets(message)),
            )
            for event in scene_events:
                self.database.conn.execute(
                    "INSERT OR IGNORE INTO room_scene_events VALUES (?, ?, ?, ?)",
                    (event.id, room_id, input_id, event.model_dump_json()),
                )
            room["pending_scene_message"] = message
        else:
            SceneRuntime.tick(scene)
        room["scene_state"] = scene.model_dump(mode="json")
        self._save(room)
        previous_speech = normalize_turn(
            room.get("previous_content") or "", actor="persona"
        ).spoken_text
        visible_message = "\n".join(
            [
                normalize_turn(message or room.get("topic") or "").spoken_text,
                f"Previous speaker: {room.get('previous_speaker_id') or ''}",
                f"Previous content: {previous_speech}",
                SceneRuntime.prompt_state(scene),
            ]
        )
        prepared = self.sessions.prepare_turn(
            persona_id,
            session_id,
            visible_message,
            current_time=scene.scene_time,
            counterpart_id=f"room:{room_id}",
        )
        public_memories = [
            memory
            for memory in prepared.relevant_memories
            if self._memory_visible_in_room(memory.metadata, room_id)
        ]
        prepared.relevant_memories = public_memories
        prepared.activated_emotional_memories = [
            memory
            for memory in prepared.activated_emotional_memories
            if self._memory_visible_in_room(memory.metadata, room_id)
        ]
        return {
            "room": {
                **{
                    key: value
                    for key, value in room.items()
                    if key not in {"metadata", "pending_scene_message"}
                },
                "transcript": [normalize_turn_for_prompt(t) for t in room.get("transcript", [])],
                "shared_public_memory": [
                    normalize_turn_for_prompt(t) for t in room.get("shared_public_memory", [])
                ],
                "previous_content": normalize_turn(
                    room.get("previous_content") or "", actor="persona"
                ).spoken_text,
            },
            "speaker_persona_id": persona_id,
            "session_id": session_id,
            "prepared": prepared,
        }

    def commit_turn(
        self,
        room_id: str,
        persona_id: str,
        session_id: str,
        user_message: str,
        persona_response: str,
    ) -> dict[str, Any]:
        room = self.get_state(room_id)
        self._require_active(room)
        self._require_room_member(room, persona_id)
        expected = room["persona_ids"][int(room.get("turn_index", 0)) % len(room["persona_ids"])]
        if persona_id != expected:
            raise CodedError("room_speaker_mismatch", persona_id)
        if room.get("room_sessions", {}).get(persona_id) != session_id:
            raise CodedError("room_session_mismatch", session_id)
        scene = RoomSceneState.model_validate(room.get("scene_state") or {})
        response_id = new_id("turn")
        channels, scene_events = SceneRuntime().accept_turn(
            scene,
            room_id=room_id,
            turn_id=response_id,
            actor=persona_id,
            raw_content=str(redact_secrets(persona_response)),
            advance_clock=False,
        )
        result = self.sessions.commit_turn(
            persona_id,
            session_id,
            user_message=user_message,
            persona_response=persona_response,
            occurred_at=scene.scene_time,
            scene_events=channels.scene_events,
            source_turn_id=response_id,
            counterpart_id=f"room:{room_id}",
        )
        room["scene_state"] = scene.model_dump(mode="json")
        room.pop("pending_scene_message", None)
        for event in scene_events:
            self.database.conn.execute(
                "INSERT OR IGNORE INTO room_scene_events VALUES (?, ?, ?, ?)",
                (event.id, room_id, response_id, event.model_dump_json()),
            )
        turn_record = {
            **channels.model_dump(mode="json"),
            "content": channels.spoken_text,
            "scene_time": scene.scene_time.isoformat(),
            "persona_id": persona_id,
            "session_id": session_id,
            "user_message": user_message,
            "persona_response": persona_response,
            "turn_id": result["turn_id"],
            "created_at": datetime.now(UTC).isoformat(),
        }
        room.setdefault("transcript", []).append(turn_record)
        room.setdefault("shared_public_memory", []).append(turn_record)
        room["turn_index"] = int(room.get("turn_index", 0)) + 1
        room["previous_speaker_id"] = persona_id
        room["previous_content"] = channels.spoken_text
        room["previous_evaluation"] = {"acknowledged": True}
        room.setdefault("speaker_selection_state", {})["previous_speaker_id"] = persona_id
        for other_id in room.get("persona_ids", []):
            if other_id == persona_id:
                continue
            self.sessions._apply_relationship_delta(
                persona_id,
                other_id,
                {"familiarity": 0.05},
                "main",
                session_id,
                result["turn_id"],
                "room_public_turn",
                additive=True,
            )
            self.sessions._apply_relationship_delta(
                other_id,
                persona_id,
                {"familiarity": 0.05},
                "main",
                room.get("room_sessions", {}).get(other_id),
                result["turn_id"],
                "room_public_turn",
                additive=True,
            )
        self._save(room)
        return {"room": room, "turn": result}

    def get_state(self, room_id: str) -> dict[str, Any]:
        row = self.database.conn.execute(
            "SELECT state_json FROM rooms WHERE id = ?", (room_id,)
        ).fetchone()
        if row is None:
            raise KeyError(room_id)
        return dict(loads(row["state_json"]))

    def close(self, room_id: str) -> dict[str, Any]:
        room = self.get_state(room_id)
        room["status"] = "closed"
        self._save(room)
        return room

    def _require_active(self, room: dict[str, Any]) -> None:
        if room.get("status") != "active":
            raise CodedError("room_closed", str(room.get("id")))

    def _require_room_member(self, room: dict[str, Any], persona_id: str) -> None:
        if persona_id not in room.get("persona_ids", []):
            raise CodedError("room_persona_not_member", persona_id)

    def _memory_visible_in_room(self, metadata: dict[str, Any], room_id: str) -> bool:
        visibility = metadata.get("visibility", "persona_private")
        if visibility in {"room_public", "global"}:
            return True
        if visibility != "shared_with_specific_personas":
            return False
        shared_rooms = {str(value) for value in metadata.get("shared_with_room_ids", [])}
        return room_id in shared_rooms

    def _save(self, room: dict[str, Any]) -> None:
        # This legacy room schema predates room metadata (it carries no
        # ``metadata`` key at all), while the orchestrator owns keys such as
        # ``rolling_summary``, the summary version stamp and the ``raw_archive_*``
        # migration archives in the very same ``state_json`` column.  Saving this
        # schema wholesale would therefore delete them, so the stored metadata is
        # carried across instead of being dropped.
        if "metadata" not in room:
            row = self.database.conn.execute(
                "SELECT state_json FROM rooms WHERE id = ?", (room["id"],)
            ).fetchone()
            if row is not None:
                try:
                    existing = loads(row["state_json"]) or {}
                except Exception:
                    existing = {}
                metadata = existing.get("metadata") if isinstance(existing, dict) else None
                if isinstance(metadata, dict) and metadata:
                    room["metadata"] = metadata
        self.database.conn.execute(
            """
            UPDATE rooms
            SET status = ?, persona_ids_json = ?, state_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                room["status"],
                dumps(room["persona_ids"]),
                dumps(room),
                datetime.now(UTC).isoformat(),
                room["id"],
            ),
        )
        self.database.conn.commit()
