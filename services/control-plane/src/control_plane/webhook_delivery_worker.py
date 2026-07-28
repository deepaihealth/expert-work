"""HX-9 PR3a — outbound webhook delivery worker (STREAM-HX § 13).

A periodic loop, started from the control-plane lifespan, that drains the
``webhook_delivery`` queue: each ready row is signed (HMAC-SHA256 over the
JSON body) and POSTed to the tenant's registered URL. The signing secret
lives in the :class:`SecretStore` (the row carries only a ref); the request
never carries platform credentials (Mini-ADR HX-J5).

Outcome → state machine (Mini-ADR HX-J2):
- 2xx                       → ``delivered``
- 4xx (config error)        → ``dead_letter`` immediately (retry won't fix)
- 5xx / timeout / network   → exponential backoff retry; ``dead_letter`` once
                              the attempt budget is spent

Per-endpoint circuit breaker (Mini-ADR HX-J4): consecutive failures trip the
breaker so a sick endpoint is skipped for a cooldown rather than retried hot
every cycle — a slow / broken tenant endpoint never back-pressures others.
Per-tenant concurrency cap bounds in-flight deliveries per tenant.

Modelled after :class:`control_plane.memory.dlq_worker.MemoryDLQWorker`
(start / stop / run_once; the loop never raises so the process is never
crashed by the worker). The enqueue side — scanning the 3 source tables
into ``webhook_delivery`` — is HX-9 PR3b.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import httpx

from expert_work.common.observability import expert_work_counter
from expert_work.persistence import (
    ApprovalStore,
    ArtifactStore,
    SkillStore,
    ThreadMetaStore,
    WebhookDeliveryStore,
    WebhookEndpointStore,
)
from expert_work.persistence.rls import bypass_rls_var
from expert_work.protocol import (
    ApprovalStatus,
    WebhookDeliveryRecord,
    WebhookDeliveryStatus,
    WebhookEndpointRecord,
    WebhookEventType,
)
from expert_work.runtime.runs import RunStatus, RunStore
from expert_work.runtime.runs.schemas import TERMINAL_RUN_STATUSES
from expert_work.runtime.secret_store import SecretNotFoundError, SecretStore

logger = logging.getLogger("expert_work.control_plane.webhook_delivery_worker")

_BATCH_SIZE: int = 100
_MAX_ATTEMPTS: int = 5
#: 1 min → 5 min → 30 min → 2 h → 6 h (same shape as the memory DLQ worker).
_BACKOFF_SCHEDULE: tuple[int, ...] = (60, 5 * 60, 30 * 60, 2 * 3600, 6 * 3600)
#: Consecutive endpoint failures that trip the circuit breaker.
_BREAKER_THRESHOLD: int = 5
#: How long a tripped breaker stays open before a probe is allowed again.
_BREAKER_COOLDOWN_S: int = 300
#: Per-source rows scanned per enqueue cycle (newest-first). Idempotent
#: enqueue (UNIQUE(endpoint_id,event_id)) makes a bounded window safe — a
#: row beyond the window was already enqueued in an earlier cycle.
_SCAN_LIMIT: int = 500
#: HMAC signature header (mirrors GitHub's ``X-Hub-Signature-256``).
_SIGNATURE_HEADER = "X-Expert-Work-Signature-256"

#: A pluggable POST: ``(url, body, headers) -> status_code``. The default is
#: httpx; tests inject a stub so no real network is touched. Raising signals
#: a transport failure (treated as retryable, like a 5xx).
HttpPost = Callable[[str, bytes, dict[str, str]], Awaitable[int]]

_delivered = expert_work_counter(
    "expert_work_webhook_deliveries_succeeded_total",
    "Webhook deliveries that received a 2xx.",
)
_dead_letters = expert_work_counter(
    "expert_work_webhook_deliveries_dead_lettered_total",
    "Webhook deliveries abandoned (4xx config error or retry budget spent).",
)
_retries = expert_work_counter(
    "expert_work_webhook_deliveries_retried_total",
    "Webhook deliveries scheduled for a backoff retry.",
)
_breaker_skips = expert_work_counter(
    "expert_work_webhook_breaker_skips_total",
    "Deliveries skipped because the endpoint's circuit breaker was open.",
)
_cycle_errors = expert_work_counter(
    "expert_work_webhook_delivery_cycle_errors_total",
    "Delivery worker cycles that ended in a caught exception.",
)
_enqueued = expert_work_counter(
    "expert_work_webhook_deliveries_enqueued_total",
    "Webhook deliveries enqueued from the 3 source tables (run / approval / artifact).",
)


def _backoff_seconds(next_attempt: int) -> int:
    if next_attempt <= 0:
        return _BACKOFF_SCHEDULE[0]
    return _BACKOFF_SCHEDULE[min(next_attempt - 1, len(_BACKOFF_SCHEDULE) - 1)]


#: Human-facing titles for the IM bot rendering — one per WebhookEventType.
#: A type missing here still renders (falls back to the raw event name), so
#: adding an event type without a title never breaks delivery.
_EVENT_TITLES: dict[str, str] = {
    "run.completed": "运行完成",
    "run.failed": "运行失败",
    "approval.requested": "审批待处理",
    "artifact.saved": "产物已保存",
    "skill_promote.requested": "技能晋升申请待审",
    "quality.drift": "质量漂移告警",
}


def _im_text(row: WebhookDeliveryRecord) -> str:
    """Render one delivery as a plain-text IM bot message.

    Generic across event types: a Chinese title line (falling back to the
    raw event type) followed by the payload's scalar fields one per line —
    good enough for a human to act on, with the delivery id for tracing.
    """
    title = _EVENT_TITLES.get(row.event_type, row.event_type)
    lines = [f"【Expert Work】{title}"]
    for key, value in sorted((row.payload or {}).items()):
        if value is None or isinstance(value, dict | list):
            continue
        # Collapse line breaks so a multi-line value (e.g. an agent-authored
        # action_summary) can't inject fake "key: value" lines into the card.
        flat = " ".join(str(value).splitlines())
        lines.append(f"{key}: {flat}")
    lines.append(f"event: {row.event_type}")
    lines.append(f"delivery: {row.id}")
    return "\n".join(lines)


def render_channel_body(payload_format: str, row: WebhookDeliveryRecord, envelope: bytes) -> bytes:
    """Shape the delivery body for the endpoint's channel.

    ``generic`` returns the signed expert_work envelope unchanged; the IM formats
    wrap :func:`_im_text` in each platform's incoming-webhook text-message
    schema. Unknown formats fall back to ``generic`` (never block delivery).
    """
    if payload_format == "feishu":
        body: dict[str, object] = {"msg_type": "text", "content": {"text": _im_text(row)}}
    elif payload_format in ("dingtalk", "wecom"):
        body = {"msgtype": "text", "text": {"content": _im_text(row)}}
    else:
        return envelope
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()


@contextlib.contextmanager
def _bypass_rls() -> Iterator[None]:
    token = bypass_rls_var.set(True)
    try:
        yield
    finally:
        bypass_rls_var.reset(token)


async def fan_out_event(
    deliveries: WebhookDeliveryStore,
    endpoints: list[WebhookEndpointRecord],
    *,
    tenant_id: UUID,
    event_type: WebhookEventType,
    event_id: str,
    run_id: UUID | None,
    agent_name: str | None,
    payload: dict[str, object],
    now: datetime,
) -> int:
    """Enqueue one event to every matching endpoint of a tenant; return how many.

    Shared fan-out for the run-event spine (the delivery worker) and the
    off-spine emitters (``quality.drift``, RT-ADR-25): endpoints are filtered by
    subscription + optional per-agent scope, deduped on ``(endpoint_id,
    event_id)``, and enqueued PENDING for the delivery loop to POST. ``run_id``
    is threaded through nullable — an off-spine event passes ``None``.
    """
    count = 0
    for ep in endpoints:
        if event_type not in ep.event_types:
            continue
        if ep.agent_name is not None and ep.agent_name != agent_name:
            continue
        with _bypass_rls():
            if await deliveries.exists_for_event(endpoint_id=ep.id, event_id=event_id):
                continue
            await deliveries.create(
                WebhookDeliveryRecord(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    endpoint_id=ep.id,
                    event_id=event_id,
                    event_type=event_type,
                    run_id=run_id,
                    payload=payload,
                    status=WebhookDeliveryStatus.PENDING,
                    attempt=0,
                    created_at=now,
                    updated_at=now,
                )
            )
        count += 1
        _enqueued.inc()
    return count


async def _httpx_post(url: str, body: bytes, headers: dict[str, str]) -> int:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, content=body, headers=headers)
        return resp.status_code


class WebhookDeliveryWorker:
    """Background task: sign + POST queued webhook deliveries."""

    def __init__(
        self,
        *,
        delivery_store: WebhookDeliveryStore,
        endpoint_store: WebhookEndpointStore,
        secret_store: SecretStore,
        run_store: RunStore,
        approval_store: ApprovalStore,
        artifact_store: ArtifactStore,
        thread_meta_store: ThreadMetaStore,
        skill_store: SkillStore | None = None,
        interval_s: int = 15,
        batch_size: int = _BATCH_SIZE,
        max_attempts: int = _MAX_ATTEMPTS,
        per_tenant_concurrency: int = 4,
        breaker_threshold: int = _BREAKER_THRESHOLD,
        breaker_cooldown_s: int = _BREAKER_COOLDOWN_S,
        scan_limit: int = _SCAN_LIMIT,
        http_post: HttpPost | None = None,
    ) -> None:
        for name, value in (
            ("interval_s", interval_s),
            ("batch_size", batch_size),
            ("max_attempts", max_attempts),
            ("per_tenant_concurrency", per_tenant_concurrency),
        ):
            if value <= 0:
                msg = f"{name} must be positive"
                raise ValueError(msg)
        self._deliveries = delivery_store
        self._endpoints = endpoint_store
        self._secrets = secret_store
        self._runs = run_store
        self._approvals = approval_store
        self._artifacts = artifact_store
        self._threads = thread_meta_store
        # SE-16 PR-8 — pending skill-promote requests fan out as
        # ``skill_promote.requested``; ``None`` disables (legacy assemblies).
        self._skills = skill_store
        self._scan_limit = scan_limit
        self._interval_s = interval_s
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._per_tenant_concurrency = per_tenant_concurrency
        self._breaker_threshold = breaker_threshold
        self._breaker_cooldown_s = breaker_cooldown_s
        self._post = http_post or _httpx_post
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        #: endpoint_id → (consecutive_failures, open_until). In-memory; a
        #: restart resets breakers (acceptable — they re-trip quickly).
        self._breaker: dict[UUID, tuple[int, datetime | None]] = {}

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="webhook-delivery-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=self._interval_s + 5)
        except (TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        finally:
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.enqueue_once()
                await self.run_once()
            except Exception:
                _cycle_errors.inc()
                logger.exception("webhook_delivery.cycle_failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue
            else:
                break

    async def enqueue_once(self) -> int:
        """Scan the 3 source tables and fan out new events into the delivery
        queue. Returns the number of deliveries enqueued this cycle.

        Idempotent: ``UNIQUE(endpoint_id, event_id)`` plus an
        :meth:`exists_for_event` pre-check means re-scanning a bounded
        recent window never double-enqueues (Mini-ADR HX-J1). Agent scope
        is resolved via ``thread_meta`` (runs / approvals carry a thread);
        artifacts have no thread on the parent row, so artifact events only
        match all-agents endpoints (``agent_name = None``) — a documented
        M0 limitation, not a silent drop.
        """
        with _bypass_rls():
            endpoints = await self._endpoints.list_enabled_all_tenants()
        if not endpoints:
            return 0
        by_tenant: dict[UUID, list[WebhookEndpointRecord]] = defaultdict(list)
        for ep in endpoints:
            by_tenant[ep.tenant_id].append(ep)

        now = datetime.now(UTC)
        agent_cache: dict[UUID, str | None] = {}
        enqueued = 0

        # run.completed / run.failed — one scan per terminal status.
        for status in TERMINAL_RUN_STATUSES:
            event_type: WebhookEventType = (
                "run.completed" if status is RunStatus.SUCCESS else "run.failed"
            )
            with _bypass_rls():
                runs = await self._runs.list_all_tenants(status=status, limit=self._scan_limit)
            for run in runs:
                if not by_tenant.get(run.tenant_id):
                    continue
                agent = await self._resolve_agent(run.thread_id, run.tenant_id, cache=agent_cache)
                enqueued += await self._fan_out(
                    by_tenant[run.tenant_id],
                    tenant_id=run.tenant_id,
                    event_type=event_type,
                    event_id=f"run:{run.run_id}",
                    run_id=run.run_id,
                    agent_name=agent,
                    payload={
                        "run_id": str(run.run_id),
                        "thread_id": str(run.thread_id),
                        "status": run.status.value,
                    },
                    now=now,
                )

        # approval.requested — pending approvals awaiting a decision.
        with _bypass_rls():
            approvals, _total = await self._approvals.list_all_tenants(
                status=ApprovalStatus.PENDING, limit=self._scan_limit
            )
        for ap in approvals:
            if not by_tenant.get(ap.tenant_id):
                continue
            agent = await self._resolve_agent(ap.thread_id, ap.tenant_id, cache=agent_cache)
            enqueued += await self._fan_out(
                by_tenant[ap.tenant_id],
                tenant_id=ap.tenant_id,
                event_type="approval.requested",
                event_id=f"approval:{ap.id}",
                run_id=ap.run_id,
                agent_name=agent,
                payload={
                    "approval_id": str(ap.id),
                    "run_id": str(ap.run_id),
                    "thread_id": str(ap.thread_id),
                    "request_id": ap.request_id,
                    # RT-6 — make the IM card actionable: what's awaiting approval
                    # plus the Tier A binding receipt (short fingerprint). ``None``
                    # for an unbound / action-screen approval → the generic
                    # renderer skips the line rather than printing an empty value.
                    "action_summary": ap.action_summary,
                    "reason_kind": ap.reason_kind,
                    "binding": ap.binding_digest[:12] if ap.binding_digest else None,
                },
                now=now,
            )

        # skill_promote.requested — pending visibility-promote requests
        # awaiting review (SE-16 PR-8). No thread/run context — the request
        # carries the proposing agent, so per-agent endpoints still match.
        if self._skills is not None:
            with _bypass_rls():
                promotes, _cursor = await self._skills.list_promote_requests_all_tenants(
                    status="pending", limit=self._scan_limit
                )
            for req in promotes:
                if not by_tenant.get(req.tenant_id):
                    continue
                enqueued += await self._fan_out(
                    by_tenant[req.tenant_id],
                    tenant_id=req.tenant_id,
                    event_type="skill_promote.requested",
                    event_id=f"skill_promote:{req.id}",
                    run_id=None,
                    agent_name=req.requested_by_agent_name,
                    payload={
                        "request_id": str(req.id),
                        "skill_id": str(req.skill_id),
                        "skill_version": req.skill_version,
                        "requested_by_agent_name": req.requested_by_agent_name,
                        "reason": req.reason,
                    },
                    now=now,
                )

        # artifact.saved — one event per (artifact, latest version). The
        # parent row has no thread, so only all-agents endpoints match.
        with _bypass_rls():
            artifacts = await self._artifacts.list_all_tenants()
        for art in artifacts:
            if art.deleted_at is not None or not by_tenant.get(art.tenant_id):
                continue
            enqueued += await self._fan_out(
                by_tenant[art.tenant_id],
                tenant_id=art.tenant_id,
                event_type="artifact.saved",
                event_id=f"artifact:{art.id}:v{art.latest_version}",
                run_id=None,
                agent_name=None,  # no thread on the parent → all-agents only
                payload={
                    "artifact_id": str(art.id),
                    "name": art.name,
                    "kind": str(art.kind),
                    "version": art.latest_version,
                },
                now=now,
            )

        if enqueued:
            logger.info("webhook_delivery.enqueued count=%d", enqueued)
        return enqueued

    async def _resolve_agent(
        self, thread_id: UUID, tenant_id: UUID, *, cache: dict[UUID, str | None]
    ) -> str | None:
        """Resolve a thread's ``agent_name`` (cached per enqueue cycle)."""
        if thread_id in cache:
            return cache[thread_id]
        with _bypass_rls():
            meta = await self._threads.get(thread_id, tenant_id=tenant_id)
        agent = meta.agent_name if meta is not None else None
        cache[thread_id] = agent
        return agent

    async def _fan_out(
        self,
        endpoints: list[WebhookEndpointRecord],
        *,
        tenant_id: UUID,
        event_type: WebhookEventType,
        event_id: str,
        run_id: UUID | None,
        agent_name: str | None,
        payload: dict[str, object],
        now: datetime,
    ) -> int:
        """Enqueue one event to every matching endpoint; return how many."""
        return await fan_out_event(
            self._deliveries,
            endpoints,
            tenant_id=tenant_id,
            event_type=event_type,
            event_id=event_id,
            run_id=run_id,
            agent_name=agent_name,
            payload=payload,
            now=now,
        )

    async def run_once(self) -> tuple[int, int, int]:
        """Drain one batch. Returns ``(delivered, retried, dead_lettered)``."""
        now = datetime.now(UTC)
        with _bypass_rls():
            ready = await self._deliveries.claim_ready(before=now, limit=self._batch_size)
        if not ready:
            return (0, 0, 0)

        # Per-tenant concurrency cap — one semaphore per tenant seen this batch.
        sems: dict[UUID, asyncio.Semaphore] = {}
        for row in ready:
            sems.setdefault(row.tenant_id, asyncio.Semaphore(self._per_tenant_concurrency))

        async def _guarded(row: WebhookDeliveryRecord) -> str:
            async with sems[row.tenant_id]:
                return await self._deliver_one(row, now=now)

        outcomes = await asyncio.gather(*(_guarded(row) for row in ready), return_exceptions=True)
        delivered = retried = dead = 0
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                # A delivery that raised past _deliver_one's own handling is
                # left untouched (status unchanged) → retried next sweep.
                logger.warning("webhook_delivery.unhandled err=%s", outcome)
                continue
            if outcome == "delivered":
                delivered += 1
            elif outcome == "retry":
                retried += 1
            elif outcome == "dead":
                dead += 1
        logger.info(
            "webhook_delivery.cycle batch=%d delivered=%d retry=%d dead=%d",
            len(ready),
            delivered,
            retried,
            dead,
        )
        return delivered, retried, dead

    async def _deliver_one(self, row: WebhookDeliveryRecord, *, now: datetime) -> str:
        """Sign + POST one delivery. Returns ``delivered`` / ``retry`` / ``dead`` / ``skip``."""
        if self._breaker_open(row.endpoint_id, now=now):
            _breaker_skips.inc()
            return "skip"

        with _bypass_rls():
            endpoint = await self._endpoints.get(
                endpoint_id=row.endpoint_id, tenant_id=row.tenant_id
            )
        if endpoint is None or not endpoint.enabled or endpoint.secret_ref is None:
            # Endpoint deleted / disabled / mis-provisioned since enqueue —
            # the delivery can never succeed; dead-letter it (no retry).
            return await self._finish(
                row, WebhookDeliveryStatus.DEAD_LETTER, now=now, error="endpoint unavailable"
            )

        try:
            secret = await self._secrets.get(endpoint.secret_ref)
        except SecretNotFoundError:
            return await self._finish(
                row, WebhookDeliveryStatus.DEAD_LETTER, now=now, error="signing secret missing"
            )

        # Channel formats — an IM endpoint gets a bot message instead of the
        # envelope. The HMAC signs whatever body is actually sent (IM
        # platforms ignore the extra headers).
        body = render_channel_body(endpoint.payload_format, row, self._envelope_bytes(row))
        headers = {
            "Content-Type": "application/json",
            _SIGNATURE_HEADER: "sha256=" + hmac.new(secret.encode(), body, sha256).hexdigest(),
            "X-Expert-Work-Event": row.event_type,
            "X-Expert-Work-Delivery": str(row.id),
        }
        try:
            status = await self._post(endpoint.url, body, headers)
        except Exception as exc:  # transport failure — retryable like a 5xx
            return await self._on_failure(row, now=now, error=f"{type(exc).__name__}: {exc}")

        if 200 <= status < 300:
            self._breaker_reset(row.endpoint_id)
            return await self._finish(
                row, WebhookDeliveryStatus.DELIVERED, now=now, response_status=status
            )
        if 400 <= status < 500 and status not in (408, 429):
            # Config error (bad URL / auth / payload) — retrying won't fix.
            self._breaker_reset(row.endpoint_id)  # not an endpoint-health fault
            return await self._finish(
                row,
                WebhookDeliveryStatus.DEAD_LETTER,
                now=now,
                response_status=status,
                error=f"non-retryable {status}",
            )
        # 5xx / 408 / 429 — retry with backoff.
        return await self._on_failure(
            row, now=now, response_status=status, error=f"retryable {status}"
        )

    def _envelope_bytes(self, row: WebhookDeliveryRecord) -> bytes:
        """Canonical JSON signed + sent. Stable key order so the signature
        the tenant recomputes matches byte-for-byte."""
        envelope = {
            "event_id": row.event_id,
            "event_type": row.event_type,
            "occurred_at": row.created_at.isoformat(),
            "tenant_id": str(row.tenant_id),
            "payload": row.payload,
        }
        return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()

    async def _on_failure(
        self,
        row: WebhookDeliveryRecord,
        *,
        now: datetime,
        response_status: int | None = None,
        error: str,
    ) -> str:
        """Record a retryable failure; trip the breaker; dead-letter if spent."""
        self._breaker_fail(row.endpoint_id, now=now)
        next_attempt = row.attempt + 1
        if next_attempt >= self._max_attempts:
            return await self._finish(
                row,
                WebhookDeliveryStatus.DEAD_LETTER,
                now=now,
                response_status=response_status,
                error=error,
            )
        return await self._finish(
            row,
            WebhookDeliveryStatus.RETRYING,
            now=now,
            response_status=response_status,
            error=error,
            next_retry_at=now + timedelta(seconds=_backoff_seconds(next_attempt)),
        )

    async def _finish(
        self,
        row: WebhookDeliveryRecord,
        status: WebhookDeliveryStatus,
        *,
        now: datetime,
        response_status: int | None = None,
        error: str | None = None,
        next_retry_at: datetime | None = None,
    ) -> str:
        updated = row.model_copy(
            update={
                "status": status,
                "attempt": row.attempt + 1,
                "next_retry_at": next_retry_at,
                "response_status": response_status,
                "error": error,
                "updated_at": now,
            }
        )
        with _bypass_rls():
            await self._deliveries.update(updated)
        if status is WebhookDeliveryStatus.DELIVERED:
            _delivered.inc()
            return "delivered"
        if status is WebhookDeliveryStatus.RETRYING:
            _retries.inc()
            return "retry"
        _dead_letters.inc()
        return "dead"

    # ----------------------------------------------------------- breaker
    def _breaker_open(self, endpoint_id: UUID, *, now: datetime) -> bool:
        state = self._breaker.get(endpoint_id)
        if state is None:
            return False
        _, open_until = state
        if open_until is None:
            return False
        if now >= open_until:
            # Cooldown elapsed — allow a probe (half-open); keep the failure
            # count so a fresh failure re-opens immediately.
            self._breaker[endpoint_id] = (state[0], None)
            return False
        return True

    def _breaker_fail(self, endpoint_id: UUID, *, now: datetime) -> None:
        failures = self._breaker.get(endpoint_id, (0, None))[0] + 1
        open_until = (
            now + timedelta(seconds=self._breaker_cooldown_s)
            if failures >= self._breaker_threshold
            else None
        )
        self._breaker[endpoint_id] = (failures, open_until)

    def _breaker_reset(self, endpoint_id: UUID) -> None:
        self._breaker.pop(endpoint_id, None)


__all__ = ["WebhookDeliveryWorker"]
