"""把 ``agent_run.enqueued_input`` 还原成图输入 —— **两个调用方唯一的来源**。

出队(:mod:`control_plane.run_queue_worker`)与孤儿重收
(:mod:`control_plane.orphan_sweep`,B-58)都要做这件事。抽出来的理由不是去重,
是这段还原**有静默丢东西的前科**:``run_queue_worker`` 里那两处注释记着
``document_names`` / ``image_refs`` 各漏过一次回读,而漏读不报错 —— 附件就那么
没了。再抄一份等于再开一次同样的口子,而且第二份还没人盯。

刻意不放进 ``api/runs.py``:那个文件 2756 行,早已过了仓内 800 行的上限。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from langchain_core.messages import messages_from_dict

from control_plane.api.runs import build_run_graph_input, replay_graph_input


def graph_input_from_enqueued(built: Any, payload: dict[str, Any], run_id: UUID) -> Any:
    """``enqueued_input`` → 图输入。``payload`` 为空时返回 ``None``。

    返回 ``None`` 表示**没有可重放的东西**,调用方必须自己决定怎么办 ——
    别把它再喂给 ``graph.astream``:``input=None`` 在 LangGraph 里是「从检查点
    续跑」的意思,没有检查点时它会抛一句与事实矛盾的
    ``EmptyInputError: Received no input for __start__``(B-58 的整个根因)。
    """
    if not payload:
        return None

    replay = payload.get("replay_messages")
    if replay:
        # P-1 ``:regenerate`` queue 模式:入队时把旧轮的
        # [System, Human, (B-67 本轮输入段)?] 原件序列化进来。
        return replay_graph_input(built, messages_from_dict(replay), run_id=run_id)

    return build_run_graph_input(
        built,
        input_text=payload.get("input"),
        image_refs=list(payload.get("image_refs") or []),
        untrusted_content=payload.get("untrusted_content"),
        inputs=payload.get("inputs") or {},
        run_id=run_id,
        document_names=list(payload.get("document_names") or []),
    )
