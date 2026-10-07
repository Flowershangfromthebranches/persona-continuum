# HIERARCHICAL_SUMMARIES.md — Memory Architecture v2, Phase 6（Hierarchical Summaries）

> **Phase 6.1（dependency hardening）已合入**：父级记录 source fingerprint、
> 子级变更自动 STALE、有界传播、`long_term_min_chapters` 正式生效。
> 见文末 [Phase 6.1](#12-phase-61-dependency-hardening) 一节。

## 0. 这一层解决什么

Phase 3 建立了 Episode（发生了什么），Phase 4 建立了 Fact（什么是真的），
Phase 5 建立了 Thread（什么还在进行）。它们都回答不了另一个问题：

> 我们**过去这几个月大体经历了什么**？

一次几百轮、跨几个月的共同经历，不可能靠一份滚动摘要承载。Phase 6 增加的是
**多尺度的历史组织层**：

```
Raw turns
   ↓  (Phase 3, 已有)
Episode ──────────────────────────────► Level 0（不新增）
   ↓  (deterministic grouping + LLM consolidation)
Chapter（阶段）───────────────────────► Level 1
   ↓  (deterministic grouping + LLM consolidation)
Long-term Summary（跨阶段历史）───────► Level 2（schema 允许更深）
```

例：

```
Episode  9/3  讨论考研焦虑
Episode  9/8  制定复习计划
Episode  9/12 数学复习遇到困难
Episode  9/18 决定调整计划
        ↓
Chapter  2026 年 9 月 · 考研准备阶段
summary  这一阶段用户开始认真准备考研，焦虑点从「要不要考」转向「数学复习效率」，
         期间两次调整学习方案。
```

**不是**什么：不是 Fact 数据库、不是 Episode 的替代、不是 Thread 的替代、不是关系状态权威源。
现有 `rolling_summary` 继续承担 **Current Arc**（当前正在聊的阶段），本轮**不改动、不删除**它。

明确不做（属于后续 Phase）：provenance-backed raw recall 注入、MemoryBundle /
production retrieval / Context Assembly、GraphRAG / Neo4j、大规模 UI、
**任何 summary 注入生产 prompt**。

---

## 1. 作用域

`(persona_id, counterpart_id, branch_id)` —— 与 Episode / Fact / Thread 完全一致。
`room_id` 只出现在 Episode 侧（作为可选的后台扫描过滤条件），不是隔离键。

## 2. 数据模型

### `memory_hierarchical_summaries`

| 字段组 | 字段 |
|---|---|
| 身份 | `id`, `persona_id`, `counterpart_id`, `branch_id`, `level`, `summary_type`, `sequence` |
| 内容 | `title`, `summary`, `summary_json`（结构化内容） |
| 时间 | `started_at`, `ended_at`, `created_at`, `updated_at` |
| 状态 | `status`（open/closed/retracted）、`summary_status`（pending/provisional/ready/failed） |
| 规模 | `source_count`, `source_token_estimate`, `importance` |
| 版本 | `consolidation_version`, `consolidation_attempts`, `last_error`, `consolidated_at` |
| 身份键 | `source_range_hash`（稳定 source identity） |
| 层级 | `parent_summary_id`（导航辅助，**不**表达多源关系） |
| 隔离 | `visibility`, `material_scope` |
| 其他 | `metadata_json`（`open_reason` / `close_reason` / grounding 报告 / 可用性标记） |

```sql
CREATE UNIQUE INDEX idx_memory_hierarchical_summaries_range
  ON memory_hierarchical_summaries(persona_id, counterpart_id, branch_id, level, source_range_hash)
  WHERE source_range_hash != '' AND status != 'retracted';
```

同一 scope、同一层级、同一 source range 只能有一行 —— 重跑 consolidation 不可能造出
「September Chapter 2 / 3 / 4」。

`level` 是普通整数：level 1 聚合 Episode，level ≥2 聚合下一层的 summary。
代码里没有「只能两层」的假设（`summary_type_for_level` / `source_type_for_level`）。

### `memory_summary_sources`（正式 source 关系表）

`(summary_id, source_type, source_id)` 主键 + `position` / `started_at` / `ended_at` / `importance`。

* Level 1：`source_type='episode'`
* Level ≥2：`source_type='summary'`

```sql
CREATE UNIQUE INDEX idx_memory_summary_sources_episode
  ON memory_summary_sources(source_type, source_id) WHERE source_type = 'episode';
```

一个 Episode 只能属于一个 Chapter（重跑分组因此幂等）；Chapter 进入 Long-term 之后
不会被重复使用，新的 Chapter 会形成**新的** segment，而不是重写旧的 segment。

链路可走通：**Long-term → Chapter → Episode → Turn → 原始文本**。

### 结构化内容（`summary_json`）

`title` / `summary` / `major_events` / `relationship_changes` / `important_decisions` /
`important_commitments` / `important_preferences_or_fact_changes` / `resolved_threads` /
`ongoing_threads` / `emotional_arc` / `unresolved_topics` / `key_entities` / `time_range` /
`importance` / **`inferences`**。

`inferences` 是刻意的隔离字段：模型**推断**出来的内容只能放这里，不得混进事实性字段。
事实语义层仍是 Fact Store。

---

## 3. 生命周期

```
CREATE(OPEN) ──append──► OPEN(provisional 文本)
                              │  边界触发
                              ▼
                          CLOSED ──► consolidate(以完整 source set 重生成) ──► READY(FINAL)
                              │
                              └──► 进入上一层的 OPEN segment …→ CLOSED → READY

源被显式删除且全部不可用 ──────────────────────────────────────► RETRACTED（墓碑）
```

* **grouping 是确定性的**（与 Episode 边界同源思路），边界满足任一即关闭当前 Chapter：
  | 边界 | 默认值 |
  |---|---|
  | `hierarchy_chapter_max_episodes` | 8 |
  | `hierarchy_chapter_max_source_tokens` | 20000 |
  | `hierarchy_chapter_max_timespan_days` | 14 |
  | `hierarchy_chapter_inactivity_gap_days` | 7 |
  Long-term segment 有自己的时间尺度：`hierarchy_long_term_max_chapters=12`、
  `hierarchy_long_term_max_source_tokens=60000`、`..._max_timespan_days=365`、
  `..._inactivity_gap_days=180`（14 天的 Chapter 尺度套在跨月 segment 上会两周就裂开）。
* **span / silence 用 source 的「开始时间」计算**：Episode 的 `ended_at` 是自己的边界触发时刻
  （即下一段对话的开始），拿它做区间运算会让每个 Chapter 都宽出后面那段空白，并掩盖真实沉默。
* **provisional ≠ final**：OPEN Chapter 可以有一份 provisional 文本；一旦关闭，
  必须从**完整 source set**重新生成 FINAL。source 变化时 `summary_status` 回到 `pending`，
  旧文本保留在 `metadata.previous_provisional`。
* **增量**：Chapter 可以不断 append，直到边界；Long-term segment 同样是 OPEN → append →
  CLOSED → consolidate。**不会**因为新增一个 Episode 就重做整个人生总结。
* **不递归压缩**：Level 1 以真实 Episode 为输入，Level 2 以真实 Chapter 为输入
  （并允许有限度地查看它们的 Episode 标题/摘要以支撑 grounding）。
  上一份 summary 从来不是下一层的唯一输入。
* **可选 topic-aware boundary**：仅当确定性边界都没触发**且** topic 重合度低于阈值时才询问模型
  （`CONTINUE` / `NEW_CHAPTER` / `UNCERTAIN`）；默认关闭，且失败/不确定都按 continue 处理 ——
  模型永远不能阻塞或误切分阶段。

## 4. Grounding validation

模型返回的内容在落库前必须通过校验（`validate_grounding`）：

| 检查 | 判定 |
|---|---|
| 引用 id（fact/thread/episode/summary） | 必须在本次 source 集合内，否则 `unknown_source_id`（硬失败） |
| 数字/日期 | ≥2 位数字必须能在 source 语料或日期词表中找到，否则 `ungrounded_number`（硬失败） |
| 关键实体 | 必须在语料中出现（子串或 ≥0.5 bigram 重合），否则 `ungrounded_entity`（硬失败） |
| 事实性陈述 | 与语料 **零** 词汇重合 → `ungrounded_statement`（硬失败）；部分重合 → `weak_grounding`（记录不拦截） |
| `inferences` | 明确豁免：解释可以，但必须打标签 |

失败 → `summary_status='failed'` + `last_error='grounding_failed'`，**不写入正文**，
失败详情（含具体哪一条）存进 `metadata.grounding.failures`，下一次 pass 可重试。
单数字（「3 套题」）不算日期，避免把中文数字/阿拉伯数字的排版差异误判为幻觉。

语料由 Episode 摘要 + Facts + Threads 事件 + **有界的原始轮次摘录**组成 ——
即 Episode 还没摘要时，grounding 也不会因为「语料空」而误杀。

## 5. 异步 / durable / 幂等

* Chapter grouping 与 consolidation 全部在后台 pass 中执行（房间路径排在
  Episode → Fact → Thread 之后），**不阻塞回复**。
* summary 行本身就是 durable 工作清单：`summary_status != ready` 即可重试；
  `recover_pending()` 只在启动时恢复清单，不自动跑模型。
* 幂等三件套：`source_range_hash` 唯一索引、`memory_summary_sources` 的 episode 唯一索引、
  `summary_status='ready' and status='closed'` 直接 noop。

## 6. 删除语义（Phase 3.1 延续）

| 情况 | 行为 |
|---|---|
| 部分 source Episode 被显式删除 | `metadata.source_availability='partial'` + `unavailable_source_count`，Chapter 保留 |
| 全部 source Episode 被删除 | Chapter → `status='retracted'`（墓碑 + `retracted_reason='sources_deleted'`），source 行移除 |
| Chapter 被 retract | 其 Long-term 父级重新计算可用性；父级全部 source 失效 → 同样 retracted |

不会出现「看起来正常、点进去什么都没有」的静默悬空。

## 7. 未接入 Prompt

Phase 6 只做 **Store + Group + Consolidate + Inspect**。Context Assembly Report 新增：

```
hierarchical_summaries_available     # 真实数量（有正文的 summary）
hierarchical_summaries_pending       # 仍欠正文的数量
hierarchical_summary_tokens = 0
summary_injection = "phase8_not_enabled"
```

Context Policy 的既有数字（local 6000/7000、`recent_dialogue_token_budget` 等）**未改动**；
summary 的长度策略（`hierarchy_chapter_summary_target_tokens≈1600`、
`hierarchy_long_term_summary_target_tokens≈3000`）属于 Memory Consolidation 策略，
与任何模型当前能看多少无关 —— 未来 Phase 8 再决定给不同模型加载多少。

## 8. 配置

```python
Config(
    hierarchy_summary_enabled=True,
    hierarchy_chapter_max_episodes=8,
    hierarchy_chapter_max_source_tokens=20000,
    hierarchy_chapter_max_timespan_days=14,
    hierarchy_chapter_inactivity_gap_days=7,
    hierarchy_topic_boundary_enabled=False,
    hierarchy_topic_shift_threshold=0.18,
    hierarchy_long_term_min_chapters=3,
    hierarchy_long_term_max_chapters=12,
    hierarchy_long_term_max_source_tokens=60000,
    hierarchy_long_term_max_timespan_days=365,
    hierarchy_long_term_inactivity_gap_days=180,
    hierarchy_chapter_summary_target_tokens=1600,
    hierarchy_long_term_summary_target_tokens=3000,
    hierarchy_summary_input_max_tokens=24000,
    hierarchy_consolidation_batch=1,
    hierarchy_backfill_batch=40,
    hierarchy_max_level=2,
)
```

## 9. 观测与调试

* 后台事件 `memory_hierarchy_report`：
  `summary_id / level / action / status / summary_status / sources / source_tokens /
  grounding_failures / version / pending / error`。不含对话正文。
* CLI：
  * `memory summaries [--persona] [--level] [--status] [--json]`
  * `memory summary <id> [--sources] [--json]` —— 层级 / 时间范围 / 状态 / 结构化内容 /
    source 列表（含 `resolvable`）/ 可用性 / 推理与 grounding 报告
  * `memory hierarchy-backfill [--limit] [--persona]` —— 有界、无模型分组
* 服务层：`assign_episode()` / `close_summary()` / `consolidate_summary()` /
  `consolidate_pending()` / `plan_long_term(close_segment=…)` / `inspect_summary()` /
  `stats()` / `recover_pending()` / `ungrouped_episodes()` / `backfill()`。
* 真实模型验收脚本：`scripts/phase6_hierarchical_summaries.py`。

## 10. Backfill

既有 109 个 Episode **不会**在启动时生成 Chapter summary：

* 打开房间时按房间做一次确定性的 bounded 分组（无模型）；
* `memory hierarchy-backfill` 给操作者一个有界的、无模型的分批入口；
* consolidation 只处理真正欠正文的 summary，每轮 `hierarchy_consolidation_batch` 个。

## 11. 与 Phase 3/4/5 的关系

```
turns → Episode（发生了什么）
          ├─► Semantic Fact（由此可知什么）
          ├─► Active Thread（什么还在进行）
          └─► Chapter → Long-term（我们过去大体经历了什么）
```

四层同时存在、互不替代；Summary 的 provenance 只指向 Episode / 下层 summary，
不复制原文，也不重写旧 Chapter（历史快照不随新事实改写；
新事实只影响**新生成**的 Long-term Summary 对时间变化的理解）。

---

## 12. Phase 6.1: dependency hardening

Phase 6 留下的洞：子 Chapter 从 `failed → ready`、或 source membership /
FINAL 内容变化之后，**已经存在的父级 Long-term Summary 不会意识到**，继续引用
旧版本。Phase 6.1 用「指纹 + 单层失效」把这个变成算术题。

### 12.1 source fingerprint

每个 Summary 记录两个值（新列，均为增量添加，旧库自动补齐）：

| 列 | 含义 |
|---|---|
| `source_fingerprint` | **当前** source 集合的指纹：ordered `(source_type, source_id, source_version)` + `consolidation_version` |
| `consolidated_fingerprint` | 生成这份文本时的指纹，由 `_store_content` 落库 |

`source_version` 是一个 source 对父级文本的**全部依赖**：

* Episode → 自身 `source_range_hash` + `summary_status` + `consolidation_version`
  + 内容哈希（title/summary/summary_json）
* 子 Summary → 自身 `source_fingerprint` + `summary_status` + 内容哈希 +
  `consolidation_version`

因此：**retry / reopen / 同内容重跑 → 指纹不变 → 不产生任何失效与重算**（A3）。
stale 的定义就是一条哈希比较：`source_fingerprint != consolidated_fingerprint`。

### 12.2 失效与有界传播（A1 / A2）

```
child changed (content / failed→ready / ready→failed / blocked / retracted)
        ↓
invalidate_parents(child)      ← 只标记直接父级，绝不递归
        ↓
父级 READY/PROVISIONAL → STALE（文本保留，但不再"当前"）
        ↓
后台有界 refresh（consolidate_pending，STALE 计入 pending 工作清单）
        ↓
refresh 成功 → _store_content → invalidate_parents(parent) → 更高一级 STALE
```

* 修改一个 Episode **不会**触发重算整个人生：传播是「每 refresh 一层」，且每一
  层都是 durable 工作清单里的一行。
* 已经 STALE 的父级不会被重复计数（`stale_count` 只记进入 STALE 的次数）。
* `refresh` 失败时行**保持 STALE**（`metadata.refresh_failed`），文本仍在、
  仍在工作清单上，绝不把旧文本洗成「无摘要」。

### 12.3 `long_term_min_chapters` 正式生效（A4）

`_assign_to_level` 在 level ≥ 2 的自动关闭前增加守卫：

```
自动关闭需要  source chapter count >= hierarchy_long_term_min_chapters
例外：explicit close（close_summary / plan_long_term(close_segment=True)）
      hard safety ceiling（segment 源 token ≥ max_source_tokens × 2）
      scope 终止（retraction）
```

两个 Chapter 加一次 200 天的沉默，不会再自动变成一个「长期人生阶段」。
被推迟的关闭记录在 `metadata.deferred_close_reason` / `deferred_close_sources`，
可通过 `memory summary <id>` 观察。

### 12.4 父级 FINAL 的前置条件

`consolidate_summary` 在 level ≥ 2 上新增 `child_sources_ready` 守卫：**final
history 只能写在 final history 之上**。任何 source Chapter 处于
PENDING / FAILED / PROVISIONAL / STALE 时，父级保持 OWED
（有文本则降级为 STALE，`metadata.blocked_reason = sources_not_ready`），
绝不基于一个没写完的子级生成 FINAL。

### 12.5 读侧变化

| API | 变化 |
|---|---|
| `pending_summaries()` | 计入 `stale` |
| `count_available()` | **不含** STALE（旧文本不再冒充当前历史） |
| `count_stale()` / `stale_summaries()` | 新增，失效队列的直接读法 |
| `inspect_summary()` | 新增 `source_fingerprint` / `consolidated_fingerprint` / `fingerprint_mismatch` / `stale_reason` / `blocked_reason` / `deferred_close_reason` / `child_sources_ready` |
| `stats()` | 新增 `stale` 计数 |
| `SummaryReadiness` | 新增 `STALE`；`needs_consolidation` 计入 |

Context Policy 未动：`local_constrained` 仍为 6000/7000/8/8/400/2000，
`historical_excerpt_*` 仍为 0/0（Phase 8 才接线）。

测试：`tests/integration/test_hierarchy_dependency.py`（10 个，覆盖 A5 全部
场景 + 传播有界性 + Context Policy 数值锁定）。
