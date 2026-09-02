from __future__ import annotations

import logging

import pytest

from persona_continuum.agent.protocols.openai_compatible import OpenAICompatibleAPIAdapter
from persona_continuum.auth.credentials import CredentialProvider


def test_api_key_runtime_resolution(app) -> None:
    app.credentials.create(
        credential_id="provider-runtime",
        provider=CredentialProvider.OPENAI,
        api_key="sk-runtime-resolution-secret-123456",
    )
    runtime = app.credentials.resolve("provider-runtime")
    assert runtime.api_key == "sk-runtime-resolution-secret-123456"


def test_encrypted_secret_storage(app) -> None:
    secret = "sk-encrypted-storage-secret-123456"
    app.credentials.create(
        credential_id="provider-encrypted",
        provider=CredentialProvider.OPENAI,
        api_key=secret,
    )
    row = app.database.conn.execute(
        "SELECT encrypted_secret FROM credentials WHERE id = ?", ("provider-encrypted",)
    ).fetchone()
    assert row is not None
    assert secret.encode() not in bytes(row["encrypted_secret"])


def test_frontend_profile_never_returns_complete_key(app) -> None:
    secret = "sk-frontend-must-not-see-this-secret-123456"
    profile = app.auth.create_profile(
        profile_id="provider-frontend",
        name="Frontend safe",
        provider_type="openai",
        base_url="https://api.openai.com/v1",
        api_key=secret,
    )
    payload = profile.model_dump(mode="json")
    row = app.database.conn.execute(
        "SELECT headers_json, metadata_json FROM api_profiles WHERE id = ?",
        (profile.id,),
    ).fetchone()
    assert secret not in str(payload)
    assert row is not None
    assert secret not in f"{row['headers_json']} {row['metadata_json']}"


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = "invalid sk-should-never-be-returned-1234567890"

    def json(self):
        return {"data": [{"id": "model-a"}, {"id": "model-b"}]}


class _Client:
    response_status = 200

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, headers):
        assert "sk-" not in url
        assert headers
        return _Response(self.response_status)


@pytest.mark.anyio
async def test_provider_connection_success(app, monkeypatch) -> None:
    from persona_continuum.auth import credentials

    monkeypatch.setattr(credentials.httpx, "AsyncClient", _Client)
    app.credentials.create(
        credential_id="provider-success",
        provider=CredentialProvider.OPENAI,
        api_key="sk-provider-success-secret-123456",
    )
    result = await app.credentials.test_connection("provider-success")
    assert result["status"] == "connected"
    assert result["models"] == ["model-a", "model-b"]


@pytest.mark.anyio
async def test_invalid_key_error(app, monkeypatch) -> None:
    from persona_continuum.auth import credentials

    class InvalidClient(_Client):
        response_status = 401

    monkeypatch.setattr(credentials.httpx, "AsyncClient", InvalidClient)
    secret = "sk-invalid-provider-secret-123456"
    app.credentials.create(
        credential_id="provider-invalid",
        provider=CredentialProvider.OPENAI,
        api_key=secret,
    )
    result = await app.credentials.test_connection("provider-invalid")
    assert result["status"] == "error"
    assert "401" in result["error"]
    assert secret not in result["error"]


def test_openai_compatible_base_url(app) -> None:
    app.credentials.create(
        credential_id="provider-compatible",
        provider=CredentialProvider.OPENAI_COMPATIBLE,
        api_key="custom-provider-key",
        base_url="https://models.example.test/v1/",
    )
    runtime = app.credentials.resolve("provider-compatible")
    assert runtime.base_url == "https://models.example.test/v1"


def test_adapter_uses_credential_manager(app) -> None:
    app.credentials.create(
        credential_id="adapter-provider",
        provider=CredentialProvider.OPENAI,
        api_key="sk-adapter-manager-secret-123456",
    )
    adapter = OpenAICompatibleAPIAdapter(
        credential_manager=app.credentials,
        credential_id="adapter-provider",
    )
    base_url, headers = adapter._request_context()
    assert base_url == "https://api.openai.com/v1"
    assert headers["Authorization"].endswith("secret-123456")


@pytest.mark.anyio
async def test_no_secret_in_logs(app, monkeypatch, caplog) -> None:
    from persona_continuum.auth import credentials

    class InvalidClient(_Client):
        response_status = 401

    monkeypatch.setattr(credentials.httpx, "AsyncClient", InvalidClient)
    secret = "sk-never-log-this-secret-123456"
    app.credentials.create(
        credential_id="provider-log",
        provider=CredentialProvider.OPENAI,
        api_key=secret,
    )
    with caplog.at_level(logging.DEBUG):
        result = await app.credentials.test_connection("provider-log")
    assert secret not in caplog.text
    assert secret not in str(result)
