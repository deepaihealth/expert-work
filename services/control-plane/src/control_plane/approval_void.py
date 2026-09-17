"""新一轮作废上一轮的待审批(班车 2 安全修复,用户拍板语义)。

缺陷:会话里上一轮停在审批上(run PAUSED、审批 PENDING),调用方直接发起新一轮。
新一轮的门控工具调用曾不经审批直接执行;事后批准上一轮的审批,续跑的是已经
属于新一轮的检查点。拍板:**新一轮作废上一轮的待审批**,不 409 调用方 ——
上一轮收成 INTERRUPTED(``new_turn``),它的审批变成不可再裁定,新一轮照常跑、
审批门完整生效。

两个入口,裁决者始终是 ``ApprovalStore.mark_decided`` 这一个 CAS(多副本安全):

* :func:`void_pending_approvals` —— 新一轮开跑前:``spawn_run`` 的 stream / queue
  两支(建行 / 入队之前),以及 ``RunQueueWorker`` 出队执行前(入队与出队之间
  会话可能又停在了审批上)。CAS 输给了同时到达的人工裁定 = 那条审批已经在续跑,
  新一轮照今天「会话里有 run 在跑」的语义继续(不拦、不等)。
* :func:`void_if_superseded` —— 审批裁定的唯一咽喉 ``resolve_approval_decision``
  在 CAS 之前调用:这条审批所在的 run 之后,会话里已经有更新的 run(新一轮建行
  早于这条审批登记,或修复前遗留的数据)。检查点已经属于新一轮,不能拿它续跑:
  同样作废,调用方拿到与「已被裁定」同一个 409。超时扫描走同一个咽喉,所以
  这类审批也不会被超时续跑。

作废写三处:审批行(CAS 赢了才往下)、run 行(PAUSED → INTERRUPTED)、一条
``approval:decided`` 审计(``reason=voided_by_new_turn``,只有 id,没有参数值)。
新一轮入口另外收口检查点(:func:`orchestrator.close_voided_turn`):那一轮悬空的
工具调用逐个补「已作废」的结果,否则严格校验配对的模型厂商会拒绝新一轮的整段请求。
咽喉那条路**不碰检查点** —— 它已经属于更新的一轮,而那一轮可能正在执行。
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
from expert_work.runtime.runs import InterruptReason, RunManager
from orchestrator import close_voided_turn

__all__ = ["VOIDED_BY", "VOID_AUDIT_REASON", "void_if_superseded", "void_pending_approvals"]

logger = logging.getLogger(__name__)

#: 作废写进审批行 ``decided_by`` 的值 —— 审批队列页据此认出「系统作废」而非人工拒绝。
VOIDED_BY = "system:new_turn"
#: 作废那条审计的 ``reason``。
VOID_AUDIT_REASON = "voided_by_new_turn"


async def void_pending_approvals(
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
) -> tuple[UUID, ...]:
    """新一轮 ``new_run_id`` 开跑前,作废会话里所有还在等裁定的审批;返回作废了哪些 run。"""
    pending = await approvals.list_pending_by_thread(thread_id=thread_id, tenant_id=tenant_id)
    voided: list[UUID] = []
    for record in pending:
        if not await _void(
            record,
            new_run_id=new_run_id,
            approvals=approvals,
            run_manager=run_manager,
            audit=audit,
            actor_id=actor_id,
            trace_id=trace_id,
        ):
            continue
        config: RunnableConfig = {
            "configurable": {"thread_id": str(thread_id), "tenant_id": str(tenant_id)}
        }
        closed = await close_voided_turn(graph, config, run_id=str(record.run_id))
        logger.info(
            "approval.void_closed_turn run_id=%s thread_id=%s tool_results=%d",
            record.run_id,
            thread_id,
            closed,
        )
        voided.append(record.run_id)
    return tuple(voided)


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
    )
    return True


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
) -> bool:
    """审批行 CAS → run 行 → 审计。CAS 输了(已被裁定 / 已被别的新一轮作废)返回 ``False``。"""
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
    await run_manager.close_paused(
        record.run_id, tenant_id=record.tenant_id, reason=InterruptReason.NEW_TURN
    )
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
            "voided_by_run_id": str(new_run_id),
        },
    )
    logger.info(
        "approval.voided run_id=%s thread_id=%s new_run_id=%s",
        record.run_id,
        record.thread_id,
        new_run_id,
    )
    return True
