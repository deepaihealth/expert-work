"""``SkillCurator`` — Capability Uplift Sprint #4 (Mini-ADRs U-26 / U-29).

Periodic background worker that walks every tenant with skills and
applies the state-machine transitions configured in ``tenant_config``:

* ``active`` skills inactive for ``skill_stale_days`` → ``stale``
* ``stale`` skills inactive for ``skill_archive_days`` → ``archived``
* ``pinned`` skills are skipped at every stage (operator escape hatch)
* ``draft`` / ``archived`` skills are inert

Auto-revival (``stale → active`` on activity) is handled by the
``SkillStore.bump_last_used_at`` SQL atomically — NOT here. The
Curator is the one-way "things go cold over time" engine.

Cadence: one sweep per ``interval_s`` (default 86400 = daily). Each
sweep is idempotent — re-running has no effect on already-transitioned
rows because the WHERE clauses filter on ``last_used_at < cutoff``.

Per-tenant configuration: ``skill_stale_days`` + ``skill_archive_days``
from ``tenant_config``. Tenants without a config row fall through to
the platform defaults (30 / 90) — same defaults the Pydantic record
carries.

Audit posture: emits one ``SKILL_CURATOR_RUN`` per sweep per tenant
with a summary (``{active_to_stale, stale_to_archived}``). Per-skill
state changes are NOT individually audited — there can be hundreds in
a single sweep; the summary + the ``state_changed_at`` column gives
SecOps enough to reconstruct what moved.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.audit import emit as audit_emit
from control_plane.tenancy import TenantConfigNotConfiguredError, TenantConfigService
from expert_work.common.observability import current_trace_id_hex
from expert_work.common.uplift_metrics import (
    record_curator_transition,
    set_curator_pinned_skills,
)
from expert_work.protocol import AuditAction, AuditResult, TenantConfigRecord
from expert_work.runtime.audit.logger import AuditLogger

if TYPE_CHECKING:
    from expert_work.persistence import SkillStore

logger = logging.getLogger("expert_work.control_plane.skill_curator")

# Default cadence — one sweep per day. Configurable via the constructor
# so tests can drive a fast loop and platform operators can dial it.
_DEFAULT_INTERVAL_S: float = 86_400.0

#: Advisory-lock classid for the single-flight sweep — makes concurrent
#: replicas race for one winner per cycle instead of each doing the same
#: (harmless but wasted) work. Mirrors
#: ``QualityDriftWorker._DRIFT_LOCK_CLASSID`` (quality_drift_worker.py); a
#: distinct value so the two never share a key (workspace_lock.py uses 1,
#: mcp_oauth_refresh_lock.py uses 2, the drift worker uses 8615, the
#: memory consolidator uses 8616).
_CURATOR_LOCK_CLASSID = 8617
#: The lock txn is held open for the whole sweep; keep it off any idle
#: reaper.
_LOCK_TXN_TIMEOUT_MS = 5 * 60 * 1000

# Fallback thresholds applied when a tenant has no tenant_config row
# yet. Match the Pydantic defaults so behavior is the same whether or
# not an admin has seeded the row.
_DEFAULT_STALE_DAYS: int = 30
_DEFAULT_ARCHIVE_DAYS: int = 90


@dataclass(frozen=True)
class TenantSweepResult:
    """One tenant's transition counts within a single sweep."""

    tenant_id: UUID
    active_to_stale: int
    stale_to_archived: int


@dataclass
class CuratorRunSummary:
    """Aggregate of one full sweep — emitted as the audit + metric payload."""

    tenant_count: int = 0
    active_to_stale: int = 0
    stale_to_archived: int = 0
    pinned_count: int = 0
    per_tenant: list[TenantSweepResult] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    def as_audit_details(self) -> dict[str, object]:
        """Stable dict for the ``SKILL_CURATOR_RUN`` audit row."""
        return {
            "tenant_count": self.tenant_count,
            "active_to_stale": self.active_to_stale,
            "stale_to_archived": self.stale_to_archived,
            "pinned_total": self.pinned_count,
            "started_at": self.started_at.isoformat(),
            "finished_at": (self.finished_at.isoformat() if self.finished_at is not None else None),
        }


class SkillCurator:
    """Periodic state-machine worker.

    Wiring: instantiated in ``control_plane.app.create_app`` and started
    by the lifespan handler, in parallel with the trigger scheduler.
    Single replica per cluster — same reason as ``TriggerScheduler``
    (the sweep is idempotent so duplicates are safe, but they're
    wasted work + audit noise).
    """

    def __init__(
        self,
        *,
        skill_store: SkillStore,
        tenant_config_service: TenantConfigService,
        audit_logger: AuditLogger,
        interval_s: float = _DEFAULT_INTERVAL_S,
        actor_id: str = "skill_curator",
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        if interval_s <= 0:
            msg = "interval_s must be positive"
            raise ValueError(msg)
        self._skills = skill_store
        self._tenant_config = tenant_config_service
        self._audit = audit_logger
        self._interval_s = interval_s
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
        """Schedule the periodic sweep loop. Idempotent."""
        if self.is_running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="skill-curator")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            # Bound the wait — sweeps are O(tenants) and shouldn't take
            # long, but never block shutdown indefinitely on a hung SQL
            # connection.
            await asyncio.wait_for(self._task, timeout=min(self._interval_s, 30.0) + 5.0)
        except (TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        finally:
            self._task = None

    async def _loop(self) -> None:
        # Don't run an immediate sweep on startup — the platform likely
        # restarted in the last few minutes / hours, and a fresh sweep
        # would compete for DB connections with replays. Sleep first;
        # the first sweep happens after ``interval_s``.
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
                # Stop event fired during the wait — exit cleanly.
                return
            except TimeoutError:
                pass
            try:
                await self.run_once()
            except Exception:
                logger.exception("skill_curator.cycle_failed")

    async def run_once(self) -> CuratorRunSummary:
        """Run one sweep, single-flight across replicas.

        The state-machine writes inside the sweep are individually
        idempotent, so two replicas racing the same sweep is safe — just
        wasted work + audit noise (the class docstring's "single replica
        per cluster" note). A ``pg_try_advisory_xact_lock`` makes the whole
        sweep single-flight (mirrors ``QualityDriftWorker.run_once``); a
        replica that misses the lock returns an empty summary immediately,
        no sweep attempted. Single-process / in-memory runs have no
        factory and need no lock.
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
                    {"cid": _CURATOR_LOCK_CLASSID, "k": "skill_curator"},
                )
            ).scalar_one()
            if not got:
                await lock_session.rollback()
                return CuratorRunSummary()
            try:
                return await self._run_sweep()
            finally:
                # rollback ends the txn → releases the xact advisory lock.
                await lock_session.rollback()

    async def _run_sweep(self) -> CuratorRunSummary:
        """One full sweep across all tenants with skills. Idempotent.

        Callable directly from tests + an operator-facing manual-run
        endpoint (M1 follow-on). Returns the summary so callers can
        assert on the transition counts in tests.
        """
        summary = CuratorRunSummary()
        tenant_ids = await self._skills.curator_distinct_tenant_ids()
        for tenant_id in tenant_ids:
            try:
                tenant_result = await self._sweep_tenant(tenant_id)
            except Exception:
                logger.exception("skill_curator.tenant_sweep_failed tenant_id=%s", tenant_id)
                continue
            summary.tenant_count += 1
            summary.active_to_stale += tenant_result.active_to_stale
            summary.stale_to_archived += tenant_result.stale_to_archived
            summary.per_tenant.append(tenant_result)

        summary.pinned_count = await self._skills.count_pinned()
        summary.finished_at = datetime.now(UTC)

        # Refresh the gauge once per sweep — bounded write rate.
        set_curator_pinned_skills(summary.pinned_count)

        # Per-tenant counters so dashboards can split-by-tenant later
        # without a relabel rule. The transition counter doesn't carry
        # a tenant label by design (cardinality blow-up risk); the per-
        # transition increments happen here in bulk.
        if summary.active_to_stale:
            record_curator_transition(
                from_state="active",
                to_state="stale",
                count=summary.active_to_stale,
            )
        if summary.stale_to_archived:
            record_curator_transition(
                from_state="stale",
                to_state="archived",
                count=summary.stale_to_archived,
            )

        # One audit row per sweep — bounded volume even on a busy day.
        try:
            await audit_emit(
                self._audit,
                # The summary is platform-scope (no single tenant
                # owns it); use the platform-tenant zero UUID.
                tenant_id=_PLATFORM_TENANT_ID,
                actor_id=self._actor_id,
                action=AuditAction.SKILL_CURATOR_RUN,
                resource_type="skill",
                resource_id=None,
                result=AuditResult.SUCCESS,
                trace_id=current_trace_id_hex(),
                details=summary.as_audit_details(),
            )
        except Exception:
            logger.exception("skill_curator.audit_emit_failed")

        logger.info(
            "skill_curator.sweep_complete tenants=%d active_to_stale=%d "
            "stale_to_archived=%d pinned_total=%d",
            summary.tenant_count,
            summary.active_to_stale,
            summary.stale_to_archived,
            summary.pinned_count,
        )
        return summary

    async def _sweep_tenant(self, tenant_id: UUID) -> TenantSweepResult:
        """Apply the two transitions for one tenant using its configured
        thresholds (or the platform defaults if no config row exists)."""
        try:
            cfg = await self._tenant_config.get(tenant_id=tenant_id)
            stale_days = cfg.skill_stale_days
            archive_days = cfg.skill_archive_days
        except TenantConfigNotConfiguredError:
            stale_days = _DEFAULT_STALE_DAYS
            archive_days = _DEFAULT_ARCHIVE_DAYS

        active_to_stale = await self._skills.curator_promote_active_to_stale(
            tenant_id=tenant_id, stale_days=stale_days
        )
        stale_to_archived = await self._skills.curator_promote_stale_to_archived(
            tenant_id=tenant_id, archive_days=archive_days
        )
        return TenantSweepResult(
            tenant_id=tenant_id,
            active_to_stale=active_to_stale,
            stale_to_archived=stale_to_archived,
        )


# Use the all-zero UUID for platform-owned audit rows (nothing wears it).
# Matches the convention from H.4 ops audits in
# ``control_plane.audit.emit`` callers that operate on no specific tenant.
_PLATFORM_TENANT_ID: UUID = UUID("00000000-0000-0000-0000-000000000000")

# Re-export the defaults so other modules + tests can reference the
# canonical values rather than duplicating literals.
DEFAULT_STALE_DAYS = _DEFAULT_STALE_DAYS
DEFAULT_ARCHIVE_DAYS = _DEFAULT_ARCHIVE_DAYS

__all__ = [
    "DEFAULT_ARCHIVE_DAYS",
    "DEFAULT_STALE_DAYS",
    "CuratorRunSummary",
    "SkillCurator",
    # Re-exported for tests that need to assert the audit tenant_id.
    "TenantConfigRecord",
    "TenantSweepResult",
]
