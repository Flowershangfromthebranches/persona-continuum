"""Retrieval snapshot: same ranking contract, without loading the whole ledger."""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta

import pytest

from persona_continuum.application.material_intelligence import (
    EvidenceUnit,
    FusedEvidence,
    MaterialJobStatus,
)
from persona_continuum.domain.persona import PersonaType

DIM = "values_desires_contradictions"


def _persona(app, name: str = "Subject") -> tuple[str, str]:
    persona = app.personas.create(
        display_name=name,
        aliases=[],
        persona_type=PersonaType.PRIVATE_LIVING_PERSON,
        run_mode="digital_continuation",
    )
    source = app.personas.add_source_text(
        persona.id, title="chat", source_type="txt", canonical_url=None,
        publisher="user", author="user", published_at=None, accessed_at=None,
        content="placeholder", metadata={},
    )
    return persona.id, source.id


def _unit(pid: str, sid: str, index: int, text: str, **kwargs) -> EvidenceUnit:
    return EvidenceUnit(
        id=f"u{index:04d}", persona_id=pid, source_id=sid, text=text, normalized_text=text,
        **kwargs,
    )


def _fused(pid: str, sid: str, name: str, ids: list[str], score: float) -> FusedEvidence:
    return FusedEvidence(
        id=f"evf_{name}",
        persona_id=pid,
        canonical_claim=f"claim {name}",
        evidence_type="behavior",
        dimension_scores={DIM: score},
        supporting_evidence_ids=ids,
        unique_evidence_ids=ids,
        source_ids=[sid],
    )


@pytest.fixture()
def seeded(app):
    pid, sid = _persona(app)
    service = app.material_intelligence
    service._persist_units(
        [
            _unit(pid, sid, 1, "重视承诺", dimension_scores={DIM: 0.9},
                  metadata={"evidence_intelligence": {"note": "value"}}),
            _unit(pid, sid, 2, "说到做到", dimension_scores={DIM: 0.8}),
            _unit(pid, sid, 3, "导出者自己的话", dimension_scores={DIM: 0.9},
                  metadata={"semantic_status": "context_only"}),
            # Never cited by any claim: ranking must not need to load these.
            *[_unit(pid, sid, 100 + i, f"闲聊 {i}") for i in range(50)],
        ]
    )
    service._replace_derived_atomic(
        pid,
        [],
        [],
        [
            _fused(pid, sid, "kept", ["u0001", "u0002"], 0.7),
            _fused(pid, sid, "context", ["u0001", "u0003"], 0.95),
            _fused(pid, sid, "dangling", ["u0001", "u9999"], 0.95),
        ],
        [],
    )
    return app, pid


def test_claims_citing_context_or_missing_units_are_excluded(seeded) -> None:
    app, pid = seeded
    ids = [item["id"] for item in app.material_intelligence.get_index(pid).retrieve(DIM)]

    assert "evf_kept" in ids
    assert "evf_context" not in ids
    assert "evf_dangling" not in ids
    assert "u0003" not in ids


def test_snapshot_skips_uncited_units_and_keeps_intelligence(seeded) -> None:
    app, pid = seeded
    index = app.material_intelligence.get_index(pid)

    kept = next(item for item in index.retrieve(DIM) if item["id"] == "evf_kept")

    assert set(index._retrieval_snapshot().distinct_units) == {"u0001", "u0002"}
    assert kept["intelligence"] == [{"note": "value"}]


def test_snapshot_is_loaded_once_per_index(seeded, monkeypatch) -> None:
    app, pid = seeded
    index = app.material_intelligence.get_index(pid)
    calls: list[int] = []
    original = index._load_snapshot
    monkeypatch.setattr(index, "_load_snapshot", lambda: calls.append(1) or original())

    threads = [threading.Thread(target=index.retrieve, args=(DIM,)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == [1]


def test_lookup_semantic_resolves_only_requested_ids(seeded) -> None:
    app, pid = seeded
    units, fused = app.material_intelligence.get_index(pid).lookup_semantic(
        ["u0002", "u0003", "evf_kept", "evf_context"]
    )

    assert set(units) == {"u0002"}
    assert set(fused) == {"evf_kept"}


def test_retrieval_is_deterministic_for_tied_scores(app) -> None:
    pid, sid = _persona(app, "Ties")
    service = app.material_intelligence
    units = [_unit(pid, sid, i, f"同分 {i}", dimension_scores={DIM: 0.5}) for i in range(1, 30)]
    service._persist_units(units)
    service._replace_derived_atomic(
        pid, [], [], [_fused(pid, sid, "all", [u.id for u in units], 0.5)], []
    )

    first = [item["id"] for item in service.get_index(pid).retrieve(DIM, top_k=10)]
    second = [item["id"] for item in service.get_index(pid).retrieve(DIM, top_k=10)]

    assert first == second


def test_ledger_without_claims_falls_back_to_all_semantic_units(app) -> None:
    pid, sid = _persona(app, "Legacy")
    app.material_intelligence._persist_units(
        [
            _unit(pid, sid, 1, "目标的话", dimension_scores={DIM: 0.6}),
            _unit(pid, sid, 2, "导出者", dimension_scores={DIM: 0.9},
                  metadata={"semantic_status": "context_only"}),
        ]
    )

    ids = [item["id"] for item in app.material_intelligence.get_index(pid).retrieve(DIM)]

    assert ids == ["u0001"]


def _material_job(app, pid: str, status: MaterialJobStatus, **progress):
    service = app.material_intelligence
    job = service.create_job(pid)
    job.status = status
    job.progress = {**job.progress, **progress}
    service._save_job(job)
    return job


def test_reclaim_fails_jobs_whose_owner_process_is_gone(app) -> None:
    pid, _ = _persona(app)
    dead = _material_job(app, pid, MaterialJobStatus.ANALYZING, worker_pid=2**22 + 12345)
    live = _material_job(app, pid, MaterialJobStatus.ANALYZING, worker_pid=os.getpid())
    done = _material_job(
        app, pid, MaterialJobStatus.READY_FOR_COMPILATION, worker_pid=2**22 + 1
    )
    service = app.material_intelligence

    assert service.reclaim_interrupted_jobs() == 1

    reclaimed = service.get_job(dead.id)
    assert reclaimed.status is MaterialJobStatus.FAILED
    assert (reclaimed.error or "").startswith("MATERIAL_JOB_INTERRUPTED")
    assert reclaimed.progress["interrupted_stage"]
    assert service.get_job(live.id).status is MaterialJobStatus.ANALYZING
    assert service.get_job(done.id).status is MaterialJobStatus.READY_FOR_COMPILATION


def test_reclaim_judges_legacy_jobs_by_silence(app) -> None:
    pid, _ = _persona(app)
    service = app.material_intelligence
    stale = _material_job(app, pid, MaterialJobStatus.SEGMENTING)
    recent = _material_job(app, pid, MaterialJobStatus.ANALYZING)
    for job, age in ((stale, timedelta(hours=3)), (recent, timedelta(minutes=5))):
        job.progress.pop("worker_pid", None)
        job.updated_at = (datetime.now(UTC) - age).isoformat()
        service._save_job(job)

    assert service.reclaim_interrupted_jobs() == 1
    assert service.get_job(stale.id).status is MaterialJobStatus.FAILED
    assert service.get_job(recent.id).status is MaterialJobStatus.ANALYZING


def test_concurrent_retrieval_never_shares_the_event_loop_connection(seeded) -> None:
    """Same SQL on one connection from two threads raised InterfaceError."""

    app, pid = seeded
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(20):
                app.material_intelligence.get_index(pid).retrieve(DIM)
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    # The event loop keeps reading and writing on the shared connection.
    for _ in range(200):
        app.database.conn.execute(
            "SELECT profile_json FROM persona_chat_style_profiles WHERE persona_id = ?", (pid,)
        ).fetchone()
        app.material_intelligence.create_job(pid)
    for thread in threads:
        thread.join()

    assert errors == []
