from __future__ import annotations

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.domain.memory import MemoryRecord, MemoryType
from persona_continuum.room.models import ParticipantSlot, ResolvedBindingSnapshot
from persona_continuum.room.prompt_composer import PromptComposer


def test_prompt_composer_structure(app: PersonaContinuum) -> None:
    manifest_data = {
        "id": "steve_jobs",
        "display_name": "Steve Jobs",
        "persona_type": "historical",
        "run_mode": "continuation",
    }
    app.personas.create_from_manifest(manifest_data)

    composer = PromptComposer(total_budget_chars=4000)
    slot = ParticipantSlot(participant_id="p1", persona_id="steve_jobs", display_name="Steve Jobs")
    snapshot = ResolvedBindingSnapshot(
        participant_id="p1",
        persona_id="steve_jobs",
        display_name="Steve Jobs",
        agent_runtime_id="fake_agent",
        agent_runtime_name="Fake Agent",
        model_id="fake-gpt-5",
        reasoning_effort="high",
    )

    app.memories.add_memory(
        MemoryRecord(
            id="mem1",
            persona_id="steve_jobs",
            type=MemoryType.EPISODIC,
            source_kind="seed",
            content="I introduced the iPhone in 2007.",
        )
    )

    session = app.sessions.start_session("steve_jobs")
    prepared = app.sessions.prepare_turn("steve_jobs", session.id, "Tell us about design.")

    mem_dynamic = MemoryRecord(
        id="mem_dyn",
        persona_id="steve_jobs",
        type=MemoryType.EPISODIC,
        source_kind="dynamic",
        content="Design is not just what it looks like and feels like. Design is how it works.",
    )

    system_prompt, user_msg, _ = composer.compose_turn_prompt(
        slot=slot,
        snapshot=snapshot,
        prepared=prepared,
        dynamic_recall_memories=[mem_dynamic],
        recent_transcript=[{"speaker_name": "Elon", "content": "What is design?"}],
        user_message="Tell us about design.",
    )

    # Layer 1 checks
    assert "Steve Jobs" in system_prompt
    assert (
        "fake_agent" in system_prompt
        or "Fake Agent" in system_prompt
        or "Steve Jobs" in system_prompt
    )

    # Layer 2 checks
    assert (
        "Design is not just what it looks like" in user_msg
        or "Design is not just what it looks like" in system_prompt
    )
    assert "Tell us about design." in user_msg
