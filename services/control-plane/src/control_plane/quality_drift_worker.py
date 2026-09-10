"""Production quality-drift detector — Stream RT-5 (RT-ADR-24/25).

Resident worker: periodically compares each active agent's recent quality mean
against its baseline mean and, when the recent mean has dropped by more than
the configured threshold, records a ``quality_drift_alert`` and emits a
``quality.drift`` webhook. Consumes the ``quality_score`` series the sampler
(RT-ADR-22) fills.

The webhook is **off the run_event spine** (RT-ADR-25): drift is not a run
event, so the delivery carries ``run_id=None`` and a synthesised ``event_id``.
It reuses the shared ``fan_out_event`` (subscription + per-agent filter +
``(endpoint_id, event_id)`` dedup) and the existing delivery loop + payload
formats (feishu/dingtalk/wecom) — no bespoke transport.

Honest boundaries (not defects): drift is a statistical signal on a subjective
LLM score, never a causal diagnosis or an auto-remediation trigger — the alert
is for a human to research. A per-(tenant, agent) cooldown suppresses repeat
alerts; an agent with too few samples in either window is skipped, not guessed.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.advisory_locks import QUALITY_DRIFT_LOCK_CLASSID
from control_plane.platform_quality_config import (
    EffectiveQualityConfig,
    PlatformQualityConfigService,
)
from control_plane.webhook_delivery_worker import fan_out_event
from expert_work.common.observability import expert_work_counter
from expert_work.persistence import (
    QualityDriftAlertStore,
    QualityScoreStore,
    WebhookDeliveryStore,
    WebhookEndpointStore,
)
from expert_work.persistence.rls import bypass_rls_var, current_tenant_id_var
from expert_work.protocol import QualityDriftAlertRecord, WebhookEndpointRecord

logger = logging.getLogger("expert_work.control_plane.quality_drift")

#: Default cadence — drift is a slow signal; an hourly check is ample.
_DEFAULT_INTERVAL_S = 3600.0

#: The lock txn is held open for the whole cycle; keep it off any idle reaper.
_LOCK_TXN_TIMEOUT_MS = 5 * 60 * 1000

_drift_total = expert_work_counter(
    "expert_work_control_plane_quality_drift_alerts_total",
    "Quality-drift alerts raised (recent mean dropped below baseline).",
)
_agent_errors = expert_work_counter(
    "expert_work_control_plane_quality_drift_agent_errors_total",
    "Per-agent drift checks that raised (isolated; cycle continues).",
)
_cycle_errors = expert_work_counter(
    "expert_work_control_plane_quality_drift_cycle_errors_total",
    "Quality-drift worker cycles that ended in a caught exception.",
)


@contextmanager
def _bypass_rls() -> Iterator[None]:
    """RLS-bypass scope for the cross-tenant agent / endpoint scan."""
    bypass = bypass_rls_var.set(True)
    tenant = current_tenant_id_var.set(None)
    try:
        yield
    finally:
        current_tenant_id_var.reset(tenant)
        bypass_rls_var.reset(bypass)


@contextmanager
def _tenant_scope(tenant_id: UUID) -> Iterator[None]:
    """Scope one tenant's window read / alert write (FORCE-RLS check)."""
    tenant = current_tenant_id_var.set(tenant_id)
    bypass = bypass_rls_var.set(False)
    try:
        yield
    finally:
        bypass_rls_var.reset(bypass)
        current_tenant_id_var.reset(tenant)


class QualityDriftWorker:
    """Background task: detect per-agent quality drift, alert + emit webhook."""

    def __init__(
        self,
        *,
        score_store: QualityScoreStore,
        alert_store: QualityDriftAlertStore,
        endpoint_store: WebhookEndpointStore,
        delivery_store: WebhookDeliveryStore,
        config: PlatformQualityConfigService,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._scores = score_store
        self._alerts = alert_store
        self._endpoints = endpoint_store
        self._deliveries = delivery_store
        self._config = config
        self._session_factory = session_factory
        # Cadence + drift windows / thresholds are read live from ``config``
        # each cycle (RT-5 PR-3b). ``_interval_s`` tracks the last cadence for
        # the stop() await bound.
        self._interval_s = _DEFAULT_INTERVAL_S
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
        self._task = asyncio.create_task(self._loop(), name="quality-drift")

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
            # Guarded cadence read: a transient config-read (DB) error keeps the
            # last cadence and retries next cycle rather than killing the task.
            try:
                cfg = await self._config.effective()
                self._interval_s = float(cfg.drift_interval_s)
            except Exception:
                _cycle_errors.inc()
                logger.exception("quality_drift.config_read_failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
                return  # stop event fired
            except TimeoutError:
                pass
            try:
                await self.run_once()
            except Exception:
                _cycle_errors.inc()
                logger.exception("quality_drift.cycle_failed")

    async def run_once(self) -> int:
        """Run one drift cycle, single-flight across replicas.

        The cooldown (check-then-insert) is not atomic, so two replicas could
        both alert for the same agent. A ``pg_try_advisory_xact_lock`` makes the
        whole cycle single-flight (mirrors the DB-level idempotency the sibling
        workers get from UNIQUE constraints); a replica that misses the lock
        skips this cycle. Single-process / in-memory runs have no factory and
        need no lock.
        """
        cfg = await self._config.effective()
        if not cfg.enabled:
            # Worker is always running; the UI toggle / deploy gate decides work.
            return 0
        if self._session_factory is None:
            return await self._run_cycle(cfg)
        async with self._session_factory() as lock_session:
            # Long-hold guard: the lock txn stays open for the cycle; keep it off
            # any idle-in-transaction reaper (same posture as PgWorkspaceLock).
            await lock_session.execute(
                text(f"SET LOCAL idle_in_transaction_session_timeout = {_LOCK_TXN_TIMEOUT_MS}")
            )
            got = (
                await lock_session.execute(
                    text("SELECT pg_try_advisory_xact_lock(:cid, hashtext(:k))"),
                    {"cid": QUALITY_DRIFT_LOCK_CLASSID, "k": "quality_drift"},
                )
            ).scalar_one()
            if not got:
                await lock_session.rollback()
                return 0
            try:
                return await self._run_cycle(cfg)
            finally:
                # rollback ends the txn → releases the xact advisory lock.
                await lock_session.rollback()

    async def _run_cycle(self, cfg: EffectiveQualityConfig) -> int:
        """Check every active agent once; returns alerts raised."""
        now = datetime.now(tz=UTC)
        recent_start = now - timedelta(hours=cfg.drift_recent_window_h)
        baseline_start = recent_start - timedelta(hours=cfg.drift_baseline_window_h)
        cooldown = timedelta(hours=cfg.drift_cooldown_h)
        with _bypass_rls():
            agents = await self._scores.list_agents_with_scores_since(since=recent_start)
            endpoints = await self._endpoints.list_enabled_all_tenants()
        by_tenant: dict[UUID, list[WebhookEndpointRecord]] = defaultdict(list)
        for ep in endpoints:
            by_tenant[ep.tenant_id].append(ep)
        alerts = 0
        for tenant_id, agent_name in agents:
            # Per-agent isolation: one agent's DB error must not starve the rest
            # of the cycle (the worker is stateless; a skipped agent retries).
            try:
                alert = await self._check_agent(
                    tenant_id=tenant_id,
                    agent_name=agent_name,
                    now=now,
                    recent_start=recent_start,
                    baseline_start=baseline_start,
                    cooldown=cooldown,
                    min_samples=cfg.drift_min_samples,
                    threshold=cfg.drift_threshold,
                )
                if alert is None:
                    continue
                alerts += 1
                _drift_total.inc()
                await self._emit(alert, by_tenant.get(tenant_id, []), now)
            except Exception:
                _agent_errors.inc()
                logger.exception("quality_drift.agent_failed")
        if alerts:
            logger.info("quality_drift.alerts", extra={"count": alerts})
        return alerts

    async def _check_agent(
        self,
        *,
        tenant_id: UUID,
        agent_name: str,
        now: datetime,
        recent_start: datetime,
        baseline_start: datetime,
        cooldown: timedelta,
        min_samples: int,
        threshold: float,
    ) -> QualityDriftAlertRecord | None:
        with _tenant_scope(tenant_id):
            recent_count, recent_mean = await self._scores.window_stats(
                tenant_id=tenant_id, agent_name=agent_name, since=recent_start, until=now
            )
        if recent_count < min_samples or recent_mean is None:
            return None
        with _tenant_scope(tenant_id):
            base_count, base_mean = await self._scores.window_stats(
                tenant_id=tenant_id, agent_name=agent_name, since=baseline_start, until=recent_start
            )
        if base_count < min_samples or base_mean is None or base_mean <= 0:
            return None
        drift_pct = (base_mean - recent_mean) / base_mean
        if drift_pct < threshold:
            # Not a significant drop (an improvement gives drift_pct <= 0).
            return None
        with _tenant_scope(tenant_id):
            last = await self._alerts.latest_alert_at(tenant_id=tenant_id, agent_name=agent_name)
            if last is not None and now - last < cooldown:
                return None
            return await self._alerts.insert(
                QualityDriftAlertRecord(
                    tenant_id=tenant_id,
                    agent_name=agent_name,
                    recent_mean=recent_mean,
                    baseline_mean=base_mean,
                    drift_pct=drift_pct,
                    recent_count=recent_count,
                    baseline_count=base_count,
                )
            )

    async def _emit(
        self, alert: QualityDriftAlertRecord, endpoints: list[WebhookEndpointRecord], now: datetime
    ) -> None:
        """Fan the alert out as a ``quality.drift`` webhook (off the run spine)."""
        payload: dict[str, object] = {
            "agent_name": alert.agent_name,
            "recent_mean": round(alert.recent_mean, 3),
            "baseline_mean": round(alert.baseline_mean, 3),
            "drift_pct": round(alert.drift_pct, 4),
            "recent_count": alert.recent_count,
            "baseline_count": alert.baseline_count,
            "detected_at": alert.detected_at.isoformat() if alert.detected_at else "",
        }
        await fan_out_event(
            self._deliveries,
            endpoints,
            tenant_id=alert.tenant_id,
            event_type="quality.drift",
            event_id=f"quality_drift:{alert.id}",
            run_id=None,
            agent_name=alert.agent_name,
            payload=payload,
            now=now,
        )


__all__ = ["QualityDriftWorker"]
