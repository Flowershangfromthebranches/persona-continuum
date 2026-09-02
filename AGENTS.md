# AGENTS.md

## Repository Instructions

- Keep the project local-first: do not add cloud LLM, embedding, database, or queue dependencies.
- Preserve decoupling: Persona (identity) != Agent Host (adapter) != Model != Reasoning Effort != Room Session.
- Enforce the Recall Gate order: `recall_started` < `recall_completed` < `agent_started` before model turn generation.
- Ensure all API keys and secrets are redacted from transcripts, room states, logs, and frontend payloads.
- Use `uv run pytest`, `uv run ruff check .`, and `uv run mypy` before claiming completion.
- Keep important explanatory comments concise and only where they clarify state transitions, evidence handling, or privacy/security behavior.
- Preserve the separation between domain models, application services, storage, MCP, CLI, Room Orchestration, Web, and Skill workflow.
- Never classify simulated continuation data as historical fact.
