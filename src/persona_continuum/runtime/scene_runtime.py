"""Monotonic scene clock, spatial reducer, and confirmed action-event reducer."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from persona_continuum.domain.scene import (
    ActionEvent,
    RoomSceneState,
    SceneEvent,
    TurnChannels,
    aware,
)
from persona_continuum.runtime.turn_normalizer import normalize_turn


class AcceptTurnResult(tuple[TurnChannels, list[SceneEvent]]):
    channels: TurnChannels
    scene_events: list[SceneEvent]
    action_events: list[ActionEvent]

    def __new__(
        cls,
        channels: TurnChannels,
        scene_events: list[SceneEvent],
        action_events: list[ActionEvent] | None = None,
    ) -> AcceptTurnResult:
        ae = action_events or []
        obj = super().__new__(cls, (channels, scene_events))
        obj.channels = channels
        obj.scene_events = scene_events
        obj.action_events = ae
        return obj


class SceneRuntime:
    @staticmethod
    def tick(state: RoomSceneState, wall_time: datetime | None = None) -> None:
        wall = aware(wall_time or datetime.now(UTC))
        elapsed = max(0.0, (wall - state.clock_wall_time).total_seconds())
        if state.time_mode != "narrative":
            state.scene_time = (
                state.scene_time.astimezone(UTC) + timedelta(seconds=elapsed)
            ).astimezone(ZoneInfo(state.timezone))
        state.clock_wall_time = max(wall, state.clock_wall_time)

    def accept_turn(
        self,
        state: RoomSceneState,
        *,
        room_id: str,
        turn_id: str,
        actor: str,
        raw_content: str,
        wall_time: datetime | None = None,
        channels: TurnChannels | None = None,
        advance_clock: bool = True,
        input_mode: str = "speech",
    ) -> AcceptTurnResult:
        wall = aware(wall_time or datetime.now(UTC))
        if advance_clock:
            self.tick(state, wall)
        normalized = channels or normalize_turn(raw_content, actor=actor, input_mode=input_mode)
        scene_events: list[SceneEvent] = []
        action_events: list[ActionEvent] = []

        # 1. Process scene events (time, presence, activity)
        for index, candidate in enumerate(normalized.scene_events):
            kind = candidate.get("type")
            payload = dict(candidate.get("payload") or {})
            if kind == "time_advance":
                if actor != "user" or state.time_mode == "realtime":
                    continue
                before = state.scene_time
                if payload.get("intent") == "next_morning":
                    local = before.astimezone(ZoneInfo(state.timezone))
                    target = (local + timedelta(days=1)).replace(
                        hour=8, minute=0, second=0, microsecond=0
                    )
                else:
                    seconds = float(payload.get("seconds", 0))
                    if not 0 <= seconds <= 86400 * 366:
                        continue
                    target = before + timedelta(seconds=seconds)
                state.scene_time = max(before, target)
                payload.update(
                    previous_scene_time=before.isoformat(),
                    current_scene_time=state.scene_time.isoformat(),
                )
            elif kind == "activity_start":
                activity = str(payload.get("activity", ""))
                if not activity:
                    continue
                state.activities[actor] = activity
                state.activity_started_at[actor] = state.scene_time
                if activity == "sleeping":
                    state.spatial.postures[actor] = "lying"
                elif activity == "awake":
                    state.spatial.postures[actor] = "sitting"
            elif kind == "activity_end":
                if state.activities.get(actor) == payload.get("activity"):
                    started = state.activity_started_at.pop(actor, state.scene_time)
                    payload["duration_seconds"] = max(
                        0, (state.scene_time - aware(started)).total_seconds()
                    )
                    state.activities.pop(actor, None)
            elif kind == "location_change":
                loc = payload.get("location")
                if not loc:
                    continue
                state.locations[actor] = str(loc)
                state.spatial.locations[actor] = str(loc)
            elif kind == "presence_change":
                pres = payload.get("presence")
                if not pres:
                    continue
                state.presence[actor] = str(pres)
            elif kind not in {"physical_action", "scene_fact"}:
                continue

            event = SceneEvent(
                id=f"scene:{room_id}:{turn_id}:s{index}",
                room_id=room_id,
                source_turn_id=turn_id,
                actor=actor,
                type=kind,
                payload=payload,
                scene_time=state.scene_time,
                created_at=wall,
            )
            scene_events.append(event)

        # 2. Process action events (spatial continuity, self vs interactive)
        for index, act in enumerate(normalized.actions):
            action_type = act.get("action_type") or act.get("action") or "physical_action"
            target_id = act.get("target_id")
            category = act.get("category") or ("interactive" if target_id else "self")
            salience = act.get("salience") or "low"
            params = dict(act.get("parameters") or {})
            state_changes: dict[str, Any] = {}

            # Self-action deterministic resolution
            if category == "self":
                status = "completed"
                if action_type in {"stand_up", "standing"}:
                    state.spatial.postures[actor] = "standing"
                    state_changes["posture"] = "standing"
                elif action_type in {"sit_down", "sitting"}:
                    state.spatial.postures[actor] = "sitting"
                    state_changes["posture"] = "sitting"
                elif action_type in {"lie_down", "lying"}:
                    state.spatial.postures[actor] = "lying"
                    state_changes["posture"] = "lying"
                elif action_type in {"walk", "step"}:
                    state.spatial.postures[actor] = "walking"
                    state_changes["posture"] = "walking"
            else:
                # Interactive action
                if actor == "user":
                    # Interactions initiated by user start as attempted unless self-approach
                    if action_type in {"approach", "sit_beside"}:
                        status = "completed"
                        if target_id:
                            prox = params.get(
                                "proximity", "near" if action_type == "approach" else "adjacent"
                            )
                            state.spatial.set_proximity(actor, target_id, prox)
                            state_changes["proximity"] = prox
                            if action_type == "sit_beside":
                                state.spatial.postures[actor] = "sitting"
                                state_changes["posture"] = "sitting"
                    else:
                        status = "attempted"
                        state_changes["attempted"] = True
                else:
                    # Persona responding or initiating
                    status = "completed"
                    if target_id and action_type in {"approach", "step_close"}:
                        prox = params.get("proximity", "near")
                        state.spatial.set_proximity(actor, target_id, prox)
                        state_changes["proximity"] = prox
                    elif target_id and action_type in {"hug", "hold_hands", "kiss"}:
                        contact = params.get("contact", action_type)
                        state.spatial.add_contact(actor, target_id, contact)
                        state.spatial.set_proximity(actor, target_id, "adjacent")
                        state_changes["contact"] = contact
                        state_changes["proximity"] = "adjacent"
                    elif target_id and action_type in {"step_back", "avoid", "decline"}:
                        state.spatial.set_proximity(actor, target_id, "medium")
                        state.spatial.remove_contact(actor, target_id)
                        state_changes["proximity"] = "medium"
                        state_changes["contact"] = "none"

            ae = ActionEvent(
                event_id=f"act:{room_id}:{turn_id}:{index}",
                room_id=room_id,
                scene_id=state.scene_id,
                actor_id=actor,
                action_type=action_type,
                target_id=target_id,
                parameters=params,
                status=cast(Literal["attempted", "completed", "interrupted", "declined"], status),
                salience=cast(Literal["low", "medium", "high"], salience),
                category=cast(Literal["self", "interactive"], category),
                scene_time=state.scene_time,
                source_turn_id=turn_id,
                state_changes=state_changes,
                created_at=wall,
            )
            action_events.append(ae)

            # Link into SceneEvent stream as well
            se = SceneEvent(
                id=f"scene:{room_id}:{turn_id}:a{index}",
                room_id=room_id,
                source_turn_id=turn_id,
                actor=actor,
                type="action_event",
                payload={"action": ae.action_type, "target": ae.target_id}
                if ae.action_type in {"hug", "wave", "nod", "smile"}
                else ae.model_dump(mode="json"),
                scene_time=state.scene_time,
                created_at=wall,
                action_event=ae,
            )
            scene_events.append(se)

        normalized.scene_events = [e.model_dump(mode="json") for e in scene_events]
        normalized.action_events = [a.model_dump(mode="json") for a in action_events]
        state.recent_events = (state.recent_events + scene_events)[-30:]
        state.recent_action_events = (state.recent_action_events + action_events)[-30:]

        if actor == "user":
            state.elapsed_since_last_interaction = max(
                0.0,
                (
                    state.scene_time - (state.last_interaction_scene_time or state.scene_time)
                ).total_seconds(),
            )
        state.last_interaction_scene_time = state.scene_time
        state.last_interaction_wall_time = wall
        return AcceptTurnResult(normalized, scene_events, action_events)

    @staticmethod
    def prompt_physical_scene(state: RoomSceneState) -> str:
        spatial = state.spatial
        lines = [
            "## Current Physical Scene",
            "Host-owned factual state. Do not narrate these facts unless naturally relevant.",
            "Act and speak from within this state.",
        ]
        if spatial.locations:
            lines.append(
                "- Locations: " + ", ".join(f"{k}: {v}" for k, v in spatial.locations.items())
            )
        if spatial.postures:
            lines.append(
                "- Postures: " + ", ".join(f"{k}: {v}" for k, v in spatial.postures.items())
            )
        if spatial.proximities:
            lines.append(
                "- Distances: " + ", ".join(f"{k}: {v}" for k, v in spatial.proximities.items())
            )
        if spatial.physical_contacts:
            lines.append(
                "- Physical contact: "
                + ", ".join(f"{k}: {', '.join(v)}" for k, v in spatial.physical_contacts.items())
            )
        else:
            lines.append("- Physical contact: none")
        return "\n".join(lines)

    @staticmethod
    def prompt_state(state: RoomSceneState) -> str:
        activities: dict[str, Any] = {}
        for actor, activity in state.activities.items():
            start = state.activity_started_at.get(actor, state.scene_time)
            activities[actor] = {
                "activity": activity,
                "duration_seconds": max(0, (state.scene_time - aware(start)).total_seconds()),
            }
        data = {
            "current_scene_time": state.scene_time.astimezone(ZoneInfo(state.timezone)).isoformat(),
            "timezone": state.timezone,
            "time_mode": state.time_mode,
            "elapsed_since_last_interaction_seconds": state.elapsed_since_last_interaction,
            "locations": state.locations,
            "current_activities": activities,
            "presence": state.presence,
            "spatial": state.spatial.model_dump(mode="json"),
            "recent_actions": [
                {
                    "actor": a.actor_id,
                    "action": a.action_type,
                    "target": a.target_id,
                    "status": a.status,
                    "salience": a.salience,
                }
                for a in state.recent_action_events[-10:]
            ],
        }
        return (
            "## Current Scene State\nThis is host-owned factual state. "
            "Do not repeat it as narration. Speak from the current moment.\n"
            + json.dumps(data, ensure_ascii=False)
            + "\n\n"
            + SceneRuntime.prompt_physical_scene(state)
        )
