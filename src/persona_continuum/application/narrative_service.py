"""NarrativeService — application facade for the Narrative Studio.

Control layer over Parallel World (simulation), Persona (characters), and
Room (interaction). Reuses RuntimePool, Compiled Context, Context Budget,
the Recall Gate, Agent Activity Contracts, Timeout Policy, and the Job
patterns; it never spawns a second agent runtime and never pins a physical
runtime to a long-lived narrative session.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import ValidationError

from persona_continuum.application._utils import dumps, loads, new_id
from persona_continuum.application.job_control import JobControlRegistry
from persona_continuum.application.job_progress import JobProgress, WorkerState, progress_now
from persona_continuum.domain.narrative import (
    AudienceKnowledgeEntry,
    AudienceState,
    AuditFinding,
    AuditSeverity,
    Beat,
    CanonEntry,
    CanonEntryType,
    CharacterArc,
    CharacterKnowledgeEntry,
    EpisodePlan,
    EpisodeStatus,
    EpisodeSummary,
    EpisodeVersion,
    ExecutableVideoProductionGuide,
    ForecastDirection,
    ForecastStatus,
    GenerationClip,
    GenerationMode,
    KnowledgeState,
    ModelPromptPackage,
    NarrativeAuditReport,
    NarrativeCharacter,
    NarrativeCharacterBinding,
    NarrativeClue,
    NarrativeForecast,
    NarrativeProject,
    NarrativeScene,
    PlotThread,
    PlotThreadStatus,
    ProductionAsset,
    ProductionGuideAsset,
    ProductionPackage,
    SceneParticipant,
    SceneStatus,
    Shot,
    StoryBible,
    StoryFact,
    VideoModelProfile,
    WriterRoomSynthesis,
)
from persona_continuum.domain.persona import PersonaType, RunMode
from persona_continuum.narrative.clip_planner import plan_clips
from persona_continuum.narrative.context_builder import NarrativeContextBuilder
from persona_continuum.narrative.continuity_auditor import NarrativeContinuityAuditor
from persona_continuum.narrative.knowledge_firewall import NarrativeKnowledgeFirewall
from persona_continuum.narrative.production import ProductionPackageBuilder
from persona_continuum.narrative.repository import NarrativeRepository
from persona_continuum.narrative.runtime import (
    NARRATIVE_AGENT_GENERATION_FAILED,
    NARRATIVE_AGENT_RUNTIME_UNAVAILABLE,
    NARRATIVE_AGENT_STRUCTURED_OUTPUT_INVALID,
    NARRATIVE_AGENT_TIMEOUT,
    NARRATIVE_PRODUCTION_CANON_REQUIRED,
    SHOOTING_SOURCE_PREVIEW_NOT_ALLOWED,
    VIDEO_GUIDE_SOURCE_NOT_READY,
    NarrativeAgentError,
    new_runtime_trace,
    normalize_runtime,
    resolve_generation_mode,
)
from persona_continuum.narrative.screenwriter import ScreenwriterStage
from persona_continuum.narrative.video_profile_registry import (
    get_profile,
    profile_capabilities_digest,
)
from persona_continuum.narrative.video_prompt_compiler import (
    compile_prompt_package,
    validate_clip_against_profile,
)
from persona_continuum.narrative.writer_room import (
    build_writer_room_protocol_config,
    writer_room_shared_context,
)

NARRATIVE_JOB_KINDS = (
    "story_bible",
    "outline",
    "forecast",
    "simulation",
    "writer_room",
    "screenwriter",
    "audit",
    "episode_pipeline",
    "production_package",
    "model_prompt_package",
    "video_production_guide",
    "complete_video_production",
)

OUTLINE_CHUNK_SIZE = 10

# Model prompt package pipeline (Task B). Stage labels for job progress plus
# the checkpoint statuses from which an in-flight package can resume.
MODEL_PROMPT_PACKAGE_STAGE_LABELS = {
    "loading_source": "加载 Canon 生产包",
    "planning_clips": "规划生成片段",
    "planning_assets": "整理资产需求",
    "compiling_prompts": "编译模型提示词",
    "validating": "整包一致性校验",
}
MODEL_PROMPT_PACKAGE_RESUMABLE_STATUSES = frozenset({"clip_planned", "compiling", "ready"})
MODEL_PROMPT_REFINEMENT_BATCH_SIZE = 4

# Executable video production guide pipeline (Task C). Stage labels for job
# progress plus the checkpoint statuses from which an in-flight guide can
# resume.
GUIDE_STAGE_LABELS = {
    "loading_source": "正在读取制作源...",
    "analyzing_assets": "正在分析参考素材...",
    "compiling_asset_prompts": "正在编译素材图片 Prompt...",
    "compiling_clip_prompts": "正在编译完整视频 Prompt...",
    "subtitle_sound": "正在生成字幕与声音方案...",
    "bgm_editing": "正在生成 BGM 与剪辑方案...",
    "rendering_guide": "正在渲染制作手册...",
}
GUIDE_RESUMABLE_STATUSES = frozenset({"drafting", "compiling"})
GUIDE_REFINEMENT_BATCH_SIZE = 4

# One-click complete video production pipeline (task #6/#7): clip plan +
# prompt package + production guide as ONE operation, reported to the user
# in creator language (never model_prompt_package / clip_fingerprint jargon).
COMPLETE_VIDEO_PRODUCTION_STAGE_LABELS = {
    "analyzing_episode": "正在分析本集所需素材",
    "planning_clips": "正在规划视频片段",
    "matching_model": "正在匹配目标视频模型",
    "character_references": "正在生成角色参考图方案",
    "scene_references": "正在生成场景参考图方案",
    "video_prompts": "正在生成完整视频 Prompt",
    "frame_chain": "正在规划尾帧接续",
    "subtitle_sound": "正在生成字幕与声音方案",
    "bgm_editing": "正在生成 BGM 与剪辑方案",
    "final_document": "正在整理完整制作文档",
}
# Inner pipeline stage -> user-facing complete-plan stage. Both phases reuse
# the same labels so the progress bar reads as ONE continuous workflow.
_COMPLETE_PLAN_STAGE_MAP_PACKAGE = {
    "loading_source": "analyzing_episode",
    "planning_clips": "planning_clips",
    "planning_assets": "character_references",
    "compiling_prompts": "video_prompts",
    "validating": "matching_model",
}
_COMPLETE_PLAN_STAGE_MAP_GUIDE = {
    "loading_source": "frame_chain",
    "analyzing_assets": "character_references",
    "compiling_asset_prompts": "scene_references",
    "compiling_clip_prompts": "video_prompts",
    "subtitle_sound": "subtitle_sound",
    "bgm_editing": "bgm_editing",
    "rendering_guide": "final_document",
}

# Whitelist of episode-plan fields a Director Agent may patch. Identity,
# trace, and bookkeeping fields are immutable (spec: patch_episode_plan).
EPISODE_PLAN_PATCHABLE_FIELDS = frozenset(
    {
        "title",
        "narrative_goal",
        "hook",
        "beats",
        "must_happen",
        "must_not_happen",
        "required_characters",
        "required_clues",
        "plot_threads",
        "clues_to_plant",
        "clues_to_echo",
        "reveal_targets",
        "forbidden_reveals",
        "relationship_targets",
        "emotion_targets",
        "world_state_targets",
        "cliffhanger",
        "estimated_duration_seconds",
    }
)
# User-message payload budget for ARGV CLIs such as Gemini/agy ``-p``.
# Leaves room for the headless policy, system prompt, and JSON schema.
OUTLINE_AUDIT_PAYLOAD_BUDGET_BYTES = 36_000


def _retryable_structured_exception(exc: BaseException, retryable_names: set[str]) -> bool:
    """Retry transport flakes, including CLI exit-without-output."""

    if type(exc).__name__ in {
        "PromptTransportLimitExceededError",
        "ContextBudgetExceededError",
    }:
        return False
    if type(exc).__name__ in retryable_names:
        return True
    return bool(getattr(exc, "retriable", False))


class NarrativeJobError(Exception):
    pass


class NarrativeService:
    def __init__(self, continuum: Any, repository: NarrativeRepository) -> None:
        self.continuum = continuum
        self.repo = repository
        self.firewall = NarrativeKnowledgeFirewall()
        self.context_builder = NarrativeContextBuilder()
        self.auditor = NarrativeContinuityAuditor()
        self.screenwriter = ScreenwriterStage()
        self.production_builder = ProductionPackageBuilder()
        self.job_control = JobControlRegistry(continuum.database)
        self._jobs: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._job_event_queues: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}
        self._runtime_traces: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------
    def create_project(self, **kwargs: Any) -> NarrativeProject:
        project = NarrativeProject(**kwargs)
        self.repo.save_project(project)
        return project

    def get_project(self, project_id: str) -> NarrativeProject:
        project = self.repo.get_project(project_id)
        if not project:
            raise KeyError(f"Narrative project not found: {project_id}")
        return project

    def list_projects(self) -> list[NarrativeProject]:
        return self.repo.list_projects()

    def update_project(self, project_id: str, updates: dict[str, Any]) -> NarrativeProject:
        project = self.get_project(project_id)
        data = project.model_dump(mode="json")
        allowed = {
            "title",
            "logline",
            "description",
            "format",
            "genre",
            "tone",
            "target_audience",
            "planned_episode_count",
            "episode_duration_seconds_min",
            "episode_duration_seconds_max",
            "story_world_id",
            "status",
            "runtime_assignment",
        }
        for key, value in updates.items():
            if key in allowed:
                data[key] = value
        updated = NarrativeProject.model_validate(data)
        updated.updated_at = datetime.now(UTC)
        self.repo.save_project(updated)
        return updated

    def delete_project(self, project_id: str) -> bool:
        return self.repo.delete_project(project_id)

    # ------------------------------------------------------------------
    # Story Bible
    # ------------------------------------------------------------------
    def get_bible(self, project_id: str, version: int | None = None) -> StoryBible | None:
        return self.repo.get_bible(project_id, version)

    def list_bible_versions(self, project_id: str) -> list[dict[str, Any]]:
        return self.repo.list_bible_versions(project_id)

    def save_bible(
        self, project_id: str, bible_updates: dict[str, Any], *, new_version: bool = True
    ) -> StoryBible:
        """Persist a bible edit. Existing versions are never overwritten:
        each edit creates the next version and marks dependent artifacts stale."""
        project = self.get_project(project_id)
        current = self.repo.get_bible(project_id)
        if current is None:
            base = StoryBible(project_id=project_id, version=1)
        else:
            base_data = current.model_dump(mode="json")
            if new_version:
                base = StoryBible.model_validate(base_data)
                base.version = current.version + 1
                base.id = new_id("bible")
                base.created_at = datetime.now(UTC)
            else:
                base = StoryBible.model_validate(base_data)
        data = base.model_dump(mode="json")
        locked = set(data.get("locked_fields") or [])
        for key, value in bible_updates.items():
            if key in locked and key != "locked_fields":
                raise ValueError(f"Story bible field is locked: {key}")
            data[key] = value
        bible = StoryBible.model_validate(data)
        bible.updated_at = datetime.now(UTC)
        self.repo.save_bible_version(bible)
        project.story_bible_version = bible.version
        project.revision += 1
        self.repo.save_project(project)
        self._mark_dependent_artifacts_stale(project)
        return bible

    def generate_story_bible_sync(
        self,
        project_id: str,
        runtime: dict[str, Any] | None = None,
        generation_mode: GenerationMode | str | None = None,
    ) -> StoryBible:
        return cast(
            StoryBible,
            self._run_sync(
                self.generate_story_bible(
                    project_id, runtime=runtime, generation_mode=generation_mode
                )
            ),
        )

    async def generate_story_bible(
        self,
        project_id: str,
        runtime: dict[str, Any] | None = None,
        generation_mode: GenerationMode | str | None = None,
    ) -> StoryBible:
        """Generate a bible in an explicit, fail-closed execution mode."""
        project = self.get_project(project_id)
        selected_runtime = self._resolve_stage_runtime(project, "story_architect", runtime)
        mode = resolve_generation_mode(generation_mode, selected_runtime)
        prompt = (
            f"Create a story bible JSON for this narrative project.\n"
            f"Title: {project.title}\nLogline: {project.logline}\n"
            f"Description: {project.description}\nFormat: {project.format.value}\n"
            f"Genre: {project.genre}\nTone: {project.tone}\n"
            "Return JSON with keys: premise, core_question, theme, world_rules, "
            "final_truth, characters (id/name/role/description), locations, master_timeline."
        )
        result, trace = await self._structured_call_async(
            prompt,
            runtime=selected_runtime,
            phase="story_bible",
            mode=mode,
            schema={
                "type": "object",
                "required": [
                    "premise",
                    "core_question",
                    "theme",
                    "world_rules",
                    "final_truth",
                    "characters",
                    "locations",
                    "master_timeline",
                ],
                "properties": {
                    "premise": {"type": "string"},
                    "core_question": {"type": "string"},
                    "theme": {"type": "string"},
                    "world_rules": {"type": "array", "items": {"type": "string"}},
                    "final_truth": {"type": "array", "items": {"type": "string"}},
                    "characters": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["id", "name", "role", "description"],
                            "properties": {
                                "id": {"type": "string"},
                                "name": {"type": "string"},
                                "role": {"type": "string"},
                                "description": {"type": "string"},
                            },
                        },
                    },
                    "locations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["id", "name", "description"],
                            "properties": {
                                "id": {"type": "string"},
                                "name": {"type": "string"},
                                "description": {"type": "string"},
                            },
                        },
                    },
                    "master_timeline": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["episode", "summary"],
                            "properties": {
                                "episode": {"type": "integer"},
                                "summary": {"type": "string"},
                                "timestamp": {"type": "string"},
                                "location": {"type": "string"},
                            },
                        },
                    },
                },
            },
            included_sections=["project", "format", "genre", "tone"],
        )
        bible_data = result if isinstance(result, dict) else {}
        try:
            bible = StoryBible(
                project_id=project_id,
                version=self._next_bible_version(project_id),
                premise=str(bible_data.get("premise") or project.logline),
                core_question=str(bible_data.get("core_question") or ""),
                theme=str(bible_data.get("theme") or ""),
                genre=list(project.genre),
                tone=list(project.tone),
                world_rules=[str(r) for r in bible_data.get("world_rules", [])],
                final_truth=[str(r) for r in bible_data.get("final_truth", [])],
                characters=self._parse_bible_characters(bible_data.get("characters", [])),
                locations=self._parse_bible_locations(bible_data.get("locations", [])),
                master_timeline=self._parse_master_timeline(bible_data.get("master_timeline", [])),
                generation_mode=mode,
                runtime_trace=trace,
            )
        except ValidationError as exc:
            raise NarrativeAgentError(
                NARRATIVE_AGENT_STRUCTURED_OUTPUT_INVALID,
                str(exc),
                stage="story_bible",
                runtime=selected_runtime,
                original_error=exc,
            ) from exc
        self.repo.save_bible_version(bible)
        project.story_bible_version = bible.version
        project.revision += 1
        self.repo.save_project(project)
        self._mark_dependent_artifacts_stale(project)
        return bible

    # ------------------------------------------------------------------
    # Characters, bindings, facts, knowledge
    # ------------------------------------------------------------------
    def add_character(self, project_id: str, **kwargs: Any) -> NarrativeCharacter:
        self.get_project(project_id)
        persona_id = str(kwargs.get("persona_id") or "").strip() or None
        world_actor_id = kwargs.get("world_actor_id")
        if persona_id:
            self._require_persona(persona_id)
            kwargs["persona_id"] = persona_id
            kwargs.setdefault("persona_origin", "existing_persona")
        character = NarrativeCharacter(project_id=project_id, **kwargs)
        conn = self.continuum.database.conn
        standalone = not conn.in_transaction
        if standalone and persona_id:
            conn.execute("BEGIN")
        try:
            self.repo.save_character(character)
            if persona_id:
                self.bind_character(
                    project_id,
                    character.id,
                    persona_id=persona_id,
                    world_actor_id=world_actor_id,
                )
            if standalone and persona_id:
                conn.commit()
        except Exception:
            if standalone and persona_id:
                conn.rollback()
            raise
        return self.repo.get_character(character.id) or character

    def list_characters(self, project_id: str) -> list[NarrativeCharacter]:
        return self.repo.list_characters(project_id)

    def _require_persona(self, persona_id: str) -> None:
        try:
            self.continuum.personas.get(persona_id)
        except Exception as exc:
            raise ValueError(f"Persona not found: {persona_id}") from exc

    def bind_character(
        self,
        project_id: str,
        character_id: str,
        persona_id: str | None = None,
        world_actor_id: str | None = None,
        *,
        unbind: bool = False,
    ) -> NarrativeCharacterBinding:
        project = self.get_project(project_id)
        character = self.repo.get_character(character_id)
        if not character or character.project_id != project.id:
            raise KeyError(f"Character not found in project: {character_id}")
        if unbind or persona_id == "":
            character.persona_id = None
            character.persona_origin = "fictional_author_defined"
            self.repo.save_character(character)
            self.repo.delete_bindings_for_character(project.id, character.id)
            return NarrativeCharacterBinding(
                project_id=project.id,
                story_character_id=character.id,
                persona_id=None,
                world_actor_id=character.world_actor_id,
            )
        if persona_id:
            self._require_persona(persona_id)
            character.persona_id = persona_id
            character.persona_origin = "existing_persona"
        character.world_actor_id = world_actor_id or character.world_actor_id
        self.repo.save_character(character)
        self.repo.delete_bindings_for_character(project.id, character.id)
        binding = NarrativeCharacterBinding(
            project_id=project.id,
            story_character_id=character.id,
            persona_id=character.persona_id,
            world_actor_id=character.world_actor_id,
        )
        self.repo.save_binding(binding)
        return binding

    def unbind_character(self, project_id: str, character_id: str) -> NarrativeCharacter:
        self.bind_character(project_id, character_id, unbind=True)
        character = self.repo.get_character(character_id)
        if not character:
            raise KeyError(f"Character not found in project: {character_id}")
        return character

    def create_missing_personas(self, project_id: str) -> list[NarrativeCharacter]:
        """Create fictional synthetic personas for characters without bindings.

        No internet research: persona content comes from the story bible /
        character design, provenance stays fictional_author_defined.
        """
        project = self.get_project(project_id)
        available = {persona.id for persona in self.continuum.personas.list()}
        created: list[NarrativeCharacter] = []
        for character in self.repo.list_characters(project_id):
            if character.persona_id and character.persona_id in available:
                continue
            persona_id = f"narrative_{character.id}"
            manifest = {
                "id": persona_id,
                "display_name": character.name,
                "persona_type": PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON.value,
                "run_mode": RunMode.COUNTERFACTUAL_CONTINUATION.value,
                "summary": character.description or character.role,
            }
            # Persona may already exist from a previous pass; that is fine.
            with contextlib.suppress(Exception):
                self.continuum.personas.create_from_manifest(manifest)
            self.bind_character(project_id, character.id, persona_id=persona_id)
            character.persona_id = persona_id
            created.append(character)
        project.revision += 1
        self.repo.save_project(project)
        return created

    def add_fact(
        self, project_id: str, text: str, *, secret: bool = True, category: str = "story_truth"
    ) -> StoryFact:
        self.get_project(project_id)
        fact = StoryFact(project_id=project_id, text=text, secret=secret, category=category)
        self.repo.save_fact(fact)
        return fact

    def list_facts(self, project_id: str) -> list[StoryFact]:
        return self.repo.list_facts(project_id)

    def set_character_knowledge(
        self,
        project_id: str,
        character_id: str,
        fact_id: str,
        state: str,
        *,
        learned_episode: int | None = None,
        confidence: float = 0.0,
        fact_text: str = "",
        notes: str = "",
    ) -> CharacterKnowledgeEntry:
        self.get_project(project_id)
        entry = CharacterKnowledgeEntry(
            project_id=project_id,
            character_id=character_id,
            fact_id=fact_id,
            fact_text=fact_text,
            state=KnowledgeState(state),
            confidence=confidence,
            learned_episode=learned_episode,
            notes=notes,
        )
        self.repo.save_knowledge_entry(entry)
        return entry

    def get_knowledge_matrix(
        self, project_id: str, episode_number: int | None = None
    ) -> dict[str, Any]:
        facts = self.repo.list_facts(project_id)
        knowledge = self.repo.list_knowledge(project_id, episode_number=episode_number)
        audience = self.repo.list_audience_knowledge(project_id, episode_number)
        characters = self.repo.list_characters(project_id)
        audience_by_fact = self.firewall.audience_view(
            facts, audience, episode_number=episode_number
        )
        kmap: dict[str, dict[str, str]] = {}
        for entry in knowledge:
            kmap.setdefault(entry.fact_id, {})[entry.character_id] = entry.state.value
        return {
            "facts": [
                {"id": f.id, "text": f.text, "secret": f.secret, "category": f.category}
                for f in facts
            ],
            "characters": [
                {"id": c.id, "name": c.name, "persona_id": c.persona_id} for c in characters
            ],
            "character_knowledge": kmap,
            "audience_knowledge": audience_by_fact,
        }

    def set_audience_knowledge(
        self,
        project_id: str,
        fact_id: str,
        state: str,
        *,
        revealed_episode: int | None = None,
        fact_text: str = "",
    ) -> AudienceKnowledgeEntry:
        self.get_project(project_id)
        entry = AudienceKnowledgeEntry(
            project_id=project_id,
            fact_id=fact_id,
            fact_text=fact_text,
            state=AudienceState(state),
            revealed_episode=revealed_episode,
        )
        self.repo.save_audience_entry(entry)
        return entry

    # ------------------------------------------------------------------
    # Plot threads / clues / arcs
    # ------------------------------------------------------------------
    def add_plot_thread(self, project_id: str, **kwargs: Any) -> PlotThread:
        self.get_project(project_id)
        thread = PlotThread(project_id=project_id, **kwargs)
        self.repo.save_plot_thread(thread)
        return thread

    def list_plot_threads(self, project_id: str) -> list[PlotThread]:
        return self.repo.list_plot_threads(project_id)

    def add_clue(self, project_id: str, **kwargs: Any) -> NarrativeClue:
        self.get_project(project_id)
        clue = NarrativeClue(project_id=project_id, **kwargs)
        self.repo.save_clue(clue)
        return clue

    def list_clues(self, project_id: str) -> list[NarrativeClue]:
        return self.repo.list_clues(project_id)

    def update_clue(self, clue: NarrativeClue) -> NarrativeClue:
        self.repo.save_clue(clue)
        return clue

    def add_arc(self, project_id: str, **kwargs: Any) -> CharacterArc:
        self.get_project(project_id)
        arc = CharacterArc(project_id=project_id, **kwargs)
        self.repo.save_arc(arc)
        return arc

    def list_arcs(self, project_id: str) -> list[CharacterArc]:
        return self.repo.list_arcs(project_id)

    # ------------------------------------------------------------------
    # Outline / episode plans
    # ------------------------------------------------------------------
    def generate_outline_sync(
        self,
        project_id: str,
        episode_count: int | None = None,
        runtime: dict[str, Any] | None = None,
        generation_mode: GenerationMode | str | None = None,
    ) -> list[EpisodePlan]:
        return cast(
            list[EpisodePlan],
            self._run_sync(
                self.generate_outline(
                    project_id,
                    episode_count,
                    runtime=runtime,
                    generation_mode=generation_mode,
                )
            ),
        )

    async def generate_outline(
        self,
        project_id: str,
        episode_count: int | None = None,
        runtime: dict[str, Any] | None = None,
        generation_mode: GenerationMode | str | None = None,
        progress: Callable[[str, str, int], None] | None = None,
    ) -> list[EpisodePlan]:
        project = self.get_project(project_id)
        count = episode_count or project.planned_episode_count
        if count < 1 or count > 120:
            raise ValueError("episode_count must be between 1 and 120")
        bible = self.repo.get_bible(project_id)
        if bible is None:
            raise ValueError("Story bible missing; generate it before the outline")
        outline_runtime = self._resolve_stage_runtime(project, "outline_writer", runtime)
        mode = resolve_generation_mode(generation_mode, outline_runtime)
        timeline = bible.master_timeline if bible else []
        plans: list[EpisodePlan] = []
        traces: list[dict[str, Any]] = []
        if mode == GenerationMode.DETERMINISTIC:
            for number in range(1, count + 1):
                timeline_item = next((t for t in timeline if t.episode == number), None)
                plan = EpisodePlan(
                    project_id=project_id,
                    episode_number=number,
                    title=(timeline_item.summary[:40] if timeline_item else f"Episode {number}"),
                    narrative_goal=timeline_item.summary if timeline_item else "",
                    estimated_duration_seconds=project.episode_duration_seconds_max,
                    status=EpisodeStatus.PLANNED,
                    generation_mode=mode,
                    runtime_trace={
                        "stage": "outline",
                        "generation_mode": mode.value,
                        "status": "completed",
                    },
                )
                plans.append(plan)
        else:
            architect_runtime = self._resolve_stage_runtime(project, "story_architect", runtime)
            self._report_outline_progress(
                progress, "outline_architecture", "正在设计季/弧结构", 6
            )
            architecture_prompt = (
                "Design the season/arc structure for a serialized narrative. "
                "Return compact JSON arcs only; do not write full episodes.\n"
                f"Episode count: {count}\nSource: {self._outline_source_brief(project, bible)}"
            )
            arc_result, arc_trace = await self._structured_call_async(
                architecture_prompt,
                runtime=architect_runtime,
                phase="outline_architecture",
                mode=GenerationMode.AGENT,
                schema={
                    "type": "object",
                    "required": ["arcs"],
                    "properties": {
                        "arcs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["start_episode", "end_episode", "goal"],
                                "properties": {
                                    "start_episode": {"type": "integer"},
                                    "end_episode": {"type": "integer"},
                                    "goal": {"type": "string"},
                                    "reversal": {"type": "string"},
                                    "ending_position": {"type": "string"},
                                },
                            },
                        }
                    },
                },
                included_sections=["project", "story_bible", "ending", "character_arcs"],
            )
            traces.append(arc_trace)
            arcs = list((arc_result or {}).get("arcs") or [])
            chunk_total = max(1, (count + OUTLINE_CHUNK_SIZE - 1) // OUTLINE_CHUNK_SIZE)
            for chunk_index, start in enumerate(
                range(1, count + 1, OUTLINE_CHUNK_SIZE)
            ):
                end = min(count, start + OUTLINE_CHUNK_SIZE - 1)
                self._report_outline_progress(
                    progress,
                    "outline_chunk",
                    f"正在写 EP{start}-EP{end}",
                    12 + int(58 * chunk_index / chunk_total),
                )
                chunk_prompt = (
                    "Write structured episode plans for exactly the requested inclusive range. "
                    "Every episode must advance causality and information control.\n"
                    f"Range: EP{start}-EP{end}\n"
                    f"Source: {self._outline_source_brief(project, bible)}\n"
                    f"Arc structure: {dumps(arcs)}\n"
                    "Return episode_plans with: episode_number, title, narrative_goal, hook, "
                    "beats, must_happen, must_not_happen, characters, plot_threads, "
                    "clues_to_plant, clues_to_echo, reveal_targets, forbidden_reveals, "
                    "relationship_targets, emotion_targets, world_state_targets, cliffhanger."
                )
                chunk_result, chunk_trace = await self._structured_call_async(
                    chunk_prompt,
                    runtime=outline_runtime,
                    phase="outline_chunk",
                    mode=GenerationMode.AGENT,
                    schema=self._outline_chunk_schema(),
                    included_sections=[
                        "story_bible",
                        "arc_structure",
                        f"episode_range_{start}_{end}",
                    ],
                    omitted_sections=["full_episode_screenplays"],
                )
                traces.append(chunk_trace)
                rows = list((chunk_result or {}).get("episode_plans") or [])
                expected = list(range(start, end + 1))
                actual = [int(row.get("episode_number", 0)) for row in rows]
                if actual != expected:
                    raise NarrativeAgentError(
                        NARRATIVE_AGENT_STRUCTURED_OUTPUT_INVALID,
                        f"Outline chunk returned episodes {actual}; expected {expected}",
                        stage="outline_chunk",
                        runtime=outline_runtime,
                    )
                plans.extend(
                    self._episode_plan_from_ai(project_id, project, row, chunk_trace)
                    for row in rows
                )
            audit_runtime = self._resolve_stage_runtime(project, "reviewer", runtime)
            audit_result, audit_traces = await self._agent_audit_outline(
                plans,
                runtime=audit_runtime,
                mode=mode,
                phase="outline_global_audit",
                progress=progress,
            )
            traces.extend(audit_traces)
            deterministic_issues = self._audit_outline(plans)
            blocking = deterministic_issues + list(
                (audit_result or {}).get("blocking_issues") or []
            )
            if blocking:
                plans, repair_traces = await self._agent_repair_outline(
                    project,
                    plans,
                    blocking,
                    runtime=outline_runtime,
                    mode=mode,
                    progress=progress,
                )
                traces.extend(repair_traces)
                final_audit, final_traces = await self._agent_audit_outline(
                    plans,
                    runtime=audit_runtime,
                    mode=mode,
                    phase="outline_global_reaudit",
                    progress=progress,
                )
                traces.extend(final_traces)
                structural = self._audit_outline(plans)
                remaining_agent = [
                    str(item)
                    for item in list((final_audit or {}).get("blocking_issues") or [])
                    if str(item).strip()
                ]
                remaining_warnings = [
                    str(item)
                    for item in list((final_audit or {}).get("warnings") or [])
                    if str(item).strip()
                ]
                if structural:
                    raise NarrativeAgentError(
                        NARRATIVE_AGENT_STRUCTURED_OUTPUT_INVALID,
                        "Outline structure invalid after repair: " + "; ".join(structural[:8]),
                        stage="outline_global_reaudit",
                        runtime=audit_runtime,
                    )
                # Plot-quality findings after one repair stay on the saved
                # outline as warnings. Discarding 60 episodes at 97% is worse
                # than letting the author inspect remaining continuity issues.
                audit_result = {
                    "blocking_issues": [],
                    "warnings": remaining_warnings + remaining_agent,
                    "unresolved_after_repair": remaining_agent,
                    "repaired": True,
                }
            for plan in plans:
                plan.runtime_trace = {"calls": traces, "global_audit": audit_result}

        for plan in plans:
            self.repo.save_episode_plan(plan)
        project.revision += 1
        self.repo.save_project(project)
        return plans

    def get_episode_plan(self, project_id: str, episode_number: int) -> EpisodePlan:
        plan = self.repo.get_episode_plan(project_id, episode_number)
        if not plan:
            raise KeyError(f"Episode plan not found: EP{episode_number}")
        return plan

    def list_episode_plans(self, project_id: str) -> list[EpisodePlan]:
        return self.repo.list_episode_plans(project_id)

    def save_episode_plan(self, plan: EpisodePlan) -> EpisodePlan:
        self.repo.save_episode_plan(plan)
        return plan

    # ------------------------------------------------------------------
    # Director-facing safe write actions
    # ------------------------------------------------------------------
    def patch_episode_plan(
        self,
        project_id: str,
        episode_number: int,
        patch: dict[str, Any],
        *,
        expected_project_revision: int,
        reason: str,
    ) -> EpisodePlan:
        """Agent-safe episode-plan patch with optimistic concurrency.

        Only whitelisted creative fields may change; identity and trace fields
        are immutable. A successful patch bumps ``project.revision`` so the
        existing fingerprint machinery marks dependent Forecast / Draft /
        Audit artifacts stale (nothing is deleted).
        """
        from persona_continuum.narrative.director import NarrativeDirectorStateConflict

        project = self.get_project(project_id)
        if int(expected_project_revision) != project.revision:
            raise NarrativeDirectorStateConflict(
                f"Project revision moved: expected {expected_project_revision}, "
                f"actual {project.revision}. Re-read state before writing."
            )
        plan = self.get_episode_plan(project_id, episode_number)
        data = plan.model_dump(mode="json")
        applied: list[str] = []
        for key, value in (patch or {}).items():
            if key not in EPISODE_PLAN_PATCHABLE_FIELDS:
                raise ValueError(f"Episode plan field is not patchable: {key}")
            data[key] = value
            applied.append(key)
        updated = EpisodePlan.model_validate(data)
        updated.updated_at = datetime.now(UTC)
        self.repo.save_episode_plan(updated)
        project.revision += 1
        self.repo.save_project(project)
        # Stale propagation reuses the fingerprint mechanism: forecasts and
        # non-canon draft versions that no longer match are flagged, not deleted.
        self._mark_dependent_artifacts_stale(project)
        updated.runtime_trace = {
            **updated.runtime_trace,
            "last_patch": {
                "fields": applied,
                "reason": reason,
                "project_revision": project.revision,
            },
        }
        self.repo.save_episode_plan(updated)
        return updated

    async def revise_episode_draft(
        self,
        project_id: str,
        episode_number: int,
        *,
        base_version_id: str,
        instructions: list[str],
        revision_mode: str = "medium",
        runtime: dict[str, Any] | None = None,
        reason: str = "",
        created_by: str | None = None,
    ) -> EpisodeVersion:
        """REVISE: produce a new immutable EpisodeVersion from an existing one.

        The Director never writes screenplays itself; this still goes through
        the Screenwriter stage with the base draft plus revision instructions.
        The base version is never modified or overwritten.
        """
        if revision_mode not in {"local", "medium", "rewrite"}:
            raise ValueError("revision_mode must be one of: local, medium, rewrite")
        if not instructions:
            raise ValueError("At least one revision instruction is required")
        project = self.get_project(project_id)
        plan = self.get_episode_plan(project_id, episode_number)
        base = self.get_episode_version(base_version_id)
        if base.project_id != project_id or base.episode_number != episode_number:
            raise ValueError("Base version does not belong to this episode")
        selected_runtime = self._resolve_stage_runtime(project, "screenwriter", runtime)
        mode = resolve_generation_mode(None, selected_runtime)
        if mode != GenerationMode.AGENT:
            raise NarrativeAgentError(
                NARRATIVE_AGENT_RUNTIME_UNAVAILABLE,
                "Draft revision requires an Agent (Screenwriter) runtime",
                stage="screenwriter",
                runtime=selected_runtime,
            )
        bible = self.repo.get_bible(project_id)
        if bible is None:
            raise ValueError("Story bible missing; generate it before revising an episode")
        summaries = [
            s
            for s in (
                self.repo.get_episode_summary(project_id, n)
                for n in range(max(1, episode_number - 5), episode_number)
            )
            if s
        ]
        future_plans = [
            p for p in self.repo.list_episode_plans(project_id) if p.episode_number > episode_number
        ][:3]
        writer_context = self.context_builder.build_writer_context(
            project,
            bible,
            plan,
            [e.text for e in self.repo.list_canon_entries(project_id)],
            self.repo.list_characters(project_id),
            self.repo.list_knowledge(project_id),
            self.repo.list_audience_knowledge(project_id),
            self.repo.list_plot_threads(project_id),
            self.repo.list_clues(project_id),
            self.repo.list_arcs(project_id),
            summaries,
            future_plans=future_plans,
        )
        writer_context["context_fingerprint"] = self.context_fingerprint(project)
        audits = [
            report
            for report in self.list_audits(project_id, episode_number)
            if report.episode_version_id == base.id
        ]
        latest_audit = audits[0] if audits else None
        prompt_payload = {
            "revision_task": {
                "mode": revision_mode,
                "instructions": list(instructions),
                "rules": (
                    "local: change ONLY what the instructions require; keep every other "
                    "scene, line, and beat verbatim. medium: apply instructions and make "
                    "minimal consistent adjustments elsewhere. rewrite: substantial rewrite "
                    "while preserving canon, knowledge boundaries, and the episode goal."
                ),
            },
            "base_version": {
                "id": base.id,
                "version": base.version,
                "title": base.title,
            },
            "base_structured_draft": base.structured_draft,
            "latest_audit_findings": (
                [
                    {
                        "severity": finding.severity.value,
                        "code": finding.code,
                        "message": finding.message,
                    }
                    for finding in latest_audit.findings
                ]
                if latest_audit
                else []
            ),
            "writer_context": writer_context,
        }
        draft_data, trace = await self._structured_call_async(
            "Revise the existing structured episode draft according to the revision "
            "instructions. Preserve canon, character knowledge boundaries, and audience "
            "knowledge. Return the requested JSON only.\n" + dumps(prompt_payload),
            runtime=selected_runtime,
            phase="screenwriter",
            mode=GenerationMode.AGENT,
            schema=self._screenwriter_schema(),
            included_sections=[
                "episode_plan",
                "canon_snapshot",
                "relevant_character_kernels",
                "knowledge_matrix",
                "active_plot_threads",
                "relevant_clues",
            ],
            omitted_sections=["full_season_screenplays", "author_notes"],
            structured_repair_attempts=2,
        )
        draft = self._render_structured_draft(draft_data)
        previous = self.repo.list_episode_versions(project_id, episode_number)
        version = EpisodeVersion(
            project_id=project_id,
            episode_number=episode_number,
            version=(previous[0].version + 1) if previous else 1,
            created_by=created_by or (selected_runtime.get("agent_id") or "reviser"),
            parent_version_id=base.id,
            revision_reason=reason or " | ".join(instructions),
            revision_instructions=[str(item) for item in instructions],
            revision_mode=revision_mode,
            title=str(draft_data.get("title") or base.title or plan.title),
            screenplay=draft["screenplay"],
            beat_sheet=[Beat(**b) for b in draft["beat_sheet"]],
            scene_ids=list(base.scene_ids),
            simulation_summary=draft["simulation_summary"],
            structured_draft=dict(draft_data or {}),
            generation_mode=GenerationMode.AGENT,
            runtime_trace=trace,
            context_fingerprint=self.context_fingerprint(project),
            story_bible_version=project.story_bible_version,
            project_revision=project.revision,
            canonical_branch_id=project.canonical_world_branch_id,
        )
        plan.status = EpisodeStatus.DRAFTED
        self.repo.save_episode_plan(plan)
        self.repo.save_episode_version(version)
        return version

    def revise_episode_draft_sync(
        self,
        project_id: str,
        episode_number: int,
        *,
        base_version_id: str,
        instructions: list[str],
        revision_mode: str = "medium",
        runtime: dict[str, Any] | None = None,
        reason: str = "",
        created_by: str | None = None,
    ) -> EpisodeVersion:
        return cast(
            EpisodeVersion,
            self._run_sync(
                self.revise_episode_draft(
                    project_id,
                    episode_number,
                    base_version_id=base_version_id,
                    instructions=instructions,
                    revision_mode=revision_mode,
                    runtime=runtime,
                    reason=reason,
                    created_by=created_by,
                )
            ),
        )

    # ------------------------------------------------------------------
    # Episode pipeline
    # ------------------------------------------------------------------
    def ensure_story_world(self, project_id: str) -> NarrativeProject:
        """Bind (creating if needed) the story world the project simulates in.

        The world is a regular Parallel World instance created from the story
        bible premise; story characters become explicit seed actors so the
        generic engine never injects a default roster. Idempotent: a project
        that already has both world and canonical branch IDs is returned as-is.
        """
        project = self.get_project(project_id)
        if project.story_world_id and project.canonical_world_branch_id:
            return project
        bible = self.repo.get_bible(project_id)
        if bible is None:
            raise ValueError("Story bible missing; generate it before simulating")
        characters = self.repo.list_characters(project_id)
        description = bible.premise or project.logline or project.title
        world, branch, _state = self.continuum.worlds.create_world(
            description=description,
            title=project.title,
            start_date=None,
            initial_actors=[c.id for c in characters],
            metadata={
                "initial_actors": [
                    {
                        "id": c.id,
                        "name": c.name,
                        "actor_type": "persona_actor",
                        "persona_id": c.persona_id,
                        "identity": {"role": c.role or "Story Character"},
                    }
                    for c in characters
                ]
            },
        )
        project.story_world_id = world.id
        project.canonical_world_branch_id = branch.id
        self.repo.save_project(project)
        return project

    def prepare_episode(self, project_id: str, episode_number: int) -> dict[str, Any]:
        """PREPARE: task-scoped context within the Context Budget discipline."""
        project = self.get_project(project_id)
        bible = self.repo.get_bible(project_id)
        if bible is None:
            raise ValueError("Story bible missing; generate it before preparing an episode")
        plan = self.get_episode_plan(project_id, episode_number)
        canon_snapshot = [e.text for e in self.repo.list_canon_entries(project_id)]
        summaries = [
            s
            for s in (
                self.repo.get_episode_summary(project_id, n)
                for n in range(max(1, episode_number - 5), episode_number)
            )
            if s
        ]
        future_plans = [
            p for p in self.repo.list_episode_plans(project_id) if p.episode_number > episode_number
        ][:3]
        context = self.context_builder.build_writer_context(
            project,
            bible,
            plan,
            canon_snapshot,
            self.repo.list_characters(project_id),
            self.repo.list_knowledge(project_id),
            self.repo.list_audience_knowledge(project_id),
            self.repo.list_plot_threads(project_id),
            self.repo.list_clues(project_id),
            self.repo.list_arcs(project_id),
            summaries,
            future_plans=future_plans,
        )
        plan.status = EpisodeStatus.PREPARED
        self.repo.save_episode_plan(plan)
        fingerprint = self.context_fingerprint(project)
        context["context_fingerprint"] = fingerprint
        return context

    def context_fingerprint(self, project: NarrativeProject) -> str:
        canon = self.repo.list_canon_entries(project.id)
        payload = dumps(
            {
                "revision": project.revision,
                "bible_version": project.story_bible_version,
                "branch": project.canonical_world_branch_id,
                "canon_tail": [e.text for e in canon][-20:],
            }
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def forecast_episode(
        self,
        project_id: str,
        episode_number: int,
        directions: list[dict[str, Any]],
        *,
        horizon_episodes: int = 5,
        runtime: dict[str, Any] | None = None,
        generation_mode: GenerationMode | str | None = None,
    ) -> NarrativeForecast:
        return cast(
            NarrativeForecast,
            self._run_sync(
                self.forecast_episode_async(
                    project_id,
                    episode_number,
                    directions,
                    horizon_episodes=horizon_episodes,
                    runtime=runtime,
                    generation_mode=generation_mode,
                )
            ),
        )

    async def forecast_episode_async(
        self,
        project_id: str,
        episode_number: int,
        directions: list[dict[str, Any]],
        *,
        horizon_episodes: int = 5,
        runtime: dict[str, Any] | None = None,
        generation_mode: GenerationMode | str | None = None,
        progress: Callable[[str, str, int], None] | None = None,
    ) -> NarrativeForecast:
        """Run bounded, isolated multi-episode simulations for each direction."""
        project = self.ensure_story_world(project_id)
        if not directions:
            raise ValueError("At least one forecast direction is required")
        if len(directions) > 5:
            raise ValueError("Forecast supports at most 5 branches")
        if horizon_episodes < 1 or horizon_episodes > 5:
            raise ValueError("horizon_episodes must be between 1 and 5")
        selected_runtime = self._resolve_stage_runtime(project, "forecast_simulator", runtime)
        mode = resolve_generation_mode(generation_mode, selected_runtime)
        if mode == GenerationMode.AGENT:
            self.create_missing_personas(project_id)
        forecast = NarrativeForecast(
            project_id=project_id,
            episode_number=episode_number,
            question=f"Direction for EP{episode_number}",
            horizon_episodes=horizon_episodes,
            context_fingerprint=self.context_fingerprint(project),
            story_bible_version=project.story_bible_version,
            project_revision=project.revision,
            canonical_branch_id=project.canonical_world_branch_id,
            generation_mode=mode,
            budget={
                "max_branches": 5,
                "max_horizon": 5,
                "max_actor_turns_per_scene": 2,
                "max_scenes_per_episode": 1,
            },
        )
        characters = self.repo.list_characters(project_id)
        plan_lookup = {p.episode_number: p for p in self.repo.list_episode_plans(project_id)}
        direction_count = max(1, len(directions))
        total_steps = max(1, direction_count * horizon_episodes)
        completed_steps = 0
        for spec in directions:
            label = str(spec.get("label", f"option_{len(forecast.directions) + 1}"))
            branch = None
            with contextlib.suppress(Exception):
                branch = self.continuum.worlds.fork_branch(
                    world_id=project.story_world_id,
                    source_branch_id=project.canonical_world_branch_id,
                    new_name=f"EP{episode_number}_{label}".replace(" ", "_")[:60],
                )
            if branch is None:
                raise RuntimeError(f"Unable to fork isolated forecast branch: {label}")
            direction = ForecastDirection(
                label=label,
                description=str(spec.get("description", "")),
                world_branch_id=branch.id,
                beats=[Beat(**b) for b in spec.get("beats", [])],
            )
            for offset in range(horizon_episodes):
                forecast_episode_number = episode_number + offset
                self._report_outline_progress(
                    progress,
                    "forecast",
                    f"正在模拟 {label} · EP{forecast_episode_number}",
                    8 + int(72 * completed_steps / total_steps),
                )
                plan = plan_lookup.get(forecast_episode_number)
                episode_goal = (
                    plan.narrative_goal if plan is not None else f"Advance {label} causally"
                )
                if mode == GenerationMode.AGENT:
                    required = {
                        str(value).strip().casefold()
                        for value in (plan.required_characters if plan else [])
                    }
                    selected_characters = [
                        c
                        for c in characters
                        if plan is None
                        or not required
                        or c.id.casefold() in required
                        or c.name.strip().casefold() in required
                    ][:6]
                    # Imported/generated outlines may use aliases that have not yet
                    # been rebound to durable character ids. Do not turn that
                    # authoring mismatch into an empty runtime cast.
                    if not selected_characters:
                        selected_characters = characters[:6]
                    if not selected_characters:
                        raise ValueError("Forecast Agent mode requires bound story characters")
                    scene = self.create_scene(
                        project_id,
                        {
                            "episode_number": forecast_episode_number,
                            "order": 1,
                            "location": "forecast branch",
                            "scene_goal": (
                                f"NON-CANON forecast option {label}: {direction.description}. "
                                f"Episode goal: {episode_goal}"
                            ),
                            "participants": [
                                {
                                    "character_id": character.id,
                                    "name": character.name,
                                    "goal": f"Choose a persona-consistent response to {label}",
                                }
                                for character in selected_characters
                            ],
                        },
                    )
                    simulated = await self.simulate_scene(
                        project_id,
                        scene,
                        branch_id=branch.id,
                        max_turns=min(2, max(1, len(selected_characters))),
                        runtime=selected_runtime,
                        generation_mode=GenerationMode.AGENT,
                    )
                    step = self._forecast_step_from_scene(
                        forecast_episode_number, simulated, direction.description
                    )
                    forecast.runtime_trace.append(dict(simulated.runtime_trace))
                else:
                    step = self._deterministic_forecast_step(
                        forecast_episode_number, direction, plan
                    )
                direction.steps.append(step)
                completed_steps += 1

            direction.summary = " | ".join(
                str(step.get("summary") or "") for step in direction.steps
            )[:4000]
            direction.beats = [
                Beat(
                    order=index,
                    title=f"EP{step['episode_number']}",
                    description=str(step.get("summary") or ""),
                )
                for index, step in enumerate(direction.steps, 1)
            ]
            direction.evaluation = self._deterministic_forecast_evaluation(direction)
            if mode == GenerationMode.AGENT:
                reviewer_runtime = self._resolve_stage_runtime(project, "reviewer", runtime)
                self._report_outline_progress(
                    progress,
                    "forecast_evaluation",
                    f"正在评估 {label}",
                    82 + int(14 * (completed_steps / total_steps)),
                )
                compact = self._compact_forecast_direction_for_eval(direction)
                try:
                    ai_eval, eval_trace = await self._structured_call_async(
                        "Evaluate this NON-CANON narrative branch. Scores are narrative "
                        "evaluation, not success probability. Return 0..1 scores and risks.\n"
                        + dumps(compact),
                        runtime=reviewer_runtime,
                        phase="forecast_evaluation",
                        mode=GenerationMode.AGENT,
                        schema=self._forecast_evaluation_schema(),
                        included_sections=[
                            "forecast_steps",
                            "author_intent",
                            "production_constraints",
                        ],
                    )
                    direction.evaluation.update(
                        {
                            key: float(value)
                            for key, value in dict((ai_eval or {}).get("scores") or {}).items()
                        }
                    )
                    direction.scores = dict(direction.evaluation)
                    direction.risks = [str(item) for item in (ai_eval or {}).get("risks", [])]
                    forecast.runtime_trace.append(eval_trace)
                except NarrativeAgentError as exc:
                    if exc.original_error_class not in {
                        "PromptTransportLimitExceededError",
                        "ContextBudgetExceededError",
                    } and "PROMPT_TRANSPORT_LIMIT_EXCEEDED" not in str(exc):
                        raise
                    direction.risks = [
                        "Evaluation used local scores because the CLI prompt transport "
                        "budget was exceeded."
                    ]
                    direction.scores = dict(direction.evaluation)
                    forecast.runtime_trace.append(
                        {
                            "stage": "forecast_evaluation",
                            "status": "skipped_transport_limit",
                            "failure": exc.to_dict(),
                        }
                    )
            else:
                direction.scores = dict(direction.evaluation)
            forecast.directions.append(direction)
        forecast.status = ForecastStatus.COMPLETED
        forecast.updated_at = datetime.now(UTC)
        self.repo.save_forecast(forecast)
        return forecast

    def get_forecast(self, forecast_id: str) -> NarrativeForecast:
        forecast = self.repo.get_forecast(forecast_id)
        if not forecast:
            raise KeyError(f"Forecast not found: {forecast_id}")
        project = self.get_project(forecast.project_id)
        if (
            forecast.context_fingerprint
            and forecast.context_fingerprint != self.context_fingerprint(project)
        ):
            forecast.stale = True
        return forecast

    def list_forecasts(self, project_id: str) -> list[NarrativeForecast]:
        return self.repo.list_forecasts(project_id)

    async def generate_forecast_directions(
        self,
        project_id: str,
        episode_number: int,
        *,
        count: int = 3,
        runtime: dict[str, Any] | None = None,
        generation_mode: GenerationMode | str | None = None,
    ) -> dict[str, Any]:
        project = self.get_project(project_id)
        plan = self.get_episode_plan(project_id, episode_number)
        selected_runtime = self._resolve_stage_runtime(project, "forecast_simulator", runtime)
        mode = resolve_generation_mode(generation_mode, selected_runtime)
        count = max(1, min(5, int(count)))
        if mode == GenerationMode.DETERMINISTIC:
            directions = [
                {
                    "label": chr(65 + index),
                    "description": f"Offline variation {index + 1}: {plan.narrative_goal}",
                    "beats": [],
                }
                for index in range(count)
            ]
            return {"generation_mode": mode.value, "directions": directions}
        result, trace = await self._structured_call_async(
            "Generate distinct, plausible NON-CANON directions for this episode. "
            "Each must create different character decisions and causal consequences.\n"
            + dumps(self.prepare_episode(project_id, episode_number)),
            runtime=selected_runtime,
            phase="forecast_direction_generation",
            mode=mode,
            schema={
                "type": "object",
                "required": ["directions"],
                "properties": {
                    "directions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["label", "description", "beats"],
                            "properties": {
                                "label": {"type": "string"},
                                "description": {"type": "string"},
                                "beats": {"type": "array", "items": {"type": "object"}},
                            },
                        },
                    }
                },
            },
            included_sections=["episode_plan", "canon", "knowledge", "clues", "arcs"],
        )
        directions = list(result.get("directions") or [])[:count]
        if not directions:
            raise NarrativeAgentError(
                NARRATIVE_AGENT_STRUCTURED_OUTPUT_INVALID,
                "Forecast direction generation returned no candidates",
                stage="forecast_direction_generation",
                runtime=selected_runtime,
            )
        return {
            "generation_mode": mode.value,
            "directions": directions,
            "runtime_trace": trace,
        }

    def select_forecast_direction(self, forecast_id: str, direction_id: str) -> NarrativeForecast:
        forecast = self.get_forecast(forecast_id)
        if not any(d.id == direction_id for d in forecast.directions):
            raise KeyError(f"Direction not found: {direction_id}")
        forecast.selected_direction_id = direction_id
        forecast.updated_at = datetime.now(UTC)
        self.repo.save_forecast(forecast)
        return forecast

    async def simulate_scene(
        self,
        project_id: str,
        scene: NarrativeScene,
        *,
        branch_id: str | None = None,
        max_turns: int = 2,
        runtime: dict[str, Any] | None = None,
        generation_mode: GenerationMode | str | None = None,
    ) -> NarrativeScene:
        """SIMULATE: let Persona actors play the scene in the story world.

        Uses the existing World SceneEngine (Room runtime underneath), so the
        Recall Gate, RuntimePool, and Context Budget all apply unchanged. The
        knowledge firewall injects per-character context via scene metadata.
        """
        project = self.ensure_story_world(project_id)
        branch = branch_id or project.canonical_world_branch_id
        if not branch:
            raise ValueError("Project has no canonical world branch")

        selected_runtime = self._resolve_stage_runtime(project, "scene_actor", runtime)
        mode = resolve_generation_mode(generation_mode, selected_runtime)
        scene.generation_mode = mode
        if mode == GenerationMode.DETERMINISTIC:
            scene.status = SceneStatus.COMPLETED
            scene.world_branch_id = branch
            scene.summary = self._deterministic_scene_summary(scene)
            scene.dialogue = [{"speaker": "narrator", "text": scene.summary}]
            scene.relationship_delta = self._heuristic_relationship_delta(scene)
            scene.runtime_trace = {
                "stage": "scene_actor",
                "generation_mode": mode.value,
                "status": "completed",
                "agent_calls": 0,
            }
            self.repo.save_scene(scene)
            return scene

        self.create_missing_personas(project_id)
        world_engine = self.continuum.worlds.engine
        world_actors = world_engine.repo.list_actor_states(project.story_world_id, branch)
        actors_by_id = {a.id: a for a in world_actors}
        actors = []
        participant_contexts: dict[str, dict[str, Any]] = {}
        character_by_slot: dict[str, NarrativeCharacter] = {}
        prompt_leak_scan: dict[str, list[str]] = {}
        bible = self.repo.get_bible(project_id)
        if bible is None:
            raise ValueError("Story bible missing")
        facts = self.repo.list_facts(project_id)
        knowledge = self.repo.list_knowledge(project_id, episode_number=scene.episode_number)
        audience = self.repo.list_audience_knowledge(project_id, scene.episode_number)
        for participant in scene.participants:
            character = self.repo.get_character(participant.character_id)
            if character is None:
                raise KeyError(f"Narrative character not found: {participant.character_id}")
            actor = actors_by_id.get(participant.character_id)
            if actor is None:
                actor = self._ensure_world_actor(
                    project, character, character.persona_id if character else None, branch
                )
            actor = self._actor_with_bound_persona(actor, character, selected_runtime)
            actors.append(actor)

            safe_view = self.firewall.build_character_context(
                character,
                bible,
                facts,
                knowledge,
                audience,
                scene,
                episode_number=scene.episode_number,
            )
            actor_context = self.context_builder.build_scene_context(
                character,
                scene,
                safe_view["prompt_blocks"],
                world_state_digest=self._branch_state(branch).model_dump(mode="json"),
                memories=[
                    str(item.get("content") or item)
                    for item in list(getattr(actor, "memory", []) or [])[-8:]
                ],
                relationship_digest=dict(getattr(actor, "relationships", {}) or {}),
            )
            actor_context["blocked_fact_ids"] = safe_view["blocked_fact_ids"]
            slot_id = f"slot_{actor.id}"
            prompt_leaks = self.firewall.scan_for_leaks(
                dumps(actor_context),
                facts,
                knowledge,
                character.id,
                bible=bible,
            )
            if prompt_leaks:
                raise NarrativeAgentError(
                    NARRATIVE_AGENT_GENERATION_FAILED,
                    f"Knowledge firewall rejected actor prompt for {character.id}",
                    stage="scene_actor",
                    runtime=selected_runtime,
                )
            prompt_leak_scan[slot_id] = prompt_leaks
            participant_contexts[slot_id] = actor_context
            character_by_slot[slot_id] = character

        topic = self._redact_story_truth(
            scene.scene_goal or scene.conflict or "Narrative scene", bible, facts
        )
        scene.status = SceneStatus.RUNNING
        scene.world_branch_id = branch
        self.repo.save_scene(scene)

        started = time.monotonic()
        (
            summary,
            updated_state,
            updated_actors,
            world_event,
        ) = await world_engine.scene_engine.execute_scene(
            world_id=project.story_world_id,
            branch_id=branch,
            actors=actors,
            topic=topic,
            current_world_time=self._world_time(branch),
            state=self._branch_state(branch),
            max_turns=max_turns,
            participant_contexts=participant_contexts,
        )

        branch_record = world_engine.repo.get_branch(branch)
        if branch_record is not None:
            branch_record.current_state = updated_state
            world_engine.repo.save_branch(branch_record)
        world_engine.repo.save_event(project.story_world_id, branch, world_event)
        for updated_actor in updated_actors:
            world_engine.repo.save_actor_state(project.story_world_id, branch, updated_actor)

        scene.dialogue = [{"speaker": "scene", "text": summary}]
        scene.summary = summary
        scene.world_event_id = world_event.id
        scene.relationship_delta = self._heuristic_relationship_delta(scene)
        room_id = str(world_event.data.get("room_id") or "")
        scene.room_id = room_id or scene.room_id
        room = self.continuum.orchestrator.get_room(room_id) if room_id else None
        output_leak_scan: dict[str, list[str]] = {}
        for turn in (room.transcript if room else []) or []:
            speaker_id = str(turn.get("speaker_id") or "")
            character = character_by_slot.get(speaker_id)
            if character is None:
                continue
            leaks = self.firewall.scan_for_leaks(
                str(turn.get("content") or ""),
                facts,
                knowledge,
                character.id,
                bible=bible,
            )
            output_leak_scan[speaker_id] = sorted(set(output_leak_scan.get(speaker_id, []) + leaks))
        if any(output_leak_scan.values()):
            raise NarrativeAgentError(
                NARRATIVE_AGENT_GENERATION_FAILED,
                "Knowledge firewall rejected character output",
                stage="scene_actor",
                runtime=selected_runtime,
            )
        scene.runtime_trace = {
            "stage": "scene_actor",
            "generation_mode": mode.value,
            "agent": selected_runtime.get("agent_id"),
            "requested_model": selected_runtime.get("model_id"),
            "reasoning": selected_runtime.get("reasoning_effort"),
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "agent_calls": len((room.transcript if room else []) or []),
            "participant_contexts": {
                key: {
                    "blocked_fact_ids": value.get("blocked_fact_ids", []),
                    "prompt_chars": len(dumps(value)),
                }
                for key, value in participant_contexts.items()
            },
            "knowledge_firewall": {
                "prompt_leak_scan": prompt_leak_scan,
                "output_leak_scan": output_leak_scan,
                "passed": True,
            },
            "status": "completed",
        }
        scene.status = SceneStatus.COMPLETED
        scene.updated_at = datetime.now(UTC)
        self.repo.save_scene(scene)
        return scene

    def create_scene(self, project_id: str, scene_data: dict[str, Any]) -> NarrativeScene:
        self.get_project(project_id)
        participants = [SceneParticipant(**p) for p in scene_data.get("participants", [])]
        scene = NarrativeScene(
            project_id=project_id,
            episode_id=scene_data.get("episode_id"),
            episode_number=scene_data.get("episode_number"),
            order=scene_data.get("order", 0),
            location=scene_data.get("location", ""),
            time=scene_data.get("time", ""),
            participants=participants,
            scene_goal=scene_data.get("scene_goal", ""),
            conflict=scene_data.get("conflict", ""),
            entry_state=scene_data.get("entry_state", ""),
            exit_state=scene_data.get("exit_state", ""),
            must_happen=scene_data.get("must_happen", []),
            must_not_happen=scene_data.get("must_not_happen", []),
            knowledge_constraints=scene_data.get("knowledge_constraints", []),
            relationship_constraints=scene_data.get("relationship_constraints", []),
            estimated_duration_seconds=scene_data.get("estimated_duration_seconds", 60),
        )
        self.repo.save_scene(scene)
        return scene

    def list_scenes(
        self, project_id: str, episode_number: int | None = None
    ) -> list[NarrativeScene]:
        return self.repo.list_scenes(project_id, episode_number)

    def generate_episode_draft_sync(
        self,
        project_id: str,
        episode_number: int,
        *,
        runtime: dict[str, Any] | None = None,
        generation_mode: GenerationMode | str | None = None,
    ) -> EpisodeVersion:
        return cast(
            EpisodeVersion,
            self._run_sync(
                self.generate_episode_draft(
                    project_id,
                    episode_number,
                    runtime=runtime,
                    generation_mode=generation_mode,
                )
            ),
        )

    async def generate_episode_draft(
        self,
        project_id: str,
        episode_number: int,
        *,
        runtime: dict[str, Any] | None = None,
        generation_mode: GenerationMode | str | None = None,
    ) -> EpisodeVersion:
        """DRAFT: real Screenwriter Agent or explicitly selected offline utility."""
        project = self.get_project(project_id)
        plan = self.get_episode_plan(project_id, episode_number)
        selected_forecast = self._selected_forecast(project_id, episode_number)
        selected_branch = selected_forecast.world_branch_id if selected_forecast else None
        scenes = [
            scene
            for scene in self.repo.list_scenes(project_id, episode_number)
            if not selected_branch
            or scene.world_branch_id in {selected_branch, project.canonical_world_branch_id}
        ]
        simulation_summary = " | ".join(s.summary for s in scenes if s.summary)
        selected_runtime = self._resolve_stage_runtime(project, "screenwriter", runtime)
        mode = resolve_generation_mode(generation_mode, selected_runtime)
        synthesis = self.repo.get_writer_room_synthesis(project_id, episode_number)
        if mode == GenerationMode.AGENT:
            writer_context = self.prepare_episode(project_id, episode_number)
            prompt_payload = {
                "writer_context": writer_context,
                "selected_forecast": (
                    self._compact_forecast_for_writer(selected_forecast)
                    if selected_forecast
                    else None
                ),
                "simulation": [
                    {
                        "scene_id": scene.id,
                        "summary": scene.summary[:2400],
                        "decisions": scene.decisions[:8],
                        "relationship_delta": scene.relationship_delta[:8],
                    }
                    for scene in scenes[-4:]
                ],
                "writer_room_synthesis": (
                    self._compact_writer_room_for_writer(synthesis) if synthesis else None
                ),
            }
            draft_data, trace = await self._structured_call_async(
                "Write a production-ready structured episode screenplay. Do not add canon, "
                "world rules, relatives, powers, or secrets absent from the supplied context. "
                "Return the requested JSON only.\n" + dumps(prompt_payload),
                runtime=selected_runtime,
                phase="screenwriter",
                mode=GenerationMode.AGENT,
                schema=self._screenwriter_schema(),
                included_sections=[
                    "episode_plan",
                    "canon_snapshot",
                    "relevant_character_kernels",
                    "knowledge_matrix",
                    "active_plot_threads",
                    "relevant_clues",
                    "simulation",
                    "writer_room_synthesis",
                    "recent_episode_summaries",
                ],
                omitted_sections=["full_season_screenplays", "author_notes"],
                structured_repair_attempts=2,
            )
            draft = self._render_structured_draft(draft_data)
            structured_draft = dict(draft_data or {})
        else:
            draft = self.screenwriter.draft_from_simulation(
                plan,
                scenes,
                simulation_summary or plan.narrative_goal,
                fmt=project.format,
                title=plan.title,
                duration_seconds=plan.estimated_duration_seconds,
            )
            structured_draft = {
                "episode_number": episode_number,
                "title": plan.title,
                "duration": plan.estimated_duration_seconds,
                "hook": plan.hook,
                "scenes": [scene.model_dump(mode="json") for scene in scenes],
                "cliffhanger": plan.cliffhanger,
            }
            trace = {
                "stage": "screenwriter",
                "generation_mode": mode.value,
                "status": "completed",
                "agent_calls": 0,
            }
        previous = self.repo.list_episode_versions(project_id, episode_number)
        version = EpisodeVersion(
            project_id=project_id,
            episode_number=episode_number,
            version=(previous[0].version + 1) if previous else 1,
            created_by=selected_runtime.get("agent_id") or "deterministic",
            title=str(structured_draft.get("title") or plan.title),
            screenplay=draft["screenplay"],
            beat_sheet=[Beat(**b) for b in draft["beat_sheet"]],
            scene_ids=[s.id for s in scenes],
            simulation_summary=draft["simulation_summary"],
            structured_draft=structured_draft,
            writer_room_synthesis=(
                synthesis.model_dump(mode="json") if synthesis is not None else {}
            ),
            generation_mode=mode,
            runtime_trace=trace,
            context_fingerprint=self.context_fingerprint(project),
            story_bible_version=project.story_bible_version,
            project_revision=project.revision,
            canonical_branch_id=project.canonical_world_branch_id,
        )
        plan.status = EpisodeStatus.DRAFTED
        self.repo.save_episode_plan(plan)
        self.repo.save_episode_version(version)
        return version

    def get_episode_versions(self, project_id: str, episode_number: int) -> list[EpisodeVersion]:
        return self.repo.list_episode_versions(project_id, episode_number)

    def get_episode_version(self, version_id: str) -> EpisodeVersion:
        version = self.repo.get_episode_version(version_id)
        if not version:
            raise KeyError(f"Episode version not found: {version_id}")
        return version

    def audit_episode(
        self,
        project_id: str,
        version_id: str,
        *,
        runtime: dict[str, Any] | None = None,
        generation_mode: GenerationMode | str | None = None,
    ) -> NarrativeAuditReport:
        return cast(
            NarrativeAuditReport,
            self._run_sync(
                self.audit_episode_async(
                    project_id,
                    version_id,
                    runtime=runtime,
                    generation_mode=generation_mode,
                )
            ),
        )

    async def audit_episode_async(
        self,
        project_id: str,
        version_id: str,
        *,
        runtime: dict[str, Any] | None = None,
        generation_mode: GenerationMode | str | None = None,
    ) -> NarrativeAuditReport:
        report = self._audit_episode_deterministic(project_id, version_id)
        project = self.get_project(project_id)
        selected_runtime = self._resolve_stage_runtime(project, "reviewer", runtime)
        mode = resolve_generation_mode(generation_mode, selected_runtime)
        if mode == GenerationMode.DETERMINISTIC:
            return report
        version = self.get_episode_version(version_id)
        plan = self.get_episode_plan(project_id, version.episode_number)
        bible = self.repo.get_bible(project_id)
        assert bible is not None
        context = self.context_builder.build_auditor_context(
            project,
            bible,
            plan,
            version.screenplay,
            [entry.text for entry in self.repo.list_canon_entries(project_id)],
            self.get_knowledge_matrix(project_id, version.episode_number),
            self.firewall.audience_view(
                self.repo.list_facts(project_id),
                self.repo.list_audience_knowledge(project_id),
            ),
            self.repo.list_plot_threads(project_id),
            self.repo.list_clues(project_id),
            self.repo.list_arcs(project_id),
            recent_audits=[report],
        )
        result, _trace = await self._structured_call_async(
            "Review this AI episode draft. Return only new findings for canon conflict, "
            "knowledge leak, timeline conflict, persona/relationship inconsistency, world-rule "
            "violation, causal gap, early reveal, forgotten clue, stagnation, arc regression, "
            "or repetition.\n" + dumps(context),
            runtime=selected_runtime,
            phase="narrative_reviewer",
            mode=mode,
            schema={
                "type": "object",
                "required": ["findings"],
                "properties": {
                    "findings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["severity", "code", "message"],
                            "properties": {
                                "severity": {"type": "string"},
                                "code": {"type": "string"},
                                "message": {"type": "string"},
                                "evidence": {"type": "object"},
                            },
                        },
                    }
                },
            },
            included_sections=["full_story_truth", "canon", "draft", "knowledge_matrix"],
        )
        valid_severity = {item.value for item in AuditSeverity}
        for item in result.get("findings", []):
            severity = str(item.get("severity") or "warning").casefold()
            if severity not in valid_severity:
                severity = AuditSeverity.WARNING.value
            report.findings.append(
                AuditFinding(
                    severity=AuditSeverity(severity),
                    code=str(item.get("code") or "AI_REVIEW_FINDING"),
                    message=str(item.get("message") or "AI reviewer finding"),
                    evidence=dict(item.get("evidence") or {}),
                )
            )
        report.blocking_count = sum(
            finding.severity == AuditSeverity.BLOCKING for finding in report.findings
        )
        report.warning_count = sum(
            finding.severity == AuditSeverity.WARNING for finding in report.findings
        )
        report.info_count = sum(
            finding.severity == AuditSeverity.INFO for finding in report.findings
        )
        report.passed = report.blocking_count == 0
        self.repo.save_audit(report)
        plan.status = EpisodeStatus.READY if report.passed else EpisodeStatus.DRAFTED
        self.repo.save_episode_plan(plan)
        return report

    def _audit_episode_deterministic(
        self, project_id: str, version_id: str
    ) -> NarrativeAuditReport:
        """AUDIT: continuity, knowledge, timeline, plot, clue, arc, pacing."""
        project = self.get_project(project_id)
        version = self.get_episode_version(version_id)
        plan = self.get_episode_plan(project_id, version.episode_number)
        bible = self.repo.get_bible(project_id)
        if bible is None:
            raise ValueError("Story bible missing")
        scenes = [
            s for s in (self.repo.get_scene(sid) for sid in version.scene_ids) if s is not None
        ]
        report = self.auditor.audit_episode(
            project,
            bible,
            plan,
            version,
            facts=self.repo.list_facts(project_id),
            knowledge=self.repo.list_knowledge(project_id),
            characters=self.repo.list_characters(project_id),
            scenes=scenes,
            threads=self.repo.list_plot_threads(project_id),
            clues=self.repo.list_clues(project_id),
            arcs=self.repo.list_arcs(project_id),
            previous_versions=[
                v
                for v in self.repo.list_episode_versions(project_id, version.episode_number)
                if v.id != version.id
            ],
            audience_state=self.firewall.audience_view(
                self.repo.list_facts(project_id),
                self.repo.list_audience_knowledge(project_id),
            ),
            context_fingerprint=version.context_fingerprint,
        )
        self.repo.save_audit(report)
        self.repo.get_episode_version(version_id)
        version.audit_report_id = report.id
        version.stale = version.context_fingerprint != self.context_fingerprint(project)
        self.repo.save_episode_version(version)
        plan.status = EpisodeStatus.READY if report.passed else EpisodeStatus.DRAFTED
        self.repo.save_episode_plan(plan)
        return report

    def list_audits(
        self, project_id: str, episode_number: int | None = None
    ) -> list[NarrativeAuditReport]:
        return self.repo.list_audits(project_id, episode_number)

    def commit_episode(
        self,
        project_id: str,
        episode_number: int,
        version_id: str,
        *,
        force: bool = False,
        override_reason: str = "",
        canon_updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """COMMIT: atomic canon promotion (validate → transaction → commit).

        A BLOCKING audit blocks the commit unless ``force`` is set with an
        explicit ``override_reason``; overrides are recorded in audit history.
        """
        project = self.get_project(project_id)
        version = self.get_episode_version(version_id)
        if version.episode_number != episode_number:
            raise ValueError("Episode version does not match episode number")

        audits = [
            a
            for a in self.repo.list_audits(project_id, episode_number)
            if a.episode_version_id == version_id
        ]
        latest_audit = audits[0] if audits else None
        if latest_audit is None:
            raise ValueError("Run the continuity audit before committing")
        if latest_audit.blocking_count > 0:
            if not (force and override_reason.strip()):
                raise ValueError(
                    "Episode has BLOCKING audit findings; commit refused "
                    "(force with override_reason to bypass)"
                )
            latest_audit.findings.append(
                AuditFinding(
                    severity=AuditSeverity.INFO,
                    code="COMMIT_OVERRIDE",
                    message=f"Force commit override: {override_reason}",
                )
            )
            self.repo.save_audit(latest_audit)

        if version.stale and not (force and override_reason.strip()):
            raise ValueError("Episode draft is stale against current canon; regenerate first")

        canon_updates = canon_updates or {}

        # ---- Atomic section: one transaction, all-or-nothing ----------------
        with self.repo.db.transaction():
            version.is_canon = True
            version.created_at = datetime.now(UTC)
            self.repo.save_episode_version(version)
            self.repo.set_canon_episode_version(project_id, episode_number, version.id)

            canon_events = canon_updates.get("canon_events") or [
                version.simulation_summary
                or f"EP{episode_number} 进入正史：{version.title or '未命名剧集'}"
            ]
            for text in canon_events:
                if not text:
                    continue
                self.repo.save_canon_entry(
                    CanonEntry(
                        project_id=project_id,
                        episode_number=episode_number,
                        entry_type=CanonEntryType.EVENT,
                        text=text,
                        source="narrative_commit",
                        world_branch_id=version.canonical_branch_id,
                    )
                )

            for row in canon_updates.get("knowledge_updates", []):
                self.set_character_knowledge(
                    project_id,
                    row["character_id"],
                    row["fact_id"],
                    row["state"],
                    learned_episode=row.get("learned_episode", episode_number),
                    fact_text=row.get("fact_text", ""),
                )
            for row in canon_updates.get("audience_updates", []):
                self.set_audience_knowledge(
                    project_id,
                    row["fact_id"],
                    row["state"],
                    revealed_episode=row.get("revealed_episode", episode_number),
                    fact_text=row.get("fact_text", ""),
                )
            for thread in canon_updates.get("plot_threads", []):
                existing = next(
                    (
                        t
                        for t in self.repo.list_plot_threads(project_id)
                        if t.id == thread.get("id")
                    ),
                    None,
                )
                if existing:
                    existing.status = PlotThreadStatus(thread.get("status", existing.status.value))
                    if thread.get("progress_note"):
                        existing.progress_notes.append(thread["progress_note"])
                    self.repo.save_plot_thread(existing)
            for clue in canon_updates.get("clue_updates", []):
                existing_clue = next(
                    (c for c in self.repo.list_clues(project_id) if c.id == clue.get("id")),
                    None,
                )
                if existing_clue:
                    for field in ("status", "actual_reveal_episode", "echo_episodes", "payoff"):
                        if field in clue:
                            setattr(existing_clue, field, clue[field])
                    self.repo.save_clue(existing_clue)
            for arc in canon_updates.get("arc_updates", []):
                existing_arc = next(
                    (
                        a
                        for a in self.repo.list_arcs(project_id)
                        if a.character_id == arc.get("character_id")
                    ),
                    None,
                )
                if existing_arc:
                    existing_arc.current_progress = float(
                        arc.get("current_progress", existing_arc.current_progress)
                    )
                    existing_arc.current_phase = arc.get(
                        "current_phase", existing_arc.current_phase
                    )
                    self.repo.save_arc(existing_arc)

            plan = self.repo.get_episode_plan(project_id, episode_number)
            if plan is not None:
                plan.status = EpisodeStatus.CANON
                self.repo.save_episode_plan(plan)

            if version.canonical_branch_id:
                project.canonical_world_branch_id = version.canonical_branch_id
            project.revision += 1
            project.updated_at = datetime.now(UTC)
            self.repo.save_project(project)

            summary_text = version.simulation_summary or version.title
            self.repo.save_episode_summary(
                EpisodeSummary(
                    project_id=project_id,
                    episode_number=episode_number,
                    summary=summary_text[:2000],
                    context_fingerprint=self.context_fingerprint(project),
                )
            )
        # ---- End atomic section --------------------------------------------

        self._mark_dependent_artifacts_stale(project)
        return {
            "committed": True,
            "episode_number": episode_number,
            "version_id": version.id,
            "canonical_branch_id": project.canonical_world_branch_id,
            "project_revision": project.revision,
        }

    # ------------------------------------------------------------------
    # Writer Room (reuses Room Protocol Engine)
    # ------------------------------------------------------------------
    async def run_writer_room(
        self,
        project_id: str,
        episode_number: int,
        participants: list[dict[str, Any]],
        *,
        cross_review: bool = True,
    ) -> dict[str, Any]:
        """Run an AI Writer's Room through the standard protocol runtime.

        ``participants``: [{"role": "mystery_editor", "persona_id": "..."}].
        Roles map to advisory instructions; personas/models stay freely chosen.
        """
        project = self.get_project(project_id)
        from persona_continuum.room.models import ParticipantSlot

        protocol_config = build_writer_room_protocol_config(cross_review=cross_review)
        slots = [
            ParticipantSlot(
                participant_id=f"slot_{idx}",
                persona_id=p.get("persona_id", ""),
                display_name=p.get("display_name") or p.get("role", f"editor_{idx}"),
                role=p.get("role", "head_writer"),
                runtime_selection=p.get("runtime_selection", "default"),
                model_selection=p.get("model_selection", "default"),
                reasoning_selection=p.get("reasoning_selection", "default"),
                auth_profile_id=p.get("auth_profile_id"),
            )
            for idx, p in enumerate(participants, 1)
        ]
        context = self.prepare_episode(project_id, episode_number)
        shared_context = writer_room_shared_context(
            project_title=project.title,
            episode_number=episode_number,
            episode_goal=self.get_episode_plan(project_id, episode_number).narrative_goal,
            writer_context=context,
        )
        room = self.continuum.orchestrator.create_room(
            title=f"Writer's Room — {project.title} EP{episode_number:02d}",
            topic=shared_context,
            participants=slots,
            protocol=__import__(
                "persona_continuum.room.models", fromlist=["RoomProtocolType"]
            ).RoomProtocolType.CUSTOM,
            protocol_config=protocol_config,
            metadata={"narrative_project_id": project_id, "kind": "writer_room"},
        )
        state = await self.continuum.orchestrator.run_protocol(room.id, shared_context)
        try:
            protocol_state = state.protocol_state
            if protocol_state is None or protocol_state.status.value not in {
                "success",
                "partial_success",
            }:
                raise NarrativeAgentError(
                    NARRATIVE_AGENT_GENERATION_FAILED,
                    state.last_error or "Writer Room protocol failed",
                    stage="writer_room",
                )
            stage_outputs = [
                {
                    "stage": task.stage,
                    "participant_id": task.participant_id,
                    "status": task.status.value,
                    "output": dict(task.output or {}),
                }
                for task in protocol_state.tasks
                if task.task_type != "room_run"
            ]
            final = dict(protocol_state.final_result or {})
            if not final or not any(
                str(final.get(key) or "").strip() or bool(final.get(key))
                for key in ("summary", "recommendations", "recommended_direction")
            ):
                raise NarrativeAgentError(
                    NARRATIVE_AGENT_GENERATION_FAILED,
                    "Writer Room completed without a successful Head Writer synthesis",
                    stage="writer_room",
                )
            recommendations = [str(item) for item in final.get("recommendations", [])]
            synthesis = WriterRoomSynthesis(
                project_id=project_id,
                episode_number=episode_number,
                room_id=room.id,
                protocol_run_id=protocol_state.run_id,
                recommended_direction=str(
                    final.get("recommended_direction") or final.get("summary") or ""
                ),
                episode_goal=self.get_episode_plan(project_id, episode_number).narrative_goal,
                recommended_beats=[
                    str(item) for item in (final.get("recommended_beats") or recommendations)
                ],
                character_notes=self._room_notes(stage_outputs, "character"),
                mystery_notes=self._room_notes(stage_outputs, "mystery"),
                continuity_constraints=self._room_notes(stage_outputs, "continuity"),
                commercial_notes=self._room_notes(stage_outputs, "commercial"),
                must_keep=[str(item) for item in final.get("must_keep", [])],
                must_change=[str(item) for item in final.get("must_change", [])],
                risks=[str(item) for item in final.get("uncertainties", [])],
                final_writer_instruction=str(
                    final.get("final_writer_instruction")
                    or final.get("summary")
                    or "\n".join(recommendations)
                ),
                stage_outputs=stage_outputs,
                runtime_trace=[
                    dict((row.get("output") or {}).get("_execution") or {})
                    for row in stage_outputs
                    if (row.get("output") or {}).get("_execution")
                ],
            )
            self.repo.save_writer_room_synthesis(synthesis)
            return {
                "room_id": room.id,
                "protocol_state": protocol_state.status.value,
                "synthesis": synthesis.model_dump(mode="json"),
                "stage_outputs": stage_outputs,
            }
        finally:
            await self.continuum.orchestrator.stop_room(room.id)

    def get_writer_room_synthesis(
        self, project_id: str, episode_number: int
    ) -> WriterRoomSynthesis | None:
        return self.repo.get_writer_room_synthesis(project_id, episode_number)

    # ------------------------------------------------------------------
    # Production package
    # ------------------------------------------------------------------
    def generate_production_package(
        self,
        project_id: str,
        episode_number: int,
        version_id: str | None = None,
        *,
        runtime: dict[str, Any] | None = None,
        generation_mode: GenerationMode | str | None = None,
        is_preview: bool = False,
    ) -> ProductionPackage:
        return cast(
            ProductionPackage,
            self._run_sync(
                self.generate_production_package_async(
                    project_id,
                    episode_number,
                    version_id,
                    runtime=runtime,
                    generation_mode=generation_mode,
                    is_preview=is_preview,
                )
            ),
        )

    async def generate_production_package_async(
        self,
        project_id: str,
        episode_number: int,
        version_id: str | None = None,
        *,
        runtime: dict[str, Any] | None = None,
        generation_mode: GenerationMode | str | None = None,
        is_preview: bool = False,
    ) -> ProductionPackage:
        project = self.get_project(project_id)
        version = (
            self.get_episode_version(version_id)
            if version_id
            else self.repo.get_canon_episode_version(project_id, episode_number)
        )
        if version is None and is_preview:
            # Preview packages keep the legacy fallback to the latest draft so
            # callers can eyeball a package before any canon commit exists.
            versions = self.repo.list_episode_versions(project_id, episode_number)
            version = versions[0] if versions else None
        if version is None:
            raise KeyError(f"No episode version to package: EP{episode_number}")
        if not version.is_canon and not is_preview:
            raise NarrativeAgentError(
                NARRATIVE_PRODUCTION_CANON_REQUIRED,
                f"EP{episode_number} has no canon episode version; commit a draft or "
                "request an explicit preview package.",
                stage="production_planner",
            )
        bible = self.repo.get_bible(project_id)
        if bible is None:
            raise ValueError("Story bible missing")
        scenes = [self.repo.get_scene(sid) for sid in version.scene_ids]
        package = self.production_builder.build(
            project_id,
            episode_number,
            version,
            bible,
            [s for s in scenes if s],
            fmt=project.format,
            visual_identity=bible.visual_identity,
        )
        package.is_preview = is_preview
        package.context_fingerprint = self.context_fingerprint(project)
        selected_runtime = self._resolve_stage_runtime(project, "production_planner", runtime)
        mode = resolve_generation_mode(generation_mode, selected_runtime)
        package.generation_mode = mode
        if mode == GenerationMode.AGENT:
            planner_payload = {
                "final_canon_episode_draft": {
                    "episode_number": version.episode_number,
                    "title": version.title,
                    "screenplay": version.screenplay,
                    "structured_draft": version.structured_draft,
                    "beat_sheet": [beat.model_dump(mode="json") for beat in version.beat_sheet],
                },
                "visual_identity": bible.visual_identity,
                "character_visual_bible": package.character_visual_bible,
                "location_visual_bible": package.location_visual_bible,
                "runtime_constraints": {
                    "duration_seconds": sum(shot.duration_seconds for shot in package.shot_list),
                    "format": project.format.value,
                },
                "target_video_model_capability": "generic",
            }
            planned, trace = await self._structured_call_async(
                "Act as a model-agnostic Director and Production Planner. Refine the "
                "shot list without changing canon. Return generic image/video prompts and "
                "continuity constraints; never bind to a vendor.\n" + dumps(planner_payload),
                runtime=selected_runtime,
                phase="production_planner",
                mode=GenerationMode.AGENT,
                schema=self._production_planner_schema(),
                included_sections=[
                    "canon_draft",
                    "visual_identity",
                    "character_visual_bible",
                    "location_visual_bible",
                    "runtime_constraints",
                ],
                structured_repair_attempts=2,
            )
            try:
                package.shot_list = [Shot.model_validate(row) for row in planned["shot_list"]]
            except ValidationError as exc:
                raise NarrativeAgentError(
                    NARRATIVE_AGENT_STRUCTURED_OUTPUT_INVALID,
                    str(exc),
                    stage="production_planner",
                    runtime=selected_runtime,
                    original_error=exc,
                ) from exc
            package.image_generation_prompts = [shot.visual_prompt for shot in package.shot_list]
            package.video_generation_prompts = [shot.motion_prompt for shot in package.shot_list]
            package.bgm_direction = str(planned.get("bgm_direction") or package.bgm_direction)
            package.continuity_notes.extend(
                str(item) for item in planned.get("continuity_notes", [])
            )
            package.runtime_trace = trace
        else:
            package.runtime_trace = {
                "stage": "production_planner",
                "generation_mode": mode.value,
                "status": "completed",
                "agent_calls": 0,
            }
        # Supersede: once a new final package replaces the previous master of
        # the same episode, the old master's model prompt packages become
        # stale. Preview packages never supersede a master. Every OTHER
        # production package of the episode is superseded — not just the
        # newest one — so an interposed preview cannot shield the real
        # previous master from the stale cascade. (Preview packages carry no
        # prompt packages, so marking them is a harmless no-op.)
        previous_packages = [
            pkg
            for pkg in self.repo.list_production_packages(project_id)
            if pkg.episode_number == episode_number and pkg.id != package.id
        ]
        self.repo.save_production_package(package)
        if not is_preview:
            for previous in previous_packages:
                self.repo.mark_model_prompt_packages_stale_for_production(
                    project_id, previous.id
                )
                # Guides built on the superseded master's packages follow the
                # same stale cascade as their source prompt packages.
                self.repo.mark_video_production_guides_stale_for_production(
                    project_id, previous.id
                )
        return package

    def list_production_packages(self, project_id: str) -> list[ProductionPackage]:
        return self.repo.list_production_packages(project_id)

    # ------------------------------------------------------------------
    # Model prompt package (profile-specific shooting pipeline)
    # ------------------------------------------------------------------
    async def generate_model_prompt_package_async(
        self,
        project_id: str,
        production_package_id: str,
        profile_id: str,
        *,
        aspect_ratio: str = "16:9",
        quality_priority: str = "balanced",
        generation_strategy: str = "auto",
        continuity_strategy: str = "auto",
        audio_strategy: str = "auto",
        prompt_language: str = "auto",
        progress_callback: Callable[[str, int | None, int | None], None] | None = None,
        stop_after_plan: bool = False,
    ) -> ModelPromptPackage:
        """Compile a profile-specific :class:`ModelPromptPackage`.

        Pipeline: planning_clips -> planning_assets -> compiling_prompts ->
        validating. Each stage boundary (and each refinement batch) reports
        ``(stage, completed, total)`` through ``progress_callback``; the
        background job worker uses it for pause/cancel checks and real-count
        progress (never fake percentages).

        ``stop_after_plan`` implements the two-step review flow: persist the
        clip plan at the ``clip_planned`` checkpoint and return before any
        prompt-compilation work starts.
        """
        project = self.get_project(project_id)
        production_package = self.repo.get_production_package(production_package_id)
        if production_package is None:
            raise KeyError(f"Production package not found: {production_package_id}")
        if production_package.is_preview:
            # Shooting must never treat a preview package as a final source.
            raise NarrativeAgentError(
                SHOOTING_SOURCE_PREVIEW_NOT_ALLOWED,
                f"Production package {production_package_id} is a preview; "
                "generate the final package from the canon episode version first.",
                stage="model_prompt_package",
            )
        version = self.repo.get_episode_version(production_package.episode_version_id or "")
        if version is None or not version.is_canon:
            raise NarrativeAgentError(
                NARRATIVE_PRODUCTION_CANON_REQUIRED,
                f"Production package {production_package_id} does not reference the "
                f"canon episode version of EP{production_package.episode_number}.",
                stage="model_prompt_package",
            )
        profile = get_profile(profile_id)
        fingerprint = self.context_fingerprint(project)
        options = {
            "aspect_ratio": aspect_ratio,
            "quality_priority": quality_priority,
            "generation_strategy": generation_strategy,
            "continuity_strategy": continuity_strategy,
            "audio_strategy": audio_strategy,
            "prompt_language": prompt_language,
        }

        def report(stage: str, completed: int | None = None, total: int | None = None) -> None:
            if progress_callback is not None:
                progress_callback(stage, completed, total)

        report("loading_source")

        # Idempotent reuse: a non-stale package for the same (production
        # package, profile) resumes at its checkpoint status instead of
        # replanning clips from scratch — but ONLY when the pinned profile
        # version AND every stored generation option still match the
        # request. A version drift or an options change supersedes the old
        # package with a fresh revision (old row preserved as stale).
        existing = next(
            (
                pkg
                for pkg in self.repo.list_model_prompt_packages(
                    project_id, production_package_id
                )
                if pkg.target_profile_id == profile_id
                and not pkg.stale
                and pkg.status in MODEL_PROMPT_PACKAGE_RESUMABLE_STATUSES
            ),
            None,
        )
        reusable = existing is not None and self._package_matches_request(
            existing, profile, options
        )
        superseded: ModelPromptPackage | None = None
        revision_reason = ""
        if existing is not None and not reusable:
            superseded = existing
            revision_reason = self._package_revision_reason(existing, profile, options)
            self.repo.mark_model_prompt_packages_stale(
                project_id, existing.episode_number, package_id=existing.id
            )
            # A superseding package invalidates every guide built on it.
            self.repo.mark_video_production_guides_stale_for_prompt_package(
                project_id, existing.id
            )
        if reusable and existing is not None and existing.status == "ready":
            return existing
        if reusable and existing is not None:
            package = existing
        else:
            report("planning_clips")
            clips = plan_clips(
                production_package.shot_list, profile, aspect_ratio, quality_priority
            )
            for clip in clips:
                violations = validate_clip_against_profile(clip, profile)
                if violations:
                    raise NarrativeAgentError(
                        violations[0],
                        f"Clip {clip.clip_number} violates target video model profile "
                        f"'{profile.id}': {', '.join(violations)}",
                        stage="model_prompt_package",
                    )
            package = compile_prompt_package(
                production_package,
                profile,
                clips,
                [],
                project_id,
                options,
            )
            # The deterministic Task A compiler output is the embedded
            # baseline; the LLM refinement stage below overwrites prompts.
            package.status = "clip_planned"
            package.context_fingerprint = fingerprint
            if superseded is not None:
                package.parent_package_id = superseded.id
                package.revision_reason = revision_reason
            self.repo.save_model_prompt_package(package)

        report("planning_assets")
        package.asset_requirements = self._merge_asset_requirements(
            package,
            self._ensure_production_assets(project, production_package, package),
        )
        self.repo.save_model_prompt_package(package)

        if stop_after_plan:
            # Two-step review flow: hand the clip plan to the human reviewer
            # before prompt compilation starts. A package already past this
            # checkpoint (compiling) is returned as-is, never downgraded.
            if package.status == "clip_planned":
                package.runtime_trace = {
                    **package.runtime_trace,
                    "pipeline": {
                        **(package.runtime_trace.get("pipeline") or {}),
                        "stop_after_plan": True,
                        "clip_count": len(package.clips),
                        "context_fingerprint": fingerprint,
                    },
                }
                self.repo.save_model_prompt_package(package)
            report("planning_assets", len(package.clips), len(package.clips))
            return package

        total_clips = len(package.clips)
        report("compiling_prompts", 0, total_clips)
        selected_runtime = self._resolve_stage_runtime(project, "shooting_agent", None)
        mode = resolve_generation_mode(None, selected_runtime)
        refined_count = 0
        fallback_count = 0
        batch_count = 0
        if mode == GenerationMode.AGENT and total_clips:
            index = 0
            batch_size = MODEL_PROMPT_REFINEMENT_BATCH_SIZE
            retried_once = False
            while index < total_clips:
                batch = package.clips[index : index + batch_size]
                batch_count += 1
                if await self._refine_clip_prompts_batch(
                    project,
                    version,
                    production_package,
                    package,
                    profile,
                    batch,
                    prompt_language,
                    selected_runtime,
                ):
                    refined_count += len(batch)
                    index += len(batch)
                    retried_once = False
                elif len(batch) == 1:
                    # Deterministic Task A prompts stay embedded; never leave
                    # a clip without a prompt.
                    fallback_count += 1
                    index += 1
                    retried_once = False
                elif not retried_once:
                    retried_once = True
                else:
                    batch_size = 1
                    retried_once = False
                report("compiling_prompts", min(index, total_clips), total_clips)

        report("validating")
        self._validate_prompt_package_coverage(production_package, package)
        package.status = "ready"
        package.runtime_trace = {
            **package.runtime_trace,
            "pipeline": {
                "stage": "model_prompt_package",
                "generation_mode": mode.value,
                "resumed_from_checkpoint": reusable,
                "stop_after_plan": False,
                "clip_count": total_clips,
                "refined_clip_count": refined_count,
                "fallback_clip_count": fallback_count,
                "batch_count": batch_count,
                "asset_requirement_count": len(package.asset_requirements),
                "context_fingerprint": fingerprint,
            },
        }
        self.repo.save_model_prompt_package(package)
        report("validating", total_clips, total_clips)
        return package

    @staticmethod
    def _package_matches_request(
        package: ModelPromptPackage,
        profile: VideoModelProfile,
        options: dict[str, str],
    ) -> bool:
        """True when the package may be reused for this exact request.

        Reuse requires the pinned profile version AND every stored generation
        option (aspect ratio, quality/strategy switches, prompt language) to
        match; anything else must go through a fresh superseding revision.
        """
        if str(package.target_profile_version) != str(profile.profile_version):
            return False
        stored = {
            "aspect_ratio": package.aspect_ratio,
            "quality_priority": package.quality_priority,
            "generation_strategy": package.generation_strategy,
            "continuity_strategy": package.continuity_strategy,
            "audio_strategy": package.audio_strategy,
            "prompt_language": package.prompt_language,
        }
        return all(stored.get(key) == value for key, value in options.items())

    @staticmethod
    def _package_revision_reason(
        package: ModelPromptPackage,
        profile: VideoModelProfile,
        options: dict[str, str],
    ) -> str:
        """Human-readable reason why a package was superseded (provenance)."""
        reasons: list[str] = []
        if str(package.target_profile_version) != str(profile.profile_version):
            reasons.append(
                f"profile {package.target_profile_version}→{profile.profile_version} update"
            )
        stored = {
            "aspect_ratio": package.aspect_ratio,
            "quality_priority": package.quality_priority,
            "generation_strategy": package.generation_strategy,
            "continuity_strategy": package.continuity_strategy,
            "audio_strategy": package.audio_strategy,
            "prompt_language": package.prompt_language,
        }
        changed = [
            f"{key} {stored.get(key)}→{value}"
            for key, value in options.items()
            if stored.get(key) != value
        ]
        if changed:
            reasons.append("options changed: " + ", ".join(changed))
        return "; ".join(reasons) or "recompile requested"

    async def _refine_clip_prompts_batch(
        self,
        project: NarrativeProject,
        version: EpisodeVersion,
        production_package: ProductionPackage,
        package: ModelPromptPackage,
        profile: VideoModelProfile,
        batch: list[GenerationClip],
        prompt_language: str,
        runtime: dict[str, Any],
    ) -> bool:
        """One batched LLM refinement pass over ``batch``.

        Returns False on any schema / identity / validation failure so the
        caller can retry once, degrade to batch size 1, or fall back to the
        embedded deterministic prompts.
        """
        payload = self._clip_refinement_payload(
            project, version, production_package, package, profile, batch, prompt_language
        )
        try:
            result, _trace = await self._structured_call_async(
                "You are the Video Prompt Compiler for one target video model. Refine the "
                "deterministic prompts of the given clips to fit the model's documented "
                "capabilities. Keep story canon, the visual bibles, and shot boundaries "
                "intact; never invent new story content, characters, or shots. Respect the "
                "requested prompt language and every capability limit in the profile "
                "digest. Return exactly one JSON value matching output_contract and keep "
                "every clip_id unchanged.\n" + dumps(payload),
                runtime=runtime,
                phase="prompt_compilation",
                mode=GenerationMode.AGENT,
                schema=_clip_prompts_schema(),
                structured_repair_attempts=1,
            )
        except NarrativeAgentError:
            return False
        rows = list((result or {}).get("clips") or [])
        by_id = {clip.id: clip for clip in batch}
        refined: dict[str, GenerationClip] = {}
        for row in rows:
            if not isinstance(row, dict):
                return False
            clip_id = str(row.get("clip_id") or "")
            target = by_id.get(clip_id)
            if target is None:
                return False
            negative_prompt = row.get("negative_prompt")
            audio_prompt = row.get("audio_prompt")
            # Profile gate: models without a separate negative-prompt field
            # (or with a positive_phrasing_only strategy) must never receive
            # model-supplied negative text.
            if (
                profile.supports_negative_prompt is not True
                or profile.negative_prompt_strategy == "positive_phrasing_only"
            ):
                negative_prompt = None
            candidate = target.model_copy(
                update={
                    "prompt": str(row.get("prompt") or ""),
                    "negative_prompt": str(negative_prompt) if negative_prompt else None,
                    "audio_prompt": str(audio_prompt) if audio_prompt else None,
                    # Merge over the deterministic baseline settings; a
                    # wholesale replace would drop planner recommendations.
                    "recommended_settings": {
                        **target.recommended_settings,
                        **dict(row.get("recommended_settings") or {}),
                    },
                    "continuity_constraints": [
                        *target.continuity_constraints,
                        *(str(item) for item in row.get("continuity_constraints") or []),
                    ],
                    "compiler_trace": {
                        **target.compiler_trace,
                        "refined_by": "prompt_compilation_agent",
                    },
                }
            )
            if not candidate.prompt.strip():
                return False
            if validate_clip_against_profile(candidate, profile):
                return False
            refined[clip_id] = candidate
        if len(refined) != len(batch):
            return False
        package.clips = [refined.get(clip.id, clip) for clip in package.clips]
        self.repo.save_model_prompt_package(package)
        return True

    def _clip_refinement_payload(
        self,
        project: NarrativeProject,
        version: EpisodeVersion,
        production_package: ProductionPackage,
        package: ModelPromptPackage,
        profile: VideoModelProfile,
        batch: list[GenerationClip],
        prompt_language: str,
    ) -> dict[str, Any]:
        """Compact user payload for one refinement batch (budget discipline)."""
        shots_by_number = {shot.shot_number: shot for shot in production_package.shot_list}
        character_names: list[str] = []
        location_names: list[str] = []
        prop_names: list[str] = []
        clips_payload: list[dict[str, Any]] = []
        for clip in batch:
            source_shots = [
                shots_by_number[number]
                for number in clip.source_shot_numbers
                if number in shots_by_number
            ]
            for shot in source_shots:
                for name in shot.characters:
                    if name and name not in character_names:
                        character_names.append(name)
                if shot.location and shot.location not in location_names:
                    location_names.append(shot.location)
                haystack = " ".join([shot.action, shot.dialogue, shot.visual_prompt]).casefold()
                for prop in production_package.prop_list:
                    prop_name = str(prop)
                    if (
                        prop_name
                        and prop_name.casefold() in haystack
                        and prop_name not in prop_names
                    ):
                        prop_names.append(prop_name)
            for entry in clip.dialogue:
                speaker = str(entry.get("speaker") or "")
                if speaker and speaker not in character_names:
                    character_names.append(speaker)
            clips_payload.append(
                {
                    "clip_id": clip.id,
                    "clip_number": clip.clip_number,
                    "generation_mode": clip.generation_mode,
                    "duration_seconds": clip.duration_seconds,
                    "aspect_ratio": clip.aspect_ratio,
                    "purpose": self._clip_text(clip.purpose, 120),
                    "visual_intent": self._clip_text(clip.visual_intent, 400),
                    "camera_intent": self._clip_text(clip.camera_intent, 160),
                    "subject_motion": self._clip_text(clip.subject_motion, 160),
                    "environment_motion": self._clip_text(clip.environment_motion, 160),
                    "dialogue": clip.dialogue,
                    "audio_intent": clip.audio_intent,
                    "continuity_constraints": list(clip.continuity_constraints),
                    "deterministic_prompt": clip.prompt,
                    "source_shots": [
                        self._compact_shot_for_prompts(shot) for shot in source_shots
                    ],
                }
            )
        ordered_numbers = [clip.clip_number for clip in package.clips]
        first_index = ordered_numbers.index(batch[0].clip_number)
        last_index = ordered_numbers.index(batch[-1].clip_number)
        return {
            "project": {
                "id": project.id,
                "title": self._clip_text(project.title, 120),
                "format": str(getattr(project.format, "value", project.format)),
            },
            "episode": {
                "episode_number": version.episode_number,
                "title": self._clip_text(version.title, 120),
                "screenplay_excerpt": self._clip_text(version.screenplay, 2400),
                "beats": [
                    {
                        "order": beat.order,
                        "title": self._clip_text(beat.title, 60),
                        "description": self._clip_text(beat.description, 160),
                    }
                    for beat in version.beat_sheet[:12]
                ],
            },
            "target_profile": profile_capabilities_digest(profile),
            "prompt_language": prompt_language,
            "continuity_hints": {
                "previous_clip": self._continuity_hint(
                    package.clips[first_index - 1] if first_index > 0 else None
                ),
                "next_clip": self._continuity_hint(
                    package.clips[last_index + 1] if last_index + 1 < len(package.clips) else None
                ),
            },
            "bible_excerpts": {
                "characters": self._bible_excerpts(
                    production_package.character_visual_bible, character_names
                ),
                "locations": self._bible_excerpts(
                    production_package.location_visual_bible, location_names
                ),
                "props": self._bible_excerpts(
                    production_package.prop_visual_bible, prop_names
                ),
            },
            "clips": clips_payload,
            "output_contract": {
                "clips": [
                    {
                        "clip_id": "string, unchanged from input",
                        "prompt": "string, final prompt for the target model",
                        "negative_prompt": "string or null",
                        "audio_prompt": "string or null",
                        "recommended_settings": "object",
                        "continuity_constraints": ["string"],
                    }
                ]
            },
        }

    def _ensure_production_assets(
        self,
        project: NarrativeProject,
        production_package: ProductionPackage,
        package: ModelPromptPackage,
    ) -> list[dict[str, Any]]:
        """Derive asset requirements from the clip plan and UPSERT
        :class:`ProductionAsset` rows by (asset_type, name).

        Files are never fabricated: ``source_uri`` / ``local_path`` stay None
        ("未准备") until a human registers the real asset.
        """
        by_key = {
            (asset.asset_type, asset.name): asset
            for asset in self.repo.list_production_assets(project.id)
        }
        characters_by_name = {
            str(entry.get("name") or ""): entry
            for entry in production_package.character_list
            if isinstance(entry, dict)
        }
        locations_by_name = {
            str(entry.get("name") or ""): entry
            for entry in production_package.location_list
            if isinstance(entry, dict)
        }
        shots_by_number = {shot.shot_number: shot for shot in production_package.shot_list}
        requirements: list[dict[str, Any]] = []
        for clip in package.clips:
            source_shots = [
                shots_by_number[number]
                for number in clip.source_shot_numbers
                if number in shots_by_number
            ]
            named: list[tuple[str, str, str | None]] = []
            seen_names: set[tuple[str, str]] = set()
            for shot in source_shots:
                for name in shot.characters:
                    key = ("character_reference", name)
                    if name and key not in seen_names:
                        seen_names.add(key)
                        entry = characters_by_name.get(name)
                        named.append(
                            (
                                "character_reference",
                                name,
                                str(entry.get("id")) if entry and entry.get("id") else None,
                            )
                        )
                if shot.location and ("location_reference", shot.location) not in seen_names:
                    seen_names.add(("location_reference", shot.location))
                    loc_entry = locations_by_name.get(shot.location)
                    named.append(
                        (
                            "location_reference",
                            shot.location,
                            str(loc_entry.get("id"))
                            if loc_entry and loc_entry.get("id")
                            else None,
                        )
                    )
                haystack = " ".join([shot.action, shot.dialogue, shot.visual_prompt]).casefold()
                for prop in production_package.prop_list:
                    prop_name = str(prop)
                    if (
                        prop_name
                        and prop_name.casefold() in haystack
                        and ("prop_reference", prop_name) not in seen_names
                    ):
                        seen_names.add(("prop_reference", prop_name))
                        named.append(("prop_reference", prop_name, None))
            for asset_type, name, external_id in named:
                asset = by_key.get((asset_type, name))
                if asset is None:
                    asset = ProductionAsset(
                        project_id=project.id,
                        episode_number=production_package.episode_number,
                        asset_type=asset_type,
                        name=name,
                        character_id=external_id
                        if asset_type == "character_reference"
                        else None,
                        location_id=external_id
                        if asset_type == "location_reference"
                        else None,
                        metadata={"production_package_id": package.id},
                    )
                    by_key[(asset_type, name)] = asset
                    self.repo.save_production_asset(asset)
                requirements.append(
                    {
                        "asset_id": asset.id,
                        "clip_number": clip.clip_number,
                        "purpose": asset_type,
                        "prepared": bool(asset.source_uri or asset.local_path),
                    }
                )
        return requirements

    @staticmethod
    def _merge_asset_requirements(
        package: ModelPromptPackage, derived: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Union of compiler-derived and clip-plan-derived requirements."""
        merged = list(package.asset_requirements)
        seen = {
            (str(row.get("asset_id")), row.get("clip_number"), str(row.get("purpose")))
            for row in merged
        }
        for row in derived:
            key = (str(row.get("asset_id")), row.get("clip_number"), str(row.get("purpose")))
            if key not in seen:
                seen.add(key)
                merged.append(row)
        return merged

    @staticmethod
    def _validate_prompt_package_coverage(
        production_package: ProductionPackage, package: ModelPromptPackage
    ) -> None:
        """Whole-package consistency: shot coverage and resolvable continuity."""
        covered: set[int] = set()
        for clip in package.clips:
            covered.update(clip.source_shot_numbers)
            if "previous_clip_end_frame" in clip.continuity_constraints and clip.clip_number <= 1:
                raise NarrativeAgentError(
                    NARRATIVE_AGENT_GENERATION_FAILED,
                    f"Clip {clip.clip_number} references a previous clip end frame but "
                    "has no predecessor; continuity references must stay resolvable.",
                    stage="model_prompt_package",
                )
        missing = sorted({shot.shot_number for shot in production_package.shot_list} - covered)
        if missing:
            raise NarrativeAgentError(
                NARRATIVE_AGENT_GENERATION_FAILED,
                f"Clip plan does not cover production shots {missing}; every master "
                "shot must map into a generation clip.",
                stage="model_prompt_package",
            )

    # ------------------------------------------------------------------
    # One-click complete video production (task #6: one user operation, not
    # three product steps)
    # ------------------------------------------------------------------
    def generate_complete_video_production_plan(
        self,
        project_id: str,
        production_package_id: str,
        target_profile_id: str,
        *,
        aspect_ratio: str = "16:9",
        quality_priority: str = "balanced",
        generation_strategy: str = "auto",
        continuity_strategy: str = "auto",
        audio_strategy: str = "auto",
        prompt_language: str = "auto",
    ) -> ExecutableVideoProductionGuide:
        return cast(
            ExecutableVideoProductionGuide,
            self._run_sync(
                self.generate_complete_video_production_plan_async(
                    project_id,
                    production_package_id,
                    target_profile_id,
                    aspect_ratio=aspect_ratio,
                    quality_priority=quality_priority,
                    generation_strategy=generation_strategy,
                    continuity_strategy=continuity_strategy,
                    audio_strategy=audio_strategy,
                    prompt_language=prompt_language,
                )
            ),
        )

    async def generate_complete_video_production_plan_async(
        self,
        project_id: str,
        production_package_id: str,
        target_profile_id: str,
        *,
        aspect_ratio: str = "16:9",
        quality_priority: str = "balanced",
        generation_strategy: str = "auto",
        continuity_strategy: str = "auto",
        audio_strategy: str = "auto",
        prompt_language: str = "auto",
        progress_callback: Callable[[str, int | None, int | None], None] | None = None,
    ) -> ExecutableVideoProductionGuide:
        """ONE operation: clip plan + model prompt package + production guide.

        Internally reuses the checkpointed pipelines (idempotent reuse, stale
        cascade, LLM refinement batches all keep working); the user never
        sees the intermediate products as separate steps. ``progress_callback``
        receives the creator-facing stages of
        :data:`COMPLETE_VIDEO_PRODUCTION_STAGE_LABELS` with the inner pipelines'
        real counts.
        """

        def mapped(
            phase_map: dict[str, str],
        ) -> Callable[[str, int | None, int | None], None]:
            def report(stage: str, completed: int | None, total: int | None) -> None:
                if progress_callback is None:
                    return
                progress_callback(
                    phase_map.get(stage, stage), completed, total
                )

            return report

        report = mapped(_COMPLETE_PLAN_STAGE_MAP_PACKAGE)
        report("loading_source", None, None)
        prompt_package = await self.generate_model_prompt_package_async(
            project_id,
            production_package_id,
            target_profile_id,
            aspect_ratio=aspect_ratio,
            quality_priority=quality_priority,
            generation_strategy=generation_strategy,
            continuity_strategy=continuity_strategy,
            audio_strategy=audio_strategy,
            prompt_language=prompt_language,
            progress_callback=report,
            stop_after_plan=False,
        )
        guide = await self.generate_video_production_guide_async(
            project_id,
            prompt_package.id,
            progress_callback=mapped(_COMPLETE_PLAN_STAGE_MAP_GUIDE),
        )
        return guide

    # ------------------------------------------------------------------
    # Executable video production guide (copy-ready export layer)
    # ------------------------------------------------------------------
    async def generate_video_production_guide_async(
        self,
        project_id: str,
        prompt_package_id: str,
        *,
        progress_callback: Callable[[str, int | None, int | None], None] | None = None,
    ) -> ExecutableVideoProductionGuide:
        """Build an :class:`ExecutableVideoProductionGuide` on top of one ready,
        fresh :class:`ModelPromptPackage`.

        Pipeline: analyzing_assets -> compiling_asset_prompts ->
        compiling_clip_prompts -> rendering_guide. The guide never replaces
        the prompt package: clip prompt refinements happen on copies and the
        source package stays untouched. Location context failures propagate
        fail-closed BEFORE any LLM call (SHOOTING_LOCATION_CONTEXT_MISSING).
        """
        # Task B lands the guide builder in parallel; import at call time so
        # this service module stays importable until that module exists.
        from persona_continuum.narrative.video_production_guide import (
            analyze_asset_necessity,
            build_executable_video_production_guide,
        )

        project = self.get_project(project_id)

        def report(
            stage: str, completed: int | None = None, total: int | None = None
        ) -> None:
            if progress_callback is not None:
                progress_callback(stage, completed, total)

        report("loading_source")
        prompt_package = self.repo.get_model_prompt_package(prompt_package_id)
        if prompt_package is None:
            raise KeyError(f"Model prompt package not found: {prompt_package_id}")
        if prompt_package.status != "ready" or prompt_package.stale:
            raise NarrativeAgentError(
                VIDEO_GUIDE_SOURCE_NOT_READY,
                f"Model prompt package {prompt_package_id} is not a usable guide "
                f"source (status={prompt_package.status}, "
                f"stale={prompt_package.stale}); recompile it first.",
                stage="video_production_guide",
            )
        production_package = self.repo.get_production_package(
            prompt_package.production_package_id
        )
        if production_package is None:
            raise KeyError(
                f"Production package not found: {prompt_package.production_package_id}"
            )
        if production_package.is_preview:
            raise NarrativeAgentError(
                SHOOTING_SOURCE_PREVIEW_NOT_ALLOWED,
                f"Production package {production_package.id} is a preview; "
                "generate the final package from the canon episode version first.",
                stage="video_production_guide",
            )
        version = self.repo.get_episode_version(production_package.episode_version_id or "")
        if version is None or not version.is_canon:
            raise NarrativeAgentError(
                NARRATIVE_PRODUCTION_CANON_REQUIRED,
                f"Production package {production_package.id} does not reference the "
                f"canon episode version of EP{production_package.episode_number}.",
                stage="video_production_guide",
            )
        profile = get_profile(prompt_package.target_profile_id)
        fingerprint = self.context_fingerprint(project)

        # Idempotent reuse (F1 semantics): a non-stale guide for the same
        # prompt package is reused when the pinned profile version AND the
        # clip plan still match; any drift supersedes the old guide(s) with
        # a fresh revision (old rows preserved as stale).
        clip_fingerprint = self._clip_plan_fingerprint(prompt_package)
        candidates = [
            g
            for g in self.repo.list_video_production_guides(
                project_id, prompt_package_id=prompt_package.id
            )
            if not g.stale and (g.status in GUIDE_RESUMABLE_STATUSES or g.status == "ready")
        ]
        existing = candidates[0] if candidates else None
        reusable = (
            existing is not None
            and str(existing.target_profile_version) == str(profile.profile_version)
            and str(
                ((existing.runtime_trace or {}).get("pipeline") or {}).get(
                    "clip_plan_fingerprint"
                )
                or ""
            )
            == clip_fingerprint
        )
        superseded: ExecutableVideoProductionGuide | None = None
        revision_reason = ""
        if existing is not None and not reusable:
            superseded = existing
            revision_reason = self._guide_revision_reason(existing, profile, clip_fingerprint)
            self.repo.mark_video_production_guides_stale_for_prompt_package(
                project_id, prompt_package.id
            )
        if reusable and existing is not None and existing.status == "ready":
            return existing
        if reusable and existing is not None:
            guide = existing
            trace_pipeline: dict[str, Any] = dict(
                (guide.runtime_trace or {}).get("pipeline") or {}
            )
            trace_pipeline["resumed_from_checkpoint"] = True
        else:
            guide = ExecutableVideoProductionGuide(
                project_id=project_id,
                episode_number=prompt_package.episode_number,
                production_package_id=prompt_package.production_package_id,
                prompt_package_id=prompt_package.id,
                episode_version_id=prompt_package.episode_version_id,
                target_profile_id=profile.id,
                target_profile_version=profile.profile_version,
                target_video_model_display_name=profile.display_name,
                aspect_ratio=prompt_package.aspect_ratio,
                prompt_language=prompt_package.prompt_language,
                status="drafting",
                context_fingerprint=fingerprint,
                parent_guide_id=superseded.id if superseded is not None else None,
                revision_reason=revision_reason,
            )
            trace_pipeline = {
                "stage": "video_production_guide",
                "resumed_from_checkpoint": False,
            }

        report("analyzing_assets")
        # Resume keeps already-refined checkpoint state; a fresh guide runs
        # the deterministic analysis (fail-closed on missing location context
        # BEFORE any LLM call) and copies clips so the source package is
        # never mutated by guide-layer refinement.
        if not guide.required_assets:
            guide.required_assets = list(
                analyze_asset_necessity(production_package, prompt_package, profile)
            )
        if not guide.clip_workflows:
            guide.clip_workflows = [clip.model_copy(deep=True) for clip in prompt_package.clips]
        guide.status = "compiling"
        guide.stale = False
        guide.context_fingerprint = fingerprint
        trace_pipeline["clip_plan_fingerprint"] = clip_fingerprint
        trace_pipeline["asset_count"] = len(guide.required_assets)
        trace_pipeline["clip_count"] = len(guide.clip_workflows)
        guide.runtime_trace = {**guide.runtime_trace, "pipeline": trace_pipeline}
        self.repo.save_video_production_guide(guide)
        report("analyzing_assets", len(guide.required_assets), len(guide.required_assets))

        selected_runtime = self._resolve_stage_runtime(project, "guide_compilation", None)
        mode = resolve_generation_mode(None, selected_runtime)
        refined_assets = 0
        fallback_assets = 0
        batch_count = 0
        total_assets = len(guide.required_assets)
        report("compiling_asset_prompts", 0, total_assets)
        if mode == GenerationMode.AGENT and total_assets:
            index = 0
            batch_size = GUIDE_REFINEMENT_BATCH_SIZE
            retried_once = False
            while index < total_assets:
                asset_batch = guide.required_assets[index : index + batch_size]
                batch_count += 1
                if await self._refine_guide_assets_batch(
                    project,
                    prompt_package,
                    profile,
                    guide,
                    asset_batch,
                    selected_runtime,
                ):
                    refined_assets += len(asset_batch)
                    index += len(asset_batch)
                    retried_once = False
                elif len(asset_batch) == 1:
                    # Task B deterministic baseline stays embedded.
                    fallback_assets += 1
                    index += 1
                    retried_once = False
                elif not retried_once:
                    retried_once = True
                else:
                    batch_size = 1
                    retried_once = False
                report("compiling_asset_prompts", min(index, total_assets), total_assets)

        refined_clips = 0
        fallback_clips = 0
        total_clips = len(guide.clip_workflows)
        report("compiling_clip_prompts", 0, total_clips)
        if mode == GenerationMode.AGENT and total_clips:
            index = 0
            batch_size = GUIDE_REFINEMENT_BATCH_SIZE
            retried_once = False
            while index < total_clips:
                clip_batch = guide.clip_workflows[index : index + batch_size]
                batch_count += 1
                if await self._refine_guide_clips_batch(
                    project,
                    prompt_package,
                    profile,
                    guide,
                    clip_batch,
                    selected_runtime,
                ):
                    refined_clips += len(clip_batch)
                    index += len(clip_batch)
                    retried_once = False
                elif len(clip_batch) == 1:
                    fallback_clips += 1
                    index += 1
                    retried_once = False
                elif not retried_once:
                    retried_once = True
                else:
                    batch_size = 1
                    retried_once = False
                report("compiling_clip_prompts", min(index, total_clips), total_clips)

        assets = self.repo.list_production_assets(project_id, production_package.id)
        # The builder builds subtitle/sound/BGM/editing sections inside the
        # final render; report those sub-stages at their real boundaries so
        # the progress reads like the creator-facing workflow (task #7).
        report("subtitle_sound")
        report("bgm_editing")
        report("rendering_guide")
        built: ExecutableVideoProductionGuide = build_executable_video_production_guide(
            production_package,
            prompt_package,
            profile,
            assets,
            project_id,
            {"episode_title": (version.title or "").strip()},
        )
        # The builder assembles the deterministic handbook; overlay the
        # working guide's identity, provenance and LLM-refined state onto it
        # so refinements survive the final render. Overlay is driven by
        # content divergence from the rebuilt baseline, not by a provenance
        # tag, so it stays correct regardless of the merge convention.
        built.id = guide.id
        built.created_at = guide.created_at
        built.parent_guide_id = guide.parent_guide_id
        built.revision_reason = guide.revision_reason
        built.status = "ready"
        built.stale = False
        built.context_fingerprint = fingerprint
        built_assets_by_key = {asset.asset_key: asset for asset in built.required_assets}
        asset_overlay = {
            asset.asset_key: asset
            for asset in guide.required_assets
            if asset.asset_key in built_assets_by_key
            and asset.generation_prompt != built_assets_by_key[asset.asset_key].generation_prompt
        }
        built.required_assets = [
            asset_overlay.get(asset.asset_key, asset) for asset in built.required_assets
        ]
        built_clips_by_id = {clip.id: clip for clip in built.clip_workflows}
        # An empty copy_ready_prompt is never a refinement result (the
        # sanitizer rejects empty rows), so it must not win the overlay:
        # unrefined working copies keep the freshly compiled baseline.
        clip_overlay = {
            clip.id: clip
            for clip in guide.clip_workflows
            if clip.id in built_clips_by_id
            and clip.copy_ready_prompt
            and clip.copy_ready_prompt != built_clips_by_id[clip.id].copy_ready_prompt
        }
        built.clip_workflows = [
            clip_overlay.get(clip.id, clip) for clip in built.clip_workflows
        ]
        trace_pipeline["stage"] = "rendering_guide"
        trace_pipeline["generation_mode"] = mode.value
        trace_pipeline["refined_asset_count"] = refined_assets
        trace_pipeline["fallback_asset_count"] = fallback_assets
        trace_pipeline["refined_clip_count"] = refined_clips
        trace_pipeline["fallback_clip_count"] = fallback_clips
        trace_pipeline["batch_count"] = batch_count
        trace_pipeline["context_fingerprint"] = fingerprint
        built.runtime_trace = {**built.runtime_trace, "pipeline": trace_pipeline}
        guide = built
        self.repo.save_video_production_guide(guide)
        report("rendering_guide", 1, 1)
        return guide

    @staticmethod
    def _clip_plan_fingerprint(package: ModelPromptPackage) -> str:
        """sha256 over the sorted (clip_number, clip_id) plan of a package."""
        payload = dumps(sorted((clip.clip_number, clip.id) for clip in package.clips))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _guide_revision_reason(
        guide: ExecutableVideoProductionGuide,
        profile: VideoModelProfile,
        clip_fingerprint: str,
    ) -> str:
        """Human-readable reason why a guide was superseded (provenance)."""
        reasons: list[str] = []
        if str(guide.target_profile_version) != str(profile.profile_version):
            reasons.append(
                f"profile {guide.target_profile_version}→{profile.profile_version} update"
            )
        stored = str(
            ((guide.runtime_trace or {}).get("pipeline") or {}).get(
                "clip_plan_fingerprint"
            )
            or ""
        )
        if stored and stored != clip_fingerprint:
            reasons.append("source prompt package clip plan changed")
        return "; ".join(reasons) or "rebuild requested"

    async def _refine_guide_assets_batch(
        self,
        project: NarrativeProject,
        prompt_package: ModelPromptPackage,
        profile: VideoModelProfile,
        guide: ExecutableVideoProductionGuide,
        batch: list[ProductionGuideAsset],
        runtime: dict[str, Any],
    ) -> bool:
        """One batched LLM refinement pass over asset image prompts.

        Returns False on any structured-output failure (or a batch whose rows
        are all empty/placeholder) so the caller can retry once, degrade to
        batch size 1, or keep the Task B deterministic baseline.
        """
        from persona_continuum.narrative.video_production_guide import (
            PLACEHOLDER_PATTERNS,
            apply_asset_prompt_enrichment,
        )

        payload = {
            "project_title": self._clip_text(project.title, 120),
            "target_model": profile.display_name,
            "prompt_language": prompt_package.prompt_language,
            "global_visual_contract": list(prompt_package.global_visual_contract)[:8],
            "assets": [
                {
                    "asset_key": asset.asset_key,
                    "asset_type": asset.asset_type,
                    "name": asset.name,
                    "purpose": self._clip_text(asset.purpose, 200),
                    "necessity": asset.necessity,
                    "baseline_prompt": asset.generation_prompt,
                    "source_bible_refs": list(asset.source_bible_refs)[:6],
                }
                for asset in batch
            ],
            "output_contract": {
                "assets": [
                    {
                        "asset_key": "string, unchanged from input",
                        "generation_prompt": "string, COMPLETE image-generation prompt",
                    }
                ]
            },
        }
        try:
            result, _trace = await self._structured_call_async(
                "You are the Reference Asset Prompt Compiler for one target video "
                "model. Refine each deterministic asset image prompt into a "
                "complete, self-contained image-generation prompt for the reference "
                "image the asset describes. Keep identity anchors (character / "
                "location / prop identity, style) intact; never invent new subjects; "
                "never answer with placeholder phrases. Respect the requested prompt "
                "language. Return exactly one JSON value matching output_contract "
                "and keep every asset_key unchanged.\n" + dumps(payload),
                runtime=runtime,
                phase="guide_compilation",
                mode=GenerationMode.AGENT,
                schema=_guide_assets_schema(),
                structured_repair_attempts=1,
            )
        except NarrativeAgentError:
            return False
        rows = self._sanitize_enrichment_rows(
            (result or {}).get("assets"),
            "asset_key",
            "generation_prompt",
            PLACEHOLDER_PATTERNS,
        )
        if not rows:
            return False
        before = {asset.asset_key: asset.generation_prompt for asset in guide.required_assets}
        guide.required_assets = apply_asset_prompt_enrichment(guide.required_assets, rows)
        changed = sum(
            1
            for asset in guide.required_assets
            if before.get(asset.asset_key) != asset.generation_prompt
        )
        if not changed:
            return False
        self.repo.save_video_production_guide(guide)
        return True

    async def _refine_guide_clips_batch(
        self,
        project: NarrativeProject,
        prompt_package: ModelPromptPackage,
        profile: VideoModelProfile,
        guide: ExecutableVideoProductionGuide,
        batch: list[GenerationClip],
        runtime: dict[str, Any],
    ) -> bool:
        """One batched LLM refinement pass over copy-ready clip prompts.

        Operates ONLY on the guide's clip copies; the source prompt package
        is never touched. Returns False on failure (see the asset variant).
        """
        from persona_continuum.narrative.video_production_guide import (
            PLACEHOLDER_PATTERNS,
            apply_clip_prompt_enrichment,
        )

        payload = {
            "project_title": self._clip_text(project.title, 120),
            "target_model": profile.display_name,
            "prompt_language": prompt_package.prompt_language,
            "global_continuity_contract": list(prompt_package.global_continuity_contract)[:8],
            "clips": [
                {
                    "clip_id": clip.id,
                    "clip_number": clip.clip_number,
                    "generation_mode": clip.generation_mode,
                    "duration_seconds": clip.duration_seconds,
                    "aspect_ratio": clip.aspect_ratio,
                    "purpose": self._clip_text(clip.purpose, 120),
                    "visual_intent": self._clip_text(clip.visual_intent, 300),
                    "dialogue": clip.dialogue,
                    "audio_intent": clip.audio_intent,
                    "continuity_constraints": list(clip.continuity_constraints)[:8],
                    "copy_ready_prompt": clip.copy_ready_prompt,
                }
                for clip in batch
            ],
            "output_contract": {
                "clips": [
                    {
                        "clip_id": "string, unchanged from input",
                        "copy_ready_prompt": "string, refined copy-ready video prompt",
                    }
                ]
            },
        }
        try:
            result, _trace = await self._structured_call_async(
                "You are the Video Prompt Compiler for one target video model. Refine "
                "each clip's copy-ready prompt so a human can paste it into the model "
                "verbatim. Keep story canon, the visual bibles, shot boundaries and "
                "every capability limit intact; never invent new story content; never "
                "answer with placeholder phrases. Respect the requested prompt "
                "language. Return exactly one JSON value matching output_contract and "
                "keep every clip_id unchanged.\n" + dumps(payload),
                runtime=runtime,
                phase="guide_compilation",
                mode=GenerationMode.AGENT,
                schema=_guide_clips_schema(),
                structured_repair_attempts=1,
            )
        except NarrativeAgentError:
            return False
        rows = self._sanitize_enrichment_rows(
            (result or {}).get("clips"),
            "clip_id",
            "copy_ready_prompt",
            PLACEHOLDER_PATTERNS,
        )
        if not rows:
            return False
        before = {clip.clip_number: clip.copy_ready_prompt for clip in guide.clip_workflows}
        guide.clip_workflows = apply_clip_prompt_enrichment(guide.clip_workflows, rows)
        changed = sum(
            1
            for clip in guide.clip_workflows
            if before.get(clip.clip_number) != clip.copy_ready_prompt
        )
        if not changed:
            return False
        self.repo.save_video_production_guide(guide)
        return True

    @staticmethod
    def _sanitize_enrichment_rows(
        rows: Any,
        key_field: str,
        value_field: str,
        placeholder_patterns: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        """Keep only enrichment rows with a real key and a non-empty value that
        contains no placeholder phrase (honesty gate before the merge)."""
        valid: list[dict[str, Any]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            key = str(row.get(key_field) or "").strip()
            value = str(row.get(value_field) or "").strip()
            if not key or not value:
                continue
            folded = value.casefold()
            if any(str(pattern).casefold() in folded for pattern in placeholder_patterns):
                continue
            valid.append({key_field: key, value_field: value})
        return valid

    def _compact_shot_for_prompts(self, shot: Shot) -> dict[str, Any]:
        return {
            "shot_number": shot.shot_number,
            "start_time": shot.start_time,
            "end_time": shot.end_time,
            "duration_seconds": shot.duration_seconds,
            "shot_size": shot.shot_size,
            "camera": shot.camera,
            "movement": shot.movement,
            "characters": list(shot.characters),
            "action": self._clip_text(shot.action, 300),
            "dialogue": self._clip_text(shot.dialogue, 300),
            "location": shot.location,
            "visual_prompt": self._clip_text(shot.visual_prompt, 500),
            "motion_prompt": self._clip_text(shot.motion_prompt, 200),
            "negative_constraints": list(shot.negative_constraints),
            "continuity_constraints": list(shot.continuity_constraints),
            "sfx": list(shot.sfx),
            "transition": shot.transition,
        }

    @staticmethod
    def _continuity_hint(clip: GenerationClip | None) -> dict[str, Any] | None:
        if clip is None:
            return None
        return {
            "clip_number": clip.clip_number,
            "purpose": NarrativeService._clip_text(clip.purpose, 120),
            "source_shot_numbers": list(clip.source_shot_numbers),
        }

    def _bible_excerpts(
        self, entries: list[dict[str, Any]], names: list[str]
    ) -> list[dict[str, Any]]:
        """Compact visual-bible excerpts for the names used by a batch."""
        excerpts: list[dict[str, Any]] = []
        wanted = {name.casefold() for name in names if name}
        for entry in entries:
            if not isinstance(entry, dict) or not wanted:
                continue
            keys = {
                str(entry.get(key, "")).casefold()
                for key in ("name", "id", "character_id", "location_id")
            }
            if not (wanted & keys):
                continue
            text = ""
            for key in ("visual_description", "description", "visual", "summary"):
                value = entry.get(key)
                if isinstance(value, str) and value.strip():
                    text = value.strip()
                    break
            excerpts.append(
                {
                    "name": next(
                        (str(entry[key]) for key in ("name", "id") if entry.get(key)), ""
                    ),
                    "visual": self._clip_text(text, 400),
                }
            )
        return excerpts

    # ------------------------------------------------------------------
    # Background jobs (existing job-control semantics)
    # ------------------------------------------------------------------
    def create_job(
        self, kind: str, project_id: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if kind not in NARRATIVE_JOB_KINDS:
            raise ValueError(f"Unknown narrative job kind: {kind}")
        job_id = new_id("njob")
        now = datetime.now(UTC).isoformat()
        row = {
            "id": job_id,
            "project_id": project_id,
            "kind": kind,
            "status": "created",
            "payload": payload or {},
            "progress": JobProgress(stage="created", label=kind).model_dump(mode="json"),
            "result": {},
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        self._save_job_row(row)
        self._jobs[job_id] = row
        self.job_control.pause_event(job_id)
        self._start_worker(job_id, f"narrative-{kind}-{job_id}")
        return self._public_job(row)

    def _save_job_row(self, row: dict[str, Any]) -> None:
        standalone = not self.continuum.database.conn.in_transaction
        self.continuum.database.conn.execute(
            """
            INSERT INTO narrative_jobs (
              id, project_id, kind, status, payload_json, progress_json, result_json,
              error, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              status = excluded.status, progress_json = excluded.progress_json,
              result_json = excluded.result_json, error = excluded.error,
              updated_at = excluded.updated_at
            """,
            (
                row["id"],
                row["project_id"],
                row["kind"],
                row["status"],
                dumps(row["payload"]),
                dumps(row["progress"]),
                dumps(row["result"]),
                row["error"],
                row["created_at"],
                datetime.now(UTC).isoformat(),
            ),
        )
        if standalone:
            self.continuum.database.conn.commit()
        self._emit(row, "narrative_job_progress")

    def _load_job_row(self, job_id: str) -> dict[str, Any] | None:
        row = self._jobs.get(job_id)
        if row:
            return row
        db_row = self.continuum.database.conn.execute(
            "SELECT * FROM narrative_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if not db_row:
            return None
        row = {
            "id": db_row["id"],
            "project_id": db_row["project_id"],
            "kind": db_row["kind"],
            "status": db_row["status"],
            "payload": loads(db_row["payload_json"]),
            "progress": loads(db_row["progress_json"]),
            "result": loads(db_row["result_json"]),
            "error": db_row["error"],
            "created_at": db_row["created_at"],
            "updated_at": db_row["updated_at"],
        }
        self._jobs[job_id] = row
        return row

    def get_job(self, job_id: str) -> dict[str, Any]:
        row = self._load_job_row(job_id)
        if not row:
            raise KeyError(f"Narrative job not found: {job_id}")
        return self._public_job(row)

    def list_jobs(self, project_id: str | None = None) -> list[dict[str, Any]]:
        if project_id:
            rows = self.continuum.database.conn.execute(
                "SELECT id FROM narrative_jobs WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        else:
            rows = self.continuum.database.conn.execute(
                "SELECT id FROM narrative_jobs ORDER BY created_at DESC"
            ).fetchall()
        result = []
        for r in rows:
            row = self._load_job_row(r["id"])
            if row:
                result.append(self._public_job(row))
        return result

    def pause_job(self, job_id: str) -> None:
        row = self._load_job_row(job_id)
        if not row:
            raise KeyError(job_id)
        self.job_control.request_pause(job_id)
        if row["status"] not in ("running", "created"):
            return
        row["status"] = "pause_requested"
        self._save_job_row(row)

    def resume_job(self, job_id: str) -> dict[str, Any]:
        row = self._load_job_row(job_id)
        if not row:
            raise KeyError(job_id)
        self.job_control.clear_pause(job_id)
        if row["status"] in ("paused", "pause_requested", "failed", "created"):
            row["status"] = "running"
            self._save_job_row(row)
            self._start_worker(job_id, f"narrative-resume-{job_id}")
        return self._public_job(row)

    def cancel_job(self, job_id: str) -> None:
        self._load_job_row(job_id)
        self.job_control.request_cancel(job_id)
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()

    def retry_job(self, job_id: str) -> dict[str, Any]:
        row = self._load_job_row(job_id)
        if not row:
            raise KeyError(job_id)
        if row["status"] != "failed":
            raise ValueError("Only failed narrative jobs can be retried")
        row["status"] = "running"
        row["error"] = None
        self._save_job_row(row)
        self.job_control.clear_all(job_id)
        self._start_worker(job_id, f"narrative-retry-{job_id}")
        return self._public_job(row)

    def dismiss_job(self, job_id: str) -> bool:
        row = self._load_job_row(job_id)
        if not row:
            return False
        if row["status"] not in ("completed", "failed", "cancelled"):
            raise NarrativeJobError("JobNotTerminalError")
        self.continuum.database.conn.execute("DELETE FROM narrative_jobs WHERE id = ?", (job_id,))
        self.continuum.database.conn.commit()
        self._jobs.pop(job_id, None)
        self.job_control.release(job_id)
        return True

    def reclaim_orphaned_jobs(self) -> None:
        """Fail jobs left running after a process restart so the UI can retry."""

        try:
            rows = self.continuum.database.conn.execute(
                "SELECT id FROM narrative_jobs "
                "WHERE status IN ('created', 'running', 'pause_requested')"
            ).fetchall()
        except Exception:
            return
        for record in rows:
            row = self._load_job_row(record["id"])
            if not row:
                continue
            row["status"] = "failed"
            row["error"] = dumps(
                {
                    "code": "NARRATIVE_JOB_INTERRUPTED",
                    "message": "Process restarted before this job finished. Retry it.",
                    "stage": row.get("kind") or "narrative",
                    "retryable": True,
                }
            )
            progress = JobProgress.model_validate(row["progress"])
            progress.touch_worker(WorkerState.LOST, finished_at=progress_now())
            row["progress"] = progress.model_dump(mode="json")
            self._save_job_row(row)

    def _start_worker(self, job_id: str, task_name: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Sync context (MCP/CLI/tests): jobs are synchronous workflows, so
            # run to completion instead of requiring a live event loop.
            asyncio.run(self._run_job(job_id))
            return
        self._tasks[job_id] = loop.create_task(self._run_job(job_id), name=task_name)

    async def _run_job(self, job_id: str) -> None:
        row = self._load_job_row(job_id)
        if not row:
            return
        trace_start = len(self._runtime_traces)
        job_started = time.monotonic()
        try:
            row["status"] = "running"
            self._set_stage(row, "running", "Narrative job running")
            payload = row["payload"]
            kind = row["kind"]
            runtime = payload.get("runtime")
            generation_mode = payload.get("generation_mode")
            if kind == "story_bible":
                result = await self.generate_story_bible(
                    row["project_id"], runtime=runtime, generation_mode=generation_mode
                )
                row["result"] = {"bible_version": result.version}
            elif kind == "outline":
                def on_outline_progress(stage: str, label: str, percent: int) -> None:
                    self._raise_if_pause_or_cancel(row)
                    self._set_stage(row, stage, label, percent=percent)

                plans = await self.generate_outline(
                    row["project_id"],
                    payload.get("episode_count"),
                    runtime=runtime,
                    generation_mode=generation_mode,
                    progress=on_outline_progress,
                )
                audit = {}
                if plans:
                    audit = dict((plans[0].runtime_trace or {}).get("global_audit") or {})
                row["result"] = {
                    "episodes": [p.episode_number for p in plans],
                    "audit": audit,
                }
            elif kind == "forecast":
                def on_forecast_progress(stage: str, label: str, percent: int) -> None:
                    self._raise_if_pause_or_cancel(row)
                    self._set_stage(row, stage, label, percent=percent)

                forecast = await self.forecast_episode_async(
                    row["project_id"],
                    int(payload.get("episode_number", 1)),
                    list(payload.get("directions") or []),
                    horizon_episodes=int(payload.get("horizon_episodes", 3)),
                    runtime=runtime,
                    generation_mode=generation_mode,
                    progress=on_forecast_progress,
                )
                row["result"] = {"forecast_id": forecast.id}
            elif kind == "simulation":
                scene = self.repo.get_scene(str(payload.get("scene_id") or ""))
                if scene is None:
                    raise KeyError("Narrative scene not found")
                simulated = await self.simulate_scene(
                    row["project_id"],
                    scene,
                    branch_id=payload.get("branch_id"),
                    runtime=runtime,
                    generation_mode=generation_mode,
                )
                row["result"] = {"scene_id": simulated.id}
            elif kind == "writer_room":
                row["result"] = await self.run_writer_room(
                    row["project_id"],
                    int(payload.get("episode_number", 1)),
                    list(payload.get("participants") or []),
                    cross_review=bool(payload.get("cross_review", True)),
                )
            elif kind == "screenwriter":
                version = await self.generate_episode_draft(
                    row["project_id"],
                    int(payload.get("episode_number", 1)),
                    runtime=runtime,
                    generation_mode=generation_mode,
                )
                row["result"] = {"version_id": version.id}
            elif kind == "audit":
                report = await self.audit_episode_async(
                    row["project_id"],
                    str(payload.get("version_id") or ""),
                    runtime=runtime,
                    generation_mode=generation_mode,
                )
                row["result"] = {
                    "audit_id": report.id,
                    "passed": report.passed,
                    "blocking": report.blocking_count,
                }
            elif kind == "production_package":
                package = await self.generate_production_package_async(
                    row["project_id"],
                    int(payload.get("episode_number", 1)),
                    payload.get("version_id"),
                    runtime=runtime,
                    generation_mode=generation_mode,
                )
                row["result"] = {"package_id": package.id}
            elif kind == "model_prompt_package":

                def on_prompt_package_progress(
                    stage: str, completed: int | None, total: int | None
                ) -> None:
                    self._raise_if_pause_or_cancel(row)
                    values: dict[str, Any] = {}
                    if completed is not None:
                        values["completed"] = completed
                    if total is not None:
                        values["total"] = total
                    # Real counts only; percent stays untouched when unknown.
                    self._set_stage(
                        row,
                        stage,
                        MODEL_PROMPT_PACKAGE_STAGE_LABELS.get(stage, stage),
                        **values,
                    )

                prompt_package = await self.generate_model_prompt_package_async(
                    row["project_id"],
                    str(payload.get("production_package_id") or ""),
                    str(payload.get("profile_id") or ""),
                    aspect_ratio=str(payload.get("aspect_ratio") or "16:9"),
                    quality_priority=str(payload.get("quality_priority") or "balanced"),
                    generation_strategy=str(payload.get("generation_strategy") or "auto"),
                    continuity_strategy=str(payload.get("continuity_strategy") or "auto"),
                    audio_strategy=str(payload.get("audio_strategy") or "auto"),
                    prompt_language=str(payload.get("prompt_language") or "auto"),
                    progress_callback=on_prompt_package_progress,
                    stop_after_plan=bool(payload.get("stop_after_plan")),
                )
                row["result"] = {"model_prompt_package_id": prompt_package.id}
            elif kind == "video_production_guide":

                def on_guide_progress(
                    stage: str, completed: int | None, total: int | None
                ) -> None:
                    self._raise_if_pause_or_cancel(row)
                    values: dict[str, Any] = {}
                    if completed is not None:
                        values["completed"] = completed
                    if total is not None:
                        values["total"] = total
                    # Real counts only; percent stays untouched when unknown.
                    self._set_stage(
                        row,
                        stage,
                        GUIDE_STAGE_LABELS.get(stage, stage),
                        **values,
                    )

                guide = await self.generate_video_production_guide_async(
                    row["project_id"],
                    str(payload.get("prompt_package_id") or ""),
                    progress_callback=on_guide_progress,
                )
                row["result"] = {"production_guide_id": guide.id}
            elif kind == "complete_video_production":

                def on_complete_plan_progress(
                    stage: str, completed: int | None, total: int | None
                ) -> None:
                    self._raise_if_pause_or_cancel(row)
                    values: dict[str, Any] = {}
                    if completed is not None:
                        values["completed"] = completed
                    if total is not None:
                        values["total"] = total
                    # Real counts only; percent stays untouched when unknown.
                    self._set_stage(
                        row,
                        stage,
                        COMPLETE_VIDEO_PRODUCTION_STAGE_LABELS.get(stage, stage),
                        **values,
                    )

                guide = await self.generate_complete_video_production_plan_async(
                    row["project_id"],
                    str(payload.get("production_package_id") or ""),
                    str(payload.get("target_profile_id") or payload.get("profile_id") or ""),
                    aspect_ratio=str(payload.get("aspect_ratio") or "16:9"),
                    quality_priority=str(payload.get("quality_priority") or "balanced"),
                    generation_strategy=str(payload.get("generation_strategy") or "auto"),
                    continuity_strategy=str(payload.get("continuity_strategy") or "auto"),
                    audio_strategy=str(payload.get("audio_strategy") or "auto"),
                    prompt_language=str(payload.get("prompt_language") or "auto"),
                    progress_callback=on_complete_plan_progress,
                )
                row["result"] = {
                    "production_guide_id": guide.id,
                    "model_prompt_package_id": guide.prompt_package_id,
                }
            elif kind == "episode_pipeline":
                steps = payload.get("steps", ["prepare", "draft", "audit"])
                results: dict[str, Any] = {}
                for idx, step in enumerate(steps):
                    self._raise_if_pause_or_cancel(row)
                    self._set_stage(
                        row,
                        step,
                        f"Stage {step}",
                        percent=int(idx * 100 / max(len(steps), 1)),
                    )
                    if step == "prepare":
                        results["prepare"] = self.prepare_episode(
                            row["project_id"], int(payload["episode_number"])
                        )
                    elif step == "draft":
                        version = await self.generate_episode_draft(
                            row["project_id"],
                            int(payload["episode_number"]),
                            runtime=runtime,
                            generation_mode=generation_mode,
                        )
                        results["draft"] = {"version_id": version.id}
                    elif step == "audit":
                        version_id = (results.get("draft") or {}).get("version_id")
                        if version_id:
                            report = await self.audit_episode_async(
                                row["project_id"],
                                version_id,
                                runtime=runtime,
                                generation_mode=generation_mode,
                            )
                            results["audit"] = {
                                "passed": report.passed,
                                "blocking": report.blocking_count,
                            }
                    elif step == "commit":
                        version_id = (results.get("draft") or {}).get("version_id")
                        if version_id:
                            results["commit"] = self.commit_episode(
                                row["project_id"],
                                int(payload["episode_number"]),
                                version_id,
                            )
                row["result"] = results
            result_payload = dict(row.get("result") or {})
            result_payload["execution"] = {
                "generation_mode": generation_mode or "auto",
                "runtime": normalize_runtime(runtime),
                "duration_ms": round((time.monotonic() - job_started) * 1000, 1),
                "calls": [dict(item) for item in self._runtime_traces[trace_start:]],
            }
            row["result"] = result_payload
            row["status"] = "completed"
            self._set_stage(row, "completed", "Done", percent=100)
        except asyncio.CancelledError:
            row["status"] = "cancelled"
            self._save_job_row(row)
            self.job_control.clear_all(job_id)
            self.job_control.release(job_id)
        except _PauseRequested:
            row["status"] = "paused"
            row["progress"]["worker_state"] = WorkerState.PAUSED.value
            self._save_job_row(row)
            self.job_control.clear_pause(job_id)
        except Exception as exc:
            row["status"] = "failed"
            row["error"] = (
                dumps(exc.to_dict()) if isinstance(exc, NarrativeAgentError) else str(exc)
            )
            self._save_job_row(row)
            self.job_control.clear_all(job_id)
            self.job_control.release(job_id)
        finally:
            self._tasks.pop(job_id, None)

    def _raise_if_pause_or_cancel(self, row: dict[str, Any]) -> None:
        if self.job_control.cancel_requested(row["id"]):
            raise asyncio.CancelledError()
        if self.job_control.pause_requested(row["id"]):
            raise _PauseRequested()

    def _set_stage(
        self,
        row: dict[str, Any],
        stage: str,
        label: str,
        percent: int | None = None,
        **values: Any,
    ) -> None:
        progress = JobProgress.model_validate(row["progress"])
        progress.update_stage(stage, label=label, percent=percent, **values)
        progress.touch_worker(WorkerState.RUNNING)
        row["progress"] = progress.model_dump(mode="json")
        self._save_job_row(row)

    def _public_job(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "kind": row["kind"],
            "status": row["status"],
            "progress": row["progress"],
            "result": row["result"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def subscribe_events(self, job_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._job_event_queues.setdefault(job_id, []).append(queue)
        return queue

    def unsubscribe_events(self, job_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._job_event_queues.setdefault(job_id, []).remove(queue)

    def _emit(self, row: dict[str, Any], event_type: str) -> None:
        event = {
            "event": event_type,
            "job_id": row["id"],
            "status": row["status"],
            "progress": row["progress"],
        }
        for queue in list(self._job_event_queues.get(row["id"], [])):
            with contextlib.suppress(Exception):
                queue.put_nowait(event)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _mark_dependent_artifacts_stale(self, project: NarrativeProject) -> None:
        fingerprint = self.context_fingerprint(project)
        for forecast in self.repo.list_forecasts(project.id):
            if forecast.context_fingerprint != fingerprint and not forecast.stale:
                forecast.stale = True
                forecast.status = (
                    ForecastStatus.STALE
                    if forecast.status == ForecastStatus.COMPLETED
                    else forecast.status
                )
                self.repo.save_forecast(forecast)
        for version in self.repo.list_stale_episode_versions(project.id):
            if version.context_fingerprint != fingerprint and not version.stale:
                version.stale = True
                self.repo.save_episode_version(version)
                # A draft lineage losing freshness invalidates the derived
                # production artifacts of the same episode (Task B sweep).
                self.repo.mark_production_packages_stale(project.id, version.episode_number)
                self.repo.mark_model_prompt_packages_stale(project.id, version.episode_number)
                self.repo.mark_video_production_guides_stale(
                    project.id, version.episode_number
                )
        # Production artifacts anchored on canon versions go stale by their
        # own recorded context fingerprint, mirroring the forecast semantics
        # above (the canon version row itself is never flagged). Per-package
        # targeting avoids flagging fresh siblings of the same episode.
        for package in self.repo.list_production_packages(project.id):
            if (
                package.context_fingerprint
                and package.context_fingerprint != fingerprint
                and not package.stale
            ):
                self.repo.mark_production_packages_stale(
                    project.id, package.episode_number, package_id=package.id
                )
        for prompt_package in self.repo.list_model_prompt_packages(project.id):
            if (
                prompt_package.context_fingerprint
                and prompt_package.context_fingerprint != fingerprint
                and not prompt_package.stale
            ):
                self.repo.mark_model_prompt_packages_stale(
                    project.id, prompt_package.episode_number, package_id=prompt_package.id
                )
                # Guides inherit staleness from their drifted source package.
                self.repo.mark_video_production_guides_stale(
                    project.id,
                    prompt_package.episode_number,
                    prompt_package_id=prompt_package.id,
                )

    def _next_bible_version(self, project_id: str) -> int:
        versions = self.repo.list_bible_versions(project_id)
        return (versions[0]["version"] + 1) if versions else 1

    def _parse_bible_characters(self, rows: Any) -> list[Any]:
        from persona_continuum.domain.narrative import StoryBibleCharacter

        result = []
        for row in rows or []:
            if isinstance(row, dict) and row.get("id"):
                result.append(StoryBibleCharacter.model_validate(row))
        return result

    def _parse_bible_locations(self, rows: Any) -> list[Any]:
        from persona_continuum.domain.narrative import StoryBibleLocation

        return [
            StoryBibleLocation.model_validate(row)
            for row in (rows or [])
            if isinstance(row, dict) and row.get("id")
        ]

    def _parse_master_timeline(self, rows: Any) -> list[Any]:
        from persona_continuum.domain.narrative import MasterTimelineItem

        return [
            MasterTimelineItem.model_validate(row)
            for row in (rows or [])
            if isinstance(row, dict) and "episode" in row
        ]

    def _run_sync(self, awaitable: Any) -> Any:
        """Run an async narrative stage from CLI/MCP or legacy sync callers."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(awaitable)
        # Some established call sites invoke sync facade methods from async
        # tests. Use a separate thread rather than nesting an event loop.
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="narrative-sync") as pool:
            return pool.submit(asyncio.run, awaitable).result()

    def _resolve_stage_runtime(
        self,
        project: NarrativeProject,
        stage: str,
        explicit: dict[str, Any] | None,
    ) -> dict[str, Any]:
        aliases = {
            "story_architect": ("story_architect", "story_bible"),
            "outline_writer": ("outline_writer", "outline"),
            "forecast_simulator": ("forecast_simulator", "forecast"),
            "scene_actor": ("scene_actor", "actor"),
            "screenwriter": ("screenwriter", "draft"),
            "reviewer": ("reviewer", "audit"),
            "production_planner": ("production_planner", "production"),
            "director": ("director",),
            "shooting_agent": ("shooting_agent", "shooting"),
        }
        selected: dict[str, Any] | None = explicit if explicit else None
        if selected is None:
            for key in aliases.get(stage, (stage,)):
                candidate = project.runtime_assignment.get(key)
                if isinstance(candidate, dict) and candidate:
                    selected = candidate
                    break
        if selected is None:
            default = project.runtime_assignment.get("default")
            selected = default if isinstance(default, dict) else {}
        return normalize_runtime(selected)

    async def _structured_call_async(
        self,
        user_message: str,
        *,
        runtime: dict[str, Any] | None,
        phase: str,
        mode: GenerationMode,
        schema: dict[str, Any],
        included_sections: list[str] | None = None,
        omitted_sections: list[str] | None = None,
        max_retries: int = 2,
        structured_repair_attempts: int = 1,
    ) -> tuple[Any, dict[str, Any]]:
        trace = new_runtime_trace(stage=phase, mode=mode, runtime=runtime)
        trace["included_sections"] = list(included_sections or [])
        trace["omitted_sections"] = list(omitted_sections or [])
        started = time.monotonic()
        if mode == GenerationMode.DETERMINISTIC:
            trace.update(
                {
                    "status": "completed",
                    "completed_at": datetime.now(UTC).isoformat(),
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                }
            )
            self._runtime_traces.append(trace)
            return None, trace

        selected = normalize_runtime(runtime)
        agent_id = selected.get("agent_id")
        if not agent_id:
            error = NarrativeAgentError(
                NARRATIVE_AGENT_RUNTIME_UNAVAILABLE,
                "Agent mode requires a configured runtime",
                stage=phase,
                runtime=selected,
            )
            trace.update({"status": "failed", "failure": error.to_dict()})
            self._runtime_traces.append(trace)
            raise error
        adapter = self.continuum.agent_registry.get_adapter(agent_id)
        if adapter is None:
            error = NarrativeAgentError(
                NARRATIVE_AGENT_RUNTIME_UNAVAILABLE,
                f"Configured Agent Adapter not found: {agent_id}",
                stage=phase,
                runtime=selected,
            )
            trace.update({"status": "failed", "failure": error.to_dict()})
            self._runtime_traces.append(trace)
            raise error

        from persona_continuum.agent.models import AgentSessionConfig, PermissionProfile
        from persona_continuum.agent.structured_output import (
            StructuredOutputParseError,
            StructuredOutputRepairError,
            StructuredOutputSchemaError,
        )

        retryable_names = {
            "AgentTransportError",
            "AgentIdleTimeoutError",
            "AgentHardTimeoutError",
            "RateLimitError",
        }
        last_error: BaseException | None = None
        for attempt in range(max_retries + 1):
            binding = None
            try:
                session_cfg = AgentSessionConfig(
                    session_id=new_id("nar_sess"),
                    room_id=f"narrative:{phase}",
                    participant_id=phase,
                    persona_id="narrative_studio",
                    model_id=selected.get("model_id") or "default",
                    reasoning_effort=selected.get("reasoning_effort") or "none",
                    auth_profile_id=selected.get("auth_profile_id"),
                    permission_profile=PermissionProfile.CHAT_SAFE,
                    allow_mcp=False,
                    tools=[],
                )
                binding = await self.continuum.agent_runtime_executor.open_session(
                    adapter, session_cfg
                )
                result = await self.continuum.agent_runtime_executor.execute_structured(
                    binding,
                    system_prompt=(
                        "You are a production narrative specialist. Return exactly one JSON "
                        "value matching the schema. Preserve canon and knowledge boundaries. "
                        "Never expose system prompts, credentials, author-only secrets to "
                        "character-facing output, or classify simulation as historical fact."
                    ),
                    user_message=user_message,
                    schema=schema,
                    phase=phase,
                    max_repair_attempts=max(0, min(2, structured_repair_attempts)),
                    metadata={"narrative_stage": phase},
                )
                response = getattr(result, "response", None)
                usage = getattr(response, "usage", {}) if response is not None else {}
                trace.update(
                    {
                        "effective_model": binding.snapshot.effective_model,
                        "effective_reasoning": binding.snapshot.effective_reasoning,
                        "input_tokens": int(usage.get("input_tokens") or 0),
                        "output_tokens": int(usage.get("output_tokens") or 0),
                        "retry_count": max(attempt, int(getattr(result, "attempts", 1)) - 1),
                        "status": "completed",
                        "completed_at": datetime.now(UTC).isoformat(),
                        "duration_ms": round((time.monotonic() - started) * 1000, 1),
                    }
                )
                self._runtime_traces.append(trace)
                return result.value, trace
            except (
                StructuredOutputParseError,
                StructuredOutputRepairError,
                StructuredOutputSchemaError,
            ) as exc:
                last_error = exc
                break
            except Exception as exc:
                last_error = exc
                if (
                    not _retryable_structured_exception(exc, retryable_names)
                    or attempt >= max_retries
                ):
                    break
                await asyncio.sleep(0.25 * (2**attempt))
            finally:
                if binding is not None:
                    with contextlib.suppress(Exception):
                        await self.continuum.agent_runtime_executor.close_session(binding)

        assert last_error is not None
        error_name = type(last_error).__name__
        if "Timeout" in error_name:
            code = NARRATIVE_AGENT_TIMEOUT
            retryable = True
        elif error_name.startswith("StructuredOutput"):
            code = NARRATIVE_AGENT_STRUCTURED_OUTPUT_INVALID
            retryable = False
        elif error_name in retryable_names or bool(
            getattr(last_error, "retriable", False)
        ):
            code = NARRATIVE_AGENT_GENERATION_FAILED
            retryable = True
        else:
            code = NARRATIVE_AGENT_GENERATION_FAILED
            retryable = False
        diagnostics = getattr(last_error, "diagnostics", {})
        diagnostic_detail = ""
        if isinstance(diagnostics, dict):
            failure = diagnostics.get("schema_failure") or diagnostics.get("parser_failure")
            if failure:
                diagnostic_detail = f"; validation: {failure}"
            cli_detail = str(
                diagnostics.get("cli_failure") or diagnostics.get("stderr_tail") or ""
            ).strip()
            if cli_detail and cli_detail not in str(last_error):
                diagnostic_detail = f"{diagnostic_detail}; cli: {cli_detail}"
            estimated = diagnostics.get("estimated_prompt_tokens")
            maximum = diagnostics.get("max_prompt_tokens")
            if estimated and maximum:
                diagnostic_detail = (
                    f"{diagnostic_detail}; prompt_tokens={estimated}/{maximum}"
                )
        error = NarrativeAgentError(
            code,
            str(last_error) + diagnostic_detail,
            stage=phase,
            runtime=selected,
            retryable=retryable,
            original_error=last_error,
        )
        trace.update(
            {
                "status": "failed",
                "failure": error.to_dict(),
                "retry_count": min(max_retries, trace.get("retry_count", 0)),
                "completed_at": datetime.now(UTC).isoformat(),
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            }
        )
        self._runtime_traces.append(trace)
        raise error

    @staticmethod
    def _report_outline_progress(
        progress: Callable[[str, str, int], None] | None,
        stage: str,
        label: str,
        percent: int,
    ) -> None:
        if progress is not None:
            progress(stage, label, percent)

    @staticmethod
    def _clip_text(value: Any, limit: int) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + "…"

    def _outline_source_brief(self, project: NarrativeProject, bible: StoryBible) -> str:
        format_value = (
            project.format.value if hasattr(project.format, "value") else str(project.format)
        )
        return dumps(
            {
                "title": project.title,
                "logline": project.logline,
                "description": self._clip_text(project.description, 240),
                "format": format_value,
                "genre": list(project.genre),
                "tone": list(project.tone),
                "planned_episode_count": project.planned_episode_count,
                "premise": self._clip_text(bible.premise, 240),
                "core_question": self._clip_text(bible.core_question, 160),
                "theme": self._clip_text(bible.theme, 160),
                "world_rules": [self._clip_text(item, 80) for item in list(bible.world_rules)[:8]],
                "final_truth": [self._clip_text(item, 80) for item in list(bible.final_truth)[:6]],
                "characters": [
                    {"id": character.id, "name": character.name, "role": character.role}
                    for character in list(bible.characters)[:16]
                ],
            }
        )

    @classmethod
    def _compact_episode_for_audit(cls, plan: EpisodePlan) -> dict[str, Any]:
        return {
            "episode": plan.episode_number,
            "title": cls._clip_text(plan.title, 80),
            "goal": cls._clip_text(plan.narrative_goal, 160),
            "hook": cls._clip_text(plan.hook, 120),
            "beats": [
                {
                    "order": beat.order,
                    "title": cls._clip_text(beat.title, 60),
                    "description": cls._clip_text(beat.description, 160),
                }
                for beat in plan.beats
            ],
            "must_happen": [cls._clip_text(item, 80) for item in plan.must_happen[:4]],
            "characters": [cls._clip_text(item, 40) for item in plan.required_characters[:4]],
            "plot_threads": [cls._clip_text(item, 40) for item in plan.plot_threads[:6]],
            "clues_to_plant": [cls._clip_text(item, 60) for item in plan.clues_to_plant[:6]],
            "clues_to_echo": [cls._clip_text(item, 60) for item in plan.clues_to_echo[:6]],
            "reveal_targets": [cls._clip_text(item, 60) for item in plan.reveal_targets[:6]],
            "forbidden_reveals": [cls._clip_text(item, 60) for item in plan.forbidden_reveals[:6]],
            "cliffhanger": cls._clip_text(plan.cliffhanger, 120),
        }

    @classmethod
    def _outline_continuity_map(cls, plans: list[EpisodePlan]) -> list[dict[str, Any]]:
        return [
            {
                "episode": plan.episode_number,
                "goal": cls._clip_text(plan.narrative_goal, 80),
                "plant": [cls._clip_text(item, 40) for item in plan.clues_to_plant[:4]],
                "echo": [cls._clip_text(item, 40) for item in plan.clues_to_echo[:4]],
                "reveal": [cls._clip_text(item, 40) for item in plan.reveal_targets[:4]],
                "forbid": [cls._clip_text(item, 40) for item in plan.forbidden_reveals[:4]],
                "threads": [cls._clip_text(item, 32) for item in plan.plot_threads[:4]],
            }
            for plan in plans
        ]

    @classmethod
    def _outline_windows(
        cls,
        plans: list[EpisodePlan],
        extra: Any = None,
        *,
        budget_bytes: int = OUTLINE_AUDIT_PAYLOAD_BUDGET_BYTES,
    ) -> list[list[EpisodePlan]]:
        extra_bytes = len(dumps(extra).encode("utf-8")) if extra is not None else 0
        windows: list[list[EpisodePlan]] = []
        current: list[EpisodePlan] = []
        for plan in plans:
            candidate = [*current, plan]
            payload_bytes = extra_bytes + len(
                dumps([cls._compact_episode_for_audit(item) for item in candidate]).encode("utf-8")
            )
            if current and (
                payload_bytes > budget_bytes or len(current) >= OUTLINE_CHUNK_SIZE
            ):
                windows.append(current)
                current = [plan]
            else:
                current = candidate
        if current:
            windows.append(current)
        return windows

    @staticmethod
    def _outline_audit_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["blocking_issues", "warnings"],
            "properties": {
                "blocking_issues": {"type": "array", "items": {"type": "string"}},
                "warnings": {"type": "array", "items": {"type": "string"}},
            },
        }

    async def _agent_audit_outline(
        self,
        plans: list[EpisodePlan],
        *,
        runtime: dict[str, Any],
        mode: GenerationMode,
        phase: str,
        progress: Callable[[str, str, int], None] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        blocking: list[str] = []
        warnings: list[str] = []
        traces: list[dict[str, Any]] = []
        continuity = self._outline_continuity_map(plans)
        if len(plans) > OUTLINE_CHUNK_SIZE:
            self._report_outline_progress(
                progress,
                phase,
                "正在审计系列连续性",
                74 if phase == "outline_global_audit" else 90,
            )
            series_result, series_trace = await self._structured_call_async(
                "Audit this master outline continuity map for repetition, secret/reveal "
                "timing, clue lifecycle, plot-thread closure, and ending reachability. "
                "Return blocking_issues and warnings.\n"
                f"Series map: {dumps(continuity)}",
                runtime=runtime,
                phase=phase,
                mode=mode,
                schema=self._outline_audit_schema(),
                included_sections=["reveal_schedule", "clue_lifecycle"],
            )
            traces.append(series_trace)
            blocking.extend(list((series_result or {}).get("blocking_issues") or []))
            warnings.extend(list((series_result or {}).get("warnings") or []))
        instruction = (
            "Audit this master outline window for repetition, character arc continuity, "
            "secret/reveal timing, plot-thread closure, clue lifecycle, ending reachability, "
            "and pacing. Return blocking_issues and warnings.\n"
            if phase == "outline_global_audit"
            else (
                "Final pass: audit this repaired outline window. Only report blocking issues "
                "that make the episode sequence causally impossible, violate knowledge/reveal "
                "timing, or exceed stated episode capacity. Return blocking_issues and "
                "warnings.\n"
            )
        )
        windows = self._outline_windows(plans)
        for window_index, window in enumerate(windows):
            base = 76 if phase == "outline_global_audit" else 92
            span = 12 if phase == "outline_global_audit" else 6
            self._report_outline_progress(
                progress,
                phase,
                f"正在审计 EP{window[0].episode_number}-EP{window[-1].episode_number}",
                base + int(span * window_index / max(len(windows), 1)),
            )
            digest = [self._compact_episode_for_audit(plan) for plan in window]
            result, trace = await self._structured_call_async(
                instruction
                + f"Window: EP{window[0].episode_number}-EP{window[-1].episode_number}\n"
                + dumps(digest),
                runtime=runtime,
                phase=phase,
                mode=mode,
                schema=self._outline_audit_schema(),
                included_sections=["master_outline", "reveal_schedule", "clue_lifecycle"],
            )
            traces.append(trace)
            blocking.extend(list((result or {}).get("blocking_issues") or []))
            warnings.extend(list((result or {}).get("warnings") or []))
        return (
            {
                "blocking_issues": list(dict.fromkeys(item for item in blocking if item)),
                "warnings": list(dict.fromkeys(item for item in warnings if item)),
            },
            traces,
        )

    async def _agent_repair_outline(
        self,
        project: NarrativeProject,
        plans: list[EpisodePlan],
        blocking: list[str],
        *,
        runtime: dict[str, Any],
        mode: GenerationMode,
        progress: Callable[[str, str, int], None] | None = None,
    ) -> tuple[list[EpisodePlan], list[dict[str, Any]]]:
        traces: list[dict[str, Any]] = []
        by_number = {plan.episode_number: plan for plan in plans}
        windows = self._outline_windows(plans, extra={"issues": blocking[:12]})
        for window_index, window in enumerate(windows):
            numbers = [plan.episode_number for plan in window]
            self._report_outline_progress(
                progress,
                "outline_repair",
                f"正在修复 EP{numbers[0]}-EP{numbers[-1]}",
                82 + int(8 * window_index / max(len(windows), 1)),
            )
            digest = [self._compact_episode_for_audit(plan) for plan in window]
            result, trace = await self._structured_call_async(
                "Repair this master outline window using the blocking audit issues. Return "
                "episode_plans for exactly this window, keep exact episode numbering, use "
                "2-6 concrete causal beats per episode, at most 4 required characters and 4 "
                "must-happen events per episode, and fit each episode's duration.\n"
                f"Window: EP{numbers[0]}-EP{numbers[-1]}\n"
                f"Blocking issues: {dumps(blocking[:12])}\n"
                f"Current outline: {dumps(digest)}",
                runtime=runtime,
                phase="outline_repair",
                mode=mode,
                schema=self._outline_chunk_schema(),
                included_sections=["master_outline", "blocking_audit"],
            )
            traces.append(trace)
            rows = list((result or {}).get("episode_plans") or [])
            actual = [int(row.get("episode_number", 0)) for row in rows]
            if actual != numbers:
                raise NarrativeAgentError(
                    NARRATIVE_AGENT_STRUCTURED_OUTPUT_INVALID,
                    f"Outline repair returned episodes {actual}; expected {numbers}",
                    stage="outline_repair",
                    runtime=runtime,
                )
            for row in rows:
                by_number[int(row["episode_number"])] = self._episode_plan_from_ai(
                    project.id, project, row, trace
                )
        repaired = [by_number[number] for number in range(1, len(plans) + 1)]
        return repaired, traces

    @staticmethod
    def _outline_chunk_schema() -> dict[str, Any]:
        string_array = {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 8,
        }
        object_array = {
            "type": "array",
            "items": {"type": "object"},
            "maxItems": 6,
        }
        beat_array = {
            "type": "array",
            "minItems": 2,
            "maxItems": 6,
            "items": {
                "type": "object",
                "required": ["order", "title", "description"],
                "properties": {
                    "order": {"type": "integer"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
        }
        return {
            "type": "object",
            "required": ["episode_plans"],
            "properties": {
                "episode_plans": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": [
                            "episode_number",
                            "title",
                            "narrative_goal",
                            "hook",
                            "beats",
                            "must_happen",
                            "must_not_happen",
                            "characters",
                            "plot_threads",
                            "clues_to_plant",
                            "clues_to_echo",
                            "reveal_targets",
                            "forbidden_reveals",
                            "relationship_targets",
                            "emotion_targets",
                            "world_state_targets",
                            "cliffhanger",
                        ],
                        "properties": {
                            "episode_number": {"type": "integer"},
                            "title": {"type": "string"},
                            "narrative_goal": {"type": "string"},
                            "hook": {"type": "string"},
                            "beats": beat_array,
                            "must_happen": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 4,
                            },
                            "must_not_happen": string_array,
                            "characters": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 4,
                            },
                            "plot_threads": string_array,
                            "clues_to_plant": string_array,
                            "clues_to_echo": string_array,
                            "reveal_targets": string_array,
                            "forbidden_reveals": string_array,
                            "relationship_targets": object_array,
                            "emotion_targets": object_array,
                            "world_state_targets": object_array,
                            "cliffhanger": {"type": "string"},
                        },
                    },
                }
            },
        }

    @staticmethod
    def _screenwriter_schema() -> dict[str, Any]:
        string_array = {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 10,
        }
        return {
            "type": "object",
            "required": [
                "episode_number",
                "title",
                "duration",
                "hook",
                "scenes",
                "reveals",
                "clues_planted",
                "clues_echoed",
                "relationship_changes",
                "knowledge_changes",
                "cliffhanger",
            ],
            "properties": {
                "episode_number": {"type": "integer"},
                "title": {"type": "string"},
                "duration": {"type": "integer"},
                "hook": {"type": "string"},
                "scenes": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 6,
                    "items": {
                        "type": "object",
                        "required": [
                            "scene_number",
                            "location",
                            "time",
                            "characters",
                            "duration",
                            "action",
                            "dialogue",
                            "visual_direction",
                            "narrative_function",
                        ],
                        "properties": {
                            "scene_number": {"type": "integer"},
                            "location": {"type": "string"},
                            "time": {"type": "string"},
                            "characters": string_array,
                            "duration": {"type": "integer"},
                            "action": string_array,
                            "dialogue": {
                                "type": "array",
                                "maxItems": 16,
                                "items": {
                                    "type": "object",
                                    "required": ["speaker", "text"],
                                    "properties": {
                                        "speaker": {"type": "string"},
                                        "text": {"type": "string"},
                                    },
                                },
                            },
                            "visual_direction": {"type": "string"},
                            "narrative_function": {"type": "string"},
                        },
                    },
                },
                "reveals": string_array,
                "clues_planted": string_array,
                "clues_echoed": string_array,
                "relationship_changes": {
                    "type": "array",
                    "items": {"type": "object"},
                    "maxItems": 8,
                },
                "knowledge_changes": {
                    "type": "array",
                    "items": {"type": "object"},
                    "maxItems": 8,
                },
                "cliffhanger": {"type": "string"},
            },
        }

    @staticmethod
    def _forecast_evaluation_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["scores", "risks"],
            "properties": {
                "scores": {"type": "object"},
                "risks": {"type": "array", "items": {"type": "string"}},
            },
        }

    @classmethod
    def _compact_forecast_direction_for_eval(cls, direction: ForecastDirection) -> dict[str, Any]:
        payload = {
            "label": cls._clip_text(direction.label, 40),
            "description": cls._clip_text(direction.description, 360),
            "summary": cls._clip_text(direction.summary, 720),
            "steps": [
                {
                    "episode": step.get("episode_number"),
                    "summary": cls._clip_text(step.get("summary"), 240),
                    "events": [
                        cls._clip_text(item, 140) for item in list(step.get("events") or [])[:3]
                    ],
                }
                for step in list(direction.steps)[:5]
            ],
        }
        raw = dumps(payload)
        if len(raw.encode("utf-8")) <= OUTLINE_AUDIT_PAYLOAD_BUDGET_BYTES:
            return payload
        payload["summary"] = cls._clip_text(direction.summary, 240)
        payload["steps"] = [
            {
                "episode": step.get("episode_number"),
                "summary": cls._clip_text(step.get("summary"), 120),
            }
            for step in list(direction.steps)[:5]
        ]
        return payload

    @staticmethod
    def _production_planner_schema() -> dict[str, Any]:
        string_array = {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 12,
        }
        return {
            "type": "object",
            "required": ["shot_list", "bgm_direction", "continuity_notes"],
            "properties": {
                "shot_list": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 80,
                    "items": {
                        "type": "object",
                        "required": [
                            "shot_number",
                            "start_time",
                            "end_time",
                            "duration_seconds",
                            "shot_size",
                            "camera",
                            "movement",
                            "characters",
                            "action",
                            "dialogue",
                            "location",
                            "visual_prompt",
                            "motion_prompt",
                            "negative_constraints",
                            "continuity_constraints",
                            "sfx",
                            "bgm",
                            "transition",
                        ],
                        "properties": {
                            "shot_number": {"type": "integer"},
                            "start_time": {"type": "string"},
                            "end_time": {"type": "string"},
                            "duration_seconds": {"type": "number"},
                            "shot_size": {"type": "string"},
                            "camera": {"type": "string"},
                            "movement": {"type": "string"},
                            "characters": string_array,
                            "action": {"type": "string"},
                            "dialogue": {"type": "string"},
                            "voiceover": {"type": "string"},
                            "location": {"type": "string"},
                            "visual_prompt": {"type": "string"},
                            "motion_prompt": {"type": "string"},
                            "negative_constraints": string_array,
                            "continuity_constraints": string_array,
                            "sfx": string_array,
                            "bgm": {"type": "string"},
                            "transition": {"type": "string"},
                        },
                    },
                },
                "bgm_direction": {"type": "string"},
                "continuity_notes": {"type": "array", "items": {"type": "string"}},
            },
        }

    @staticmethod
    def _episode_plan_from_ai(
        project_id: str,
        project: NarrativeProject,
        row: dict[str, Any],
        trace: dict[str, Any],
    ) -> EpisodePlan:
        return EpisodePlan(
            project_id=project_id,
            episode_number=int(row["episode_number"]),
            title=str(row["title"]),
            narrative_goal=str(row["narrative_goal"]),
            hook=str(row["hook"]),
            beats=[Beat.model_validate(item) for item in row.get("beats", [])],
            must_happen=[str(item) for item in row.get("must_happen", [])],
            must_not_happen=[str(item) for item in row.get("must_not_happen", [])],
            required_characters=[str(item) for item in row.get("characters", [])],
            plot_threads=[str(item) for item in row.get("plot_threads", [])],
            clues_to_plant=[str(item) for item in row.get("clues_to_plant", [])],
            clues_to_echo=[str(item) for item in row.get("clues_to_echo", [])],
            required_clues=[
                str(item) for item in row.get("clues_to_plant", []) + row.get("clues_to_echo", [])
            ],
            reveal_targets=[str(item) for item in row.get("reveal_targets", [])],
            forbidden_reveals=[str(item) for item in row.get("forbidden_reveals", [])],
            relationship_targets=list(row.get("relationship_targets", [])),
            emotion_targets=list(row.get("emotion_targets", [])),
            world_state_targets=list(row.get("world_state_targets", [])),
            cliffhanger=str(row.get("cliffhanger", "")),
            estimated_duration_seconds=project.episode_duration_seconds_max,
            status=EpisodeStatus.PLANNED,
            generation_mode=GenerationMode.AGENT,
            runtime_trace=dict(trace),
        )

    @staticmethod
    def _audit_outline(plans: list[EpisodePlan]) -> list[str]:
        issues: list[str] = []
        numbers = [plan.episode_number for plan in plans]
        if numbers != list(range(1, len(plans) + 1)):
            issues.append("episode numbering is not contiguous")
        seen_goals: dict[str, int] = {}
        revealed: dict[str, int] = {}
        for plan in plans:
            if len(plan.beats) < 2 or len(plan.beats) > 6:
                issues.append(f"EP{plan.episode_number} must contain 2-6 concrete causal beats")
            if any(not beat.title.strip() or not beat.description.strip() for beat in plan.beats):
                issues.append(f"EP{plan.episode_number} contains an empty beat")
            if len(plan.required_characters) > 4:
                issues.append(f"EP{plan.episode_number} exceeds 4 required characters")
            if len(plan.must_happen) > 4:
                issues.append(f"EP{plan.episode_number} exceeds 4 must-happen events")
            goal = plan.narrative_goal.strip().casefold()
            if goal and goal in seen_goals:
                issues.append(
                    f"duplicate narrative goal in EP{seen_goals[goal]} and EP{plan.episode_number}"
                )
            seen_goals[goal] = plan.episode_number
            for reveal in plan.reveal_targets:
                key = reveal.strip().casefold()
                if key in revealed:
                    issues.append(
                        "reveal repeated as first reveal in "
                        f"EP{revealed[key]} and EP{plan.episode_number}"
                    )
                revealed[key] = plan.episode_number
        return issues

    @staticmethod
    def _render_structured_draft(data: dict[str, Any]) -> dict[str, Any]:
        scenes = list(data.get("scenes") or [])
        lines = [
            f"EP{int(data.get('episode_number', 0)):02d} 《{data.get('title', '')}》",
            f"HOOK: {data.get('hook', '')}",
        ]
        beats: list[dict[str, Any]] = []
        for index, scene in enumerate(scenes, 1):
            lines.append(
                f"\nSCENE {scene.get('scene_number', index)} — "
                f"{scene.get('location', '')} / {scene.get('time', '')}"
            )
            action = scene.get("action") or []
            lines.extend(str(item) for item in (action if isinstance(action, list) else [action]))
            dialogue = scene.get("dialogue") or []
            for item in dialogue if isinstance(dialogue, list) else [dialogue]:
                if isinstance(item, dict):
                    lines.append(f"{item.get('speaker', '')}: {item.get('text', '')}")
                else:
                    lines.append(str(item))
            beats.append(
                {
                    "order": index,
                    "title": str(scene.get("narrative_function") or f"Scene {index}"),
                    "description": str(scene.get("visual_direction") or scene.get("action") or ""),
                }
            )
        lines.append(f"\nCLIFFHANGER: {data.get('cliffhanger', '')}")
        return {
            "screenplay": "\n".join(lines),
            "beat_sheet": beats,
            "simulation_summary": " | ".join(
                str(scene.get("narrative_function") or scene.get("action") or "")
                for scene in scenes
            ),
        }

    def _selected_forecast(self, project_id: str, episode_number: int) -> ForecastDirection | None:
        for forecast in self.repo.list_forecasts(project_id):
            if (
                forecast.episode_number == episode_number
                and forecast.selected_direction_id
                and not forecast.stale
            ):
                return next(
                    (
                        direction
                        for direction in forecast.directions
                        if direction.id == forecast.selected_direction_id
                    ),
                    None,
                )
        return None

    @staticmethod
    def _compact_forecast_for_writer(direction: ForecastDirection) -> dict[str, Any]:
        """Keep causal outcomes, not Room transcripts or execution diagnostics."""
        return {
            "id": direction.id,
            "label": direction.label,
            "description": direction.description[:2000],
            "evaluation": dict(direction.evaluation),
            "risks": list(direction.risks[:8]),
            "beats": [beat.model_dump(mode="json") for beat in direction.beats[:8]],
            "steps": [
                {
                    "episode_number": step.get("episode_number"),
                    "summary": str(step.get("summary") or "")[:1800],
                    "character_decisions": list(step.get("character_decisions") or [])[:8],
                    "relationship_changes": list(step.get("relationship_changes") or [])[:8],
                    "knowledge_changes": list(step.get("knowledge_changes") or [])[:8],
                    "plot_progression": list(step.get("plot_progression") or [])[:8],
                    "clue_changes": list(step.get("clue_changes") or [])[:8],
                    "predicted_episode_beats": list(step.get("predicted_episode_beats") or [])[:8],
                }
                for step in direction.steps[:5]
            ],
        }

    @staticmethod
    def _compact_writer_room_for_writer(
        synthesis: WriterRoomSynthesis,
    ) -> dict[str, Any]:
        """Use the durable editorial decision; omit raw protocol task payloads."""
        return {
            "recommended_direction": synthesis.recommended_direction[:2400],
            "episode_goal": synthesis.episode_goal[:1200],
            "recommended_beats": synthesis.recommended_beats[:10],
            "character_notes": synthesis.character_notes[:8],
            "mystery_notes": synthesis.mystery_notes[:8],
            "continuity_constraints": synthesis.continuity_constraints[:8],
            "commercial_notes": synthesis.commercial_notes[:8],
            "must_keep": synthesis.must_keep[:8],
            "must_change": synthesis.must_change[:8],
            "risks": synthesis.risks[:8],
            "final_writer_instruction": synthesis.final_writer_instruction[:5000],
        }

    @staticmethod
    def _deterministic_scene_summary(scene: NarrativeScene) -> str:
        participants = ", ".join(item.name or item.character_id for item in scene.participants)
        return (
            f"NON-CANON offline scene at {scene.location or 'unspecified'}: "
            f"{participants} pursue {scene.scene_goal or scene.conflict or 'the scene goal'}."
        )

    @staticmethod
    def _forecast_step_from_scene(
        episode_number: int, scene: NarrativeScene, direction: str
    ) -> dict[str, Any]:
        return {
            "episode_number": episode_number,
            "summary": scene.summary,
            "events": [scene.summary],
            "character_decisions": list(scene.decisions),
            "relationship_changes": list(scene.relationship_delta),
            "knowledge_changes": [],
            "plot_progression": [direction],
            "clue_changes": [],
            "world_changes": ([scene.world_event_id] if scene.world_event_id else []),
            "predicted_episode_beats": [scene.summary],
            "scene_ids": [scene.id],
        }

    @staticmethod
    def _deterministic_forecast_step(
        episode_number: int,
        direction: ForecastDirection,
        plan: EpisodePlan | None,
    ) -> dict[str, Any]:
        goal = plan.narrative_goal if plan else direction.description
        summary = f"EP{episode_number}: {direction.label} -> {goal}"
        return {
            "episode_number": episode_number,
            "summary": summary,
            "events": [summary],
            "character_decisions": [],
            "relationship_changes": [],
            "knowledge_changes": [],
            "plot_progression": [goal],
            "clue_changes": list(plan.required_clues if plan else []),
            "world_changes": [],
            "predicted_episode_beats": [
                beat.description for beat in (plan.beats if plan else direction.beats)
            ],
            "scene_ids": [],
        }

    @staticmethod
    def _deterministic_forecast_evaluation(
        direction: ForecastDirection,
    ) -> dict[str, float]:
        step_count = max(1, len(direction.steps))
        event_count = sum(len(step.get("events", [])) for step in direction.steps)
        density = min(1.0, event_count / step_count)
        base = 0.65 if direction.description else 0.5
        keys = (
            "persona_consistency",
            "causal_consistency",
            "continuity_consistency",
            "character_arc_progress",
            "plot_progress",
            "mystery_strength",
            "information_control",
            "foreshadowing_utilization",
            "emotional_intensity",
            "conflict_density",
            "novelty",
            "author_intent_alignment",
            "production_feasibility",
            "hook_strength",
            "midpoint_turn_strength",
            "cliffhanger_strength",
            "visual_payoff",
            "retention_potential",
        )
        return {key: round(min(1.0, (base + density) / 2), 3) for key in keys}

    @staticmethod
    def _redact_story_truth(text: str, bible: StoryBible, facts: list[StoryFact]) -> str:
        result = text
        for blocked in [*bible.final_truth, *(fact.text for fact in facts if fact.secret)]:
            if blocked.strip():
                result = result.replace(blocked.strip(), "[REDACTED STORY TRUTH]")
        if bible.author_notes.strip():
            result = result.replace(bible.author_notes.strip(), "[REDACTED AUTHOR NOTES]")
        return result

    @staticmethod
    def _room_notes(stage_outputs: list[dict[str, Any]], marker: str) -> list[str]:
        notes: list[str] = []
        for row in stage_outputs:
            if marker not in str(row.get("stage") or "").casefold():
                continue
            output = dict(row.get("output") or {})
            for key in ("summary", "recommendation"):
                if output.get(key):
                    notes.append(str(output[key]))
            notes.extend(str(item) for item in output.get("concerns", []))
        return notes

    @staticmethod
    def _actor_with_bound_persona(
        actor: Any,
        character: NarrativeCharacter | None,
        runtime: dict[str, Any] | None,
    ) -> Any:
        persona_id = str((character.persona_id if character else None) or "").strip()
        cloned = actor.model_copy(deep=True)
        cloned.persona_id = persona_id
        if cloned.runtime_config is not None:
            cloned.runtime_config = cloned.runtime_config.model_copy(
                update={"persona_id": persona_id}
            )
        selected = dict(runtime or {})
        if selected.get("agent_id"):
            return NarrativeService._actor_with_runtime(cloned, selected)
        return cloned

    @staticmethod
    def _actor_with_runtime(actor: Any, runtime: dict[str, Any]) -> Any:
        from persona_continuum.world.models import ActorRuntimeConfig

        cloned = actor.model_copy(deep=True)
        cloned.runtime_config = ActorRuntimeConfig(
            persona_id=cloned.persona_id or "",
            agent_id=runtime.get("agent_id") or "unconfigured",
            model_id=runtime.get("model_id") or "auto",
            reasoning_effort=runtime.get("reasoning_effort") or "none",
            credential_id=runtime.get("auth_profile_id"),
            auth_profile_id=runtime.get("auth_profile_id"),
            credential_provider="narrative_runtime_assignment",
        )
        return cloned

    def _heuristic_relationship_delta(self, scene: NarrativeScene) -> list[dict[str, Any]]:
        """Multi-dimensional relationship deltas (never a single affinity)."""
        text = (scene.summary or "").lower()
        conflict = any(k in text for k in ("conflict", "argu", "怒", "吵", "confront", "accus"))
        warm = any(k in text for k in ("reconcile", "trust", "warm", "和解", "拥抱"))
        deltas = []
        for participant in scene.participants:
            delta = {
                "character_id": participant.character_id,
                "familiarity": 0.05,
                "trust": -0.2 if conflict else (0.15 if warm else 0.0),
                "affection": 0.05 if warm else 0.0,
                "respect": 0.0,
                "dependence": 0.0,
                "resentment": 0.2 if conflict else 0.0,
                "jealousy": 0.0,
                "perceived_threat": 0.15 if conflict else 0.0,
                "unresolved_conflict": 0.3 if conflict else 0.0,
                "reason": "heuristic scene outcome",
            }
            deltas.append(delta)
        return deltas

    def _ensure_world_actor(
        self,
        project: NarrativeProject,
        character: NarrativeCharacter | None,
        persona_id: str | None,
        branch_id: str,
    ) -> Any:
        from persona_continuum.world.models import Actor, ActorRuntimeConfig, ActorType

        actor_id = character.id if character else "narrative_actor"
        existing = self.continuum.worlds.engine.actor_mgr.get_actor(actor_id)
        if existing:
            return existing
        # Fall back to the same default runtime the world engine would pick so
        # the actor goes through RuntimePool like every other world actor.
        default_runtime = self.continuum.worlds.engine.repo.get_world(project.story_world_id)
        default_cfg = (default_runtime.default_actor_runtime if default_runtime else {}) or {}
        runtime = ActorRuntimeConfig(
            persona_id=persona_id or "",
            agent_id=default_cfg.get("agent_id", "unconfigured"),
            model_id=default_cfg.get("model_id", "auto"),
            runtime_source=default_cfg.get("runtime_source", "local_cli"),
            credential_provider=default_cfg.get("credential_provider", "unconfigured"),
        )
        return Actor(
            id=actor_id,
            name=character.name if character else "Narrative Actor",
            actor_type=ActorType.PERSONA_ACTOR,
            persona_id=persona_id or "",
            runtime_config=runtime,
        )

    def _branch_state(self, branch_id: str) -> Any:
        branch = self.continuum.worlds.engine.repo.get_branch(branch_id)
        if branch and branch.current_state:
            return branch.current_state
        from persona_continuum.world.models import WorldState

        return WorldState(timestamp=datetime.now(UTC).date().isoformat())

    def _world_time(self, branch_id: str) -> str:
        branch = self.continuum.worlds.engine.repo.get_branch(branch_id)
        if branch and branch.current_state:
            return str(branch.current_state.timestamp)
        return datetime.now(UTC).date().isoformat()

    def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()


class _PauseRequested(Exception):
    pass


def _guide_assets_schema() -> dict[str, Any]:
    """JSON schema for the batched asset image prompt refinement
    (guide_compilation)."""
    return {
        "type": "object",
        "required": ["assets"],
        "properties": {
            "assets": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "required": ["asset_key", "generation_prompt"],
                    "properties": {
                        "asset_key": {"type": "string"},
                        "generation_prompt": {"type": "string"},
                    },
                },
            }
        },
    }


def _guide_clips_schema() -> dict[str, Any]:
    """JSON schema for the batched copy-ready prompt refinement
    (guide_compilation)."""
    return {
        "type": "object",
        "required": ["clips"],
        "properties": {
            "clips": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "required": ["clip_id", "copy_ready_prompt"],
                    "properties": {
                        "clip_id": {"type": "string"},
                        "copy_ready_prompt": {"type": "string"},
                    },
                },
            }
        },
    }


def _clip_prompts_schema() -> dict[str, Any]:
    """JSON schema for the batched clip prompt refinement (prompt_compilation)."""
    return {
        "type": "object",
        "required": ["clips"],
        "properties": {
            "clips": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "required": ["clip_id", "prompt"],
                    "properties": {
                        "clip_id": {"type": "string"},
                        "prompt": {"type": "string"},
                        "negative_prompt": {"type": ["string", "null"]},
                        "audio_prompt": {"type": ["string", "null"]},
                        "recommended_settings": {"type": "object"},
                        "continuity_constraints": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 8,
                        },
                    },
                },
            }
        },
    }
