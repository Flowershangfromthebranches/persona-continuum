from __future__ import annotations

import json
from typing import Any

from persona_continuum.domain.session import PreparedTurn
from persona_continuum.room.attachments import describe_attachment_for_prompt
from persona_continuum.room.models import ParticipantSlot, ResolvedBindingSnapshot


class PromptComposer:
    def __init__(
        self,
        identity_budget_chars: int = 2500,
        recall_budget_chars: int = 2500,
        transcript_budget_chars: int = 3000,
        total_budget_chars: int = 10000,
    ) -> None:
        # These values remain accepted for API compatibility with the first
        # Room prompt composer. Context sizing now belongs to the shared Agent
        # Context Budget Manager; this composer must not silently lose evidence.
        self.identity_budget_chars = identity_budget_chars
        self.recall_budget_chars = recall_budget_chars
        self.transcript_budget_chars = transcript_budget_chars
        self.total_budget_chars = total_budget_chars

    def compose_turn_prompt(
        self,
        slot: ParticipantSlot,
        binding: ResolvedBindingSnapshot | None = None,
        prepared: PreparedTurn | None = None,
        dynamic_recall_memories: list[Any] | None = None,
        recent_transcript: list[dict[str, Any]] | None = None,
        room_topic: str | None = None,
        snapshot: ResolvedBindingSnapshot | None = None,
        user_message: str | None = None,
        kernel: Any | None = None,
        context_mode: str = "full",
        summary_block: str | None = None,
        cursor_span: tuple[int, int] | None = None,
    ) -> tuple[str, str, str]:
        """Compose (system_prompt, user_prompt, full_prompt) for the agent turn.

        ``context_mode`` records how much room history is in this prompt:
        ``delta`` (persistent thread already owns older turns), ``rehydrated``
        (thread lost; kernel + summary + recent window), ``windowed``
        (stateless: bounded raw window + long-term summary), or ``full``.
        """
        resolved_binding = binding or snapshot
        dynamic_recall_memories = dynamic_recall_memories or []
        recent_transcript = recent_transcript or []
        if prepared is None:
            raise ValueError("prepared turn context is required")
        manifest = prepared.identity_anchor
        name = slot.display_name or manifest.display_name

        if kernel is not None:
            identity_kernel_text = kernel.identity_block
            constraints_text = kernel.constraints_block
        else:
            # --- Layer 1: Persona Identity Kernel (uncached fallback) ---
            kernel_lines = [
                f"# Digital Persona: {name}",
                f"- Persona ID: {slot.persona_id}",
                f"- Type: {manifest.persona_type.value}",
                f"- Run Mode: {manifest.run_mode.value}",
            ]
            if manifest.birth_date or manifest.death_date:
                b_date = manifest.birth_date or "unknown"
                d_date = manifest.death_date or "present"
                kernel_lines.append(f"- Lifespan: {b_date} to {d_date}")
            if manifest.aliases:
                kernel_lines.append(f"- Aliases: {', '.join(manifest.aliases)}")
            if resolved_binding and resolved_binding.agent_runtime_name:
                kernel_lines.append(f"- Agent Runtime: {resolved_binding.agent_runtime_name}")

            compiled = prepared.compiled_persona_context.get("by_key", {})
            if "values" in compiled:
                val_str = json.dumps(compiled["values"], ensure_ascii=False)
                kernel_lines.append(f"- Core Values: {val_str}")
            if "expression_style" in compiled:
                exp_str = json.dumps(compiled["expression_style"], ensure_ascii=False)
                kernel_lines.append(f"- Expression Tendencies: {exp_str}")
            if "decision_heuristics" in compiled:
                dec_str = json.dumps(compiled["decision_heuristics"], ensure_ascii=False)
                kernel_lines.append(f"- Decision Patterns: {dec_str}")

            identity_kernel_text = "\n".join(kernel_lines)

            # --- Behavioral & Fact Boundary Constraints ---
            constraints_text = (
                "## Persona Execution Constraints\n"
                f"1. You are embodying {name} in an interactive multi-agent room.\n"
                "2. Express thoughts in first-person voice consistent with your personality.\n"
                "3. Do not break character, mention prompt structure, "
                "or output raw internal states.\n"
                "4. Respect fact boundaries: do not assert certainty for unverified facts.\n"
                "5. If referencing history, rely strictly on your memories and retrieved context."
            )

        # --- Current Internal State ---
        emotions_str = (
            ", ".join(
                f"{e.name}: {e.intensity:.2f}"
                for e in prepared.current_emotions
                if e.intensity > 0.1
            )
            or "balanced"
        )
        needs_str = (
            ", ".join(f"{n.name}: {n.level:.2f}" for n in prepared.current_needs if n.level > 0.2)
            or "stable"
        )
        goals_str = ", ".join(str(goal) for goal in prepared.active_goals) or (
            "engage authentically in current discussion"
        )

        internal_state_text = (
            f"## Current Internal State\n"
            f"- Affect/Emotions: {emotions_str}\n"
            f"- Motivations/Needs: {needs_str}\n"
            f"- Active Goals: {goals_str}\n"
        )

        # --- Layer 2: Dynamic Recall Evidence ---
        recall_lines = []
        all_memories = list(dynamic_recall_memories) + list(prepared.relevant_memories)
        seen_mem_ids: set[str] = set()
        for m in all_memories:
            m_id = getattr(m, "id", None) or (m.get("id") if isinstance(m, dict) else None)
            m_content = getattr(m, "content", None) or (
                m.get("content") if isinstance(m, dict) else str(m)
            )
            if m_id and m_id in seen_mem_ids:
                continue
            if m_id:
                seen_mem_ids.add(m_id)
            recall_lines.append(f"- [Memory]: {m_content}")

        recall_text = ""
        if recall_lines:
            recall_text = "## Dynamic Recall & Retrieved Memories\n" + "\n".join(recall_lines)

        # --- Room Context & Recent Transcript ---
        transcript_lines = []
        for t in recent_transcript:
            spk = t.get("speaker_name") or t.get("persona_id") or "Participant"
            cnt = str(t.get("content", ""))
            attachments = t.get("attachments") or (t.get("metadata") or {}).get(
                "attachments"
            )
            if isinstance(attachments, list) and attachments:
                notes = " ".join(
                    describe_attachment_for_prompt(a)
                    for a in attachments
                    if isinstance(a, dict)
                )
                if notes:
                    cnt = f"{cnt}\n{notes}" if cnt else notes
            transcript_lines.append(f"{spk}: {cnt}")

        transcript_text = ""
        if transcript_lines:
            if context_mode == "delta" and cursor_span is not None:
                header = (
                    "## Room Dialogue Since Your Last Turn "
                    f"(turns {cursor_span[0]}-{cursor_span[1]})"
                )
            elif context_mode == "rehydrated":
                header = (
                    "## Recent Room Dialogue (context rehydrated after runtime restart)"
                )
            elif context_mode == "windowed":
                header = "## Recent Raw Turns"
            else:
                header = "## Recent Room Dialogue"
            transcript_text = header + "\n" + "\n".join(transcript_lines)

        summary_text = summary_block or ""

        # System Prompt
        system_sections = [
            f"You are {name}, a persistent digital persona in Persona Continuum.",
            identity_kernel_text,
            internal_state_text,
            constraints_text,
        ]
        system_prompt = "\n\n".join(s for s in system_sections if s)

        # User / Turn Prompt
        topic_header = f"Discussion Topic: {room_topic}\n" if room_topic else ""
        user_msg_section = f"Audience / User Message: {user_message}\n" if user_message else ""
        user_sections = [
            topic_header,
            user_msg_section,
            summary_text,
            recall_text,
            transcript_text,
            f"Please respond as {name} to continue the conversation:",
        ]
        if context_mode == "delta" and not transcript_lines:
            user_sections.insert(
                -1,
                "（自你上次发言以来房间没有新的发言；请基于当前讨论自然接续。）",
            )
        user_prompt = "\n\n".join(s for s in user_sections if s)

        full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"
        return system_prompt, user_prompt, full_prompt
