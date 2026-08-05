"""Sandbox-egress 407/blocked metrics worker — sandbox-egress-age-quota Task 3.

The credential-proxy already writes every connection verdict into
``sandbox_egress_audit`` (audit-over-blocking, sandbox-egress design §3.1).
Rather than adding Prometheus metrics infrastructure to the proxy — a new
aiohttp service, no scrape target — this control-plane worker periodically
scans the audit table and republishes blocked/error verdicts as a Counter.
Control-plane is already a ``dns_sd`` scrape target and already runs
:class:`~control_plane.approval_metrics.ApprovalGaugeWorker` on the same
"scan a table, feed a metric" shape — zero new infrastructure here versus
three new layers (metrics endpoint + scrape config + service discovery) on
the proxy side. The cost: metrics lag by one scan interval (60s); the
alert's 15m window absorbs that easily.

The cursor is process-memory state: the previous cycle's scan-start
timestamp, seeded to "now" at worker construction (the ``_cursor`` field
default below). A restart builds a fresh worker and so loses the cursor,
re-starting from "now" rather than replaying history — replaying would
re-alert on every old fault after every restart. Counter semantics
(``increase()``) are blind to the lost window regardless; alerting cares
about "still happening", not the precise historical total.

Window edges: this cycle reads ``cursor <= occurred_at < scan_start_time``
and then sets ``cursor = scan_start_time``. The upper bound guards against
double-counting: without it, a row written between the ``now`` snapshot and
the ``SELECT`` running would be counted this cycle *and* counted again next
cycle (its ``occurred_at`` is still ``>= `` the new cursor). A row whose
``occurred_at`` predates the new cursor but that commits *after* the scan
started ("late arrival") is skipped by the next cycle regardless — the
proxy's audit write is a synchronous commit on the same clock, so that race
window is milliseconds — not worth the complexity of tracking a safety
margin.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from expert_work.common.observability import expert_work_counter
from expert_work.persistence.sandbox_egress_audit import SandboxEgressAuditStore

logger = logging.getLogger(__name__)

_BLOCKED = expert_work_counter(
    "expert_work_control_plane_sandbox_egress_blocked_total",
    "Blocked / errored sandbox egress attempts recorded in sandbox_egress_audit, by verdict.",
    label_names=("verdict",),
)
_CYCLE_ERRORS = expert_work_counter(
    "expert_work_control_plane_sandbox_egress_metrics_cycle_errors_total",
    "Scan cycles of the egress-audit metrics worker that failed.",
)

#: Refresh cadence — the ApprovalGaugeWorker precedent; a minute of lag is
#: invisible next to the alert's 15m window.
_INTERVAL_S = 60.0


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass
class SandboxEgressMetricsWorker:
    """Periodic scanner that feeds ``sandbox_egress_audit`` verdicts into a Counter."""

    audit_store: SandboxEgressAuditStore
    interval_s: float = _INTERVAL_S

    #: First cycle scans from worker-construction time, not from the epoch —
    #: no history backfill (see module docstring).
    _cursor: datetime = field(default_factory=_utc_now, init=False, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop.clear()
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _loop(self) -> None:
        # Refresh once at startup so the counter is live before the first
        # interval elapses (a restart must not blank the signal for 60s).
        await self.refresh_once()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
                return
            except TimeoutError:
                await self.refresh_once()

    async def refresh_once(self) -> bool:
        """One scan cycle; ``False`` (and a counter) on a failed read."""
        now = _utc_now()
        try:
            counts = await self.audit_store.count_by_verdict_since(since=self._cursor, until=now)
        except Exception:
            logger.exception("sandbox_egress_metrics.cycle_failed")
            _CYCLE_ERRORS.inc()
            return False
        for verdict, n in counts.items():
            _BLOCKED.labels(verdict=verdict).inc(n)
        self._cursor = now
        return True
