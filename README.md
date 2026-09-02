# Persona Continuum

[中文](#中文) · [English](#english)

## 中文

Persona Continuum 是一个本地优先的数字人格平台，面向 Codex、Claude Code、
Cursor、OpenCode、TRAE Work 等 Agent 宿主。项目提供 Python 包、CLI、MCP Server、Web UI
和 Agent Skill，用于创建、存储、运行、评估、导入和导出带证据来源的结构化 Persona。

### 它是什么

- 以 SQLite 和 SQLite FTS5 为基础的本地 Persona 仓库。
- 由宿主 Agent 提交研究产物、保留来源与不确定性的八维 Persona 编译管线。
- 管理记忆、情绪、需求、关系、目标、会话和分支状态的运行时。
- 将历史事实、推断、用户修正与反事实模拟严格分离的延续引擎。
- 支持多人讨论、专家会诊、平行世界和长篇叙事创作的本地工作台。

### 它不是什么

- 不是云端 LLM、Embedding、数据库或任务队列的封装。
- 不会在 MCP Server 内调用付费模型 API；自然语言研究和推理由当前宿主 Agent 完成。
- 不是用单条角色提示词替代证据、来源和知识边界的角色扮演工具。
- 不会把模拟延续或叙事预测标记为历史事实或既定正史。

### 安装与首次运行

```bash
uv sync
uv run persona-continuum init
uv run persona-continuum doctor --json
uv run persona-continuum persona create "Alex Chen"
uv run persona-continuum persona list
```

启动本地 Web UI：

```bash
uv run persona-continuum web
# 或指定地址和端口
uv run persona-continuum web --host 127.0.0.1 --port 8765 --open
```

MCP Server 可供 Codex、TRAE Work 及其他兼容 MCP 的宿主使用。Codex 的 stdio 配置示例：

```toml
[mcp_servers.persona_continuum]
command = "uv"
args = ["run", "persona-continuum-mcp"]
cwd = "/absolute/path/to/persona-continuum"
startup_timeout_sec = 20
tool_timeout_sec = 60
```

### 创建与使用 Persona

通过 MCP 创建 Persona 时，宿主 Agent 应按以下顺序执行：

1. `persona_create`
2. `persona_add_sources`，或用 `persona_add_source_text` 保存网页研究正文
3. `persona_create_compilation_task`
4. `persona_submit_research_artifact`
5. `persona_compile`
6. `persona_structural_check`
7. 使用 `evaluation_create_suite` / `evaluation_prepare_case` /
   `evaluation_commit_result` 完成宿主驱动的质量评估

Web UI 与平行世界共用 `PersonaCreationOrchestrator`。创建任务可暂停、恢复、取消，
也可等待用户补充访谈缺口。公共人物研究必须显式具备 Web Search/Fetch 能力；能力缺失时
任务会明确失败，不会用模型记忆伪装成已检索证据。私人人物默认使用本地材料和引导式访谈。

与 Persona 对话时，先调用 `persona_prepare_turn`，由宿主 Agent 基于结构化上下文生成回复，
再调用 `persona_commit_turn`。反思和反事实延续同样采用 prepare → host artifact → commit，
并始终绑定到明确的分支。

### 多 Agent、平行世界与叙事创作

- **多人房间**：支持实时 Web UI、WebSocket 事件、发言调度、专家会诊和可恢复会话。
- **Agent / Profile Library**：Persona 身份、Agent Host、模型、Reasoning Effort 与 Room Session
  相互解耦；组织、机构和群体决策 Profile 不会被误当作缺失的人类角色。
- **Recall Gate**：模型生成前必须满足
  `recall_started < recall_completed < agent_started`。
- **Parallel World**：保留因果链、分支隔离、重放与明确的模拟标记。
- **Narrative Studio**：分离作者真相、角色知识和观众知识；预测分支在显式提交前不进入正史，
  并可生成面向 AI 视频制作的 Production Package。

更多设计说明见 [`docs/README.md`](docs/README.md)、
[`docs/WEB_ROOM.md`](docs/WEB_ROOM.md) 和
[`docs/NARRATIVE_STUDIO.md`](docs/NARRATIVE_STUDIO.md)。

### 本地数据与隐私

默认数据目录为 `~/.persona-continuum`，可通过 `PERSONA_CONTINUUM_HOME` 修改。
私人人格材料、数据库、运行时记录、导出包和凭据默认留在本机；导出必须由用户显式触发。
仓库忽略 `.env*`、密钥文件、本地 Persona 数据、数据库、缓存以及本机 Agent 工作目录。
请勿把真实 API Key、个人材料或生产凭据放入 Git。为真实私人个体创建 Persona 前，请确认
已获得适当同意，并评估身份、隐私、肖像、声音和分发风险。

所有房间状态、日志、WebSocket 帧和前端载荷都应经过密钥脱敏。删除来源会递归失效其派生
声明、记忆和编译结果；删除 Persona 会移除数据库记录、索引、运行时状态、包目录和相关会话。

### 已知限制

- 音频转写、视频理解和图像 OCR 目前是扩展点，并非内置能力。
- 公共研究、语义反思、基准评审和延续推理由宿主 Agent 完成，而不是 MCP Server。
- `persona_structural_check` 只检查结构完整性；真实 Persona 质量仍需宿主驱动的评估套件。
- 本地测试通过不等于外部 Agent、模型、浏览器或物理设备的生产验收。

### 致谢与原创性说明

Persona Continuum 是原创实现，其设计受到若干开源项目与公开规范启发：

- 华术创建的 Nuwa Skill 启发了公共人物研究、并行来源采集、认知框架提取、决策启发式、
  表达 DNA、证据透明度与 Persona 质量验证流程。
- MiroFish 启发了世界事件、分支时间线、多 Agent 模拟框架和反事实演化；本项目不包含、
  不修改 MiroFish 或 OASIS 的代码。
- Model Context Protocol 与 MCP Python SDK 提供本地 MCP Server 使用的协议和 SDK。
- TRAE Work、Codex、Claude Code、Cursor、OpenCode 等兼容 Agent 宿主提供模型推理与工具执行环境。

Persona Continuum 未复制 Nuwa Skill、MiroFish 或 OASIS 的代码、提示模板或专有结构。
领域模型、SQLite 持久层、Persona 运行时、记忆系统、情绪与关系引擎、分支隔离、产物 Schema
及 MCP 实现均为原创。其他设计依据记录在 [`docs/`](docs/) 下的相关文档中。

### 性能原则

Persona Continuum 只优化执行策略，不降低研究深度、证据标准、模型选择、推理级别、
来源追踪、矛盾处理和质量门槛。能力缓存、持久运行时池、增量研究、优先级调度和上下文增量
传输都必须保持同一质量策略与失败契约。只读性能摘要位于 `GET /api/performance/summary`。

---

## English

Persona Continuum is a local-first digital persona platform for Agent hosts
such as Codex, Claude Code, Cursor, OpenCode, and TRAE Work. It provides a Python package,
CLI, MCP server, and Agent Skill for creating, storing, running, evaluating,
exporting, and importing structured persona packages.

## What It Is

- A local persona repository backed by SQLite and SQLite FTS5.
- A persona compiler that stores evidence-backed artifacts submitted by the host Agent.
- A runtime that prepares structured turn context and commits memory, affect,
  need, relationship, and session state.
- A host-artifact counterfactual continuation engine that keeps simulated data separate from history.
- A CLI and stdio MCP server for Agent hosts.

## What It Is Not

- Not a cloud LLM wrapper.
- Not an embedding API client.
- Not a role-play prompt generator that hides evidence boundaries.
- Not a claim that simulated continuations are historical facts.

## No LLM API Mechanism

Persona Continuum does not require or call OpenAI, Anthropic, Gemini, DeepSeek,
OpenAI-compatible, cloud embedding, Zep Cloud, or other paid model APIs. The
running Agent host performs natural-language research and reasoning, then
submits structured artifacts to the local MCP server.

## Install

```bash
uv sync
uv run persona-continuum init
uv run persona-continuum doctor --json
```

## MCP Configuration

The server works with Codex, TRAE Work, and other MCP-compatible hosts.
Codex stores MCP servers in `config.toml`; for stdio:

```toml
[mcp_servers.persona_continuum]
command = "uv"
args = ["run", "persona-continuum-mcp"]
cwd = "/absolute/path/to/persona-continuum"
startup_timeout_sec = 20
tool_timeout_sec = 60
```

## Skill Installation

Repo-scoped Codex skills are discovered from `.agents/skills` or parent skill
locations. For this project, copy or symlink:

```bash
mkdir -p .agents/skills
ln -s "$(pwd)/skills/persona-continuum" .agents/skills/persona-continuum
```

Codex also supports user skills under `$HOME/.agents/skills`.

## First Run

```bash
uv run persona-continuum persona create "Alex Chen"
uv run persona-continuum persona list
```

## Create A Persona

Through MCP, the host Agent should call:

1. `persona_create`
2. `persona_add_sources`
   or `persona_add_source_text` for structured web research text
3. `persona_create_compilation_task`
4. `persona_submit_research_artifact`
5. `persona_compile`
6. `persona_structural_check` for structural completeness
7. `evaluation_create_suite` / `evaluation_prepare_case` / `evaluation_commit_result`
   for host-driven persona quality benchmarks

### Persona Creation Runtime

The Web UI's **Create Persona** flow and Parallel World Persona completion use
the same application-layer `PersonaCreationOrchestrator`. It freezes the
selected READY Agent, reported ModelCapability, Reasoning effort and
Credential/Auth binding, then routes all output through the existing
`PersonaService` → `EvidenceSource` → eight `ResearchArtifact` dimensions →
`CompilationService` pipeline. Public Deep Research requires an explicit
ResearchToolBroker; without Web Search/Fetch capability the job fails closed
instead of using model memory as research. Private personas default to local
materials and Guided Interview, and fictional personas use supplied works or
setting documents.

Creation jobs are asynchronous and durable in `persona_creation_jobs`; the UI
can pause, resume, cancel, answer interview gaps, and follow persisted events.
Parallel World Preview and Direct Create both stop for explicit confirmation
when a human Actor has no safe Persona match. Organizations and environment
actors are not treated as missing people.

Public Research uses adaptive `standard`, `deep`, `exhaustive`, or `custom`
quality policies. Deep starts at 30 independent sources with a 60+ preferred
target, then continues when life-stage, dimension, contradiction, source
balance, or marginal-information gates are not satisfied. Raw reposts are
clustered and do not count as independent evidence; every round persists a
research checkpoint, gap analysis, source-quality assessment, and information
gain snapshot. Soft budgets may be exceeded for high-priority gaps, while a
hard budget or exhausted source space is reported explicitly as
`completed_with_gaps`.

## Chat With A Persona

Use `persona_start_session`, `persona_prepare_turn`, generate the final reply in
the host Agent, then call `persona_commit_turn`.

## Upload Sources

Supported formats: `.txt`, `.md`, `.json`, `.jsonl`, `.csv`, `.html`, `.docx`,
text-extractable `.pdf`, and `.zip` containing those files. ZIP path traversal
and oversized files are rejected. ZIP members are parsed with their own format
parsers; DOCX/PDF bytes are never decoded as UTF-8 text.

## Persona Packages

V1.1 exports use:

```text
manifest.yaml
package_schema.json
checksums.json
data/*.jsonl
files/{identity,cognition,affect,expression,evidence,continuation,evaluation,runtime}
```

Each `data/*.jsonl` record carries `schema_version: "1.1"`. `checksums.json`
covers `manifest.yaml`, `package_schema.json`, every data file, and every
persona file. Imports reject duplicate ZIP members, unchecked files, modified
component files, invalid schema, unsafe paths, and checksum mismatches before
transactional import. Modes are `full`, `identity_only`, and `redacted`.
Single-persona `full` export omits active cross-persona rooms by default to
avoid dangling sessions; use `room_export_mode="bundle"` when a multi-persona
room and all room personas must be portable together.

## Counterfactual Continuation

Use `continuation_create`, add world events, create branches, then call
`continuation_prepare_step`. The MCP server returns constraints and an artifact
schema; the host Agent must call `continuation_commit_step` with structured
state deltas before any branch advances. Without that artifact, branches stay
`waiting_for_host`. A branch can be selected or compiled only after at least one
valid host step has been committed; branch compilation writes isolated
counterfactual files under `files/continuation/branches/<branch_id>/` and does
not append simulated events to base historical identity files.

## Reflection

`persona_prepare_reflection` returns recent turns, affect, relationships, needs,
goals, activated memory slots, host questions, and an output schema.
Pass `branch_id` to bind reflection to a single branch. By default reflection
uses the persona's current main branch or `main`; it does not mix sibling branch
turns. `persona_commit_reflection` validates and persists host semantic
reflection atomically.
`persona_run_reflection` remains an extractive local fallback and is marked
`reflection_type=extractive_fallback`.

## Data Location

Default data lives under `~/.persona-continuum`. Override with:

```bash
PERSONA_CONTINUUM_HOME=/path/to/data uv run persona-continuum doctor
```

## Privacy

Private materials stay local by default. Export is explicit. For private real
people, obtain appropriate consent and consider identity, privacy, likeness,
voice, and distribution risks.

Source-level deletion removes the source row, invalidates directly supported
claims and memories, recursively removes source-derived artifacts, compiled
components, snapshots, task artifact payloads, and lineage, rewrites evidence
files, removes FTS entries, and marks the persona `needs_recompile`. Session
deletion can delete derived digital-experience, reflection, relationship-update,
and unresolved-event data.

Runtime affect, needs, relationships, active goals, self narrative updates,
unresolved conflicts, and reflection insights are branch-scoped. Branch runtime
files live under `runtime/branches/<branch_id>/runtime_state.json`; siblings do
not read each other's runtime state, and child branches inherit only a snapshot
from their parent at creation.

Deleting a persona removes its database rows, FTS entries, lineage, runtime
state, continuation/evaluation state, package directory, and room sessions. If a
deleted persona was in a room, the transcript marks those turns as deleted, the
speaker order is repaired, and empty rooms are closed.

## Multi-Agent Room Runtime & Web UI

Persona Continuum includes an interactive multi-agent discussion room runtime with real-time Web UI, WebSocket event streaming, and native Agent Adapter support.

### Key Capabilities
- **Decoupled Architecture**: Persona identity, Agent Host runtime, Model selection, and Reasoning effort are fully decoupled.
- **Protocol-First Agent Adapters**: Built-in adapters for Codex, Cursor, Grok, Claude Code, Gemini CLI, OpenCode, Qwen, Kimi, Copilot, Qoder, WorkBuddy, DeepSeek, and OpenAI-Compatible APIs. External adapters can be placed in `~/.persona-continuum/adapters/*.toml`.
- **Dynamic Recall Gate**: Automatic intent and temporal inquiry analysis before agent generation (`recall_started` < `recall_completed` < `agent_started`).
- **Speaker Director**: Natural topic flow, question-target steering, silence prevention, and manual intervention controls.
- **Security & Secret Redaction**: Strict secret scrubbing across state snapshots, database transcripts, WebSocket frames, and UI logs.
- **Modern Web Dashboard**: Real-time token streaming, persona cards, runtime status probes, and live room control panel.

### Agent / Profile Library

The 人物 view is a unified Agent/Profile Library. It keeps the formal
evidence-backed Persona pipeline and also supports organization, institution,
and collective decision profiles. Parallel World first classifies World
Entity Candidates with the selected Agent plus deterministic safety checks,
then asks for confirmation before Actor Completion creates any missing
profile. Existing profiles can be upgraded through a versioned enrichment job;
previous versions and provenance remain queryable. See
[`docs/PROFILE_LIBRARY.md`](docs/PROFILE_LIBRARY.md),
[`docs/WORLD_ENTITY_CLASSIFICATION.md`](docs/WORLD_ENTITY_CLASSIFICATION.md),
[`docs/ACTOR_COMPLETION_ENGINE.md`](docs/ACTOR_COMPLETION_ENGINE.md), and
[`docs/PROFILE_ENRICHMENT.md`](docs/PROFILE_ENRICHMENT.md).
Runtime activity, timeout ownership, child-worker liveness, and API reasoning
capability contracts are documented in [`docs/AGENT_ACTIVITY_CONTRACT.md`](docs/AGENT_ACTIVITY_CONTRACT.md),
[`docs/AGENT_TIMEOUT_OWNERSHIP.md`](docs/AGENT_TIMEOUT_OWNERSHIP.md),
[`docs/BACKGROUND_CHILD_JOB_LIVENESS.md`](docs/BACKGROUND_CHILD_JOB_LIVENESS.md), and
[`docs/API_REASONING_CAPABILITY.md`](docs/API_REASONING_CAPABILITY.md).

### Narrative Studio

The 叙事创作 view is the author control layer for long-form stories (micro
drama, series, novel). The relationship between the layers is strict:

- **Persona** — who a character is (identity, memory, affect, relationships).
- **Room** — how characters interact (multi-agent protocol runtime).
- **Parallel World** — what happens (causal simulation, branches, replay).
- **Narrative Studio** — what the author intends: a versioned Story Bible,
  Story Truth vs Character Knowledge vs Audience Knowledge (information gap),
  canon with atomic audited commits, episode pipeline (PREPARE → FORECAST →
  SIMULATE → DRAFT → AUDIT → COMMIT), isolated forecast branches, clues and
  foreshadowing tracking, and AI-video Production Packages.

Simulations are always non-canonical until an explicit commit; the Narrative
Knowledge Firewall keeps author-only truth out of character prompts. See
[`docs/NARRATIVE_STUDIO.md`](docs/NARRATIVE_STUDIO.md).

### Launching the Web UI

```bash
# Start Web UI on default port 8765
uv run persona-continuum web

# Or specify host and port
uv run persona-continuum web --host 127.0.0.1 --port 8765 --open
```

### Agent Inspection & Room CLI

```bash
# Scan and probe available agent runtimes on the machine
uv run persona-continuum agents scan --json
uv run persona-continuum agents list
uv run persona-continuum agents inspect codex

# Manage discussion rooms
uv run persona-continuum room list
uv run persona-continuum room inspect <room_id>
```

## Known Limits

- Audio transcription, video understanding, and image OCR are extension points,
  not implemented features.
- Public research, semantic reflection, benchmark judging, and continuation
  reasoning must be performed by the host Agent, not by the MCP server.
- `persona_structural_check` reports structural completeness. `persona_validate`
  remains a compatibility alias. Persona quality requires the host-driven
  evaluation suite.
Private Persona material intelligence is documented in
[`docs/PRIVATE_PERSONA_MATERIAL_INTELLIGENCE.md`](docs/PRIVATE_PERSONA_MATERIAL_INTELLIGENCE.md):
raw EvidenceSource records remain authoritative while message/paragraph-level
evidence, deduplication, fusion, contradictions, episodes and full-corpus
retrieval feed the existing eight-dimensional compiler.

## Performance

Persona Continuum keeps local-first and never lowers quality to go faster: the
**Quality Policy is fixed** (research depth, evidence standards, dimensions,
reasoning level, model choice, provenance, lifecycle/contradiction/negative
evidence, compile + validate, parallel-world actor fidelity). Only the
**Execution Strategy** is optimized — repeated model discovery, per-call process
spawning, full re-analysis each round, serial research/room/world waits,
duplicate memory retrieval, and per-event full-row rewrites.

- `src/persona_continuum/performance/` — capability cache, persistent runtime
  pool, research source/query caches, priority execution scheduler, tracer.
- Room transcript cursor + `RoomContextManager` + `StaticPersonaKernel` send a
  persistent Agent only the dialogue delta (rehydrating after a runtime restart)
  instead of re-uploading the whole history.
- Persona Creation analyzes only new evidence each round and still runs one
  Final Global Audit over all evidence before compile/validate.
- Config: `[performance]` keys default to `Automatic`;
  `Config.legacy_execution()` reproduces the pre-optimization execution path
  under the same Quality Policy for A/B comparison.
- Read-only observability: `GET /api/performance/summary`, or
  `uv run python scripts/performance_benchmark.py` for a before/after table.
- Full write-up: [`docs/reports/acceptance/PERFORMANCE_OPTIMIZATION_ACCEPTANCE.md`](docs/reports/acceptance/PERFORMANCE_OPTIMIZATION_ACCEPTANCE.md).

## Acknowledgements and Inspirations

Persona Continuum is an original implementation, but its design was informed
by several open-source projects and public specifications:

- Nuwa Skill, created by Huashu, inspired the public-person research workflow,
  parallel source collection, cognitive-framework extraction, decision
  heuristics, expression DNA, evidence transparency, and persona-quality
  validation.
- MiroFish inspired world events, branching timelines, multi-agent simulation,
  and counterfactual evolution. Persona Continuum does not include or modify
  MiroFish or OASIS code.
- Model Context Protocol and the MCP Python SDK provide the protocol and SDK
  used by the local MCP server.
- TRAE Work, Codex, Claude Code, Cursor, OpenCode, and other compatible Agent
  hosts provide the model-reasoning and tool-execution environment.

Persona Continuum does not copy code, prompt templates, or proprietary
structures from Nuwa Skill, MiroFish, or OASIS. Its domain model, SQLite
persistence layer, persona runtime, memory system, affect and relationship
engines, branch isolation, artifact schemas, and MCP implementation are
original. Additional design references are documented in the relevant files
under [`docs/`](docs/).
