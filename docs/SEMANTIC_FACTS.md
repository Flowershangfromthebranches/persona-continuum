# SEMANTIC_FACTS.md — Memory Architecture v2, Phase 4（Semantic Facts + Temporal Validity）

## 0. 这一层解决什么

Phase 3 之后，系统能回答「发生过哪几段事情」。但仍然回答不了：

> 用户喜欢喝什么？他现在在做什么项目？他下个月打算去哪？这些是什么时候成立的？

这些是跨 Episode 的**可复用事实**。Phase 4 建立 Semantic Memory：

```
Raw Turn → Episode → Episode Summary（可选）
                  → Semantic Fact Extraction
                        → Dedup / Conflict Classification
                        → Temporal Validity
                        → Fact Store + Provenance
```

明确不做（属于后续 Phase）：Active Threads、Relationship Timeline、Hierarchical
Summary、Hybrid Retrieval、MemoryBundle、**任何 Fact 注入 prompt**、GraphRAG、Neo4j。

---

## 1. 作用域

`(persona_id, counterpart_id, branch_id)` —— 与 Episode / memory 一致的隔离。
同一个 user 在另一个人格里说过的话，**不会**自动跨 persona 共享（未来若需要
global counterpart memory，另开设计，本轮不偷偷实现）。

---

## 2. 数据模型

### `memory_semantic_facts`

| 字段组 | 字段 |
|---|---|
| 身份 | `id`, `persona_id`, `counterpart_id`, `branch_id`, `category`, `fact_key`, `value_key` |
| 内容 | `subject`, `predicate`, `value_json`, `display_text` |
| 状态 | `status`（active/superseded/candidate/retracted/expired）, `origin`, `durability`, `plan_status` |
| 证据 | `confidence`, `evidence_count`, `last_confirmed_at` |
| 时间 | `valid_from`, `valid_until`, `observed_at`, `temporal_expression`, `temporal_normalized`, `temporal_confidence` |
| 版本 | `superseded_by_fact_id`, `supersedes_fact_id`, `extraction_version` |
| 隔离 | `visibility`, `material_scope` |
| 其他 | `created_at`, `updated_at`, `metadata_json` |

`fact_key = category|subject|predicate`（规范化后），`value_key` 由 value 规范化得到。
**唯一部分索引**保证同一 `(scope, fact_key, value_key)` 只能有一条 `active`：

```sql
CREATE UNIQUE INDEX idx_memory_semantic_facts_active_key
  ON memory_semantic_facts(persona_id, counterpart_id, branch_id, fact_key, value_key)
  WHERE status = 'active';
```

把去重做成数据库约束而不是应用层自觉。被 superseded 的行退出该索引，历史因此完整保留。

### `memory_fact_sources`（provenance 关系表）

`(fact_id, source_type, episode_id, turn_id)` 主键 + `session_id`, `room_id`,
`evidence_role`, `excerpt_available`。**没有**把 provenance 塞进 JSON。

`evidence_count` 是**从关系行重算**的（distinct episode，或没有 episode 时的 distinct
turn），不是自增计数器——重放不会让它漂移。

### Episode 表上的提取状态

`fact_extraction_status` / `fact_extraction_attempts` / `fact_extraction_error` /
`facts_extracted_at`（真实列，可索引查询），构成 durable 的待办清单：
重启后 `recover_pending()` 能找回未提取的 Episode。

---

## 3. 抽取

* **输入**：Episode 的**原始 source turns**（不是摘要）+ 可选结构化摘要 + 该 scope 下最近
  的 active facts（供模型判断 relation，上限 `memory_fact_max_existing_context`）。
  从原始轮次抽取，避免「摘要再摘要」的信息损耗。
* **schema 校验**：`FactExtractionPayload`；越界数值回落、枚举未知值降级（未知 origin →
  `inferred`，未知 status → 不可能是 active）、单条文本截断、列表上限 32。
  其中一个畸形条目**不会**毁掉整批：垃圾条目被丢弃，有效条目保留；但如果模型声称有事实却
  一条都不可用 → `invalid_fact_payload`，不入库。
* **空结果是合法的**：`{"facts": []}` 表示「没什么值得记的」，不是失败。
* **不阻塞聊天**：抽取永远在回复路径之外，异步、分批、可失败、可重试。
* **不需要摘要**：摘要还 pending 时照样可以从原始轮次抽 Fact。

---

## 4. 去重 / 冲突分类（核心规则）

模型给出 `relation`（same/supports/compatible/supersedes/contradicts/unrelated）与
`related_fact_id`，服务端按固定规则执行——**任何一次失效都是显式的，且双向记账**：

| 情形 | 动作 |
|---|---|
| 该 Episode 已经产出了这条事实（含同值历史行） | **重放**：不新增证据、不改状态、不翻转历史 |
| 同 scope+slot+value 已存在且 active | **强化**：加证据行、`evidence_count` 重算、confidence 递增、`last_confirmed_at` 只前进、origin 只能变强 |
| 同 slot 不同 value 且 relation=supersedes（或 contradicts+exclusive） | **失效**：旧行 `valid_until = 新行 valid_from`、`superseded_by`、`status=superseded`；新行 `supersedes` 指向旧行 |
| relation=compatible（slot 非单值） | **并存**：两条 active，旧的不动 |
| relation=compatible 但 predicate 声明为单值 | 判定矛盾 → 落 `candidate` + 冲突标记（**不**静默覆盖） |
| relation 未分类 / contradictions（非单值） | 落 `candidate` + `metadata.conflict_with_fact_id` + `unresolved_conflict`；旧事实保持 active |
| 无同 slot 事实 | 新建 active |

额外两条安全规则：

* **temporary 不能失效 permanent**：一时状态不得让长期偏好作废（落 candidate）。
* **temporary 也不能占住 permanent 的位**：一次「现在有点想喝 X」之后到来的真正偏好，
  会取代这个临时状态（临时状态保留为历史）。

---

## 5. 时间有效性

* 新建时 `valid_from = Episode.started_at`（`observed_at` 同），`valid_until = null`。
* 失效时 `valid_until = 新事实的 valid_from` —— 旧值不会消失，只是**不再有效**。
* `durability=temporary` → `valid_until = observed_at + memory_fact_temporary_valid_hours`
  （默认 24h），并在 metadata 里标注。
* 相对时间（「下个月」）保留 `temporal_expression` 原文，尽量给出 `temporal_normalized`
  （如 `2026-10`）与 `temporal_confidence`；**解析失败不影响事实落库**。
* 计划类（PLAN/GOAL/COMMITMENT）用 `plan_status` 表达
  planned/active/completed/cancelled/expired。同一个 slot+value 上的状态推进（planned →
  active）是**状态变化**，不是新事实：仍是一条 Fact，证据累积。

---

## 6. 来源角色

`origin` ∈ `user_asserted` / `persona_asserted` / `system_observed` / `inferred`。

`memory_fact_persist_inferred` 默认 **False**：纯人格推断不落库（记为
`skipped_inferred`，不是失败）。这条是为了防止：

> 苏禾说「你肯定就是舍不得她。」 → 系统固化成「用户舍不得她」→ 未来把它当作用户事实。

即使显式打开该开关，落库的 origin 仍然是 `inferred`、confidence 保持低位，绝不会变成
`user_asserted`。同一事实后来被用户确认时，origin 只会**升强**（inferred → user_asserted）。

---

## 7. 幂等

三层保证：

1. `memory_fact_sources` 的 `(fact_id, source_type, episode_id, turn_id)` 主键 —— 同一证据只计一次。
2. active 唯一部分索引 —— 同值事实不可能出现第二条 active。
3. 抽取前检查「该 Episode 是否已是这条事实（含历史行）的证据」—— 重放直接跳过，
   因此 `A → B` 重复执行不会变成 `A → B → B2`，也不会把已被取代的旧值翻回来。

`fact_extraction_status='ready'` 让同一 Episode 默认不再重跑；`force=True` 用于显式重放测试。

---

## 8. 删除生命周期（与 Phase 3.1 联动）

`delete_session(delete_derived_memories=True)`：

* 该 session 唯一派生的 Episode、Episode 关系、summary lineage 一并删除；
* 指向这些 turn/episode 的 `memory_fact_sources` 行移除；
* **证据被清空的 Fact 标记 `retracted`**（而不是继续假装有依据）；
* 其他 session / branch / counterpart 的 Fact 完全不受影响。

`delete_derived_memories=False`：Episode 与 Fact 保留，但
Episode 打上 `source_availability=partial/deleted`，Fact 打上
`metadata.provenance_availability=partial`；`inspect_fact()` 的每条 source 都会显示
`resolvable: false`。**任何情况下都不会出现「看起来正常、点进去什么都没有」的静默悬空。**

---

## 9. 未接入 Prompt

Phase 4 只做 **Store + consolidate + inspect**。Context Assembly Report 新增：

```
semantic_facts_available   # 真实存储统计（active 数量）
semantic_memory_tokens = 0
fact_injection = "phase8_not_enabled"
```

`episodes_selected` / `episode_tokens` 仍为 0（Phase 3 结论不变）；
`threads_*` / `historical_excerpt_*` 仍为 `None`（层不存在）。
这样如果将来出现质量问题，能清楚区分「存储 bug」与「prompt 质量回归」。

---

## 10. 配置

```python
Config(
    memory_fact_extraction_enabled=True,
    memory_fact_persist_inferred=False,      # 默认不固化纯推断
    memory_fact_temporary_valid_hours=24,
    memory_fact_confidence_step=0.05,
    memory_fact_max_existing_context=30,
    memory_fact_extraction_input_max_tokens=12000,
    memory_fact_extraction_batch=3,          # 每次后台 pass 的模型调用上限
)
```

## 11. 观测与调试

* 后台事件 `memory_fact_report`：
  `episode_id / action / facts_created / facts_reinforced / facts_superseded /
  facts_compatible / facts_conflict_pending / facts_replayed / skipped_inferred /
  error / pending / extraction_version`。不含对话正文。
* CLI：
  * `memory facts [--persona] [--counterpart] [--status] [--category] [--json]`
  * `memory fact <id> [--sources]` —— 含 status/origin/confidence/valid_from/valid_until/
    superseded_by/evidence_count/source episode ids/source turn ids/是否可解析
* 服务层：`inspect_fact()` / `stats()` / `pending_extraction_episodes()` / `recover_pending()`。
* 真实模型验收脚本：`scripts/phase4_semantic_facts.py`（见下）。

## 12. 与 Phase 3 的关系

Episode 是「发生了什么」，Fact 是「由此可知什么」。Fact 不替换 Episode，
`digital_experience` / Episode / Fact 三层同时存在：

```
14 digital_experience  →  1 Episode  →  N Semantic Facts
```

Fact 的 provenance 只指向 Episode（再经 Episode 指向 turn），不复制原文。
