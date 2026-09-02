# Actor Completion Engine

Actor Completion Engine replaces the old Persona-only completion wording in
Parallel World creation. After classification and profile matching, the user
sees missing Agent-capable entities and explicitly selects which records to
complete.

## Type-specific creation

- `person` delegates to `PersonaCreationOrchestrator`, including EvidenceSource,
  ResearchArtifact, eight dimensions, CompilationService and validation.
- `organization` creates or reuses an Organization Profile and starts a typed
  Profile Enrichment Job.
- `institution` uses institutional goals, policy tools, power structure,
  stakeholders and historical decision patterns.
- `collective` uses group characteristics, incentives, adoption tendencies,
  common positions and internal divisions.

Existing profiles are reused and enriched rather than duplicated. Private or
unknown people remain material/Guided Interview cases; the engine does not
search a same-name stranger. Non-Agent entities are never submitted to this
engine.

The engine persists the source entity id in each async job, allowing the UI to
bind the completed profile back to the correct Actor after long research. Jobs
are restartable and bounded by the existing runtime/credential selection.
Completion responses expose job metadata and counts, never uploaded material
contents. A missing or unavailable runtime fails explicitly rather than falling
back to a generated profile.

## Shared world gate

Preview and Direct Create both execute entity classification, profile match,
missing-profile confirmation, completion/binding, then world persistence. A
world is not created while a selected Agent profile is unresolved.
