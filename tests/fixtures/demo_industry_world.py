"""Explicit demo world content for tests.

Nothing in this file is part of the engine: the Parallel World engine starts
empty by default. Tests that exercise organization/technology/project behavior
import these builders to construct a small fictional industry world (Novacore
vs Helion) instead of relying on any built-in default roster.
"""

from __future__ import annotations

from typing import Any

DEMO_START_DATE = "2011-10-05"


def demo_organizations() -> dict[str, dict[str, Any]]:
    return {
        "novacore_corp": {
            "name": "Novacore Corporation",
            "cash_reserves_billions": 81.6,
            "rd_budget_annual_billions": 2.4,
            "headcount_engineers": 12000,
            "datacenter_market_share": 0.02,
            "consumer_device_market_share": 0.28,
            "chip_design_capability": "custom_soc_arm",
            "active_initiatives": ["novacore_ai_chip_initiative"],
        },
        "helion_corp": {
            "name": "Helion Corporation",
            "cash_reserves_billions": 3.0,
            "rd_budget_annual_billions": 1.0,
            "headcount_engineers": 6000,
            "datacenter_market_share": 0.65,
            "gpu_architecture": "fermi_kepler_transition",
            "parallel_runtime_ecosystem_strength": 0.75,
            "active_initiatives": ["parallel_runtime_acceleration"],
        },
    }


def demo_technologies() -> dict[str, dict[str, Any]]:
    return {
        "lithography_node": {"current_nm": 32, "next_target_nm": 28},
        "parallel_compute_runtime": {
            "maturity_level": "early_research",
            "dominant_paradigm": "gpu_runtime",
        },
        "on_device_accelerator": {"readiness_level": "concept", "hardware_support": False},
    }


def demo_projects() -> dict[str, dict[str, Any]]:
    return {
        "novacore_ai_chip_initiative": {
            "name": "Novacore AI Silicon Initiative",
            "owner": "novacore_corp",
            "budget_billions": 5.0,
            "team_engineers": 500,
            "target_node_nm": 28,
            "progress_percent": 5,
            "status": "active",
            "started_at": DEMO_START_DATE,
        }
    }


def demo_relationships() -> dict[str, dict[str, Any]]:
    return {
        "novacore_corp:helion_corp": {
            "affinity": -0.4,
            "status": "rivalry",
            "commercial_ties": "minimal",
        }
    }


def demo_resources() -> dict[str, Any]:
    return {
        "global_foundry_capacity_utilization": 0.92,
        "rare_earth_supply_stability": "high",
    }


def demo_economy() -> dict[str, Any]:
    return {
        "us_gdp_growth": 0.022,
        "semiconductor_industry_growth": 0.08,
        "ai_vc_investment_annual_billions": 0.4,
    }


def demo_seed_metadata() -> dict[str, Any]:
    return {
        "initial_organizations": demo_organizations(),
        "initial_technologies": demo_technologies(),
        "initial_projects": demo_projects(),
        "initial_relationships": demo_relationships(),
        "initial_resources": demo_resources(),
        "initial_state": {"economy": demo_economy()},
        "initial_actors": [
            {"id": "ada_lovelace", "name": "Ada Lovelace", "actor_type": "persona_actor"},
            {
                "id": "novacore_corp",
                "name": "Novacore Corporation",
                "actor_type": "organization_actor",
            },
        ],
    }
