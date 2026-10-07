"""Host-owned scene facts, spatial state, and structured action events."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

SceneEventType = Literal[
    "time_advance",
    "activity_start",
    "activity_end",
    "location_change",
    "presence_change",
    "physical_action",
    "scene_fact",
    "action_event",
]

ActionStatus = Literal["attempted", "completed", "interrupted", "declined"]
ActionSalience = Literal["low", "medium", "high"]
ActionCategory = Literal["self", "interactive"]

# Known postures and proximity levels
Posture = Literal["standing", "sitting", "lying", "walking", "sleeping", "unknown"]
Proximity = Literal["far", "medium", "near", "very_close", "adjacent"]


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class ActionEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: f"act_evt_{uuid4().hex}")
    room_id: str
    scene_id: str = ""
    actor_id: str
    action_type: str
    target_id: str | None = None
    object_id: str | None = None
    manner: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    status: ActionStatus = "completed"
    salience: ActionSalience = "low"
    category: ActionCategory = "self"
    scene_time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    duration: float | None = None
    source_turn_id: str = ""
    visibility: str = "public"
    state_changes: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    _aware = field_validator("scene_time", "created_at")(aware)


class SceneEvent(BaseModel):
    id: str = Field(default_factory=lambda: f"scene_evt_{uuid4().hex}")
    room_id: str
    scene_time: datetime
    actor: str
    type: SceneEventType
    payload: dict[str, Any] = Field(default_factory=dict)
    source_turn_id: str
    confidence: float = Field(default=1.0, ge=0, le=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    action_event: ActionEvent | None = None

    _aware = field_validator("scene_time", "created_at")(aware)


class SpatialSceneState(BaseModel):
    """Sparse spatial and physical state of actors in a scene."""

    locations: dict[str, str] = Field(default_factory=dict)  # actor_id -> location
    zones: dict[str, str] = Field(default_factory=dict)  # actor_id -> zone/sub-location
    postures: dict[str, str] = Field(default_factory=dict)  # actor_id -> posture
    orientations: dict[str, str] = Field(default_factory=dict)  # actor_id -> orientation/facing
    proximities: dict[str, str] = Field(default_factory=dict)  # sorted_pair_key -> proximity
    physical_contacts: dict[str, list[str]] = Field(
        default_factory=dict
    )  # sorted_pair_key -> [contact_types]
    held_objects: dict[str, list[str]] = Field(default_factory=dict)  # actor_id -> [object_names]

    @staticmethod
    def pair_key(actor1: str, actor2: str) -> str:
        a, b = sorted([actor1.strip(), actor2.strip()])
        return f"{a}:{b}"

    def get_proximity(self, actor1: str, actor2: str, default: str = "medium") -> str:
        return self.proximities.get(self.pair_key(actor1, actor2), default)

    def set_proximity(self, actor1: str, actor2: str, proximity: str) -> None:
        self.proximities[self.pair_key(actor1, actor2)] = proximity

    def get_contacts(self, actor1: str, actor2: str) -> list[str]:
        return list(self.physical_contacts.get(self.pair_key(actor1, actor2), []))

    def add_contact(self, actor1: str, actor2: str, contact: str) -> None:
        key = self.pair_key(actor1, actor2)
        contacts = set(self.physical_contacts.get(key, []))
        contacts.add(contact)
        self.physical_contacts[key] = sorted(contacts)

    def remove_contact(self, actor1: str, actor2: str, contact: str | None = None) -> None:
        key = self.pair_key(actor1, actor2)
        if contact is None:
            self.physical_contacts.pop(key, None)
        elif key in self.physical_contacts:
            self.physical_contacts[key] = [c for c in self.physical_contacts[key] if c != contact]
            if not self.physical_contacts[key]:
                self.physical_contacts.pop(key, None)


class RoomSceneState(BaseModel):
    scene_time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    timezone: str = "Asia/Shanghai"
    time_mode: Literal["realtime", "narrative", "hybrid"] = "hybrid"
    scene_id: str = Field(default_factory=lambda: f"scene_{uuid4().hex}")
    locations: dict[str, str] = Field(default_factory=dict)
    activities: dict[str, str] = Field(default_factory=dict)
    activity_started_at: dict[str, datetime] = Field(default_factory=dict)
    presence: dict[str, str] = Field(default_factory=dict)
    spatial: SpatialSceneState = Field(default_factory=SpatialSceneState)
    last_interaction_scene_time: datetime | None = None
    last_interaction_wall_time: datetime | None = None
    clock_wall_time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    elapsed_since_last_interaction: float = 0
    recent_events: list[SceneEvent] = Field(default_factory=list)
    recent_action_events: list[ActionEvent] = Field(default_factory=list)
    pending_action_attempts: list[ActionEvent] = Field(default_factory=list)

    _aware = field_validator("scene_time", "clock_wall_time")(aware)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value


class TurnChannels(BaseModel):
    raw_content: str
    spoken_text: str = ""
    actions: list[dict[str, Any]] = Field(default_factory=list)
    scene_events: list[dict[str, Any]] = Field(default_factory=list)
    action_events: list[dict[str, Any]] = Field(default_factory=list)
    non_voice_context: list[str] = Field(default_factory=list)
    input_mode: Literal["speech", "action", "mixed"] = "speech"
    normalization_version: int = 2


class SceneOutput(BaseModel):
    speech: str = Field(
        description=(
            "What this person says, at a natural conversational length. "
            "Not a narrator. Not a one-or-two-sentence quota."
        )
    )
    actions: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Optional own physical actions. Zero is fine; no 1-2 action quota.",
    )
    scene_updates: list[dict[str, Any]] = Field(default_factory=list)
    state_intents: list[dict[str, Any]] = Field(default_factory=list)
