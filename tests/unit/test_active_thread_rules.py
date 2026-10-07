"""Active Thread rules: identity, validation, bounds, lexical continuation.

Pure logic only -- the store-level acceptance (create/update/resolve/reopen,
idempotency, restart, provenance) lives in
``tests/integration/test_active_threads.py``.
"""

from __future__ import annotations

import pytest

from persona_continuum.application.thread_service import (
    ThreadService,
    _lexical_tokens,
    _overlap_ratio,
)
from persona_continuum.domain.thread import (
    LIVE_STATUSES,
    THREAD_RESOLUTION_VERSION,
    MemoryActiveThread,
    ThreadAction,
    ThreadOperation,
    ThreadResolutionPayload,
    ThreadStatus,
    ThreadType,
    canonical_thread_key,
    normalize_thread_text,
)


class _Database:
    """Lifecycle mapping never touches the database, so a stub is enough."""

    class _Conn:
        def execute(self, *args, **kwargs):  # pragma: no cover - unused
            raise AssertionError("status mapping must not touch the database")

        def commit(self) -> None:  # pragma: no cover - unused
            raise AssertionError("status mapping must not commit")

    conn = _Conn()


class _Episodes:
    def __getattr__(self, name: str):  # pragma: no cover - unused
        raise AssertionError(f"status mapping must not touch Episodes ({name})")


def _service() -> ThreadService:
    return ThreadService(_Database(), _Episodes(), None)


def _thread(status: ThreadStatus) -> MemoryActiveThread:
    return MemoryActiveThread(
        id="thread_1",
        persona_id="su_he",
        counterpart_id="user",
        thread_key="plan|重庆旅行",
        thread_type=ThreadType.PLAN,
        title="重庆旅行",
        status=status,
    )


# --- identity ---------------------------------------------------------------


def test_thread_key_converges_on_one_subject() -> None:
    keys = {
        canonical_thread_key(ThreadType.PLAN, "重庆旅行"),
        canonical_thread_key("plan", " 重庆 旅行 "),
        canonical_thread_key(ThreadType.PLAN, "重庆旅行计划"),
    }
    assert keys == {"plan|重庆旅行"}


def test_thread_key_is_scoped_by_type_not_by_wording_noise() -> None:
    plan = canonical_thread_key(ThreadType.PLAN, "考研复习")
    project = canonical_thread_key(ThreadType.PROJECT, "考研复习")
    assert plan != project  # type participates in identity
    assert plan == canonical_thread_key(ThreadType.PLAN, "考研复习的事情")


def test_normalize_is_case_and_space_insensitive() -> None:
    assert normalize_thread_text("  A  B ") == "a b"


def test_live_statuses_are_the_in_flight_ones() -> None:
    assert {status.value for status in LIVE_STATUSES} == {
        "open",
        "active",
        "waiting",
        "stale",
    }
    assert ThreadStatus.RESOLVED not in LIVE_STATUSES
    assert not _thread(ThreadStatus.RESOLVED).is_live
    assert _thread(ThreadStatus.STALE).is_live


# --- weak keyword continuation (why the shortlist exists) --------------------


def test_weak_continuation_has_no_keyword_overlap() -> None:
    """The case the whole layer exists for: "票我买好了。" vs "重庆旅行".

    A keyword/BM25 gate cannot connect these, which is exactly why candidate
    selection always keeps recent live threads in front of the resolver instead
    of relying on lexical match.
    """

    assert _overlap_ratio(_lexical_tokens("票我买好了。"), _lexical_tokens("重庆旅行")) == 0.0
    # A topic match does show up when one exists.
    assert _overlap_ratio(_lexical_tokens("去重庆"), _lexical_tokens("重庆旅行")) > 0.0


def test_lexical_tokens_use_cjk_bigrams_and_latin_words() -> None:
    tokens = _lexical_tokens("Persona Continuum 记忆 bug")
    assert "persona" in tokens
    assert "continuum" in tokens
    assert "记忆" in tokens
    assert "重庆" not in tokens


# --- action validation ------------------------------------------------------


def _action(**overrides) -> ThreadAction:
    payload = {
        "operation": "create",
        "thread_type": "plan",
        "title": "重庆旅行",
        "summary": "用户准备下个月去重庆。",
        "confidence": 0.8,
        "reason": "明确的下个月出行计划",
    }
    payload.update(overrides)
    return ThreadAction.model_validate(payload)


def test_action_degrades_unknown_enums_instead_of_failing() -> None:
    action = _action(operation="conquer", thread_type="intergalactic")
    assert action.operation is ThreadOperation.NOOP
    assert action.thread_type is ThreadType.GENERAL


def test_create_requires_a_subject_but_references_require_an_id() -> None:
    assert _action().is_usable
    assert not _action(title="", thread_key_hint=None).is_usable
    assert not _action(operation="update", thread_id=None).is_usable
    assert _action(operation="update", thread_id="thread_1").is_usable
    assert _action(operation="noop", thread_id=None).is_usable


def test_action_bounds_text_and_references() -> None:
    action = _action(
        title="标" * 500,
        reason="理" * 900,
        source_turn_ids=[f"turn_{index}" for index in range(50)],
        related_fact_ids=["fact_1", "fact_1", "", None],
    )
    assert len(action.title) <= 120
    assert len(action.reason) <= 400
    assert len(action.source_turn_ids) <= 16
    assert action.related_fact_ids == ["fact_1"]


def test_action_confidence_out_of_range_falls_back() -> None:
    assert _action(confidence=4.2).confidence == 0.5
    assert _action(confidence="0.7").confidence == 0.7


def test_action_key_and_display_title() -> None:
    action = _action(title="重庆旅行", thread_type="plan")
    assert action.key == "plan|重庆旅行"
    assert action.display_title() == "重庆旅行"
    assert _action(title="", thread_key_hint="去重庆").display_title() == "去重庆"


# --- payload validation -----------------------------------------------------


def test_payload_drops_junk_but_keeps_valid_actions() -> None:
    payload = ThreadResolutionPayload.model_validate(
        {
            "threads": [
                {"operation": "noop"},
                "junk",
                None,
                {"operation": "resolve"},  # unusable: nothing to resolve
                {"operation": "resolve", "thread_id": "thread_1"},
            ]
        }
    )
    assert len(payload.threads) == 3
    assert len(payload.usable()) == 2


def test_payload_is_bounded() -> None:
    payload = ThreadResolutionPayload.model_validate(
        {"threads": [{"operation": "noop"} for _ in range(50)]}
    )
    assert len(payload.threads) == 8


def test_parse_resolution_rejects_a_lying_payload() -> None:
    assert ThreadService.parse_resolution({"threads": []}) is not None
    assert ThreadService.parse_resolution({"threads": ["junk"]}) is None
    assert ThreadService.parse_resolution({"facts": []}) is None
    assert ThreadService.parse_resolution("nope") is None


# --- lifecycle mapping ------------------------------------------------------


def test_status_transitions_are_explicit() -> None:
    service = _service()
    active = _thread(ThreadStatus.ACTIVE)
    assert service._status_after(active, ThreadOperation.RESOLVE) is ThreadStatus.RESOLVED
    assert service._status_after(active, ThreadOperation.CANCEL) is ThreadStatus.CANCELLED
    assert service._status_after(active, ThreadOperation.WAITING) is ThreadStatus.WAITING
    assert service._status_after(active, ThreadOperation.UPDATE) is ThreadStatus.ACTIVE
    # A RESOLVED thread returning to the conversation becomes ACTIVE again.
    resolved = _thread(ThreadStatus.RESOLVED)
    assert service._status_after(resolved, ThreadOperation.UPDATE) is ThreadStatus.ACTIVE
    assert service._status_after(resolved, ThreadOperation.REOPEN) is ThreadStatus.ACTIVE
    # Silence downgrades, it never resolves.
    stale = _thread(ThreadStatus.STALE)
    assert service._status_after(stale, ThreadOperation.UPDATE) is ThreadStatus.ACTIVE


def test_thread_version_is_stamped() -> None:
    assert THREAD_RESOLUTION_VERSION >= 1
    assert _thread(ThreadStatus.ACTIVE).consolidation_version == THREAD_RESOLUTION_VERSION


@pytest.mark.parametrize("status", list(ThreadStatus))
def test_thread_status_round_trips(status: ThreadStatus) -> None:
    assert ThreadStatus.from_raw(status.value) is status
    assert ThreadStatus.from_raw("nonsense") is ThreadStatus.ACTIVE
