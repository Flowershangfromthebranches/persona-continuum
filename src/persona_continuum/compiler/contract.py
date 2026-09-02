"""Versioned contract between ResearchArtifact output and Compiled Persona.

The bug this replaces
---------------------
``CompilationService._merge_components`` kept only keys that already matched the
compiled schema and did::

    for key, value in components.items():
        if key not in defaults:
            continue          # silent drop

Research agents emit *evidence-shaped* keys (``decision_style``,
``default_tone``, ``defense_mechanisms``) while the compiled persona wants
*schema-shaped* keys (``decision_heuristics``, ``expression_style``,
``defenses``).  Nothing bridged the two, so 15 of 18 components came out empty
with no error anywhere.

The contract
------------
1. :data:`COMPILE_CONTRACT` declares the 18 canonical components, their value
   shape, and whether they are core or optional.
2. :data:`KEY_MAP` declares a **generic semantic bridge** from evidence-shaped
   keys to schema slots.  It is generic in the sense that matters: it encodes
   the relationship between two vocabularies, not facts about any one persona,
   so it applies to every persona compiled by this system.
3. :func:`normalize_artifact` applies the bridge and returns an explicit record
   of what mapped, what was aliased and what was left unused.  **Nothing is
   dropped silently** -- an unmapped key becomes an ``unused_artifact_fields``
   entry that surfaces in the coverage report.
4. :class:`CompileCoverage` turns that record into the per-compile report.

A gap is a legitimate outcome.  ``temperament = gap`` is correct when no
evidence supports it; inventing content to reach 18/18 is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

CONTRACT_VERSION = "1.0"

ValueShape = Literal["list", "dict"]

# ---------------------------------------------------------------------------
# 1. The canonical component schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComponentSpec:
    key: str
    shape: ValueShape
    core: bool  # core components must be present for a persona to be usable
    note: str = ""


def _spec(key: str, shape: ValueShape, core: bool, note: str = "") -> ComponentSpec:
    return ComponentSpec(key=key, shape=shape, core=core, note=note)


COMPILE_CONTRACT: dict[str, ComponentSpec] = {
    # identity & timeline
    "identity_profile": _spec("identity_profile", "dict", True, "who the persona is"),
    "timeline_events": _spec("timeline_events", "list", True, "dated life events"),
    "self_narrative_evidence": _spec(
        "self_narrative_evidence", "list", False, "how they talk about themselves"
    ),
    # cognition
    "mental_models": _spec("mental_models", "list", True, "beliefs about how things work"),
    "decision_heuristics": _spec(
        "decision_heuristics", "list", True, "how they choose under uncertainty"
    ),
    "values": _spec("values", "list", True, "what they hold to"),
    "contradictions": _spec("contradictions", "list", False, "internal tensions"),
    "failure_patterns": _spec(
        "failure_patterns", "list", False, "recurring ways they go wrong"
    ),
    # affect
    "temperament": _spec("temperament", "dict", False, "baseline emotional makeup"),
    "emotional_triggers": _spec("emotional_triggers", "list", False, "what sets them off"),
    "attachment_patterns": _spec(
        "attachment_patterns", "dict", False, "how they bond and hold boundaries"
    ),
    "needs_and_desires": _spec("needs_and_desires", "list", False, "what they are after"),
    "defenses": _spec("defenses", "list", False, "how they protect themselves"),
    # expression
    "expression_style": _spec("expression_style", "dict", True, "how they sound"),
    "vocabulary": _spec("vocabulary", "list", False, "characteristic phrasing"),
    "dialogue_examples": _spec("dialogue_examples", "list", False, "attested utterances"),
    "anti_patterns": _spec("anti_patterns", "list", False, "how they must never sound"),
    # relationships
    "relationships": _spec("relationships", "list", True, "standing ties to others"),
}

CORE_COMPONENTS: frozenset[str] = frozenset(
    key for key, spec in COMPILE_CONTRACT.items() if spec.core
)

# ---------------------------------------------------------------------------
# 2. The semantic bridge
# ---------------------------------------------------------------------------

BridgingMode = Literal["append", "nest"]


@dataclass(frozen=True)
class MappingRule:
    """How one evidence-shaped key lands in the compiled schema.

    ``label`` is prepended when a value is flattened into a list, so the
    original evidence name survives the trip and nothing becomes anonymous.
    """

    target: str
    mode: BridgingMode = "append"
    label: str | None = None
    note: str = ""


def _r(target: str, mode: BridgingMode = "append", label: str | None = None) -> MappingRule:
    return MappingRule(target=target, mode=mode, label=label)


#: Evidence-shaped key -> schema slot.  Every entry is a vocabulary mapping and
#: therefore persona-independent.  Keys emitted verbatim by a conforming
#: producer are present too, so a producer that already speaks the schema
#: passes through unchanged.
KEY_MAP: dict[str, MappingRule] = {
    # -- identity ----------------------------------------------------------
    "identity_profile": _r("identity_profile", "nest"),
    "core_invariants": _r("identity_profile", "nest", "core_invariants"),
    "life_stages": _r("identity_profile", "nest", "life_stages"),
    "entities": _r("identity_profile", "nest", "entities"),
    "negative_evidence": _r("identity_profile", "nest", "negative_evidence"),
    "persona_type": _r("identity_profile", "nest", "persona_type"),
    "authenticity_label": _r("identity_profile", "nest", "authenticity_label"),
    "third_party_assessments": _r("identity_profile", "nest", "third_party_assessments"),
    "self_narrative_evidence": _r("self_narrative_evidence"),
    "timeline": _r("timeline_events"),
    "timeline_events": _r("timeline_events"),
    "chronological_behavior_trace": _r("timeline_events", "append", "behavior_trace"),
    # -- cognition ---------------------------------------------------------
    "mental_models": _r("mental_models"),
    "beliefs": _r("mental_models", "append", "belief"),
    "views_on_ai": _r("mental_models", "append", "view"),
    "technical_knowledge": _r("mental_models", "nest", "technical_knowledge"),
    "ai_usage": _r("mental_models", "nest", "ai_usage"),
    "decision_heuristics": _r("decision_heuristics"),
    "decision_style": _r("decision_heuristics", "append", "decision_style"),
    "uncertainty_response_stages": _r("decision_heuristics", "append", "uncertainty_stage"),
    "risk_preparation_behaviors": _r("decision_heuristics", "append", "risk_preparation"),
    "behavioral_boundary_rules": _r("decision_heuristics", "append", "boundary_rule"),
    "hypothetical_gray_behaviors_under_survival_pressure": _r(
        "decision_heuristics", "append", "survival_gray_behavior"
    ),
    "failure_patterns": _r("failure_patterns"),
    # -- values ------------------------------------------------------------
    "values": _r("values"),
    "moral_boundaries": _r("values", "append", "moral_boundary"),
    "contradictions": _r("contradictions"),
    "needs_and_desires": _r("needs_and_desires"),
    "desires_and_motivations": _r("needs_and_desires", "append", "desire"),
    # -- affect ------------------------------------------------------------
    "temperament": _r("temperament", "nest"),
    "stress_response_ladder": _r("temperament", "nest", "stress_response_ladder"),
    "emotional_triggers": _r("emotional_triggers"),
    "attachment_patterns": _r("attachment_patterns", "nest"),
    "attachment_and_boundaries": _r("attachment_patterns", "nest"),
    "defenses": _r("defenses"),
    "defense_mechanisms": _r("defenses", "append", "defense"),
    # -- expression --------------------------------------------------------
    "expression_style": _r("expression_style", "nest"),
    "default_tone": _r("expression_style", "nest", "default_tone"),
    "emotion_expression_map": _r("expression_style", "nest", "emotion_expression_map"),
    "body_language": _r("expression_style", "nest", "body_language"),
    "dialogue_escalation_style": _r("expression_style", "nest", "dialogue_escalation_style"),
    "communication_patterns": _r("expression_style", "nest", "communication_patterns"),
    "vocabulary": _r("vocabulary"),
    "dialogue_examples": _r("dialogue_examples"),
    "sample_utterances": _r("dialogue_examples", "append", "utterance"),
    "quotes": _r("dialogue_examples", "append", "quote"),
    "anti_patterns": _r("anti_patterns"),
    "excluded_styles": _r("anti_patterns", "append", "excluded_style"),
    # -- relationships -----------------------------------------------------
    "relationships": _r("relationships"),
    "relationship_inventory": _r("relationships", "append", "relationship"),
}

# ---------------------------------------------------------------------------
# 3. Normalization
# ---------------------------------------------------------------------------


def _flatten(value: Any, label: str | None) -> list[Any]:
    """Turn any evidence value into list entries, prefixing the source name."""
    if isinstance(value, dict):
        out: list[Any] = []
        for key, item in value.items():
            stem = f"{label}.{key}" if label else str(key)
            if isinstance(item, (list, tuple)):
                out.extend(f"{stem}: {entry}" for entry in item)
            else:
                out.append(f"{stem}: {item}")
        return out
    if isinstance(value, (list, tuple)):
        return [f"{label}: {entry}" if label else entry for entry in value]
    return [f"{label}: {value}" if label else value]


@dataclass
class NormalizationRecord:
    """What happened to one artifact's ``extracted_components``."""

    dimension: str
    mapped: dict[str, list[Any]] = field(default_factory=dict)
    aliases_used: list[str] = field(default_factory=list)
    unused: list[str] = field(default_factory=list)

    @property
    def total_fields(self) -> int:
        return len(self.aliases_used) + len(self.unused)

    @property
    def mapped_field_count(self) -> int:
        return len(self.aliases_used)


#: Sub-fields that state what the character must NOT know.  They are boundary
#: specifications, not beliefs, and compiling them would hand the character the
#: very term the boundary exists to withhold.
BOUNDARY_SUBFIELDS: frozenset[str] = frozenset({"does_not_know", "must_not_know"})


def _strip_boundary(value: Any) -> Any:
    """Drop does_not_know / must_not_know members from a nested value."""
    if isinstance(value, dict):
        return {
            key: _strip_boundary(item)
            for key, item in value.items()
            if str(key).lower() not in BOUNDARY_SUBFIELDS
        }
    if isinstance(value, list):
        return [_strip_boundary(item) for item in value]
    return value


def normalize_artifact(dimension: str, components: dict[str, Any]) -> NormalizationRecord:
    """Apply the bridge, recording every decision instead of dropping anything."""
    record = NormalizationRecord(dimension=dimension)
    for key, value in (components or {}).items():
        rule = KEY_MAP.get(key)
        if rule is None:
            record.unused.append(f"{dimension}.{key}")
            continue
        record.aliases_used.append(f"{dimension}.{key}")
        if rule.mode == "nest" and isinstance(value, dict):
            stripped = _strip_boundary(value)
            if stripped != value:
                record.unused.append(f"{dimension}.{key}#boundary")
            value = stripped
            if not value:
                continue
        bucket = record.mapped.setdefault(rule.target, [])
        # Provenance is carried by ``aliases_used`` (and by the coverage
        # report / lineage rows), not by prefixing every value string with its
        # source key -- the compiled persona should stay readable.
        label = rule.label or key
        if rule.mode == "nest":
            target_shape = COMPILE_CONTRACT[rule.target].shape
            if target_shape == "dict":
                bucket.append((label, value))
            else:
                bucket.extend(_flatten(value, label))
        else:
            bucket.extend(_flatten(value, rule.label))
    return record


# ---------------------------------------------------------------------------
# 4. Coverage
# ---------------------------------------------------------------------------


@dataclass
class CompileCoverage:
    """Per-compile answer to "did the research actually land?"."""

    contract_version: str = CONTRACT_VERSION
    artifact_fields_total: int = 0
    artifact_fields_mapped: int = 0
    unused_artifact_fields: list[str] = field(default_factory=list)
    filled_components: list[str] = field(default_factory=list)
    missing_core_components: list[str] = field(default_factory=list)
    missing_optional_components: list[str] = field(default_factory=list)

    @property
    def artifact_field_coverage(self) -> float:
        if self.artifact_fields_total == 0:
            return 0.0
        return round(self.artifact_fields_mapped / self.artifact_fields_total, 4)

    @property
    def compiled_component_coverage(self) -> float:
        return round(len(self.filled_components) / len(COMPILE_CONTRACT), 4)

    @property
    def ok(self) -> bool:
        """Usable means every core component landed, not that nothing is missing."""
        return not self.missing_core_components

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "artifact_field_coverage": self.artifact_field_coverage,
            "compiled_component_coverage": self.compiled_component_coverage,
            "artifact_fields_total": self.artifact_fields_total,
            "artifact_fields_mapped": self.artifact_fields_mapped,
            "unused_artifact_fields": list(self.unused_artifact_fields),
            "filled_components": list(self.filled_components),
            "missing_core_components": list(self.missing_core_components),
            "missing_optional_components": list(self.missing_optional_components),
            "ok": self.ok,
        }

    def render_table(self) -> str:
        lines = [f"PersonaCompileContract v{self.contract_version}"]
        for key in COMPILE_CONTRACT:
            state = "OK     " if key in self.filled_components else "GAP    "
            marker = "core" if key in CORE_COMPONENTS else "opt "
            lines.append(f"  {state} {marker}  {key}")
        lines.append(
            f"  artifact fields {self.artifact_fields_mapped}/{self.artifact_fields_total}"
            f"  components {len(self.filled_components)}/{len(COMPILE_CONTRACT)}"
        )
        if self.unused_artifact_fields:
            lines.append("  unused artifact fields:")
            lines.extend(f"    - {name}" for name in self.unused_artifact_fields)
        return "\n".join(lines)


def build_coverage(
    records: list[NormalizationRecord], merged: dict[str, Any]
) -> CompileCoverage:
    coverage = CompileCoverage()
    for record in records:
        coverage.artifact_fields_total += record.total_fields
        coverage.artifact_fields_mapped += record.mapped_field_count
        coverage.unused_artifact_fields.extend(record.unused)
    for key, spec in COMPILE_CONTRACT.items():
        value = merged.get(key)
        filled = bool(value) if spec.shape == "dict" else bool(value)
        if filled:
            coverage.filled_components.append(key)
        elif spec.core:
            coverage.missing_core_components.append(key)
        else:
            coverage.missing_optional_components.append(key)
    return coverage


__all__ = [
    "COMPILE_CONTRACT",
    "CONTRACT_VERSION",
    "CORE_COMPONENTS",
    "ComponentSpec",
    "CompileCoverage",
    "KEY_MAP",
    "MappingRule",
    "NormalizationRecord",
    "build_coverage",
    "normalize_artifact",
]
