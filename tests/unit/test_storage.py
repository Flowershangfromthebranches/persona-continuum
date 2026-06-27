from __future__ import annotations

import zipfile

import pytest

from persona_continuum.domain.memory import MemoryType
from persona_continuum.domain.persona import PersonaType, RunMode
from persona_continuum.security.validation import SecurityError


def test_sqlite_migrations_enable_fts5(app) -> None:
    assert app.database.has_fts5()

    persona = app.personas.create(
        display_name="Alex Chen",
        aliases=["Alex"],
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode=RunMode.DIGITAL_CONTINUATION,
    )
    memory = app.memories.add_memory(
        persona.id,
        content="Alex trusts quiet prototypes more than loud presentations.",
        memory_type=MemoryType.SEMANTIC,
        importance=0.8,
        source_kind="system_summary",
    )

    results = app.memories.search_memories(persona.id, "quiet prototypes", limit=3)

    assert results[0].id == memory.id
    assert results[0].access_count == 1


def test_memory_correction_preserves_revision_chain(app) -> None:
    persona = app.personas.create(
        display_name="Alex Chen",
        aliases=[],
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode=RunMode.DIGITAL_CONTINUATION,
    )
    original = app.memories.add_memory(
        persona.id,
        content="Alex dislikes field research.",
        memory_type=MemoryType.SEMANTIC,
        importance=0.6,
        source_kind="historical_inference",
    )

    corrected = app.memories.correct_memory(
        persona.id,
        original.id,
        "Alex dislikes shallow field research, but values careful observation.",
        reason="User correction from source transcript.",
    )

    old = app.memories.get_memory(original.id)
    assert old is not None
    assert old.validity == "superseded"
    assert corrected.supersedes_id == original.id
    assert corrected.user_corrected is True
    assert corrected.source_kind == "user_correction"


def test_zip_import_blocks_path_traversal(app, tmp_path) -> None:
    persona = app.personas.create(
        display_name="Alex Chen",
        aliases=[],
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode=RunMode.DIGITAL_CONTINUATION,
    )
    bad_zip = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad_zip, "w") as archive:
        archive.writestr("../escape.txt", "do not extract")

    with pytest.raises(SecurityError):
        app.personas.add_sources(persona.id, [bad_zip])
