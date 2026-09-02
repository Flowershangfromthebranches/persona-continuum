# Core Architecture Upgrade Acceptance

## Result

- Parallel World actor decisions call `AgentAdapter` and produce structured `ActionProposal` records.
- Actor prompts contain branch state and exclude post-world-time memories.
- Resolver checks budget, talent, technology, relationships, and deadlines.
- Branch-scoped world, belief, and relationship memories remain isolated.
- Autonomous Room runs Host, deterministic Discussion Director, Persona Agents, and Host summary.
- Room creation is request-idempotent and exposes persisted/WebSocket progress.
- Provider secrets are AES-256-GCM encrypted and never returned in full.
- OpenAI-compatible and named provider connection tests resolve credentials through CredentialManager.
- Parallel World navigation exposes ordinary controls first and moves causal/replay/injection tools under Developer Tools.

## Automated Evidence

- `uv run pytest -q --tb=short`: 206 passed, 2 warnings.
- `uv run ruff check .`: passed.
- `uv run mypy`: passed, 145 source files.
- `node --check src/persona_continuum/web/static/app.js`: passed.
- `uv run python scripts/preflight_acceptance.py`: `overall_status: COMPLETED`.

The two pytest warnings are an existing Starlette TestClient deprecation and a deliberate duplicate ZIP entry used by a tamper-detection test; neither is a runtime failure.

## Runtime Boundary

Automated tests use an explicitly registered Fake Agent Adapter to prove routing and varying proposal behavior without spending external API credits. Production autonomous decisions require a ready real Agent Adapter. No live paid-provider call was made during this acceptance run.
