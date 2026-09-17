"""一轮对话的 Dynamic-Prompt 原始 inputs —— 续跑 / 复活 / 重新生成都从这里取。

B-61 之后 inputs 不只渲染提示词:``inputs`` 节点把它写成
``inputs/<run_id>/inputs.json``,``tools_node`` 用它填平台绑定的工具参数。两者都读
``configurable[PROMPT_INPUTS_KEY]``,而那个值只有「新开一轮」的入口手里有(请求体)。
下面三条路径手里没有,必须找回**这一轮**当初的那一份:

* **审批续跑** —— 新 run_id、``graph_input=None``。拿空 inputs 重填,被绑参数被摘掉,
  RT-6 摘要对不上,一次正常的批准变成完整性否决;沙箱的 ``EXPERT_WORK_INPUTS`` 指向
  ``inputs/<续跑 run_id>/``,那里什么都没有(inputs 节点只在一轮开头跑)。
* **孤儿复活** —— 同 run_id,但可能在另一个副本上。
* **``:regenerate``** —— 新的一轮,输入就是被取代那一轮的原件,inputs 也该是那一份。

**来源:这一轮第一个 run 的 ``system_prompt`` 帧**(BUG-16 起它就带着 ``inputs``)。
选它而不是检查点里新开一个通道,理由三条:

1. **按 run_id 取,不会串轮。** 检查点里的通道是「整个会话最后写的那一份」,
   而会话是长线程,「同一个会话里每一轮 inputs 指向不同的业务对象」是对接方的真实用法。
   任何一轮没写通道(回滚期间的旧版本、滚动发布时的旧副本),续跑就会拿到**上一轮**
   的值去填工具参数 —— 这比拿空值严重得多,而且没有任何信号。帧是按 run_id 存的,
   取错轮在结构上不可能。
2. **老数据也有。** 帧从 BUG-16 起就在写,发布之前暂停的轮、之前跑过的轮,
   续跑 / 重新生成都能取到;新通道只对发布之后的轮有效。
3. **不改图的输入、不改检查点形状。**

「这一轮第一个 run」沿审批链往前找(``supersede._approval_chain``,与重新生成定轮
用的是同一段代码):前驱是 PAUSED、且它那张审批单的 ``continuation_run_id`` 正是
后一个 run。审批单与 run 行都是持久的,另一个副本一样读得到。

**已知代价**:帧是后台批量落库的(H-7,写失败只记指标不阻断 run)。帧丢了,这里取到
空 inputs,效果等于修复之前 —— 被绑参数被摘掉、审批续跑被判完整性否决(有审计行);
不会拿错值。帧缺失时记一条 warning(只有 id,没有值)。

inputs 是客户数据:本模块的日志只记 run_id,不记任何值、不记变量名。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from control_plane.supersede import _approval_chain
from expert_work.persistence import ApprovalStore
from expert_work.runtime.runs import RunEventStore, RunStore
from orchestrator.sse import SYSTEM_PROMPT_EVENT

logger = logging.getLogger(__name__)

__all__ = ["TurnInputs", "resolve_turn_inputs"]


@dataclass(frozen=True)
class TurnInputs:
    """一轮的原始 inputs,以及它们落在谁名下。"""

    #: 这一轮第一个 run。``inputs/<它>/inputs.json`` 就在它名下,续跑段的沙箱要指向它。
    root_run_id: UUID
    #: 那个 run 开跑时的 ``RunRequest.inputs``,原样(与 ``system_prompt`` 帧同一份)。
    inputs: dict[str, Any] = field(default_factory=dict)


async def resolve_turn_inputs(
    *,
    run_id: UUID,
    thread_id: UUID,
    tenant_id: UUID,
    runs: RunStore | None,
    approvals: ApprovalStore | None,
    event_store: RunEventStore | None,
) -> TurnInputs:
    """``run_id`` 所在那一轮的原始 inputs。

    ``run_id`` 可以是这一轮里的任意一段(首段、审批续跑段)。取不到(没接持久化、
    run 不在这个会话里、帧缺失、帧里没有 ``inputs``)一律返回空 inputs ——
    与这一轮本来就没有 inputs 同一个结果。存储层的异常照常抛出:调用方都在副作用
    之前调这里,抛出去比静默拿空值去跑更好。
    """
    if runs is None or event_store is None:
        return TurnInputs(root_run_id=run_id)
    rows = await runs.list_by_thread(thread_id=thread_id, tenant_id=tenant_id)
    target = next((r for r in rows if r.run_id == run_id), None)
    if target is None:
        return TurnInputs(root_run_id=run_id)
    root = run_id
    if approvals is not None:
        chain = await _approval_chain(target, rows, approvals=approvals, tenant_id=tenant_id)
        root = chain[-1]
    frames = await event_store.list(run_id=root, event_names=(SYSTEM_PROMPT_EVENT,), limit=1)
    if not frames:
        # 一轮的第一个 run 必然发过这一帧(``run_agent`` 在第一个业务帧之前发)。
        # 没有 = 落库丢了,或审批链没串上(root 其实是个续跑段)。两种都要看得见。
        logger.warning("turn_inputs.prompt_frame_missing run_id=%s root_run_id=%s", run_id, root)
        return TurnInputs(root_run_id=root)
    return TurnInputs(root_run_id=root, inputs=_inputs_of(frames[0].data))


def _inputs_of(data: object) -> dict[str, Any]:
    """帧里的 ``inputs``;形状不对就当没有(帧是我们自己写的,这里只防坏数据)。"""
    if not isinstance(data, Mapping):
        return {}
    raw = data.get("inputs")
    if not isinstance(raw, Mapping):
        return {}
    return dict(raw)
