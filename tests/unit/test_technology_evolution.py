from __future__ import annotations

from persona_continuum.world.models import TechnologyEntity, TechnologyRequirement
from persona_continuum.world.technology import TechnologyEvolutionModel


def _demo_technologies() -> dict[str, TechnologyEntity]:
    return {
        "lithography_process": TechnologyEntity(
            id="lithography_process",
            name="Advanced Lithography Process",
            maturity=0.6,
            cost=2.0,
            performance=2.5,
            adoption=0.3,
        ),
        "memory_stack": TechnologyEntity(
            id="memory_stack",
            name="High-Bandwidth Memory Stack",
            maturity=0.2,
            cost=4.0,
            performance=5.0,
            adoption=0.05,
            dependencies=[
                TechnologyRequirement(
                    technology_id="lithography_process",
                    minimum_maturity=0.4,
                    description="Requires precision packaging",
                )
            ],
        ),
    }


def test_technology_requires_dependencies() -> None:
    model = TechnologyEvolutionModel(_demo_technologies())

    # 1. Base technology with satisfied dependencies advances
    res = model.advance(
        "lithography_process", investment_billions=2.0, engineer_years=500
    )
    assert res["success"] is True
    assert res["new_maturity"] > res["old_maturity"]

    # 2. Blocked technology cannot advance if prerequisite maturity is below threshold
    base = model.get_technology("lithography_process")
    assert base is not None
    base.maturity = 0.1

    passed, msg = model.check_prerequisites("memory_stack")
    assert passed is False
    assert "maturity too low" in msg

    res_blocked = model.advance("memory_stack", investment_billions=5.0, engineer_years=1000)
    assert res_blocked["success"] is False
    assert res_blocked["blocked"] is True

    # 3. Once prerequisite is reached, advancement succeeds
    base.maturity = 0.8
    passed, msg = model.check_prerequisites("memory_stack")
    assert passed is True

    res_success = model.advance("memory_stack", investment_billions=2.0, engineer_years=500)
    assert res_success["success"] is True
    assert res_success["new_maturity"] > res_success["old_maturity"]


def test_model_starts_empty_without_explicit_technologies() -> None:
    """The generic engine must not auto-create any example technologies."""
    model = TechnologyEvolutionModel()
    assert model.technologies == {}
