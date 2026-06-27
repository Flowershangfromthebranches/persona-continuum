from __future__ import annotations

import zipfile
from pathlib import Path

from scripts.build_source_package import build_source_package


def test_source_package_excludes_local_caches_and_virtualenv(tmp_path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    output = build_source_package(project_root, tmp_path / "persona-continuum-source.zip")

    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()

    forbidden = [
        "/.venv/",
        "/.mypy_cache/",
        "/.pytest_cache/",
        "/.ruff_cache/",
        "/__pycache__/",
        "/__MACOSX/",
    ]
    for name in names:
        assert not any(token in f"/{name}" for token in forbidden), name
    assert any(name.endswith("pyproject.toml") for name in names)
    assert any(name.endswith("src/persona_continuum/mcp/server.py") for name in names)
