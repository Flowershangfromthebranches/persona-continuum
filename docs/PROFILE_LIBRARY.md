# Agent / Profile Library

Persona Continuum now exposes one library for every entity that can become a
decision-making World Agent. The existing `PersonaService`, evidence tables,
memory service and eight-dimension compiler remain authoritative for
`PERSONA_PROFILE`; the other profile types use the same library envelope and a
typed, evidence-aware payload.

## Profile types

| Type | Typical examples | World use |
| --- | --- | --- |
| `persona` | historical, public, private or fictional people | Persona-bound actor and memory evolution |
| `organization` | Apple, NVIDIA, TSMC, OpenAI | resources, strategy, culture and competitive decisions |
| `institution` | governments, regulators, central banks | policy tools, authority and institutional goals |
| `collective` | developers, consumers, investors, media | incentives, adoption, common positions and internal divisions |

`ActorProfile` is the common envelope. `PersonaProfile`,
`OrganizationProfile`, `InstitutionProfile`, and `CollectiveProfile` are typed
views of that envelope. A Persona profile always retains `persona_id`; it is
not replaced by a free-form system prompt.

## Library card and detail

The 人物 navigation item is a structured library with search, type/status
filters, source/evidence sorting and grouped cards. Cards show name, slug,
type, evidence-grounded `summary`, status, source/evidence counts, coverage,
version and update time. The detail drawer shows runtime snapshot, coverage,
type-specific payload and version history. Non-Persona profiles can be archived
without deleting provenance.

`ProfileSummaryGenerator` is called after Persona compilation and structured
profile enrichment. It asks the selected Agent for one to three factual
sentences and falls back to compiled fields or source text; the generic legacy
placeholder is never used as a card summary.

## Persistence

- `actor_profiles` is the unified current record.
- `profile_versions` stores the previous record before every upgrade.
- `profile_enrichment_jobs` stores async runtime binding, scope, progress and
  restartable status.

Existing Persona rows are synchronized lazily and at library list time. No
database reset or package-format replacement is required.
