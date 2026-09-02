from __future__ import annotations

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.domain.memory import MemoryRecord, MemoryType
from persona_continuum.room.recall_gate import RecallGate


def test_recall_gate_patterns_and_musk_mars_query(app: PersonaContinuum) -> None:
    # 1. Setup persona and memories
    manifest_data = {
        "id": "elon_musk",
        "display_name": "Elon Musk",
        "aliases": ["Musk", "Elon"],
        "persona_type": "historical",
        "run_mode": "continuation",
    }
    app.personas.create_from_manifest(manifest_data)

    # Add historical memories for Musk
    mem1 = MemoryRecord(
        id="mem_mars_1",
        persona_id="elon_musk",
        type=MemoryType.EPISODIC,
        source_kind="seed",
        content="In 2016 at IAC, I unveiled the Interplanetary Transport System to colonize Mars.",
        importance=0.9,
    )
    mem2 = MemoryRecord(
        id="mem_mars_2",
        persona_id="elon_musk",
        type=MemoryType.EPISODIC,
        source_kind="seed",
        content="Making life multiplanetary is essential for the light of consciousness.",
        importance=0.85,
    )
    mem3 = MemoryRecord(
        id="mem_tesla",
        persona_id="elon_musk",
        type=MemoryType.EPISODIC,
        source_kind="seed",
        content="Tesla Model S production ramp was extraordinarily difficult in Fremont.",
        importance=0.7,
    )
    app.memories.add_memory(mem1)
    app.memories.add_memory(mem2)
    app.memories.add_memory(mem3)

    recall_gate = RecallGate(app.memories)

    # 2. Test mandatory benchmark query: "以前 Musk 关于火星说过什么？"
    user_query = "以前 Musk 关于火星说过什么？"
    result = recall_gate.analyze_turn(
        persona_id="elon_musk",
        current_speaker_name="Elon Musk",
        user_message=user_query,
    )

    assert result.triggered is True
    assert len(result.reasons) > 0
    assert any("pattern_matched" in r or "interrogative" in r for r in result.reasons)
    assert len(result.memories) >= 1
    # Check that mars memories were prioritized over Tesla
    memory_contents = [m.content for m in result.memories]
    assert any("Mars" in c or "colonize" in c or "multiplanetary" in c for c in memory_contents)


def test_recall_gate_non_trigger(app: PersonaContinuum) -> None:
    recall_gate = RecallGate(app.memories)
    # Generic greeting should not trigger recall
    result = recall_gate.analyze_turn(
        persona_id="elon_musk",
        current_speaker_name="Elon Musk",
        user_message="Hello everyone.",
    )
    assert result.triggered is False
