from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from persona_continuum.agent.adapters.command_code import CommandCodeAdapter
from persona_continuum.agent.models import (
    AgentEventType,
    AgentSessionConfig,
    AgentStatus,
    AgentTurn,
    SelectionStrategy,
)

SAMPLE_COMMAND_CODE_LIST_MODELS = """
Available models  ·  57 models

Open Source

deepseek/deepseek-v4-pro             hybrid-attention long-context reasoning
deepseek/deepseek-v4-flash           fast hybrid-attention reasoning (default)
moonshotai/kimi-k3                   long-horizon coding & knowledge work with 1M context
moonshotai/kimi-k2.7-code            improved long-horizon coding with vision
zai-org/glm-5.3                      frontier coding with emergent cyber capabilities
minimaxai/minimax-m3                 frontier coding, agents & native multimodality
qwen/qwen3.8-max                     autonomous long-horizon coding & professional work

Anthropic

claude-sonnet-5                      best combo of speed & intelligence (recommended)
claude-opus-5                        most intelligent Opus for agents and coding
claude-haiku-4-5                     fastest & most compact, great for quick tasks

OpenAI

gpt-5.6-sol                          frontier model for complex professional work
gpt-5.6-terra                        balances intelligence and cost
gpt-5.6-luna                         optimized for cost-sensitive workloads

Google

google/gemini-3.7-flash              higher-quality coding & agentic workflows, fewer tokens
google/gemini-3.5-flash              Pro-level coding proficiency, parallel agentic execution

xAI

xai/grok-4.6                         frontier performance on coding, knowledge work, and STEM
xai/grok-4.5                         smartest model for coding, agentic tasks, knowledge work

Pass the full id, or just the short name after the last "/":
cmd --model moonshotai/kimi-k2.5
"""


def test_command_code_adapter_init() -> None:
    adapter = CommandCodeAdapter()
    assert adapter.adapter_id == "command_code"
    assert adapter.name == "Command Code"
    assert "command-code" in adapter.binary_candidates
    assert "cmd" in adapter.binary_candidates


def test_command_code_adapter_fallback_models() -> None:
    adapter = CommandCodeAdapter()
    models = adapter._fallback_models()
    assert len(models) >= 15
    model_ids = {m.id for m in models}
    assert "deepseek/deepseek-v4-flash" in model_ids
    assert "google/gemini-3.7-flash" in model_ids
    assert "gpt-5.6-sol" in model_ids
    assert "claude-sonnet-5" in model_ids


def test_command_code_adapter_parse_models() -> None:
    adapter = CommandCodeAdapter()
    models = adapter._parse_models(SAMPLE_COMMAND_CODE_LIST_MODELS)
    assert len(models) == 17
    by_id = {m.id: m for m in models}

    # Verify deepseek model
    ds_model = by_id["deepseek/deepseek-v4-pro"]
    assert ds_model.provider == "deepseek"
    assert "none" in ds_model.supported_reasoning_efforts
    assert ds_model.reasoning_selection == SelectionStrategy.STARTUP

    # Verify anthropic model
    claude_model = by_id["claude-sonnet-5"]
    assert claude_model.provider == "anthropic"

    # Verify openai model
    sol_model = by_id["gpt-5.6-sol"]
    assert sol_model.provider == "openai"
    assert sol_model.default_reasoning_effort == "medium"

    # Verify google model
    gemini_model = by_id["google/gemini-3.7-flash"]
    assert gemini_model.provider == "google"

    # Verify xai model
    grok_model = by_id["xai/grok-4.6"]
    assert grok_model.provider == "xai"


def test_command_code_adapter_build_exec_argv() -> None:
    adapter = CommandCodeAdapter()
    config = AgentSessionConfig(
        room_id="room_1",
        participant_id="p_1",
        persona_id="persona_1",
        model_id="deepseek/deepseek-v4-flash",
        reasoning_effort="high",
    )
    argv = adapter.build_exec_argv("command-code", config, "Hello world")
    assert argv == [
        "command-code",
        "-p",
        "Hello world",
        "--yolo",
        "--output-format",
        "text",
        "-m",
        "deepseek/deepseek-v4-flash",
        "--effort",
        "high",
    ]


@pytest.mark.anyio
async def test_command_code_adapter_probe_not_found() -> None:
    adapter = CommandCodeAdapter()
    with patch.object(adapter, "_find_binary", return_value=None):
        probe = await adapter.probe()
        assert probe.status == AgentStatus.DISABLED
        assert probe.binary_path is None
        # A not-installed runtime must not advertise any model list.
        assert probe.models == []


@pytest.mark.anyio
async def test_command_code_adapter_probe_success() -> None:
    adapter = CommandCodeAdapter()
    with (
        patch.object(adapter, "_find_binary", return_value="/usr/local/bin/command-code"),
        patch(
            "persona_continuum.agent.adapters.command_code.safe_exec_cmd",
            side_effect=[
                (0, "command-code 1.31.0\n", ""),  # --version
                (0, "✔ Authentication verified\n✔ Authenticated as testuser\n", ""),  # status
                (0, SAMPLE_COMMAND_CODE_LIST_MODELS, ""),  # --list-models
            ],
        ),
    ):
        probe = await adapter.probe()
        assert probe.status == AgentStatus.READY
        assert probe.auth_status == "configured"
        assert probe.version == "command-code 1.31.0"
        assert len(probe.models) == 17


@pytest.mark.anyio
async def test_command_code_adapter_probe_unauth() -> None:
    adapter = CommandCodeAdapter()
    with (
        patch.object(adapter, "_find_binary", return_value="/usr/local/bin/command-code"),
        patch(
            "persona_continuum.agent.adapters.command_code.safe_exec_cmd",
            side_effect=[
                (0, "command-code 1.31.0\n", ""),
                (1, "Not logged in. Please run command-code login.\n", ""),
                (0, SAMPLE_COMMAND_CODE_LIST_MODELS, ""),
            ],
        ),
    ):
        probe = await adapter.probe()
        assert probe.status == AgentStatus.AUTH_REQUIRED
        assert probe.auth_status == "auth_required"


@pytest.mark.anyio
async def test_command_code_adapter_send_buffered_final() -> None:
    adapter = CommandCodeAdapter()
    config = AgentSessionConfig(
        room_id="room_1",
        participant_id="p_1",
        persona_id="persona_1",
    )
    with patch.object(adapter, "_find_binary", return_value="/usr/local/bin/command-code"):
        session = await adapter.create_session(config)
        assert session.is_active

        # Mock subprocess
        fake_proc = AsyncMock()
        fake_proc.returncode = 0
        fake_proc.stdin = None
        fake_proc.stdout = AsyncMock()
        fake_proc.stdout.read = AsyncMock(side_effect=[b"Hello ", b"from Command Code", b""])
        fake_proc.stderr = None
        fake_proc.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            turn = AgentTurn(user_message="Say hello")
            events = []
            async for ev in adapter.send(session, turn):
                events.append(ev)

            chunk_events = [e for e in events if e.type == AgentEventType.CHUNK]
            done_events = [e for e in events if e.type == AgentEventType.DONE]
            assert len(chunk_events) == 1
            assert len(done_events) == 1
            assert done_events[0].content == "Hello from Command Code"

        await adapter.close(session)
        assert not session.is_active
