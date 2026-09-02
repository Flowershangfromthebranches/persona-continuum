"""Provenance taxonomy and per-persona retrieval policy.

Why this module exists
----------------------
Every piece of persona evidence used to be forced into one of five buckets, and
fictional personas were pushed wholesale into ``counterfactual_simulated``.
Because retrieval only admitted ``historical*`` source kinds, a fictional
character's entire biography became invisible at turn time -- the persona was
compiled but empty.

The fix is not to relabel fiction as history (that would break the AGENTS.md
invariant that simulated data must never be presented as historical fact).  The
fix is to give fiction its own provenance family, and to make *which* family is
visible a policy decision driven by the persona's nature.

Families
--------
``historical_*``
    Real people whose past is documented.  ``historical_self_report`` and
    ``historical_third_party_report`` already cover the observed/documented
    distinction, so no new names are introduced here.
``fictional_*``
    Author-defined canon and canon narrative experience.  ``fictional_author_defined``
    is what the author states about the character; ``fictional_canon`` is what the
    character has lived inside the story.
``*_simulated``
    Counterfactual or narrative divergence from a base persona.
``operational``
    Operator corrections and runtime interaction memories.

Scope isolation
---------------
Provenance answers *where a fact came from*.  Material scope answers *who is
allowed to see it*.  They are orthogonal: an ``author_only``
``fictional_author_defined`` claim is still fiction, it just is not something the
character knows.  See :data:`CHARACTER_VISIBLE`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from persona_continuum.domain.persona import PersonaType

# ---------------------------------------------------------------------------
# Provenance families
# ---------------------------------------------------------------------------

HISTORICAL_KINDS: frozenset[str] = frozenset(
    {
        "historical_self_report",  # the person said it
        "historical_third_party_report",  # someone documented it about them
        "historical_inference",  # derived from the above, never promoted to fact
    }
)

FICTIONAL_KINDS: frozenset[str] = frozenset(
    {
        "fictional_author_defined",  # author states it about the character
        "fictional_canon",  # the character lived it inside the story
    }
)

SIMULATED_KINDS: frozenset[str] = frozenset(
    {
        "counterfactual_simulated",  # a branch that never happened for a real person
        "narrative_simulated",  # a branch variant of a fictional character
        # Compatibility name still emitted by ContinuationService and stored
        # in existing local databases. It has the same branch-only visibility.
        "counterfactual_host_artifact",
    }
)

OPERATIONAL_KINDS: frozenset[str] = frozenset(
    {
        "user_correction",  # operator explicitly corrected the record
        "digital_experience",  # memory of an actual runtime interaction
    }
)

ALL_PROVENANCE_KINDS: frozenset[str] = (
    HISTORICAL_KINDS | FICTIONAL_KINDS | SIMULATED_KINDS | OPERATIONAL_KINDS
)

#: Branch ids that represent the persona itself rather than a divergence.
BASE_BRANCH_IDS: frozenset[str] = frozenset({"", "main", "shared_pre_divergence"})


def is_known_provenance(kind: str) -> bool:
    return str(kind) in ALL_PROVENANCE_KINDS


# ---------------------------------------------------------------------------
# Material scope
# ---------------------------------------------------------------------------

CHARACTER_VISIBLE = "character_visible"
AUTHOR_ONLY = "author_only"
EVALUATION_ONLY = "evaluation_only"

MATERIAL_SCOPES: frozenset[str] = frozenset(
    {CHARACTER_VISIBLE, AUTHOR_ONLY, EVALUATION_ONLY}
)

#: Scopes that may never reach an actor's runtime context.
NON_CHARACTER_SCOPES: frozenset[str] = frozenset({AUTHOR_ONLY, EVALUATION_ONLY})

DEFAULT_MATERIAL_SCOPE = CHARACTER_VISIBLE


def normalise_material_scope(value: Any) -> str:
    """Default closed: an unrecognised scope is treated as author-only.

    Failing closed is deliberate.  A mislabelled scope that leaks a story secret
    into a character prompt is far worse than one that hides a harmless fact,
    and the latter shows up as an obvious coverage gap instead of a silent leak.
    """
    scope = str(value or "").strip().lower()
    return scope if scope in MATERIAL_SCOPES else AUTHOR_ONLY


# ---------------------------------------------------------------------------
# Retrieval policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PersonaRetrievalPolicy:
    """Decide which provenance families a persona may read.

    The rule is driven by the persona's nature and by whether the caller is
    reading the base persona or one of its simulated branches:

    * real / historical persona  -> historical evidence (+ branch simulation)
    * fictional persona          -> author-defined canon (+ branch simulation)
    * branch of either           -> the base identity plus branch-local simulation

    A fictional persona never reads historical kinds and a real persona never
    reads fictional kinds, so canon can never be mistaken for documented fact.
    """

    persona_type: PersonaType
    branch_id: str | None = None

    @property
    def is_branch(self) -> bool:
        return str(self.branch_id or "main") not in BASE_BRANCH_IDS

    @property
    def _identity_kinds(self) -> frozenset[str]:
        if self.persona_type is PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON:
            return FICTIONAL_KINDS
        return HISTORICAL_KINDS

    def allowed_source_kinds(self) -> frozenset[str]:
        kinds = set(self._identity_kinds) | set(OPERATIONAL_KINDS)
        if self.is_branch:
            kinds |= set(SIMULATED_KINDS)
        return frozenset(kinds)

    def allows(self, source_kind: str) -> bool:
        return str(source_kind) in self.allowed_source_kinds()

    def rejected_source_kinds(self, source_kind: str) -> str:
        """Human-readable reason a kind was filtered out (for audit trails)."""
        kind = str(source_kind)
        if kind in self.allowed_source_kinds():
            return ""
        if not is_known_provenance(kind):
            return f"unknown provenance kind: {kind}"
        return f"{kind} not visible to {self.persona_type.value} on branch {self.branch_id}"


def normalise_fictional_provenance(artifact: dict[str, Any]) -> bool:
    """Force a fictional artifact's evidence into the fictional provenance family.

    Applied wherever a fictional artifact is submitted or restored, so the
    boundary holds across retries instead of depending on what the research
    model happened to emit.

    The rule is *not* "fictional becomes historical" -- that would break the
    invariant that simulated content must never be presented as documented
    fact.  It is also not "fictional becomes counterfactual_simulated", which
    was the previous behaviour and made the entire biography invisible to the
    persona because retrieval only admits the persona's own provenance family.

    Returns True when anything was rewritten.
    """
    rewritten = False
    for claim in artifact.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        kind = str(claim.get("claim_type") or "")
        if kind in FICTIONAL_KINDS:
            continue
        claim["claim_type"] = (
            "fictional_canon" if kind == "digital_experience" else "fictional_author_defined"
        )
        rewritten = True
    for memory in artifact.get("memories") or []:
        if not isinstance(memory, dict):
            continue
        kind = str(memory.get("source_kind") or "")
        if kind in FICTIONAL_KINDS:
            continue
        memory["source_kind"] = (
            "fictional_canon" if kind == "digital_experience" else "fictional_author_defined"
        )
        rewritten = True
    return rewritten


def filter_by_policy(
    rows: Iterable[Any],
    policy: PersonaRetrievalPolicy,
    *,
    kind_of: Any,
    scope_of: Any,
) -> list[Any]:
    """Keep rows whose provenance and material scope the policy admits."""
    kept: list[Any] = []
    for row in rows:
        if normalise_material_scope(scope_of(row)) in NON_CHARACTER_SCOPES:
            continue
        if not policy.allows(kind_of(row)):
            continue
        kept.append(row)
    return kept


__all__ = [
    "ALL_PROVENANCE_KINDS",
    "AUTHOR_ONLY",
    "BASE_BRANCH_IDS",
    "CHARACTER_VISIBLE",
    "DEFAULT_MATERIAL_SCOPE",
    "EVALUATION_ONLY",
    "FICTIONAL_KINDS",
    "HISTORICAL_KINDS",
    "MATERIAL_SCOPES",
    "NON_CHARACTER_SCOPES",
    "OPERATIONAL_KINDS",
    "SIMULATED_KINDS",
    "PersonaRetrievalPolicy",
    "filter_by_policy",
    "is_known_provenance",
    "normalise_fictional_provenance",
    "normalise_material_scope",
]
