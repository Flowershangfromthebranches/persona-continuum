from __future__ import annotations

import zipfile
from pathlib import Path

from persona_continuum.application.starter_kit_service import StarterKitService

TAIBU_IDS = {"玄衡先生-主持", "子平先生", "紫薇先生", "易卦先生", "三式先生", "西学占测师"}


def _count(app, table: str, persona_id: str) -> int:
    row = app.database.conn.execute(
        f"SELECT COUNT(*) AS n FROM {table} WHERE persona_id = ?", (persona_id,)
    ).fetchone()
    return int(row["n"])


def test_bundled_packages_carry_only_compiled_persona_content(app) -> None:
    found = app.starter_kits.packages()
    assert len(found) == 6
    for package in found:
        with zipfile.ZipFile(package) as archive:
            data_files = {name for name in archive.namelist() if name.startswith("data/")}
        assert data_files == {"data/compiled_components.jsonl"}, package.name


def test_first_run_installs_taibu_personas_without_memories(app) -> None:
    report = app.starter_kits.ensure_installed()

    assert report["status"] == "installed"
    assert set(report["installed"]) == TAIBU_IDS
    installed = {persona.id for persona in app.personas.list()}
    assert installed >= TAIBU_IDS
    for persona_id in TAIBU_IDS:
        assert _count(app, "memories", persona_id) == 0
        assert _count(app, "sessions", persona_id) == 0


def test_starter_kit_installs_once_and_respects_deletion(app) -> None:
    app.starter_kits.ensure_installed()
    app.personas.delete("子平先生")

    again = app.starter_kits.ensure_installed()

    assert again["status"] == "already_installed"
    assert "子平先生" not in {persona.id for persona in app.personas.list()}


def test_existing_persona_with_same_id_is_never_overwritten(app) -> None:
    app.personas.create_from_manifest(
        {"id": "子平先生", "display_name": "我自己的子平先生", "persona_type": "fictional"}
    )

    report = app.starter_kits.ensure_installed()

    assert "子平先生" in report["skipped"]
    assert app.personas.get("子平先生").manifest.display_name == "我自己的子平先生"


def test_missing_kit_is_reported_without_writing_marker(app, tmp_path: Path) -> None:
    service = StarterKitService(app.config, app.personas, kit_dirs=[tmp_path / "none"])

    report = service.ensure_installed()

    assert report["status"] == "unavailable"
    assert not service.marker_path.exists()
