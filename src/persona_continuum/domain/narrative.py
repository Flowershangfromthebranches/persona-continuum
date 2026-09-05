"""Narrative Studio domain models.

Narrative Studio is the *author control layer* on top of Parallel World
(simulation), Persona (characters), and Room (interaction). These models
define projects, the story bible, the three-tier knowledge system
(Story Truth / Character Knowledge / Audience Knowledge), canon, episodes,
scenes, plot threads, clues, character arcs, forecasts, audits, and
production packages.

Canonical rule: every simulated artifact defaults to NON_CANONICAL. Only an
explicit commit promotes content to CANON.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from persona_continuum.application._utils import new_id


def utc_now() -> datetime:
    return datetime.now(UTC)


class NarrativeFormat(StrEnum):
    MICRO_DRAMA = "micro_drama"
    SERIES = "series"
    NOVEL = "novel"
    INTERACTIVE_STORY = "interactive_story"
    CUSTOM = "custom"


class NarrativeProjectStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class CanonStatus(StrEnum):
    CANON = "canon"
    NON_CANONICAL = "noncanonical"


class KnowledgeState(StrEnum):
    UNKNOWN = "unknown"
    SUSPECTED = "suspected"
    KNOWN = "known"
    MISBELIEVED = "misbelieved"


class AudienceState(StrEnum):
    HIDDEN = "hidden"
    TEASED = "teased"
    PARTIAL = "partial"
    REVEALED = "revealed"
    CONFIRMED = "confirmed"


class CanonEntryType(StrEnum):
    FACT = "fact"
    EVENT = "event"
    RELATIONSHIP_STATE = "relationship_state"
    WORLD_RULE = "world_rule"
    CHARACTER_STATE = "character_state"
    CHARACTER_KNOWLEDGE = "character_knowledge"
    AUDIENCE_KNOWLEDGE = "audience_knowledge"
    PLOT_THREAD = "plot_thread"
    CLUE = "clue"


class PlotThreadStatus(StrEnum):
    PLANNED = "planned"
    ACTIVE = "active"
    PAUSED = "paused"
    RESOLVED = "resolved"
    ABANDONED = "abandoned"


class ClueType(StrEnum):
    CLUE = "clue"
    FORESHADOWING = "foreshadowing"
    RED_HERRING = "red_herring"
    MYSTERY = "mystery"
    SECRET = "secret"


class ClueStatus(StrEnum):
    PLANNED = "planned"
    PLANTED = "planted"
    ECHOED = "echoed"
    REVEALED = "revealed"
    RESOLVED = "resolved"
    ABANDONED = "abandoned"


class EpisodeStatus(StrEnum):
    PLANNED = "planned"
    PREPARED = "prepared"
    SIMULATING = "simulating"
    DRAFTED = "drafted"
    AUDITING = "auditing"
    READY = "ready"
    CANON = "canon"


class SceneStatus(StrEnum):
    PLANNED = "planned"
    ENTRY = "entry"
    RUNNING = "running"
    EXIT = "exit"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class AuditSeverity(StrEnum):
    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"


class ForecastStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    SELECTED = "selected"
    STALE = "stale"
    CANCELLED = "cancelled"


class GenerationMode(StrEnum):
    """How a narrative stage obtains creative output.

    AUTO selects an Agent only when a runtime is configured. Once selected,
    Agent failure is terminal; AUTO never changes execution mode after failure.
    """

    AUTO = "auto"
    AGENT = "agent"
    DETERMINISTIC = "deterministic"


class NarrativeDirectorMode(StrEnum):
    """Narrative Director Agent autonomy level."""

    DISCUSS = "discuss"
    ADVISE = "advise"
    AGENT = "agent"


class NarrativeDirectorSessionStatus(StrEnum):
    ACTIVE = "active"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_FOR_USER = "waiting_for_user"
    WAITING_FOR_CANON_APPROVAL = "waiting_for_canon_approval"
    NEEDS_HUMAN_GUIDANCE = "needs_human_guidance"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NarrativeDirectorActionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class NarrativeRole(StrEnum):
    HEAD_WRITER = "head_writer"
    STORY_ARCHITECT = "story_architect"
    CHARACTER_EDITOR = "character_editor"
    MYSTERY_EDITOR = "mystery_editor"
    CONTINUITY_EDITOR = "continuity_editor"
    COMMERCIAL_EDITOR = "commercial_editor"
    DIRECTOR = "director"
    AUDIENCE_PROXY = "audience_proxy"


class NarrativeProject(BaseModel):
    """A long-form narrative work (novel, series, micro drama, ...)."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("nproj"))
    title: str
    logline: str = ""
    description: str = ""
    format: NarrativeFormat = NarrativeFormat.SERIES
    genre: list[str] = Field(default_factory=list)
    tone: list[str] = Field(default_factory=list)
    target_audience: str = ""
    planned_episode_count: int = 12
    episode_duration_seconds_min: int = 60
    episode_duration_seconds_max: int = 120
    story_world_id: str | None = None
    canonical_world_branch_id: str | None = None
    story_bible_version: int = 0
    status: NarrativeProjectStatus = NarrativeProjectStatus.DRAFT
    revision: int = 1
    runtime_assignment: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class StoryBibleCharacter(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    role: str = ""
    description: str = ""
    persona_id: str | None = None
    visual: dict[str, Any] = Field(default_factory=dict)


class StoryBibleLocation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    description: str = ""
    visual: dict[str, Any] = Field(default_factory=dict)


class StoryBibleOrganization(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    description: str = ""
    visual: dict[str, Any] = Field(default_factory=dict)


class MasterTimelineItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    episode: int
    summary: str
    timestamp: str | None = None
    location: str | None = None


class ReversalPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    episode: int
    title: str
    description: str = ""


class StoryBible(BaseModel):
    """Highest-level creative constraints. Versioned; old versions are kept."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("bible"))
    project_id: str
    version: int = 1
    premise: str = ""
    core_question: str = ""
    theme: str = ""
    genre: list[str] = Field(default_factory=list)
    tone: list[str] = Field(default_factory=list)
    visual_identity: dict[str, Any] = Field(default_factory=dict)
    world_rules: list[str] = Field(default_factory=list)
    technology_rules: list[str] = Field(default_factory=list)
    supernatural_rules: list[str] = Field(default_factory=list)
    # Story Truth: what the AUTHOR knows. Never visible to characters unless
    # it has explicitly entered their CharacterKnowledge state.
    final_truth: list[str] = Field(default_factory=list)
    ending: str = ""
    characters: list[StoryBibleCharacter] = Field(default_factory=list)
    organizations: list[StoryBibleOrganization] = Field(default_factory=list)
    locations: list[StoryBibleLocation] = Field(default_factory=list)
    master_timeline: list[MasterTimelineItem] = Field(default_factory=list)
    major_reversals: list[ReversalPlan] = Field(default_factory=list)
    narrative_constraints: list[str] = Field(default_factory=list)
    forbidden_shortcuts: list[str] = Field(default_factory=list)
    # Author notes are author-only and must never enter character prompts.
    author_notes: str = ""
    locked_fields: list[str] = Field(default_factory=list)
    generation_mode: GenerationMode = GenerationMode.DETERMINISTIC
    runtime_trace: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class CharacterKnowledgeEntry(BaseModel):
    """What a character currently knows about a story fact."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("ckn"))
    project_id: str
    character_id: str
    fact_id: str
    fact_text: str = ""
    state: KnowledgeState = KnowledgeState.UNKNOWN
    confidence: float = 0.0
    learned_episode: int | None = None
    learned_scene: str | None = None
    source_event_id: str | None = None
    source_character_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    valid_from: str | None = None
    valid_to: str | None = None
    notes: str = ""
    updated_at: datetime = Field(default_factory=utc_now)


class AudienceKnowledgeEntry(BaseModel):
    """What the audience currently knows about a story fact (suspense dial)."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("aud"))
    project_id: str
    fact_id: str
    fact_text: str = ""
    state: AudienceState = AudienceState.HIDDEN
    revealed_episode: int | None = None
    notes: str = ""
    updated_at: datetime = Field(default_factory=utc_now)


class StoryFact(BaseModel):
    """A unit of Story Truth tracked across the knowledge matrix."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("fact"))
    project_id: str
    text: str
    category: str = "story_truth"
    secret: bool = True
    created_at: datetime = Field(default_factory=utc_now)


class CanonEntry(BaseModel):
    """A canonical fact/event/state. Simulations never auto-create these."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("canon"))
    project_id: str
    episode_number: int | None = None
    entry_type: CanonEntryType = CanonEntryType.FACT
    text: str
    data: dict[str, Any] = Field(default_factory=dict)
    source: str = "narrative_commit"  # narrative_commit | import | author
    world_branch_id: str | None = None
    world_event_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class CanonRevision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("crev"))
    project_id: str
    canon_entry_id: str
    reason: str
    previous_text: str = ""
    new_text: str = ""
    created_at: datetime = Field(default_factory=utc_now)


class NarrativeCharacter(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("nchar"))
    project_id: str
    name: str
    role: str = ""
    description: str = ""
    # Explicit mapping to the Persona / World layers.
    persona_id: str | None = None
    world_actor_id: str | None = None
    persona_origin: str = "fictional_author_defined"  # fictional_author_defined | existing_persona
    # fictional_author_defined | counterfactual_simulated
    provenance: str = "fictional_author_defined"
    visual: dict[str, Any] = Field(default_factory=dict)
    dialogue_samples: list[str] = Field(default_factory=list)
    backstory: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class NarrativeCharacterBinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("nbind"))
    project_id: str
    story_character_id: str
    persona_id: str | None = None
    world_actor_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class Beat(BaseModel):
    model_config = ConfigDict(extra="ignore")

    order: int = 0
    title: str = ""
    description: str = ""
    start_seconds: int | None = None
    end_seconds: int | None = None


class EpisodePlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("eplan"))
    project_id: str
    episode_number: int
    title: str = ""
    narrative_goal: str = ""
    hook: str = ""
    beats: list[Beat] = Field(default_factory=list)
    must_happen: list[str] = Field(default_factory=list)
    must_not_happen: list[str] = Field(default_factory=list)
    required_characters: list[str] = Field(default_factory=list)
    required_clues: list[str] = Field(default_factory=list)
    plot_threads: list[str] = Field(default_factory=list)
    clues_to_plant: list[str] = Field(default_factory=list)
    clues_to_echo: list[str] = Field(default_factory=list)
    reveal_targets: list[str] = Field(default_factory=list)
    forbidden_reveals: list[str] = Field(default_factory=list)
    relationship_targets: list[dict[str, Any]] = Field(default_factory=list)
    emotion_targets: list[dict[str, Any]] = Field(default_factory=list)
    world_state_targets: list[dict[str, Any]] = Field(default_factory=list)
    cliffhanger: str = ""
    estimated_duration_seconds: int = 90
    status: EpisodeStatus = EpisodeStatus.PLANNED
    generation_mode: GenerationMode = GenerationMode.DETERMINISTIC
    runtime_trace: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SceneParticipant(BaseModel):
    model_config = ConfigDict(extra="ignore")

    character_id: str
    name: str = ""
    goal: str = ""
    knowledge_fact_ids: list[str] = Field(default_factory=list)
    must_not_reveal: list[str] = Field(default_factory=list)
    emotion_start: dict[str, Any] = Field(default_factory=dict)


class NarrativeScene(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("scene"))
    project_id: str
    episode_id: str | None = None
    episode_number: int | None = None
    order: int = 0
    location: str = ""
    time: str = ""
    participants: list[SceneParticipant] = Field(default_factory=list)
    scene_goal: str = ""
    conflict: str = ""
    entry_state: str = ""
    exit_state: str = ""
    must_happen: list[str] = Field(default_factory=list)
    must_not_happen: list[str] = Field(default_factory=list)
    knowledge_constraints: list[str] = Field(default_factory=list)
    relationship_constraints: list[str] = Field(default_factory=list)
    estimated_duration_seconds: int = 60
    world_branch_id: str | None = None
    room_id: str | None = None
    status: SceneStatus = SceneStatus.PLANNED
    # Simulation output (transcript stays append-only in world/room storage;
    # here we keep structured results only).
    dialogue: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    relationship_delta: list[dict[str, Any]] = Field(default_factory=list)
    affect_delta: list[dict[str, Any]] = Field(default_factory=list)
    world_event_id: str | None = None
    summary: str = ""
    generation_mode: GenerationMode = GenerationMode.AGENT
    runtime_trace: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PlotThread(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("thread"))
    project_id: str
    title: str
    description: str = ""
    kind: str = "main"  # main | subplot
    status: PlotThreadStatus = PlotThreadStatus.PLANNED
    opened_episode: int | None = None
    closed_episode: int | None = None
    progress_notes: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)


class CharacterArcTurningPoint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    episode: int
    description: str


class CharacterArc(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("arc"))
    project_id: str
    character_id: str
    start_state: str = ""
    target_state: str = ""
    current_phase: str = "setup"
    turning_points: list[CharacterArcTurningPoint] = Field(default_factory=list)
    current_progress: float = 0.0  # 0.0 - 1.0
    notes: str = ""
    updated_at: datetime = Field(default_factory=utc_now)


class NarrativeClue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("clue"))
    project_id: str
    title: str
    description: str = ""
    type: ClueType = ClueType.CLUE
    introduced_episode: int | None = None
    introduced_scene: str | None = None
    echo_episodes: list[int] = Field(default_factory=list)
    planned_reveal_episode: int | None = None
    actual_reveal_episode: int | None = None
    payoff: str = ""
    status: ClueStatus = ClueStatus.PLANNED
    updated_at: datetime = Field(default_factory=utc_now)


class ForecastDirection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("fdir"))
    label: str
    description: str = ""
    world_branch_id: str | None = None
    beats: list[Beat] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)
    summary: str = ""
    steps: list[dict[str, Any]] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    evaluation: dict[str, float] = Field(default_factory=dict)


class NarrativeForecast(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("fcst"))
    project_id: str
    episode_number: int
    question: str
    horizon_episodes: int = 5
    directions: list[ForecastDirection] = Field(default_factory=list)
    status: ForecastStatus = ForecastStatus.RUNNING
    selected_direction_id: str | None = None
    # Stale detection inputs.
    context_fingerprint: str = ""
    story_bible_version: int = 0
    project_revision: int = 0
    canonical_branch_id: str | None = None
    stale: bool = False
    generation_mode: GenerationMode = GenerationMode.DETERMINISTIC
    runtime_trace: list[dict[str, Any]] = Field(default_factory=list)
    budget: dict[str, int] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AuditFinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("find"))
    severity: AuditSeverity = AuditSeverity.INFO
    code: str
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class NarrativeAuditReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("naudit"))
    project_id: str
    episode_number: int
    episode_version_id: str | None = None
    findings: list[AuditFinding] = Field(default_factory=list)
    blocking_count: int = 0
    warning_count: int = 0
    info_count: int = 0
    passed: bool = True
    context_fingerprint: str = ""
    story_bible_version: int = 0
    project_revision: int = 0
    created_at: datetime = Field(default_factory=utc_now)


class EpisodeVersion(BaseModel):
    """An immutable version of an episode draft. Canon points at one version."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("epv"))
    project_id: str
    episode_number: int
    version: int = 1
    created_by: str = "author"
    # Revision provenance: set when this version was produced by revising an
    # earlier one (e.g. via the Narrative Director Agent). V1 keeps defaults.
    parent_version_id: str | None = None
    revision_reason: str = ""
    revision_instructions: list[str] = Field(default_factory=list)
    revision_mode: str | None = None  # local | medium | rewrite
    title: str = ""
    screenplay: str = ""
    beat_sheet: list[Beat] = Field(default_factory=list)
    scene_ids: list[str] = Field(default_factory=list)
    simulation_summary: str = ""
    structured_draft: dict[str, Any] = Field(default_factory=dict)
    writer_room_synthesis: dict[str, Any] = Field(default_factory=dict)
    generation_mode: GenerationMode = GenerationMode.DETERMINISTIC
    runtime_trace: dict[str, Any] = Field(default_factory=dict)
    context_fingerprint: str = ""
    story_bible_version: int = 0
    project_revision: int = 0
    canonical_branch_id: str | None = None
    stale: bool = False
    audit_report_id: str | None = None
    is_canon: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class Shot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    shot_number: int
    start_time: str = "00:00"
    end_time: str = "00:00"
    duration_seconds: float = 0.0
    shot_size: str = "medium"
    camera: str = ""
    movement: str = ""
    characters: list[str] = Field(default_factory=list)
    action: str = ""
    dialogue: str = ""
    voiceover: str = ""
    location: str = ""
    visual_prompt: str = ""
    motion_prompt: str = ""
    negative_constraints: list[str] = Field(default_factory=list)
    continuity_constraints: list[str] = Field(default_factory=list)
    sfx: list[str] = Field(default_factory=list)
    bgm: str = ""
    transition: str = "cut"
    # Structured generation intents consumed by the clip planner / video
    # prompt compiler. Legacy visual_prompt / motion_prompt stay untouched.
    visual_intent: str = ""
    camera_intent: str = ""
    subject_motion_intent: str = ""
    environment_motion_intent: str = ""
    emotion_intent: str = ""
    audio_intent: list[str] = Field(default_factory=list)
    generation_notes: list[str] = Field(default_factory=list)


class ProductionPackage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("prod"))
    project_id: str
    episode_number: int
    episode_version_id: str | None = None
    format: NarrativeFormat = NarrativeFormat.MICRO_DRAMA
    screenplay: str = ""
    shot_list: list[Shot] = Field(default_factory=list)
    scene_list: list[dict[str, Any]] = Field(default_factory=list)
    character_list: list[dict[str, Any]] = Field(default_factory=list)
    location_list: list[dict[str, Any]] = Field(default_factory=list)
    prop_list: list[str] = Field(default_factory=list)
    dialogue_track: list[dict[str, Any]] = Field(default_factory=list)
    subtitle_track: list[dict[str, Any]] = Field(default_factory=list)
    sound_effect_plan: list[dict[str, Any]] = Field(default_factory=list)
    bgm_direction: str = ""
    image_generation_prompts: list[str] = Field(default_factory=list)
    video_generation_prompts: list[str] = Field(default_factory=list)
    character_visual_bible: list[dict[str, Any]] = Field(default_factory=list)
    location_visual_bible: list[dict[str, Any]] = Field(default_factory=list)
    prop_visual_bible: list[dict[str, Any]] = Field(default_factory=list)
    continuity_notes: list[str] = Field(default_factory=list)
    generation_mode: GenerationMode = GenerationMode.DETERMINISTIC
    runtime_trace: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    # Model-facing pipeline metadata. video_generation_prompts remains the
    # legacy flat prompt list and is never rewritten.
    is_preview: bool = False
    stale: bool = False
    context_fingerprint: str = ""
    generic_video_guidance: list[str] = Field(default_factory=list)


class WriterRoomSynthesis(BaseModel):
    """Durable Head Writer output consumed by the Screenwriter stage."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("wrsyn"))
    project_id: str
    episode_number: int
    room_id: str
    protocol_run_id: str | None = None
    recommended_direction: str = ""
    episode_goal: str = ""
    recommended_beats: list[str] = Field(default_factory=list)
    character_notes: list[str] = Field(default_factory=list)
    mystery_notes: list[str] = Field(default_factory=list)
    continuity_constraints: list[str] = Field(default_factory=list)
    commercial_notes: list[str] = Field(default_factory=list)
    must_keep: list[str] = Field(default_factory=list)
    must_change: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    final_writer_instruction: str = ""
    stage_outputs: list[dict[str, Any]] = Field(default_factory=list)
    runtime_trace: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class EpisodeSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project_id: str
    episode_number: int
    summary: str
    context_fingerprint: str = ""
    created_at: datetime = Field(default_factory=utc_now)


class NarrativeDirectorSession(BaseModel):
    """A durable Director conversation bound to a project (and optionally one
    episode). Sessions are logical only; every model turn still acquires a
    runtime session through the existing turn-scoped executor."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("ndir"))
    project_id: str
    episode_number: int | None = None
    mode: NarrativeDirectorMode = NarrativeDirectorMode.AGENT
    status: NarrativeDirectorSessionStatus = NarrativeDirectorSessionStatus.ACTIVE
    runtime: dict[str, Any] = Field(default_factory=dict)
    # Optimistic-concurrency snapshot taken when the current action loop began.
    project_revision: int = 0
    story_bible_version: int = 0
    conversation_summary: str = ""
    # Pending HIGH_IMPACT_WRITE action awaiting explicit user confirmation.
    pending_action: dict[str, Any] = Field(default_factory=dict)
    last_error: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class NarrativeDirectorMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("ndmsg"))
    session_id: str
    role: str  # user | assistant | system
    content: str
    created_at: datetime = Field(default_factory=utc_now)


class NarrativeDirectorAction(BaseModel):
    """One persisted Director Action invocation (tool call) and its result."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("nda"))
    session_id: str
    message_id: str = ""
    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: NarrativeDirectorActionStatus = NarrativeDirectorActionStatus.PENDING
    result: dict[str, Any] = Field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None


class ShootingSessionStatus(StrEnum):
    """Mirror of NarrativeDirectorSessionStatus values for shooting sessions."""

    ACTIVE = "active"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_FOR_USER = "waiting_for_user"
    WAITING_FOR_CANON_APPROVAL = "waiting_for_canon_approval"
    NEEDS_HUMAN_GUIDANCE = "needs_human_guidance"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ShootingMessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ShootingActionStatus(StrEnum):
    """Mirror of NarrativeDirectorActionStatus values for shooting actions."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class VideoModelProfile(BaseModel):
    """Verified capability / prompting profile for one external video model.

    ``None`` capability flags mean UNKNOWN and are treated as a hard
    requirement gap by downstream planners. Built-in profiles come from the
    packaged YAML files in ``persona_continuum.narrative.video_profiles``;
    nothing may be asserted that is not stated by an official source.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("vmp"))
    display_name: str
    vendor: str
    model_family: str = ""
    model_version: str | None = None
    profile_version: str = "1"
    verification_status: str = "draft"  # verified | draft | custom
    verified_at: datetime | None = None
    official_sources: list[str] = Field(default_factory=list)
    supported_modes: list[str] = Field(default_factory=list)
    supported_durations_seconds: list[float] | None = None
    duration_min_seconds: float | None = None
    duration_max_seconds: float | None = None
    supported_aspect_ratios: list[str] = Field(default_factory=list)
    # None = UNKNOWN (hard requirement); True/False are explicit facts.
    supports_audio: bool | None = None
    supports_dialogue: bool | None = None
    supports_sfx: bool | None = None
    supports_image_to_video: bool | None = None
    supports_text_to_video: bool | None = None
    supports_first_frame: bool | None = None
    supports_last_frame: bool | None = None
    supports_first_last_frame: bool | None = None
    supports_reference_images: bool | None = None
    supports_timestamp_prompting: bool | None = None
    supports_negative_prompt: bool | None = None
    max_reference_images: int | None = None
    negative_prompt_strategy: str = "unknown"
    prompt_language_preferences: list[str] = Field(default_factory=list)
    prompt_strategy: dict[str, Any] = Field(default_factory=dict)
    clip_planning: dict[str, Any] = Field(default_factory=dict)
    continuity_strategy: dict[str, Any] = Field(default_factory=dict)
    recommended_defaults: dict[str, Any] = Field(default_factory=dict)
    # How the guide layer (video production guide / copy-ready prompts)
    # addresses reference inputs inside a prompt:
    # named_reference | external_reference_only | textual_anchor.
    # Same honesty rule as negative_prompt_strategy: conservative default,
    # never asserting an undocumented capability.
    reference_prompt_syntax: str = "textual_anchor"
    # Platform UI terminology translation (task #63/#64): how the guide words
    # reference inputs for THIS vendor's actual product UI, e.g. Veo calls
    # them "Ingredients" / "Start Frame" while Runway uses "Input Image" /
    # "Character Reference". Empty entries fall back to neutral wording.
    platform_terms: dict[str, str] = Field(default_factory=dict)
    # native_screen_text | post_composite: whether the model can reliably
    # render in-frame readable text (default: composite in post).
    screen_text_strategy: str = "post_composite"


class ProductionAsset(BaseModel):
    """A reference image / frame asset registered for video generation."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("past"))
    project_id: str
    episode_number: int | None = None
    # character_reference | location_reference | prop_reference |
    # start_frame | end_frame | style_reference | other
    asset_type: str
    name: str
    character_id: str | None = None
    location_id: str | None = None
    source_uri: str | None = None
    local_path: str | None = None
    description: str = ""
    # Free-form metadata; callers may store "production_package_id" here so
    # the repository can index assets per production package.
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class GenerationClip(BaseModel):
    """One generation-ordered video clip derived from the shot list."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("clip"))
    clip_number: int
    source_shot_numbers: list[int] = Field(default_factory=list)
    scene_number: int | None = None
    purpose: str = ""
    duration_seconds: float
    # Head shot's location; empty keeps old serialized JSON compatible.
    location: str = ""
    generation_mode: str = "auto"  # auto | text_to_video | image_to_video
    target_profile_id: str = ""
    aspect_ratio: str = "16:9"
    start_frame_asset_id: str | None = None
    end_frame_asset_id: str | None = None
    reference_asset_ids: list[str] = Field(default_factory=list)
    visual_intent: str = ""
    camera_intent: str = ""
    subject_motion: str = ""
    environment_motion: str = ""
    dialogue: list[dict[str, Any]] = Field(default_factory=list)
    audio_intent: list[str] = Field(default_factory=list)
    continuity_constraints: list[str] = Field(default_factory=list)
    planning_rationale: str = ""
    prompt: str = ""
    negative_prompt: str | None = None
    audio_prompt: str | None = None
    postproduction_audio_plan: list[str] = Field(default_factory=list)
    recommended_settings: dict[str, Any] = Field(default_factory=dict)
    compiler_trace: dict[str, Any] = Field(default_factory=dict)
    # Copy-ready guide layer: self-contained prompt built by the video
    # production guide compiler, plus deterministic character/prop
    # attribution (character_ids from the planner, prop_ids from the
    # prompt-package compiler).
    copy_ready_prompt: str = ""
    character_ids: list[str] = Field(default_factory=list)
    prop_ids: list[str] = Field(default_factory=list)
    # User-facing start-frame token resolved by the guide layer (e.g.
    # "FRAME_01" carried from the previous clip, or "EP01_CLIP01_START" for a
    # dedicated scene start-frame asset). Empty on planner output; the guide
    # compiler fills it so the UI never has to re-derive frame logic.
    start_frame_asset_key: str = ""


class ModelPromptPackage(BaseModel):
    """A profile-specific prompt package compiled from a production package."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("pkgs"))
    project_id: str
    episode_number: int
    production_package_id: str
    episode_version_id: str
    target_profile_id: str
    target_profile_version: str
    target_video_model_display_name: str = ""
    aspect_ratio: str = "16:9"
    generation_strategy: str = "auto"
    quality_priority: str = "balanced"
    continuity_strategy: str = "auto"
    audio_strategy: str = "auto"
    prompt_language: str = "auto"
    status: str = "clip_planned"  # clip_planned | compiling | ready | failed
    clips: list[GenerationClip] = Field(default_factory=list)
    global_visual_contract: list[str] = Field(default_factory=list)
    global_continuity_contract: list[str] = Field(default_factory=list)
    asset_requirements: list[dict[str, Any]] = Field(default_factory=list)
    runtime_trace: dict[str, Any] = Field(default_factory=dict)
    parent_package_id: str | None = None
    revision_reason: str = ""
    stale: bool = False
    context_fingerprint: str = ""
    profile_update_available: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class NarrativeShootingSession(BaseModel):
    """A durable shooting-plan conversation bound to a project (and
    optionally one episode). Sessions are logical only — there is NO
    persistent runtime lease; every model turn still acquires a turn-scoped
    executor session like the Director Agent."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("nsho"))
    project_id: str
    episode_number: int | None = None
    mode: str = "agent"
    status: ShootingSessionStatus = ShootingSessionStatus.ACTIVE
    runtime: dict[str, Any] = Field(default_factory=dict)
    project_revision: int = 0
    story_bible_version: int = 0
    conversation_summary: str = ""
    pending_action: dict[str, Any] = Field(default_factory=dict)
    last_error: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class NarrativeShootingMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("nshm"))
    session_id: str
    role: ShootingMessageRole = ShootingMessageRole.USER
    content: str
    created_at: datetime = Field(default_factory=utc_now)


class NarrativeShootingAction(BaseModel):
    """One persisted shooting-session Action invocation and its result."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("nsha"))
    session_id: str
    message_id: str = ""
    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: ShootingActionStatus = ShootingActionStatus.PENDING
    result: dict[str, Any] = Field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None


class ProductionGuideAsset(BaseModel):
    """One required visual asset inside an executable video production guide.

    ``asset_key`` is the typed token referenced across the guide and its
    prompts, e.g. ``@CHAR_FANG_NING_MASTER`` (prefixes: ``@CHAR_`` /
    ``@LOC_`` / ``@PROP_`` / ``@STYLE_`` / ``@FRAME_``).
    ``generation_prompt`` must be a COMPLETE image-generation prompt — the
    guide layer never emits placeholder phrases.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("vga"))
    project_id: str
    episode_number: int | None = None
    asset_key: str
    # character | location | prop | style | reference_frame
    asset_type: str
    name: str
    purpose: str = ""
    # required | recommended | optional
    necessity: str = "recommended"
    # episode | project | scene
    reuse_scope: str = "episode"
    generation_prompt: str = ""
    recommended_aspect_ratio: str | None = None
    # NEEDED | PROMPT_READY | EXISTING | GENERATED | BOUND
    status: str = "NEEDED"
    notes: list[str] = Field(default_factory=list)
    character_id: str | None = None
    location_id: str | None = None
    source_bible_refs: list[str] = Field(default_factory=list)
    # deterministic | llm_refined
    provenance: str = "deterministic"
    # CharacterVisualIdentity (task #13): the fixed visual character design
    # (age/face/hair/height/body/costume/palette/temperament) synthesized when
    # the bibles lack concrete visual data. Deterministic per (project,
    # character) so every episode reuses the SAME identity instead of drifting
    # back to a generic description.
    visual_identity: dict[str, Any] = Field(default_factory=dict)
    compiler_trace: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class ExecutableVideoProductionGuide(BaseModel):
    """Third deterministic export layer of the video production pipeline.

    Built on top of an existing :class:`ModelPromptPackage` (never replacing
    it): a self-contained, human-operable handbook that turns the compiled
    clips into an executable shooting plan — required assets with complete
    image prompts, per-clip copy-ready prompts, continuity/frame-chain
    workflow, and post-production plans. Simulated or LLM-refined content is
    never presented as historical fact; provenance stays explicit.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("vgud"))
    project_id: str
    episode_number: int
    production_package_id: str
    prompt_package_id: str
    episode_version_id: str
    target_profile_id: str
    target_profile_version: str = ""
    target_video_model_display_name: str = ""
    aspect_ratio: str = "16:9"
    prompt_language: str = "auto"
    status: str = "drafting"  # drafting | compiling | ready | failed
    title: str = ""
    overview: dict[str, Any] = Field(default_factory=dict)
    required_assets: list[ProductionGuideAsset] = Field(default_factory=list)
    clip_workflows: list[GenerationClip] = Field(default_factory=list)
    continuity_workflow: dict[str, Any] = Field(default_factory=dict)
    frame_chain: dict[str, Any] = Field(default_factory=dict)
    subtitle_plan: list[dict[str, Any]] = Field(default_factory=list)
    screen_composite_plan: list[dict[str, Any]] = Field(default_factory=list)
    dialogue_plan: list[dict[str, Any]] = Field(default_factory=list)
    sound_plan: list[dict[str, Any]] = Field(default_factory=list)
    bgm_plan: dict[str, Any] = Field(default_factory=dict)
    editing_plan: dict[str, Any] = Field(default_factory=dict)
    final_checklist: list[str] = Field(default_factory=list)
    markdown_document: str = ""
    parent_guide_id: str | None = None
    revision_reason: str = ""
    stale: bool = False
    context_fingerprint: str = ""
    runtime_trace: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
