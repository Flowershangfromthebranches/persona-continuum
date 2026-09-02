from __future__ import annotations

from persona_continuum.room.director import SpeakerDirector
from persona_continuum.room.models import DirectorConfig, DirectorMode, ParticipantSlot


def test_director_smart_selection() -> None:
    director = SpeakerDirector(seed=42)
    slots = [
        ParticipantSlot(
            participant_id="p1", persona_id="steve", display_name="Steve", speaking_weight=1.0
        ),
        ParticipantSlot(
            participant_id="p2", persona_id="elon", display_name="Elon", speaking_weight=1.0
        ),
    ]

    # First turn: initial selection
    spk1, reason1 = director.select_next_speaker(slots, turn_index=0)
    assert spk1 in {"p1", "p2"}

    # After p1 spoke, p2 has longer silence so p2 should be selected
    recent = [{"participant_id": "p1", "content": "Let us discuss design."}]
    spk2, reason2 = director.select_next_speaker(slots, turn_index=1, recent_transcript=recent)
    assert spk2 == "p2"
    assert "silence" in reason2 or "score" in reason2 or "natural_flow" in reason2


def test_director_consecutive_limit() -> None:
    director = SpeakerDirector(seed=42)
    slots = [
        ParticipantSlot(participant_id="p1", persona_id="steve", max_consecutive_turns=1),
        ParticipantSlot(participant_id="p2", persona_id="elon", max_consecutive_turns=2),
    ]

    # If p1 just spoke, and max_consecutive_turns is 1, p1 is suppressed
    recent = [{"participant_id": "p1", "content": "First turn."}]
    spk, _ = director.select_next_speaker(slots, turn_index=1, recent_transcript=recent)
    assert spk == "p2"


def test_director_round_robin() -> None:
    config = DirectorConfig(mode=DirectorMode.ROUND_ROBIN)
    director = SpeakerDirector(config=config, seed=42)
    slots = [
        ParticipantSlot(participant_id="p1", persona_id="steve"),
        ParticipantSlot(participant_id="p2", persona_id="elon"),
        ParticipantSlot(participant_id="p3", persona_id="friedrich"),
    ]

    spk0, _ = director.select_next_speaker(slots, turn_index=0)
    spk1, _ = director.select_next_speaker(slots, turn_index=1)
    spk2, _ = director.select_next_speaker(slots, turn_index=2)
    spk3, _ = director.select_next_speaker(slots, turn_index=3)

    assert spk0 == "p1"
    assert spk1 == "p2"
    assert spk2 == "p3"
    assert spk3 == "p1"


def test_director_manual_override() -> None:
    director = SpeakerDirector(seed=42)
    slots = [
        ParticipantSlot(participant_id="p1", persona_id="steve"),
        ParticipantSlot(participant_id="p2", persona_id="elon"),
    ]

    spk, reason = director.select_next_speaker(slots, manual_speaker_id="p2")
    assert spk == "p2"
    assert reason == "manual_override"
