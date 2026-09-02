"""SQLite persistence for the Narrative Studio subsystem.

Follows the WorldRepository idiom: ``standalone = not db.conn.in_transaction``
so an outer transaction (e.g. an atomic canon commit) owns the commit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from persona_continuum.application._utils import dumps, loads
from persona_continuum.domain.narrative import (
    AudienceKnowledgeEntry,
    CanonEntry,
    CanonRevision,
    CharacterArc,
    CharacterKnowledgeEntry,
    EpisodePlan,
    EpisodeSummary,
    EpisodeVersion,
    ExecutableVideoProductionGuide,
    ModelPromptPackage,
    NarrativeAuditReport,
    NarrativeCharacter,
    NarrativeCharacterBinding,
    NarrativeClue,
    NarrativeDirectorAction,
    NarrativeDirectorMessage,
    NarrativeDirectorSession,
    NarrativeForecast,
    NarrativeProject,
    NarrativeScene,
    NarrativeShootingAction,
    NarrativeShootingMessage,
    NarrativeShootingSession,
    PlotThread,
    ProductionAsset,
    ProductionPackage,
    StoryBible,
    StoryFact,
    WriterRoomSynthesis,
)
from persona_continuum.storage.database import Database


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _pk_col(table: str) -> str:
    return "id" if table == "narrative_projects" else "project_id"


def _parse_dt(value: str | None) -> datetime:
    try:
        return datetime.fromisoformat(value) if value else datetime.now(UTC)
    except ValueError:
        return datetime.now(UTC)


class NarrativeRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------
    def save_project(self, project: NarrativeProject) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_projects (
              id, title, logline, description, format, genre_json, tone_json,
              target_audience, planned_episode_count,
              episode_duration_seconds_min, episode_duration_seconds_max,
              story_world_id, canonical_world_branch_id, story_bible_version,
              status, revision, runtime_assignment_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              title = excluded.title,
              logline = excluded.logline,
              description = excluded.description,
              format = excluded.format,
              genre_json = excluded.genre_json,
              tone_json = excluded.tone_json,
              target_audience = excluded.target_audience,
              planned_episode_count = excluded.planned_episode_count,
              episode_duration_seconds_min = excluded.episode_duration_seconds_min,
              episode_duration_seconds_max = excluded.episode_duration_seconds_max,
              story_world_id = excluded.story_world_id,
              canonical_world_branch_id = excluded.canonical_world_branch_id,
              story_bible_version = excluded.story_bible_version,
              status = excluded.status,
              revision = excluded.revision,
              runtime_assignment_json = excluded.runtime_assignment_json,
              updated_at = excluded.updated_at
            """,
            (
                project.id,
                project.title,
                project.logline,
                project.description,
                project.format.value,
                dumps(project.genre),
                dumps(project.tone),
                project.target_audience,
                project.planned_episode_count,
                project.episode_duration_seconds_min,
                project.episode_duration_seconds_max,
                project.story_world_id,
                project.canonical_world_branch_id,
                project.story_bible_version,
                project.status.value,
                project.revision,
                dumps(project.runtime_assignment),
                project.created_at.isoformat(),
                _now(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def get_project(self, project_id: str) -> NarrativeProject | None:
        row = self.db.conn.execute(
            "SELECT * FROM narrative_projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not row:
            return None
        return NarrativeProject(
            id=row["id"],
            title=row["title"],
            logline=row["logline"],
            description=row["description"],
            format=row["format"],
            genre=loads(row["genre_json"]),
            tone=loads(row["tone_json"]),
            target_audience=row["target_audience"],
            planned_episode_count=row["planned_episode_count"],
            episode_duration_seconds_min=row["episode_duration_seconds_min"],
            episode_duration_seconds_max=row["episode_duration_seconds_max"],
            story_world_id=row["story_world_id"],
            canonical_world_branch_id=row["canonical_world_branch_id"],
            story_bible_version=row["story_bible_version"],
            status=row["status"],
            revision=row["revision"],
            runtime_assignment=loads(row["runtime_assignment_json"]),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )

    def list_projects(self) -> list[NarrativeProject]:
        rows = self.db.conn.execute(
            "SELECT id FROM narrative_projects ORDER BY created_at DESC"
        ).fetchall()
        return [
            project
            for project in (self.get_project(row["id"]) for row in rows)
            if project is not None
        ]

    def delete_project(self, project_id: str) -> bool:
        project = self.get_project(project_id)
        if not project:
            return False
        with self.db.conn:
            for table in (
                "narrative_story_bible_versions",
                "narrative_story_facts",
                "narrative_characters",
                "narrative_character_bindings",
                "narrative_episode_plans",
                "narrative_episode_versions",
                "narrative_scenes",
                "narrative_canon_facts",
                "narrative_canon_revisions",
                "narrative_knowledge",
                "narrative_audience_knowledge",
                "narrative_plot_threads",
                "narrative_character_arcs",
                "narrative_clues",
                "narrative_forecasts",
                "narrative_writer_room_runs",
                "narrative_audits",
                "narrative_production_packages",
                "narrative_episode_summaries",
                "narrative_projects",
            ):
                self.db.conn.execute(
                    f"DELETE FROM {table} WHERE {_pk_col(table)} = ?",
                    (project_id,),
                )
        return True

    # ------------------------------------------------------------------
    # Story Bible versions
    # ------------------------------------------------------------------
    def save_bible_version(self, bible: StoryBible) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_story_bible_versions (
              id, project_id, version, bible_json, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_id, version) DO UPDATE SET
              bible_json = excluded.bible_json
            """,
            (
                bible.id,
                bible.project_id,
                bible.version,
                dumps(bible.model_dump(mode="json")),
                _now(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def get_bible(self, project_id: str, version: int | None = None) -> StoryBible | None:
        if version is None:
            row = self.db.conn.execute(
                "SELECT bible_json FROM narrative_story_bible_versions "
                "WHERE project_id = ? ORDER BY version DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        else:
            row = self.db.conn.execute(
                "SELECT bible_json FROM narrative_story_bible_versions "
                "WHERE project_id = ? AND version = ?",
                (project_id, version),
            ).fetchone()
        if not row:
            return None
        return StoryBible.model_validate(loads(row["bible_json"]))

    def list_bible_versions(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            "SELECT id, version, created_at FROM narrative_story_bible_versions "
            "WHERE project_id = ? ORDER BY version DESC",
            (project_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Story facts
    # ------------------------------------------------------------------
    def save_fact(self, fact: StoryFact) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_story_facts (id, project_id, text, category, secret, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              text = excluded.text, category = excluded.category, secret = excluded.secret
            """,
            (
                fact.id,
                fact.project_id,
                fact.text,
                fact.category,
                1 if fact.secret else 0,
                fact.created_at.isoformat(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def list_facts(self, project_id: str) -> list[StoryFact]:
        rows = self.db.conn.execute(
            "SELECT * FROM narrative_story_facts WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()
        return [
            StoryFact(
                id=row["id"],
                project_id=row["project_id"],
                text=row["text"],
                category=row["category"],
                secret=bool(row["secret"]),
                created_at=_parse_dt(row["created_at"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Characters & bindings
    # ------------------------------------------------------------------
    def save_character(self, character: NarrativeCharacter) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_characters (
              id, project_id, name, role, description, persona_id, world_actor_id,
              persona_origin, provenance, visual_json, dialogue_samples_json,
              backstory, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              name = excluded.name, role = excluded.role,
              description = excluded.description, persona_id = excluded.persona_id,
              world_actor_id = excluded.world_actor_id,
              persona_origin = excluded.persona_origin,
              provenance = excluded.provenance,
              visual_json = excluded.visual_json,
              dialogue_samples_json = excluded.dialogue_samples_json,
              backstory = excluded.backstory,
              updated_at = excluded.updated_at
            """,
            (
                character.id,
                character.project_id,
                character.name,
                character.role,
                character.description,
                character.persona_id,
                character.world_actor_id,
                character.persona_origin,
                character.provenance,
                dumps(character.visual),
                dumps(character.dialogue_samples),
                character.backstory,
                character.created_at.isoformat(),
                _now(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def get_character(self, character_id: str) -> NarrativeCharacter | None:
        row = self.db.conn.execute(
            "SELECT * FROM narrative_characters WHERE id = ?", (character_id,)
        ).fetchone()
        return self._character_from_row(row) if row else None

    def list_characters(self, project_id: str) -> list[NarrativeCharacter]:
        rows = self.db.conn.execute(
            "SELECT * FROM narrative_characters WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()
        return [c for c in (self._character_from_row(row) for row in rows) if c]

    def _character_from_row(self, row: Any) -> NarrativeCharacter:
        return NarrativeCharacter(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            role=row["role"],
            description=row["description"],
            persona_id=row["persona_id"],
            world_actor_id=row["world_actor_id"],
            persona_origin=row["persona_origin"],
            provenance=row["provenance"],
            visual=loads(row["visual_json"]),
            dialogue_samples=loads(row["dialogue_samples_json"]),
            backstory=row["backstory"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )

    def delete_bindings_for_character(self, project_id: str, character_id: str) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            DELETE FROM narrative_character_bindings
            WHERE project_id = ? AND story_character_id = ?
            """,
            (project_id, character_id),
        )
        if standalone:
            self.db.conn.commit()

    def save_binding(self, binding: NarrativeCharacterBinding) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_character_bindings (
              id, project_id, story_character_id, persona_id, world_actor_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                binding.id,
                binding.project_id,
                binding.story_character_id,
                binding.persona_id,
                binding.world_actor_id,
                _now(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def list_bindings(self, project_id: str) -> list[NarrativeCharacterBinding]:
        rows = self.db.conn.execute(
            "SELECT * FROM narrative_character_bindings WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()
        return [
            NarrativeCharacterBinding(
                id=row["id"],
                project_id=row["project_id"],
                story_character_id=row["story_character_id"],
                persona_id=row["persona_id"],
                world_actor_id=row["world_actor_id"],
                created_at=_parse_dt(row["created_at"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Episode plans
    # ------------------------------------------------------------------
    def save_episode_plan(self, plan: EpisodePlan) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_episode_plans (
              id, project_id, episode_number, plan_json, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, episode_number) DO UPDATE SET
              plan_json = excluded.plan_json,
              status = excluded.status,
              updated_at = excluded.updated_at
            """,
            (
                plan.id,
                plan.project_id,
                plan.episode_number,
                dumps(plan.model_dump(mode="json")),
                plan.status.value,
                plan.created_at.isoformat(),
                _now(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def get_episode_plan(
        self, project_id: str, episode_number: int
    ) -> EpisodePlan | None:
        row = self.db.conn.execute(
            "SELECT plan_json FROM narrative_episode_plans "
            "WHERE project_id = ? AND episode_number = ?",
            (project_id, episode_number),
        ).fetchone()
        if not row:
            return None
        return EpisodePlan.model_validate(loads(row["plan_json"]))

    def list_episode_plans(self, project_id: str) -> list[EpisodePlan]:
        rows = self.db.conn.execute(
            "SELECT plan_json FROM narrative_episode_plans "
            "WHERE project_id = ? ORDER BY episode_number",
            (project_id,),
        ).fetchall()
        return [EpisodePlan.model_validate(loads(row["plan_json"])) for row in rows]

    # ------------------------------------------------------------------
    # Episode versions
    # ------------------------------------------------------------------
    def save_episode_version(self, version: EpisodeVersion) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_episode_versions (
              id, project_id, episode_number, version, created_by, version_json,
              is_canon, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, episode_number, version) DO UPDATE SET
              version_json = excluded.version_json,
              is_canon = excluded.is_canon
            """,
            (
                version.id,
                version.project_id,
                version.episode_number,
                version.version,
                version.created_by,
                dumps(version.model_dump(mode="json")),
                1 if version.is_canon else 0,
                version.created_at.isoformat(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def get_episode_version(self, version_id: str) -> EpisodeVersion | None:
        row = self.db.conn.execute(
            "SELECT version_json FROM narrative_episode_versions WHERE id = ?", (version_id,)
        ).fetchone()
        if not row:
            return None
        return EpisodeVersion.model_validate(loads(row["version_json"]))

    def list_episode_versions(
        self, project_id: str, episode_number: int
    ) -> list[EpisodeVersion]:
        rows = self.db.conn.execute(
            "SELECT version_json FROM narrative_episode_versions "
            "WHERE project_id = ? AND episode_number = ? ORDER BY version DESC",
            (project_id, episode_number),
        ).fetchall()
        return [EpisodeVersion.model_validate(loads(row["version_json"])) for row in rows]

    def get_canon_episode_version(
        self, project_id: str, episode_number: int
    ) -> EpisodeVersion | None:
        row = self.db.conn.execute(
            "SELECT version_json FROM narrative_episode_versions "
            "WHERE project_id = ? AND episode_number = ? AND is_canon = 1 "
            "ORDER BY version DESC LIMIT 1",
            (project_id, episode_number),
        ).fetchone()
        if not row:
            return None
        return EpisodeVersion.model_validate(loads(row["version_json"]))

    def set_canon_episode_version(
        self, project_id: str, episode_number: int, version_id: str
    ) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            "UPDATE narrative_episode_versions SET is_canon = 0 "
            "WHERE project_id = ? AND episode_number = ? AND is_canon = 1",
            (project_id, episode_number),
        )
        self.db.conn.execute(
            "UPDATE narrative_episode_versions SET is_canon = 1 WHERE id = ?",
            (version_id,),
        )
        if standalone:
            self.db.conn.commit()

    def list_stale_episode_versions(self, project_id: str) -> list[EpisodeVersion]:
        rows = self.db.conn.execute(
            "SELECT version_json FROM narrative_episode_versions "
            "WHERE project_id = ? AND is_canon = 0 ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
        return [EpisodeVersion.model_validate(loads(row["version_json"])) for row in rows]

    # ------------------------------------------------------------------
    # Scenes
    # ------------------------------------------------------------------
    def save_scene(self, scene: NarrativeScene) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_scenes (
              id, project_id, episode_id, episode_number, scene_order, scene_json,
              status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              scene_json = excluded.scene_json,
              status = excluded.status,
              updated_at = excluded.updated_at
            """,
            (
                scene.id,
                scene.project_id,
                scene.episode_id,
                scene.episode_number,
                scene.order,
                dumps(scene.model_dump(mode="json")),
                scene.status.value,
                scene.created_at.isoformat(),
                _now(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def get_scene(self, scene_id: str) -> NarrativeScene | None:
        row = self.db.conn.execute(
            "SELECT scene_json FROM narrative_scenes WHERE id = ?", (scene_id,)
        ).fetchone()
        if not row:
            return None
        return NarrativeScene.model_validate(loads(row["scene_json"]))

    def list_scenes(
        self, project_id: str, episode_number: int | None = None
    ) -> list[NarrativeScene]:
        if episode_number is None:
            rows = self.db.conn.execute(
                "SELECT scene_json FROM narrative_scenes "
                "WHERE project_id = ? ORDER BY episode_number, scene_order",
                (project_id,),
            ).fetchall()
        else:
            rows = self.db.conn.execute(
                "SELECT scene_json FROM narrative_scenes "
                "WHERE project_id = ? AND episode_number = ? ORDER BY scene_order",
                (project_id, episode_number),
            ).fetchall()
        return [NarrativeScene.model_validate(loads(row["scene_json"])) for row in rows]

    # ------------------------------------------------------------------
    # Canon
    # ------------------------------------------------------------------
    def save_canon_entry(self, entry: CanonEntry) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_canon_facts (
              id, project_id, episode_number, entry_type, text, data_json,
              source, world_branch_id, world_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              text = excluded.text, data_json = excluded.data_json
            """,
            (
                entry.id,
                entry.project_id,
                entry.episode_number,
                entry.entry_type.value,
                entry.text,
                dumps(entry.data),
                entry.source,
                entry.world_branch_id,
                entry.world_event_id,
                entry.created_at.isoformat(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def list_canon_entries(
        self, project_id: str, up_to_episode: int | None = None
    ) -> list[CanonEntry]:
        if up_to_episode is None:
            rows = self.db.conn.execute(
                "SELECT * FROM narrative_canon_facts WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        else:
            rows = self.db.conn.execute(
                "SELECT * FROM narrative_canon_facts WHERE project_id = ? "
                "AND (episode_number IS NULL OR episode_number <= ?) ORDER BY created_at",
                (project_id, up_to_episode),
            ).fetchall()
        return [
            CanonEntry(
                id=row["id"],
                project_id=row["project_id"],
                episode_number=row["episode_number"],
                entry_type=row["entry_type"],
                text=row["text"],
                data=loads(row["data_json"]),
                source=row["source"],
                world_branch_id=row["world_branch_id"],
                world_event_id=row["world_event_id"],
                created_at=_parse_dt(row["created_at"]),
            )
            for row in rows
        ]

    def save_canon_revision(self, revision: CanonRevision) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_canon_revisions (
              id, project_id, canon_entry_id, reason, previous_text, new_text, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision.id,
                revision.project_id,
                revision.canon_entry_id,
                revision.reason,
                revision.previous_text,
                revision.new_text,
                _now(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    # ------------------------------------------------------------------
    # Character knowledge (upsert per project+character+fact)
    # ------------------------------------------------------------------
    def save_knowledge_entry(self, entry: CharacterKnowledgeEntry) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_knowledge (
              id, project_id, character_id, fact_id, entry_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, character_id, fact_id) DO UPDATE SET
              entry_json = excluded.entry_json,
              updated_at = excluded.updated_at
            """,
            (
                entry.id,
                entry.project_id,
                entry.character_id,
                entry.fact_id,
                dumps(entry.model_dump(mode="json")),
                _now(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def list_knowledge(
        self,
        project_id: str,
        character_id: str | None = None,
        episode_number: int | None = None,
    ) -> list[CharacterKnowledgeEntry]:
        if character_id:
            rows = self.db.conn.execute(
                "SELECT entry_json FROM narrative_knowledge "
                "WHERE project_id = ? AND character_id = ?",
                (project_id, character_id),
            ).fetchall()
        else:
            rows = self.db.conn.execute(
                "SELECT entry_json FROM narrative_knowledge WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        entries = [
            CharacterKnowledgeEntry.model_validate(loads(row["entry_json"])) for row in rows
        ]
        if episode_number is not None:
            # A character knows a fact as of an episode if it was learned at or
            # before that episode and not invalidated yet.
            entries = [
                e
                for e in entries
                if e.learned_episode is None or e.learned_episode <= episode_number
            ]
        return entries

    # ------------------------------------------------------------------
    # Audience knowledge (upsert per project+fact)
    # ------------------------------------------------------------------
    def save_audience_entry(self, entry: AudienceKnowledgeEntry) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_audience_knowledge (
              id, project_id, fact_id, entry_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_id, fact_id) DO UPDATE SET
              entry_json = excluded.entry_json,
              updated_at = excluded.updated_at
            """,
            (
                entry.id,
                entry.project_id,
                entry.fact_id,
                dumps(entry.model_dump(mode="json")),
                _now(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def list_audience_knowledge(
        self, project_id: str, episode_number: int | None = None
    ) -> list[AudienceKnowledgeEntry]:
        rows = self.db.conn.execute(
            "SELECT entry_json FROM narrative_audience_knowledge WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        entries = [
            AudienceKnowledgeEntry.model_validate(loads(row["entry_json"])) for row in rows
        ]
        if episode_number is not None:
            entries = [
                e
                for e in entries
                if e.revealed_episode is None or e.revealed_episode <= episode_number
            ]
        return entries

    # ------------------------------------------------------------------
    # Plot threads / arcs / clues
    # ------------------------------------------------------------------
    def save_plot_thread(self, thread: PlotThread) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_plot_threads (
              id, project_id, title, status, thread_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              title = excluded.title, status = excluded.status,
              thread_json = excluded.thread_json, updated_at = excluded.updated_at
            """,
            (
                thread.id,
                thread.project_id,
                thread.title,
                thread.status.value,
                dumps(thread.model_dump(mode="json")),
                _now(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def list_plot_threads(
        self, project_id: str, status: str | None = None
    ) -> list[PlotThread]:
        if status:
            rows = self.db.conn.execute(
                "SELECT thread_json FROM narrative_plot_threads "
                "WHERE project_id = ? AND status = ?",
                (project_id, status),
            ).fetchall()
        else:
            rows = self.db.conn.execute(
                "SELECT thread_json FROM narrative_plot_threads WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        return [PlotThread.model_validate(loads(row["thread_json"])) for row in rows]

    def save_arc(self, arc: CharacterArc) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_character_arcs (
              id, project_id, character_id, arc_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_id, character_id) DO UPDATE SET
              arc_json = excluded.arc_json, updated_at = excluded.updated_at
            """,
            (
                arc.id,
                arc.project_id,
                arc.character_id,
                dumps(arc.model_dump(mode="json")),
                _now(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def list_arcs(self, project_id: str) -> list[CharacterArc]:
        rows = self.db.conn.execute(
            "SELECT arc_json FROM narrative_character_arcs WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        return [CharacterArc.model_validate(loads(row["arc_json"])) for row in rows]

    def save_clue(self, clue: NarrativeClue) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_clues (id, project_id, title, status, clue_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              title = excluded.title, status = excluded.status,
              clue_json = excluded.clue_json, updated_at = excluded.updated_at
            """,
            (
                clue.id,
                clue.project_id,
                clue.title,
                clue.status.value,
                dumps(clue.model_dump(mode="json")),
                _now(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def list_clues(self, project_id: str, status: str | None = None) -> list[NarrativeClue]:
        if status:
            rows = self.db.conn.execute(
                "SELECT clue_json FROM narrative_clues WHERE project_id = ? AND status = ?",
                (project_id, status),
            ).fetchall()
        else:
            rows = self.db.conn.execute(
                "SELECT clue_json FROM narrative_clues WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        return [NarrativeClue.model_validate(loads(row["clue_json"])) for row in rows]

    # ------------------------------------------------------------------
    # Forecasts / audits / production packages / summaries
    # ------------------------------------------------------------------
    def save_forecast(self, forecast: NarrativeForecast) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_forecasts (
              id, project_id, episode_number, status, forecast_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              status = excluded.status, forecast_json = excluded.forecast_json,
              updated_at = excluded.updated_at
            """,
            (
                forecast.id,
                forecast.project_id,
                forecast.episode_number,
                forecast.status.value,
                dumps(forecast.model_dump(mode="json")),
                forecast.created_at.isoformat(),
                _now(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def get_forecast(self, forecast_id: str) -> NarrativeForecast | None:
        row = self.db.conn.execute(
            "SELECT forecast_json FROM narrative_forecasts WHERE id = ?", (forecast_id,)
        ).fetchone()
        if not row:
            return None
        return NarrativeForecast.model_validate(loads(row["forecast_json"]))

    def list_forecasts(self, project_id: str) -> list[NarrativeForecast]:
        rows = self.db.conn.execute(
            "SELECT forecast_json FROM narrative_forecasts "
            "WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
        return [NarrativeForecast.model_validate(loads(row["forecast_json"])) for row in rows]

    def save_audit(self, report: NarrativeAuditReport) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_audits (
              id, project_id, episode_number, episode_version_id, passed, audit_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              passed = excluded.passed, audit_json = excluded.audit_json
            """,
            (
                report.id,
                report.project_id,
                report.episode_number,
                report.episode_version_id,
                1 if report.passed else 0,
                dumps(report.model_dump(mode="json")),
                _now(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def list_audits(
        self, project_id: str, episode_number: int | None = None
    ) -> list[NarrativeAuditReport]:
        if episode_number is None:
            rows = self.db.conn.execute(
                "SELECT audit_json FROM narrative_audits "
                "WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        else:
            rows = self.db.conn.execute(
                "SELECT audit_json FROM narrative_audits "
                "WHERE project_id = ? AND episode_number = ? ORDER BY created_at DESC",
                (project_id, episode_number),
            ).fetchall()
        return [NarrativeAuditReport.model_validate(loads(row["audit_json"])) for row in rows]

    def save_writer_room_synthesis(self, synthesis: WriterRoomSynthesis) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_writer_room_runs (
              id, project_id, episode_number, room_id, synthesis_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET synthesis_json = excluded.synthesis_json
            """,
            (
                synthesis.id,
                synthesis.project_id,
                synthesis.episode_number,
                synthesis.room_id,
                dumps(synthesis.model_dump(mode="json")),
                synthesis.created_at.isoformat(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def get_writer_room_synthesis(
        self, project_id: str, episode_number: int
    ) -> WriterRoomSynthesis | None:
        row = self.db.conn.execute(
            "SELECT synthesis_json FROM narrative_writer_room_runs "
            "WHERE project_id = ? AND episode_number = ? ORDER BY created_at DESC LIMIT 1",
            (project_id, episode_number),
        ).fetchone()
        if not row:
            return None
        return WriterRoomSynthesis.model_validate(loads(row["synthesis_json"]))

    def save_production_package(self, package: ProductionPackage) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_production_packages (
              id, project_id, episode_number, episode_version_id, package_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                package.id,
                package.project_id,
                package.episode_number,
                package.episode_version_id,
                dumps(package.model_dump(mode="json")),
                _now(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def get_production_package(self, package_id: str) -> ProductionPackage | None:
        row = self.db.conn.execute(
            "SELECT package_json FROM narrative_production_packages WHERE id = ?", (package_id,)
        ).fetchone()
        if not row:
            return None
        return ProductionPackage.model_validate(loads(row["package_json"]))

    def list_production_packages(self, project_id: str) -> list[ProductionPackage]:
        rows = self.db.conn.execute(
            "SELECT package_json FROM narrative_production_packages "
            "WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
        return [ProductionPackage.model_validate(loads(row["package_json"])) for row in rows]

    def mark_production_packages_stale(
        self, project_id: str, episode_number: int, *, package_id: str | None = None
    ) -> int:
        """Flag production packages of one episode as stale.

        Follows the mark_model_prompt_packages_stale pattern; the stale flag
        lives inside package_json because the table has no dedicated column.
        Pass ``package_id`` to target a single package instead of the episode.
        """

        standalone = not self.db.conn.in_transaction
        if package_id is None:
            rows = self.db.conn.execute(
                "SELECT id, package_json FROM narrative_production_packages "
                "WHERE project_id = ? AND episode_number = ?",
                (project_id, episode_number),
            ).fetchall()
        else:
            rows = self.db.conn.execute(
                "SELECT id, package_json FROM narrative_production_packages "
                "WHERE project_id = ? AND episode_number = ? AND id = ?",
                (project_id, episode_number, package_id),
            ).fetchall()
        updated = 0
        for row in rows:
            package = ProductionPackage.model_validate(loads(row["package_json"]))
            if package.stale:
                continue
            package.stale = True
            self.db.conn.execute(
                "UPDATE narrative_production_packages SET package_json = ? WHERE id = ?",
                (dumps(package.model_dump(mode="json")), row["id"]),
            )
            updated += 1
        if standalone:
            self.db.conn.commit()
        return updated

    def save_episode_summary(self, summary: EpisodeSummary) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_episode_summaries (
              id, project_id, episode_number, summary, context_fingerprint, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, episode_number) DO UPDATE SET
              summary = excluded.summary,
              context_fingerprint = excluded.context_fingerprint,
              created_at = excluded.created_at
            """,
            (
                f"esum_{summary.project_id}_{summary.episode_number}",
                summary.project_id,
                summary.episode_number,
                summary.summary,
                summary.context_fingerprint,
                _now(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def get_episode_summary(
        self, project_id: str, episode_number: int
    ) -> EpisodeSummary | None:
        row = self.db.conn.execute(
            "SELECT * FROM narrative_episode_summaries WHERE project_id = ? AND episode_number = ?",
            (project_id, episode_number),
        ).fetchone()
        if not row:
            return None
        return EpisodeSummary(
            project_id=row["project_id"],
            episode_number=row["episode_number"],
            summary=row["summary"],
            context_fingerprint=row["context_fingerprint"],
            created_at=_parse_dt(row["created_at"]),
        )

    # ------------------------------------------------------------------
    # Director sessions / messages / actions
    # ------------------------------------------------------------------
    def save_director_session(self, session: NarrativeDirectorSession) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_director_sessions (
              id, project_id, episode_number, mode, status, runtime_json,
              conversation_summary, project_revision, story_bible_version,
              pending_action_json, last_error, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              episode_number = excluded.episode_number,
              mode = excluded.mode,
              status = excluded.status,
              runtime_json = excluded.runtime_json,
              conversation_summary = excluded.conversation_summary,
              project_revision = excluded.project_revision,
              story_bible_version = excluded.story_bible_version,
              pending_action_json = excluded.pending_action_json,
              last_error = excluded.last_error,
              updated_at = excluded.updated_at
            """,
            (
                session.id,
                session.project_id,
                session.episode_number,
                session.mode.value,
                session.status.value,
                dumps(session.runtime),
                session.conversation_summary,
                session.project_revision,
                session.story_bible_version,
                dumps(session.pending_action),
                session.last_error,
                session.created_at.isoformat(),
                _now(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def get_director_session(self, session_id: str) -> NarrativeDirectorSession | None:
        row = self.db.conn.execute(
            "SELECT * FROM narrative_director_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        return NarrativeDirectorSession.model_validate(
            {
                "id": row["id"],
                "project_id": row["project_id"],
                "episode_number": row["episode_number"],
                "mode": row["mode"],
                "status": row["status"],
                "runtime": loads(row["runtime_json"]),
                "conversation_summary": row["conversation_summary"],
                "project_revision": row["project_revision"],
                "story_bible_version": row["story_bible_version"],
                "pending_action": loads(row["pending_action_json"]),
                "last_error": row["last_error"],
                "created_at": _parse_dt(row["created_at"]),
                "updated_at": _parse_dt(row["updated_at"]),
            }
        )

    def list_director_sessions(self, project_id: str) -> list[NarrativeDirectorSession]:
        rows = self.db.conn.execute(
            "SELECT id FROM narrative_director_sessions WHERE project_id = ? "
            "ORDER BY updated_at DESC",
            (project_id,),
        ).fetchall()
        return [
            session
            for session in (self.get_director_session(row["id"]) for row in rows)
            if session is not None
        ]

    def list_director_sessions_in_status(self, status: str) -> list[NarrativeDirectorSession]:
        rows = self.db.conn.execute(
            "SELECT id FROM narrative_director_sessions WHERE status = ?",
            (status,),
        ).fetchall()
        return [
            session
            for session in (self.get_director_session(row["id"]) for row in rows)
            if session is not None
        ]

    def cancel_running_director_actions(self, session_id: str, reason: str) -> int:
        """Flag in-flight actions as cancelled after an interrupted loop."""
        standalone = not self.db.conn.in_transaction
        cursor = self.db.conn.execute(
            """
            UPDATE narrative_director_actions
            SET status = 'cancelled', error_code = 'DIRECTOR_INTERRUPTED',
                error_message = ?, completed_at = ?
            WHERE session_id = ? AND status = 'running'
            """,
            (reason, _now(), session_id),
        )
        if standalone:
            self.db.conn.commit()
        return cursor.rowcount

    def append_director_message(
        self, message: NarrativeDirectorMessage
    ) -> NarrativeDirectorMessage:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_director_messages (id, session_id, role, content, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                message.id,
                message.session_id,
                message.role,
                message.content,
                message.created_at.isoformat(),
            ),
        )
        if standalone:
            self.db.conn.commit()
        return message

    def list_director_messages(self, session_id: str) -> list[NarrativeDirectorMessage]:
        rows = self.db.conn.execute(
            "SELECT * FROM narrative_director_messages WHERE session_id = ? "
            "ORDER BY created_at, id",
            (session_id,),
        ).fetchall()
        return [
            NarrativeDirectorMessage(
                id=row["id"],
                session_id=row["session_id"],
                role=row["role"],
                content=row["content"],
                created_at=_parse_dt(row["created_at"]),
            )
            for row in rows
        ]

    def save_director_action(self, action: NarrativeDirectorAction) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_director_actions (
              id, session_id, message_id, action, arguments_json, result_json,
              status, error_code, error_message, started_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              result_json = excluded.result_json,
              status = excluded.status,
              error_code = excluded.error_code,
              error_message = excluded.error_message,
              completed_at = excluded.completed_at
            """,
            (
                action.id,
                action.session_id,
                action.message_id,
                action.action,
                dumps(action.arguments),
                dumps(action.result),
                action.status.value,
                action.error_code,
                action.error_message,
                action.started_at.isoformat(),
                action.completed_at.isoformat() if action.completed_at else None,
            ),
        )
        if standalone:
            self.db.conn.commit()

    def list_director_actions(self, session_id: str) -> list[NarrativeDirectorAction]:
        rows = self.db.conn.execute(
            "SELECT * FROM narrative_director_actions WHERE session_id = ? "
            "ORDER BY started_at, id",
            (session_id,),
        ).fetchall()
        return [
            NarrativeDirectorAction(
                id=row["id"],
                session_id=row["session_id"],
                message_id=row["message_id"],
                action=row["action"],
                arguments=loads(row["arguments_json"]),
                result=loads(row["result_json"]),
                status=row["status"],
                error_code=row["error_code"],
                error_message=row["error_message"],
                started_at=_parse_dt(row["started_at"]),
                completed_at=_parse_dt(row["completed_at"]) if row["completed_at"] else None,
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Model prompt packages
    # ------------------------------------------------------------------
    def save_model_prompt_package(self, pkg: ModelPromptPackage) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_model_prompt_packages (
              id, project_id, episode_number, production_package_id,
              episode_version_id, target_profile_id, target_profile_version,
              status, stale, context_fingerprint, profile_update_available,
              package_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              production_package_id = excluded.production_package_id,
              episode_version_id = excluded.episode_version_id,
              target_profile_id = excluded.target_profile_id,
              target_profile_version = excluded.target_profile_version,
              status = excluded.status,
              stale = excluded.stale,
              context_fingerprint = excluded.context_fingerprint,
              profile_update_available = excluded.profile_update_available,
              package_json = excluded.package_json,
              updated_at = excluded.updated_at
            """,
            (
                pkg.id,
                pkg.project_id,
                pkg.episode_number,
                pkg.production_package_id,
                pkg.episode_version_id,
                pkg.target_profile_id,
                pkg.target_profile_version,
                pkg.status,
                1 if pkg.stale else 0,
                pkg.context_fingerprint,
                1 if pkg.profile_update_available else 0,
                dumps(pkg.model_dump(mode="json")),
                pkg.created_at.isoformat(),
                _now(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def get_model_prompt_package(self, package_id: str) -> ModelPromptPackage | None:
        row = self.db.conn.execute(
            "SELECT package_json FROM narrative_model_prompt_packages WHERE id = ?",
            (package_id,),
        ).fetchone()
        if not row:
            return None
        return ModelPromptPackage.model_validate(loads(row["package_json"]))

    def list_model_prompt_packages(
        self,
        project_id: str,
        production_package_id: str | None = None,
    ) -> list[ModelPromptPackage]:
        if production_package_id is not None:
            rows = self.db.conn.execute(
                "SELECT package_json FROM narrative_model_prompt_packages "
                "WHERE project_id = ? AND production_package_id = ? "
                "ORDER BY created_at DESC",
                (project_id, production_package_id),
            ).fetchall()
        else:
            rows = self.db.conn.execute(
                "SELECT package_json FROM narrative_model_prompt_packages "
                "WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        return [ModelPromptPackage.model_validate(loads(row["package_json"])) for row in rows]

    def mark_model_prompt_packages_stale(
        self,
        project_id: str,
        episode_number: int,
        stale: bool = True,
        *,
        package_id: str | None = None,
    ) -> int:
        """Flag model prompt packages of one episode as stale.

        The stale flag lives both in the dedicated column and inside
        package_json; every read path hydrates from package_json, so both
        copies must be updated together (mirrors
        mark_production_packages_stale). Pass ``package_id`` to target a
        single package instead of the episode.
        """

        standalone = not self.db.conn.in_transaction
        if package_id is None:
            rows = self.db.conn.execute(
                "SELECT id, package_json FROM narrative_model_prompt_packages "
                "WHERE project_id = ? AND episode_number = ?",
                (project_id, episode_number),
            ).fetchall()
        else:
            rows = self.db.conn.execute(
                "SELECT id, package_json FROM narrative_model_prompt_packages "
                "WHERE project_id = ? AND episode_number = ? AND id = ?",
                (project_id, episode_number, package_id),
            ).fetchall()
        updated = 0
        for row in rows:
            pkg = ModelPromptPackage.model_validate(loads(row["package_json"]))
            if pkg.stale == stale:
                continue
            pkg.stale = stale
            self.db.conn.execute(
                "UPDATE narrative_model_prompt_packages SET stale = ?, "
                "package_json = ?, updated_at = ? WHERE id = ?",
                (
                    1 if stale else 0,
                    dumps(pkg.model_dump(mode="json")),
                    _now(),
                    pkg.id,
                ),
            )
            updated += 1
        if standalone:
            self.db.conn.commit()
        return updated

    def mark_model_prompt_packages_stale_for_production(
        self, project_id: str, production_package_id: str
    ) -> int:
        """Flag prompt packages derived from one production master as stale.

        Like mark_model_prompt_packages_stale, the JSON copy is updated
        alongside the column because readers hydrate from package_json.
        """

        standalone = not self.db.conn.in_transaction
        rows = self.db.conn.execute(
            "SELECT id, package_json FROM narrative_model_prompt_packages "
            "WHERE project_id = ? AND production_package_id = ?",
            (project_id, production_package_id),
        ).fetchall()
        updated = 0
        for row in rows:
            pkg = ModelPromptPackage.model_validate(loads(row["package_json"]))
            if pkg.stale:
                continue
            pkg.stale = True
            self.db.conn.execute(
                "UPDATE narrative_model_prompt_packages SET stale = 1, "
                "package_json = ?, updated_at = ? WHERE id = ?",
                (dumps(pkg.model_dump(mode="json")), _now(), pkg.id),
            )
            updated += 1
        if standalone:
            self.db.conn.commit()
        return updated

    # ------------------------------------------------------------------
    # Production assets
    # ------------------------------------------------------------------
    def save_production_asset(self, asset: ProductionAsset) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_production_assets (
              id, project_id, episode_number, production_package_id,
              asset_type, asset_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              episode_number = excluded.episode_number,
              production_package_id = excluded.production_package_id,
              asset_type = excluded.asset_type,
              asset_json = excluded.asset_json
            """,
            (
                asset.id,
                asset.project_id,
                asset.episode_number,
                asset.metadata.get("production_package_id"),
                asset.asset_type,
                dumps(asset.model_dump(mode="json")),
                asset.created_at.isoformat(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def list_production_assets(
        self,
        project_id: str,
        production_package_id: str | None = None,
    ) -> list[ProductionAsset]:
        if production_package_id is not None:
            rows = self.db.conn.execute(
                "SELECT asset_json FROM narrative_production_assets "
                "WHERE project_id = ? AND production_package_id = ? "
                "ORDER BY created_at, id",
                (project_id, production_package_id),
            ).fetchall()
        else:
            rows = self.db.conn.execute(
                "SELECT asset_json FROM narrative_production_assets "
                "WHERE project_id = ? ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
        return [ProductionAsset.model_validate(loads(row["asset_json"])) for row in rows]

    # ------------------------------------------------------------------
    # Shooting sessions / messages / actions
    # ------------------------------------------------------------------
    def save_shooting_session(self, session: NarrativeShootingSession) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_shooting_sessions (
              id, project_id, episode_number, mode, status, runtime_json,
              conversation_summary, project_revision, story_bible_version,
              pending_action_json, last_error, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              episode_number = excluded.episode_number,
              mode = excluded.mode,
              status = excluded.status,
              runtime_json = excluded.runtime_json,
              conversation_summary = excluded.conversation_summary,
              project_revision = excluded.project_revision,
              story_bible_version = excluded.story_bible_version,
              pending_action_json = excluded.pending_action_json,
              last_error = excluded.last_error,
              updated_at = excluded.updated_at
            """,
            (
                session.id,
                session.project_id,
                session.episode_number,
                session.mode,
                session.status.value,
                dumps(session.runtime),
                session.conversation_summary,
                session.project_revision,
                session.story_bible_version,
                dumps(session.pending_action),
                session.last_error,
                session.created_at.isoformat(),
                _now(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def get_shooting_session(self, session_id: str) -> NarrativeShootingSession | None:
        row = self.db.conn.execute(
            "SELECT * FROM narrative_shooting_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        return NarrativeShootingSession.model_validate(
            {
                "id": row["id"],
                "project_id": row["project_id"],
                "episode_number": row["episode_number"],
                "mode": row["mode"],
                "status": row["status"],
                "runtime": loads(row["runtime_json"]),
                "conversation_summary": row["conversation_summary"],
                "project_revision": row["project_revision"],
                "story_bible_version": row["story_bible_version"],
                "pending_action": loads(row["pending_action_json"]),
                "last_error": row["last_error"],
                "created_at": _parse_dt(row["created_at"]),
                "updated_at": _parse_dt(row["updated_at"]),
            }
        )

    def list_shooting_sessions(self, project_id: str) -> list[NarrativeShootingSession]:
        rows = self.db.conn.execute(
            "SELECT id FROM narrative_shooting_sessions WHERE project_id = ? "
            "ORDER BY updated_at DESC",
            (project_id,),
        ).fetchall()
        return [
            session
            for session in (self.get_shooting_session(row["id"]) for row in rows)
            if session is not None
        ]

    def append_shooting_message(
        self, message: NarrativeShootingMessage
    ) -> NarrativeShootingMessage:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_shooting_messages
              (id, session_id, role, content, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                message.id,
                message.session_id,
                message.role.value,
                message.content,
                message.created_at.isoformat(),
            ),
        )
        if standalone:
            self.db.conn.commit()
        return message

    def list_shooting_messages(self, session_id: str) -> list[NarrativeShootingMessage]:
        rows = self.db.conn.execute(
            "SELECT * FROM narrative_shooting_messages WHERE session_id = ? "
            "ORDER BY created_at, id",
            (session_id,),
        ).fetchall()
        return [
            NarrativeShootingMessage(
                id=row["id"],
                session_id=row["session_id"],
                role=row["role"],
                content=row["content"],
                created_at=_parse_dt(row["created_at"]),
            )
            for row in rows
        ]

    def save_shooting_action(self, action: NarrativeShootingAction) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_shooting_actions (
              id, session_id, message_id, action, arguments_json, result_json,
              status, error_code, error_message, started_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              result_json = excluded.result_json,
              status = excluded.status,
              error_code = excluded.error_code,
              error_message = excluded.error_message,
              completed_at = excluded.completed_at
            """,
            (
                action.id,
                action.session_id,
                action.message_id,
                action.action,
                dumps(action.arguments),
                dumps(action.result),
                action.status.value,
                action.error_code,
                action.error_message,
                action.started_at.isoformat(),
                action.completed_at.isoformat() if action.completed_at else None,
            ),
        )
        if standalone:
            self.db.conn.commit()

    def list_shooting_actions(self, session_id: str) -> list[NarrativeShootingAction]:
        rows = self.db.conn.execute(
            "SELECT * FROM narrative_shooting_actions WHERE session_id = ? "
            "ORDER BY started_at, id",
            (session_id,),
        ).fetchall()
        return [
            NarrativeShootingAction(
                id=row["id"],
                session_id=row["session_id"],
                message_id=row["message_id"],
                action=row["action"],
                arguments=loads(row["arguments_json"]),
                result=loads(row["result_json"]),
                status=row["status"],
                error_code=row["error_code"],
                error_message=row["error_message"],
                started_at=_parse_dt(row["started_at"]),
                completed_at=_parse_dt(row["completed_at"]) if row["completed_at"] else None,
            )
            for row in rows
        ]

    def cancel_running_shooting_actions(self, session_id: str) -> int:
        """Flag in-flight shooting actions as cancelled after an interrupted loop."""
        standalone = not self.db.conn.in_transaction
        cursor = self.db.conn.execute(
            """
            UPDATE narrative_shooting_actions
            SET status = 'cancelled', error_code = 'SHOOTING_INTERRUPTED',
                error_message = 'session loop interrupted', completed_at = ?
            WHERE session_id = ? AND status = 'running'
            """,
            (_now(), session_id),
        )
        if standalone:
            self.db.conn.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Executable video production guides
    # ------------------------------------------------------------------
    def save_video_production_guide(self, guide: ExecutableVideoProductionGuide) -> None:
        standalone = not self.db.conn.in_transaction
        self.db.conn.execute(
            """
            INSERT INTO narrative_video_production_guides (
              id, project_id, episode_number, production_package_id,
              prompt_package_id, episode_version_id, target_profile_id,
              target_profile_version, status, stale, context_fingerprint,
              guide_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              production_package_id = excluded.production_package_id,
              prompt_package_id = excluded.prompt_package_id,
              episode_version_id = excluded.episode_version_id,
              target_profile_id = excluded.target_profile_id,
              target_profile_version = excluded.target_profile_version,
              status = excluded.status,
              stale = excluded.stale,
              context_fingerprint = excluded.context_fingerprint,
              guide_json = excluded.guide_json,
              updated_at = excluded.updated_at
            """,
            (
                guide.id,
                guide.project_id,
                guide.episode_number,
                guide.production_package_id,
                guide.prompt_package_id,
                guide.episode_version_id,
                guide.target_profile_id,
                guide.target_profile_version,
                guide.status,
                1 if guide.stale else 0,
                guide.context_fingerprint,
                dumps(guide.model_dump(mode="json")),
                guide.created_at.isoformat(),
                _now(),
            ),
        )
        if standalone:
            self.db.conn.commit()

    def get_video_production_guide(
        self, guide_id: str
    ) -> ExecutableVideoProductionGuide | None:
        row = self.db.conn.execute(
            "SELECT guide_json FROM narrative_video_production_guides WHERE id = ?",
            (guide_id,),
        ).fetchone()
        if not row:
            return None
        return ExecutableVideoProductionGuide.model_validate(loads(row["guide_json"]))

    def list_video_production_guides(
        self,
        project_id: str,
        production_package_id: str | None = None,
        prompt_package_id: str | None = None,
    ) -> list[ExecutableVideoProductionGuide]:
        query = (
            "SELECT guide_json FROM narrative_video_production_guides "
            "WHERE project_id = ?"
        )
        params: list[Any] = [project_id]
        if production_package_id is not None:
            query += " AND production_package_id = ?"
            params.append(production_package_id)
        if prompt_package_id is not None:
            query += " AND prompt_package_id = ?"
            params.append(prompt_package_id)
        rows = self.db.conn.execute(query + " ORDER BY created_at DESC", tuple(params)).fetchall()
        return [
            ExecutableVideoProductionGuide.model_validate(loads(row["guide_json"]))
            for row in rows
        ]

    def mark_video_production_guides_stale(
        self,
        project_id: str,
        episode_number: int,
        *,
        guide_id: str | None = None,
        prompt_package_id: str | None = None,
    ) -> int:
        """Flag video production guides of one episode as stale.

        The stale flag lives both in the dedicated column and inside
        guide_json; every read path hydrates from guide_json, so both copies
        must be updated together (mirrors mark_model_prompt_packages_stale).
        Narrow the sweep with ``guide_id`` or ``prompt_package_id``.
        """
        conditions = ["project_id = ?", "episode_number = ?"]
        params: list[Any] = [project_id, episode_number]
        if guide_id is not None:
            conditions.append("id = ?")
            params.append(guide_id)
        if prompt_package_id is not None:
            conditions.append("prompt_package_id = ?")
            params.append(prompt_package_id)
        return self._mark_video_production_guides_stale_where(conditions, params)

    def mark_video_production_guides_stale_for_production(
        self, project_id: str, production_package_id: str
    ) -> int:
        """Flag guides built on prompt packages derived from one production
        master as stale.

        Like mark_video_production_guides_stale, the JSON copy is updated
        alongside the column because readers hydrate from guide_json.
        """
        return self._mark_video_production_guides_stale_where(
            ["project_id = ?", "production_package_id = ?"],
            [project_id, production_package_id],
        )

    def mark_video_production_guides_stale_for_prompt_package(
        self, project_id: str, prompt_package_id: str
    ) -> int:
        """Flag every guide built on one prompt package as stale."""
        return self._mark_video_production_guides_stale_where(
            ["project_id = ?", "prompt_package_id = ?"],
            [project_id, prompt_package_id],
        )

    def _mark_video_production_guides_stale_where(
        self, conditions: list[str], params: list[Any]
    ) -> int:
        standalone = not self.db.conn.in_transaction
        where = " AND ".join(conditions)
        rows = self.db.conn.execute(
            "SELECT id, guide_json FROM narrative_video_production_guides "
            f"WHERE {where}",
            tuple(params),
        ).fetchall()
        updated = 0
        for row in rows:
            guide = ExecutableVideoProductionGuide.model_validate(loads(row["guide_json"]))
            if guide.stale:
                continue
            guide.stale = True
            self.db.conn.execute(
                "UPDATE narrative_video_production_guides SET stale = 1, "
                "guide_json = ?, updated_at = ? WHERE id = ?",
                (dumps(guide.model_dump(mode="json")), _now(), row["id"]),
            )
            updated += 1
        if standalone:
            self.db.conn.commit()
        return updated
