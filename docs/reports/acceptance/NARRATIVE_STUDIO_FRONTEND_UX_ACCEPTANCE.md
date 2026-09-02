# Narrative Studio Frontend UX Acceptance

Status: **NARRATIVE_STUDIO_FRONTEND_UX = COMPLETE**

This round is a frontend / UX completion of Narrative Studio. Narrative Engine
capabilities (Story Bible, Episode, Forecast, Persona Simulation, Audit, Canon,
Production) are unchanged. Domain models and Canon semantics are unchanged.

## Before

Narrative Studio was a developer debug panel:

- New project used `prompt()` for title/logline; `format` and episode count were hardcoded.
- Runtime Routing was a six-card grid of bare agent/model/reasoning text inputs at the top of the project.
- Story Bible was mostly read-only; Final Truth sat in the open; no version UI.
- Cast used `prompt()` to add characters; no bind-existing-Persona control; “自动创建虚构人格” looked like full Persona Creation.
- Outline, Forecast, Simulation, Writer Room, Draft, Audit, Commit, Production were one row of buttons on a single Episodes tab.
- Simulation called `forecast_simulator` and dumped JSON. Cast defaulted to `narrCast.slice(0, 6)`.
- Writer Room auto-filled story Personas as Head Writer / editors.
- `ensure_story_world` was not in the web authoring path.
- Information Gap and Clues occupied top-level navigation.
- Runtime IDs, world/branch IDs, and JSON were the default surface.

## After

Narrative Studio is a sequential short-drama workbench:

1. 作品设定
2. 故事圣经
3. 角色
4. 全剧大纲
5. 单集创作
6. 连续性
7. 制作

Default Simple Mode hides runtime/world/branch IDs, JSON, fingerprints, and generation traces. Those remain under 高级 / 技术详情.

## New Project Flow

- `dlg-narr-new` replaces browser prompts.
- Fields: title*, logline*, description, format, genre[], tone[], target_audience, planned_episode_count, duration min/max.
- Defaults: `micro_drama`, 60 episodes, 90–120 seconds. User can change them.
- POST `/api/narratives` only. No new create API.

## Project Settings

- Dedicated 作品设定 tab.
- PATCH `/api/narratives/{project_id}` for the same fields.
- Save button.
- Danger zone: 删除作品 with `confirmDlg` second confirmation, then existing DELETE API.

## Runtime Settings

- Removed from the main canvas.
- Header control: **⚙ 创作模型** opens a modal.
- Default creative model: source (本地 CLI / API) + Agent / Model / Reasoning `<select>`s.
- Reuses `getSelectableAgentsForSource`, `getRuntimeModels`, `getRuntimeReasoningOptions`, `runtimeModelOptionLabel`, `runtimeReasoningNotice` via `fillSharedRuntimeSelects` / `bindSharedRuntimeSelector`.
- Only READY (CLI) / Connected (API) agents. No free-text IDs.
- Advanced: per-stage override (collapsed). Stages: Story Architect, Outline Writer, Forecast Simulator, **Scene Actor**, Screenwriter, Reviewer, Production Planner. Each inherits the default until override is enabled.
- Generation Mode lives in the same advanced block. Default AI Agent. Deterministic / Offline available. AUTO only in Advanced Mode.
- Copy: AI Agent 失败将直接报错，不会静默使用规则模式.

## Story Bible

Sections: 核心故事, 世界规则, 主要角色, 关键地点, 全剧时间线, 作者秘密.

Shows Premise, Core Question, Theme, World Rules, Characters, Locations, Master Timeline, Final Truth.

Final Truth is collapsed with **AUTHOR ONLY / 仅作者可见 / 不会作为角色知识注入 Persona**.

Actions: 编辑, 保存新版本, 版本历史, AI 重新生成. Storage is the existing bible GET / PATCH / versions / generate-job routes.

## Cast Binding

Character cards show name, role, description, Persona binding.

- Bind from Persona Library `<select>` (display names, not raw IDs in Simple Mode).
- After bind: ✓ 已绑定 {name}, 查看人格, 更换.
- Binding is optional. Extra story characters can stay unbound.
- Advanced: 创建轻量虚构 Persona, with copy that this is **not** full Persona Creation.

After Story Bible generation, unmatched bible characters can be synced (全选 / 部分选择) through POST `/characters`. Match is by bible character id / name. Existing Persona bindings are not overwritten.

## Outline

Independent 全剧大纲 tab. AI 生成 / 重新生成. Cards show EP number, title, narrative_goal, hook, cliffhanger, status. Click opens 单集创作.

## Episode Workbench

Left rail: EP01… EPn. Center: current Episode Plan. Below: ordered pipeline.

| Step | Name | Notes |
| --- | --- | --- |
| 1 | 剧集计划 | from outline |
| 2 | Forecast | OPTIONAL, NON-CANON |
| 3 | Persona 排练 | OPTIONAL, NON-CANON, `scene_actor` |
| 4 | 剧本 | disabled without EpisodePlan |
| 5 | 连续性审核 | disabled without Draft |
| 6 | 提交正史 | disabled without passing audit / when BLOCKING |
| 7 | 制作 | warning if not yet canon |

Disabled buttons show a reason. Pipeline jobs show progress %, runtime label, 暂停 / 继续 / 取消, and structured failure (stage, agent, model, reasoning, error, 重试) without API keys.

## Persona Rehearsal

Renamed from Simulation. Subtitle: 让绑定的人格在 NON-CANON 场景中自由互动，作为编剧参考.

Author sets location, background, goal, and participants. Default-checked actors are **bound Personas only** — not `narrCast.slice(0, 6)`.

Runtime stage is `scene_actor`, not `forecast_simulator`.

Results default to dialogue, decisions, state/relationship/knowledge changes. Raw JSON is under 技术详情.

Forecast and rehearsal both auto-`ensure_story_world()` on the service (idempotent). Authors never click “初始化世界”.

## Writer Room

Removed from the main pipeline. Lives under 高级创作工具. Runs only after Writer Personas / roles are configured in 创作模型. Story characters are not auto-assigned as writers. Backend Writer Room is unchanged.

## Continuity

Information Gap + Clues are no longer top-level nav. Continuity tab sections: 信息差, 伏笔与线索, 角色弧 / Plot Threads, Canon 状态. Technical IDs hidden in Simple Mode.

## Production

Structured collapsible blocks: Screenplay, Shot List, Image Prompt, Video Prompt, Dialogue Timing, Subtitle, SFX, BGM. No 3000-character screenplay truncate.

## Project header status

Under the title:

`微短剧 · 60集 · 90–120秒`

chips: Story Bible ✓, 角色绑定 N, Outline ✓, Canon EP n/N.

## Tests

See the **Quality gates** section below for the live command log. Automated coverage added:

| Requirement | Evidence |
| --- | --- |
| Full project settings create/edit | `tests/integration/test_narrative_api.py::test_project_full_settings_create_and_edit` |
| READY-only runtime selector + cascade helpers | `tests/unit/test_narrative_studio_frontend_contract.py::test_runtime_settings_are_modal_with_shared_selectors` |
| Existing Persona binding | `tests/unit/test_narrative_studio_ux.py::test_bind_existing_persona_keeps_unbound_characters` |
| Story Bible → Cast sync no duplicate / no bind overwrite | frontend `unmatchedBibleCharacters()` + bind unit test |
| Scene Actor routing | UX unit test + JS contract `narrativePayload("scene_actor"` |
| Story World auto ensure | `test_forecast_and_simulation_auto_ensure_story_world` |
| Episode pipeline gating copy | JS contract |
| Writer Room no auto story Personas | JS contract + Writer Room hidden until configured |
| Project delete confirmation | `confirmDlg(..., true)` on 删除作品; DELETE API already tested |

## Remaining Risks

- Workbench DOM is vanilla-JS rendered; not covered by a headless browser E2E. Contract tests + API/service tests stand in.
- AUTO generation mode is hidden until Advanced Mode. Authors who relied on AUTO in the old grid must open 创作模型 → Generation Mode after enabling 技术详情.
- Writer Room requires manually assigning library Personas to writer roles. There is no bundled “staff writer” Persona.
- `ensure_story_world` still needs a Story Bible. Forecast / rehearsal before a bible still fail, with a bible-missing error rather than a world-ID prompt.
- Long jobs still poll `/api/narrative-jobs/{id}` every 800ms (same as before). The global Background Job Center remains Persona/Profile jobs; Narrative jobs surface in the project header card.

## 完整创作流程人工 walkthrough

Do not hardcode story text into product code. Use a project such as 《裁员通知来自十年后》 as data.

1. Open **叙事创作**. Confirm the flow hint: 作品设定 → 故事圣经 → 绑定核心人物 → 全剧大纲 → 单集创作 → 审核并提交正史 → 制作包.
2. Click **新建作品**. Fill the modal (not a browser prompt): title, logline, description, format=微短剧, genres/tones, audience, 60 集, 90–120 秒. Create.
3. Land on **作品设定**. Edit and **保存**. Confirm header line `微短剧 · 60集 · 90–120秒`.
4. Click **⚙ 创作模型**. Choose 本地 CLI or API, then Agent / Model / Reasoning from READY/Connected selects only. Do not type IDs. Save. Optionally expand 按阶段覆盖 and set Scene Actor independently.
5. **故事圣经** → AI 重新生成. Wait for the job card (percent + runtime + 暂停/取消). Read Premise / rules / characters. Confirm Final Truth is collapsed and marked AUTHOR ONLY.
6. If the banner appears, sync Story Bible characters (full or partial). Confirm existing bindings are not replaced.
7. **角色**: bind 方宁 and 陈默 from the Persona dropdown (names, not IDs). Leave other characters unbound. Open 高级操作 only if a lightweight synthetic Persona is needed; read the warning.
8. **全剧大纲** → AI 生成. Confirm 60 (or planned) episode cards with goal / hook / cliffhanger.
9. Click **EP01**. Workbench rail selects EP01. Pipeline steps 1–7 are visible. Forecast and Persona 排练 are optional and labelled NON-CANON.
10. Optional Forecast: AI 生成候选方向 or 手动方向, set Horizon, 开始推演. Choose a card as 创作方向. Confirm copy: 选择方向 ≠ 提交正史. No raw JSON by default.
11. Persona 排练: set 地点 / 背景 / 目标. Confirm 方宁 and 陈默 are checked because they are bound; extras are not auto-checked. Start rehearsal. Read dialogue cards, not JSON. Technical JSON is folded.
12. **生成剧本**. If outline is missing, the button stays disabled with “没有 EpisodePlan…”.
13. **审核**. If there is no draft, the button stays disabled. BLOCKING findings disable 提交正史 with a reason.
14. **提交正史**. Header Canon chip increments.
15. **生成制作包**. Open Screenplay / Shot List / Image / Video / Dialogue / Subtitle / SFX / BGM. Confirm the screenplay is not truncated to 3000 characters.
16. Open **连续性** for 信息差 / 伏笔 / 角色弧 / Canon. Confirm 信息差 and 伏笔 are not top-level tabs.
17. Delete is only in 作品设定 danger zone, and asks for confirmation.

Throughout, the author never types agent ID, model ID, persona ID, world ID, branch ID, or JSON.

## Quality gates

Recorded at time of writing for the non-pytest gates:

```text
uv run ruff check .
All checks passed!

uv run mypy
Success: no issues found in 206 source files

uv run python -m compileall -q src
(ok)

git diff --check
(ok)

node --check src/persona_continuum/web/static/app.js
(ok)
```

Targeted tests:

```text
uv run pytest tests/unit/test_narrative_studio_ux.py \
  tests/unit/test_narrative_studio_frontend_contract.py \
  tests/integration/test_narrative_api.py -q
15 passed
```

Full suite:

```text
uv run pytest -q --tb=line
1061 passed, 4 skipped, 1 warning in 1162.88s (0:19:22)
```

The warning is an existing zipfile duplicate-name notice in `test_import_rejects_checksum_tampering_and_rolls_back`, not from this UX change.

Static files on the local dashboard (`http://127.0.0.1:8000/`) already include the new workbench HTML/CSS/JS (`app.css?v=2.7.0`, `app.js?v=2.7.0`). Restart `persona-continuum web` so the running process picks up `ensure_story_world` auto-init, `/arcs`, and `latest_audit` on episode list. Hard-refresh the Narrative tab so the browser drops the previous `app.js` document.
