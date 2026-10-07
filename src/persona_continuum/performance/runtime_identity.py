"""Stable runtime identity for concurrency capability binding.

A verified independent-session capability is only valid for the exact runtime
that was probed: adapter, CLI binary identity/version, model, credential
identity, and runtime origin.  This module derives those identities without
ever storing (or logging) a raw secret.

The binary fingerprint hashes the resolved absolute path plus the reported
version — never the whole binary — so a rebuild at a different path or an
upgraded CLI is a different identity.

Credential identity resolution order:

1. ``CredentialManager`` (encrypted store) when one is available;
2. an adapter-provided ``credential_identity_hash()`` hook (e.g. a local OAuth
   auth store used by a vendor CLI);
3. a legacy environment-variable fingerprint;
4. empty ("unknown"), which simply means the capability is not credential-bound.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from persona_continuum.performance.runtime_capability_store import (
    PROBE_CONTRACT_VERSION,
    RuntimeIdentity,
)

# Credential-bearing env prefixes/names per runtime family.  This is only a
# fallback for adapters that have not migrated to CredentialManager; the pool
# must not grow an ever-longer guess table as the primary mechanism.
_CREDENTIAL_ENV_PREFIXES: tuple[str, ...] = (
    "XAI_",
    "GROK_",
    "OPENAI_",
    "ANTHROPIC_",
    "GEMINI_",
    "GOOGLE_",
    "CODEX_",
    "CLAUDE_",
    "ACP_",
    "DEEPSEEK_",
    "OPENROUTER_",
    "ALIBABA_",
    "DASHSCOPE_",
    "AZURE_",
    "MOONSHOT_",
    "MINIMAX_",
    "ZHIPU_",
)
_CREDENTIAL_ENV_TOKENS: tuple[str, ...] = ("KEY", "TOKEN", "SECRET", "CREDENTIAL", "AUTH")

# Known local auth stores that a vendor CLI uses instead of an env var.  Only
# the file *content hash* is retained; the token itself is never stored.
_LOCAL_AUTH_STORES: dict[str, tuple[str, ...]] = {
    "grok": ("~/.grok/auth.json",),
    "xai": ("~/.grok/auth.json",),
    "gemini_cli": ("~/.gemini/oauth_creds.json",),
    "codex": ("~/.codex/auth.json",),
    "claude": ("~/.claude/.credentials.json",),
}


def binary_fingerprint(path: str | None, version: str | None) -> str:
    """Stable, non-reversible identity for a resolved CLI binary + version."""

    if not path and not version:
        return ""
    absolute = ""
    if path:
        try:
            absolute = str(Path(path).expanduser().resolve())
        except (OSError, RuntimeError):
            absolute = str(path)
    material = f"{absolute}\x1f{version or ''}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _is_credential_env(name: str) -> bool:
    upper = name.upper()
    if not any(upper.startswith(prefix) for prefix in _CREDENTIAL_ENV_PREFIXES):
        return False
    return any(token in upper for token in _CREDENTIAL_ENV_TOKENS)


def legacy_env_credential_fingerprint(env: dict[str, str] | None = None) -> str:
    """Hash the credential-bearing env subset.  No raw value is retained."""

    source = dict(os.environ) if env is None else dict(env)
    interesting = {
        name: value
        for name, value in source.items()
        if value and _is_credential_env(name)
    }
    if not interesting:
        return ""
    material = "\x1f".join(
        f"{name}={interesting[name]}" for name in sorted(interesting)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def local_auth_store_fingerprint(paths: tuple[str, ...]) -> str:
    """Hash a vendor CLI's local auth store content (never the token)."""

    digests: list[str] = []
    for raw in paths:
        candidate = Path(raw).expanduser()
        try:
            if candidate.is_file():
                digest = hashlib.sha256(candidate.read_bytes()).hexdigest()[:32]
                digests.append(f"{raw}:{digest}")
        except OSError:
            continue
    if not digests:
        return ""
    return hashlib.sha256("\x1f".join(sorted(digests)).encode("utf-8")).hexdigest()[:32]


def _adapter_binary_path(adapter: Any) -> str | None:
    finder = getattr(adapter, "_find_binary", None)
    if callable(finder):
        try:
            resolved = finder()
        except Exception:
            resolved = None
        if resolved:
            return str(resolved)
    for attribute in ("binary_path", "_resolved_binary", "binary"):
        value = getattr(adapter, attribute, None)
        if isinstance(value, str) and value:
            return value
    return None


def _adapter_runtime_origin(adapter: Any, fallback: str) -> str:
    for attribute in ("runtime_origin", "provider"):
        value = getattr(adapter, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def resolve_credential_identity(
    adapter: Any,
    *,
    credential_manager: Any | None = None,
    credential_id: str | None = None,
) -> str:
    """Best available irreversible credential identity for this runtime."""

    manager = credential_manager or getattr(adapter, "credential_manager", None)
    if manager is not None:
        identifier = credential_id
        if not identifier:
            candidates = getattr(adapter, "credential_candidates", None)
            if callable(candidates):
                with_manager = candidates()
                if with_manager:
                    identifier = str(with_manager)
        if not identifier:
            identifier = str(
                getattr(adapter, "adapter_id", "") or getattr(adapter, "name", "") or ""
            )
        hasher = getattr(manager, "credential_identity_hash", None)
        if callable(hasher) and identifier:
            try:
                identity = str(hasher(identifier))
            except Exception:
                identity = ""
            if identity:
                return identity
    hook = getattr(adapter, "credential_identity_hash", None)
    if callable(hook):
        try:
            identity = str(hook())
        except Exception:
            identity = ""
        if identity:
            return identity
    adapter_id = str(getattr(adapter, "adapter_id", "") or "")
    stores = _LOCAL_AUTH_STORES.get(adapter_id)
    if stores:
        identity = local_auth_store_fingerprint(stores)
        if identity:
            return identity
    return legacy_env_credential_fingerprint()


def resolve_runtime_identity(
    adapter: Any,
    *,
    model_id: str = "",
    credential_manager: Any | None = None,
    credential_id: str | None = None,
    binary_version: str | None = None,
    probe_version: str = PROBE_CONTRACT_VERSION,
) -> RuntimeIdentity:
    """Build the full identity a concurrency capability is bound to."""

    adapter_id = str(getattr(adapter, "adapter_id", "") or "")
    binary_path = _adapter_binary_path(adapter)
    version = binary_version
    if version is None:
        declared = getattr(adapter, "binary_version", None)
        version = str(declared) if declared else ""
    return RuntimeIdentity(
        adapter_id=adapter_id,
        binary_identity=binary_fingerprint(binary_path, version),
        binary_version=str(version or ""),
        model_id=str(model_id or ""),
        credential_identity_hash=resolve_credential_identity(
            adapter, credential_manager=credential_manager, credential_id=credential_id
        ),
        runtime_origin=_adapter_runtime_origin(adapter, adapter_id),
        probe_version=probe_version,
    )


__all__ = [
    "binary_fingerprint",
    "legacy_env_credential_fingerprint",
    "local_auth_store_fingerprint",
    "resolve_credential_identity",
    "resolve_runtime_identity",
]
