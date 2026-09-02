# Executable Video Production Guide（完整视频制作手册）验收报告

日期：2026-09-02 · 范围：Executable Video Production Guide（Spec 计划 §0–§11 全量实施 · Guide Tasks A–E 代码与测试 + 本报告文档、全量质量门与浏览器 E2E 复核）

## 1. 真实代码变化

| 层 | 文件 | 变化 |
| --- | --- | --- |
| 领域模型 | `domain/narrative.py` | `GenerationClip` 追加 `copy_ready_prompt / character_ids / prop_ids`；`VideoModelProfile` 追加 `reference_prompt_syntax`（默认 `textual_anchor`）；新增 `ProductionGuideAsset`（`vga_`）与 `ExecutableVideoProductionGuide`（`vgud_`，clip 快照副本，绝不回写源包） |
| Profile | `narrative/video_profile_registry.py` + 6 个内置 YAML | `profile_capabilities_digest` 加入 `reference_prompt_syntax`；六个内置 Profile 全部保守缺省 `textual_anchor`（官方资料未证实 @命名引用语法，不臆造） |
| 纯函数 | `narrative/clip_planner.py` | Pass 2 物化处为每个 clip 填 `character_ids`（来源 shots 去重，`plan_clips` 签名未变） |
| 纯函数 | `narrative/video_prompt_compiler.py` | 编译循环后 prop 归属（bible 条目名确定性命中 shot 描述 → `prop_ids`）；新增 `compile_copy_ready_prompt`（旧 `compile_clip_prompt` 零改动叠加；独立预算豁免 `max_prompt_chars`；location 无法解析 raise `SHOOTING_LOCATION_CONTEXT_MISSING`）；`_GENERIC_LOCATION_ANCHOR` 加注「仅限旧编译器；新手册层禁止」 |
| 手册渲染（NEW） | `narrative/video_production_guide.py` | 纯函数渲染层：`analyze_asset_necessity`（无硬编码故事名）/ `build_reference_asset_prompts`（完整图片 Prompt，status 永不 `BOUND`）/ `build_frame_chain` / subtitle·screen_composite·dialogue·sound·bgm·editing·checklist 计划 / `build_executable_video_production_guide` / `render_guide_markdown`（13 节） / `PLACEHOLDER_PATTERNS` 全文档 fail-closed |
| 错误码 | `narrative/runtime.py` | 新增 `SHOOTING_LOCATION_CONTEXT_MISSING`、`VIDEO_GUIDE_SOURCE_NOT_READY` |
| 服务层 | `application/narrative_service.py` | job kind `video_production_guide`（5 阶段，真实标签无假百分比）+ `generate_video_production_guide_async`（canon/preview 门、`clip_plan_fingerprint` 幂等复用、checkpoint、LLM 批精修 overlay + 黑名单降级、supersede 携 `parent_guide_id/revision_reason`）+ stale 联动钩子并列追加 |
| 存储 | `storage/migrations.py`、`narrative/repository.py` | `narrative_video_production_guides` 表（status/stale/context_fingerprint/…+guide_json）+ 双索引；save/get/list + `mark_video_production_guides_stale(_for_prompt_package/_for_production)` |
| Shooting | `narrative/shooting.py`、`application/shooting_service.py` | SAFE_WRITE 第 9 项 `build_complete_production_guide` + `_write_build_complete_production_guide`（job-less 直调异步流水线，artifacts 摘要） |
| API | `web/api.py`、`web/server.py` | +4 端点：`POST …/production/{pkg}/production-guide`（202 job + fail-fast）、`GET …/production/{pkg}/production-guide`、`GET …/production-guides/{id}`（派生 stale / profile_update_available）、`GET …/production-guides/{id}/export`（`text/markdown; charset=utf-8` + attachment，直读已持久化 markdown）；错误映射 +2；**用户 export-zip 代码零改动** |
| UI | `web/static/app.js`、`app.css` | 制作页两层 → 三层：第三块「完整制作手册」（生成按钮 → job 面板真实阶段标签 → 摘要卡 → [打开完整手册] 全页 Drawer（safe_markdown 连续文档 + 逐条图片/视频 Prompt 复制按钮 + 复制完整手册 + 导出 Markdown 下载））；Clip 卡编辑器与 export-zip 原样保留 |
| 文档 | `docs/NARRATIVE_VIDEO_PRODUCTION_PIPELINE.md` | 新增 10 节（见 §2）+ UI Workflow 三层 + API/动作/job kinds/Testing 更新 |
| 测试 | `tests/unit/test_narrative_video_production_guide.py`（12例）、`tests/unit/test_video_production_guide_pipeline.py`（8例）+ 既有文件定点补齐 | 20 个新测试（名称见 §11） |

## 2. 参考文档依据与手册结构

依据项目外部设计说明《视频生成模型.md》（不包含在本仓库中）：@GOOSE_MASTER/@WORLD_MASTER 永久素材体系（身份/空间锚点）、逐段生成 + 尾帧接首帧、字幕/对白/音效时间轴、整集一条 BGM。映射为本手册 13 节结构（`_SECTION_TITLES`）：

一、制作目标 → 二、目标视频模型与基础参数 → 三、永久参考素材 → 四、本集场景/道具素材 → 五、视频结构总览表 → 六、逐 Clip 工单 → 七、尾帧接续流程 → 八、字幕时间轴 → 九、对白与音效 → 十、BGM → 十一、剪辑与转场 → 十二、成片合成顺序 → 十三、最终检查清单。

`docs/NARRATIVE_VIDEO_PRODUCTION_PIPELINE.md` 同步新增节：Executable Video Production Guide（定义/数据流/生成时机/staleness 规则）、Reference Asset Prompt Pipeline（REQUIRED/RECOMMENDED/OPTIONAL 自动分析、状态生命周期 NEEDED/PROMPT_READY/EXISTING/GENERATED/BOUND，绝不伪造文件）、Master Reference vs Start Frame（三锚点）、Copy Ready Prompt、Frame Chain、Screen Composite Plan、Subtitle、SFX、BGM（整集单条 Prompt）、Editing。

## 3. 手册生成走查：确定性 + LLM 精修双路径

- **确定性路径（唯一事实源）**：`test_guide_pipeline_happy_path_deterministic`（零 LLM → ready）；
  `test_guide_markdown_is_deterministic_across_builds`（相同输入 + 固定 created_at → 逐字节一致）；
  `test_guide_markdown_renders_all_thirteen_sections_idempotently`（13 节幂等）。
- **LLM 精修路径（浏览器 E2E 实测，fake responder 脚本化）**：本跑 `runtime_trace.pipeline` =
  `{"stage": "rendering_guide", "generation_mode": "agent", "batch_count": 2, "refined_asset_count": 3,
  "fallback_asset_count": 0, "refined_clip_count": 1, "fallback_clip_count": 0,
  "clip_plan_fingerprint": "ee99e2…46253", "context_fingerprint": "07ca03…2aa"}` ——
  素材图片 Prompt 与 clip copy-ready Prompt 均走批精修 overlay，失败批整批降级确定性基线（计数入 trace）。
- **幂等与改版**：同 fingerprint 非 stale ready 手册直接复用；clip 计划漂移 → 旧手册置 stale +
  新手册 `parent_guide_id` + `revision_reason`（`test_guide_pipeline_is_idempotent_per_prompt_package` /
  `test_guide_pipeline_supersedes_on_clip_plan_drift`）。

## 4. Copy Ready Prompt：自包含 + 无占位符 + location fail-closed

- 自包含：单块多段（目标与序号 / REFERENCE INPUTS / 人物身份 / 环境身份 / 动作 / 表演 / 摄影机 /
  时间发展 / 环境运动 / AUDIO / CONTINUITY / ENDING / STRICT），不读其它字段即可成立
  （`test_copy_ready_prompt_is_self_contained_with_all_sections`）；`reference_prompt_syntax`
  三档分支锁定（`test_copy_ready_prompt_textual_anchor_start_frame_phrasing` 等）。
- 无占位符：`PLACEHOLDER_PATTERNS`（the established location / per shot / natural subject motion /
  TBD / TODO / 待定）对全部渲染文本 fail-closed（`GUIDE_PLACEHOLDER_DETECTED`）；LLM 精修输出过同一
  黑名单（`test_apply_clip_prompt_enrichment_merges_by_clip_id_and_rejects_placeholders`）；
  **旧编译器哨兵断言（`test_narrative_video_prompt_compiler.py` 的 "the established location"）零改动通过**。
- location fail-closed：`test_build_guide_fails_closed_on_unresolvable_location` +
  `test_copy_ready_prompt_fails_closed_on_missing_location`；API 侧映射 422。浏览器 E2E 种子的
  `location_visual_bible`（Office）与 clip locations 精确匹配，走查未触发（预期）。

## 5. 三锚点：Master Reference vs Start Frame

- Character Master = **身份锚点**（`@CHAR_<NAME>_MASTER`，跨镜复用同一张身份图）；
- Environment Master = **空间锚点**（`@LOC_<NAME>_MASTER`）；
- Previous End Frame = **物理连续性锚点**（`FRAME_NN`，每 chained clip 的接续首帧），与母版类资产
  明确区分为两类（`continuity_workflow.anchors` 三者并列）。
- 闭合性：markdown/工单引用的 @token 必存在于 required_assets
  （`test_markdown_asset_tokens_are_closed_under_required_assets`）；分类不依赖故事专名
  （`test_analyze_classifies_assets_from_structure_not_names` / `…_survives_fixture_rename`）；
  素材状态绝不 `BOUND`（`test_reference_asset_prompts_are_complete_and_never_bound`）。

## 6. 尾帧链（Frame Chain）验证

`test_frame_chain_links_three_clips`：Clip 01 无起始帧；Clip 02 起始 = `FRAME_01`；每 clip
「生成后操作」= 最后 0.5–1.0 秒选稳定尾帧（按时长适配区间）+ 保存名 + 下一段用途；末镜
「收尾（不产接续帧）」，为字幕/fade 留干净画面。浏览器 E2E 种子为 1 clip（无链），故 E2E 无
FRAME_NN 资产——诚实记录，链路正确性由上述单测覆盖。

## 7. Stale 传播与换模型联动

- 上游钩子并列追加：prompt package supersede（`test_guide_stale_propagates_from_prompt_package_hook`）、
  production master 重生成（`test_guide_stale_propagates_from_production_hook`）、母版/prompt 包
  supersede、fingerprint 清扫、clip PATCH 保存路径、`_write_set_target_model`（换目标视频模型 →
  `mark_video_production_guides_stale…`，受影响手册读取时派生 `stale=true` 与
  `profile_update_available`）。
- 来源门：prompt 包未 ready / 非 canon / preview → **409** `VIDEO_GUIDE_SOURCE_NOT_READY`
  （`test_guide_pipeline_rejects_unready_source_package`）。

## 8. Shooting Agent 集成

SAFE_WRITE 9 项（+`build_complete_production_guide`：args `prompt_package_id` 必填 +
可选 `production_package_id`）；`test_build_complete_production_guide_is_a_safe_write_action` 锁定
注册与风险级；与 Director registry 集合级零交集断言保持；`SHOOTING_LOCATION_CONTEXT_MISSING`
经既有 finish 路径透出。

## 9. API 契约（E2E 网络实测）

| 端点 | 方法 | 实测 |
| --- | --- | --- |
| `/api/narratives/{pid}/production/{pkg}/production-guide` | POST | **202**（application/json，job `njob_42c2f50a0aad479c`） |
| `/api/narrative-jobs/{id}` | GET | **200** ×N（浏览器内 1 次 + 走查紧轮询线程若干次，全部 200） |
| `/api/narratives/{pid}/production/{pkg}/production-guide` | GET | **200**（guide `vgud_2dc539a4c2d64744`，status=ready，stale=false） |
| `/api/narratives/{pid}/production-guides/{id}/export` | GET | **200** `text/markdown; charset=utf-8`，attachment `guide-ep01-vgud_2dc539a4c2d64744.md`（16090 字节，含一→十三） |

唯一 4xx：进入制作页时 `loadNarrativeGuide` 对"尚无手册"的预探测 GET 返回 **404**（app.js 注释
明确该 404 属预期：存 null；与 favicon 404 同列已知无害），对应一条资源加载 console 记录，无其他
console 错误、无其他 4xx。

## 10. 人工 Walkthrough（真实浏览器 E2E，Playwright + 系统 Chrome headless）

环境：系统 Python 3.12 的 Playwright + 本地 Google Chrome（headless，1440×1000 @2x）；
受控服务器 `127.0.0.1:8793` + scratch 数据目录 + `include_fake_agent=True`（全部 LLM 走脚本化
responder，零外网）；种子链 project→outline→draft→audit→commit→production package（canon，
location_visual_bible 与 clip locations 匹配）→ prompt package ready。走查步骤：三层渲染与空态
→ 点 [生成完整制作手册] → 捕获 POST 202 → job 完成紧轮询 → 摘要卡 → [打开完整手册] 抽屉 →
导出下载 → Clip 工单截图 → 关抽屉 → 回归抽查。截图位于 `docs/reports/acceptance/screenshots/`：

| 截图 | 验证点 |
| --- | --- |
| `07_production_guide_block.png` | 第三块「完整制作手册 · EP01」摘要卡：**EP01 AI视频完整制作手册** + `目标视频模型：Generic Video Model` `16:9` + `需要准备：1 人物参考 · 1 场景参考 · 0 道具参考 · 1 其他参考` + `视频片段：1 · 预计总时长：5s` + [打开完整手册] / [重新生成手册] |
| `08_guide_drawer.png` | 全页 Drawer 顶部：标题 + [复制完整手册] [导出 Markdown] [✕ 关闭] + 「每条 Prompt 独立复制」提示 + 连续 markdown（制作目标 → 二、目标视频模型与基础参数 → 三、永久参考素材 → 3.1 `@CHAR_FANG_MASTER · 角色母图（身份锚点）`…） |
| `09_guide_clip_workorder.png` | Clip 工单卡：`Clip 1 · 5s` + 用途/生成模式（文生视频）/连续性 + **copy-ready prompt 块（172 字符自包含）** + 推荐设置 + [复制视频Prompt] |

断言结果（19/19 PASS）：

- 三层渲染（`.narr-prod-layer` ×3）、第三块标题「完整制作手册」、空态提示「还没有手册…」；
- POST 202 + job 完成紧轮询：实测中间阶段 `compiling_asset_prompts → compiling_clip_prompts →
  completed`（全部属于 5 个真实阶段集合；UI 标签「正在生成手册...」**无假百分比**；其余阶段快于
  5ms 轮询窗口，最终阶段 `rendering_guide` 由 `runtime_trace.pipeline.stage` API 真值佐证，完整
  5 阶段序列由 GUIDE_STAGE_LABELS 单测/前端契约测试锁定）；
- 摘要卡六字段齐备；抽屉 markdown 连续（一、制作目标 → 十三、最终检查清单，10700 字符）；
- 逐条复制按钮 asset×3 / clip×1，[复制完整手册] 与 [导出 Markdown] 各 1；点击导出触发
  **download 事件**（16090 字节 .md，内容含一/十三节）；
- 页面内 fetch 复核导出响应头：200 + `text/markdown; charset=utf-8`；
- 回归抽查：方案层 Clip 编辑器完好（select ×6、复制 Prompt 按钮 ×1）、用户 export-zip 按钮 ×1 原样；
- 清理：服务器关闭（8793 进程终止）、scratch 数据目录与全部临时脚本删除——**截图是唯一痕迹**；
  截图 01–06（上一验收报告）原样保留，本轮仅新增 07–09。

## 11. 测试与质量门

新测试 2 文件 20 例（Guide Task E 补齐后全量基线）：

- `tests/unit/test_narrative_video_production_guide.py`（12）：analyze_classifies_assets_from_structure_not_names /
  analyze_classification_survives_fixture_rename / reference_asset_prompts_are_complete_and_never_bound /
  build_reference_asset_prompts_refills_blank_assets_deterministically / frame_chain_links_three_clips /
  build_executable_guide_full_structure / guide_markdown_renders_all_thirteen_sections_idempotently /
  guide_markdown_is_deterministic_across_builds / markdown_asset_tokens_are_closed_under_required_assets /
  build_guide_fails_closed_on_unresolvable_location / apply_asset_prompt_enrichment_merges_good_rows_only /
  apply_clip_prompt_enrichment_merges_by_clip_id_and_rejects_placeholders
- `tests/unit/test_video_production_guide_pipeline.py`（8）：guide_pipeline_happy_path_deterministic /
  guide_pipeline_is_idempotent_per_prompt_package / guide_pipeline_rejects_unready_source_package /
  guide_pipeline_supersedes_on_clip_plan_drift / guide_stale_propagates_from_prompt_package_hook /
  guide_stale_propagates_from_production_hook / guide_pipeline_agent_refinement_overlays_and_counts /
  guide_pipeline_agent_fallback_keeps_deterministic_baseline
- 另有定点补齐：prompt compiler +3（self-contained / textual anchor phrasing / missing-location fail-closed）、
  profile registry +1（reference_prompt_syntax 缺省）、shooting agent +1（新动作 SAFE_WRITE）。

门禁（Guide Task F 全新运行，精确输出）：

- `uv run ruff check src tests` — **All checks passed!**
- `uv run mypy` — **Success: no issues found in 214 source files**
- `uv run python -m compileall -q src` — **PASS**（无输出）
- `git diff --check` — **PASS**（无空白错误）
- `node --check src/persona_continuum/web/static/app.js` — **PASS**
- 全量 pytest：**1148 passed, 8 skipped, 0 failed**（本任务线全量记录；按任务约束 22 分钟套件
  不在 Task F 重跑，本轮零代码改动仅文档/报告/截图）

既有契约回归：用户 export-zip 两集成用例（`tests/integration/test_narrative_shooting_api.py` 的
zip 测试）保持绿；旧编译器哨兵断言零改动；`narrative/director.py` 与
`narrative_director_service.py` 零改动。

## 12. 已知限制

- E2E 阶段观测：fake responder 快于紧轮询窗口，浏览器走查实捕 2/5 中间阶段；完整 5 阶段序列
  由单元/契约测试与 `runtime_trace.pipeline.stage=rendering_guide` 佐证（无任何伪造进度证据）。
- 浏览器种子为 1 clip：FRAME_NN 尾帧链与多镜总览表在 E2E 中无实例，由 3-clip 单测覆盖。
- LLM 精修批失败时整批降级确定性基线（行为正确，表达不如 LLM 丰富；refined/fallback 计数入
  `runtime_trace.pipeline` 供人工复核）。
- 手册为纯导出层：不回写源 ModelPromptPackage（clip 快照副本），旧 `compile_clip_prompt` 逻辑与
  `_GENERIC_LOCATION_ANCHOR` 仅服务旧路径。
- Profile 层限制沿用上一份验收报告（kling draft、seedance 快照级验证等）。
