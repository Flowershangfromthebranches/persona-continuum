from __future__ import annotations

import json
from pathlib import Path

import pytest

from persona_continuum.ingestion.errors import MaterialParseError, UnrecognizedJsonStructureError
from persona_continuum.ingestion.streaming import StreamingMaterialReader


def _reader(limit: int = 1024) -> StreamingMaterialReader:
    return StreamingMaterialReader(legacy_json_max_bytes=limit)


def test_jsonl_streaming_reads_every_row(tmp_path: Path) -> None:
    path = tmp_path / "chat.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for index in range(1005):
            handle.write(
                json.dumps(
                    {"sender": "me" if index % 2 == 0 else "friend", "content": f"msg-{index}"},
                    ensure_ascii=False,
                )
                + "\n"
            )
    records = list(_reader().iter_path(path, "jsonl"))
    assert len(records) == 1005
    assert records[0].text == "msg-0"
    assert records[502].text == "msg-502"
    assert records[-1].text == "msg-1004"


def test_json_root_array_streaming(tmp_path: Path) -> None:
    path = tmp_path / "root.json"
    rows = [{"sender": "me", "content": f"row-{index}"} for index in range(250)]
    path.write_text(json.dumps(rows), encoding="utf-8")
    # Force the ijson path rather than json.loads of the whole document.
    records = list(_reader(limit=8).iter_path(path, "json"))
    assert [item.text for item in records] == [f"row-{index}" for index in range(250)]


def test_messages_array_json_streaming(tmp_path: Path) -> None:
    path = tmp_path / "messages.json"
    payload = {
        "version": 1,
        "messages": [
            {"sender": "me", "content": "hello"},
            {"sender": "friend", "content": "hi"},
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    records = list(_reader(limit=8).iter_path(path, "json"))
    assert [item.text for item in records] == ["hello", "hi"]
    assert [item.speaker for item in records] == ["me", "friend"]


def test_csv_streaming_reads_all_rows(tmp_path: Path) -> None:
    path = tmp_path / "chat.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write("timestamp,sender,content\n")
        for index in range(1500):
            handle.write(f"2024-01-01T00:00:{index:02d}Z,me,csv-{index}\n")
    records = list(_reader().iter_path(path, "csv"))
    assert len(records) == 1500
    assert records[0].text == "csv-0"
    assert records[-1].text == "csv-1499"


def test_large_unknown_json_structure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "unknown.json"
    payload = {"meta": {"nested": {"graph": {"nodes": [1, 2, 3]}}}, "note": "x" * 4096}
    path.write_text(json.dumps(payload), encoding="utf-8")
    original = json.loads

    def guarded(value: object, *args: object, **kwargs: object) -> object:
        if isinstance(value, (str, bytes)) and len(value) > 1024:
            raise AssertionError("full json.loads of large unknown document")
        return original(value, *args, **kwargs)

    monkeypatch.setattr("persona_continuum.ingestion.streaming.json.loads", guarded)
    with pytest.raises(UnrecognizedJsonStructureError):
        list(_reader(limit=64).iter_path(path, "json"))


def test_malformed_jsonl_is_not_silently_skipped(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"content":"ok"}\n{not json}\n', encoding="utf-8")
    with pytest.raises(MaterialParseError, match="jsonl_parse_error"):
        list(_reader().iter_path(path, "jsonl"))


def test_pipe_delimited_chat_txt_is_split_per_line(tmp_path: Path) -> None:
    path = tmp_path / "chat.txt"
    lines = [
        "# Persona 聊天证据",
        "# 对方 = 目标 Persona",
        "#",
    ]
    for index in range(2500):
        speaker = "对方" if index % 2 else "我"
        lines.append(f"2023-02-14 17:06:{index % 60:02d} | {speaker} | msg-{index}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    records = list(_reader().iter_path(path, "txt"))
    assert len(records) == 2500
    assert records[0].text == "msg-0"
    assert records[0].speaker == "我"
    assert records[0].timestamp.startswith("2023-02-14")
    assert records[1250].text == "msg-1250"
    assert records[-1].text == "msg-2499"
    assert records[-1].speaker == "对方"


def test_txt_without_blank_lines_does_not_collapse_to_one_record(tmp_path: Path) -> None:
    path = tmp_path / "diary.txt"
    path.write_text("第一句。\n第二句。\n第三句。\n", encoding="utf-8")
    records = list(_reader().iter_path(path, "txt"))
    assert len(records) == 1 or len(records) >= 3
    assert "第一句" in records[0].text
    joined = "\n".join(item.text for item in records)
    assert "第二句" in joined
    assert "第三句" in joined


def test_normalize_jsonl_keeps_locators(tmp_path: Path) -> None:
    source = tmp_path / "in.jsonl"
    dest = tmp_path / "normalized.jsonl"
    source.write_text(
        '{"sender":"me","content":"hello","timestamp":"2024-01-01T00:00:00Z"}\n',
        encoding="utf-8",
    )
    count = _reader().normalize_path_to_jsonl(
        source, dest, source_id="src_test", source_type="jsonl", filename="in.jsonl"
    )
    assert count == 1
    row = json.loads(dest.read_text(encoding="utf-8").splitlines()[0])
    assert row["source_id"] == "src_test"
    assert row["speaker"] == "me"
    assert row["locator"]["row"] == 0
