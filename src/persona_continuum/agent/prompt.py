"""Lossless rendering of Agent turns across heterogeneous transports."""

from __future__ import annotations

import json
from typing import Any

from persona_continuum.agent.models import AgentTurn, PromptEnvelope, PromptMode


class AgentPromptRenderer:
    """Render one canonical prompt envelope for every adapter family.

    A transport may choose native role messages, but it may not choose between
    ``full_prompt`` and ``user_message`` on its own.  ``full_prompt`` remains a
    compatibility escape hatch only when the caller explicitly selects
    ``PromptMode.FULL_PROMPT``.
    """

    SYSTEM_HEADER = "[SYSTEM INSTRUCTIONS]"
    USER_HEADER = "[USER INPUT]"
    FULL_PROMPT_HEADER = "[LEGACY FULL PROMPT]"
    MESSAGES_HEADER = "[CONVERSATION CONTEXT]"
    OUTPUT_HEADER = "[EXPECTED OUTPUT SCHEMA]"

    @classmethod
    def envelope(cls, turn: AgentTurn) -> PromptEnvelope:
        explicit_full_prompt = (
            turn.prompt_mode == PromptMode.FULL_PROMPT and turn.full_prompt is not None
        )
        return PromptEnvelope(
            system_prompt=str(turn.system_prompt or ""),
            user_message=str(turn.user_message or ""),
            full_prompt=(str(turn.full_prompt) if explicit_full_prompt else None),
            messages=[dict(message) for message in turn.messages if isinstance(message, dict)],
            expected_output=turn.expected_output,
            context_budget=dict(turn.context_budget or {}),
        )

    @classmethod
    def render_for_single_prompt(
        cls,
        turn: AgentTurn | PromptEnvelope,
        *,
        include_system: bool = True,
    ) -> str:
        """Return a single string while preserving system and user boundaries."""

        envelope = turn if isinstance(turn, PromptEnvelope) else cls.envelope(turn)
        sections: list[str] = []
        if include_system:
            sections.append(
                f"{cls.SYSTEM_HEADER}\n{envelope.system_prompt}"
                if envelope.system_prompt
                else cls.SYSTEM_HEADER
            )

        if envelope.messages:
            rendered_messages: list[str] = []
            for message in envelope.messages:
                role = str(message.get("role") or "message").upper()
                content = message.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        str(item.get("text") or item.get("content") or "")
                        if isinstance(item, dict)
                        else str(item)
                        for item in content
                    )
                rendered_messages.append(f"{role}: {content if content is not None else ''}")
            sections.append(f"{cls.MESSAGES_HEADER}\n" + "\n\n".join(rendered_messages))

        if envelope.full_prompt is not None and envelope.full_prompt != envelope.user_message:
            sections.append(f"{cls.FULL_PROMPT_HEADER}\n{envelope.full_prompt}")
        if envelope.user_message or envelope.full_prompt is None:
            sections.append(f"{cls.USER_HEADER}\n{envelope.user_message}")
        if envelope.expected_output is not None:
            sections.append(
                f"{cls.OUTPUT_HEADER}\n{cls._serialize_schema(envelope.expected_output)}"
            )
        return "\n\n".join(sections)

    @classmethod
    def render_for_native_roles(cls, turn: AgentTurn | PromptEnvelope) -> list[dict[str, Any]]:
        """Return OpenAI/Anthropic-style role messages without dropping content."""

        envelope = turn if isinstance(turn, PromptEnvelope) else cls.envelope(turn)
        messages: list[dict[str, Any]] = []
        if envelope.system_prompt:
            messages.append({"role": "system", "content": envelope.system_prompt})
        messages.extend(dict(message) for message in envelope.messages)
        if envelope.full_prompt is not None and envelope.full_prompt != envelope.user_message:
            messages.append(cls._user_content(envelope.full_prompt, turn))
        if envelope.user_message or envelope.full_prompt is None:
            messages.append(cls._user_content(envelope.user_message, turn))
        return messages

    @classmethod
    def _user_content(
        cls, text: str, turn: AgentTurn | PromptEnvelope
    ) -> dict[str, Any]:
        """Build the user message content.

        Legacy ``metadata["inline_images"]`` base64 items are still honoured
        for callers that constructed turns before canonical attachments
        existed.  Canonical ``turn.attachments`` are NOT inlined here: each
        adapter materialises its own carrier (local path / native protocol /
        inline base64 at the HTTP boundary), so the shared renderer must stay
        byte-free.
        """

        images: list[dict[str, Any]] = []
        if not isinstance(turn, PromptEnvelope):
            raw = (turn.metadata or {}).get("inline_images")
            if isinstance(raw, list):
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    media_type = str(item.get("media_type") or "").strip()
                    data = str(item.get("data") or "").strip()
                    if media_type.startswith("image/") and data:
                        images.append({"media_type": media_type, "data": data})
        if not images:
            return {"role": "user", "content": text}
        content: list[dict[str, Any]] = []
        if text:
            content.append({"type": "text", "text": text})
        for image in images:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": (
                            f"data:{image['media_type']};base64,{image['data']}"
                        )
                    },
                }
            )
        return {"role": "user", "content": content}

    @classmethod
    def render(cls, turn: AgentTurn, mode: PromptMode | str) -> str | list[dict[str, Any]]:
        selected = cls._coerce_mode(mode)
        if selected == PromptMode.NATIVE_ROLES:
            return cls.render_for_native_roles(turn)
        # FULL_PROMPT is still routed through the explicit renderer so a
        # separately supplied system prompt is never silently discarded.
        return cls.render_for_single_prompt(turn)

    @staticmethod
    def _coerce_mode(value: PromptMode | str) -> PromptMode:
        try:
            return value if isinstance(value, PromptMode) else PromptMode(str(value))
        except ValueError:
            return PromptMode.PROTOCOL_SPECIFIC

    @staticmethod
    def _serialize_schema(value: Any) -> str:
        if hasattr(value, "model_json_schema"):
            value = value.model_json_schema()
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return str(value)


__all__ = ["AgentPromptRenderer"]
