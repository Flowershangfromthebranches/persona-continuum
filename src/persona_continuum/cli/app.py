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
agents_app = typer.Typer(help="Discover and inspect agent cognitive hosts.")
room_app = typer.Typer(help="Manage multi-agent persona rooms.")
app.add_typer(persona_app, name="persona")
app.add_typer(session_app, name="session")
app.add_typer(memory_app, name="memory")
app.add_typer(continuation_app, name="continuation")
app.add_typer(agents_app, name="agents")
app.add_typer(room_app, name="room")


def build() -> PersonaContinuum:
    continuum = PersonaContinuum(Config())
    continuum.init()
    return continuum


def _websocket_backend_available() -> bool:
    from persona_continuum.web.server import websocket_backend_available

    return websocket_backend_available()


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
        "websocket_backend": _websocket_backend_available(),
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
def export(
    persona_id: str,
    output: Path | None = None,
    mode: Annotated[
        str,
        typer.Option(
            "--mode",
            help="full, identity_only, redacted, or public_compiled",
        ),
    ] = "full",
) -> None:
    continuum = build()
    path = continuum.personas.export_persona(persona_id, output, mode=mode)
    continuum.close()
    typer.echo(str(path))


@app.command("import")
def import_package(package: Path) -> None:
    """Import a validated Persona package into the local repository."""

    continuum = build()
    try:
        persona = continuum.personas.import_persona(package)
        typer.echo(persona.id)
    finally:
        continuum.close()


@app.command("web")
def web_command(
    port: Annotated[int, typer.Option("--port", "-p", help="Port to listen on")] = 8000,
    host: Annotated[str, typer.Option("--host", "-h", help="Host to bind to")] = "127.0.0.1",
) -> None:
    """Start Persona Continuum local Web server."""
    from persona_continuum.web.server import run_web_server

    continuum = build()
    typer.echo(f"Starting Persona Continuum Web UI at http://{host}:{port}")
    try:
        run_web_server(continuum, host=host, port=port)
    finally:
        continuum.close()


@agents_app.command("scan")
def agents_scan(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Scan and list detected local agent hosts and capabilities."""
    import asyncio

    continuum = build()
    try:
        probes = asyncio.run(continuum.agent_discovery.scan(force_refresh=True))
        if json_output:
            typer.echo(
                json.dumps(
                    [p.model_dump(mode="json") for p in probes], ensure_ascii=False, indent=2
                )
            )
        else:
            for p in probes:
                status_icon = (
                    "✓"
                    if p.status.value == "ready"
                    else ("⚠" if p.status.value in {"auth_required", "detected"} else "✗")
                )
                typer.echo(f"{status_icon} [{p.status.value.upper()}] {p.name} ({p.id})")
                if p.binary_path:
                    typer.echo(f"   Binary: {p.binary_path}")
                if p.version:
                    typer.echo(f"   Version: {p.version}")
                if p.models:
                    model_names = ", ".join(m.id for m in p.models[:4])
                    typer.echo(f"   Models ({len(p.models)}): {model_names}")
                if p.status_detail:
                    typer.echo(f"   Detail: {p.status_detail}")
    finally:
        continuum.close()


@agents_app.command("list")
def agents_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List registered agent adapters."""
    import asyncio

    continuum = build()
    try:
        probes = asyncio.run(continuum.agent_discovery.scan(force_refresh=False))
        if json_output:
            typer.echo(json.dumps([p.model_dump(mode="json") for p in probes], ensure_ascii=False))
        else:
            for p in probes:
                typer.echo(f"{p.id}\t{p.status.value}\t{p.name}")
    finally:
        continuum.close()


@agents_app.command("inspect")
def agents_inspect(
    agent_id: str, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    """Inspect detailed capabilities and model list of an agent."""
    import asyncio

    continuum = build()
    try:
        probe = asyncio.run(continuum.agent_discovery.probe_adapter(agent_id))
        if not probe:
            typer.echo(f"Agent '{agent_id}' not found.", err=True)
            raise typer.Exit(code=1)
        if json_output:
            typer.echo(json.dumps(probe.model_dump(mode="json"), ensure_ascii=False, indent=2))
        else:
            typer.echo(f"Agent: {probe.name} ({probe.id})")
            typer.echo(f"Status: {probe.status.value}")
            typer.echo(f"Binary: {probe.binary_path or 'none'}")
            typer.echo(f"Protocols: {', '.join(probe.protocols)}")
            typer.echo("Models:")
            for m in probe.models:
                efforts = ", ".join(m.supported_reasoning_efforts)
                typer.echo(f"  - {m.id} ({m.display_name}) [Efforts: {efforts}]")
    finally:
        continuum.close()


@room_app.command("list")
def room_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List multi-agent persona rooms."""
    continuum = build()
    try:
        rooms = continuum.orchestrator.list_rooms()
        if json_output:
            typer.echo(json.dumps([r.model_dump(mode="json") for r in rooms], ensure_ascii=False))
        else:
            for r in rooms:
                p_str = ", ".join(p.persona_id for p in r.participants)
                title_str = r.title or ""
                typer.echo(
                    f"{r.id}\t{r.status.value}\tTurns: {r.turn_index}\t[{p_str}]\t{title_str}"
                )
    finally:
        continuum.close()


@room_app.command("inspect")
def room_inspect(
    room_id: str, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    """Inspect room details, participant bindings, and transcripts."""
    continuum = build()
    try:
        room = continuum.orchestrator.get_room(room_id)
        if not room:
            typer.echo(f"Room '{room_id}' not found.", err=True)
            raise typer.Exit(code=1)
        if json_output:
            typer.echo(json.dumps(room.model_dump(mode="json"), ensure_ascii=False, indent=2))
        else:
            typer.echo(f"Room ID: {room.id}")
            typer.echo(f"Title: {room.title}")
            typer.echo(f"Status: {room.status.value}")
            typer.echo(f"Topic: {room.topic or 'None'}")
            typer.echo("Participant Bindings:")
            for _p_id, snap in room.binding_snapshots.items():
                typer.echo(
                    f"  - {snap.display_name} ({snap.persona_id}) -> "
                    f"{snap.agent_runtime_name} | {snap.model_id} (effort: {snap.reasoning_effort})"
                )
            typer.echo(f"Transcripts ({len(room.transcript)} turns):")
            for t in room.transcript:
                typer.echo(f"  [{t.get('speaker_name')}]: {t.get('content', '')[:80]}...")
    finally:
        continuum.close()


@app.command()
def web(
    host: Annotated[str, typer.Option("--host", help="Host IP to bind web server")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port number")] = 8000,
    log_level: Annotated[str, typer.Option("--log-level", help="Log level")] = "info",
) -> None:
    """Start the Persona Continuum Web UI."""
    from persona_continuum.web.server import run_web_server

    continuum = build()
    try:
        typer.echo(f"Starting Persona Continuum Web UI on http://{host}:{port}")
        run_web_server(continuum, host=host, port=port, log_level=log_level)
    finally:
        continuum.close()


@app.command()
def mcp() -> None:
    from persona_continuum.mcp.server import main as mcp_main

    mcp_main()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
