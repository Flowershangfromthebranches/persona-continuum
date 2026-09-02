from __future__ import annotations

from typing import Any

from persona_continuum.world.models import TechnologyEntity


class TechnologyEvolutionModel:
    """Models technology maturity curves, cost reductions, performance scaling,

    and strict prerequisite dependency chains.

    Starts empty by default; technologies only exist when the world seed,
    LLM World Builder, or callers provide them explicitly.
    """

    def __init__(self, technologies: dict[str, TechnologyEntity] | None = None) -> None:
        self.technologies = technologies or {}

    def get_technology(self, tech_id: str) -> TechnologyEntity | None:
        return self.technologies.get(tech_id)

    def check_prerequisites(self, tech_id: str) -> tuple[bool, str]:
        """Verifies whether all dependency prerequisite technologies meet their minimum maturity."""
        tech = self.get_technology(tech_id)
        if not tech:
            return False, f"Unknown technology '{tech_id}'"

        for req in tech.dependencies:
            dep = self.get_technology(req.technology_id)
            if not dep:
                return False, f"Missing prerequisite technology '{req.technology_id}'"
            if dep.maturity < req.minimum_maturity:
                msg = (
                    f"Prerequisite '{dep.name}' maturity too low "
                    f"({dep.maturity:.2f} < required {req.minimum_maturity:.2f}). "
                    f"Reason: {req.description}"
                )
                return False, msg

        return True, "All prerequisite dependencies satisfied."

    def advance(
        self,
        tech_id: str,
        investment_billions: float,
        engineer_years: int,
        lead_org_id: str | None = None,
    ) -> dict[str, Any]:
        """Advances technology maturity subject to dependencies, capital, and engineering effort."""
        tech = self.get_technology(tech_id)
        if not tech:
            return {"success": False, "error": f"Unknown technology '{tech_id}'"}

        passed, msg = self.check_prerequisites(tech_id)
        if not passed:
            return {"success": False, "blocked": True, "error": msg}

        # Calculate incremental progress
        capital_factor = min(1.0, investment_billions / 2.0)
        talent_factor = min(1.0, engineer_years / 500.0)
        gain = 0.05 + 0.15 * capital_factor * talent_factor

        old_maturity = tech.maturity
        tech.maturity = min(1.0, tech.maturity + gain)

        # Economies of scale: cost drops as maturity rises
        tech.cost = max(0.5, tech.cost * (1.0 - gain * 0.4))
        tech.performance = tech.performance * (1.0 + gain * 0.8)
        tech.adoption = min(0.95, tech.adoption + gain * 0.3)
        if lead_org_id:
            tech.lead_org_id = lead_org_id

        return {
            "success": True,
            "tech_id": tech_id,
            "old_maturity": old_maturity,
            "new_maturity": tech.maturity,
            "current_cost": tech.cost,
            "current_performance": tech.performance,
            "adoption": tech.adoption,
        }

    def export_dict(self) -> dict[str, dict[str, Any]]:
        return {tid: t.model_dump() for tid, t in self.technologies.items()}

    def import_dict(self, data: dict[str, Any]) -> None:
        self.technologies = {
            tid: TechnologyEntity.model_validate(tdata) for tid, tdata in data.items()
        }
