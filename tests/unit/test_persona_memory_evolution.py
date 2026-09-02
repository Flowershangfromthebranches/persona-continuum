from __future__ import annotations

from persona_continuum.world.models import MemoryCategory
from persona_continuum.world.persona_memory import PersonaMemoryEvolution


def test_persona_memory_evolves() -> None:
    evo = PersonaMemoryEvolution()

    # 1. Add core identity memory
    id_mem = evo.add_identity_memory(
        persona_id="ada_lovelace",
        content="Believes in complete end-to-end integration of hardware and software.",
        importance=0.99,
    )
    assert id_mem.memory_type == MemoryCategory.IDENTITY

    # 2. Add counterfactual world experience memory
    exp_mem = evo.add_world_experience(
        persona_id="ada_lovelace",
        content="2015 custom server chip tapeout delayed due to packaging bottleneck.",
        occurred_at="2015-08-12",
        emotional_valence=-0.6,
        importance=0.85,
    )
    assert exp_mem.memory_type == MemoryCategory.WORLD_EXPERIENCE

    # Verify separation
    all_mems = evo.get_memories_for_persona("ada_lovelace")
    assert len(all_mems) == 2
    assert len(evo.get_memories_for_persona("ada_lovelace", MemoryCategory.IDENTITY)) == 1
    assert len(evo.get_memories_for_persona("ada_lovelace", MemoryCategory.WORLD_EXPERIENCE)) == 1

    # 3. Verify belief adaptation from counterfactual experience (generic dimensions)
    initial_beliefs = {"risk_vigilance": 0.4, "strategic_patience": 0.8}
    evolved = evo.evolve_beliefs("ada_lovelace", initial_beliefs, exp_mem.content)
    assert evolved["risk_vigilance"] > initial_beliefs["risk_vigilance"]
    assert evolved["strategic_patience"] < initial_beliefs["strategic_patience"]


def test_memory_type_robust_normalization() -> None:
    from persona_continuum.compiler.schemas import ArtifactMemory
    from persona_continuum.domain.memory import MemoryType

    assert MemoryType.from_raw("reflective") == MemoryType.SEMANTIC
    assert MemoryType.from_raw("philosophical") == MemoryType.SEMANTIC
    assert MemoryType.from_raw("belief") == MemoryType.SEMANTIC
    assert MemoryType.from_raw("episodic") == MemoryType.EPISODIC
    assert MemoryType.from_raw("incident_event") == MemoryType.EPISODIC
    assert MemoryType.from_raw("emotional_feeling") == MemoryType.EMOTIONAL
    assert MemoryType.from_raw("social_relationship") == MemoryType.RELATIONAL
    assert MemoryType.from_raw("unknown_custom_type") == MemoryType.SEMANTIC

    mem = ArtifactMemory.model_validate(
        {"content": "Deep thought about destiny", "type": "reflective"}
    )
    assert mem.type == MemoryType.SEMANTIC.value
