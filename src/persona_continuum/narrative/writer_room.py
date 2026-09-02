"""AI Writer's Room — a declarative Room Protocol template.

Reuses the existing Room Protocol Engine (RoomProtocolType.CUSTOM with
declarative stages). This is NOT a second orchestrator: the writer room is a
protocol configuration that the standard MultiAgentOrchestrator executes.
"""

from __future__ import annotations

from typing import Any

from persona_continuum.room.models import RoomProtocolConfig

WRITER_ROOM_STAGE_IDS = (
    "story_analysis",
    "independent_proposals",
    "character_consistency_review",
    "mystery_review",
    "commercial_review",
    "cross_review",
    "editor_synthesis",
)

WRITER_ROOM_ROLE_BRIEFS: dict[str, str] = {
    "head_writer": (
        "You are the Head Writer. Own the episode's dramatic spine and final "
        "synthesis. Enforce the story bible's narrative constraints."
    ),
    "story_architect": (
        "You are the Story Architect. Analyze structure: goals, beats, "
        "causality, and pacing across episodes."
    ),
    "character_editor": (
        "You are the Character Editor. Guard persona consistency, motivation, "
        "and character-arc progression."
    ),
    "mystery_editor": (
        "You are the Mystery Editor. Track clues, foreshadowing, and the "
        "information gap between characters and the audience."
    ),
    "continuity_editor": (
        "You are the Continuity Editor. Check canon, timeline, world rules, "
        "and knowledge-state violations."
    ),
    "commercial_editor": (
        "You are the Commercial Short-Drama Editor. Optimize hook strength, "
        "midpoint turn, cliffhanger, and retention pacing."
    ),
    "director": (
        "You are the Director. Translate beats into visual and staging intent "
        "for the production package."
    ),
    "audience_proxy": (
        "You are the Audience Proxy. React as a first-time viewer: flag "
        "confusion, boredom, or premature reveals."
    ),
}


def build_writer_room_protocol_config(
    *,
    cross_review: bool = True,
    max_rounds: int = 1,
) -> RoomProtocolConfig:
    """Declarative stages executed by the standard Room Protocol Engine."""
    stages: list[dict[str, Any]] = [
        {
            "id": "story_analysis",
            "label": "Story Analysis",
            "allowed_roles": ["story_architect", "head_writer"],
            "action": "analysis",
        },
        {
            "id": "independent_proposals",
            "label": "Independent Proposals",
            "allowed_roles": [
                "head_writer",
                "story_architect",
                "character_editor",
                "mystery_editor",
                "commercial_editor",
                "director",
                "audience_proxy",
            ],
            "action": "analysis",
            # Sequential on purpose: pooled CLI app-servers multiplex several
            # logical threads over one process, and concurrent turns on the
            # same pooled process can race.  Members still propose
            # independently because earlier submissions are hidden from them.
            "parallel": False,
        },
        {
            "id": "character_consistency_review",
            "label": "Character Consistency Review",
            "allowed_roles": ["character_editor", "continuity_editor"],
            "action": "review",
            "optional": True,
        },
        {
            "id": "mystery_review",
            "label": "Mystery & Information Control Review",
            "allowed_roles": ["mystery_editor"],
            "action": "review",
            "optional": True,
        },
        {
            "id": "commercial_review",
            "label": "Commercial Review",
            "allowed_roles": ["commercial_editor"],
            "action": "review",
            "optional": True,
        },
        {
            "id": "cross_review",
            "label": "Cross Review",
            "allowed_roles": ["head_writer", "continuity_editor", "audience_proxy"],
            "action": "review",
            "optional": not cross_review,
        },
        {
            "id": "editor_synthesis",
            "label": "Editor Synthesis",
            "allowed_roles": ["head_writer"],
            "action": "synthesis",
        },
    ]
    return RoomProtocolConfig(
        stages=stages,
        transitions=[],
        # Only the Head Writer is a hard requirement; other advisory roles are
        # optional so small rooms can run with a single participant.
        allowed_roles=["head_writer"],
        completion_condition={"stage": "editor_synthesis"},
        max_rounds=max_rounds,
        cross_review=cross_review,
        finalizer_role="head_writer",
    )


def writer_room_shared_context(
    *,
    project_title: str,
    episode_number: int,
    episode_goal: str,
    writer_context: dict[str, Any],
) -> str:
    """Compact shared context text injected into every writer room turn."""
    import json

    return (
        f"### WRITER'S ROOM — {project_title} EP{episode_number:02d}\n"
        f"Episode goal: {episode_goal}\n"
        "You are reviewing one episode of an original fictional work. "
        "Story truth below is AUTHOR-VIEW ONLY: never assume characters know it.\n"
        "Context:\n```json\n"
        f"{json.dumps(writer_context, ensure_ascii=False, default=str)[:4000]}\n```"
    )
