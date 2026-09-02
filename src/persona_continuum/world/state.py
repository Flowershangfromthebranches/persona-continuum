from __future__ import annotations

import copy
from typing import Any

from persona_continuum.world.models import WorldSeed, WorldSnapshot, WorldState

INITIAL_STATE_METADATA_KEYS = (
    "initial_state",
    "initial_entities",
    "initial_organizations",
    "initial_relationships",
    "initial_resources",
    "initial_projects",
    "initial_technologies",
    "initial_environment",
)


class WorldStateManager:
    """Manages WorldState initialization, deep snapshotting, restoration, and structured diffs.

    The engine is domain-agnostic: WorldState starts empty unless the WorldSeed
    (or LLM World Builder metadata) explicitly provides initial conditions. No
    example world (companies, technologies, markets) is ever auto-generated.
    """

    def initialize_state_from_seed(self, seed: WorldSeed) -> WorldState:
        """Initializes the baseline WorldState at `seed.start_date`.

        Initial conditions are read from the backward-compatible WorldSeed
        `initial_*` fields, then from the same keys inside `seed.metadata`
        (older LLM World Builder payloads), then from
        `metadata.entity_classification.non_agent_entities` rows. Missing
        sections stay empty instead of being filled with example content.
        """
        sections: dict[str, dict[str, Any] | list[Any] | None] = {}
        for key in INITIAL_STATE_METADATA_KEYS:
            value = getattr(seed, key, None)
            if value is None:
                value = seed.metadata.get(key)
            sections[key] = value

        extra_state = sections["initial_state"] or {}
        economy: dict[str, Any] = dict(
            extra_state.get("economy", {}) if isinstance(extra_state, dict) else {}
        )
        entities: dict[str, dict[str, Any]] = dict(sections["initial_entities"] or {})
        orgs: dict[str, dict[str, Any]] = dict(sections["initial_organizations"] or {})
        techs: dict[str, dict[str, Any]] = dict(sections["initial_technologies"] or {})
        relationships: dict[str, dict[str, Any]] = dict(
            sections["initial_relationships"] or {}
        )
        resources: dict[str, Any] = dict(sections["initial_resources"] or {})
        active_projects: dict[str, dict[str, Any]] = dict(sections["initial_projects"] or {})

        # Environment rows describe ambient systems (markets, weather, rules of
        # the setting); they are world entities, not autonomous actors.
        environment = sections["initial_environment"]
        if isinstance(environment, dict):
            for env_id, value in environment.items():
                entities.setdefault(
                    str(env_id),
                    value if isinstance(value, dict) else {"value": value},
                )
        elif isinstance(environment, list):
            for row in environment:
                if isinstance(row, dict) and row.get("id"):
                    entities.setdefault(str(row["id"]), dict(row))

        # Extra top-level initial_state sections merge into matching containers
        # so a single `initial_state` blob can describe the whole world.
        if isinstance(extra_state, dict):
            for key, value in extra_state.items():
                if key == "economy":
                    continue
                if key == "organizations" and isinstance(value, dict):
                    orgs.update(value)
                elif key == "technologies" and isinstance(value, dict):
                    techs.update(value)
                elif key == "relationships" and isinstance(value, dict):
                    relationships.update(value)
                elif key == "resources" and isinstance(value, dict):
                    resources.update(value)
                elif key == "active_projects" and isinstance(value, dict):
                    active_projects.update(value)
                elif key == "entities" and isinstance(value, dict):
                    entities.update(value)

        classification = seed.metadata.get("entity_classification", {})
        non_agent_rows = (
            classification.get("non_agent_entities", [])
            if isinstance(classification, dict)
            else []
        )
        for row in non_agent_rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            entity_id = str(row["id"])
            entities.setdefault(
                entity_id,
                {
                    "name": str(row.get("name") or entity_id),
                    "type": str(row.get("subtype") or "world_entity"),
                    "initial_world_role": str(row.get("initial_world_role") or "initial_condition"),
                    "rationale": str(row.get("rationale") or ""),
                    "confidence": float(row.get("confidence") or 0.0),
                },
            )

        return WorldState(
            timestamp=seed.start_date,
            entities=entities,
            organizations=orgs,
            technologies=techs,
            economy=economy,
            relationships=relationships,
            resources=resources,
            active_projects=active_projects,
            events=[],
        )

    def snapshot(self, state: WorldState, world_id: str, branch_id: str) -> WorldSnapshot:
        """Captures an immutable deep copy snapshot of current state."""
        return WorldSnapshot(
            world_id=world_id,
            branch_id=branch_id,
            timestamp=state.timestamp,
            state=copy.deepcopy(state),
        )

    def restore(self, snapshot: WorldSnapshot) -> WorldState:
        """Restores and returns a cloned WorldState from a snapshot."""
        return copy.deepcopy(snapshot.state)

    def diff(self, state_a: WorldState, state_b: WorldState) -> dict[str, Any]:
        """Calculates structured delta between two world states."""
        org_diffs: dict[str, Any] = {}
        for org_id, org_b in state_b.organizations.items():
            if org_id not in state_a.organizations:
                org_diffs[org_id] = {"status": "created", "state": org_b}
            else:
                org_a = state_a.organizations[org_id]
                delta: dict[str, Any] = {}
                for k, v in org_b.items():
                    if org_a.get(k) != v:
                        delta[k] = {"before": org_a.get(k), "after": v}
                if delta:
                    org_diffs[org_id] = delta

        project_diffs: dict[str, Any] = {}
        for p_id, p_b in state_b.active_projects.items():
            if p_id not in state_a.active_projects:
                project_diffs[p_id] = {"status": "created", "details": p_b}
            else:
                p_a = state_a.active_projects[p_id]
                if p_a != p_b:
                    project_diffs[p_id] = {
                        "progress_delta": p_b.get("progress_percent", 0)
                        - p_a.get("progress_percent", 0),
                        "status_before": p_a.get("status"),
                        "status_after": p_b.get("status"),
                    }

        new_events = [e for e in state_b.events if e not in state_a.events]

        return {
            "from_timestamp": state_a.timestamp,
            "to_timestamp": state_b.timestamp,
            "organization_changes": org_diffs,
            "project_changes": project_diffs,
            "new_event_count": len(new_events),
            "new_events": new_events,
        }
