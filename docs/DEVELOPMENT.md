# Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy
uv run persona-continuum doctor --json
```

The project targets Python 3.12+ and macOS-first local development while keeping
core logic cross-platform.
