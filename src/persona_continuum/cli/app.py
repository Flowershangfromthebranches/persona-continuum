from __future__ import annotations

import json
import platform
import sqlite3
import sys
from pathlib import Path
from typing import Annotated

import typer

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.domain.memory import MemoryType
from persona_continuum.domain.persona import PersonaType, RunMode

app = typer.Typer(help="Persona Continuum local persona platform.")
persona_app = typer.Typer(help="Manage personas.")
session_app = typer.Typer(help="Manage sessions.")
memory_app = typer.Typer(help="Manage memories.")
continuation_app = typer.Typer(help="Manage continuation branches.")
app.add_typer(persona_app, name="persona")
app.add_typer(session_app, name="session")
app.add_typer(memory_app, name="memory")
app.add_typer(continuation_app, name="continuation")


def build() -> PersonaContinuum:
    continuum = PersonaContinuum(Config())
    continuum.init()
    return continuum


def _probe_fts5() -> bool:
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE fts5_probe USING fts5(x)")
        conn.close()
        return True
    except sqlite3.OperationalError:
        return False


def _is_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


@app.command()
def init() -> None:
    continuum = build()
    typer.echo(f"Initialized {continuum.config.data_dir}")
    continuum.close()


@app.command()
def upgrade(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    continuum = build()
    personas = continuum.personas.list(include_archived=True)
    for persona in personas:
        continuum.memories.rebuild_index(persona.id)
        continuum.personas.update_manifest(persona.manifest)
    result = {
        "data_dir": str(continuum.config.data_dir),
        "database": str(continuum.config.database_path),
        "persona_count": len(personas),
        "fts_rebuilt": True,
        "schema_version": "1.1",
    }
    continuum.close()
    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    else:
        typer.echo(f"Upgraded {len(personas)} personas in {result['data_dir']}")


@app.command()
def doctor(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    config = Config()
    continuum: PersonaContinuum | None = None
    init_error: str | None = None
    try:
        continuum = build()
    except Exception as exc:
        init_error = f"{exc.__class__.__name__}: {exc}"
    checks = {
        "python": sys.version.split()[0],
        "python_ok": sys.version_info >= (3, 12),
        "platform": platform.system(),
        "data_dir": str(config.data_dir),
        "data_dir_writable": _is_writable_dir(config.data_dir),
        "sqlite_fts5": continuum.database.has_fts5() if continuum else _probe_fts5(),
        "database": str(config.database_path),
        "database_init_ok": init_error is None,
        "database_init_error": init_error,
        "skill_file": str(Path.cwd() / "skills" / "persona-continuum" / "SKILL.md"),
        "skill_file_exists": (Path.cwd() / "skills" / "persona-continuum" / "SKILL.md").exists(),
    }
    if continuum:
        continuum.close()
    if json_output:
        typer.echo(json.dumps(checks, ensure_ascii=False, separators=(",", ":")))
    else:
        for key, value in checks.items():
            typer.echo(f"{key}: {value}")


@persona_app.command("list")
def persona_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    continuum = build()
    data = [persona.manifest.model_dump(mode="json") for persona in continuum.personas.list()]
    continuum.close()
    typer.echo(
        json.dumps(data, ensure_ascii=False)
        if json_output
        else "\n".join(item["id"] for item in data)
    )


@persona_app.command("create")
def persona_create(display_name: str) -> None:
    continuum = build()
    persona = continuum.personas.create(
        display_name=display_name,
        aliases=[],
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode=RunMode.DIGITAL_CONTINUATION,
    )
    continuum.close()
    typer.echo(persona.id)


@persona_app.command("show")
def persona_show(
    persona_id: str, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    continuum = build()
    persona = continuum.personas.get(persona_id)
    continuum.close()
    if json_output:
        typer.echo(json.dumps(persona.manifest.model_dump(mode="json"), ensure_ascii=False))
    else:
        typer.echo(f"{persona.id}: {persona.display_name}")


@session_app.command("list")
def session_list(persona_id: str | None = None) -> None:
    continuum = build()
    sessions = continuum.sessions.list_sessions(persona_id)
    continuum.close()
    typer.echo(
        "\n".join(f"{session.id}\t{session.persona_id}\t{session.status}" for session in sessions)
    )


@memory_app.command("search")
def memory_search(persona_id: str, query: str) -> None:
    continuum = build()
    memories = continuum.memories.search_memories(persona_id, query)
    continuum.close()
    typer.echo("\n".join(f"{memory.id}\t{memory.content}" for memory in memories))


@memory_app.command("add")
def memory_add(persona_id: str, content: str) -> None:
    continuum = build()
    memory = continuum.memories.add_memory(
        persona_id,
        content=content,
        memory_type=MemoryType.SEMANTIC,
        source_kind="user_correction",
        source_confidence=1.0,
    )
    continuum.close()
    typer.echo(memory.id)


@continuation_app.command("list")
def continuation_list(persona_id: str) -> None:
    continuum = build()
    rows = continuum.database.conn.execute(
        "SELECT id FROM continuations WHERE persona_id = ?", (persona_id,)
    ).fetchall()
    continuum.close()
    typer.echo("\n".join(str(row["id"]) for row in rows))


@app.command()
def export(persona_id: str, output: Path | None = None) -> None:
    continuum = build()
    path = continuum.personas.export_persona(persona_id, output)
    continuum.close()
    typer.echo(str(path))


@app.command()
def mcp() -> None:
    from persona_continuum.mcp.server import main as mcp_main

    mcp_main()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
