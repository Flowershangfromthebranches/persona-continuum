"""Streaming private-material uploads with staging, binding, and orphan cleanup."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from persona_continuum.application._utils import new_id
from persona_continuum.config import Config
from persona_continuum.ingestion.errors import MaterialTooLargeError
from persona_continuum.ingestion.streaming import infer_source_type
from persona_continuum.security.paths import ensure_child_path
from persona_continuum.security.validation import NotFoundError, SecurityError
from persona_continuum.storage.database import Database

_UNSAFE_FILENAME = re.compile(r"[^\w.\- \u3400-\u9fff]+", flags=re.UNICODE)


class MaterialUploadService:
    """Stage large private files under data_dir without trusting client paths."""

    def __init__(self, config: Config, database: Database) -> None:
        self.config = config
        self.database = database

    @property
    def staging_root(self) -> Path:
        return ensure_child_path(self.config.data_dir, self.config.private_material_uploads_dir)

    def sanitize_filename(self, filename: str) -> str:
        raw = str(filename or "").replace("\x00", "").strip()
        if not raw:
            raise SecurityError("invalid_upload_filename")
        candidate = Path(raw)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise SecurityError("path_traversal")
        name = candidate.name
        if not name or name in {".", ".."}:
            raise SecurityError("invalid_upload_filename")
        safe = _UNSAFE_FILENAME.sub("_", name).strip("._ ")
        if not safe:
            raise SecurityError("invalid_upload_filename")
        return safe[:200]

    def get(self, upload_id: str) -> dict[str, Any]:
        row = self.database.conn.execute(
            "SELECT * FROM persona_material_uploads WHERE id = ?", (upload_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"material_upload_not_found:{upload_id}")
        return dict(row)

    def public_record(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "upload_id": row["id"],
            "filename": row["filename"],
            "size": int(row["size"]),
            "sha256": row["sha256"],
            "source_type": row["source_type"],
            "status": row["status"],
        }

    async def astream_chunks(
        self,
        *,
        filename: str,
        chunks: AsyncIterator[bytes],
        content_length: int | None = None,
    ) -> dict[str, Any]:
        limit = int(self.config.max_private_material_bytes)
        if content_length is not None and content_length > limit:
            raise MaterialTooLargeError(content_length, limit)
        return await self._awrite(filename=filename, chunks=chunks, limit=limit)

    def stream_chunks(
        self,
        *,
        filename: str,
        chunks: Iterator[bytes],
        content_length: int | None = None,
    ) -> dict[str, Any]:
        limit = int(self.config.max_private_material_bytes)
        if content_length is not None and content_length > limit:
            raise MaterialTooLargeError(content_length, limit)
        return self._write(filename=filename, chunks=chunks, limit=limit)

    async def _awrite(
        self, *, filename: str, chunks: AsyncIterator[bytes], limit: int
    ) -> dict[str, Any]:
        stored = self.sanitize_filename(filename)
        source_type = infer_source_type(stored)
        upload_id = new_id("upl")
        self.staging_root.mkdir(parents=True, exist_ok=True)
        part_path = ensure_child_path(self.staging_root, self.staging_root / f"{upload_id}.part")
        digest = hashlib.sha256()
        size = 0
        try:
            with part_path.open("wb") as handle:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > limit:
                        raise MaterialTooLargeError(size, limit)
                    digest.update(chunk)
                    handle.write(chunk)
            return self._finalize(
                upload_id=upload_id,
                filename=stored,
                source_type=source_type,
                part_path=part_path,
                size=size,
                sha256=digest.hexdigest(),
            )
        except Exception:
            self._remove_if_exists(part_path)
            raise

    def _write(self, *, filename: str, chunks: Iterator[bytes], limit: int) -> dict[str, Any]:
        stored = self.sanitize_filename(filename)
        source_type = infer_source_type(stored)
        upload_id = new_id("upl")
        self.staging_root.mkdir(parents=True, exist_ok=True)
        part_path = ensure_child_path(self.staging_root, self.staging_root / f"{upload_id}.part")
        digest = hashlib.sha256()
        size = 0
        try:
            with part_path.open("wb") as handle:
                for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > limit:
                        raise MaterialTooLargeError(size, limit)
                    digest.update(chunk)
                    handle.write(chunk)
            return self._finalize(
                upload_id=upload_id,
                filename=stored,
                source_type=source_type,
                part_path=part_path,
                size=size,
                sha256=digest.hexdigest(),
            )
        except Exception:
            self._remove_if_exists(part_path)
            raise

    def _finalize(
        self,
        *,
        upload_id: str,
        filename: str,
        source_type: str,
        part_path: Path,
        size: int,
        sha256: str,
    ) -> dict[str, Any]:
        upload_dir = ensure_child_path(self.staging_root, self.staging_root / upload_id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        final_path = ensure_child_path(upload_dir, upload_dir / filename)
        os.replace(part_path, final_path)
        now = datetime.now(UTC).isoformat()
        row = {
            "id": upload_id,
            "filename": filename,
            "stored_filename": filename,
            "path": str(final_path),
            "size": size,
            "sha256": sha256,
            "source_type": source_type,
            "status": "staged",
            "job_id": None,
            "persona_id": None,
            "created_at": now,
            "bound_at": None,
            "consumed_at": None,
        }
        self.database.conn.execute(
            "INSERT INTO persona_material_uploads "
            "(id, filename, stored_filename, path, size, sha256, source_type, status, "
            "job_id, persona_id, created_at, bound_at, consumed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"],
                row["filename"],
                row["stored_filename"],
                row["path"],
                row["size"],
                row["sha256"],
                row["source_type"],
                row["status"],
                row["job_id"],
                row["persona_id"],
                row["created_at"],
                row["bound_at"],
                row["consumed_at"],
            ),
        )
        self.database.conn.commit()
        return row

    def resolve_staged_path(self, upload_id: str) -> Path:
        row = self.get(upload_id)
        path = Path(str(row["path"]))
        return ensure_child_path(self.staging_root, path)

    def bind(self, upload_id: str, *, job_id: str, persona_id: str) -> dict[str, Any]:
        row = self.get(upload_id)
        if row["status"] not in {"staged", "bound"}:
            raise SecurityError(f"material_upload_not_bindable:{upload_id}")
        path = self.resolve_staged_path(upload_id)
        if not path.is_file():
            raise NotFoundError(f"material_upload_missing:{upload_id}")
        now = datetime.now(UTC).isoformat()
        self.database.conn.execute(
            "UPDATE persona_material_uploads SET status = ?, job_id = ?, persona_id = ?, "
            "bound_at = ? WHERE id = ?",
            ("bound", job_id, persona_id, now, upload_id),
        )
        self.database.conn.commit()
        row["status"] = "bound"
        row["job_id"] = job_id
        row["persona_id"] = persona_id
        row["bound_at"] = now
        return row

    def mark_consumed(self, upload_id: str, *, stored_path: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.database.conn.execute(
            "UPDATE persona_material_uploads SET status = ?, path = ?, "
            "consumed_at = ? WHERE id = ?",
            ("consumed", stored_path, now, upload_id),
        )
        self.database.conn.commit()

    def delete_staging_tree(self, upload_id: str) -> None:
        part_path = ensure_child_path(self.staging_root, self.staging_root / f"{upload_id}.part")
        upload_dir = ensure_child_path(self.staging_root, self.staging_root / upload_id)
        self._remove_if_exists(part_path)
        if upload_dir.exists():
            shutil.rmtree(upload_dir, ignore_errors=True)

    def cleanup_job_staging(self, job_id: str) -> int:
        rows = self.database.conn.execute(
            "SELECT id, status FROM persona_material_uploads WHERE job_id = ?",
            (job_id,),
        ).fetchall()
        removed = 0
        for row in rows:
            if str(row["status"]) == "consumed":
                continue
            self.delete_staging_tree(str(row["id"]))
            self.database.conn.execute(
                "DELETE FROM persona_material_uploads WHERE id = ?", (row["id"],)
            )
            removed += 1
        if removed:
            self.database.conn.commit()
        return removed

    def cleanup_orphans(self, *, max_age_seconds: int | None = None) -> int:
        max_age = (
            int(self.config.private_material_upload_orphan_seconds)
            if max_age_seconds is None
            else int(max_age_seconds)
        )
        cutoff = (datetime.now(UTC) - timedelta(seconds=max(0, max_age))).isoformat()
        rows = self.database.conn.execute(
            "SELECT id, status, created_at FROM persona_material_uploads "
            "WHERE status IN ('staged', 'bound') AND created_at < ?",
            (cutoff,),
        ).fetchall()
        removed = 0
        for row in rows:
            self.delete_staging_tree(str(row["id"]))
            self.database.conn.execute(
                "DELETE FROM persona_material_uploads WHERE id = ?", (row["id"],)
            )
            removed += 1
        # Incomplete .part files are never a successful artifact.
        for part in self.staging_root.glob("*.part"):
            ensure_child_path(self.staging_root, part)
            self._remove_if_exists(part)
            removed += 1
        if removed:
            self.database.conn.commit()
        return removed

    @staticmethod
    def _remove_if_exists(path: Path) -> None:
        if path.exists():
            path.unlink()
