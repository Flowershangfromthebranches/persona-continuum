from __future__ import annotations

import re

from pydantic import BaseModel

from persona_continuum.room.models import ParticipantSlot


class DiscussionDecision(BaseModel):
    next_speaker: str
    reason: str
    priority: float


class DiscussionDirector:
    """Selects a relevant responder from topic, relationships, and conversation state."""

    def select_next_speaker(
        self,
        *,
        conversation: list[dict[str, object]],
        participants: list[ParticipantSlot],
        topic: str,
        relationships: dict[str, dict[str, float]] | None = None,
    ) -> DiscussionDecision:
        if not participants:
            raise ValueError("No participants available in autonomous discussion")
        last = conversation[-1] if conversation else {}
        last_speaker = str(last.get("participant_id") or "")
        content = f"{topic} {last.get('content', '')}".lower()
        tokens = set(re.findall(r"[\w\u4e00-\u9fff]+", content))
        relationship_map = relationships or {}

        scored: list[tuple[float, str, ParticipantSlot, list[str]]] = []
        for participant in participants:
            if not participant.allow_director_auto_select:
                continue
            reasons: list[str] = []
            score = participant.speaking_weight
            name = (participant.display_name or participant.persona_id).lower()
            if name and name in content:
                score += 4.0
                reasons.append("directly_referenced")
            expertise_hits = sum(
                1
                for term in participant.expertise
                if term.lower() in content or term.lower() in tokens
            )
            if expertise_hits:
                score += 2.5 * expertise_hits
                reasons.append("domain_expertise")
            relation = relationship_map.get(last_speaker, {}).get(participant.participant_id)
            if relation is None:
                relation = participant.relationships.get(last_speaker)
            if relation is not None:
                score += abs(float(relation)) * 2.0
                reasons.append(
                    "competitive_relationship" if relation < 0 else "collaborative_relationship"
                )
            if participant.participant_id == last_speaker:
                score -= 10.0
                reasons.append("recent_speaker_penalty")
            turns_since = next(
                (
                    index
                    for index, entry in enumerate(reversed(conversation))
                    if entry.get("participant_id") == participant.participant_id
                ),
                len(conversation) + 2,
            )
            score += min(float(turns_since), 5.0) * 0.3
            scored.append((score, participant.participant_id, participant, reasons))

        if not scored:
            raise ValueError("No participant allows autonomous selection")
        scored.sort(key=lambda item: (-item[0], item[1]))
        score, _, participant, reasons = scored[0]
        return DiscussionDecision(
            next_speaker=participant.participant_id,
            reason=" + ".join(reasons or ["topic_relevance"]),
            priority=round(score, 3),
        )
