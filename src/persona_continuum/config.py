from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class Config(BaseModel):
    """Runtime configuration for local storage."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    data_dir: Path = Field(
        default_factory=lambda: Path(
            os.environ.get("PERSONA_CONTINUUM_HOME", "~/.persona-continuum")
        ).expanduser()
    )
    max_source_bytes: int = 10 * 1024 * 1024

    @property
    def database_path(self) -> Path:
        return self.data_dir / "persona_continuum.sqlite"

    @property
    def personas_dir(self) -> Path:
        return self.data_dir / "personas"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def sources_dir(self) -> Path:
        return self.data_dir / "sources"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.personas_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self.sources_dir.mkdir(parents=True, exist_ok=True)
