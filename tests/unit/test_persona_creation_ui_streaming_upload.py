from __future__ import annotations

from pathlib import Path


def test_persona_creation_ui_does_not_base64_new_files() -> None:
    text = Path("src/persona_continuum/web/static/app.js").read_text(encoding="utf-8")
    start = text.index("function uploadPrivateMaterialFile")
    end = text.index("async function submitPersonaInterviewAnswer")
    chunk = text[start:end]
    assert "content_base64" not in chunk
    assert "btoa(" not in chunk
    assert "arrayBuffer" not in chunk
    assert "String.fromCharCode" not in chunk
    assert "persona-material/uploads" in chunk
    assert "XMLHttpRequest" in chunk
    assert "xhr.send(file)" in chunk
    resume_chunk_start = text.index('$("#pc-resume").hidden')
    resume_line = text[resume_chunk_start : resume_chunk_start + 250]
    assert "pause_requested" in resume_line


def test_upload_handler_streams_request_body() -> None:
    api_src = Path("src/persona_continuum/web/api.py").read_text(encoding="utf-8")
    start = api_src.index("async def upload_persona_material")
    end = api_src.index("async def list_persona_material_jobs")
    chunk = api_src[start:end]
    assert "request.stream()" in chunk
    assert "await request.body()" not in chunk
    assert "request.json()" not in chunk
