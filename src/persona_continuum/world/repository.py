from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any

from persona_continuum.application._utils import dumps, loads, parse_dt
from persona_continuum.storage.database import Database
from persona_continuum.world.models import (
    Actor,
    CausalEdge,
    CausalGraph,
    CausalNode,
    CausalNodeType,
    CausalRelationType,
    MemoryCategory,
    OrganizationEntity,
    OutcomeEvaluation,
    ReplayStep,
    SimulationBranch,
    TechnologyEntity,
    TimelineEvent,
    WorldDirectorPolicy,
    WorldMemoryRecord,
    WorldRecord,
    WorldRuntimeBinding,
    WorldSeed,
    WorldSnapshot,
    WorldState,
)


def _parse_dt(val: Any) -> datetime:
    parsed = parse_dt(val)
    return parsed if parsed is not None else datetime.now(UTC)


class WorldRepository:
    """Persistent storage for parallel worlds, branches, snapshots, events,

    directors, causal graphs, organizations, technologies, memories, replays, and runtime bindings.
    """

    def __init__(self, database: Database) -> None:
        self.db = database

    # ─── 1. World Record ───────────────────────────────────────────────────
    def save_world(self, world: WorldRecord) -> None:
        standalone = not self.db.conn.in_transaction
        now = datetime.now(UTC).isoformat()
        self.db.conn.execute(
            """
            INSERT INTO worlds (
              id, title, description, seed_json, status,
              builder_runtime_json, default_actor_runtime_json,
              director_runtime_json, evaluator_runtime_json,
              created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              title = excluded.title,
              description = excluded.description,
              seed_json = excluded.seed_json,
              status = excluded.status,
              builder_runtime_json = excluded.builder_runtime_json,
              default_actor_runtime_json = excluded.default_actor_runtime_json,
              director_runtime_json = excluded.director_runtime_json,
              evaluator_runtime_json = excluded.evaluator_runtime_json,
              updated_at = excluded.updated_at
            """,
            (
                world.id,
                world.title,
                world.description,
                dumps(world.seed.model_dump()),
                world.status,
                dumps(world.builder_runtime),
                dumps(world.default_actor_runtime),
                dumps(world.director_runtime),
                dumps(world.evaluator_runtime),
                world.created_at.isoformat(),
                now,
            ),
        )
        # A World-tick transaction owns the commit; standalone writes commit here.
        if standalone:
            self.db.conn.commit()

    def get_world(self, world_id: str) -> WorldRecord | None:
        query = (
            "SELECT id, title, description, seed_json, status, "
            "builder_runtime_json, default_actor_runtime_json, "
            "director_runtime_json, evaluator_runtime_json, "
            "created_at, updated_at "
            "FROM worlds WHERE id = ?"
        )
        row = self.db.conn.execute(query, (world_id,)).fetchone()
        if not row:
            return None
        return WorldRecord(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            seed=WorldSeed(**loads(row["seed_json"])),
            status=row["status"],
            builder_runtime=(
                loads(row["builder_runtime_json"])
                if "builder_runtime_json" in row and row["builder_runtime_json"]
                else {}
            ),
            default_actor_runtime=(
                loads(row["default_actor_runtime_json"])
                if "default_actor_runtime_json" in row and row["default_actor_runtime_json"]
                else {}
            ),
            director_runtime=(
                loads(row["director_runtime_json"])
                if "director_runtime_json" in row and row["director_runtime_json"]
                else {}
            ),
            evaluator_runtime=(
                loads(row["evaluator_runtime_json"])
                if "evaluator_runtime_json" in row and row["evaluator_runtime_json"]
                else {}
            ),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )

    def list_worlds(self) -> list[WorldRecord]:
        query = (
            "SELECT id, title, description, seed_json, status, "
            "builder_runtime_json, default_actor_runtime_json, "
            "director_runtime_json, evaluator_runtime_json, "
            "created_at, updated_at "
            "FROM worlds ORDER BY created_at DESC"
        )
        rows = self.db.conn.execute(query).fetchall()
        return [
            WorldRecord(
                id=r["id"],
                title=r["title"],
                description=r["description"],
                seed=WorldSeed(**loads(r["seed_json"])),
                status=r["status"],
                builder_runtime=(
                    loads(r["builder_runtime_json"])
                    if "builder_runtime_json" in r and r["builder_runtime_json"]
                    else {}
                ),
                default_actor_runtime=(
                    loads(r["default_actor_runtime_json"])
                    if "default_actor_runtime_json" in r and r["default_actor_runtime_json"]
                    else {}
                ),
                director_runtime=(
                    loads(r["director_runtime_json"])
                    if "director_runtime_json" in r and r["director_runtime_json"]
                    else {}
                ),
                evaluator_runtime=(
                    loads(r["evaluator_runtime_json"])
                    if "evaluator_runtime_json" in r and r["evaluator_runtime_json"]
                    else {}
                ),
                created_at=_parse_dt(r["created_at"]),
                updated_at=_parse_dt(r["updated_at"]),
            )
            for r in rows
        ]

    def delete_world(self, world_id: str) -> bool:
        tables = [
            "world_runtime_bindings",
            "technologies",
            "organizations",
            "replay_snapshots",
            "world_memories",
            "causal_edges",
            "causal_nodes",
            "world_directors",
            "world_simulation_runs",
            "world_scenes",
            "world_actor_states",
            "world_timeline_events",
            "world_snapshots",
            "world_branches",
            "worlds",
        ]
        with self.db.conn:
            for tbl in tables:
                with contextlib.suppress(Exception):
                    self.db.conn.execute(f"DELETE FROM {tbl} WHERE world_id = ?", (world_id,))
            cur = self.db.conn.execute("DELETE FROM worlds WHERE id = ?", (world_id,))
        return bool(cur.rowcount > 0)

    # ─── 1.1 Runtime Bindings ──────────────────────────────────────────────
    def save_runtime_bindings(self, world_id: str, bindings: list[WorldRuntimeBinding]) -> None:
        standalone = not self.db.conn.in_transaction
        # `with conn` commits on exit, so it is only used for standalone
        # writes; inside a World-tick transaction the caller owns the commit.
        context_manager = self.db.conn if standalone else contextlib.nullcontext()
        with context_manager:
            for b in bindings:
                self.db.conn.execute(
                    """
                    INSERT INTO world_runtime_bindings (
                      world_id, actor_id, persona_id, profile_id, profile_type, runtime_source,
                      agent_id, model_id, reasoning_effort, auth_profile_id,
                      resolved_at, capability_snapshot_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(world_id, actor_id) DO UPDATE SET
                      persona_id = excluded.persona_id,
                      profile_id = excluded.profile_id,
                      profile_type = excluded.profile_type,
                      runtime_source = excluded.runtime_source,
                      agent_id = excluded.agent_id,
                      model_id = excluded.model_id,
                      reasoning_effort = excluded.reasoning_effort,
                      auth_profile_id = excluded.auth_profile_id,
                      resolved_at = excluded.resolved_at,
                      capability_snapshot_json = excluded.capability_snapshot_json
                    """,
                    (
                        world_id,
                        b.actor_id,
                        b.persona_id,
                        b.profile_id,
                        b.profile_type,
                        b.runtime_source,
                        b.agent_id,
                        b.model_id,
                        b.reasoning_effort,
                        b.auth_profile_id,
                        b.resolved_at,
                        dumps(b.capability_snapshot),
                    ),
                )

    def list_runtime_bindings(self, world_id: str) -> list[WorldRuntimeBinding]:
        rows = self.db.conn.execute(
            "SELECT * FROM world_runtime_bindings WHERE world_id = ?", (world_id,)
        ).fetchall()
        return [
            WorldRuntimeBinding(
                world_id=r["world_id"],
                actor_id=r["actor_id"],
                persona_id=r["persona_id"],
                profile_id=dict(r).get("profile_id"),
                profile_type=dict(r).get("profile_type"),
                runtime_source=r["runtime_source"],
                agent_id=r["agent_id"],
                model_id=r["model_id"],
                reasoning_effort=r["reasoning_effort"],
                auth_profile_id=r["auth_profile_id"],
                resolved_at=r["resolved_at"],
                capability_snapshot=(
                    loads(r["capability_snapshot_json"]) if r["capability_snapshot_json"] else {}
                ),
            )
            for r in rows
        ]

    def get_runtime_binding(self, world_id: str, actor_id: str) -> WorldRuntimeBinding | None:
        row = self.db.conn.execute(
            "SELECT * FROM world_runtime_bindings WHERE world_id = ? AND actor_id = ?",
            (world_id, actor_id),
        ).fetchone()
        if not row:
            return None
        return WorldRuntimeBinding(
            world_id=row["world_id"],
            actor_id=row["actor_id"],
            persona_id=row["persona_id"],
            profile_id=dict(row).get("profile_id"),
            profile_type=dict(row).get("profile_type"),
            runtime_source=row["runtime_source"],
            agent_id=row["agent_id"],
            model_id=row["model_id"],
            reasoning_effort=row["reasoning_effort"],
            auth_profile_id=row["auth_profile_id"],
            resolved_at=row["resolved_at"],
            capability_snapshot=(
                loads(row["capability_snapshot_json"]) if row["capability_snapshot_json"] else {}
            ),
        )

    # ─── 2. Simulation Branches ────────────────────────────────────────────
    def save_branch(self, branch: SimulationBranch) -> None:
        standalone = not self.db.conn.in_transaction
        now = datetime.now(UTC).isoformat()
        self.db.conn.execute(
            """
            INSERT INTO world_branches (
              id, world_id, parent_branch_id, parent_snapshot_id,
              name, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              name = excluded.name,
              status = excluded.status,
              updated_at = excluded.updated_at
            """,
            (
                branch.id,
                branch.world_id,
                branch.parent_branch_id,
                branch.parent_snapshot_id,
                branch.name,
                branch.status,
                branch.created_at.isoformat(),
                now,
            ),
        )
        # A World-tick transaction owns the commit; standalone writes commit here.
        if standalone:
            self.db.conn.commit()

    def get_branch(self, branch_id: str) -> SimulationBranch | None:
        query = (
            "SELECT id, world_id, parent_branch_id, parent_snapshot_id, name, status, "
            "created_at, updated_at FROM world_branches WHERE id = ?"
        )
        row = self.db.conn.execute(query, (branch_id,)).fetchone()
        if not row:
            return None
        snap_query = (
            "SELECT state_json FROM world_snapshots WHERE branch_id = ? "
            "ORDER BY created_at DESC LIMIT 1"
        )
        snap_row = self.db.conn.execute(snap_query, (branch_id,)).fetchone()
        current_state = WorldState(**loads(snap_row["state_json"])) if snap_row else None

        return SimulationBranch(
            id=row["id"],
            world_id=row["world_id"],
            parent_branch_id=row["parent_branch_id"],
            parent_snapshot_id=row["parent_snapshot_id"],
            name=row["name"],
            status=row["status"],
            current_state=current_state,
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )

    def list_branches(self, world_id: str) -> list[SimulationBranch]:
        query = (
            "SELECT id, world_id, parent_branch_id, parent_snapshot_id, name, status, "
            "created_at, updated_at FROM world_branches WHERE world_id = ? ORDER BY created_at ASC"
        )
        rows = self.db.conn.execute(query, (world_id,)).fetchall()
        branches: list[SimulationBranch] = []
        for r in rows:
            snap_query = (
                "SELECT state_json FROM world_snapshots WHERE branch_id = ? "
                "ORDER BY created_at DESC LIMIT 1"
            )
            snap_row = self.db.conn.execute(snap_query, (r["id"],)).fetchone()
            current_state = WorldState(**loads(snap_row["state_json"])) if snap_row else None
            branches.append(
                SimulationBranch(
                    id=r["id"],
                    world_id=r["world_id"],
                    parent_branch_id=r["parent_branch_id"],
                    parent_snapshot_id=r["parent_snapshot_id"],
                    name=r["name"],
                    status=r["status"],
                    current_state=current_state,
                    created_at=_parse_dt(r["created_at"]),
                    updated_at=_parse_dt(r["updated_at"]),
                )
            )
        return branches

    # ─── 3. World Snapshots ────────────────────────────────────────────────
    def save_snapshot(self, snapshot: WorldSnapshot) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO world_snapshots (
              id, world_id, branch_id, timestamp, state_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.id,
                snapshot.world_id,
                snapshot.branch_id,
                snapshot.timestamp,
                dumps(snapshot.state.model_dump()),
                snapshot.created_at.isoformat(),
            ),
        )
        # A World-tick transaction owns the commit; standalone writes commit here.
        if standalone:
            self.db.conn.commit()

    def get_snapshot(self, snapshot_id: str) -> WorldSnapshot | None:
        query = (
            "SELECT id, world_id, branch_id, timestamp, state_json, created_at "
            "FROM world_snapshots WHERE id = ?"
        )
        row = self.db.conn.execute(query, (snapshot_id,)).fetchone()
        if not row:
            return None
        return WorldSnapshot(
            id=row["id"],
            world_id=row["world_id"],
            branch_id=row["branch_id"],
            timestamp=row["timestamp"],
            state=WorldState(**loads(row["state_json"])),
            created_at=_parse_dt(row["created_at"]),
        )

    def list_snapshots(self, world_id: str, branch_id: str) -> list[WorldSnapshot]:
        query = (
            "SELECT id, world_id, branch_id, timestamp, state_json, created_at "
            "FROM world_snapshots WHERE world_id = ? AND branch_id = ? ORDER BY created_at ASC"
        )
        rows = self.db.conn.execute(query, (world_id, branch_id)).fetchall()
        return [
            WorldSnapshot(
                id=r["id"],
                world_id=r["world_id"],
                branch_id=r["branch_id"],
                timestamp=r["timestamp"],
                state=WorldState(**loads(r["state_json"])),
                created_at=_parse_dt(r["created_at"]),
            )
            for r in rows
        ]

    # ─── 4. Timeline Events ────────────────────────────────────────────────
    def save_event(self, world_id: str, branch_id: str, event: TimelineEvent) -> None:
        standalone = not self.db.conn.in_transaction
        now = datetime.now(UTC).isoformat()
        self.db.conn.execute(
            """
            INSERT INTO world_timeline_events (
              id, world_id, branch_id, event_time, actors_json, cause, effect,
              causal_chain_json, confidence, data_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                event.id,
                world_id,
                branch_id,
                event.event_time,
                dumps(event.actors),
                event.cause,
                event.effect,
                dumps(event.causal_chain),
                event.confidence,
                dumps(event.data),
                now,
            ),
        )
        # A World-tick transaction owns the commit; standalone writes commit here.
        if standalone:
            self.db.conn.commit()

    def list_events(self, world_id: str, branch_id: str) -> list[TimelineEvent]:
        query = (
            "SELECT id, event_time, actors_json, cause, effect, causal_chain_json, "
            "confidence, data_json FROM world_timeline_events "
            "WHERE world_id = ? AND branch_id = ? ORDER BY event_time ASC"
        )
        rows = self.db.conn.execute(query, (world_id, branch_id)).fetchall()
        return [
            TimelineEvent(
                id=r["id"],
                event_time=r["event_time"],
                actors=loads(r["actors_json"]),
                cause=r["cause"],
                effect=r["effect"],
                causal_chain=loads(r["causal_chain_json"]),
                confidence=float(r["confidence"]),
                data=loads(r["data_json"]),
            )
            for r in rows
        ]

    # ─── 5. Actors ─────────────────────────────────────────────────────────
    def save_actor_state(self, world_id: str, branch_id: str, actor: Actor) -> None:
        standalone = not self.db.conn.in_transaction
        now = datetime.now(UTC).isoformat()
        self.db.conn.execute(
            """
            INSERT INTO world_actor_states (
              id, world_id, branch_id, actor_id, state_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(world_id, branch_id, actor_id) DO UPDATE SET
              state_json = excluded.state_json,
              updated_at = excluded.updated_at
            """,
            (
                f"{world_id}:{branch_id}:{actor.id}",
                world_id,
                branch_id,
                actor.id,
                dumps(actor.model_dump()),
                now,
            ),
        )
        # A World-tick transaction owns the commit; standalone writes commit here.
        if standalone:
            self.db.conn.commit()

    def list_actor_states(self, world_id: str, branch_id: str) -> list[Actor]:
        rows = self.db.conn.execute(
            "SELECT state_json FROM world_actor_states WHERE world_id = ? AND branch_id = ?",
            (world_id, branch_id),
        ).fetchall()
        return [Actor(**loads(r["state_json"])) for r in rows]

    # ─── 6. Director & Policy ──────────────────────────────────────────────
    def save_director(
        self,
        world_id: str,
        branch_id: str,
        policy: WorldDirectorPolicy,
        current_speed: str = "month",
        status: str = "active",
    ) -> None:
        standalone = not self.db.conn.in_transaction
        now = datetime.now(UTC).isoformat()
        self.db.conn.execute(
            """
            INSERT INTO world_directors (
              id, world_id, branch_id, policy_json, current_speed, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(world_id, branch_id) DO UPDATE SET
              policy_json = excluded.policy_json,
              current_speed = excluded.current_speed,
              status = excluded.status,
              updated_at = excluded.updated_at
            """,
            (
                f"{world_id}:{branch_id}",
                world_id,
                branch_id,
                dumps(policy.model_dump()),
                current_speed,
                status,
                now,
                now,
            ),
        )
        # A World-tick transaction owns the commit; standalone writes commit here.
        if standalone:
            self.db.conn.commit()

    def get_director(self, world_id: str, branch_id: str) -> tuple[WorldDirectorPolicy, str] | None:
        row = self.db.conn.execute(
            """
            SELECT policy_json, current_speed
            FROM world_directors
            WHERE world_id = ? AND branch_id = ?
            """,
            (world_id, branch_id),
        ).fetchone()
        if not row:
            return None
        return WorldDirectorPolicy.model_validate(loads(row["policy_json"])), row["current_speed"]

    # ─── 7. Causal Graph Nodes & Edges ─────────────────────────────────────
    def save_causal_graph(self, world_id: str, branch_id: str, graph: CausalGraph) -> None:
        standalone = not self.db.conn.in_transaction
        now = datetime.now(UTC).isoformat()
        for node in graph.nodes.values():
            self.db.conn.execute(
                """
                INSERT INTO causal_nodes (
                  id, world_id, branch_id, node_type, name, timestamp, properties_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    node.id,
                    world_id,
                    branch_id,
                    str(node.node_type),
                    node.name,
                    node.timestamp,
                    dumps(node.properties),
                    now,
                ),
            )
        for edge in graph.edges:
            self.db.conn.execute(
                """
                INSERT INTO causal_edges (
                  id, world_id, branch_id, source_id, target_id, relation_type,
                  weight, properties_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    edge.id,
                    world_id,
                    branch_id,
                    edge.source_id,
                    edge.target_id,
                    str(edge.relation_type),
                    edge.weight,
                    dumps(edge.properties),
                    now,
                ),
            )
        # A World-tick transaction owns the commit; standalone writes commit here.
        if standalone:
            self.db.conn.commit()

    def get_causal_graph(self, world_id: str, branch_id: str) -> CausalGraph:
        n_rows = self.db.conn.execute(
            """
            SELECT id, node_type, name, timestamp, properties_json
            FROM causal_nodes
            WHERE world_id = ? AND branch_id = ?
            """,
            (world_id, branch_id),
        ).fetchall()
        nodes: dict[str, CausalNode] = {}
        for r in n_rows:
            nodes[r["id"]] = CausalNode(
                id=r["id"],
                node_type=CausalNodeType(r["node_type"]),
                name=r["name"],
                timestamp=r["timestamp"],
                properties=loads(r["properties_json"]),
            )

        e_rows = self.db.conn.execute(
            """
            SELECT id, source_id, target_id, relation_type, weight, properties_json
            FROM causal_edges
            WHERE world_id = ? AND branch_id = ?
            """,
            (world_id, branch_id),
        ).fetchall()
        edges: list[CausalEdge] = [
            CausalEdge(
                id=r["id"],
                source_id=r["source_id"],
                target_id=r["target_id"],
                relation_type=CausalRelationType(r["relation_type"]),
                weight=float(r["weight"]),
                properties=loads(r["properties_json"]),
            )
            for r in e_rows
        ]
        return CausalGraph(nodes=nodes, edges=edges)

    # ─── 8. Organizations ──────────────────────────────────────────────────
    def save_organization(self, world_id: str, branch_id: str, org: OrganizationEntity) -> None:
        standalone = not self.db.conn.in_transaction
        now = datetime.now(UTC).isoformat()
        self.db.conn.execute(
            """
            INSERT INTO organizations (
              id, world_id, branch_id, name, leadership_json, departments_json,
              employees_count, budget_billions, cash_reserves_billions, technology_json,
              projects_json, strategy_json, culture_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(world_id, branch_id, id) DO UPDATE SET
              leadership_json = excluded.leadership_json,
              departments_json = excluded.departments_json,
              employees_count = excluded.employees_count,
              budget_billions = excluded.budget_billions,
              cash_reserves_billions = excluded.cash_reserves_billions,
              technology_json = excluded.technology_json,
              projects_json = excluded.projects_json,
              strategy_json = excluded.strategy_json,
              culture_json = excluded.culture_json,
              updated_at = excluded.updated_at
            """,
            (
                org.id,
                world_id,
                branch_id,
                org.name,
                dumps(org.leadership),
                dumps([d.model_dump() for d in org.departments]),
                org.employees_count,
                org.budget_billions,
                org.cash_reserves_billions,
                dumps(org.technology),
                dumps(org.projects),
                dumps(org.strategy.model_dump()),
                dumps(org.culture),
                now,
            ),
        )
        # A World-tick transaction owns the commit; standalone writes commit here.
        if standalone:
            self.db.conn.commit()

    def list_organizations(self, world_id: str, branch_id: str) -> dict[str, OrganizationEntity]:
        rows = self.db.conn.execute(
            """
            SELECT id, name, leadership_json, departments_json, employees_count,
                   budget_billions, cash_reserves_billions, technology_json,
                   projects_json, strategy_json, culture_json
            FROM organizations
            WHERE world_id = ? AND branch_id = ?
            """,
            (world_id, branch_id),
        ).fetchall()
        orgs: dict[str, OrganizationEntity] = {}
        for r in rows:
            orgs[r["id"]] = OrganizationEntity(
                id=r["id"],
                name=r["name"],
                leadership=loads(r["leadership_json"]),
                departments=loads(r["departments_json"]),
                employees_count=r["employees_count"],
                budget_billions=r["budget_billions"],
                cash_reserves_billions=r["cash_reserves_billions"],
                technology=loads(r["technology_json"]),
                projects=loads(r["projects_json"]),
                strategy=loads(r["strategy_json"]),
                culture=loads(r["culture_json"]),
            )
        return orgs

    # ─── 9. Technologies ───────────────────────────────────────────────────
    def save_technology(self, world_id: str, branch_id: str, tech: TechnologyEntity) -> None:
        standalone = not self.db.conn.in_transaction
        now = datetime.now(UTC).isoformat()
        self.db.conn.execute(
            """
            INSERT INTO technologies (
              id, world_id, branch_id, name, maturity, cost, performance, adoption,
              dependencies_json, lead_org_id, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(world_id, branch_id, id) DO UPDATE SET
              maturity = excluded.maturity,
              cost = excluded.cost,
              performance = excluded.performance,
              adoption = excluded.adoption,
              dependencies_json = excluded.dependencies_json,
              lead_org_id = excluded.lead_org_id,
              updated_at = excluded.updated_at
            """,
            (
                tech.id,
                world_id,
                branch_id,
                tech.name,
                tech.maturity,
                tech.cost,
                tech.performance,
                tech.adoption,
                dumps([d.model_dump() for d in tech.dependencies]),
                tech.lead_org_id,
                now,
            ),
        )
        # A World-tick transaction owns the commit; standalone writes commit here.
        if standalone:
            self.db.conn.commit()

    def list_technologies(self, world_id: str, branch_id: str) -> dict[str, TechnologyEntity]:
        rows = self.db.conn.execute(
            """
            SELECT id, name, maturity, cost, performance, adoption,
                   dependencies_json, lead_org_id
            FROM technologies
            WHERE world_id = ? AND branch_id = ?
            """,
            (world_id, branch_id),
        ).fetchall()
        techs: dict[str, TechnologyEntity] = {}
        for r in rows:
            techs[r["id"]] = TechnologyEntity(
                id=r["id"],
                name=r["name"],
                maturity=r["maturity"],
                cost=r["cost"],
                performance=r["performance"],
                adoption=r["adoption"],
                dependencies=loads(r["dependencies_json"]),
                lead_org_id=r["lead_org_id"],
            )
        return techs

    # ─── 10. World Memories ────────────────────────────────────────────────
    def save_world_memory(self, world_id: str, branch_id: str, memory: WorldMemoryRecord) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO world_memories (
              id, world_id, branch_id, persona_id, memory_type, content, occurred_at,
              written_at, importance, emotional_valence, source_event_id, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                memory.id,
                world_id,
                branch_id,
                memory.persona_id,
                str(memory.memory_type),
                memory.content,
                memory.occurred_at,
                memory.written_at,
                memory.importance,
                memory.emotional_valence,
                memory.source_event_id,
                dumps(memory.metadata),
            ),
        )
        # A World-tick transaction owns the commit; standalone writes commit here.
        if standalone:
            self.db.conn.commit()

    def list_world_memories(
        self, world_id: str, branch_id: str, persona_id: str | None = None
    ) -> list[WorldMemoryRecord]:
        if persona_id:
            rows = self.db.conn.execute(
                """
                SELECT id, persona_id, memory_type, content, occurred_at, written_at,
                       importance, emotional_valence, source_event_id, metadata_json
                FROM world_memories
                WHERE world_id = ? AND branch_id = ? AND persona_id = ?
                ORDER BY written_at ASC
                """,
                (world_id, branch_id, persona_id),
            ).fetchall()
        else:
            rows = self.db.conn.execute(
                """
                SELECT id, persona_id, memory_type, content, occurred_at, written_at,
                       importance, emotional_valence, source_event_id, metadata_json
                FROM world_memories
                WHERE world_id = ? AND branch_id = ?
                ORDER BY written_at ASC
                """,
                (world_id, branch_id),
            ).fetchall()
        return [
            WorldMemoryRecord(
                id=r["id"],
                persona_id=r["persona_id"],
                memory_type=MemoryCategory(r["memory_type"]),
                content=r["content"],
                occurred_at=r["occurred_at"],
                written_at=r["written_at"],
                importance=r["importance"],
                emotional_valence=r["emotional_valence"],
                source_event_id=r["source_event_id"],
                metadata=loads(r["metadata_json"]),
            )
            for r in rows
        ]

    # ─── 11. Replay Snapshots ──────────────────────────────────────────────
    def save_replay_step(self, world_id: str, branch_id: str, step: ReplayStep) -> None:
        standalone = not self.db.conn.in_transaction
        now = datetime.now(UTC).isoformat()
        self.db.conn.execute(
            """
            INSERT INTO replay_snapshots (
              id, world_id, branch_id, timestamp, state_json, causal_subgraph_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"{world_id}:{branch_id}:{step.step_index}",
                world_id,
                branch_id,
                step.timestamp,
                dumps(step.state_snapshot.model_dump()),
                dumps({"milestones": step.causal_milestones, "diff": step.diff_summary}),
                now,
            ),
        )
        # A World-tick transaction owns the commit; standalone writes commit here.
        if standalone:
            self.db.conn.commit()

    def save_evaluation(self, evaluation: OutcomeEvaluation) -> None:
        standalone = not self.db.conn.in_transaction
        now = datetime.now(UTC).isoformat()
        self.db.conn.execute(
            """
            INSERT INTO world_simulation_runs (
              id, world_id, question, metrics_json, branch_results_json,
              distribution_json, summary, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evaluation.id,
                evaluation.world_id,
                evaluation.question,
                dumps(evaluation.metrics),
                dumps(evaluation.branch_results),
                dumps(evaluation.distribution),
                evaluation.causal_summary,
                now,
            ),
        )
        # A World-tick transaction owns the commit; standalone writes commit here.
        if standalone:
            self.db.conn.commit()
