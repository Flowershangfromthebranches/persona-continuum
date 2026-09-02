"""One parser, validator, and bounded repair path for Agent JSON output."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, TypeAdapter

from persona_continuum.agent.models import AgentTurn, StructuredOutputMode
from persona_continuum.agent.response_collector import (
    AgentStructuredOutputError,
    sanitize_diagnostic,
)


def schema_as_json(schema: Any) -> Any:
    if schema is None:
        return None
    if hasattr(schema, "model_json_schema"):
        return schema.model_json_schema()
    if isinstance(schema, dict):
        return schema
    try:
        return TypeAdapter(schema).json_schema()
    except Exception:
        return {"description": str(schema)}


class StructuredOutputParseError(AgentStructuredOutputError):
    code = "STRUCTURED_OUTPUT_PARSE_FAILED"


class StructuredOutputSchemaError(AgentStructuredOutputError):
    code = "STRUCTURED_OUTPUT_SCHEMA_FAILED"


class StructuredOutputRepairError(AgentStructuredOutputError):
    code = "STRUCTURED_OUTPUT_REPAIR_FAILED"


@dataclass(slots=True)
class StructuredResult:
    value: Any
    raw_text: str
    attempts: int = 1
    repair_attempted: bool = False
    diagnostics: dict[str, Any] = field(default_factory=dict)
    response: Any = None


class StructuredOutputEngine:
    """Validate native or prompt-only final text against one schema boundary."""

    def parse_and_validate(
        self,
        text: str,
        schema: Any,
        *,
        phase: str,
        adapter: str | None = None,
        protocol: str | None = None,
        structured_output_mode: StructuredOutputMode | str | None = None,
        repair_attempted: bool = False,
    ) -> StructuredResult:
        raw = str(text or "")
        diagnostics = self._diagnostics(
            raw,
            phase=phase,
            adapter=adapter,
            protocol=protocol,
            structured_output_mode=structured_output_mode,
            repair_attempted=repair_attempted,
        )
        try:
            value = self.parse_json(raw)
        except StructuredOutputParseError as exc:
            exc.diagnostics.update(diagnostics)
            raise
        try:
            validated = self.validate(value, schema)
        except StructuredOutputSchemaError as exc:
            exc.diagnostics.update(diagnostics)
            raise
        return StructuredResult(
            value=validated,
            raw_text=raw,
            attempts=1,
            repair_attempted=repair_attempted,
            diagnostics=diagnostics,
        )

    @staticmethod
    def parse_json(text: str) -> Any:
        """Parse fenced JSON, JSON surrounded by prose, objects, or arrays."""

        raw = str(text or "").strip()
        if not raw:
            raise StructuredOutputParseError(
                "Structured output is empty",
                diagnostics={"raw_chars": 0, "parser_failure": "empty_output"},
            )
        fenced = re.search(r"```(?:json|javascript|js)?\s*(.*?)\s*```", raw, re.I | re.S)
        candidate = fenced.group(1).strip() if fenced else raw
        decoder = json.JSONDecoder()
        starts = [0]
        starts.extend(index for index, char in enumerate(candidate) if char in "[{" and index)
        starts.extend(index for index, char in enumerate(candidate) if char in "ntf" and index)
        seen: set[int] = set()
        for start in starts:
            if start in seen:
                continue
            seen.add(start)
            try:
                value, _ = decoder.raw_decode(candidate, start)
                return value
            except (json.JSONDecodeError, TypeError):
                continue
        raise StructuredOutputParseError(
            "Structured output did not contain a parseable JSON value",
            diagnostics={
                "raw_chars": len(raw),
                "parser_failure": "json_value_not_found",
            },
        )

    @staticmethod
    def _strip_trailing_commas(candidate: str) -> str:
        """Remove ``,`` directly before ``}``/``]`` (outside string literals)."""
        text = candidate
        cleaned: list[str] = []
        in_string = False
        escaped = False
        length = len(text)
        for index, char in enumerate(text):
            if in_string:
                cleaned.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
                cleaned.append(char)
                continue
            if char == ",":
                lookahead = index + 1
                while lookahead < length and text[lookahead] in " \t\r\n":
                    lookahead += 1
                if lookahead < length and text[lookahead] in "}]":
                    continue
            cleaned.append(char)
        return "".join(cleaned)

    @staticmethod
    def _close_truncated_json(candidate: str) -> str:
        """Deterministically close truncated JSON (open strings and brackets).

        Truncation is a common CLI failure mode.  Closing is syntax-only: the
        result still has to pass schema validation, so a cut that loses a
        required field falls through to the model repair path.
        """
        stack: list[str] = []
        in_string = False
        escaped = False
        for char in candidate:
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char in "{[":
                stack.append(char)
            elif char in "}]" and stack:
                stack.pop()
        closing = ""
        if in_string:
            closing += '"'
        for open_char in reversed(stack):
            closing += "}" if open_char == "{" else "]"
        return candidate + closing

    @staticmethod
    def _parsable_prefix(text: str) -> bool:
        """Whether the text starts with a parseable JSON value."""
        try:
            json.loads(text)
            return True
        except json.JSONDecodeError:
            pass
        stripped = text.lstrip()
        decoder = json.JSONDecoder()
        for index, char in enumerate(stripped):
            if char not in "[{":
                continue
            try:
                decoder.raw_decode(stripped, index)
                return True
            except (json.JSONDecodeError, TypeError):
                continue
        return False

    @classmethod
    def local_repair(cls, text: str) -> str:
        """Deterministic, model-free repair of simple JSON syntax damage.

        Handles markdown fences, prose prefixes/suffixes, trailing commas,
        and truncated (unclosed) output.  Anything semantic is left to the
        bounded model repair path; the caller re-validates against the schema
        so a bad local fix can never enter the pipeline.
        """
        raw = str(text or "").strip()
        if not raw:
            return raw
        fenced = re.search(r"```(?:json|javascript|js)?\s*(.*?)\s*```", raw, re.I | re.S)
        candidate = fenced.group(1).strip() if fenced else raw
        # Crop prose around the outermost JSON value before other fixes.
        decoder = json.JSONDecoder()
        probe = candidate.lstrip()
        offset = len(candidate) - len(probe)
        for start_char in ("{", "["):
            index = candidate.find(start_char, offset)
            if index >= 0:
                try:
                    decoder.raw_decode(candidate, index)
                    cropped = candidate[index:]
                    break
                except (json.JSONDecodeError, TypeError):
                    continue
        else:
            cropped = candidate
        fixed = cls._strip_trailing_commas(cropped)
        if cls._parsable_prefix(fixed):
            return fixed
        closed = cls._close_truncated_json(fixed)
        if cls._parsable_prefix(closed):
            return closed
        return raw

    @staticmethod
    def validate(value: Any, schema: Any) -> Any:
        if schema is None:
            return value
        try:
            if isinstance(schema, type) and issubclass(schema, BaseModel):
                validated = schema.model_validate(value)
                return validated.model_dump(mode="json")
            if isinstance(schema, BaseModel):
                validated = schema.__class__.model_validate(value)
                return validated.model_dump(mode="json")
            if isinstance(schema, dict):
                StructuredOutputEngine._validate_json_schema(value, schema, path="$" )
                return value
            validated = TypeAdapter(schema).validate_python(value)
            if isinstance(validated, BaseModel):
                return validated.model_dump(mode="json")
            return validated
        except StructuredOutputSchemaError:
            raise
        except Exception as exc:
            raise StructuredOutputSchemaError(
                "Structured output failed schema validation",
                diagnostics={"schema_failure": sanitize_diagnostic(exc, limit=2000)},
            ) from exc

    @staticmethod
    def repair_turn(
        *,
        original_text: str,
        schema: Any,
        parser_failure: str,
        phase: str,
    ) -> AgentTurn:
        schema_text = json.dumps(schema_as_json(schema), ensure_ascii=False, sort_keys=True)
        return AgentTurn(
            user_message=(
                "Repair the following Agent output into exactly one JSON value. "
                "Return JSON only, with no markdown or explanation.\n"
                f"Phase: {phase}\n"
                f"Parser or schema error: {parser_failure}\n"
                f"Target JSON Schema: {schema_text}\n"
                f"Original output:\n{original_text}"
            ),
            system_prompt=(
                "You are a strict structured-output repair worker. Preserve the original "
                "meaning, do not invent fields, and return only JSON."
            ),
            expected_output=schema_as_json(schema),
            stream=False,
            metadata={"phase": "structured_output_repair", "repair_for": phase},
        )

    @staticmethod
    def _diagnostics(
        text: str,
        *,
        phase: str,
        adapter: str | None,
        protocol: str | None,
        structured_output_mode: StructuredOutputMode | str | None,
        repair_attempted: bool,
    ) -> dict[str, Any]:
        mode = (
            structured_output_mode.value
            if isinstance(structured_output_mode, StructuredOutputMode)
            else str(structured_output_mode or "unknown")
        )
        result: dict[str, Any] = {
            "phase": phase,
            "adapter": adapter,
            "protocol": protocol,
            "structured_output_mode": mode,
            "raw_chars": len(text),
            "repair_attempted": repair_attempted,
        }
        return result

    @staticmethod
    def _validate_json_schema(value: Any, schema: dict[str, Any], *, path: str) -> None:
        alternatives = schema.get("anyOf") or schema.get("oneOf")
        if isinstance(alternatives, list) and alternatives:
            failures: list[str] = []
            for alternative in alternatives:
                if not isinstance(alternative, dict):
                    continue
                try:
                    StructuredOutputEngine._validate_json_schema(
                        value, alternative, path=path
                    )
                    return
                except StructuredOutputSchemaError as exc:
                    failures.append(str(exc))
            raise StructuredOutputSchemaError(
                "Structured output did not match any schema alternative",
                diagnostics={"schema_failure": f"{path}: {failures[:3]}"},
            )
        expected_type = schema.get("type")
        type_ok = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "null": value is None,
        }
        expected_types = (
            [str(item) for item in expected_type]
            if isinstance(expected_type, (list, tuple))
            else ([str(expected_type)] if expected_type else [])
        )
        if expected_types and not any(type_ok.get(item, True) for item in expected_types):
            raise StructuredOutputSchemaError(
                "Structured output JSON type is invalid",
                diagnostics={"schema_failure": f"{path}: expected {expected_types}"},
            )
        if "enum" in schema and value not in schema["enum"]:
            raise StructuredOutputSchemaError(
                "Structured output value is outside the schema enum",
                diagnostics={"schema_failure": f"{path}: enum"},
            )
        if isinstance(value, dict):
            required = schema.get("required") or []
            missing = [str(key) for key in required if key not in value]
            if missing:
                raise StructuredOutputSchemaError(
                    "Structured output is missing required fields",
                    diagnostics={"schema_failure": f"{path}: missing {missing}"},
                )
            properties = schema.get("properties") or {}
            for key, child_schema in properties.items():
                if key in value and isinstance(child_schema, dict):
                    StructuredOutputEngine._validate_json_schema(
                        value[key], child_schema, path=f"{path}.{key}"
                    )
            if schema.get("additionalProperties") is False:
                unknown = sorted(set(value) - set(properties))
                if unknown:
                    raise StructuredOutputSchemaError(
                        "Structured output contains unknown fields",
                        diagnostics={"schema_failure": f"{path}: unknown {unknown[:20]}"},
                    )
        elif isinstance(value, list) and isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                StructuredOutputEngine._validate_json_schema(
                    item, schema["items"], path=f"{path}[{index}]"
                )


__all__ = [
    "StructuredOutputEngine",
    "StructuredResult",
    "StructuredOutputParseError",
    "StructuredOutputSchemaError",
    "StructuredOutputRepairError",
    "schema_as_json",
]
