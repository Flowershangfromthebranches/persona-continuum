from __future__ import annotations

import copy

from persona_continuum.world.models import (
    SimulationBranch,
    WorldSnapshot,
    WorldState,
)


class BranchManager:
    """Manages independent timeline branches and branch forking."""

    def __init__(self) -> None:
        self._branches: dict[str, SimulationBranch] = {}

    def create_root_branch(
        self, world_id: str, name: str = "main", initial_state: WorldState | None = None
    ) -> SimulationBranch:
        branch = SimulationBranch(
            world_id=world_id,
            name=name,
            current_state=copy.deepcopy(initial_state) if initial_state else None,
        )
        self._branches[branch.id] = branch
        return copy.deepcopy(branch)

    def fork_branch(
        self,
        world_id: str,
        parent_branch_id: str,
        snapshot: WorldSnapshot,
        name: str,
    ) -> SimulationBranch:
        branch = SimulationBranch(
            world_id=world_id,
            name=name,
            parent_branch_id=parent_branch_id,
            parent_snapshot_id=snapshot.id,
            current_state=copy.deepcopy(snapshot.state),
        )
        self._branches[branch.id] = branch
        return copy.deepcopy(branch)

    def get_branch(self, branch_id: str) -> SimulationBranch | None:
        b = self._branches.get(branch_id)
        return copy.deepcopy(b) if b else None

    def list_branches(self, world_id: str) -> list[SimulationBranch]:
        return [copy.deepcopy(b) for b in self._branches.values() if b.world_id == world_id]

    def update_branch_state(self, branch_id: str, state: WorldState) -> None:
        if branch_id in self._branches:
            self._branches[branch_id].current_state = copy.deepcopy(state)
