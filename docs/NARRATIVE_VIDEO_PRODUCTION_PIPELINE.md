# Narrative Video Production Pipeline（视频生成方案流水线）

本管线把一个**已提交正史**的制作母版（Production Package）转换为**针对特定视频模型**的
完整生成方案（Model Prompt Package）：确定性 Clip Plan → 参考素材登记 → 批量 Prompt
编译 → 整包校验。在其上再叠加第三层**可执行制作手册**（Executable Video Production
Guide）：把编译完成的方案变成自包含、逐条可复制的拍摄工单。它不替代 Director（故事与
正史仍归 Director/人工管辖），也不伪造任何模型能力——无法从官方文档验证的能力一律记为
`null`（未知），绝不编造。

```text
Canon Episode Version
        ↓  generate_production_package（canon gate）
Production Package（制作母版，模型无关）
        ↓  generate_model_prompt_package_async（4 阶段后台任务）
Model Prompt Package（目标视频模型专属，clip 级 Prompt）
        ↓  Shooting Agent（自然语言修改 clip plan / prompt）
最终可提交给视频模型 API 的逐 clip 生成方案
        ↓  generate_video_production_guide_async（video_production_guide 任务，5 阶段）
Executable Video Production Guide（可执行制作手册：素材图 Prompt + 逐镜 Copy-Ready Prompt）
```

核心文件：

```text
narrative/video_profiles/*.yaml           6 个内置 VideoModelProfile（官方调研数据）
narrative/video_profile_registry.py       加载 / 查询 / 能力摘要 digest
narrative/clip_planner.py                 确定性 Clip 规划（merge/split）
narrative/video_prompt_compiler.py        确定性 Prompt 基线编译器
narrative/video_production_guide.py       可执行制作手册渲染层（素材/尾帧链/工单/后期）
narrative/shooting.py                     Shooting Agent 契约（registry/schema/上下文构建）
application/shooting_service.py           Shooting 会话循环（镜像 Director 骨架，瘦身）
application/narrative_service.py          4+5 阶段任务流水线 + canon gate + stale 传播
narrative/repository.py                   package/asset/shooting 三件套持久化
storage/migrations.py                     5 张新表（幂等，对旧 DB 零影响）
web/api.py + web/server.py                REST 端点
web/static（index.html/app.js/app.css）   制作页三层 UI + 拍摄 Agent 面板
```

## Production Master（制作母版）

`generate_production_package` 产出模型无关的结构化母版：剧本、节拍表、Shot List、
三套 Visual Bible、Dialogue/Subtitle 轨、SFX 计划、BGM 方向、连续性备注。
只有引用**已提交正史**（canon episode version）的母版才是正式生产源。

`Shot` 新增制作意图字段（全部带默认值，旧 JSON 兼容）：
`visual_intent / camera_intent / subject_motion_intent / environment_motion_intent /
emotion_intent / audio_intent / generation_notes`。它们是规划与编译的**输入意图**，
不直接作为最终 Prompt 提交给任何模型。

## Shot vs Generation Clip

**Shot** 是剪辑意义上的镜头（母版产物）；**GenerationClip** 是一次视频模型生成调用
（时长受目标模型能力约束）。两者**绝非 1:1**：

- 相邻同场景、同地点、无对白、cut 过渡的短镜头会**合并**为一个 clip
  （`source_shot_numbers` 跨多个 shot）；
- 超过模型时长上限的单镜头会**拆分**为 ceil 个 clip（每个 clip 都共享同一 source shot）；
- 对白镜头永不跨对白边界合并；非 cut 过渡（dissolve 等）强制 clip 边界。

## VideoModelProfile

每个目标视频模型对应一个 YAML Profile（`narrative/video_profiles/`），由
`video_profile_registry.load_builtin_profiles()` 在首次访问时加载并缓存。
字段分三类：

- 身份：`id / display_name / vendor / model_family / model_version / profile_version /
  verification_status(verified|draft|custom) / verified_at / official_sources`；
- 能力：`supported_modes / supported_durations_seconds / duration_min/max_seconds /
  supported_aspect_ratios / supports_audio / supports_dialogue / supports_sfx /
  supports_image_to_video / supports_text_to_video / supports_first_frame /
  supports_last_frame / supports_first_last_frame / supports_reference_images /
  max_reference_images / supports_timestamp_prompting / supports_negative_prompt /
  negative_prompt_strategy`；
- 策略：`prompt_language_preferences / prompt_strategy / clip_planning /
  continuity_strategy / recommended_defaults`。

**能力布尔字段类型是 `bool | None`，`None` 表示未知（官方文档查不到）**，下游对所有
能力判断必须显式区分 `False`（确认不支持）与 `None`（未知，保守处理），例如
`supports_last_frame is not False` 才启用首尾帧链式参考。

内置 6 个 Profile：

| id | display_name | vendor | 状态 | 要点 |
| --- | --- | --- | --- | --- |
| `generic` | Generic Video Model | generic | verified | 模型无关基线，prefer 8s / max 10s，无枚举约束 |
| `veo_3_1` | Google Veo 3.1 | google | verified | `veo-3.1-generate-001`；4/6/8s；9:16/16:9；原生音频 + 负向提示词；时间戳序列等未知能力记 null |
| `runway_gen_4_5` | Runway Gen-4.5 | runway | verified | `gen4.5`；2–10s；仅首帧参考；无负向提示词字段；时间戳序列提示支持；prompt 上限 1000 字符 |
| `hailuo` | MiniMax Hailuo | minimax | verified | `MiniMax-H3`；4–15s；首尾帧 + 参考（图≤9）；音频经 reference_audio；无负向提示词 |
| `seedance` | ByteDance Seedance | bytedance | verified（快照级） | Doubao Seedance 2.5/2.0 系；参考图 0–30；30s 直出；多镜头叙事；中英文；枚举缺口标 null |
| `kling` | Kling AI | kuaishou | **draft** | 仅记 kling-v3/v3-omni、音画同出、3–15s；参数级字段全部 null |

所有官方来源 URL 写在各 YAML 的 `official_sources`。`get_profile(unknown_id)` 抛
`VIDEO_PROFILE_NOT_FOUND`。`profile_capabilities_digest()` 输出仅含已验证能力的摘要，
供 Shooting Agent 上下文注入（不给模型看编造能力的机会）。

## Profile 版本化

- 创建方案时把注册表当前 `profile_version` 钉死到 `ModelPromptPackage.target_profile_version`；
- Profile 日后更新**不删除旧包**、不改写历史；
- 读取时派生 `profile_update_available = (target_profile_version != 注册表当前版本)`，
  UI 据此显示横幅并提供 [重新编译]。

## Clip Planner

`clip_planner.plan_clips(shots, profile, aspect_ratio, quality_priority)` 是零 I/O
的确定性纯函数，禁止 1 Shot = 1 Clip 假设：

- `effective_max = min(max(prefer, hard_min), hard_max)`，其中 prefer 来自
  `clip_planning.prefer_duration_seconds`（默认 8s），hard min/max 来自 profile 时长包络；
- **Merge**：相邻 shot 同地点、两侧无对白、cut（或空）过渡、合计时长 ≤ effective_max、
  已合并 shot 数 < `clip_planning.max_major_actions`（默认 2）→ 合并为一个 clip；
- **Split**：单 shot 超过 effective_max → 拆成 ceil 个 part，每个 part 独立成 clip
  共享同一 `source_shot_numbers`，时长 snap 到最近的受支持值（不低于 hard_min）；
- **对白**：对白行绝不跨 clip 边界；
- **过渡**：非 cut 过渡强制边界；
- **模式**：近景/中景且含角色、且 profile 确认支持 I2V → `image_to_video`，
  否则 `text_to_video`；
- **连续性**：相邻同场景 clip 在 profile 未确认禁用尾帧时挂
  `previous_clip_end_frame` 约束；
- 每个 clip 写 `purpose / planning_rationale / recommended_settings`（含时长、比例、
  目标时长与质量策略）。

同一组 shots 在不同 profile 下产出不同计划（例：generic prefer 8s 会合并 4s 相邻镜头，
runway prefer 5s 则全部独立；12s 超限镜头 generic 拆 2、runway 拆 3）。

## Reference Assets

`ProductionAsset` 登记生成所需素材（`character_reference / location_reference /
prop_reference / start_frame / end_frame / style_reference / other`）。
`source_uri / local_path` 可空——**为空即"未准备"**（UI 显示未准备标记），流水线只做
登记与引用提取，绝不伪造本地文件。`planning_assets` 阶段按 clip plan 幂等提取引用
（已存在跳过）。

## Prompt Compiler

`video_prompt_compiler.compile_clip_prompt / compile_prompt_package` 是确定性基线
编译器（LLM 批量编译失败时的兜底，其输出同样接受宿主能力校验）：

- **I2V**（存在参考图/首帧资产）：Prompt 聚焦 subject motion、camera movement、
  environment motion 与 temporal progression，**不重复**静态视觉（不复读角色圣经全文）；
- **T2V**：补全 subject visual / environment / composition / lighting / style 段落；
- **负向提示词**：`supports_negative_prompt=False`（如 Runway）→ `negative_prompt=None`，
  并把否定约束改写为正向表述（`"no camera movement"` → `"locked camera"`，
  positive phrasing only）；
- **音频路由**：`supports_audio` 为 False/None → `audio_prompt=None`，音频意图保留在
  `postproduction_audio_plan`；`supports_audio=True`（如 Veo）→ 生成 `audio_prompt`；
- **语言**：按 `prompt_language_preferences` + 用户 `prompt_language` override；
- **trace**：每 clip 记录 `compiler_trace`（profile_id / profile_version /
  generation_mode / 编译路径）。

## Canon Gate

`generate_production_package`（正式包）要求该集存在**已提交正史**；无 canon 时抛
`NARRATIVE_PRODUCTION_CANON_REQUIRED`（HTTP 409），不再回退到最新草稿。
显式预览可传 `is_preview=True`：保留旧降级行为，产出包 `is_preview=True`，
UI 醒目标记 **PREVIEW / NON-CANON**，且默认禁止其作为 Shooting 流水线的正式源
（POST prompt-packages 对 preview 源抛 `SHOOTING_SOURCE_PREVIEW_NOT_ALLOWED`，409）。

## 多阶段任务流水线

`generate_model_prompt_package_async(project_id, production_package_id, profile_id,
options)` 注册为后台任务 kind=`model_prompt_package`，POST prompt-packages 返回 202
+ job_id（复用既有 jobs 暂停/重试/取消/WS 全套）。四阶段：

1. **planning_clips**：clip planner 产出 Clip Plan；宿主逐 clip 校验时长/比例/模式
   ∈ profile 能力，违例 fail-fast（`VIDEO_PROFILE_DURATION_UNSUPPORTED` 等，422）。
   完成即 UPSERT package 行（status=`clip_planned`）——断点续跑检查点；
2. **planning_assets**：从 clip plan 幂等提取引用资产；
3. **compiling_prompts**：按批（3–5 clips）结构化编译，批间检查暂停/取消；单批失败
   先修复重试、降级 batch=1、最终回退确定性编译器；宿主对每 clip 再次能力校验
   （LLM 说了不算）；
4. **validating**：整包校验（clip 覆盖所有 source shots、连续性引用闭合）后置
   status=`ready`。

进度诚实：阶段名 + 真实计数（`percent` 仅完成时为 100），无捏造百分比。
幂等：同 (production_package_id, profile_id) 且非 stale 的半成品入口跳过已完成阶段。

任务 kind 共两种：`model_prompt_package`（上述 4 阶段）与 `video_production_guide`
（5 阶段，见下节「Executable Video Production Guide」）。

## Stale Rules

- 正史 EpisodeVersion 变更（重新 commit / patch bump revision）→ 该集 ProductionPackage
  fingerprint 不匹配置 `stale=True`，其 ModelPromptPackages 一并置 stale；
- 重新生成 ProductionPackage → 新建母版，旧母版派生的 packages 置 stale；
- Profile 更新 → 旧包保留，读取时派生 `profile_update_available`。

旧数据永不删除，UI 用横幅 + [重新编译] 引导。

手册层联动的 stale 规则：

- ProductionPackage / ModelPromptPackage 置 stale → 关联手册经
  `mark_video_production_guides_stale_for_production` /
  `mark_video_production_guides_stale_for_prompt_package` 一并置 stale；
- 手册自身改版：源包 clip plan 指纹或钉死的 profile 版本漂移 → 旧手册 stale、
  新手册记录 `parent_guide_id` + `revision_reason`（详见下节幂等与改版）。

## Executable Video Production Guide（可执行制作手册）

第三层**确定性导出层**（`narrative/video_production_guide.py`），叠加在某个
`status=ready` 且未 stale 的 Model Prompt Package 之上，把编译完成的 clips 变成
**自包含、人可操作**的拍摄手册：参考素材完整图片 Prompt、每条 clip 的 Copy-Ready
视频 Prompt、尾帧接续工作流、字幕/对白/音效/BGM/剪辑/最终检查清单。手册**不替代**
Prompt Package：精修只发生在副本上，源包永不被修改；装配纯函数零 I/O、零 LLM、
确定性（相同输入 + 固定 `created_at` → 逐字节相同的 markdown）。

数据流与 5 阶段任务：

```text
Model Prompt Package（ready 且非 stale）
        ↓  POST .../production/{pkg}/production-guide → 202 + job（kind=video_production_guide）
1. loading_source          校验源包 ready/非 stale、母版非 preview、引用已提交正史
2. analyzing_assets        analyze_asset_necessity 确定性分类参考素材（checkpoint 落库）
3. compiling_asset_prompts 素材图片 Prompt 精修批（AGENT 模式；批间检查暂停/取消，失败降级）
4. compiling_clip_prompts  逐镜 copy-ready Prompt 精修批（同上，黑名单 + 非空校验后 overlay）
5. rendering_guide         build_executable_video_production_guide 装配 + 渲染 markdown
ExecutableVideoProductionGuide（markdown_document 持久化；UI 抽屉与导出共用同一份）
```

进度诚实：阶段名 + 真实计数，无捏造百分比。AGENT 模式由 runtime `guide_compilation`
档位决定；未配置时走确定性基线（零 LLM），两者产出的手册都经过同一占位符黑名单校验。

何时生成：

- UI 制作页第三层 [生成完整制作手册] → 202 job → 摘要卡 + 抽屉；
- Shooting Agent SAFE_WRITE 动作 `build_complete_production_guide`（无 job，动作
  循环内联执行，同一 `generate_video_production_guide_async` 入口）；
- 前置门 fail-fast（HTTP 同步返回而非注定失败的后台任务）：源包必须 ready 且非
  stale（`VIDEO_GUIDE_SOURCE_NOT_READY`，409）、母版禁止 preview
  （`SHOOTING_SOURCE_PREVIEW_NOT_ALLOWED`，409）、必须引用已提交正史
  （`NARRATIVE_PRODUCTION_CANON_REQUIRED`，409）、clip 地点必须能解析到 location
  bible/list（`SHOOTING_LOCATION_CONTEXT_MISSING`，422，在任何 LLM 调用之前）。

幂等与改版：

- 同一 prompt package 的非 stale 手册，在钉死的 profile 版本与 clip plan 指纹
  （`clip_plan_fingerprint`：排序后 (clip_number, clip_id) 的 sha256）都未漂移时
  **直接复用**（status=ready 原样返回；半成品从 checkpoint 续跑并标记
  `resumed_from_checkpoint`）；
- 任一漂移 → 旧手册置 stale、新手册 `parent_guide_id` 指向旧册并记录
  `revision_reason`（"profile vN→vM update" / "clip plan changed"），旧行永不删除；
- 上游 stale 钩子见「Stale Rules」。

## Reference Asset Prompt Pipeline（参考素材 Prompt 流水线）

`analyze_asset_necessity` 从**结构**（clip.character_ids 频次 + 近景/中景 shot、
clip.location 去重、clip.prop_ids 频次、package 级视觉方向）自动分类每一条参考素材，
不含任何硬编码故事名（单测用改名夹具锁定）：

- **necessity**：`required`（角色：≥3 clip 或含近景/中景；场景：≥2 clip；道具：≥3 clip；
  接续帧恒为 required）/ `recommended`（2 clip 等）/ `optional`（单 clip、风格基准）；
- **asset_key**：`@CHAR_<NAME>_MASTER` / `@LOC_<NAME>_MASTER` / `@PROP_<NAME>` /
  `@STYLE_MASTER` / `@FRAME_NN`（typed token，手册与 Prompt 内唯一的素材引用方式）；
- **完整图片 Prompt**：`build_reference_asset_prompts` 为 character/location/prop/style
  生成**自包含**出图 Prompt（bible 全量视觉字段 + 色板 + 禁止变体 + 全局风格行 +
  出图操作说明）；没有 bible 条目也基于最佳可用上下文给完整 Prompt——**绝不留占位符**；
- **状态生命周期**：`NEEDED → PROMPT_READY → EXISTING → GENERATED → BOUND`。本层只到
  `PROMPT_READY`（图片 Prompt 就绪）与 `NEEDED`（reference_frame 捕获指令）；
  `EXISTING/GENERATED/BOUND` 留给后续真实文件登记层——**绝不伪造文件**，手册层
  永不出现 `BOUND`；
- **reference_frame 不是图片**：它是「Clip 生成完成后如何挑尾帧」的捕获指令，在手册
  第四节与素材清单中明确标注。

## Master Reference vs Start Frame（三类锚点）

连续性体系由三个各司其职的锚点组成（`continuity_workflow.anchors`）：

| 锚点 | 作用 | 说明 |
| --- | --- | --- |
| Character Master（`@CHAR_*_MASTER`） | 身份锚点 | 锁定角色长相、身形与服装，跨镜复用同一张设定图 |
| Environment Master（`@LOC_*_MASTER`） | 空间锚点 | 锁定场景空间布局、光线与材质，跨镜复用同一张环境图 |
| Previous End Frame（`@FRAME_NN`） | 物理连续性锚点 | 上一镜尾帧 = 下一镜首帧，保证动作、机位与光影物理连续 |

母图（Master）管「**是谁 / 在哪里**」，Start Frame 管「**从哪个物理状态继续**」：
每镜工单同时列出身份锚点、空间锚点与 Start Frame，三类锚点不混用、不互相替代。

## Copy Ready Prompt（逐镜可复制 Prompt）

`video_prompt_compiler.compile_copy_ready_prompt` 为每条 clip 生成**一个自包含代码块**
（GOAL / REFERENCE INPUTS / CHARACTER IDENTITY / ENVIRONMENT IDENTITY / ACTION AND
PERFORMANCE / CAMERA / TEMPORAL PROGRESSION / ENVIRONMENT MOTION / AUDIO（按能力）/
CONTINUITY / ENDING）：把 prompt、audio、连续性与收尾整合在同一块内，复制即可开工，
无需翻阅其它章节：

- **无占位符黑名单**：`PLACEHOLDER_PATTERNS`（"the established location" / "per shot" /
  "natural subject motion" / "TBD" / "TODO" / "待定"）在任何输出（素材 Prompt、
  copy-ready Prompt、markdown 正文）中出现即 fail-closed（`GUIDE_PLACEHOLDER_DETECTED`）；
  LLM 精修行同样过黑名单 + 非空校验才允许 overlay，否则回退确定性基线；
- **SHOOTING_LOCATION_CONTEXT_MISSING fail-closed**：clip.location 解析不到 location
  bible/list 条目时在任何 LLM 调用**之前**抛错（HTTP 422）——手册层绝不回退到
  "the established location" 式含糊锚点；
- 复用基线编译器的确定性决策（生成模式、audio/postproduction 拆分），但**不做**
  `max_prompt_chars` 截断（copy-ready prompt 自管分节预算）。

## Frame Chain（尾帧链）

`build_frame_chain` 输出确定性接续计划：

- **命名**：`FRAME_01`、`FRAME_02`……第 N 镜产出 `FRAME_N`，第 N+1 镜以它为首帧
  （Clip 1 无 Start Frame，自由开场）；
- **挑选区间**：每镜「最后 0.5–1.0 秒」（按时长适配，如 4s 镜为 3.0–3.5s），并附挑选
  标准（主体完整无变形、动作基本停止、构图干净、无字幕水印）；
- **收尾**：最后一镜不产出接续帧，干净收尾直接进后期（字幕 / fade）；
- 链路图 + 逐镜衔接表进入手册第七节；每 clip 的 frame_plan 同时驱动 copy-ready
  prompt 的连续性分节。

## Screen Composite Plan（画面合成建议）

`build_screen_composite_plan` 按 clip 的分镜文本做**确定性风险扫描**（手机/邮件/屏幕/
信件等画面内文本词 + 字幕/通知/时间戳等叠加词）：命中条目给出「生成时留白 / 后期贴图或
UI 叠加」的建议并记录命中词；未命中输出「无画面内文本风险」。模型从不负责渲染可读
文本——乱码风险在计划阶段消除。

## Subtitle（字幕时间轴）

`build_subtitle_plan` 直接对齐母版 subtitle_track → 逐条 时间/说话人/文本/位置建议。
字幕全部后期叠加，**绝不让视频模型把字幕烧进画面**；无字幕轨时输出明确的后期指引。

## SFX（对白与音效时间轴）

`build_dialogue_plan` 按 profile 能力决定每条台词 `native`（Prompt 驱动口型与语音）还是
后期配音；`build_sound_plan` 逐 clip 铺设环境声与关键音效（声音不硬切，逐镜列出过渡
处理）。能力未知（None）一律保守按后期配音处理。

## BGM（整集单条 Prompt）

`build_bgm_plan` 输出**一整条约等于全集时长**的配乐 Prompt：从 00:00 连续铺到片尾，
不在 Clip 边界分段生成；母版 `bgm_direction` 作为方向输入，时长取全集总时长。

## Editing（剪辑与转场）

`build_editing_plan` 输出剪辑三原则（画面优先硬切 / 声音不硬切 / BGM 一条到底）、
逐剪辑点表（含 dissolve 等过渡的原因）、环境声 0.3–0.8 秒交叉淡化、最终成片顺序与
最终检查清单（人物/空间/道具一致性、尾帧链完整性、屏幕文本、字幕、对白、BGM、
成片交付逐项打勾）。

## Shooting Agent（拍摄代理）

production 侧兄弟代理，镜像 Director 骨架并**瘦身**：无 canon-gate/audit 修复环、无
HIGH_IMPACT 确认。与 Director 一样**不依赖 Native Tool Calling**——每轮通过
`execute_structured`（phase=`shooting_agent`，schema=`SHOOTING_DECISION_SCHEMA`）
产出 Decision JSON（`decision: execute_action | reply | finish`），宿主执行动作。

权限模型（`narrative/shooting.py`，与 Director registry **集合级零交集**，单测锁死）：

- **READ_ONLY**（14 项，所有模式可用）：`get_production_package`、`get_shot_list`、
  `get_character_visual_bible`、`get_location_visual_bible`、`get_prop_visual_bible`、
  `get_dialogue_track`、`get_subtitle_track`、`get_sfx_plan`、`get_bgm_direction`、
  `get_continuity_notes`、`get_video_model_profiles`、`get_model_prompt_packages`、
  `get_production_assets`、`get_canon_episode`；
- **SAFE_WRITE**（9 项，AGENT 模式自动执行，写面仅限 clip/package/assets/guide）：
  `create_clip_plan`、`revise_clip_plan`、`create_model_prompt_package`、
  `revise_generation_clip_prompt`、`set_clip_reference_assets`、`set_generation_mode`、
  `set_target_video_model`、`set_continuity_strategy`、`build_complete_production_guide`
  （在源 prompt package 上构建可执行制作手册，动作内联执行、走同一 fail-closed 门）；
- **未注册即拒绝**：所有 story/canon 动作（`commit_episode`、`patch_episode_plan`、
  `revise_episode_draft`、`audit_episode`、`generate_production_package`、
  `patch_story_bible` 等 11 项列入 `SHOOTING_FORBIDDEN_ACTIONS`）以及 Director 的
  HUMAN_ONLY 动作均**不在** Shooting registry 中，输出引用它们一律
  `SHOOTING_ACTION_NOT_ALLOWED`（HTTP 403）。

参数规范化：`SHOOTING_ACTION_ARGUMENT_ALIASES` 把常见字段别名归一（如 `mode` →
`generation_mode`、`clip_number` → `clip_id`）；`SHOOTING_ACTION_DEFAULTED_FIELDS`
由宿主安全注入 session 绑定的 `project_id` 与最新包 id。会话缺失必需参数 →
`SHOOTING_ACTION_INVALID_ARGUMENTS`。

上下文构建（`build_shooting_context`）：只提供 Canon Episode 摘要、Production
Master、Profile 能力摘要、当前 Clip Plan、被引用的 Visual Bibles、Production
Assets、prev/next clip 与用户请求；**结构性排除 Story Bible final_truth**（service
根本不读它），并经上下文预算截断（priority 梯子，priority 0 永不丢弃）。

会话循环：`_run_loop` 每轮一个动作；连续 3 次失败或单轮 8 个动作耗尽 →
`NEEDS_HUMAN_GUIDANCE`；`finish/reply` 回 `ACTIVE`；pause/cancel 在轮间生效。
Runtime lease 是 **turn-scoped**（open → execute → finally close），服务不持有任何
跨轮 binding。同一会话 RUNNING 期间拒绝并发消息（`SHOOTING_SESSION_BUSY`，409）。

## Runtime

`NarrativeRole` 新增阶段 `shooting_agent`，在「⚙ 创作模型 → 高级：按阶段覆盖」中与
其它阶段并列配置；未单独配置时继承默认创作模型。CLI / harness / OpenAI-compatible
API 均可担任 Shooting Agent（由 Fake Plain-CLI 全链路测试证明：宿主经
`execute_structured` 收到结构化决策，全程无 native tool calling）。

## API

```text
GET  /api/narratives/video-model-profiles                     6 profiles 列表
GET  /api/narratives/video-model-profiles/{profile_id}        单 profile（未知 → 404）
GET  /api/narratives/{pid}/production/{pkg}/prompt-packages   方案列表（含 stale/update_available）
POST /api/narratives/{pid}/production/{pkg}/prompt-packages   202 + job_id（canon gate 409 / preview 409）
GET  /api/narratives/{pid}/prompt-packages/{package_id}       方案详情
PATCH /api/narratives/{pid}/prompt-packages/{package_id}/clips/{clip_id}
                                                              同步改 clip（422 能力违例）
POST /api/narratives/{pid}/production/{pkg}/production-guide  202 + job（手册 5 阶段）
GET  /api/narratives/{pid}/production/{pkg}/production-guide  该母版最新手册
GET  /api/narratives/{pid}/production-guides/{guide_id}       手册详情
GET  /api/narratives/{pid}/production-guides/{guide_id}/export
                                                              下载 markdown（text/markdown attachment）
GET/POST /api/narratives/{pid}/production-assets              素材登记/列表
POST/GET /api/narratives/{pid}/shooting/sessions              拍摄会话
GET      /api/narratives/{pid}/shooting/sessions/{sid}        会话快照（session/messages/actions）
POST     .../messages | /pause | /resume | /cancel
GET  /api/narrative-jobs/{job_id}                             既有任务面（四阶段进度）
```

错误映射：`NARRATIVE_PRODUCTION_CANON_REQUIRED`/`SHOOTING_SOURCE_PREVIEW_NOT_ALLOWED`/
`SHOOTING_SESSION_BUSY`/`VIDEO_GUIDE_SOURCE_NOT_READY` → 409；
`SHOOTING_ACTION_NOT_ALLOWED` → 403；
`VIDEO_PROFILE_NOT_FOUND`/`VIDEO_PROFILE_DURATION_UNSUPPORTED`/
`VIDEO_PROFILE_ASPECT_RATIO_UNSUPPORTED`/`VIDEO_PROFILE_MODE_UNSUPPORTED`/
`SHOOTING_LOCATION_CONTEXT_MISSING` → 422。

## UI Workflow（制作页三层）

制作页纵向分三层，**模型严格分离**：

1. **制作母版层**：Episode + CANON ✓ 标记、Screenplay/Shot List/三套 Visual Bible/
   Dialogue/Subtitle/SFX/BGM/Continuity；legacy `video_generation_prompts` 移入
   "高级 → 基础运动描述"折叠块，附免责声明（模型无关的运动/画面意图，不是可提交给
   特定视频模型的最终 Prompt）。
2. **视频生成方案层**：创建方案表单（目标视频模型下拉=profile registry；生成方式/比例/
   连续性策略按 profile 过滤；质量策略；声音策略；Prompt 语言）→ [生成 Clip Plan] →
   Clip Plan 审阅卡（编号/时长/来源 Shots/用途/模式/参考素材/连续性，支持拆分/合并/
   改时长/换模式/换素材）→ [生成完整 Prompt] → 完整 Clip 卡（目标模型/Profile 版本/
   Prompt/Audio Prompt/Continuity/Recommended Settings + 复制/重新编译/让拍摄 Agent 修改）。
3. **完整制作手册层**：[生成完整制作手册] → 真实阶段标签进度（无假百分比）→ 手册
   摘要卡（目标模型 / 需要 X 人物参考 · Y 场景参考 · Z 道具参考 / N Clips / 总时长）→
   [打开完整手册] 抽屉：连续 markdown（一、制作目标 → 十三、最终检查清单）+ 素材卡与
   Clip 卡逐条复制按钮（图片 Prompt / 视频 Prompt 独立复制）+ [复制完整手册] +
   [导出 Markdown]（attachment 下载）；stale / profile 更新横幅带 [重新生成手册]。

拍摄 Agent 面板镜像 Director 面板（讨论/建议/代理三模式）；PREVIEW 包醒目标记；
`profile_update_available` 派生横幅。响应式：桌面双区，≤900px 单列堆叠。

## Testing

```bash
uv run pytest tests/unit/test_narrative_video_profile_registry.py \
              tests/unit/test_narrative_clip_planner.py \
              tests/unit/test_narrative_video_prompt_compiler.py \
              tests/unit/test_narrative_shooting_agent.py \
              tests/unit/test_narrative_video_production_guide.py \
              tests/unit/test_video_production_guide_pipeline.py \
              tests/integration/test_narrative_shooting_api.py
```

覆盖：6 profile 加载与 verified/draft 状态、未知能力为 null；16-shot 合成夹具的
merge/split/对白边界/过渡边界/双 profile 差异；I2V 运动优先与负向改写；audio 路由；
能力违例三码；Shooting registry 与 Director 零交集；plain-CLI fake runtime 全链路
（无 native tool calling）；turn-scoped lease 无长持有；canon gate 409、preview 409、
四阶段诚实进度、单 clip PATCH 校验、stale 级联、profile 版本钉死与派生横幅。
可执行制作手册（`test_narrative_video_production_guide.py` 12 例）：素材结构化分类
（改名夹具不漂移）、完整图片 Prompt 永不 BOUND、尾帧链三镜衔接、手册全结构 + 13 节
markdown 幂等/逐字节确定性、asset token 闭合、location fail-closed、精修 overlay
黑名单/空行拒绝（`test_video_production_guide_pipeline.py` 8 例）：确定性 happy path、
幂等复用、未就绪源包拒绝、clip plan 漂移改版（parent/revision_reason）、双 stale 钩子、
AGENT 精修 overlay 计数、批量失败回退确定性基线。
