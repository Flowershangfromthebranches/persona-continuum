from __future__ import annotations

from typing import Any

from persona_continuum.security.validation import CodedError


class MaterialTooLargeError(CodedError):
    """Raised when a private-material upload exceeds the configured byte limit."""

    def __init__(self, size: int, limit: int) -> None:
        self.size = size
        self.limit = limit
        super().__init__(
            "private_material_too_large",
            f"private_material_too_large:{size}:{limit}",
        )


class UnrecognizedJsonStructureError(CodedError):
    """Large JSON that is not a known message/record array must fail closed."""

    def __init__(self, message: str = "unrecognized_json_structure") -> None:
        super().__init__("unrecognized_json_structure", message)


class MaterialParseError(CodedError):
    """A single record could not be parsed; the locator is part of the error."""

    def __init__(self, message: str, *, locator: dict[str, Any] | None = None) -> None:
        self.locator = dict(locator or {})
        super().__init__("material_parse_error", message)
