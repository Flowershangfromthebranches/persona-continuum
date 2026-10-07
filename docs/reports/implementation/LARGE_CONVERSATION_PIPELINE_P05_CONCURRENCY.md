# Large Conversation / Runtime Concurrency — P0.5 Final Close-out

Scope: the five remaining P0.5 issues only.  No Context-Runtime redesign; the
approved P0.4 contracts (resolver, revisions, unified accounting, workload
scope, episode packing, V4 checkpoint, etc.) are untouched.

## 1. Files changed

New:

- `src/persona_continuum/performance/runtime_capability_store.py` — persistent
  `RuntimeCapabilityStore`, `RuntimeIdentity`, `ProbeStatus`, `CapabilityState`.
- `src/persona_continuum/performance/runtime_identity.py` — binary fingerprint,
  credential identity resolution, env/auth-store fallback.
- `src/persona_continuum/performance/provider_failure.py` — probe failure
  classification + speedup/status derivation.
- `src/persona_continuum/application/provider_capacity.py` — capacity-error
  classifier (concurrency vs rate vs quota vs payment).
- `tests/unit/test_runtime_capability_store.py`, `tests/unit/test_runtime_identity.py`,
  `tests/unit/test_adaptive_concurrency_retry.py`, `tests/unit/test_probe_report_validity.py`,
  `tests/integration/test_persistent_concurrency_capability.py`.

Changed:

- `performance/concurrency_cache.py` — identity-bound cache over the persistent
  store; `configure_default_runtime_capability_store`.
- `performance/runtime_pool.py` — credential-aware `make_key`.
- `application/classification_dispatch.py` — adaptive retry / downgrade / requeue.
- `application/material_pipeline.py` — capability + retry telemetry fields.
- `application/material_intelligence.py` — persistent capability resolution,
  runtime-downgrade write-back, metrics/telemetry plumbing.
- `application/container.py` — binds the capability store to the app data dir.
- `application/persona_creation_service.py` — builds the runtime identity and
  passes it to Material analysis.
- `agent/adapters/grok.py` — `runtime_origin = "xai"`; `agent/adapters/codex.py`
  passes credential identity to the pool key.
- `auth/credentials.py` — `CredentialManager.credential_identity_hash`.
- `config.py` — `material_concurrency_retry_limit`,
  `runtime_concurrency_capability_ttl_seconds`.
- `storage/migrations.py` — `runtime_concurrency_capabilities` table.
- `scripts/grok_independent_session_probe.py` — full rewrite (identity, statuses,
  validity, persistent write, CLI args).
- `scripts/mock_classification_concurrency_benchmark.py` — adaptive case.
- `tests/unit/test_workload_scope_concurrency.py`,
  `tests/unit/test_performance_runtime_pool.py` — updated for the new semantics.

## 2. Persistent RuntimeCapabilityStore design

`RuntimeCapabilityStore` writes to the project's SQLite database
(`Config().database_path`, i.e. `<data_dir>/persona_continuum.sqlite`) from the
probe process and is read by the App on a later start — no shared Python process
required.  Bare `IndependentSessionConcurrencyCache()` stays in-memory for unit
tests; the app-wide `default_concurrency_cache()` is persistent and is rebound to
the running app's data directory by `PersonaContinuum.__init__`.

State on read is derived: `verified` / `partial` / `stale` (TTL) / `invalidated`
(corruption) / `unverified` / `missing`; anything other than verified/partial
yields one worker.

## 3. Schema / persistence location

Table `runtime_concurrency_capabilities` in
`<data_dir>/persona_continuum.sqlite`:

```
capability_key (PK)              adapter_id            binary_identity
binary_version                   model_id              credential_identity_hash
runtime_origin                   probe_version         max_verified_independent_sessions
parallel_independent_sessions_verified                 parallel_same_session_verified
current_recommended              probe_status          probe_sample_count
verified_at                      expires_at            last_runtime_downgrade_at
downgrade_reason                 downgrade_expires_at  last_failure_kind
last_failure_at                  metadata_json         updated_at
```

`capability_key = sha256(adapter_id, binary_identity, binary_version, model_id,
credential_identity_hash, runtime_origin, probe_version)[:32]`.

## 4. Capability identity

`RuntimeIdentity` binds: adapter id; CLI binary identity (sha256 of resolved
absolute path + version — not the whole binary); binary version; model id;
credential identity hash; runtime origin (e.g. `xai`); probe contract version.
Changing any one produces a different row and the old verification is not
reused.

Credential identity resolution order: `CredentialManager.credential_identity_hash`
→ adapter `credential_identity_hash()` hook → vendor local auth-store content
hash → credential-env fingerprint → empty.  Only the hash is ever stored.

## 5. TTL / invalidation

- Default TTL 24h (`runtime_concurrency_capability_ttl_seconds`, configurable).
  Past `expires_at` the state is `stale` and the limit falls to 1.
- `record_runtime_downgrade` lowers only `current_recommended`; `max_verified`
  survives, and recovery is automatic when `downgrade_expires_at` passes.
- `invalidate` (session collision / response cross-talk / frame correlation
  failure) clears `parallel_independent_sessions_verified` and forces 1.

## 6. RuntimePool credential identity

`AgentRuntimePool.make_key(adapter_id, command, env, *, credential_identity=,
binary_identity=, runtime_origin=)`.  `credential_identity` (from
`CredentialManager`) is the primary discriminator; the legacy env fingerprint is
now a fallback covering `XAI_/GROK_/OPENAI_/ANTHROPIC_/GEMINI_/GOOGLE_/CODEX_/
CLAUDE_/ACP_/DEEPSEEK_/OPENROUTER_/ALIBABA_/DASHSCOPE_/AZURE_/MOONSHOT_/MINIMAX_/
ZHIPU_` credential-like names.  Raw secrets never enter the key.
Codex passes `resolve_credential_identity(self, credential_id=...)`.

## 7. Adaptive concurrency retry state machine

`ClassificationDispatch` worker: run window → on failure classify →
if retriable concurrency and `attempts < retry_limit` and `max > 1`:
halve concurrency (N → N/2 → 1), bounded backoff (+jitter), requeue the **same**
window (queue unfinished-counter balanced), other workers keep running.
Otherwise the failure becomes the task error.  Completed windows are never
rerun; their checkpoints are untouched.

Config: `material_concurrency_retry_limit` (default 2).  Backoff/sleep are
injectable for tests.

## 8. Error classification

| signal | kind | downgrade+retry |
|---|---|---|
| 402 / payment required / balance exhausted | `payment_required` | no |
| quota / usage balance / requests-per-day / daily limit | `quota_exhausted` | no |
| too many concurrent sessions / session capacity | `too_many_sessions` | yes |
| 429 + concurrent/parallel/simultaneous | `concurrency_limit` | yes |
| parallel/concurrency wording without 429 | `provider_parallelism_rejected` | yes |
| runtime pool / lease / affinity unavailable | `runtime_pool_capacity` | yes |
| 429 plain rate limit | `rate_limit_concurrency` | yes |

Payment/quota are evaluated before concurrency so a 429 that is really an
exhausted quota is never downgraded.

## 9. Probe speedup validity rules

`speedup = serial_wall * width / wave_wall` **only** when
`correlation_ok` (success == width, unique sessions == width, every response
matched its own label, no transport exceptions).  Otherwise `speedup = null`,
`speedup_valid = false`.  `max_verified` rises only for fully successful widths.
Statuses: `verified / partial / unverified / failed_environment / failed_auth /
failed_payment / failed_provider_quota / failed_correlation / failed_runtime`.
A payment/auth baseline failure aborts after the first wave (no repeated
bombing).

## 10. Mock 1 / 2 / 4 benchmark

`docs/reports/implementation/mock-classification-concurrency.json` — 10K target
turns, 500K context, 18 windows, 400–800 ms fake latency:

| workers | wall (s) | speedup | calls | peak active |
|---|---|---|---|---|
| 1 | 9.81 | 1.00× | 18 | 1 |
| 2 | 5.14 | 1.91× | 18 | 2 |
| 4 | 2.99 | 3.28× | 18 | 4 |

`calls_consistent = true`.

## 11. Adaptive 4 → 2 → 1 benchmark

Same file, `adaptive_downgrade`: 18 windows, one window rejected once with
`429 too many concurrent sessions` → downgrade **4 → 2**, requeue, resume.

`successful_windows = 18`, `window_attempts = 19`, `retried_windows = ["w07"]`,
`window_retries = 1`, `concurrency_downgrades = 1`, `peak_active = 4`,
`failed_windows = 0`.

## 12. Checkpoint duplicate check

`checkpoint_duplicates = 0` in the adaptive run: the retried window committed
exactly once; every other window ran exactly once.  Unit tests assert the same
(`test_completed_windows_are_not_rerun`, `test_concurrency_reject_downgrades_and_requeues`).

## 13. Persistent cache cross-process tests

`tests/unit/test_runtime_capability_store.py` (write → new store instance reads
4; TTL expiry → 1; CLI version / model / credential change → no hit; same
identity → hit; no raw token; runtime downgrade 4→2; corruption invalidates) and
`tests/integration/test_persistent_concurrency_capability.py`
(`test_probe_written_by_one_process_is_read_by_a_new_app_process` uses the app's
real data-dir DB and the app-configured store).

## 14. Grok live synthetic probe result

`GROK_CONCURRENCY_LIVE_PROBE = RAN` (synthetic prompts only; no persona data).
`docs/reports/implementation/grok-concurrency-probe.json`.

Grok Build 1.0.25, origin `xai`, ACP session starts.  The **serial baseline**
returned `HTTP 402 Payment Required: Grok Build usage balance exhausted`, so the
probe aborted after one request and reported:

- `probe_status = failed_payment`
- `max_verified = 1`, capability state `unverified`, source `fallback`
- `speedup_valid = false`, `two_speedup = null`, `four_speedup = null`

No invalid 2.26× / 3.33× numbers are produced, and no verified capability is
written.

## 15. New tests

- `tests/unit/test_runtime_capability_store.py` (9)
- `tests/unit/test_runtime_identity.py` (6)
- `tests/unit/test_adaptive_concurrency_retry.py` (11)
- `tests/unit/test_probe_report_validity.py` (6)
- `tests/unit/test_performance_runtime_pool.py::test_hundred_material_windows_do_not_starve_pool`
- `tests/integration/test_persistent_concurrency_capability.py` (2)
- updated `tests/unit/test_workload_scope_concurrency.py` for the downgrade ladder.

Full unit suite: 905 passed, 3 skipped.  Targeted integration (context scale,
pipeline v2, material integrity, private material, persistent capability) 73
passed; persona creation runtime + resumable 34 passed.

## 16. Lint / typecheck

- `ruff`: clean on every P0.5 file.  The only remaining repo error is a
  pre-existing `E501` in `scripts/material_p02_benchmark.py`.
- `mypy`: 7 pre-existing errors remain, all in
  `application/persona_creation_service.py` (Persona identity work from an
  earlier session).  The three errors introduced this round were fixed; 233
  source files checked.

## 17. Known risks

- Grok concurrency stays unverified while the account is out of balance; keep 1.
- The provider-capacity classifier is text-based; a provider that reports
  congestion with an unrecognised phrase degrades to `other` (no downgrade) and
  fails fast rather than silently retrying forever.
- A vendor CLI that rewrites its local auth store on token refresh changes the
  credential identity hash and invalidates the capability (fail-closed, by
  design).
- The persisted row is only as good as the probe's identity inputs; if a CLI
  reports no version, the binary identity still binds path+version string, and a
  genuinely version-less CLI would over-match.  Grok reports a version.

## 18. Direct answers

**A. Can the 10K Qwen real-chat benchmark start now?**
Yes.  The Qwen path is unchanged and its P0.4/P0.5 regressions pass; the new
capability store does not alter Qwen (a `per_request` adapter) behaviour.

**B. After Grok's balance is restored, is re-running the synthetic probe enough
to decide 1/2/4 workers?**
Yes.  `scripts/grok_independent_session_probe.py --force --model grok-4.6`
writes the verified width into the persistent store; production then uses
`min(config.max_llm_concurrency, current_recommended, pool capacity)`.

**C. After a 4-way verification, does an App restart still see 4 workers?**
Yes.  The capability is persisted in the app's SQLite DB and the App rebinds its
store to that same data directory at startup; a new process reads the same row.

**D. Under temporary production concurrency pressure, does it auto 4→2→1 and
retry only the failed window?**
Yes.  Retriable concurrency failures halve the width, back off, and requeue the
same window; completed windows and checkpoints are untouched.  Quota/payment
failures are not downgraded.

**E. After a credential / model / CLI-version change, is the old 4-way
capability guaranteed not to be reused?**
Yes.  All of those feed the identity key (including an irreversible credential
hash and the CLI binary+version), so the old row cannot match; the runtime falls
back to 1 until a fresh probe verifies the new identity.
