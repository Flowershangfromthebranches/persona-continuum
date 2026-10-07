"""Runtime identity + credential-aware pool keys (P0.5 B/C)."""

from __future__ import annotations

from pathlib import Path

from persona_continuum.auth.credentials import CredentialManager, CredentialProvider
from persona_continuum.performance.runtime_identity import (
    binary_fingerprint,
    legacy_env_credential_fingerprint,
    resolve_runtime_identity,
)
from persona_continuum.performance.runtime_pool import AgentRuntimePool
from persona_continuum.storage.database import Database


def test_binary_fingerprint_tracks_path_and_version() -> None:
    assert binary_fingerprint("/usr/bin/grok", "1.0.25") == binary_fingerprint(
        "/usr/bin/grok", "1.0.25"
    )
    assert binary_fingerprint("/usr/bin/grok", "1.0.25") != binary_fingerprint(
        "/usr/bin/grok", "1.1.0"
    )
    assert binary_fingerprint("/usr/bin/grok", "1.0.25") != binary_fingerprint(
        "/opt/grok", "1.0.25"
    )


def test_legacy_env_fingerprint_covers_xai_and_hides_raw() -> None:
    a = legacy_env_credential_fingerprint({"XAI_API_KEY": "xai-secret-a"})
    b = legacy_env_credential_fingerprint({"XAI_API_KEY": "xai-secret-b"})
    c = legacy_env_credential_fingerprint({"GROK_TOKEN": "xai-secret-a"})
    assert a and a != b
    assert "xai-secret-a" not in a
    # A non-credential env var must not participate.
    assert legacy_env_credential_fingerprint({"PATH": "/usr/bin"}) == ""
    # GROK_ prefix is covered, so it is not silently ignored.
    assert c != ""


def test_pool_key_separates_credentials_and_is_stable() -> None:
    command = ["grok", "agent", "stdio"]
    key_a = AgentRuntimePool.make_key("grok", command, {"XAI_API_KEY": "xai-a"})
    key_b = AgentRuntimePool.make_key("grok", command, {"XAI_API_KEY": "xai-b"})
    key_a2 = AgentRuntimePool.make_key("grok", command, {"XAI_API_KEY": "xai-a"})
    assert key_a != key_b
    assert key_a == key_a2
    assert "xai-a" not in key_a and "xai-b" not in key_b


def test_pool_key_prefers_explicit_credential_identity() -> None:
    command = ["codex", "app-server"]
    key = AgentRuntimePool.make_key(
        "codex",
        command,
        {"OPENAI_API_KEY": "sk-ambient"},
        credential_identity="hash-1",
        binary_identity="bin-1",
        runtime_origin="openai",
    )
    same = AgentRuntimePool.make_key(
        "codex",
        command,
        {"OPENAI_API_KEY": "sk-ambient"},
        credential_identity="hash-1",
        binary_identity="bin-1",
        runtime_origin="openai",
    )
    other = AgentRuntimePool.make_key(
        "codex",
        command,
        {"OPENAI_API_KEY": "sk-ambient"},
        credential_identity="hash-2",
        binary_identity="bin-1",
        runtime_origin="openai",
    )
    assert key == same and key != other


def test_credential_manager_identity_hash_is_irreversible(tmp_path: Path) -> None:
    database = Database(tmp_path / "runtime.sqlite")
    database.migrate()
    manager = CredentialManager(database, tmp_path / "credential.key")
    manager.create(
        provider=CredentialProvider.GROK, api_key="xai-account-one", credential_id="grok-a"
    )
    manager.create(
        provider=CredentialProvider.GROK, api_key="xai-account-two", credential_id="grok-b"
    )
    hash_a = manager.credential_identity_hash("grok-a")
    hash_b = manager.credential_identity_hash("grok-b")
    assert hash_a and hash_b and hash_a != hash_b
    assert "xai-account-one" not in hash_a
    # Provider fallback resolves the most recently updated grok credential.
    assert manager.credential_identity_hash("grok") in {hash_a, hash_b}


class _StubAdapter:
    adapter_id = "grok"
    runtime_origin = "xai"
    binary_version = "1.0.25"
    credential_manager = None

    def _find_binary(self) -> str:
        return "/usr/local/bin/grok"

    def credential_identity_hash(self) -> str:
        return "adapter-hook-hash"


def test_resolve_runtime_identity_uses_adapter_hook() -> None:
    identity = resolve_runtime_identity(
        _StubAdapter(), model_id="grok-4.6", binary_version="1.0.25"
    )
    assert identity.adapter_id == "grok"
    assert identity.runtime_origin == "xai"
    assert identity.model_id == "grok-4.6"
    assert identity.credential_identity_hash == "adapter-hook-hash"
    assert identity.binary_identity == binary_fingerprint("/usr/local/bin/grok", "1.0.25")
