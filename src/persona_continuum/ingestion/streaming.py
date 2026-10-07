"""Streaming readers and canonical JSONL normalizers for private materials.

Large JSON / JSONL / CSV chat exports must never be fully loaded with
``json.loads`` or assembled into a Python list of every record.  Callers
iterate records, persist a batch, and release it.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, TextIO

import ijson  # type: ignore[import-untyped]

from persona_continuum.ingestion.errors import MaterialParseError, UnrecognizedJsonStructureError

KNOWN_JSON_ARRAY_KEYS = (
    "messages",
    "records",
    "items",
    "data",
    "conversations",
    "conversation",
    "turns",
    "rows",
)

STRUCTURED_SUFFIXES = {"json", "jsonl", "csv"}
TEXT_FIELD_KEYS = ("content", "text", "message", "body")
SPEAKER_FIELD_KEYS = ("speaker", "sender", "author", "from", "user")
TIMESTAMP_FIELD_KEYS = ("timestamp", "time", "created_at", "date", "sent_at")
CONVERSATION_FIELD_KEYS = ("conversation_id", "thread_id", "chat_id")

# Pipe-delimited private chat exports, e.g.
# ``2023-02-14 17:06:11 | 对方 | 你好``.
CHAT_PIPE_LINE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s*[|｜]\s*"
    r"(?P<speaker>[^|｜]{1,80})\s*[|｜]\s*(?P<text>.*)$"
)
SPEAKER_PREFIX = re.compile(r"^([^:：\n]{1,60})[:：]\s*(.+)$", flags=re.DOTALL)
MAX_TEXT_UNIT_CHARS = 8000


@dataclass(frozen=True)
class MaterialRecord:
    """One message/row from a private material file."""

    row_index: int
    text: str
    speaker: str | None = None
    timestamp: str | None = None
    conversation_id: str | None = None
    locator: dict[str, Any] = field(default_factory=dict)
    fields: dict[str, Any] = field(default_factory=dict)


def infer_source_type(filename: str) -> str:
    suffix = Path(str(filename or "")).suffix.lower().lstrip(".")
    if suffix in {
        "txt",
        "md",
        "json",
        "jsonl",
        "csv",
        "html",
        "htm",
        "pdf",
        "docx",
    }:
        return suffix
    return "user_file"


def _first_str(row: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is None or value == "":
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _compact_fields(row: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in row.items():
        if value is None or value == "":
            continue
        if isinstance(value, (dict, list)):
            compact[str(key)] = type(value).__name__
            continue
        text = str(value)
        compact[str(key)] = text if len(text) <= 4000 else text[:4000]
    return compact


def record_from_row(
    row: dict[str, Any], index: int, *, filename: str | None = None
) -> MaterialRecord:
    text = _first_str(row, TEXT_FIELD_KEYS) or ""
    speaker = _first_str(row, SPEAKER_FIELD_KEYS)
    timestamp = _first_str(row, TIMESTAMP_FIELD_KEYS)
    conversation_id = _first_str(row, CONVERSATION_FIELD_KEYS)
    locator = {
        "row": index,
        "line": index + 1,
        "segment_index": index,
        "filename": filename,
        "raw_keys": [str(key) for key in row],
    }
    if conversation_id:
        locator["conversation_id"] = conversation_id
    return MaterialRecord(
        row_index=index,
        text=text,
        speaker=speaker,
        timestamp=timestamp,
        conversation_id=conversation_id,
        locator=locator,
        fields=_compact_fields(row),
    )


def canonical_jsonl_row(
    record: MaterialRecord,
    *,
    source_id: str,
    filename: str | None = None,
) -> dict[str, Any]:
    locator = dict(record.locator)
    locator["source_id"] = source_id
    if filename:
        locator["filename"] = filename
    return {
        "source_id": source_id,
        "row_index": record.row_index,
        "timestamp": record.timestamp,
        "speaker": record.speaker,
        "text": record.text,
        "conversation_id": record.conversation_id,
        "locator": locator,
        "raw_keys": locator.get("raw_keys") or list(record.fields.keys()),
    }


class StreamingMaterialReader:
    """Incrementally iterate chat/document records without a full in-memory parse."""

    def __init__(self, *, legacy_json_max_bytes: int) -> None:
        self.legacy_json_max_bytes = max(0, int(legacy_json_max_bytes))

    def iter_path(
        self,
        path: Path,
        source_type: str | None = None,
        *,
        filename: str | None = None,
    ) -> Iterator[MaterialRecord]:
        suffix = (source_type or path.suffix.lstrip(".")).lower().lstrip(".")
        name = filename or path.name
        if suffix == "jsonl":
            yield from self._iter_jsonl_file(path, filename=name)
            return
        if suffix == "csv":
            yield from self._iter_csv_file(path, filename=name)
            return
        if suffix == "json":
            yield from self._iter_json_file(path, filename=name)
            return
        yield from self._iter_text_file(path, filename=name)

    def iter_text(
        self,
        content: str,
        source_type: str,
        *,
        filename: str | None = None,
    ) -> Iterator[MaterialRecord]:
        suffix = str(source_type or "").lower().lstrip(".")
        if suffix == "jsonl":
            yield from self._iter_jsonl_text(content, filename=filename)
            return
        if suffix == "csv":
            yield from self._iter_csv_text(content, filename=filename)
            return
        if suffix == "json":
            encoded = content.encode("utf-8")
            yield from self._iter_json_bytes(
                encoded, filename=filename, allow_legacy=True
            )
            return
        yield from self._iter_text_buffer(content, filename=filename)

    def normalize_path_to_jsonl(
        self,
        source_path: Path,
        dest_path: Path,
        *,
        source_id: str,
        source_type: str | None = None,
        filename: str | None = None,
    ) -> int:
        """Stream canonical JSONL next to the immutable original. Return record count."""

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with dest_path.open("w", encoding="utf-8", newline="\n") as handle:
            for record in self.iter_path(source_path, source_type, filename=filename):
                if not record.text.strip():
                    continue
                handle.write(
                    json.dumps(
                        canonical_jsonl_row(
                            record, source_id=source_id, filename=filename or source_path.name
                        ),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                handle.write("\n")
                count += 1
        return count

    def iter_normalized_jsonl(self, path: Path) -> Iterator[MaterialRecord]:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise MaterialParseError(
                        f"jsonl_parse_error:line:{line_no}",
                        locator={"line": line_no, "path": str(path)},
                    ) from exc
                if not isinstance(item, dict):
                    raise MaterialParseError(
                        f"jsonl_record_not_object:line:{line_no}",
                        locator={"line": line_no, "path": str(path)},
                    )
                index = int(item.get("row_index") or line_no - 1)
                locator = dict(item.get("locator") or {})
                locator.setdefault("line", line_no)
                locator.setdefault("row", index)
                yield MaterialRecord(
                    row_index=index,
                    text=str(item.get("text") or "").strip(),
                    speaker=str(item["speaker"]) if item.get("speaker") else None,
                    timestamp=str(item["timestamp"]) if item.get("timestamp") else None,
                    conversation_id=(
                        str(item["conversation_id"]) if item.get("conversation_id") else None
                    ),
                    locator=locator,
                    fields=dict(item),
                )

    def _iter_jsonl_file(self, path: Path, *, filename: str | None) -> Iterator[MaterialRecord]:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            yield from self._iter_jsonl_handle(handle, filename=filename)

    def _iter_jsonl_text(self, content: str, *, filename: str | None) -> Iterator[MaterialRecord]:
        yield from self._iter_jsonl_handle(io.StringIO(content), filename=filename)

    def _iter_jsonl_handle(
        self, handle: TextIO, *, filename: str | None
    ) -> Iterator[MaterialRecord]:
        index = 0
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise MaterialParseError(
                    f"jsonl_parse_error:line:{line_no}",
                    locator={"line": line_no, "filename": filename},
                ) from exc
            if not isinstance(item, dict):
                raise MaterialParseError(
                    f"jsonl_record_not_object:line:{line_no}",
                    locator={"line": line_no, "filename": filename},
                )
            record = record_from_row(item, index, filename=filename)
            record.locator["line"] = line_no
            yield record
            index += 1

    def _iter_csv_file(self, path: Path, *, filename: str | None) -> Iterator[MaterialRecord]:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            yield from self._iter_csv_handle(handle, filename=filename)

    def _iter_csv_text(self, content: str, *, filename: str | None) -> Iterator[MaterialRecord]:
        yield from self._iter_csv_handle(io.StringIO(content), filename=filename)

    def _iter_csv_handle(self, handle: TextIO, *, filename: str | None) -> Iterator[MaterialRecord]:
        try:
            reader = csv.DictReader(handle)
            for index, row in enumerate(reader):
                yield record_from_row(
                    {str(k): v for k, v in dict(row).items()}, index, filename=filename
                )
        except csv.Error as exc:
            raise MaterialParseError(
                "csv_parse_error",
                locator={"filename": filename},
            ) from exc

    def _iter_json_file(self, path: Path, *, filename: str | None) -> Iterator[MaterialRecord]:
        size = path.stat().st_size
        allow_legacy = size <= self.legacy_json_max_bytes
        with path.open("rb") as handle:
            yield from self._iter_json_handle(
                handle, filename=filename, allow_legacy=allow_legacy, size=size
            )

    def _iter_json_bytes(
        self, raw: bytes, *, filename: str | None, allow_legacy: bool
    ) -> Iterator[MaterialRecord]:
        yield from self._iter_json_handle(
            io.BytesIO(raw),
            filename=filename,
            allow_legacy=allow_legacy,
            size=len(raw),
        )

    def _iter_json_handle(
        self,
        handle: BinaryIO,
        *,
        filename: str | None,
        allow_legacy: bool,
        size: int,
    ) -> Iterator[MaterialRecord]:
        if allow_legacy and size <= self.legacy_json_max_bytes:
            yield from self._iter_legacy_json(handle, filename=filename)
            return
        prefix = detect_json_array_prefix(handle, filename=filename)
        handle.seek(0)
        index = 0
        try:
            for item in ijson.items(handle, prefix):
                if not isinstance(item, dict):
                    raise MaterialParseError(
                        f"json_record_not_object:index:{index}",
                        locator={"row": index, "filename": filename, "prefix": prefix},
                    )
                for row in expand_nested_records(item):
                    yield record_from_row(row, index, filename=filename)
                    index += 1
        except UnrecognizedJsonStructureError:
            raise
        except MaterialParseError:
            raise
        except Exception as exc:
            raise MaterialParseError(
                f"json_stream_parse_error:{exc}",
                locator={"filename": filename, "prefix": prefix, "row": index},
            ) from exc

    def _iter_legacy_json(
        self, handle: BinaryIO, *, filename: str | None
    ) -> Iterator[MaterialRecord]:
        raw = handle.read()
        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise MaterialParseError(
                "json_parse_error",
                locator={"filename": filename},
            ) from exc
        rows = _legacy_json_rows(parsed)
        for index, row in enumerate(rows):
            yield record_from_row(row, index, filename=filename)

    def _iter_text_file(self, path: Path, *, filename: str | None) -> Iterator[MaterialRecord]:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            yield from self._iter_text_lines(handle, filename=filename)

    def _iter_text_buffer(self, content: str, *, filename: str | None) -> Iterator[MaterialRecord]:
        yield from self._iter_text_lines(io.StringIO(content), filename=filename)

    def _iter_text_lines(self, handle: TextIO, *, filename: str | None) -> Iterator[MaterialRecord]:
        """Stream text/chat files without concatenating the whole document.

        Private chat exports are often one message per line with no blank
        separators.  Gluing those lines into a single EvidenceUnit makes
        clustering/token sets hang on multi-million-character strings.
        """

        index = 0
        chat_mode: bool | None = None
        paragraph: list[str] = []

        def emit(
            text: str,
            *,
            speaker: str | None = None,
            timestamp: str | None = None,
            source_kind: str | None = None,
            line_no: int | None = None,
        ) -> Iterator[MaterialRecord]:
            nonlocal index
            stripped = text.strip()
            if not stripped:
                return
            for piece in split_oversized_text(stripped):
                locator = {
                    "row": index,
                    "segment_index": index,
                    "filename": filename,
                    "line": line_no,
                }
                fields: dict[str, Any] = {}
                if source_kind:
                    fields["source_kind"] = source_kind
                    locator["source_kind"] = source_kind
                yield MaterialRecord(
                    row_index=index,
                    text=piece,
                    speaker=speaker,
                    timestamp=timestamp,
                    locator=locator,
                    fields=fields,
                )
                index += 1

        def flush_paragraph() -> Iterator[MaterialRecord]:
            nonlocal paragraph
            text = "".join(paragraph).strip()
            paragraph = []
            if not text:
                return
            speaker = None
            body = text
            match = SPEAKER_PREFIX.match(text)
            if match:
                speaker, body = match.group(1).strip(), match.group(2).strip()
            yield from emit(body, speaker=speaker)

        for line_no, raw_line in enumerate(handle, start=1):
            stripped = raw_line.strip()
            if chat_mode is not True and not stripped:
                yield from flush_paragraph()
                continue
            if chat_mode is None:
                if not stripped or stripped.startswith("#"):
                    continue
                chat_mode = bool(CHAT_PIPE_LINE.match(stripped))
            if chat_mode:
                if not stripped or stripped.startswith("#"):
                    continue
                matched = CHAT_PIPE_LINE.match(stripped)
                if matched:
                    yield from emit(
                        matched.group("text"),
                        speaker=matched.group("speaker").strip(),
                        timestamp=matched.group("timestamp"),
                        source_kind="chat_import",
                        line_no=line_no,
                    )
                    continue
                yield from emit(stripped, source_kind="chat_import", line_no=line_no)
                continue
            if not stripped:
                yield from flush_paragraph()
                continue
            paragraph.append(raw_line)
            if sum(len(part) for part in paragraph) >= MAX_TEXT_UNIT_CHARS:
                yield from flush_paragraph()
        yield from flush_paragraph()


def split_oversized_text(text: str, *, max_chars: int = MAX_TEXT_UNIT_CHARS) -> list[str]:
    value = str(text or "").strip()
    if not value:
        return []
    if len(value) <= max_chars:
        return [value]
    pieces: list[str] = []
    remaining = value
    while remaining:
        if len(remaining) <= max_chars:
            pieces.append(remaining)
            break
        window = remaining[:max_chars]
        cut = max(
            window.rfind("\n"),
            window.rfind("。"),
            window.rfind("！"),
            window.rfind("？"),
            window.rfind(". "),
            window.rfind("! "),
            window.rfind("? "),
        )
        if cut < max_chars // 4:
            cut = max_chars
        else:
            cut += 1
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    return [item for item in pieces if item]


def detect_json_array_prefix(handle: BinaryIO, *, filename: str | None = None) -> str:
    """Return an ijson items prefix for a known array, without loading the document.

    Large files with an unrecognized shape raise ``UnrecognizedJsonStructureError``
    instead of falling back to ``json.loads``.
    """

    pending_key: str | None = None
    try:
        for prefix, event, value in ijson.parse(handle):
            if event == "start_array" and prefix == "":
                return "item"
            if event == "map_key" and prefix == "":
                pending_key = str(value)
                continue
            if pending_key is None:
                if event == "end_map" and prefix == "":
                    break
                continue
            if event == "start_array" and prefix == pending_key:
                if pending_key in KNOWN_JSON_ARRAY_KEYS:
                    return f"{pending_key}.item"
                pending_key = None
                continue
            if event in {
                "start_map",
                "string",
                "number",
                "boolean",
                "null",
            } and prefix == pending_key:
                pending_key = None
                continue
            if event == "end_map" and prefix == "":
                break
    except UnrecognizedJsonStructureError:
        raise
    except Exception as exc:
        raise UnrecognizedJsonStructureError(
            f"unrecognized_json_structure:{filename or 'json'}:{exc}"
        ) from exc
    raise UnrecognizedJsonStructureError(
        f"unrecognized_json_structure:{filename or 'json'}"
    )


def expand_nested_records(item: dict[str, Any]) -> Iterator[dict[str, Any]]:
    if _first_str(item, TEXT_FIELD_KEYS):
        yield dict(item)
        return
    for key in KNOWN_JSON_ARRAY_KEYS:
        nested = item.get(key)
        if isinstance(nested, list) and nested and all(isinstance(child, dict) for child in nested):
            for child in nested:
                yield from expand_nested_records(dict(child))
            return
    yield dict(item)


def _legacy_json_rows(parsed: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed):
        for item in parsed:
            rows.extend(expand_nested_records(item))
        return rows
    if isinstance(parsed, dict):
        for key in KNOWN_JSON_ARRAY_KEYS:
            value = parsed.get(key)
            if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                for item in value:
                    rows.extend(expand_nested_records(item))
                return rows
    return []
