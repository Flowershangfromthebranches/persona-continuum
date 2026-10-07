# Large Conversation Pipeline V2 — P0 修复与接线实施报告

日期：2026-09-08 · 范围：仅 P0（未实现 P1 Semantic Gate）· 未提交 PR，等待人工检查

## 1. 修改文件列表

| 文件 | 变更 |
| --- | --- |
| src/persona_continuum/config.py | 新增 3 个 V2 配置；移除无消费者的 P1 死配置 material_semantic_gate_mode |
| src/persona_continuum/application/material_chat.py | （上轮新建，本轮修正）Turn id=anchor 原始 unit id；merge_turns 跨批缝合 |
| src/persona_continuum/application/material_pipeline.py | build_analysis_windows 控制流修复（BLOCKER 1）+ max_units 160→1200/None + context-only 不占 cap + V2 metrics 字段 |
| src/persona_continuum/application/material_intelligence.py | 主链接线（BLOCKER 2–8、9–12），详见下文 |
| src/persona_continuum/application/chat_style_profiler.py | （上轮新建，本轮接入）流式、类型化 |
| src/persona_continuum/storage/migrations.py | 新增 persona_chat_style_profiles 派生表（每 Persona 一条，不产 per-statistic EvidenceUnit） |
| src/persona_continuum/application/container.py | 服务接线配置注入 |
| src/persona_continuum/application/persona_creation_service.py | 分类进度文案改为“目标语义记录”，context 不占额度 |
| src/persona_continuum/web/static/app.js | 任务中心显示原始/目标/上下文消息、Turn、窗口 x/y、高价值证据、Agent 调用、tokens、风格画像状态 |
| tests/integration/test_material_classification_integrity.py | 按 V4 语义重写（去除 reviewed_ids/missing_ids 旧断言） |
| tests/performance/test_local_material_creation.py | case A 改为验证“不再按 160 强拆” |
| tests/integration/test_large_conversation_pipeline_v2.py | 新增：16 条 V2 P0 验收测试 |
| scripts/large_chat_pipeline_v2_benchmark.py | 新增：mock-Agent synthetic benchmark A/B |

## 2. 各 BLOCKER 修复说明

- **B1 build_analysis_windows 缩进**：flush 判断曾在 for 循环外且吞掉了 append/flush 尾部（5 unit → 0 window）。已恢复为 for 内 flush、循环后尾 flush。测试：5 unit→≥1 窗；1000 unit 无丢失；尾批 flush。
- **B2 target/context 分流**：_speaker_role_map 在真实 ingest（_iter_source_units/_make_unit）解析 # 对方 = 目标 Persona 头，写入 EvidenceUnit.speaker_role + metadata.semantic_status（target_pending / context_only）。context_only 永不删除、不进 classification_total、不产生 pending 挂账；无头文件/未知 speaker 保守按普通证据处理，不丢。1000 交替消息实测 raw=1000 / target=500 / context=500，Agent 只见 target 为 target_units、context 出现在 units 行（semantic_role=context）。
- **B3 Turn 入链**：流式主链改为 persisted units → fold_conversation_turns（source+conversation+speaker 相同且 gap≤90s；carry 跨批 merge_turns 缝合不拆爆）→ AnalysisWindow → Agent。Turn.evidence_unit_ids 保留全部底层 id（5 条→1 Turn→5 ids 有测试）；输出锚定 anchor_id，supporting_evidence_ids 校验必须落在本窗输入内。
- **B4 配置真正生效**：_classify_with_agent 传 self.analysis_window_max_units（默认 1200，可为 None）；主约束仍是 token budget + transport safe bytes + source/conversation 边界；Prompt Size Guard 与 re-batch 原样保留（argv 传输下有专门测试）。
- **B5/B6 全面 v4**：production schema 无 reviewed_ids；output_contract 为 “Return only target turns … Do not return reviewed_ids”；全链搜索无 reviewed_ids 生产残留（仅 v3 兼容读路径与 prompt 历史常量）。
- **B7 _classification_values 重写**：只验证（窗内 id、supporting 范围、维度合法、结构完整），返回 values 列表而非 (values, missing)。缺席 = 成功审阅，本地标 reviewed_no_independent_evidence，零 missing-id retry（测试：500 target 输出 12 → 12 extracted + 488 reviewed + 0 retry；空输出也是合法全审阅）。
- **B8 semantic_status 持久化**：extracted/reviewed 行写入 classification_contract=conversation-evidence-v4 + status，_classification_done 按状态 resume；_mark_units_status 用 json_set 定点更新，不触碰原文列。中断续跑测试：60% 完成后，第二次只处理剩余 40%、零重叠。
- **B9/B10 ChatStyleProfiler 接入**：analyze_sources_async 两条路径（流式大语料 + 内存）均调 _run_chat_style_profiler（仅 target 角色行；exporter 语言习惯被排除）；结果单行持久化（含全部统计项、corpus_size、time_range、representative ids、contract），并作为 kind=expression_profile 候选注入 PersonaEvidenceIndex.retrieve 的 expression_dna 通道，标注 statistical=True。无 LLM 调用。
- **B11 metrics 实数据**：raw/target/context 消息数、conversation/target turn 数、窗口 total/completed、reviewed/extracted、avg/max turns per window、agent_calls、input/output tokens、style_profile_status 全部由运行中的真实计数写入 job.progress.chat_pipeline。
- **B12 checkpoint 版本**：v4 key 含 classification_contract + TURN_POLICY_VERSION；_material_intelligence_cache_key(contract="v3") 逐字节复现历史 v3 key，legacy key 同样保留——成功的 v3/legacy 行按自身命名空间读取复用、绝不重收费，也绝不冒充 v4 stamp（各有测试）。

## 3. V3 → V4 兼容说明

- 历史 v3 成功行：cache key 按冻结的 CLASSIFICATION_PROMPT_V3 重算命中 → 视为已完成，不重分析、不重收费；无 semantic_status 的旧行照常走 v4 审阅补齐（一次性），之后获得 v4 stamp。
- 空/失败的 v3 行：不视为完成，照常分类。
- v4 与 v3 命名空间互斥：v4 行不会被 v3 读路径冒充；换模型不会重放语料审阅（v4 判据是 semantic_status+contract，模型变化体现在 key 内仅供需要精确复用的一方使用）。

## 4. 测试

新增 tests/integration/test_large_conversation_pipeline_v2.py（16 项，对应任务 14 条 + 附加）；integrity 套件按 V4 重写；全部通过。

结果（本机，mock Agent）：
- 材料链核心 6 文件：103 passed
- 宽范围（material/persona_creation/profile_enrich/prompt_transport/resumable）：166 passed, 3 skipped, 0 failed（含曾偶发的 retry-reuse，本轮修复了窗口排序不稳定根因后复跑通过）
- 改动文件 ruff：All checks passed；mypy：no issues

## 5. Benchmark（synthetic，mock Agent，无真实额度）

A：1,000 raw（500 target/500 context，多连发气泡）
→ target turns=183，windows=1，agent_calls=2（1 窗 + 1 re-batch split），legacy-160 基线=7 窗；调用 -71%；pending=0；provenance 1000/1000；style=completed。

B：10,000 raw（5,000/5,000）
→ conversation turns=3,638，target turns=1,819，windows=7，agent_calls=24，legacy=63；-62%；输入 355,595 tok / 输出 864 tok（稀疏输出使 output 极小——旧协议 output≈逐条 echoed）；pending=0；provenance 100%。

385,034 条外推：target≈192k → 折叠后 turns 数量级 ~85k；按 B 的 ~260 target-turns/窗 与默认 32K planning 预算，预计数百次调用（相对 ~2,400 次为 ≥85% 削减；1M/128K profile 下更少）。Benchmark C（真实模型小样 10k–50k）与全量运行需真实 runtime 额度，留待人工授权后执行。

## 6. before/after 汇总

| | before (V1/未接线) | after (P0) |
| --- | --- | --- |
| 1000 条 50/50 聊天 | 7+ 窗，全消息语义分类 | 1 窗、500 条 context 零计费 |
| 10k 条 | 63 窗基线 | 7 窗 / 24 calls（含 guard re-batch） |
| 输出协议 | reviewed_ids 逐条回传 | 稀疏 units，缺席=已审阅 |
| 中断续跑 | 部分窗重放 | 已完成 Turn 不重收费 |
| 进度 | 385k 条原始记录 | 目标语义记录 + 窗口 x/y + 证据计数 |

## 7. 已知风险

1. context_units 借用的邻居 Turn 以 anchor id 呈现，若模型对 context 行输出证据，仍会落到该 anchor 的真实 ledger 行上（保守复用，无越界写）。
2. v4 判据用 semantic_status+contract（跨模型不复判）：换更强模型不会对已审阅语料自动重评；如需按模型复判，应显式 bump TURN_POLICY_VERSION 或引入 per-model stamp。
3. 窗口数依赖 profile：小 transport（argv 64KB）下 re-batch 会增加 calls（B 中 7→24 即此效应）；这是 Prompt Size Guard 的正确行为，不是回归。
4. Benchmark C / 385k 全量需真实模型与授权，未在本轮执行。
5. persona_creation_service.py 中既有 22 个 E501 / 6 个 mypy 错误属用户脏工作区预存问题，未越权修复。

## 8. P1

未实现（按本轮禁令）。P1 需单独提交，且已具备 deterministic gate 所需的全部持久化基础（semantic_status、Turn、style profile、metrics）。上轮遗留的无人读取的 material_semantic_gate_mode 配置已删除。

---

# P0.1 收尾 + Persona 创建表单简化（2026-09-08 追加）

## 9. A1 context_only 语义隔离

统一 helper `material_intelligence._is_persona_semantic_unit(unit)`（metadata.semantic_status != context_only）。
全部下游语义消费者改为单一判据：

- `EvidenceSimilarityAnalyzer.cluster`（in-memory 聚类输入过滤）；
- `PersonaContradictionAnalyzer.analyze`（by_id 过滤，context 行不可能成对）；
- `PersonaEvidenceFusionService.fuse`（by_id 过滤）；
- `PersonaEvidenceIndex.retrieve`：dimension 检索（或无 relationship 的泛检索）只放行语义 units；fused 行要求 supporting_evidence_ids 全部为语义 units；relationship 泛检索保留 ledger 全量（对话上下文/关系剧集仍可看到 exporter 消息）；
- `coverage()` 的 dimension 统计 SQL 追加 `json_extract(metadata_json,'$.semantic_status') != 'context_only'`；
- `_cluster_persisted` 主游标 SQL 同样排除 context 行；
- 两条 in-memory 路径的 relation_units 候选过滤；
- 编译审计 ledger 回填（persona_creation_service 的 unit_by_id/fused_by_id）。

回归测试：`tests/integration/test_pipeline_p01_notes.py::test_context_never_retrieved_as_persona`（in-memory 与 persisted 双路径）——输入 对方:好的。/ 我:我决定选择拒绝这个项目，因为这是我的价值观。 后：两条 raw 行均保留、exporter 行仍在 episodes（relationship 侧可见），但 `decisions_and_behavior` 与 `values_desires_contradictions` 检索及聚簇均不出现 exporter 文本。

## 10. A2/A3/A4 确定性阶段去 O(n^2)

- A2 exact_duplicate：`_contradictions_from_clusters` 对 `cluster_type == "exact_duplicate"` 直接跳过；`_cluster_persisted` 中 exact 命中不再重复尝试近邻放置，且 exact 标记不被 near 命中降级覆盖。5000 条相同“好的”：`PersonaContradictionAnalyzer.analyze` 零次 `_jaccard` 调用（test_exact_duplicate_5000_has_no_pairwise_work，patch 断言）。
- A3 near_duplicate 变体上限：`PersonaContradictionAnalyzer._variants` 按 normalized_text 去重、按 id 稳定选取、按排序截断到 `max_variants_per_cluster`（config `material_contradiction_max_variants_per_cluster` 默认 64，container 注入）。簇规模从 O(n) 变体对变为 ≤O(64^2) 常数。test_variants_bounded_and_deterministic 验证乱序输入输出一致。
- A4 singleton 不生成 fused：`PersonaEvidenceFusionService.fuse` 成员 <2 跳过；`_fuse_persisted` 同样跳过。原单证据测试改为两条近重复证据仍出 fused（33 passed）。

## 11. A5 持久化路径基准

`scripts/large_chat_pipeline_v2_benchmark.py`：`in_memory_unit_limit=1` 且 `max_source_bytes=1` 双强制，并断言 ingest/classification 阶段 hook 实际被调用（persisted_path_verified=true）。分阶段计时通过方法包装采集。

| 规模 | 总耗时 | ingest | folding | classify | style | cluster | contradict | fuse | index | agent calls | legacy 基线 | 削减 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 10k | 3.41s | 0.54 | 0.03 | 1.44 | 0.43 | 0.57 | 0.0002 | 0.0001 | 0.04 | 26 | 63 | −58.7% |
| 30k | 12.5s | 1.76 | 0.08 | 5.27 | 1.46 | 2.49 | 0.001 | 0.0006 | 0.17 | 82 | 188 | −56.4% |
| 50k | 23.6s | 3.08 | 0.14 | 10.23 | 2.78 | 5.04 | 0.0018 | 0.001 | 0.26 | 137 | 313 | −56.2% |

5k→25k target 消息（5×）下：ingest 5.7×、cluster 8.8×、classify 7.1×、contradiction/fusion 近乎不增长；无平方级曲线（cluster 超线性主要来自 token 索引常数与 SQLite 批写，非 O(n²) pairwise）。provenance 100%，pending 0，context 全部标为 context_only。

## 12. B Persona 创建表单统一为 persona_notes

- 前端：`pc-identity-context` / `pc-user-facts` / `pc-research-instructions` 三个字段删除，合并为 `#pc-persona-notes` 备注说明（可选），placeholder/helper 按任务文案；提交体只发 `persona_notes`。旧任务回填显示：persona_notes ?? 旧三字段拼接。基本身份保留 名称/别名/人物来源/作品；研究策略区（研究方式/日期/运行来源/模型/策略）不变。
- 后端：`PersonaCreationJob.persona_notes`（持久化到 job_config，`_save` 注入；before-validator 在 persona_notes 缺失时从旧三字段合成 → 历史任务可读、旧 API 字段保留 deprecated 兼容）。`web/api.py` 接受 `persona_notes`。
- 事实/指令分流：`application/persona_notes.py::parse_persona_notes` 保守四分（instruction/identity_hint/user_supplied_fact/research_constraint）；默认全部归 instruction，仅显式“我确认”类句子进 user_supplied_fact。研究规划 prompt 注入“用户备注属于研究指引，不能直接作为人物事实”；identity_hint 供 IdentityResolver 消歧；只有 user_supplied_fact 作为 user_provided Evidence source 摄取（notes_facts_source_id 防重）。
- 空 notes：不阻塞，全走名称/来源/资料自动判断（测试 notes="" 通过）。
- B4 本地私人优先：privacy_scope=private（或 private_* 类型）+ 现实人物 + 上传资料且 notes 未明确要求联网 → creation_mode 强制 private_materials、research_mode=local、web_scope=none、job_config.source_priority=local_evidence_first（不自动同名公网研究）；notes 明确“联网补充”则 hybrid。
- 测试（test_pipeline_p01_notes.py 等，共 10 项全过）覆盖任务 B7 的 6 项。
- UI 截图：docs/reports/implementation/assets/persona-create-form-notes.png（in-app browser 实测渲染，弹窗含“备注说明（可选）”，三个旧字段不再出现）。

## 13. 本轮验证

- 定向：material 核心 4 文件 74 passed；新增 P0.1/notes 10 passed；stall-fix/identity/ui-streaming 等全部通过。
- 宽回归 `-k 'material or persona_creation or profile_enrich or prompt_transport or resumable'`：175 passed, 4 skipped（此前 166，新增 10，1 处按 A4 语义调整）。约 7.8 分钟。
- ruff（修改的 8 个文件）全部通过；mypy（4 核心源文件）no issues。persona_creation_service.py 残余 21 个 E501 为脏工作区预存，未越权处理。
- 未运行 385,034 全量真实聊天；未提交 PR。P1 Semantic Gate 未实现。
