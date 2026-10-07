"""First-run installation of the bundled Taibuge starter personas.

The packages are ``public_compiled`` exports: compiled persona components
only, no memories, sessions, rooms or runtime state.  Installation runs once
per data directory; a marker file records it so a persona the user later
deletes is never silently re-imported.
"""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

import persona_continuum
from persona_continuum.application.persona_service import PersonaService
from persona_continuum.config import Config

TAIBUGE_KIT = "taibuge"
_MARKER_FILE = "starter_kits.json"


def default_kit_dirs(kit: str = TAIBUGE_KIT) -> list[Path]:
    """Wheel installs ship the kit inside the package; a checkout uses ``examples/``."""

    package_root = Path(persona_continuum.__file__).resolve().parent
    return [
        package_root / "starter_kits" / kit,
        package_root.parents[1] / "examples" / kit / "personas",
    ]


class StarterKitService:
    def __init__(
        self,
        config: Config,
        personas: PersonaService,
        kit_dirs: list[Path] | None = None,
    ) -> None:
        self.config = config
        self.personas = personas
        self.kit_dirs = kit_dirs if kit_dirs is not None else default_kit_dirs()

    @property
    def marker_path(self) -> Path:
        return self.config.data_dir / _MARKER_FILE

    def _read_marker(self) -> dict[str, Any]:
        try:
            data = json.loads(self.marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def packages(self) -> list[Path]:
        for directory in self.kit_dirs:
            found = sorted(directory.glob("*.persona.zip"))
            if found:
                return found
        return []

    @staticmethod
    def _package_persona_id(package: Path) -> str:
        with zipfile.ZipFile(package) as archive:
            manifest = yaml.safe_load(archive.read("manifest.yaml"))
        return str(manifest.get("original_persona_id") or manifest["persona"]["id"])

    def ensure_installed(self, kit: str = TAIBUGE_KIT) -> dict[str, Any]:
        """Import the kit once; personas that already exist are left untouched."""

        marker = self._read_marker()
        if kit in marker:
            return {"kit": kit, "status": "already_installed", "installed": [], "skipped": []}
        packages = self.packages()
        if not packages:
            return {"kit": kit, "status": "unavailable", "installed": [], "skipped": []}
        existing = {persona.id for persona in self.personas.list(include_archived=True)}
        installed: list[str] = []
        skipped: list[str] = []
        for package in packages:
            persona_id = self._package_persona_id(package)
            if persona_id in existing:
                skipped.append(persona_id)
                continue
            self.personas.import_persona(package, new_id=persona_id)
            installed.append(persona_id)
        marker[kit] = {
            "installed_at": datetime.now(UTC).isoformat(),
            "installed": installed,
            "skipped": skipped,
        }
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text(
            json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {"kit": kit, "status": "installed", "installed": installed, "skipped": skipped}
