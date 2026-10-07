"""Large Conversation Pipeline V2 P0 acceptance tests.

Covers the wiring the V2 modification task demands: speaker-role routing,
ConversationTurn folding, dynamic windows, sparse conversation-evidence-v4
classification, checkpoint resume, style profiling, and metrics, with the
non-regression guarantees (no dropped raw rows, no exporter attribution,
ordinary documents unchanged).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from persona_continuum.application.chat_style_profiler import ChatStyleProfiler
from persona_continuum.application.material_chat import (
    SEMANTIC_CONTEXT_ONLY,
    SEMANTIC_EVIDENCE_EXTRACTED,
    SEMANTIC_REVIEWED_NO_EVIDENCE,
    SEMANTIC_TARGET_PENDING,
    fold_conversation_turns,
    parse_speaker_role_map,
)
from persona_continuum.application.material_intelligence import EvidenceUnit
from persona_continuum.application.material_pipeline import (
    AnalysisWindow,
    MaterialPipelineMetrics,
    ResolvedExecutionProfile,
    build_analysis_windows,
)
from persona_continuum.domain.persona import PersonaType

CHAT_KIND = "chat_import"
BASE = datetime(2023, 4, 3, 20, 0, 0, tzinfo=UTC)


def _persona(app, name="V2 Subject"):
    return app.personas.create(
        display_name=name,
        aliases=[],
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        run_mode="digital_continuation",
    )


def _stamp(offset_seconds):
    return (BASE + timedelta(seconds=offset_seconds)).isoformat().split("+")[0].replace("T", " ")


def _chat_unit(index, speaker, stamp, text=None, role=None, status=None):
    payload = text or f"消息{index}"
    return EvidenceUnit(
        id=f"evu_{index:05d}",
        persona_id="v2-persona",
        source_id="src_v2",
        source_locator={"segment_index": index},
        speaker=speaker,
        timestamp=stamp,
        text=payload,
        normalized_text=payload,
        source_kind=CHAT_KIND,
        speaker_role=role,
        metadata={} if status is None else {"semantic_status": status},
    )


# ---------------------------------------------------------------- roles ---
def test_parse_speaker_role_map_handles_real_headers():
    roles = parse_speaker_role_map(
        "# 我 = 聊天记录导出者\n# 对方 = 目标 Persona\n2023-04-03 20:44:53 | 对方 | hi"
    )
    assert roles == {"我": "exporter", "对方": "target_persona"}
    assert parse_speaker_role_map("no header here\n2023 x") == {}


@pytest.mark.anyio
async def test_ingest_assigns_roles_and_status_per_speaker(app):
    """BLOCKER 2: the role map is applied in the real ingest pipeline."""

    app.material_intelligence.semantic_gate_mode = "full"
    persona = _persona(app)
    lines = []
    for index in range(200):
        lines.append(f"{_stamp(index * 20)} | 对方 | target line {index}")
        lines.append(f"{_stamp(index * 20 + 10)} | 我 | context line {index}")
    source = app.personas.add_source_text(
        persona.id,
        title="role-chat",
        source_type="txt",
        canonical_url=None,
        publisher="user",
        author="user",
        published_at=None,
        accessed_at=None,
        content="# 我 = 聊天记录导出者\n# 对方 = 目标 Persona\n" + "\n".join(lines),
        metadata={},
    )
    calls: list[dict] = []

    async def analyzer(phase, payload):
        calls.append(payload)
        return {"units": []}

    job = await app.material_intelligence.analyze_sources_async(
        persona.id,
        [source.id],
        agent_analyzer=analyzer,
        agent_phases=("classify",),
    )
    assert job.status.value == "READY_FOR_COMPILATION"
    rows = {
        unit.id: unit
        for batch in app.material_intelligence._iter_units_batched(persona.id)
        for unit in batch
    }
    assert len(rows) == 400  # nothing dropped
    target_rows = [row for row in rows.values() if row.speaker == "对方"]
    exporter_rows_all = [row for row in rows.values() if row.speaker == "我"]
    assert len(target_rows) == 200
    assert all(row.speaker_role == "target_persona" for row in target_rows)
    assert all(
        (row.metadata or {}).get("semantic_status") == SEMANTIC_CONTEXT_ONLY
        for row in exporter_rows_all
    )
    target_ids = {tid for call in calls for tid in call["target_units"]}
    exporter_rows = {row.id for row in rows.values() if row.speaker == "我"}
    # Exporter lines never enter the semantic lane but DO ride along as
    # prompt context (P0-F).
    assert target_ids.isdisjoint(exporter_rows)
    rendered = {row["id"] for call in calls for row in call["units"]}
    assert exporter_rows <= rendered
    chat = job.progress.get("chat_pipeline") or {}
    assert chat.get("raw_message_count") == 400
    assert chat.get("target_message_count") == 200
    assert chat.get("context_message_count") == 200


@pytest.mark.anyio
async def test_context_only_never_keeps_pending(app):
    """Context-only lines cannot leave classification permanently pending."""

    persona = _persona(app)
    called = False

    async def analyzer(phase, payload):
        nonlocal called
        called = True
        return {"units": []}

    # Only the exporter speaks: zero semantic targets exist, so the Agent
    # must not be billed and pending must reach zero immediately.
    lines = [f"{_stamp(index * 30)} | 我 | ctx {index}" for index in range(20)]
    source = app.personas.add_source_text(
        persona.id,
        title="target-silent",
        source_type="txt",
        canonical_url=None,
        publisher="user",
        author="user",
        published_at=None,
        accessed_at=None,
        content="# 我 = 聊天记录导出者\n# 对方 = 目标 Persona\n" + "\n".join(lines),
        metadata={},
    )
    job = await app.material_intelligence.analyze_sources_async(
        persona.id,
        [source.id],
        agent_analyzer=analyzer,
        agent_phases=("classify",),
        incremental=True,
    )
    assert job.progress.get("classification_pending") == 0
    assert job.progress.get("classification_total") == 0
    assert not called  # no semantic target at all: nothing to bill


# ------------------------------------------------------------- folding ---
def test_five_same_speaker_messages_fold_into_one_turn():
    units = [_chat_unit(index, "对方", _stamp(index * 15)) for index in range(5)]
    turns = fold_conversation_turns(units, gap_seconds=90)
    assert len(turns) == 1
    assert turns[0].evidence_unit_ids == [unit.id for unit in units]
    assert turns[0].anchor_id == "evu_00000"
    assert turns[0].text == "\n".join(unit.text for unit in units)


def test_speaker_change_flushes_turn():
    units = [
        _chat_unit(0, "对方", _stamp(0)),
        _chat_unit(1, "对方", _stamp(10)),
        _chat_unit(2, "我", _stamp(20)),
        _chat_unit(3, "对方", _stamp(30)),
    ]
    turns = fold_conversation_turns(units, gap_seconds=90)
    assert [len(turn.evidence_unit_ids) for turn in turns] == [2, 1, 1]


def test_gap_over_threshold_flushes_turn():
    units = [
        _chat_unit(0, "对方", _stamp(0)),
        _chat_unit(1, "对方", _stamp(89)),
        _chat_unit(2, "对方", _stamp(180)),
    ]
    turns = fold_conversation_turns(units, gap_seconds=90)
    assert [len(turn.evidence_unit_ids) for turn in turns] == [2, 1]


def test_emoji_voice_and_unicode_survive_folding():
    tricky = [
        _chat_unit(0, "对方", _stamp(0), "<voice>还可以。</voice>"),
        _chat_unit(1, "对方", _stamp(10), '<emoji name="流泪" count="4"/>'),
        _chat_unit(2, "对方", _stamp(20), "😂❤️"),
    ]
    turns = fold_conversation_turns(tricky, gap_seconds=90)
    assert len(turns) == 1
    folded = turns[0]
    assert folded.evidence_unit_ids == [unit.id for unit in tricky]
    for token in ("<voice>还可以。</voice>", '<emoji name="流泪" count="4"/>', "😂❤️"):
        assert token in folded.text


# ------------------------------------------------------------ windows ----
def test_windows_ignore_legacy_160_ceiling_for_short_chats():
    units = [
        EvidenceUnit(
            id=f"evu_{index:05d}",
            persona_id="p",
            source_id="s",
            text="短句" * 6,
            normalized_text="短句" * 6,
        )
        for index in range(1000)
    ]
    windows = build_analysis_windows(
        units,
        estimate_tokens=lambda value: max(1, len(value) // 4),
        target_tokens=100_000,
        max_units=1200,
    )
    assert sum(len(window.evidence_unit_ids) for window in windows) == 1000
    # Pre-V2 (max_units=160) forced ceil(1000/160)=7 windows; a token budget
    # of 100k now packs them into one reasoning window.
    assert len(windows) == 1


def test_windows_honour_safety_ceiling_when_configured():
    units = [
        EvidenceUnit(id=f"u{index}", persona_id="p", source_id="s", text="x", normalized_text="x")
        for index in range(500)
    ]
    windows = build_analysis_windows(
        units, estimate_tokens=lambda value: 5, target_tokens=1_000_000, max_units=100
    )
    assert len(windows) == 5
    assert all(len(window.evidence_unit_ids) == 100 for window in windows)


def test_windows_allow_none_ceiling():
    units = [
        EvidenceUnit(id=f"u{index}", persona_id="p", source_id="s", text="x", normalized_text="x")
        for index in range(250)
    ]
    windows = build_analysis_windows(
        units, estimate_tokens=lambda value: 5, target_tokens=1_000_000, max_units=None
    )
    assert len(windows) == 1


# ------------------------------------------------------- sparse output ---
@pytest.mark.anyio
async def test_sparse_500_targets_with_12_emitted_marks_rest(app):
    """BLOCKER 7: 12/500 emitted = full success; 488 local marks; 0 retries."""

    persona = _persona(app)
    service = app.material_intelligence
    source = app.personas.add_source_text(
        persona.id,
        title="sparse-src",
        source_type="txt",
        canonical_url=None,
        publisher="user",
        author="user",
        published_at=None,
        accessed_at=None,
        content="placeholder",
        metadata={},
    )
    units = [
        EvidenceUnit(
            id=f"evu_{index:05d}",
            persona_id=persona.id,
            source_id=source.id,
            source_locator={"segment_index": index},
            speaker="对方",
            timestamp=_stamp(index * 600),
            text=f"target message {index}",
            normalized_text=f"target message {index}",
            source_kind=CHAT_KIND,
        )
        for index in range(500)
    ]
    service._persist_units(units)

    calls = []

    async def analyzer(phase, payload):
        calls.append(payload)
        emit = payload["target_units"][:12] if len(calls) == 1 else []
        return {
            "units": [
                {
                    "id": tid,
                    "claims": ["extracted"],
                    "dimension_scores": {"decisions_and_behavior": 0.8},
                }
                for tid in emit
            ]
        }

    profile = ResolvedExecutionProfile()
    metrics = MaterialPipelineMetrics()
    events = []

    async def report(event):
        events.append(event)

    await service._classify_persisted_with_agent(
        persona.id,
        analyzer,
        execution_profile=profile,
        metrics=metrics,
        window_progress=report,
    )
    assert len(calls) == metrics.agent_calls
    # A big window may split for the Prompt Size Guard (rebatched_windows),
    # but the sparse semantics must not re-request missing ids: dispatch stays
    # bounded by windows + re-batch splits, never by per-unit retries.
    assert len(calls) <= metrics.classification_windows_total + metrics.rebatched_windows + 1
    assert metrics.reviewed_no_independent_evidence == 500 - 12
    stored = {unit.id: unit for batch in service._iter_units_batched(persona.id) for unit in batch}
    assert len(stored) == 500
    extracted = [
        unit
        for unit in stored.values()
        if (unit.metadata or {}).get("semantic_status") == SEMANTIC_EVIDENCE_EXTRACTED
    ]
    reviewed = [
        unit
        for unit in stored.values()
        if (unit.metadata or {}).get("semantic_status") == SEMANTIC_REVIEWED_NO_EVIDENCE
    ]
    assert len(extracted) == 12
    assert len(extracted) + len(reviewed) == 500
    assert not any(
        (unit.metadata or {}).get("semantic_status") == SEMANTIC_TARGET_PENDING
        for unit in stored.values()
    )
    assert events[-1]["classification_pending"] == 0


@pytest.mark.anyio
async def test_resume_after_partial_run_bills_only_remaining(app):
    """BLOCKER 8: a 60% interrupted run resumes with only the remaining 40%."""

    persona = _persona(app)
    service = app.material_intelligence
    source = app.personas.add_source_text(
        persona.id,
        title="resume-src",
        source_type="txt",
        canonical_url=None,
        publisher="user",
        author="user",
        published_at=None,
        accessed_at=None,
        content="placeholder",
        metadata={},
    )
    units = [
        EvidenceUnit(
            id=f"evu_{index:05d}",
            persona_id=persona.id,
            source_id=source.id,
            source_locator={"segment_index": index},
            speaker="对方",
            timestamp=_stamp(index * 600),
            text=f"msg {index}",
            normalized_text=f"msg {index}",
            source_kind=CHAT_KIND,
        )
        for index in range(50)
    ]
    service._persist_units(units)

    def five_turn_windows(items, **kwargs):
        return [
            AnalysisWindow(
                id=f"aw-{start}",
                evidence_unit_ids=[turn.id for turn in items[start : start + 5]],
                source_ids=[str(turn.source_id) for turn in items[start : start + 5]][:1],
                text="",
                token_estimate=16,
            )
            for start in range(0, len(items), 5)
        ]

    import persona_continuum.application.material_intelligence as mi

    original = mi.build_analysis_windows
    mi.build_analysis_windows = five_turn_windows
    first_pass: list[list[str]] = []
    second_pass: list[str] = []
    try:

        async def flaky(phase, payload):
            first_pass.append(list(payload["target_units"]))
            if len(first_pass) >= 7:
                raise RuntimeError("synthetic interruption at 60%")
            return {"units": []}

        profile = ResolvedExecutionProfile()
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            await service._classify_persisted_with_agent(
                persona.id, flaky, execution_profile=profile, metrics=MaterialPipelineMetrics()
            )
        completed = [tid for call in first_pass[:6] for tid in call]
        assert len(completed) == 30

        async def resuming(phase, payload):
            second_pass.extend(payload["target_units"])
            return {"units": []}

        await service._classify_persisted_with_agent(
            persona.id, resuming, execution_profile=profile, metrics=MaterialPipelineMetrics()
        )
    finally:
        mi.build_analysis_windows = original
    assert set(second_pass).isdisjoint(completed)
    assert len(set(second_pass)) == 20


@pytest.mark.anyio
async def test_production_request_carries_no_reviewed_ids(app):
    """BLOCKER 5/6: v4 request bodies never mention reviewed_ids."""

    persona = _persona(app)
    service = app.material_intelligence
    source = app.personas.add_source_text(
        persona.id,
        title="contract-src",
        source_type="txt",
        canonical_url=None,
        publisher="user",
        author="user",
        published_at=None,
        accessed_at=None,
        content="placeholder",
        metadata={},
    )
    units = [
        EvidenceUnit(
            id=f"evu_{index:05d}",
            persona_id=persona.id,
            source_id=source.id,
            source_locator={"segment_index": index},
            speaker="对方",
            timestamp=_stamp(index * 600),
            text=f"c {index}",
            normalized_text=f"c {index}",
            source_kind=CHAT_KIND,
        )
        for index in range(5)
    ]
    service._persist_units(units)
    seen: list[dict] = []

    async def analyzer(phase, payload):
        seen.append(payload)
        return {"units": []}

    await service._classify_persisted_with_agent(
        persona.id,
        analyzer,
        execution_profile=ResolvedExecutionProfile(),
        metrics=MaterialPipelineMetrics(),
    )
    assert seen
    for request in seen:
        assert "reviewed_ids" not in request
        for row in request["units"]:
            assert "reviewed_ids" not in row
        assert "Do not return reviewed_ids" in request["output_contract"]
        # The schema handed to the Agent no longer requires the field either.
        from persona_continuum.application.material_intelligence import (
            MATERIAL_AGENT_OUTPUT_SCHEMAS,
        )

        assert "reviewed_ids" not in json.dumps(
            MATERIAL_AGENT_OUTPUT_SCHEMAS["classify"], ensure_ascii=False
        )
    stored = service._load_units(persona.id)
    stamped = [
        unit
        for unit in stored
        if (unit.metadata or {}).get("classification_contract") == "conversation-evidence-v4"
    ]
    assert stamped  # checkpoints wrote the v4 contract name


# ----------------------------------------------------------- profiler ----
def test_style_profiler_only_target_persona():
    target = [
        _chat_unit(
            index,
            "对方",
            _stamp(index * 60),
            text=f"目标说{index}",
            role="target_persona",
            status=SEMANTIC_TARGET_PENDING,
        )
        for index in range(6)
    ]
    exporter = [
        _chat_unit(
            100 + index,
            "我",
            _stamp(index * 60 + 30),
            text="导出者话痨导出者话痨",
            role="exporter",
            status=SEMANTIC_CONTEXT_ONLY,
        )
        for index in range(4)
    ]
    profile = ChatStyleProfiler().profile("p", target + exporter)
    assert profile is not None
    assert profile.corpus_size == 6
    assert "导出者话痨" not in str(profile.statistics)
    assert profile.method == "statistical_deterministic"
    assert profile.kind == "expression_profile"


@pytest.mark.anyio
async def test_style_profile_persists_and_feeds_expression_dna(app):
    persona = _persona(app)
    lines = [
        f"{_stamp(index * 40)} | 对方 | {'哈哈' if index % 3 == 0 else '好的吧'}{index}"
        for index in range(12)
    ]
    lines[3] = f'{_stamp(3 * 40)} | 对方 | <emoji name="流泪" count="2"/>'
    lines[7] = f"{_stamp(7 * 40)} | 对方 | <voice>语音转写内容</voice>"
    source = app.personas.add_source_text(
        persona.id,
        title="style-src",
        source_type="txt",
        canonical_url=None,
        publisher="user",
        author="user",
        published_at=None,
        accessed_at=None,
        content="# 我 = 聊天记录导出者\n# 对方 = 目标 Persona\n" + "\n".join(lines),
        metadata={},
    )

    async def analyzer(phase, payload):
        return {"units": []}

    job = await app.material_intelligence.analyze_sources_async(
        persona.id, [source.id], agent_analyzer=analyzer, agent_phases=("classify",)
    )
    metrics = job.progress.get("chat_pipeline") or {}
    assert metrics.get("style_profile_status") == "completed"
    profile = app.material_intelligence.get_style_profile(persona.id)
    assert profile is not None
    assert profile["kind"] == "expression_profile"
    assert profile["corpus_size"] == 12
    emoji_names = {row["value"] for row in profile["statistics"]["platform_emoji"]}
    assert "流泪" in emoji_names
    assert profile["statistics"]["voice_message_count"] == 1
    assert profile["representative_evidence_ids"]
    index = app.material_intelligence.get_index(persona.id)
    hits = index.retrieve("expression_dna", top_k=5, diversity=False)
    assert any(hit["kind"] == "expression_profile" for hit in hits)


# ------------------------------------------------------ document parity ---
@pytest.mark.anyio
async def test_plain_documents_unaffected(app):
    """Ordinary (non-chat) material keeps the document pipeline."""

    persona = _persona(app)
    source = app.personas.add_source_text(
        persona.id,
        title="notes",
        source_type="txt",
        canonical_url=None,
        publisher="user",
        author="user",
        published_at=None,
        accessed_at=None,
        content="我在上海长大。后来选择了工程师这份职业，喜欢把复杂问题拆解清楚。",
        metadata={"provenance": "user_provided"},
    )
    calls = []

    async def analyzer(phase, payload):
        calls.append(payload)
        return {"units": []}

    job = await app.material_intelligence.analyze_sources_async(
        persona.id,
        [source.id],
        agent_analyzer=analyzer,
        agent_phases=("classify",),
    )
    assert job.status.value == "READY_FOR_COMPILATION"
    rows = app.material_intelligence._load_units(persona.id)
    assert rows
    for row in rows:
        assert row.source_kind == "user_provided"
        # No chat routing ever touches plain documents.
        assert (row.metadata or {}).get("semantic_status") not in {
            "context_only",
            "target_pending",
        }
        assert row.speaker_role is None
    assert calls  # documents still get classification

