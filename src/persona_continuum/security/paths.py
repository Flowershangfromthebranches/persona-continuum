from __future__ import annotations

import zipfile
from pathlib import Path

from persona_continuum.security.validation import SecurityError


def safe_slug(value: str) -> str:
    allowed = []
    previous_dash = False
    for char in value.lower():
        if char.isalnum():
            allowed.append(char)
            previous_dash = False
        elif not previous_dash:
            allowed.append("-")
            previous_dash = True
    slug = "".join(allowed).strip("-")
    return slug or "persona"


def ensure_child_path(root: Path, target: Path) -> Path:
    root_resolved = root.resolve()
    target_resolved = target.resolve()
    if root_resolved != target_resolved and root_resolved not in target_resolved.parents:
        raise SecurityError("path_traversal")
    return target_resolved


def validate_zip_members(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            member_path = Path(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise SecurityError("zip_path_traversal")
