from __future__ import annotations

from typing import Any

from persona_continuum.world.models import EconomicState, OrganizationEntity, TechnologyEntity


class EconomicSimulationLayer:
    """Simulates market demand, compute total addressable market (TAM),

    corporate revenues, R&D operational expenditure, and capital flow.
    """

    def __init__(self, state: EconomicState | None = None) -> None:
        self.state = state or EconomicState()

    def step_economy(
        self,
        organizations: dict[str, OrganizationEntity],
        technologies: dict[str, TechnologyEntity],
        delta_years: float = 1.0,
    ) -> dict[str, Any]:
        """Advances macro market demand and adjusts corporate balance sheets."""
        # 1. Expand market TAM when the most mature technology advances
        maturity_values = [
            t.maturity
            for t in technologies.values()
            if isinstance(t.maturity, (int, float))
        ]
        if maturity_values:
            growth_rate = 1.0 + (max(maturity_values) * 0.25 * delta_years)
            self.state.enterprise_compute_tam_billions *= growth_rate

        # 2. Iterate through organizations and compute quarterly/annual cash flow
        flow_summary: dict[str, Any] = {}
        for org_id, org in organizations.items():
            # Revenue calculation
            base_rev = org.budget_billions * 1.5
            # Innovation multiplier from proprietary technologies
            tech_mult = 1.0
            for t_id in org.technology:
                t = technologies.get(t_id)
                if t:
                    tech_mult += t.adoption * 0.3

            revenue = base_rev * tech_mult * delta_years
            opex = org.budget_billions * 0.8 * delta_years
            r_and_d = sum(d.budget_billions for d in org.departments) * delta_years

            net_income = revenue - (opex + r_and_d)
            org.cash_reserves_billions += max(-org.cash_reserves_billions * 0.5, net_income)

            flow_summary[org_id] = {
                "revenue_billions": round(revenue, 2),
                "r_and_d_billions": round(r_and_d, 2),
                "net_income_billions": round(net_income, 2),
                "ending_cash_billions": round(org.cash_reserves_billions, 2),
            }

        return {
            "gdp_index": self.state.gdp_index,
            "enterprise_compute_tam_billions": round(self.state.enterprise_compute_tam_billions, 2),
            "mobile_silicon_tam_billions": round(self.state.mobile_silicon_tam_billions, 2),
            "corporate_cash_flows": flow_summary,
        }

    def export_dict(self) -> dict[str, Any]:
        return self.state.model_dump()

    def import_dict(self, data: dict[str, Any]) -> None:
        self.state = EconomicState.model_validate(data)
