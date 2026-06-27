from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from math import exp
from typing import Any

from persona_continuum.application._utils import dt, dumps, loads, new_id, parse_dt
from persona_continuum.domain.memory import MemoryRecord, MemoryType
from persona_continuum.storage.database import Database


class MemoryService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add_memory(
        self,
        persona_id: str,
        *,
        content: str,
        memory_type: MemoryType | str,
        importance: float = 0.5,
        source_kind: str,
        source_id: str | None = None,
        source_confidence: float = 0.5,
        participants: list[str] | None = None,
        emotions: dict[str, float] | None = None,
        occurred_at: datetime | None = None,
        branch_id: str = "main",
        unresolved: bool = False,
        user_corrected: bool = False,
        forgettable: bool = True,
        supersedes_id: str | None = None,
        metadata: dict[str, object] | None = None,
        commit: bool = True,
    ) -> MemoryRecord:
        record = MemoryRecord(
            id=new_id("mem"),
            persona_id=persona_id,
            content=content,
            type=MemoryType(memory_type),
            occurred_at=occurred_at,
            participants=participants or [],
            emotions=emotions or {},
            source_id=source_id,
            source_kind=source_kind,
            source_confidence=source_confidence,
            importance=importance,
            branch_id=branch_id,
            unresolved=unresolved,
            user_corrected=user_corrected,
            forgettable=forgettable,
            supersedes_id=supersedes_id,
            metadata=metadata or {},
        )
        self._insert(record, commit=commit)
        return record

    def get_memory(self, memory_id: str) -> MemoryRecord | None:
        row = self.database.conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        return self._row_to_memory(row) if row else None

    def search_memories(
        self,
        persona_id: str,
        query: str,
        limit: int = 8,
        *,
        branch_id: str | None = None,
        include_main_history: bool = True,
        include_shared_pre_divergence: bool = True,
        visibility: set[str] | None = None,
        exclude_visibility: set[str] | None = None,
    ) -> list[MemoryRecord]:
        query = query.strip()
        rows = self._candidate_rows(persona_id, query, max(limit * 8, 50))
        rows = [
            row
            for row in rows
            if self._row_allowed(
                row,
                branch_id=branch_id,
                include_main_history=include_main_history,
                include_shared_pre_divergence=include_shared_pre_divergence,
                visibility=visibility,
                exclude_visibility=exclude_visibility,
            )
        ]
        records = self._score_and_select(rows, query, limit)
        now = datetime.now(UTC).isoformat()
        for record in records:
            self.database.conn.execute(
                """
                UPDATE memories
                SET access_count = access_count + 1, last_accessed_at = ?
                WHERE id = ?
                """,
                (now, record.id),
            )
            record.access_count += 1
            record.last_accessed_at = datetime.fromisoformat(now)
        self.database.conn.commit()
        return records

    def _row_allowed(
        self,
        row: sqlite3.Row,
        *,
        branch_id: str | None,
        include_main_history: bool,
        include_shared_pre_divergence: bool,
        visibility: set[str] | None,
        exclude_visibility: set[str] | None,
    ) -> bool:
        metadata = dict(loads(row["metadata_json"]))
        row_visibility = str(metadata.get("visibility", "persona_private"))
        if visibility is not None and row_visibility not in visibility:
            return False
        if exclude_visibility is not None and row_visibility in exclude_visibility:
            return False
        allowed = {branch_id}
        if branch_id is None:
            allowed = {"main"}
        if include_main_history:
            allowed.add("main")
        if include_shared_pre_divergence:
            allowed.add("shared_pre_divergence")
        row_branch = str(row["branch_id"])
        if row_branch in allowed:
            return True
        ancestor_boundaries = self._ancestor_branch_boundaries(branch_id)
        if row_branch in ancestor_boundaries:
            return self._memory_within_ancestor_boundary(row, ancestor_boundaries[row_branch])
        return bool(
            metadata.get("branch_scope") == "pre_divergence" and include_shared_pre_divergence
        )

    def _ancestor_branch_boundaries(self, branch_id: str | None) -> dict[str, str]:
        if branch_id is None or branch_id in {"main", "shared_pre_divergence"}:
            return {}
        boundaries: dict[str, str] = {}
        current_id = branch_id
        while current_id:
            row = self.database.conn.execute(
                "SELECT branch_json FROM continuation_branches WHERE id = ?", (current_id,)
            ).fetchone()
            if row is None:
                break
            branch = dict(loads(row["branch_json"]))
            parent_id = branch.get("parent_branch_id")
            parent_state = dict(branch.get("persona_state", {}))
            divergence_at = parent_state.get("divergence_at")
            if parent_id and divergence_at:
                boundaries[str(parent_id)] = str(divergence_at)
            current_id = str(parent_id) if parent_id else ""
        return boundaries

    def _memory_within_ancestor_boundary(self, row: sqlite3.Row, boundary: str) -> bool:
        metadata = dict(loads(row["metadata_json"]))
        step_value = metadata.get("simulation_step_date") or metadata.get("created_from_step_id")
        if step_value is None:
            return False
        step_dt = self._parse_boundary_dt(step_value)
        boundary_dt = self._parse_boundary_dt(boundary)
        if step_dt is None or boundary_dt is None:
            return str(step_value) <= str(boundary)
        return step_dt <= boundary_dt

    def _parse_boundary_dt(self, value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value
        return parse_dt(str(value))

    def correct_memory(
        self, persona_id: str, memory_id: str, new_content: str, reason: str
    ) -> MemoryRecord:
        original = self.get_memory(memory_id)
        if original is None:
            raise KeyError(memory_id)
        self.database.conn.execute(
            "UPDATE memories SET validity = 'superseded', metadata_json = ? WHERE id = ?",
            (dumps({"superseded_reason": reason}), memory_id),
        )
        self.database.conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
        corrected = self.add_memory(
            persona_id,
            content=new_content,
            memory_type=original.type,
            importance=original.importance,
            source_kind="user_correction",
            source_confidence=1.0,
            participants=original.participants,
            emotions=original.emotions,
            branch_id=original.branch_id,
            user_corrected=True,
            supersedes_id=original.id,
            metadata={"corrected_from": original.id, "correction_reason": reason},
        )
        self.database.conn.commit()
        return corrected

    def forget_memory(self, persona_id: str, memory_id: str, reason: str = "user_request") -> bool:
        self.database.conn.execute(
            """
            UPDATE memories
            SET validity = 'forgotten', metadata_json = ?
            WHERE persona_id = ? AND id = ?
            """,
            (dumps({"forgotten_reason": reason}), persona_id, memory_id),
        )
        self.database.conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
        self.database.conn.commit()
        return True

    def delete_memory(self, persona_id: str, memory_id: str, *, commit: bool = True) -> None:
        self.database.conn.execute(
            "DELETE FROM memories_fts WHERE persona_id = ? AND memory_id = ?",
            (persona_id, memory_id),
        )
        self.database.conn.execute(
            "DELETE FROM memories WHERE persona_id = ? AND id = ?", (persona_id, memory_id)
        )
        if commit:
            self.database.conn.commit()

    def rebuild_index(self, persona_id: str | None = None) -> None:
        if persona_id is None:
            self.database.conn.execute("DELETE FROM memories_fts")
            rows = self.database.conn.execute(
                "SELECT id, persona_id, content FROM memories WHERE validity = 'valid'"
            ).fetchall()
        else:
            self.database.conn.execute(
                "DELETE FROM memories_fts WHERE persona_id = ?", (persona_id,)
            )
            rows = self.database.conn.execute(
                """
                SELECT id, persona_id, content FROM memories
                WHERE persona_id = ? AND validity = 'valid'
                """,
                (persona_id,),
            ).fetchall()
        for row in rows:
            self.database.conn.execute(
                "INSERT INTO memories_fts(memory_id, persona_id, content) VALUES (?, ?, ?)",
                (row["id"], row["persona_id"], row["content"]),
            )
        self.database.conn.commit()

    def consolidate_memories(self, persona_id: str) -> dict[str, int]:
        rows = self.database.conn.execute(
            "SELECT id, content FROM memories WHERE persona_id = ? AND validity = 'valid'",
            (persona_id,),
        ).fetchall()
        seen: dict[str, str] = {}
        merged = 0
        for row in rows:
            normalized = " ".join(str(row["content"]).lower().split())
            if normalized in seen:
                self.forget_memory(persona_id, str(row["id"]), "duplicate_consolidation")
                merged += 1
            else:
                seen[normalized] = str(row["id"])
        return {"merged": merged}

    def _candidate_rows(self, persona_id: str, query: str, limit: int) -> list[sqlite3.Row]:
        if not query:
            return self.database.conn.execute(
                """
                SELECT *, NULL AS fts_score, 0 AS fts_matched
                FROM memories
                WHERE persona_id = ? AND validity = 'valid'
                ORDER BY importance DESC, written_at DESC
                LIMIT ?
                """,
                (persona_id, limit),
            ).fetchall()
        rows: list[sqlite3.Row] = []
        fts_query = self._fts_query(query)
        if fts_query:
            try:
                rows = self.database.conn.execute(
                    """
                    SELECT m.*,
                      bm25(memories_fts) AS fts_score,
                      1 AS fts_matched
                    FROM memories_fts
                    JOIN memories m ON m.id = memories_fts.memory_id
                    WHERE memories_fts MATCH ? AND memories_fts.persona_id = ?
                      AND m.validity = 'valid'
                    ORDER BY fts_score ASC, m.importance DESC, m.written_at DESC
                    LIMIT ?
                    """,
                    (fts_query, persona_id, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if len(rows) < limit:
            existing = {str(row["id"]) for row in rows}
            fallback = self.database.conn.execute(
                """
                SELECT *, NULL AS fts_score, 0 AS fts_matched
                FROM memories
                WHERE persona_id = ? AND validity = 'valid'
                ORDER BY importance DESC, written_at DESC
                LIMIT ?
                """,
                (persona_id, limit),
            ).fetchall()
            rows.extend(row for row in fallback if str(row["id"]) not in existing)
        return rows

    def _score_and_select(
        self, rows: list[sqlite3.Row], query: str, limit: int
    ) -> list[MemoryRecord]:
        features = self._query_features(query)
        query_has_cjk = self._has_cjk(query)
        scored: list[tuple[float, MemoryRecord]] = []
        for row in rows:
            row_data = dict(row)
            record = self._row_to_memory(row)
            text_score = self._text_score(features, record.content)
            fts_score_raw = row_data.get("fts_score")
            fts_component = (
                1.0 / (1.0 + abs(float(fts_score_raw))) if fts_score_raw is not None else 0.0
            )
            if query and text_score <= 0 and fts_component <= 0:
                continue
            recency = self._recency_score(record.written_at)
            score_breakdown = {
                "fts_relevance": round(fts_component, 4),
                "text_relevance": round(text_score, 4),
                "importance": round(record.importance, 4),
                "source_confidence": round(record.source_confidence, 4),
                "recency": round(recency, 4),
                "unresolved": 1.0 if record.unresolved else 0.0,
                "validity": 1.0 if record.validity == "valid" else 0.0,
            }
            score = (
                text_score * 0.38
                + fts_component * 0.18
                + record.importance * 0.18
                + record.source_confidence * 0.12
                + recency * 0.07
                + (0.05 if record.unresolved else 0.0)
                + (0.02 if record.validity == "valid" else 0.0)
            )
            if fts_component > 0:
                reason = "fts_match"
            elif text_score > 0:
                reason = "ngram_match" if query_has_cjk else "text_match"
            elif query:
                reason = "importance_fallback"
            else:
                reason = "empty_query_importance"
            record.metadata.update(
                {
                    "score": round(score, 6),
                    "score_breakdown": score_breakdown,
                    "retrieval_reason": reason,
                }
            )
            scored.append((score, record))
        scored.sort(
            key=lambda item: (item[0], item[1].importance, item[1].written_at), reverse=True
        )
        return [record for _, record in scored[:limit]]

    def _fts_query(self, query: str) -> str:
        tokens = [
            token.strip(".,!?;:，。！？；：()[]{}\"'").lower()
            for token in query.replace("\n", " ").split()
        ]
        tokens = [token for token in tokens if token]
        if tokens:
            return " OR ".join(tokens)
        if self._has_cjk(query) and len(query) >= 2:
            return query
        return ""

    def _query_features(self, query: str) -> set[str]:
        query = query.strip().lower()
        if not query:
            return set()
        features = {query}
        for token in query.split():
            cleaned = token.strip(".,!?;:，。！？；：()[]{}\"'").lower()
            if cleaned:
                features.add(cleaned)
        compact = "".join(ch for ch in query if not ch.isspace())
        if self._has_cjk(compact):
            for size in (2, 3):
                for index in range(0, max(0, len(compact) - size + 1)):
                    features.add(compact[index : index + size])
        return {feature for feature in features if feature}

    def _text_score(self, features: set[str], content: str) -> float:
        if not features:
            return 0.0
        normalized = content.lower()
        matched = sum(1 for feature in features if feature in normalized)
        return min(1.0, matched / max(1, min(len(features), 6)))

    def _has_cjk(self, value: str) -> bool:
        return any("\u4e00" <= char <= "\u9fff" for char in value)

    def _recency_score(self, written_at: datetime) -> float:
        age_days = max(0.0, (datetime.now(UTC) - written_at).total_seconds() / 86400)
        return exp(-age_days / 90)

    def _insert(self, record: MemoryRecord, *, commit: bool = True) -> None:
        self.database.conn.execute(
            """
            INSERT INTO memories VALUES (
              ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            """,
            (
                record.id,
                record.persona_id,
                record.content,
                record.type.value,
                dt(record.occurred_at),
                dt(record.written_at),
                dumps(record.participants),
                dumps(record.emotions),
                record.source_id,
                record.source_kind,
                record.source_confidence,
                record.importance,
                record.validity,
                record.access_count,
                dt(record.last_accessed_at),
                record.branch_id,
                int(record.unresolved),
                int(record.user_corrected),
                int(record.forgettable),
                record.supersedes_id,
                dumps(record.metadata),
            ),
        )
        self.database.conn.execute(
            "INSERT INTO memories_fts(memory_id, persona_id, content) VALUES (?, ?, ?)",
            (record.id, record.persona_id, record.content),
        )
        if commit:
            self.database.conn.commit()

    def _row_to_memory(self, row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=str(row["id"]),
            persona_id=str(row["persona_id"]),
            content=str(row["content"]),
            type=MemoryType(str(row["type"])),
            occurred_at=parse_dt(row["occurred_at"]),
            written_at=parse_dt(row["written_at"]) or datetime.now(UTC),
            participants=list(loads(row["participants_json"])),
            emotions=dict(loads(row["emotions_json"])),
            source_id=row["source_id"],
            source_kind=str(row["source_kind"]),
            source_confidence=float(row["source_confidence"]),
            importance=float(row["importance"]),
            validity=str(row["validity"]),
            access_count=int(row["access_count"]),
            last_accessed_at=parse_dt(row["last_accessed_at"]),
            branch_id=str(row["branch_id"]),
            unresolved=bool(row["unresolved"]),
            user_corrected=bool(row["user_corrected"]),
            forgettable=bool(row["forgettable"]),
            supersedes_id=row["supersedes_id"],
            metadata=dict(loads(row["metadata_json"])),
        )
