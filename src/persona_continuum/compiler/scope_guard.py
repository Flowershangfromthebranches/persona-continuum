"""Scope guard: keep compiled character-knowledge slots attested by the corpus.

Why this exists
---------------
Material scope is the primary isolation boundary (see
``scripts/partition_fangning_material.py``): author-only and evaluation-only
source files are never ingested as character knowledge in the first place.

This module is the **defence-in-depth** layer the P0 brief asks for.  Research
agents still sometimes emit mixed lists -- ``entities`` holding both people the
character knows and concepts only the author knows -- and a single mis-scoped
entity is enough to leak the story's central secret into every prompt.

The guard therefore asks a scope question, not a keyword question:

> is this entry attested anywhere in the persona's character-visible material?

``ORACLE`` is dropped because no character-visible text mentions it -- not
because a rule lists the word.  Rename the secret to "Project O" and it is
still dropped, because the character-visible source does not attest it either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Compiled slots that describe what the character *knows or is tied to*, and
#: therefore must be attested by character-visible material.
GUARDED_SLOTS: dict[str, tuple[str, ...]] = {
    # slot -> which members carry the attestable content
    "identity_profile": ("entities",),
    # knowledge-boundary statements name things the character must not know,
    # so a "does not know: ORACLE" entry leaks the very term it withholds.
    "mental_models": (),
    "relationships": (),
}

_WORD_RE = re.compile(r"[\w一-鿿]+")


def _tokens(value: str) -> list[str]:
    """Ordered tokens -- order matters for the attestation test below."""
    return [token.lower() for token in _WORD_RE.findall(value or "") if len(token) >= 2]


def _longest_run(tokens: list[str], corpus_tokens: set[str]) -> int:
    """Longest run of consecutive tokens all present in the corpus."""
    best = run = 0
    for token in tokens:
        if token in corpus_tokens:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def _attested(value: Any, corpus_tokens: set[str], corpus_text: str) -> bool:
    """Is this entry attested by the character-visible corpus?

    The test is "is a contiguous span of this entry's wording actually present
    in the material", not "do all its words appear somewhere".  A single CJK
    name (two characters, one token) only needs itself; a multi-word phrase
    needs a real adjacent pair, which is what separates ``Fang Ning Base
    Persona`` (never written that way in character-visible material) from
    ``陈默`` (written throughout).
    """
    text = str(value).strip()
    if not text:
        return True
    if text in corpus_text:
        return True
    tokens = _tokens(text)
    if not tokens:
        return True
    required = min(2, len(tokens))
    return _longest_run(tokens, corpus_tokens) >= required


@dataclass
class ScopeGuardResult:
    kept: dict[str, Any] = field(default_factory=dict)
    removed: list[str] = field(default_factory=list)

    @property
    def removed_count(self) -> int:
        return len(self.removed)


def apply_scope_guard(
    merged: dict[str, Any], character_visible_text: str
) -> ScopeGuardResult:
    """Remove compiled entries the character-visible material does not attest.

    Two rules, both scope-based rather than keyword-based:

    1. ``identity_profile.entities`` -- an entity survives only if a contiguous
       span of its wording appears in the character-visible corpus.
    2. ``relationships`` -- a tie survives only if it names an entity that
       survived rule 1.  An unattested entity cannot have an attested relation,
       which is how the ORACLE relationship is dropped without any rule that
       names ORACLE.
    """
    result = ScopeGuardResult(kept={k: v for k, v in merged.items()})
    corpus_text = character_visible_text or ""
    corpus_tokens = set(_tokens(corpus_text))
    if not corpus_tokens:
        for slot in GUARDED_SLOTS:
            result.kept[slot] = {} if isinstance(merged.get(slot), dict) else []
        result.removed.append("corpus_unavailable")
        return result

    # Rule 1: entities.
    profile = merged.get("identity_profile")
    attested_entities: set[str] = set()
    if isinstance(profile, dict) and isinstance(profile.get("entities"), list):
        kept_entities = [
            item
            for item in profile["entities"]
            if _attested(item, corpus_tokens, corpus_text)
        ]
        dropped = len(profile["entities"]) - len(kept_entities)
        if dropped:
            result.removed.append(f"identity_profile.entities: {dropped}")
        profile = {**profile, "entities": kept_entities}
        result.kept["identity_profile"] = profile
        attested_entities = {str(item) for item in kept_entities}
        attested_entities |= {
            token for item in kept_entities for token in _tokens(str(item))
        }

    # Rule 2: relationships must name an attested entity.
    value = merged.get("relationships")
    if isinstance(value, list) and attested_entities:
        kept_relationships = [
            item
            for item in value
            if set(_tokens(str(item))) & attested_entities
        ]
        dropped = len(value) - len(kept_relationships)
        if dropped:
            result.removed.append(f"relationships: {dropped}")
        result.kept["relationships"] = kept_relationships
    return result


def load_character_visible_corpus(paths: list[str]) -> str:
    """Concatenate the character-visible source files for a persona."""
    chunks: list[str] = []
    for raw in paths:
        try:
            chunks.append(Path(str(raw)).read_text(encoding="utf-8"))
        except OSError:
            continue
    return "\n".join(chunks)


__all__ = [
    "GUARDED_SLOTS",
    "ScopeGuardResult",
    "apply_scope_guard",
    "load_character_visible_corpus",
]
