"""Narrative Shooting Agent — action registry, permissions, and contracts.

The Shooting Agent is the *production-side* sibling of the Director: it turns
a canon production master into per-model generation clips and video prompts.
It understands the author's natural-language production requests and emits
strictly structured JSON decisions; every write goes through
``NarrativeShootingService`` dispatched domain actions (see
``application/shooting_service.py``).

Design invariants:
- No native tool calling: the Shooting runtime only produces JSON matching
  ``SHOOTING_DECISION_SCHEMA`` via ``execute_structured``.
- Permission model: every action carries an explicit risk level. Actions that
  would touch story/canon (commit_episode, draft revision, story bible or
  persona mutation) are *not registered at all* — the dispatcher answers
  ``SHOOTING_ACTION_NOT_ALLOWED`` for them by construction.
- Zero overlap with the Director action registry: the two registries share
  no action names, so an agent can never be routed across both hosts.
- Knowledge firewall: the Shooting context must never include Story Bible
  ``final_truth`` or author-only notes — production needs only.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from persona_continuum.application._utils import dumps

SHOOTING_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["decision"],
    "properties": {
        "decision": {
            "enum": ["execute_action", "reply", "finish"],
        },
        "action": {"type": "string"},
        "arguments": {"type": "object"},
        "reply_text": {"type": "string"},
        "finish_summary": {"type": "string"},
    },
}

SHOOTING_ACTION_NOT_ALLOWED = "SHOOTING_ACTION_NOT_ALLOWED"
SHOOTING_SESSION_NOT_FOUND = "SHOOTING_SESSION_NOT_FOUND"
SHOOTING_SESSION_BUSY = "SHOOTING_SESSION_BUSY"
SHOOTING_ACTION_INVALID_ARGUMENTS = "SHOOTING_ACTION_INVALID_ARGUMENTS"
SHOOTING_ACTION_FAILED = "SHOOTING_ACTION_FAILED"

# Session mode aliases accepted from API/UI input; the domain stores the
# canonical english value (see NarrativeShootingSession.mode).
SHOOTING_MODE_ALIASES: dict[str, str] = {
    "讨论": "discuss",
    "建议": "advise",
    "代理": "agent",
    "discuss": "discuss",
    "advise": "advise",
    "agent": "agent",
}


class ShootingActionRisk(StrEnum):
    READ_ONLY = "read_only"
    SAFE_WRITE = "safe_write"


class ShootingActionSpec:
    """One registered Shooting action and its argument schema."""

    def __init__(
        self,
        name: str,
        risk: ShootingActionRisk,
        description: str,
        arguments: dict[str, Any] | None = None,
        required: list[str] | None = None,
    ) -> None:
        self.name = name
        self.risk = risk
        self.description = description
        self.arguments = arguments or {}
        self.required = required or []


def _spec(
    name: str,
    risk: ShootingActionRisk,
    description: str,
    arguments: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> ShootingActionSpec:
    return ShootingActionSpec(name, risk, description, arguments, required)


_PROJECT_ONLY = {"project_id": {"type": "string"}}
_PKG_ONLY = {
    "project_id": {"type": "string"},
    "production_package_id": {"type": "string"},
}
_PROMPT_PKG_ONLY = {
    "project_id": {"type": "string"},
    "prompt_package_id": {"type": "string"},
}
_CLIP_ARGS = {
    **_PROMPT_PKG_ONLY,
    "clip_id": {"type": "string"},
}
_GENERATION_OPTIONS = {
    "aspect_ratio": {"type": "string"},
    "quality_priority": {"type": "string"},
    "generation_strategy": {"type": "string"},
    "continuity_strategy": {"type": "string"},
    "audio_strategy": {"type": "string"},
    "prompt_language": {"type": "string"},
}


# READ_ONLY actions are available in every Shooting mode.
_READ_ONLY = [
    _spec(
        "get_production_package",
        ShootingActionRisk.READ_ONLY,
        "Read one production package (screenplay summary, shot list, tracks).",
        _PKG_ONLY,
        ["project_id", "production_package_id"],
    ),
    _spec(
        "get_shot_list",
        ShootingActionRisk.READ_ONLY,
        "Read the ordered shot list of a production package.",
        _PKG_ONLY,
        ["project_id", "production_package_id"],
    ),
    _spec(
        "get_character_visual_bible",
        ShootingActionRisk.READ_ONLY,
        "Read the character visual bible of a production package.",
        _PKG_ONLY,
        ["project_id", "production_package_id"],
    ),
    _spec(
        "get_location_visual_bible",
        ShootingActionRisk.READ_ONLY,
        "Read the location visual bible of a production package.",
        _PKG_ONLY,
        ["project_id", "production_package_id"],
    ),
    _spec(
        "get_prop_visual_bible",
        ShootingActionRisk.READ_ONLY,
        "Read the prop visual bible of a production package.",
        _PKG_ONLY,
        ["project_id", "production_package_id"],
    ),
    _spec(
        "get_dialogue_track",
        ShootingActionRisk.READ_ONLY,
        "Read the dialogue track of a production package.",
        _PKG_ONLY,
        ["project_id", "production_package_id"],
    ),
    _spec(
        "get_subtitle_track",
        ShootingActionRisk.READ_ONLY,
        "Read the subtitle track of a production package.",
        _PKG_ONLY,
        ["project_id", "production_package_id"],
    ),
    _spec(
        "get_sfx_plan",
        ShootingActionRisk.READ_ONLY,
        "Read the sound effect plan of a production package.",
        _PKG_ONLY,
        ["project_id", "production_package_id"],
    ),
    _spec(
        "get_bgm_direction",
        ShootingActionRisk.READ_ONLY,
        "Read the BGM direction of a production package.",
        _PKG_ONLY,
        ["project_id", "production_package_id"],
    ),
    _spec(
        "get_continuity_notes",
        ShootingActionRisk.READ_ONLY,
        "Read the continuity notes of a production package.",
        _PKG_ONLY,
        ["project_id", "production_package_id"],
    ),
    _spec(
        "get_video_model_profiles",
        ShootingActionRisk.READ_ONLY,
        "List built-in video model capability profiles (digests).",
        _PROJECT_ONLY,
        ["project_id"],
    ),
    _spec(
        "get_model_prompt_packages",
        ShootingActionRisk.READ_ONLY,
        "List model prompt packages of the project (optionally per production package).",
        {
            "project_id": {"type": "string"},
            "production_package_id": {"type": "string"},
        },
        ["project_id"],
    ),
    _spec(
        "get_production_assets",
        ShootingActionRisk.READ_ONLY,
        "List registered reference/frame assets (optionally per production package).",
        {
            "project_id": {"type": "string"},
            "production_package_id": {"type": "string"},
        },
        ["project_id"],
    ),
    _spec(
        "get_canon_episode",
        ShootingActionRisk.READ_ONLY,
        "Read the canon episode summary bound to the session.",
        {
            "project_id": {"type": "string"},
            "episode_number": {"type": "integer"},
        },
        ["project_id"],
    ),
]

# SAFE_WRITE actions run automatically in AGENT (代理) mode. Every write is
# scoped to model prompt packages / clips / assets — never story or canon.
_SAFE_WRITE = [
    ShootingActionSpec(
        "create_clip_plan",
        ShootingActionRisk.SAFE_WRITE,
        "Plan generation clips for one target video model profile (idempotent).",
        {
            **_PKG_ONLY,
            "profile_id": {"type": "string"},
            **_GENERATION_OPTIONS,
        },
        ["project_id", "production_package_id", "profile_id"],
    ),
    ShootingActionSpec(
        "revise_clip_plan",
        ShootingActionRisk.SAFE_WRITE,
        "Revise the clip plan in place: re-plan options or per-clip duration overrides.",
        {
            **_PROMPT_PKG_ONLY,
            "instruction": {"type": "string"},
            "aspect_ratio": {"type": "string"},
            "quality_priority": {"type": "string"},
            "prompt_language": {"type": "string"},
            "clip_durations": {"type": "object"},
        },
        ["project_id", "prompt_package_id", "instruction"],
    ),
    ShootingActionSpec(
        "create_model_prompt_package",
        ShootingActionRisk.SAFE_WRITE,
        "Compile the full profile-specific prompt package (4-stage pipeline, idempotent).",
        {
            **_PKG_ONLY,
            "profile_id": {"type": "string"},
            **_GENERATION_OPTIONS,
        },
        ["project_id", "production_package_id", "profile_id"],
    ),
    ShootingActionSpec(
        "revise_generation_clip_prompt",
        ShootingActionRisk.SAFE_WRITE,
        "Revise one clip's prompt fields in place (instruction recorded in trace).",
        {
            **_CLIP_ARGS,
            "instruction": {"type": "string"},
            "prompt": {"type": "string"},
            "negative_prompt": {"type": "string"},
            "audio_prompt": {"type": "string"},
        },
        ["project_id", "prompt_package_id", "clip_id", "instruction"],
    ),
    ShootingActionSpec(
        "set_clip_reference_assets",
        ShootingActionRisk.SAFE_WRITE,
        "Set the reference asset ids of one clip (assets must already exist).",
        {
            **_CLIP_ARGS,
            "reference_asset_ids": {"type": "array"},
        },
        ["project_id", "prompt_package_id", "clip_id", "reference_asset_ids"],
    ),
    ShootingActionSpec(
        "set_generation_mode",
        ShootingActionRisk.SAFE_WRITE,
        "Set the generation mode of one clip (validated against the profile).",
        {
            **_CLIP_ARGS,
            "generation_mode": {"type": "string"},
        },
        ["project_id", "prompt_package_id", "clip_id", "generation_mode"],
    ),
    ShootingActionSpec(
        "set_target_video_model",
        ShootingActionRisk.SAFE_WRITE,
        "Switch the target video model profile; affected clips are recompiled "
        "deterministically and validated against the new profile.",
        {
            **_PROMPT_PKG_ONLY,
            "profile_id": {"type": "string"},
        },
        ["project_id", "prompt_package_id", "profile_id"],
    ),
    ShootingActionSpec(
        "set_continuity_strategy",
        ShootingActionRisk.SAFE_WRITE,
        "Set the package-level continuity strategy label.",
        {
            **_PROMPT_PKG_ONLY,
            "continuity_strategy": {"type": "string"},
        },
        ["project_id", "prompt_package_id", "continuity_strategy"],
    ),
    ShootingActionSpec(
        "build_complete_production_guide",
        ShootingActionRisk.SAFE_WRITE,
        "Build the complete executable video production guide (required assets "
        "with image prompts, copy-ready clip prompts, continuity/frame-chain "
        "workflow, post-production plan) on top of one prompt package.",
        {
            **_PROMPT_PKG_ONLY,
            "production_package_id": {"type": "string"},
        },
        ["project_id", "prompt_package_id"],
    ),
]

# Story/canon capabilities are intentionally NOT registered. Any model output
# referencing them must fail with SHOOTING_ACTION_NOT_ALLOWED.
SHOOTING_FORBIDDEN_ACTIONS = (
    "commit_episode",
    "generate_episode_draft",
    "revise_episode_draft",
    "patch_episode_plan",
    "patch_story_bible",
    "audit_episode",
    "generate_production_package",
    "delete_project",
    "delete_canon",
    "delete_persona",
    "modify_persona_base",
)

SHOOTING_ACTION_REGISTRY: dict[str, ShootingActionSpec] = {
    spec.name: spec for spec in (*_READ_ONLY, *_SAFE_WRITE)
}

# Common alias mistakes models make when emitting arguments. They are
# normalized before validation so a semantically-correct call is not
# rejected over field naming.
SHOOTING_ACTION_ARGUMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "prompt_package_id": ("package_id", "model_prompt_package_id", "prompt_pkg_id"),
    "production_package_id": ("production_id", "prod_package_id", "production_pkg_id"),
    "reference_asset_ids": ("reference_assets", "asset_ids", "reference_ids"),
    "generation_mode": ("mode",),
    "instruction": ("instructions", "revision_instruction", "note"),
    "duration_seconds": ("duration", "duration_sec"),
    "clip_id": ("clip", "clip_number", "clip_num"),
    "profile_id": ("video_profile_id", "model_profile_id", "target_profile_id"),
    "continuity_strategy": ("continuity", "strategy"),
}

# Per-action fields the HOST can safely default when the model omits them.
# "prompt_package_id" defaults to the latest non-stale package of the
# session-bound episode; "production_package_id" on READ paths defaults to
# the latest final (non-preview) package of the bound episode.
SHOOTING_ACTION_DEFAULTED_FIELDS: dict[str, set[str]] = {
    "get_production_package": {"production_package_id"},
    "get_shot_list": {"production_package_id"},
    "get_character_visual_bible": {"production_package_id"},
    "get_location_visual_bible": {"production_package_id"},
    "get_prop_visual_bible": {"production_package_id"},
    "get_dialogue_track": {"production_package_id"},
    "get_subtitle_track": {"production_package_id"},
    "get_sfx_plan": {"production_package_id"},
    "get_bgm_direction": {"production_package_id"},
    "get_continuity_notes": {"production_package_id"},
    "get_canon_episode": {"episode_number"},
    "get_model_prompt_packages": {"production_package_id"},
    "get_production_assets": {"production_package_id"},
    "revise_clip_plan": {"prompt_package_id"},
    "revise_generation_clip_prompt": {"prompt_package_id"},
    "set_clip_reference_assets": {"prompt_package_id"},
    "set_generation_mode": {"prompt_package_id"},
    "set_target_video_model": {"prompt_package_id"},
    "set_continuity_strategy": {"prompt_package_id"},
    "build_complete_production_guide": {"prompt_package_id"},
}


def normalize_shooting_action_arguments(
    action_name: str,
    arguments: dict[str, Any] | None,
    *,
    session_project_id: str | None = None,
    session_episode_number: int | None = None,
) -> dict[str, Any]:
    """Normalize and validate model-emitted action arguments.

    - maps known aliases onto canonical field names,
    - coerces episode_number to int and defaults it to the session binding,
    - drops empty values.

    Returns the normalized arguments; callers still enforce spec.required
    (minus SHOOTING_ACTION_DEFAULTED_FIELDS) so a wrong call fails with an
    explicit, self-correctable message instead of an obscure runner error.
    ``project_id`` and session ids are host-injected scope and never trusted
    from the model.
    """
    normalized: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        canonical = key
        for target, aliases in SHOOTING_ACTION_ARGUMENT_ALIASES.items():
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
        normalized[canonical] = value
    # Host-injected scope: the session's project always wins. The model
    # never needs to repeat project/episode, and a model-supplied project_id
    # can never widen (or redirect) the session's scope.
    if session_project_id:
        normalized["project_id"] = session_project_id
    spec = SHOOTING_ACTION_REGISTRY.get(action_name)
    if (
        "episode_number" not in normalized
        and session_episode_number
        and spec is not None
        and "episode_number" in spec.arguments
    ):
        normalized["episode_number"] = session_episode_number
    return normalized


def missing_shooting_required_arguments(
    spec: ShootingActionSpec, arguments: dict[str, Any]
) -> list[str]:
    defaulted = SHOOTING_ACTION_DEFAULTED_FIELDS.get(spec.name, set())
    return [field for field in spec.required if field not in arguments and field not in defaulted]


SHOOTING_MAX_ACTIONS_PER_TURN = 8

SHOOTING_SYSTEM_PROMPT = """You are the Narrative Shooting Agent of Persona Continuum.

Your task is NOT to write or change the story. You orchestrate the
production side end to end: read the canon production master, plan
generation clips per target video model profile, arrange reference assets,
compile/revise model-specific video prompts, and assemble the complete
executable video production guide (assets -> clips -> prompts -> guide) on
natural-language request. Creation of the story itself is owned by the
Director/Screenwriter pipeline; you never compete with it.

Hard rules:
1. Read real production state before writing (Read Before Write).
2. Never claim an action succeeded unless its tool result status is "succeeded".
3. All modifications must go through formal shooting actions.
4. NEVER modify the story bible, episode plan, draft versions, audits, canon,
   or the Persona Base. Those actions do not exist for you and return
   SHOOTING_ACTION_NOT_ALLOWED. Never attempt to route around them.
5. Only emit actions from the provided allowed_actions registry.
6. The HOST validates every output against the target model's capability
   profile (durations, aspect ratio, modes, reference limits). Never claim a
   capability the profile digest does not document; unknown (null) flags are
   hard requirement gaps.
7. Prefer deterministic recompilation: when the target model changes, let the
   host recompile clips instead of hand-editing prompts.
8. Do not expand scope to other episodes or production packages unless the
   user asked for it.
9. Use EXACT argument names from the action registry (e.g. "prompt_package_id",
   "production_package_id", "reference_asset_ids"; "instruction" as a string).
10. The session is already bound to one project (and usually one episode):
    omit "project_id"/"episode_number"; "prompt_package_id" may be omitted to
    target the latest non-stale package of the bound episode.
11. Keep prompts inside the profile's language preference and documented
    character budget.

Interaction modes:
- "讨论" (discuss): read-only. Answer questions and propose plans; every write
  action is rejected by the host.
- "建议" (advise): read-only. Produce a concrete shooting/prompt plan in your
  reply; do not execute writes.
- "代理" (agent): full autonomy. Execute SAFE_WRITE actions directly.

Decision schema:
- "execute_action": set "action" and "arguments" (one action per turn).
- "reply": answer with "reply_text" (discuss/advise or clarifying questions).
- "finish": end the turn with "finish_summary" when the user request is done.

Respond with exactly one JSON value matching the schema."""

# Section priorities for the deterministic context-fit ladder: when the
# assembled context exceeds the budget, sections are dropped from the HIGHEST
# priority number (least important) first. Priority 0 sections — including
# the user request — are always kept.
_CONTEXT_SECTION_PRIORITY: dict[str, int] = {
    "task": 0,
    "mode": 0,
    "session": 0,
    "user_request": 0,
    "allowed_actions": 0,
    "profile_digest": 1,
    "prompt_package": 1,
    "production_package": 2,
    "canon_episode": 2,
    "clip_hints": 3,
    "visual_bibles": 3,
    "production_assets": 4,
    "recent_messages": 4,
}


def _truncate_strings(value: Any, limit: int) -> Any:
    """Deterministically shorten every long string inside a JSON-ish value."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit]
    if isinstance(value, list):
        return [_truncate_strings(item, limit) for item in value]
    if isinstance(value, dict):
        return {key: _truncate_strings(item, limit) for key, item in value.items()}
    return value


def build_shooting_context(
    *,
    task: str = "narrative_shooting",
    mode: str = "agent",
    session: dict[str, Any] | None = None,
    user_request: str = "",
    allowed_actions: list[dict[str, str]] | None = None,
    profile_digest: dict[str, Any] | None = None,
    prompt_package: dict[str, Any] | None = None,
    production_package: dict[str, Any] | None = None,
    canon_episode: dict[str, Any] | None = None,
    clip_hints: dict[str, Any] | None = None,
    visual_bibles: dict[str, list[dict[str, Any]]] | None = None,
    production_assets: list[dict[str, Any]] | None = None,
    recent_messages: list[dict[str, Any]] | None = None,
    max_chars: int = 24000,
) -> str:
    """Assemble the Shooting Agent context payload with a hard size guard.

    Sections are serialized compactly; when the joined JSON exceeds
    ``max_chars`` the least-priority sections (see ``_CONTEXT_SECTION_PRIORITY``)
    are dropped first, then remaining long strings are truncated. The user
    request, session scope and action registry are never dropped.

    MUST NOT be fed Story Bible ``final_truth`` or author-only notes: callers
    pass production-scope data only (canon production master + bibles).
    """
    sections: dict[str, Any] = {
        "task": task,
        "mode": mode,
        "session": session or {},
        "user_request": user_request,
        "allowed_actions": allowed_actions or [],
    }
    optional: dict[str, Any] = {
        "profile_digest": profile_digest,
        "prompt_package": prompt_package,
        "production_package": production_package,
        "canon_episode": canon_episode,
        "clip_hints": clip_hints,
        "visual_bibles": visual_bibles,
        "production_assets": production_assets,
        "recent_messages": recent_messages,
    }
    for key, value in optional.items():
        if value:
            sections[key] = value

    def render(payload: dict[str, Any]) -> str:
        return dumps(payload)

    rendered = render(sections)
    if len(rendered) <= max_chars:
        return rendered
    # Deterministic fit ladder: drop least-priority sections first.
    remaining = dict(sections)
    droppable = sorted(
        (
            (priority, key)
            for key, priority in _CONTEXT_SECTION_PRIORITY.items()
            if key in remaining and priority > 0
        ),
        reverse=True,
    )
    for _priority, key in droppable:
        remaining.pop(key, None)
        rendered = render(remaining)
        if len(rendered) <= max_chars:
            return rendered
    # Everything essential survived and it is still too large: truncate the
    # long strings of non-critical sections while keeping the user request.
    for key in list(remaining):
        if key == "user_request":
            continue
        remaining[key] = _truncate_strings(remaining[key], 400)
    rendered = render(remaining)
    if len(rendered) <= max_chars:
        return rendered
    budget = max(0, max_chars - len(render(remaining)) + len(remaining["user_request"]))
    remaining["user_request"] = user_request[: max(0, budget)]
    return render(remaining)
