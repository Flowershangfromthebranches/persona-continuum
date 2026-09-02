# ACP Transport and Persona Job Center Validation

Validation was approved and executed on 2026-08-26 after the implementation
hard stop.

## Automated results

- Focused ACP and Job Center regression file: `19 passed`.
- Related ACP timeout, response-contract, Grok fallback, Task Center UI,
  observability, Profile Enrichment, and Persona Enrichment tests: `53 passed`
  before static cleanup.
- Post-cleanup focused regression set: `57 passed`.
- Complete repository test suite after all fixes: `440 passed, 2 skipped,
  1 warning` in 392.50 seconds.
- Ruff: all checks passed.
- mypy: no issues in 146 source files.
- `git diff --check`: passed for the implementation scope.

The one pytest warning is produced by the intentional checksum-tampering
regression fixture, which writes a duplicate `data/sources.jsonl` ZIP member
before asserting that import rejects the archive and rolls back.

## Static-gate fixes made during validation

Validation found only formatting and typing failures in the new code. The
minimal cleanup:

- normalized ACP imports and builtin timeout spelling;
- wrapped long SQL/diagnostic/test lines without changing query semantics;
- retained safe `sqlite3.Row.keys()` compatibility through a local key set;
- narrowed optional ACP observed-frame values before integer conversion;
- separated Persona Creation and Profile Enrichment loop variable types.

The complete repository test suite was rerun after these changes.

## Remaining live acceptance boundary

No real external ACP Agent, real Grok/Cursor/OpenCode CLI, credentialed Persona
Enrichment run, or physical browser session was invoked. Those checks depend on
the locally installed Agent/runtime state and should be treated as a separate
live acceptance gate, not inferred from the automated results above. Pyright
was not run because this checkout has no configured or installed pyright
executable; Ruff and mypy are the configured static gates in `pyproject.toml`.
