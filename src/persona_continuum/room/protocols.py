from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from persona_continuum.room.models import (
    ParticipantSlot,
    RoomProtocolConfig,
    RoomProtocolType,
)


class ProtocolStageDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    allowed_roles: tuple[str, ...] = ()
    action: str = "participant_action"
    parallel: bool = False
    optional: bool = False


class ProtocolDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: RoomProtocolType
    name: str
    description: str
    stages: tuple[ProtocolStageDefinition, ...]
    required_role_groups: tuple[tuple[str, ...], ...] = ()

    @property
    def initial_stage(self) -> str:
        return self.stages[0].id

    def stage(self, stage_id: str) -> ProtocolStageDefinition:
        for stage in self.stages:
            if stage.id == stage_id:
                return stage
        raise KeyError(stage_id)

    def next_stage(self, stage_id: str, config: RoomProtocolConfig) -> str | None:
        ids = [stage.id for stage in self.stages]
        try:
            index = ids.index(stage_id)
        except ValueError as exc:
            raise ValueError(f"unknown_protocol_stage:{stage_id}") from exc
        for candidate in self.stages[index + 1 :]:
            if candidate.id == "cross_review" and not config.cross_review:
                continue
            if candidate.id == "optional_rebuttal" and not config.rebuttal:
                continue
            if candidate.id == "vote" and not config.voting_enabled:
                continue
            if candidate.id == "cross_examination" and not config.cross_examination:
                continue
            return candidate.id
        return None


def _stage(
    stage_id: str,
    label: str,
    roles: tuple[str, ...] = (),
    *,
    action: str = "participant_action",
    parallel: bool = False,
    optional: bool = False,
) -> ProtocolStageDefinition:
    return ProtocolStageDefinition(
        id=stage_id,
        label=label,
        allowed_roles=roles,
        action=action,
        parallel=parallel,
        optional=optional,
    )


BUILTIN_PROTOCOLS: dict[RoomProtocolType, ProtocolDefinition] = {
    RoomProtocolType.FREE_DISCUSSION: ProtocolDefinition(
        id=RoomProtocolType.FREE_DISCUSSION,
        name="自由讨论",
        description="成员按轮询、智能或手动选择自由发言。",
        stages=(
            _stage("waiting_user", "等待用户", action="wait"),
            _stage("discussion", "自由讨论", ("member", "host", "expert", "critic")),
            _stage("final", "完成", action="finalize", optional=True),
        ),
    ),
    RoomProtocolType.HOST_MODERATED: ProtocolDefinition(
        id=RoomProtocolType.HOST_MODERATED,
        name="主持人模式",
        description="主持人点名、推进并生成最终答复。",
        required_role_groups=(("host",),),
        stages=(
            _stage("user_input", "等待用户", action="wait"),
            _stage("host_analysis", "主持人分析", ("host",), action="host_analysis"),
            _stage("host_selects_speaker", "主持人选择发言人", ("host",), action="routing"),
            _stage("member_response", "成员回应", ("member", "expert", "critic")),
            _stage("host_decides_next", "主持人决定下一步", ("host",), action="routing"),
            _stage("host_final", "主持人总结", ("host",), action="synthesis"),
        ),
    ),
    RoomProtocolType.EXPERT_CONSULTATION: ProtocolDefinition(
        id=RoomProtocolType.EXPERT_CONSULTATION,
        name="专家会诊",
        description="主持人路由相关专家，独立分析后交叉评审并综合。",
        required_role_groups=(("host",), ("expert",)),
        stages=(
            _stage("waiting_user", "等待用户", action="wait"),
            _stage("host_analysis", "主持人分析", ("host",), action="host_analysis"),
            _stage("expert_routing", "专家路由", ("host",), action="routing"),
            _stage("expert_task_creation", "创建专家任务", action="create_tasks"),
            _stage(
                "independent_analysis", "独立分析", ("expert",), action="analysis", parallel=True
            ),
            _stage("result_collection", "结果收集", action="barrier"),
            _stage(
                "cross_review",
                "交叉评审",
                ("expert",),
                action="review",
                parallel=True,
                optional=True,
            ),
            _stage(
                "optional_rebuttal",
                "补充反驳",
                ("expert",),
                action="rebuttal",
                parallel=True,
                optional=True,
            ),
            _stage("host_synthesis", "主持人综合", ("host",), action="synthesis"),
            _stage("final_response", "最终答复", action="finalize"),
        ),
    ),
    RoomProtocolType.DEBATE: ProtocolDefinition(
        id=RoomProtocolType.DEBATE,
        name="辩论",
        description="正反双方按确定阶段陈述、质询、反驳，由裁判评审。",
        required_role_groups=(("pro",), ("con",), ("judge", "host")),
        stages=(
            _stage("question", "辩题", action="wait"),
            _stage("opening", "开场", ("host",), action="host_analysis", optional=True),
            _stage("pro_argument", "正方陈述", ("pro",), parallel=True),
            _stage("con_argument", "反方陈述", ("con",), parallel=True),
            _stage("cross_examination", "交叉质询", ("pro", "con"), parallel=True, optional=True),
            _stage("rebuttal", "反驳", ("pro", "con"), parallel=True),
            _stage("final_argument", "总结陈词", ("pro", "con"), parallel=True),
            _stage("judge_review", "裁判评审", ("judge", "host"), action="review"),
            _stage("final", "最终结论", ("judge", "host"), action="finalize"),
        ),
    ),
    RoomProtocolType.COMMITTEE: ProtocolDefinition(
        id=RoomProtocolType.COMMITTEE,
        name="委员会",
        description="委员独立发表意见、评审并投票，由主席形成决策。",
        required_role_groups=(("chair",), ("member", "expert")),
        stages=(
            _stage("question", "议题", action="wait"),
            _stage(
                "independent_opinions",
                "独立意见",
                ("member", "expert"),
                action="analysis",
                parallel=True,
            ),
            _stage(
                "cross_review",
                "交叉评审",
                ("member", "expert", "critic"),
                action="review",
                parallel=True,
            ),
            _stage(
                "optional_discussion", "委员会讨论", ("member", "expert", "critic"), optional=True
            ),
            _stage(
                "vote",
                "投票",
                ("member", "expert", "chair"),
                action="vote",
                parallel=True,
                optional=True,
            ),
            _stage("chair_synthesis", "主席综合", ("chair",), action="synthesis"),
            _stage("final_decision", "最终决策", ("chair",), action="finalize"),
        ),
    ),
}


class ProtocolRegistry:
    def list_definitions(self) -> list[ProtocolDefinition]:
        return list(BUILTIN_PROTOCOLS.values())

    def get(
        self,
        protocol: RoomProtocolType | str,
        config: RoomProtocolConfig | None = None,
    ) -> ProtocolDefinition:
        protocol_id = RoomProtocolType(protocol)
        if protocol_id != RoomProtocolType.CUSTOM:
            return BUILTIN_PROTOCOLS[protocol_id]
        policy = config or RoomProtocolConfig()
        if not policy.stages:
            raise ValueError("custom_protocol_requires_stages")
        stages = tuple(self._custom_stage(item) for item in policy.stages)
        return ProtocolDefinition(
            id=RoomProtocolType.CUSTOM,
            name="自定义协议",
            description="由声明式阶段和转换配置定义的协作协议。",
            stages=stages,
            required_role_groups=tuple((role,) for role in policy.allowed_roles),
        )

    def validate_participants(
        self,
        definition: ProtocolDefinition,
        participants: list[ParticipantSlot],
    ) -> None:
        enabled_roles = {participant.role for participant in participants if participant.enabled}
        for alternatives in definition.required_role_groups:
            if not enabled_roles.intersection(alternatives):
                raise ValueError(
                    f"protocol_role_required:{definition.id.value}:{'|'.join(alternatives)}"
                )
        ids = [participant.participant_id for participant in participants]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate_participant_id")

    @staticmethod
    def public_definition(definition: ProtocolDefinition) -> dict[str, Any]:
        return definition.model_dump(mode="json")

    @staticmethod
    def _custom_stage(item: dict[str, Any]) -> ProtocolStageDefinition:
        stage_id = str(item.get("id") or "").strip()
        if not stage_id:
            raise ValueError("custom_protocol_stage_id_required")
        return ProtocolStageDefinition(
            id=stage_id,
            label=str(item.get("label") or stage_id),
            allowed_roles=tuple(str(role) for role in item.get("allowed_roles") or ()),
            action=str(item.get("action") or "participant_action"),
            parallel=bool(item.get("parallel")),
            optional=bool(item.get("optional")),
        )


def default_protocol_registry() -> ProtocolRegistry:
    return ProtocolRegistry()
