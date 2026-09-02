from __future__ import annotations

from persona_continuum.domain.narrative import (
    AudienceState,
    CanonStatus,
    EpisodePlan,
    EpisodeVersion,
    ExecutableVideoProductionGuide,
    GenerationClip,
    KnowledgeState,
    NarrativeClue,
    NarrativeProject,
    NarrativeScene,
    PlotThread,
    ProductionGuideAsset,
    ProductionPackage,
    Shot,
    StoryBible,
    VideoModelProfile,
)


def test_project_defaults_are_format_agnostic() -> None:
    project = NarrativeProject(title="任意作品")
    assert project.format.value == "series"
    assert project.revision == 1
    assert project.canonical_world_branch_id is None


def test_story_bible_is_versioned_and_author_only_fields_exist() -> None:
    bible = StoryBible(project_id="p1", version=3, final_truth=["真相A"], author_notes="备注")
    assert bible.version == 3
    assert bible.final_truth == ["真相A"]


def test_knowledge_and_audience_states() -> None:
    assert KnowledgeState("suspected") == KnowledgeState.SUSPECTED
    assert AudienceState("partial") == AudienceState.PARTIAL


def test_episode_plan_and_version_roundtrip() -> None:
    plan = EpisodePlan(project_id="p1", episode_number=12, title="回收伏笔")
    data = plan.model_dump(mode="json")
    restored = EpisodePlan.model_validate(data)
    assert restored.episode_number == 12

    version = EpisodeVersion(project_id="p1", episode_number=12, version=2)
    assert version.is_canon is False


def test_scene_participants_carry_knowledge_constraints() -> None:
    scene = NarrativeScene(
        project_id="p1",
        episode_number=1,
        location="地下停车场",
        participants=[{"character_id": "c1", "goal": "得到真相", "must_not_reveal": ["数字方宁"]}],
    )
    assert scene.participants[0].must_not_reveal == ["数字方宁"]


def test_production_shot_fields() -> None:
    shot = Shot(shot_number=1, visual_prompt="interior", duration_seconds=3.0)
    data = shot.model_dump(mode="json")
    assert data["negative_constraints"] == []


def test_canon_status_defaults_to_noncanonical() -> None:
    assert CanonStatus.NON_CANONICAL.value == "noncanonical"
    assert PlotThread(project_id="p", title="t").status.value == "planned"
    assert NarrativeClue(project_id="p", title="c").status.value == "planned"


def test_production_package_defaults() -> None:
    package = ProductionPackage(project_id="p1", episode_number=1)
    assert package.shot_list == []


def test_generation_clip_guide_fields_default_and_old_json_roundtrip() -> None:
    """Guide-layer fields default empty: OLD serialized JSON that predates
    them must still validate (empty keeps old JSON compatible), and a full
    clip survives a JSON round-trip."""
    clip = GenerationClip(clip_number=1, duration_seconds=5.0)
    assert clip.copy_ready_prompt == ""
    assert clip.character_ids == []
    assert clip.prop_ids == []
    assert clip.location == ""

    legacy = {"clip_number": 2, "duration_seconds": 6.0, "prompt": "legacy text"}
    restored = GenerationClip.model_validate(legacy)
    assert restored.copy_ready_prompt == ""
    assert restored.character_ids == []
    assert restored.prop_ids == []
    assert restored.prompt == "legacy text"

    full = GenerationClip(
        clip_number=3,
        duration_seconds=8.0,
        location="office",
        copy_ready_prompt="self-contained copy-ready prompt",
        character_ids=["fang"],
        prop_ids=["怀表"],
    )
    data = full.model_dump(mode="json")
    assert GenerationClip.model_validate(data) == full


def test_video_model_profile_reference_prompt_syntax_default() -> None:
    profile = VideoModelProfile(id="vmp_x", display_name="X", vendor="V")
    # Conservative default: never assert an undocumented reference capability.
    assert profile.reference_prompt_syntax == "textual_anchor"
    data = profile.model_dump(mode="json")
    assert data["reference_prompt_syntax"] == "textual_anchor"
    assert VideoModelProfile.model_validate(data) == profile


def test_guide_models_importable_and_serializable() -> None:
    asset = ProductionGuideAsset(
        project_id="p1",
        asset_key="@CHAR_FANG_MASTER",
        asset_type="character",
        name="Fang",
    )
    assert asset.status == "NEEDED"
    assert asset.provenance == "deterministic"
    assert asset.necessity == "recommended"
    assert asset.reuse_scope == "episode"
    assert asset.generation_prompt == ""

    guide = ExecutableVideoProductionGuide(
        project_id="p1",
        episode_number=1,
        production_package_id="prod1",
        prompt_package_id="pkg1",
        episode_version_id="ver1",
        target_profile_id="generic",
    )
    assert guide.status == "drafting"
    assert guide.stale is False
    assert guide.parent_guide_id is None
    assert guide.revision_reason == ""
    assert guide.markdown_document == ""
    assert guide.required_assets == []
    assert guide.clip_workflows == []

    restored = ExecutableVideoProductionGuide.model_validate(
        guide.model_dump(mode="json")
    )
    assert restored == guide
