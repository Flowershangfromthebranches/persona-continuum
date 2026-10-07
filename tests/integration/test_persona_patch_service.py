from __future__ import annotations

import json
from pathlib import Path

from persona_continuum.application._utils import dumps, new_id
from persona_continuum.application.persona_patch_service import PersonaPatch
from persona_continuum.domain.persona import PersonaType, RunMode, utc_now
from persona_continuum.runtime.core_fidelity import behavioral_implications, core_traits

FIXTURE = Path(__file__).parents[1] / "fixtures/core_fidelity_adult.json"


def _seed_compiled(app, persona_id: str, components: dict) -> None:
    merged, _ = app.compilation.merge_components(
        [{"dimension": "expression_dna", "extracted_components": components}]
    )
    for key, content in merged.items():
        app.database.conn.execute(
            "INSERT INTO compiled_components VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("comp"),
                persona_id,
                1,
                "persona_component",
                key,
                dumps(content),
                "[]",
                utc_now().isoformat(),
            ),
        )
    app.database.conn.commit()


def test_persona_patch_bumps_version_and_preserves_lived_state(app) -> None:
    components = json.loads(FIXTURE.read_text())
    persona_id = "patch-adult"
    app.personas.create(
        display_name="苏禾校准",
        aliases=[],
        persona_id=persona_id,
        persona_type=PersonaType.FICTIONAL_OR_SYNTHETIC_PERSON,
        run_mode=RunMode.DIGITAL_CONTINUATION,
    )
    _seed_compiled(app, persona_id, components)
    session = app.sessions.start_session(
        persona_id, initial_relationship={"relationship_kind": "friend"}
    )
    app.sessions.prepare_turn(persona_id, session.id, "今天作业好烦")
    app.sessions.commit_turn(
        persona_id, session.id, user_message="今天作业好烦", persona_response="先喝奶茶。"
    )
    intimacy = next(n for n in app.motivation.get_needs(persona_id) if n.name == "intimacy")
    stored = app.database.conn.execute(
        "SELECT level, baseline FROM needs WHERE persona_id=? AND name='intimacy'",
        (persona_id,),
    ).fetchone()
    stored_touch = app.database.conn.execute(
        "SELECT level, baseline FROM needs WHERE persona_id=? AND name='touch_closeness'",
        (persona_id,),
    ).fetchone()
    affect = app.database.conn.execute(
        "SELECT intensity FROM affect_states WHERE persona_id=? AND name='affection'",
        (persona_id,),
    ).fetchone()
    rel = app.relationships.get_relationship(persona_id, "user")
    memory_count = app.database.conn.execute(
        "SELECT COUNT(*) AS n FROM memories WHERE persona_id=?", (persona_id,)
    ).fetchone()["n"]

    patch = PersonaPatch.model_validate(
        {
            "persona_id": persona_id,
            "base_compile_version": 1,
            "base_manifest_version": app.personas.get(persona_id).manifest.version,
            "target_manifest_version": "0.1.1",
            "patch_reason": "author_directed_core_trait_calibration",
            "component_patches": [
                {
                    "component_key": "dominant_traits",
                    "content": [
                        {
                            "trait": "exceptionally_high_sex_drive",
                            "strength": 0.97,
                            "stability": "stable",
                            "behavioral_implications": ["性欲是持续驱动力"],
                        }
                    ],
                },
                {
                    "component_key": "expression_style",
                    "action": "merge",
                    "content": {"desire_shame": "low", "playful_provocation": "very_high"},
                },
            ],
            "runtime_baseline_patches": [
                {
                    "name": "touch_closeness",
                    "baseline": 0.9,
                    "rebound_rate": 0.4,
                    "preserve_current_level": True,
                }
            ],
            "memory_corrections": [
                {
                    "content": "对稳定成年伴侣的普通成人调情，我不应默认解释为越界。",
                    "reason": "author_directed_canonical_correction",
                }
            ],
            "relationship_corrections": [
                {
                    "counterpart": "user",
                    "fields": {
                        "relationship_kind": "partner",
                        "bond_stage": "established",
                        "relationship_prior": 0.55,
                    },
                    "reason": "lived couple classification lag",
                }
            ],
        }
    )
    preview = app.persona_patches.preview(patch)
    assert preview["current_compile_version"] == 1
    assert "exceptionally_high_sex_drive" in str(preview["trait_changes"])
    result = app.persona_patches.apply(patch)
    assert result["ok"] is True
    updated = app.personas.get(persona_id)
    assert updated.id == persona_id
    assert updated.manifest.version == "0.1.1"
    traits = core_traits(app.compiled_context.prepare_context(persona_id, "")["core_components"])
    assert traits[0].trait == "exceptionally_high_sex_drive"
    assert traits[0].strength == 0.97
    after_intimacy = app.database.conn.execute(
        "SELECT level, baseline FROM needs WHERE persona_id=? AND name='intimacy'",
        (persona_id,),
    ).fetchone()
    after_touch = app.database.conn.execute(
        "SELECT level, baseline FROM needs WHERE persona_id=? AND name='touch_closeness'",
        (persona_id,),
    ).fetchone()
    after_affect = app.database.conn.execute(
        "SELECT intensity FROM affect_states WHERE persona_id=? AND name='affection'",
        (persona_id,),
    ).fetchone()
    after_rel = app.relationships.get_relationship(persona_id, "user")
    after_memories = app.database.conn.execute(
        "SELECT COUNT(*) AS n FROM memories WHERE persona_id=?", (persona_id,)
    ).fetchone()["n"]
    assert after_intimacy["level"] == stored["level"]
    assert after_intimacy["baseline"] == stored["baseline"]
    assert after_touch["baseline"] == 0.9
    assert after_touch["level"] == stored_touch["level"]
    assert after_affect["intensity"] == affect["intensity"]
    assert after_rel.trust == rel.trust
    assert after_rel.affection == rel.affection
    assert after_rel.relationship_kind == "partner"
    assert after_rel.bond_stage == "established"
    assert after_memories == memory_count + 1
    assert result["continuity"]["persona_id_same"] is True
    assert result["continuity"]["affect_preserved"] is True
    implications = behavioral_implications(
        app.compiled_context.prepare_context(persona_id, "")["core_components"],
        [intimacy],
        after_rel,
    )
    assert "naturally initiate affectionate teasing" in " ".join(implications)
    assert session.id in {
        row["id"]
        for row in app.database.conn.execute(
            "SELECT id FROM sessions WHERE persona_id=?", (persona_id,)
        )
    }
