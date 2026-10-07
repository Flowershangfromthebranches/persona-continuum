# Runtime Concurrency / Large Conversation Pipeline — P0.6 Close-out

Scope: exactly the three confirmed issues — (A) duplicate downgrade on a
simultaneous rejection wave, (B) fail-closed generic HTTP 429, (C) per-width
probe speedup validity. No Context Pipeline refactor, no Conversation/Evidence/
V4 change, no schema migration, no checkpoint invalidation.

## 1. Files changed

- `src/persona_continuum/application/provider_capacity.py` — rewritten
  classification table (CONCURRENCY / TRANSIENT_RATE / QUOTA / PAYMENT / AUTH /
  UNKNOWN_429 / UNKNOWN_PROVIDER), structured-code precedence.
- `src/persona_continuum/application/classification_dispatch.py` — concurrency
  generation/epoch, state lock, stale-wave replay, retry-budget split, terminal
  error codes.
- `src/persona_continuum/application/material_pipeline.py` — generation /
  stale / terminal telemetry fields.
- `src/persona_continuum/application/material_intelligence.py` — expose the new
  counters in the task-center `chat_pipeline` telemetry.
- `scripts/grok_independent_session_probe.py` — per-width speedup validity.
- `scripts/mock_classification_concurrency_benchmark.py` — scenarios A–D.
- `tests/unit/test_concurrency_generation_wave.py` (new)
- `tests/unit/test_adaptive_concurrency_retry.py` (classification table + updated)
- `tests/unit/test_probe_report_validity.py` (partial-probe test)
- `tests/unit/test_workload_scope_concurrency.py` (updated to explicit
  concurrency message)
- `docs/reports/implementation/mock-classification-concurrency.json` (rerun)

## 2. ConcurrencyGeneration design

Each dispatcher owns `generation` (starts 0) and `_max` (effective limit). A
worker captures `(attempt_generation, attempt_limit)` inside the admission gate,
so every attempt knows which concurrency state it ran under. All
generation/limit mutations happen under one `asyncio.Lock`
(`_concurrency_state_lock`): compare, decide, half the limit, bump the
generation, record the event, and fire the persistent-downgrade hook atomically.

Only a rejection whose `attempt_generation == current_generation` may lower the
width: `4 → 2 → 1` (halving, never 4→3→2→1). A rejection from an older
generation is *stale*.

## 3. Simultaneous rejection wave flow

Four workers in flight at width 4 all return `too many concurrent sessions`:

1. W1 (first to take the state lock, `gen==0==current`) → `4→2`, `gen=1`,
   requeued; this is the only adaptive retry.
2. W2/W3/W4 (`gen=0 != current 1`) → **stale**: requeued at `gen=1 / limit=2`,
   never downgraded, never terminal.
3. All four replay at width 2 and succeed. 12/12 complete, `checkpoint_duplicates=0`.

## 4. Stale rejection definition

A window whose attempt began under a generation that the current generation has
already superseded. It is proof only that the *old* width was too high — which
has already been handled — so it is replayed, counted in
`stale_rejection_count` / `stale_replayed_windows`, and bounded by
`stale_replay_limit` to rule out an infinite loop.

## 5. Retry budget semantics

Two independent budgets:

- `adaptive_retries` (window) — grows only when the window truly fails under the
  **current** generation; capped by `material_concurrency_retry_limit` (default 2).
- `stale_replays` (window) — grows only for stale replays; capped by
  `stale_replay_limit` (default 4). It never consumes the adaptive budget, so a
  window cannot be failed because another window triggered the downgrade.

`window_retries` counts adaptive retries; `window_retries`/`stale_replayed_windows`
are separate telemetry.

## 6. ProviderFailure classification table

| input | kind | downgrade+retry |
|---|---|---|
| structured concurrency code (e.g. `rate_limit_concurrency`) | `concurrency_limit` | yes |
| `too many concurrent sessions` | `too_many_sessions` | yes |
| `concurrency limit exceeded` / `parallel request limit` / `max concurrent` | `concurrency_limit` | yes |
| runtime pool / lease unavailable | `runtime_pool_capacity` | yes |
| `too many requests` / `requests per minute` / `rate limit exceeded` / RPM/RPS | `transient_rate_limit` | retry only, **no** downgrade |
| `quota` / `usage limit` / `daily\|monthly limit` / `requests per day` | `quota_exhausted` | no |
| `402` / `payment required` / `balance exhausted` | `payment_required` | no |
| `401/403` / unauthorized / invalid api key | `auth_failure` | no |
| `429` with no identifiable semantics | `unknown_429` | no |
| anything else | `unknown_provider_failure` | no |

Machine-readable codes are checked before text. Order:
payment → auth → quota → explicit concurrency → explicit transient rate →
unknown 429 → unknown provider failure.

## 7. Generic 429 behaviour

`HTTP 429`, `HTTP 429 unknown provider capacity error`, `HTTP 429 too many
requests`:
- bare/unknown → `unknown_429`, `retriable_concurrency=False`: no downgrade, no
  capability change, fail closed.
- `too many requests` / RPM / RPS → `transient_rate_limit`: bounded replay with
  backoff, still never a capability downgrade.

`429 quota exhausted` is evaluated as quota before any 429 handling and never
downgrades.

## 8. Persistent capability downgrade — when it happens

`record_runtime_downgrade` is invoked only from `_downgrade_locked`, i.e. only
for an explicit concurrency rejection that is the **first** of its generation.

It does **not** happen for: stale rejections, `transient_rate_limit`,
`unknown_429`, `quota_exhausted`, `payment_required`, `auth_failure`. The
verified capability schema is untouched (no migration).

## 9. Partial probe speedup rule

Speedup validity is per width. For each width the payload carries
`success_count`, `correlation_ok`, `speedup_valid`, `speedup`. A failed 4-way
wave sets `four_speedup=null`/`four_speedup_valid=false` but leaves a valid
2-way result intact; top-level adds `overall_probe_complete=false`.

Example: serial ok, 2-way 2/2 ok, 4-way 3/4 →
`probe_status=partial`, `max_verified=2`, `two_speedup_valid=true`,
`two_speedup=1.87`, `four_speedup_valid=false`, `four_speedup=null`. Production
may use verified 2.

## 10. Wave benchmark — 4 → 2

`scenario_a_simultaneous_wave` (18 windows, 4 workers, true in-flight latch):

`successful_windows=18`, `window_attempts=22`, `window_retries=1`,
`stale_replayed_windows=3`, `stale_rejection_count=3`,
`concurrency_downgrades=1`, downgrade event `4→2 generation=1`,
`persistent_downgrades=[["too_many_sessions", 2]]` (exactly one),
`checkpoint_duplicates=0`, `failed_windows=0`.

## 11. Two-level benchmark — 4 → 2 → 1

`scenario_b_two_level_wave` (4 windows):

`successful_windows=4`, `concurrency_downgrades=2`, events
`(4→2, gen1)` then `(2→1, gen2)`, `final_effective_concurrency=1`,
`checkpoint_duplicates=0`, no terminal error.

## 12. Serial final failure test

`scenario_c_serial_failure`: after `4→2→1`, a window still rejected at width 1
fails with `PROVIDER_CONCURRENCY_LIMIT_AT_SERIAL_EXECUTION` (no infinite
retry); `terminal_concurrency_failures=1`.

## 13. Unknown-429 regression

`scenario_d_generic_429`: `HTTP 429` → `provider_failure_kind=unknown_429`,
`concurrency_downgrades=0`, `persistent_downgrades=[]`,
`final_effective_concurrency=4` (unchanged). Unit tests also cover the full
classification table and the structured-code precedence case.

## 14. Partial probe regression

`tests/unit/test_probe_report_validity.py::test_partial_probe_keeps_two_way_speedup`
drives the real probe script end to end with a fake adapter that fails only the
4-way wave: status `partial`, `max_verified=2`, `two_speedup` retained,
`four_speedup=null`.

Live Grok probe: not re-run this round (the account is still out of balance at
402; re-probing would only repeat `failed_payment`). The stored
`grok-concurrency-probe.json` reports `failed_payment`, `speedup_valid=false`,
`two_speedup=null`, `four_speedup=null`, `max_verified=1`.

## 15. Checkpoint duplicate count

`0` in every scenario (A, B, C, D) and in every wave unit test; the retried
window commits exactly once.

## 16. Test results

- New/updated unit: `test_concurrency_generation_wave.py` (7),
  `test_adaptive_concurrency_retry.py` (classification table + state machine),
  `test_probe_report_validity.py`, `test_workload_scope_concurrency.py`.
- Full unit suite: **924 passed, 3 skipped**.
- Targeted integration (context scale / V4 checkpoint, persistent capability,
  material integrity, private material, pipeline v2, local material performance):
  **86 passed**.
- Keyword subset (concurrency/capability/dispatch/material/checkpoint/v4/probe/
  identity): **191 passed, 3 skipped**.
- Wave tests repeated 5× — stable, no flakes.
- `ijson`/`mcp` were available, so no environment skips were needed for this run.

## 17. Lint / typecheck

- `ruff`: clean on all changed files.
- `mypy`: the only remaining errors are the 7 pre-existing
  `persona_creation_service.py` identity-session errors; the changed modules are
  clean (233 files checked).

## 18. Known risks

- A rejection wave is only collapsed when windows are genuinely concurrent; if
  the provider rejects serially one window at a time, each is a real new-generation
  failure and the ladder still walks (correctly).
- `transient_rate_limit` windows retry up to `material_concurrency_retry_limit`
  then fail terminally; a prolonged RPM limit still fails the task (unchanged
  provider budget), it simply never corrupts the verified capability.
- `unknown_429` is intentionally terminal; it cannot be recovered by lowering
  width, so the task fails fast with an explicit kind.
- Structured concurrency detection depends on providers emitting a stable code;
  text heuristics cover the known phrasings.

## 19. Direct answers

**A. Four windows rejected together at width 4 — only one 4→2?**
Yes. Only the first rejection of the current generation downgrades; the other
three are stale and replayed. Scenario A shows `concurrency_downgrades=1` and
one persistent downgrade.

**B. Are the other old-generation failures all requeued rather than terminating
the task?**
Yes. Stale rejections are requeued at the new width and never set the terminal
error; scenario A completes 18/18 with `failed_windows=0`.

**C. Does 2→1 happen only after a genuine failure at width 2?**
Yes. A window must start under `generation=1 / limit=2` and be rejected there;
scenario B shows events `(4→2, gen1)` then `(2→1, gen2)` only.

**D. Does generic/unknown HTTP 429 ever lower the persisted independent-session
capability?**
No. `unknown_429` and `transient_rate_limit` never downgrade and never call the
persistent write-back; scenario D shows `concurrency_downgrades=0` and an empty
persistent-downgrade list with the width unchanged at 4.

**E. 2-way probe success + 4-way failure — can the App use verified 2?**
Yes. `probe_status=partial` with `max_verified=2` is persisted, and the
persistent store returns limit 2 while `two_speedup` is preserved.

**F. Is the Large Conversation / Runtime Concurrency P0 work now complete?**
Yes. Both confirmed production defects and the probe-report inconsistency are
fixed, with generation-scoped downgrade, fail-closed classification, and
per-width probe validity all covered by regression tests; the remaining mypy
errors are unrelated prior-session Persona identity work.
