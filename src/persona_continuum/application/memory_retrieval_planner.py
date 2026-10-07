"""MemoryRetrievalPlanner: multi-tier, component-aware context retrieval.

Phase 8 replaces the binary RecallGate (recall / don't recall) with a planner
that runs every turn, distinguishes retrieval depth (BASE, STANDARD, DEEP), and
assembles an explainable, deduplicated MemoryBundle.

Principles:
1. BASE retrieval runs every turn: active threads, relationship state,
   current-valid high-confidence facts, current arc summary, recent dialogue.
2. Active threads are retrieved without requiring lexical hits, enabling
   implicit continuation (e.g. "票买好了" matches the live "重庆旅行" thread).
3. STANDARD retrieval expands into episodes, temporal/superseded facts,
   thread milestones, and chapters.
4. DEEP retrieval activates RawRecall for verbatim historical details
   (e.g. train departure time, berth number, verbatim quotes, error codes).
5. Freshness: reads current database state every turn.
6. Fallback: failure in any memory component gracefully degrades without crashing.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any

from persona_continuum.application.episode_service import EpisodeService
from persona_continuum.application.fact_service import SemanticFactService
from persona_continuum.application.hierarchy_service import HierarchyService
from persona_continuum.application.memory_service import MemoryService
from persona_continuum.application.raw_recall_service import RawRecallService
from persona_continuum.application.thread_service import ThreadService
from persona_continuum.domain.hierarchical_summary import SummaryReadiness
from persona_continuum.domain.memory_bundle import (
    BundleEpisodeItem,
    BundleFactItem,
    BundleHierarchicalSummaryItem,
    BundleRelationshipContext,
    BundleThreadItem,
    FactReliability,
    MemoryBundle,
    RetrievalMetadata,
    RetrievalMode,
    RetrievalTimings,
    estimate_bundle_tokens,
)
from persona_continuum.domain.raw_recall import (
    HistoricalExcerpt,
    RawMemoryRef,
    RawMemoryRefType,
    RawRecallBudget,
    RawRecallScope,
)
from persona_continuum.domain.semantic_fact import FactOrigin, FactStatus
from persona_continuum.room.context_policy import ContextProfile
from persona_continuum.room.recall_gate import RecallAnalysisResult, RecallGate
from persona_continuum.runtime.relationship_engine import RelationshipEngine
from persona_continuum.storage.database import Database

_logger = logging.getLogger(__name__)

# Patterns signaling STANDARD memory expansion
_PAST_PATTERNS = re.compile(
    r"(以前|之前|当时|上次|曾经|记得|你说过|我们讨论过|"
    r"发生过什么|为什么后来|谁曾经|过去|历史|从前|早前|后来|"
    r"earlier|previously|before|back then|remember when|"
    r"what did you say|in the past|history|formerly|used to)",
    re.IGNORECASE,
)

_PLAN_PATTERNS = re.compile(
    r"(计划|打算|准备|行程|去|出发|买好|票|约好|看电影|旅行|约|和好|吵架|联系|决定)",
    re.IGNORECASE,
)

_TEMPORAL_QUERY_PATTERNS = re.compile(
    r"(以前|过去|曾经|去年|前年|那时候|当时|那会儿|改喝|换成|之前|原来|从前)",
    re.IGNORECASE,
)

# Patterns signaling DEEP raw recall (asking for fine-grained verbatim details)
_DETAIL_PATTERNS = re.compile(
    r"(几点|车厢|铺位|卧铺|班次|哪天|哪趟|哪个|具体|原话|怎么说的|原句|"
    r"什么名字|叫什么|起名|哪家|哪家店|错误码|0x[0-9a-fA-F]+|ERR_[A-Za-z0-9_]+|"
    r"什么颜色|哪个颜色|哪张|多少钱|哪一桌|下到几点|住在哪里|民宿老板|老板娘|"
    r"买了什么|丢了什么|落下什么|落在|遗落|猫的名字|送的什么|包装|"
    r"多长时间|多久|晚点|品牌|型号|焦段|光圈|规格|背带|WiFi|wifi|密码|"
    r"剂量|频次|几粒|医嘱|地址|什么路|几号|端口|分支|书店|和解|争执)",
    re.IGNORECASE,
)

_IMPLICIT_CONTINUATION_TRIGGERS = [
    "买好了",
    "票买好了",
    "她又联系我了",
    "还是之前那个",
    "我决定去了",
    "去不了了",
    "已经订了",
    "退掉了",
    "和好了",
    "这次就到这",
]


def _bm25_score(query: str, text: str) -> float:
    """Fast lexical overlap score."""
    if not query or not text:
        return 0.0
    q_tokens = [t for t in re.findall(r"[\u4e00-\u9fff]|[a-zA-Z0-9_]+", query.casefold()) if t]
    if not q_tokens:
        return 0.0
    t_text = text.casefold()
    matches = sum(1 for token in q_tokens if token in t_text)
    return matches / max(1, len(q_tokens))


class MemoryRetrievalPlanner:
    """Assembles a bounded, deduplicated MemoryBundle for one conversation turn."""

    def __init__(
        self,
        database: Database,
        *,
        memories: MemoryService | None = None,
        episodes: EpisodeService | None = None,
        facts: SemanticFactService | None = None,
        threads: ThreadService | None = None,
        hierarchies: HierarchyService | None = None,
        raw_recall: RawRecallService | None = None,
        relationships: RelationshipEngine | None = None,
        recall_gate: RecallGate | None = None,
    ) -> None:
        self.database = database
        self.memories = memories
        self.episodes = episodes
        self.facts = facts
        self.threads = threads
        self.hierarchies = hierarchies
        self.raw_recall = raw_recall
        self.relationships = relationships
        self.recall_gate = recall_gate

    def plan_and_retrieve(
        self,
        *,
        persona_id: str,
        counterpart_id: str = "user",
        branch_id: str = "main",
        user_message: str = "",
        recent_transcript: list[dict[str, Any]] | None = None,
        room_topic: str | None = None,
        stored_rolling_summary: str | None = None,
        context_profile: ContextProfile | None = None,
        recall_gate_signal: RecallAnalysisResult | None = None,
        relationship_stance: str = "",
        current_time: datetime | None = None,
    ) -> MemoryBundle:
        """Execute multi-tier retrieval and produce a fresh MemoryBundle."""
        start_total = time.perf_counter()
        timings = RetrievalTimings()
        reasons: list[str] = []
        transcript = recent_transcript or []
        msg_clean = user_message.strip()

        # Step 1: Evaluate RecallGate signal (if provided or compute locally)
        gate_triggered = False
        gate_reasons: list[str] = []
        gate_query = msg_clean
        if recall_gate_signal is not None:
            gate_triggered = recall_gate_signal.triggered
            gate_reasons = list(recall_gate_signal.reasons)
            gate_query = recall_gate_signal.query or msg_clean
        elif self.recall_gate is not None:
            analysis = self.recall_gate.plan_turn(
                persona_id=persona_id,
                current_speaker_name=persona_id,
                user_message=msg_clean,
                recent_transcript=transcript,
                branch_id=branch_id,
            )
            gate_triggered = analysis.triggered
            gate_reasons = analysis.reasons
            gate_query = analysis.query or msg_clean

        # Step 2: Determine Retrieval Mode (BASE vs STANDARD vs DEEP)
        mode = RetrievalMode.BASE
        reasons.append("base_retrieval_active")

        is_past_query = bool(
            _PAST_PATTERNS.search(msg_clean) or _TEMPORAL_QUERY_PATTERNS.search(msg_clean)
        )
        is_plan_query = bool(_PLAN_PATTERNS.search(msg_clean))
        is_detail_query = bool(_DETAIL_PATTERNS.search(msg_clean))
        is_question = any(
            q in msg_clean for q in ["?", "？", "什么", "吗", "哪", "几", "谁", "怎么", "为什么"]
        )

        is_implicit_continuation = any(
            trig in msg_clean for trig in _IMPLICIT_CONTINUATION_TRIGGERS
        )

        if gate_triggered:
            reasons.append("recall_gate_signal")
            mode = RetrievalMode.STANDARD
        if is_past_query:
            reasons.append("past_temporal_query")
            mode = RetrievalMode.STANDARD
        if is_plan_query:
            reasons.append("plan_or_commitment_pattern")
            mode = RetrievalMode.STANDARD
        if is_implicit_continuation:
            reasons.append("implicit_thread_continuation")
            mode = RetrievalMode.STANDARD
        if room_topic:
            reasons.append("room_topic_context")

        if is_detail_query and (is_past_query or is_question or gate_triggered):
            reasons.append("detail_request_requires_raw_evidence")
            mode = RetrievalMode.DEEP

        # Step 3: Fetch Active Threads (BASE: always fetch live threads!)
        t0 = time.perf_counter()
        live_threads: list[BundleThreadItem] = []
        counts_available: dict[str, int] = {}
        counts_selected: dict[str, int] = {}

        if self.threads is not None:
            try:
                raw_threads = self.threads.list_threads(
                    persona_id=persona_id,
                    counterpart_id=counterpart_id,
                    branch_id=branch_id,
                    limit=50,
                )
                counts_available["threads"] = len(raw_threads)
                for th in raw_threads:
                    score = _bm25_score(msg_clean, f"{th.title} {th.summary}")
                    if th.is_live:
                        # Live threads receive baseline boost so implicit continuation works
                        score = max(score, 0.55)
                        if is_implicit_continuation:
                            # Match title/milestones
                            for ms in th.milestones:
                                if any(tok in msg_clean for tok in [ms, th.title]):
                                    score = max(score, 0.9)
                    elif is_past_query and score > 0.1:
                        score = max(score, 0.4)
                    else:
                        continue  # skip inactive threads with no lexical relevance

                    milestones = list(th.milestones)
                    events: list[dict[str, Any]] = []
                    source_turns: list[str] = []
                    source_eps: list[str] = []
                    if score >= 0.4:
                        try:
                            src_rows = self.threads.thread_sources(th.id)
                            source_turns = [
                                str(r["turn_id"]) for r in src_rows if r.get("turn_id")
                            ]
                            source_eps = [
                                str(r["episode_id"]) for r in src_rows if r.get("episode_id")
                            ]
                            th_events = self.threads.thread_events(th.id, limit=5)
                            events = [
                                {
                                    "event_type": e.event_type.value,
                                    "summary": e.summary,
                                    "occurred_at": e.occurred_at.isoformat()
                                    if e.occurred_at
                                    else None,
                                }
                                for e in th_events
                            ]
                        except Exception as exc:  # noqa: BLE001
                            _logger.debug("Failed fetching thread details: %s", exc)

                    display_item = BundleThreadItem(
                        id=th.id,
                        thread_key=th.thread_key,
                        title=th.title,
                        summary=th.summary,
                        thread_type=th.thread_type,
                        status=th.status,
                        is_live=th.is_live,
                        current_state=dict(th.current_state),
                        milestones=milestones,
                        recent_events=events,
                        relevance_score=score,
                        source_episode_ids=source_eps,
                        source_turn_ids=source_turns,
                    )
                    display_item.token_estimate = estimate_bundle_tokens(
                        display_item.clean_display()
                    )
                    live_threads.append(display_item)
            except Exception as exc:  # noqa: BLE001
                _logger.warning("Thread query fallback: %s", exc)
        timings.thread_query_ms = round((time.perf_counter() - t0) * 1000, 2)

        # Check if any live thread strongly matched the user message
        # (not merely baseline live presence)
        matched_live_threads = [t for t in live_threads if t.relevance_score >= 0.7]
        if matched_live_threads and mode == RetrievalMode.BASE:
            mode = RetrievalMode.STANDARD
            reasons.append("active_thread_matched")

        # Step 4: Fetch Semantic Facts
        t0 = time.perf_counter()
        selected_facts: list[BundleFactItem] = []
        if self.facts is not None:
            try:
                raw_facts = self.facts.list_facts(
                    persona_id=persona_id,
                    counterpart_id=counterpart_id,
                    branch_id=branch_id,
                    limit=100,
                )
                counts_available["facts"] = len(raw_facts)

                # Determine if user is asking about the past ("以前/曾经喜欢喝什么")
                temporal_terms = [
                    "以前", "过去", "曾经", "那时候", "原来", "之前", "改喝",
                ]
                wants_past_facts = bool(
                    _TEMPORAL_QUERY_PATTERNS.search(msg_clean)
                    and any(term in msg_clean for term in temporal_terms)
                )

                for f in raw_facts:
                    # Ignore unconfirmed candidates with conflicts unless disambiguating
                    if f.status == FactStatus.CANDIDATE:
                        continue
                    if f.status == FactStatus.RETRACTED or f.status == FactStatus.EXPIRED:
                        continue

                    score = _bm25_score(msg_clean, f.display_text)
                    is_current = f.is_valid_now

                    if wants_past_facts:
                        # If asking about past, superseded facts match with high relevance
                        if f.status == FactStatus.SUPERSEDED:
                            score = max(score, 0.7)
                        elif is_current and score > 0.2:
                            score = max(score, 0.4)
                    else:
                        # Regular current turn: active facts valid now get priority
                        if is_current:
                            score = max(score, 0.5 if mode == RetrievalMode.BASE else 0.6)
                        elif f.status == FactStatus.SUPERSEDED and score > 0.3:
                            # Superseded fact only included if explicitly mentioned
                            score = score * 0.5
                        else:
                            continue

                    # Reliability label
                    if f.status == FactStatus.SUPERSEDED:
                        rel = FactReliability.HISTORICAL_SUPERSEDED
                    elif f.origin == FactOrigin.USER_ASSERTED:
                        rel = FactReliability.CONFIRMED_USER
                    elif f.origin == FactOrigin.PERSONA_ASSERTED:
                        rel = FactReliability.CONFIRMED_PERSONA
                    elif f.origin == FactOrigin.SYSTEM_OBSERVED:
                        rel = FactReliability.SYSTEM_OBSERVED
                    else:
                        rel = FactReliability.INFERRED

                    # Get sources
                    source_turns = []
                    source_eps = []
                    if score >= 0.4:
                        try:
                            f_sources = self.facts.fact_sources(f.id)
                            source_turns = [
                                str(s["turn_id"]) for s in f_sources if s.get("turn_id")
                            ]
                            source_eps = [
                                str(s["episode_id"]) for s in f_sources if s.get("episode_id")
                            ]
                        except Exception as exc:  # noqa: BLE001
                            _logger.debug("Failed fetching fact sources: %s", exc)

                    b_fact = BundleFactItem(
                        id=f.id,
                        fact_key=f.fact_key,
                        subject=f.subject,
                        predicate=f.predicate,
                        value=(
                            f.value_json.get("text", f.value_key)
                            if f.value_json
                            else f.value_key
                        ),
                        display_text=f.display_text,
                        status=f.status,
                        origin=f.origin,
                        reliability=rel,
                        confidence=f.confidence,
                        valid_from=f.valid_from,
                        valid_until=f.valid_until,
                        is_valid_now=f.is_valid_now,
                        category=f.category,
                        plan_status=f.plan_status,
                        relevance_score=score,
                        provenance_strength=f.confidence,
                        source_turn_ids=source_turns,
                        source_episode_ids=source_eps,
                    )
                    b_fact.token_estimate = estimate_bundle_tokens(b_fact.clean_display())
                    selected_facts.append(b_fact)
            except Exception as exc:  # noqa: BLE001
                _logger.warning("Fact query fallback: %s", exc)
        timings.fact_query_ms = round((time.perf_counter() - t0) * 1000, 2)

        # Step 5: Fetch Relevant Episodes (STANDARD / DEEP)
        t0 = time.perf_counter()
        selected_episodes: list[BundleEpisodeItem] = []
        if self.episodes is not None and mode in {RetrievalMode.STANDARD, RetrievalMode.DEEP}:
            try:
                raw_episodes = self.episodes.list_episodes(
                    persona_id=persona_id,
                    counterpart_id=counterpart_id,
                    branch_id=branch_id,
                    limit=50,
                )
                counts_available["episodes"] = len(raw_episodes)
                for ep in raw_episodes:
                    if not ep.summary and not ep.title:
                        continue
                    struct = ep.structured_summary()
                    topics_str = " ".join(struct.topics)
                    entities_str = " ".join(struct.entities)
                    text_corpus = f"{ep.title} {ep.summary} {topics_str} {entities_str}"
                    score = _bm25_score(msg_clean, text_corpus)

                    # Also score against active thread titles/milestones
                    for b_thread in live_threads:
                        if b_thread.relevance_score >= 0.5 and ep.id in b_thread.source_episode_ids:
                            score = max(score, 0.6)

                    if score >= 0.2:
                        turns = []
                        try:
                            turns = [
                                t.turn_id for t in self.episodes.episode_turns(ep.id)
                            ]
                        except Exception as exc:  # noqa: BLE001
                            _logger.debug("Failed fetching episode turns: %s", exc)

                        time_str = ""
                        if ep.started_at:
                            time_str = ep.started_at.strftime("%Y-%m-%d %H:%M")

                        b_ep = BundleEpisodeItem(
                            id=ep.id,
                            title=ep.title,
                            summary=ep.summary,
                            status=ep.status,
                            started_at=ep.started_at,
                            ended_at=ep.ended_at,
                            time_range_display=time_str,
                            topics=list(struct.topics),
                            entities=list(struct.entities),
                            user_stated=list(struct.user_stated),
                            importance=ep.importance,
                            confidence=ep.confidence,
                            relevance_score=score,
                            source_turn_ids=turns,
                        )
                        b_ep.token_estimate = estimate_bundle_tokens(b_ep.clean_display())
                        selected_episodes.append(b_ep)
            except Exception as exc:  # noqa: BLE001
                _logger.warning("Episode query fallback: %s", exc)
        timings.episode_query_ms = round((time.perf_counter() - t0) * 1000, 2)

        # Step 6: Fetch Hierarchical Summaries (STANDARD / DEEP)
        t0 = time.perf_counter()
        selected_summaries: list[BundleHierarchicalSummaryItem] = []
        if self.hierarchies is not None and mode in {RetrievalMode.STANDARD, RetrievalMode.DEEP}:
            try:
                raw_summaries = self.hierarchies.list_summaries(
                    persona_id=persona_id,
                    counterpart_id=counterpart_id,
                    branch_id=branch_id,
                    limit=30,
                )
                counts_available["summaries"] = len(raw_summaries)
                for s in raw_summaries:
                    # Ignore STALE, FAILED, RETRACTED
                    if s.summary_status in {
                        SummaryReadiness.STALE,
                        SummaryReadiness.FAILED,
                    }:
                        continue
                    score = _bm25_score(msg_clean, f"{s.title} {s.summary}")
                    if is_past_query and score == 0.0:
                        score = 0.3  # past macro overview
                    if score >= 0.2:
                        time_str = ""
                        if s.started_at:
                            time_str = s.started_at.strftime("%Y-%m-%d")

                        b_sum = BundleHierarchicalSummaryItem(
                            id=s.id,
                            level=s.level,
                            summary_type=s.summary_type,
                            title=s.title,
                            summary=s.summary,
                            readiness=s.summary_status,
                            is_provisional=s.is_provisional,
                            started_at=s.started_at,
                            ended_at=s.ended_at,
                            time_range_display=time_str,
                            relevance_score=score,
                        )
                        b_sum.token_estimate = estimate_bundle_tokens(b_sum.clean_display())
                        selected_summaries.append(b_sum)
            except Exception as exc:  # noqa: BLE001
                _logger.warning("Hierarchy query fallback: %s", exc)
        timings.summary_query_ms = round((time.perf_counter() - t0) * 1000, 2)

        # Step 7: Check if DEEP retrieval should trigger based on memory hits
        # (e.g. if an episode/fact/thread hits a query with detail patterns like time, error code)
        if (
            mode == RetrievalMode.STANDARD
            and (is_detail_query or is_past_query)
            and (
                any(ep.relevance_score >= 0.4 for ep in selected_episodes)
                or any(th.relevance_score >= 0.5 for th in live_threads)
            )
        ):
            mode = RetrievalMode.DEEP
            reasons.append("memory_hit_detail_expansion")

        # Step 8: Execute DEEP Raw Recall
        t0 = time.perf_counter()
        raw_excerpts: list[HistoricalExcerpt] = []
        if self.raw_recall is not None and mode == RetrievalMode.DEEP:
            try:
                # Gather memory refs from top candidates
                memory_refs: list[RawMemoryRef | dict[str, Any]] = []
                for b_ep in sorted(selected_episodes, key=lambda e: -e.relevance_score)[:3]:
                    memory_refs.append(
                        RawMemoryRef(
                            ref_type=RawMemoryRefType.EPISODE,
                            ref_id=b_ep.id,
                            relevance=b_ep.relevance_score,
                        )
                    )
                for b_th in sorted(live_threads, key=lambda t: -t.relevance_score)[:2]:
                    memory_refs.append(
                        RawMemoryRef(
                            ref_type=RawMemoryRefType.THREAD,
                            ref_id=b_th.id,
                            relevance=b_th.relevance_score,
                        )
                    )
                for b_f in sorted(selected_facts, key=lambda x: -x.relevance_score)[:3]:
                    memory_refs.append(
                        RawMemoryRef(
                            ref_type=RawMemoryRefType.FACT,
                            ref_id=b_f.id,
                            relevance=b_f.relevance_score,
                        )
                    )
                for b_s in sorted(selected_summaries, key=lambda y: -y.relevance_score)[:2]:
                    memory_refs.append(
                        RawMemoryRef(
                            ref_type=RawMemoryRefType.SUMMARY,
                            ref_id=b_s.id,
                            relevance=b_s.relevance_score,
                        )
                    )

                # Budget from profile
                budget = RawRecallBudget(
                    max_total_tokens=4000,
                    max_excerpts=4,
                    max_tokens_per_excerpt=1200,
                )
                if context_profile is not None:
                    if context_profile.name == "local_constrained":
                        budget.max_total_tokens = 1500
                        budget.max_excerpts = 2
                        budget.max_tokens_per_excerpt = 600
                    elif context_profile.name == "remote_quality":
                        budget.max_total_tokens = max(
                            4000, context_profile.historical_excerpt_token_budget or 8000
                        )
                        budget.max_excerpts = max(
                            4, context_profile.historical_excerpt_messages or 6
                        )
                        budget.max_tokens_per_excerpt = 2000

                scope = RawRecallScope(
                    persona_id=persona_id,
                    counterpart_id=counterpart_id,
                    branch_id=branch_id,
                )
                recall_result = self.raw_recall.recall(
                    scope=scope,
                    query=gate_query or msg_clean,
                    memory_refs=memory_refs,
                    budget=budget,
                )
                raw_excerpts = recall_result.excerpts
                counts_available["raw_excerpts"] = recall_result.anchors_considered
            except Exception as exc:  # noqa: BLE001
                _logger.warning("Raw recall fallback: %s", exc)
        timings.raw_recall_ms = round((time.perf_counter() - t0) * 1000, 2)

        # Step 9: Relationship Context
        rel_context = BundleRelationshipContext()
        if self.relationships is not None:
            try:
                rel_state = self.relationships.get_relationship(
                    persona_id, counterpart_id, branch_id=branch_id
                )
                rel_context.current_state = rel_state.model_dump(mode="python")
                rel_context.relationship_kind = rel_state.relationship_kind
                rel_context.stance = (
                    relationship_stance or getattr(rel_state, "relationship_kind", "")
                )
                rel_context.token_estimate = estimate_bundle_tokens(
                    f"{rel_context.relationship_kind}: {rel_context.stance}"
                )
            except Exception as exc:  # noqa: BLE001
                _logger.debug("Relationship query fallback: %s", exc)

        # Step 10: Deduplication across components
        dedup_counts: dict[str, int] = {}

        # Deduplicate facts: same subject + predicate takes highest confidence / relevance
        seen_fact_slots: dict[str, BundleFactItem] = {}
        deduped_facts: list[BundleFactItem] = []
        for df in sorted(selected_facts, key=lambda x: (-x.relevance_score, -x.confidence)):
            slot = df.fact_key or f"{df.subject}|{df.predicate}"
            if slot in seen_fact_slots:
                dedup_counts["facts"] = dedup_counts.get("facts", 0) + 1
                continue
            seen_fact_slots[slot] = df
            deduped_facts.append(df)

        # Rank and sort components
        deduped_threads = sorted(
            live_threads, key=lambda t: (-t.relevance_score, -float(t.is_live))
        )
        deduped_episodes = sorted(selected_episodes, key=lambda e: -e.relevance_score)
        deduped_summaries = sorted(selected_summaries, key=lambda s: (-s.relevance_score, s.level))

        # Recent dialogue estimate
        recent_tokens = sum(
            estimate_bundle_tokens(str(t.get("content") or "")) for t in transcript
        )
        arc_tokens = estimate_bundle_tokens(stored_rolling_summary or "")

        counts_selected["facts"] = len(deduped_facts)
        counts_selected["threads"] = len(deduped_threads)
        counts_selected["episodes"] = len(deduped_episodes)
        counts_selected["summaries"] = len(deduped_summaries)
        counts_selected["raw_excerpts"] = len(raw_excerpts)

        timings.total_retrieval_ms = round((time.perf_counter() - start_total) * 1000, 2)
        timings.bundle_build_ms = timings.total_retrieval_ms

        metadata = RetrievalMetadata(
            mode=mode,
            reasons=reasons,
            query=gate_query or msg_clean,
            timings=timings,
            recall_gate_triggered=gate_triggered,
            recall_gate_reasons=gate_reasons,
            counts_available=counts_available,
            counts_selected=counts_selected,
            dedup_removed=dedup_counts,
        )

        return MemoryBundle(
            persona_id=persona_id,
            counterpart_id=counterpart_id,
            branch_id=branch_id,
            current_arc=stored_rolling_summary,
            current_arc_tokens=arc_tokens,
            recent_dialogue=transcript,
            recent_dialogue_tokens=recent_tokens,
            semantic_facts=deduped_facts,
            active_threads=deduped_threads,
            relevant_episodes=deduped_episodes,
            relationship_context=rel_context,
            hierarchical_summaries=deduped_summaries,
            historical_excerpts=raw_excerpts,
            retrieval_metadata=metadata,
        )


__all__ = ["MemoryRetrievalPlanner"]
