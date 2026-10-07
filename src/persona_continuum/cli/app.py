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
recall_app = typer.Typer(help="Debug memory recall (Phase 7 raw excerpts).")
continuation_app = typer.Typer(help="Manage continuation branches.")
agents_app = typer.Typer(help="Discover and inspect agent cognitive hosts.")
room_app = typer.Typer(help="Manage multi-agent persona rooms.")
app.add_typer(persona_app, name="persona")
app.add_typer(session_app, name="session")
app.add_typer(memory_app, name="memory")
app.add_typer(recall_app, name="recall")
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


def _install_starter_kit(continuum: PersonaContinuum) -> None:
    report = continuum.starter_kits.ensure_installed()
    if report["installed"]:
        typer.echo(f"Installed 太卜阁 starter personas: {', '.join(report['installed'])}")


@app.command()
def init() -> None:
    continuum = build()
    _install_starter_kit(continuum)
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


@memory_app.command("episodes")
def memory_episodes(
    persona_id: Annotated[str | None, typer.Option("--persona")] = None,
    room_id: Annotated[str | None, typer.Option("--room")] = None,
    status: Annotated[str | None, typer.Option("--status")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 20,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List Episodes (bounded stretches of shared experience)."""

    continuum = build()
    try:
        episodes = continuum.episodes.list_episodes(
            persona_id=persona_id, room_id=room_id, status=status, limit=limit
        )
        if json_output:
            typer.echo(
                json.dumps(
                    [episode.model_dump(mode="json") for episode in episodes],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        for episode in episodes:
            window = f"{episode.started_at:%Y-%m-%d %H:%M}"
            if episode.ended_at:
                window += f" → {episode.ended_at:%H:%M}"
            typer.echo(
                f"{episode.id}\t[{episode.status.value}/{episode.summary_status}]"
                f"\t{episode.turn_count} turns\t{episode.boundary_reason}\t{window}"
                f"\t{episode.title or '(未摘要)'}"
            )
    finally:
        continuum.close()


@memory_app.command("episode")
def memory_episode(
    episode_id: str,
    show_sources: Annotated[bool, typer.Option("--sources")] = False,
    show_text: Annotated[bool, typer.Option("--text")] = False,
) -> None:
    """Inspect one Episode and, optionally, its provenance and raw text."""

    continuum = build()
    try:
        payload = continuum.episodes.inspect_episode(episode_id)
        if payload is None:
            typer.echo(f"episode not found: {episode_id}", err=True)
            raise typer.Exit(code=1)
        episode = payload["episode"]
        typer.echo(f"# {episode['title'] or '(未摘要)'}")
        typer.echo(f"id: {episode['id']}")
        typer.echo(
            "scope: "
            f"persona={episode['persona_id']} counterpart={episode['counterpart_id']} "
            f"branch={episode['branch_id']} session={episode['session_id']} "
            f"room={episode['room_id']}"
        )
        typer.echo(
            f"status: {episode['status']} / summary={episode['summary_status']} "
            f"boundary={episode['boundary_reason']} turns={episode['turn_count']}"
        )
        if episode["last_error"]:
            typer.echo(f"last_error: {episode['last_error']}")
        if episode["summary"]:
            typer.echo(f"\n{episode['summary']}")
        summary = payload["summary"]
        for key in (
            "important_events",
            "commitments",
            "unresolved",
            "emotional_arc",
            "topics",
            "entities",
            "user_stated",
            "persona_stated",
            "inferred_context",
        ):
            values = summary.get(key) or []
            if values:
                typer.echo(f"- {key}: " + "; ".join(str(value) for value in values))
        typer.echo(f"\nsource_turn_ids: {len(payload['source_turn_ids'])}")
        if show_sources:
            for source in payload.get("sources") or []:
                typer.echo(json.dumps(source, ensure_ascii=False))
        if show_text:
            # Explicit opt-in: this is the only path that prints dialogue.
            for turn in continuum.episodes.episode_turns(episode_id):
                text = continuum.episodes.resolve_turn_text(turn)
                typer.echo(f"\n--- {turn.turn_id} (position {turn.position}) ---")
                typer.echo(text)
    finally:
        continuum.close()


@memory_app.command("episode-coverage")
def memory_episode_coverage(
    persona_id: Annotated[str | None, typer.Option("--persona")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check the invariant: no committed turn may be unaccounted for."""

    continuum = build()
    try:
        coverage = continuum.episodes.coverage(persona_id=persona_id)
        if json_output:
            typer.echo(json.dumps(coverage, ensure_ascii=False, indent=2))
            return
        typer.echo(f"committed turns:            {coverage['committed_turns']}")
        typer.echo(f"assigned to episode:        {coverage['assigned_to_episode']}")
        typer.echo(f"pending consolidation:      {coverage['pending_consolidation']}")
        typer.echo(f"unassigned (backfill):      {coverage['unassigned_pending_backfill']}")
        typer.echo(f"orphaned:                   {coverage['orphaned']}")
        typer.echo(f"episodes:                   {coverage['episodes']}")
    finally:
        continuum.close()


@memory_app.command("episode-backfill")
def memory_episode_backfill(
    limit: Annotated[int, typer.Option("--limit")] = 40,
    persona_id: Annotated[str | None, typer.Option("--persona")] = None,
) -> None:
    """Lazily fold pre-Phase-3 turns into Episodes, oldest first."""

    continuum = build()
    try:
        result = continuum.episodes.backfill(limit=limit, persona_id=persona_id)
        typer.echo(
            f"processed={result['processed']} created={result['created']} "
            f"appended={result['appended']} remaining_in_batch={result['remaining']}"
        )
    finally:
        continuum.close()


@memory_app.command("facts")
def memory_facts(
    persona_id: Annotated[str | None, typer.Option("--persona")] = None,
    counterpart_id: Annotated[str | None, typer.Option("--counterpart")] = None,
    status: Annotated[str | None, typer.Option("--status")] = None,
    category: Annotated[str | None, typer.Option("--category")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 20,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List Semantic Facts (durable, provenance-backed, time-scoped)."""

    continuum = build()
    try:
        facts = continuum.facts.list_facts(
            persona_id=persona_id,
            counterpart_id=counterpart_id,
            status=status,
            category=category,
            limit=limit,
        )
        if json_output:
            typer.echo(
                json.dumps(
                    [fact.model_dump(mode="json") for fact in facts],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        for fact in facts:
            window = ""
            if fact.valid_from:
                window = f"{fact.valid_from:%Y-%m-%d}"
                window += f"→{fact.valid_until:%Y-%m-%d}" if fact.valid_until else "→open"
            typer.echo(
                f"{fact.id}\t[{fact.status.value}/{fact.category.value}]"
                f"\t{fact.display_text}\torigin={fact.origin.value}"
                f"\tevidence={fact.evidence_count}\tconf={fact.confidence:.2f}\t{window}"
            )
        stats = continuum.facts.stats(
            persona_id=persona_id, counterpart_id=counterpart_id
        )
        typer.echo(
            f"\nfacts={stats['facts']} active={stats['active']} "
            f"superseded={stats['superseded']} candidate={stats['candidate']} "
            f"retracted={stats['retracted']}"
        )
    finally:
        continuum.close()


@memory_app.command("fact")
def memory_fact(
    fact_id: str,
    show_sources: Annotated[bool, typer.Option("--sources")] = False,
) -> None:
    """Inspect one Semantic Fact and its provenance chain."""

    continuum = build()
    try:
        payload = continuum.facts.inspect_fact(fact_id)
        if payload is None:
            typer.echo(f"fact not found: {fact_id}", err=True)
            raise typer.Exit(code=1)
        fact = payload["fact"]
        typer.echo(f"# {fact['display_text'] or '(no display text)'}")
        typer.echo(f"id: {fact['id']}")
        typer.echo(
            "scope: "
            f"persona={fact['persona_id']} counterpart={fact['counterpart_id']} "
            f"branch={fact['branch_id']}"
        )
        typer.echo(
            f"category: {fact['category']}\tsubject: {fact['subject']}\t"
            f"predicate: {fact['predicate']}\tvalue: {fact['value_json']}"
        )
        typer.echo(
            f"status: {fact['status']}\torigin: {fact['origin']}\t"
            f"confidence: {fact['confidence']}\tevidence_count: {fact['evidence_count']}"
        )
        typer.echo(
            f"valid_from: {fact['valid_from']}\tvalid_until: {fact['valid_until']}\t"
            f"valid_now: {payload['valid_now']}"
        )
        if fact["plan_status"] != "not_applicable":
            typer.echo(f"plan_status: {fact['plan_status']}")
        if fact["temporal_expression"]:
            typer.echo(
                f"temporal: {fact['temporal_expression']} → {fact['temporal_normalized']} "
                f"(confidence {fact['temporal_confidence']})"
            )
        typer.echo(
            f"superseded_by: {payload['superseded_by']}\tsupersedes: {payload['supersedes']}"
        )
        if show_sources:
            for source in payload.get("sources") or []:
                typer.echo(json.dumps(source, ensure_ascii=False))
            typer.echo(f"source_turn_ids: {payload.get('source_turn_ids')}")
    finally:
        continuum.close()


@memory_app.command("threads")
def memory_threads(
    persona_id: Annotated[str | None, typer.Option("--persona")] = None,
    counterpart_id: Annotated[str | None, typer.Option("--counterpart")] = None,
    status: Annotated[str | None, typer.Option("--status")] = None,
    thread_type: Annotated[str | None, typer.Option("--type")] = None,
    live: Annotated[bool, typer.Option("--live")] = False,
    limit: Annotated[int, typer.Option("--limit")] = 20,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List Active Threads: things that are still in flight."""

    continuum = build()
    try:
        threads = continuum.threads.list_threads(
            persona_id=persona_id,
            counterpart_id=counterpart_id,
            status=status,
            thread_type=thread_type,
            live_only=live,
            limit=limit,
        )
        if json_output:
            typer.echo(
                json.dumps(
                    [thread.model_dump(mode="json") for thread in threads],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        for thread in threads:
            activity = f"{thread.last_activity_at:%Y-%m-%d}"
            typer.echo(
                f"{thread.id}\t[{thread.status.value}/{thread.thread_type.value}]"
                f"\t{thread.title}\tlast={activity}"
                f"\tconf={thread.confidence:.2f}\timp={thread.importance:.2f}"
            )
            if thread.current_state.get("milestones"):
                typer.echo(f"    milestones: {thread.current_state['milestones']}")
        stats = continuum.threads.stats(
            persona_id=persona_id, counterpart_id=counterpart_id
        )
        typer.echo(
            f"\nthreads={stats['threads']} live={stats['live']} events={stats['events']} "
            f"pending_candidates={stats['pending_candidates']}"
        )
        typer.echo(f"by_status={stats['by_status']}")
    finally:
        continuum.close()


@memory_app.command("thread")
def memory_thread(
    thread_id: str,
    show_events: Annotated[bool, typer.Option("--events")] = False,
    show_sources: Annotated[bool, typer.Option("--sources")] = False,
) -> None:
    """Inspect one Active Thread: history, facts, provenance, candidates."""

    continuum = build()
    try:
        payload = continuum.threads.inspect_thread(thread_id)
        if payload is None:
            typer.echo(f"thread not found: {thread_id}", err=True)
            raise typer.Exit(code=1)
        thread = payload["thread"]
        typer.echo(f"# {thread['title'] or '(untitled)'}")
        typer.echo(f"id: {thread['id']}")
        typer.echo(
            "scope: "
            f"persona={thread['persona_id']} counterpart={thread['counterpart_id']} "
            f"branch={thread['branch_id']}"
        )
        typer.echo(
            f"type: {thread['thread_type']}\tstatus: {thread['status']}\t"
            f"key: {thread['thread_key']}"
        )
        typer.echo(
            f"opened_at: {thread['opened_at']}\tlast_activity_at: {thread['last_activity_at']}\t"
            f"resolved_at: {thread['resolved_at']}\tcancelled_at: {thread['cancelled_at']}"
        )
        typer.echo(
            f"importance: {thread['importance']}\tconfidence: {thread['confidence']}"
        )
        if thread["summary"]:
            typer.echo(f"\n{thread['summary']}")
        if thread["current_state"]:
            typer.echo(f"\nstate: {json.dumps(thread['current_state'], ensure_ascii=False)}")
        if payload["related_previous_thread_id"]:
            typer.echo(f"related_previous_thread_id: {payload['related_previous_thread_id']}")
        typer.echo(
            f"\nsource_episode_ids: {payload['source_episode_ids']}\t"
            f"all_sources_resolvable: {payload['all_sources_resolvable']}"
        )
        for fact in payload.get("facts") or []:
            typer.echo(
                f"  fact[{fact['relation']}] {fact['fact_id']}\t{fact['display_text']}"
            )
        if payload.get("candidates"):
            typer.echo("\npending link candidates:")
            for candidate in payload["candidates"]:
                typer.echo(
                    f"  {candidate['id']}\tepisode={candidate['episode_id']}\t"
                    f"conf={candidate['confidence']}\t{candidate['reason']}"
                )
        if show_events:
            typer.echo("\nevents:")
            for event in payload["events"]:
                typer.echo(
                    f"  {event['occurred_at']}\t[{event['event_type']}]"
                    f"\t{event['summary']}\tepisode={event['source_episode_id']}"
                    f"\tavailability={event['source_availability']}"
                )
        if show_sources:
            for source in payload["sources"]:
                typer.echo(json.dumps(source, ensure_ascii=False))
    finally:
        continuum.close()


@memory_app.command("thread-backfill")
def memory_thread_backfill(
    limit: Annotated[int, typer.Option("--limit")] = 5,
    persona_id: Annotated[str | None, typer.Option("--persona")] = None,
    sweep: Annotated[bool, typer.Option("--sweep")] = False,
) -> None:
    """Show the bounded Thread-resolution backlog (never drains everything)."""

    continuum = build()
    try:
        payload = continuum.threads.backfill(limit=limit, persona_id=persona_id)
        typer.echo(
            f"batch={payload['batch']} remaining={payload['remaining']} "
            f"total_pending={continuum.threads.count_pending_resolution(persona_id=persona_id)}"
        )
        for episode_id in payload["episode_ids"]:
            typer.echo(f"  {episode_id}")
        if sweep:
            typer.echo(json.dumps(continuum.threads.sweep_stale(), ensure_ascii=False))
    finally:
        continuum.close()


@memory_app.command("summaries")
def memory_summaries(
    persona_id: Annotated[str | None, typer.Option("--persona")] = None,
    counterpart_id: Annotated[str | None, typer.Option("--counterpart")] = None,
    level: Annotated[int | None, typer.Option("--level")] = None,
    status: Annotated[str | None, typer.Option("--status")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 20,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List Chapters (level 1) and Long-term segments (level 2+)."""

    continuum = build()
    try:
        summaries = continuum.hierarchies.list_summaries(
            persona_id=persona_id,
            counterpart_id=counterpart_id,
            level=level,
            status=status,
            limit=limit,
        )
        if json_output:
            typer.echo(
                json.dumps(
                    [summary.model_dump(mode="json") for summary in summaries],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        for summary in summaries:
            typer.echo(
                f"{summary.id}\tL{summary.level}/{summary.summary_type.value}"
                f"\t[{summary.status.value}/{summary.summary_status.value}]"
                f"\t{summary.title or '(未总结)'}\t{summary.time_range_label()}"
                f"\tsources={summary.source_count}\ttokens={summary.source_token_estimate}"
                f"\timp={summary.importance:.2f}"
            )
            if summary.last_error:
                typer.echo(f"    last_error: {summary.last_error}")
        stats = continuum.hierarchies.stats(
            persona_id=persona_id, counterpart_id=counterpart_id
        )
        typer.echo(
            f"\nsummaries={stats['summaries']} available={stats['available']} "
            f"pending={stats['pending']} ungrouped_episodes={stats['ungrouped_episodes']}"
        )
        typer.echo(f"by_level={stats['by_level']} by_readiness={stats['by_readiness']}")
    finally:
        continuum.close()


@memory_app.command("summary")
def memory_summary(
    summary_id: str,
    show_sources: Annotated[bool, typer.Option("--sources")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Inspect one summary and its provenance chain down to raw text."""

    continuum = build()
    try:
        payload = continuum.hierarchies.inspect_summary(summary_id)
        if payload is None:
            typer.echo(f"summary not found: {summary_id}", err=True)
            raise typer.Exit(code=1)
        if json_output:
            typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
            return
        summary = payload["summary"]
        content = payload["content"]
        typer.echo(f"# {content['title'] or summary['title'] or '(未总结)'}")
        typer.echo(f"id: {summary['id']}")
        typer.echo(
            f"level: {summary['level']} ({summary['summary_type']})\t"
            f"status: {summary['status']}\tsummary_status: {summary['summary_status']}"
        )
        typer.echo(
            "scope: "
            f"persona={summary['persona_id']} counterpart={summary['counterpart_id']} "
            f"branch={summary['branch_id']}"
        )
        typer.echo(
            f"time_range: {payload['time_range']}\tsources: {summary['source_count']}\t"
            f"source_tokens: {summary['source_token_estimate']}"
        )
        typer.echo(
            f"source_availability: {payload['source_availability']}\t"
            f"unavailable_source_count: {payload['unavailable_source_count']}\t"
            f"all_sources_resolvable: {payload['all_sources_resolvable']}"
        )
        if content["summary"]:
            typer.echo(f"\n{content['summary']}")
        for label, key in (
            ("major_events", "major_events"),
            ("relationship_changes", "relationship_changes"),
            ("fact_changes", "important_preferences_or_fact_changes"),
            ("resolved_threads", "resolved_threads"),
            ("ongoing_threads", "ongoing_threads"),
            ("unresolved_topics", "unresolved_topics"),
            ("inferences", "inferences"),
        ):
            items = content.get(key) or []
            if not items:
                continue
            typer.echo(f"\n{label}:")
            for item in items:
                typer.echo(f"  - {item}")
        if show_sources:
            typer.echo("\nsources:")
            for source in payload["sources"]:
                typer.echo(json.dumps(source, ensure_ascii=False))
    finally:
        continuum.close()


@memory_app.command("hierarchy-backfill")
def memory_hierarchy_backfill(
    limit: Annotated[int, typer.Option("--limit")] = 20,
    persona_id: Annotated[str | None, typer.Option("--persona")] = None,
) -> None:
    """Group ungrouped Episodes into Chapters (bounded, no model call)."""

    continuum = build()
    try:
        payload = continuum.hierarchies.backfill(limit=limit, persona_id=persona_id)
        typer.echo(
            f"processed={payload['processed']} remaining={payload['remaining']} "
            f"created={payload['created']} appended={payload['appended']} "
            f"pending_summaries={payload['pending_summaries']}"
        )
    finally:
        continuum.close()


@recall_app.command("raw")
def recall_raw(
    persona_id: str,
    counterpart_id: Annotated[str, typer.Option("--counterpart")] = "user",
    branch_id: Annotated[str, typer.Option("--branch")] = "main",
    query: Annotated[str, typer.Option("--query", "-q")] = "",
    episode: Annotated[list[str] | None, typer.Option("--episode")] = None,
    fact: Annotated[list[str] | None, typer.Option("--fact")] = None,
    thread: Annotated[list[str] | None, typer.Option("--thread")] = None,
    summary: Annotated[list[str] | None, typer.Option("--summary")] = None,
    ref: Annotated[list[str] | None, typer.Option("--ref")] = None,
    max_total_tokens: Annotated[int, typer.Option("--max-total-tokens")] = 4000,
    max_excerpts: Annotated[int, typer.Option("--max-excerpts")] = 4,
    max_tokens_per_excerpt: Annotated[int, typer.Option("--max-tokens-per-excerpt")] = 1200,
    max_turns_per_excerpt: Annotated[int, typer.Option("--max-turns-per-excerpt")] = 12,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    show_text: Annotated[bool, typer.Option("--text")] = False,
) -> None:
    """Provenance-backed raw recall (Phase 7 debug entry point).

    Shows the selected excerpts with their scores, selection reasons, memory
    sources, episodes, turns, speakers, times, token estimates and availability.
    The raw text is printed only with ``--text``: ordinary logs never carry
    conversation content.
    """

    from typing import Any

    from persona_continuum.domain.raw_recall import (
        RawMemoryRef,
        RawMemoryRefType,
        RawRecallBudget,
        RawRecallScope,
    )

    refs: list[RawMemoryRef | dict[str, Any]] = []
    for identifier in episode or []:
        refs.append(RawMemoryRef(ref_type=RawMemoryRefType.EPISODE, ref_id=identifier))
    for identifier in fact or []:
        refs.append(RawMemoryRef(ref_type=RawMemoryRefType.FACT, ref_id=identifier))
    for identifier in thread or []:
        refs.append(RawMemoryRef(ref_type=RawMemoryRefType.THREAD, ref_id=identifier))
    for identifier in summary or []:
        refs.append(RawMemoryRef(ref_type=RawMemoryRefType.SUMMARY, ref_id=identifier))
    for ref_str in ref or []:
        kind, _, identifier = ref_str.partition(":")
        refs.append(RawMemoryRef(ref_type=RawMemoryRefType(kind), ref_id=identifier))
    if not refs:
        typer.echo("no memory refs given (use --episode/--fact/--thread/--summary/--ref)", err=True)
        raise typer.Exit(code=1)

    continuum = build()
    try:
        result = continuum.raw_recall.recall(
            scope=RawRecallScope(
                persona_id=persona_id, counterpart_id=counterpart_id, branch_id=branch_id
            ),
            query=query,
            memory_refs=refs,
            budget=RawRecallBudget(
                max_total_tokens=max_total_tokens,
                max_excerpts=max_excerpts,
                max_tokens_per_excerpt=max_tokens_per_excerpt,
                max_turns_per_excerpt=max_turns_per_excerpt,
            ),
        )
        if json_output:
            payload = result.model_dump(mode="json")
            if not show_text:
                for excerpt in payload["excerpts"]:
                    for message in excerpt["messages"]:
                        message["raw_text"] = f"<{len(message['raw_text'])} chars>"
            typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
            return
        typer.echo(
            f"scope persona={persona_id} counterpart={counterpart_id} branch={branch_id}"
        )
        typer.echo(
            f"anchors={result.anchors_considered} excerpts={len(result.excerpts)} "
            f"merged={result.windows_merged} deduplicated={result.turns_deduplicated} "
            f"tokens={result.total_tokens}"
        )
        for unavail in result.unavailable:
            typer.echo(
                f"unavailable: {unavail.ref.ref_type.value}:{unavail.ref.ref_id} "
                f"({unavail.availability.value}) {unavail.detail}"
            )
        for explanation in result.explain():
            typer.echo(
                f"\nexcerpt {explanation['excerpt_id']}\n"
                f"  reason: {explanation['selection_reason']}\n"
                f"  scores: relevance={explanation['relevance_score']} "
                f"provenance={explanation['provenance_score']}\n"
                f"  memory: {', '.join(explanation['memory_refs'])}\n"
                f"  window: {explanation['started_at']} ~ {explanation['ended_at']} "
                f"messages={explanation['messages']} tokens={explanation['token_estimate']} "
                f"truncated={explanation['truncated']} "
                f"({explanation['truncation_reason']})\n"
                f"  availability: {explanation['source_availability']}\n"
                f"  episodes: {', '.join(explanation['episode_ids'])}\n"
                f"  turns: {', '.join(explanation['turn_ids'])}"
            )
            if show_text:
                excerpt = next(
                    item
                    for item in result.excerpts
                    if item.excerpt_id == explanation["excerpt_id"]
                )
                for message in excerpt.messages:
                    stamp = f" {message.timestamp}" if message.timestamp else ""
                    typer.echo(f"    [{message.speaker}]{stamp} {message.raw_text}")
    finally:
        continuum.close()


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
    port: Annotated[int, typer.Option("--port", "-p", help="Port number")] = 8000,
    log_level: Annotated[str, typer.Option("--log-level", help="Log level")] = "info",
    open_browser: Annotated[
        bool, typer.Option("--open", help="Open the Web UI in the default browser")
    ] = False,
) -> None:
    """Start the Persona Continuum Web UI."""
    from persona_continuum.web.server import run_web_server

    continuum = build()
    try:
        _install_starter_kit(continuum)
        url = f"http://{host}:{port}"
        typer.echo(f"Starting Persona Continuum Web UI on {url}")
        if open_browser:
            import threading
            import webbrowser

            threading.Timer(1.5, webbrowser.open, args=(url,)).start()
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
