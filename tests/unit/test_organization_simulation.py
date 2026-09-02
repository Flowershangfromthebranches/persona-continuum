from __future__ import annotations

import pytest

from persona_continuum.world.models import Department, OrganizationEntity, OrgStrategy
from persona_continuum.world.organization import OrganizationManager


def _demo_org() -> OrganizationEntity:
    return OrganizationEntity(
        id="novacore",
        name="Novacore Corporation",
        leadership=["ada_lovelace"],
        departments=[
            Department(
                name="Silicon Engineering",
                mission="Custom SoC architecture",
                headcount=1200,
                budget_billions=2.5,
                current_focus="Dedicated AI silicon",
            ),
            Department(
                name="AI Research",
                mission="On-device models",
                headcount=600,
                budget_billions=1.2,
                current_focus="Core modeling",
            ),
            Department(
                name="Hardware Engineering",
                mission="Product platforms",
                headcount=4000,
                budget_billions=4.0,
                current_focus="Integration",
            ),
            Department(
                name="Software Engineering",
                mission="OS and developer ecosystem",
                headcount=5000,
                budget_billions=3.5,
                current_focus="Developer APIs",
            ),
        ],
        employees_count=60000,
        budget_billions=25.0,
        cash_reserves_billions=120.0,
        technology=["custom_soc", "secure_enclave"],
        projects=["Novacore_silicon"],
        strategy=OrgStrategy(
            primary_goal="Vertical integration",
            priority_domains=["mobile_silicon"],
            risk_appetite="calculated",
        ),
    )


def test_company_has_resources() -> None:
    mgr = OrganizationManager({"novacore": _demo_org()})
    org = mgr.get_organization("novacore")
    assert org is not None
    assert org.cash_reserves_billions >= 100.0
    assert org.employees_count >= 50000
    assert len(org.departments) >= 4

    # 1. Successful hiring action deductions
    initial_cash = org.cash_reserves_billions
    initial_headcount = org.employees_count
    res_hire = mgr.hire("novacore", "Silicon Engineering", count=200, cost_billions=0.1)
    assert res_hire["success"] is True
    assert org.employees_count == initial_headcount + 200
    assert org.cash_reserves_billions == pytest.approx(initial_cash - 0.1)

    # 2. Reinvestment action
    res_invest = mgr.invest("novacore", "Datacenter_Cluster_Alpha", amount_billions=5.0)
    assert res_invest["success"] is True
    assert "Datacenter_Cluster_Alpha" in org.projects
    assert org.cash_reserves_billions == pytest.approx(initial_cash - 5.1)

    # 3. Capital exhaustion check
    res_fail = mgr.invest("novacore", "Impossible_Galaxy_Project", amount_billions=999.0)
    assert res_fail["success"] is False
    assert "Insufficient cash reserves" in res_fail["error"]


def test_manager_starts_empty_without_explicit_organizations() -> None:
    """The generic engine must not auto-create any example organizations."""
    mgr = OrganizationManager()
    assert mgr.organizations == {}
