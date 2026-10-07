# Context Capability / Prompt Transport Audit

Capability discovery and budgeting repair. Context changes affect only
windows that have not yet been dispatched. Existing classification
checkpoints, `conversation-evidence-v4`, `TURN_POLICY_VERSION`, and
Semantic Gate state are not invalidated.

No private chat was sent to an external model. CLI probes were read-only
(`--version`, `--help`, `models`/`--list-models`, `status --json`,
`agy -p /help|/model|/usage`).

## 1. Adapter context audit

| Adapter | Native (fallback) | Runtime discovery | Effective rule | Verified when |
|---|---|---|---|---|
| Grok Build | grok-4.6 = 500K official table | ACP `modelUsage.*.contextWindow`, session/new metadata | runtime > catalog > official table | runtime / ACP / official table |
| Claude Code | Sonnet 5 / Opus 5 = 1M, Haiku 4.5 = 200K | CLI metadata if present | runtime > CLI > official | official registry or runtime |
| Codex | GPT-5.6 Sol API native 1.05M is **native only** | `model/list` context fields, turn usage | **runtime 372K beats API 1.05M** | runtime / protocol_model_list |
| Gemini / agy | 2.5 Pro = 1,048,576 (was 2,097,152) | stream-json usage, `/model` | runtime > CLI listing > official | official or runtime |
| Qoder | catalog via `--list-models` | `--context-window` request, then runtime | requested ≠ effective | runtime after bind |
| Command Code | descriptions like “1M context” | `status --json.context_window` | active model runtime covers registry | `status --json` |
| OpenCode ACP | DISCOVERABLE | `session/new` `contextWindow` | runtime_reported | ACP session metadata |
| Copilot | wrapper, not upstream API | catalog if any | **native-only upstream registry** | wrapper runtime only |
| Qwen / Kimi / DeepSeek | official 1M where documented | stdin CLIs, registry fallback | runtime > catalog > official | official or runtime |
| CodeBuddy / WorkBuddy | argv `-p` | none dynamic this round | registry fallback | official table only |

## 2. Adapter transport audit

Inspected `send()` / `spawn()`, not names.

| Adapter | Mode | Max bytes (default) | Large prompt | Notes |
|---|---|---|---|---|
| codex | stdin | 8MB | yes | app-server stream + exec stdin |
| grok | stream (ACP) | 32MB | yes | headless `-p` is fallback only |
| claude_code | **stdin** (was unknown/64KB) | 8MB | yes | `StreamingJsonCliAdapter` writes stdin |
| gemini_cli | argv (`-p`) or **stdin** when binary is `agy` | 64KB / 8MB | argv no / agy yes | stream-json persistent path |
| opencode | stream | 32MB | yes | ACP |
| command_code | argv | 64KB | no | `-p <prompt>`; context ≠ transport |
| qwen | stdin | 8MB | yes | `run` + stdin write |
| kimi | stdin | 8MB | yes | `chat` + stdin write |
| copilot | stdin | 8MB | yes | inherited plain CLI |
| qoder | argv | 64KB | no | `-p`; `--input-format` exists but unauthenticated, not migrated |
| codebuddy | argv | 64KB | no | `-p`; also has stream-json input (future) |
| workbuddy | argv | 64KB | no | same as codebuddy |
| deepseek_harness | stdin | 8MB | yes | `chat` |

## 3. Hidden limiters found

1. Grok static `131072` overlaying live 500K.
2. Global `PREFERRED_WORKING_RATIO = 0.5` treated as a second model cap.
3. Fixed `max_units=1200` / `max_episodes=24` splitting 500K windows.
4. Claude Code transport inferred as `unknown` → 64KB.
5. Gemini 2.5 Pro table `2,097,152` (overstated).
6. PromptTransportResolver ignoring `StreamingJsonCliAdapter.send()` stdin.
7. Codex `model/list` dropping context fields; API native could leak into effective.
8. Qoder `--context-window` unbound; requested 1M shown as effective.
9. Command Code `status --json.context_window` unused.
10. ACP `DISCOVERABLE` not reading `contextWindow` / `modelUsage`.
11. Planning 32K displayed as Native.
12. `formatTokens` divided by 1024 (`500000` → `488K`).
13. Project static registry marked `verified=yes` / `provider_metadata`.

## 4. Files changed (this round)

- `src/persona_continuum/agent/context_capability.py`
- `src/persona_continuum/agent/context_fields.py` (new)
- `src/persona_continuum/agent/phase_policy.py` (new)
- `src/persona_continuum/agent/context_budget.py`
- `src/persona_continuum/agent/prompt_transport.py`
- `src/persona_continuum/agent/models.py`
- `src/persona_continuum/agent/adapter.py`
- `src/persona_continuum/agent/discovery.py`
- `src/persona_continuum/agent/protocols/acp.py`
- `src/persona_continuum/agent/protocols/streaming_json_cli.py`
- `src/persona_continuum/agent/adapters/{grok,claude,codex,gemini,command_code,other_vendors}.py`
- `src/persona_continuum/application/{material_pipeline,material_intelligence,persona_creation_service}.py`
- `src/persona_continuum/config.py`
- `src/persona_continuum/web/static/{app.js,index.html}`
- tests under `tests/unit/test_context_capability*.py`, transport audit, ACP, Codex, Qoder, Command Code, agy streaming
- `scripts/context_window_scale_benchmark.py`
- `docs/reports/implementation/context-window-scale-benchmark.json`

## 5. Resolver priority

```
runtime_reported
  > runtime_dynamic_probe
  > agent_model_metadata
  > provider_official_registry
  > project_static_registry
  > user_override (downscale only; never verified until runtime confirms)
  > planning_fallback (32K, never shown as Native)
```

Verified only for: runtime, machine-readable agent metadata, official registry.

Wrappers (`codex`, `copilot`, `command_code`): `upstream_context_is_native_only=True`. Official API native fills **Native**, not **Effective**.

## 6. Runtime metadata propagation

```
CLI / ACP / app-server
  → extract_runtime_context_facts()
      contextWindow | remaining | used | maxOutput | compaction
  → session.session_data
      effective_context_window
      remaining_context_tokens
      auto_compaction_detected
  → RuntimeBindingSnapshot
  → ContextCapabilityResolver
  → EffectiveModelCapabilities
  → AgentContextBudgetManager + PhaseContextPolicy
  → material_batch_target_tokens
      min(remaining, usable, phase_working, transport_tokens)
```

## 7. PhaseContextPolicy (defaults, configurable)

| Phase | Ratio |
|---|---|
| material_classification (verified, fresh) | 0.75–0.85 (default 0.80) |
| persona_compilation | 0.65 |
| research | 0.65 |
| audit | 0.70 |
| persistent_agent_session | ≤ 0.60 |
| unknown / unverified | 0.50 |

Working target is **not** model context. UI labels it “Phase Working Target”.

Config: `Config.phase_working_ratios`.

## 8. Dynamic caps

`material_analysis_window_max_units` / `_episodes` default `None` (auto).

- Unverified: scale from 64K/1200/24.
- Verified tokenizer/runtime remaining: **count caps disabled**; token + transport dominate.
- Explicit integers still honored.

## 9. UI

`formatTokens` uses decimal (500K = 500,000; 1.05M = 1,050,000). Detail panel:

Model Native, Runtime Effective, Current Remaining, Usable After Reserve,
Phase Working Target, Prompt Transport, Transport Safe Capacity,
Actual Current Prompt, Utilization, Source, Trust, Verified.

Planning fallback is shown only when Native is Unknown, labelled “not native context”.

Browser E2E was not run (no UI session against a live job). Layout is the existing details panel.

## 10. Tests

Focused unit run: 122 passed (context, transport, grok, claude stdin, gemini, qoder, command-code, ACP, Codex parse). Broader adapter/material set: 77 passed, 1 assertion updated (`official_capability_table` → `provider_official_registry`). Ruff clean on changed modules.

## 11. 64K / 128K / 200K / 500K / 1M synthetic benchmark

2000 synthetic ConversationEpisodes, verified token budget, no private data.

| Runtime | Working | Windows | Cap triggered |
|---|---|---|---|
| 64K | 52,428 | 3 | token_budget |
| 128K | 104,857 | 2 | token_budget |
| 200K | 160,000 | 1 | end |
| 500K | 400,000 | 1 | end |
| 1M | 800,000 | 1 | end |

128K → 500K does **not** keep the same call count because of a 24-episode cap. See `docs/reports/implementation/context-window-scale-benchmark.json`.

## 12. Grok 4.6 before / after

| | Before | After |
|---|---|---|
| Fallback table | 131,072 | 500,000 |
| Mode | FIXED | DISCOVERABLE |
| Authority | static table | `modelUsage.*.contextWindow` |
| UI 500000 | ~488K (÷1024) | 500K (500,000) |

This machine: `grok 1.0.25`. `grok models` lists `grok-4.6` as default (text, no JSON window). Live window is ACP usage.

## 13. Claude Code before / after

| | Before | After |
|---|---|---|
| Transport | unknown / 64KB | stdin / 8MB |
| Sonnet 5 | missing → 32K planning | 1M official fallback |
| Haiku 4.5 | missing | 200K |
| Opus 5 | missing | 1M |

CLI: **NOT_INSTALLED** on this machine.

## 14. Gemini / agy before / after

| | Before | After |
|---|---|---|
| 2.5 Pro | 2,097,152 | 1,048,576 |
| Transport | argv `-p` only | agy: stdin stream-json persistent; `-p` fallback |
| Probe | — | agy 1.2.0; `/model` = `gemini-3.7-flash-high` (read-only, 0 tokens) |

## 15. Qoder before / after

| | Before | After |
|---|---|---|
| Mode | (unset / argv) | CONFIGURABLE_AND_DISCOVERABLE |
| `--context-window` | unbound | bound from `requested_context_window` |
| Effective | requested treated as granted | runtime must verify; UI shows Requested vs Effective |
| List models | **NOT_TESTED** (not logged in) | fallback catalog + registry windows |

CLI: `qodercli 1.1.41`. `--input-format` exists; stdin migration not done this round (still `-p`).

## 16. Codex runtime vs API

Example: official GPT-5.6 Sol native = 1,050,000; Codex runtime reported 372,000 → **effective = 372,000**, native stays 1.05M. Without runtime, effective stays Unknown (planning 32K), not forced 1.05M.

CLI: `codex-cli 0.147.0`. `model/list` now parses context fields.

## 17. Still not dynamically discovering context

| Adapter | Why |
|---|---|
| Claude Code | binary not installed here |
| Qoder | not logged in; no JSON model metadata captured |
| Qwen / Kimi | binaries not installed |
| Copilot (`gh`) | wrapper; no machine-readable context in this probe |
| CodeBuddy / WorkBuddy | argv `-p`; no status JSON context parsed this round |
| DeepSeek Harness (`dsh 0.1.0-rc.7`) | profile launcher; no context listing probed |
| OpenCode models list | ids only, no window in `opencode models` text |

## 18. Follow-ups

1. Confirm Grok ACP `modelUsage.grok-4.6.contextWindow` on a live ACP session (read-only usage frame).
2. Qoder stdin / `--input-format` once authenticated; keep argv as the true current carrier until proven.
3. CodeBuddy `stream-json` input (same family as Claude/agy).
4. Parse Command Code BYOK `contextWindow` from provider config files.
5. Surface `auto_compaction_detected` in job telemetry and shrink later windows.
6. Wire `actual_prompt_tokens` / utilization from RuntimeExecutor into the UI payload (fields exist; live job not exercised).

## Local CLI probes

| CLI | Version | Context reported | Transport | Method | Status |
|---|---|---|---|---|---|
| grok | 1.0.25 | listing has no window | ACP stream | `grok models`, `--help` | probed |
| agy | 1.2.0 | `/model` id only | stdin stream-json | `--help`, `/model`, `/usage` | probed |
| codex | 0.147.0 | not listed in `--help` | stdin | `--help` | probed |
| qodercli | 1.1.41 | `--context-window` flag | argv `-p` | `--help`, `--list-models` | models NOT_TESTED (login) |
| command-code | 1.53.0 | **1048576** active model | argv `-p` | `status --json` | probed |
| opencode | 1.18.22 | none in `models` text | ACP stream | `--help`, `models` | probed |
| codebuddy | 2.149.0 | none | argv; stream-json available | `--help` | probed |
| dsh | 0.1.0-rc.7 | none | unknown profile | `--help` | probed |
| claude | — | — | — | — | NOT_INSTALLED |
| qwen | — | — | — | — | NOT_INSTALLED |
| kimi | — | — | — | — | NOT_INSTALLED |
