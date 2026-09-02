# Resumable Persona Creation Validation

## 验收环境

- 全部验证基于 FakeAgent / 确定性测试环境（无真实模型调用）。
- 命令：`uv run pytest tests -q`、`uv run ruff check .`、`uv run mypy`。
- Benchmark：`uv run python scripts/benchmark_resumable_upgrade.py`。

## 验收结果

| 检查项 | 结果 |
| --- | --- |
| `uv run ruff check .` | All checks passed |
| `uv run mypy` | Success: no issues found in 181 source files |
| 全量测试（unit + integration + e2e + performance） | **678 passed, 2 skipped**（此前 677 passed + 1 failed，失败项已修复） |
| 新增回归测试 `tests/integration/test_resumable_persona_creation.py` | 18 passed |
| Benchmark 脚本 | 全部完成，数据见 IMPLEMENTATION 文档 D 节 |

## 任务书回归清单核对（第五十一节等）

- **Test 1 正常人格**：e2e `test_persona_creation_e2e.py` + lifecycle 测试通过。
- **Test 2 Batch 失败**：`test_batch_checkpoint_reuses_finished_batches` —
  已成功 batch 通过 fingerprint checkpoint 复用，失败模型上仍成功，模型零调用。
- **Test 3 单维失败（7/8）**：`test_dimension_failure_preserves_completed_dimensions`
  — 7 维 processed ledger + artifact 持久保留；重试只有第 8 维发出模型提示。
- **Test 4 Pause**：`test_pause_and_resume_job_lifecycle` + safe-pause 边界测试。
- **Test 5 Pause + 换模型 + Resume**：resume 支持 runtime 覆盖
  （`resume_with_runtime_change` 执行历史），checkpoint 保留。
- **Test 6 Failure + 换模型**：`test_retry_with_runtime_change_keeps_checkpoints`
  — 新 retry run 继承 checkpoints，原 job 标记 superseded。
- **Test 7 Reasoning 更换**：runtime 覆盖仅影响未来执行步骤（checkpoint
  key 不含 reasoning level，按任务书要求）。
- **Test 8 Context Window**：`test_job_resolution_uses_declared_context_window` +
  `test_batch_count_scales_with_effective_context_window` — 32K/128K/256K/1M
  批次数正确缩放，不再固定 32K。
- **Test 9 Structured JSON malformed**：`test_local_repair_fixes_simple_json_damage`
  — trailing comma / fence / 截断 / prose 全部本地确定性修复，不再调用模型。
- **Test 10 Crash Recovery**：`resume_pending_jobs`（既有）+ 新增的
  batch/dimension checkpoint 使恢复粒度到 batch；job 行持久化 + worker heartbeat。

## 其余验收标准

- **RuntimePool**：12 会话/4 runtime → [3,3,3,3]；affinity 保持
  （`acquire_managed` 回到同一物理进程）；crash 进程被丢弃替换（既有测试）。
- **Room**：state_json 只保留最近窗口（1000 turn 时 18.9KB，10→1000 增长 2.7x）；
  transcript 权威来源为 `room_transcripts`（host/injection 亦入库）；
  restart 后无损重水合；summary 后台刷新不阻塞 turn；interactive 预留 1 槽。
- **World**：tick 持久化单事务（BEGIN=1/COMMIT=1，失败 ROLLBACK）；
  50 actor 只加载 6 个 active 的 memory；builder 高置信分类跳过第二次 LLM。
- **Rename**：display_name 可改；persona_id / binding / artifact / package
  manifest 全部验证不变；重名不修改 persona_id；非法名（空/超长/控制字符）
  报 `ConflictError`。
- **不减少研究质量**：研究轮数 / Evidence Audit / Consistency Audit / Final
  Audit / 八维 / reasoning 默认值全部未动；所有优化来自 checkpoint、复用、
  并行、事务合并与正确 context budget。
