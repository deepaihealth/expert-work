"""Rollback gate (Stream SE, SE-7d-3a) — executes the regression-rollback decision.

The symmetric counterpart of SE-7c's :class:`PromotionGate`. Given a live skill
version + its promote-time baseline success rate, it aggregates the post-
promotion outcome window (SE-7d-1 :meth:`SkillStore.skill_run_outcomes`), runs
the SE-7d-2 judge (:func:`decide_rollback`), and on a ``ROLLBACK`` verdict:

* archives the skill (``set_status(ARCHIVED)``),
* feeds the circuit breaker ``ok=False`` on the SAME ``{tenant}:{agent}`` scope
  as auto-promote — a promote that later rolls back IS a failed promote, so a
  run of bad auto-promotes trips the breaker and degrades the channel to all-
  human (SE-A12), and
* writes the ``SKILL_EVOLUTION_ROLLED_BACK`` audit entry, whose ``details``
  carry the rollback evidence (rate / baseline / drop / p-value / n). A rollback
  is not a replay, so it does NOT write a ``skill_eval_result`` row.

The window math (``since``) and ``now`` are injected by the SE-7d-3b monitor so
this stays deterministic. The cross-tenant enumeration loop + the orchestrator
run-end emission both live in SE-7d-3b (real path, integration-validated).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from control_plane.skill_evolution_limits import CircuitBreaker
from control_plane.skill_rollback import (
    RollbackConfig,
    RollbackDecision,
    decide_rollback,
    should_rollback,
)
from expert_work.persistence.feedback_store import FeedbackStore
from expert_work.persistence.rls import (
    bypass_rls_var,
    current_tenant_id_var,
    current_user_id_var,
)
from expert_work.persistence.skill.base import SkillStore
from expert_work.protocol import AuditAction, AuditEntry, AuditResult, TrajectoryOutcome
from expert_work.protocol.skill import SkillStatus
from expert_work.runtime.audit.logger import AuditLogger

__all__ = ["RollbackGate"]


def _scope_key(tenant_id: UUID, agent_name: str) -> str:
    return f"{tenant_id}:{agent_name}"


@contextmanager
def _tenant_scope(tenant_id: UUID) -> Iterator[None]:
    """Tenant-scoped RLS context for the feedback join (Stream HX-2).

    The SE-7d-3b monitor runs the whole sweep under an owner RLS bypass,
    but ``feedback`` is a FORCE-RLS table — an owner bypass reads zero
    rows *silently*. The monitor only rolls back tenant skills
    (``_resolve_target`` guards ``tenant_id`` non-None), so a plain
    per-tenant scope is always available here.
    """
    tenant = current_tenant_id_var.set(tenant_id)
    bypass = bypass_rls_var.set(False)
    user = current_user_id_var.set(None)
    try:
        yield
    finally:
        current_user_id_var.reset(user)
        bypass_rls_var.reset(bypass)
        current_tenant_id_var.reset(tenant)


@dataclass
class RollbackGate:
    """Applies the rollback judge + side effects to one live skill version."""

    skill_store: SkillStore
    breaker: CircuitBreaker
    audit_logger: AuditLogger | None = None
    config: RollbackConfig | None = None
    #: Stream HX-2 (Mini-ADR HX-B2) — when wired, user 👎 on a window
    #: thread demotes that sample to ``failed`` before scoring. ``None``
    #: keeps the machine-outcome-only behaviour.
    feedback_store: FeedbackStore | None = None
    #: Live pilot finding #8 — archiving changes the agent's auto-attach set
    #: without a spec-version bump; the BuiltAgent cache must be invalidated
    #: or runs keep binding the rolled-back skill until a restart.
    cache_invalidator: Callable[[UUID], None] | None = None

    async def maybe_rollback(
        self,
        *,
        skill_id: UUID,
        skill_version: int,
        tenant_id: UUID,
        agent_name: str,
        promote_baseline: float,
        since: datetime,
        now: datetime,
    ) -> RollbackDecision:
        """Decide + (if ROLLBACK) archive the version, feed the breaker, audit."""
        usages = await self.skill_store.skill_run_usage_window(
            skill_id=skill_id,
            skill_version=skill_version,
            tenant_id=tenant_id,
            since=since,
        )
        outcomes: list[TrajectoryOutcome] = [u.outcome for u in usages]
        disapproved = 0
        if self.feedback_store is not None and usages:
            # PR4 — 反馈来源(console/external)在这里**刻意不参与判断**:这里问
            # 的是「这个技能版本是不是跑砸了」,员工与终端用户的 👎 同等有效
            # (spec §6)。展示面要区分来源,判断面不区分 —— 不要在这里补一个
            # source 过滤。
            with _tenant_scope(tenant_id):
                down = await self.feedback_store.down_rated_threads(
                    thread_ids=[u.thread_id for u in usages]
                )
            if down:
                # A user 👎 overrides the machine verdict for that sample —
                # the run "succeeded" but failed the user. ``cancelled``
                # stays cancelled (still excluded by the judge).
                outcomes = [
                    "failed" if u.thread_id in down and u.outcome == "success" else u.outcome
                    for u in usages
                ]
                disapproved = sum(1 for u in usages if u.thread_id in down)
        decision = decide_rollback(outcomes, promote_baseline=promote_baseline, config=self.config)
        if should_rollback(decision):
            await self.skill_store.set_status(
                skill_id=skill_id, tenant_id=tenant_id, status=SkillStatus.ARCHIVED
            )
            self.breaker.record(_scope_key(tenant_id, agent_name), ok=False, now=now)
            await self._audit(tenant_id, skill_id, skill_version, agent_name, decision, disapproved)
            if self.cache_invalidator is not None:
                self.cache_invalidator(tenant_id)
        return decision

    async def _audit(
        self,
        tenant_id: UUID,
        skill_id: UUID,
        skill_version: int,
        agent_name: str,
        decision: RollbackDecision,
        disapproved: int,
    ) -> None:
        if self.audit_logger is None:
            return
        await self.audit_logger.write(
            AuditEntry(
                tenant_id=tenant_id,
                actor_type="system",
                actor_id="skill-evolution-worker",
                action=AuditAction.SKILL_EVOLUTION_ROLLED_BACK,
                resource_type="skill",
                resource_id=str(skill_id),
                result=AuditResult.SUCCESS,
                details={
                    "agent_name": agent_name,
                    "skill_version": skill_version,
                    "observed_rate": round(decision.observed_rate, 4),
                    "baseline_rate": round(decision.baseline_rate, 4),
                    "drop": round(decision.drop, 4),
                    "p_value": round(decision.p_value, 6),
                    "n_cases": decision.n_cases,
                    "disapproved_n": disapproved,
                    "reason": decision.reason,
                },
            )
        )
