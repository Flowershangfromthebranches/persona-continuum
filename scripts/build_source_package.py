from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

EXCLUDED_NAMES = {
    ".agents",
    ".codex",
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "__MACOSX",
    "dist",
    "build",
}

EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".sqlite", ".db"}

EXCLUDED_PATH_PREFIXES = {
    ("docs", "superpowers"),
}

ALLOWED_TOP_LEVEL_DIRS = {
    "docs",
    "examples",
    "scripts",
    "skills",
    "src",
    "tests",
}

ALLOWED_TOP_LEVEL_FILES = {
    ".gitignore",
    ".python-version",
    "AGENTS.md",
    "LICENSE",
    "README.md",
    "pyproject.toml",
    "uv.lock",
}

FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)


def should_include(path: Path, project_root: Path) -> bool:
    relative = path.relative_to(project_root)
    if any(part in EXCLUDED_NAMES for part in relative.parts):
        return False
    if any(relative.parts[: len(prefix)] == prefix for prefix in EXCLUDED_PATH_PREFIXES):
        return False
    if any("venv" in part.lower() for part in relative.parts):
        return False
    if not relative.parts:
        return False
    top_level = relative.parts[0]
    if len(relative.parts) == 1:
        if top_level not in ALLOWED_TOP_LEVEL_FILES:
            return False
    elif top_level not in ALLOWED_TOP_LEVEL_DIRS:
        return False
    if path.name == ".DS_Store":
        return False
    return path.suffix not in EXCLUDED_SUFFIXES


def build_source_package(project_root: Path, output_path: Path) -> Path:
    project_root = project_root.resolve()
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(project_root.rglob("*")):
            if not path.is_file() or not should_include(path, project_root):
                continue
            if path.resolve() == output_path:
                continue
            relative = path.relative_to(project_root)
            info = zipfile.ZipInfo(str(Path(project_root.name) / relative), FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a reproducible Persona Continuum source zip."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("dist/persona-continuum-source.zip"),
        help="Output zip path.",
    )
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    output = build_source_package(project_root, args.output)
    print(output)


if __name__ == "__main__":
    main()
