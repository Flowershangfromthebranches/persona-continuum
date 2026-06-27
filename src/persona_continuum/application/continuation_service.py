from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from persona_continuum.application._utils import dumps, loads, new_id
from persona_continuum.application.compiled_context_service import CompiledPersonaContextService
from persona_continuum.application.memory_service import MemoryService
from persona_continuum.application.persona_service import PersonaService
from persona_continuum.domain.continuation import ContinuationBranch, ContinuationTask
from persona_continuum.domain.memory import MemoryType
from persona_continuum.domain.persona import RunMode
from persona_continuum.security.validation import CodedError
from persona_continuum.storage.database import Database

REQUIRED_STEP_FIELDS = [
    "evaluated_events",
    "chosen_actions",
    "world_state_delta",
    "persona_state_delta",
    "relationship_deltas",
    "affect_deltas",
    "goal_deltas",
    "new_memories",
    "rejected_alternatives",
    "causal_explanation",
    "uncertainty",
    "evidence_links",
    "next_step_date",
]


class ContinuationEvaluatedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1)
    date: str | None = None


class ContinuationChosenAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1)
    reason: str | None = None


class ContinuationRelationshipDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    counterpart_id: str = Field(min_length=1)
    changes: dict[str, float] = Field(min_length=1)
    reason: str | None = None


class ContinuationGoalDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    delta: float | None = None


class ContinuationMemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1)
    importance: float = Field(default=0.55, ge=0, le=1)


class ContinuationRejectedAlternative(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ContinuationEvidenceLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1)
    id: str = Field(min_length=1)


class ContinuationStepArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluated_events: list[ContinuationEvaluatedEvent]
    chosen_actions: list[ContinuationChosenAction]
    world_state_delta: dict[str, Any] = Field(min_length=1)
    persona_state_delta: dict[str, Any] = Field(min_length=1)
    relationship_deltas: list[ContinuationRelationshipDelta]
    affect_deltas: dict[str, float]
    goal_deltas: list[ContinuationGoalDelta]
    new_memories: list[ContinuationMemoryCandidate]
    rejected_alternatives: list[ContinuationRejectedAlternative]
    causal_explanation: str = Field(min_length=1)
    uncertainty: float = Field(ge=0, le=1)
    evidence_links: list[ContinuationEvidenceLink]
    next_step_date: str = Field(min_length=1)


class ContinuationService:
    def __init__(
        self,
        database: Database,
        personas: PersonaService,
        memories: MemoryService,
        compiled_context: CompiledPersonaContextService,
    ) -> None:
        self.database = database
        self.personas = personas
        self.memories = memories
        self.compiled_context = compiled_context

    def create(self, persona_id: str, divergence_condition: str) -> ContinuationTask:
        self.personas.get(persona_id)
        task = ContinuationTask(
            id=new_id("cont"),
            persona_id=persona_id,
            divergence_condition=divergence_condition,
        )
        self._insert_task(task)
        return task

    def get(self, continuation_id: str) -> ContinuationTask:
        row = self.database.conn.execute(
            "SELECT task_json FROM continuations WHERE id = ?", (continuation_id,)
        ).fetchone()
        if row is None:
            raise KeyError(continuation_id)
        return ContinuationTask.model_validate(loads(row["task_json"]))

    def add_world_events(
        self, continuation_id: str, events: list[dict[str, Any]]
    ) -> ContinuationTask:
        task = self.get(continuation_id)
        task.world_events.extend(events)
        task.status = "planning"
        task.updated_at = datetime.now(UTC)
        self._save_task(task)
        return task

    def create_branch(
        self,
        continuation_id: str,
        parent_branch_id: str | None = None,
        seed: int = 0,
    ) -> ContinuationBranch:
        task = self.get(continuation_id)
        parent: ContinuationBranch | None = None
        if parent_branch_id is not None:
            parent = self._get_parent_branch(continuation_id, parent_branch_id)
        branch = ContinuationBranch(
            id=new_id("branch"),
            continuation_id=continuation_id,
            persona_id=task.persona_id,
            parent_branch_id=parent_branch_id,
            divergence_event=task.divergence_condition,
            world_state=self._inherited_world_state(task.world_events, parent),
            persona_state=self._inherited_persona_state(parent),
            key_events=list(parent.key_events) if parent else [],
            relationship_changes=list(parent.relationship_changes) if parent else [],
            persona_changes=list(parent.persona_changes) if parent else [],
            random_seed=seed,
        )
        self._insert_branch(branch)
        source_branch_id = parent.id if parent else "main"
        self._clone_runtime_branch(task.persona_id, source_branch_id, branch.id)
        return branch

    def prepare_step(self, branch_id: str, target_date: str) -> dict[str, Any]:
        branch = self.get_branch(branch_id)
        if branch.status not in {"active", "created"}:
            raise CodedError("continuation_invalid_state", branch.status)
        persona = self.personas.get(branch.persona_id)
        branch.status = "waiting_for_host"
        branch.persona_state["pending_step"] = {"target_date": target_date}
        branch.updated_at = datetime.now(UTC)
        self._save_branch(branch)
        compiled_context = self.compiled_context.prepare_context(
            branch.persona_id,
            f"{branch.divergence_event} {target_date}",
            max_items=16,
            branch_id=branch.id,
        )
        compiled_by_key = dict(compiled_context.get("by_key", {}))
        historical_decision_patterns = []
        for key in ("decision_heuristics", "mental_models"):
            value = compiled_by_key.get(key)
            if isinstance(value, list):
                historical_decision_patterns.extend(value)
            elif value:
                historical_decision_patterns.append(value)
        return {
            "branch_id": branch.id,
            "persona_state": branch.persona_state,
            "compiled_persona_context": compiled_context,
            "time_range": {"target_date": target_date},
            "age_and_health_state": branch.persona_state.get("age_and_health", {}),
            "current_goals_and_desires": branch.persona_state.get("goals", []),
            "relationship_network": branch.relationship_changes,
            "branch_events_so_far": branch.key_events,
            "current_world_state": branch.world_state,
            "new_external_events": branch.world_state.get("events", []),
            "influence_boundary": {
                "persona_can_influence": ["chosen_actions", "relationship_responses"],
                "persona_cannot_influence": ["external_world_events"],
            },
            "historical_decision_patterns": historical_decision_patterns,
            "causal_constraints": [
                "Do not present counterfactual events as historical fact.",
                "Every durable change needs a state delta and uncertainty estimate.",
            ],
            "output_artifact_schema": {
                "required": [
                    *REQUIRED_STEP_FIELDS,
                ],
                "persona_id": persona.id,
            },
        }

    def commit_step(self, branch_id: str, artifact: dict[str, Any]) -> ContinuationBranch:
        branch = self.get_branch(branch_id)
        if branch.status != "waiting_for_host":
            raise CodedError("continuation_not_waiting_for_host", branch.status)
        try:
            validated = ContinuationStepArtifact.model_validate(artifact)
        except ValidationError as exc:
            raise CodedError("invalid_continuation_artifact", str(exc)) from exc
        artifact_data = validated.model_dump(mode="json")
        self._validate_next_step_date(branch, validated.next_step_date)
        branch.world_state.setdefault("deltas", []).append(artifact_data["world_state_delta"])
        branch.persona_state.setdefault("deltas", []).append(artifact_data["persona_state_delta"])
        branch.persona_state["last_uncertainty"] = float(validated.uncertainty)
        branch.relationship_changes.extend(list(artifact_data["relationship_deltas"]))
        branch.persona_changes.append(dumps(artifact_data["persona_state_delta"]))
        branch.key_events.extend(list(artifact_data["evaluated_events"]))
        branch.key_events.extend(
            {"content": str(action), "source_kind": "counterfactual_host_action"}
            for action in artifact_data["chosen_actions"]
        )
        for memory_data in artifact_data["new_memories"]:
            memory = self.memories.add_memory(
                branch.persona_id,
                content=str(memory_data["content"]),
                memory_type=MemoryType.COUNTERFACTUAL,
                importance=float(memory_data.get("importance", 0.55)),
                source_kind="counterfactual_host_artifact",
                branch_id=branch.id,
                metadata={
                    "branch_id": branch.id,
                    "causal_explanation": artifact_data["causal_explanation"],
                    "simulation_step_date": artifact_data["next_step_date"],
                    "created_from_step_id": artifact_data["next_step_date"],
                },
            )
            self._insert_lineage(
                branch.persona_id,
                child_type="memory",
                child_id=memory.id,
                parent_type="continuation_branch",
                parent_id=branch.id,
                relation="counterfactual_step_from",
            )
        branch.persona_state["next_step_date"] = artifact_data["next_step_date"]
        branch.persona_state["submitted_step_count"] = (
            int(branch.persona_state.get("submitted_step_count", 0)) + 1
        )
        branch.status = "active"
        branch.updated_at = datetime.now(UTC)
        self._save_branch(branch)
        return branch

    def advance_branch(self, branch_id: str, target_date: str) -> ContinuationBranch:
        branch = self.get_branch(branch_id)
        if branch.status not in {"active", "created"}:
            raise CodedError("continuation_invalid_state", branch.status)
        branch.status = "waiting_for_host"
        branch.persona_state["pending_step"] = {"target_date": target_date}
        branch.updated_at = datetime.now(UTC)
        self._save_branch(branch)
        return branch

    def compare_branches(self, continuation_id: str) -> dict[str, Any]:
        branches = self.list_branches(continuation_id)
        return {
            "branch_count": len(branches),
            "scores": {branch.id: branch.score for branch in branches},
            "stable_changes": self._stable_state_deltas(branches),
        }

    def score_branch(self, branch_id: str) -> ContinuationBranch:
        branch = self.get_branch(branch_id)
        uncertainty = float(branch.persona_state.get("last_uncertainty", 0.5))
        factors = {
            "initial_persona_consistency": 0.75 if branch.persona_changes else 0.45,
            "branch_history_consistency": 0.8 if branch.status != "eliminated" else 0.2,
            "causal_consistency": 0.75 if branch.persona_state.get("deltas") else 0.35,
            "world_constraint_consistency": 0.75 if branch.world_state.get("deltas") else 0.45,
            "relationship_reasonableness": 0.65 if branch.relationship_changes else 0.5,
            "evidence_support": min(1.0, 0.4 + len(branch.key_events) * 0.1),
            "unexplained_jump_penalty": 0.9 if branch.persona_state.get("deltas") else 0.4,
            "uncertainty": max(0.0, 1.0 - uncertainty),
        }
        branch.score = sum(factors.values()) / len(factors)
        branch.credibility = branch.score
        branch.persona_state["score_breakdown"] = factors
        self._save_branch(branch)
        return branch

    def select_main_branch(self, continuation_id: str, branch_id: str) -> ContinuationTask:
        task = self.get(continuation_id)
        branch = self.get_branch(branch_id)
        if branch.continuation_id != continuation_id or branch.persona_id != task.persona_id:
            raise CodedError("continuation_branch_mismatch", branch_id)
        self._require_branch_ready_for_compile(branch)
        task.main_branch_id = branch_id
        task.status = "completed"
        self._save_task(task)
        persona = self.personas.get(task.persona_id)
        persona.manifest.current_main_branch = branch_id
        persona.manifest.has_counterfactual_continuation = True
        self.personas.update_manifest(persona.manifest)
        return task

    def compile_persona(self, continuation_id: str) -> object:
        task = self.get(continuation_id)
        if task.main_branch_id is None:
            raise CodedError("continuation_main_branch_missing", continuation_id)
        branch = self.get_branch(task.main_branch_id)
        if branch.continuation_id != continuation_id or branch.persona_id != task.persona_id:
            raise CodedError("continuation_branch_mismatch", task.main_branch_id)
        self._require_branch_ready_for_compile(branch)
        persona = self.personas.get(task.persona_id)
        branch_hash = self._branch_compile_hash(branch)
        if branch.persona_state.get("compiled_hash") == branch_hash:
            return persona
        persona.manifest.run_mode = RunMode.COUNTERFACTUAL_CONTINUATION
        persona.manifest.has_counterfactual_continuation = True
        persona.manifest.current_main_branch = task.main_branch_id
        persona.manifest.version = self._bump_version(persona.manifest.version)
        persona.manifest.compile_state = "compiled"
        version = self._next_component_version(persona.id)
        self._write_branch_persona_version(persona.id, branch, version)
        branch.persona_state["compiled_hash"] = branch_hash
        branch.persona_state["compiled_version"] = version
        branch.updated_at = datetime.now(UTC)
        self._save_branch(branch)
        return self.personas.update_manifest(persona.manifest)

    def get_branch(self, branch_id: str) -> ContinuationBranch:
        row = self.database.conn.execute(
            "SELECT branch_json FROM continuation_branches WHERE id = ?", (branch_id,)
        ).fetchone()
        if row is None:
            raise KeyError(branch_id)
        return ContinuationBranch.model_validate(loads(row["branch_json"]))

    def list_branches(self, continuation_id: str) -> list[ContinuationBranch]:
        rows = self.database.conn.execute(
            "SELECT branch_json FROM continuation_branches WHERE continuation_id = ?",
            (continuation_id,),
        ).fetchall()
        return [ContinuationBranch.model_validate(loads(row["branch_json"])) for row in rows]

    def _insert_task(self, task: ContinuationTask) -> None:
        self.database.conn.execute(
            "INSERT INTO continuations VALUES (?, ?, ?, ?, ?)",
            (
                task.id,
                task.persona_id,
                dumps(task.model_dump()),
                task.created_at.isoformat(),
                task.updated_at.isoformat(),
            ),
        )
        self.database.conn.commit()

    def _save_task(self, task: ContinuationTask) -> None:
        self.database.conn.execute(
            "UPDATE continuations SET task_json = ?, updated_at = ? WHERE id = ?",
            (dumps(task.model_dump()), task.updated_at.isoformat(), task.id),
        )
        self.database.conn.commit()

    def _insert_branch(self, branch: ContinuationBranch) -> None:
        self.database.conn.execute(
            "INSERT INTO continuation_branches VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                branch.id,
                branch.continuation_id,
                branch.persona_id,
                dumps(branch.model_dump()),
                branch.score,
                branch.status,
                branch.created_at.isoformat(),
                branch.updated_at.isoformat(),
            ),
        )
        self.database.conn.commit()

    def _save_branch(self, branch: ContinuationBranch) -> None:
        self.database.conn.execute(
            """
            UPDATE continuation_branches
            SET branch_json = ?, score = ?, status = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                dumps(branch.model_dump()),
                branch.score,
                branch.status,
                branch.updated_at.isoformat(),
                branch.id,
            ),
        )
        self.database.conn.commit()

    def _stable_state_deltas(self, branches: list[ContinuationBranch]) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        values: dict[str, dict[str, Any]] = {}
        for branch in branches:
            for delta in branch.persona_state.get("deltas", []):
                key = dumps(delta)
                counts[key] = counts.get(key, 0) + 1
                values[key] = delta if isinstance(delta, dict) else {"value": delta}
        threshold = max(1, len(branches))
        return [values[key] for key, count in counts.items() if count >= threshold]

    def _validate_next_step_date(self, branch: ContinuationBranch, next_step_date: str) -> None:
        try:
            next_dt = datetime.fromisoformat(next_step_date)
        except ValueError as exc:
            raise CodedError("invalid_continuation_artifact", "next_step_date") from exc
        pending_date = branch.persona_state.get("pending_step", {}).get("target_date")
        if pending_date:
            try:
                pending_dt = datetime.fromisoformat(str(pending_date))
            except ValueError:
                pending_dt = None
            if pending_dt is not None and next_dt <= pending_dt:
                raise CodedError("invalid_continuation_artifact", "next_step_date_not_monotonic")
        previous_date = branch.persona_state.get("next_step_date")
        if previous_date:
            try:
                previous_dt = datetime.fromisoformat(str(previous_date))
            except ValueError:
                previous_dt = None
            if previous_dt is not None and next_dt <= previous_dt:
                raise CodedError("invalid_continuation_artifact", "next_step_date_not_monotonic")

    def _require_branch_ready_for_compile(self, branch: ContinuationBranch) -> None:
        if branch.status not in {"active", "completed"}:
            raise CodedError("continuation_branch_not_ready", branch.id)
        if int(branch.persona_state.get("submitted_step_count", 0)) <= 0:
            raise CodedError("continuation_branch_not_ready", branch.id)

    def _branch_compile_hash(self, branch: ContinuationBranch) -> str:
        payload = {
            "branch_id": branch.id,
            "continuation_id": branch.continuation_id,
            "world_state": branch.world_state,
            "persona_state_deltas": branch.persona_state.get("deltas", []),
            "relationship_changes": branch.relationship_changes,
            "key_events": branch.key_events,
        }
        import hashlib

        return hashlib.sha256(dumps(payload).encode("utf-8")).hexdigest()

    def _get_parent_branch(
        self, continuation_id: str, parent_branch_id: str
    ) -> ContinuationBranch:
        try:
            parent = self.get_branch(parent_branch_id)
        except KeyError as exc:
            raise CodedError(
                "continuation_parent_branch_not_found", parent_branch_id
            ) from exc
        if parent.continuation_id != continuation_id:
            raise CodedError("continuation_parent_branch_mismatch", parent_branch_id)
        return parent

    def _inherited_world_state(
        self, task_events: list[dict[str, Any]], parent: ContinuationBranch | None
    ) -> dict[str, Any]:
        if parent is None:
            return {"events": task_events}
        value = json.loads(json.dumps(parent.world_state, ensure_ascii=False, default=str))
        return dict(value) if isinstance(value, dict) else {}

    def _inherited_persona_state(self, parent: ContinuationBranch | None) -> dict[str, Any]:
        if parent is None:
            return {"mode": "counterfactual_simulation"}
        value = json.loads(json.dumps(parent.persona_state, ensure_ascii=False, default=str))
        inherited = dict(value) if isinstance(value, dict) else {}
        inherited["divergence_at"] = (
            inherited.get("next_step_date")
            or inherited.get("pending_step", {}).get("target_date")
            or datetime.now(UTC).isoformat()
        )
        inherited["parent_branch_snapshot"] = parent.id
        return inherited

    def _clone_runtime_branch(
        self, persona_id: str, source_branch_id: str, target_branch_id: str
    ) -> None:
        for table in ("affect_states", "needs", "relationships"):
            self.database.conn.execute(
                f"DELETE FROM {table} WHERE persona_id = ? AND branch_id = ?",
                (persona_id, target_branch_id),
            )
        self.database.conn.execute(
            """
            INSERT OR REPLACE INTO affect_states
            SELECT persona_id, ?, name, kind, intensity, baseline, decay_rate,
                   updated_at, triggers_json, confidence
            FROM affect_states
            WHERE persona_id = ? AND branch_id = ?
            """,
            (target_branch_id, persona_id, source_branch_id),
        )
        self.database.conn.execute(
            """
            INSERT OR REPLACE INTO needs
            SELECT persona_id, ?, name, level, baseline, updated_at, confidence, reasons_json
            FROM needs
            WHERE persona_id = ? AND branch_id = ?
            """,
            (target_branch_id, persona_id, source_branch_id),
        )
        self.database.conn.execute(
            """
            INSERT OR REPLACE INTO relationships
            SELECT persona_id, ?, counterpart, state_json, updated_at
            FROM relationships
            WHERE persona_id = ? AND branch_id = ?
            """,
            (target_branch_id, persona_id, source_branch_id),
        )
        persona = self.personas.get(persona_id)
        runtime_root = Path(persona.package_path) / "runtime" / "branches"
        source = runtime_root / source_branch_id / "runtime_state.json"
        target = runtime_root / target_branch_id / "runtime_state.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            payload = dict(loads(source.read_text(encoding="utf-8")))
            payload["branch_id"] = target_branch_id
            target.write_text(dumps(payload), encoding="utf-8")
        else:
            target.write_text(
                dumps(
                    {
                        "schema_version": "1.1",
                        "branch_id": target_branch_id,
                        "revision": datetime.now(UTC).isoformat(),
                        "active_goals": [],
                        "self_narrative_updates": [],
                        "unresolved_conflicts": [],
                        "reflection_insights": [],
                    }
                ),
                encoding="utf-8",
            )
        self.database.conn.commit()

    def _next_component_version(self, persona_id: str) -> int:
        row = self.database.conn.execute(
            """
            SELECT COALESCE(MAX(version), 0) AS version
            FROM compiled_components
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

    def _write_branch_persona_version(
        self, persona_id: str, branch: ContinuationBranch, version: int
    ) -> None:
        persona = self.personas.get(persona_id)
        root = Path(persona.package_path)
        provenance = {
            "branch provenance": {
                "branch_id": branch.id,
                "continuation_id": branch.continuation_id,
                "compiled_version": version,
                "persona_state_delta": branch.persona_state.get("deltas", []),
                "relationship_delta": branch.relationship_changes,
                "counterfactual_memories": branch.key_events,
            }
        }
        branch_root = root / "continuation" / "branches" / branch.id
        branch_root.mkdir(parents=True, exist_ok=True)
        file_payloads = {
            "self_narrative_delta.json": branch.persona_state.get("deltas", []),
            "persona_state_delta.json": branch.persona_state.get("deltas", []),
            "relationship_delta.json": branch.relationship_changes,
            "world_state.json": branch.world_state,
            "provenance.json": provenance,
        }
        for filename, payload in file_payloads.items():
            (branch_root / filename).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        components = {
            "branch_provenance": provenance,
            "persona_state_delta": branch.persona_state.get("deltas", []),
            "relationship_delta": branch.relationship_changes,
            "counterfactual_memories": [
                memory.content
                for memory in self.memories.search_memories(
                    persona_id,
                    "",
                    limit=100,
                    branch_id=branch.id,
                    include_main_history=False,
                    include_shared_pre_divergence=False,
                )
            ],
        }
        now = datetime.now(UTC).isoformat()
        for key, value in components.items():
            component_id = new_id("comp")
            self.database.conn.execute(
                """
                INSERT OR REPLACE INTO compiled_components
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    component_id,
                    persona_id,
                    version,
                    "counterfactual_delta",
                    key,
                    dumps({"branch_id": branch.id, "value": value}),
                    dumps([branch.id]),
                    now,
                ),
            )
            self._insert_lineage(
                persona_id,
                child_type="compiled_component",
                child_id=component_id,
                parent_type="continuation_branch",
                parent_id=branch.id,
                relation="compiled_from",
            )
        self.database.conn.commit()

    def _insert_lineage(
        self,
        persona_id: str,
        *,
        child_type: str,
        child_id: str,
        parent_type: str,
        parent_id: str,
        relation: str,
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
                dumps({}),
                datetime.now(UTC).isoformat(),
            ),
        )
