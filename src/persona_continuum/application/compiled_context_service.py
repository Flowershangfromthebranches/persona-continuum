from __future__ import annotations

import json
from typing import Any

from persona_continuum.application._utils import loads
from persona_continuum.storage.database import Database


class CompiledPersonaContextService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def prepare_context(
        self,
        persona_id: str,
        query: str,
        *,
        max_items: int = 12,
        max_context_size: int | None = None,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        base_version = self._latest_base_version(persona_id)
        effective_branch_id = branch_id or self._current_main_branch(persona_id)
        rows = []
        if base_version is not None:
            rows.extend(
                self.database.conn.execute(
                    """
                    SELECT id, version, component_type, component_key, content_json,
                           source_artifact_ids_json, created_at
                    FROM compiled_components
                    WHERE persona_id = ?
                      AND version = ?
                      AND component_type = 'persona_component'
                    ORDER BY component_key
                    """,
                    (persona_id, base_version),
                ).fetchall()
            )
        if effective_branch_id:
            rows.extend(
                self.database.conn.execute(
                    """
                    SELECT id, version, component_type, component_key, content_json,
                           source_artifact_ids_json, created_at
                    FROM compiled_components
                    WHERE persona_id = ?
                      AND component_type = 'counterfactual_delta'
                    ORDER BY version DESC, component_key
                    """,
                    (persona_id,),
                ).fetchall()
            )
        if not rows:
            return {
                "version": None,
                "runtime_version": {
                    "base_compile_version": None,
                    "continuation_versions": [],
                    "active_branch_id": effective_branch_id,
                    "digital_runtime_revision": None,
                },
                "components": [],
                "by_key": {},
                "active_goals": self._runtime_active_goals(
                    persona_id, branch_id=effective_branch_id
                ),
                "runtime": self._runtime_state(persona_id, effective_branch_id),
            }
        features = self._features(query)
        scored: list[tuple[float, dict[str, Any]]] = []
        selected_versions: set[int] = set()
        for row in rows:
            content = loads(row["content_json"])
            if not self._branch_allowed(content, effective_branch_id):
                continue
            component = {
                "component_id": str(row["id"]),
                "component_key": str(row["component_key"]),
                "component_type": str(row["component_type"]),
                "content": content,
                "source_artifact_ids": list(loads(row["source_artifact_ids_json"])),
                "version": int(row["version"]),
            }
            selected_versions.add(int(row["version"]))
            score = self._score(str(row["component_key"]), content, features)
            component["score"] = round(score, 6)
            scored.append((score, component))
        scored.sort(key=lambda item: (item[0], item[1]["component_key"]), reverse=True)
        selected = [component for _, component in scored[:max_items]]
        selected_ids = {str(component["component_id"]) for component in selected}
        priority_keys = {
            "values",
            "needs_and_desires",
            "decision_heuristics",
            "expression_style",
            "branch_provenance",
            "persona_state_delta",
            "relationship_delta",
            "counterfactual_memories",
        }
        for _, component in scored:
            if component["component_key"] not in priority_keys:
                continue
            if str(component["component_id"]) in selected_ids:
                continue
            selected.append(component)
            selected_ids.add(str(component["component_id"]))
        if max_context_size is not None:
            selected = self._fit_budget(selected, max_context_size)
        by_key = {str(component["component_key"]): component["content"] for component in selected}
        runtime_state = self._runtime_state(persona_id, effective_branch_id)
        active_goals = self._active_goals(by_key)
        active_goals.extend(
            self._runtime_active_goals(
                persona_id, runtime_state, branch_id=effective_branch_id
            )
        )
        active_goals = self._dedupe(active_goals)[:8]
        latest_version = max(selected_versions) if selected_versions else base_version
        return {
            "version": latest_version,
            "runtime_version": {
                "base_compile_version": base_version,
                "continuation_versions": sorted(
                    version for version in selected_versions if version != base_version
                ),
                "active_branch_id": effective_branch_id,
                "digital_runtime_revision": runtime_state.get("revision"),
            },
            "components": selected,
            "by_key": by_key,
            "active_goals": active_goals,
            "runtime": runtime_state,
        }

    def _latest_base_version(self, persona_id: str) -> int | None:
        row = self.database.conn.execute(
            """
            SELECT MAX(version) AS version
            FROM compiled_components
            WHERE persona_id = ? AND component_type = 'persona_component'
            """,
            (persona_id,),
        ).fetchone()
        if row is None or row["version"] is None:
            return None
        return int(row["version"])

    def _current_main_branch(self, persona_id: str) -> str | None:
        row = self.database.conn.execute(
            "SELECT manifest_json FROM personas WHERE id = ?", (persona_id,)
        ).fetchone()
        if row is None:
            return None
        manifest = loads(row["manifest_json"])
        value = manifest.get("current_main_branch") if isinstance(manifest, dict) else None
        return str(value) if value else None

    def _features(self, query: str) -> set[str]:
        query = query.strip().lower()
        if not query:
            return set()
        features = {query}
        for token in query.replace("\n", " ").split():
            cleaned = token.strip(".,!?;:，。！？；：()[]{}\"'").lower()
            if cleaned:
                features.add(cleaned)
        compact = "".join(ch for ch in query if not ch.isspace())
        if any("\u4e00" <= char <= "\u9fff" for char in compact):
            for size in (2, 3):
                for index in range(0, max(0, len(compact) - size + 1)):
                    features.add(compact[index : index + size])
        return features

    def _score(self, key: str, content: Any, features: set[str]) -> float:
        text = f"{key} {json.dumps(content, ensure_ascii=False, default=str)}".lower()
        if not features:
            return 0.1
        matched = sum(1 for feature in features if feature in text)
        base = matched / max(1, min(len(features), 8))
        type_boost = 0.2 if key in {"values", "needs_and_desires", "decision_heuristics"} else 0.0
        return min(1.0, base + type_boost)

    def _fit_budget(
        self, components: list[dict[str, Any]], max_context_size: int
    ) -> list[dict[str, Any]]:
        selected = []
        used = 0
        for component in components:
            size = len(json.dumps(component, ensure_ascii=False, default=str))
            if size > max_context_size:
                continue
            if used + size > max_context_size:
                break
            selected.append(component)
            used += size
        return selected

    def _active_goals(self, by_key: dict[str, Any]) -> list[str]:
        goals: list[str] = []
        for key in ("needs_and_desires", "values", "decision_heuristics"):
            value = by_key.get(key)
            if isinstance(value, list):
                goals.extend(str(item) for item in value[:4])
            elif value:
                goals.append(str(value))
        deduped: list[str] = []
        seen: set[str] = set()
        for goal in goals:
            if goal in seen:
                continue
            seen.add(goal)
            deduped.append(goal)
        return deduped[:8]

    def _runtime_state(self, persona_id: str, branch_id: str | None = None) -> dict[str, Any]:
        root_row = self.database.conn.execute(
            "SELECT package_path FROM personas WHERE id = ?", (persona_id,)
        ).fetchone()
        if root_row is None:
            return {}
        from pathlib import Path

        effective_branch_id = branch_id or self._current_main_branch(persona_id) or "main"
        path = (
            Path(str(root_row["package_path"]))
            / "runtime"
            / "branches"
            / effective_branch_id
            / "runtime_state.json"
        )
        if not path.exists():
            return {}
        try:
            value = loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return dict(value) if isinstance(value, dict) else {}

    def _runtime_active_goals(
        self,
        persona_id: str,
        runtime_state: dict[str, Any] | None = None,
        *,
        branch_id: str | None = None,
    ) -> list[str]:
        state = (
            runtime_state
            if runtime_state is not None
            else self._runtime_state(persona_id, branch_id)
        )
        goals: list[str] = []
        for item in list(state.get("active_goals", [])):
            if isinstance(item, dict):
                label = item.get("label") or item.get("goal_id") or item.get("content")
                if label:
                    goals.append(str(label))
            elif item:
                goals.append(str(item))
        return goals

    def _dedupe(self, values: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            deduped.append(value)
        return deduped

    def _branch_allowed(self, content: Any, branch_id: str | None) -> bool:
        if not isinstance(content, dict):
            return True
        content_branch = content.get("branch_id")
        if content_branch is None:
            return True
        if branch_id is None:
            return False
        return content_branch in {branch_id, "main", "shared_pre_divergence"}
