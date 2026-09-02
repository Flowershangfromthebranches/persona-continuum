# Adaptive Persona Research Quality Implementation

## Scope

本次升级只增强现有 Persona Creation Runtime 的研究质量系统。Persona 仍然严格走：

`PersonaService.create()` → `EvidenceSource` → `CompilationService.create_task()` →
八维 `ResearchArtifact` → `submit_research_artifact()` → `compile_persona()` →
`validate_persona()`。

LLM 不直接写 Persona package，也不允许没有 `source_id` 的事实进入公共人物的强证据。

## ResearchPolicy

`ResearchPolicy` 现在支持 `standard`、`deep`、`exhaustive` 和可扩展的 `custom` profile。

| Profile | Minimum independent | Preferred target | Soft budget | Hard budget | Categories | Per dimension |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Standard | 15 | 25 | 40 | 60 | 4 | 2 |
| Deep | 30 | 60 | 100 | 150 | 6 | 4 |
| Exhaustive | 50 | 100 | 150 | 200 | 7 | 6 |

Deep 及 Exhaustive 的 effective target 会按 subject richness 扩展。`soft_max_sources`
不是停止条件；高优先级缺口或高信息增益可以越过 soft budget。`hard_max_sources`、资料
空间耗尽或运行时不可用会产生可审计的 stop reason。旧 job 只使用保存的 policy snapshot，
旧 `max_sources` 会同时映射到旧 soft/hard budget。

## Stop Gate / Marginal Gain

`AdaptiveResearchStopGate` 在覆盖率同时满足以下条件时才允许 `completed`：

- independent source minimum；
- 每一个 required dimension 的独立证据；
- 动态 life-stage evidence；
- source category diversity；
- primary/secondary balance；
- contradiction-oriented search 已执行；
- 没有 high-priority gap；
- marginal information gain 达到低收益窗口要求或有效目标已经满足。

`MarginalInformationGainTracker` 每轮保存 claims、supported claims、contradictions、
behavior、expression、relationship、life-stage、category、primary evidence 和 dimension
coverage 的增量。它不以文本长度代替信息量。

## Gap-driven research / life stages

`ResearchGapAnalyzer` 会逐轮检查八维、生命阶段、来源多样性、primary/secondary、矛盾、
失败、关系、表达和决策证据，并生成带 priority 的 `ResearchGap`。低 yield / 高 duplicate
query 会写入 query history，不会无限重复。Research Plan 由 Agent 动态生成 life stages，
来源按事件时间或正文中的事件年份映射到多个阶段；来源 publication year 不用于伪造生命
阶段覆盖。

## Source independence / quality

`SourceIndependenceAnalyzer` 使用 canonical URL、content hash、规范化近似文本、标题/发布者、
origin identifier 和 citation chain 形成 `SourceCluster`。覆盖率按独立 cluster 计数，而非
按 URL 数量计数。`SourceQualityAssessment` 同时记录 authority、primary/secondary、specificity、
independence、historical relevance 和 metadata completeness。cluster 元数据保存到
`persona_source_clusters`，不复制正文。

## Private / fictional coverage

Private persona 不使用公共人物的来源数量门槛。覆盖率保存 message volume、conversation time
span、interaction contexts、relationship contexts、behavioral episodes、guided interview
answers、dimension coverage 和 high-priority gaps。大聊天导出按消息和事件信息量计数。
Fictional persona 另外保存作品文本量、场景、对话、行为事件、关系和用户设定覆盖率，不使用
公共 Web source threshold。资料不足仍可以编译为 `draft` / `completed_with_gaps`，继续材料或访谈会追加现有 Evidence
并保留 provenance。Fictional persona 继续依赖作品/设定/对话 Evidence 与现有八维 compiler。

## Persistence / API / UI

- `persona_creation_jobs` 新增 life-stage progress、information gain、research gaps、query
  history、checkpoints、stop reason 和 private coverage JSON 字段。
- 新增 `persona_research_checkpoints` 表，保存每轮 raw/independent/quality count、coverage、
  gain、gaps 和 stop reason。
- 新增 `persona_source_clusters` 表，保存独立性 cluster 元数据。
- 人物页和 Parallel World Persona Creation Engine 共享 Standard / Deep / Exhaustive / Custom
  选项；进度显示 raw/independent、target/soft budget、八维、life stages、primary/secondary、
  recent gain、stop reason 和 gaps。
- 既有 REST、events/WebSocket 和 World Persona completion API 保持不变，新增字段随 job snapshot
  和 `persona_coverage_updated` event 返回。
- 新增 `POST /api/persona-creation/jobs/{job_id}/continue`，从已完成/有缺口任务创建新的
  CompilationTask，复制已有 artifacts 后继续 targeted enrichment，保留旧 provenance。

## Tests written

`tests/unit/test_adaptive_research_quality.py` 已写入以下覆盖：

- `test_deep_policy_new_defaults`
- `test_exhaustive_policy_defaults`
- `test_old_job_policy_snapshot_compatible`
- `test_raw_reposts_do_not_count_as_independent_sources`
- `test_many_reposts_cannot_game_deep_source_floor`
- `test_source_cluster_counts_independent_origins`
- `test_deep_requires_all_dimensions`
- `test_deep_requires_life_stage_coverage`
- `test_publication_year_not_equal_life_stage`
- `test_primary_secondary_count_is_per_source_not_category`
- `test_contradiction_search_required`
- `test_gap_analyzer_finds_missing_dimension`
- `test_gap_analyzer_finds_missing_life_stage`
- `test_low_yield_queries_are_not_repeated`
- `test_deep_does_not_stop_at_minimum_source_count`
- `test_deep_can_continue_beyond_preferred_target`
- `test_deep_stops_after_low_marginal_gain`
- `test_deep_high_gain_continues`
- `test_soft_max_can_be_exceeded_for_high_priority_gap`
- `test_hard_max_returns_completed_with_gaps`
- `test_sparse_person_does_not_loop_forever`
- `test_rich_person_adaptive_target_expands`
- `test_private_persona_does_not_use_public_source_threshold`
- `test_large_chat_export_counts_information_not_file_count`
- `test_ui_no_longer_says_20_sources`
- `test_progress_reports_independent_source_count`
- `test_progress_reports_stop_reason`

## Validation status

用户于 2026-08-25 明确批准进入验证阶段。最终验证结果记录在
`ADAPTIVE_PERSONA_RESEARCH_VALIDATION.md`：当前完整套件为 `307 passed, 1 skipped, 1 warning`，
Ruff、Mypy、JavaScript 语法检查和 `git diff --check` 均通过。本轮没有执行真实联网研究、
真实 CLI/API Provider 调用或真实 60/100+ 来源 Persona 创建；自动化通过不替代这些外部验收。
