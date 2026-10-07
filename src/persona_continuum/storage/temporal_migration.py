"""One-time, additive archive -> semantic views migration. No runtime state resets."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from persona_continuum.domain.scene import RoomSceneState
from persona_continuum.room.context_manager import LEGACY_SUMMARY_MARKER
from persona_continuum.runtime.scene_runtime import SceneRuntime
from persona_continuum.runtime.turn_normalizer import normalize_turn_for_prompt, semantic_experience

VERSION = "room_scene_style_firewall_v1"


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def migrate_temporal_history(conn: sqlite3.Connection) -> dict[str, Any]:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS room_scene_events (
            id TEXT PRIMARY KEY, room_id TEXT NOT NULL, source_turn_id TEXT NOT NULL,
            event_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_scene_events_room ON room_scene_events(room_id);
        CREATE TABLE IF NOT EXISTS runtime_data_migrations (
            version TEXT PRIMARY KEY, completed_at TEXT NOT NULL, report_json TEXT NOT NULL
        );
    """)
    done = conn.execute(
        "SELECT report_json FROM runtime_data_migrations WHERE version=?", (VERSION,)
    ).fetchone()
    if done:
        return {**json.loads(done[0]), "already_applied": True}
    report: dict[str, Any] = {
        "version": VERSION,
        "rooms": 0,
        "turns": 0,
        "archived_memories": 0,
        "semantic_memories": 0,
    }
    turn_scene_times: dict[str, str] = {}
    conn.execute("SAVEPOINT temporal_history")
    try:
        for room in conn.execute("SELECT id, state_json, created_at FROM rooms").fetchall():
            state = json.loads(room["state_json"])
            rows = conn.execute(
                "SELECT * FROM room_transcripts WHERE room_id=? ORDER BY created_at, rowid",
                (room["id"],),
            ).fetchall()
            original_transcript = state.get("transcript", [])
            retained = {
                str(t.get("turn_id") or f"legacy:{room['id']}:{i}"): {
                    **t,
                    "turn_id": t.get("turn_id") or f"legacy:{room['id']}:{i}",
                }
                for i, t in enumerate(original_transcript)
            }
            retained_ids = set(retained)
            turns = []
            for row in rows:
                turns.append(
                    {
                        **retained.pop(str(row["turn_id"]), {}),
                        "turn_id": row["turn_id"],
                        "participant_id": row["participant_id"],
                        "speaker_name": row["speaker_name"],
                        "content": row["content"],
                        "created_at": row["created_at"],
                        "metadata": json.loads(row["metadata_json"]),
                    }
                )
            turns.extend(retained.values())
            turns.sort(key=lambda t: t.get("created_at") or room["created_at"])
            initial = datetime.fromisoformat(
                (turns[0].get("created_at") if turns else None) or room["created_at"]
            )
            scene = RoomSceneState(scene_time=initial, clock_wall_time=initial)
            facts = []
            for index, turn in enumerate(turns):
                turn.setdefault("turn_id", f"legacy:{room['id']}:{index}")
                wall = datetime.fromisoformat(turn.get("created_at") or initial.isoformat())
                channels, events = SceneRuntime().accept_turn(
                    scene,
                    room_id=str(room["id"]),
                    turn_id=str(turn.get("turn_id", "legacy")),
                    actor=str(turn.get("participant_id") or turn.get("persona_id") or "user"),
                    raw_content=str(turn.get("content", turn.get("persona_response", ""))),
                    wall_time=wall,
                )
                turn.update(channels.model_dump(mode="json"))
                turn["scene_time"] = scene.scene_time.isoformat()
                turn_scene_times[str(turn["turn_id"])] = turn["scene_time"]
                meta = turn.setdefault("metadata", {})
                meta["channels"] = {
                    **channels.model_dump(mode="json"),
                    "scene_time": scene.scene_time.isoformat(),
                }
                conn.execute(
                    "UPDATE room_transcripts SET metadata_json=? WHERE room_id=? AND turn_id=?",
                    (encode(meta), room["id"], turn["turn_id"]),
                )
                for event in events:
                    conn.execute(
                        "INSERT OR IGNORE INTO room_scene_events VALUES (?, ?, ?, ?)",
                        (event.id, room["id"], event.source_turn_id, event.model_dump_json()),
                    )
                normalized = normalize_turn_for_prompt(turn)
                facts.append(encode(normalized))
                report["turns"] += 1
            state["scene_state"] = scene.model_dump(mode="json")
            # Keep any pre-existing snapshot window; durable rows remain the authority.
            old_ids = retained_ids
            state["transcript"] = [t for t in turns if t.get("turn_id") in old_ids]
            meta = state.setdefault("metadata", {})
            meta["raw_archive_transcript_v0"] = original_transcript
            # NOTE: this migration used to overwrite ``rolling_summary`` with the
            # scene/relationship evidence dump above and stamp it
            # ``rolling_summary_version = 1``.  That is evidence, not a summary,
            # and stamping it made the runtime inject a raw transcript as if it
            # were the canonical long-term summary (see
            # room/context_manager.LEGACY_SUMMARY_MARKER).  The evidence text now
            # lives under its own key; any pre-existing summary is preserved
            # rather than clobbered, and no version marker is written.
            meta["raw_archive_scene_evidence_v1"] = "\n".join(facts)
            if meta.get("rolling_summary") and not meta.get(
                "raw_archive_rolling_summary_v0"
            ):
                meta["raw_archive_rolling_summary_v0"] = meta["rolling_summary"]
            existing_summary = meta.get("rolling_summary")
            if isinstance(existing_summary, str) and existing_summary.lstrip().startswith(
                LEGACY_SUMMARY_MARKER
            ):
                # Quarantine a legacy dump so it can never be read as canonical.
                meta["raw_archive_legacy_rolling_summary"] = existing_summary
                meta.pop("rolling_summary", None)
                meta.pop("rolling_summary_version", None)
            meta["voice_reset_participants"] = [
                p["participant_id"] for p in state.get("participants", [])
            ]
            meta["scene_migration_version"] = VERSION
            conn.execute("UPDATE rooms SET state_json=? WHERE id=?", (encode(state), room["id"]))
            report["rooms"] += 1

        for row in conn.execute(
            "SELECT * FROM memories WHERE type='digital_experience'"
        ).fetchall():
            metadata = json.loads(row["metadata_json"])
            if metadata.get("semantic_experience_version"):
                continue
            metadata["retrieval_role"] = "raw_archive"
            conn.execute(
                "UPDATE memories SET metadata_json=? WHERE id=?", (encode(metadata), row["id"])
            )
            report["archived_memories"] += 1
            if row["validity"] != "valid":
                continue
            turn = conn.execute(
                "SELECT * FROM session_turns WHERE id=?", (metadata.get("turn_id", ""),)
            ).fetchone()
            occurred = turn_scene_times.get(str(metadata.get("turn_id"))) or row["occurred_at"]
            if turn:
                user, response = turn["user_message"], turn["persona_response"]
                turn_meta = json.loads(turn["context_json"])
                occurred = turn_meta.get("occurred_at") or occurred
            else:
                raw = str(row["content"])
                user, sep, response = raw.removeprefix("User asked: ").partition(
                    "\nPersona answered: "
                )
                if not sep:
                    response = ""
            copy = dict(row)
            new_meta = {
                **metadata,
                "retrieval_role": "event",
                "semantic_experience_version": 1,
                "raw_archive_id": row["id"],
                "migration_version": VERSION,
            }
            copy.update(
                id=f"semantic:{row['id']}",
                content=semantic_experience(
                    user, response, counterpart=str(metadata.get("counterpart_id", "user"))
                ),
                occurred_at=occurred,
                written_at=datetime.now(UTC).isoformat(),
                metadata_json=encode(new_meta),
                access_count=0,
                last_accessed_at=None,
            )
            conn.execute(
                f"INSERT OR IGNORE INTO memories ({','.join(copy)}) "
                f"VALUES ({','.join('?' for _ in copy)})",
                tuple(copy.values()),
            )
            report["semantic_memories"] += 1
        conn.execute("DELETE FROM memories_fts")
        for row in conn.execute(
            "SELECT id, persona_id, content, metadata_json FROM memories WHERE validity='valid'"
        ).fetchall():
            if json.loads(row["metadata_json"]).get("retrieval_role") != "raw_archive":
                conn.execute(
                    "INSERT INTO memories_fts(memory_id, persona_id, content) VALUES (?,?,?)",
                    (row["id"], row["persona_id"], row["content"]),
                )
        conn.execute(
            "INSERT INTO runtime_data_migrations VALUES (?, ?, ?)",
            (VERSION, datetime.now(UTC).isoformat(), encode(report)),
        )
        conn.execute("RELEASE temporal_history")
    except Exception:
        conn.execute("ROLLBACK TO temporal_history")
        conn.execute("RELEASE temporal_history")
        raise
    return report
