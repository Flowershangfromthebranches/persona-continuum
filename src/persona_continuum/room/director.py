from __future__ import annotations

import random
from typing import Any

from persona_continuum.room.models import DirectorConfig, DirectorMode, ParticipantSlot


class SpeakerDirector:
    def __init__(self, config: DirectorConfig | None = None, seed: int | None = None) -> None:
        self.config = config or DirectorConfig()
        self._rng = random.Random(seed) if seed is not None else random.Random()

    def select_next_speaker(
        self,
        participants: list[ParticipantSlot],
        transcript: list[dict[str, Any]] | None = None,
        turn_index: int | None = None,
        topic: str | None = None,
        manual_speaker_id: str | None = None,
        manual_override_id: str | None = None,
        recent_transcript: list[dict[str, Any]] | None = None,
        participant_states: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[str, str]:
        """Select next speaker and return (participant_id, selection_reason)."""
        if not participants:
            raise ValueError("No participants available in room")

        manual_id = manual_speaker_id or manual_override_id
        # 1. Manual override
        if manual_id:
            for p in participants:
                if p.participant_id == manual_id:
                    return p.participant_id, "manual_override"

        # If only one participant
        if len(participants) == 1:
            return participants[0].participant_id, "sole_participant"

        history = transcript if transcript is not None else (recent_transcript or [])
        turn_idx = turn_index if turn_index is not None else len(history)

        # 2. Round Robin mode
        if self.config.mode == DirectorMode.ROUND_ROBIN:
            p = participants[turn_idx % len(participants)]
            return p.participant_id, f"round_robin_turn_{turn_idx}"

        # 3. Natural / Director Mode Scoring
        last_turn = history[-1] if history else None
        last_speaker_id = last_turn.get("participant_id") if last_turn else None
        last_content = str(last_turn.get("content", "")) if last_turn else ""

        # Compute consecutive turns for last speaker
        consecutive_count = 0
        for entry in reversed(history):
            if entry.get("participant_id") == last_speaker_id:
                consecutive_count += 1
            else:
                break

        # Calculate turns since last spoke for each participant
        turns_since_spoke: dict[str, int] = {}
        for p in participants:
            turns_ago = 999
            for idx, entry in enumerate(reversed(history)):
                if entry.get("participant_id") == p.participant_id:
                    turns_ago = idx
                    break
            turns_since_spoke[p.participant_id] = turns_ago

        scores: list[tuple[float, ParticipantSlot, str]] = []
        for p in participants:
            if not p.allow_director_auto_select:
                continue

            reason_parts: list[str] = []
            score = p.speaking_weight * 1.0

            # Direct mention in previous utterance
            p_name = (p.display_name or p.persona_id).lower()
            if p_name and p_name in last_content.lower():
                score += self.config.mention_weight
                reason_parts.append(f"mentioned({p_name})")

            # Question target bonus
            if ("?" in last_content or "？" in last_content) and (
                p_name and p_name in last_content.lower()
            ):
                score += self.config.question_target_weight
                reason_parts.append("question_target")

            # Silence bonus (haven't spoken in a while)
            silence_turns = turns_since_spoke.get(p.participant_id, 999)
            if silence_turns > 2:
                bonus = min(silence_turns * self.config.silence_bonus_weight, 3.0)
                score += bonus
                reason_parts.append(f"silence_bonus(+{bonus:.1f})")

            # Internal state bonuses (needs / affect)
            if participant_states and p.persona_id in participant_states:
                state = participant_states[p.persona_id]
                # High emotion intensity increases desire to speak
                emotions = state.get("emotions", [])
                max_intensity = max((float(e.get("intensity", 0)) for e in emotions), default=0.0)
                if max_intensity > 0.5:
                    score += max_intensity * self.config.affect_weight
                    reason_parts.append(f"high_affect(+{max_intensity:.2f})")

            # Topic relevance
            if topic and p_name in topic.lower():
                score += self.config.topic_relevance_weight
                reason_parts.append("topic_match")

            # Penalties: consecutive turns
            if p.participant_id == last_speaker_id:
                if consecutive_count >= p.max_consecutive_turns:
                    score -= 999.0  # Disallow exceeding max consecutive turns
                    reason_parts.append("max_consecutive_exceeded")
                else:
                    score -= self.config.recent_speaking_penalty * consecutive_count
                    reason_parts.append(f"consecutive_penalty(-{consecutive_count})")

            # Random jitter to avoid deterministic starvation
            jitter = self._rng.uniform(0.0, self.config.random_jitter)
            score += jitter

            reason_str = ", ".join(reason_parts) if reason_parts else "natural_flow"
            scores.append((score, p, reason_str))

        if not scores:
            p = self._rng.choice(participants)
            return p.participant_id, "fallback_choice"

        scores.sort(key=lambda item: item[0], reverse=True)
        winner = scores[0][1]
        winner_reason = f"{scores[0][2]} (score: {scores[0][0]:.2f})"
        return winner.participant_id, winner_reason
