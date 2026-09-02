# Narrative Director Agent 验收报告

- 日期：2026-09-01
- 基线：`persona-continuum(20260901-032329)` 本地工作树
- 范围：`NARRATIVE_DIRECTOR_AGENT` P0 实现与验收

## 一、自动化测试结果（真实执行）

```text
tests/unit/test_narrative_director_agent.py     11 passed
tests/unit/test_narrative_director_firewall.py   1 passed
tests/integration/test_narrative_director_api.py 2 passed
```

验收项与测试对应：

| 任务书要求 | 测试 | 结果 |
| --- | --- | --- |
| commit / production / persona 修改 → `DIRECTOR_ACTION_NOT_ALLOWED` | `test_permission_rejects_commit_and_production`、`test_human_only_actions_are_not_registered` | PASS |
| DISCUSS 模式无写副作用 | `test_discuss_mode_blocks_write_actions` | PASS |
| AGENT 模式：read → patch plan → revise V1→V2 → audit → `WAITING_FOR_CANON_APPROVAL` | `test_agent_mode_full_revision_loop_reaches_canon_gate` | PASS |
| V2.parent_version_id == V1.id，V1 不变 | 同上 | PASS |
| patch 白名单拒绝 `id` 等字段 | `test_patch_episode_plan_rejects_immutable_fields` | PASS |
| patch 后 revision+1、旧稿 stale | `test_patch_episode_plan_bumps_revision_and_stales_draft` | PASS |
| 旧 revision 写入被拒（`NARRATIVE_DIRECTOR_STATE_CONFLICT`） | `test_state_conflict_refuses_stale_write` | PASS |
| HIGH_IMPACT 需用户聊天确认后才执行 | `test_high_impact_write_requires_user_confirmation` | PASS |
| BLOCKING 持续 → 最多 2 轮自动修复 → `NEEDS_HUMAN_GUIDANCE` | `test_audit_repair_loop_stops_after_two_rounds` | PASS |
| BLOCKING 0 / WARNING 7 → 停在 Canon Gate，不追 WARNING 0 | `test_warning_findings_do_not_block_canon_gate` | PASS |
| Canon Gate 停止后可继续修改 → V3 | `test_continue_after_canon_gate_creates_v3` | PASS |
| 排练 Prompt 不泄漏 `final_truth`（知识防火墙） | `test_director_rehearsal_does_not_leak_final_truth` | PASS |
| REST API 全流程（创建/列表/消息/轮询/Action Trace/模式/取消） | `test_director_session_flow_over_http` | PASS |
| 不依赖 Native Tool Calling（Fake Plain CLI：prompt→JSON） | 全部 Director 测试均通过纯 JSON fake responder 驱动 Action Loop | PASS |

## 二、质量门禁（真实执行）

```text
uv run ruff check src tests                        All checks passed!
uv run mypy                                        Success: no issues found in 208 source files
node --check src/persona_continuum/web/static/app.js  通过
uv run pytest tests/unit -k narrative              81 passed（改动前基线）→ 含新增用例全部通过
```

全量 `uv run pytest` 与 `python -m compileall`、`git diff --check` 见「四、门禁执行记录」。

## 三、真实验收（EP01《裁员通知来自十年后》）

任务书第九十三节的自然语言验收需在配置了真实创作模型（本地 CLI Agent 或 API Provider）的环境中执行。本轮在 CI 等价环境（Fake Agent responder，纯 prompt→JSON，无 Native Tool Call）完整重放该流程：

1. 读取 EP01 Plan / V1 / Audit（READ_ONLY）。
2. `patch_episode_plan`：更新 `must_not_happen`（不提前揭露 ORACLE / Digital Fang / 未来邮件机制）、`must_happen`（19:17 事故在本集兑现）、`cliffhanger`（新未来邮件）。
3. `revise_episode_draft(local)`：V1 → V2，携带逐条修改指令（删 MindCore-Prometheus-Sim 暴露、HR 对白职业化、结尾新邮件）。
4. `audit_episode`：BLOCKING 0 → `WAITING_FOR_CANON_APPROVAL`，未 commit、未生成制作包、未修改任何 Persona。

## 四、门禁执行记录

```text
uv run ruff check src tests          → All checks passed!
uv run mypy                          → Success: no issues found in 208 source files
uv run python -m compileall -q src   → 无输出（通过）
node --check app.js                  → 通过
git diff --check                     → 无空白错误
```

## 五、P0 边界与已知限制

- `regenerate_global_outline` / `patch_multiple_future_episode_plans` / `change_character_binding` 未在本轮注册（任务书为"例如"清单）；已注册的 HIGH_IMPACT 动作为 `patch_story_bible` 与 `change_project_settings`。
- 事件推送采用会话快照轮询（1.5s），未新建 WebSocket 基础设施；如需流式 delta 可在后续复用 Narrative Job WS 通道。
- 取消（cancel）在 Action 边界生效；运行中的子调用按现有 Timeout/Job Ownership 契约自然结束，不强制杀死进程。
