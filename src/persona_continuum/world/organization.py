from __future__ import annotations

from typing import Any

from persona_continuum.world.models import Department, OrganizationEntity, OrgStrategy


class OrganizationManager:
    """Manages full organizational entities with departments, talent pools,

    capital reserves, technology portfolios, and corporate actions.

    Starts empty by default; organizations only exist when the world seed,
    LLM World Builder, or callers provide them explicitly.
    """

    def __init__(self, organizations: dict[str, OrganizationEntity] | None = None) -> None:
        self.organizations = organizations or {}

    def get_organization(self, org_id: str) -> OrganizationEntity | None:
        return self.organizations.get(org_id)

    def hire(
        self, org_id: str, department_name: str, count: int, cost_billions: float = 0.05
    ) -> dict[str, Any]:
        org = self.get_organization(org_id)
        if not org:
            return {"success": False, "error": f"Unknown organization {org_id}"}

        # Check cash reserves
        if org.cash_reserves_billions < cost_billions:
            err = f"Insufficient cash reserves ({org.cash_reserves_billions}B < {cost_billions}B)"
            return {"success": False, "error": err}

        org.cash_reserves_billions -= cost_billions
        org.employees_count += count

        dept = next((d for d in org.departments if d.name.lower() == department_name.lower()), None)
        if dept:
            dept.headcount += count
            dept.budget_billions += cost_billions * 0.8
        else:
            org.departments.append(
                Department(name=department_name, headcount=count, budget_billions=cost_billions)
            )

        return {
            "success": True,
            "org_id": org_id,
            "added_headcount": count,
            "total_headcount": org.employees_count,
            "remaining_cash": org.cash_reserves_billions,
        }

    def invest(self, org_id: str, target_project: str, amount_billions: float) -> dict[str, Any]:
        org = self.get_organization(org_id)
        if not org:
            return {"success": False, "error": f"Unknown organization {org_id}"}

        if org.cash_reserves_billions < amount_billions:
            err = f"Insufficient cash reserves ({org.cash_reserves_billions}B < {amount_billions}B)"
            return {"success": False, "error": err}

        org.cash_reserves_billions -= amount_billions
        if target_project not in org.projects:
            org.projects.append(target_project)

        return {
            "success": True,
            "org_id": org_id,
            "project": target_project,
            "amount_invested": amount_billions,
            "remaining_cash": org.cash_reserves_billions,
        }

    def adjust_strategy(self, org_id: str, new_strategy: OrgStrategy) -> dict[str, Any]:
        org = self.get_organization(org_id)
        if not org:
            return {"success": False, "error": f"Unknown organization {org_id}"}

        org.strategy = new_strategy
        return {"success": True, "org_id": org_id, "strategy": org.strategy.model_dump()}

    def acquire(
        self, acquirer_org_id: str, target_name: str, cost_billions: float, talent_count: int
    ) -> dict[str, Any]:
        acquirer = self.get_organization(acquirer_org_id)
        if not acquirer:
            return {"success": False, "error": f"Unknown organization {acquirer_org_id}"}

        if acquirer.cash_reserves_billions < cost_billions:
            err = (
                f"Insufficient cash reserves "
                f"({acquirer.cash_reserves_billions}B < {cost_billions}B)"
            )
            return {"success": False, "error": err}

        acquirer.cash_reserves_billions -= cost_billions
        acquirer.employees_count += talent_count
        acquirer.projects.append(f"Integration_{target_name}")

        return {
            "success": True,
            "acquirer": acquirer_org_id,
            "target": target_name,
            "cost_billions": cost_billions,
            "absorbed_talent": talent_count,
            "remaining_cash": acquirer.cash_reserves_billions,
        }

    def export_dict(self) -> dict[str, dict[str, Any]]:
        return {oid: org.model_dump() for oid, org in self.organizations.items()}

    def import_dict(self, data: dict[str, Any]) -> None:
        self.organizations = {
            oid: OrganizationEntity.model_validate(odata) for oid, odata in data.items()
        }
