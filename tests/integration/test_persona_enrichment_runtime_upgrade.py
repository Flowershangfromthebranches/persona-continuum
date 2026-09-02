from __future__ import annotations

import asyncio

from persona_continuum.agent.adapters.gemini import GeminiCliAdapter
from persona_continuum.agent.models import AgentProbeResult, AgentStatus
from persona_continuum.application.persona_creation_service import PersonaCreationJob
from persona_continuum.application.research_backend import NativeCliResearchBackend
from persona_continuum.domain.persona import PersonaType
from persona_continuum.domain.profile import EnrichmentInputMode, ProfileEnrichmentJob, ProfileType


def test_gemini_cli_reports_native_web_research() -> None:
    capability = GeminiCliAdapter()._research_capability
    assert capability.mode == "native_cli"
    assert capability.search and capability.fetch
    assert capability.verification_status == "declared"
    assert capability.source.startswith("gemini_cli:")


def test_gemini_cli_models_are_parsed_from_runtime_output() -> None:
    models = GeminiCliAdapter._parse_models(
        "Fetching available models...\n"
        "gemini-3.7-flash-high\tGemini 3.7 Flash (High)\n"
        "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n"
        "gemini-3.7-flash-lowGemini 3.7 Flash (Low)\n"
    )
    assert [model.id for model in models] == [
        "gemini-3.7-flash-high",
        "claude-sonnet-4-6",
        "gemini-3.7-flash-low",
    ]
    assert models[0].supported_reasoning_efforts == ["high"]
    assert models[0].source == "official_cli"
    assert models[1].supported_reasoning_efforts == []


def test_unknown_research_capability_is_explicitly_probeable() -> None:
    probe = AgentProbeResult(id="unknown", name="Unknown", status=AgentStatus.READY)
    assert probe.research.mode == "none"
    assert probe.research.verification_status == "unknown"
    assert not probe.research.search


def test_native_research_backend_requires_provenance() -> None:
    assert (
        NativeCliResearchBackend._normalise_many(
            [{"url": "https://example.test", "content": "summary"}]
        )
        == []
    )
    result = NativeCliResearchBackend._normalise_many(
        [{"url": "https://example.test", "content": "source", "citation": "c1"}]
    )
    assert result[0]["canonical_url"] == "https://example.test"


def test_profile_enrichment_job_persists_input_mode_and_delta(app) -> None:
    profile = app.profile_library.create_profile(
        profile_type=ProfileType.ORGANIZATION,
        display_name="Runtime Test Organization",
    )
    job = app.profile_library.save_enrichment_job(
        ProfileEnrichmentJob(
            id="runtime_mode_job",
            target_profile_id=profile.id,
            target_profile_type=profile.profile_type,
            enrichment_input_mode=EnrichmentInputMode.LOCAL_MATERIALS,
            parent_job_id="parent",
            base_persona_version=2,
            input_material_ids=["material-1"],
            input_material_count=1,
            new_source_ids=["source-2"],
            evidence_delta={"new_source_ids": ["source-2"]},
        )
    )
    assert job.enrichment_input_mode == EnrichmentInputMode.LOCAL_MATERIALS
    assert job.input_material_count == 1
    assert job.new_source_ids == ["source-2"]
    assert job.evidence_delta["new_source_ids"] == ["source-2"]


def test_child_enrichment_run_does_not_mutate_terminal_parent(app) -> None:
    async def scenario() -> None:
        persona = app.personas.create(
            display_name="Child Run Subject",
            aliases=[],
            persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
            run_mode="counterfactual_continuation",
        )
        task = app.compilation.create_task(persona.id)
        parent = PersonaCreationJob(
            id="terminal_parent",
            display_name=persona.display_name,
            persona_type=persona.manifest.persona_type,
            creation_mode="fictional",
            runtime_source="test",
            agent_id="fake_agent",
            model_id="fake-gpt-5",
            status="completed",
            persona_id=persona.id,
            compilation_task_id=task.id,
            job_config={"enrichment_input_mode": "local_materials"},
        )
        app.persona_creation._save(parent)
        child = await app.persona_creation.create_enrichment_run(
            parent_job_id=parent.id,
            enrichment_input_mode="local_materials",
            materials=[{"id": "m1", "content": "new evidence"}],
        )
        assert child.id != parent.id
        assert child.job_config["parent_job_id"] == parent.id
        assert child.job_config["input_material_count"] == 1
        assert app.persona_creation.get_job(parent.id).status == "completed"

    asyncio.run(scenario())
