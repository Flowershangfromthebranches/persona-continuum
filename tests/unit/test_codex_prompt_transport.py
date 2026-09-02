from persona_continuum.agent.adapters.codex import CodexAdapter
from persona_continuum.agent.prompt_transport import resolve_prompt_transport_capability


def test_codex_declares_large_prompt_safe_transport() -> None:
    capability = resolve_prompt_transport_capability(CodexAdapter())

    assert capability.transport_mode == "stdin"
    assert capability.supports_large_prompt is True
    assert capability.safe_prompt_bytes > 64 * 1024
