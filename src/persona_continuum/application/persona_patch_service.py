"""Author-directed persona calibration with lineage, without resetting lived state."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from persona_continuum.application._utils import dumps, loads, new_id
from persona_continuum.application.compilation_service import (
    CompilationService,
    _render_markdown_evidence,
)
from persona_continuum.application.memory_service import MemoryService
from persona_continuum.application.persona_service import PersonaService
from persona_continuum.compiler.contract import COMPILE_CONTRACT
from persona_continuum.domain.core_traits import CoreTrait, strength_value
from persona_continuum.domain.memory import MemoryType
from persona_continuum.runtime.motivation_engine import MotivationEngine
from persona_continuum.runtime.persona_seed import build_seed
from persona_continuum.runtime.relationship_engine import RELATIONSHIP_FIELDS, RelationshipEngine
from persona_continuum.security.validation import CodedError
from persona_continuum.storage.database import Database

PatchAction = Literal["replace", "merge"]


class ComponentPatch(BaseModel):
    component_key: str
    action: PatchAction = "replace"
    content: Any
    reason: str = ""


class RuntimeBaselinePatch(BaseModel):
    name: str
    baseline: float | str | None = None
    rebound_rate: float | None = None
    satiation_response: float | None = None
    preserve_current_level: bool = True


class MemoryCorrectionSpec(BaseModel):
    content: str
    reason: str
    memory_type: str = "semantic"
    supersedes_id: str | None = None
    importance: float = 0.9
    participants: list[str] = Field(default_factory=list)


class RelationshipCorrectionSpec(BaseModel):
    counterpart: str
    fields: dict[str, Any]
    reason: str
    preserve_numeric: list[str] = Field(
        default_factory=lambda: [
            "trust",
            "affection",
            "resentment",
            "perceived_threat",
            "unresolved_conflict",
            "familiarity",
            "dependence",
            "respect",
            "jealousy",
        ]
    )


class PreserveFlags(BaseModel):
    episodic_memories: bool = True
    current_affect: bool = True
    current_need_levels: bool = True
    relationship_history: bool = True
    branch_history: bool = True
    sessions: bool = True
    persona_id: bool = True


class PersonaPatch(BaseModel):
    schema_version: str = "1.0"
    persona_id: str
    base_compile_version: int
    base_manifest_version: str | None = None
    target_manifest_version: str | None = None
    patch_reason: str
    provenance_kind: str = "fictional_author_defined"
    provenance_note: str = "author_directed_canonical_correction"
    component_patches: list[ComponentPatch] = Field(default_factory=list)
    runtime_baseline_patches: list[RuntimeBaselinePatch] = Field(default_factory=list)
    memory_corrections: list[MemoryCorrectionSpec] = Field(default_factory=list)
    relationship_corrections: list[RelationshipCorrectionSpec] = Field(default_factory=list)
    preserve: PreserveFlags = Field(default_factory=PreserveFlags)


def snapshot_runtime(
    database: Database,
    persona_id: str,
    branch_id: str = "main",
) -> dict[str, Any]:
    """Read lived runtime rows without applying homeostasis."""
    affect = [
        {
            "name": str(row["name"]),
            "intensity": float(row["intensity"]),
            "baseline": float(row["baseline"]),
        }
        for row in database.conn.execute(
            "SELECT name, intensity, baseline FROM affect_states "
            "WHERE persona_id=? AND branch_id=? ORDER BY name",
            (persona_id, branch_id),
        )
    ]
    needs = [
        {
            "name": str(row["name"]),
            "level": float(row["level"]),
            "baseline": float(row["baseline"]),
        }
        for row in database.conn.execute(
            "SELECT name, level, baseline FROM needs "
            "WHERE persona_id=? AND branch_id=? ORDER BY name",
            (persona_id, branch_id),
        )
    ]
    relationships = [
        loads(row["state_json"])
        for row in database.conn.execute(
            "SELECT state_json FROM relationships WHERE persona_id=? AND branch_id=? "
            "ORDER BY counterpart",
            (persona_id, branch_id),
        )
    ]
    memory_row = database.conn.execute(
        "SELECT COUNT(*) AS n FROM memories WHERE persona_id=?", (persona_id,)
    ).fetchone()
    session_row = database.conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE persona_id=?", (persona_id,)
    ).fetchone()
    turn_row = database.conn.execute(
        """
        SELECT COUNT(*) AS n FROM session_turns
        WHERE persona_id=? OR session_id IN (SELECT id FROM sessions WHERE persona_id=?)
        """,
        (persona_id, persona_id),
    ).fetchone()
    return {
        "persona_id": persona_id,
        "branch_id": branch_id,
        "affect": affect,
        "needs": needs,
        "relationships": relationships,
        "memory_count": int(memory_row["n"] if memory_row else 0),
        "session_count": int(session_row["n"] if session_row else 0),
        "turn_count": int(turn_row["n"] if turn_row else 0),
    }


class PersonaPatchService:
    def __init__(
        self,
        database: Database,
        personas: PersonaService,
        compilation: CompilationService,
        memories: MemoryService,
        motivation: MotivationEngine,
        relationships: RelationshipEngine,
        compiled_context: Any,
    ) -> None:
        self.database = database
        self.personas = personas
        self.compilation = compilation
        self.memories = memories
        self.motivation = motivation
        self.relationships = relationships
        self.compiled_context = compiled_context

    def preview(self, patch: PersonaPatch) -> dict[str, Any]:
        persona = self.personas.get(patch.persona_id)
        base_version = self._latest_compile_version(patch.persona_id)
        if base_version != patch.base_compile_version:
            raise CodedError(
                "patch_base_version_mismatch",
                f"expected {patch.base_compile_version}, latest {base_version}",
            )
        if patch.base_manifest_version and persona.manifest.version != patch.base_manifest_version:
            raise CodedError(
                "patch_manifest_version_mismatch",
                f"expected {patch.base_manifest_version}, latest {persona.manifest.version}",
            )
        current = self._components_at(patch.persona_id, base_version)
        patched = self._apply_component_patches(current, patch)
        self._validate_components(patched)
        target_compile = base_version + 1
        target_manifest = patch.target_manifest_version or self.compilation._bump_version(
            persona.manifest.version
        )
        patched_keys = {item.component_key for item in patch.component_patches}
        return {
            "persona_id": patch.persona_id,
            "current_version": persona.manifest.version,
            "target_version": target_manifest,
            "current_compile_version": base_version,
            "target_compile_version": target_compile,
            "patch_reason": patch.patch_reason,
            "provenance": {
                "kind": patch.provenance_kind,
                "note": patch.provenance_note,
            },
            "components_to_update": sorted(
                key
                for key in patched_keys
                if key in current and current.get(key) != patched.get(key)
            ),
            "components_to_add": sorted(
                key for key in patched_keys if key not in current
            ),
            "components_untouched": sorted(
                key for key in current if key not in patched_keys
            ),
            "trait_changes": self._trait_diff(
                current.get("dominant_traits"), patched.get("dominant_traits")
            ),
            "need_baseline_changes": [
                item.model_dump() for item in patch.runtime_baseline_patches
            ],
            "expression_changes": {
                "current": current.get("expression_style"),
                "target": patched.get("expression_style"),
            },
            "embodied_identity_changes": {
                "current": current.get("embodied_identity"),
                "target": patched.get("embodied_identity"),
            },
            "current_state_preserved": patch.preserve.model_dump(),
            "suspected_corrupted_memories": sum(
                1 for item in patch.memory_corrections if item.supersedes_id
            ),
            "proposed_memory_corrections": len(patch.memory_corrections),
            "relationship_corrections": len(patch.relationship_corrections),
            "runtime_snapshot": snapshot_runtime(self.database, patch.persona_id),
        }

    def apply(self, patch: PersonaPatch) -> dict[str, Any]:
        preview = self.preview(patch)
        before = snapshot_runtime(self.database, patch.persona_id)
        persona = self.personas.get(patch.persona_id)
        base_version = patch.base_compile_version
        target_compile = base_version + 1
        current = self._components_at(patch.persona_id, base_version)
        patched = self._apply_component_patches(current, patch)
        self._validate_components(patched)
        patch_id = new_id("patch")
        now = datetime.now(UTC).isoformat()
        for key, content in patched.items():
            self.database.conn.execute(
                """
                INSERT INTO compiled_components
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("comp"),
                    patch.persona_id,
                    target_compile,
                    "persona_component",
                    key,
                    dumps(content),
                    dumps([patch_id]),
                    now,
                ),
            )
        self._write_package_files(patch.persona_id, patched)
        self._apply_need_baselines(patch)
        seed = build_seed(self.compiled_context.runtime_seed_components(patch.persona_id))
        self._insert_change_event(
            patch.persona_id,
            "main",
            "runtime_profile",
            "runtime",
            "main",
            seed.model_dump(mode="json"),
        )
        correction_ids = self._apply_memory_corrections(patch)
        relationship_after = self._apply_relationship_corrections(patch)
        self._insert_change_event(
            patch.persona_id,
            "main",
            "persona_patch",
            "persona",
            patch.persona_id,
            {
                "patch_id": patch_id,
                "patch": patch.model_dump(mode="json"),
                "before": before,
                "preview": {
                    "current_version": preview["current_version"],
                    "target_version": preview["target_version"],
                    "current_compile_version": preview["current_compile_version"],
                    "target_compile_version": preview["target_compile_version"],
                },
                "memory_correction_ids": correction_ids,
                "relationship_after": relationship_after,
                "provenance": preview["provenance"],
            },
        )
        manifest = persona.manifest
        manifest.version = patch.target_manifest_version or self.compilation._bump_version(
            manifest.version
        )
        manifest.compile_state = "compiled"
        self.personas.update_manifest(manifest)
        self.compilation._insert_snapshot(patch.persona_id, patch_id, target_compile)
        after = snapshot_runtime(self.database, patch.persona_id)
        return {
            "ok": True,
            "patch_id": patch_id,
            "preview": preview,
            "before": before,
            "after": after,
            "memory_correction_ids": correction_ids,
            "continuity": self._continuity(before, after, patch),
        }

    def _latest_compile_version(self, persona_id: str) -> int:
        row = self.database.conn.execute(
            """
            SELECT COALESCE(MAX(version), 0) AS version
            FROM compiled_components
            WHERE persona_id=? AND component_type='persona_component'
            """,
            (persona_id,),
        ).fetchone()
        return int(row["version"] if row and row["version"] is not None else 0)

    def _components_at(self, persona_id: str, version: int) -> dict[str, Any]:
        rows = self.database.conn.execute(
            """
            SELECT component_key, content_json FROM compiled_components
            WHERE persona_id=? AND version=? AND component_type='persona_component'
            """,
            (persona_id, version),
        ).fetchall()
        return {str(row["component_key"]): loads(row["content_json"]) for row in rows}

    def _apply_component_patches(
        self, current: dict[str, Any], patch: PersonaPatch
    ) -> dict[str, Any]:
        result = dict(current)
        for item in patch.component_patches:
            if item.component_key not in COMPILE_CONTRACT:
                raise CodedError("unknown_component_key", item.component_key)
            if item.action == "replace" or item.component_key not in result:
                result[item.component_key] = item.content
                continue
            existing = result[item.component_key]
            if isinstance(existing, dict) and isinstance(item.content, dict):
                result[item.component_key] = {**existing, **item.content}
            elif isinstance(existing, list) and isinstance(item.content, list):
                result[item.component_key] = self._merge_lists(existing, item.content)
            else:
                result[item.component_key] = item.content
        return result

    def _merge_lists(self, existing: list[Any], incoming: list[Any]) -> list[Any]:
        merged = list(existing)
        for item in incoming:
            if not isinstance(item, dict):
                if item not in merged:
                    merged.append(item)
                continue
            name = item.get("name") or item.get("trait") or item.get("need")
            replaced = False
            if name:
                for index, prior in enumerate(merged):
                    if isinstance(prior, dict) and (
                        prior.get("name") == name
                        or prior.get("trait") == name
                        or prior.get("need") == name
                    ):
                        merged[index] = {**prior, **item}
                        replaced = True
                        break
            if not replaced:
                merged.append(item)
        return merged

    def _validate_components(self, components: dict[str, Any]) -> None:
        for key, spec in COMPILE_CONTRACT.items():
            if key not in components:
                continue
            value = components[key]
            if spec.shape == "dict" and not isinstance(value, dict):
                raise CodedError("component_shape_mismatch", f"{key} expected dict")
            if spec.shape == "list" and not isinstance(value, list):
                raise CodedError("component_shape_mismatch", f"{key} expected list")
        for item in components.get("dominant_traits") or []:
            if isinstance(item, dict) and (item.get("trait") or item.get("name")):
                CoreTrait(
                    trait=str(item.get("trait") or item.get("name")),
                    strength=strength_value(item.get("strength")),
                    stability=str(item.get("stability") or "stable"),
                    behavioral_implications=list(item.get("behavioral_implications") or []),
                )

    def _write_package_files(self, persona_id: str, merged: dict[str, Any]) -> None:
        file_map: dict[str, Any] = {
            "identity/profile.json": {
                "identity_profile": merged.get("identity_profile", {}),
                "schema_version": "1.1",
            },
            "identity/timeline.jsonl": merged.get("timeline_events", []),
            "identity/self_narrative.md": _render_markdown_evidence(
                merged.get("self_narrative_evidence", [])
            ),
            "cognition/mental_models.json": merged.get("mental_models", []),
            "cognition/decision_heuristics.json": merged.get("decision_heuristics", []),
            "cognition/values.json": merged.get("values", []),
            "cognition/contradictions.json": merged.get("contradictions", []),
            "cognition/failure_patterns.json": merged.get("failure_patterns", []),
            "affect/temperament.json": merged.get("temperament", {}),
            "affect/emotional_triggers.json": merged.get("emotional_triggers", []),
            "affect/attachment.json": merged.get("attachment_patterns", {}),
            "affect/needs.json": merged.get("needs_and_desires", []),
            "identity/dominant_traits.json": merged.get("dominant_traits", []),
            "identity/embodied_identity.json": merged.get("embodied_identity", {}),
            "affect/defenses.json": merged.get("defenses", []),
            "expression/style.json": merged.get("expression_style", {}),
            "expression/vocabulary.json": merged.get("vocabulary", []),
            "expression/dialogue_examples.jsonl": merged.get("dialogue_examples", []),
            "expression/anti_patterns.json": merged.get("anti_patterns", []),
            "identity/erotic_profile.json": merged.get("erotic_profile", {}),
            "relationships/relationships.json": merged.get("relationships", []),
        }
        root = Path(self.personas.get(persona_id).package_path)
        for relative, value in file_map.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if relative.endswith(".jsonl"):
                entries = value if isinstance(value, list) else [value]
                path.write_text(
                    "\n".join(
                        dumps(item) if not isinstance(item, str) else item for item in entries
                    ),
                    encoding="utf-8",
                )
            elif relative.endswith(".md"):
                path.write_text(str(value), encoding="utf-8")
            else:
                path.write_text(
                    json.dumps(value, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )

    def _apply_need_baselines(self, patch: PersonaPatch) -> None:
        for item in patch.runtime_baseline_patches:
            row = self.database.conn.execute(
                "SELECT * FROM needs WHERE persona_id=? AND branch_id=? AND name=?",
                (patch.persona_id, "main", item.name),
            ).fetchone()
            if row is None:
                continue
            baseline = (
                strength_value(item.baseline)
                if item.baseline is not None
                else float(row["baseline"])
            )
            level = float(row["level"]) if item.preserve_current_level else baseline
            self.database.conn.execute(
                """
                UPDATE needs SET baseline=?, level=?
                WHERE persona_id=? AND branch_id=? AND name=?
                """,
                (baseline, level, patch.persona_id, "main", item.name),
            )

    def _apply_memory_corrections(self, patch: PersonaPatch) -> list[str]:
        ids: list[str] = []
        for spec in patch.memory_corrections:
            if spec.supersedes_id:
                original = self.memories.get_memory(spec.supersedes_id)
                if original is None:
                    raise CodedError("memory_not_found", spec.supersedes_id)
                metadata = dict(original.metadata)
                metadata["superseded_reason"] = spec.reason
                metadata["persona_repair"] = True
                self.database.conn.execute(
                    "UPDATE memories SET validity='superseded', metadata_json=? WHERE id=?",
                    (dumps(metadata), spec.supersedes_id),
                )
                self.database.conn.execute(
                    "DELETE FROM memories_fts WHERE memory_id=?", (spec.supersedes_id,)
                )
            record = self.memories.add_memory(
                patch.persona_id,
                content=spec.content,
                memory_type=MemoryType.from_raw(spec.memory_type),
                importance=spec.importance,
                source_kind="user_correction",
                source_confidence=1.0,
                participants=spec.participants,
                user_corrected=True,
                supersedes_id=spec.supersedes_id,
                metadata={
                    "correction_reason": spec.reason,
                    "provenance": patch.provenance_note,
                    "persona_repair": True,
                    "compile_version": patch.base_compile_version + 1,
                },
                commit=False,
            )
            ids.append(record.id)
        return ids

    def _apply_relationship_corrections(self, patch: PersonaPatch) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for spec in patch.relationship_corrections:
            before = self.relationships.get_relationship(patch.persona_id, spec.counterpart)
            fields = dict(spec.fields)
            for name in spec.preserve_numeric:
                fields.pop(name, None)
                if name in RELATIONSHIP_FIELDS:
                    fields.pop(name, None)
            after = self.relationships.patch_fields(
                patch.persona_id,
                spec.counterpart,
                fields,
                spec.reason,
                commit=False,
            )
            results.append(
                {
                    "counterpart": spec.counterpart,
                    "before": before.model_dump(mode="json"),
                    "after": after.model_dump(mode="json"),
                    "reason": spec.reason,
                }
            )
        return results

    def _insert_change_event(
        self,
        persona_id: str,
        branch_id: str,
        event_type: str,
        target_type: str,
        target_id: str,
        data: dict[str, Any],
    ) -> None:
        self.database.conn.execute(
            "INSERT INTO change_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("evt"),
                persona_id,
                branch_id,
                event_type,
                target_type,
                target_id,
                None,
                None,
                dumps(data),
                datetime.now(UTC).isoformat(),
            ),
        )

    def _trait_diff(self, current: Any, target: Any) -> list[dict[str, Any]]:
        def as_map(value: Any) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for item in value if isinstance(value, list) else []:
                if isinstance(item, dict) and (item.get("trait") or item.get("name")):
                    result[str(item.get("trait") or item.get("name"))] = item
                elif isinstance(item, str):
                    result[item] = {"trait": item}
            return result

        before, after = as_map(current), as_map(target)
        keys = sorted(set(before) | set(after))
        diff = []
        for key in keys:
            if key not in before:
                diff.append(
                    {"trait": key, "action": "ADD", "current": None, "target": after[key]}
                )
            elif key not in after:
                diff.append(
                    {"trait": key, "action": "REMOVE", "current": before[key], "target": None}
                )
            elif before[key] != after[key]:
                diff.append(
                    {
                        "trait": key,
                        "action": "UPDATE",
                        "current": before[key],
                        "target": after[key],
                    }
                )
            else:
                diff.append(
                    {
                        "trait": key,
                        "action": "NO CHANGE",
                        "current": before[key],
                        "target": after[key],
                    }
                )
        return diff

    def _continuity(
        self, before: dict[str, Any], after: dict[str, Any], patch: PersonaPatch
    ) -> dict[str, Any]:
        before_affect = {row["name"]: row["intensity"] for row in before["affect"]}
        after_affect = {row["name"]: row["intensity"] for row in after["affect"]}
        before_levels = {row["name"]: row["level"] for row in before["needs"]}
        after_levels = {row["name"]: row["level"] for row in after["needs"]}
        return {
            "persona_id_same": before["persona_id"] == after["persona_id"] == patch.persona_id,
            "branch_same": before["branch_id"] == after["branch_id"],
            "affect_preserved": before_affect == after_affect,
            "need_current_preserved": before_levels == after_levels,
            "session_count_same": before["session_count"] == after["session_count"],
            "turn_count_same": before["turn_count"] == after["turn_count"],
            "memory_count_before": before["memory_count"],
            "memory_count_after": after["memory_count"],
            "relationship_counterparts_same": [
                row.get("counterpart") for row in before["relationships"]
            ]
            == [row.get("counterpart") for row in after["relationships"]],
        }
