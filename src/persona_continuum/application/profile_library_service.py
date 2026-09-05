from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from persona_continuum.agent.adapter import AgentAdapter
from persona_continuum.agent.context_budget import AgentContextBudgetManager
from persona_continuum.agent.models import (
    AgentSessionConfig,
    PermissionProfile,
)
from persona_continuum.agent.runtime_executor import AgentRuntimeExecutor
from persona_continuum.agent.structured_output import StructuredResult
from persona_continuum.application._utils import dumps, loads, new_id
from persona_continuum.application.job_progress import JobNotTerminalError
from persona_continuum.domain.persona import PersonaManifest, PersonaRecord
from persona_continuum.domain.profile import (
    ActorProfile,
    CollectiveProfile,
    EnrichmentInputMode,
    InstitutionProfile,
    OrganizationProfile,
    PersonaProfile,
    ProfileCoverageState,
    ProfileEnrichmentJob,
    ProfileEnrichmentStatus,
    ProfileStatus,
    ProfileType,
    ProfileVersion,
    profile_now,
)
from persona_continuum.numeric import safe_int
from persona_continuum.security.paths import safe_slug
from persona_continuum.security.validation import NotFoundError

if TYPE_CHECKING:
    from persona_continuum.application.persona_service import PersonaService
    from persona_continuum.storage.database import Database


PROFILE_SUMMARY_SYSTEM_PROMPT = (
    "You generate concise evidence-grounded summaries for Persona Continuum.\n"
    'Return JSON only: {"summary": "..."}.\n'
    "Write one to three factual sentences, no marketing language, no invented facts,\n"
    "and no claim that is not supported by the supplied compiled profile or evidence.\n"
    "If the display name contains Chinese characters or the profile context is Chinese, "
    "write the summary in natural, accurate Chinese (中文).\n"
    "The summary is a library card blurb, not a system prompt."
)


class ProfileSummaryGenerator:
    """Produces bounded blurbs while keeping the compiled profile authoritative."""

    GENERIC_SUMMARIES = {
        "compiled digital continuum persona.",
        "persona compiled continuum asset.",
    }

    def __init__(self, runtime_executor: AgentRuntimeExecutor | None = None) -> None:
        self.runtime_executor = runtime_executor or AgentRuntimeExecutor()

    @classmethod
    def normalize(cls, value: object, *, display_name: str) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if text.casefold() in cls.GENERIC_SUMMARIES:
            return ""
        if "尚未完成证据编译" in text or "可继续完善的草稿" in text:
            return ""
        if not text:
            return ""
        # Card summaries should remain compact while retaining complete
        # sentences when the model supplied a longer explanation.
        text = text[:420].rstrip()
        sentences = re.split(r"(?<=[。！？.!?])\s+", text)
        text = " ".join(sentences[:3]).strip()
        if not text:
            return f"{display_name} 的档案正在根据现有证据建立。"
        return text

    @classmethod
    def fallback(
        cls,
        *,
        profile_type: ProfileType,
        display_name: str,
        compiled_profile: dict[str, Any] | None = None,
        evidence: Iterable[Any] = (),
        source_count: int = 0,
    ) -> str:
        compiled = compiled_profile or {}
        candidates: list[object] = []
        for key in (
            "summary",
            "blurb",
            "identity_summary",
            "mission",
            "institutional_goal",
            "group_characteristics",
        ):
            value = compiled.get(key)
            if value:
                candidates.append(value)
        identity = compiled.get("identity_profile")
        if isinstance(identity, dict):
            candidates.extend(
                identity.get(key) for key in ("summary", "description", "role") if identity.get(key)
            )
        for candidate in candidates:
            summary = cls.normalize(candidate, display_name=display_name)
            if summary:
                return summary
        evidence_list = list(evidence)
        keywords = [
            w.lower()
            for w in re.split(r"[\s\(\)\-_/]+", display_name)
            if len(w) >= 2 and not w.isdigit()
        ]
        for source in evidence_list:
            if isinstance(source, dict):
                content = str(source.get("content") or "").strip()
                title = str(source.get("title") or "")
            else:
                content = str(getattr(source, "content", "") if source is not None else "").strip()
                title = str(getattr(source, "title", "") if source is not None else "")
            if (
                "README" in title
                or content.startswith("{")
                or content.startswith("[")
                or content.startswith("#")
            ):
                continue
            if keywords and not any(k in content.lower() or k in title.lower() for k in keywords):
                continue
            sentences = [
                s.strip()
                for s in re.split(r"(?<=[。！？.!?])\s+", content)
                if len(s.strip()) > 20 and not s.strip().startswith("#")
            ]
            if sentences:
                candidate = " ".join(sentences[:2])
                summary = cls.normalize(candidate, display_name=display_name)
                if summary:
                    return summary
        labels = {
            ProfileType.PERSONA: "人物人格档案",
            ProfileType.ORGANIZATION: "组织决策档案",
            ProfileType.INSTITUTION: "制度决策档案",
            ProfileType.COLLECTIVE: "群体行为档案",
        }
        if source_count <= 0 and not compiled:
            return (
                f"{display_name} 的{labels[profile_type]}尚未完成证据编译，当前为可继续完善的草稿。"
            )
        return (
            f"{display_name} 是一个基于 {source_count} 个证据来源建立的"
            f"{labels[profile_type]}，覆盖当前已编译的决策与关系信息。"
        )

    async def generate(
        self,
        *,
        profile_type: ProfileType,
        display_name: str,
        compiled_profile: dict[str, Any] | None = None,
        evidence: Iterable[Any] = (),
        adapter: AgentAdapter | None = None,
        runtime: dict[str, Any] | None = None,
        fallback_source_count: int = 0,
    ) -> str:
        runtime = runtime or {}
        evidence_items = list(evidence)
        if adapter is None:
            return self.fallback(
                profile_type=profile_type,
                display_name=display_name,
                compiled_profile=compiled_profile,
                evidence=evidence_items,
                source_count=fallback_source_count,
            )
        prompt_base = {
            "profile_type": profile_type.value,
            "display_name": display_name,
            "compiled_profile": compiled_profile or {},
        }
        output_schema = {
            "type": "object",
            "required": ["summary"],
            "properties": {"summary": {"type": "string"}},
            "additionalProperties": True,
        }
        def evidence_payload(item: Any) -> dict[str, str]:
            return {
                "id": str(getattr(item, "id", "")),
                "title": str(getattr(item, "title", "")),
                "content": str(getattr(item, "content", "")),
            }
        session_binding = None
        try:
            session_binding = await self.runtime_executor.open_session(
                adapter,
                AgentSessionConfig(
                    session_id=f"profile_summary_{safe_slug(display_name)}",
                    room_id="profile_summary",
                    participant_id=f"summary:{safe_slug(display_name)}",
                    persona_id=display_name,
                    model_id=runtime.get("model_id"),
                    reasoning_effort=runtime.get("reasoning_effort"),
                    auth_profile_id=runtime.get("auth_profile_id"),
                    permission_profile=PermissionProfile.CHAT_SAFE,
                    allow_mcp=False,
                    tools=[],
                    system_prompt=PROFILE_SUMMARY_SYSTEM_PROMPT,
                )
            )
            manager: AgentContextBudgetManager | None = getattr(
                self.runtime_executor, "context_budget_manager", None
            )
            if manager is None:
                raise ValueError("profile summary context budget manager missing")
            base_text = json.dumps(prompt_base, ensure_ascii=False)
            batches = list(
                manager.iter_batches(
                    evidence_items,
                    item_text=lambda item: json.dumps(evidence_payload(item), ensure_ascii=False),
                    max_items=12,
                    phase="profile_summary",
                    model=session_binding.session.session_data.get("model_capability"),
                    base_text=base_text,
                    system_prompt=PROFILE_SUMMARY_SYSTEM_PROMPT,
                    expected_output=output_schema,
                )
            ) or [[]]
            summaries: list[str] = []
            for batch in batches:
                prompt = {
                    **prompt_base,
                    "evidence": [evidence_payload(item) for item in batch],
                }
                result = await self.runtime_executor.execute_structured(
                    session_binding,
                    system_prompt=PROFILE_SUMMARY_SYSTEM_PROMPT,
                    user_message=json.dumps(prompt, ensure_ascii=False),
                    schema=output_schema,
                    phase="profile_summary",
                    metadata={"profile_summary": True, "batch_size": len(batch)},
                )
                if not isinstance(result, StructuredResult):
                    raise ValueError("profile summary structured result missing")
                candidate = self.normalize(
                    result.value.get("summary"), display_name=display_name
                )
                if candidate:
                    summaries.append(candidate)
            summary = self.normalize(" ".join(summaries), display_name=display_name)
            if summary:
                return summary
        except Exception as exc:
            # A card must never block a completed profile because a summary
            # turn was unavailable.  The fallback remains evidence-derived.
            runtime["summary_agent_error"] = str(exc)[:500]
        finally:
            if session_binding is not None:
                with contextlib.suppress(Exception):
                    await self.runtime_executor.close(session_binding)
        return self.fallback(
            profile_type=profile_type,
            display_name=display_name,
            compiled_profile=compiled_profile,
            evidence=evidence_items,
            source_count=fallback_source_count,
        )


class ProfileLibraryService:
    """Unified Actor/Profile Library backed by the existing Persona store."""

    def __init__(
        self,
        database: Database,
        personas: PersonaService,
        runtime_executor: AgentRuntimeExecutor | None = None,
    ) -> None:
        self.database = database
        self.personas = personas
        self.runtime_executor = runtime_executor or AgentRuntimeExecutor()
        self.summary_generator = ProfileSummaryGenerator(self.runtime_executor)

    def sync_personas(self) -> list[ActorProfile]:
        profiles: list[ActorProfile] = []
        for persona in self.personas.list(include_archived=True):
            profiles.append(self.sync_persona(persona))
        return profiles

    def sync_persona(
        self,
        persona: PersonaRecord | str,
        *,
        summary: str | None = None,
        runtime_snapshot: dict[str, Any] | None = None,
    ) -> ActorProfile:
        record = self.personas.get(persona) if isinstance(persona, str) else persona
        manifest = record.manifest
        sources = list(self.personas.get_sources(record.id))
        evidence_count = safe_int(
            self.database.conn.execute(
                "SELECT COUNT(*) AS count FROM claims WHERE persona_id = ?", (record.id,)
            ).fetchone()["count"],
            default=0,
            minimum=0,
        ) or 0
        evidence_count += len(sources)
        normalized_summary = self.summary_generator.normalize(
            summary or manifest.summary,
            display_name=record.display_name,
        )
        if not normalized_summary:
            normalized_summary = self.summary_generator.fallback(
                profile_type=ProfileType.PERSONA,
                display_name=record.display_name,
                compiled_profile={},
                evidence=sources,
                source_count=len(sources),
            )
            if manifest.summary != normalized_summary:
                manifest.summary = normalized_summary
                self.personas.update_manifest(manifest)
        status = self._profile_status(manifest)
        coverage_state = self._coverage_state(manifest)
        now = profile_now()
        existing = self.database.conn.execute(
            "SELECT created_at, version, summary, payload_json, runtime_snapshot_json "
            "FROM actor_profiles WHERE id = ?",
            (record.id,),
        ).fetchone()
        created_at = str(existing["created_at"]) if existing else now
        payload = loads(existing["payload_json"]) if existing else {}
        runtime_snapshot_value = (
            runtime_snapshot
            if runtime_snapshot is not None
            else dict(loads(existing["runtime_snapshot_json"] or "{}"))
            if existing
            else {}
        )
        previous_manifest_version = str(payload.get("manifest_version") or "")
        version = (
            safe_int(existing["version"], default=1, minimum=1) if existing else None
        ) or self._manifest_version(manifest.version)
        if existing and previous_manifest_version and previous_manifest_version != manifest.version:
            # Preserve the previously compiled snapshot before the Persona
            # compiler's manifest revision becomes the active library version.
            self.database.conn.execute(
                """INSERT OR IGNORE INTO profile_versions (
                  id, profile_id, version, summary, payload_json, source_ids_json,
                  created_at, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id("profile_version"),
                    record.id,
                    safe_int(existing["version"], default=1, minimum=1) or 1,
                    str(existing["summary"] or ""),
                    existing["payload_json"] or "{}",
                    dumps([source.id for source in sources]),
                    now,
                    "persona_compile",
                ),
            )
            version += 1
        payload["manifest_version"] = manifest.version
        self.database.conn.execute(
            """INSERT INTO actor_profiles (
              id, profile_type, display_name, slug, aliases_json, summary, status,
              source_count, evidence_count, coverage_state, compile_state, persona_id,
              runtime_snapshot_json, coverage_json, payload_json, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              profile_type=excluded.profile_type, display_name=excluded.display_name,
              slug=excluded.slug, aliases_json=excluded.aliases_json, summary=excluded.summary,
              status=excluded.status, source_count=excluded.source_count,
              evidence_count=excluded.evidence_count, coverage_state=excluded.coverage_state,
              compile_state=excluded.compile_state, persona_id=excluded.persona_id,
              runtime_snapshot_json=excluded.runtime_snapshot_json,
              coverage_json=excluded.coverage_json,
              payload_json=excluded.payload_json, version=excluded.version,
              updated_at=excluded.updated_at""",
            (
                record.id,
                ProfileType.PERSONA.value,
                record.display_name,
                safe_slug(record.display_name),
                dumps(manifest.aliases),
                normalized_summary,
                status.value,
                len(sources),
                evidence_count,
                coverage_state.value,
                manifest.compile_state,
                record.id,
                dumps(runtime_snapshot_value),
                dumps({"confidence": manifest.confidence}),
                dumps(payload),
                version,
                created_at,
                now,
            ),
        )
        self.database.conn.commit()
        return self.get_profile(record.id)

    async def generate_persona_summary(
        self,
        persona_id: str,
        *,
        adapter: AgentAdapter | None = None,
        runtime: dict[str, Any] | None = None,
    ) -> ActorProfile:
        """Refresh the library blurb after compile/enrichment.

        The optional Agent call is bounded to summary generation only.  The
        manifest, sources and compiled components remain authoritative and the
        deterministic generator is used if the selected runtime is unavailable.
        """
        profile = self.sync_persona(persona_id, runtime_snapshot=runtime)
        persona = self.personas.get(persona_id)
        summary = await self.summary_generator.generate(
            profile_type=ProfileType.PERSONA,
            display_name=persona.display_name,
            compiled_profile=profile.payload,
            evidence=self.personas.get_sources(persona_id),
            adapter=adapter,
            runtime=runtime,
            fallback_source_count=profile.source_count,
        )
        manifest = persona.manifest
        manifest.summary = summary
        self.personas.update_manifest(manifest)
        return self.sync_persona(persona_id, summary=summary, runtime_snapshot=runtime)

    def create_profile(
        self,
        *,
        profile_type: ProfileType,
        display_name: str,
        summary: str = "",
        aliases: list[str] | None = None,
        payload: dict[str, Any] | None = None,
        status: ProfileStatus = ProfileStatus.DRAFT,
        compile_state: str = "draft",
        runtime_snapshot: dict[str, Any] | None = None,
        source_count: int = 0,
        evidence_count: int = 0,
        profile_id: str | None = None,
    ) -> ActorProfile:
        if profile_type == ProfileType.PERSONA:
            raise ValueError("persona_profiles_require_persona_creation_pipeline")
        display_name = str(display_name or "").strip()
        if not display_name:
            raise ValueError("display_name_required")
        now = profile_now()
        profile_id = profile_id or f"profile_{safe_slug(display_name)}"
        slug = safe_slug(display_name)
        # Check the display name and every supplied alias before allocating a
        # new library record.  A duplicate alias is just as dangerous as a
        # duplicate display name because World Entity Matching treats aliases
        # as exact profile identifiers.
        for duplicate_name in [display_name, *(aliases or [])]:
            duplicate = self.find_by_name(duplicate_name)
            if duplicate and duplicate.id != profile_id:
                raise ValueError(f"profile_already_exists:{duplicate.id}")
        existing_id = self.database.conn.execute(
            "SELECT id FROM actor_profiles WHERE id = ? OR slug = ?",
            (profile_id, slug),
        ).fetchone()
        if existing_id:
            raise ValueError(f"profile_already_exists:{existing_id['id']}")
        summary = self.summary_generator.normalize(summary, display_name=display_name)
        if not summary:
            summary = self.summary_generator.fallback(
                profile_type=profile_type,
                display_name=display_name,
                compiled_profile=payload,
                source_count=source_count,
            )
        self.database.conn.execute(
            """INSERT INTO actor_profiles (
              id, profile_type, display_name, slug, aliases_json, summary, status,
              source_count, evidence_count, coverage_state, compile_state, persona_id,
              runtime_snapshot_json, coverage_json, payload_json, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                profile_id,
                profile_type.value,
                display_name,
                slug,
                dumps(aliases or []),
                summary,
                status.value,
                source_count,
                evidence_count,
                ProfileCoverageState.PARTIAL.value,
                compile_state,
                None,
                dumps(runtime_snapshot or {}),
                dumps({}),
                dumps(payload or {}),
                1,
                now,
                now,
            ),
        )
        self.database.conn.commit()
        return self.get_profile(profile_id)

    def find_by_name(
        self,
        display_name: str,
        *,
        profile_type: ProfileType | str | None = None,
    ) -> ActorProfile | None:
        """Find an existing profile before creating a duplicate slug/version."""
        needle = str(display_name or "").strip().casefold()
        if not needle:
            return None
        profiles = (
            self.list_profiles(profile_type=profile_type) if profile_type else self.list_profiles()
        )
        for profile in profiles:
            values = {
                profile.display_name.casefold(),
                profile.slug.casefold(),
                profile.id.casefold(),
            }
            values.update(alias.casefold() for alias in profile.aliases)
            if needle in values:
                return profile
        return None

    def list_profiles(
        self,
        *,
        profile_type: ProfileType | str | None = None,
        status: ProfileStatus | str | None = None,
        query: str | None = None,
        sort_by: str = "updated_at",
        descending: bool = True,
    ) -> list[ActorProfile]:
        self.sync_personas()
        clauses = ["1 = 1"]
        values: list[Any] = []
        if profile_type:
            clauses.append("profile_type = ?")
            values.append(ProfileType(profile_type).value)
        if status:
            clauses.append("status = ?")
            values.append(ProfileStatus(status).value)
        if query:
            clauses.append("(display_name LIKE ? OR slug LIKE ? OR summary LIKE ?)")
            needle = f"%{query}%"
            values.extend([needle, needle, needle])
        safe_sort = {
            "updated_at": "updated_at",
            "created_at": "created_at",
            "source_count": "source_count",
            "evidence_count": "evidence_count",
            "display_name": "display_name",
        }.get(sort_by, "updated_at")
        direction = "DESC" if descending else "ASC"
        query_sql = (
            f"SELECT * FROM actor_profiles WHERE {' AND '.join(clauses)} "
            f"ORDER BY {safe_sort} {direction}"
        )
        rows = self.database.conn.execute(
            query_sql,
            values,
        ).fetchall()
        return [self._row_to_profile(row) for row in rows]

    def get_profile(self, profile_id: str) -> ActorProfile:
        row = self.database.conn.execute(
            "SELECT * FROM actor_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        if row is None:
            # A legacy Persona may not have been synchronized yet.
            with contextlib.suppress(Exception):
                return self.sync_persona(profile_id)
            raise NotFoundError(profile_id)
        return self._row_to_profile(row)

    def get_detail(self, profile_id: str) -> dict[str, Any]:
        profile = self.get_profile(profile_id)
        versions = self.list_versions(profile_id)
        result = profile.model_dump(mode="json")
        result["versions"] = [version.model_dump(mode="json") for version in versions]
        if profile.persona_id:
            manifest = self.personas.get(profile.persona_id).manifest
            result["persona"] = manifest.model_dump(mode="json")
            result["sources"] = [
                {
                    "id": source.id,
                    "title": source.title,
                    "source_type": source.source_type,
                    "metadata": source.metadata,
                }
                for source in self.personas.get_sources(profile.persona_id)
            ]
        return result

    def archive_profile(self, profile_id: str) -> ActorProfile:
        """Archive a unified profile without deleting provenance or versions."""
        return self.update_profile(
            profile_id,
            status=ProfileStatus.ARCHIVED,
            compile_state="archived",
            created_by="profile_archive",
        )

    def update_profile(
        self,
        profile_id: str,
        *,
        summary: str | None = None,
        payload: dict[str, Any] | None = None,
        status: ProfileStatus | str | None = None,
        compile_state: str | None = None,
        coverage: dict[str, Any] | None = None,
        runtime_snapshot: dict[str, Any] | None = None,
        source_ids: list[str] | None = None,
        source_count: int | None = None,
        evidence_count: int | None = None,
        created_by: str = "profile_runtime",
    ) -> ActorProfile:
        current = self.get_profile(profile_id)
        self.database.conn.execute(
            """INSERT INTO profile_versions (
              id, profile_id, version, summary, payload_json, source_ids_json,
              created_at, created_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("profile_version"),
                current.id,
                current.version,
                current.summary,
                dumps(current.payload),
                dumps(source_ids or []),
                profile_now(),
                created_by,
            ),
        )
        now = profile_now()
        next_summary = self.summary_generator.normalize(
            summary if summary is not None else current.summary,
            display_name=current.display_name,
        )
        next_status = ProfileStatus(status).value if status is not None else current.status.value
        next_compile_state = compile_state or current.compile_state
        next_coverage = coverage if coverage is not None else current.coverage
        next_coverage_state = (
            ProfileCoverageState.GAPS.value
            if next_status == ProfileStatus.COMPLETED_WITH_GAPS.value
            else ProfileCoverageState.COMPLETE.value
            if next_compile_state in {"compiled", "completed"}
            else current.coverage_state.value
        )
        self.database.conn.execute(
            """UPDATE actor_profiles SET summary = ?, status = ?, compile_state = ?,
              coverage_state = ?, source_count = ?, evidence_count = ?,
              coverage_json = ?, payload_json = ?, runtime_snapshot_json = ?,
              version = ?, updated_at = ? WHERE id = ?""",
            (
                next_summary,
                next_status,
                next_compile_state,
                next_coverage_state,
                source_count if source_count is not None else current.source_count,
                evidence_count if evidence_count is not None else current.evidence_count,
                dumps(next_coverage),
                dumps(payload if payload is not None else current.payload),
                dumps(
                    runtime_snapshot if runtime_snapshot is not None else current.runtime_snapshot
                ),
                current.version + 1,
                now,
                profile_id,
            ),
        )
        self.database.conn.commit()
        return self.get_profile(profile_id)

    def list_versions(self, profile_id: str) -> list[ProfileVersion]:
        rows = self.database.conn.execute(
            "SELECT * FROM profile_versions WHERE profile_id = ? ORDER BY version DESC",
            (profile_id,),
        ).fetchall()
        return [
            ProfileVersion(
                id=str(row["id"]),
                profile_id=str(row["profile_id"]),
                version=safe_int(row["version"], default=1, minimum=1) or 1,
                summary=str(row["summary"]),
                payload=dict(loads(row["payload_json"] or "{}")),
                source_ids=list(loads(row["source_ids_json"] or "[]")),
                created_at=str(row["created_at"]),
                created_by=str(row["created_by"]),
            )
            for row in rows
        ]

    def save_enrichment_job(self, job: ProfileEnrichmentJob) -> ProfileEnrichmentJob:
        job.updated_at = profile_now()
        if job.evidence_delta:
            job.progress["evidence_delta"] = dict(job.evidence_delta)
        self.database.conn.execute(
            """INSERT INTO profile_enrichment_jobs (
              id, target_profile_id, target_profile_type, job_type, selected_runtime_json,
              research_policy_json, requested_scope, status, progress_json, error,
              failure_json, agent_call_audits_json,
              source_count, new_version, persona_creation_job_id, enrichment_input_mode,
              research_focus,
              parent_job_id, base_persona_version, input_material_ids_json,
              input_material_count, new_source_ids_json, visibility, dismissed_at,
              superseded_by, worker_state, worker_started_at, worker_heartbeat_at,
              worker_finished_at, agent_call_count, created_at, updated_at
            ) VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(id) DO UPDATE SET status=excluded.status,
              selected_runtime_json=excluded.selected_runtime_json,
              research_policy_json=excluded.research_policy_json,
              requested_scope=excluded.requested_scope, progress_json=excluded.progress_json,
              error=excluded.error, source_count=excluded.source_count,
              failure_json=excluded.failure_json,
              agent_call_audits_json=excluded.agent_call_audits_json,
              new_version=excluded.new_version,
              persona_creation_job_id=excluded.persona_creation_job_id,
              enrichment_input_mode=excluded.enrichment_input_mode,
              research_focus=excluded.research_focus,
              parent_job_id=excluded.parent_job_id,
              base_persona_version=excluded.base_persona_version,
              input_material_ids_json=excluded.input_material_ids_json,
              input_material_count=excluded.input_material_count,
              new_source_ids_json=excluded.new_source_ids_json,
              visibility=excluded.visibility,
              dismissed_at=excluded.dismissed_at,
              superseded_by=excluded.superseded_by,
              worker_state=excluded.worker_state,
              worker_started_at=excluded.worker_started_at,
              worker_heartbeat_at=excluded.worker_heartbeat_at,
              worker_finished_at=excluded.worker_finished_at,
              agent_call_count=excluded.agent_call_count,
              updated_at=excluded.updated_at""",
            (
                job.id,
                job.target_profile_id,
                job.target_profile_type.value,
                job.job_type,
                dumps(job.selected_runtime),
                dumps(job.research_policy),
                job.requested_scope,
                job.status.value,
                dumps(job.progress),
                job.error,
                dumps(job.failure_json) if job.failure_json else None,
                dumps(job.agent_call_audits),
                job.source_count,
                job.new_version,
                job.persona_creation_job_id,
                job.enrichment_input_mode.value,
                job.research_focus,
                job.parent_job_id,
                job.base_persona_version,
                dumps(job.input_material_ids),
                job.input_material_count,
                dumps(job.new_source_ids),
                job.visibility,
                job.dismissed_at,
                job.superseded_by,
                job.worker_state,
                job.worker_started_at,
                job.worker_heartbeat_at,
                job.worker_finished_at,
                job.agent_call_count,
                job.created_at,
                job.updated_at,
            ),
        )
        self.database.conn.commit()
        return self.get_enrichment_job(job.id)

    def get_enrichment_job(self, job_id: str) -> ProfileEnrichmentJob:
        row = self.database.conn.execute(
            "SELECT * FROM profile_enrichment_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(job_id)
        row_keys = set(row.keys())
        return ProfileEnrichmentJob(
            id=str(row["id"]),
            target_profile_id=str(row["target_profile_id"]),
            target_profile_type=ProfileType(str(row["target_profile_type"])),
            job_type=str(row["job_type"]),
            selected_runtime=dict(loads(row["selected_runtime_json"] or "{}")),
            research_policy=dict(loads(row["research_policy_json"] or "{}")),
            requested_scope=str(row["requested_scope"]),
            research_focus=row["research_focus"] if "research_focus" in row_keys else None,
            enrichment_input_mode=EnrichmentInputMode(
                str(row["enrichment_input_mode"] or "local_materials")
            ),
            status=ProfileEnrichmentStatus(str(row["status"])),
            visibility=str(row["visibility"] or "user") if "visibility" in row_keys else "user",
            dismissed_at=(row["dismissed_at"] if "dismissed_at" in row_keys else None),
            superseded_by=(row["superseded_by"] if "superseded_by" in row_keys else None),
            worker_state=str(row["worker_state"] or "starting")
            if "worker_state" in row_keys
            else "starting",
            worker_started_at=(
                row["worker_started_at"] if "worker_started_at" in row_keys else None
            ),
            worker_heartbeat_at=(
                row["worker_heartbeat_at"] if "worker_heartbeat_at" in row_keys else None
            ),
            worker_finished_at=(
                row["worker_finished_at"] if "worker_finished_at" in row_keys else None
            ),
            agent_call_count=(
                safe_int(row["agent_call_count"], default=0, minimum=0) or 0
                if "agent_call_count" in row_keys
                else 0
            ),
            progress=dict(loads(row["progress_json"] or "{}")),
            failure_json=(dict(loads(row["failure_json"])) if row["failure_json"] else None),
            agent_call_audits=list(loads(row["agent_call_audits_json"] or "[]")),
            error=row["error"],
            source_count=safe_int(row["source_count"], default=0, minimum=0) or 0,
            new_version=(
                safe_int(row["new_version"], default=0, minimum=0)
                if row["new_version"] is not None
                else None
            ),
            persona_creation_job_id=row["persona_creation_job_id"],
            parent_job_id=row["parent_job_id"],
            base_persona_version=(
                safe_int(row["base_persona_version"], default=0, minimum=0)
                if row["base_persona_version"] is not None
                else None
            ),
            input_material_ids=list(loads(row["input_material_ids_json"] or "[]")),
            input_material_count=(
                safe_int(row["input_material_count"], default=0, minimum=0) or 0
            ),
            new_source_ids=list(loads(row["new_source_ids_json"] or "[]")),
            evidence_delta=dict(loads(row["progress_json"] or "{}").get("evidence_delta") or {}),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def list_enrichment_jobs(
        self,
        profile_id: str | None = None,
        *,
        task_center: bool = False,
        include_internal: bool = False,
        include_dismissed: bool = False,
        page: int = 1,
        page_size: int = 20,
    ) -> list[ProfileEnrichmentJob]:
        conditions: list[str] = []
        params: list[Any] = []
        if profile_id:
            conditions.append("target_profile_id = ?")
            params.append(profile_id)
        if task_center:
            conditions.append("dismissed_at IS NULL")
            # Keep the failed predecessor in durable history but highlight
            # only the fresh retry in the primary Task Center.
            conditions.append("(superseded_by IS NULL OR superseded_by = '')")
            if not include_internal:
                conditions.append("COALESCE(visibility, 'user') = 'user'")
            where = " AND ".join(conditions) or "1 = 1"
            terminal_values = sorted(
                status.value
                for status in (
                    ProfileEnrichmentStatus.COMPLETED,
                    ProfileEnrichmentStatus.COMPLETED_WITH_GAPS,
                    ProfileEnrichmentStatus.FAILED,
                    ProfileEnrichmentStatus.CANCELLED,
                )
            )
            active_rows = self.database.conn.execute(
                f"SELECT * FROM profile_enrichment_jobs WHERE {where} "
                f"AND status NOT IN ({','.join('?' for _ in terminal_values)}) "
                "ORDER BY updated_at DESC, created_at DESC",
                (*params, *terminal_values),
            ).fetchall()
            # Keep terminal pagination correct when the unified Task Center
            # requests a look-ahead larger than 1000 rows for a deep page.
            size = max(1, safe_int(page_size, default=20, minimum=1) or 20)
            offset = max(0, (safe_int(page, default=1, minimum=1) or 1) - 1) * size
            terminal_rows = self.database.conn.execute(
                f"SELECT * FROM profile_enrichment_jobs WHERE {where} "
                f"AND status IN ({','.join('?' for _ in terminal_values)}) "
                "ORDER BY updated_at DESC, created_at DESC LIMIT ? OFFSET ?",
                (*params, *terminal_values, size, offset),
            ).fetchall()
            return [self._row_to_enrichment(row) for row in [*active_rows, *terminal_rows]]

        where = " AND ".join(conditions) or "1 = 1"
        rows = self.database.conn.execute(
            f"SELECT * FROM profile_enrichment_jobs WHERE {where} ORDER BY created_at DESC",
            tuple(params),
        ).fetchall()
        if not include_dismissed:
            rows = [row for row in rows if row["dismissed_at"] is None]
        return [self._row_to_enrichment(row) for row in rows]

    def task_center_counts(self, *, include_internal: bool = False) -> dict[str, int]:
        conditions = [
            "dismissed_at IS NULL",
            "(superseded_by IS NULL OR superseded_by = '')",
        ]
        if not include_internal:
            conditions.append("COALESCE(visibility, 'user') = 'user'")
        where = " AND ".join(conditions)
        rows = self.database.conn.execute(
            "SELECT status, COUNT(*) AS count FROM profile_enrichment_jobs "
            f"WHERE {where} GROUP BY status"
        ).fetchall()
        counts = {
            str(row["status"]): safe_int(row["count"], default=0, minimum=0) or 0
            for row in rows
        }
        terminal = {
            ProfileEnrichmentStatus.COMPLETED.value,
            ProfileEnrichmentStatus.COMPLETED_WITH_GAPS.value,
            ProfileEnrichmentStatus.FAILED.value,
            ProfileEnrichmentStatus.CANCELLED.value,
        }
        active = sum(count for status, count in counts.items() if status not in terminal)
        paused = counts.get(ProfileEnrichmentStatus.PAUSED.value, 0)
        paused_runtime = counts.get(ProfileEnrichmentStatus.PAUSED_RUNTIME_UNAVAILABLE.value, 0)
        return {
            **counts,
            "active": active,
            "badge": max(0, active - paused - paused_runtime),
            "terminal": sum(counts.get(status, 0) for status in terminal),
        }

    def dismiss_enrichment_job(self, job_id: str) -> ProfileEnrichmentJob:
        job = self.get_enrichment_job(job_id)
        terminal = {
            ProfileEnrichmentStatus.COMPLETED,
            ProfileEnrichmentStatus.COMPLETED_WITH_GAPS,
            ProfileEnrichmentStatus.FAILED,
            ProfileEnrichmentStatus.CANCELLED,
        }
        if job.status not in terminal:
            raise JobNotTerminalError(job.id, job.status.value)
        job.dismissed_at = profile_now()
        return self.save_enrichment_job(job)

    def cleanup_enrichment_jobs(
        self, statuses: Iterable[str], *, include_internal: bool = False
    ) -> dict[str, Any]:
        requested = [str(status) for status in statuses]
        allowed = {
            ProfileEnrichmentStatus.COMPLETED.value,
            ProfileEnrichmentStatus.COMPLETED_WITH_GAPS.value,
            ProfileEnrichmentStatus.FAILED.value,
            ProfileEnrichmentStatus.CANCELLED.value,
        }
        selected = sorted(set(requested) & allowed)
        if not selected:
            return {"dismissed_job_ids": [], "dismissed_count": 0, "ignored_statuses": requested}
        conditions = [
            "dismissed_at IS NULL",
            f"status IN ({','.join('?' for _ in selected)})",
        ]
        params: list[Any] = list(selected)
        if not include_internal:
            conditions.append("COALESCE(visibility, 'user') = 'user'")
        where = " AND ".join(conditions)
        rows = self.database.conn.execute(
            f"SELECT id FROM profile_enrichment_jobs WHERE {where}", tuple(params)
        ).fetchall()
        ids = [str(row["id"]) for row in rows]
        if ids:
            now = profile_now()
            placeholders = ",".join("?" for _ in ids)
            self.database.conn.execute(
                "UPDATE profile_enrichment_jobs SET dismissed_at = ?, updated_at = ? "
                f"WHERE id IN ({placeholders})",
                (now, now, *ids),
            )
            self.database.conn.commit()
        return {
            "dismissed_job_ids": ids,
            "dismissed_count": len(ids),
            "ignored_statuses": [status for status in requested if status not in selected],
        }

    def _row_to_enrichment(self, row: Any) -> ProfileEnrichmentJob:
        return self.get_enrichment_job(str(row["id"]))

    def _row_to_profile(self, row: Any) -> ActorProfile:
        data = {
            "id": str(row["id"]),
            "profile_type": ProfileType(str(row["profile_type"])),
            "display_name": str(row["display_name"]),
            "slug": str(row["slug"]),
            "aliases": list(loads(row["aliases_json"] or "[]")),
            "summary": str(row["summary"] or ""),
            "status": ProfileStatus(str(row["status"])),
            "source_count": safe_int(row["source_count"], default=0, minimum=0) or 0,
            "evidence_count": safe_int(row["evidence_count"], default=0, minimum=0) or 0,
            "coverage_state": ProfileCoverageState(str(row["coverage_state"])),
            "compile_state": str(row["compile_state"]),
            "persona_id": row["persona_id"],
            "runtime_snapshot": dict(loads(row["runtime_snapshot_json"] or "{}")),
            "coverage": dict(loads(row["coverage_json"] or "{}")),
            "payload": dict(loads(row["payload_json"] or "{}")),
            "version": safe_int(row["version"], default=1, minimum=1) or 1,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        cls = {
            ProfileType.PERSONA: PersonaProfile if data["persona_id"] else ActorProfile,
            ProfileType.ORGANIZATION: OrganizationProfile,
            ProfileType.INSTITUTION: InstitutionProfile,
            ProfileType.COLLECTIVE: CollectiveProfile,
        }[data["profile_type"]]
        if cls is PersonaProfile and not data["persona_id"]:
            cls = ActorProfile
        return cls.model_validate(data)

    @staticmethod
    def _profile_status(manifest: PersonaManifest) -> ProfileStatus:
        if manifest.archived:
            return ProfileStatus.ARCHIVED
        if manifest.compile_state == "compiled":
            return ProfileStatus.COMPILED
        if manifest.compile_state == "completed_with_gaps":
            return ProfileStatus.COMPLETED_WITH_GAPS
        return ProfileStatus.DRAFT

    @staticmethod
    def _coverage_state(manifest: PersonaManifest) -> ProfileCoverageState:
        if manifest.compile_state == "compiled":
            return ProfileCoverageState.COMPLETE
        if manifest.compile_state == "completed_with_gaps":
            return ProfileCoverageState.GAPS
        return ProfileCoverageState.PARTIAL

    @staticmethod
    def _manifest_version(value: str) -> int:
        match = re.search(r"(\d+)", value or "")
        # Persona manifests historically start at 0.x; the unified library
        # still exposes a human-facing version sequence beginning at v1.
        return max(1, safe_int(match.group(1), default=1, minimum=1) or 1) if match else 1


__all__ = [
    "ProfileLibraryService",
    "ProfileSummaryGenerator",
]
