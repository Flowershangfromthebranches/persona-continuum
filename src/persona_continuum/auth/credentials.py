from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field

from persona_continuum.application._utils import new_id, parse_dt
from persona_continuum.storage.database import Database


def _redact_error(value: str) -> str:
    if "sk-" in value or "Bearer " in value or "xai-" in value:
        return "Provider request failed; secret-bearing details were redacted"
    return value


def build_runtime_environment(
    *,
    credential_manager: CredentialManager | None = None,
    credential_id: str | None = None,
    auth_env_var: str | None = None,
    target_name: str = "API_KEY",
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    """Compatibility wrapper around the unified encrypted credential runtime."""
    if credential_manager is not None:
        return credential_manager.build_process_environment(
            credential_id,
            target_name=target_name,
            overrides=overrides,
        )
    environment = dict(os.environ)
    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "DEEPSEEK_API_KEY",
        "XAI_API_KEY",
    ):
        environment.pop(name, None)
    if overrides:
        environment.update(overrides)
    if auth_env_var:
        raise RuntimeError(
            "Legacy environment credentials are disabled; configure CredentialManager instead"
        )
    return environment


class CredentialProvider(StrEnum):
    OPENAI = "openai"
    OPENAI_COMPATIBLE = "openai_compatible"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    DEEPSEEK = "deepseek"
    GROK = "grok"
    CUSTOM = "custom"


class CredentialRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    provider: CredentialProvider
    base_url: str
    header_names: list[str] = Field(default_factory=list)
    api_key_hint: str = ""
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class RuntimeCredential:
    id: str
    provider: CredentialProvider
    api_key: str
    base_url: str
    headers: dict[str, str]


class CredentialManager:
    """Owns encrypted-at-rest provider secrets and ephemeral runtime resolution."""

    DEFAULT_BASE_URLS = {
        CredentialProvider.OPENAI: "https://api.openai.com/v1",
        CredentialProvider.ANTHROPIC: "https://api.anthropic.com/v1",
        CredentialProvider.GOOGLE: "https://generativelanguage.googleapis.com/v1beta",
        CredentialProvider.DEEPSEEK: "https://api.deepseek.com/v1",
        CredentialProvider.GROK: "https://api.x.ai/v1",
    }

    def __init__(self, database: Database, key_path: Path) -> None:
        self.database = database
        self.key_path = key_path
        self._key: bytearray | None = None

    def create(
        self,
        *,
        provider: CredentialProvider | str,
        api_key: str,
        base_url: str | None = None,
        headers: dict[str, str] | None = None,
        credential_id: str | None = None,
    ) -> CredentialRecord:
        provider_value = (
            CredentialProvider.GOOGLE
            if str(provider).lower() == "gemini"
            else CredentialProvider(provider)
        )
        secret = api_key.strip()
        if not secret and provider_value not in {
            CredentialProvider.OPENAI_COMPATIBLE,
            CredentialProvider.CUSTOM,
        }:
            raise ValueError("api_key is required")
        resolved_base = (base_url or self.DEFAULT_BASE_URLS.get(provider_value) or "").rstrip("/")
        if not resolved_base:
            raise ValueError("base_url is required for this provider")
        record_id = credential_id or new_id("credential")
        now = datetime.now(UTC)
        encrypted = self._encrypt(
            record_id,
            {"api_key": secret, "headers": {str(k): str(v) for k, v in (headers or {}).items()}},
        )
        self.database.conn.execute(
            """
            INSERT INTO credentials (
              id, provider, base_url, encrypted_secret, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              provider = excluded.provider,
              base_url = excluded.base_url,
              encrypted_secret = excluded.encrypted_secret,
              updated_at = excluded.updated_at
            """,
            (
                record_id,
                provider_value.value,
                resolved_base,
                encrypted,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        self.database.conn.commit()
        return self.get_record(record_id)  # type: ignore[return-value]

    def get_record(self, credential_id: str) -> CredentialRecord | None:
        row = self.database.conn.execute(
            "SELECT * FROM credentials WHERE id = ?", (credential_id,)
        ).fetchone()
        if not row:
            return None
        payload = self._decrypt(str(row["id"]), bytes(row["encrypted_secret"]))
        key = str(payload.get("api_key", ""))
        headers = payload.get("headers", {})
        return CredentialRecord(
            id=str(row["id"]),
            provider=CredentialProvider(str(row["provider"])),
            base_url=str(row["base_url"]),
            header_names=sorted(str(name) for name in headers),
            api_key_hint=self._mask_key(key),
            created_at=parse_dt(row["created_at"]) or datetime.now(UTC),
            updated_at=parse_dt(row["updated_at"]) or datetime.now(UTC),
        )

    def list(self) -> list[CredentialRecord]:
        rows = self.database.conn.execute(
            "SELECT id FROM credentials ORDER BY created_at"
        ).fetchall()
        return [record for row in rows if (record := self.get_record(str(row["id"]))) is not None]

    def delete(self, credential_id: str) -> bool:
        cursor = self.database.conn.execute(
            "DELETE FROM credentials WHERE id = ?", (credential_id,)
        )
        self.database.conn.commit()
        return cursor.rowcount > 0

    def get(self, credential: CredentialProvider | str) -> RuntimeCredential:
        """Resolve an encrypted credential by id first, then by provider.

        This is the only runtime secret resolution entry point used by adapters.
        """
        identifier = (
            credential.value if isinstance(credential, CredentialProvider) else str(credential)
        )
        row = self.database.conn.execute(
            "SELECT * FROM credentials WHERE id = ?", (identifier,)
        ).fetchone()
        if not row:
            try:
                provider = (
                    CredentialProvider.GOOGLE.value
                    if identifier.lower() == "gemini"
                    else CredentialProvider(identifier).value
                )
            except ValueError:
                provider = identifier
            row = self.database.conn.execute(
                "SELECT * FROM credentials WHERE provider = ? ORDER BY updated_at DESC LIMIT 1",
                (provider,),
            ).fetchone()
        if not row:
            raise KeyError(f"Credential not found: {identifier}")
        payload = self._decrypt(str(row["id"]), bytes(row["encrypted_secret"]))
        return RuntimeCredential(
            id=str(row["id"]),
            provider=CredentialProvider(str(row["provider"])),
            api_key=str(payload.get("api_key", "")),
            base_url=str(row["base_url"]).rstrip("/"),
            headers={str(k): str(v) for k, v in dict(payload.get("headers", {})).items()},
        )

    def resolve(self, credential: CredentialProvider | str) -> RuntimeCredential:
        """Compatibility alias for callers migrating to get()."""
        return self.get(credential)

    def has(self, credential: CredentialProvider | str | None) -> bool:
        if not credential:
            return False
        try:
            self.get(credential)
        except KeyError:
            return False
        return True

    def request_headers(self, credential_id: str) -> tuple[str, dict[str, str]]:
        credential = self.get(credential_id)
        headers = dict(credential.headers)
        if credential.api_key:
            if credential.provider == CredentialProvider.ANTHROPIC:
                headers.setdefault("x-api-key", credential.api_key)
                headers.setdefault("anthropic-version", "2023-06-01")
            elif credential.provider == CredentialProvider.GOOGLE:
                headers.setdefault("x-goog-api-key", credential.api_key)
            else:
                headers.setdefault("Authorization", f"Bearer {credential.api_key}")
        return credential.base_url, headers

    def build_process_environment(
        self,
        credential: CredentialProvider | str | None = None,
        *,
        target_name: str | None = None,
        overrides: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build a subprocess environment without exposing ambient provider secrets."""
        environment = dict(os.environ)
        for name in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "DEEPSEEK_API_KEY",
            "XAI_API_KEY",
        ):
            environment.pop(name, None)
        if overrides:
            environment.update({str(key): str(value) for key, value in overrides.items()})
        if credential and self.has(credential):
            runtime = self.get(credential)
            env_name = (
                target_name
                or {
                    CredentialProvider.OPENAI: "OPENAI_API_KEY",
                    CredentialProvider.OPENAI_COMPATIBLE: "OPENAI_API_KEY",
                    CredentialProvider.ANTHROPIC: "ANTHROPIC_API_KEY",
                    CredentialProvider.GOOGLE: "GEMINI_API_KEY",
                    CredentialProvider.DEEPSEEK: "DEEPSEEK_API_KEY",
                    CredentialProvider.GROK: "XAI_API_KEY",
                    CredentialProvider.CUSTOM: "API_KEY",
                }[runtime.provider]
            )
            if runtime.api_key:
                environment[env_name] = runtime.api_key
        return environment

    async def test_connection(self, credential_id: str) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            base_url, headers = self.request_headers(credential_id)
            base_clean = base_url.rstrip("/")
            candidate_urls = [f"{base_clean}/models"]
            if not base_clean.endswith("/v1") and "/v1" not in base_clean:
                candidate_urls.append(f"{base_clean}/v1/models")
                candidate_urls.append(f"{base_clean}/api/tags")
                candidate_urls.append(f"{base_clean}/v1beta/models")

            response = None
            last_err = ""
            models: list[str] = []

            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                for url in candidate_urls:
                    try:
                        res = await client.get(url, headers=headers)
                        if res.status_code == 200:
                            response = res
                            break
                        last_err = self._safe_http_error(res.status_code, res.text)
                    except Exception as e:
                        last_err = _redact_error(str(e))

            latency = round((time.perf_counter() - started) * 1000, 1)
            if not response or response.status_code != 200:
                return {
                    "connected": False,
                    "status": "error",
                    "latency": latency,
                    "models": [],
                    "error": last_err or "Provider connection failed",
                }
            data = response.json()
            items = data.get("data") or data.get("models") or [] if isinstance(data, dict) else data
            for item in items if isinstance(items, list) else []:
                model_id = item.get("id") or item.get("name") if isinstance(item, dict) else item
                if model_id:
                    models.append(str(model_id).removeprefix("models/"))
            return {
                "connected": True,
                "status": "connected",
                "latency": latency,
                "models": models[:100],
                "error": None,
            }
        except Exception as exc:
            return {
                "connected": False,
                "status": "error",
                "latency": round((time.perf_counter() - started) * 1000, 1),
                "models": [],
                "error": _redact_error(str(exc)),
            }

    def close(self) -> None:
        if self._key is not None:
            for index in range(len(self._key)):
                self._key[index] = 0
            self._key = None

    def _encrypt(self, credential_id: str, payload: dict[str, Any]) -> bytes:
        nonce = os.urandom(12)
        plaintext = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ciphertext = AESGCM(bytes(self._encryption_key())).encrypt(
            nonce, plaintext, credential_id.encode("utf-8")
        )
        return nonce + ciphertext

    def _decrypt(self, credential_id: str, encrypted: bytes) -> dict[str, Any]:
        if len(encrypted) < 29:
            raise ValueError("Encrypted credential is invalid")
        plaintext = AESGCM(bytes(self._encryption_key())).decrypt(
            encrypted[:12], encrypted[12:], credential_id.encode("utf-8")
        )
        try:
            return dict(json.loads(plaintext.decode("utf-8")))
        finally:
            plaintext = b""

    def _encryption_key(self) -> bytearray:
        if self._key is not None:
            return self._key
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if self.key_path.exists():
            raw = self.key_path.read_bytes()
        else:
            raw = AESGCM.generate_key(bit_length=256)
            self.key_path.write_bytes(raw)
            self.key_path.chmod(0o600)
        if len(raw) != 32:
            raise ValueError("Credential encryption key must be 32 bytes")
        self._key = bytearray(raw)
        return self._key

    def _mask_key(self, api_key: str) -> str:
        if not api_key:
            return "not_required"
        if len(api_key) <= 8:
            return "••••••••"
        return f"{api_key[:3]}••••{api_key[-4:]}"

    def _safe_http_error(self, status_code: int, body: str) -> str:
        messages = {
            401: "Invalid API key or expired credential (HTTP 401)",
            403: "Credential lacks permission for the models endpoint (HTTP 403)",
            404: "Models endpoint not found; check the provider base URL (HTTP 404)",
        }
        return messages.get(
            status_code,
            f"Provider returned HTTP {status_code}: {_redact_error(body)[:200]}",
        )
