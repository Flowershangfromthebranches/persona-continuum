from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from persona_continuum.agent.adapters.codex import CodexAdapter, _reap_subprocess

pytest.importorskip("persona_continuum.agent.adapters.cursor")

from persona_continuum.agent.adapters.cursor import CursorAdapter  # noqa: E402

from persona_continuum.agent.adapters.grok import GrokBuildAdapter
from persona_continuum.agent.manifest_adapter import ManifestAgentAdapter
from persona_continuum.agent.models import AgentSessionConfig, AgentStatus
from persona_continuum.agent.protocols import acp as acp_protocol
from persona_continuum.agent.protocols.acp import ACPAdapter
from persona_continuum.agent.protocols.jsonrpc_stdio import JsonRpcStdioAdapter


def _make_executable(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.anyio
async def test_codex_subprocess_cleanup_reaps_running_child() -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    await _reap_subprocess(proc, timeout=0.1)

    assert proc.returncode is not None


def _codex_model_server(tmp_path: Path, pages: dict[str, object]) -> Path:
    return _make_executable(
        tmp_path / "codex_model_server.py",
        f"""#!/usr/bin/env python3
import json
import sys

PAGES = json.loads({json.dumps(json.dumps(pages))})

while True:
    line = sys.stdin.readline()
    if not line:
        break
    request = json.loads(line)
    request_id = request.get("id")
    method = request.get("method")
    if method == "initialize":
        response = {{"jsonrpc": "2.0", "id": request_id, "result": {{}}}}
    elif method == "model/list":
        cursor = request.get("params", {{}}).get("cursor") or "__first__"
        response = {{"jsonrpc": "2.0", "id": request_id, "result": PAGES[cursor]}}
    else:
        continue
    sys.stdout.write(json.dumps(response) + "\\n")
    sys.stdout.flush()
""",
    )


def _strict_acp_server(
    tmp_path: Path,
    *,
    name: str,
    auth_methods: list[dict[str, object]],
    reject_session_new: bool = False,
) -> tuple[Path, Path]:
    request_log = tmp_path / f"{name}.jsonl"
    script = _make_executable(
        tmp_path / f"{name}.py",
        f"""#!/usr/bin/env python3
import json
import sys

LOG_PATH = {str(request_log)!r}
AUTH_METHODS = json.loads({json.dumps(json.dumps(auth_methods))})
REJECT_SESSION_NEW = {reject_session_new!r}

if "--version" in sys.argv:
    print("mock-acp 1.0")
    raise SystemExit(0)
if "models" in sys.argv:
    print("grok-4.6")
    print("grok-4.5")
    raise SystemExit(0)

while True:
    line = sys.stdin.readline()
    if not line:
        break
    request = json.loads(line)
    with open(LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(request) + "\\n")
    request_id = request.get("id")
    method = request.get("method")
    if method == "initialize":
        params = request.get("params", {{}})
        if set(params) != {{"protocolVersion", "clientCapabilities"}}:
            response = {{
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {{"code": -32602, "message": "invalid initialize params"}},
            }}
        else:
            response = {{
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {{"protocolVersion": 1, "authMethods": AUTH_METHODS}},
            }}
    elif method == "authenticate":
        params = request.get("params", {{}})
        if "token" in params or set(params) - {{"methodId", "_meta"}}:
            response = {{
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {{"code": -32602, "message": "invalid authenticate params"}},
            }}
        elif params.get("methodId") not in {{item["id"] for item in AUTH_METHODS}}:
            response = {{
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {{"code": -32602, "message": "unknown auth method"}},
            }}
        else:
            response = {{"jsonrpc": "2.0", "id": request_id, "result": {{"authenticated": True}}}}
    elif method == "session/new":
        if REJECT_SESSION_NEW:
            response = {{
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {{"code": -32000, "message": "session rejected"}},
            }}
        else:
            response = {{
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {{"sessionId": "real-session-id"}},
            }}
    else:
        continue
    sys.stdout.write(json.dumps(response) + "\\n")
    sys.stdout.flush()
""",
    )
    return script, request_log


def _session_config(**kwargs: object) -> AgentSessionConfig:
    return AgentSessionConfig(
        room_id="room",
        participant_id="participant",
        persona_id="persona",
        **kwargs,
    )


@pytest.mark.anyio
async def test_codex_model_list_reasoning_option_objects(tmp_path: Path) -> None:
    server = _codex_model_server(
        tmp_path,
        {
            "__first__": {
                "data": [
                    {
                        "id": "gpt-official",
                        "displayName": "GPT Official",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "max", "description": "Maximum"},
                            {"reasoningEffort": "focused", "description": "Focused"},
                        ],
                        "defaultReasoningEffort": "focused",
                    }
                ],
                "nextCursor": None,
            }
        },
    )
    adapter = CodexAdapter()
    adapter._resolved_binary = str(server)

    models = await adapter.list_models()

    assert models[0].supported_reasoning_efforts == ["max", "focused"]
    assert models[0].default_reasoning_effort == "focused"


@pytest.mark.anyio
async def test_codex_model_list_reasoning_object_schema(tmp_path: Path) -> None:
    server = _codex_model_server(
        tmp_path,
        {
            "__first__": {
                "data": [
                    {
                        "id": "object-schema-model",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "xhigh", "description": "Extra high"},
                            {
                                "reasoningEffort": "future-adaptive",
                                "description": "Future server value",
                            },
                        ],
                        "defaultReasoningEffort": "future-adaptive",
                    }
                ],
                "nextCursor": None,
            }
        },
    )
    adapter = CodexAdapter()
    adapter._resolved_binary = str(server)

    models = await adapter.list_models()

    assert models[0].supported_reasoning_efforts == ["xhigh", "future-adaptive"]
    assert all(isinstance(effort, str) for effort in models[0].supported_reasoning_efforts)
    assert models[0].default_reasoning_effort == "future-adaptive"
    assert models[0].source == "protocol_model_list"


@pytest.mark.anyio
async def test_codex_model_list_preserves_unknown_effort(tmp_path: Path) -> None:
    server = _codex_model_server(
        tmp_path,
        {
            "__first__": {
                "data": [
                    {
                        "id": "future-model",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "future-ultra", "description": "Future"}
                        ],
                        "defaultReasoningEffort": "future-ultra",
                    }
                ],
                "nextCursor": None,
            }
        },
    )
    adapter = CodexAdapter()
    adapter._resolved_binary = str(server)

    models = await adapter.list_models()

    assert models[0].supported_reasoning_efforts == ["future-ultra"]
    assert models[0].default_reasoning_effort == "future-ultra"


@pytest.mark.anyio
async def test_codex_model_list_pagination(tmp_path: Path) -> None:
    server = _codex_model_server(
        tmp_path,
        {
            "__first__": {
                "data": [{"id": "page-one", "supportedReasoningEfforts": []}],
                "nextCursor": "page-2",
            },
            "page-2": {
                "data": [{"id": "page-two", "supportedReasoningEfforts": []}],
                "nextCursor": None,
            },
        },
    )
    adapter = CodexAdapter()
    adapter._resolved_binary = str(server)

    models = await adapter.list_models()

    assert [model.id for model in models] == ["page-one", "page-two"]
    assert all(model.source == "protocol_model_list" for model in models)


@pytest.mark.anyio
async def test_codex_model_schema_error_not_silent_dynamic_fallback(tmp_path: Path) -> None:
    server = _codex_model_server(
        tmp_path,
        {"__first__": {"data": [{"id": "broken", "supportedReasoningEfforts": [{}]}]}},
    )
    adapter = CodexAdapter()
    adapter._resolved_binary = str(server)

    models = await adapter.list_models()

    assert adapter.model_discovery_error
    assert all(model.source != "dynamic" for model in models)


test_codex_official_model_list_object_shape = test_codex_model_list_reasoning_option_objects


@pytest.mark.anyio
async def test_grok_official_auth_params_exact_shape(tmp_path: Path, app) -> None:
    server, log = _strict_acp_server(
        tmp_path,
        name="grok_shape",
        auth_methods=[
            {"id": "xai.api_key", "name": "API key"},
            {"id": "cached_token", "name": "Cached login"},
        ],
    )
    app.credentials.create(
        credential_id="grok-shape",
        provider="grok",
        api_key="xai-grok-shape-secret-123456",
    )
    adapter = GrokBuildAdapter()
    adapter.credential_manager = app.credentials
    adapter._resolved_binary = str(server)

    session = await adapter.create_session(_session_config(auth_profile_id="grok-shape"))
    await adapter.close(session)

    requests = [json.loads(line) for line in log.read_text().splitlines()]
    initialize = next(item for item in requests if item.get("method") == "initialize")
    authenticate = next(item for item in requests if item.get("method") == "authenticate")
    assert initialize["params"] == {"protocolVersion": 1, "clientCapabilities": {}}
    assert authenticate["params"] == {
        "methodId": "xai.api_key",
        "_meta": {"headless": True},
    }
    assert "token" not in authenticate["params"]
    assert session.session_data["acp_session_id"] == "real-session-id"


@pytest.mark.anyio
async def test_grok_env_key_does_not_bypass_credential_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _ = _strict_acp_server(
        tmp_path,
        name="grok_env_probe",
        auth_methods=[{"id": "xai.api_key", "name": "API key"}],
    )
    monkeypatch.setenv("XAI_API_KEY", "test")
    adapter = GrokBuildAdapter()
    adapter._resolved_binary = str(server)

    assert (await adapter.probe()).status == AgentStatus.AUTH_REQUIRED


@pytest.mark.anyio
async def test_grok_cached_login_can_probe_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _ = _strict_acp_server(
        tmp_path,
        name="grok_cached_probe",
        auth_methods=[{"id": "cached_token", "name": "Cached login"}],
    )
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    adapter = GrokBuildAdapter()
    adapter._resolved_binary = str(server)

    assert (await adapter.probe()).status == AgentStatus.READY


test_grok_auth_methods_not_automatically_auth_required = test_grok_cached_login_can_probe_ready


def test_grok_46_efforts_exact() -> None:
    model = {item.id: item for item in GrokBuildAdapter()._fallback_models()}["grok-4.6"]
    assert model.supported_reasoning_efforts == ["low", "medium", "high", "xhigh"]
    assert model.default_reasoning_effort == "high"
    assert model.source == "official_capability_table"
    assert model.verified_for_version


def test_grok_45_efforts_exact() -> None:
    model = {item.id: item for item in GrokBuildAdapter()._fallback_models()}["grok-4.5"]
    assert model.supported_reasoning_efforts == ["low", "medium", "high"]
    assert model.default_reasoning_effort == "high"
    assert model.source == "official_capability_table"
    assert model.verified_for_version


@pytest.mark.anyio
async def test_generic_acp_authenticate_has_no_token(
    tmp_path: Path,
) -> None:
    server, log = _strict_acp_server(
        tmp_path,
        name="generic_agent_auth",
        auth_methods=[{"id": "agent-default", "type": "agent"}],
    )
    adapter = ACPAdapter("generic", "Generic", [str(server)], acp_args=[])
    adapter._resolved_binary = str(server)

    session = await adapter.create_session(_session_config())
    await adapter.close(session)

    requests = [json.loads(line) for line in log.read_text().splitlines()]
    authenticate = next(item for item in requests if item.get("method") == "authenticate")
    assert authenticate["params"] == {"methodId": "agent-default"}
    assert "token" not in authenticate["params"]


@pytest.mark.anyio
async def test_generic_acp_terminal_auth_not_sent_to_authenticate(tmp_path: Path) -> None:
    server, log = _strict_acp_server(
        tmp_path,
        name="generic_terminal_auth",
        auth_methods=[{"id": "terminal-login", "type": "terminal"}],
    )
    adapter = ACPAdapter("generic", "Generic", [str(server)], acp_args=[])
    adapter._resolved_binary = str(server)

    interactive_error = getattr(
        acp_protocol,
        "InteractiveAuthRequiredError",
        type("MissingInteractiveAuthRequiredError", (Exception,), {}),
    )
    with pytest.raises(interactive_error):
        await adapter.create_session(_session_config())

    requests = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any(item.get("method") == "authenticate" for item in requests)


@pytest.mark.anyio
async def test_generic_acp_ready_requires_session_new(tmp_path: Path) -> None:
    server, _ = _strict_acp_server(
        tmp_path,
        name="generic_session_rejected",
        auth_methods=[],
        reject_session_new=True,
    )
    adapter = ACPAdapter("generic", "Generic", [str(server)], acp_args=[])
    adapter._resolved_binary = str(server)

    assert (await adapter.probe()).status != AgentStatus.READY


def test_generic_acp_does_not_fabricate_reasoning() -> None:
    adapter = ACPAdapter("generic", "Generic", ["missing"])
    models = adapter._parse_models("model-a\nmodel-b\n")
    assert models
    assert all(model.supported_reasoning_efforts == [] for model in models)
    assert all(model.default_reasoning_effort is None for model in models)


@pytest.mark.anyio
async def test_cursor_effort_flag_without_enum_does_not_fabricate_values(tmp_path: Path) -> None:
    binary = _make_executable(
        tmp_path / "cursor_agent.py",
        """#!/usr/bin/env python3
import sys
if "--help" in sys.argv:
    print("usage: agent [--effort VALUE]")
elif "models" in sys.argv:
    print("cursor-fast")
else:
    print("cursor 1.0")
""",
    )
    adapter = CursorAdapter()
    adapter._resolved_binary = str(binary)

    models = await adapter.list_models()

    assert models[0].supported_reasoning_efforts == []
    assert models[0].default_reasoning_effort is None
    assert models[0].reasoning_selection.value == "unsupported"


@pytest.mark.anyio
async def test_cursor_effort_flag_without_allowed_values_has_empty_reasoning_capability(
    tmp_path: Path,
) -> None:
    binary = _make_executable(
        tmp_path / "cursor_effort_flag_only.py",
        """#!/usr/bin/env python3
import sys
if "--help" in sys.argv:
    print("usage: agent --effort <EFFORT>")
elif "models" in sys.argv:
    print("cursor-fast")
    print("cursor-thinking-profile")
else:
    print("cursor 1.0")
""",
    )
    adapter = CursorAdapter()
    adapter._resolved_binary = str(binary)

    models = await adapter.list_models()

    assert [model.id for model in models] == ["cursor-fast", "cursor-thinking-profile"]
    assert all(model.supported_reasoning_efforts == [] for model in models)
    assert all(model.default_reasoning_effort is None for model in models)
    assert all(model.reasoning_selection.value == "unsupported" for model in models)


@pytest.mark.anyio
async def test_grok_cli_help_efforts_do_not_define_model_capability(tmp_path: Path) -> None:
    binary = _make_executable(
        tmp_path / "grok_misleading_help.py",
        """#!/usr/bin/env python3
import sys
if "--help" in sys.argv:
    print("usage: grok --effort {none,max}")
elif "models" in sys.argv:
    print("grok-4.6")
else:
    print("grok 1.0")
""",
    )
    adapter = GrokBuildAdapter()
    adapter._resolved_binary = str(binary)

    models = await adapter.list_models()

    assert len(models) == 1
    assert models[0].supported_reasoning_efforts == ["low", "medium", "high", "xhigh"]
    assert models[0].default_reasoning_effort == "high"
    assert models[0].source == "official_capability_table"


def test_manifest_string_model_has_no_fabricated_reasoning() -> None:
    adapter = ManifestAgentAdapter(
        {"id": "manifest-test", "binary": "missing", "models": ["custom-model"]}
    )
    model = adapter._default_models[0]
    assert model.supported_reasoning_efforts == []
    assert model.default_reasoning_effort is None


@pytest.mark.anyio
async def test_jsonrpc_version_only_is_not_ready(tmp_path: Path) -> None:
    binary = _make_executable(
        tmp_path / "version_only.py",
        '#!/usr/bin/env python3\nprint("version-only 1.0")\n',
    )
    adapter = JsonRpcStdioAdapter("jsonrpc", "JSON-RPC", [str(binary)])
    adapter._resolved_binary = str(binary)

    probe = await adapter.probe()

    assert probe.status == AgentStatus.DETECTED
    assert all(model.supported_reasoning_efforts == [] for model in probe.models)
