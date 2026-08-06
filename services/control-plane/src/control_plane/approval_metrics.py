"""Approval-queue gauge worker — Stream HX-4 (Mini-ADR HX-D2).

A run paused for human approval sits in ``agent_approval`` until a
verdict or the 24h timeout sweep. This single-replica lifespan task
refreshes ``expert_work_control_plane_approvals_pending`` every cycle so a
growing queue (approvals nobody is answering) is visible and alertable
— the skill-curator gauge precedent.

One platform-wide number, no tenant label (cardinality discipline);
per-tenant counts come from the API. The count runs under a bypass
scope: ``agent_approval`` is ENABLE-only RLS, so the owner exemption
covers the cross-tenant read (no GUC emitted). A failed read logs and
skips the cycle — observability never touches the business path.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from expert_work.common.observability import expert_work_counter, expert_work_gauge
from expert_work.persistence import ApprovalStore
from expert_work.persistence.rls import bypass_rls_var, current_tenant_id_var

logger = logging.getLogger(__name__)

_approvals_pending = expert_work_gauge(
    "expert_work_control_plane_approvals_pending",
    "Approval rows currently pending a human verdict, platform-wide (Stream HX-4).",
)
_cycle_errors = expert_work_counter(
    "expert_work_control_plane_approval_gauge_cycle_errors_total",
    "ApprovalGaugeWorker cycles that ended in a caught exception.",
)

#: Refresh cadence. Approvals move on human timescales — a minute of
#: gauge lag is invisible next to the 24h timeout horizon, so this is
#: a constant rather than another settings knob.
_INTERVAL_S = 60.0

#: ``stop()`` 等待当前这一轮 sweep 收尾的上限,超时就取消。刻意**不是**
#: 别处那种 ``interval + 5``:本 worker 的 interval 是分钟级,那个式子给出的
#: 「上界」比 K8s 默认 30s 优雅期还长,等于没有上界。5 秒足够一轮正常 sweep
#: 收尾;收不了尾就取消 —— 这些 sweep 都是周期性、幂等的,下次启动会重来。
_STOP_TIMEOUT_S = 5.0


@contextmanager
def _bypass_scope() -> Iterator[None]:
    bypass = bypass_rls_var.set(True)
    tenant = current_tenant_id_var.set(None)
    try:
        yield
    finally:
        current_tenant_id_var.reset(tenant)
        bypass_rls_var.reset(bypass)


@dataclass
class ApprovalGaugeWorker:
    """Periodic refresher for the pending-approvals gauge."""

    approval_store: ApprovalStore
    interval_s: float = _INTERVAL_S

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
            try:
                await asyncio.wait_for(self._task, timeout=_STOP_TIMEOUT_S)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            finally:
                self._task = None

    async def _loop(self) -> None:
        # Refresh once at startup so the gauge is live before the first
        # interval elapses (a restart must not blank the signal for 60s).
        await self.refresh_once()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
                return
            except TimeoutError:
                await self.refresh_once()

    async def refresh_once(self) -> bool:
        """One gauge refresh; ``False`` (and a counter) on a failed read."""
        try:
            with _bypass_scope():
                pending = await self.approval_store.count_pending()
        except Exception:
            logger.exception("approval_gauge.cycle_failed")
            _cycle_errors.inc()
            return False
        _approvals_pending.set(pending)
        return True
