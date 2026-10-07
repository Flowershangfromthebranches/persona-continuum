# Phase 6 实施报告：Hierarchical Summaries

本轮只做 **Phase 6（Hierarchical Summaries）**。未开始 Phase 7 / Phase 8 / GraphRAG / Neo4j /
大规模 UI；未把任何 summary 注入生产 prompt；Context Policy 所有现有数值未改动；
Episode boundary（Phase 3）未修改。

---

## Hierarchy schema

**tables**

| 表 | 作用 |
|---|---|
| `memory_hierarchical_summaries` | Chapter（level 1）/ Long-term Segment（level ≥2）本身 |
| `memory_summary_sources` | 正式 source 关系表（`source_type = episode \| summary`） |

**levels**

* `level` 是普通整数，`summary_type_for_level(level)`：`1 → chapter`，`≥2 → long_term`；
  `source_type_for_level(level)`：`1 → episode`，`≥2 → summary`。代码里没有「只有两层」的假设。
* 本轮实际构建到 level 2（`hierarchy_max_level=2`），schema 与 API 已支持更深。

**sources**

* Level 1 指向真实 Episode；Level 2 指向真实 Chapter（并允许有界地查看其 Episode 摘要作为 grounding 支撑）。
* `(summary_id, source_type, source_id)` 主键 + `position` / `started_at` / `ended_at` / `importance`。
* `source_type='episode'` 上有唯一索引：一个 Episode 只属于一个 Chapter（分组幂等的前提）。
* 一个 Chapter 进入某个 segment 后不会被别的 segment 复用；后续新 Chapter 形成**新的** segment。

**scope**

* `(persona_id, counterpart_id, branch_id)`，与 Episode / Fact / Thread 一致；`room_id` 只用于后台扫描过滤。

**identity / 幂等**

* `source_range_hash = hash(scope, level, ordered source ids)`；
  `UNIQUE(scope, level, source_range_hash) WHERE status != 'retracted'` —— 重跑 consolidation
  不可能产生「September Chapter 2 / 3 / 4」。

---

## Lifecycle

| 阶段 | 行为 |
|---|---|
| open | 无 OPEN summary 时创建；`started_at` 取首个 source 起点 |
| append | 追加 source（`INSERT OR IGNORE` + 重算聚合值 + 重算 hash）；source 变化后 `summary_status` 回到 `pending`（旧 provisional 文本留在 `metadata.previous_provisional`） |
| boundary | 确定性边界，满足任一即关闭：`max_episodes`(8) / `max_source_tokens`(20000) / `max_timespan_days`(14) / `inactivity_gap_days`(7)；level≥2 使用自己的尺度（12 / 60000 / 365 天 / 180 天）。**区间与沉默都按 source 的开始时间计算**（Episode 的 `ended_at` 是自己的边界触发时刻，用它会把下一个 sitting 的空档算进来） |
| close | `status=closed` + `summary_status=pending`（欠一份 FINAL）；`ended_at` 取最后一个 source 的结束 |
| provisional | OPEN summary 的文本显式为 `provisional`；不是最终历史 |
| final | 关闭后从**完整 source set** 重新生成 → `ready`；`ready + closed` 时再次调用直接 noop（`force=True` 才原地重生成，用于父级追上子级） |
| long-term | Chapter 关闭时自动进入 level 2 的 OPEN segment（增量吸收）；segment 按自己的边界关闭，或由 `plan_long_term(close_segment=True)` 显式收束后生成 FINAL |
| topic boundary | 可选、默认关闭；仅当确定性边界未触发且 topic 重合度低于阈值时才询问模型，`UNCERTAIN`/异常一律 continue |

**不递归压缩**：Level 1 的输入是真实 Episode（不是上一份 summary），
Level 2 的输入是真实 Chapter（不是上一份 segment），并把它们的 Fact / Thread 事件带上，
所以「早期喜欢茉莉奶绿、后期改喝美式」这类时间变化能被表达出来。

---

## Grounding

| 检查 | 行为 |
|---|---|
| facts | 语料包含 Fact 的 `display_text` / `status` / `plan_status` / `valid_from` / `valid_until`（模型看得到什么，grounding 就必须覆盖什么） |
| threads | 语料包含 Thread 的 title / summary / status / milestones / 事件（含 `opened_at` / `resolved_at`） |
| entities | `key_entities` 必须出现在语料中（子串或 ≥0.5 bigram 重合），否则硬失败 |
| dates/numbers | ≥2 位数字（日期、金额）必须能在语料/日期词表找到；**引用 id 内部的十六进制不会被当成数字**；单数字不算日期（避免中文/阿拉伯数字排版差异误判） |
| citations | 引用 id 必须在本次 source 集合**或 sources 文本中出现**的 id 内 —— 后者覆盖「Chapter 正文里引用了某个 thread id，Long-term 再引用它」的合法情形 |
| statements | 事实性陈述与语料零重合 → 硬失败；部分重合 → `weak_grounding` 警告（记录不拦截） |
| inferences | 显式豁免：解释必须写在 `inferences`，不得混进事实性字段 |
| hallucination handling | 硬失败 → `summary_status=failed` + `last_error=grounding_failed`，**不写入正文**，失败明细（哪一条、哪一类）存 `metadata.grounding.failures`，可重试 |

真实模型跑下来，grounding 确实拦住了两次：一次是模型把 Fact 的到期日写进 summary（**校验器漏了语料**，已修），
一次是模型引用 Chapter 正文里的 thread id（**校验器过严**，已修）。两次都不是模型幻觉 —— 这两个修正本身就是验收价值。

---

## Provenance

* `long-term → chapter`：`memory_summary_sources(source_type='summary')`；`inspect_summary()` 列出每个 source 的
  `level` / `title` / `episode_count` / `resolvable`。
* `chapter → episode`：`source_type='episode'`，附 `turn_count` 与 `resolvable`。
* `episode → turn`：`EpisodeService.episode_turns()` + `resolve_turn_text()`。
* `raw resolution`：Phase 3 的既有解析路径（`session_turns` / `room_transcripts`），
  验收测试断言 Long-term → Chapter → Episode → Turn → 原文整条链可读（真实模型 benchmark 中
  `provenance_resolvable=True`）。
* 删除：部分 source 失效 → `partial` + `unavailable_source_count`；全部失效 → `retracted`
  （`retracted_reason='sources_deleted'`，source 行移除）；父级 segment 同步重算，同样可被 retract。

---

## Tests

新增：`tests/integration/test_hierarchical_summaries.py`（19）+ `tests/unit/test_hierarchy_rules.py`（20）。

| 验收项 | 用例 | 结果 |
|---|---|---|
| grouping | `test_a_related_episodes_form_one_chapter` | 10 个 Episode → 2 个 Chapter（8/2），`max_sources` 关闭 |
| boundary | `test_b_*`、`test_b2_*` | 长沉默 → Chapter A closed / B open；可选 topic classifier 只在 NEW_CHAPTER 时切分，异常/UNKNOWN → continue |
| fact evolution | `test_c_*` | payload 同时携带旧值（superseded + `valid_until`）与新值；provisional → close → final 两段式 |
| thread lifecycle | `test_d_*` | payload 的事件序列 create → milestone → resolve |
| relationship arc | `test_e_*` | 事件序列 create → resolve → reopen；摘要有三段轨迹而非终态 |
| hallucination | `test_f_*`、`test_f2_*` | 编造实体/日期/事件 → `grounding_failed`，正文不落库；带标签的 `inferences` 允许 |
| restart | `test_h_*` | 重启后 `recover_pending()` 找回 pending，重跑不重复 |
| idempotency | `test_g_*` | 重放分组全 noop；ready+closed 再跑 noop；summary 数量不变 |
| failure | `test_i_*` | provider 异常 / 非 JSON → failed 可重试，Episodes/Facts/Threads 不受影响 |
| scope | `test_j_*` | counterpart / branch 各自独立 |
| deletion | `test_l_*` | 全部 source 删除 → `retracted` + `deleted` 标记；source 行清空 |
| provenance | `test_k_*` | Long-term → Chapter → Episode → Turn → 原文可解析 |
| grounding（纯逻辑） | `test_hierarchy_rules.py` 20 项 | 实体/数字/引用/陈述/警告/推理豁免/id 掩码/Fact 有效期 |
| prompt 边界 | `test_m_*` | `hierarchical_summaries_available` 真实、`hierarchical_summary_tokens=0`、`summary_injection=phase8_not_enabled` |
| backfill | `test_n_*` | 有界、无模型；`remaining` 为真实剩余数 |
| long-term 增量 | `test_o_*` | 段吸收第 2 个 Chapter → 显式收束 → FINAL |
| CLI | `test_p_*` | `memory summaries` / `memory summary --sources --json` / `memory hierarchy-backfill` |

---

## Real-model benchmark

* model：`command_code` adapter（effective `deepseek/deepseek-v4-pro`），reasoning `none`
  （codex 用量上限、grok 余额耗尽，按「换项目现有可用 provider」处理；grok 的部分运行记录在
  `phase6-hierarchical-summaries-grok-partial.json`）
* turns：15（3 个阶段 × 5 个 sitting，间隔 250 分钟 > Episode idle gap）
* episodes：15
* chapters：3（每个阶段 1 个，`max_sources` 关闭；最后一个显式关闭）
* long-term summaries：1（3 个 Chapter，显式收束 + 强制刷新以纳入第二次才成功的 Chapter 3）

| 指标 | 值 |
|---|---|
| major events expected | 10 |
| captured | **10** |
| missed | 0 |
| hallucinated | 0（`东京/柏林/结婚/辞职…` 等未出现；语料中也不存在） |
| fact-change accuracy | captured（「2026-09-01 最喜欢茉莉奶绿 → 2026-09-29 起只喝美式」） |
| thread-lifecycle accuracy | 3/3（计划 → 买票 → 收拾行李 → 出发 → 归来 → resolved） |
| relationship-arc accuracy | captured（与小陈：争吵 → 和好 → 再吵 → 谈开后和解；并区分「林涛的陪伴轨迹」） |
| temporal ordering | 正确（9 月初备考 → 9/29 出行+偏好变化 → 10/26 归来 → 归后两天冲突） |
| provenance | `all_sources_resolvable=True`（Long-term → 3 Chapter → 15 Episode → turns） |

**人工判读（逐条）**

* 三个阶段都被捕捉，且**没有把不同阶段混成一句**：Chapter 1 讲备考推进，Chapter 2 讲重庆，
  Chapter 3 讲人际冲突反复，Long-term 再把三者按时间串起来。
* **事实变化表达正确**：Long-term 写「阶段早期（2026-09-01）最喜欢茉莉奶绿、复习靠它提神；
  2026-09-29 起改为只喝美式（喝腻了）」，没有只留旧值或只留新值。
* **Thread 生命周期完整**：Chapter 2 写「计划 → 买票 → 收拾行李 → 出发 → 归来并关闭」，
  而不是停留在「用户计划去重庆」。
* **关系轨迹不只保留终态**：Chapter 3 与 Long-term 都写了「争吵 → 和好 → 再次争吵 → 谈开后和解」。
* **不确定性被正确标注**：模型把「备考可能被出行打断」「旅行期间可能没有联系」等放进 `inferences`，
  而不是写成事实 —— 与 Fact Store 的分工没有被破坏。
* **无捏造**：`hallucinated_terms=0`，且 groundg 校验（实体/日期/引用/陈述）全部通过。
* 观察到的**保守之处**（不是错误）：Chapter 3 全程用「未具名的她」「疑似小陈」表述，
  因为来源里第二次争吵的对象确实没有点名 —— 这正是希望的行为。

---

## Regression

| 面 | 结果 |
|---|---|
| Context Policy | 未改动任何数字；`test_room_context_policy_ab.py` 通过 |
| Episode | Episode 边界代码未改；`test_episode_*` 通过 |
| Facts | `test_semantic_facts.py` 通过（含 Phase 4.1 的 cross-slot 回归） |
| Threads | `test_active_threads.py` 通过（房间 pass 阶段序列更新为 summary → facts → threads → **hierarchy**，两处既有断言同步更新） |
| Room | `test_episode_room_consolidation.py` 通过；`test_room_protocol_public_messages.py` 通过 |
| Memory 定向回归 | **97 passed**（hierarchy / hierarchy rules / threads / facts / room consolidation / context policy / room protocol） |
| 全量 `uv run pytest` | **1796 passed, 9 failed, 8 skipped**（39m52s；本轮新增 39 个用例，1757 → 1796） |
| 全量 9 个失败 | 与 Phase 5 报告中的同一批（scene runtime / 协议房间 / Persona Creation / 旧 transcript 格式 / 时间边界），**均不经 Episode/Fact/Thread/Summary/Context Policy 路径**，无新增失败 |
| ruff（本轮改动文件） | 全部通过 |
| mypy | 本轮新增/修改模块 0 错误（全仓仍为会话前既有的 15 errors / 3 files） |

---

## 复现方式

```bash
# 全量真实模型 benchmark（临时 data dir；--chapter-max-episodes 让一个阶段≈一个 Chapter）
uv run python scripts/phase6_hierarchical_summaries.py \
    --adapter command_code --reasoning none \
    --data-dir /tmp/pc-phase6 --out /tmp/phase6-report.json

# 只重跑 Chapter / Long-term consolidation（复用同一 data dir，不重复 45 次调用）
uv run python scripts/phase6_hierarchical_summaries.py \
    --adapter command_code --reasoning none --consolidate-only \
    --data-dir /tmp/pc-phase6 --out /tmp/phase6-report.json
```

原始记录：`docs/reports/implementation/phase6-hierarchical-summaries-real-model.json`

---

## Remaining risks（仅 Phase 6 未解决项）

1. **segment 可能在某个 Chapter 仍欠正文时被收束**：真实 benchmark 里 Chapter 3 第一次
   grounding 失败，而 segment 已经关闭并生成过一版文本（缺少该阶段）。现在可以用
   `consolidate_summary(force=True)` 刷新父级，但**没有自动父子联动**：
   子级从 failed→ready 不会自动让父级重新生成。
2. **`hierarchy_long_term_min_chapters` 目前不参与关闭判断**：段关闭由自身的 boundary 或显式
   `close_segment` 决定，`min_chapters` 只在配置里存在；没有「至少 N 个 Chapter 才值得关闭」的自动规则。
3. **grounding 目前是词面/结构校验，不是蕴含校验**：它可以拦住凭空出现的实体、日期、id 和整句新事件，
   但拦不住「用来源里的词拼出一句来源没有的判断」（这类只能靠 `inferences` 纪律 + 人工抽查）。
4. **Chapter 标题与边界的语义质量依赖模型**：确定性分组不完美（把一个阶段切成两个、
   或把两个阶段并成一个），topic-aware classifier 默认关闭，本轮没有 benchmark 它的收益。
5. **多级嵌套只验证到 level 2**：level 3+ 的 schema / API 已就绪，但没有真实数据验证
   （需要 12+ Chapters 才会自然发生）。
6. **`hierarchy_consolidation_batch=1`**：房间后台 pass 每轮只处理 1 个 summary，
   大量历史 backfill 时收敛较慢（这是刻意的 bounded 设计，但没有自适应速率）。
7. **provisional 文本不进入任何 prompt**：本轮明确不接入，所以「provisional 的读者」目前只有
   inspect / CLI；Phase 8 决定注入策略时需要重新评估 provisional 是否可被引用。
