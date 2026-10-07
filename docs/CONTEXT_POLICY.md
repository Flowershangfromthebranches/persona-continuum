# CONTEXT_POLICY.md — Memory Semantics vs Context Policy

## 1. 为什么需要这层

一次本地优化把「这台机器能跑多大 prompt」写成了整个产品的策略：
`room_prompt_target_tokens=6000` / `room_prompt_hard_tokens=7000`、
`room_raw_message_window=8`、`room_recall_top_k=8`、
`room_memory_max_tokens=400`、`room_summary_output_max_tokens=800`
以及 `step_turn` 里硬编码的 6 级降级阶梯。

它们对 MacBook Air M3 24GB 上的 Qwen3.8-27B 4-bit 是合理的「生存模式」，
但对 128K / 256K / 1M 的 API 或 CLI 模型就是**质量上限**：
一个 1M context 的模型同样只能看到最近 8 条消息、最多 8 条记忆、
一份 ≤800 token 的摘要，并且在 7000 token 处被**硬拒绝**。

完整的污染面审计见 [`reports/context-policy-audit.md`](reports/context-policy-audit.md)。

## 2. 两个概念，不许再混

| | Memory Semantics | Context Policy |
|---|---|---|
| 回答的问题 | 苏禾**记得**什么 | 这一轮这个模型**能看到多少** |
| 依赖 | 与模型无关 | provider / model / context window / 本地或远程 / 本机资源 / reasoning |
| 载体 | `memories`、`room_transcripts`、`relationships`、`lineage`、`rolling_summary` | `ContextProfile` + `ResolvedContextPolicy` |
| 能否丢失数据 | 不能 | 不涉及（只选择本轮可见内容） |

**不变式**：切换 Context Policy 不修改 Memory Store 的任何一个字段。
验收测试：`tests/integration/test_room_context_policy_ab.py`。

## 3. Context Profile

`src/persona_continuum/room/context_policy.py`

| 字段 | local_constrained | balanced | remote_quality |
|---|---|---|---|
| `prompt_target_tokens` | 6000（来自 Config） | 由 ratio 推导 | 由 ratio 推导 |
| `prompt_hard_tokens` | 7000（来自 Config） | 由 capability 推导 | 由 capability 推导 |
| `prompt_budget_ratio` | 0.45 | 0.55 | 0.70 |
| `min_prompt_floor_tokens` | — | 16384 | 65536 |
| `unknown_window_prompt_budget` | 7000 | 32768 | 131072 |
| `recent_dialogue_token_budget`（**主单位**） | 1500 | 6144 | 32768 |
| `recent_message_window`（**上限兜底**） | 8 | 32 | 256 |
| `recall_top_k` | 8 | 16 | 48 |
| `memory_max_tokens`（单条） | 400 | 800 | 2000 |
| `memory_budget_tokens`（整块） | 2000 | 6000 | 24576 |
| `summary_max_chars` | 2400 | 8000 | 32000 |
| `summary_output_max_tokens` | 800 | 2000 | 8000 |
| `summary_input_max_tokens` | 4096 | 16384 | 98304 |
| `generation_reserve_tokens` | 1024 | 2048 | 4096 |
| `reasoning_reserve_tokens` | 0 | 2048 | 8192 |
| `historical_excerpt_token_budget` | 0 | 0 | 12000 |
| 降级阶梯 | 历史 6 级（逐字保留） | 3 级 | 1 级（不降级） |

`local_constrained` 的数值**始终从 `Config` 读取**（`local_profile_from_config`），
所以已经压测过的本地调优仍然是用户的，只是不再充当所有人的默认值。

### 降级阶梯

`local_constrained` 返回与修复前**完全相同**的 6 级阶梯：

```
(8, 400, None, None) (4, 300, None, None) (3, 200, None, None)
(2, 133, 6,    None) (2, 120, 4,    1200) (1, 100, 2, 800)
```

`remote_quality` 只有一级 `(48, 2000, None, None)`：大模型不会因为大而被削。

## 4. AUTO 解析

`resolve_context_policy(ContextPolicyRequest) -> ResolvedContextPolicy`

```
1) window = request.context_window，否则查 ModelCapabilityRegistry（source=provider_official_registry）
2) local_endpoint = request.local_memory_constrained is True
                    或（不为 False 且）base_url 指向 loopback / 私网 / *.local
3) AUTO:
     无 window + local_endpoint        -> local_constrained   (unknown_local_endpoint)
     无 window                         -> balanced            (unknown_context_window_defaults_to_balanced)
     local_endpoint 且 window ≤ 32K     -> local_constrained
     window ≥ 128K                     -> remote_quality
     local_endpoint 且 window > 32K     -> balanced
     window ≥ 32K                      -> balanced
     window < 32K                      -> local_constrained   (+ constrained_by_model_capability_not_local_memory)
4) 显式 strategy：先取请求的 profile，再做能力钳制（只能向上放宽，不能强制塞不下的形态）
     quality  + window < 128K -> balanced；window < 32K -> local_constrained
     balanced + window < 32K  -> local_constrained
5) 若最终 profile 是 local_constrained，用 request.local_profile（Config 派生）替换常量
```

**CLI 不被当作本地**：CLI 进程在本机跑，但模型通常不在本机。
kimi-cli 背后的 1M 模型解析为 `remote_quality`，不会被 24GB Mac 拖低。
只有「API 端点指向本机 / 私网」或显式声明 `local_memory_constrained` 才算本地受限。

## 5. 动态 Context Budget

```
capability_budget = window - generation_reserve - reasoning_reserve - safety_reserve
                    (safety_reserve = max(512, min(32768, window // 40)))
profile_budget    = explicit hard 或 max(min_prompt_floor, ceil(window × ratio))
                    (window 未知时使用 profile.unknown_window_prompt_budget)
budget            = min(profile_budget, capability_budget)   # capability 未知时只用 profile_budget
if profile is local_constrained:  budget = min(budget, local_measured_safe_prompt_tokens)
target            = profile.prompt_target_tokens 或 budget
hard              = budget
```

验算（保持本地不变）：

* 本地 16K：`14848 = 16384 − 1024 − 0 − 512`；`min(7000, 14848) = 7000` → `target 6000 / hard 7000`（与修复前一致）
* 远程 1M：`962712` vs `700000 = 0.70 × 1M` → `budget 700000`
* 远程 128K：`91751`
* 未知 window + balanced：`32768`（而不是 7000）

## 6. recent dialogue：token 预算是主单位

`room/context_manager.py`

```python
recent_window_start(transcript, *, message_window, token_budget) -> int
```

* 新到旧贪心填充，直到 `recent_dialogue_token_budget` 用尽或 `message_window` 条数用尽；
* 最新一条永远保留（它就是正在被回应的那条；单条超限由 transport 守卫处理）；
* `token_budget=None` 时退化为历史行为 `max(0, len − message_window)`，旧调用方不受影响。

`prepare()` 与 `eviction_boundary()` **共用同一个函数**——两者若不一致，
就会出现「离开窗口但从未被摘要」的孤儿 turn。

`_room_summary_boundary()` 取房间里**最小**的窗口起点：共享摘要必须覆盖
任何参与者窗口之外的内容，否则孤儿 turn 仍然存在。

## 7. 观测

每个 turn 产出一条 `room_context_policy` 事件（策略解析结果与原因）和一条
`room_context_report` 事件（Context Assembly Report）：

```
context_profile / context_strategy_requested / context_profile_reasons
adapter_id / model_id / context_window / context_window_source / context_window_verified
local_endpoint / local_safety_ceiling_applied
effective_context_budget / capability_budget_tokens / profile_budget_tokens
generation_reserve_tokens / reasoning_reserve_tokens / safety_reserve_tokens
transcript_total_entries / transcript_in_prompt / raw_window / recent_dialogue_token_budget
persona_tokens / dynamic_state_tokens / voice_exemplar_tokens / memory_tokens
summary_tokens / scene_facts_tokens / current_message_tokens / estimated_prompt_tokens
prompt_target_tokens / prompt_hard_tokens / prompt_budget_stage / prompt_budget_stages_available
memory_top_k / memory_candidates / memory_selected / memory_injected / memory_max_tokens
```

`episodes_selected` / `threads_selected` / `episode_tokens` / `thread_tokens` /
`relationship_event_tokens` / `historical_excerpt_tokens` / `raw_excerpts_expanded`
目前是 `None`：表示「该层尚未实现」，**不是 0**。它们是 Memory Architecture v2
（episode / threads / provenance 回溯）落地后要填的字段。

报告里不含任何对话正文与原始 recall query，避免把私密内容写进日志。

## 8. 用户配置

```python
Config(
    room_context_strategy="auto",                 # auto | quality | balanced | local_constrained
    room_context_local_memory_constrained=None,   # True/False 覆盖端点本地性判断
)

ParticipantSlot(..., context_strategy="auto")     # 每个参与者可覆盖
RoomSessionState(metadata={"context_strategy": "quality"})  # 每个房间可覆盖
```

优先级：显式参数 → participant slot → room metadata → 全局 Config 默认值。
`auto` 在任何一层都表示「无意见」，会继续向下传递。

## 9. 尚未实现（不是本层职责）

* Memory Architecture v2：Episodes / Semantic Facts 时间有效性 / Active Threads /
  Hierarchical Summaries / Provenance 回溯 / Hybrid MemoryBundle。
  Context Policy 已经为它们预留了预算字段（`historical_excerpt_token_budget` 等），
  但检索与存储层仍是现有的 `memories` + FTS/BM25 + `rolling_summary`。
* `room_prompt_max_tokens` 仍是死配置（审计报告 1.11）：它的语义现在由
  `local_measured_safe_prompt_tokens` 与动态 budget 承担。
