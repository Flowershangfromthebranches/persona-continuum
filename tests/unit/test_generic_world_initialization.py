"""Generic world initialization acceptance tests.

Proves that creating a world completely unrelated to any example domain
(Apple/NVIDIA/CUDA etc.) never auto-generates example organizations,
technologies, actors, or markets. The engine is domain-agnostic: everything
comes from the seed.
"""

from __future__ import annotations

from persona_continuum.world.actors import ActorManager
from persona_continuum.world.builder import WorldBuilderService
from persona_continuum.world.engine import ParallelWorldEngine  # noqa: F401  (import proves wiring)
from persona_continuum.world.evaluator import OutcomeEvaluator
from persona_continuum.world.models import SimulationBranch, WorldSeed
from persona_continuum.world.organization import OrganizationManager
from persona_continuum.world.state import WorldStateManager
from persona_continuum.world.technology import TechnologyEvolutionModel

FORBIDDEN_TOKENS = ("apple", "nvidia", "cuda", "tsmc", "silicon_valley", "steve_jobs")


def _historical_fiction_seed() -> WorldSeed:
    """A Song-dynasty maritime trade world: shares nothing with tech-industry demos."""
    return WorldSeed(
        baseline_world="historical_song_china",
        start_date="1125-03-01",
        divergence=[
            {
                "type": "historical_change",
                "condition": "The Quanzhou maritime bureau opens an early deep-water port",
                "consequence": "Overseas trade expands a decade ahead of the baseline timeline",
            }
        ],
        rules=[
            "Monsoon seasons determine feasible shipping windows.",
            "Actors act only on knowledge available before the current timestamp.",
        ],
        initial_actors=["shen_guan", "quanzhou_bureau"],
        location="Quanzhou",
    )


def test_state_initializes_empty_without_seed_conditions() -> None:
    state = WorldStateManager().initialize_state_from_seed(_historical_fiction_seed())
    assert state.organizations == {}
    assert state.technologies == {}
    assert state.economy == {}
    assert state.active_projects == {}
    assert state.relationships == {}
    blob = state.model_dump_json().lower()
    for token in FORBIDDEN_TOKENS:
        assert token not in blob, f"forbidden example token leaked into generic world: {token}"


def test_actor_manager_never_generates_default_roster() -> None:
    actors = ActorManager().initialize_actors_for_world(_historical_fiction_seed())
    actor_ids = set(actors.keys())
    assert actor_ids == {"shen_guan", "quanzhou_bureau"}
    blob = str(actor_ids).lower()
    for token in FORBIDDEN_TOKENS:
        assert token not in blob


def test_seed_builder_does_not_inject_default_actors() -> None:
    seed = WorldBuilderService().build_seed(
        "北宋泉州市舶司提前开放深水港，海外贸易扩张。"
    )
    assert seed.initial_actors == []
    assert seed.location is None
    blob = seed.model_dump_json().lower()
    for token in FORBIDDEN_TOKENS:
        assert token not in blob


def test_managers_start_empty() -> None:
    assert OrganizationManager().organizations == {}
    assert TechnologyEvolutionModel().technologies == {}


def test_evaluator_is_domain_agnostic() -> None:
    state = WorldStateManager().initialize_state_from_seed(_historical_fiction_seed())
    branch = SimulationBranch(world_id="w_hist", name="trade_boom", current_state=state)
    evaluation = OutcomeEvaluator().evaluate_simulation(
        world_id="w_hist",
        question="Does the deep-water port succeed?",
        branches=[branch],
    )
    blob = evaluation.model_dump_json().lower()
    for token in FORBIDDEN_TOKENS:
        assert token not in blob, f"evaluator leaked example token: {token}"


def test_seed_with_explicit_initial_conditions_is_respected() -> None:
    """Backward-compatible WorldSeed fields drive initialization when provided."""
    seed = _historical_fiction_seed()
    seed.initial_organizations = {
        "quanzhou_bureau": {"name": "Quanzhou Maritime Bureau", "cash_reserves_billions": 2.0}
    }
    seed.initial_state = {"economy": {"maritime_trade_index": 1.1}}
    state = WorldStateManager().initialize_state_from_seed(seed)
    assert "quanzhou_bureau" in state.organizations
    assert state.economy == {"maritime_trade_index": 1.1}


def test_legacy_seed_metadata_initial_state_still_works() -> None:
    """Older seeds carrying initial conditions in metadata remain readable."""
    seed = _historical_fiction_seed()
    seed.metadata["initial_organizations"] = {
        "quanzhou_bureau": {"name": "Quanzhou Maritime Bureau"}
    }
    state = WorldStateManager().initialize_state_from_seed(seed)
    assert "quanzhou_bureau" in state.organizations
