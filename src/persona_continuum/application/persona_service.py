from __future__ import annotations

import builtins
import contextlib
import hashlib
import json
import shutil
import sqlite3
import tempfile
import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from persona_continuum.application._utils import dumps, loads, new_id, sha256_file
from persona_continuum.config import Config
from persona_continuum.domain.evidence import EvidenceSource
from persona_continuum.domain.persona import PersonaManifest, PersonaRecord, PersonaType, RunMode
from persona_continuum.ingestion.loader import SourceLoader
from persona_continuum.security.paths import ensure_child_path, safe_slug, validate_zip_members
from persona_continuum.security.validation import (
    CodedError,
    ConflictError,
    NotFoundError,
    SecurityError,
)
from persona_continuum.storage.database import Database

DATA_TABLES = [
    "sources",
    "claims",
    "memories",
    "affect_states",
    "needs",
    "relationships",
    "sessions",
    "session_turns",
    "compilation_tasks",
    "continuations",
    "continuation_branches",
    "rooms",
    "lineage",
    "research_artifacts",
    "compiled_components",
    "compile_snapshots",
    "change_events",
    "change_event_supports",
    "evaluation_suites",
    "evaluation_cases",
    "evaluation_results",
]

DATA_FILES = {
    "sources": "data/sources.jsonl",
    "claims": "data/claims.jsonl",
    "memories": "data/memories.jsonl",
    "affect_states": "data/affect_states.jsonl",
    "needs": "data/needs.jsonl",
    "relationships": "data/relationships.jsonl",
    "sessions": "data/sessions.jsonl",
    "session_turns": "data/session_turns.jsonl",
    "compilation_tasks": "data/compilation_tasks.jsonl",
    "continuations": "data/continuations.jsonl",
    "continuation_branches": "data/continuation_branches.jsonl",
    "rooms": "data/rooms.jsonl",
    "lineage": "data/lineage.jsonl",
    "research_artifacts": "data/research_artifacts.jsonl",
    "compiled_components": "data/compiled_components.jsonl",
    "compile_snapshots": "data/compile_snapshots.jsonl",
    "change_events": "data/change_events.jsonl",
    "change_event_supports": "data/change_event_supports.jsonl",
    "evaluation_suites": "data/evaluation_suites.jsonl",
    "evaluation_cases": "data/evaluation_cases.jsonl",
    "evaluation_results": "data/evaluation_results.jsonl",
}

BUNDLE_PERSONAS_FILE = "data/personas.jsonl"


class PersonaService:
    def __init__(self, config: Config, database: Database) -> None:
        self.config = config
        self.database = database
        self.loader = SourceLoader(config.max_source_bytes)

    def create(
        self,
        *,
        display_name: str,
        aliases: list[str],
        persona_type: PersonaType,
        run_mode: RunMode,
        birth_date: str | None = None,
        death_date: str | None = None,
        data_cutoff_date: str | None = None,
        sensitivity: str = "normal",
        persona_id: str | None = None,
    ) -> PersonaRecord:
        base_id = persona_id or safe_slug(display_name)
        persona_id = self._unique_id(base_id)
        manifest = PersonaManifest(
            id=persona_id,
            display_name=display_name,
            aliases=aliases,
            persona_type=persona_type,
            run_mode=run_mode,
            birth_date=birth_date,
            death_date=death_date,
            data_cutoff_date=data_cutoff_date,
            sensitivity=sensitivity,
        )
        package_path = self.config.personas_dir / persona_id
        package_path.mkdir(parents=True, exist_ok=True)
        self._write_package_skeleton(manifest)
        now = datetime.now(UTC).isoformat()
        self.database.conn.execute(
            "INSERT INTO personas VALUES (?, ?, ?, ?, ?, ?)",
            (persona_id, dumps(manifest.model_dump()), str(package_path), 0, now, now),
        )
        self.database.conn.commit()
        return self.get(persona_id)

    def list(self, include_archived: bool = False) -> list[PersonaRecord]:
        if include_archived:
            rows = self.database.conn.execute(
                "SELECT * FROM personas ORDER BY created_at"
            ).fetchall()
        else:
            rows = self.database.conn.execute(
                "SELECT * FROM personas WHERE archived = 0 ORDER BY created_at"
            ).fetchall()
        return [self._row_to_persona(row) for row in rows]

    def get(self, persona_id: str) -> PersonaRecord:
        row = self.database.conn.execute(
            "SELECT * FROM personas WHERE id = ?", (persona_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(persona_id)
        return self._row_to_persona(row)

    def update_manifest(self, manifest: PersonaManifest) -> PersonaRecord:
        now = datetime.now(UTC).isoformat()
        self.database.conn.execute(
            "UPDATE personas SET manifest_json = ?, archived = ?, updated_at = ? WHERE id = ?",
            (dumps(manifest.model_dump()), int(manifest.archived), now, manifest.id),
        )
        self.database.conn.commit()
        self._write_package_skeleton(manifest)
        return self.get(manifest.id)

    def activate(self, persona_id: str) -> PersonaRecord:
        persona = self.get(persona_id)
        persona.manifest.active = True
        return self.update_manifest(persona.manifest)

    def archive(self, persona_id: str) -> PersonaRecord:
        persona = self.get(persona_id)
        persona.manifest.archived = True
        self.database.conn.execute("UPDATE personas SET archived = 1 WHERE id = ?", (persona_id,))
        self.database.conn.commit()
        return self.update_manifest(persona.manifest)

    def delete(self, persona_id: str) -> bool:
        persona = self.get(persona_id)
        package_path = ensure_child_path(self.config.personas_dir, Path(persona.package_path))
        try:
            self.database.conn.execute("BEGIN")
            self._remove_persona_from_rooms(persona_id)
            self._delete_persona_rows(persona_id)
            self.database.conn.commit()
        except Exception:
            self.database.conn.rollback()
            raise
        shutil.rmtree(package_path, ignore_errors=True)
        return True

    def add_sources(self, persona_id: str, paths: Sequence[Path]) -> Sequence[EvidenceSource]:
        self.get(persona_id)
        added = []
        for doc in self.loader.load_many(paths):
            hash_value = (
                sha256_file(doc.path) if doc.path.exists() else self._hash_text(doc.content)
            )
            source = EvidenceSource(
                id=new_id("src"),
                persona_id=persona_id,
                source_type=doc.source_type,
                path=str(doc.path),
                title=doc.title,
                hash=hash_value,
                content=doc.content,
                metadata=doc.metadata,
            )
            try:
                self.database.conn.execute(
                    "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        source.id,
                        source.persona_id,
                        source.source_type,
                        source.path,
                        source.title,
                        source.hash,
                        source.content,
                        dumps(source.metadata),
                        source.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"duplicate_source:{doc.path}") from exc
            added.append(source)
        if added:
            manifest = self.get(persona_id).manifest
            manifest.source_count = self.source_count(persona_id)
            self.update_manifest(manifest)
        return added

    def add_source_text(
        self,
        persona_id: str,
        *,
        title: str,
        source_type: str,
        canonical_url: str | None,
        publisher: str | None,
        author: str | None,
        published_at: str | None,
        accessed_at: str | None,
        content: str,
        hash: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> EvidenceSource:
        self.get(persona_id)
        computed_hash = self._hash_text(content)
        if hash and hash != computed_hash:
            raise CodedError("source_hash_mismatch", hash)
        hash_value = hash or computed_hash
        source = EvidenceSource(
            id=new_id("src"),
            persona_id=persona_id,
            source_type=source_type,
            path=canonical_url or f"text://{hash_value}",
            title=title,
            hash=hash_value,
            content=content,
            metadata={
                **(metadata or {}),
                "canonical_url": canonical_url,
                "publisher": publisher,
                "author": author,
                "published_at": published_at,
                "accessed_at": accessed_at,
                "ingest_method": "text",
            },
        )
        try:
            self.database.conn.execute(
                "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source.id,
                    source.persona_id,
                    source.source_type,
                    source.path,
                    source.title,
                    source.hash,
                    source.content,
                    dumps(source.metadata),
                    source.created_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"duplicate_source:{hash_value}") from exc
        manifest = self.get(persona_id).manifest
        manifest.source_count = self.source_count(persona_id)
        self.update_manifest(manifest)
        self._write_database_evidence_files(persona_id)
        return source

    def source_count(self, persona_id: str) -> int:
        row = self.database.conn.execute(
            "SELECT COUNT(*) AS count FROM sources WHERE persona_id = ?", (persona_id,)
        ).fetchone()
        return int(row["count"])

    def get_sources(self, persona_id: str) -> Sequence[EvidenceSource]:
        rows = self.database.conn.execute(
            "SELECT * FROM sources WHERE persona_id = ? ORDER BY created_at", (persona_id,)
        ).fetchall()
        return [
            EvidenceSource(
                id=str(row["id"]),
                persona_id=str(row["persona_id"]),
                source_type=str(row["source_type"]),
                path=str(row["path"]),
                title=str(row["title"]),
                hash=str(row["hash"]),
                content=str(row["content"]),
                metadata=dict(loads(row["metadata_json"])),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def delete_source(
        self, persona_id: str, source_id: str, strategy: str = "invalidate_and_recompile"
    ) -> bool:
        if strategy not in {"invalidate_and_recompile", "hard_delete_derived"}:
            raise CodedError("invalid_delete_source_strategy", strategy)
        self.get(persona_id)
        row = self.database.conn.execute(
            "SELECT id FROM sources WHERE persona_id = ? AND id = ?", (persona_id, source_id)
        ).fetchone()
        if row is None:
            raise NotFoundError(source_id)
        now = datetime.now(UTC).isoformat()
        descendants = self._collect_lineage_descendants(persona_id, "source", source_id)
        artifact_ids = {
            object_id
            for object_type, object_id in descendants
            if object_type == "research_artifact"
        }
        component_ids = {
            object_id
            for object_type, object_id in descendants
            if object_type == "compiled_component"
        }
        claim_ids = {
            object_id for object_type, object_id in descendants if object_type == "claim"
        }
        memory_ids = {
            object_id for object_type, object_id in descendants if object_type == "memory"
        }
        self.database.conn.execute(
            """
            UPDATE memories
            SET validity = 'source_deleted',
                metadata_json = json_set(
                  metadata_json,
                  '$.invalidated_reason',
                  'source_deleted',
                  '$.invalidated_at',
                  ?
                )
            WHERE persona_id = ? AND source_id = ?
            """,
            (now, persona_id, source_id),
        )
        for memory_id in memory_ids:
            self.database.conn.execute(
                """
                UPDATE memories
                SET validity = 'source_deleted',
                    metadata_json = json_set(
                      metadata_json,
                      '$.invalidated_reason',
                      'source_deleted',
                      '$.invalidated_at',
                      ?
                    )
                WHERE persona_id = ? AND id = ?
                """,
                (now, persona_id, memory_id),
            )
        self.database.conn.execute(
            """
            DELETE FROM memories_fts
            WHERE memory_id IN (
              SELECT id FROM memories
              WHERE persona_id = ? AND source_id = ? AND validity = 'source_deleted'
            )
            """,
            (persona_id, source_id),
        )
        for memory_id in memory_ids:
            self.database.conn.execute(
                "DELETE FROM memories_fts WHERE persona_id = ? AND memory_id = ?",
                (persona_id, memory_id),
            )
        self.database.conn.execute(
            """
            UPDATE claims
            SET confidence = 0,
                has_counter_evidence = 1,
                metadata_json = json_set(
                  metadata_json,
                  '$.invalidated_reason',
                  'source_deleted',
                  '$.invalidated_at',
                  ?
                )
            WHERE persona_id = ? AND source_id = ?
            """,
            (now, persona_id, source_id),
        )
        for claim_id in claim_ids:
            self.database.conn.execute(
                """
                UPDATE claims
                SET confidence = 0,
                    has_counter_evidence = 1,
                    metadata_json = json_set(
                      metadata_json,
                      '$.invalidated_reason',
                      'source_deleted',
                      '$.invalidated_at',
                      ?
                    )
                WHERE persona_id = ? AND id = ?
                """,
                (now, persona_id, claim_id),
            )
        if artifact_ids:
            placeholders = ",".join("?" for _ in artifact_ids)
            self.database.conn.execute(
                f"DELETE FROM research_artifacts WHERE persona_id = ? AND id IN ({placeholders})",
                (persona_id, *sorted(artifact_ids)),
            )
        if component_ids:
            placeholders = ",".join("?" for _ in component_ids)
            self.database.conn.execute(
                f"DELETE FROM compiled_components WHERE persona_id = ? AND id IN ({placeholders})",
                (persona_id, *sorted(component_ids)),
            )
        if artifact_ids:
            for artifact_id in sorted(artifact_ids):
                self.database.conn.execute(
                    """
                    DELETE FROM compiled_components
                    WHERE persona_id = ?
                      AND source_artifact_ids_json LIKE ?
                    """,
                    (persona_id, f"%{artifact_id}%"),
                )
        if artifact_ids or component_ids:
            self.database.conn.execute(
                "DELETE FROM compile_snapshots WHERE persona_id = ?", (persona_id,)
            )
        if artifact_ids:
            task_rows = self.database.conn.execute(
                """
                SELECT id, artifacts_json, plan_json
                FROM compilation_tasks
                WHERE persona_id = ?
                """,
                (persona_id,),
            ).fetchall()
            for task_row in task_rows:
                artifacts = [
                    artifact
                    for artifact in list(loads(task_row["artifacts_json"]))
                    if str(artifact.get("artifact_id")) not in artifact_ids
                    and source_id not in set(map(str, artifact.get("source_ids", [])))
                ]
                plan = dict(loads(task_row["plan_json"]))
                plan["needs_recompile_reason"] = "source_deleted"
                self.database.conn.execute(
                    """
                    UPDATE compilation_tasks
                    SET status = 'needs_recompile',
                        artifacts_json = ?,
                        plan_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        dumps(artifacts),
                        dumps(plan),
                        datetime.now(UTC).isoformat(),
                        task_row["id"],
                    ),
                )
        self.database.conn.execute(
            "DELETE FROM sources WHERE persona_id = ? AND id = ?", (persona_id, source_id)
        )
        affected_objects = {
            ("source", source_id),
            *descendants,
        }
        for object_type, object_id in affected_objects:
            self.database.conn.execute(
                """
                DELETE FROM lineage
                WHERE persona_id = ?
                  AND (
                    (child_type = ? AND child_id = ?)
                    OR (parent_type = ? AND parent_id = ?)
                  )
                """,
                (persona_id, object_type, object_id, object_type, object_id),
            )
        self.database.conn.commit()
        manifest = self.get(persona_id).manifest
        manifest.source_count = self.source_count(persona_id)
        manifest.compile_state = "needs_recompile"
        self.update_manifest(manifest)
        self._reset_derived_persona_files(persona_id)
        self._write_database_evidence_files(persona_id)
        return True

    def export_persona(
        self,
        persona_id: str,
        output_path: Path | None = None,
        mode: str = "full",
        room_export_mode: str = "omit",
    ) -> Path:
        if mode not in {"full", "identity_only", "redacted"}:
            raise CodedError("invalid_export_mode", mode)
        if room_export_mode not in {"omit", "transcript_only", "bundle"}:
            raise CodedError("invalid_room_export_mode", room_export_mode)
        persona = self.get(persona_id)
        output_path = output_path or (self.config.exports_dir / f"{persona_id}.persona.zip")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_package_skeleton(persona.manifest)
        package_root = Path(persona.package_path)
        export_persona_ids = self._export_persona_ids(persona_id, room_export_mode, mode)
        with tempfile.TemporaryDirectory(prefix="persona-export-") as raw_tmp:
            staging = Path(raw_tmp)
            self._stage_package(
                persona,
                staging,
                package_root,
                mode,
                room_export_mode=room_export_mode,
                export_persona_ids=export_persona_ids,
            )
            with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(staging.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(staging))
        return output_path

    def import_persona(self, package_path: Path, new_id: str | None = None) -> PersonaRecord:
        validate_zip_members(package_path)
        package_root: Path | None = None
        with zipfile.ZipFile(package_path) as archive:
            self._verify_package_archive(archive)
            raw_manifest = yaml.safe_load(archive.read("manifest.yaml"))
            manifest = PersonaManifest.model_validate(raw_manifest["persona"])
            original_id = manifest.id
            manifest.id = new_id or self._unique_id(manifest.id)
            if self.database.conn.execute(
                "SELECT 1 FROM personas WHERE id = ?", (manifest.id,)
            ).fetchone():
                manifest.id = self._unique_id(manifest.id)
            manifest.active = False
            package_root = ensure_child_path(
                self.config.personas_dir, self.config.personas_dir / manifest.id
            )
            id_maps = self._build_import_id_maps(archive, original_id, manifest.id)
            try:
                self.database.conn.execute("BEGIN")
                if package_root.exists():
                    shutil.rmtree(package_root)
                package_root.mkdir(parents=True)
                self._extract_package_files(archive, package_root)
                self._write_package_skeleton(manifest)
                self._rewrite_package_file_ids(
                    package_root, original_id, manifest.id, id_maps
                )
                now = datetime.now(UTC).isoformat()
                self.database.conn.execute(
                    "INSERT INTO personas VALUES (?, ?, ?, ?, ?, ?)",
                    (manifest.id, dumps(manifest.model_dump()), str(package_root), 0, now, now),
                )
                self._import_bundle_personas(archive, original_id, id_maps)
                self._import_data_files(archive, original_id, manifest.id, id_maps)
                self._rebuild_fts(manifest.id)
                for mapped_id in set(id_maps.get("personas", {}).values()) - {manifest.id}:
                    self._rebuild_fts(mapped_id)
                self._assert_import_consistency(manifest.id)
                self.database.conn.commit()
            except Exception:
                self.database.conn.rollback()
                if package_root is not None:
                    shutil.rmtree(package_root, ignore_errors=True)
                raise
        return self.get(manifest.id)

    def _stage_package(
        self,
        persona: PersonaRecord,
        staging: Path,
        package_root: Path,
        mode: str,
        *,
        room_export_mode: str,
        export_persona_ids: set[str],
    ) -> None:
        data_root = staging / "data"
        files_root = staging / "files"
        data_root.mkdir(parents=True)
        files_root.mkdir(parents=True)
        manifest_payload = {
            "package_schema_version": "1.1",
            "export_mode": mode,
            "room_export_mode": room_export_mode,
            "exported_at": datetime.now(UTC).isoformat(),
            "original_persona_id": persona.id,
            "persona": json.loads(dumps(persona.manifest.model_dump())),
        }
        (staging / "manifest.yaml").write_text(
            yaml.safe_dump(manifest_payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        (staging / "package_schema.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.1",
                    "data_record_shape": {"schema_version": "1.1", "data": "sqlite row"},
                    "required_files": ["manifest.yaml", "package_schema.json", "checksums.json"],
                    "modes": ["full", "identity_only", "redacted"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        for table in self._tables_for_mode(mode):
            rows = self._export_rows_for_table(table, export_persona_ids, room_export_mode)
            lines = []
            for row in rows:
                data = dict(row)
                if self._skip_export_row(table, data):
                    continue
                if mode == "redacted":
                    if table != "sources":
                        continue
                    data = self._redacted_source_row(data)
                lines.append(dumps({"schema_version": "1.1", "data": data}))
            (staging / DATA_FILES[table]).write_text("\n".join(lines), encoding="utf-8")
        if room_export_mode == "bundle":
            self._stage_bundle_personas(persona.id, export_persona_ids, staging)
        if mode == "redacted":
            self._stage_redacted_files(persona, files_root)
            (staging / "redaction_manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.1",
                        "mode": "redacted",
                        "removed": [
                            "source.content",
                            "source.path",
                            "research_artifacts",
                            "compiled_components",
                            "private persona files",
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        else:
            for path in sorted(package_root.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(package_root)
                if any(part.startswith("__pycache__") for part in relative.parts):
                    continue
                target = files_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        checksums = {
            "schema_version": "1.1",
            "algorithm": "sha256",
            "files": self._package_checksums(staging),
        }
        (staging / "checksums.json").write_text(
            json.dumps(checksums, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _tables_for_mode(self, mode: str) -> builtins.list[str]:
        if mode == "identity_only":
            return [
                "sources",
                "claims",
                "memories",
                "lineage",
                "research_artifacts",
                "compiled_components",
                "compile_snapshots",
            ]
        return DATA_TABLES

    def _skip_export_row(self, table: str, data: dict[str, Any]) -> bool:
        metadata = {}
        if data.get("metadata_json"):
            with contextlib.suppress(Exception):
                metadata = dict(loads(str(data["metadata_json"])))
        if metadata.get("invalidated_reason") == "source_deleted":
            return True
        if table == "memories" and data.get("validity") != "valid":
            return True
        return table == "claims" and float(data.get("confidence") or 0) <= 0

    def _redacted_source_row(self, data: dict[str, Any]) -> dict[str, Any]:
        redacted = dict(data)
        redacted["content"] = "[redacted]"
        redacted["path"] = "[redacted]"
        redacted["hash"] = "[redacted]"
        metadata = dict(loads(str(redacted.get("metadata_json", "{}"))))
        for key in [
            "path",
            "local_path",
            "upload_path",
            "source_path",
            "zip_member",
            "canonical_url",
            "url",
            "source_url",
            "publisher",
            "author",
            "published_at",
            "accessed_at",
            "hash",
        ]:
            metadata.pop(key, None)
        metadata["redacted"] = True
        redacted["metadata_json"] = dumps(metadata)
        return redacted

    def _export_rows_for_table(
        self, table: str, persona_ids: set[str], room_export_mode: str
    ) -> builtins.list[sqlite3.Row]:
        if table == "rooms":
            if room_export_mode != "bundle":
                return []
            rows = self.database.conn.execute("SELECT * FROM rooms ORDER BY rowid").fetchall()

            def room_contains_persona(row: sqlite3.Row) -> bool:
                room_persona_ids = loads(row["persona_ids_json"])
                if not isinstance(room_persona_ids, builtins.list):
                    return False
                return bool({str(value) for value in room_persona_ids} & persona_ids)

            return [row for row in rows if room_contains_persona(row)]
        if table == "change_event_supports":
            placeholders = ",".join("?" for _ in persona_ids)
            return self.database.conn.execute(
                f"""
                SELECT s.*
                FROM change_event_supports s
                JOIN change_events e ON e.id = s.event_id
                WHERE e.persona_id IN ({placeholders})
                ORDER BY s.event_id, s.turn_id
                """,
                tuple(sorted(persona_ids)),
            ).fetchall()
        placeholders = ",".join("?" for _ in persona_ids)
        return self.database.conn.execute(
            f"SELECT * FROM {table} WHERE persona_id IN ({placeholders}) ORDER BY rowid",
            tuple(sorted(persona_ids)),
        ).fetchall()

    def _export_persona_ids(
        self, persona_id: str, room_export_mode: str, mode: str
    ) -> set[str]:
        if mode != "full" or room_export_mode != "bundle":
            return {persona_id}
        persona_ids = {persona_id}
        rows = self.database.conn.execute("SELECT persona_ids_json FROM rooms").fetchall()
        for row in rows:
            values = loads(row["persona_ids_json"])
            if not isinstance(values, builtins.list):
                continue
            room_personas = {str(value) for value in values}
            if persona_id in room_personas:
                persona_ids.update(room_personas)
        return persona_ids

    def _stage_bundle_personas(
        self, primary_persona_id: str, persona_ids: set[str], staging: Path
    ) -> None:
        rows = self.database.conn.execute(
            f"SELECT * FROM personas WHERE id IN ({','.join('?' for _ in persona_ids)})",
            tuple(sorted(persona_ids)),
        ).fetchall()
        lines = []
        for row in rows:
            if str(row["id"]) == primary_persona_id:
                continue
            lines.append(dumps({"schema_version": "1.1", "data": dict(row)}))
        if lines:
            path = staging / BUNDLE_PERSONAS_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(lines), encoding="utf-8")

    def _remove_persona_from_rooms(self, persona_id: str) -> None:
        rows = self.database.conn.execute("SELECT * FROM rooms ORDER BY rowid").fetchall()
        for row in rows:
            state = dict(loads(row["state_json"]))
            persona_ids = [str(value) for value in list(loads(row["persona_ids_json"]))]
            if persona_id not in persona_ids:
                continue
            remaining = [value for value in persona_ids if value != persona_id]
            room_sessions = dict(state.get("room_sessions", {}))
            deleted_session_id = room_sessions.pop(persona_id, None)
            state["persona_ids"] = remaining
            state["room_sessions"] = room_sessions
            for turn in state.get("transcript", []):
                if isinstance(turn, dict) and str(turn.get("persona_id")) == persona_id:
                    turn["participant_deleted"] = True
            if state.get("previous_speaker_id") == persona_id:
                state["previous_speaker_deleted"] = True
            if not remaining:
                state["status"] = "closed"
                state["turn_index"] = 0
            else:
                state["status"] = (
                    "active" if state.get("status") == "active" else state.get("status")
                )
                state["turn_index"] = int(state.get("turn_index", 0)) % len(remaining)
            if deleted_session_id:
                self.database.conn.execute(
                    "DELETE FROM session_turns WHERE session_id = ?", (deleted_session_id,)
                )
                self.database.conn.execute(
                    "DELETE FROM sessions WHERE id = ?", (deleted_session_id,)
                )
            self.database.conn.execute(
                """
                UPDATE rooms
                SET status = ?, persona_ids_json = ?, state_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    state["status"],
                    dumps(remaining),
                    dumps(state),
                    datetime.now(UTC).isoformat(),
                    row["id"],
                ),
            )

    def _delete_persona_rows(self, persona_id: str) -> None:
        for sql in [
            "DELETE FROM memories_fts WHERE persona_id = ?",
            "DELETE FROM affect_states WHERE persona_id = ?",
            "DELETE FROM needs WHERE persona_id = ?",
            "DELETE FROM relationships WHERE persona_id = ?",
            "DELETE FROM change_events WHERE persona_id = ?",
            "DELETE FROM lineage WHERE persona_id = ?",
            "DELETE FROM compiled_components WHERE persona_id = ?",
            "DELETE FROM compile_snapshots WHERE persona_id = ?",
            "DELETE FROM research_artifacts WHERE persona_id = ?",
            "DELETE FROM claims WHERE persona_id = ?",
            "DELETE FROM memories WHERE persona_id = ?",
            "DELETE FROM sources WHERE persona_id = ?",
            "DELETE FROM session_turns WHERE persona_id = ?",
            "DELETE FROM sessions WHERE persona_id = ?",
            "DELETE FROM continuation_branches WHERE persona_id = ?",
            "DELETE FROM continuations WHERE persona_id = ?",
            "DELETE FROM evaluation_results WHERE persona_id = ?",
            "DELETE FROM evaluation_cases WHERE persona_id = ?",
            "DELETE FROM evaluation_suites WHERE persona_id = ?",
            "DELETE FROM compilation_tasks WHERE persona_id = ?",
            "DELETE FROM personas WHERE id = ?",
        ]:
            self.database.conn.execute(sql, (persona_id,))

    def _stage_redacted_files(self, persona: PersonaRecord, files_root: Path) -> None:
        safe_files: dict[str, Any] = {
            "manifest.yaml": yaml.safe_dump(
                json.loads(dumps(persona.manifest.model_dump())),
                sort_keys=False,
                allow_unicode=True,
            ),
            "identity/profile.json": {
                "display_name": persona.manifest.display_name,
                "aliases": persona.manifest.aliases,
                "redacted": True,
            },
            "evidence/sources.jsonl": "\n".join(
                dumps(
                    {
                        **source.model_dump(mode="json"),
                        "content": "[redacted]",
                        "path": "[redacted]",
                        "hash": "[redacted]",
                        "metadata": {"redacted": True},
                    }
                )
                for source in self.get_sources(persona.id)
            ),
            "evidence/claims.jsonl": "",
            "runtime/redaction_notice.json": {
                "redacted": True,
                "reason": (
                    "Original source content, local paths, artifacts, and compiled "
                    "private components are excluded."
                ),
            },
        }
        for relative, value in safe_files.items():
            path = files_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(value, str):
                path.write_text(value, encoding="utf-8")
            else:
                path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def _package_checksums(self, staging: Path) -> dict[str, str]:
        checksums: dict[str, str] = {}
        for path in sorted(staging.rglob("*")):
            if not path.is_file() or path.name == "checksums.json":
                continue
            checksums[str(path.relative_to(staging))] = self._sha256_path(path)
        return checksums

    def _collect_lineage_descendants(
        self, persona_id: str, parent_type: str, parent_id: str
    ) -> set[tuple[str, str]]:
        descendants: set[tuple[str, str]] = set()
        frontier = [(parent_type, parent_id)]
        while frontier:
            current_type, current_id = frontier.pop()
            rows = self.database.conn.execute(
                """
                SELECT child_type, child_id
                FROM lineage
                WHERE persona_id = ? AND parent_type = ? AND parent_id = ?
                """,
                (persona_id, current_type, current_id),
            ).fetchall()
            for row in rows:
                item = (str(row["child_type"]), str(row["child_id"]))
                if item in descendants:
                    continue
                descendants.add(item)
                frontier.append(item)
        return descendants

    def _reset_derived_persona_files(self, persona_id: str) -> None:
        persona = self.get(persona_id)
        root = Path(persona.package_path)
        resets: dict[str, Any] = {
            "identity/profile.json": {
                "display_name": persona.manifest.display_name,
                "aliases": persona.manifest.aliases,
                "compile_state": "needs_recompile",
            },
            "identity/timeline.jsonl": "",
            "identity/self_narrative.md": "",
            "cognition/mental_models.json": [],
            "cognition/decision_heuristics.json": [],
            "cognition/values.json": [],
            "cognition/contradictions.json": [],
            "cognition/failure_patterns.json": [],
            "affect/temperament.json": {},
            "affect/emotional_triggers.json": [],
            "affect/attachment.json": {},
            "affect/needs.json": [],
            "affect/defenses.json": [],
            "expression/style.json": {},
            "expression/vocabulary.json": [],
            "expression/dialogue_examples.jsonl": "",
            "expression/anti_patterns.json": [],
            "relationships/relationships.json": [],
            "continuation/branch_provenance.json": {},
            "runtime/compile_report.json": {"compile_state": "needs_recompile"},
        }
        for relative, value in resets.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(value, str):
                path.write_text(value, encoding="utf-8")
            else:
                path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def _verify_package_archive(self, archive: zipfile.ZipFile) -> None:
        listed_names = [name for name in archive.namelist() if not name.endswith("/")]
        if len(listed_names) != len(set(listed_names)):
            raise SecurityError("checksum_duplicate_zip_member")
        names = set(listed_names)
        required = {"manifest.yaml", "package_schema.json", "checksums.json"}
        missing = required - names
        if missing:
            raise SecurityError(f"package_missing:{','.join(sorted(missing))}")
        schema = json.loads(archive.read("package_schema.json"))
        if schema.get("schema_version") != "1.1":
            raise SecurityError("unsupported_package_schema")
        checksums = json.loads(archive.read("checksums.json"))
        if checksums.get("algorithm") != "sha256":
            raise SecurityError("unsupported_checksum_algorithm")
        checksum_files = dict(checksums.get("files", {}))
        expected = names - {"checksums.json"}
        if set(checksum_files) != expected:
            missing_checksums = expected - set(checksum_files)
            extra_checksums = set(checksum_files) - expected
            detail = ",".join(sorted(missing_checksums or extra_checksums))
            raise SecurityError(f"checksum_manifest_mismatch:{detail}")
        for relative, expected in checksum_files.items():
            if relative not in names:
                raise SecurityError(f"package_missing:{relative}")
            actual = hashlib.sha256(archive.read(relative)).hexdigest()
            if actual != expected:
                raise SecurityError(f"checksum_mismatch:{relative}")

    def _build_import_id_maps(
        self,
        archive: zipfile.ZipFile,
        original_persona_id: str,
        target_persona_id: str,
    ) -> dict[str, dict[str, str]]:
        maps: dict[str, dict[str, str]] = {"personas": {original_persona_id: target_persona_id}}
        for row in self._read_bundle_persona_rows(archive):
            old = str(row.get("id"))
            if not old or old == original_persona_id:
                continue
            maps["personas"][old] = (
                old if not self._id_exists("personas", old) else self._unique_id(old)
            )
        for table in DATA_TABLES:
            relative = DATA_FILES[table]
            if relative not in archive.namelist():
                continue
            rows = self._read_data_rows(archive, table)
            if table in {"affect_states", "needs", "relationships", "change_event_supports"}:
                continue
            maps[table] = {}
            for row in rows:
                old_id = row.get("id")
                if not old_id:
                    continue
                old = str(old_id)
                prefix = old.split("_", 1)[0] if "_" in old else table[:4]
                maps[table][old] = old if not self._id_exists(table, old) else new_id(prefix)
        return maps

    def _extract_package_files(self, archive: zipfile.ZipFile, package_root: Path) -> None:
        for member in archive.infolist():
            if member.is_dir() or not member.filename.startswith("files/"):
                continue
            relative = Path(member.filename).relative_to("files")
            target = ensure_child_path(package_root, package_root / relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))

    def _import_data_files(
        self,
        archive: zipfile.ZipFile,
        original_persona_id: str,
        target_persona_id: str,
        id_maps: dict[str, dict[str, str]],
    ) -> None:
        for table in DATA_TABLES:
            if DATA_FILES[table] not in archive.namelist():
                continue
            for row in self._read_data_rows(archive, table):
                remapped = self._remap_record(
                    table, row, original_persona_id, target_persona_id, id_maps
                )
                self._insert_row(table, remapped)

    def _read_bundle_persona_rows(
        self, archive: zipfile.ZipFile
    ) -> builtins.list[dict[str, Any]]:
        if BUNDLE_PERSONAS_FILE not in archive.namelist():
            return []
        rows = []
        raw = archive.read(BUNDLE_PERSONAS_FILE).decode("utf-8")
        for line in raw.splitlines():
            if not line.strip():
                continue
            record = loads(line)
            if record.get("schema_version") != "1.1":
                raise SecurityError("unsupported_data_schema:personas")
            rows.append(dict(record["data"]))
        return rows

    def _import_bundle_personas(
        self,
        archive: zipfile.ZipFile,
        original_persona_id: str,
        id_maps: dict[str, dict[str, str]],
    ) -> None:
        now = datetime.now(UTC).isoformat()
        for row in self._read_bundle_persona_rows(archive):
            old_id = str(row["id"])
            if old_id == original_persona_id:
                continue
            new_persona_id = id_maps.get("personas", {}).get(old_id, old_id)
            manifest_data = self._remap_json(
                loads(str(row["manifest_json"])),
                original_persona_id,
                id_maps["personas"][original_persona_id],
                id_maps,
            )
            if isinstance(manifest_data, dict):
                manifest_data["id"] = new_persona_id
            manifest = PersonaManifest.model_validate(manifest_data)
            package_path = ensure_child_path(
                self.config.personas_dir, self.config.personas_dir / new_persona_id
            )
            package_path.mkdir(parents=True, exist_ok=True)
            self._write_package_skeleton(manifest)
            self.database.conn.execute(
                "INSERT INTO personas VALUES (?, ?, ?, ?, ?, ?)",
                (
                    new_persona_id,
                    dumps(manifest.model_dump()),
                    str(package_path),
                    int(row.get("archived", 0)),
                    str(row.get("created_at") or now),
                    str(row.get("updated_at") or now),
                ),
            )

    def _read_data_rows(
        self, archive: zipfile.ZipFile, table: str
    ) -> builtins.list[dict[str, Any]]:
        raw = archive.read(DATA_FILES[table]).decode("utf-8")
        rows = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            record = loads(line)
            if record.get("schema_version") != "1.1":
                raise SecurityError(f"unsupported_data_schema:{table}")
            rows.append(dict(record["data"]))
        return rows

    def _remap_record(
        self,
        table: str,
        row: dict[str, Any],
        original_persona_id: str,
        target_persona_id: str,
        id_maps: dict[str, dict[str, str]],
    ) -> dict[str, Any]:
        remapped = dict(row)
        if "persona_id" in remapped:
            remapped["persona_id"] = id_maps.get("personas", {}).get(
                str(remapped["persona_id"]), target_persona_id
            )
        if "id" in remapped and table in id_maps:
            remapped["id"] = id_maps[table].get(str(remapped["id"]), remapped["id"])
        for key, mapped_table in {
            "event_id": "change_events",
            "source_id": "sources",
            "session_id": "sessions",
            "turn_id": "session_turns",
            "task_id": "compilation_tasks",
            "continuation_id": "continuations",
            "parent_branch_id": "continuation_branches",
            "supersedes_id": "memories",
            "suite_id": "evaluation_suites",
            "case_id": "evaluation_cases",
        }.items():
            if remapped.get(key) is not None:
                remapped[key] = id_maps.get(mapped_table, {}).get(str(remapped[key]), remapped[key])
        if table == "continuation_branches" and remapped.get("branch_json"):
            remapped["branch_json"] = dumps(
                self._remap_json(
                    loads(remapped["branch_json"]), original_persona_id, target_persona_id, id_maps
                )
            )
        for json_key in [
            "metadata_json",
            "plan_json",
            "artifacts_json",
            "task_json",
            "state_json",
            "branch_json",
            "persona_ids_json",
            "used_memory_ids_json",
            "context_json",
            "artifact_json",
            "source_artifact_ids_json",
            "files_manifest_json",
            "data_json",
        ]:
            if json_key in remapped and remapped[json_key] is not None:
                with contextlib.suppress(json.JSONDecodeError):
                    remapped[json_key] = dumps(
                        self._remap_json(
                            loads(str(remapped[json_key])),
                            original_persona_id,
                            target_persona_id,
                            id_maps,
                        )
                    )
        if table == "lineage":
            remapped["child_id"] = self._remap_object_id(
                str(remapped["child_type"]), str(remapped["child_id"]), id_maps
            )
            remapped["parent_id"] = self._remap_object_id(
                str(remapped["parent_type"]), str(remapped["parent_id"]), id_maps
            )
        return remapped

    def _remap_json(
        self,
        value: Any,
        original_persona_id: str,
        target_persona_id: str,
        id_maps: dict[str, dict[str, str]],
    ) -> Any:
        if isinstance(value, dict):
            return {
                self._remap_scalar_id(
                    key, original_persona_id, target_persona_id, id_maps
                ): self._remap_json(item, original_persona_id, target_persona_id, id_maps)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                self._remap_json(item, original_persona_id, target_persona_id, id_maps)
                for item in value
            ]
        if isinstance(value, str):
            return self._remap_scalar_id(
                value, original_persona_id, target_persona_id, id_maps
            )
        return value

    def _remap_scalar_id(
        self,
        value: str,
        original_persona_id: str,
        target_persona_id: str,
        id_maps: dict[str, dict[str, str]],
    ) -> str:
        if value == original_persona_id:
            return target_persona_id
        for mapping in id_maps.values():
            if value in mapping:
                return mapping[value]
        return value

    def _remap_object_id(
        self, object_type: str, value: str, id_maps: dict[str, dict[str, str]]
    ) -> str:
        type_to_table = {
            "source": "sources",
            "claim": "claims",
            "memory": "memories",
            "session": "sessions",
            "turn": "session_turns",
            "session_turn": "session_turns",
            "compilation_task": "compilation_tasks",
            "continuation": "continuations",
            "continuation_branch": "continuation_branches",
            "research_artifact": "research_artifacts",
            "compiled_component": "compiled_components",
            "change_event": "change_events",
            "room": "rooms",
            "evaluation_suite": "evaluation_suites",
            "evaluation_case": "evaluation_cases",
            "evaluation_result": "evaluation_results",
        }
        table = type_to_table.get(object_type)
        return id_maps.get(table or "", {}).get(value, value)

    def _rewrite_package_file_ids(
        self,
        package_root: Path,
        original_persona_id: str,
        target_persona_id: str,
        id_maps: dict[str, dict[str, str]],
    ) -> None:
        replacements: dict[str, str] = {original_persona_id: target_persona_id}
        for mapping in id_maps.values():
            replacements.update(mapping)
        for path in package_root.rglob("*"):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            updated = text
            for old, new in replacements.items():
                if old != new:
                    updated = updated.replace(old, new)
            if updated != text:
                path.write_text(updated, encoding="utf-8")

    def _insert_row(self, table: str, row: dict[str, Any]) -> None:
        allowed_tables = set(DATA_TABLES)
        if table not in allowed_tables:
            raise SecurityError(f"unsupported_table:{table}")
        if table in {"affect_states", "needs", "relationships", "change_events"}:
            row.setdefault("branch_id", "main")
        columns = [
            str(info["name"])
            for info in self.database.conn.execute(f"PRAGMA table_info({table})").fetchall()
        ]
        values = [row.get(column) for column in columns]
        placeholders = ",".join("?" for _ in columns)
        self.database.conn.execute(
            f"INSERT OR REPLACE INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
            values,
        )

    def _id_exists(self, table: str, value: str) -> bool:
        if table in {"affect_states", "needs", "relationships"}:
            return False
        row = self.database.conn.execute(
            f"SELECT 1 FROM {table} WHERE id = ? LIMIT 1", (value,)
        ).fetchone()
        return row is not None

    def _rebuild_fts(self, persona_id: str) -> None:
        self.database.conn.execute("DELETE FROM memories_fts WHERE persona_id = ?", (persona_id,))
        rows = self.database.conn.execute(
            """
            SELECT id, content FROM memories
            WHERE persona_id = ? AND validity = 'valid'
            """,
            (persona_id,),
        ).fetchall()
        for row in rows:
            self.database.conn.execute(
                "INSERT INTO memories_fts(memory_id, persona_id, content) VALUES (?, ?, ?)",
                (row["id"], persona_id, row["content"]),
            )

    def _assert_import_consistency(self, persona_id: str) -> None:
        for row in self.database.conn.execute("PRAGMA foreign_key_check").fetchall():
            raise SecurityError(f"foreign_key_check_failed:{dict(row)}")
        self.get(persona_id)

    def _write_database_evidence_files(self, persona_id: str) -> None:
        root = Path(self.get(persona_id).package_path)
        (root / "evidence").mkdir(parents=True, exist_ok=True)
        sources = self.get_sources(persona_id)
        (root / "evidence" / "sources.jsonl").write_text(
            "\n".join(source.model_dump_json() for source in sources), encoding="utf-8"
        )
        rows = self.database.conn.execute(
            """
            SELECT * FROM claims
            WHERE persona_id = ? AND confidence > 0
            ORDER BY created_at
            """,
            (persona_id,),
        ).fetchall()
        (root / "evidence" / "claims.jsonl").write_text(
            "\n".join(dumps(dict(row)) for row in rows), encoding="utf-8"
        )

    def _sha256_path(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _write_package_skeleton(self, manifest: PersonaManifest) -> None:
        root = self.config.personas_dir / manifest.id
        folders = [
            "identity",
            "cognition",
            "affect",
            "expression",
            "relationships",
            "evidence",
            "continuation",
            "evaluation",
            "runtime",
        ]
        for folder in folders:
            (root / folder).mkdir(parents=True, exist_ok=True)
        (root / "manifest.yaml").write_text(
            yaml.safe_dump(
                json.loads(dumps(manifest.model_dump())), sort_keys=False, allow_unicode=True
            ),
            encoding="utf-8",
        )
        defaults: dict[str, Any] = {
            "identity/profile.json": {
                "display_name": manifest.display_name,
                "aliases": manifest.aliases,
            },
            "identity/timeline.jsonl": "",
            "identity/self_narrative.md": "",
            "identity/boundaries.json": {"fact_boundaries": "Do not promote inference to fact."},
            "cognition/mental_models.json": [],
            "cognition/decision_heuristics.json": [],
            "cognition/values.json": [],
            "cognition/contradictions.json": [],
            "cognition/failure_patterns.json": [],
            "affect/temperament.json": {},
            "affect/emotional_triggers.json": [],
            "affect/attachment.json": {},
            "affect/needs.json": [],
            "affect/defenses.json": [],
            "expression/style.json": {},
            "expression/vocabulary.json": [],
            "expression/dialogue_examples.jsonl": "",
            "expression/anti_patterns.json": [],
            "evidence/sources.jsonl": "",
            "evidence/claims.jsonl": "",
            "evidence/conflicts.jsonl": "",
        }
        for relative, value in defaults.items():
            path = root / relative
            if path.exists():
                continue
            if isinstance(value, str):
                path.write_text(value, encoding="utf-8")
            else:
                path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def _row_to_persona(self, row: sqlite3.Row) -> PersonaRecord:
        manifest = PersonaManifest.model_validate(loads(row["manifest_json"]))
        return PersonaRecord(
            id=str(row["id"]),
            display_name=manifest.display_name,
            manifest=manifest,
            package_path=str(row["package_path"]),
        )

    def _unique_id(self, base: str) -> str:
        candidate = safe_slug(base)
        suffix = 2
        while self.database.conn.execute(
            "SELECT 1 FROM personas WHERE id = ?", (candidate,)
        ).fetchone():
            candidate = f"{safe_slug(base)}-{suffix}"
            suffix += 1
        return candidate

    def _hash_text(self, content: str) -> str:
        import hashlib

        return hashlib.sha256(content.encode("utf-8")).hexdigest()
