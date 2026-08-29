"""谁执行一个 run,谁把自己的 trace 写回 ``agent_run.trace_id``。

``token_usage`` 没有 ``run_id`` 列 —— ``totals_by_trace_ids`` 全靠 trace 把
``agent_run`` 和 ``token_usage`` 两张表连起来。所以行里的 ``trace_id`` 必须是
**执行那一段**的 trace,而不是建行那一刻的:连接一断,Runs 列表 / 详情 / 对话页
的 token 字段就是空的,而用量其实好好地躺在库里、只是查不回来。

建行时 API handler 写下的是那一刻的 ``current_trace_id_hex()``。什么时候够用、
什么时候不够:

* ``mode: "stream"`` —— 建行和执行在同一个 HTTP 请求的 context 里,
  ``sse.py`` 的 ``expert_work.session.run`` 根 span 挂在请求 span 下面,父子
  同 trace,天然对得上,不需要回写。
* ``mode: "queue"`` —— HTTP 立刻 202 返回,真正执行在 :class:`RunQueueWorker`
  的后台任务里,``session.run`` 在那儿另起一个 trace。
* **orphan sweep 回收重跑** —— 原主实例崩了,续跑发生在 sweep 的轮询循环里,
  同样是新 trace。
* **触发器(cron / webhook)** —— 建行时压根没传 trace(历史上默认 ``None``),
  行里是 ``NULL``。那条路走的是「建行时就带上执行 trace」,不经过这里。

2026-08-29 测试环境实测:近 5 天 222 个成功 run 里 32 个「用量写了、挂在另一
条 trace 上」,全部是上面的后两类。

软失败:回写不成功只记日志。这是可观测性接线,不该拦住一次本来能跑的 run。
"""

from __future__ import annotations

import logging
from uuid import UUID

from expert_work.runtime.runs import RunStore

__all__ = ["bind_exec_trace"]


async def bind_exec_trace(
    *,
    runs: RunStore,
    run_id: UUID,
    tenant_id: UUID,
    known_trace_id: str | None,
    exec_trace_id: str | None,
    source: str,
) -> None:
    """把 ``run_id`` 那行的 ``trace_id`` 换成 ``exec_trace_id``。

    ``exec_trace_id`` 由调用方在自己的模块里取 ``current_trace_id_hex()`` 后
    传进来 —— 不在这里取,是为了让每个执行入口保留自己的打桩缝(测试进程没有
    ``init_tracing``,``expert_work_span`` 开出来是 no-op span,那时该函数恒
    返回 ``None``)。

    ``exec_trace_id`` 为 ``None`` 时**保留原值**:那时建行时的 trace 是唯一
    已知的关联,擦掉它只会更糟。

    ``source`` 只进日志,用来一眼看出是哪个执行入口没绑上。
    """
    logger = logging.getLogger(f"expert_work.control_plane.{source}")
    if exec_trace_id is None or exec_trace_id == known_trace_id:
        return
    try:
        ok = await runs.set_trace_id(run_id=run_id, tenant_id=tenant_id, trace_id=exec_trace_id)
    except Exception:
        logger.warning("%s.trace_bind_failed run_id=%s", source, run_id, exc_info=True)
        return
    if not ok:
        logger.warning("%s.trace_bind_missed run_id=%s", source, run_id)
