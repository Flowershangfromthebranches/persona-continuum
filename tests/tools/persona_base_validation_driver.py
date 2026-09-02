"""Drive a scripted Base Persona test through the production runtime.

Every turn goes through ``prepare_turn`` -> host-authored reply ->
``commit_turn``, so affect / needs / relationship state actually accumulates
instead of resetting, which is what rounds 3 and 4 measure.

Input:  a JSON file describing rounds and turns (see ``--script``).
Output: a JSON file with the transcript plus the state trajectory after each
        turn (see ``--out``).

Usage:
    .venv/bin/python scripts/persona_stress_driver.py \
        --script <script.json> --out <result.json>
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from persona_continuum.application.container import PersonaContinuum

TRACKED_EMOTIONS = ["fear", "anxiety", "anger", "frustration", "sadness", "shame", "guilt"]
TRACKED_NEEDS = ["safety", "control", "autonomy", "being_understood"]


def reset_state(app: PersonaContinuum, persona_id: str, branch_id: str) -> None:
    """Zero the runtime baseline so one round cannot contaminate the next.

    Continuation branches must exist before they can be selected, so rounds
    share ``main`` and the driver resets it instead of forking fake branches.
    """
    conn = app.database.conn
    conn.execute(
        "UPDATE affect_states SET intensity = baseline, triggers_json = '[]' "
        "WHERE persona_id = ? AND branch_id = ?",
        (persona_id, branch_id),
    )
    conn.execute(
        "UPDATE needs SET level = baseline, reasons_json = '[]' "
        "WHERE persona_id = ? AND branch_id = ?",
        (persona_id, branch_id),
    )
    conn.execute(
        "DELETE FROM relationships WHERE persona_id = ? AND branch_id = ?",
        (persona_id, branch_id),
    )
    conn.commit()


def snapshot(app: PersonaContinuum, persona_id: str, branch_id: str) -> dict[str, Any]:
    emotions = {
        state.name: round(state.intensity, 3)
        for state in app.affect.get_emotions(persona_id, branch_id)
    }
    needs = {
        state.name: round(state.level, 3)
        for state in app.motivation.get_needs(persona_id, branch_id)
    }
    return {
        "emotions": {name: emotions.get(name, 0.0) for name in TRACKED_EMOTIONS},
        "needs": {name: needs.get(name, 0.5) for name in TRACKED_NEEDS},
    }


def run(app: PersonaContinuum, persona_id: str, script: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"persona_id": persona_id, "rounds": []}

    for round_spec in script["rounds"]:
        counterpart = round_spec.get("counterpart", "user")
        session = app.sessions.start_session(
            persona_id, title=round_spec["title"], counterpart_id=counterpart
        )
        session_id = session["session_id"] if isinstance(session, dict) else session.id
        branch_id = round_spec.get("branch_id", "main")
        # Continuation branches only exist once registered, so rounds share
        # `main`; reset it instead so round N's emotion cannot leak into N+1.
        if round_spec.get("reset_state", True):
            reset_state(app, persona_id, branch_id)

        # Relationship values are written absolutely (not as deltas), so seed
        # the canonical EP01 baseline for this counterpart before turn 1.
        if initial := round_spec.get("initial_relationship"):
            app.relationships.update_relationship(
                persona_id, counterpart, initial, "EP01 canonical baseline", branch_id
            )

        round_result: dict[str, Any] = {
            "round": round_spec["id"],
            "title": round_spec["title"],
            "counterpart": counterpart,
            "session_id": session_id,
            "turns": [],
        }

        for turn in round_spec["turns"]:
            prepared = app.sessions.prepare_turn(
                persona_id,
                session_id,
                turn["prompt"],
                external_events=turn.get("external_events"),
                counterpart_id=counterpart,
                branch_id=branch_id,
                max_context_items=12,
            )
            before = snapshot(app, persona_id, branch_id)
            relationship_before = app.relationships.get_relationship(
                persona_id, counterpart, branch_id=branch_id
            )

            app.sessions.commit_turn(
                persona_id,
                session_id,
                user_message=turn["prompt"],
                persona_response=turn["response"],
                state_patch=turn.get("state_patch"),
                counterpart_id=counterpart,
            )

            after = snapshot(app, persona_id, branch_id)
            relationship_after = app.relationships.get_relationship(
                persona_id, counterpart, branch_id=branch_id
            )

            round_result["turns"].append(
                {
                    "id": turn["id"],
                    "prompt": turn["prompt"],
                    "response": turn["response"],
                    "scripted_stimulus": turn.get("state_patch"),
                    "external_events": turn.get("external_events"),
                    "mood_before": prepared.current_mood,
                    "appraisal": prepared.event_appraisal,
                    "state_before": before,
                    "state_after": after,
                    "relationship": {
                        "trust_before": round(relationship_before.trust, 3),
                        "trust_after": round(relationship_after.trust, 3),
                        "familiarity_after": round(relationship_after.familiarity, 3),
                        "resentment_after": round(relationship_after.resentment, 3),
                    },
                }
            )

        app.sessions.end_session(session_id)
        result["rounds"].append(round_result)

    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    with open(args.script, encoding="utf-8") as handle:
        script = json.load(handle)

    app = PersonaContinuum()
    app.personas.activate(script["persona_id"])
    result = run(app, script["persona_id"], script)

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
