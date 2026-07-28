"""``MemoryConsolidator`` — Capability Uplift Sprint #7.

Background worker that promotes raw ``transient`` long-term memory items
into ``consolidated`` summaries via a two-pass periodic sweep:

* **SUB-PASS 1 (cluster → consolidate)** — embedding pre-filter finds
  candidate clusters of similar transient items; one LLM call per
  cluster verifies + summarises + applies the Hermes 4 + expert_work 2
  anti-mislearn rules in a single three-in-one prompt (Mini-ADR U-35).

* **SUB-PASS 2 (lone-item noise purge)** — sweeps aged transient items
  that have never been retrieved (``access_count == 0``) and have
  never been reviewed (``last_reviewed_at IS NULL``); a single
  LLM call classifies each as ``durable`` or one of the noise
  categories; noise rows are soft-deleted, durable rows are stamped
  ``last_reviewed_at`` to skip re-review (Mini-ADR U-37).

Cadence: one sweep per ``interval_s`` (default 14400 = 4 h). Each sweep
is idempotent — re-running is safe because (a) consolidated items have
``consolidated_from`` set so candidates filter them out, and (b) reviewed
items have ``last_reviewed_at`` set so purge candidates filter them out.

Audit posture: per-consolidation / per-rejection / per-purge /
per-review-durable rows + one ``MEMORY_CONSOLIDATOR_RUN`` summary per
sweep. Per-row audits are bounded by tenant_config thresholds
(``memory_purge_max_per_run`` not exposed for Sprint #7 — capped here
at 100 per (tenant, user) per tick).

See ``docs/streams/STREAM-UPLIFT-DESIGN.md`` § 8 (Mini-ADRs U-33~U-42).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.audit import emit as audit_emit
from control_plane.tenancy import TenantConfigNotConfiguredError, TenantConfigService
from expert_work.common.observability import current_trace_id_hex
from expert_work.common.uplift_metrics import (
    record_consolidator_llm_tokens,
    record_consolidator_run,
    record_memory_cluster_candidates,
    record_memory_cluster_rejected,
    record_memory_consolidated,
    record_memory_predictive_review,
    record_memory_purged,
    record_memory_reviewed_durable,
)
from expert_work.persistence.token_usage_store import TokenUsageRecord, TokenUsageStore
from expert_work.protocol import AuditAction, AuditResult, MemoryItem, StructuredOutputSpec
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.middleware import LLMOutputValidationError

if TYPE_CHECKING:
    from expert_work.persistence.memory.base import MemoryStore

logger = logging.getLogger("expert_work.control_plane.memory_consolidator")

# Default cadence — one sweep per 4 hours. Configurable via constructor
# so tests drive a fast loop and operators dial it.
_DEFAULT_INTERVAL_S: float = 14_400.0

#: Advisory-lock classid for the single-flight sweep — makes concurrent
#: replicas race for one winner per cycle instead of each spending LLM
#: calls on the same work. Mirrors ``QualityDriftWorker._DRIFT_LOCK_CLASSID``
#: (quality_drift_worker.py); a distinct value so the two never share a key
#: (workspace_lock.py uses 1, mcp_oauth_refresh_lock.py uses 2, the drift
#: worker uses 8615).
_CONSOLIDATOR_LOCK_CLASSID = 8616
#: The lock txn is held open for the whole sweep; keep it off any idle
#: reaper.
_LOCK_TXN_TIMEOUT_MS = 5 * 60 * 1000

# Per-(tenant, user) safety caps. Hard-coded for Sprint #7 — Mini-ADR
# U-34. Prevents a runaway worker from emitting thousands of LLM calls
# in one tick because a single user happened to accumulate a large
# transient backlog. M1 dogfood may surface a tenant-config knob.
_MAX_USERS_PER_TICK: int = 50
_MAX_CLUSTER_CANDIDATES_PER_USER: int = 10
_MAX_PURGE_PER_USER: int = 100
_MAX_TRANSIENT_SCAN_PER_USER: int = 500

# Transient scan window — only look at items written in the last 30 d
# for cluster-pass seeds. Older items either already consolidated, got
# purged by SUB-PASS 2, or are kept as long-tail (M2-C archive job will
# eventually claim them).
_TRANSIENT_SCAN_AGE_DAYS: int = 30

# Default anti-mislearn categories (Mini-ADR U-36 — Hermes 4 + expert_work 2).
_REJECT_CATEGORIES: tuple[str, ...] = (
    "env_failure",
    "negative_tool",
    "transient_error",
    "one_off_narrative",
    "time_bound",
    "credential_shape",
)


# ---------------------------------------------------------------------------
# Protocols — kept minimal so any LLM / embedder implementation can plug in
# (orchestrator's LLMRouter + OpenAICompatibleEmbedder are the production
#  adapters; tests inject deterministic fakes).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsolidatorLLMReply:
    """One LLM response from the consolidator aux model.

    ``parsed`` (Stream RT-1) carries the schema-validated dict when the
    call was made with ``output_schema`` and the adapter's underlying
    router enforced it (``additional_kwargs["parsed"]``); ``None`` for
    plain-text replies or adapters without structured support.
    """

    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    parsed: dict[str, Any] | None = None


class ConsolidatorAuxModel(Protocol):
    """Single-shot aux-model interface for the consolidator.

    Implementations wrap whatever underlying client / router the
    deployment uses (Anthropic, OpenAI, internal vLLM, etc). The worker
    only needs a synchronous request-response shape; tool-calling is
    out-of-scope (anti-mislearn / summarisation are pure-text outputs).

    ``tenant_id`` is passed so production adapters can route the call
    through :class:`CredentialsResolver` (Stream O) and pick the right
    credential per-tenant. The null adapter ignores it.

    ``output_schema`` (Stream RT-1) asks for a schema-enforced JSON
    reply; router-backed adapters thread it to
    ``LLMRouter(output_schema=...)`` and surface the validated dict on
    :attr:`ConsolidatorLLMReply.parsed`. Implementations without
    structured support may ignore it (callers fall back to text).
    """

    async def __call__(
        self,
        *,
        prompt: str,
        model: str | None,
        tenant_id: UUID,
        output_schema: StructuredOutputSpec | None = None,
    ) -> ConsolidatorLLMReply:
        """Send ``prompt`` to the aux model and return its reply."""


class ConsolidatorEmbedder(Protocol):
    """Single-text embedder for the consolidator's summary writes."""

    async def embed_one(self, text: str, *, tenant_id: UUID) -> tuple[float, ...]:
        """Return the embedding vector for ``text`` (Stream O O-9 —
        ``tenant_id`` resolves the per-tenant embedding credential)."""


class _OrchestratorEmbedderAdapter:
    """Wraps an orchestrator-style ``Embedder`` (batch) as a
    :class:`ConsolidatorEmbedder` (single-text)."""

    def __init__(self, embedder: object) -> None:
        self._embedder = embedder

    async def embed_one(self, text: str, *, tenant_id: UUID) -> tuple[float, ...]:
        # Embedder.embed returns ``list[tuple[float, ...]]``; we always
        # send a single text and unwrap.
        result = await self._embedder.embed([text], tenant_id=tenant_id)  # type: ignore[attr-defined]
        if not result:
            msg = "embedder returned empty result for single text"
            raise RuntimeError(msg)
        first = result[0]
        return tuple(float(v) for v in first)


class _NullConsolidatorAuxModel:
    """Default aux-model adapter that returns ``keep=false,
    reject_reason="false_cluster"`` for every prompt.

    Sprint #7 ships the worker infrastructure (schema + sweep + audit +
    metrics) without committing the platform to a specific LLM client
    wire-up. Stream O PR B replaces this with
    :class:`~control_plane.credentials_aux_adapter.LLMRouterAuxModelAdapter`
    in production; this null implementation is preserved as the
    explicit "no LLM configured" fallback for tests and as a graceful
    degrade if credentials are unavailable."""

    async def __call__(
        self,
        *,
        prompt: str,
        model: str | None,
        tenant_id: UUID,
        output_schema: StructuredOutputSpec | None = None,
    ) -> ConsolidatorLLMReply:
        # Single-item review uses a different shape; we return a "keep"
        # verdict for that path so reviewed_durable counts increment
        # rather than silently dropping the row. Both shapes are
        # valid for their respective parsers. Structured calls (RT-1)
        # additionally get the dict on ``parsed``, mirroring the router
        # contract.
        payload: dict[str, Any]
        if '"is_noise"' in prompt:
            payload = {"is_noise": False, "category": "durable"}
        else:
            payload = {"keep": False, "summary": None, "reject_reason": "false_cluster"}
        return ConsolidatorLLMReply(
            text=json.dumps(payload),
            model=model or "null",
            parsed=payload if output_schema is not None else None,
        )


# ---------------------------------------------------------------------------
# Prompts (Mini-ADR U-36)
# ---------------------------------------------------------------------------


_ANTI_MISLEARN_RULES = """REJECT consolidation if the cluster (or single item) \
represents any of:

1. Environment-dependent failure
   missing binary / fresh-install error / unconfigured credential /
   post-migration mismatch / "command not found" / uninstalled package.
   These are transient to the current environment, not durable user facts.

2. Negative claim about a tool
   "browser tools do not work" / "X tool is broken" / "cannot use Y" -
   may be a one-time misuse; do not harden into a refusal pattern.

3. Session-specific transient error that resolved
   if retry / different approach worked, the lesson is the recovery
   pattern, not the original failure.

4. One-off task narrative
   "user asked me to refactor X" / "fixed bug in Y" - task-scoped, not
   user-scoped; expires when the task ends.

5. (expert_work extension) Time-bound state
   current model availability / current quota / today's date /
   "the API returned 503 today" - bound to wall-clock time, not durable.

6. (expert_work extension) Credential-shaped content
   anything looking like a token / key / password / connection string -
   never long-term, always purge to audit.
"""


def _build_cluster_prompt(items: Sequence[MemoryItem]) -> str:
    """Mini-ADR U-35 + U-36 three-in-one prompt: verify cluster + summarise
    + apply anti-mislearn rules in a single LLM call."""
    items_rendered = "\n".join(f"- ({item.id}) {item.content}" for item in items)
    valid_reasons = ["false_cluster"] + [f"anti_mislearn:{c}" for c in _REJECT_CATEGORIES]
    valid_reasons_json = json.dumps(valid_reasons)
    return f"""You are a memory consolidator. You receive a candidate cluster of \
{len(items)} memory items that an embedding-similarity prefilter \
identified as likely-related.

{_ANTI_MISLEARN_RULES}
Otherwise, write ONE summary fact (under 200 chars) that captures the
durable user truth this cluster represents. Prefer the user's own
phrasing over paraphrase.

Cluster items:
{items_rendered}

Respond ONLY with JSON of the exact shape:
{{
  "keep": true | false,
  "summary": "<= 200 chars OR null when keep=false>",
  "reject_reason": <one of {valid_reasons_json} OR null when keep=true>
}}
"""


def _build_single_review_prompt(item: MemoryItem) -> str:
    """Mini-ADR U-37 — lone-item review. Same 6 categories as the
    cluster prompt, single-item verdict shape."""
    valid_categories = ["durable", *_REJECT_CATEGORIES]
    valid_categories_json = json.dumps(valid_categories)
    return f"""You are reviewing one long-term memory item for noise.

{_ANTI_MISLEARN_RULES}
Item content:
{item.content}

Respond ONLY with JSON of the exact shape:
{{
  "is_noise": true | false,
  "category": <one of {valid_categories_json}>
}}

"category": "durable" means keep; any reject category means soft-delete.
"""


def _build_due_review_prompt(item: MemoryItem) -> str:
    """P5b-2b ⑦ — predictive re-review of a fact whose validity window came
    due. Verdict is still-true vs no-longer-true (NOT noise classification)."""
    return f"""You are re-checking whether one long-term memory is still true today.

Item content:
{item.content}

Decide whether this is still true now. If it is, optionally estimate how many
more days until it is worth re-checking. If it is no longer true (the world
changed, it lapsed, it was time-bound and has passed), mark it not valid.

Respond ONLY with JSON of the exact shape:
{{
  "still_valid": true | false,
  "expected_valid_days": <positive integer OR null>
}}
"""


# ---------------------------------------------------------------------------
# Reply parsing — Stream RT-1 PR-2: pydantic wire models + output_schema.
# The *shape* is enforced by the models (and, through the specs below, by
# the router's validation loop before the reply ever gets here); the
# *semantic* rules (keep=true needs a non-empty summary, the reject_reason
# / category vocabularies) stay in the parse functions. The Sprint #7
# semantic rules and failure direction are preserved; shape validation is
# tighter and every divergence degrades conservatively (RT-ADR-3), and the
# router's enforce+retry loop makes those divergences near-unreachable.
# ---------------------------------------------------------------------------


class _ClusterReplyModel(BaseModel):
    """Wire shape demanded by :func:`_build_cluster_prompt`.

    ``summary`` / ``reject_reason`` default to ``None`` like the pre-RT-1
    ``data.get(...)`` — only the ``keep`` decision is required. A missing
    descriptive field flows into the same semantic checks as before
    instead of voiding a verdict the old parser accepted.
    """

    model_config = ConfigDict(extra="forbid")

    keep: bool
    summary: str | None = None
    reject_reason: str | None = None


class _SingleReviewReplyModel(BaseModel):
    """Wire shape demanded by :func:`_build_single_review_prompt`."""

    model_config = ConfigDict(extra="forbid")

    is_noise: bool
    category: str


class _DueReviewReplyModel(BaseModel):
    """Wire shape demanded by :func:`_build_due_review_prompt`."""

    model_config = ConfigDict(extra="forbid")

    still_valid: bool
    expected_valid_days: int | None = None


_CLUSTER_OUTPUT_SPEC = StructuredOutputSpec(
    schema=_ClusterReplyModel.model_json_schema(), name="memory_cluster_verdict"
)
_SINGLE_REVIEW_OUTPUT_SPEC = StructuredOutputSpec(
    schema=_SingleReviewReplyModel.model_json_schema(), name="memory_review_verdict"
)
_DUE_REVIEW_OUTPUT_SPEC = StructuredOutputSpec(
    schema=_DueReviewReplyModel.model_json_schema(), name="memory_due_review_verdict"
)


@dataclass(frozen=True)
class ClusterVerdict:
    """Parsed cluster-prompt response."""

    keep: bool
    summary: str | None
    reject_reason: str | None  # "false_cluster" | "anti_mislearn:<category>" | None


@dataclass(frozen=True)
class SingleReviewVerdict:
    """Parsed lone-item review response."""

    is_noise: bool
    category: str  # "durable" | one of _REJECT_CATEGORIES


@dataclass(frozen=True)
class _DueVerdict:
    """Parsed due-review response (P5b-2b ⑦)."""

    still_valid: bool
    expected_valid_days: int | None


def _parse_cluster_reply(reply: ConsolidatorLLMReply) -> ClusterVerdict | None:
    """Validate the cluster LLM reply; return ``None`` on malformed input.

    Prefers the router-validated ``parsed`` dict (RT-1 structured
    output); plain-text replies validate through the same model. A
    ``None`` return means "treat as false_cluster, skip silently"
    (defensive — a malformed LLM reply should not crash the worker).
    """
    try:
        if reply.parsed is not None:
            data = _ClusterReplyModel.model_validate(reply.parsed)
        else:
            data = _ClusterReplyModel.model_validate_json(reply.text)
    except ValidationError:
        return None
    if data.keep:
        if data.summary is None or not data.summary.strip():
            return None
        return ClusterVerdict(keep=True, summary=data.summary.strip(), reject_reason=None)
    if data.reject_reason is None:
        return None
    if data.reject_reason != "false_cluster" and not data.reject_reason.startswith(
        "anti_mislearn:"
    ):
        return None
    return ClusterVerdict(keep=False, summary=None, reject_reason=data.reject_reason)


def _parse_single_reply(reply: ConsolidatorLLMReply) -> SingleReviewVerdict | None:
    """Validate the lone-item review LLM reply; ``None`` on malformed."""
    try:
        if reply.parsed is not None:
            data = _SingleReviewReplyModel.model_validate(reply.parsed)
        else:
            data = _SingleReviewReplyModel.model_validate_json(reply.text)
    except ValidationError:
        return None
    if data.category not in (("durable", *_REJECT_CATEGORIES)):
        return None
    return SingleReviewVerdict(is_noise=data.is_noise, category=data.category)


def _parse_due_reply(reply: ConsolidatorLLMReply) -> _DueVerdict | None:
    """Validate the due-review LLM reply; ``None`` on malformed.

    ``expected_valid_days`` is normalized to None-or-positive here — a
    non-positive value (<=0) degrades to None rather than passing through.
    Mirrors ``orchestrator.graph_builder.memory._parse_valid_days``'s
    tolerance philosophy for the same untrusted-LLM-output shape: a bad
    window never rejects the verdict, it just opts out of a renewed
    window. Without this, a negative window would flow into
    ``_review_due_fact``'s ``verdict.expected_valid_days or
    item.expected_valid_days`` (truthy) and then into ``renew_review``,
    producing a ``due_at`` in the past — the fact would come due again
    on every consolidator tick forever, and — because renewal re-arms
    from the (now negative) stored window on subsequent null/degrade
    verdicts — never self-heal.
    """
    try:
        if reply.parsed is not None:
            data = _DueReviewReplyModel.model_validate(reply.parsed)
        else:
            data = _DueReviewReplyModel.model_validate_json(reply.text)
    except ValidationError:
        return None
    expected_valid_days = data.expected_valid_days
    if expected_valid_days is not None and expected_valid_days <= 0:
        expected_valid_days = None
    return _DueVerdict(still_valid=data.still_valid, expected_valid_days=expected_valid_days)


# ---------------------------------------------------------------------------
# Summary types
# ---------------------------------------------------------------------------


@dataclass
class ConsolidatorRunSummary:
    """One full sweep's aggregate counters (returned + audited)."""

    tenant_count: int = 0
    user_count: int = 0
    cluster_candidates: int = 0
    consolidated: int = 0
    cluster_rejected: int = 0
    purged: int = 0
    reviewed_durable: int = 0
    expired: int = 0
    renewed: int = 0
    errors: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    def as_audit_details(self) -> dict[str, object]:
        return {
            "tenant_count": self.tenant_count,
            "user_count": self.user_count,
            "cluster_candidates": self.cluster_candidates,
            "consolidated": self.consolidated,
            "cluster_rejected": self.cluster_rejected,
            "purged": self.purged,
            "reviewed_durable": self.reviewed_durable,
            "expired": self.expired,
            "renewed": self.renewed,
            "errors": self.errors,
            "started_at": self.started_at.isoformat(),
            "finished_at": (self.finished_at.isoformat() if self.finished_at else None),
        }


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class MemoryConsolidator:
    """Periodic short→long memory consolidation worker.

    Wired in :func:`control_plane.app.create_app` and started by the
    lifespan handler. Single replica per cluster (the sweep is
    idempotent so duplicates are safe, but they are wasted LLM calls).
    """

    def __init__(
        self,
        *,
        memory_store: MemoryStore,
        tenant_config_service: TenantConfigService,
        audit_logger: AuditLogger,
        aux_model: ConsolidatorAuxModel,
        embedder: ConsolidatorEmbedder,
        interval_s: float = _DEFAULT_INTERVAL_S,
        default_aux_model_name: str = "claude-sonnet-4-6",
        actor_id: str = "memory_consolidator",
        usage_store: TokenUsageStore | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        if interval_s <= 0:
            msg = "interval_s must be positive"
            raise ValueError(msg)
        self._memory = memory_store
        self._tenant_config = tenant_config_service
        self._audit = audit_logger
        self._aux = aux_model
        self._embedder = embedder
        # Chargeback (same gap as SE-A43's aux metering, other half): every
        # consolidator aux call writes a token_usage row so per-tenant cost
        # views include memory-consolidation spend. ``None`` records nothing.
        self._usage_store = usage_store
        self._interval_s = interval_s
        self._default_model = default_aux_model_name
        self._actor_id = actor_id
        # Advisory-lock session factory (multi-replica single-flight, same
        # shape as ``QualityDriftWorker``). ``None`` (in-memory / single
        # process) skips the lock — matches quality_drift's degrade.
        self._session_factory = session_factory
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the periodic loop. Idempotent."""
        if self.is_running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="memory-consolidator")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=min(self._interval_s, 30.0) + 5.0)
        except (TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        finally:
            self._task = None

    async def _loop(self) -> None:
        # Like SkillCurator: don't run an immediate sweep on startup —
        # platform likely restarted recently, sweep would compete with
        # replays. Sleep first; first sweep after ``interval_s``.
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
                return  # stop event fired
            except TimeoutError:
                pass
            try:
                await self.run_once()
                record_consolidator_run(outcome="ok")
            except Exception:
                logger.exception("memory_consolidator.cycle_failed")
                record_consolidator_run(outcome="error")

    async def run_once(self) -> ConsolidatorRunSummary:
        """Run one sweep, single-flight across replicas.

        Each per-user write inside the sweep is individually idempotent, so
        two replicas racing the same sweep is safe — just wasted LLM calls
        (the class docstring's "single replica per cluster" note). A
        ``pg_try_advisory_xact_lock`` makes the whole sweep single-flight
        (mirrors ``QualityDriftWorker.run_once``); a replica that misses the
        lock returns an empty summary immediately, no sweep attempted.
        Single-process / in-memory runs have no factory and need no lock.
        """
        if self._session_factory is None:
            return await self._run_sweep()
        async with self._session_factory() as lock_session:
            # Long-hold guard: the lock txn stays open for the sweep; keep it
            # off any idle-in-transaction reaper (same posture as
            # PgWorkspaceLock / QualityDriftWorker).
            await lock_session.execute(
                text(f"SET LOCAL idle_in_transaction_session_timeout = {_LOCK_TXN_TIMEOUT_MS}")
            )
            got = (
                await lock_session.execute(
                    text("SELECT pg_try_advisory_xact_lock(:cid, hashtext(:k))"),
                    {"cid": _CONSOLIDATOR_LOCK_CLASSID, "k": "memory_consolidator"},
                )
            ).scalar_one()
            if not got:
                await lock_session.rollback()
                return ConsolidatorRunSummary()
            try:
                return await self._run_sweep()
            finally:
                # rollback ends the txn → releases the xact advisory lock.
                await lock_session.rollback()

    async def _run_sweep(self) -> ConsolidatorRunSummary:
        """One full sweep across all tenants. Idempotent.

        Returns the summary so tests can assert on transition counts.
        """
        summary = ConsolidatorRunSummary()
        tenant_ids = await self._list_tenants()
        for tenant_id in tenant_ids:
            try:
                cfg = await self._resolve_thresholds(tenant_id)
            except Exception:
                logger.exception("memory_consolidator.tenant_config_failed tenant_id=%s", tenant_id)
                summary.errors += 1
                continue
            summary.tenant_count += 1
            users = (await self._memory.distinct_users(tenant_id=tenant_id))[:_MAX_USERS_PER_TICK]
            for user_id in users:
                summary.user_count += 1
                try:
                    await self._sweep_user(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        cfg=cfg,
                        summary=summary,
                    )
                except Exception:
                    logger.exception(
                        "memory_consolidator.user_sweep_failed tenant_id=%s user_id=%s",
                        tenant_id,
                        user_id,
                    )
                    summary.errors += 1

        summary.finished_at = datetime.now(UTC)

        # One audit row per sweep — bounded volume.
        try:
            await audit_emit(
                self._audit,
                tenant_id=_PLATFORM_TENANT_ID,
                actor_id=self._actor_id,
                action=AuditAction.MEMORY_CONSOLIDATOR_RUN,
                resource_type="memory_item",
                resource_id=None,
                result=AuditResult.SUCCESS,
                trace_id=current_trace_id_hex(),
                details=summary.as_audit_details(),
            )
        except Exception:
            logger.exception("memory_consolidator.audit_emit_failed")

        logger.info(
            "memory_consolidator.sweep_complete tenants=%d users=%d "
            "candidates=%d consolidated=%d rejected=%d purged=%d "
            "reviewed_durable=%d expired=%d renewed=%d errors=%d",
            summary.tenant_count,
            summary.user_count,
            summary.cluster_candidates,
            summary.consolidated,
            summary.cluster_rejected,
            summary.purged,
            summary.reviewed_durable,
            summary.expired,
            summary.renewed,
            summary.errors,
        )
        return summary

    # ------------------------------------------------------------------
    # Per-user sweep — SUB-PASS 1 + SUB-PASS 2
    # ------------------------------------------------------------------

    async def _sweep_user(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        cfg: _ResolvedThresholds,
        summary: ConsolidatorRunSummary,
    ) -> None:
        # SUB-PASS 1: cluster → consolidate
        clusters = await self._find_candidate_clusters(
            tenant_id=tenant_id,
            user_id=user_id,
            min_cluster_size=cfg.min_cluster_size,
            similarity=cfg.similarity,
        )
        if clusters:
            record_memory_cluster_candidates(len(clusters))
            summary.cluster_candidates += len(clusters)
        for cluster in clusters:
            try:
                await self._consolidate_or_reject(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    cluster=cluster,
                    summary=summary,
                )
            except Exception:
                logger.exception(
                    "memory_consolidator.cluster_failed tenant=%s user=%s size=%d",
                    tenant_id,
                    user_id,
                    len(cluster),
                )
                summary.errors += 1

        # SUB-PASS 2a (Stream HX-2, Mini-ADR HX-B3): user-👎-flagged items
        # go through the same U-37 single-item review, regardless of age /
        # retrieval history and regardless of ``purge_enabled`` — a user
        # explicitly disputing a memory is not background noise sweeping,
        # so the tenant's purge opt-out does not silence it. ``mark_reviewed``
        # (durable verdict) clears the flag; soft-delete (noise) removes the
        # row outright.
        flagged = await self._memory.list_review_flagged(
            tenant_id=tenant_id,
            user_id=user_id,
            limit=_MAX_PURGE_PER_USER,
        )
        for item in flagged:
            try:
                await self._review_lone_item(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    item=item,
                    summary=summary,
                )
            except Exception:
                logger.exception(
                    "memory_consolidator.flagged_review_failed tenant=%s user=%s id=%s",
                    tenant_id,
                    user_id,
                    item.id,
                )
                summary.errors += 1

        # SUB-PASS 2: lone-item noise purge
        if cfg.purge_enabled:
            candidates = await self._memory.list_purge_candidates(
                tenant_id=tenant_id,
                user_id=user_id,
                min_age_days=cfg.purge_min_age_days,
                limit=_MAX_PURGE_PER_USER,
            )
            for item in candidates:
                try:
                    await self._review_lone_item(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        item=item,
                        summary=summary,
                    )
                except Exception:
                    logger.exception(
                        "memory_consolidator.lone_review_failed tenant=%s user=%s id=%s",
                        tenant_id,
                        user_id,
                        item.id,
                    )
                    summary.errors += 1

        # SUB-PASS 3 (P5b-2b ⑦): predictive review of facts whose validity
        # window came due. Opt-in per tenant (LLM cost per due fact); reuses
        # the lone-item review skeleton with a still-true/false verdict.
        if cfg.predictive_review_enabled:
            due = await self._memory.list_due_for_review(
                tenant_id=tenant_id,
                user_id=user_id,
                limit=_MAX_PURGE_PER_USER,
            )
            for item in due:
                try:
                    await self._review_due_fact(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        item=item,
                        summary=summary,
                    )
                except Exception:
                    logger.exception(
                        "memory_consolidator.due_review_failed tenant=%s user=%s id=%s",
                        tenant_id,
                        user_id,
                        item.id,
                    )
                    summary.errors += 1

    async def _find_candidate_clusters(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        min_cluster_size: int,
        similarity: float,
    ) -> list[list[MemoryItem]]:
        """Embedding seed-walk — for each unseen transient item, ask the
        store for cosine neighbors. Mini-ADR U-35 candidate algorithm.
        """
        transients = await self._memory.list_transient(
            tenant_id=tenant_id,
            user_id=user_id,
            max_age_days=_TRANSIENT_SCAN_AGE_DAYS,
            limit=_MAX_TRANSIENT_SCAN_PER_USER,
        )
        if len(transients) < min_cluster_size:
            return []
        cosine_max = max(0.0, 1.0 - similarity)
        seen: set[UUID] = set()
        clusters: list[list[MemoryItem]] = []
        for item in transients:
            if item.id in seen:
                continue
            if len(clusters) >= _MAX_CLUSTER_CANDIDATES_PER_USER:
                break
            neighbors = await self._memory.vector_neighbors(
                tenant_id=tenant_id,
                user_id=user_id,
                embedding=item.embedding,
                cosine_max=cosine_max,
                limit=20,
            )
            if len(neighbors) < min_cluster_size:
                continue
            # Filter out any items we already absorbed into an earlier
            # cluster — disjoint result set.
            fresh = [n for n in neighbors if n.id not in seen]
            if len(fresh) < min_cluster_size:
                continue
            clusters.append(fresh)
            seen.update(n.id for n in fresh)
        return clusters

    async def _record_aux_usage(
        self, reply: ConsolidatorLLMReply, *, tenant_id: UUID, user_id: UUID
    ) -> None:
        """One ``token_usage`` row per consolidator aux call (never-fail).

        ``usage_kind='memory_consolidation'`` keeps this spend out of every
        agent's conversation cost while chargeback still sees it. The
        consolidator serves the tenant, not one agent — the fixed
        ``agent_name`` makes the rows self-describing in per-kind views.
        """
        if self._usage_store is None:
            return
        try:
            await self._usage_store.insert(
                TokenUsageRecord(
                    tenant_id=tenant_id,
                    agent_name="memory-consolidator",
                    agent_version="-",
                    model=reply.model or self._default_model,
                    user_id=user_id,
                    usage_kind="memory_consolidation",
                    trace_id=current_trace_id_hex(),
                    input_tokens=reply.input_tokens,
                    output_tokens=reply.output_tokens,
                )
            )
        except Exception:
            logger.warning("memory_consolidator.aux_usage_persist_failed", exc_info=True)

    async def _consolidate_or_reject(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        cluster: Sequence[MemoryItem],
        summary: ConsolidatorRunSummary,
    ) -> None:
        prompt = _build_cluster_prompt(cluster)
        try:
            reply = await self._aux(
                prompt=prompt,
                model=None,
                tenant_id=tenant_id,
                output_schema=_CLUSTER_OUTPUT_SPEC,
            )
        except LLMOutputValidationError:
            # RT-ADR-3 — the router exhausted its validation retries;
            # identical degrade to a malformed reply: false_cluster skip.
            record_memory_cluster_rejected(reason="false_cluster")
            summary.cluster_rejected += 1
            return
        record_consolidator_llm_tokens(
            model=reply.model or self._default_model,
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
        )
        await self._record_aux_usage(reply, tenant_id=tenant_id, user_id=user_id)
        verdict = _parse_cluster_reply(reply)
        if verdict is None:
            # Malformed reply — treat as false_cluster and skip
            record_memory_cluster_rejected(reason="false_cluster")
            summary.cluster_rejected += 1
            return

        if verdict.keep and verdict.summary is not None:
            embedding = await self._embedder.embed_one(verdict.summary, tenant_id=tenant_id)
            new_item = await self._memory.write_consolidated(
                tenant_id=tenant_id,
                user_id=user_id,
                content=verdict.summary,
                embedding=embedding,
                source_ids=[item.id for item in cluster],
            )
            record_memory_consolidated()
            summary.consolidated += 1
            await self._safe_audit(
                tenant_id=tenant_id,
                action=AuditAction.MEMORY_CONSOLIDATED,
                resource_id=str(new_item.id),
                details={
                    "user_id": str(user_id),
                    "source_count": len(cluster),
                    "summary": verdict.summary,
                },
            )
            return

        reject_reason = verdict.reject_reason or "false_cluster"
        record_memory_cluster_rejected(reason=_reason_label(reject_reason))
        summary.cluster_rejected += 1
        if reject_reason != "false_cluster":
            # anti_mislearn rejections deserve an audit row so SecOps
            # can see what the consolidator refused; false_cluster is
            # just noise (embedding pre-filter was wrong, no decision
            # made).
            await self._safe_audit(
                tenant_id=tenant_id,
                action=AuditAction.MEMORY_CONSOLIDATION_REJECTED,
                resource_id=None,
                details={
                    "user_id": str(user_id),
                    "source_ids": [str(item.id) for item in cluster],
                    "reject_reason": reject_reason,
                },
            )

    async def _review_lone_item(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        item: MemoryItem,
        summary: ConsolidatorRunSummary,
    ) -> None:
        prompt = _build_single_review_prompt(item)
        try:
            reply = await self._aux(
                prompt=prompt,
                model=None,
                tenant_id=tenant_id,
                output_schema=_SINGLE_REVIEW_OUTPUT_SPEC,
            )
        except LLMOutputValidationError:
            # RT-ADR-3 — validation retries exhausted; same conservative
            # degrade as a malformed reply: keep + mark reviewed.
            await self._memory.mark_reviewed(
                tenant_id=tenant_id, user_id=user_id, memory_id=item.id
            )
            record_memory_reviewed_durable()
            summary.reviewed_durable += 1
            return
        record_consolidator_llm_tokens(
            model=reply.model or self._default_model,
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
        )
        await self._record_aux_usage(reply, tenant_id=tenant_id, user_id=user_id)
        verdict = _parse_single_reply(reply)
        if verdict is None:
            # Malformed — treat conservatively as "keep + mark reviewed
            # so we don't loop on this item forever".
            await self._memory.mark_reviewed(
                tenant_id=tenant_id, user_id=user_id, memory_id=item.id
            )
            record_memory_reviewed_durable()
            summary.reviewed_durable += 1
            return
        if verdict.is_noise:
            await self._memory.soft_delete(tenant_id=tenant_id, user_id=user_id, memory_id=item.id)
            record_memory_purged(category=verdict.category)
            summary.purged += 1
            await self._safe_audit(
                tenant_id=tenant_id,
                action=AuditAction.MEMORY_PURGED_AS_NOISE,
                resource_id=str(item.id),
                details={
                    "user_id": str(user_id),
                    "category": verdict.category,
                    "content_snapshot": item.content,
                },
            )
            return
        await self._memory.mark_reviewed(tenant_id=tenant_id, user_id=user_id, memory_id=item.id)
        record_memory_reviewed_durable()
        summary.reviewed_durable += 1
        await self._safe_audit(
            tenant_id=tenant_id,
            action=AuditAction.MEMORY_REVIEWED_DURABLE,
            resource_id=str(item.id),
            details={"user_id": str(user_id)},
        )

    async def _review_due_fact(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        item: MemoryItem,
        summary: ConsolidatorRunSummary,
    ) -> None:
        """P5b-2b ⑦ — SUB-PASS 3 per-item verdict: still-true → renew_review
        (extend the window), no-longer-true → expire(). Mirrors
        :meth:`_review_lone_item`'s degrade-conservatively shape."""
        prompt = _build_due_review_prompt(item)
        try:
            reply = await self._aux(
                prompt=prompt,
                model=None,
                tenant_id=tenant_id,
                output_schema=_DUE_REVIEW_OUTPUT_SPEC,
            )
        except LLMOutputValidationError:
            # RT-ADR-3 — validation retries exhausted; conservative degrade:
            # keep + re-arm from the existing window so we don't loop on
            # this item forever.
            await self._memory.renew_review(
                tenant_id=tenant_id,
                user_id=user_id,
                memory_id=item.id,
                expected_valid_days=item.expected_valid_days,
            )
            record_memory_predictive_review(outcome="degraded")
            return
        record_consolidator_llm_tokens(
            model=reply.model or self._default_model,
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
        )
        await self._record_aux_usage(reply, tenant_id=tenant_id, user_id=user_id)
        verdict = _parse_due_reply(reply)
        if verdict is None:
            # Malformed — same conservative degrade as above.
            await self._memory.renew_review(
                tenant_id=tenant_id,
                user_id=user_id,
                memory_id=item.id,
                expected_valid_days=item.expected_valid_days,
            )
            record_memory_predictive_review(outcome="degraded")
            return
        if not verdict.still_valid:
            await self._memory.expire(tenant_id=tenant_id, user_id=user_id, memory_id=item.id)
            record_memory_predictive_review(outcome="expired")
            summary.expired += 1
            await self._safe_audit(
                tenant_id=tenant_id,
                action=AuditAction.MEMORY_FACT_EXPIRED,
                resource_id=str(item.id),
                details={"user_id": str(user_id), "content_snapshot": item.content},
            )
            return
        new_window = verdict.expected_valid_days or item.expected_valid_days
        await self._memory.renew_review(
            tenant_id=tenant_id,
            user_id=user_id,
            memory_id=item.id,
            expected_valid_days=new_window,
        )
        record_memory_predictive_review(outcome="renewed")
        summary.renewed += 1
        await self._safe_audit(
            tenant_id=tenant_id,
            action=AuditAction.MEMORY_FACT_RENEWED,
            resource_id=str(item.id),
            details={"user_id": str(user_id), "expected_valid_days": new_window},
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _list_tenants(self) -> list[UUID]:
        """Tenants with at least one live transient memory row.

        Mirrors :meth:`SkillCurator._sweep_tenant`'s use of
        ``SkillStore.curator_distinct_tenant_ids`` (Sprint #4
        Mini-ADR U-26). Tenants without transient data are skipped
        cheaply — no LLM, no audit, no metric noise.
        """
        try:
            return await self._memory.consolidator_distinct_tenant_ids()
        except Exception:
            logger.exception("memory_consolidator.tenant_list_failed")
            return []

    async def _resolve_thresholds(self, tenant_id: UUID) -> _ResolvedThresholds:
        try:
            cfg = await self._tenant_config.get(tenant_id=tenant_id)
        except TenantConfigNotConfiguredError:
            return _ResolvedThresholds.defaults()
        return _ResolvedThresholds(
            min_cluster_size=cfg.memory_consolidation_min_cluster_size,
            similarity=cfg.memory_consolidation_similarity,
            purge_enabled=cfg.memory_purge_enabled,
            purge_min_age_days=cfg.memory_purge_min_age_days,
            predictive_review_enabled=cfg.memory_predictive_review_enabled,
        )

    async def _safe_audit(
        self,
        *,
        tenant_id: UUID,
        action: AuditAction,
        resource_id: str | None,
        details: dict[str, object],
    ) -> None:
        try:
            await audit_emit(
                self._audit,
                tenant_id=tenant_id,
                actor_id=self._actor_id,
                action=action,
                resource_type="memory_item",
                resource_id=resource_id,
                result=AuditResult.SUCCESS,
                trace_id=current_trace_id_hex(),
                details=details,
            )
        except Exception:
            logger.exception("memory_consolidator.audit_emit_failed action=%s", action.value)


def _reason_label(reject_reason: str) -> str:
    """Map ``anti_mislearn:<category>`` → ``<category>`` for metric labels.
    ``false_cluster`` and unknown shapes pass through unchanged."""
    if reject_reason.startswith("anti_mislearn:"):
        return reject_reason.split(":", 1)[1]
    return reject_reason


@dataclass(frozen=True)
class _ResolvedThresholds:
    min_cluster_size: int
    similarity: float
    purge_enabled: bool
    purge_min_age_days: int
    predictive_review_enabled: bool

    @classmethod
    def defaults(cls) -> _ResolvedThresholds:
        return cls(
            min_cluster_size=3,
            similarity=0.85,
            purge_enabled=True,
            purge_min_age_days=30,
            predictive_review_enabled=False,
        )


# Use the all-zero UUID for platform-owned audit rows.
_PLATFORM_TENANT_ID: UUID = UUID("00000000-0000-0000-0000-000000000000")


def make_consolidator_embedder(embedder: object) -> ConsolidatorEmbedder:
    """Factory that wraps an orchestrator-style batch embedder for the
    consolidator's single-text needs. Top-level public so tests +
    ``control_plane.app`` wire-up can construct without depending on
    private name."""
    return _OrchestratorEmbedderAdapter(embedder)


def make_null_consolidator_aux_model() -> ConsolidatorAuxModel:
    """Construct the default no-op aux model that emits valid-shape
    JSON for both prompt families. See :class:`_NullConsolidatorAuxModel`
    for the rationale (worker runs end-to-end even before a production
    LLM adapter is wired)."""
    return _NullConsolidatorAuxModel()


__all__ = [
    "ClusterVerdict",
    "ConsolidatorAuxModel",
    "ConsolidatorEmbedder",
    "ConsolidatorLLMReply",
    "ConsolidatorRunSummary",
    "MemoryConsolidator",
    "SingleReviewVerdict",
    "make_consolidator_embedder",
    "make_null_consolidator_aux_model",
]
