from __future__ import annotations

import json
import re
from types import MethodType
from typing import Any

RUNTIME = {
    "agent_id": "fake_agent",
    "model_id": "fake-gpt-5",
    "reasoning_effort": "high",
}


def install_narrative_responder(app: Any, monkeypatch: Any) -> Any:
    adapter = app.agent_registry.get_adapter("fake_agent")
    assert adapter is not None
    original = adapter._generate_mock_response

    def responder(self: Any, session: Any, turn: Any) -> str:
        prompt = turn.user_message or ""
        if "Design the season/arc structure" in prompt:
            match = re.search(r"Episode count: (\d+)", prompt)
            count = int(match.group(1)) if match else 3
            return json.dumps(
                {"arcs": [{"start_episode": 1, "end_episode": count, "goal": "Escalate"}]}
            )
        if "Write structured episode plans" in prompt:
            match = re.search(r"Range: EP(\d+)-EP(\d+)", prompt)
            start, end = (int(match.group(1)), int(match.group(2))) if match else (1, 1)
            return json.dumps(
                {
                    "episode_plans": [
                        {
                            "episode_number": number,
                            "title": f"AI Episode {number}",
                            "narrative_goal": f"Causal goal {number}",
                            "hook": f"Hook {number}",
                            "beats": [
                                {"order": 1, "title": "Turn", "description": "Advance"},
                                {"order": 2, "title": "Cost", "description": "Complicate"},
                            ],
                            "must_happen": [f"event-{number}"],
                            "must_not_happen": ["premature final reveal"],
                            "characters": ["fang"],
                            "plot_threads": ["main"],
                            "clues_to_plant": [f"clue-{number}"],
                            "clues_to_echo": [],
                            "reveal_targets": [],
                            "forbidden_reveals": ["digital source"],
                            "relationship_targets": [],
                            "emotion_targets": [],
                            "world_state_targets": [],
                            "cliffhanger": f"Cliffhanger {number}",
                        }
                        for number in range(start, end + 1)
                    ]
                }
            )
        if "Audit this master outline" in prompt:
            return json.dumps({"blocking_issues": [], "warnings": []})
        if "Repair this master outline" in prompt:
            match = re.search(r"Window: EP(\d+)-EP(\d+)", prompt)
            start, end = (int(match.group(1)), int(match.group(2))) if match else (1, 1)
            return json.dumps(
                {
                    "episode_plans": [
                        {
                            "episode_number": number,
                            "title": f"AI Episode {number}",
                            "narrative_goal": f"Causal goal {number}",
                            "hook": f"Hook {number}",
                            "beats": [
                                {"order": 1, "title": "Turn", "description": "Advance"},
                                {"order": 2, "title": "Cost", "description": "Complicate"},
                            ],
                            "must_happen": [f"event-{number}"],
                            "must_not_happen": ["premature final reveal"],
                            "characters": ["fang"],
                            "plot_threads": ["main"],
                            "clues_to_plant": [f"clue-{number}"],
                            "clues_to_echo": [],
                            "reveal_targets": [],
                            "forbidden_reveals": ["digital source"],
                            "relationship_targets": [],
                            "emotion_targets": [],
                            "world_state_targets": [],
                            "cliffhanger": f"Cliffhanger {number}",
                        }
                        for number in range(start, end + 1)
                    ]
                }
            )
        if "Create a story bible JSON" in prompt:
            return json.dumps(
                {
                    "premise": "AI premise",
                    "core_question": "Who sent the message?",
                    "theme": "Identity",
                    "world_rules": ["No physical time travel"],
                    "final_truth": ["Digital Fang sent the message"],
                    "characters": [
                        {"id": "fang", "name": "Fang", "role": "lead", "description": "worker"}
                    ],
                    "locations": [
                        {"id": "office", "name": "Office", "description": "Workplace"}
                    ],
                    "master_timeline": [],
                }
            )
        if "Write a production-ready structured episode screenplay" in prompt:
            return json.dumps(
                {
                    "episode_number": 1,
                    "title": "AI Draft",
                    "duration": 90,
                    "hook": "A message arrives",
                    "scenes": [
                        {
                            "scene_number": 1,
                            "location": "Office",
                            "time": "18:29",
                            "characters": ["fang"],
                            "duration": 45,
                            "action": ["Fang opens the message"],
                            "dialogue": [{"speaker": "Fang", "text": "This cannot be real."}],
                            "visual_direction": "Phone glow",
                            "narrative_function": "Hook",
                        }
                    ],
                    "reveals": [],
                    "clues_planted": ["timestamp"],
                    "clues_echoed": [],
                    "relationship_changes": [],
                    "knowledge_changes": [],
                    "cliffhanger": "Do not trust future me",
                }
            )
        if "Act as a model-agnostic Director" in prompt:
            return json.dumps(
                {
                    "shot_list": [
                        {
                            "shot_number": 1,
                            "start_time": "00:00",
                            "end_time": "00:05",
                            "duration_seconds": 5,
                            "shot_size": "close-up",
                            "camera": "eye-level",
                            "movement": "slow push",
                            "characters": ["Fang"],
                            "action": "Phone illuminates",
                            "dialogue": "",
                            "location": "Office",
                            "visual_prompt": "generic cinematic office phone close-up",
                            "motion_prompt": "slow push toward phone",
                            "negative_constraints": ["no costume drift"],
                            "continuity_constraints": ["same phone"],
                            "sfx": ["notification"],
                            "bgm": "low pulse",
                            "transition": "cut",
                        }
                    ],
                    "bgm_direction": "restrained pulse",
                    "continuity_notes": ["generic schema; no vendor binding"],
                }
            )
        if "Evaluate this NON-CANON narrative branch" in prompt:
            return json.dumps(
                {
                    "scores": {
                        "emotional_intensity": 0.8,
                        "novelty": 0.7,
                        "conflict_density": 0.75,
                    },
                    "risks": ["May reveal too much"],
                }
            )
        if "Generate distinct, plausible NON-CANON directions" in prompt:
            return json.dumps(
                {
                    "directions": [
                        {"label": "A", "description": "Trust Chen", "beats": []},
                        {"label": "B", "description": "Test Chen", "beats": []},
                    ]
                }
            )
        if "Review this AI episode draft" in prompt:
            return json.dumps({"findings": []})
        if "You are the Narrative Shooting Agent" in prompt:
            # Default scripted Shooting decision: a single terminating turn.
            # Tests that need a full action script wrap this responder.
            return json.dumps(
                {
                    "decision": "execute_action",
                    "action": "get_video_model_profiles",
                    "arguments": {},
                }
            )
        if "You are the Reference Asset Prompt Compiler" in prompt:
            # Guide asset-prompt refinement: echo every REAL asset_key found
            # in the payload (the output_contract example "string, unchanged
            # from input" never matches the token charset) with a deterministic
            # refined prompt (non-empty, no placeholder phrase).
            asset_keys = re.findall(r'"asset_key":\s*"([^"]+)"', prompt)
            asset_keys = list(
                dict.fromkeys(
                    key for key in asset_keys if re.fullmatch(r"[@\w\-]+", key)
                )
            )
            return json.dumps(
                {
                    "assets": [
                        {
                            "asset_key": key,
                            "generation_prompt": (
                                f"Refined reference prompt {index + 1} for {key}: "
                                "full-body identity anchor with locked wardrobe, "
                                "palette and silhouette, self-contained framing "
                                "notes and lighting reference."
                            ),
                        }
                        for index, key in enumerate(asset_keys)
                    ]
                }
            )
        if "Refine each clip's copy-ready prompt" in prompt:
            # Guide clip-prompt refinement (distinct from the model-package
            # compiler branch above by the copy-ready phrasing). Must stay
            # BEFORE the generic Video Prompt Compiler match: this message
            # also starts with that prefix.
            clip_ids = re.findall(r'"clip_id":\s*"([^"]+)"', prompt)
            clip_ids = list(
                dict.fromkeys(
                    cid for cid in clip_ids if re.fullmatch(r"[\w\-]+", cid)
                )
            )
            return json.dumps(
                {
                    "clips": [
                        {
                            "clip_id": clip_id,
                            "copy_ready_prompt": (
                                f"Refined copy-ready guide prompt {index + 1} for "
                                f"{clip_id}: complete self-contained shot description "
                                "with identity anchors, camera, motion, continuity "
                                "and ending sections."
                            ),
                        }
                        for index, clip_id in enumerate(clip_ids)
                    ]
                }
            )
        if "You are the Video Prompt Compiler for one target video model" in prompt:
            # Clip-prompt refinement: echo every clip_id found in the payload
            # with a deterministic refined prompt (never empty).
            clip_ids = re.findall(r'"clip_id":\s*"([^"]+)"', prompt)
            # Only echo REAL clip ids: the payload's output_contract embeds a
            # documentation example ("string, unchanged from input") that a
            # well-behaved model never returns as a data row.
            clip_ids = list(
                dict.fromkeys(
                    cid for cid in clip_ids if re.fullmatch(r"[\w\-]+", cid)
                )
            )
            return json.dumps(
                {
                    "clips": [
                        {
                            "clip_id": clip_id,
                            "prompt": f"Refined cinematic prompt for clip {index + 1}",
                            "recommended_settings": {},
                        }
                        for index, clip_id in enumerate(clip_ids)
                    ]
                }
            )
        return original(session, turn)

    monkeypatch.setattr(adapter, "_generate_mock_response", MethodType(responder, adapter))
    return adapter


def seed_project(app: Any, *, episodes: int = 3) -> tuple[Any, Any]:
    project = app.narratives.create_project(
        title="Future Layoff",
        logline="A message predicts a layoff and death",
        format="micro_drama",
        planned_episode_count=episodes,
        runtime_assignment={
            stage: dict(RUNTIME)
            for stage in (
                "story_architect",
                "outline_writer",
                "forecast_simulator",
                "screenwriter",
                "reviewer",
                "production_planner",
                "scene_actor",
                "director",
                "shooting_agent",
            )
        },
    )
    bible = app.narratives.save_bible(
        project.id,
        {
            "premise": "A worker receives a future notice",
            "final_truth": ["Digital Fang is the real source"],
            "world_rules": ["No physical time travel"],
        },
    )
    return project, bible
