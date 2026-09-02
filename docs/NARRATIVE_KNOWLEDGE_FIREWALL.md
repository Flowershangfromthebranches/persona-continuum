# Narrative Knowledge Firewall

`narrative/knowledge_firewall.py` — extends the Temporal Knowledge Firewall
idea to story knowledge.

## Principle

> A Persona actor may only ever see what its CharacterKnowledge state allows
> under the current canon/branch/episode/scene.

Character prompts must NOT contain: `StoryBible.final_truth`, other characters'
secrets, future reveals, audience-only state, author notes, future episode
plans — unless the content already entered that character's knowledge state.

## Enforcement points

1. **Prompt assembly** — `build_character_context()` reads only the character's
   OWN `CharacterKnowledgeEntry` rows (hard-filtered by `character_id`);
   story truth appears only as the character's KNOWN/SUSPECTED/MISBELIEVED
   facts; scene `must_not_reveal` and knowledge constraints are injected.
2. **Output scanning** — `scan_for_leaks()` detects secret fact text or
   `[fact:<id>]` markers in a character's dialogue/draft when the character
   does not KNOW the fact (BLOCKING `NARRATIVE_KNOWLEDGE_LEAK` audit finding).
3. **Auditor integration** — `continuity_auditor.audit_episode` scans scene
   dialogue per speaker against the knowledge matrix and audience state.

## Audience matrix

`audience_view()` maps fact → `hidden|teased|partial|revealed|confirmed`,
queriable per episode; the UI renders the full character×fact×audience matrix.

## Tests

`tests/unit/test_narrative_knowledge_firewall.py` and the firewall section of
`tests/e2e/test_narrative_studio_e2e.py` prove: unknown facts are blocked,
future-learned facts are invisible at earlier episodes, other characters'
knowledge never leaks, and leaked dialogue triggers a BLOCKING finding.
