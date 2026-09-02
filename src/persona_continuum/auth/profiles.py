from __future__ import annotations

import os
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from persona_continuum.application._utils import dumps, loads, new_id, parse_dt
from persona_continuum.auth.credentials import CredentialManager, CredentialProvider
from persona_continuum.storage.database import Database

_SECRET_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9_\-]{20,}", re.IGNORECASE),
    re.compile(r"Bearer\s+([a-zA-Z0-9_\-\.]{15,})", re.IGNORECASE),
    re.compile(r"xai-[a-zA-Z0-9_\-]{20,}", re.IGNORECASE),
    re.compile(r"ghp_[a-zA-Z0-9]{30,}", re.IGNORECASE),
    re.compile(r"api[-_]?key\s*[:=]\s*['\"]?([a-zA-Z0-9_\-]{16,})['\"]?", re.IGNORECASE),
]


def redact_secrets(text: Any, extra_secrets: list[str] | None = None) -> Any:
    if isinstance(text, dict):
        return {k: redact_secrets(v, extra_secrets) for k, v in text.items()}
    if isinstance(text, list):
        return [redact_secrets(v, extra_secrets) for v in text]
    if not isinstance(text, str) or not text:
        return text
    redacted = text
    if extra_secrets:
        for secret in extra_secrets:
            if secret and len(secret) > 4:
                redacted = redacted.replace(secret, "[REDACTED_API_KEY]")
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    return redacted


_ENV_VAR_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PlaintextSecretNotAllowedError(ValueError):
    """Raised when a plaintext API key is passed instead of an env var identifier."""


def validate_auth_env_var(auth_env_var: str | None) -> str | None:
    if not auth_env_var:
        return None
    cleaned = auth_env_var.strip()
    if not cleaned:
        return None
    # Reject suspected plaintext secrets
    if (
        cleaned.startswith(("sk-", "Bearer ", "xai-", "ghp-", "ghp_"))
        or any(p.search(cleaned) for p in _SECRET_PATTERNS)
        or not _ENV_VAR_NAME_PATTERN.match(cleaned)
    ):
        raise PlaintextSecretNotAllowedError(
            "plaintext_secret_not_allowed: auth_env_var must be an environment variable name"
        )
    return cleaned


def resolve_api_key(auth_env_var: str | None) -> str | None:
    if not auth_env_var:
        return None
    cleaned_var = validate_auth_env_var(auth_env_var)
    if not cleaned_var:
        return None
    val = os.environ.get(cleaned_var)
    return val.strip() if val else None


class MissingSecretEnvError(ValueError):
    """Raised when an environment variable referenced in headers or auth_env_var is missing."""


def resolve_profile_headers(
    headers: dict[str, Any] | None,
    auth_env_var: str | None = None,
) -> dict[str, str]:
    resolved: dict[str, str] = {}
    if auth_env_var:
        key = resolve_api_key(auth_env_var)
        if not key:
            raise MissingSecretEnvError(f"missing_secret_env: {auth_env_var}")
        resolved["Authorization"] = f"Bearer {key}"

    if not headers:
        return resolved

    for k, v in headers.items():
        if isinstance(v, dict):
            env_var = str(v.get("env") or v.get("env_var", "")).strip()
            if env_var:
                val = os.environ.get(env_var)
                if not val:
                    raise MissingSecretEnvError(f"missing_secret_env: {env_var}")
                resolved[k] = val
                continue
        elif isinstance(v, str):
            if v.startswith("{") and "env" in v:
                try:
                    data = loads(v)
                    if isinstance(data, dict) and "env" in data:
                        env_var = str(data["env"]).strip()
                        val = os.environ.get(env_var)
                        if not val:
                            raise MissingSecretEnvError(f"missing_secret_env: {env_var}")
                        resolved[k] = val
                        continue
                except Exception:
                    pass
            if v.startswith("$"):
                env_var = v[1:].strip()
                val = os.environ.get(env_var)
                if not val:
                    raise MissingSecretEnvError(f"missing_secret_env: {env_var}")
                resolved[k] = val
                continue
            resolved[k] = v
        else:
            resolved[k] = str(v)
    return resolved


class AuthProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    auth_type: str = "local_cli"  # local_cli, chatgpt_account, env_var, keychain
    name: str = "Default"
    env_var_name: str | None = None
    account_name: str | None = None
    provider_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class APIProfileRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    provider_type: str = "openai_compatible"
    base_url: str
    auth_env_var: str | None = None
    default_model: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    credential_status: str = "not_configured"
    api_key_hint: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


_SENSITIVE_HEADER_KEYS = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "apikey",
    "x-auth-token",
    "secret",
    "token",
}


def validate_headers(headers: dict[str, Any] | None) -> dict[str, str]:
    if not headers:
        return {}
    validated: dict[str, str] = {}
    for k, v in headers.items():
        k_lower = k.lower().strip()
        if isinstance(v, dict):
            env_var = str(v.get("env") or v.get("env_var", "")).strip()
            if not env_var or not _ENV_VAR_NAME_PATTERN.match(env_var):
                raise PlaintextSecretNotAllowedError(
                    f"plaintext_secret_not_allowed: Header '{k}' invalid env var reference"
                )
            validated[k] = dumps({"env": env_var})
            continue

        val_str = str(v).strip()
        if k_lower in _SENSITIVE_HEADER_KEYS and (
            val_str.startswith(("sk-", "Bearer ", "xai-", "ghp-", "ghp_"))
            or any(p.search(val_str) for p in _SECRET_PATTERNS)
            or (
                len(val_str) > 15
                and not val_str.startswith("$")
                and not _ENV_VAR_NAME_PATTERN.match(val_str)
            )
        ):
            raise PlaintextSecretNotAllowedError(
                f"plaintext_secret_not_allowed: Header '{k}' cannot contain plaintext secrets."
            )

        if any(p.search(val_str) for p in _SECRET_PATTERNS):
            raise PlaintextSecretNotAllowedError(
                f"plaintext_secret_not_allowed: Header '{k}' contains plaintext secret pattern"
            )

        validated[k] = val_str
    return validated


def _clean_base_url(url: str) -> str:
    u = url.strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/chat"):
        if u.endswith(suffix):
            u = u[: -len(suffix)].rstrip("/")
    return u


class AuthProfileService:
    def __init__(
        self, database: Database, credential_manager: CredentialManager | None = None
    ) -> None:
        self.database = database
        self.credential_manager = credential_manager

    def create_profile(
        self,
        name: str,
        base_url: str,
        provider_type: str = "openai_compatible",
        auth_env_var: str | None = None,
        default_model: str | None = None,
        headers: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        profile_id: str | None = None,
        api_key: str | None = None,
    ) -> APIProfileRecord:
        record_id = profile_id or new_id("apiprof")
        now = datetime.now(UTC)
        validated_env_var = validate_auth_env_var(auth_env_var)
        validated_headers = (
            {str(key): "[ENCRYPTED]" for key in (headers or {})}
            if api_key is not None and self.credential_manager
            else validate_headers(headers)
        )
        cleaned_url = _clean_base_url(base_url)
        record = APIProfileRecord(
            id=record_id,
            name=name,
            provider_type=provider_type,
            base_url=cleaned_url,
            auth_env_var=validated_env_var,
            default_model=default_model.strip() if default_model else None,
            headers=validated_headers,
            metadata=metadata or {},
            created_at=now,
            updated_at=now,
        )
        self.database.conn.execute(
            """
            INSERT INTO api_profiles (
                id, name, provider_type, base_url, auth_env_var, default_model,
                headers_json, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.name,
                record.provider_type,
                record.base_url,
                record.auth_env_var,
                record.default_model,
                dumps(record.headers),
                dumps(record.metadata),
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
            ),
        )
        self.database.conn.commit()
        if api_key is not None and self.credential_manager:
            self.credential_manager.create(
                provider=(
                    CredentialProvider.GOOGLE
                    if provider_type.lower() == "gemini"
                    else CredentialProvider(provider_type)
                ),
                api_key=api_key,
                base_url=cleaned_url,
                headers={str(key): str(value) for key, value in (headers or {}).items()},
                credential_id=record.id,
            )
        return self.get_profile(record.id) or record

    def list_profiles(self) -> list[APIProfileRecord]:
        rows = self.database.conn.execute(
            "SELECT * FROM api_profiles ORDER BY created_at"
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def get_profile(self, profile_id: str) -> APIProfileRecord | None:
        row = self.database.conn.execute(
            "SELECT * FROM api_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        return self._row_to_record(row) if row else None

    def update_profile(
        self,
        profile_id: str,
        *,
        name: str,
        base_url: str,
        provider_type: str,
        auth_env_var: str | None = None,
        default_model: str | None = None,
        headers: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        api_key: str | None = None,
    ) -> APIProfileRecord:
        """Update provider metadata without requiring a secret replacement."""

        existing = self.get_profile(profile_id)
        if existing is None:
            raise KeyError(f"API profile not found: {profile_id}")
        validated_env_var = validate_auth_env_var(auth_env_var)
        validated_headers = (
            {str(key): "[ENCRYPTED]" for key in (headers or {})}
            if api_key is not None and self.credential_manager
            else validate_headers(headers if headers is not None else existing.headers)
        )
        cleaned_url = _clean_base_url(base_url)
        now = datetime.now(UTC).isoformat()
        self.database.conn.execute(
            """
            UPDATE api_profiles
            SET name = ?, provider_type = ?, base_url = ?, auth_env_var = ?,
                default_model = ?, headers_json = ?, metadata_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                str(name or existing.name).strip(),
                str(provider_type or existing.provider_type),
                cleaned_url,
                validated_env_var,
                default_model.strip() if default_model else None,
                dumps(validated_headers),
                dumps(metadata if metadata is not None else existing.metadata),
                now,
                profile_id,
            ),
        )
        self.database.conn.commit()
        if api_key is not None and self.credential_manager:
            self.credential_manager.create(
                provider=(
                    CredentialProvider.GOOGLE
                    if provider_type.lower() == "gemini"
                    else CredentialProvider(provider_type)
                ),
                api_key=api_key,
                base_url=cleaned_url,
                headers={str(key): str(value) for key, value in (headers or {}).items()},
                credential_id=profile_id,
            )
        updated = self.get_profile(profile_id)
        if updated is None:
            raise KeyError(f"API profile not found after update: {profile_id}")
        return updated

    def delete_profile(self, profile_id: str) -> bool:
        cursor = self.database.conn.execute("DELETE FROM api_profiles WHERE id = ?", (profile_id,))
        self.database.conn.commit()
        if self.credential_manager:
            self.credential_manager.delete(profile_id)
        return cursor.rowcount > 0

    async def test_connection(self, profile_id: str) -> dict[str, Any]:
        profile = self.get_profile(profile_id)
        if not profile:
            return {
                "ok": False,
                "connected": False,
                "auth_valid": False,
                "latency_ms": 0,
                "latency": 0.0,
                "model_count": 0,
                "models": [],
                "error_code": "profile_not_found",
                "error_message": f"提供方配置未找到: {profile_id}",
                "error": f"提供方配置未找到: {profile_id}",
            }

        if self.credential_manager and self.credential_manager.has(profile_id):
            result = await self.credential_manager.test_connection(profile_id)
            err = result.get("error")
            err_code = (
                "auth_failed"
                if err and "401" in str(err)
                else ("connection_failed" if err else None)
            )
            return {
                "ok": result["connected"],
                "connected": result["connected"],
                "auth_valid": result["connected"] or (err_code != "auth_failed"),
                "status": result["status"],
                "latency": result["latency"],
                "latency_ms": int(result["latency"]),
                "model_count": len(result["models"]),
                "models": result["models"],
                "error_code": err_code,
                "error_message": err,
                "error": err,
            }

        # Fallback to env var resolution
        if not profile.auth_env_var:
            return {
                "ok": False,
                "connected": False,
                "auth_valid": False,
                "latency_ms": 0,
                "latency": 0.0,
                "model_count": 0,
                "models": [],
                "error_code": "missing_env_var_config",
                "error_message": "未配置 API 密钥或环境变量名",
                "error": "未配置 API 密钥或环境变量名",
            }

        env_val = os.environ.get(profile.auth_env_var)
        if not env_val:
            return {
                "ok": False,
                "connected": False,
                "auth_valid": False,
                "latency_ms": 0,
                "latency": 0.0,
                "model_count": 0,
                "models": [],
                "error_code": "env_var_not_found",
                "error_message": f"未在系统中检测到环境变量 [{profile.auth_env_var}]",
                "error": f"未在系统中检测到环境变量 [{profile.auth_env_var}]",
            }

        started = time.perf_counter()
        base_clean = profile.base_url.rstrip("/")
        headers = dict(profile.headers or {})
        headers.setdefault("Authorization", f"Bearer {env_val}")
        candidate_urls = [
            f"{base_clean}/models",
            f"{base_clean}/v1/models",
            f"{base_clean}/api/tags",
        ]

        models: list[str] = []
        last_err = ""
        success = False
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                for url in candidate_urls:
                    try:
                        res = await client.get(url, headers=headers)
                        if res.status_code == 200:
                            success = True
                            data = res.json()
                            raw_items = (
                                data.get("data") or data.get("models") or []
                                if isinstance(data, dict)
                                else data
                            )
                            items = raw_items if isinstance(raw_items, list) else []
                            for item in items:
                                mid = (
                                    item.get("id") or item.get("name")
                                    if isinstance(item, dict)
                                    else item
                                )
                                if mid:
                                    models.append(str(mid).removeprefix("models/"))
                            break
                        last_err = f"HTTP_{res.status_code}: {res.text[:120]}"
                    except Exception as exc:
                        last_err = str(exc)
        except Exception as exc:
            last_err = str(exc)

        latency = round((time.perf_counter() - started) * 1000, 1)
        if not success and profile.default_model:
            models.append(profile.default_model)
            success = True

        err_code = (
            "auth_failed" if "401" in last_err else ("connection_failed" if not success else None)
        )
        return {
            "ok": success,
            "connected": success,
            "auth_valid": success or (err_code != "auth_failed"),
            "status": "connected" if success else "error",
            "latency": latency,
            "latency_ms": int(latency),
            "model_count": len(models),
            "models": models[:100],
            "error_code": err_code,
            "error_message": last_err if not success else None,
            "error": last_err if not success else None,
        }

    def _row_to_record(self, row: Any) -> APIProfileRecord:
        raw_headers = dict(loads(row["headers_json"]))
        sanitized_headers = redact_secrets(raw_headers)
        credential = (
            self.credential_manager.get_record(str(row["id"])) if self.credential_manager else None
        )
        return APIProfileRecord(
            id=str(row["id"]),
            name=str(row["name"]),
            provider_type=str(row["provider_type"]),
            base_url=str(row["base_url"]),
            auth_env_var=row["auth_env_var"],
            default_model=row["default_model"],
            headers=sanitized_headers if isinstance(sanitized_headers, dict) else {},
            metadata=dict(loads(row["metadata_json"])),
            credential_status="encrypted" if credential else "not_configured",
            api_key_hint=credential.api_key_hint if credential else "",
            created_at=parse_dt(row["created_at"]) or datetime.now(UTC),
            updated_at=parse_dt(row["updated_at"]) or datetime.now(UTC),
        )
