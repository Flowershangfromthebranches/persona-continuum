from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

from persona_continuum.agent.adapter import AgentAdapter
from persona_continuum.agent.adapters import (
    ClaudeCodeAdapter,
    CodeBuddyAdapter,
    CodexAdapter,
    CommandCodeAdapter,
    CopilotAdapter,
    DeepSeekHarnessAdapter,
    FakeAgentAdapter,
    GeminiCliAdapter,
    GrokBuildAdapter,
    KimiAdapter,
    OpenCodeAdapter,
    QoderAdapter,
    QwenAdapter,
    WorkBuddyAdapter,
)
from persona_continuum.agent.manifest_adapter import ManifestAgentAdapter

try:
    from persona_continuum.agent.adapters import CursorAdapter
except ImportError:  # Cursor adapter module not present yet.
    CursorAdapter = None

if TYPE_CHECKING:
    from persona_continuum.auth.credentials import CredentialManager


class AgentRegistry:
    def __init__(
        self,
        include_builtins: bool = True,
        include_fake: bool = False,
        credential_manager: CredentialManager | None = None,
    ) -> None:
        self._adapters: dict[str, AgentAdapter] = {}
        self.credential_manager = credential_manager
        self._plugins_loaded = False
        if include_builtins:
            self._register_builtins(include_fake=include_fake)
        elif include_fake:
            self.register_adapter(FakeAgentAdapter())

    def _register_builtins(self, include_fake: bool = False) -> None:
        builtins: list[AgentAdapter] = [
            CodexAdapter(),
            GrokBuildAdapter(),
            ClaudeCodeAdapter(),
            GeminiCliAdapter(),
            OpenCodeAdapter(),
            CommandCodeAdapter(),
            QwenAdapter(),
            KimiAdapter(),
            CopilotAdapter(),
            QoderAdapter(),
            CodeBuddyAdapter(),
            WorkBuddyAdapter(),
            DeepSeekHarnessAdapter(),
        ]
        if CursorAdapter is not None:
            builtins.append(CursorAdapter())
        if include_fake:
            builtins.append(FakeAgentAdapter())
        for b in builtins:
            self.register_adapter(b)

    def register_adapter(self, adapter: AgentAdapter) -> None:
        if self.credential_manager is not None:
            cast(Any, adapter).credential_manager = self.credential_manager
        self._adapters[adapter.adapter_id] = adapter

    def unregister_adapter(self, adapter_id: str) -> None:
        self._adapters.pop(adapter_id, None)

    def get_adapter(self, adapter_id: str) -> AgentAdapter | None:
        return self._adapters.get(adapter_id)

    def get_all_adapters(self) -> list[AgentAdapter]:
        return list(self._adapters.values())

    async def probe_all(self) -> list[Any]:
        adapters = self.get_all_adapters()
        if not adapters:
            return []
        return list(await asyncio.gather(*(adapter.probe() for adapter in adapters)))

    def list_adapters(self) -> list[AgentAdapter]:
        return list(self._adapters.values())

    def load_manifests_from_dir(self, directory: Path | None = None) -> list[AgentAdapter]:
        if directory is None:
            directory = Path(os.path.expanduser("~/.persona-continuum/adapters"))
        if not directory.exists() or not directory.is_dir():
            return []

        loaded: list[AgentAdapter] = []
        for path in directory.iterdir():
            if path.suffix.lower() in {".toml", ".yaml", ".yml", ".json"}:
                try:
                    content = path.read_text(encoding="utf-8")
                    data: dict[str, Any] = {}
                    if path.suffix.lower() in {".toml"}:
                        try:
                            import tomllib

                            data = tomllib.loads(content)
                        except ImportError:
                            continue
                    elif path.suffix.lower() in {".yaml", ".yml"}:
                        data = yaml.safe_load(content) or {}
                    elif path.suffix.lower() == ".json":
                        data = json.loads(content)

                    if isinstance(data, dict) and ("id" in data or "name" in data):
                        adapter = ManifestAgentAdapter(data, source_path=path)
                        self.register_adapter(adapter)
                        loaded.append(adapter)
                except Exception:
                    pass
        return loaded

    def load_plugin_entrypoints(self) -> list[AgentAdapter]:
        if self._plugins_loaded:
            return []
        self._plugins_loaded = True
        loaded: list[AgentAdapter] = []
        try:
            entry_points = importlib.metadata.entry_points(group="persona_continuum.agent_adapters")
            for ep in entry_points:
                try:
                    adapter_cls = ep.load()
                    adapter_instance = adapter_cls()
                    if isinstance(adapter_instance, AgentAdapter):
                        self.register_adapter(adapter_instance)
                        loaded.append(adapter_instance)
                except Exception:
                    pass
        except Exception:
            pass
        return loaded
