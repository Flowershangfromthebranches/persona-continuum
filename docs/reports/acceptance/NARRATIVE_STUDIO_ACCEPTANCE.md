# Narrative Studio — Acceptance Report

Commands run (all green at time of writing):

```bash
uv run pytest tests/unit/test_narrative_models.py \
  tests/unit/test_narrative_knowledge_firewall.py tests/unit/test_narrative_canon.py \
  tests/unit/test_narrative_clues.py tests/unit/test_narrative_context_builder.py \
  tests/unit/test_narrative_forecast.py tests/unit/test_narrative_continuity_auditor.py \
  tests/unit/test_narrative_production.py \
  tests/unit/test_generic_world_initialization.py \
  tests/integration/test_narrative_api.py \
  tests/integration/test_narrative_episode_pipeline.py \
  tests/integration/test_narrative_world_branch_isolation.py \
  tests/integration/test_narrative_room_integration.py \
  tests/e2e/test_narrative_studio_e2e.py
# → all pass

uv run pytest tests/unit        # full unit regression incl. world genericization
uv run ruff check .
uv run mypy
python -m compileall src
node --check src/persona_continuum/web/static/app.js
```

## Acceptance checklist mapping

| Requirement | Evidence |
| --- | --- |
| 60-集微短剧项目可创建 | `test_project_crud`, E2E `_setup_project` (60 EP micro_drama) |
| AI 建立 Story Bible / 角色阵容 | `narrative_generate_story_bible` + fallback; E2E bible v2 |
| 虚构角色自动创建 Persona | `create_missing_personas` → `fictional_or_synthetic_person` (E2E §1) |
| Persona 进入故事 World | `ensure_story_world` seeds explicit actors (E2E §2) |
| 60 集结构生成 | `generate_outline_sync` (planned_episode_count) |
| 每集多候选未来推演 | `forecast_episode` 2–5 directions (E2E §5) |
| 候选未来严格分支隔离 | `test_forecast_branches_are_isolated...`, E2E branch assertions |
| Story Truth / Character / Audience 严格隔离 | firewall unit tests + E2E §4 |
| 人物基于自己知道的信息互动 | `simulate_scene` + firewall prompt blocks (E2E §6) |
| Writer Room 复用 Room Protocol Engine | `test_writer_room_runs_through_protocol_engine` (CUSTOM stages) |
| 模拟结果整理为商业剧本 | ScreenwriterStage timed beat sheet + screenplay (E2E §7) |
| Auditor 发现知识泄漏/吃书/时间线/人物一致性 | `test_narrative_continuity_auditor.py` (BLOCKING codes) |
| Blocking Audit 默认不能进入正史 | `test_blocking_audit_refuses_commit_unless_forced` |
| 用户确认后 Commit / 原子化 | `commit_episode` transaction test (`test_commit_is_atomic...`) |
| 下一集基于新正史继续 | E2E §8 (EP2 prepare sees EP1 summary; revision bump) |
| 结构化 Production Package | E2E §9 + `test_narrative_production.py` |
| 现有功能回归正常 | full unit suite + world integration suites pass |
| 无新增云依赖 | pure stdlib/sqlite; no new pyproject deps |
| RuntimePool / 性能架构不牺牲 | no runtime changes; scene sim uses existing per-turn lease |
| 无作品/厂商写死 | `test_generic_world_initialization.py` (7 tests) |

## Live E2E closeout (2026-08-30, real Agent only)

`scripts/narrative_live_resume.py` reran the remaining P0 chain (Writer Room →
Screenwriter → Continuity Audit → Production Planner → Commit → EP02 context)
against the real local Codex CLI (`codex-cli 0.147.0`, runtime_source
`local_cli`, `include_fake_agent=False`) on the pre-existing live project
`nproj_b0982945cd984025`.

- Runtime used: **Codex / alibailian/qwen3.8-flash / low** (structured output
  mode `prompt_only`). All 12 Writer Room stage tasks and all three downstream
  narrative stages recorded this exact runtime in their traces; zero
  deterministic fallback, zero fake-agent rows.
- Head Writer synthesis was non-empty and gate-checked
  (`final_writer_instruction` persisted as `wrsyn_4b40ab4910f540ed`).
- Screenwriter produced 4 structured scenes (`epv_6b6403784e844a08`);
  audit `naudit_3613bc5cfa2b4c8c` passed with 0 blocking / 11 warning;
  production package `prod_a425e3e715f346de` has 22 shots.
- EP01 committed to canon (`project_revision` 5 → 6,
  `branch_d62baddcc75b4345`); EP02 `prepare_episode` consumed 2 canon
  snapshots. RuntimePool released all leases (active_leases=0).
- Evidence JSON: `/tmp/narrative-live-final-qwen.json`
  (11/11 checks green; full chain ~14 min wall time).

Fixes landed while reaching this state (all covered by unit/integration
regression, `uv run pytest`/`ruff`/`mypy` green):

1. Codex adapter now declares its real `stdin` prompt transport, so the
   shared 64KB ARGV guard no longer misclassifies app-server turns
   (`PROMPT_TRANSPORT_LIMIT_EXCEEDED` on synthesis) —
   `tests/unit/test_codex_prompt_transport.py`.
2. `turn/completed(status=failed)` from the app-server now surfaces as a
   retriable `AGENT_TRANSPORT_ERROR` instead of degrading into the exec
   fallback and masquerading as `REASONING_BINDING_UNVERIFIED` —
   `tests/integration/test_codex_app_server_protocol.py::test_codex_failed_turn_reports_retriable_transport_error`.
3. Protocol actions recover exactly like autonomous turns: a retriable
   runtime failure restarts that participant's session once and retries the
   task (`room_runtime_recovered` event) instead of failing the whole room.
4. Pooled Codex handshake failures only kill a process this acquisition
   actually spawned, so one flaky startup no longer evicts sibling threads
   (`pooled_runtime_affinity_unavailable` cascade).
5. Writer Room `independent_proposals` runs sequentially; advisory-only
   review stages are optional, so a single Head Writer legacy room still
   synthesizes; `stop_room` now always runs (lease leak fix).
6. Empty Head Writer synthesis is a hard failure
   (`NARRATIVE_AGENT_GENERATION_FAILED`), never a silent pass-through.

Known external limitation during this window: the local opencodex router
returned `stream disconnected before completion: adapter_eof` for every
direct `codex exec -c model=gpt-5.6-sol|terra|luna` probe during this
closeout, while `alibailian/qwen3.8-flash` and the account
default worked. Per "choose the test model yourself", the acceptance run
therefore completed on qwen3.8-flash/low; the gpt-5.6*-family Live rerun
can be redone with `scripts/narrative_live_resume.py --model gpt-5.6-sol
--reasoning low` once that upstream route recovers.
