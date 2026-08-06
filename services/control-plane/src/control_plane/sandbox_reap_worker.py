"""Periodic idle-sandbox sweep for the ``agent_sandbox`` backend.

Whole-branch review Important-1. ``AgentSandboxClient.reap`` and everything
it rests on (``_IDLE_TTL_S``, ``last_used_at``, ``list_active(only_idle=…)``)
were built for an idle sweep that **nothing ran**: ``reap``'s only caller in
the whole repo is the ``POST /v1/sandboxes/reap`` admin endpoint. The docker
supervisor does not have this hole — it ships its own in-process reaper
(``sandbox_supervisor/app.py:171-182``, ``SandboxReaper.run_forever``) — but
that reaper lives inside the supervisor service, which the cloud backend does
not go through at all.

Only wired for :class:`~orchestrator.tools.agent_sandbox.AgentSandboxClient`.
Under ``sandbox_backend="supervisor"`` the supervisor's own reaper is already
sweeping, and that backend is frozen for this wave — a second sweeper driving
it from here would be a new feature on frozen code.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from orchestrator.tools.sandbox import SandboxRuntime

logger = logging.getLogger(__name__)

#: How often to sweep. The docker supervisor's own reaper defaults to 10s
#: (``SandboxSupervisorSettings.reaper_interval_s``); this one is far lazier
#: on purpose. Every cycle is a DB read plus one ``connect``+``kill`` per
#: genuinely-idle sandbox, the thing it is reclaiming has a 15-minute idle
#: TTL, and the platform-side timeout is the backstop underneath it — so
#: sweeping 15x an hour instead of 360x buys nothing but load. A constant
#: rather than another settings knob, same call as ``ApprovalGaugeWorker``'s
#: ``_INTERVAL_S``: nothing in ops needs to tune the lag on a 15-minute TTL.
_INTERVAL_S = 240.0

#: ``stop()`` 等待当前这一轮 sweep 收尾的上限,超时就取消。刻意**不是**
#: 别处那种 ``interval + 5``:本 worker 的 interval 是分钟级,那个式子给出的
#: 「上界」比 K8s 默认 30s 优雅期还长,等于没有上界。5 秒足够一轮正常 sweep
#: 收尾;收不了尾就取消 —— 这些 sweep 都是周期性、幂等的,下次启动会重来。
_STOP_TIMEOUT_S = 5.0


@dataclass
class SandboxReapWorker:
    """Runs ``runtime.reap(force=False)`` on a timer.

    Safe to run on every replica concurrently, and that is the intended
    deployment (this service is explicitly multi-replica) — but "idempotent"
    is doing real work in that sentence, so here is *why* rather than just
    the claim:

    * The row list comes from ``sandbox_instance`` (``list_active``), so two
      replicas can hand the same ``(sandbox_id, container_id)`` to their own
      sweep at the same time.
    * Both then ``connect`` + ``kill`` that sandbox. The loser's kill hits an
      already-dead sandbox and raises — which ``reap`` already swallows on
      purpose (a sandbox reclaimed by the platform must still get its row
      cleared, or the warm slot wedges forever).
    * Both then ``mark_destroyed`` the same row. That is a plain terminal
      ``UPDATE ... SET state='DESTROYED'`` keyed by id — running it twice
      writes the same end state, it does not error and does not double-count
      anything that matters (the returned tally can over-count across
      replicas; it is a log line, not a metric anyone reconciles).

    So no lock, no leader election, no single-flight advisory lock (unlike
    ``QualityDriftWorker``, which takes one because ITS cycle raises webhooks
    — a duplicated side effect that leaves the system visibly different).
    """

    runtime: SandboxRuntime
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
        # Unlike ``ApprovalGaugeWorker`` there is no sweep at startup: a
        # restart is exactly when in-flight sandboxes are most likely to be
        # mid-``acquire``, and nothing degrades by waiting one interval (the
        # thing being reclaimed is already 15 minutes idle).
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
                return
            except TimeoutError:
                await self.sweep_once()

    async def sweep_once(self) -> int:
        """One ``reap(force=False)``; ``0`` (and a log line) on failure.

        Never propagates — an exception out of the loop body would kill the
        task and silently stop every future sweep, which is the failure mode
        this worker exists to prevent in the first place.
        """
        try:
            reaped = await self.runtime.reap(force=False)
        except Exception:
            logger.exception("sandbox_reap.cycle_failed")
            return 0
        if reaped:
            logger.info("sandbox_reap.swept reaped=%d", reaped)
        return reaped
