# MEMORY_EPISODES.md — Memory Architecture v2, Phase 3（Episode + Consolidation）

## 0. 这一层解决什么

Phase 1/2 把「这一轮给模型看多少」从机器限制里解耦出来。Phase 3 解决另一半：
**共同历史本身被正确组织了吗。**

在此之前，一次对话在存储上只有两种形态：

* `room_transcripts` / `session_turns` —— 逐条原始记录（L0 事实源）
* `memories`（`digital_experience`）—— 逐轮的运行时经历
* 一份 room 级 `rolling_summary` —— 一个被压到几百 token 的滚动摘要

缺的是中间层：**「发生过的一段事情」**。14 条 `digital_experience` 可能属于同一场
讨论，但在数据上它们彼此孤立，只能靠关键词重新命中。

Phase 3 增加的就是这一层，且只增加这一层：

```
raw committed turns
      ↓  (deterministic, 同步, 与 commit 同事务)
Episode（有边界、有来源、可溯源）
      ↓  (LLM, 异步, 幂等, 可重试)
结构化 Episode Summary
```

明确不做（属于后续 Phase）：Semantic Fact 时间有效性、Active Threads、Relationship
Timeline、Hierarchical Summary、GraphRAG / Neo4j、MemoryBundle、Hybrid Retrieval、
raw excerpt 注入 prompt。

---

## 1. 作用域：先审计 room_id / session_id

结论（依据现有 schema 与调用路径）：

| | 含义 | 是否作为 Episode 身份键 |
|---|---|---|
| `session_id` | 一段对话容器；`session_turns.session_id` 引用它，跨进程重启稳定 | ✅ 是 |
| `room_id` | 运行时/UI 容器；只有房间对话才有，`room_transcripts` 以它为键 | ❌ 否，但必须记录 |

两者**不等价**，所以不是「重复存等价字段」：

* 同一个 persona 与同一个 counterpart 可以在不同 room 里继续关系（room 变了、关系没变），
  因此 `room_id` 不能作为隔离键；
* 但房间轮次的原始文本在 `room_transcripts` 里以 `room_id` 定位，
  溯源需要它。

最终身份键：**`(persona_id, counterpart_id, branch_id, session_id)`**，外加一个查询用的
`room_id` 字段。隔离强度：counterpart 之间不混、branch 之间不混、不同 session 之间不混
（新房间 = 新 session = 新 Episode，绝不追加到旧 session 的 Episode 上）。

### 1.1 Episode owner ≠ source speaker（Phase 3.1）

Episode 属于 `persona + counterpart + branch + session`，但它的 **source 可以不是这个
persona 说的**。多人格房间里用户只说一次：

```
shared user turn（room_transcripts 里唯一一行 raw event）
   ├── Episode 苏禾   contains source
   └── Episode 顾言   contains source
```

实现方式：`memory_episode_turns.source_kind = 'shared_user'` + `speaker = 'user'`，
与 `'session_turn'`（该 persona 自己的轮次）区分。共享用户轮次**不会被复制成多份
fake persona turn**，Raw History 仍是单一事实源。

两条语义边界：

* `turn_count` 仍只数**该 Episode 自己的** committed turns；共享来源是额外 source，
  不能触发 `max_turns` 边界。
* 一个 persona 回复**另一个 persona** 时（房间里没有新的用户消息），该轮属于
  `persona↔persona` 这个 counterpart scope，是另一个 Episode —— 这是作用域隔离在起作用，
  不是 turn 丢失。


---

## 2. 数据模型

### `memory_episodes`

| 字段组 | 字段 |
|---|---|
| 身份 | `id`, `persona_id`, `counterpart_id`, `branch_id`, `session_id`, `room_id`, `sequence` |
| 内容 | `title`, `summary`, `summary_json`（schema 校验后的结构化摘要） |
| 时间 | `started_at`, `ended_at`, `created_at`, `updated_at` |
| 边界 | `status`, `boundary_reason`（`metadata.start_reason` / `close_reason` 记录两半） |
| 规模 | `turn_count`, `source_token_estimate`, `source_first_turn_id`, `source_last_turn_id`, `source_range_hash` |
| 摘要状态 | `summary_status`, `consolidation_version`, `consolidation_attempts`, `last_error`, `consolidated_at` |
| 隔离 | `visibility`, `provenance`, `material_scope` |
| 其他 | `importance`, `confidence`, `metadata_json` |

`status`：`open` / `closed` / `pending_consolidation` / `failed`
（`failed` 表示上次摘要失败，**仍然可重试**，不是终态。）

### `memory_episode_turns`（provenance 关系表）

`(episode_id, turn_id)` 主键，另有 `source_kind`（`session_turn` / `room_transcript`）、
`position`、`session_id`、`room_id`、`speaker`、`occurred_at`、`token_estimate`。

**没有**把 `source_turn_ids` 塞进一个不断增长的 JSON array：关系表可以高效双向查询
（episode → turns、turn → episode），并且有 `ON DELETE CASCADE`。

索引：`memory_episodes` 上 `(scope, sequence)` 唯一、`(scope, status)`、
`(session_id, status)`、`(persona_id, started_at, ended_at)`、`(status, summary_status, updated_at)`；
`memory_episode_turns` 上 `turn_id`、`(session_id, turn_id)`、`(episode_id, position)`。

### 与 `digital_experience` / `lineage` 的关系

* `digital_experience` 继续逐轮写入，**不删除、不替代**；Episode 是更高粒度的组织层
  （测试断言 3 条 experience → 1 个 Episode）。
* `lineage` 复用（不引入第二套图存储）：每个 source turn 一条
  `episode --episode_contains_turn--> session_turn`，摘要落盘后一条
  `memory_summary --episode_summary_of--> episode`。

---

## 3. 生命周期

```
turn commit ──► assign_turn（同步、确定性、与 turn 同事务）
                 │
                 ├─ 无 OPEN Episode            → CREATE   (first_turn)
                 ├─ 相邻且未越界               → APPEND
                 └─ 触发边界                   → CLOSE 旧 + CREATE 新
                                                 idle_gap / max_turns / max_tokens
后续（异步、可失败）：
   OPEN/CLOSED/PENDING/FAILED + summary_status != ready
                 └─ consolidate_episode → summary_status=ready, status=closed
```

**边界规则（第一版刻意保持便宜、可解释、无模型参与）**

| 触发 | 默认阈值 | 理由 |
|---|---|---|
| 作用域变化 | — | 换了 counterpart / branch / session 就是另一段经历 |
| 空闲间隔 | 180 分钟 | 一次「坐下来聊」的自然尺度 |
| 轮数上限 | 24 轮 | 超过这个规模的单次会话很少仍是「一件事」 |
| token 上限 | 6000 | 再大摘要就无法忠实代表这一段 |
| 显式关闭 | API | 供测试与未来的话题切换信号使用 |

这些数字**与 working window / prompt budget 完全无关**：24 ≠ 8，6000 ≠ 7000，
测试里显式断言了这一点（`EpisodeService` 上不存在任何 prompt 预算属性）。

**允许 LLM 参与关闭，但不依赖它**：边界判定完全不调用模型。模型只负责「怎么描述」，
它的失败会落到 `failed + last_error`，Episode 依然被关闭、raw turns 依然完整、
下一次调度继续重试。

---

## 4. 摘要契约

`EpisodeSummary`（`domain/episode.py`，schema 校验，不信任模型 JSON）：

```
title, summary,
important_events, commitments, unresolved, emotional_arc,
topics, entities, importance, confidence,
user_stated, persona_stated, inferred_context
```

* 列表统一清洗：去空、去重、单条 ≤400 字符、最多 24 条（防模型写出无界 blob）。
* `importance` / `confidence` 越界回落 0.5，不会成为 1.7。
* **来源分离**：`user_stated`（用户明确说过）、`persona_stated`（人格说过，含它的猜测）、
  `inferred_context`（推断，不得当事实）。Prompt 明确要求不得把
  「你肯定就是舍不得她」这类人格猜测写成用户事实 —— 这条在测试里有专门断言，
  为 Phase 4 的 Semantic Fact 预留了不混淆结构与数据。
* 摘要只写 `memory_episodes`；**transcript 正文从不写入 Episode 表**
  （测试断言 episode 行里搜不到原文）。

---

## 5. 覆盖率不变式

```
committed_turns = session_turns（persona 已提交轮次）
assigned        = 有 memory_episode_turns 的轮次
pending         = 已分配但所属 Episode 摘要未 ready 的轮次
unassigned      = 尚未分配的轮次（升级前的历史，等待 lazy backfill）
orphaned        = 无法到达的轮次：所属 session 已消失，既不能分配也不能 backfill
unavailable_sources = provenance 行所指向的 raw source 被显式删除（session/room 删除）
```

恒等式 `assigned + unassigned = committed`；**`orphaned` 必须为 0**。

三个概念必须分开，混起来要么掩盖真实丢失，要么误报：

| | 含义 | 处理 |
|---|---|---|
| `unassigned_pending_backfill` | 真实存在、只是还没组织 | `backfill()` 收回 |
| `unavailable_sources` | 来源被**显式删除**（用户删了 session/room） | Episode 打 `source_availability` 标记，显式可见 |
| `orphaned` | 轮次存在但无从归属（session 消失） | 结构性为 0；出现即为数据损坏 |

`source_availability` 逐条 Episode 记录（`complete` / `partial` / `deleted`），
并带 `unavailable_source_count`。`inspect_episode()` 的每条 source 都有 `resolvable`
布尔值 —— **不存在「看起来正常、点进去什么都没有」的静默悬空**。

### 5.1 删除语义（Phase 3.1 / A5）

现有契约：`delete_session` 会级联删除 `session_turns`（`delete_derived_memories`
只控制派生 memory/change events）。Episode 层据此定义：

| 调用 | Episode 行为 |
|---|---|
| `delete_session(True)` | 该 session 唯一派生的 Episode、source 关系、summary lineage 一并删除；指向这些 turn/episode 的 Fact 证据行移除，证据清空的 Fact 标记 `retracted` |
| `delete_session(False)` | Episode 保留（调用方明确要求保留 derived memory），打 `partial`/`deleted` 标记；受影响的 Fact 打 `metadata.provenance_availability=partial` |
| `delete_room` | `room_transcripts` 被清空 → `shared_user` 来源不可解析 → 受影响 Episode 打 `partial`；persona 自己的轮次在 `session_turns` 里，仍然可解析 |

`source_kind` 决定去哪个 store 解析：`session_turn` → `session_turns`；
`shared_user` / `room_transcript` → `room_transcripts`。一一对应，不做跨 store 兜底，
因此「不可用」的判定是确定且对称的。

查看方式：`persona-continuum memory episode-coverage [--persona X] [--json]`。

---

## 6. 迁移与旧数据

* 新增两张表 + 一张 `schema_migrations` 版本表；**不动任何既有表**。
* 迁移纯增量、`IF NOT EXISTS`、可重复执行；`rollback_memory_episodes()` 是显式回滚路径
  （只 DROP 这两张表 + 版本行），测试断言回滚后 memories / session_turns 完好且可重新迁移。
* 旧数据：**lazy backfill**，`backfill(limit=40)` 按 `created_at` 从旧到新分批；
  不跑模型（只做分配），所以不会在升级后第一次启动卡住。

真实数据实测（1.1GB 生产库的**只读副本**，`sqlite3.backup()` 快照）：

| 步骤 | 结果 |
|---|---|
| migrate | **0.10s** |
| 223 条既有 committed turns 全量 backfill | **0.04s**（6 批 × 40） |
| 结果 | 223 assigned / 0 unassigned / **orphaned 0** / 109 episodes |
| 数据完整性 | memories 966、session_turns 223、personas 22、rooms 25、room_transcripts 318 —— 前后完全一致 |
| 第二次执行 | 0.02s，processed=0，episodes 仍为 109（幂等） |

---

## 7. 异步与重启

* 分配是同步的（与 commit 同事务）；摘要永远在回复路径之外。
* 房间路径：turn 提交后 `_schedule_episode_consolidation` 起一个后台任务，
  复用既有的 `_summary_inference_lock` 与「房间正在发言则让路」规则，
  每轮最多 `memory_episode_consolidation_batch`（默认 3）个 Episode。
* 打开旧房间时也会触发一次扫描：backfill 出来的 pending 没有活跃轮次可依附，
  重开房间就是处理它们的自然时机。
* 重启恢复：`init()` → `recover_pending()` 把 `closed`/`failed` 且未 ready 的 Episode
  重新标为 `pending_consolidation`（只恢复工作清单，不自动重跑）。
* 崩溃安全：Episode 行本身就是 durable 状态，没有内存队列。

---

## 8. 配置

```python
Config(
    memory_episode_enabled=True,
    memory_episode_max_turns=24,
    memory_episode_max_tokens=6000,
    memory_episode_idle_gap_minutes=180,
    memory_episode_consolidation_enabled=True,
    memory_episode_summary_input_max_tokens=16384,
    memory_episode_consolidation_batch=3,
    memory_episode_backfill_batch=40,
)
```

## 9. 观测与调试

* 每个 turn 的 `persona_commit_completed` 事件带 `episode_id`。
* 后台摘要完成后广播 `memory_consolidation_report`：
  `episode_id / action / boundary_reason / source_turn_count / source_token_estimate /
  summary_created / consolidation_version / pending / error / episode_status`。
  普通日志不含对话正文。
* CLI：
  * `memory episodes [--persona] [--room] [--status] [--json]`
  * `memory episode <id> [--sources] [--text]`（`--text` 是唯一会打印正文的路径）
  * `memory episode-coverage`
  * `memory episode-backfill [--limit] [--persona]`
* 真实模型验收脚本：`scripts/phase3_su_he_conversation.py`
  （`--adapter`/`--model` 走真实模型；`--summariser stub` 只做结构验证）。

## 10. 本轮明确不接入 Prompt

Episode 不进入 Context Assembly：Phase 3 的目标是**先正确记下来**。
Context Assembly Report 里的 episode/thread/excerpt 字段仍为 `None`（表示该层尚未接入
上下文），Phase 8 再把 Episode 纳入 MemoryBundle。唯一的例外是可显式调用的
inspector / CLI，它们不属于生产 prompt 路径。
