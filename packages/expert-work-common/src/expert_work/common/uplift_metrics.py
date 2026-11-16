"""Prometheus counters for the Capability Uplift threat scanners.

See ``docs/streams/STREAM-UPLIFT-DESIGN.md`` § 1.3 + § 2.6 + § 3.7.

Lives in :mod:`expert_work.common` so the orchestrator memory-recall
path (which has its own threat scan + drift redaction) and the
control-plane API + scheduler paths can all import without forming a
``orchestrator → control_plane`` cycle.

Counters are intentionally low-cardinality:

- ``scope`` ∈ ``{all, context, strict}`` (3 values)
- ``result`` ∈ ``{clean, blocked, warned}`` (3 values)
- ``phase`` ∈ ``{create, fire}`` (2 values)
- ``source`` ∈ ``{api, writeback, dlq}`` for memory writes (3 values)
- ``pattern_id`` is bounded by the pattern registry (currently ~30 IDs);
  if the registry grows past 100 we'll move to a histogram-of-counts
  per ``category`` instead.
"""

from __future__ import annotations

from collections.abc import Iterable

from expert_work.common.observability import expert_work_counter
from expert_work.common.observability.metrics import expert_work_gauge
from expert_work.common.threat_patterns import ThreatFinding

_scan_total = expert_work_counter(
    "expert_work_uplift_threat_scan_total",
    "Number of threat-scan invocations, partitioned by scope, result, "
    "and (Sprint #3 U-22) which normalized variant matched. "
    "Variant enum: original | nfkc | collapsed | base64.",
    label_names=("scope", "result", "variant"),
)

_pattern_hit_total = expert_work_counter(
    "expert_work_uplift_threat_pattern_hit_total",
    "Per-pattern hit count from threat scans (used for tuning). "
    "Sprint #3 U-22 adds the ``variant`` label so SecOps can see which "
    "obfuscation pre-processing pass surfaced a finding.",
    label_names=("pattern_id", "scope", "variant"),
)

_triggers_blocked_total = expert_work_counter(
    "expert_work_uplift_triggers_blocked_total",
    "Triggers refused by the prompt-injection scanner.",
    label_names=("phase",),
)

# Capability Uplift Sprint #2 — memory poisoning defense + drift detection.
_memory_blocked_total = expert_work_counter(
    "expert_work_uplift_memory_writes_blocked_total",
    "Memory writes refused by the strict prompt-injection scanner.",
    label_names=("source",),  # api / writeback / dlq
)

_memory_redacted_total = expert_work_counter(
    "expert_work_uplift_memory_recalls_redacted_total",
    "Memory items replaced with a [BLOCKED:<category>] placeholder at recall time.",
)

_memory_drift_total = expert_work_counter(
    "expert_work_uplift_memory_drift_total",
    "Memory rows whose recomputed content_hash diverged from the stored value.",
)

# Capability Uplift Sprint #6 — memory hybrid retrieval observability.
_memory_retrieval_total = expert_work_counter(
    "expert_work_uplift_memory_retrieval_total",
    "Memory recall invocations, partitioned by mode and result.",
    label_names=("mode", "result"),  # mode=hybrid|vector, result=hit|miss
)

# Capability Uplift Sprint #8 — memory frozen snapshot + Anthropic cache anchor.
_memory_inject_mode_total = expert_work_counter(
    "expert_work_uplift_memory_recall_inject_mode_total",
    "Memory recall injection mode chosen at agent_node render time.",
    label_names=("mode",),  # per_session | per_turn
)

# Stream CM-4 — cross-encoder rerank of long-term memory recall candidates.
_memory_rerank_total = expert_work_counter(
    "expert_work_cm_memory_rerank_total",
    "Long-term memory recall reranks, partitioned by outcome.",
    label_names=("outcome",),  # reranked | degraded
)

# Stream CM-6 — MMR diversity selection at the end of the recall pipeline.
_memory_mmr_total = expert_work_counter(
    "expert_work_cm_memory_mmr_total",
    "Long-term memory recall MMR selections, partitioned by outcome.",
    label_names=("outcome",),  # applied | degraded
)

# Stream Memory-Enhance (M-3) — read-time verification of recall candidates.
_memory_verify_total = expert_work_counter(
    "expert_work_memory_verify_total",
    "Long-term memory read-time verifications, partitioned by outcome.",
    label_names=("outcome",),  # verified | degraded
)

# Stream P5a (Task 8) — recall abstention threshold gate.
_memory_abstain_total = expert_work_counter(
    "expert_work_memory_abstain_total",
    "Recalls that returned empty because the top candidate's similarity fell "
    "below the abstention threshold (P5a).",
)

# Stream P5a (Task 7) — retrieval query rewrite outcomes.
_memory_rewrite_total = expert_work_counter(
    "expert_work_memory_rewrite_total",
    "Recall query rewrites, partitioned by outcome (P5a).",
    label_names=("outcome",),  # rewritten | unchanged | degraded
)

# Stream CM-7 — Mem0-style reconciliation of run-end memory writes.
_memory_reconcile_total = expert_work_counter(
    "expert_work_cm_memory_reconcile_total",
    "Run-end memory write reconciliations, partitioned by applied op.",
    label_names=("op",),  # see record_memory_reconcile
)

# B-300 redesign §7.1 row 6 — the target the reconcile model picked: its cosine rank
# within the candidate's cell and a coarse similarity band (no gate; counted only).
_memory_reconcile_target_total = expert_work_counter(
    "expert_work_memory_reconcile_target_total",
    "Reconcile targets picked by the model, by op, cosine rank in the cell and similarity.",
    label_names=("op", "rank", "similarity"),
)

# B-300 D3 (incl. G13) — long-term memory extraction, per pass. ``extraction_pass`` is
# ``user`` (only what the user typed) or ``conversation`` (the transcript).
_memory_extraction_total = expert_work_counter(
    "expert_work_memory_extraction_total",
    "Long-term memory extraction calls, by pass and outcome (B-300).",
    label_names=(
        "extraction_pass",
        "outcome",
    ),  # extracted | empty | truncated | parse_failed | error
)
# B-300 redesign §4 / §5.4 — what the program changed after checking a reply's ``basis`` /
# ``turn`` / ``event_date``: ``downgraded_<reason>`` (a user answer / agreement that does
# not anchor on a user line, so it is stored as from the assistant), ``pointer_missing``
# (no evidence pointer because ``turn`` is missing or out of range; a ``turn`` on a context
# line is never counted here), ``event_date_dropped``, ``context_line_dropped`` (``turn`` is a
# context line above ``=== new ===``: not stored — B-300 test acceptance 11-16), and, user
# 11-16: a user answer / agreement whose ``turn`` is an assistant line directly followed by a
# new-segment user line is re-anchored to that user line and checked again —
# ``turn_reanchored_context`` (the assistant line was the last context line) /
# ``turn_reanchored_in_segment`` (it was in the new segment too); passed: stored as
# user-confirmed, pointer on that line. Sum them with ``check=~"turn_reanchored_.*"``.
_memory_extraction_check_total = expert_work_counter(
    "expert_work_memory_extraction_check_total",
    "Extraction results the program changed after checking them, by pass and check (B-300).",
    label_names=("extraction_pass", "check"),
)
# B-300 redesign PR S (B-176 M2-1) — what incremental slicing did to a write-back segment.
_memory_writeback_segment_total = expert_work_counter(
    "expert_work_memory_writeback_segment_total",
    "Write-back segment events (incremental slicing, retries, config errors), by event (B-300).",
    label_names=("event",),
)
_memory_extraction_message_truncated_total = expert_work_counter(
    "expert_work_memory_extraction_message_truncated_total",
    "Messages cut at the per-message character cap in an extraction input (B-300).",
    label_names=("extraction_pass",),
)

_anthropic_cache_anchors_total = expert_work_counter(
    "expert_work_uplift_anthropic_cache_anchors_total",
    "Total cache_control anchor markers added by upstream injectors "
    "(currently only Sprint #8 memory frozen snapshot).",
)

# Capability Uplift Sprint #5 — MCP client HTTP/SSE transport observability.
_mcp_call_total = expert_work_counter(
    "expert_work_uplift_mcp_call_total",
    "MCP tool-call attempts partitioned by transport / server / result. "
    "Result enum: ok | timeout | 4xx | 5xx | circuit_open | transport_err.",
    label_names=("transport", "server", "result"),
)

_mcp_circuit_state_total = expert_work_counter(
    "expert_work_uplift_mcp_circuit_state_total",
    "MCP per-server circuit-breaker transitions (Mini-ADR U-13). "
    "State enum: closed | half_open | open.",
    label_names=("server", "state"),
)

# Capability Uplift Sprint #3 — Skill supporting files + drift + publish gate.
_skill_view_total = expert_work_counter(
    "expert_work_uplift_skill_view_total",
    "skill_view tool invocations partitioned by result. Result enum: ok | not_found | truncated.",
    label_names=("result",),
)

_skill_zip_reject_total = expert_work_counter(
    "expert_work_uplift_skill_zip_reject_total",
    "Skill ZIP imports rejected at the boundary. "
    "Reason label is an allowlisted enum (no user paths) for Oracle defense.",
    label_names=("reason",),
)

_skill_blocked_total = expert_work_counter(
    "expert_work_uplift_skill_blocked_total",
    "Skill content rejected by the threat scanner (Mini-ADR U-21). "
    "Phase enum: zip_import | skill_view.",
    label_names=("phase",),
)

_skill_drift_total = expert_work_counter(
    "expert_work_uplift_skill_drift_total",
    "Skill rows whose recomputed content_hash diverged from the stored "
    "value (Mini-ADR U-21 — DB row mutated past the strict scan).",
)

_skill_redacted_total = expert_work_counter(
    "expert_work_uplift_skill_redacted_total",
    "skill_view results replaced with a [BLOCKED] placeholder at read time "
    "because context-scope re-scan matched (Mini-ADR U-21).",
)

_skill_high_risk_event_total = expert_work_counter(
    "expert_work_uplift_skill_high_risk_event_total",
    "High-risk skill publish gate events (Mini-ADR U-24). "
    "Event enum: activation_blocked | activated.",
    label_names=("event",),
)

# Capability Uplift Sprint #4 — Curator state machine (Mini-ADR U-31).
_curator_transition_total = expert_work_counter(
    "expert_work_uplift_curator_transition_total",
    "Skill lifecycle transitions driven by the Curator worker or by an "
    "activity-triggered auto-revive (Mini-ADRs U-26 / U-29). "
    "from_state ∈ {active, stale}; to_state ∈ {active, stale, archived}.",
    label_names=("from_state", "to_state"),
)

_curator_pinned_skills = expert_work_gauge(
    "expert_work_uplift_curator_pinned_skills",
    "Total number of pinned skills across all tenants (Curator escape "
    "hatch — Mini-ADR U-25). Refreshed at the end of each Curator sweep.",
)

_skill_view_archived_blocked_total = expert_work_counter(
    "expert_work_uplift_skill_view_archived_blocked_total",
    "skill_view calls that hit an archived skill — cold path (Mini-ADR "
    "U-29). Expected to be near-zero in steady state; a non-trivial rate "
    "means an agent's manifest references an auto-archived skill that "
    "should be unarchived or replaced.",
)

# Capability Uplift Sprint #7 — MemoryConsolidator (Mini-ADR U-41).
_memory_cluster_candidates_total = expert_work_counter(
    "expert_work_uplift_memory_cluster_candidates_total",
    "MemoryConsolidator cluster candidates surfaced by the embedding "
    "pre-filter (Mini-ADR U-35). Used to track candidate volume vs. "
    "actual consolidations (a low conversion ratio means embedding "
    "threshold is too permissive, or LLM is over-rejecting).",
)

_memory_consolidated_total = expert_work_counter(
    "expert_work_uplift_memory_consolidated_total",
    "Consolidated long-term memory items written by the consolidator "
    "(LLM verified the cluster + summary written + sources linked).",
)

_memory_cluster_rejected_total = expert_work_counter(
    "expert_work_uplift_memory_cluster_rejected_total",
    "Clusters rejected by the consolidator LLM. Reason label "
    "distinguishes anti-mislearn categories (env_failure | "
    "negative_tool | transient_error | one_off_narrative | "
    "time_bound | credential_shape) from false_cluster (embedding "
    "thought related, LLM disagrees).",
    label_names=("reason",),
)

_memory_purged_total = expert_work_counter(
    "expert_work_uplift_memory_purged_total",
    "Lone-item transient memories soft-deleted by the consolidator "
    "noise-purge sub-pass (Mini-ADR U-37). Category label mirrors "
    "the cluster_rejected reason enum so the same recording rule "
    "groups apply across both paths.",
    label_names=("category",),
)

_memory_reviewed_durable_total = expert_work_counter(
    "expert_work_uplift_memory_reviewed_durable_total",
    "Lone-item transient memories the consolidator reviewed and "
    "decided to keep — last_reviewed_at marked so future ticks skip "
    "(Mini-ADR U-37 protection #3).",
)

# Stream P5b-2b ⑦ — MemoryConsolidator SUB-PASS 3 predictive review.
_memory_predictive_review_total = expert_work_counter(
    "expert_work_memory_predictive_review_total",
    "Predictive review verdicts by outcome — a fact whose predicted "
    "validity window (expected_valid_days) came due was re-reviewed by "
    "the aux LLM.",
    label_names=("outcome",),  # outcome ∈ renewed | expired | degraded
)

# B-300 redesign rev 9 §7.12 — a summary the user's later statement denied part of is
# rewritten (at the end of the write-back job, or by the consolidation sweep).
_memory_summary_rewrite_total = expert_work_counter(
    "expert_work_memory_summary_rewrite_total",
    "Rewrites of a summary memory flagged by a later user statement, by outcome.",
    label_names=("outcome",),
)

_consolidator_llm_tokens_total = expert_work_counter(
    "expert_work_uplift_consolidator_llm_tokens_total",
    "Aux model token consumption by the MemoryConsolidator. Cost "
    "telemetry is split from main-model accounting so the Sprint #7 "
    "spend can be attributed precisely (model + kind labels).",
    label_names=("model", "kind"),  # kind ∈ input | output
)

_consolidator_runs_total = expert_work_counter(
    "expert_work_uplift_consolidator_runs_total",
    "MemoryConsolidator worker sweeps that completed (1 per scheduled "
    "tick). The labeled outcome lets the alert distinguish 'sweep ran "
    "but had nothing to do' from 'sweep didn't run at all'.",
    label_names=("outcome",),  # outcome ∈ ok | error
)

# Stream O — credentials management (Mini-ADR O-8).
_credentials_resolve_total = expert_work_counter(
    "expert_work_uplift_credentials_resolve_total",
    "CredentialsResolver lookups, partitioned by mode, role, key, and "
    "result. ``role`` ∈ provider | tool; ``key`` is the provider name "
    "or tool name; ``result`` ∈ ok | missing_cred.",
    label_names=("mode", "role", "key", "result"),
)

_manifest_provider_rejected_total = expert_work_counter(
    "expert_work_uplift_manifest_provider_rejected_total",
    "Agent manifest publish attempts rejected because the referenced "
    "provider is not in the platform's supported_providers whitelist.",
    label_names=("provider",),
)

_manifest_stale_write_total = expert_work_counter(
    "expert_work_uplift_manifest_stale_write_total",
    "Agent-manifest updates refused by the If-Match optimistic-concurrency "
    "check. ``conflict`` is a genuine concurrent edit the caller would "
    "otherwise have silently overwritten; ``missing_header`` is a caller that "
    "did not send the precondition at all.",
    label_names=("outcome",),
)

_manifest_build_check_total = expert_work_counter(
    "expert_work_uplift_manifest_build_check_total",
    "Save-time dry-run builds of an agent manifest, by outcome. ``rejected`` "
    "is a manifest that passed schema validation but cannot be assembled into "
    "a runnable agent (missing platform credential, unresolvable skill / "
    "template, memory declared with no embedder, sub-agent cycle) — before "
    "this gate those saved fine and failed on every subsequent run instead. "
    "``platform_gap`` is a manifest the deployment cannot run yet (no credential "
    "for the provider, no embedder for declared long-term memory) — saved with a "
    "warning, because the author cannot fix platform state. ``skipped`` means no "
    "agent runtime was wired to check against.",
    label_names=("outcome",),
)

_legacy_credentials_fallback_total = expert_work_counter(
    "expert_work_uplift_legacy_credentials_fallback_total",
    "Stream O transition-period fallback: callers still reading the "
    "deprecated ``embedding_api_key_ref`` / ``rerank_api_key_ref`` env "
    "fields. Should drop to zero once ops migrate to "
    "platform_*_credentials. (``tavily_api_key_ref`` was removed in M2.)",
    label_names=("role",),
)

# 二期 PR2 T4 — built-agent cache bounds observability.
_built_agent_cache_entries = expert_work_gauge(
    "expert_work_built_agent_cache_entries",
    "Live entries in the in-process built-agent caches. "
    "``scope`` ∈ runtime (AgentRuntime top-level cache) | subagent "
    "(delegated child-builder closure cache). Refreshed after every "
    "put / invalidation / eviction / expiry.",
    label_names=("scope",),
)


def record_threat_scan(*, scope: str, result: str, variant: str = "original") -> None:
    """Bump ``expert_work_uplift_threat_scan_total``.

    ``variant`` defaults to ``"original"`` so Sprint #1 / Sprint #2
    callers that pre-date U-22 obfuscation pre-processing keep working
    without changes.
    """
    _scan_total.labels(scope=scope, result=result, variant=variant).inc()


def record_threat_pattern_hits(
    findings: Iterable[ThreatFinding], *, scope: str, variant: str = "original"
) -> None:
    """Bump ``expert_work_uplift_threat_pattern_hit_total`` once per finding.

    ``variant`` defaults to ``"original"`` for the same backward-compat
    reason as :func:`record_threat_scan`.
    """
    for f in findings:
        _pattern_hit_total.labels(pattern_id=f.pattern_id, scope=scope, variant=variant).inc()


def record_trigger_blocked(*, phase: str) -> None:
    """Bump ``expert_work_uplift_triggers_blocked_total{phase}``."""
    _triggers_blocked_total.labels(phase=phase).inc()


def record_memory_blocked(*, source: str) -> None:
    """Bump ``expert_work_uplift_memory_writes_blocked_total{source}``.

    ``source`` ∈ ``{"api", "writeback", "dlq"}`` (Sprint #2 § 3.7); B-300 D2
    adds ``"reconcile_update"`` (an UPDATE's new text) and ``"consolidation"``
    (a summary) — the same labels as the ``MEMORY_INJECTION_BLOCKED`` audit's
    ``source``. ``writeback`` covers the run-end write-back (in-run and
    background) and the pre-compaction flush.
    """
    _memory_blocked_total.labels(source=source).inc()


def record_memory_redacted() -> None:
    """Bump ``expert_work_uplift_memory_recalls_redacted_total`` once per redacted item."""
    _memory_redacted_total.inc()


def record_memory_drift() -> None:
    """Bump ``expert_work_uplift_memory_drift_total`` once per drift detection."""
    _memory_drift_total.inc()


def record_memory_retrieval(*, mode: str, result: str) -> None:
    """Bump ``expert_work_uplift_memory_retrieval_total{mode,result}``.

    ``mode`` ∈ ``{"hybrid", "vector"}`` (Sprint #6 § 7.7).
    ``result`` ∈ ``{"hit", "miss"}``.
    """
    _memory_retrieval_total.labels(mode=mode, result=result).inc()


def record_memory_inject_mode(*, mode: str) -> None:
    """Bump ``expert_work_uplift_memory_recall_inject_mode_total{mode}``.

    ``mode`` ∈ ``{"per_session", "per_turn"}`` (Sprint #8 § 9.8).
    """
    _memory_inject_mode_total.labels(mode=mode).inc()


def record_memory_rerank(*, outcome: str) -> None:
    """Bump ``expert_work_cm_memory_rerank_total{outcome}`` (Stream CM-4).

    ``outcome`` ∈ ``{"reranked", "degraded"}`` — ``reranked`` = the
    reranker reordered the recall candidates; ``degraded`` = rerank failed
    or returned nothing usable and the RRF order was kept. Only emitted
    when a reranker is wired (its absence is signalled by the metric's
    absence).
    """
    _memory_rerank_total.labels(outcome=outcome).inc()


def record_memory_mmr(*, outcome: str) -> None:
    """Bump ``expert_work_cm_memory_mmr_total{outcome}`` (Stream CM-6).

    ``outcome`` ∈ ``{"applied", "degraded"}`` — ``applied`` = MMR selected
    the final top-k from the recall candidates; ``degraded`` = the
    selection failed (or thinned to nothing) and the input order was kept.
    """
    _memory_mmr_total.labels(outcome=outcome).inc()


def record_memory_verify(*, outcome: str) -> None:
    """Bump ``expert_work_memory_verify_total{outcome}`` (Stream Memory-Enhance M-3).

    ``outcome`` ∈ ``{"verified", "degraded"}`` — ``verified`` = the read-time
    verifier ran and the kept set was applied (may have dropped some);
    ``degraded`` = verification failed and ALL candidates were kept (fail-open,
    memory recall never breaks the turn). Only emitted when verification is
    enabled and there were candidates to check.
    """
    _memory_verify_total.labels(outcome=outcome).inc()


def record_memory_abstain() -> None:
    """Bump ``expert_work_memory_abstain_total`` (P5a abstention gate)."""
    _memory_abstain_total.inc()


def record_memory_rewrite(*, outcome: str) -> None:
    """Bump ``expert_work_memory_rewrite_total{outcome}`` (P5a query rewrite).

    ``outcome`` ∈ ``{"rewritten", "unchanged", "degraded"}`` — ``rewritten`` =
    the rewriter returned a non-empty query different from the original;
    ``unchanged`` = it returned the original (empty reply or identical text);
    ``degraded`` = the rewrite call failed and the original was kept (fail-open,
    recall never breaks on a rewrite error). Only emitted when rewrite is
    enabled and a rewriter is wired.
    """
    _memory_rewrite_total.labels(outcome=outcome).inc()


def record_memory_reconcile(*, op: str) -> None:
    """Bump ``expert_work_cm_memory_reconcile_total{op}`` (Stream CM-7; B-300 redesign
    rev 9 §7.9 — the write and the retirements are decided separately, so the counter no
    longer has ``update`` / ``delete`` / ``blocked_*`` / ``lower_confidence_*`` values).

    Whether the candidate is written: ``add`` (stored); ``degraded`` (the model gave no
    usable decision for it: stored, nothing retired); ``noop`` (a duplicate, not stored);
    ``noop_refreshed`` (a duplicate that moved the user-side target's confirmed date);
    ``noop_same_evidence`` (a restatement of a memory taken from the same message: not
    stored, no refresh); ``noop_out_of_scope`` (the model's duplicate target is one the
    candidate could not be recalled through: stored); ``noop_other_event_date`` (the
    duplicate target has another event date — another occasion: stored);
    ``noop_weaker_target_written`` (the model judged it a duplicate of an assistant-side
    memory: stored, and that memory joins the ones to retire);
    ``noop_weaker_user_side_kept`` (the model judged it a duplicate of a user-side memory
    with a weaker source whose text is not the same: stored, that memory is kept — the
    model's "duplicate" includes "says less" — and the new row links to it);
    ``noop_already_superseded`` (it repeats a memory that a statement not older than it
    has just retired: not stored, so the overturned value is not brought back);
    ``written_as_history`` (stored as an already-superseded row linked to a later
    statement of the same conversation); ``collision_weaker_replaced`` (the same text was
    held by a live row with a weaker source: that row retired, the candidate stored);
    ``collision_cross_kind_not_written`` (a fact with the same text as a live event of
    this agent whose source is not weaker: not stored — other agents cannot recall it);
    ``legacy_op`` (the model still answered UPDATE / DELETE; read as ADD plus a list).

    Each older memory the candidate would retire: ``retired`` (done);
    ``retire_forced_weaker`` (it had the same text and a weaker source — retired by the
    program whatever the model said); ``event_date_one_sided_retire`` (retired where only
    the candidate has an event date); ``summary_rewrite_marked`` (a summary the model
    listed as no longer true was flagged for a rewrite instead); ``older_coexists`` (the
    candidate was said earlier than the memory: both stay, linked).

    Not retired — **never an error by itself**: the model wanted to, the program did not
    let it, and the candidate was stored all the same. ``retire_refused_cross_cell``
    (another kind or agent), ``retire_refused_rank`` (not among the 2 nearest by cosine
    in the cell), ``retire_refused_event_date`` (both have an event date and it differs,
    or it is the same and the candidate is no correction),
    ``retire_refused_undated_vs_dated`` (an undated non-correction against a dated
    memory), ``retire_refused_older_line`` (the candidate's line is older than the
    memory's evidence message, record time or last restatement),
    ``retire_refused_unknown_time`` (the candidate's line time is unknown),
    ``retire_refused_same_evidence`` (second-pass output against first-pass output of the
    same message), ``retire_refused_assistant`` (an assistant-side candidate against a
    user-side memory), ``retire_refused_corrected`` (the memory was corrected in the
    console), ``retire_refused_confirmed_vs_stated`` (a user-confirmed candidate against
    a user-stated memory — the default rule; the new row links to the old one),
    ``retire_refused_summary`` (a summary row), ``retire_target_gone`` (deleted, retired
    or folded meanwhile). ``retire_unknown_id`` = an id in the model's lists that is not
    one of this candidate's neighbours.

    ``reconcile_failed`` = one reconcile call failed inside (lookup, model, unusable
    reply, store error): nothing of that batch is written directly; the write-back raises
    to the job's retry, and on the last attempt the candidates not written yet go to the
    DLQ (redesign PR S, §7.6).
    """
    _memory_reconcile_total.labels(op=op).inc()


def _similarity_band(similarity: float) -> str:
    if similarity >= 0.8:
        return ">=0.8"
    if similarity >= 0.6:
        return "0.6-0.8"
    if similarity >= 0.4:
        return "0.4-0.6"
    return "<0.4"


def record_memory_reconcile_target(*, op: str, rank: int | None, similarity: float) -> None:
    """Bump ``expert_work_memory_reconcile_target_total{op, rank, similarity}`` (B-300
    redesign §7.1 row 6): ``op`` is ``noop`` for the memory the model said the candidate
    repeats, ``replaces`` / ``restates`` for one it listed; ``rank`` is the target's
    cosine rank among the candidate's
    same-cell neighbours (``"1"``, ``"2"``, ``"3+"``), ``"other_cell"`` when it is in
    another cell; ``similarity`` is a band (``"<0.4"`` … ``">=0.8"``)."""
    rank_label = "other_cell" if rank is None else (str(rank) if rank <= 2 else "3+")
    _memory_reconcile_target_total.labels(
        op=op, rank=rank_label, similarity=_similarity_band(similarity)
    ).inc()


def record_memory_extraction(*, pass_name: str, outcome: str) -> None:
    """Bump ``expert_work_memory_extraction_total{extraction_pass, outcome}`` (B-300 D3).

    ``outcome``: ``extracted`` (at least one memory), ``empty`` (a valid reply
    with nothing worth remembering), ``truncated`` (the model stopped at its
    output cap), ``parse_failed`` (no usable JSON), ``error`` (the call failed).
    """
    _memory_extraction_total.labels(extraction_pass=pass_name, outcome=outcome).inc()


def record_memory_extraction_check(*, pass_name: str, check: str) -> None:
    """Bump ``expert_work_memory_extraction_check_total{extraction_pass, check}`` (B-300
    redesign §5.4): ``downgraded_out_of_range`` / ``downgraded_not_user_line`` /
    ``downgraded_previous_not_assistant``, ``pointer_missing`` (``turn`` missing or out of
    range only; context-line cases are counted under ``context_line_*`` instead),
    ``event_date_dropped``,
    ``context_line_dropped`` (the memory's ``turn`` is a context line: dropped, both passes;
    replaces ``downgraded_context_line``, B-300 test acceptance 11-16). Second pass only,
    user 11-16 「这种情况照旧保存」「按用户确认重新核对」「同一批里也改指」: a
    ``user_answer`` / ``user_agreement`` whose ``turn`` is an ``[assistant]`` line directly
    followed by a new-segment ``[user]`` line that passes the usual check is re-anchored to
    that user line — stored as ``user_confirmed`` with that line's evidence pointer, in the
    new segment, time and event-date checks from that line; counted as
    ``turn_reanchored_context`` when the assistant line was the last context line, otherwise
    ``turn_reanchored_in_segment`` (the same output gives the same row however the write-back
    is sliced). Any other context-line item is dropped (``context_line_dropped``); any other
    in-segment assistant-line answer keeps ``downgraded_not_user_line``."""
    _memory_extraction_check_total.labels(extraction_pass=pass_name, check=check).inc()


def record_memory_writeback_segment(*, event: str, count: int = 1) -> None:
    """Add ``count`` to ``expert_work_memory_writeback_segment_total{event}`` (B-300 redesign
    PR S): ``empty`` (nothing new since the previous job — no model call),
    ``straddling_turn_skipped`` (a turn began before the start and ended after it; it belongs to
    the previous job), ``turns_older_than_window`` (with no earlier job, turns opened more than
    30 days ago or with no timestamp — left out), ``turns_over_cap_dropped`` (older whole turns
    dropped to keep the segment
    within the extraction cap; replaces the line-level ``input_capped`` counter), ``halved`` (a
    retry split the segment into two halves), ``precompaction_flush_skipped`` (the pre-compaction
    flush no longer extracts), ``already_done`` (another job at the same message count is done),
    ``config_error`` (a configuration-class error: finished without a retry),
    ``lease_expired_failed`` (a job whose last attempt died mid-way and whose lease ran out was
    settled ``failed`` by the next claim — its segment is abandoned)."""
    if count:
        _memory_writeback_segment_total.labels(event=event).inc(count)


def record_memory_extraction_message_truncated(*, pass_name: str, count: int) -> None:
    """Add ``count`` to
    ``expert_work_memory_extraction_message_truncated_total{extraction_pass}``."""
    if count:
        _memory_extraction_message_truncated_total.labels(extraction_pass=pass_name).inc(count)


def record_anthropic_cache_anchor() -> None:
    """Bump ``expert_work_uplift_anthropic_cache_anchors_total`` once per
    anchor marker applied on the outbound Anthropic request."""
    _anthropic_cache_anchors_total.inc()


def record_mcp_call(*, transport: str, server: str, result: str) -> None:
    """Bump ``expert_work_uplift_mcp_call_total{transport,server,result}``.

    ``transport`` ∈ ``{"stdio", "sse", "streamable_http"}``;
    ``result`` ∈ ``{"ok", "timeout", "4xx", "5xx", "circuit_open",
    "transport_err"}`` (Sprint #5 § 6.7).
    """
    _mcp_call_total.labels(transport=transport, server=server, result=result).inc()


def record_mcp_circuit_state(*, server: str, state: str) -> None:
    """Bump ``expert_work_uplift_mcp_circuit_state_total{server,state}`` once
    per state transition (Mini-ADR U-13). ``state`` ∈ ``{"closed",
    "half_open", "open"}``."""
    _mcp_circuit_state_total.labels(server=server, state=state).inc()


# Capability Uplift Sprint #3 — Skill subsystem.


def record_skill_view(*, result: str) -> None:
    """Bump ``expert_work_uplift_skill_view_total{result}``.

    ``result`` ∈ ``{"ok", "not_found", "truncated"}`` (Sprint #3 § 4.7).
    Drift / context-scope redaction emit through ``record_skill_drift`` /
    ``record_skill_redacted`` and do **not** double-count here.
    """
    _skill_view_total.labels(result=result).inc()


def record_skill_zip_reject(*, reason: str) -> None:
    """Bump ``expert_work_uplift_skill_zip_reject_total{reason}``.

    Reason must be one of the allowlisted enums (Sprint #3 § 4.7) —
    Oracle defense: we never use the user's path as a label value.
    """
    _skill_zip_reject_total.labels(reason=reason).inc()


def record_skill_blocked(*, phase: str) -> None:
    """Bump ``expert_work_uplift_skill_blocked_total{phase}`` (Mini-ADR U-21).

    ``phase`` ∈ ``{"zip_import", "skill_view", "supporting_file_api"}``.
    The third value extends the design's two-value enum to cover the
    single-file PUT/DELETE endpoints — same write-time strict-scan
    semantic as ``zip_import`` but a different actor / API surface so
    SecOps can split-out the block rate.
    """
    _skill_blocked_total.labels(phase=phase).inc()


def record_skill_drift() -> None:
    """Bump ``expert_work_uplift_skill_drift_total`` once per drift detection
    (Mini-ADR U-21)."""
    _skill_drift_total.inc()


def record_skill_redacted() -> None:
    """Bump ``expert_work_uplift_skill_redacted_total`` once per context-scope
    redaction in ``skill_view`` (Mini-ADR U-21)."""
    _skill_redacted_total.inc()


def record_skill_high_risk_event(*, event: str) -> None:
    """Bump ``expert_work_uplift_skill_high_risk_event_total{event}`` (Mini-ADR
    U-24). ``event`` ∈ ``{"activation_blocked", "activated"}``."""
    _skill_high_risk_event_total.labels(event=event).inc()


def record_curator_transition(*, from_state: str, to_state: str, count: int = 1) -> None:
    """Bump ``expert_work_uplift_curator_transition_total{from_state,to_state}``
    by ``count`` (Mini-ADR U-31). Used by the Curator worker for batch
    transitions and by :func:`SkillStore.bump_last_used_at` for the
    per-call ``stale → active`` auto-revive path."""
    if count <= 0:
        return
    _curator_transition_total.labels(from_state=from_state, to_state=to_state).inc(count)


def set_curator_pinned_skills(count: int) -> None:
    """Refresh the ``expert_work_uplift_curator_pinned_skills`` gauge — called
    at the end of each Curator sweep so the Admin dashboard tracks the
    operator-managed escape-hatch population (Mini-ADR U-31)."""
    _curator_pinned_skills.set(count)


def record_skill_view_archived_blocked() -> None:
    """Bump ``expert_work_uplift_skill_view_archived_blocked_total``. Fires
    when ``skill_view`` resolves to an archived skill — cold path
    (Mini-ADR U-29)."""
    _skill_view_archived_blocked_total.inc()


# Capability Uplift Sprint #7 — MemoryConsolidator recorders (Mini-ADR U-41).


def record_memory_cluster_candidates(count: int = 1) -> None:
    """Bump ``expert_work_uplift_memory_cluster_candidates_total`` by ``count``."""
    if count <= 0:
        return
    _memory_cluster_candidates_total.inc(count)


def record_memory_consolidated() -> None:
    """Bump ``expert_work_uplift_memory_consolidated_total`` once per
    consolidation commit (Mini-ADR U-34)."""
    _memory_consolidated_total.inc()


def record_memory_cluster_rejected(*, reason: str) -> None:
    """Bump ``expert_work_uplift_memory_cluster_rejected_total{reason}``.

    ``reason`` ∈ ``{"false_cluster", "env_failure", "negative_tool",
    "transient_error", "one_off_narrative", "time_bound",
    "credential_shape"}`` (Mini-ADR U-36).
    """
    _memory_cluster_rejected_total.labels(reason=reason).inc()


def record_memory_purged(*, category: str) -> None:
    """Bump ``expert_work_uplift_memory_purged_total{category}``. Category
    enum matches :func:`record_memory_cluster_rejected` reason enum
    minus ``false_cluster`` (single-item review has no false-cluster
    case)."""
    _memory_purged_total.labels(category=category).inc()


def record_memory_reviewed_durable() -> None:
    """Bump ``expert_work_uplift_memory_reviewed_durable_total`` once per
    lone-item review verdict = durable (Mini-ADR U-37 protection #3)."""
    _memory_reviewed_durable_total.inc()


def record_memory_predictive_review(*, outcome: str) -> None:
    """Bump ``expert_work_memory_predictive_review_total{outcome}`` (P5b-2b ⑦).

    ``outcome`` ∈ ``{"renewed", "expired", "degraded"}`` — ``renewed`` = the
    fact was judged still true and its validity window extended;
    ``expired`` = the fact was judged no longer true and retracted;
    ``degraded`` = the verdict call failed or was malformed and the fact
    was conservatively kept + re-armed (never silently drop a fact).
    """
    _memory_predictive_review_total.labels(outcome=outcome).inc()


def record_memory_summary_rewrite(*, outcome: str) -> None:
    """Bump ``expert_work_memory_summary_rewrite_total{outcome}`` (B-300 redesign rev 9
    §7.12): ``rewritten`` (a new summary replaced the old one), ``emptied`` (nothing of
    it remained true: retired, no new summary), ``unchanged`` (the model returned the
    same sentence — the reconcile model said it was denied, the rewrite found nothing to
    remove; worth watching), ``collided`` (the new sentence already exists as a live
    row), ``failed`` (the model call or its reply failed: the flag stays, no retry for 4
    hours), ``stale`` (the flag moved or the summary was replaced while rewriting:
    nothing stored), ``blocked`` (the new sentence was refused by the write scan: flag
    cleared), ``abandoned`` (flagged for 7 days without success: flag cleared),
    ``corrected_member`` (a member was corrected in the console meanwhile: summary kept,
    flag cleared), ``statement_gone`` (the user's statement the flag was set by was deleted
    and has no valid successor: nothing to rewrite by, flag cleared, no model call)."""
    _memory_summary_rewrite_total.labels(outcome=outcome).inc()


def record_consolidator_llm_tokens(*, model: str, input_tokens: int, output_tokens: int) -> None:
    """Bump ``expert_work_uplift_consolidator_llm_tokens_total{model,kind}``.

    Aux model spend is tracked separately from main-model accounting
    so the Sprint #7 cost is unambiguous in the dashboards.
    """
    if input_tokens > 0:
        _consolidator_llm_tokens_total.labels(model=model, kind="input").inc(input_tokens)
    if output_tokens > 0:
        _consolidator_llm_tokens_total.labels(model=model, kind="output").inc(output_tokens)


def record_consolidator_run(*, outcome: str) -> None:
    """Bump ``expert_work_uplift_consolidator_runs_total{outcome}``.

    ``outcome`` ∈ ``{"ok", "error"}``. Called once per scheduled tick;
    ``ok`` covers "ran cleanly, possibly with nothing to do"; ``error``
    covers an unhandled exception escaping the worker entry."""
    _consolidator_runs_total.labels(outcome=outcome).inc()


# Stream O — credentials recorders (Mini-ADR O-8).


def record_credentials_resolve(*, mode: str, role: str, key: str, result: str) -> None:
    """Bump ``expert_work_uplift_credentials_resolve_total``.

    ``mode`` ∈ ``{"platform", "tenant"}``;
    ``role`` ∈ ``{"provider", "tool"}``;
    ``key``  is the provider name or tool name;
    ``result`` ∈ ``{"ok", "missing_cred"}``."""
    _credentials_resolve_total.labels(mode=mode, role=role, key=key, result=result).inc()


def record_manifest_provider_rejected(*, provider: str) -> None:
    """Bump ``expert_work_uplift_manifest_provider_rejected_total{provider}``."""
    _manifest_provider_rejected_total.labels(provider=provider).inc()


def record_manifest_stale_write(*, outcome: str) -> None:
    """Bump ``expert_work_uplift_manifest_stale_write_total{outcome}``.

    ``outcome`` ∈ ``{"missing_header", "conflict"}``."""
    _manifest_stale_write_total.labels(outcome=outcome).inc()


def record_manifest_build_check(*, outcome: str) -> None:
    """Bump ``expert_work_uplift_manifest_build_check_total{outcome}``.

    ``outcome`` ∈ ``{"ok", "rejected", "platform_gap", "skipped"}``."""
    _manifest_build_check_total.labels(outcome=outcome).inc()


def record_legacy_credentials_fallback(*, role: str) -> None:
    """Bump ``expert_work_uplift_legacy_credentials_fallback_total{role}``.

    ``role`` ∈ ``{"embedding", "rerank"}``."""
    _legacy_credentials_fallback_total.labels(role=role).inc()


def set_built_agent_cache_entries(*, scope: str, count: int) -> None:
    """Set ``expert_work_built_agent_cache_entries{scope}`` (二期 PR2 T4).

    ``scope`` ∈ ``{"runtime", "subagent"}``; ``count`` is the cache's
    current ``len``. Callers set the absolute size after every mutation
    (put / invalidation / LRU eviction / TTL expiry)."""
    _built_agent_cache_entries.labels(scope=scope).set(count)


__all__ = [
    "record_anthropic_cache_anchor",
    "record_consolidator_llm_tokens",
    "record_consolidator_run",
    "record_credentials_resolve",
    "record_curator_transition",
    "record_legacy_credentials_fallback",
    "record_manifest_build_check",
    "record_manifest_provider_rejected",
    "record_manifest_stale_write",
    "record_mcp_call",
    "record_mcp_circuit_state",
    "record_memory_blocked",
    "record_memory_cluster_candidates",
    "record_memory_cluster_rejected",
    "record_memory_consolidated",
    "record_memory_drift",
    "record_memory_extraction",
    "record_memory_extraction_check",
    "record_memory_extraction_message_truncated",
    "record_memory_inject_mode",
    "record_memory_predictive_review",
    "record_memory_purged",
    "record_memory_redacted",
    "record_memory_retrieval",
    "record_memory_reviewed_durable",
    "record_memory_summary_rewrite",
    "record_memory_writeback_segment",
    "record_skill_blocked",
    "record_skill_drift",
    "record_skill_high_risk_event",
    "record_skill_redacted",
    "record_skill_view",
    "record_skill_view_archived_blocked",
    "record_skill_zip_reject",
    "record_threat_pattern_hits",
    "record_threat_scan",
    "record_trigger_blocked",
    "set_built_agent_cache_entries",
    "set_curator_pinned_skills",
]
