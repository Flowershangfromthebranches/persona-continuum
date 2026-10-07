# Final audit context budget repair — 2026-09-11

## Failure and cause

Job `pcjob_b591e9dd17f343c9` failed before dispatching either final audit to
`gemini_cli / gemini-3.8-flash-high`. The saved diagnostics estimated 93,197
tokens for the evidence audit and 93,240 for consistency, against a 26,672-token
prompt budget. The 32,768-token planning window was an unverified fallback;
it was not a measured Gemini model limit.

The audit builder checked only the transport byte ceiling. Its ledger entries
copied entire supporting-evidence ID lists, including one with 3,509 IDs.
Nested uncertainty fields also bypassed compaction. At the last compaction
level, the builder returned the payload without requiring it to fit.

## Change

- Bound ledger reference samples and expose total/omitted counts. Keep stable
  ledger IDs and source IDs; leave complete local evidence records untouched.
- Continue backfilling evidence referenced by retained claims, including links
  outside the sampled references.
- Compact structured uncertainty while preserving scalar flags and levels.
- Check both fully rendered audit prompts, including their instructions and
  output schema, with the existing model context budget manager. Enforce the
  transport limit independently. Reject an oversized final compaction level
  before opening an audit session.
- Keep both final audits, every required dimension, and explicit quality gates.

## Validation

Read-only replay against the affected task's stored artifacts and evidence:

| Audit | Before (tokens) | After (tokens) | Prompt budget |
| --- | ---: | ---: | ---: |
| Evidence | 93,255 | 22,507 | 26,672 |
| Consistency | 93,298 | 22,550 | 26,672 |

The replay's before values include the new short sampling explanation; the
saved original failure values above use the original envelope. The resulting
payload retained all eight dimensions and 99 ledger entries, including
backfilled references. No private source text is included in this report.

`uv run pytest -q tests/integration/test_incremental_extraction.py
tests/unit/test_audit_issue_contract.py
tests/integration/test_persona_creation_pause_boundaries.py`: **64 passed**.

`uv run ruff check .`: 36 existing findings. The changed service has the same
21 findings, matched by rule, message, and source line, as its pre-edit copy;
the changed test file has none.

`uv run mypy`: seven existing errors. A `--shadow-file` check against the
pre-edit service reproduced all seven. No new typing errors were introduced.
Diff whitespace checks passed for both changed Python files. The complete test
suite was not run.

## Running service

The prior task had no live worker despite its persisted extracting status.
The pause API immediately checkpointed it. The old backend was gracefully
stopped, port release confirmed, and the updated backend started successfully.
The same task resumed with its existing artifacts and runtime selection.

At 22:26 Asia/Shanghai, both final audit calls had been started and the task
reported `MODEL_RUNNING`, with 13 cumulative calls and approximately 64,651
prompt characters (83,703 UTF-8 bytes before the transport envelope). This
confirms the original pre-dispatch budget failure was crossed. Final model
audit/compilation completion was still pending at this checkpoint.
