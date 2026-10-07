from __future__ import annotations

import json
import tracemalloc
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.application.material_intelligence import MaterialJobStatus
from persona_continuum.config import Config
from persona_continuum.domain.persona import PersonaType
from persona_continuum.ingestion.errors import MaterialTooLargeError
from persona_continuum.ingestion.streaming import StreamingMaterialReader
from persona_continuum.security.validation import SecurityError
from persona_continuum.web.server import create_web_app


def _continuum(tmp_path: Path, **overrides: object) -> PersonaContinuum:
    config = Config(data_dir=tmp_path / "pc-data", **overrides)  # type: ignore[arg-type]
    app = PersonaContinuum(config, include_fake_agent=True)
    app.init()
    return app


def _persona(app: PersonaContinuum, name: str = "Streaming Subject"):
    return app.personas.create(
        display_name=name,
        aliases=[],
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        run_mode="digital_continuation",
    )


def _write_jsonl(path: Path, count: int, *, prefix: str = "msg") -> None:
    with path.open("w", encoding="utf-8") as handle:
        for index in range(count):
            handle.write(
                json.dumps(
                    {
                        "timestamp": f"2024-01-01T00:00:{index % 60:02d}Z",
                        "sender": "me" if index % 2 == 0 else "friend",
                        "content": f"{prefix}-{index}",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def test_streaming_upload_134mb_temp_file(tmp_path: Path) -> None:
    app = _continuum(tmp_path, max_private_material_bytes=200 * 1024 * 1024)
    try:
        target = 134 * 1024 * 1024
        chunk = b'{"sender":"me","content":"hello-world-private-chat"}\n'
        size = 0

        def chunks() -> object:
            nonlocal size
            while size < target:
                piece = chunk if size + len(chunk) <= target else chunk[: target - size]
                size += len(piece)
                yield piece

        row = app.material_uploads.stream_chunks(filename="chat.jsonl", chunks=chunks())
        assert row["size"] >= target
        assert row["source_type"] == "jsonl"
        assert Path(row["path"]).is_file()
        assert Path(row["path"]).stat().st_size == row["size"]
        assert not list(app.config.private_material_uploads_dir.glob("*.part"))
    finally:
        app.close()


def test_upload_over_limit_returns_413_and_cleans_part(tmp_path: Path) -> None:
    app = _continuum(tmp_path, max_private_material_bytes=64 * 1024)
    try:
        with TestClient(create_web_app(app)) as client:
            payload = b"x" * (128 * 1024)
            response = client.post(
                "/api/persona-material/uploads?filename=huge.jsonl",
                content=payload,
                headers={"Content-Type": "application/octet-stream"},
            )
        assert response.status_code == 413
        body = response.json()
        assert body["ok"] is False
        assert "private_material_too_large" in str(body["error"])
        staging = app.config.private_material_uploads_dir
        assert list(staging.glob("*.part")) == []
        assert all(not path.is_file() or path.suffix != ".part" for path in staging.rglob("*"))
        remaining = [path for path in staging.rglob("*") if path.is_file()]
        assert remaining == []
    finally:
        app.close()


def test_interrupted_upload_does_not_leave_complete_artifact(tmp_path: Path) -> None:
    app = _continuum(tmp_path, max_private_material_bytes=10 * 1024 * 1024)
    try:

        def broken() -> object:
            yield b'{"content":"start"}\n' * 100
            raise RuntimeError("client_disconnected")

        with pytest.raises(RuntimeError, match="client_disconnected"):
            app.material_uploads.stream_chunks(filename="partial.jsonl", chunks=broken())
        staging = app.config.private_material_uploads_dir
        assert list(staging.glob("*.part")) == []
        files = [path for path in staging.rglob("*") if path.is_file()]
        assert files == []
        rows = app.database.conn.execute("SELECT id FROM persona_material_uploads").fetchall()
        assert rows == []
    finally:
        app.close()


def test_path_traversal_filename_is_rejected(tmp_path: Path) -> None:
    app = _continuum(tmp_path)
    try:
        with pytest.raises(SecurityError, match="path_traversal"):
            app.material_uploads.sanitize_filename("../../etc/passwd")
        with TestClient(create_web_app(app)) as client:
            response = client.post(
                "/api/persona-material/uploads?filename=../../etc/passwd",
                content=b'{"content":"x"}\n',
                headers={"Content-Type": "application/octet-stream"},
            )
        assert response.status_code == 400
    finally:
        app.close()


def test_jsonl_100k_rows_stream_without_full_list_build(tmp_path: Path) -> None:
    path = tmp_path / "big.jsonl"
    _write_jsonl(path, 100_000)
    reader = StreamingMaterialReader(legacy_json_max_bytes=1024)
    seen = 0
    first = middle = last = None
    for record in reader.iter_path(path, "jsonl"):
        if seen == 0:
            first = record.text
        if seen == 50_000:
            middle = record.text
        last = record.text
        seen += 1
    assert seen == 100_000
    assert first == "msg-0"
    assert middle == "msg-50000"
    assert last == "msg-99999"


def test_evidence_units_persist_in_batches_and_keep_edges(tmp_path: Path) -> None:
    app = _continuum(
        tmp_path,
        max_source_bytes=512,
        max_private_material_bytes=8 * 1024 * 1024,
        material_intelligence_batch_size=25,
        material_intelligence_in_memory_unit_limit=40,
    )
    try:
        persona = _persona(app)
        path = tmp_path / "chat.jsonl"
        _write_jsonl(path, 120)
        source = app.personas.add_external_file_source(persona.id, path, filename="chat.jsonl")
        assert source.metadata["storage_mode"] == "external_file"
        assert source.content == ""
        assert Path(source.metadata["path"]).is_file()
        assert Path(source.metadata["normalized_path"]).is_file()

        persist_sizes: list[int] = []
        original = app.material_intelligence._persist_units

        def spy(units):  # type: ignore[no-untyped-def]
            persist_sizes.append(len(list(units)))
            assert len(units) <= app.material_intelligence.batch_size
            return original(units)

        app.material_intelligence._persist_units = spy  # type: ignore[method-assign]

        def forbid_load(persona_id: str):  # type: ignore[no-untyped-def]
            raise AssertionError("must not load all EvidenceUnits into memory")

        app.material_intelligence._load_units = forbid_load  # type: ignore[method-assign]
        job = app.material_intelligence.analyze_sources(persona.id, [source.id])
        assert job.status == MaterialJobStatus.READY_FOR_COMPILATION
        assert persist_sizes
        assert max(persist_sizes) <= 25
        index = app.material_intelligence.get_index(persona.id)
        assert index.unit_count() == 120
        assert index.contains_text("msg-0")
        assert index.contains_text("msg-60")
        assert index.contains_text("msg-119")
        assert Path(source.path).is_file()
        assert source.hash
    finally:
        app.close()


@pytest.mark.anyio
async def test_retry_does_not_reupload_persisted_original(tmp_path: Path) -> None:
    app = _continuum(tmp_path, max_source_bytes=256, max_private_material_bytes=1024 * 1024)
    try:
        await app.agent_discovery.scan(force_refresh=True)
        persona = _persona(app)
        path = tmp_path / "once.jsonl"
        _write_jsonl(path, 8)
        first = app.personas.add_external_file_source(persona.id, path, filename="once.jsonl")
        original_path = Path(first.path)
        assert original_path.is_file()
        mtime = original_path.stat().st_mtime
        second = app.personas.add_external_file_source(persona.id, path, filename="once.jsonl")
        assert second.id == first.id
        assert Path(second.path) == original_path
        assert original_path.stat().st_mtime == mtime
    finally:
        app.close()


@pytest.mark.anyio
async def test_legacy_content_base64_still_works(tmp_path: Path) -> None:
    import base64

    app = _continuum(tmp_path)
    try:
        await app.agent_discovery.scan(force_refresh=True)
        payload = '{"sender":"me","content":"legacy-base64-row"}\n'
        async def skip_analyze(*_args: object, **_kwargs: object) -> object:
            return type(
                "Job",
                (),
                {
                    "id": "pmjob_skip",
                    "status": MaterialJobStatus.READY_FOR_COMPILATION,
                    "progress": {},
                    "coverage": {},
                    "model_dump": lambda self, mode="json": {
                        "id": "pmjob_skip",
                        "status": "READY_FOR_COMPILATION",
                    },
                },
            )()

        app.persona_creation._analyze_private_materials = skip_analyze  # type: ignore[method-assign]
        job = await app.persona_creation.create_job(
            display_name="Legacy Base64 Subject",
            persona_type=PersonaType.PRIVATE_LIVING_PERSON,
            creation_mode="private_materials",
            runtime_source="test",
            agent_id="fake_agent",
            model_id="fake-gpt-5",
            materials=[
                {
                    "filename": "legacy.jsonl",
                    "source_type": "user_file",
                    "content_base64": base64.b64encode(payload.encode("utf-8")).decode("ascii"),
                }
            ],
            start_worker=False,
        )
        await app.persona_creation._ingest_configured_materials(job)
        sources = app.personas.get_sources(job.persona_id or "")
        assert sources
        assert "legacy-base64-row" in sources[0].content or sources[0].metadata.get("filename")
        app.material_intelligence.analyze_sources(
            job.persona_id or "", [item.id for item in sources]
        )
        units = app.material_intelligence.get_index(job.persona_id or "").units()
        assert any(item.text == "legacy-base64-row" for item in units)
    finally:
        app.close()


@pytest.mark.anyio
async def test_private_uploaded_material_does_not_enter_web_research(tmp_path: Path) -> None:
    app = _continuum(tmp_path)
    try:
        queries: list[str] = []

        class Broker:
            async def search(self, query: str, **_kwargs: object) -> list[object]:
                queries.append(query)
                return []

        app.persona_creation.research_broker = Broker()  # type: ignore[assignment]

        async def skip_analyze(*_args: object, **_kwargs: object) -> object:
            return type(
                "Job",
                (),
                {
                    "id": "pmjob_skip",
                    "status": MaterialJobStatus.READY_FOR_COMPILATION,
                    "progress": {},
                    "coverage": {},
                    "model_dump": lambda self, mode="json": {
                        "id": "pmjob_skip",
                        "status": "READY_FOR_COMPILATION",
                    },
                },
            )()

        app.persona_creation._analyze_private_materials = skip_analyze  # type: ignore[method-assign]
        await app.agent_discovery.scan(force_refresh=True)
        upload = app.material_uploads.stream_chunks(
            filename="private.jsonl",
            chunks=iter([b'{"sender":"me","content":"secret-local-only"}\n']),
        )
        job = await app.persona_creation.create_job(
            display_name="Private Upload Subject",
            persona_type=PersonaType.PRIVATE_LIVING_PERSON,
            creation_mode="private_materials",
            runtime_source="test",
            agent_id="fake_agent",
            model_id="fake-gpt-5",
            materials=[
                {
                    "upload_id": upload["id"],
                    "filename": "private.jsonl",
                    "source_type": "user_file",
                }
            ],
            start_worker=False,
        )
        await app.persona_creation._ingest_configured_materials(job)
        assert queries == []
        sources = app.personas.get_sources(job.persona_id or "")
        assert sources
        assert sources[0].metadata.get("privacy") == "private_material"
    finally:
        app.close()


def test_http_upload_then_job_materials_use_upload_id(tmp_path: Path) -> None:
    app = _continuum(tmp_path)
    try:
        with TestClient(create_web_app(app)) as client:
            response = client.post(
                "/api/persona-material/uploads?filename=chat.jsonl",
                content=b'{"sender":"me","content":"http-row"}\n',
                headers={"Content-Type": "application/octet-stream", "X-Filename": "chat.jsonl"},
            )
        assert response.status_code == 201
        data = response.json()["data"]
        assert data["upload_id"]
        assert data["filename"] == "chat.jsonl"
        assert data["source_type"] == "jsonl"
        assert "content_base64" not in data
    finally:
        app.close()


def test_streaming_reader_peak_memory_stays_bounded(tmp_path: Path) -> None:
    path = tmp_path / "mem.jsonl"
    _write_jsonl(path, 20_000)
    file_size = path.stat().st_size
    reader = StreamingMaterialReader(legacy_json_max_bytes=1024)
    tracemalloc.start()
    count = 0
    for _record in reader.iter_path(path, "jsonl"):
        count += 1
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert count == 20_000
    assert peak < max(8 * 1024 * 1024, file_size // 2)


def test_pipe_chat_txt_is_segmented_into_messages_not_one_blob(tmp_path: Path) -> None:
    app = _continuum(
        tmp_path,
        max_source_bytes=512,
        max_private_material_bytes=8 * 1024 * 1024,
        material_intelligence_batch_size=50,
        material_intelligence_in_memory_unit_limit=80,
    )
    try:
        persona = _persona(app, "Chat Txt Subject")
        path = tmp_path / "wechat.txt"
        lines = ["# Persona 聊天证据", "#"]
        for index in range(200):
            speaker = "对方" if index % 2 else "我"
            lines.append(f"2023-02-14 17:06:{index % 60:02d} | {speaker} | line-{index}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        source = app.personas.add_external_file_source(persona.id, path, filename="wechat.txt")
        job = app.material_intelligence.analyze_sources(persona.id, [source.id])
        assert job.status == MaterialJobStatus.READY_FOR_COMPILATION
        index = app.material_intelligence.get_index(persona.id)
        assert index.unit_count() == 200
        assert index.contains_text("line-0")
        assert index.contains_text("line-100")
        assert index.contains_text("line-199")
        unit = next(
            item for item in index.units() if item.text == "line-0"
        )
        assert unit.speaker == "我"
        assert unit.source_kind == "chat_import"
    finally:
        app.close()


def test_oversized_unit_is_resegmented_on_retry(tmp_path: Path) -> None:
    app = _continuum(
        tmp_path,
        max_source_bytes=512,
        max_private_material_bytes=8 * 1024 * 1024,
        material_intelligence_batch_size=40,
        material_intelligence_in_memory_unit_limit=60,
    )
    try:
        persona = _persona(app, "Oversized Unit Subject")
        path = tmp_path / "chat.txt"
        lines = ["# header"]
        for index in range(120):
            lines.append(f"2024-01-01 12:00:{index % 60:02d} | 对方 | blob-{index}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        source = app.personas.add_external_file_source(persona.id, path, filename="chat.txt")
        app.database.conn.execute(
            "INSERT INTO persona_evidence_units "
            "(id, persona_id, source_id, source_locator_json, speaker, speaker_role, "
            "timestamp, text, normalized_text, normalized_text_hash, evidence_type, "
            "dimension_candidates_json, dimension_scores_json, life_stage_candidates_json, "
            "relationship_entities_json, context_tags_json, confidence, extraction_method, "
            "source_kind, event_time, metadata_json, created_at) "
            "VALUES (?, ?, ?, '{}', NULL, NULL, NULL, ?, ?, 'x', 'behavioral_observation', "
            "'[]', '{}', '[]', '[]', '[]', 0.5, 'paragraph_segmenter', 'user_provided', "
            "NULL, '{}', '2026-01-01T00:00:00+00:00')",
            (
                "evu_giant",
                persona.id,
                source.id,
                "x" * 20000,
                "x" * 20000,
            ),
        )
        app.database.conn.commit()
        job = app.material_intelligence.analyze_sources(
            persona.id, [source.id], incremental=True
        )
        assert job.status == MaterialJobStatus.READY_FOR_COMPILATION
        index = app.material_intelligence.get_index(persona.id)
        assert index.unit_count() == 120
        assert index.contains_text("blob-0")
        assert index.contains_text("blob-119")
        assert not index.contains_text("x" * 20000)
    finally:
        app.close()


def test_too_large_sync_upload_raises_and_cleans(tmp_path: Path) -> None:
    app = _continuum(tmp_path, max_private_material_bytes=1024)
    try:
        with pytest.raises(MaterialTooLargeError):
            app.material_uploads.stream_chunks(
                filename="too-big.jsonl",
                chunks=iter([b"x" * 2048]),
            )
        assert list(app.config.private_material_uploads_dir.glob("*.part")) == []
    finally:
        app.close()
