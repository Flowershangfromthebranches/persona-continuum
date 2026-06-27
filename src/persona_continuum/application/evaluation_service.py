from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from persona_continuum.application._utils import dumps, loads, new_id
from persona_continuum.application.persona_service import PersonaService
from persona_continuum.security.validation import CodedError
from persona_continuum.storage.database import Database

BENCHMARK_DIMENSIONS = [
    "factual_qa",
    "known_decision_replay",
    "conflict_handling",
    "refuse_fabrication_when_insufficient",
    "unknown_modern_question",
    "expression_style",
    "multi_turn_stability",
    "affect_continuity",
    "relationship_continuity",
    "memory_recall",
    "persona_jailbreak_resistance",
    "historical_counterfactual_separation",
]


class EvaluationCaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: str
    prompt: str = Field(min_length=1)
    expected_behavior: str = Field(min_length=1)
    grading_rubric: dict[str, Any]
    required_evidence: list[Any]

    @field_validator("dimension")
    @classmethod
    def _dimension(cls, value: str) -> str:
        if value not in BENCHMARK_DIMENSIONS:
            raise ValueError("invalid_evaluation_dimension")
        return value


class EvaluationResultInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1)
    scores: dict[str, float] = Field(min_length=1)
    evidence: list[Any]
    failure_modes: list[Any]
    confidence: float = Field(ge=0, le=1)
    version: str

    @field_validator("scores")
    @classmethod
    def _scores(cls, value: dict[str, float]) -> dict[str, float]:
        unknown = set(value) - set(BENCHMARK_DIMENSIONS)
        if unknown:
            raise ValueError(f"invalid_evaluation_scores:{','.join(sorted(unknown))}")
        for score in value.values():
            if score < 0 or score > 1:
                raise ValueError("score_out_of_range")
        return value


class EvaluationService:
    def __init__(self, database: Database, personas: PersonaService) -> None:
        self.database = database
        self.personas = personas

    def create_suite(
        self, persona_id: str, name: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.personas.get(persona_id)
        now = datetime.now(UTC).isoformat()
        suite = {
            "id": new_id("suite"),
            "persona_id": persona_id,
            "name": name,
            "dimensions": BENCHMARK_DIMENSIONS,
            "metadata": metadata or {},
            "created_at": now,
            "updated_at": now,
        }
        self.database.conn.execute(
            "INSERT INTO evaluation_suites VALUES (?, ?, ?, ?, ?, ?)",
            (suite["id"], persona_id, name, dumps(suite["metadata"]), now, now),
        )
        self.database.conn.commit()
        return suite

    def add_case(self, suite_id: str, case: dict[str, Any]) -> dict[str, Any]:
        suite = self._get_suite(suite_id)
        try:
            validated = EvaluationCaseInput.model_validate(case)
        except ValidationError as exc:
            raise CodedError("invalid_evaluation_case", str(exc)) from exc
        case_data = validated.model_dump(mode="json")
        now = datetime.now(UTC).isoformat()
        record = {
            "id": new_id("case"),
            "suite_id": suite_id,
            "persona_id": suite["persona_id"],
            "case": case_data,
            "created_at": now,
        }
        self.database.conn.execute(
            "INSERT INTO evaluation_cases VALUES (?, ?, ?, ?, ?)",
            (record["id"], suite_id, suite["persona_id"], dumps(case_data), now),
        )
        self.database.conn.commit()
        return record

    def prepare_case(self, case_id: str) -> dict[str, Any]:
        row = self.database.conn.execute(
            "SELECT * FROM evaluation_cases WHERE id = ?", (case_id,)
        ).fetchone()
        if row is None:
            raise KeyError(case_id)
        case = dict(loads(row["case_json"]))
        persona = self.personas.get(str(row["persona_id"]))
        return {
            "case_id": case_id,
            "persona_id": persona.id,
            "persona_manifest": persona.manifest.model_dump(mode="json"),
            "case": case,
            "quality_report_type": "host_agent_benchmark",
            "output_schema": {
                "required": [
                    "answer",
                    "scores",
                    "evidence",
                    "failure_modes",
                    "confidence",
                    "version",
                ],
                "score_dimensions": BENCHMARK_DIMENSIONS,
            },
        }

    def commit_result(self, case_id: str, result: dict[str, Any]) -> dict[str, Any]:
        row = self.database.conn.execute(
            "SELECT persona_id, case_json FROM evaluation_cases WHERE id = ?", (case_id,)
        ).fetchone()
        if row is None:
            raise KeyError(case_id)
        try:
            validated = EvaluationResultInput.model_validate(result)
        except ValidationError as exc:
            raise CodedError("invalid_evaluation_result", str(exc)) from exc
        result_data = validated.model_dump(mode="json")
        case_data = dict(loads(row["case_json"]))
        dimension = str(case_data.get("dimension", ""))
        if dimension not in result_data["scores"]:
            raise CodedError("invalid_evaluation_result", f"missing_case_score:{dimension}")
        persona_id = str(row["persona_id"])
        if not self._version_exists(persona_id, result_data["version"]):
            raise CodedError(
                "invalid_evaluation_result", f"unknown_persona_version:{result_data['version']}"
            )
        duplicate = self.database.conn.execute(
            """
            SELECT 1 FROM evaluation_results
            WHERE case_id = ? AND persona_id = ? AND version = ?
            LIMIT 1
            """,
            (case_id, persona_id, result_data["version"]),
        ).fetchone()
        if duplicate is not None:
            raise CodedError("invalid_evaluation_result", "duplicate_case_version")
        now = datetime.now(UTC).isoformat()
        record = {
            "id": new_id("eval"),
            "case_id": case_id,
            "persona_id": persona_id,
            "version": result_data["version"],
            "result": result_data,
            "created_at": now,
        }
        self.database.conn.execute(
            "INSERT INTO evaluation_results VALUES (?, ?, ?, ?, ?, ?)",
            (
                record["id"],
                case_id,
                record["persona_id"],
                record["version"],
                dumps(result_data),
                now,
            ),
        )
        self.database.conn.execute(
            """
            INSERT OR IGNORE INTO lineage
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("lin"),
                record["persona_id"],
                "evaluation_result",
                record["id"],
                "evaluation_case",
                case_id,
                "evaluates",
                dumps({}),
                now,
            ),
        )
        self.database.conn.commit()
        return record

    def compare_versions(
        self, persona_id: str, version_a: str | None = None, version_b: str | None = None
    ) -> dict[str, Any]:
        self.personas.get(persona_id)
        rows = self.database.conn.execute(
            """
            SELECT version, result_json FROM evaluation_results
            WHERE persona_id = ?
              AND (? IS NULL OR version = ?)
              OR persona_id = ? AND (? IS NULL OR version = ?)
            """,
            (persona_id, version_a, version_a, persona_id, version_b, version_b),
        ).fetchall()
        by_version: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            version = str(row["version"] or "unversioned")
            by_version.setdefault(version, []).append(dict(loads(row["result_json"])))
        summary = {
            version: self._average_scores(results) for version, results in by_version.items()
        }
        return {
            "persona_id": persona_id,
            "structural_completeness_is_separate": True,
            "versions": summary,
        }

    def _get_suite(self, suite_id: str) -> dict[str, Any]:
        row = self.database.conn.execute(
            "SELECT * FROM evaluation_suites WHERE id = ?", (suite_id,)
        ).fetchone()
        if row is None:
            raise KeyError(suite_id)
        return {
            "id": str(row["id"]),
            "persona_id": str(row["persona_id"]),
            "name": str(row["name"]),
            "metadata": dict(loads(row["metadata_json"])),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _average_scores(self, results: list[dict[str, Any]]) -> dict[str, float]:
        totals: dict[str, list[float]] = {}
        for result in results:
            for key, value in dict(result.get("scores", {})).items():
                totals.setdefault(key, []).append(float(value))
        return {key: sum(values) / len(values) for key, values in totals.items() if values}

    def _version_exists(self, persona_id: str, version: str) -> bool:
        persona = self.personas.get(persona_id)
        if persona.manifest.version == version:
            return True
        rows = self.database.conn.execute(
            "SELECT manifest_json, version FROM compile_snapshots WHERE persona_id = ?",
            (persona_id,),
        ).fetchall()
        for row in rows:
            manifest = dict(loads(row["manifest_json"]))
            if str(manifest.get("version")) == version:
                return True
            if str(row["version"]) == version:
                return True
        return False


__all__ = ["BENCHMARK_DIMENSIONS", "EvaluationService"]
