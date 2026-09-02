from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from persona_continuum.application._utils import new_id
from persona_continuum.numeric import safe_acp_stream_limit, safe_probability, safe_timeout


class ActorType(StrEnum):
    PERSONA_ACTOR = "persona_actor"
    ORGANIZATION_ACTOR = "organization_actor"
    INSTITUTION_ACTOR = "institution_actor"
    COLLECTIVE_ACTOR = "collective_actor"
    ENVIRONMENT_ACTOR = "environment_actor"
    # Legacy values remain readable so existing worlds can be resumed.
    PERSISTENT_PERSONA = "persistent_persona"
    GENERATED_ACTOR = "generated_actor"
    ORGANIZATION = "organization"
    ENVIRONMENT = "environment"


class ActionType(StrEnum):
    OBSERVE = "observe"
    DECIDE = "decide"
    COMMUNICATE = "communicate"
    RESEARCH = "research"
    HIRE = "hire"
    INVEST = "invest"
    LAUNCH_PROJECT = "launch_project"
    NEGOTIATE = "negotiate"
    WAIT = "wait"
    REFLECT = "reflect"


class SimulationSpeed(StrEnum):
    YEAR = "year"
    QUARTER = "quarter"
    MONTH = "month"
    DAY = "day"
    HOUR = "hour"


class CausalNodeType(StrEnum):
    EVENT = "event"
    ACTOR = "actor"
    DECISION = "decision"
    TECHNOLOGY = "technology"
    ORGANIZATION = "organization"
    MARKET = "market"


class CausalRelationType(StrEnum):
    CAUSES = "causes"
    INFLUENCES = "influences"
    DEPENDS_ON = "depends_on"
    CONFLICTS_WITH = "conflicts_with"


class MemoryCategory(StrEnum):
    IDENTITY = "identity"
    WORLD_EXPERIENCE = "world_experience"
    BELIEF_CHANGE = "belief_change"
    RELATIONSHIP_CHANGE = "relationship_change"


class ResolutionStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL_SUCCESS = "partial_success"


class DivergenceItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str = "historical_change"
    condition: str
    consequence: str


class WorldSeed(BaseModel):
    model_config = ConfigDict(extra="ignore")

    baseline_world: str = "real_world"
    start_date: str  # YYYY-MM-DD or YYYY
    divergence: list[DivergenceItem] = Field(default_factory=list)
    immutable_facts: list[str] = Field(default_factory=list)
    simulation_end: str = "2030"
    rules: list[str] = Field(default_factory=list)
    location: str | None = None
    initial_actors: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Backward-compatible optional initial conditions. When omitted the world
    # starts empty: no organizations, technologies, economy, or projects are
    # auto-generated. Older seeds without these fields remain readable.
    initial_state: dict[str, Any] | None = None
    initial_entities: dict[str, dict[str, Any]] | None = None
    initial_organizations: dict[str, dict[str, Any]] | None = None
    initial_relationships: dict[str, dict[str, Any]] | None = None
    initial_resources: dict[str, Any] | None = None
    initial_projects: dict[str, dict[str, Any]] | None = None
    initial_technologies: dict[str, dict[str, Any]] | None = None
    initial_environment: dict[str, Any] | list[Any] | None = None


class WorldRuntimeBinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    world_id: str
    actor_id: str
    persona_id: str | None = None
    profile_id: str | None = None
    profile_type: str | None = None
    runtime_source: str = "local_cli"  # "local_cli" | "api"
    agent_id: str = "default"
    model_id: str = "default"
    reasoning_effort: str = "none"
    auth_profile_id: str | None = None
    resolved_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    capability_snapshot: dict[str, Any] = Field(default_factory=dict)


class ActorRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    persona_id: str = ""
    profile_id: str | None = None
    profile_type: str | None = None
    agent_id: str
    model_id: str = "default"
    runtime_source: str = "local_cli"  # "local_cli" | "api"
    reasoning_effort: str = "none"
    credential_provider: str = "unconfigured"
    credential_id: str | None = None
    auth_profile_id: str | None = None
    inherit_default_runtime: bool = True
    # Deprecated compatibility override.  When omitted, AgentTimeoutPolicy
    # selects phase-aware idle and hard limits.
    turn_timeout_seconds: float | None = None
    idle_timeout_seconds: float | None = None
    hard_timeout_seconds: float | None = None
    acp_stream_limit_bytes: int = 16 * 1024 * 1024

    @model_validator(mode="before")
    @classmethod
    def _normalise_runtime_limits(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        for key in ("turn_timeout_seconds", "idle_timeout_seconds", "hard_timeout_seconds"):
            if data.get(key) is not None:
                data[key] = safe_timeout(data[key])
        data["acp_stream_limit_bytes"] = safe_acp_stream_limit(
            data.get("acp_stream_limit_bytes")
        )
        return data


class Actor(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    actor_type: ActorType = ActorType.PERSISTENT_PERSONA
    persona_id: str | None = None
    profile_id: str | None = None
    profile_type: str | None = None
    organization_id: str | None = None
    agent_runtime_id: str | None = None
    model_id: str | None = None
    runtime_config: ActorRuntimeConfig | None = None
    identity: dict[str, Any] = Field(default_factory=dict)
    goals: list[str] = Field(default_factory=list)
    beliefs: dict[str, Any] = Field(default_factory=dict)
    relationships: dict[str, Any] = Field(default_factory=dict)
    resources: dict[str, Any] = Field(default_factory=dict)
    memory: list[dict[str, Any]] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        if self.runtime_config is None:
            self.runtime_config = ActorRuntimeConfig(
                persona_id=self.persona_id or self.id,
                agent_id=self.agent_runtime_id or "unconfigured",
                model_id=self.model_id or "auto",
                credential_provider="unconfigured",
            )
        self.persona_id = self.runtime_config.persona_id
        self.profile_id = self.runtime_config.profile_id or self.profile_id
        self.profile_type = self.runtime_config.profile_type or self.profile_type
        self.agent_runtime_id = self.runtime_config.agent_id
        self.model_id = self.runtime_config.model_id


class ActorAction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("act"))
    actor_id: str
    action_type: ActionType
    target: str | None = None
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    timestamp: str | None = None


class ActionProposal(BaseModel):
    """An actor's LLM-generated intent. It has no authority to mutate world state."""

    model_config = ConfigDict(extra="ignore")

    actor: str
    intent: str
    action_type: ActionType
    target: str | None = None
    reasoning_summary: str
    expected_effect: str
    confidence: float = Field(ge=0.0, le=1.0)
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalise_confidence(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        normalized = safe_probability(data.get("confidence"), default=0.5)
        data["confidence"] = normalized if normalized is not None else 0.5
        return data

    def to_action(self, timestamp: str | None = None) -> ActorAction:
        return ActorAction(
            actor_id=self.actor,
            action_type=self.action_type,
            target=self.target,
            description=self.intent,
            parameters=self.parameters,
            timestamp=timestamp,
        )


class ActionResolution(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action_id: str
    success: bool
    status: ResolutionStatus | None = None
    success_probability: float
    cost: dict[str, Any] = Field(default_factory=dict)
    outcome_description: str
    state_mutations: dict[str, Any] = Field(default_factory=dict)
    event_id: str | None = None

    def model_post_init(self, __context: Any) -> None:
        if self.status is None:
            self.status = ResolutionStatus.SUCCESS if self.success else ResolutionStatus.FAILURE


class TimelineEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("evt"))
    event_time: str
    actors: list[str] = Field(default_factory=list)
    cause: str
    effect: str
    causal_chain: list[str] = Field(default_factory=list)
    confidence: float = 1.0
    importance: float = 0.5
    data: dict[str, Any] = Field(default_factory=dict)


class WorldDirectorPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_frequency: float = 1.0
    simulation_speed: SimulationSpeed = SimulationSpeed.MONTH
    importance_threshold: float = 0.4
    randomness: float = 0.1
    intervention_level: float = 0.5


class DirectorDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    speed: SimulationSpeed
    trigger_milestones: list[str] = Field(default_factory=list)
    reason: str
    generated_events: list[TimelineEvent] = Field(default_factory=list)


class CausalNode(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    node_type: CausalNodeType
    name: str
    timestamp: str
    properties: dict[str, Any] = Field(default_factory=dict)


class CausalEdge(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("edge"))
    source_id: str
    target_id: str
    relation_type: CausalRelationType
    weight: float = 1.0
    properties: dict[str, Any] = Field(default_factory=dict)


class CausalPathStep(BaseModel):
    model_config = ConfigDict(extra="ignore")

    node: CausalNode
    relation_to_next: str | None = None


class CausalGraph(BaseModel):
    model_config = ConfigDict(extra="ignore")

    nodes: dict[str, CausalNode] = Field(default_factory=dict)
    edges: list[CausalEdge] = Field(default_factory=list)


class Department(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    mission: str = ""
    headcount: int = 100
    budget_billions: float = 0.5
    current_focus: str = ""
    lead_persona_id: str | None = None


class OrgStrategy(BaseModel):
    model_config = ConfigDict(extra="ignore")

    primary_goal: str = ""
    priority_domains: list[str] = Field(default_factory=list)
    risk_appetite: str = "moderate"
    target_competitors: list[str] = Field(default_factory=list)


class OrganizationEntity(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    leadership: list[str] = Field(default_factory=list)
    departments: list[Department] = Field(default_factory=list)
    employees_count: int = 1000
    budget_billions: float = 5.0
    cash_reserves_billions: float = 10.0
    technology: list[str] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)
    strategy: OrgStrategy = Field(default_factory=OrgStrategy)
    culture: dict[str, Any] = Field(default_factory=dict)


class TechnologyRequirement(BaseModel):
    model_config = ConfigDict(extra="ignore")

    technology_id: str
    minimum_maturity: float = 0.5
    description: str = ""


class TechnologyEntity(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    maturity: float = 0.1  # 0.0 to 1.0
    cost: float = 1.0  # relative index
    performance: float = 1.0  # relative index
    adoption: float = 0.0  # 0.0 to 1.0
    dependencies: list[TechnologyRequirement] = Field(default_factory=list)
    lead_org_id: str | None = None


class EconomicState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    gdp_index: float = 100.0
    market_demand: float = 1.0
    enterprise_compute_tam_billions: float = 20.0
    mobile_silicon_tam_billions: float = 30.0
    inflation_rate: float = 0.02


class WorldMemoryRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("wmem"))
    persona_id: str
    memory_type: MemoryCategory = MemoryCategory.WORLD_EXPERIENCE
    content: str
    occurred_at: str
    written_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    importance: float = 0.5
    emotional_valence: float = 0.0  # -1.0 to 1.0
    source_event_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActorObservation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    timestamp: str
    visible_events: list[TimelineEvent] = Field(default_factory=list)
    org_status: dict[str, Any] = Field(default_factory=dict)
    market_signals: dict[str, Any] = Field(default_factory=dict)


class ActorDecisionInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    actor_id: str
    world_state: WorldState
    identity_memories: list[WorldMemoryRecord] = Field(default_factory=list)
    world_memories: list[WorldMemoryRecord] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)
    beliefs: dict[str, Any] = Field(default_factory=dict)
    resources: dict[str, Any] = Field(default_factory=dict)
    observation: ActorObservation


class ActorDecisionOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    actor_id: str
    reasoning_trace: str = ""
    plan_summary: str = ""
    expected_impact: str = ""
    action: ActorAction


class WorldState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    timestamp: str
    entities: dict[str, dict[str, Any]] = Field(default_factory=dict)
    organizations: dict[str, dict[str, Any]] = Field(default_factory=dict)
    technologies: dict[str, dict[str, Any]] = Field(default_factory=dict)
    economy: dict[str, Any] = Field(default_factory=dict)
    relationships: dict[str, dict[str, Any]] = Field(default_factory=dict)
    resources: dict[str, Any] = Field(default_factory=dict)
    active_projects: dict[str, dict[str, Any]] = Field(default_factory=dict)
    events: list[str] = Field(default_factory=list)


class WorldSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("snap"))
    world_id: str
    branch_id: str
    timestamp: str
    state: WorldState
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SimulationBranch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("branch"))
    world_id: str
    name: str
    parent_branch_id: str | None = None
    parent_snapshot_id: str | None = None
    status: str = "running"
    current_state: WorldState | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WorldRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("world"))
    title: str
    description: str
    seed: WorldSeed
    status: str = "active"
    builder_runtime: dict[str, Any] = Field(default_factory=dict)
    default_actor_runtime: dict[str, Any] = Field(default_factory=dict)
    director_runtime: dict[str, Any] = Field(default_factory=dict)
    evaluator_runtime: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class OutcomeEvaluation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: new_id("eval"))
    world_id: str
    question: str
    metrics: dict[str, float] = Field(default_factory=dict)
    branch_results: list[dict[str, Any]] = Field(default_factory=list)
    distribution: dict[str, int] = Field(default_factory=dict)
    causal_summary: str = ""
    total_branches: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ReplayStep(BaseModel):
    model_config = ConfigDict(extra="ignore")

    step_index: int
    timestamp: str
    state_snapshot: WorldState
    active_events: list[TimelineEvent] = Field(default_factory=list)
    causal_milestones: list[str] = Field(default_factory=list)
    diff_summary: str = ""


class ReplayTrajectory(BaseModel):
    model_config = ConfigDict(extra="ignore")

    world_id: str
    branch_id: str
    steps: list[ReplayStep] = Field(default_factory=list)
    outcome_summary: str = ""


class WorldSimulationReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    world_id: str
    title: str
    summary: str
    initial_divergence: list[dict[str, Any]] = Field(default_factory=list)
    timeline_events: list[TimelineEvent] = Field(default_factory=list)
    major_events: list[TimelineEvent] = Field(default_factory=list)
    causal_chains: list[dict[str, Any]] = Field(default_factory=list)
    actor_evolution: list[dict[str, Any]] = Field(default_factory=list)
    organization_evolution: list[dict[str, Any]] = Field(default_factory=list)
    branch_comparisons: list[dict[str, Any]] = Field(default_factory=list)
    outcome_evaluation: dict[str, Any] = Field(default_factory=dict)
    uncertainty_analysis: str = ""
    markdown_report: str = ""
