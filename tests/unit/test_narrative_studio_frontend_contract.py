"""Static contracts for the Narrative Studio workbench UX rewrite."""

from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "src" / "persona_continuum" / "web" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
NARR_HTML = HTML.split('id="view-narrative"', 1)[1].split('id="view-worlds"', 1)[0]
NARR_JS = JS.split("// ─── Narrative Studio", 1)[1].split("// ─── Event Listeners", 1)[0]


def test_information_architecture_uses_creator_tabs() -> None:
    for label in ("作品设定", "故事圣经", "角色", "全剧大纲", "单集创作", "连续性", "制作"):
        assert label in NARR_HTML
    assert 'id="btn-narr-tab-settings"' in NARR_HTML
    assert 'id="btn-narr-tab-outline"' in NARR_HTML
    assert 'id="btn-narr-tab-episode"' in NARR_HTML
    assert 'id="btn-narr-tab-continuity"' in NARR_HTML
    assert "角色阵容" not in NARR_HTML
    assert 'id="btn-narr-tab-knowledge"' not in NARR_HTML
    assert 'id="btn-narr-tab-clues"' not in NARR_HTML


def test_project_list_supports_multiselect_delete() -> None:
    assert 'id="narr-list-toolbar"' in NARR_HTML
    assert 'id="narr-select-all"' in NARR_HTML
    assert 'id="btn-narr-delete-selected"' in NARR_HTML
    assert "deleteSelectedNarrativeProjects" in NARR_JS
    assert "toggleNarrativeProjectSelection" in NARR_JS
    assert 'method: "DELETE"' in NARR_JS


def test_create_project_uses_modal_not_prompt() -> None:
    assert 'id="dlg-narr-new"' in HTML
    assert 'id="narr-new-title"' in HTML
    assert 'id="narr-new-logline"' in HTML
    assert 'id="narr-new-format"' in HTML
    assert 'id="narr-new-episodes"' in HTML
    assert "prompt(\"作品名称" not in NARR_JS
    assert "prompt(\"一句话简介" not in NARR_JS
    assert "prompt(\"角色名" not in NARR_JS
    assert "function fieldEl(id)" in NARR_JS
    assert '$(`${prefix}-title`)' not in NARR_JS
    assert "data-i=\"narrCreate\"" in HTML


def test_runtime_modal_hydrates_after_agent_scan() -> None:
    assert "hydrateRuntimeSelectors(" in NARR_JS
    assert "refreshOpenNarrativeRuntimeModal" in JS
    assert "populateNarrativeRuntimeModal" in NARR_JS
    assert "loadAgents(true)" in NARR_JS
    assert "runtime_source:" in NARR_JS
    assert "btn-narr-runtime-refresh" in HTML


def test_add_character_from_persona_library() -> None:
    assert "从人格库添加" in HTML
    assert "创建普通剧情角色" in HTML
    assert "添加并绑定" in HTML
    assert "openNarrativeAddCharacterModal" in NARR_JS
    assert "unbindNarrativePersona" in NARR_JS
    assert "Persona 排练仅允许已绑定 Persona 的角色" in NARR_JS


def test_runtime_settings_are_modal_with_shared_selectors() -> None:
    assert "⚙ 创作模型" in HTML
    assert 'id="dlg-narr-runtime"' in HTML
    assert "narr-runtime-grid" not in NARR_HTML
    assert "placeholder=\"agent\"" not in NARR_JS
    assert "bindSharedRuntimeSelector(" in NARR_JS
    assert "fillSharedRuntimeSelects(" in JS
    assert "getSelectableAgentsForSource(" in JS
    assert "getRuntimeModels(" in JS
    assert "getRuntimeReasoningOptions(" in JS
    assert "runtimeModelOptionLabel(" in JS
    assert "runtimeReasoningNotice(" in JS
    assert '["scene_actor", "Scene Actor"]' in NARR_JS
    assert "没有 READY / Connected Runtime" in JS


def test_persona_binding_and_lightweight_warning() -> None:
    assert "创建轻量虚构 Persona" in HTML
    assert "这不会运行完整 Persona Creation" in HTML
    assert "characters/bind" in NARR_JS
    assert "unmatchedBibleCharacters" in NARR_JS
    assert "同步到角色阵容" in NARR_JS


def test_scene_actor_routing_and_rehearsal_defaults() -> None:
    assert 'narrativePayload("scene_actor"' in NARR_JS
    assert "narrCast.slice(0, 6)" not in NARR_JS
    assert "Persona 排练" in NARR_JS
    assert "让绑定的人格在 NON-CANON 场景中自由互动" in NARR_JS
    assert "技术详情" in NARR_JS


def test_writer_room_does_not_auto_assign_story_personas() -> None:
    assert "filter(item => item.persona_id).slice(0, roles.length)" not in NARR_JS
    assert "writerRoomParticipants()" in NARR_JS
    assert "请先在创作模型设置中配置 Writer Personas" in NARR_JS
    assert "高级创作工具 · Writer Room" in HTML


def test_episode_pipeline_has_prerequisite_copy() -> None:
    assert "没有 EpisodePlan，无法生成剧本" in NARR_JS
    assert "没有 Draft，无法审核" in NARR_JS
    assert "Audit 存在 BLOCKING 问题" in NARR_JS
    assert "尚未提交正史，制作包可能不是最终版本" in NARR_JS
    assert "1 剧集计划" in NARR_JS
    assert "3 Persona 排练" in NARR_JS


def test_simple_mode_hides_technical_details() -> None:
    assert "body:not(.narr-advanced) .narr-tech" in (STATIC / "app.css").read_text(
        encoding="utf-8"
    )
    assert "pc-narr-advanced" in NARR_JS
    assert "AUTHOR ONLY" in HTML or "AUTHOR ONLY" in NARR_JS


def test_production_master_layer_badges_and_legacy_prompts() -> None:
    """Layer 1 keeps the master rendering; legacy prompts collapse with disclaimer."""
    assert "renderNarrativeProductionLayers" in NARR_JS
    assert "narrProductionPackageCard" in NARR_JS
    assert 't("prodCanonBadge")' in NARR_JS
    assert 't("prodPreviewBadge")' in NARR_JS
    assert 't("prodStaleBanner")' in NARR_JS
    assert 't("prodVideoPromptAdvanced")' in NARR_JS
    assert 't("prodVideoPromptDisclaimer")' in NARR_JS
    assert "narr-vp-legacy" in NARR_JS
    assert "narrVpBibleLine" in NARR_JS
    assert "Visual Bibles" in NARR_JS
    assert "character_visual_bible" in NARR_JS
    # Exact disclaimer copy must ship in the i18n dictionaries (full JS scope).
    assert "这是模型无关的运动/画面意图，不是可直接提交给特定视频模型的最终Prompt。" in JS


def test_video_generation_plan_layer_and_job_progress() -> None:
    """Layer 2 creates prompt-packages via 202 job and shows stage labels + counts."""
    assert "narrVpPlanFormHtml" in NARR_JS
    assert "narrVpCopyAllText" in NARR_JS
    assert "narrVpAssetsSection" in NARR_JS
    assert "renderNarrativePlanJob" in NARR_JS
    assert "pollNarrativePlanJob" in NARR_JS
    assert "submitNarrativePromptPackageJob" in NARR_JS
    assert "patchNarrativeClip" in NARR_JS
    assert 'planning_clips: "ppStagePlanningClips"' in NARR_JS
    assert "profile_update_available" in NARR_JS
    assert "profile.modes" in NARR_JS
    assert "profile.aspect_ratios" in NARR_JS
    assert "capabilities.supports_audio" in NARR_JS
    assert "durations.supported" in NARR_JS
    assert "reference_asset_ids" in NARR_JS
    # Stage labels with real counts only; never a fake percentage.
    assert "never a fake percentage" in NARR_JS
    assert "progress.completed" in NARR_JS
    assert "正在规划 Clips..." in JS
    assert "Profile已更新：当前Package基于旧Profile" in JS


def test_shooting_agent_panel_mirrors_director_panel() -> None:
    """Shooting panel reuses the Director structure and the shared runtime selector."""
    assert 'id="narr-shooting-panel"' in NARR_HTML
    assert 'id="btn-narr-shooting"' in NARR_HTML
    assert 'id="shoot-target-model"' in NARR_HTML
    assert 'data-shoot-mode="discuss"' in NARR_HTML
    assert 'data-shoot-mode="advise"' in NARR_HTML
    assert 'data-shoot-mode="agent"' in NARR_HTML
    assert 'id="btn-narr-shoot-save-runtime"' in NARR_HTML
    assert 'id="narr-shoot-thread"' in NARR_HTML
    assert 'id="narr-shoot-input"' in NARR_HTML
    assert 'name="shoot-source"' in NARR_HTML
    # Two clearly separated controls with helper copy.
    assert "目标视频模型 = 最终 Prompt 交给哪个视频生成模型。" in NARR_HTML
    assert "拍摄 Agent = 谁负责思考。" in NARR_HTML
    assert "openShootingPanel" in NARR_JS
    assert "SHOOTING_ACTION_LABELS" in NARR_JS
    assert "revise_clip_plan" in NARR_JS
    assert "waiting_for_canon_approval" in NARR_JS
    assert 'bindSharedRuntimeSelector("shoot"' in NARR_JS
    assert 'readSharedRuntime("shoot")' in NARR_JS
    assert "shooting_agent: cfg" in NARR_JS
    # Runtime per-stage override exposes the shooting stage via shared controls.
    assert '["shooting_agent", "Shooting Agent"]' in NARR_JS
    assert '"Shooting Agent": "stageShootingAgent"' in NARR_JS


def test_production_guide_layer_and_drawer() -> None:
    """Layer 3 schedules the guide job, renders a summary card and a drawer."""
    assert "narrVpGuideSection" in NARR_JS
    assert "narrVpGuideSummaryCard" in NARR_JS
    assert "loadNarrativeGuide" in NARR_JS
    assert "createNarrativeGuideJob" in NARR_JS
    assert "pollNarrativeGuideJob" in NARR_JS
    assert "renderNarrativeGuideJob" in NARR_JS
    assert "NARR_GUIDE_STAGE_LABELS" in NARR_JS
    assert "rendering_guide: \"prodGuideStageRenderingGuide\"" in NARR_JS
    assert "openNarrativeGuideDrawer" in NARR_JS
    assert "handleNarrativeGuideDrawerAction" in NARR_JS
    assert 'drawer.id = "narr-guide-drawer"' in NARR_JS
    # Section-level actions plus drawer-level actions (copy + export wiring).
    assert 'data-narr-vp-action="guide-create"' in NARR_JS
    assert 'data-narr-vp-action="guide-open"' in NARR_JS
    assert 'data-narr-guide-action="copy-all"' in NARR_JS
    assert 'data-narr-guide-action="copy-asset"' in NARR_JS
    assert 'data-narr-guide-action="copy-clip"' in NARR_JS
    assert 'data-narr-guide-action="export-md"' in NARR_JS
    assert "production-guides/" in NARR_JS
    assert "copy_ready_prompt" in NARR_JS
    assert "generation_prompt" in NARR_JS
    # The drawer markdown goes through the shared safe-rendering bridge.
    assert "renderMessageBody(" in NARR_JS


def test_production_guide_i18n_keys_present_in_both_locales() -> None:
    """Every prodGuide* key ships in BOTH the zh-CN and the en dictionary."""
    zh_block = JS.split('"zh-CN": {', 1)[1].split("\n    en: {", 1)[0]
    en_block = JS.split("\n    en: {", 1)[1].split("\n  };", 1)[0]
    guide_keys = (
        "prodLayerGuide",
        "prodLayerGuideHelp",
        "prodGuideCreate",
        "prodGuideRegenerate",
        "prodGuideCreating",
        "prodGuideReady",
        "prodGuideOpen",
        "prodGuideCopyAll",
        "prodGuideExportMd",
        "prodGuideCopyAssetPrompt",
        "prodGuideCopyClipPrompt",
        "prodGuideCopyHint",
        "prodGuideAssetsNeeded",
        "prodGuideAssetChar",
        "prodGuideAssetLoc",
        "prodGuideAssetProp",
        "prodGuideAssetOther",
        "prodGuideClips",
        "prodGuideTotalDuration",
        "prodGuideStaleBanner",
        "prodGuideClose",
        "prodGuideEmpty",
        "prodGuideStageLoadingSource",
        "prodGuideStageAnalyzingAssets",
        "prodGuideStageCompilingAssetPrompts",
        "prodGuideStageCompilingClipPrompts",
        "prodGuideStageRenderingGuide",
    )
    for key in guide_keys:
        assert f'{key}: "' in zh_block, key
        assert f'{key}: "' in en_block, key
    # Stage labels mirror the backend GUIDE_STAGE_LABELS wording.
    for label in (
        "正在读取制作源...",
        "正在分析参考素材...",
        "正在编译素材图片 Prompt...",
        "正在编译完整视频 Prompt...",
        "正在渲染制作手册...",
    ):
        assert label in zh_block
