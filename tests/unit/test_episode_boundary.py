"""Episode boundary rules and summary schema (Phase 3, pure logic)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from persona_continuum.application.episode_service import (
    EPISODE_SUMMARY_SCHEMA,
    EpisodeService,
    estimate_text_tokens,
)
from persona_continuum.config import Config
from persona_continuum.domain.episode import (
    BoundaryReason,
    EpisodeStatus,
    EpisodeSummary,
    MemoryEpisode,
)


class _Database:
    """Boundary decisions never touch the database, so a stub is enough."""

    class _Conn:
        def execute(self, *args, **kwargs):  # pragma: no cover - unused
            raise AssertionError("decide_boundary must not touch the database")

        def commit(self) -> None:  # pragma: no cover - unused
            raise AssertionError("decide_boundary must not commit")

    conn = _Conn()


def _service(**overrides) -> EpisodeService:
    config = Config(memory_episode_max_turns=4, memory_episode_max_tokens=200, **overrides)
    return EpisodeService(_Database(), config=config)  # type: ignore[arg-type]


def _episode(**overrides) -> MemoryEpisode:
    now = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    payload = {
        "id": "episode_x",
        "persona_id": "su_he",
        "counterpart_id": "user",
        "session_id": "sess_x",
        "status": EpisodeStatus.OPEN,
        "started_at": now,
        "ended_at": now,
        "turn_count": 1,
        "source_token_estimate": 10,
    }
    payload.update(overrides)
    return MemoryEpisode(**payload)  # type: ignore[arg-type]


def test_first_turn_creates_the_first_episode() -> None:
    decision = _service().decide_boundary(None, occurred_at=datetime.now(UTC), turn_tokens=10)
    assert decision == {"action": "create", "reason": BoundaryReason.FIRST_TURN.value}


def test_continuing_conversation_appends() -> None:
    episode = _episode()
    decision = _service().decide_boundary(
        episode, occurred_at=episode.ended_at + timedelta(minutes=5), turn_tokens=10
    )
    assert decision["action"] == "append"
    assert decision["reason"] == BoundaryReason.APPEND.value


def test_idle_gap_closes_and_starts_a_new_episode() -> None:
    episode = _episode()
    decision = _service().decide_boundary(
        episode, occurred_at=episode.ended_at + timedelta(minutes=181), turn_tokens=10
    )
    assert decision["action"] == "close_then_create"
    assert decision["reason"] == BoundaryReason.IDLE_GAP.value


def test_max_turns_closes_the_episode() -> None:
    episode = _episode(turn_count=4)
    decision = _service().decide_boundary(
        episode, occurred_at=episode.ended_at + timedelta(minutes=1), turn_tokens=10
    )
    assert decision["reason"] == BoundaryReason.MAX_TURNS.value


def test_max_tokens_closes_the_episode() -> None:
    episode = _episode(source_token_estimate=195)
    decision = _service().decide_boundary(
        episode, occurred_at=episode.ended_at + timedelta(minutes=1), turn_tokens=10
    )
    assert decision["reason"] == BoundaryReason.MAX_TOKENS.value


def test_a_non_open_episode_never_receives_appends() -> None:
    for status in (
        EpisodeStatus.CLOSED,
        EpisodeStatus.PENDING_CONSOLIDATION,
        EpisodeStatus.FAILED,
    ):
        decision = _service().decide_boundary(
            _episode(status=status),
            occurred_at=datetime.now(UTC),
            turn_tokens=1,
        )
        assert decision["action"] == "create"


def test_episode_thresholds_are_not_the_working_window_numbers() -> None:
    """An Episode is a unit of memory, not a unit of prompt."""

    service = _service()
    assert service.max_turns == 4
    assert service.idle_gap == timedelta(minutes=180)
    default_service = EpisodeService(_Database(), config=Config())  # type: ignore[arg-type]
    assert default_service.max_turns == 24
    assert default_service.idle_gap == timedelta(minutes=180)
    # Nothing in the Episode layer reads the room prompt budget.
    assert not hasattr(default_service, "prompt_target_tokens")


def test_summary_schema_covers_the_required_fields() -> None:
    required = {
        "title",
        "summary",
        "important_events",
        "commitments",
        "unresolved",
        "emotional_arc",
        "topics",
        "entities",
        "importance",
        "user_stated",
        "persona_stated",
        "inferred_context",
    }
    assert required <= set(EPISODE_SUMMARY_SCHEMA["properties"])
    assert set(EPISODE_SUMMARY_SCHEMA["required"]) == {"title", "summary"}


def test_summary_validation_cleans_and_bounds_a_model_payload() -> None:
    summary = EpisodeSummary.model_validate(
        {
            "title": "  讨论考研与重庆理工  ",
            "summary": "用户考虑重庆理工。",
            "important_events": ["用户表达数学焦虑", "", None, "苏禾进行了安慰"],
            "commitments": "用户计划第二天开始复习",
            "unresolved": ["尚未确定完整复习安排"],
            "emotional_arc": ["焦虑", "被理解", "形成计划"],
            "importance": 1.7,
            "confidence": "0.4",
            "unknown_key": "dropped",
        }
    )
    assert summary.title == "讨论考研与重庆理工"
    assert summary.important_events == ["用户表达数学焦虑", "苏禾进行了安慰"]
    assert summary.commitments == ["用户计划第二天开始复习"]
    assert summary.importance == 0.5  # out of range -> default, never 1.7
    assert summary.confidence == pytest.approx(0.4)
    assert "unknown_key" not in summary.model_dump()


def test_summary_caps_generated_lists() -> None:
    summary = EpisodeSummary.model_validate(
        {"title": "t", "summary": "s", "important_events": [f"事件{i}" for i in range(200)]}
    )
    assert len(summary.important_events) <= 24


def test_persona_guess_is_separable_from_user_fact() -> None:
    """The persona's interpretation must never be stored as a user fact."""

    summary = EpisodeSummary.model_validate(
        {
            "title": "吵架",
            "summary": "用户与小陈争吵。",
            "user_stated": ["我不想再联系她了"],
            "persona_stated": ["你肯定就是舍不得她"],
            "inferred_context": ["用户可能仍然在意这段关系"],
        }
    )
    assert "你肯定就是舍不得她" in summary.persona_stated
    assert "你肯定就是舍不得她" not in summary.user_stated
    assert summary.inferred_context == ["用户可能仍然在意这段关系"]


def test_token_estimate_is_cjk_aware() -> None:
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("嗯") == 1
    # CJK is counted per character, not at a chars/4 ratio.
    assert estimate_text_tokens("重庆理工") == 4
    assert estimate_text_tokens("aaaaaaaa") == 2
