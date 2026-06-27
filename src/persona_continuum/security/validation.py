from __future__ import annotations

from pathlib import Path


class PersonaContinuumError(Exception):
    code = "persona_continuum_error"


class CodedError(PersonaContinuumError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class NotFoundError(PersonaContinuumError):
    code = "not_found"


class ConflictError(PersonaContinuumError):
    code = "conflict"


class SecurityError(PersonaContinuumError):
    code = "security_error"


def clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def ensure_file_size(path: Path, max_bytes: int) -> None:
    if path.stat().st_size > max_bytes:
        raise SecurityError(f"file_too_large:{path.name}")
