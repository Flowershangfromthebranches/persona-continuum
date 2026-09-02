from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterable
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from persona_continuum.agent.adapter import AgentAdapter
from persona_continuum.agent.models import (
    AgentSessionConfig,
    PermissionProfile,
)
from persona_continuum.agent.runtime_executor import AgentRuntimeExecutor
from persona_continuum.agent.structured_output import StructuredResult
from persona_continuum.application._utils import new_id
from persona_continuum.domain.profile import ActorProfile, ProfileStatus

if TYPE_CHECKING:
    from persona_continuum.application.profile_library_service import ProfileLibraryService


class EntityClassificationError(RuntimeError):
    pass


class WorldEntityCandidate(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    normalized_name: str = ""
    extracted_context: str = ""
    confidence: float = 0.0
    raw: dict[str, Any] = Field(default_factory=dict)


class ClassifiedWorldEntity(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    category: str
    agent_capable: bool
    subtype: str
    rationale: str = ""
    confidence: float = 0.0
    profile_id: str | None = None
    profile_match_status: str = "missing"
    runtime_inheritance: str = "default_actor_runtime"
    initial_world_role: str = ""


class WorldEntityClassificationResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    candidates: list[WorldEntityCandidate] = Field(default_factory=list)
    classified_agents: list[ClassifiedWorldEntity] = Field(default_factory=list)
    non_agent_entities: list[ClassifiedWorldEntity] = Field(default_factory=list)
    ambiguity_list: list[ClassifiedWorldEntity] = Field(default_factory=list)
    missing_profiles: list[ClassifiedWorldEntity] = Field(default_factory=list)
    classification_source: str = "deterministic"
    llm_used: bool = False
    warnings: list[str] = Field(default_factory=list)


CLASSIFICATION_SYSTEM_PROMPT = """You are Persona Continuum's World Entity Classification Agent.
Classify extracted entities, not future outcomes. Decide which entities can act as
decision-making Agents and which are only initial world entities or conditions.
Use the supplied context and return JSON only:
{"entities":[{"id":"...","category":"agent|non_agent","agent_capable":true,
"subtype":"person|organization|institution|collective|event|policy|technology|resource|market_state|product|project|location|metric|process",
"rationale":"...","confidence":0.0}]}
Do not turn a policy, event, technology, resource, product, project, location,
metric, or process into an Agent merely because a person or organization is named.
"""


AGENT_SUBTYPES = {"person", "organization", "institution", "collective"}
NON_AGENT_SUBTYPES = {
    "event",
    "policy",
    "technology",
    "resource",
    "market_state",
    "product",
    "project",
    "location",
    "metric",
    "process",
}


class WorldEntityClassificationService:
    """LLM-assisted entity classification plus deterministic safety validation."""

    def __init__(self, profiles: ProfileLibraryService) -> None:
        self.profiles = profiles
        self.runtime_executor = (
            getattr(profiles, "runtime_executor", None) or AgentRuntimeExecutor()
        )

    async def classify(
        self,
        *,
        description: str,
        entities: Iterable[dict[str, Any] | WorldEntityCandidate],
        adapter: AgentAdapter | None = None,
        runtime: dict[str, Any] | None = None,
        require_llm: bool = True,
    ) -> WorldEntityClassificationResult:
        candidates = [self._candidate(item) for item in entities]
        if not candidates:
            candidates = self._extract_candidates(description)
        if not candidates:
            raise EntityClassificationError("world_entity_candidates_required")

        llm_rows: dict[str, dict[str, Any]] = {}
        warnings: list[str] = []
        llm_used = False
        # Fast path: when the World Builder already emitted an explicit
        # subtype/actor_type AND its own high classification confidence for
        # every candidate, deterministic validation accepts them directly and
        # the second classification LLM call is skipped.  Low-confidence,
        # missing, or conflicting candidates still go to the classifier.
        if all(self._builder_fast_path_confident(candidate) for candidate in candidates):
            warnings.append("classification_fast_path:builder_confidence")
        elif adapter is not None:
            try:
                llm_rows = await self._classify_with_llm(
                    description=description,
                    candidates=candidates,
                    adapter=adapter,
                    runtime=runtime or {},
                )
                llm_used = bool(llm_rows)
            except Exception as exc:
                if require_llm:
                    error = f"entity_classification_agent_failed:{exc}"
                    raise EntityClassificationError(error) from exc
                warnings.append(f"llm_classification_unavailable:{exc}")
        elif require_llm:
            raise EntityClassificationError("entity_classification_agent_required")

        classified = [
            self._merge_classification(candidate, llm_rows.get(candidate.id))
            for candidate in candidates
        ]
        self._validate_deterministically(classified, candidates, warnings)
        self._match_profiles(classified)
        self._apply_explicit_profile_bindings(classified, candidates)
        agents = [item for item in classified if item.agent_capable]
        non_agents = [item for item in classified if not item.agent_capable]
        ambiguity = [
            item
            for item in classified
            if item.confidence < 0.78 or item.profile_match_status == "ambiguous"
        ]
        missing = [
            item
            for item in agents
            if item.profile_match_status in {"missing", "ambiguous"}
        ]
        source = "llm+deterministic" if llm_used else "deterministic"
        return WorldEntityClassificationResult(
            candidates=candidates,
            classified_agents=agents,
            non_agent_entities=non_agents,
            ambiguity_list=ambiguity,
            missing_profiles=missing,
            classification_source=source,
            llm_used=llm_used,
            warnings=warnings,
        )

    def classify_sync(
        self,
        *,
        description: str,
        entities: Iterable[dict[str, Any] | WorldEntityCandidate],
    ) -> WorldEntityClassificationResult:
        """Deterministic preflight for callers that already have an LLM roster.

        The async ``classify`` path should be used when an Agent is available;
        this method exists for stored WorldSeed resumes and explicit user edits.
        """
        candidates = [self._candidate(item) for item in entities]
        if not candidates:
            candidates = self._extract_candidates(description)
        classified = [self._merge_classification(candidate, None) for candidate in candidates]
        warnings: list[str] = []
        self._validate_deterministically(classified, candidates, warnings)
        self._match_profiles(classified)
        self._apply_explicit_profile_bindings(classified, candidates)
        return WorldEntityClassificationResult(
            candidates=candidates,
            classified_agents=[item for item in classified if item.agent_capable],
            non_agent_entities=[item for item in classified if not item.agent_capable],
            ambiguity_list=[
                item
                for item in classified
                if item.confidence < 0.78 or item.profile_match_status == "ambiguous"
            ],
            missing_profiles=[
                item
                for item in classified
                if item.agent_capable and item.profile_match_status in {"missing", "ambiguous"}
            ],
            classification_source="deterministic",
            warnings=warnings,
        )

    async def _classify_with_llm(
        self,
        *,
        description: str,
        candidates: list[WorldEntityCandidate],
        adapter: AgentAdapter,
        runtime: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        session_binding = None
        prompt = {
            "world_description": description,
            "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
        }
        try:
            session_binding = await self.runtime_executor.open_session(
                adapter,
                AgentSessionConfig(
                    session_id="world_entity_classification",
                    room_id="world_entity_classification",
                    participant_id="world_entity_classifier",
                    persona_id="world_entity_classifier",
                    model_id=runtime.get("model_id"),
                    reasoning_effort=runtime.get("reasoning_effort"),
                    auth_profile_id=runtime.get("auth_profile_id"),
                    permission_profile=PermissionProfile.CHAT_SAFE,
                    allow_mcp=False,
                    tools=[],
                    system_prompt=CLASSIFICATION_SYSTEM_PROMPT,
                )
            )
            result = await self.runtime_executor.execute_structured(
                session_binding,
                system_prompt=CLASSIFICATION_SYSTEM_PROMPT,
                user_message=json.dumps(prompt, ensure_ascii=False),
                schema={"type": "object"},
                phase="classification",
                metadata={"world_entity_classification": True},
            )
            if not isinstance(result, StructuredResult):
                raise EntityClassificationError("entity_classification_result_invalid")
            payload = result.value
            rows = payload.get("entities") if isinstance(payload, dict) else payload
            if not isinstance(rows, list):
                raise EntityClassificationError("entity_classification_payload_invalid")
            return {
                str(item.get("id")): dict(item)
                for item in rows
                if isinstance(item, dict) and item.get("id")
            }
        finally:
            if session_binding is not None:
                with contextlib.suppress(Exception):
                    await self.runtime_executor.close(session_binding)

    def _builder_fast_path_confident(self, candidate: WorldEntityCandidate) -> bool:
        """Whether the builder's own classification is explicit and confident.

        Requires both an explicit subtype/actor_type signal (the deterministic
        path scores those >= 0.90) and a builder-declared
        ``classification_confidence`` of at least 0.90.  Candidates without
        the declared confidence stay on the classifier path.
        """
        deterministic = self._deterministic_classification(candidate)
        if float(deterministic["confidence"]) < 0.85:
            return False
        raw_confidence = candidate.raw.get("classification_confidence")
        if raw_confidence is None:
            return False
        try:
            return float(raw_confidence) >= 0.9
        except (TypeError, ValueError):
            return False

    def _merge_classification(
        self,
        candidate: WorldEntityCandidate,
        llm_row: dict[str, Any] | None,
    ) -> ClassifiedWorldEntity:
        deterministic = self._deterministic_classification(candidate)
        row = llm_row or {}
        manual_override = candidate.raw.get("classification_override") or candidate.raw.get(
            "subtype_override"
        )
        if isinstance(manual_override, dict):
            override_subtype = manual_override.get("subtype")
            override_category = manual_override.get("category")
        else:
            override_subtype = manual_override
            override_category = None
        subtype = str(
            override_subtype
            or row.get("subtype")
            or deterministic["subtype"]
        ).lower()
        category = str(
            override_category
            or row.get("category")
            or deterministic["category"]
        ).lower()
        capable_value = row.get("agent_capable")
        if isinstance(manual_override, dict) and "agent_capable" in manual_override:
            capable_value = manual_override.get("agent_capable")
        elif manual_override:
            capable_value = subtype in AGENT_SUBTYPES
        agent_capable = (
            bool(capable_value)
            if capable_value is not None
            else bool(deterministic["agent_capable"])
        )
        if subtype in AGENT_SUBTYPES:
            category = "agent"
            agent_capable = True
        elif subtype in NON_AGENT_SUBTYPES:
            category = "non_agent"
            agent_capable = False
        if category not in {"agent", "non_agent"}:
            category = "agent" if agent_capable else "non_agent"
        confidence = float(row.get("confidence", deterministic["confidence"]) or 0.0)
        confidence = max(0.0, min(1.0, confidence))
        return ClassifiedWorldEntity(
            id=candidate.id,
            name=candidate.name,
            category=category,
            agent_capable=agent_capable,
            subtype=subtype,
            rationale=str(row.get("rationale") or deterministic["rationale"]),
            confidence=confidence,
            initial_world_role="initial_condition" if not agent_capable else "decision_actor",
        )

    def _validate_deterministically(
        self,
        classified: list[ClassifiedWorldEntity],
        candidates: list[WorldEntityCandidate],
        warnings: list[str],
    ) -> None:
        by_id = {candidate.id: candidate for candidate in candidates}
        for item in classified:
            candidate = by_id[item.id]
            deterministic = self._deterministic_classification(candidate)
            # A clear event/policy/resource/technology signal cannot be
            # promoted to an Agent solely by a hallucinated LLM subtype.
            if deterministic["subtype"] in NON_AGENT_SUBTYPES and item.subtype in AGENT_SUBTYPES:
                proposed_subtype = item.subtype
                item.subtype = deterministic["subtype"]
                item.category = "non_agent"
                item.agent_capable = False
                item.confidence = min(item.confidence, 0.64)
                item.rationale = (
                    f"deterministic safety correction: {deterministic['rationale']}; "
                    f"model proposed {proposed_subtype}"
                )
                warnings.append(f"classification_corrected:{item.id}")
            if item.subtype not in AGENT_SUBTYPES | NON_AGENT_SUBTYPES:
                item.confidence = min(item.confidence, 0.55)
                item.category = "agent" if item.agent_capable else "non_agent"
                warnings.append(f"classification_subtype_unknown:{item.id}")

    def _match_profiles(self, entities: list[ClassifiedWorldEntity]) -> None:
        profiles = [
            profile
            for profile in self.profiles.list_profiles()
            if profile.status != ProfileStatus.ARCHIVED
        ]
        expected_types = {
            "person": "persona",
            "organization": "organization",
            "institution": "institution",
            "collective": "collective",
        }
        exact: dict[str, list[ActorProfile]] = {}
        for profile in profiles:
            for value in (profile.id, profile.slug, profile.display_name, *profile.aliases):
                key = self._normalise(str(value))
                if key:
                    exact.setdefault(key, []).append(profile)
        for entity in entities:
            if not entity.agent_capable:
                entity.profile_match_status = "non_agent"
                continue
            expected_type = expected_types.get(entity.subtype)
            candidates = [
                profile
                for profile in exact.get(self._normalise(entity.name), [])
                if expected_type is None or profile.profile_type.value == expected_type
            ]
            if not candidates:
                candidates = [
                    profile
                    for profile in exact.get(self._normalise(entity.id), [])
                    if expected_type is None or profile.profile_type.value == expected_type
                ]
            if len(candidates) == 1:
                entity.profile_id = candidates[0].id
                entity.profile_match_status = "matched"
                entity.confidence = max(entity.confidence, 0.92)
                continue
            if len(candidates) > 1:
                entity.profile_match_status = "ambiguous"
                entity.confidence = min(entity.confidence, 0.70)
                continue
            fuzzy: list[tuple[float, ActorProfile]] = []
            needle = self._normalise(entity.name)
            for profile in profiles:
                if expected_type is not None and profile.profile_type.value != expected_type:
                    continue
                score = SequenceMatcher(None, needle, self._normalise(profile.display_name)).ratio()
                if score >= 0.86:
                    fuzzy.append((score, profile))
            fuzzy.sort(key=lambda item: item[0], reverse=True)
            if fuzzy and len([item for item in fuzzy if item[0] >= fuzzy[0][0] - 0.02]) == 1:
                entity.profile_id = fuzzy[0][1].id
                entity.profile_match_status = "ambiguous"
                entity.confidence = min(entity.confidence, float(fuzzy[0][0]))
            else:
                entity.profile_match_status = "missing"

    def _apply_explicit_profile_bindings(
        self,
        entities: list[ClassifiedWorldEntity],
        candidates: list[WorldEntityCandidate],
    ) -> None:
        """Honor a user-confirmed binding when a World Create retry resumes.

        The binding is still validated by the deterministic classification gate;
        it only prevents a second match pass from forgetting an explicit
        profile selected in the preview table.
        """
        by_id = {candidate.id: candidate for candidate in candidates}
        expected_types = {
            "person": "persona",
            "organization": "organization",
            "institution": "institution",
            "collective": "collective",
        }
        for entity in entities:
            candidate = by_id.get(entity.id)
            raw = candidate.raw if candidate is not None else {}
            explicit = raw.get("profile_id") or raw.get("persona_id")
            if explicit and entity.agent_capable:
                try:
                    profile = self.profiles.get_profile(str(explicit))
                except Exception:
                    continue
                expected_type = expected_types.get(entity.subtype)
                if expected_type and profile.profile_type.value != expected_type:
                    continue
                entity.profile_id = str(explicit)
                entity.profile_match_status = "matched"
                entity.confidence = max(entity.confidence, 0.95)

    @staticmethod
    def _candidate(item: dict[str, Any] | WorldEntityCandidate) -> WorldEntityCandidate:
        if isinstance(item, WorldEntityCandidate):
            candidate = item
        else:
            raw = dict(item) if isinstance(item, dict) else {"id": str(item), "name": str(item)}
            name = str(raw.get("name") or raw.get("display_name") or raw.get("id") or "entity")
            candidate = WorldEntityCandidate(
                id=str(raw.get("id") or raw.get("entity_id") or new_id("entity")),
                name=name,
                normalized_name=WorldEntityClassificationService._normalise(name),
                extracted_context=str(raw.get("extracted_context") or raw.get("context") or ""),
                confidence=float(raw.get("confidence") or 0.0),
                raw=raw,
            )
        if not candidate.normalized_name:
            candidate.normalized_name = WorldEntityClassificationService._normalise(candidate.name)
        return candidate

    @classmethod
    def _extract_candidates(cls, description: str) -> list[WorldEntityCandidate]:
        # This is only a resume/preflight fallback.  LLM World Builder output
        # should provide the authoritative candidate roster.
        parts = [part.strip() for part in re.split(r"[,，、;；\n]", description) if part.strip()]
        return [
            WorldEntityCandidate(
                id=cls._normalise(part) or new_id("entity"),
                name=part,
                normalized_name=cls._normalise(part),
                extracted_context=description,
                confidence=0.45,
            )
            for part in parts[:32]
        ]

    @classmethod
    def _deterministic_classification(cls, candidate: WorldEntityCandidate) -> dict[str, Any]:
        raw = candidate.raw
        explicit_type = str(
            raw.get("subtype")
            or raw.get("entity_type")
            or raw.get("subtype_hint")
            or ""
        ).lower()
        actor_type = str(raw.get("actor_type") or "").lower()
        if explicit_type in AGENT_SUBTYPES:
            return {
                "category": "agent",
                "agent_capable": True,
                "subtype": explicit_type,
                "confidence": 0.95,
                "rationale": "explicit agent subtype from World Builder",
            }
        if explicit_type in NON_AGENT_SUBTYPES:
            return {
                "category": "non_agent",
                "agent_capable": False,
                "subtype": explicit_type,
                "confidence": 0.95,
                "rationale": "explicit non-agent subtype from World Builder",
            }
        actor_map = {
            "persona_actor": "person",
            "persistent_persona": "person",
            "generated_actor": "person",
            "organization_actor": "organization",
            "organization": "organization",
            "institution_actor": "institution",
            "collective_actor": "collective",
        }
        if actor_type in {"environment_actor", "environment"}:
            return {
                "category": "non_agent",
                "agent_capable": False,
                "subtype": "market_state",
                "confidence": 0.92,
                "rationale": "environment actor is a world condition, not a decision Agent",
            }
        if actor_type in actor_map:
            subtype = actor_map[actor_type]
            return {
                "category": "agent",
                "agent_capable": True,
                "subtype": subtype,
                "confidence": 0.90,
                "rationale": "explicit actor type from World Builder",
            }
        text = f"{candidate.name} {candidate.extracted_context}".casefold()
        if any(
            token in text
            for token in ("政策", "policy", "法规", "regulation", "制裁", "补贴")
        ):
            subtype = "policy"
        elif any(
            token in text
            for token in ("事件", "危机", "下降", "增长", "发布", "事故", "event", "crisis")
        ):
            subtype = "event"
        elif any(
            token in text
            for token in ("产能", "资源", "capital", "resource", "人才", "预算")
        ):
            subtype = "resource"
        elif any(token in text for token in ("技术", "technology", "芯片", "制程", "模型", "cuda")):
            subtype = "technology"
        elif any(
            token in text
            for token in ("政府", "欧盟", "美联储", "工信部", "监管", "government", "federal")
        ):
            subtype = "institution"
        elif any(
            token in text
            for token in ("社区", "消费者", "投资者", "媒体", "开发者", "community", "investors")
        ):
            subtype = "collective"
        elif any(
            token in text
            for token in (
                "公司",
                "集团",
                "inc",
                "corp",
                "ltd",
                "企业",
                "组织",
                "apple",
                "nvidia",
                "tsmc",
                "openai",
            )
        ):
            subtype = "organization"
        else:
            subtype = "person"
        category = "agent" if subtype in AGENT_SUBTYPES else "non_agent"
        return {
            "category": category,
            "agent_capable": category == "agent",
            "subtype": subtype,
            "confidence": 0.68 if subtype == "person" else 0.80,
            "rationale": (
                "deterministic lexical/context classification; "
                "confirm low-confidence entities"
            ),
        }

    @staticmethod
    def _normalise(value: str) -> str:
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.casefold())


__all__ = [
    "ClassifiedWorldEntity",
    "EntityClassificationError",
    "WorldEntityCandidate",
    "WorldEntityClassificationResult",
    "WorldEntityClassificationService",
]
