# Narrative Domain Model

All models live in `src/persona_continuum/domain/narrative.py` and persist via
`narrative/repository.py` into the `narrative_*` SQLite tables.

## NarrativeProject
`format` (micro_drama/series/novel/interactive_story/custom), genre/tone,
target audience, planned episode count, per-episode duration range,
`story_world_id` (Parallel World binding), `canonical_world_branch_id`
(current canon pointer), `story_bible_version`, `status`, `revision`.

## StoryBible (versioned)
premise, core_question, theme, world/technology/supernatural rules,
**final_truth** (Story Truth — author only), ending, characters/organizations/
locations (with visual dictionaries), master_timeline, major_reversals,
narrative_constraints, forbidden_shortcuts, author_notes, locked_fields.
Every edit creates a new version; old versions are immutable and dependent
Forecasts/Drafts become stale.

## Three-tier knowledge system
- **Story Truth** — `StoryFact` (author-level secrets).
- **Character Knowledge** — `CharacterKnowledgeEntry` per character×fact with
  state `unknown|suspected|known|misbelieved`, learned episode/scene, evidence.
- **Audience Knowledge** — `AudienceKnowledgeEntry` per fact with state
  `hidden|teased|partial|revealed|confirmed` (the suspense dial).

## Canon
`CanonEntry` (fact/event/relationship/world rule/character state/knowledge/
audience knowledge/plot thread/clue) with `source="narrative_commit"`,
plus `CanonRevision` history. Simulations never write here.

## Episodes & scenes
`EpisodePlan` (hook, beats, must_happen/must_not_happen, reveal targets,
forbidden reveals, relationship/emotion/world targets, cliffhanger),
`NarrativeScene` (participants with per-character goals, knowledge
constraints, must-not-reveal), `EpisodeVersion` (immutable drafts with
context fingerprint and audit linkage; canon points at one version).

## Threads / arcs / clues
`PlotThread` (planned/active/paused/resolved/abandoned), `CharacterArc`
(start/target state, phases, turning points, progress), `NarrativeClue`
(clue/foreshadowing/red_herring/mystery/secret with introduce/echo/reveal
episode tracking).

## Forecasts, audits, production
`NarrativeForecast` with `ForecastDirection`s (each bound to an isolated
world branch), `NarrativeAuditReport` with `AuditFinding`s, and
`ProductionPackage` with `Shot`s and visual bibles.
