from __future__ import annotations

from typing import Any

from persona_continuum.world.models import (
    ActionProposal,
    ActionResolution,
    ActionType,
    Actor,
    ActorAction,
    ResolutionStatus,
    WorldState,
)


class ActionResolver:
    """Evaluates actor actions against resources, technology maturity, and competition.

    Decides success probability, resource cost, systemic impact, and applies state mutations.
    """

    def resolve_proposal(
        self, proposal: ActionProposal, actor: Actor, state: WorldState
    ) -> tuple[ActionResolution, WorldState, ActorAction]:
        """Judge feasibility only; the proposal remains the source of what to do."""
        action = proposal.to_action(timestamp=state.timestamp)
        feasibility, reasons = self._check_constraints(action, actor, state)
        if feasibility <= 0.0:
            return (
                ActionResolution(
                    action_id=action.id,
                    success=False,
                    status=ResolutionStatus.FAILURE,
                    success_probability=0.0,
                    outcome_description=(
                        f"{actor.name}'s proposal could not occur: " + "; ".join(reasons)
                    ),
                    state_mutations={"constraint_failures": reasons},
                ),
                state.model_copy(deep=True),
                action,
            )

        resolution, mutated = self.resolve_action(action, actor, state)
        resolution.success_probability = min(resolution.success_probability, feasibility)
        if feasibility < 0.75 and resolution.success:
            resolution.status = ResolutionStatus.PARTIAL_SUCCESS
            resolution.outcome_description = (
                f"Partial success under constraints ({'; '.join(reasons)}): "
                f"{resolution.outcome_description}"
            )
        return resolution, mutated, action

    def _check_constraints(
        self, action: ActorAction, actor: Actor, state: WorldState
    ) -> tuple[float, list[str]]:
        params = action.parameters
        org_id = actor.organization_id or actor.identity.get("organization")
        if not org_id and actor.id in state.organizations:
            org_id = actor.id
        organization = state.organizations.get(str(org_id), {})
        scores: list[float] = []
        reasons: list[str] = []

        requested_budget = float(
            params.get("budget_billions")
            or params.get("amount_billions")
            or params.get("investment_billions")
            or 0.0
        )
        available_budget = float(
            organization.get("cash_reserves_billions")
            or actor.resources.get("capital_access_billions")
            or actor.resources.get("capital_authority_billions")
            or actor.resources.get("cash_reserves_billions")
            or 0.0
        )
        if requested_budget > 0:
            ratio = min(1.0, available_budget / requested_budget) if requested_budget else 1.0
            scores.append(ratio)
            if ratio < 1.0:
                reasons.append(
                    f"budget {available_budget:.2f}B available vs {requested_budget:.2f}B required"
                )

        requested_talent = int(
            params.get("team_engineers") or params.get("headcount") or params.get("count") or 0
        )
        available_talent = int(
            organization.get("employees_count")
            or organization.get("headcount_engineers")
            or actor.resources.get("engineers_count")
            or actor.resources.get("talent_pool_engineers")
            or 0
        )
        if requested_talent > 0:
            ratio = min(1.0, available_talent / requested_talent) if available_talent else 0.0
            scores.append(ratio)
            if ratio < 1.0:
                reasons.append(
                    f"talent {available_talent} available vs {requested_talent} required"
                )

        required_maturity = params.get("required_technology_maturity")
        if required_maturity is not None:
            technology = state.technologies.get(str(action.target or ""), {})
            current_maturity = float(
                technology.get("maturity") or technology.get("maturity_level", 0.0)
            )
            required = float(required_maturity)
            ratio = min(1.0, current_maturity / required) if required > 0 else 1.0
            scores.append(ratio)
            if ratio < 1.0:
                reasons.append(
                    f"technology maturity {current_maturity:.2f} vs {required:.2f} required"
                )

        minimum_relationship = params.get("minimum_relationship")
        if minimum_relationship is not None and action.target:
            relation = actor.relationships.get(action.target, 0.0)
            if isinstance(relation, dict):
                relation = relation.get("affinity", 0.0)
            relation_score = float(relation)
            required_relation = float(minimum_relationship)
            ratio = 1.0 if relation_score >= required_relation else max(0.0, relation_score + 1) / 2
            scores.append(ratio)
            if ratio < 1.0:
                reasons.append(
                    f"relationship {relation_score:.2f} below {required_relation:.2f} required"
                )

        deadline = str(params.get("deadline") or "")
        if deadline and deadline < state.timestamp:
            scores.append(0.0)
            reasons.append(f"deadline {deadline} has passed at {state.timestamp}")

        if not scores:
            return 0.85, ["no explicit blocking constraint"]
        return min(scores), reasons or ["constraints satisfied"]

    def resolve_action(
        self, action: ActorAction, actor: Actor, state: WorldState
    ) -> tuple[ActionResolution, WorldState]:
        mutated_state = state.model_copy(deep=True)
        act_type = action.action_type
        params = action.parameters

        if act_type == ActionType.LAUNCH_PROJECT:
            return self._resolve_launch_project(action, actor, mutated_state, params)
        elif act_type == ActionType.INVEST:
            return self._resolve_invest(action, actor, mutated_state, params)
        elif act_type == ActionType.HIRE:
            return self._resolve_hire(action, actor, mutated_state, params)
        elif act_type == ActionType.RESEARCH:
            return self._resolve_research(action, actor, mutated_state, params)
        elif act_type == ActionType.NEGOTIATE:
            return self._resolve_negotiate(action, actor, mutated_state, params)
        elif act_type == ActionType.COMMUNICATE:
            return self._resolve_communicate(action, actor, mutated_state, params)
        elif act_type == ActionType.DECIDE:
            return self._resolve_decide(action, actor, mutated_state, params)
        else:
            # Default observation/wait/reflect
            outcome = f"{actor.name} completed action '{act_type.value}': {action.description}"
            res = ActionResolution(
                action_id=action.id,
                success=True,
                success_probability=1.0,
                cost={},
                outcome_description=outcome,
                state_mutations={},
            )
            return res, mutated_state

    def _resolve_launch_project(
        self, action: ActorAction, actor: Actor, state: WorldState, params: dict[str, Any]
    ) -> tuple[ActionResolution, WorldState]:
        project_id = params.get("project_id") or f"proj_{action.id}"
        project_name = params.get("name") or action.description or "New Strategic Initiative"
        budget = float(params.get("budget_billions", 2.0))
        team_size = int(params.get("team_engineers", 200))
        org_id = actor.identity.get("organization") or (
            actor.id if actor.id in state.organizations else ""
        )

        org = state.organizations.get(org_id, {})
        if not org:
            res = ActionResolution(
                action_id=action.id,
                success=False,
                success_probability=0.0,
                cost={},
                outcome_description=(
                    f"{actor.name} cannot launch '{project_name}': no known "
                    "organization backing the action."
                ),
                state_mutations={},
            )
            return res, state

        available_cash = float(org.get("cash_reserves_billions", 0.0))

        # Check capital availability
        success = available_cash >= budget
        prob = min(0.95, available_cash / max(budget, 1.0))

        if success and org_id in state.organizations:
            state.organizations[org_id]["cash_reserves_billions"] -= budget
            if project_id not in state.organizations[org_id].get("active_initiatives", []):
                state.organizations[org_id].setdefault("active_initiatives", []).append(project_id)

        state.active_projects[project_id] = {
            "name": project_name,
            "owner": org_id,
            "budget_billions": budget,
            "team_engineers": team_size,
            "progress_percent": 10 if success else 0,
            "status": "active" if success else "insufficient_capital",
            "started_at": state.timestamp,
        }

        if success:
            desc = (
                f"{actor.name} successfully launched '{project_name}' with "
                f"${budget}B budget and {team_size} engineers."
            )
        else:
            desc = f"{actor.name} failed to launch '{project_name}' due to capital constraints."

        res = ActionResolution(
            action_id=action.id,
            success=success,
            success_probability=prob,
            cost={"budget_billions": budget if success else 0},
            outcome_description=desc,
            state_mutations={
                "new_project": project_id,
                "status": "active" if success else "failed",
            },
        )
        return res, state

    def _resolve_invest(
        self, action: ActorAction, actor: Actor, state: WorldState, params: dict[str, Any]
    ) -> tuple[ActionResolution, WorldState]:
        amount = float(params.get("amount_billions", 1.0))
        target = action.target or "strategic_rd"
        org_id = actor.identity.get("organization") or (
            actor.id if actor.id in state.organizations else ""
        )

        if org_id not in state.organizations:
            res = ActionResolution(
                action_id=action.id,
                success=False,
                success_probability=0.0,
                cost={},
                outcome_description=(
                    f"{actor.name} cannot invest in '{target}': no known "
                    "organization backing the action."
                ),
                state_mutations={},
            )
            return res, state

        state.organizations[org_id]["cash_reserves_billions"] -= amount
        state.organizations[org_id]["rd_budget_annual_billions"] = (
            float(state.organizations[org_id].get("rd_budget_annual_billions", 0.0))
            + amount * 0.5
        )

        desc = (
            f"{actor.name} ({org_id}) invested ${amount}B into {target}, "
            f"expanding the organization's investment pipeline."
        )
        res = ActionResolution(
            action_id=action.id,
            success=True,
            success_probability=0.9,
            cost={"capital_billions": amount},
            outcome_description=desc,
            state_mutations={"target": target, "amount_billions": amount},
        )
        return res, state

    def _resolve_hire(
        self, action: ActorAction, actor: Actor, state: WorldState, params: dict[str, Any]
    ) -> tuple[ActionResolution, WorldState]:
        count = int(params.get("count", 100))
        org_id = actor.identity.get("organization") or (
            actor.id if actor.id in state.organizations else ""
        )

        if org_id not in state.organizations:
            res = ActionResolution(
                action_id=action.id,
                success=False,
                success_probability=0.0,
                cost={},
                outcome_description=(
                    f"{actor.name} cannot recruit into '{org_id or 'unknown'}': "
                    "no known organization backing the action."
                ),
                state_mutations={},
            )
            return res, state

        org = state.organizations[org_id]
        headcount_key = (
            "headcount_engineers"
            if "headcount_engineers" in org
            else "headcount"
        )
        org[headcount_key] = int(org.get(headcount_key, 0)) + count

        desc = f"{actor.name} recruited {count} specialists into {org_id}."
        res = ActionResolution(
            action_id=action.id,
            success=True,
            success_probability=0.85,
            cost={"talent_headcount": count},
            outcome_description=desc,
            state_mutations={"headcount_added": count},
        )
        return res, state

    def _resolve_research(
        self, action: ActorAction, actor: Actor, state: WorldState, params: dict[str, Any]
    ) -> tuple[ActionResolution, WorldState]:
        domain = action.target or "general_research"
        level = params.get("breakthrough_level", "prototype_proven")

        state.technologies[domain] = {
            "maturity_level": level,
            "lead_contributor": actor.id,
            "patented": True,
        }

        desc = f"{actor.name} advanced research in '{domain}' to '{level}'."
        res = ActionResolution(
            action_id=action.id,
            success=True,
            success_probability=0.8,
            cost={},
            outcome_description=desc,
            state_mutations={"technology": domain, "level": level},
        )
        return res, state

    def _resolve_negotiate(
        self, action: ActorAction, actor: Actor, state: WorldState, params: dict[str, Any]
    ) -> tuple[ActionResolution, WorldState]:
        counterpart = action.target or ""
        rel_key = f"{actor.id}:{counterpart}"
        alt_key = f"{counterpart}:{actor.id}"

        curr_rel = state.relationships.get(rel_key) or state.relationships.get(alt_key) or {}
        new_affinity = min(1.0, curr_rel.get("affinity", 0.0) + 0.3)
        state.relationships[rel_key] = {
            "affinity": new_affinity,
            "status": "strategic_alignment",
            "deal_terms": params.get("terms", "capacity_reservation"),
        }

        desc = (
            f"{actor.name} concluded negotiation with {counterpart} "
            f"(Affinity: {new_affinity:+.2f})."
        )
        res = ActionResolution(
            action_id=action.id,
            success=True,
            success_probability=0.8,
            cost={},
            outcome_description=desc,
            state_mutations={"relationship": rel_key, "affinity": new_affinity},
        )
        return res, state

    def _resolve_communicate(
        self, action: ActorAction, actor: Actor, state: WorldState, params: dict[str, Any]
    ) -> tuple[ActionResolution, WorldState]:
        target = action.target or "public"
        message = action.description or params.get("message", "Strategic message communicated.")
        desc = f"{actor.name} communicated to {target}: {message}"
        res = ActionResolution(
            action_id=action.id,
            success=True,
            success_probability=1.0,
            cost={},
            outcome_description=desc,
            state_mutations={"target": target, "message": message},
        )
        return res, state

    def _resolve_decide(
        self, action: ActorAction, actor: Actor, state: WorldState, params: dict[str, Any]
    ) -> tuple[ActionResolution, WorldState]:
        decision = action.description
        desc = f"{actor.name} enacted executive decision: {decision}"
        res = ActionResolution(
            action_id=action.id,
            success=True,
            success_probability=1.0,
            cost={},
            outcome_description=desc,
            state_mutations={"decision": decision},
        )
        return res, state
