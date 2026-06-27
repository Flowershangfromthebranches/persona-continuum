# AGENTS.md

## Repository Instructions

- Keep the project local-first: do not add cloud LLM, embedding, database, or queue dependencies.
- Use `uv run pytest`, `uv run ruff check .`, and `uv run mypy` before claiming completion.
- Keep important explanatory comments concise and only where they clarify state transitions, evidence handling, or privacy/security behavior.
- Preserve the separation between domain models, application services, storage, MCP, CLI, and Skill workflow.
- Never classify simulated continuation data as historical fact.
