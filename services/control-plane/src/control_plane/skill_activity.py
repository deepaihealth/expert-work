"""``ThrottledActivityRecorder`` — Capability Uplift Sprint #4 (Mini-ADR U-27).

Activity tracking for the Curator state machine. Every ``agent build``
that binds a skill (via ``_load_skills``) and every ``skill_view`` tool
call should mark the skill as just-used so the daily Curator sweep
doesn't transition it to ``stale``.

Naive ``UPDATE skill SET last_used_at = NOW()`` on every call would
amplify writes to the skill row under high agent-build / skill_view
fan-out (e.g. 100 agent runs/sec * 5 skills each = 500 UPDATEs/sec on
a small skill set). The recorder dedupes per-skill writes to once per
``ttl_seconds`` window (default 1 hour) — that's plenty granular for a
state machine measured in days.

Process-local: each control-plane replica throttles independently. The
worst case under N replicas is N writes per skill per hour, which is
still negligible. Cross-process coordination via Redis would be tighter
but not worth the dependency for state-machine-grade time scales.

B-84 —— ``kind='view'`` 在原有 ``last_used_at`` bump 之外, 再往
``skill_run_usage`` 追加一条 ``outcome='viewed'`` 的证据行。两件事各有各的
去重门, 这是必须的而不是讲究:

* ``last_used_at`` 的 TTL 窗口(``_last``)**两种 kind 共用, 与 B-84 之前逐行
  一致** —— 绑定路径的节流行为一个字没变。
* ``viewed`` 行按 ``(tenant_id, skill_id, thread_id)`` 去重(``_viewed``),
  与上面那道门完全独立。共用一道的话, 每次 agent build 都发生的 ``bind`` 会
  几乎吃掉每一次 ``view``, 一个**正在被读**的技能照样留不下证据行 —— 那正是
  这个信号要消灭的假象。

``viewed`` 行不进 Curator, 也不进回滚判定
(``SkillStore.skill_run_usage_window`` 会把它们滤掉)。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from expert_work.common.skill_activity import SkillActivityKind, SkillViewEvent
from expert_work.common.uplift_metrics import record_curator_transition
from expert_work.protocol import SkillRunUsage

if TYPE_CHECKING:
    from expert_work.persistence import SkillStore

logger = logging.getLogger("expert_work.control_plane.skill_activity")

# Cap the LRU at 10k entries — bounded memory even if a tenant has
# thousands of skills churning through agent builds. Eviction is a
# minor accuracy hit (an evicted skill may get a redundant UPDATE on
# its next bump) but never a correctness hit.
_LRU_CAP: int = 10_000

# Default 1 hour. State machine cares about days; 1 hour throttle
# leaves up to 1 hour of staleness in worst case (negligible against
# 30-day stale threshold).
_DEFAULT_TTL_SECONDS: int = 3600


class ThrottledActivityRecorder:
    """In-process per-skill throttle around ``SkillStore.bump_last_used_at``.

    Implements :class:`expert_work.common.skill_activity.SkillActivityRecorder`
    via ``record``; ``maybe_record`` exposes the same logic with a
    boolean return for tests that want to assert whether a SQL UPDATE
    actually fired.

    Thread-safety: uses an :class:`asyncio.Lock` to serialize the
    "check + mark" critical section. Fine under asyncio's single-thread
    model (one coroutine in the lock at a time); not safe under
    free-threaded Python without further work — but the control plane
    runs under the standard GIL'd asyncio loop.

    Process-local: under N control-plane replicas, worst case is N
    SQL UPDATEs per skill per ttl window. The Curator's day-scale
    decisions don't notice the difference.
    """

    def __init__(
        self,
        store: SkillStore,
        *,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        cap: int = _LRU_CAP,
    ) -> None:
        self._store = store
        self._ttl = ttl_seconds
        self._cap = cap
        self._last: OrderedDict[UUID, float] = OrderedDict()
        # B-84 — ``viewed`` 行的去重门, 与 ``_last`` 完全独立(见模块 docstring)。
        # key 是 ``(tenant_id, skill_id, thread_id)``: 一个 thread 内同一技能
        # 记一次就够, 因为消费方问的是「有没有打开过」不是「打开过几次」。
        # 进程内的, 多副本下最坏是每副本各插一行 —— 存在性查询对重复行免疫。
        self._viewed: OrderedDict[tuple[UUID, UUID, UUID], None] = OrderedDict()
        self._lock = asyncio.Lock()

    def reset(self) -> None:
        """Drop all cached timestamps — for tests that want a fresh
        throttle state without re-constructing the recorder."""
        self._last.clear()
        self._viewed.clear()

    async def record(
        self,
        *,
        skill_id: UUID,
        tenant_id: UUID,
        kind: SkillActivityKind = "bind",
        view: SkillViewEvent | None = None,
    ) -> None:
        """:class:`SkillActivityRecorder` Protocol entry — fire-and-forget
        from the agent hot path. Discards the boolean result; tests use
        :meth:`maybe_record` instead when they need to assert it."""
        await self.maybe_record(skill_id=skill_id, tenant_id=tenant_id, kind=kind, view=view)

    async def maybe_record(
        self,
        *,
        skill_id: UUID,
        tenant_id: UUID,
        kind: SkillActivityKind = "bind",
        view: SkillViewEvent | None = None,
    ) -> bool:
        """Bump ``last_used_at`` if the throttle window has elapsed.

        B-84 — ``kind='view'`` 还会(在它自己的去重门之后)往
        ``skill_run_usage`` 插一条 ``outcome='viewed'``。两件事的成败彼此独立:
        ``last_used_at`` 被节流掉不影响证据行照插, 反之亦然。

        Returns ``True`` if the ``last_used_at`` SQL UPDATE actually fired
        (used by tests + the metrics layer to count real activity vs.
        squashed dupes) —— 返回值**只**说这一件, 与证据行无关, 免得既有
        调用方的语义被悄悄改掉。Failures from the store are logged +
        swallowed — the agent hot path must NOT fail because activity
        tracking hiccuped.
        """
        if kind == "view" and view is not None:
            await self._maybe_record_view(skill_id=skill_id, tenant_id=tenant_id, view=view)

        now = time.monotonic()
        async with self._lock:
            last = self._last.get(skill_id)
            if last is not None and (now - last) < self._ttl:
                # Move to MRU end so eviction follows usage.
                self._last.move_to_end(skill_id)
                return False
            self._last[skill_id] = now
            self._last.move_to_end(skill_id)
            # LRU eviction once we're past cap.
            while len(self._last) > self._cap:
                self._last.popitem(last=False)

        try:
            updated, auto_revived = await self._store.bump_last_used_at(
                skill_id=skill_id, tenant_id=tenant_id
            )
        except Exception:
            # Per-skill activity is best-effort — never fail the agent
            # run because the audit / state-machine bookkeeping
            # tripped. Log so SecOps can spot pathological rates.
            logger.exception(
                "skill_activity.bump_failed skill_id=%s tenant_id=%s",
                skill_id,
                tenant_id,
            )
            return False

        if auto_revived:
            # Per-call auto-revive — single transition. The audit row
            # belongs to the caller (orchestrator) because they know
            # the actor; the metric is platform-scoped here.
            record_curator_transition(from_state="stale", to_state="active", count=1)
            logger.info(
                "skill_activity.auto_revived skill_id=%s tenant_id=%s",
                skill_id,
                tenant_id,
            )
        return updated

    async def _maybe_record_view(
        self,
        *,
        skill_id: UUID,
        tenant_id: UUID,
        view: SkillViewEvent,
    ) -> bool:
        """Append one ``outcome='viewed'`` evidence row, deduped per thread.

        Returns ``True`` iff a row was actually written. Errors are logged +
        swallowed: 这是记账, 不能让一次 ``skill_view`` 失败。
        """
        key = (tenant_id, skill_id, view.thread_id)
        async with self._lock:
            if key in self._viewed:
                self._viewed.move_to_end(key)
                return False
            self._viewed[key] = None
            while len(self._viewed) > self._cap:
                self._viewed.popitem(last=False)

        try:
            await self._store.record_skill_run_usage(
                usage=SkillRunUsage(
                    id=uuid4(),
                    # 消费方租户 —— 平台技能(``skill.tenant_id IS NULL``)的
                    # 证据行也落在读它的那个租户名下, 这正是 B-84 记在这张表
                    # 而不是 ``skill`` 行上的原因: 平台技能因此对每个租户各自
                    # 可量, 而且写入不需要 bypass RLS。
                    tenant_id=tenant_id,
                    skill_id=skill_id,
                    skill_version=view.skill_version,
                    thread_id=view.thread_id,
                    agent_name=view.agent_name,
                    outcome="viewed",
                    created_at=datetime.now(UTC),
                )
            )
        except Exception:
            logger.exception(
                "skill_activity.view_row_failed skill_id=%s tenant_id=%s",
                skill_id,
                tenant_id,
            )
            return False
        return True
