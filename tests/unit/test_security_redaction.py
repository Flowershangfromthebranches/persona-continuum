from __future__ import annotations

import os

import pytest

from persona_continuum.auth.profiles import (
    PlaintextSecretNotAllowedError,
    redact_secrets,
    resolve_api_key,
)


def test_redact_secrets_keys() -> None:
    raw_str = (
        "Error calling api with key sk-proj-1234567890abcdef1234567890 and Bearer secret_token_xyz"
    )
    redacted = redact_secrets(raw_str)
    assert "sk-proj-1234567890" not in redacted
    assert "[REDACTED_SECRET]" in redacted or "[REDACTED_API_KEY]" in redacted

    url = "https://api.example.com/v1?api_key=sk-12345678901234567890"
    redacted_url = redact_secrets(url)
    assert "sk-1234567890" not in redacted_url


def test_redact_secrets_dict() -> None:
    data = {
        "headers": {"Authorization": "Bearer my_super_secret_token_123"},
        "key": "sk-ant-api03-abcdef1234567890abcdef",
        "nested": {"deep": "Bearer deep_secret_token_456"},
    }
    redacted = redact_secrets(data)
    assert isinstance(redacted, dict)
    assert "my_super_secret_token_123" not in str(redacted)
    assert "sk-ant-api03" not in str(redacted)
    assert "deep_secret_token_456" not in str(redacted)


def test_resolve_api_key() -> None:
    os.environ["TEST_PC_SECRET_KEY"] = "sk-resolved-secret-999"
    try:
        resolved = resolve_api_key("TEST_PC_SECRET_KEY")
        assert resolved == "sk-resolved-secret-999"

        # Direct plaintext secrets MUST be rejected
        with pytest.raises((PlaintextSecretNotAllowedError, ValueError)):
            resolve_api_key("sk-direct-key-12345678901234567890")

        with pytest.raises((PlaintextSecretNotAllowedError, ValueError)):
            resolve_api_key("Bearer my-direct-token-xyz-123456")

        assert resolve_api_key(None) is None
    finally:
        os.environ.pop("TEST_PC_SECRET_KEY", None)


def test_secret_in_custom_header_rejected(tmp_path) -> None:
    from persona_continuum.auth.profiles import AuthProfileService
    from persona_continuum.storage.database import Database

    db = Database(tmp_path / "test_auth.db")
    db.migrate()
    auth_svc = AuthProfileService(db)

    # 1. Plaintext secret in Authorization header
    with pytest.raises(PlaintextSecretNotAllowedError):
        auth_svc.create_profile(
            name="Compromised Profile",
            base_url="https://api.openai.com/v1",
            headers={"Authorization": "Bearer sk-proj-1234567890abcdef1234567890"},
        )

    # 2. Plaintext secret in X-API-Key header
    with pytest.raises(PlaintextSecretNotAllowedError):
        auth_svc.create_profile(
            name="Compromised X-API-Key",
            base_url="https://api.anthropic.com/v1",
            headers={"X-API-Key": "sk-ant-api03-abcdef12345678901234567890"},
        )

    # 3. Environment variable reference dict is ALLOWED
    os.environ["VALID_TEST_VAR"] = "sk-resolved-valid-secret"
    try:
        prof = auth_svc.create_profile(
            name="Secure Profile",
            base_url="https://api.openai.com/v1",
            headers={"X-API-Key": {"env": "VALID_TEST_VAR"}},
        )
        assert prof.id is not None
        # SQLite should store the reference, not the plaintext
        stored = auth_svc.get_profile(prof.id)
        assert stored is not None
        assert "sk-resolved-valid-secret" not in str(stored.headers)
    finally:
        os.environ.pop("VALID_TEST_VAR", None)


def test_api_header_env_reference_resolves_at_runtime(tmp_path) -> None:
    from persona_continuum.auth.profiles import (
        AuthProfileService,
        MissingSecretEnvError,
        resolve_profile_headers,
    )
    from persona_continuum.storage.database import Database

    db = Database(tmp_path / "test_auth_runtime.db")
    db.migrate()
    auth_svc = AuthProfileService(db)

    os.environ["RUNTIME_SECRET_KEY"] = "sk-live-secret-value-777"
    try:
        prof = auth_svc.create_profile(
            name="Runtime Header Test",
            base_url="https://api.openai.com/v1",
            auth_env_var="RUNTIME_SECRET_KEY",
            headers={"X-Custom-Auth": {"env": "RUNTIME_SECRET_KEY"}},
        )

        # 1. SQLite contains env var reference, NOT plaintext
        stored = auth_svc.get_profile(prof.id)
        assert stored is not None
        assert "sk-live-secret-value-777" not in str(stored.headers)

        # 2. Runtime resolution produces actual secret in HTTP headers
        resolved = resolve_profile_headers(stored.headers, stored.auth_env_var)
        assert resolved["Authorization"] == "Bearer sk-live-secret-value-777"
        assert resolved["X-Custom-Auth"] == "sk-live-secret-value-777"

        # 3. Missing env var raises MissingSecretEnvError
        os.environ.pop("RUNTIME_SECRET_KEY")
        with pytest.raises(MissingSecretEnvError):
            resolve_profile_headers(stored.headers, stored.auth_env_var)
    finally:
        os.environ.pop("RUNTIME_SECRET_KEY", None)


@pytest.mark.anyio
async def test_openai_compatible_does_not_guess_reasoning_from_model_slug() -> None:
    from persona_continuum.agent.protocols.openai_compatible import (
        OpenAICompatibleAPIAdapter,
    )

    # Adapter with no explicit reasoning metadata in config
    adapter = OpenAICompatibleAPIAdapter(
        default_model="o3-mini",
        model_capabilities={},
    )

    models = await adapter.list_models()
    by_id = {m.id: m for m in models}

    assert "o3-mini" in by_id
    assert by_id["o3-mini"].supported_reasoning_efforts == []
    assert by_id["o3-mini"].default_reasoning_effort is None

    # With explicit model_capabilities configuration
    adapter_configured = OpenAICompatibleAPIAdapter(
        default_model="o3-mini",
        model_capabilities={
            "o3-mini": {
                "reasoning_efforts": ["low", "medium", "high"],
                "default_reasoning_effort": "medium",
            }
        },
    )
    models_conf = await adapter_configured.list_models()
    by_id_conf = {m.id: m for m in models_conf}
    assert by_id_conf["o3-mini"].supported_reasoning_efforts == ["low", "medium", "high"]
    assert by_id_conf["o3-mini"].default_reasoning_effort == "medium"
