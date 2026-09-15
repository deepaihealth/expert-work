"""谁执行一个 run,谁把执行事实写回 ``agent_run`` 那一行。

两件事,同一条规矩、同一批调用点:

* :func:`bind_exec_trace` —— 执行那一段的 OTel trace(``trace_id``)
* :func:`bind_exec_spec` —— 执行时**实际用的 manifest 版本**
  (``agent_spec_sha256``)

两者的共同点是:建行那一刻的值不等于执行那一刻的值,而只有执行方知道后者。
所以规矩必须写在这里、由每个执行入口调用 —— 这条规矩只写在一个执行入口里
正是 #1382 的病因。

---

**trace 部分。**

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

---

**配置版本部分。**

配置页对 manifest 是**原地编辑**:``thread_meta`` 上记的 ``agent_name`` /
``agent_version`` 编辑前后完全一样。没有 ``agent_spec_sha256``,「这条 run 跑的
是哪一版配置」只能拿时间戳去 ``agent_spec_revision`` 里比对着猜。

同样必须在**执行时**写,不能在建行时写 —— 排队的 run 建行时还没构建过,
而 worker 真去执行时读到的可能已经是编辑后的版本。

**不该记的地方**(记了反而是错的):

* ``spawn_run`` 的 ``mode="queue"`` 分支 —— 它只入队、立刻 202 返回,手里那个
  ``built`` 直接丢掉。真正的构建晚一步发生在 :class:`RunQueueWorker` 里,
  用的可能是另一版。
* ``trigger_delivery`` —— 它构建 agent 只为拿 ``built.graph`` 往原会话注入
  投递消息,不执行任何 run。
* ``api/plan.py`` / ``api/agents.py`` 的工具清单与详情端点 —— 只读,无 run。

---

软失败:两个回写都只记日志。这是可观测性接线,不该拦住一次本来能跑的 run。
"""

from __future__ import annotations

import logging
from uuid import UUID

from expert_work.persistence.platform_agent_template import compute_spec_sha256
from expert_work.protocol import AgentSpec
from expert_work.runtime.runs import RunStore

__all__ = ["bind_exec_spec", "bind_exec_trace"]


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


async def bind_exec_spec(
    *,
    runs: RunStore | None,
    run_id: UUID,
    tenant_id: UUID,
    spec: AgentSpec,
    stored_sha256: str,
    source: str,
) -> None:
    """把这一轮实际构建所用 manifest 的内容哈希写进 ``run_id`` 那行。

    绑的是 **``stored_sha256``**——调用方从同一个 record 里带出来的
    ``agent_spec.spec_sha256`` / ``agent_spec_revision.spec_sha256`` 那一列,
    不是这里现算的。**列才是真源,重算只用于比对**:T5b 的读回宽容
    (:mod:`expert_work.persistence.stored_spec`)会在存量 manifest 多出未知键时
    剔掉它们,``compute_spec_sha256(spec)`` 算出来的是**剔完键之后**的哈希 ——
    回滚窗口里这个值不再等于库里那一列(N+1 写的、含那个键)。而
    ``run.agent_spec_sha256`` 与 ``agent_spec_revision.spec_sha256`` 的等值
    join 正是靠那一列撑住的契约(见模块 docstring)。两者一旦分叉,说明宽容
    在这次执行路径上真的生效了——这是唯一的信号,所以要打 warning(键名带
    ``spec_sha256_diverged``,``stored`` / ``computed`` 两个哈希都进日志;内容
    哈希不是秘密,只有 ``spec_json`` 里的值才需要避)。

    ``stored_sha256`` 为空串时回退用现算值,且不打日志:这条分支在本仓库里
    **今天到不了**——``AgentSpecDraft.spec_sha256`` 与 ``AgentSpecRecord.spec_sha256``
    都钉了 ``Field(min_length=64, max_length=64)``,调用方手里的
    ``record.spec_sha256`` / ``record.draft.spec_sha256`` 要么是合法的 64 位值,
    要么在读回那一步就已经 ``ValidationError`` 了,轮不到传空串进这里。留着
    这条回退,是为了给「调用方以后手里压根没有那一列」这个假想形态一个确定
    语义——退回重算,而不是往 ``run.agent_spec_sha256`` 里写一个空串。

    ``runs`` 为 ``None`` 时直接返回:那是没接持久化的 :class:`RunManager`
    (纯内存注册表),压根没有一行可以标注 —— 与「有行但写失败」是两回事,
    后者会记日志。

    ``source`` 只进日志,用来一眼看出是哪个执行入口没绑上。
    """
    logger = logging.getLogger(f"expert_work.control_plane.{source}")
    if runs is None:
        return
    try:
        computed = compute_spec_sha256(spec)
        bound = stored_sha256 or computed
        if stored_sha256 and stored_sha256 != computed:
            logger.warning(
                "%s.spec_sha256_diverged run_id=%s stored=%s computed=%s",
                source,
                run_id,
                stored_sha256,
                computed,
            )
        ok = await runs.set_agent_spec_sha256(
            run_id=run_id,
            tenant_id=tenant_id,
            agent_spec_sha256=bound,
        )
    except Exception:
        logger.warning("%s.spec_bind_failed run_id=%s", source, run_id, exc_info=True)
        return
    if not ok:
        logger.warning("%s.spec_bind_missed run_id=%s", source, run_id)
