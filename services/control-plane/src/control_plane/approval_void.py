"""新一轮作废上一轮的待审批(班车 2 安全修复,用户拍板语义)。

缺陷:会话里上一轮停在审批上(run PAUSED、审批 PENDING),调用方直接发起新一轮。
新一轮的门控工具调用曾不经审批直接执行;事后批准上一轮的审批,续跑的是已经
属于新一轮的检查点。拍板:**新一轮作废上一轮的待审批**,不 409 调用方 ——
上一轮收成 INTERRUPTED(``new_turn``),它的审批变成不可再裁定,新一轮照常跑、
审批门完整生效。

审批行上的裁决者始终是 ``ApprovalStore.mark_decided`` 这一个 CAS(多副本安全);
检查点上的裁决者是 ``pending_approval``:新一轮的图输入总会清掉它,所以续跑在
写检查点之前核对它,更新的一轮一律过不了(终审 C1)。

* :func:`close_previous_turn` —— 新一轮开跑前(``spawn_run`` 的 stream / queue 两支,
  建行 / 入队之前;``RunQueueWorker`` 出队执行之前):
  1. :func:`void_pending_approvals` 作废会话里还在等裁定的审批(审批行 CAS →
     一条审计 → run 行)。CAS 输给同时到达的人工裁定 = 那条审批已经在续跑,
     新一轮照今天「会话里有 run 在跑」的语义继续(不拦、不等)。
  2. :func:`repair_turn_tail` **按历史的形状**收口上一轮(终审 I1):尾巴是没有
     结果的工具调用、而它所属的那一轮已经结束,就逐个补结果。这一步不看作废
     有没有发生 —— 审批登记窗口、作废中途失败、取消落在工具执行之前,都是同一个
     形状,都在这里收口。
* :func:`void_if_superseded` —— 审批裁定的唯一咽喉 ``resolve_approval_decision``
  在 CAS 之前调用:这条审批所在的 run 之后,会话里已经有更新的 run(新一轮建行
  早于这条审批登记,或修复前遗留的数据)。同样作废,调用方拿到与「已被裁定」同一个
  409。超时扫描走同一个咽喉。**不碰检查点** —— 它已经属于更新的一轮。
* :func:`void_decided_approval` —— 咽喉赢了 CAS、写检查点前的核对却发现会话已经
  被新一轮占用:把这次裁定改记为作废(续跑 id 清掉,同 key 重放只会得到 409),
  收掉 run 行,不写检查点、不开续跑。

作废留下的记录:审批行 ``rejected`` / ``decided_by=system:new_turn``、run 行
``interrupted`` / ``new_turn``、一条 ``approval:decided`` 审计
(``reason=voided_by_new_turn``,只有 id,没有参数值)。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig

from control_plane.audit import emit
from expert_work.persistence.approval import ApprovalStore
from expert_work.protocol import ApprovalRecord, ApprovalStatus, AuditAction
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.runs import InterruptReason, RunManager, RunStatus
from orchestrator import repair_unanswered_tail
from orchestrator.approval_turn import UNRUN_TOOL_CALL_CONTENT, VOIDED_APPROVAL_CONTENT

__all__ = [
    "VOIDED_BY",
    "VOID_AUDIT_REASON",
    "close_previous_turn",
    "repair_turn_tail",
    "void_decided_approval",
    "void_if_superseded",
    "void_pending_approvals",
]

logger = logging.getLogger(__name__)

#: 作废写进审批行 ``decided_by`` 的值 —— 审批队列页据此认出「系统作废」而非人工拒绝。
VOIDED_BY = "system:new_turn"
#: 作废那条审计的 ``reason``。
VOID_AUDIT_REASON = "voided_by_new_turn"

#: 这些状态的 run 还在进行,它留下的尾巴不能动。
_LIVE_RUN_STATUSES = frozenset({RunStatus.PENDING, RunStatus.QUEUED, RunStatus.RUNNING})


async def close_previous_turn(
    *,
    graph: Any,
    thread_id: UUID,
    tenant_id: UUID,
    new_run_id: UUID,
    approvals: ApprovalStore,
    run_manager: RunManager,
    audit: AuditLogger,
    actor_id: str,
    trace_id: str | None = None,
    on_behalf_of: str | None = None,
) -> None:
    """新一轮 ``new_run_id`` 开跑前:先作废待审批,再按形状收口上一轮的尾巴。"""
    await void_pending_approvals(
        thread_id=thread_id,
        tenant_id=tenant_id,
        new_run_id=new_run_id,
        approvals=approvals,
        run_manager=run_manager,
        audit=audit,
        actor_id=actor_id,
        trace_id=trace_id,
        on_behalf_of=on_behalf_of,
    )
    await repair_turn_tail(
        graph=graph,
        thread_id=thread_id,
        tenant_id=tenant_id,
        new_run_id=new_run_id,
        approvals=approvals,
        run_manager=run_manager,
        audit=audit,
        actor_id=actor_id,
        trace_id=trace_id,
        on_behalf_of=on_behalf_of,
    )


async def void_pending_approvals(
    *,
    thread_id: UUID,
    tenant_id: UUID,
    new_run_id: UUID,
    approvals: ApprovalStore,
    run_manager: RunManager,
    audit: AuditLogger,
    actor_id: str,
    trace_id: str | None = None,
    on_behalf_of: str | None = None,
) -> tuple[UUID, ...]:
    """作废会话里所有还在等裁定的审批;返回这次真正作废了哪些 run。"""
    pending = await approvals.list_pending_by_thread(thread_id=thread_id, tenant_id=tenant_id)
    voided: list[UUID] = []
    for record in pending:
        if await _void(
            record,
            new_run_id=new_run_id,
            approvals=approvals,
            run_manager=run_manager,
            audit=audit,
            actor_id=actor_id,
            trace_id=trace_id,
            on_behalf_of=on_behalf_of,
        ):
            voided.append(record.run_id)
    return tuple(voided)


async def repair_turn_tail(
    *,
    graph: Any,
    thread_id: UUID,
    tenant_id: UUID,
    new_run_id: UUID,
    approvals: ApprovalStore,
    run_manager: RunManager,
    audit: AuditLogger,
    actor_id: str,
    trace_id: str | None = None,
    on_behalf_of: str | None = None,
) -> int:
    """按历史的形状收口上一轮;返回补了几条工具结果。

    先看会话里除新一轮之外最近的那一轮:没有、或它是 SUCCESS(成功结束的一轮不会
    留下没有结果的工具调用),就不去读检查点 —— 绝大多数新一轮只多这一条带索引的查询。
    """
    store = run_manager.store
    if store is None:
        return 0
    latest = await store.latest_by_thread(
        thread_id=thread_id, tenant_id=tenant_id, exclude_run_id=new_run_id
    )
    if latest is None or latest.status is RunStatus.SUCCESS:
        return 0

    async def content_for(raw_run_id: str) -> str | None:
        try:
            tail_run_id = UUID(raw_run_id)
        except ValueError:
            return None
        row = await store.get(run_id=tail_run_id, tenant_id=tenant_id)
        if row is None or row.status in _LIVE_RUN_STATUSES:
            return None
        if row.status is RunStatus.PAUSED:
            await _settle_paused(
                tail_run_id,
                tenant_id=tenant_id,
                new_run_id=new_run_id,
                approvals=approvals,
                run_manager=run_manager,
                audit=audit,
                actor_id=actor_id,
                trace_id=trace_id,
                on_behalf_of=on_behalf_of,
            )
            return VOIDED_APPROVAL_CONTENT
        if row.status is RunStatus.INTERRUPTED and row.error == InterruptReason.NEW_TURN:
            return VOIDED_APPROVAL_CONTENT
        return UNRUN_TOOL_CALL_CONTENT

    # 只带会话,不带新一轮的 run_id(理由见 ``repair_unanswered_tail``)。
    config: RunnableConfig = {
        "configurable": {"thread_id": str(thread_id), "tenant_id": str(tenant_id)}
    }
    closed = await repair_unanswered_tail(graph, config, content_for=content_for)
    if closed:
        logger.info(
            "approval.turn_tail_repaired thread_id=%s new_run_id=%s tool_results=%d",
            thread_id,
            new_run_id,
            closed,
        )
    return closed


async def _settle_paused(
    run_id: UUID,
    *,
    tenant_id: UUID,
    new_run_id: UUID,
    approvals: ApprovalStore,
    run_manager: RunManager,
    audit: AuditLogger,
    actor_id: str,
    trace_id: str | None,
    on_behalf_of: str | None,
) -> None:
    """尾巴属于一个 PAUSED 的 run、而新一轮要开跑了:把它的审批与 run 行收拾到位。

    * 审批还是 PENDING —— 它在上面的作废之后才登记(登记窗口):照常作废。
    * 还没有审批行(同一个窗口的更早一刻)—— 先收 run 行;审批稍后登记成 PENDING,
      有人裁定或超时扫描时由咽喉作废。
    * 审批已被系统作废、run 行还是 PAUSED —— 上一次作废在收 run 行时失败了,补上。
    * 审批已被人裁定(续跑还没写进检查点):不动,续跑写之前的核对会失败并自己收尾。
    """
    approval = await approvals.get_by_run(run_id=run_id, tenant_id=tenant_id)
    if approval is not None and approval.status is ApprovalStatus.PENDING:
        await _void(
            approval,
            new_run_id=new_run_id,
            approvals=approvals,
            run_manager=run_manager,
            audit=audit,
            actor_id=actor_id,
            trace_id=trace_id,
            on_behalf_of=on_behalf_of,
        )
    elif approval is None or approval.decided_by == VOIDED_BY:
        await run_manager.close_paused(run_id, tenant_id=tenant_id, reason=InterruptReason.NEW_TURN)


async def void_if_superseded(
    approval: ApprovalRecord,
    *,
    approvals: ApprovalStore,
    run_manager: RunManager,
    audit: AuditLogger,
    actor_id: str,
    trace_id: str | None = None,
) -> bool:
    """这条待审批所在的 run 之后会话里已经有更新的 run → 作废它并返回 ``True``。

    ``True`` 与这次 CAS 谁赢无关:更新的 run 已经存在,这条审批无论如何不能再续跑。
    没接持久化(``run_manager.store is None``)或找不到这条审批的 run 行时判不了,
    返回 ``False``,保持原有行为。
    """
    newer = await _newer_run_id(run_manager, approval)
    if newer is None:
        return False
    await _void(
        approval,
        new_run_id=newer,
        approvals=approvals,
        run_manager=run_manager,
        audit=audit,
        actor_id=actor_id,
        trace_id=trace_id,
        on_behalf_of=None,
    )
    return True


async def void_decided_approval(
    approval: ApprovalRecord,
    *,
    continuation_run_id: UUID,
    approvals: ApprovalStore,
    run_manager: RunManager,
    audit: AuditLogger,
    actor_id: str,
    trace_id: str | None = None,
) -> None:
    """咽喉赢了 CAS,写检查点前发现会话已被新一轮占用:这次裁定作废,不续跑。"""
    voided = await approvals.void_continuation(
        run_id=approval.run_id,
        tenant_id=approval.tenant_id,
        continuation_run_id=continuation_run_id,
        decided_by=VOIDED_BY,
        decided_at=datetime.now(UTC),
    )
    await run_manager.close_paused(
        approval.run_id, tenant_id=approval.tenant_id, reason=InterruptReason.NEW_TURN
    )
    newer = await _newer_run_id(run_manager, approval)
    if voided:
        await _emit_void_audit(
            approval,
            new_run_id=newer,
            audit=audit,
            actor_id=actor_id,
            trace_id=trace_id,
            on_behalf_of=None,
        )
    logger.warning(
        "approval.decision_voided_before_resume run_id=%s thread_id=%s new_run_id=%s voided=%s",
        approval.run_id,
        approval.thread_id,
        newer,
        voided,
    )


async def _newer_run_id(run_manager: RunManager, approval: ApprovalRecord) -> UUID | None:
    store = run_manager.store
    if store is None:
        return None
    rows = await store.list_by_thread(thread_id=approval.thread_id, tenant_id=approval.tenant_id)
    own = next((r for r in rows if r.run_id == approval.run_id), None)
    if own is None:
        return None
    newer = [r.run_id for r in rows if r.created_at > own.created_at]
    return newer[-1] if newer else None


async def _void(
    record: ApprovalRecord,
    *,
    new_run_id: UUID,
    approvals: ApprovalStore,
    run_manager: RunManager,
    audit: AuditLogger,
    actor_id: str,
    trace_id: str | None,
    on_behalf_of: str | None,
) -> bool:
    """审批行 CAS → 审计 → run 行。CAS 输了(已被裁定 / 已被别的新一轮作废)返回 ``False``。"""
    won = await approvals.mark_decided(
        run_id=record.run_id,
        tenant_id=record.tenant_id,
        status=ApprovalStatus.REJECTED,
        decided_by=VOIDED_BY,
        decided_at=datetime.now(UTC),
    )
    if not won:
        logger.info(
            "approval.void_lost run_id=%s thread_id=%s new_run_id=%s",
            record.run_id,
            record.thread_id,
            new_run_id,
        )
        return False
    # 审计紧跟 CAS:作废在 CAS 那一刻就发生了。后面收 run 行失败的话,下一个新一轮
    # 收口尾巴时会补上(``_settle_paused``),审计不会因此丢。
    await _emit_void_audit(
        record,
        new_run_id=new_run_id,
        audit=audit,
        actor_id=actor_id,
        trace_id=trace_id,
        on_behalf_of=on_behalf_of,
    )
    await run_manager.close_paused(
        record.run_id, tenant_id=record.tenant_id, reason=InterruptReason.NEW_TURN
    )
    logger.info(
        "approval.voided run_id=%s thread_id=%s new_run_id=%s",
        record.run_id,
        record.thread_id,
        new_run_id,
    )
    return True


async def _emit_void_audit(
    record: ApprovalRecord,
    *,
    new_run_id: UUID | None,
    audit: AuditLogger,
    actor_id: str,
    trace_id: str | None,
    on_behalf_of: str | None,
) -> None:
    await emit(
        audit,
        tenant_id=record.tenant_id,
        actor_id=actor_id,
        action=AuditAction.APPROVAL_DECIDED,
        resource_type="approval",
        resource_id=str(record.run_id),
        reason=VOID_AUDIT_REASON,
        trace_id=trace_id,
        details={
            "thread_id": str(record.thread_id),
            "decision": "reject",
            "status": ApprovalStatus.REJECTED.value,
            "request_id": record.request_id,
            "voided_by_run_id": str(new_run_id) if new_run_id is not None else None,
        },
        on_behalf_of=on_behalf_of,
    )
