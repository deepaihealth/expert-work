"""两级取消 + 跨副本广播 —— 所有取消入口共用的一处。

两级取消(RT-ADR-17)原来散在五个入口(控制台 / 对外端点 / 租户停用 /
Agent 停用 / Agent 删除)各写一遍 ``run_manager.cancel(...) or
run_store.request_cancel(...)``:本副本持有的 run 由 ``RunManager.cancel``
就地置位 ``abort_event``;别的副本持有的 run 只能靠 ``request_cancel`` CAS
改 durable 行,属主副本要到 ``_heartbeat_loop`` 下一次续租
(``lease_ttl_s / 3``,生产默认 30s/3 = **10s**)CAS 失败才停。

跨副本取消亚秒化:CAS 赢了就在失效总线上广播一条 ``run_cancel``
(run_id + tenant_id,不带用户数据),属主副本的 handler
(``invalidation_bus.build_invalidation_handlers``)立刻做一次同样的心跳检查
并置位 ``abort_event``。#1313 的 CAS / 状态机一步不绕:总线只是让属主
**现在**看一眼,而不是最多 10s 后;Redis 不通时 publish 静默失败,周期心跳
照旧兜底。收成一个函数是为了新入口不会漏广播 —— 只接一处就漏四处。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from control_plane.invalidation_bus import InvalidationBus, InvalidationEvent, NoopInvalidationBus
from expert_work.runtime.runs import InterruptReason, RunManager, RunStore

__all__ = ["RUN_CANCEL_KIND", "cancel_run_two_level"]

#: 失效总线上的事件种类 —— 与 ``invalidation_bus.KINDS`` / handler 表同名。
RUN_CANCEL_KIND = "run_cancel"


async def cancel_run_two_level(
    *,
    run_manager: RunManager,
    run_store: RunStore,
    bus: InvalidationBus | NoopInvalidationBus | None,
    run_id: UUID,
    tenant_id: UUID,
    reason: InterruptReason,
    now: datetime | None = None,
) -> bool:
    """本副本持有 → 就地叫停;否则 CAS 改行,赢了就广播。返回是否真的停了什么。

    ``bus`` 为 ``None``(未接线的 app / 单测)时只少一次广播,取消语义不变。
    """
    if await run_manager.cancel(run_id, reason=reason):
        return True
    won = await run_store.request_cancel(
        run_id=run_id,
        tenant_id=tenant_id,
        updated_at=now if now is not None else datetime.now(UTC),
        reason=reason,
    )
    if won and bus is not None:
        bus.publish_soon(
            InvalidationEvent(kind=RUN_CANCEL_KIND, tenant_id=str(tenant_id), run_id=str(run_id))
        )
    return won
