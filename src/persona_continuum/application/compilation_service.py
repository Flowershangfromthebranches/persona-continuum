from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from persona_continuum.application._utils import dumps, loads, new_id
from persona_continuum.application.memory_service import MemoryService
from persona_continuum.application.persona_service import PersonaService
from persona_continuum.compiler.schemas import ResearchArtifact
from persona_continuum.domain.evidence import EvidenceClaim
from persona_continuum.domain.memory import MemoryType
from persona_continuum.domain.persona import PersonaRecord
from persona_continuum.security.validation import CodedError
from persona_continuum.storage.database import Database

REQUIRED_DIMENSIONS = [
    "identity_and_timeline",
    "works_and_views",
    "interviews_and_dialogue",
    "expression_dna",
    "decisions_and_behavior",
    "third_party_views",
    "affect_relationship_defense",
    "values_desires_contradictions",
]

REQUIRED_COMPONENTS_BY_DIMENSION = {
    "identity_and_timeline": [
        "identity_profile",
        "timeline_events",
        "self_narrative_evidence",
    ],
    "works_and_views": ["mental_models", "values"],
    "interviews_and_dialogue": ["dialogue_examples", "vocabulary"],
    "expression_dna": ["expression_style", "vocabulary", "anti_patterns"],
    "decisions_and_behavior": ["decision_heuristics", "failure_patterns"],
    "third_party_views": ["relationships", "contradictions"],
    "affect_relationship_defense": [
        "temperament",
        "emotional_triggers",
        "attachment_patterns",
        "defenses",
        "relationships",
    ],
    "values_desires_contradictions": ["values", "needs_and_desires", "contradictions"],
}

ALLOWED_CLAIM_TYPES = {
    "historical_self_report",
    "historical_third_party_report",
    "historical_inference",
    "counterfactual_simulated",
    "user_correction",
}


class CompilationTask(BaseModel):
    id: str
    persona_id: str
    status: str
    plan: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class EvaluationResult(BaseModel):
    persona_id: str
    score: float
    evidence: list[str]
    failure_reason: str | None = None
    confidence: float
    recommended_fix: str | None = None
    dimensions: dict[str, float] = Field(default_factory=dict)


class CompiledPersona(BaseModel):
    persona: PersonaRecord
    manifest: Any
    claims: list[EvidenceClaim]


class CompilationService:
    def __init__(
        self,
        database: Database,
        personas: PersonaService,
        memories: MemoryService,
    ) -> None:
        self.database = database
        self.personas = personas
        self.memories = memories

    def create_task(self, persona_id: str) -> CompilationTask:
        self.personas.get(persona_id)
        task = CompilationTask(
            id=new_id("task"),
            persona_id=persona_id,
            status="created",
            plan={
                "dimensions": [
                    "identity_and_timeline",
                    "works_and_views",
                    "interviews_and_dialogue",
                    "expression_dna",
                    "decisions_and_behavior",
                    "third_party_views",
                    "affect_relationship_defense",
                    "values_desires_contradictions",
                ],
                "host_agent_responsibility": "Research and reasoning happen outside MCP.",
            },
        )
        now = datetime.now(UTC).isoformat()
        self.database.conn.execute(
            "INSERT INTO compilation_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task.id,
                persona_id,
                task.status,
                dumps(task.plan),
                dumps(task.artifacts),
                None,
                now,
                now,
            ),
        )
        self.database.conn.commit()
        return task

    def get_task(self, task_id: str) -> CompilationTask:
        row = self.database.conn.execute(
            "SELECT * FROM compilation_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return CompilationTask(
            id=str(row["id"]),
            persona_id=str(row["persona_id"]),
            status=str(row["status"]),
            plan=dict(loads(row["plan_json"])),
            artifacts=list(loads(row["artifacts_json"])),
            error=row["error"],
        )

    def submit_research_artifact(self, task_id: str, artifact: dict[str, Any]) -> CompilationTask:
        task = self.get_task(task_id)
        validated = self._validate_artifact(task, artifact)
        artifact_data = validated.model_dump(mode="json")
        canonical_sha256 = self._canonical_artifact_sha256(artifact_data)
        if self._looks_like_sha256(validated.artifact_hash) and (
            validated.artifact_hash != canonical_sha256
        ):
            raise CodedError("artifact_hash_mismatch", validated.artifact_hash)
        artifact_data["artifact_canonical_sha256"] = canonical_sha256
        duplicate_hash = next(
            (
                existing
                for existing in task.artifacts
                if existing.get("artifact_hash") == validated.artifact_hash
            ),
            None,
        )
        if duplicate_hash is not None:
            if self._canonical_artifact_payload(duplicate_hash) != self._canonical_artifact_payload(
                artifact_data
            ):
                raise CodedError("artifact_hash_conflict", validated.artifact_hash)
            task.status = "extracting"
            self._save_task(task)
            return task
        duplicate_canonical = next(
            (
                existing
                for existing in task.artifacts
                if existing.get("artifact_canonical_sha256") == canonical_sha256
            ),
            None,
        )
        if duplicate_canonical is not None:
            task.status = "extracting"
            self._save_task(task)
            return task
        existing_hash_row = self.database.conn.execute(
            """
            SELECT artifact_json FROM research_artifacts
            WHERE persona_id = ? AND task_id = ? AND artifact_hash = ?
            LIMIT 1
            """,
            (task.persona_id, task.id, validated.artifact_hash),
        ).fetchone()
        if existing_hash_row is not None and self._canonical_artifact_payload(
            dict(loads(existing_hash_row["artifact_json"]))
        ) != self._canonical_artifact_payload(artifact_data):
            raise CodedError("artifact_hash_conflict", validated.artifact_hash)
        existing_canonical_row = self.database.conn.execute(
            """
            SELECT artifact_json FROM research_artifacts
            WHERE persona_id = ? AND task_id = ? AND artifact_canonical_sha256 = ?
            LIMIT 1
            """,
            (task.persona_id, task.id, canonical_sha256),
        ).fetchone()
        if existing_canonical_row is None:
            task.artifacts.append(artifact_data)
            self._insert_research_artifact(task, artifact_data)
        else:
            existing_data = dict(loads(existing_canonical_row["artifact_json"]))
            if not any(
                existing.get("artifact_canonical_sha256") == canonical_sha256
                for existing in task.artifacts
            ):
                task.artifacts.append(existing_data)
        task.status = "extracting"
        self._save_task(task)
        return task

    def compile_persona(self, persona_id: str, task_id: str) -> CompiledPersona:
        task = self.get_task(task_id)
        if task.persona_id != persona_id:
            raise ValueError("task_persona_mismatch")
        if task.status in {"completed", "completed_with_gaps"}:
            persona = self.personas.get(persona_id)
            return CompiledPersona(persona=persona, manifest=persona.manifest, claims=[])
        task.status = "merging"
        self._save_task(task)
        version = self._next_compile_version(persona_id)
        claims: list[EvidenceClaim] = []
        for artifact in task.artifacts:
            dimension = str(artifact.get("dimension", "unspecified"))
            for claim_data in artifact.get("claims", []):
                claim = self._create_claim(persona_id, dimension, dict(claim_data))
                claim.metadata["compile_version"] = version
                claim.metadata["compile_task_id"] = task.id
                existing = self._find_existing_claim(claim)
                if existing is None:
                    self._insert_claim(claim)
                    self._insert_lineage(
                        persona_id,
                        child_type="claim",
                        child_id=claim.id,
                        parent_type="research_artifact",
                        parent_id=str(artifact["artifact_id"]),
                        relation="derived_from",
                    )
                    if claim.source_id:
                        self._insert_lineage(
                            persona_id,
                            child_type="claim",
                            child_id=claim.id,
                            parent_type="source",
                            parent_id=claim.source_id,
                            relation="supported_by",
                        )
                    claims.append(claim)
                else:
                    claims.append(existing)
            for memory_data in artifact.get("memories", []):
                existing_memory = self._find_existing_memory(persona_id, dict(memory_data))
                if existing_memory is None:
                    memory = self.memories.add_memory(
                        persona_id,
                        content=str(memory_data["content"]),
                        memory_type=MemoryType(str(memory_data.get("type", "semantic"))),
                        importance=float(memory_data.get("importance", 0.5)),
                        source_kind=str(memory_data.get("source_kind", "historical_inference")),
                        source_id=memory_data.get("source_id"),
                        source_confidence=float(memory_data.get("source_confidence", 0.5)),
                        participants=list(memory_data.get("participants", [])),
                        metadata={
                            "artifact_id": artifact["artifact_id"],
                            "dimension": dimension,
                            "compile_version": version,
                            "compile_task_id": task.id,
                        },
                    )
                    self._insert_lineage(
                        persona_id,
                        child_type="memory",
                        child_id=memory.id,
                        parent_type="research_artifact",
                        parent_id=str(artifact["artifact_id"]),
                        relation="derived_from",
                    )
                    if memory.source_id:
                        self._insert_lineage(
                            persona_id,
                            child_type="memory",
                            child_id=memory.id,
                            parent_type="source",
                            parent_id=memory.source_id,
                            relation="supported_by",
                        )
            if expression := artifact.get("expression"):
                self._write_json(persona_id, "expression/style.json", expression)
        self._write_compiled_components(persona_id, task.artifacts, version)
        persona = self.personas.get(persona_id)
        persona.manifest.source_count = self.personas.source_count(persona_id)
        persona.manifest.confidence = self._confidence(persona_id)
        persona.manifest.version = self._bump_version(persona.manifest.version)
        persona.manifest.compile_state = "compiled"
        self.personas.update_manifest(persona.manifest)
        self._write_evidence_files(persona_id)
        present = {str(artifact.get("dimension")) for artifact in task.artifacts}
        missing = [dimension for dimension in REQUIRED_DIMENSIONS if dimension not in present]
        component_gaps = self._component_gaps(task.artifacts)
        task.plan["missing_dimensions"] = missing
        task.plan["component_gaps"] = component_gaps
        task.plan["compiled_version"] = version
        task.status = "completed_with_gaps" if missing or component_gaps else "completed"
        self._save_task(task)
        self._insert_snapshot(persona_id, task.id, version)
        return CompiledPersona(
            persona=self.personas.get(persona_id), manifest=persona.manifest, claims=claims
        )

    def validate_persona(self, persona_id: str) -> EvaluationResult:
        source_count = self.personas.source_count(persona_id)
        claim_count = self._count("claims", persona_id)
        memory_count = self._count("memories", persona_id)
        component_count = self._count_components(persona_id)
        gap_rows = self.database.conn.execute(
            """
            SELECT plan_json FROM compilation_tasks
            WHERE persona_id = ? AND status = 'completed_with_gaps'
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (persona_id,),
        ).fetchall()
        dimensions = {
            "structural_completeness": min(1.0, (source_count + claim_count + memory_count) / 12),
            "source_coverage": min(1.0, source_count / 3),
            "claim_coverage": min(1.0, claim_count / 8),
            "memory_coverage": min(1.0, memory_count / 8),
            "compiled_component_coverage": min(1.0, component_count / 18),
            "gap_report_absent": 0.0 if gap_rows else 1.0,
        }
        score = (
            dimensions["compiled_component_coverage"] * 0.5
            + dimensions["structural_completeness"] * 0.2
            + dimensions["source_coverage"] * 0.1
            + dimensions["claim_coverage"] * 0.1
            + dimensions["memory_coverage"] * 0.1
        )
        result = EvaluationResult(
            persona_id=persona_id,
            score=score,
            evidence=[
                f"sources={source_count}",
                f"claims={claim_count}",
                f"memories={memory_count}",
            ],
            confidence=min(1.0, 0.3 + source_count * 0.2 + claim_count * 0.1),
            recommended_fix=None if score >= 0.5 else "Add more sourced artifacts.",
            dimensions=dimensions,
        )
        self._write_json(persona_id, "evaluation/latest.json", result.model_dump())
        return result

    def rollback_to_version(self, persona_id: str, version: int) -> object:
        row = self.database.conn.execute(
            """
            SELECT manifest_json, files_manifest_json
            FROM compile_snapshots
            WHERE persona_id = ? AND version = ?
            """,
            (persona_id, version),
        ).fetchone()
        if row is None:
            raise CodedError("snapshot_not_found", str(version))
        persona = self.personas.get(persona_id)
        snapshot = loads(row["files_manifest_json"])
        try:
            self.database.conn.execute("BEGIN")
            persona.manifest = persona.manifest.model_validate(loads(row["manifest_json"]))
            root = Path(persona.package_path)
            files = dict(snapshot.get("files", {})) if isinstance(snapshot, dict) else {}
            for relative, file_data in files.items():
                target = root / str(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(file_data, dict) and "content" in file_data:
                    target.write_text(str(file_data["content"]), encoding="utf-8")
            components = list(snapshot.get("components", [])) if isinstance(snapshot, dict) else []
            if components:
                self.database.conn.execute(
                    "DELETE FROM compiled_components WHERE persona_id = ?", (persona_id,)
                )
                for component in components:
                    self.database.conn.execute(
                        """
                        INSERT OR REPLACE INTO compiled_components
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            component["id"],
                            component["persona_id"],
                            component["version"],
                            component["component_type"],
                            component["component_key"],
                            component["content_json"],
                            component["source_artifact_ids_json"],
                            component["created_at"],
                        ),
                    )
            self._remove_data_after_version(persona_id, version)
            updated = self.personas.update_manifest(persona.manifest)
            self.database.conn.commit()
            return updated
        except Exception:
            self.database.conn.rollback()
            raise

    def _validate_artifact(
        self, task: CompilationTask, artifact: dict[str, Any]
    ) -> ResearchArtifact:
        if "schema_version" not in artifact:
            artifact = self._normalize_legacy_artifact(artifact)
        try:
            validated = ResearchArtifact.model_validate(artifact)
        except ValidationError as exc:
            raise CodedError("invalid_artifact", str(exc)) from exc
        rows = self.database.conn.execute(
            "SELECT id FROM sources WHERE persona_id = ?", (task.persona_id,)
        ).fetchall()
        valid_source_ids = {str(row["id"]) for row in rows}
        if any(source_id not in valid_source_ids for source_id in validated.source_ids):
            raise CodedError("invalid_artifact", "source_id_not_found")
        return validated

    def _normalize_legacy_artifact(self, artifact: dict[str, Any]) -> dict[str, Any]:
        source_ids = {
            str(item["source_id"])
            for collection in (artifact.get("claims", []), artifact.get("memories", []))
            for item in collection
            if isinstance(item, dict) and item.get("source_id")
        }
        dimension = str(artifact.get("dimension", "identity_and_timeline"))
        if dimension not in REQUIRED_DIMENSIONS:
            dimension = "identity_and_timeline"
        components: dict[str, Any] = dict(artifact.get("extracted_components", {}))
        if expression := artifact.get("expression"):
            components.setdefault("expression_style", expression)
        components.setdefault("identity_profile", {"summary": f"Legacy artifact for {dimension}"})
        return {
            "artifact_id": str(artifact.get("artifact_id") or new_id("art")),
            "schema_version": "1.1",
            "dimension": dimension,
            "source_ids": sorted(source_ids) or ["legacy_unsourced"],
            "claims": artifact.get("claims", []),
            "memories": artifact.get("memories", []),
            "extracted_components": components,
            "conflicts": artifact.get("conflicts", []),
            "uncertainty": artifact.get(
                "uncertainty", {"level": 0.5, "notes": ["legacy_artifact"]}
            ),
            "created_by": str(artifact.get("created_by", "legacy_agent_artifact")),
            "artifact_hash": str(artifact.get("artifact_hash") or dumps(artifact)),
        }

    def _insert_research_artifact(
        self, task: CompilationTask, artifact_data: dict[str, Any]
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.database.conn.execute(
            """
            INSERT OR IGNORE INTO research_artifacts (
              id,
              persona_id,
              task_id,
              dimension,
              artifact_hash,
              artifact_canonical_sha256,
              artifact_json,
              created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_data["artifact_id"],
                task.persona_id,
                task.id,
                artifact_data["dimension"],
                artifact_data["artifact_hash"],
                artifact_data.get("artifact_canonical_sha256"),
                dumps(artifact_data),
                now,
            ),
        )
        for source_id in artifact_data.get("source_ids", []):
            self._insert_lineage(
                task.persona_id,
                child_type="research_artifact",
                child_id=str(artifact_data["artifact_id"]),
                parent_type="source",
                parent_id=str(source_id),
                relation="extracted_from",
            )

    def _find_existing_claim(self, claim: EvidenceClaim) -> EvidenceClaim | None:
        row = self.database.conn.execute(
            """
            SELECT * FROM claims
            WHERE persona_id = ?
              AND content = ?
              AND dimension = ?
              AND COALESCE(source_id, '') = COALESCE(?, '')
              AND claim_type = ?
            LIMIT 1
            """,
            (
                claim.persona_id,
                claim.content,
                claim.dimension,
                claim.source_id,
                claim.claim_type,
            ),
        ).fetchone()
        if row is None:
            return None
        return EvidenceClaim(
            id=str(row["id"]),
            persona_id=str(row["persona_id"]),
            content=str(row["content"]),
            dimension=str(row["dimension"]),
            source_id=row["source_id"],
            claim_type=str(row["claim_type"]),
            raw_location=row["raw_location"],
            event_time=row["event_time"],
            reliability=float(row["reliability"]),
            is_self_report=bool(row["is_self_report"]),
            is_third_party_report=bool(row["is_third_party_report"]),
            has_counter_evidence=bool(row["has_counter_evidence"]),
            inference_strength=float(row["inference_strength"]),
            confidence=float(row["confidence"]),
            created_by=str(row["created_by"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            metadata=dict(loads(row["metadata_json"])),
        )

    def _find_existing_memory(self, persona_id: str, memory_data: dict[str, Any]) -> str | None:
        row = self.database.conn.execute(
            """
            SELECT id FROM memories
            WHERE persona_id = ?
              AND content = ?
              AND type = ?
              AND source_kind = ?
              AND COALESCE(source_id, '') = COALESCE(?, '')
              AND validity = 'valid'
            LIMIT 1
            """,
            (
                persona_id,
                str(memory_data["content"]),
                str(memory_data.get("type", "semantic")),
                str(memory_data.get("source_kind", "historical_inference")),
                memory_data.get("source_id"),
            ),
        ).fetchone()
        return str(row["id"]) if row else None

    def _insert_lineage(
        self,
        persona_id: str,
        *,
        child_type: str,
        child_id: str,
        parent_type: str,
        parent_id: str,
        relation: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.database.conn.execute(
            """
            INSERT OR IGNORE INTO lineage
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("lin"),
                persona_id,
                child_type,
                child_id,
                parent_type,
                parent_id,
                relation,
                dumps(metadata or {}),
                datetime.now(UTC).isoformat(),
            ),
        )

    def _next_compile_version(self, persona_id: str) -> int:
        row = self.database.conn.execute(
            """
            SELECT COALESCE(MAX(version), 0) AS version
            FROM compile_snapshots
            WHERE persona_id = ?
            """,
            (persona_id,),
        ).fetchone()
        return int(row["version"]) + 1

    def _bump_version(self, version: str) -> str:
        parts = version.split(".")
        if len(parts) >= 3 and parts[-1].isdigit():
            parts[-1] = str(int(parts[-1]) + 1)
            return ".".join(parts)
        return f"{version}.1"

    def _write_compiled_components(
        self, persona_id: str, artifacts: list[dict[str, Any]], version: int
    ) -> None:
        merged = self._merge_components(artifacts)
        file_map = {
            "identity/profile.json": {
                "identity_profile": merged["identity_profile"],
                "schema_version": "1.1",
            },
            "identity/timeline.jsonl": merged["timeline_events"],
            "identity/self_narrative.md": "\n".join(merged["self_narrative_evidence"]),
            "cognition/mental_models.json": merged["mental_models"],
            "cognition/decision_heuristics.json": merged["decision_heuristics"],
            "cognition/values.json": merged["values"],
            "cognition/contradictions.json": merged["contradictions"],
            "cognition/failure_patterns.json": merged["failure_patterns"],
            "affect/temperament.json": merged["temperament"],
            "affect/emotional_triggers.json": merged["emotional_triggers"],
            "affect/attachment.json": merged["attachment_patterns"],
            "affect/needs.json": merged["needs_and_desires"],
            "affect/defenses.json": merged["defenses"],
            "expression/style.json": merged["expression_style"],
            "expression/vocabulary.json": merged["vocabulary"],
            "expression/dialogue_examples.jsonl": merged["dialogue_examples"],
            "expression/anti_patterns.json": merged["anti_patterns"],
            "relationships/relationships.json": merged["relationships"],
        }
        for relative, value in file_map.items():
            if relative.endswith(".jsonl"):
                self._write_jsonl(
                    persona_id, relative, value if isinstance(value, list) else [value]
                )
            elif relative.endswith(".md"):
                path = Path(self.personas.get(persona_id).package_path) / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(value), encoding="utf-8")
            else:
                self._write_json(persona_id, relative, value)
        artifact_ids = [str(artifact["artifact_id"]) for artifact in artifacts]
        for key, value in merged.items():
            component_id = new_id("comp")
            self.database.conn.execute(
                """
                INSERT OR IGNORE INTO compiled_components
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    component_id,
                    persona_id,
                    version,
                    "persona_component",
                    key,
                    dumps(value),
                    dumps(artifact_ids),
                    datetime.now(UTC).isoformat(),
                ),
            )
            for artifact_id in artifact_ids:
                self._insert_lineage(
                    persona_id,
                    child_type="compiled_component",
                    child_id=component_id,
                    parent_type="research_artifact",
                    parent_id=artifact_id,
                    relation="compiled_from",
                )

    def _merge_components(self, artifacts: list[dict[str, Any]]) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "identity_profile": {},
            "timeline_events": [],
            "self_narrative_evidence": [],
            "mental_models": [],
            "decision_heuristics": [],
            "values": [],
            "contradictions": [],
            "failure_patterns": [],
            "temperament": {},
            "emotional_triggers": [],
            "attachment_patterns": {},
            "needs_and_desires": [],
            "defenses": [],
            "expression_style": {},
            "vocabulary": [],
            "dialogue_examples": [],
            "anti_patterns": [],
            "relationships": [],
        }
        for artifact in artifacts:
            components = dict(artifact.get("extracted_components", {}))
            for key, value in components.items():
                if key not in defaults:
                    continue
                if isinstance(defaults[key], list):
                    defaults[key] = self._dedupe_list([*defaults[key], *self._as_list(value)])
                elif isinstance(defaults[key], dict):
                    if isinstance(value, dict):
                        defaults[key] = {**defaults[key], **value}
                    else:
                        defaults[key] = {"value": value}
        return defaults

    def _component_gaps(self, artifacts: list[dict[str, Any]]) -> dict[str, list[str]]:
        gaps: dict[str, list[str]] = {}
        by_dimension: dict[str, dict[str, Any]] = {}
        for artifact in artifacts:
            dimension = str(artifact.get("dimension"))
            components = by_dimension.setdefault(dimension, {})
            components.update(dict(artifact.get("extracted_components", {})))
        for dimension, required_keys in REQUIRED_COMPONENTS_BY_DIMENSION.items():
            if dimension not in by_dimension:
                continue
            missing = [
                key
                for key in required_keys
                if self._is_gap_value(by_dimension[dimension].get(key))
            ]
            if missing:
                gaps[dimension] = missing
        return gaps

    def _is_gap_value(self, value: Any) -> bool:
        if value is None:
            return True
        if value in ({}, [], ""):
            return True
        return isinstance(value, list) and all(self._is_gap_value(item) for item in value)

    def _as_list(self, value: Any) -> list[Any]:
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

    def _dedupe_list(self, values: list[Any]) -> list[Any]:
        seen: set[str] = set()
        deduped = []
        for value in values:
            key = dumps(value)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(value)
        return deduped

    def _write_jsonl(self, persona_id: str, relative_path: str, values: list[Any]) -> None:
        path = Path(self.personas.get(persona_id).package_path) / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [dumps(value) if not isinstance(value, str) else value for value in values]
        path.write_text("\n".join(lines), encoding="utf-8")

    def _insert_snapshot(self, persona_id: str, task_id: str, version: int) -> None:
        persona = self.personas.get(persona_id)
        files_manifest: dict[str, Any] = {"schema_version": "1.1", "files": {}, "components": []}
        root = Path(persona.package_path)
        for path in root.rglob("*"):
            if path.is_file():
                content = path.read_text(encoding="utf-8", errors="ignore")
                files_manifest["files"][str(path.relative_to(root))] = {
                    "size": path.stat().st_size,
                    "sha256": self._hash_text(content),
                    "content": content,
                }
        rows = self.database.conn.execute(
            """
            SELECT * FROM compiled_components
            WHERE persona_id = ? AND version = ?
            ORDER BY component_key
            """,
            (persona_id, version),
        ).fetchall()
        files_manifest["components"] = [dict(row) for row in rows]
        self.database.conn.execute(
            """
            INSERT OR IGNORE INTO compile_snapshots
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("snap"),
                persona_id,
                version,
                task_id,
                dumps(persona.manifest.model_dump()),
                dumps(files_manifest),
                datetime.now(UTC).isoformat(),
            ),
        )
        self.database.conn.commit()

    def _canonical_artifact_payload(self, artifact: dict[str, Any]) -> str:
        comparable = dict(artifact)
        comparable.pop("artifact_hash", None)
        comparable.pop("artifact_canonical_sha256", None)
        return dumps(comparable)

    def _canonical_artifact_sha256(self, artifact: dict[str, Any]) -> str:
        return self._hash_text(self._canonical_artifact_payload(artifact))

    def _looks_like_sha256(self, value: str) -> bool:
        return re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None

    def _remove_data_after_version(self, persona_id: str, version: int) -> None:
        claim_rows = self.database.conn.execute(
            "SELECT id, metadata_json FROM claims WHERE persona_id = ?", (persona_id,)
        ).fetchall()
        for row in claim_rows:
            metadata = dict(loads(row["metadata_json"]))
            compile_version = metadata.get("compile_version")
            if isinstance(compile_version, int | float) and int(compile_version) > version:
                self.database.conn.execute(
                    "DELETE FROM claims WHERE persona_id = ? AND id = ?",
                    (persona_id, row["id"]),
                )
        memory_rows = self.database.conn.execute(
            "SELECT id, metadata_json FROM memories WHERE persona_id = ?", (persona_id,)
        ).fetchall()
        for row in memory_rows:
            metadata = dict(loads(row["metadata_json"]))
            compile_version = metadata.get("compile_version")
            if isinstance(compile_version, int | float) and int(compile_version) > version:
                self.database.conn.execute(
                    "DELETE FROM memories_fts WHERE persona_id = ? AND memory_id = ?",
                    (persona_id, row["id"]),
                )
                self.database.conn.execute(
                    "DELETE FROM memories WHERE persona_id = ? AND id = ?",
                    (persona_id, row["id"]),
                )

    def _hash_text(self, value: str) -> str:
        import hashlib

        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _create_claim(
        self, persona_id: str, dimension: str, claim_data: dict[str, Any]
    ) -> EvidenceClaim:
        claim_type = str(claim_data.get("claim_type", "historical_inference"))
        return EvidenceClaim(
            id=new_id("claim"),
            persona_id=persona_id,
            content=str(claim_data["content"]),
            dimension=dimension,
            source_id=claim_data.get("source_id"),
            claim_type=claim_type,
            reliability=float(claim_data.get("reliability", claim_data.get("confidence", 0.5))),
            is_self_report=claim_type == "historical_self_report",
            is_third_party_report=claim_type == "historical_third_party_report",
            has_counter_evidence=bool(claim_data.get("has_counter_evidence", False)),
            inference_strength=float(claim_data.get("inference_strength", 0.5)),
            confidence=float(claim_data.get("confidence", 0.5)),
            raw_location=claim_data.get("raw_location"),
            event_time=claim_data.get("event_time"),
            metadata=dict(claim_data.get("metadata", {})),
        )

    def _insert_claim(self, claim: EvidenceClaim) -> None:
        self.database.conn.execute(
            """
            INSERT INTO claims VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                claim.id,
                claim.persona_id,
                claim.content,
                claim.dimension,
                claim.source_id,
                claim.claim_type,
                claim.raw_location,
                claim.event_time,
                claim.reliability,
                int(claim.is_self_report),
                int(claim.is_third_party_report),
                int(claim.has_counter_evidence),
                claim.inference_strength,
                claim.confidence,
                claim.created_by,
                dumps(claim.metadata),
                claim.created_at.isoformat(),
            ),
        )
        self.database.conn.commit()

    def _save_task(self, task: CompilationTask) -> None:
        self.database.conn.execute(
            """
            UPDATE compilation_tasks
            SET status = ?, plan_json = ?, artifacts_json = ?, error = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                task.status,
                dumps(task.plan),
                dumps(task.artifacts),
                task.error,
                datetime.now(UTC).isoformat(),
                task.id,
            ),
        )
        self.database.conn.commit()

    def _confidence(self, persona_id: str) -> dict[str, float]:
        rows = self.database.conn.execute(
            """
            SELECT dimension, AVG(confidence) AS confidence
            FROM claims
            WHERE persona_id = ?
            GROUP BY dimension
            """,
            (persona_id,),
        ).fetchall()
        return {str(row["dimension"]): float(row["confidence"]) for row in rows}

    def _count(self, table: str, persona_id: str) -> int:
        if table not in {"claims", "memories", "sources"}:
            raise ValueError(f"unsupported_count_table:{table}")
        row = self.database.conn.execute(
            f"SELECT COUNT(*) AS count FROM {table} WHERE persona_id = ?", (persona_id,)
        ).fetchone()
        return int(row["count"])

    def _count_components(self, persona_id: str) -> int:
        row = self.database.conn.execute(
            "SELECT COUNT(*) AS count FROM compiled_components WHERE persona_id = ?",
            (persona_id,),
        ).fetchone()
        return int(row["count"])

    def _write_json(self, persona_id: str, relative_path: str, value: Any) -> None:
        path = Path(self.personas.get(persona_id).package_path) / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )

    def _write_evidence_files(self, persona_id: str) -> None:
        root = Path(self.personas.get(persona_id).package_path)
        sources = self.personas.get_sources(persona_id)
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
