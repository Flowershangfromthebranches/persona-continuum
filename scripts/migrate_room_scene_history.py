"""Run/rehearse the additive scene migration with immutable-state fingerprint checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from persona_continuum.storage.temporal_migration import migrate_temporal_history


def digest(conn: sqlite3.Connection, query: str) -> str:
    value = hashlib.sha256()
    for row in conn.execute(query):
        value.update(json.dumps(tuple(row), ensure_ascii=False, default=str).encode())
    return value.hexdigest()


def run(database: Path, receipt: Path) -> dict[str, object]:
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    protected = {
        "raw_transcript": (
            "SELECT id, room_id, turn_id, content, created_at "
            "FROM room_transcripts ORDER BY id"
        ),
        "session_turns": "SELECT * FROM session_turns ORDER BY id",
        "relationships": "SELECT * FROM relationships ORDER BY persona_id, branch_id, counterpart",
        "affect": "SELECT * FROM affect_states ORDER BY persona_id, branch_id, name",
        "needs": "SELECT * FROM needs ORDER BY persona_id, branch_id, name",
        "sessions": "SELECT * FROM sessions ORDER BY id",
        "personas": "SELECT * FROM personas ORDER BY id",
    }
    before = {name: digest(conn, query) for name, query in protected.items()}
    result = migrate_temporal_history(conn)
    after = {name: digest(conn, query) for name, query in protected.items()}
    if before != after:
        raise RuntimeError("protected_state_changed")
    result["protected_state_sha256"] = before
    result["protected_state_unchanged"] = True
    result["database"] = str(database)
    conn.close()
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "protected_state_sha256"}, ensure_ascii=False
        )
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    run(args.database, args.receipt)
