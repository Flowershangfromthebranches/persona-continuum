# ACTIVE_THREADS.md — Memory Architecture v2, Phase 5（Active Threads）

## 0. 这一层解决什么

Phase 3 建立了 Episode（发生了什么），Phase 4 建立了 Semantic Fact（什么是真的）。
两者都回答不了另一个问题：

> 这件事**还没结束**，它现在推进到哪儿了？

事实与 Thread 的区别是这一层存在的全部理由：

| | Semantic Fact | Active Thread |
|---|---|---|
| 回答 | 什么是真的 | 什么事情还在进行 |
| 形态 | 一个带有效期窗口的**值** | 一个有状态与历史的**过程** |
| 结束 | 值被取代（`valid_until`） | 事情本身了结（`RESOLVED`） |
| 例子 | 「用户计划下个月去重庆」 | 「重庆旅行这件事推进到买票了」 |

因此：

```
"下个月我准备去重庆。"   →  Fact: 重庆旅行计划(planned)   →  Thread: 重庆旅行(ACTIVE)
   ... 几十个无关轮次 ...
"票我买好了。"          →  Thread: 重庆旅行 + MILESTONE(tickets_purchased)
   ... 更多无关轮次 ...
"我已经从重庆回来了。"   →  Thread: 重庆旅行 RESOLVED（Plan Fact → completed）
```

**「票我买好了。」里没有任何关键词能匹配「重庆旅行」。** 这正是这一层存在的理由，
也是 RecallGate 的 regex 无法解决、而本层必须解决的问题。

明确不做（属于后续 Phase）：Hierarchical Summaries、Provenance raw recall 注入、
MemoryBundle / 生产 Context Assembly、GraphRAG / Neo4j、
**任何 Thread / Fact 注入生产 prompt**、Relationship Timeline 重构。

---

## 1. 作用域

`(persona_id, counterpart_id, branch_id)` —— 与 Episode / Fact / memory 完全一致的隔离。
`room_id` 只用于 provenance（`room_transcripts` 以它为键），**不是**隔离键：
同一个人在另一个房间继续同一段关系时，Thread 必须延续。

## 2. 数据模型

### `memory_active_threads`

| 字段组 | 字段 |
|---|---|
| 身份 | `id`, `persona_id`, `counterpart_id`, `branch_id`, `thread_key`, `thread_type` |
| 内容 | `title`, `summary`, `current_state_json` |
| 状态 | `status`（open/active/waiting/resolved/cancelled/stale） |
| 权重 | `importance`, `confidence` |
| 时间 | `opened_at`, `last_activity_at`, `resolved_at`, `cancelled_at`, `stale_at`, `created_at`, `updated_at` |
| 版本 | `consolidation_version` |
| 隔离 | `visibility`, `material_scope` |
| 关联 | `related_previous_thread_id`（久别重开的同一件事） |
| 其他 | `metadata_json` |

```sql
CREATE UNIQUE INDEX idx_memory_active_threads_live_key
  ON memory_active_threads(persona_id, counterpart_id, branch_id, thread_key)
  WHERE status IN ('open','active','waiting','stale');
```

「一个 scope 内同一 canonical key 只能有一条**存活**的 Thread」是数据库约束，不是应用层自觉。
RESOLVED/CANCELLED 行退出该索引，所以历史保留了，而同一件事**新的一次发生**仍然可以表达。

### `memory_thread_events`（append-only 历史）

`event_id` / `thread_id` / `event_type`（create/update/milestone/waiting/resolve/cancel/reopen/stale）/
`event_key` / `summary` / `state_json` / `occurred_at` /
`source_episode_id` / `source_turn_id` / `source_availability` / `confidence` / `metadata_json`

* `(thread_id, event_key)` 唯一：`event_key` 由
  `(event_type, episode_id, turn_id, 归一化 summary)` 哈希得到，是**稳定来源身份**。
  同一 Episode 重放两次 = 一次事件，milestone 不会被写两遍。
* 状态变更**只追加**，旧状态从不被覆盖：
  重庆计划 → 决定去 → 选时间 → 买票 → 出发 → 完成 全程可读，
  而不是只剩一行 `status=resolved`。

### `memory_thread_sources`（provenance 关系表）

`(thread_id, source_type, episode_id, turn_id)` 主键 + `session_id` / `room_id` /
`evidence_role`（opened/supporting/milestone/resolved）/ `excerpt_available`。

链路：**Thread → Thread Event → Episode → Source Turns → 原始对话文本**。
`inspect_thread()` 对每条 source 给出 `resolvable`，所以「看起来正常、点进去什么都没有」
不会发生。

### `memory_thread_facts`（Thread ↔ Fact lineage）

`(thread_id, fact_id)` 主键 + `relation`（primary/supporting/milestone/resolved_by）。

Thread **不复制** Fact 正文：它只指向 Fact，Fact 仍是自身文本的唯一事实源；
Fact 被删除时 link 随 `ON DELETE CASCADE` 一起消失。

### `memory_thread_link_candidates`（歧义安全）

`(episode_id, thread_id)` 唯一 + `confidence` / `reason` / `status`（pending/accepted/rejected）。

低置信度或模型明确表示「几个候选同样合理」时，**不写 Thread**，只登记候选链接。

### Episode 上的 resolutio 状态

`thread_resolution_status` / `thread_resolution_attempts` / `thread_resolution_error` /
`threads_resolved_at`（真实列，可索引）构成 **durable 待办清单**，重启后 `recover_pending()` 能找回。

---

## 3. 生命周期

```
CREATE ──► ACTIVE ──► WAITING ──► ACTIVE
             │  ▲                   │
   MILESTONE │  │ REOPEN            │ RESOLVE
             ▼  │                   ▼
           (推进)              RESOLVED ──►（长期沉默）STALE
             │                        ▲
             └──── CANCEL ──► CANCELLED

RESOLVED/CANCELLED --(超过 reopen 窗口后的新事件)--> 新 Thread + related_previous_thread_id
```

* **RESOLVE 必须有明确结束证据**（「已经和好了」「问题修好了」「我从重庆回来了」）。
  长时间没聊到**永远不会**自动 RESOLVED，最多 STALE。
* **REOPEN**：最近窗口内（`memory_thread_reopen_window_days`，默认 30 天）同一件事再次出现 →
  原地 reopen；超过窗口 → 新建 Thread，但 `related_previous_thread_id` 指向旧行，历史不丢。
* **CANCELLED 不会被静默复活**：明确取消过的事情再次出现时建新 Thread（并链接旧的），
  而不是假装它从未被取消。
* **阶段推进 ≠ 完成**：买票是 MILESTONE，不是 RESOLVED；Plan Fact 保持 `planned`。

---

## 4. Resolver

### 输入（每 Episode 一次）

```
当前 Episode 原始 turns
+ 结构化摘要（可选）
+ 该 Episode 产出/更新的 Semantic Facts
+ 当前 scope 的候选 Thread 列表（短名单）
```

### 候选短名单（deterministic，无模型）

1. **最近活跃 Thread 保底**：`memory_thread_top_recent`（默认 8）一定进入列表 ——
   即使 lexical 匹配为 0，这就是「票我买好了。」仍能看到「重庆旅行」的原因。
2. **lexical/topic 重合**（CJK bigram + latin word 的包含度）
3. **Fact lineage**：已与本 Episode 的 Fact 关联的 Thread 强烈优先
4. **最近结束的 Thread**（reopen 窗口内）：否则永远无法做出 REOPEN 判断

上限 `memory_thread_max_candidates`（默认 24）。**这是 Memory Consolidation 内部预算，
不是生产 prompt 预算**：它与 `room_prompt_target_tokens` 6000/7000、`recent_message_window`
没有任何关系，也不允许由它们推导。

### 输出（schema 校验）

`operation` / `thread_id` / `new_thread{...}` / `confidence` / `reason` /
`state_update` / `milestone` / `source_turn_ids` / `related_fact_ids` / `ambiguous_thread_ids`

* 引用不存在的 `thread_id` / `fact_id` / `turn_id` → **拒绝**（`skipped_invalid`），
  模型无法凭空制造数据库 ID。
* 跨 scope 引用（另一个 counterpart / branch / persona 的 Thread）→ 拒绝。
* 一条畸形 action 不会毁掉整批；声称有 action 但一条都不可用 → `invalid_thread_payload`。
* 空列表是合法答案（「没什么在进行中的」）。

### 歧义安全

| 情形 | 行为 |
|---|---|
| `confidence < memory_thread_link_confidence_threshold`（默认 0.55） | 记为 candidate，**不改 Thread** |
| 模型填了 `ambiguous_thread_ids` | 每个候选记一行 candidate，**不改 Thread** |
| 多个 relationship Thread 同时存在 + 无证据的「她联系我了」 | 不猜，登记候选 |

### 确定性身份（不依赖模型）

| 情形 | 处理 |
|---|---|
| 同一 key（`重庆旅行计划` → `重庆旅行`，噪音词剥离 + CJK 内部空格归一） | 合并进原 Thread |
| 包含关系（`重庆旅行安排` ⊇ `重庆旅行`，并要求 type 不冲突） | 合并进原 Thread，别名记入 `metadata.merged_subjects` |
| 「去重庆」vs「重庆旅行」（key 与包含都不成立） | 由候选列表 + 模型语义判断完成延续；服务端不做激进的模糊合并 |

第三条是刻意的取舍：错误合并会破坏身份，代价高于多一条候选。
所以「不许生成四个 Thread」由**两道防线**共同保证，而不是靠一个会误伤的相似度阈值。

---

## 5. 幂等与重启

| 层 | 机制 |
|---|---|
| Episode 级 | `thread_resolution_status='ready'` 让同一 Episode 默认不再重跑（`force=True` 用于显式重放） |
| 事件级 | `event_key` 唯一 + `INSERT OR IGNORE` |
| 投影级 | 只有**新事件真的插入**了才更新 thread 行（confidence / last_activity / milestones 不会漂移） |
| 来源级 | `_has_source(thread, episode)`：该 Episode 已经产出过这条 Thread → `replayed` |
| 重启 | Episode 行本身就是 durable 状态；`recover_pending()` 只恢复工作清单，不自动重跑 |

崩溃在「Thread 已创建」与「Episode 标记 ready」之间 → 重新解析时命中 `_has_source` → `replayed`，
不会产生第二条 Thread。

## 6. Backfill

109 个既有 Episode **不会**在启动时全部跑 Resolver：

* 打开房间时 `_memory_backlog_pending()` 会检查三层待办（Episode / Fact / Thread），
  有欠账才调度一次 bounded pass；
* 后台 pass 每轮最多 `memory_thread_resolution_batch`（默认 2）个 Episode；
* CLI `memory thread-backfill [--limit]` 给出有界视图与剩余量；
* `sweep_stale()` 是无模型的 bounded UPDATE。

## 7. 不阻塞聊天

Thread 解析与 Episode/Fact 一样：**永远不在回复路径上**。
失败写入 `thread_resolution_status='failed'` + `thread_resolution_error`，
用户回复不受影响，下一次 pass / 重启 / 房间重开都会重试。

Pipeline 顺序（同一个后台 pass 内，一次只跑一个推理）：

```
Raw Turn → Episode（同步，与 commit 同事务）
        → Episode Consolidation（模型）
        → Semantic Fact Extraction（模型）
        → Active Thread Resolution（模型）
```

## 8. 删除生命周期（Phase 3.1 语义）

| 调用 | Thread 行为 |
|---|---|
| `delete_session(True)` | 受影响事件的 `source_availability='deleted'`，source 行 `excerpt_available=0`，Thread `metadata.provenance_availability='deleted'`；**Thread 与事件历史保留**（对话做过什么不会被删除改写） |
| `delete_session(False)` | 同上，标记为 `partial` |
| Fact 被删除 | `memory_thread_facts` 行随外键级联消失，Thread 不留下假的 lineage |

任何情况下都不会出现「看起来正常、点进去什么都没有」的静默悬空。

## 9. 未接入 Prompt

Phase 5 只做 **Store + Resolve + Lifecycle + Inspect**。Context Assembly Report 新增：

```
threads_available            # 真实存活数（store 统计）
threads_selected = 0         # 设计为 0
thread_tokens = 0            # 设计为 0
thread_injection = "phase8_not_enabled"
threads_pending_resolution   # durable 待办数量
```

Context Policy 的任何一个数字都没有改动：
`episodes_selected` / `episode_tokens` 仍为 0；`relationship_event_tokens` /
`historical_excerpt_tokens` / `raw_excerpts_expanded` 仍为 `None`（该层不存在）。

## 10. 配置

```python
Config(
    memory_thread_resolution_enabled=True,
    memory_thread_resolution_batch=2,
    memory_thread_max_candidates=24,           # consolidation 预算，非 prompt 预算
    memory_thread_top_recent=8,
    memory_thread_link_confidence_threshold=0.55,
    memory_thread_stale_after_days=60,
    memory_thread_reopen_window_days=30,
    memory_thread_max_creates_per_episode=3,
    memory_thread_resolution_input_max_tokens=12000,
)
```

## 11. 观测与调试

* 后台事件 `memory_thread_report`：
  `episode_id / action / threads_created / threads_updated / threads_milestoned /
  threads_waiting / threads_resolved / threads_cancelled / threads_reopened /
  threads_merged / candidates_recorded / candidate_links / skipped_low_confidence /
  skipped_noop / skipped_invalid / replayed / error / pending / resolution_version`。
  不含对话正文。
* CLI：
  * `memory threads [--persona] [--status] [--type] [--live] [--json]`
  * `memory thread <id> [--events] [--sources]` —— 状态 / 里程碑 / current_state /
    related facts / events / sources / pending candidates / 是否可解析回原文
  * `memory thread-backfill [--limit] [--sweep]`
* 服务层：`inspect_thread()` / `stats()` / `candidate_threads()` /
  `pending_resolution_episodes()` / `recover_pending()` / `sweep_stale()` /
  `decide_link_candidate()`。
* 真实模型验收脚本：`scripts/phase5_active_threads.py`。

## 12. 与 Phase 3 / 4 的关系

```
turns → Episode（发生了什么）
          ├─► Semantic Fact（由此可知什么）
          └─► Active Thread（什么事情还在进行）── 关联 Fact，但不复制 Fact
```

三层同时存在、互不替代；Thread 的 provenance 只指向 Episode（再经 Episode 指向 turn），
不复制原文。
