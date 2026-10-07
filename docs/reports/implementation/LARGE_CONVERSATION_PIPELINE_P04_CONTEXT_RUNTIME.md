# Large Conversation / Context Runtime — P0.4 Final Acceptance

Scope: finish P0.4 (A remaining-revision grain, B streaming accounting, C adapter
session vs workload scope, D Grok independent-session concurrency, E/F/G lifecycle,
H observability, I/J benchmarks, L regressions). No Persona feature work, no V4
contract change, no TURN_POLICY_VERSION bump, no checkpoint invalidation, no PR.

## 1. Files touched this round

Context runtime:

- `src/persona_continuum/agent/context_fields.py` (new): revision split
  (`context_capability_revision` / `context_remaining_revision`), usage semantics,
  `ContextScope` / `AdapterSessionMode` / `WorkloadContextScope`, pending-usage flush.
- `src/persona_continuum/agent/runtime_executor.py`: unified
  `_begin_context_accounting` / `_finalize_context_accounting` shared by
  `_execute_turn_locked` and `_stream_events_locked`.
- `src/persona_continuum/agent/adapter.py`: binding snapshot carries both revisions,
  scope, and adapter session mode.
- `src/persona_continuum/agent/models.py`: `AgentCapabilityFlags` gains
  `adapter_session_mode` / `parallel_turns_same_session` /
  `parallel_independent_sessions` / `max_parallel_independent_sessions`.
- `src/persona_continuum/agent/discovery.py`: `_apply_context_capability_declaration`
  copies those adapter declarations onto every probe.
- `src/persona_continuum/application/material_pipeline.py`:
  `ResolvedExecutionProfile` three-dimensional concurrency + `classification_worker_count`.
- `src/persona_continuum/application/classification_dispatch.py` (new): independent
  window dispatch with rate-limit downgrade.
- `src/persona_continuum/application/material_intelligence.py`: per-window
  `_participant_id`, verified-cache concurrency injection, metrics stamping.
- `src/persona_continuum/performance/runtime_pool.py`: physical leases released per
  execution; logical affinity retained only while needed.
- `src/persona_continuum/performance/concurrency_cache.py` (new): verified
  independent-session limit cache (24h TTL).
- `src/persona_continuum/agent/adapters/grok.py`, `agent/protocols/acp.py`,
  `plain_cli.py`, `streaming_json_cli.py`: declare usage semantics and concurrency
  dimensions.
- `src/persona_continuum/agent/phase_policy.py` (new), `context_budget.py`,
  `context_capability.py`: phase working targets and dynamic count caps.

Tests / scripts / artifacts:

- `tests/unit/test_runtime_remaining_freshness.py` (A/B regression matrix)
- `tests/unit/test_workload_scope_concurrency.py` (C/D regression, extended)
- `tests/unit/test_context_capability_matrix.py`
- `tests/integration/test_persisted_context_window_scale.py` (window-isolation added)
- `scripts/mock_classification_concurrency_benchmark.py`
- `scripts/grok_independent_session_probe.py`
- `docs/reports/implementation/mock-classification-concurrency.json`
- `docs/reports/implementation/grok-concurrency-probe.json`

## 2. `remaining_context_revision` design

- `context_capability_revision` advances on window/effective/max-output/capability facts.
- `context_remaining_revision` advances **only** when remaining actually refreshed:
  explicit `remaining*` field, or a new `used_context_tokens` under
  `usage_kind == CUMULATIVE_CONTEXT_USAGE` (derived remaining).
- `context_usage_revision` is kept as a telemetry total and is **never** used to
  decide whether to skip the local estimate.
- `_finalize_context_accounting` compares `remaining_revision_start` captured at turn
  start, not the coarse usage revision.

## 3. Capability vs remaining revision

| revision | advances on | used for estimate-skip |
|---|---|---|
| `context_capability_revision` | context_window / effective / max_output / model capability | no |
| `context_remaining_revision` | verified remaining or cumulative used-context | yes |
| `context_usage_revision` | any usage stamp (telemetry) | no |

## 4. execute_text / streaming unified accounting

Both paths call the same `_begin_context_accounting` (records revision start, scope,
pre-turn remaining, model/session identity) and `_finalize_context_accounting`
(consumes any pending flush, then either trusts a runtime-refreshed remaining or does
a local `previous - input - output` estimate for PERSISTENT/PER_WINDOW only).
Streaming output tokens prefer adapter `output_tokens`/`completion_tokens`; otherwise
they are estimated once from the final aggregated text — never per chunk.
Cancellation/failure still accounts when the session keeps context
(`_session_still_holds_context`); a dead process/fresh session does not inherit it.

## 5. AdapterSessionMode

`NONE | PER_REQUEST | PERSISTENT_CAPABLE | UNKNOWN`, declared per adapter and carried
on the probe, binding snapshot, and execution profile.

## 6. WorkloadContextScope

`PER_REQUEST | PER_WINDOW | PERSISTENT | UNKNOWN`, resolved from the phase/pipeline,
not from adapter type.

## 7. Context scope mapping

| workload | scope |
|---|---|
| Material classification / windows | `PER_WINDOW` (`_participant_id = persona_material_intelligence:<window.id>`) |
| Room / persona chat | `PERSISTENT` |
| Research worker | `PER_REQUEST` |
| Persona Compiler | `PER_REQUEST` (per request) |
| Audit | `PER_REQUEST` |

## 8. Concurrency capability

- `parallel_turns_same_session` — concurrent turns inside one logical session.
- `parallel_independent_sessions` — concurrent independent logical sessions.
- `max_parallel_independent_sessions` — verified ceiling.
- Effective workers = `min(config.max_llm_concurrency, verified independent limit,
  runtime-pool capacity)`; a PERSISTENT scope without same-session parallelism stays
  at 1.

Declarations: `plain_cli` / `streaming_json_cli` / `command_code` / `fake` →
`per_request`, independent=4. ACP / Codex → `persistent_capable`, independent=1
(unverified). Grok inherits ACP (1) until a live probe verifies more.

## 9. RuntimePool lifecycle

Execution leases are per physical operation; a PER_WINDOW Material window acquires,
runs, and always releases its physical lease on completion (`_drop_job_session` after
each `classify` call). Logical affinity is retained only across the retry/repair span,
so hundreds of windows cannot starve the pool.

## 10. Mock concurrency benchmark (1 / 2 / 4 workers)

`docs/reports/implementation/mock-classification-concurrency.json` — 10K target turns,
500K context, 18 windows, fake 400–800 ms latency:

| workers | wall (s) | speedup | agent calls | peak active |
|---|---|---|---|---|
| 1 | 9.81 | 1.00× | 18 | 1 |
| 2 | 5.14 | 1.91× | 18 | 2 |
| 4 | 2.99 | 3.28× | 18 | 4 |

Call counts, evidence and checkpoints stay identical across widths.

## 11. Grok Build live synthetic probe

`GROK_CONCURRENCY_LIVE_PROBE = RAN` (synthetic only; no persona data, no uploads).
`docs/reports/implementation/grok-concurrency-probe.json`.

Grok Build 1.0.25 is installed and has `auth.json`, and ACP sessions start with unique
session ids (1 / 2 / 4 distinct ids, no cross-talk). **Every turn returned HTTP 402
"usage balance exhausted"**, so `success = 0` and the probe correctly reports
`probe_status = "unverified"`, `max_verified = 1`. No concurrency was written to the
cache. Enabling 4-way requires a funded account and a re-run.

## 12. Are Grok sessions truly parallel?

Not established. Session isolation was observed (distinct ids, no response bleed), but
with zero successful turns there is no wall-clock evidence of parallel throughput.
Fail-closed: `unverified → 1`.

## 13. Grok Material recommended concurrency

`1` until a funded, successful probe verifies 2- and 4-way speedups. The verified
cache + `material_intelligence` injection will raise it to the verified limit
automatically after a passing probe.

## 14. Context remaining streaming tests

`tests/unit/test_runtime_remaining_freshness.py`:

- persistent + explicit remaining → no double decrement;
- persistent + silent runtime → estimated decrement;
- per-request streaming → no cross-request accumulation;
- per-window → accumulates only inside the window;
- streaming cancel while session alive → estimate kept; after close → dropped;
- model switch / rebind → stale remaining invalidated;
- `execute_text` and `stream_events` produce the same snapshot for the same usage.

## 15. Regression tests

`tests/unit`: 871 passed, 3 skipped. Targeted P0.4 integration:
`test_persisted_context_window_scale.py`, `test_large_conversation_pipeline_v2.py`,
`test_room_bindings_update.py` (28), material integrity / private material (50),
persona creation runtime / resumable (34), room media / attachments / protocol (52) —
all passed. Includes window-isolation: each AnalysisWindow carries a distinct
`_participant_id`.

## 16. Lint / typecheck

- P0.4 files: `ruff check` clean.
- `mypy`: clean for `agent/models.py`, `agent/discovery.py`, `agent/context_fields.py`,
  `runtime_executor.py`, `material_pipeline.py`, `concurrency_cache.py`,
  `classification_dispatch.py`.
- Pre-existing, out-of-scope failures remain in prior-session files
  (`domain/identity.py`, `application/identity_resolver.py`,
  `persona_creation_service.py` mypy 7 errors; identity test E501/F401). Not touched
  here — they are Persona identity work, not this Context Runtime round.

## 17. Known risks

- Grok concurrency remains unverified (account 402). Do not raise above 1 by guess.
- Provider 429 / concurrency rejection is handled by dispatch downgrade, but no live
  429 was observed this round.
- Real-CLI agent-discovery integration tests can hang on this machine (they call live
  `codebuddy` / `qodercli` discovery); unrelated to P0.4, which is why the full
  `tests/` sweep was replaced with scoped integration runs.

## 18. Direct answers

**a. Start 10K real private chat + Qwen benchmark now?**
Yes. The Qwen path is runtime-first with corrected 1M registry, stdin transport, and
the unified remaining accounting; all P0.4 regressions pass. Run it against a private
build, not through any external model.

**b. Start 10K real private chat + Grok Build benchmark now?**
Not yet. The Grok Build account returns HTTP 402, so no turn completes; classification
would fail entirely. Restore balance, re-run the probe, then benchmark.

**c. Grok live concurrency unverified — how many workers?**
Keep `1` (the fail-closed default). Only a passing live probe should raise it.
