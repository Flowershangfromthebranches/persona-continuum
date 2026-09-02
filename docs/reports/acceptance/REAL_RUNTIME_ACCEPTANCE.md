# REAL_RUNTIME_ACCEPTANCE.md

## Persona Continuum — 真实运行链路（Runtime Wiring Repair）验收报告

**验收时间**: 2026-08-24
**环境**: macOS (Apple Silicon), Python 3.12.8, SQLite
**测试范围**:
1. 房间 30% 初始化修复与 8 阶段生命周期验证
2. 前端 Lobby 纯净展示就绪的 Local CLI 与 API Provider
3. 服务端驱动的全自动多轮辩论（>= 3 Turns）
4. 平行世界 AI 引擎选型（World Builder、Actor 默认运行时、按 Actor 覆盖）
5. LLM World Builder 动态解析 WorldSeed 与 Actor 阵容
6. 平行世界真实大模型 Actor 推演、决策、时钟推进与 WebSocket 广播
7. 全量静态检查与单元测试验证（`ruff`, `mypy`, `pytest`）

---

### 一、核心问题解决与修复清单

| 模块 / 阶段 | 问题根因 | 修复方案 | 验证状态 |
|---|---|---|---|
| **P0. Room 30% 初始化失败** | 缺乏预检机制，30% 阶段在模型解析失败时直接崩溃导致卡死在创建中 | 重构为明确的 8 阶段流水线 (`5% -> 15% -> 25% -> 40% -> 55% -> 70% -> 90% -> 100%`)，并在 25% 阶段执行 `preflight_room_bindings` 强预检 | **已修复并通过** |
| **P1. 房间 Lobby 选项过滤** | 显示了未就绪/不可用的 Agent | 严格过滤 `agent.status === 'ready' && agent.runtime_source === 'local_cli'`，无可用时提示引导 | **已修复并通过** |
| **P2. 区分 Local CLI 与 API** | 单一混杂选择框 | 每个 Participant Slot 提供 `[ 本地 CLI ]` vs `[ API Provider ]` 单选切换 | **已修复并通过** |
| **P3. 模型/思考预算级联** | 模型选择硬编码 | 严格根据所选 Agent 的 `models` 动态加载，并联动其 `supported_reasoning_efforts` | **已修复并通过** |
| **P4/P5. 错误显性化与就绪状态** | 报错后静默卡死或错误进入空白房间 | 在 Lobby 显示 `#room-init-error` 面板，只有在 `room.status === 'ready'` 时才进入 Live Room | **已修复并通过** |
| **P6/P8. 服务端驱动自动辩论** | 需要前端手动点步 | 房间默认模式为 `AUTONOMOUS`，服务端 `run_autonomous_discussion` 循环驱动多轮对话 | **已修复并通过** |
| **P7. 主持人插话机制** | 条件过严导致按钮禁用 | 输入框非空且房间在 `ready`/`discussing`/`paused` 均可插话 | **已修复并通过** |
| **P9/P10/P11. API 提供方探测与注册** | API 添加后无法即时挂载到 Agent 列表 | 新增 `AgentRegistry.register_api_provider` / `unregister_api_provider`，添加后自动触发缓存刷新 | **已修复并通过** |
| **P12/P14. 平行世界 AI 引擎选型** | 平行世界无法选择推演大模型 | 创建界面增加 World Builder 引擎和默认 Actor 运行时选择（CLI / API -> Agent -> Model -> Reasoning） | **已修复并通过** |
| **P13/P15. LLM World Builder 解析** | 角色和世界只能写死模板 | `LLMWorldBuilder` 调用真实大模型将分歧描述解析为结构化 `WorldSeed`，并支持前端角色表预览与覆盖 | **已修复并通过** |
| **P16/P17. 运行时配置持久化** | 重启或分支后运行时丢失 | 新增 `world_runtime_bindings` 数据库表，持久化冻结每一个 Actor 的 Agent、Model 与凭据配置 | **已修复并通过** |
| **P18/P19. 仿真链路与 WebSocket** | 推演无实时追踪，且失败时易发生状态损坏 | 新增 `/api/worlds/{world_id}/ws` 广播推演阶段；仿真执行 Fail-closed 策略，Actor 决策失败立即回滚 | **已修复并通过** |

---

### 二、真实运行时（Real Runtime）全链路验收结果

使用当时本机可用的本地 CLI Agent (`codebuddy`) 及真实大模型执行端到端验收：

#### 1. 真实多智能体房间辩论验收（Test 1）
- **参与角色**: Steve Jobs 与 Jensen Huang
- **辩论议题**: "Should consumer platforms build proprietary neural hardware or standardize on open CUDA accelerators?"
- **运行时绑定**: `codebuddy` (Model: `codebuddy-default`)
- **8阶段预检与初始化**:
  - `5%` creating -> `15%` scanning_runtimes -> `25%` validating_bindings -> `40%` loading_personas -> `55%` loading_memories -> `70%` connecting_agents -> `90%` validating_sessions -> `100%` ready
- **服务端自动推演（3 Turns）**:
  - Turn 1: Director 选定 `Jensen Huang`（Reason: domain_expertise），生成专业论述并提交记忆。
  - Turn 2: Director 选定 `Steve Jobs`（Reason: competitive_relationship），进行针锋相对的架构回应。
  - Turn 3: Director 选定 `Jensen Huang`（Reason: domain_expertise + competitive_relationship），完成深度总结。
- **结果**: **3 轮真实 LLM 对弈全部完成，Transcript 完整持久化，通过验收。**

#### 2. 真实平行世界推演验收（Test 2）
- **分歧描述**: `"2011年10月5日，乔布斯健康好转重返苹果，决定全面启动自研AI神经处理器项目并与英伟达展开芯片架构博弈。"`
- **World Builder**: 由 `codebuddy` 实时解析自然语言，生成包含 5 个动态角色（`steve_jobs`, `apple_inc`, `nvidia_corp`, `apple_silicon_org`, `jen_hsun_huang`）的 `WorldSeed`。
- **Actor 运行时绑定**: 5 个 Actor 均成功创建并持久化至 `world_runtime_bindings` 表。
- **仿真时钟步进（Step Timestep）**:
  - 5 个 Actor 并行执行 LLM 观察、反思与决策（`decision_source: llm`）。
  - 生成 5 个已提交的时间线事件（包含战略项目立项、CUDA竞争评估与NPU研发观察）。
  - 时钟成功推进至 `2011-10-06`。
- **结果**: **LLM World Builder 与 Actor 推演链路全部真实打通，通过验收。**

---

### 三、质量门禁检查（Quality Gates）

```bash
$ uv run ruff check .
All checks passed!

$ uv run mypy
Success: no issues found in 146 source files

$ uv run pytest
============ 212 passed, 1 skipped, 2 warnings in 136.63s (0:02:16) ============
```

**结论**: 所有目标（房间可靠创建并自动讨论、创建房间只展示真实可用 CLI/API、平行世界明确选择 Agent/Model/Reasoning 并真实推演）已全部达成并通过严苛验收。
