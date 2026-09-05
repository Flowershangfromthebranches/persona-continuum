from __future__ import annotations

from persona_continuum.domain.identity import (
    IdentitySpec,
    ResolvedIdentity,
    SubjectKind,
    WebResearchScope,
)


class ResearchQueryBuilder:
    """Build grounded, disambiguated research queries from ResolvedIdentity and IdentitySpec.

    Guarantees:
    - Never search a bare name for fictional characters or original characters.
    - Always inject positive discriminators (work_or_universe, canonical_name, aliases).
    - Original characters (OC) are constrained to background_only research (world, occupation, era)
      and never searched as real-world persons to prevent identity pollution.
    """

    @classmethod
    def build_queries(
        cls,
        spec: IdentitySpec,
        resolved: ResolvedIdentity,
        raw_queries: list[str],
    ) -> list[str]:
        scope = spec.effective_web_scope()
        positive_discriminator = (
            resolved.work_or_universe
            or spec.work_or_universe
            or ""
        ).strip()

        built: list[str] = []
        for raw in raw_queries:
            q = str(raw).strip()
            if not q:
                continue

            if (
                spec.subject_kind == SubjectKind.ORIGINAL_CHARACTER
                or scope == WebResearchScope.BACKGROUND_ONLY
            ):
                # Strip an OC's personal name to avoid unrelated real-person matches.
                # Transform personal biographical queries into background/context research.
                name = resolved.canonical_name or spec.display_name
                background_query = q.replace(name, "").strip()
                for alias in resolved.aliases:
                    background_query = background_query.replace(alias, "").strip()
                if positive_discriminator and positive_discriminator not in background_query:
                    background_query = f"{positive_discriminator} {background_query}".strip()
                if background_query and len(background_query) >= 2:
                    built.append(background_query)
                else:
                    # Fallback to general context if name was the whole query
                    built.append(f"{positive_discriminator or '背景资料'} {q}".strip())

            elif spec.subject_kind == SubjectKind.FICTIONAL_CHARACTER:
                # Fictional Canon rule: must contain the work_or_universe or main canon identifier
                if positive_discriminator and positive_discriminator not in q:
                    built.append(f"{positive_discriminator} {q}".strip())
                else:
                    built.append(q)

            else:
                # Real person: if disambiguation is specified, inject context keywords
                if positive_discriminator and positive_discriminator not in q:
                    built.append(f"{q} {positive_discriminator}".strip())
                else:
                    built.append(q)

        # Deduplicate while preserving order
        seen: set[str] = set()
        deduped: list[str] = []
        for item in built:
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        return deduped


class IdentityResolver:
    """Resolve and disambiguate character/person identity before deep research."""

    @classmethod
    def resolve_spec(cls, spec: IdentitySpec) -> ResolvedIdentity:
        canonical = spec.display_name.strip()
        aliases = [a.strip() for a in spec.aliases if a.strip()]
        work = (spec.work_or_universe or "").strip() or None

        positive_terms: list[str] = []
        negative_terms: list[str] = []

        if work:
            positive_terms.append(f"{canonical} {work}")
            for a in aliases:
                positive_terms.append(f"{a} {work}")
            negative_terms.append(f"unrelated real persons named {canonical}")

        if spec.identity_context:
            positive_terms.append(spec.identity_context[:100].strip())

        return ResolvedIdentity(
            display_name=spec.display_name,
            canonical_name=canonical,
            aliases=aliases,
            subject_kind=spec.subject_kind,
            work_or_universe=work,
            life_status=spec.life_status,
            confidence=(
                0.95
                if work
                or spec.identity_context
                or spec.subject_kind == SubjectKind.REAL_PERSON
                else 0.75
            ),
            positive_search_terms=positive_terms,
            negative_search_terms=negative_terms,
            summary=spec.identity_context or work or "",
        )
