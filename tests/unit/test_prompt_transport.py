from types import SimpleNamespace

from persona_continuum.agent.prompt_transport import (
    capability_for_mode,
    resolve_prompt_transport_capability,
)
from persona_continuum.agent.protocols.acp import ACPAdapter
from persona_continuum.agent.protocols.openai_compatible import OpenAICompatibleAPIAdapter
from persona_continuum.application.job_progress import (
    PersonaFailureCode,
    classify_persona_failure,
    is_retryable_failure,
)


def test_openai_compatible_http_declares_rpc_transport() -> None:
    capability = resolve_prompt_transport_capability(OpenAICompatibleAPIAdapter())

    assert capability.transport_mode == "rpc"
    assert capability.supports_large_prompt is True
    assert capability.safe_prompt_bytes > 1024 * 1024
    # The failed large-persona job packed ~360KB; HTTP must accept that payload.
    assert capability.allows_bytes(362_606)
    assert not capability_for_mode("unknown").allows_bytes(362_606)


def test_protocols_list_http_infers_rpc_without_declared_mode() -> None:
    adapter = SimpleNamespace(protocols=["openai_compatible_http"])

    capability = resolve_prompt_transport_capability(adapter)

    assert capability.transport_mode == "rpc"
    assert capability.source == "adapter_shape_protocol"
    assert capability.allows_bytes(362_606)


def test_protocols_list_acp_infers_stream() -> None:
    adapter = SimpleNamespace(protocols=["acp"])

    capability = resolve_prompt_transport_capability(adapter)

    assert capability.transport_mode == "stream"
    assert capability.supports_large_prompt is True


def test_acp_adapter_declares_stream_transport() -> None:
    adapter = ACPAdapter(
        adapter_id="acp_test",
        name="ACP Test",
        binary_candidates=["/nonexistent/acp"],
    )

    capability = resolve_prompt_transport_capability(adapter)

    assert capability.transport_mode == "stream"
    assert capability.safe_prompt_bytes > 1024 * 1024


def test_http_base_url_infers_rpc() -> None:
    adapter = SimpleNamespace(base_url="https://api.example.com/v1")

    capability = resolve_prompt_transport_capability(adapter)

    assert capability.transport_mode == "rpc"
    assert capability.source == "adapter_shape_http_base_url"


def test_print_flag_still_infers_argv() -> None:
    adapter = SimpleNamespace(exec_args=["--print", "run"])

    capability = resolve_prompt_transport_capability(adapter)

    assert capability.transport_mode == "argv"
    assert capability.allows_bytes(362_606) is False


def test_wrapped_transport_limit_failure_is_retryable() -> None:
    # Rows written before AgentRuntimeError was preserved on the job.
    legacy = {
        "code": "PERSONA_CREATION_ERROR",
        "message": "PROMPT_TRANSPORT_LIMIT_EXCEEDED",
        "phase": "dimension_extraction",
        "retriable": False,
    }
    assert is_retryable_failure(legacy) is True
    assert (
        classify_persona_failure(Exception("PROMPT_TRANSPORT_LIMIT_EXCEEDED"))
        == PersonaFailureCode.TRANSPORT_FAILED.value
    )
