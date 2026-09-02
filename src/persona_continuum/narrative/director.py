"""Narrative Director Agent — action registry, permissions, and contracts.

The Director is an *orchestration* agent: it understands author intent in
natural language and drives the existing Narrative Studio capabilities by
emitting strictly structured JSON actions. It never touches the database,
source code, or Persona Base directly; every write goes through
``NarrativeService`` domain actions dispatched by the host
(see ``application/narrative_director_service.py``).

Design invariants:
- No native tool calling: the Director runtime only produces JSON matching
  ``DIRECTOR_DECISION_SCHEMA`` via ``execute_structured``.
- Permission model: every action carries an explicit risk level.
  HUMAN_ONLY actions are *not registered at all* — the dispatcher answers
  ``DIRECTOR_ACTION_NOT_ALLOWED`` for them by construction.
- Canon Gate: the Director may drive steps 1-5 of the episode pipeline but
  never commit canon and never generate production packages.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

DIRECTOR_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["decision"],
    "properties": {
        "decision": {
            "enum": ["answer", "execute_action", "request_user_input", "stop"],
        },
        "message": {"type": "string"},
        "action": {"type": "string"},
        "arguments": {"type": "object"},
        "reason": {"type": "string"},
        "expected_result": {"type": "string"},
    },
}


class DirectorActionRisk(StrEnum):
    READ_ONLY = "read_only"
    SAFE_WRITE = "safe_write"
    HIGH_IMPACT_WRITE = "high_impact_write"
    HUMAN_ONLY = "human_only"


class NarrativeDirectorStateConflict(Exception):
    """Optimistic-concurrency violation: project state moved under the Director.

    The Director must re-read state and decide again; it must never overwrite
    a newer version produced elsewhere (another tab, manual edit, commit).
    """

    code = "NARRATIVE_DIRECTOR_STATE_CONFLICT"


class DirectorActionSpec:
    """One registered Director action and its argument schema."""

    def __init__(
        self,
        name: str,
        risk: DirectorActionRisk,
        description: str,
        arguments: dict[str, Any] | None = None,
        required: list[str] | None = None,
    ) -> None:
        self.name = name
        self.risk = risk
        self.description = description
        self.arguments = arguments or {}
        self.required = required or []


def _episode_action_args(*, with_patch: bool = False) -> dict[str, Any]:
    args: dict[str, Any] = {
        "project_id": {"type": "string"},
        "episode_number": {"type": "integer"},
    }
    if with_patch:
        args["patch"] = {"type": "object"}
    return args


def _spec(
    name: str,
    risk: DirectorActionRisk,
    description: str,
    arguments: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> DirectorActionSpec:
    return DirectorActionSpec(name, risk, description, arguments, required)


_PROJECT_ONLY = {"project_id": {"type": "string"}}
_EPISODE_ARGS = _episode_action_args()


# READ_ONLY actions are available in every Director mode.
_READ_ONLY = [
    _spec(
        "get_project",
        DirectorActionRisk.READ_ONLY,
        "Read project metadata, revision, and runtime assignment.",
    ),
    _spec(
        "get_story_bible",
        DirectorActionRisk.READ_ONLY,
        "Read the current story bible (author view).",
    ),
    _spec(
        "get_story_bible_versions",
        DirectorActionRisk.READ_ONLY,
        "List story bible version metadata.",
        _PROJECT_ONLY,
        ["project_id"],
    ),
    _spec(
        "get_characters",
        DirectorActionRisk.READ_ONLY,
        "List narrative characters.",
        _PROJECT_ONLY,
        ["project_id"],
    ),
    _spec(
        "get_character_bindings",
        DirectorActionRisk.READ_ONLY,
        "List character-to-persona bindings.",
        _PROJECT_ONLY,
        ["project_id"],
    ),
    _spec(
        "get_episode_plan",
        DirectorActionRisk.READ_ONLY,
        "Read one episode plan.",
        _EPISODE_ARGS,
        ["project_id", "episode_number"],
    ),
    _spec(
        "get_episode_plans",
        DirectorActionRisk.READ_ONLY,
        "List all episode plans.",
        _PROJECT_ONLY,
        ["project_id"],
    ),
    _spec(
        "get_episode_versions",
        DirectorActionRisk.READ_ONLY,
        "List draft versions of one episode.",
        _EPISODE_ARGS,
        ["project_id", "episode_number"],
    ),
    _spec(
        "get_episode_version",
        DirectorActionRisk.READ_ONLY,
        "Read one draft version by id.",
        {"version_id": {"type": "string"}},
        ["version_id"],
    ),
    _spec(
        "get_episode_audits",
        DirectorActionRisk.READ_ONLY,
        "List continuity audit reports for one episode.",
        _EPISODE_ARGS,
        ["project_id", "episode_number"],
    ),
    _spec(
        "get_forecasts",
        DirectorActionRisk.READ_ONLY,
        "List forecasts of the project.",
        _PROJECT_ONLY,
        ["project_id"],
    ),
    _spec(
        "get_selected_forecast",
        DirectorActionRisk.READ_ONLY,
        "Read the selected forecast direction for one episode.",
        _EPISODE_ARGS,
        ["project_id", "episode_number"],
    ),
    _spec(
        "get_scenes",
        DirectorActionRisk.READ_ONLY,
        "List scenes (optionally per episode).",
        {
            "project_id": {"type": "string"},
            "episode_number": {"type": "integer"},
        },
        ["project_id"],
    ),
    _spec(
        "get_rehearsal_results",
        DirectorActionRisk.READ_ONLY,
        "Read completed Persona rehearsal scenes for one episode.",
        _EPISODE_ARGS,
        ["project_id", "episode_number"],
    ),
    _spec(
        "get_knowledge_matrix",
        DirectorActionRisk.READ_ONLY,
        "Read the character knowledge matrix.",
        {
            "project_id": {"type": "string"},
            "episode_number": {"type": "integer"},
        },
        ["project_id"],
    ),
    _spec(
        "get_audience_knowledge",
        DirectorActionRisk.READ_ONLY,
        "Read the audience knowledge (suspense) view.",
        _PROJECT_ONLY,
        ["project_id"],
    ),
    _spec(
        "get_clues",
        DirectorActionRisk.READ_ONLY,
        "List clues and foreshadowing.",
        _PROJECT_ONLY,
        ["project_id"],
    ),
    _spec(
        "get_plot_threads",
        DirectorActionRisk.READ_ONLY,
        "List plot threads.",
        _PROJECT_ONLY,
        ["project_id"],
    ),
    _spec(
        "get_character_arcs",
        DirectorActionRisk.READ_ONLY,
        "List character arcs.",
        _PROJECT_ONLY,
        ["project_id"],
    ),
    _spec(
        "get_canon",
        DirectorActionRisk.READ_ONLY,
        "List canon entries.",
        _PROJECT_ONLY,
        ["project_id"],
    ),
    _spec(
        "get_pipeline_state",
        DirectorActionRisk.READ_ONLY,
        "Read the episode pipeline state (plan/forecast/rehearsal/draft/audit/commit readiness).",
        _EPISODE_ARGS,
        ["project_id", "episode_number"],
    ),
]

# SAFE_WRITE actions run automatically in AGENT mode.
_SAFE_WRITE = [
    DirectorActionSpec(
        "patch_episode_plan",
        DirectorActionRisk.SAFE_WRITE,
        "Patch whitelisted episode-plan fields. Requires expected_project_revision "
        "for optimistic concurrency.",
        {
            **_episode_action_args(with_patch=True),
            "expected_project_revision": {"type": "integer"},
            "reason": {"type": "string"},
        },
        ["project_id", "episode_number", "patch", "expected_project_revision", "reason"],
    ),
    DirectorActionSpec(
        "generate_forecast_directions",
        DirectorActionRisk.SAFE_WRITE,
        "Generate candidate forecast directions for one episode.",
        {**_episode_action_args(), "count": {"type": "integer"}},
        ["project_id", "episode_number"],
    ),
    DirectorActionSpec(
        "run_forecast",
        DirectorActionRisk.SAFE_WRITE,
        "Run a NON-CANON forecast for one episode from candidate directions.",
        {
            **_episode_action_args(),
            "directions": {"type": "array"},
            "horizon_episodes": {"type": "integer"},
        },
        ["project_id", "episode_number", "directions"],
    ),
    DirectorActionSpec(
        "select_forecast_direction",
        DirectorActionRisk.SAFE_WRITE,
        "Select one forecast direction as the working direction.",
        {
            "project_id": {"type": "string"},
            "forecast_id": {"type": "string"},
            "direction_id": {"type": "string"},
        },
        ["project_id", "forecast_id", "direction_id"],
    ),
    DirectorActionSpec(
        "run_persona_rehearsal",
        DirectorActionRisk.SAFE_WRITE,
        "Create a NON-CANON Persona rehearsal scene for bound characters and simulate it.",
        {
            **_episode_action_args(),
            "location": {"type": "string"},
            "background": {"type": "string"},
            "goal": {"type": "string"},
            "character_ids": {"type": "array"},
            "max_turns": {"type": "integer"},
        },
        ["project_id", "episode_number"],
    ),
    DirectorActionSpec(
        "generate_episode_draft",
        DirectorActionRisk.SAFE_WRITE,
        "Generate a new episode draft version via the Screenwriter stage.",
        _episode_action_args(),
        ["project_id", "episode_number"],
    ),
    DirectorActionSpec(
        "revise_episode_draft",
        DirectorActionRisk.SAFE_WRITE,
        "Revise an existing draft version into a new immutable version (local|medium|rewrite).",
        {
            "project_id": {"type": "string"},
            "episode_number": {"type": "integer"},
            "base_version_id": {"type": "string"},
            "instructions": {"type": "array"},
            "revision_mode": {"type": "string", "enum": ["local", "medium", "rewrite"]},
        },
        ["project_id", "episode_number", "base_version_id", "instructions", "revision_mode"],
    ),
    DirectorActionSpec(
        "audit_episode",
        DirectorActionRisk.SAFE_WRITE,
        "Run the continuity audit for one draft version.",
        {
            "project_id": {"type": "string"},
            "version_id": {"type": "string"},
        },
        ["project_id", "version_id"],
    ),
]

# HIGH_IMPACT_WRITE actions require explicit user confirmation even in AGENT
# mode. The dispatcher refuses them until the user confirms in chat.
_HIGH_IMPACT = [
    DirectorActionSpec(
        "patch_story_bible",
        DirectorActionRisk.HIGH_IMPACT_WRITE,
        "Patch story bible fields and save a new bible version. Invalidates downstream artifacts.",
        {
            "project_id": {"type": "string"},
            "patch": {"type": "object"},
            "reason": {"type": "string"},
            "user_confirmed": {"type": "boolean"},
        },
        ["project_id", "patch", "reason"],
    ),
    DirectorActionSpec(
        "change_project_settings",
        DirectorActionRisk.HIGH_IMPACT_WRITE,
        "Change project-level settings (title, format, episode count, duration).",
        {
            "project_id": {"type": "string"},
            "updates": {"type": "object"},
            "reason": {"type": "string"},
            "user_confirmed": {"type": "boolean"},
        },
        ["project_id", "updates", "reason"],
    ),
]

# HUMAN_ONLY capabilities are intentionally NOT registered. Any model output
# referencing them must fail with DIRECTOR_ACTION_NOT_ALLOWED.
HUMAN_ONLY_ACTIONS = (
    "commit_episode",
    "force_commit_episode",
    "generate_production_package",
    "delete_project",
    "delete_canon",
    "delete_persona",
    "modify_persona_base",
    "force_override_audit",
)

ACTION_REGISTRY: dict[str, DirectorActionSpec] = {
    spec.name: spec
    for spec in (*_READ_ONLY, *_SAFE_WRITE, *_HIGH_IMPACT)
}

# Common alias mistakes models make when emitting arguments. They are
# normalized before validation so a semantically-correct call is not
# rejected over field naming.
ACTION_ARGUMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "base_version_id": ("episode_version_id", "base_version", "from_version_id"),
    "version_id": ("episode_version_id", "draft_version_id"),
    "instructions": ("instruction", "revision_instructions", "changes", "edit_instructions"),
    "revision_mode": ("mode", "scope"),
    "expected_project_revision": ("project_revision", "revision", "base_revision"),
    "direction_id": ("direction", "selected_direction_id"),
    "character_ids": ("characters", "participant_ids", "character_id"),
    "horizon_episodes": ("horizon", "episodes"),
    "count": ("num_directions", "n"),
}

# Per-action fields the HOST can safely default when the model omits them.
# Everything else in spec.required is hard-required and produces an
# explicit INVALID_ARGUMENTS failure the model can self-correct from.
ACTION_DEFAULTED_FIELDS: dict[str, set[str]] = {
    "patch_episode_plan": {"episode_number", "expected_project_revision"},
    "revise_episode_draft": {"episode_number", "base_version_id", "revision_mode"},
    "audit_episode": {"version_id", "episode_number"},
    "generate_episode_draft": {"episode_number"},
    "generate_forecast_directions": {"episode_number", "count"},
    "run_forecast": {"episode_number", "horizon_episodes"},
    "run_persona_rehearsal": {"episode_number", "max_turns"},
}


def normalize_action_arguments(
    action_name: str,
    arguments: dict[str, Any] | None,
    *,
    session_project_id: str | None = None,
    session_episode_number: int | None = None,
    session_project_revision: int | None = None,
) -> dict[str, Any]:
    """Normalize and validate model-emitted action arguments.

    - maps known aliases onto canonical field names,
    - coerces episode_number to int and defaults it to the session binding,
    - wraps a single instruction string into a list,
    - drops empty values.

    Returns the normalized arguments; callers still enforce spec.required
    (minus ACTION_DEFAULTED_FIELDS) so a wrong call fails with an explicit,
    self-correctable message instead of an obscure runner error.
    """
    normalized: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        canonical = key
        for target, aliases in ACTION_ARGUMENT_ALIASES.items():
            if key in aliases:
                canonical = target
                break
        if value is None or value == "":
            continue
        if canonical == "episode_number":
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
        if canonical == "instructions":
            value = [value] if isinstance(value, str) else list(value)
        normalized[canonical] = value
    # Host-injected scope: the model never needs to repeat project/episode.
    if "project_id" not in normalized and session_project_id:
        normalized["project_id"] = session_project_id
    # Session-bound defaults: the user is already working on a specific
    # episode; the model should not need to repeat it every turn.
    spec = ACTION_REGISTRY.get(action_name)
    if (
        "episode_number" not in normalized
        and session_episode_number
        and spec is not None
        and "episode_number" in spec.arguments
    ):
        normalized["episode_number"] = session_episode_number
    if (
        action_name == "patch_episode_plan"
        and "expected_project_revision" not in normalized
        and session_project_revision is not None
    ):
        # Optimistic-concurrency snapshot taken when the loop started.
        normalized["expected_project_revision"] = session_project_revision
    return normalized


def missing_required_arguments(spec: DirectorActionSpec, arguments: dict[str, Any]) -> list[str]:
    defaulted = ACTION_DEFAULTED_FIELDS.get(spec.name, set())
    return [field for field in spec.required if field not in arguments and field not in defaulted]


DIRECTOR_MAX_ACTIONS_PER_TURN = 12
DIRECTOR_MAX_AUTO_AUDIT_REPAIR_ROUNDS = 2

DIRECTOR_SYSTEM_PROMPT = """You are the Narrative Director Agent of Persona Continuum.

Your task is NOT to write the story yourself. You understand the author's
intent and operate the current project through the allowed Narrative Actions.
Creation is executed by the Screenwriter stage; review by the Reviewer stage.

Hard rules:
1. Read real project state before writing (Read Before Write).
2. Never claim an action succeeded unless its tool result status is "succeeded".
3. All modifications must go through formal actions.
4. Never modify the Persona Base; if a character feels off, revise the draft.
5. Never change Story Bible core rules on your own initiative.
6. Never treat NON-CANON simulations as canon.
7. Never commit canon; never generate production packages.
8. Once the continuity audit has BLOCKING == 0, stop and wait for human canon approval.
9. Do not keep revising just to clear WARNINGs; BLOCKING == 0 is the stop line.
10. Prefer local revision for local feedback; do not rewrite a whole episode unnecessarily.
11. Do not expand scope to other episodes unless the user asked for it.
12. If a state conflict is reported, re-read state and never overwrite newer versions.
13. HIGH_IMPACT_WRITE actions (story bible, project settings) require explicit
    user confirmation: first answer/ask, and only emit the action with
    "user_confirmed": true after the user clearly confirmed in chat.
14. Use EXACT argument names from the action registry (e.g. "base_version_id",
    NOT "episode_version_id"; "instructions" as a list of strings).
15. The session is already bound to one episode: omit "episode_number" unless
    targeting a different episode. For revise_episode_draft you may omit
    "base_version_id" to revise from the latest draft. For
    patch_episode_plan you may omit "expected_project_revision".

Decision schema:
- "answer": reply with "message" only (discuss/advise).
- "execute_action": set "action" and "arguments" (one action per turn).
- "request_user_input": ask the user and stop.
- "stop": finish with a final "message".

Respond with exactly one JSON value matching the schema."""
