"""B-236 第二层 —— run 开始时的开场复制节点(``START → conversation_seed → agent``)。

复制本身见 :mod:`orchestrator.tools.conversation_seed`。本节点只管门与收尾:

* 只在**对话视图**里做(``workspace_layout == "conversation"`` 且绑了 agent、有对话 id),
  委派子代(``configurable["child_run"]``)不做 —— 与 ``inputs_node`` / ``workspace_ingest``
  同一道闸,子代用的是父对话的目录,父开场时已经复制过;
* **永不让 run 失败**:任何异常只记日志(异常类型,不带路径 —— 路径里有客户起的目录名)
  和计数,返回 ``{}``;没写标记,下一次 run 再试;
* 超出上限没复制的目录写进 ``seed_skipped_dirs``,工作区快照段每轮据此告诉模型它们只在
  ``/agent/<目录>/``。之后的 run 标记已在,节点什么都不返回,状态里的值随检查点留着。
  配额满了整个没复制时另置 ``seed_quota_blocked``,快照段换一句说明。
* 状态里的名单有上限(:data:`MAX_REPORTED_DIRS` 个、每个 :data:`MAX_REPORTED_NAME_CHARS`
  字符):名字是沙箱能起的,它们进检查点、每轮进提示词。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.runnables import RunnableConfig

from expert_work.common.observability import (
    ExpertWorkComponent,
    expert_work_counter,
    expert_work_span,
)
from orchestrator.graph_builder._config import cancellation_token, configurable_uuid
from orchestrator.graph_builder.memory import MemoryNode
from orchestrator.state import AgentState
from orchestrator.tools.conversation_seed import (
    MAX_REPORTED_DIRS,
    SeedQuotaGate,
    seed_conversation,
)
from orchestrator.tools.locks import KeyedLock
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from orchestrator.tools.registry import ToolContext, workspace_view_fields

logger = logging.getLogger(__name__)

#: 记进状态的没复制目录名最多多长(review 7)。个数上限 :data:`MAX_REPORTED_DIRS` 在复制那一侧
#: 就收住了。快照段渲染时还会再收一遍。
MAX_REPORTED_NAME_CHARS = 120

conversation_seed_total = expert_work_counter(
    "expert_work_conversation_seed_total",
    "B-236 conversation-start copies of the agent's base directories, by outcome "
    "(seeded = copied and marked; failed = no marker written, retried next run).",
    ("outcome",),
)


def make_conversation_seed_node(
    *, store: NasWorkspaceStore, lock: KeyedLock, quota: SeedQuotaGate | None = None
) -> MemoryNode:
    """构造开场复制节点。``store`` 必须是 NAS 实现(复制按目录 fd 在宿主侧做)。"""

    async def conversation_seed_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        del state
        token = cancellation_token(config)
        token.raise_if_cancelled()
        configurable = config.get("configurable") or {}
        if configurable.get("child_run"):
            return {}
        tenant_id = configurable_uuid(config, "tenant_id")
        user_id = configurable_uuid(config, "user_id")
        view = ToolContext(**workspace_view_fields(configurable)).workspace_view
        if tenant_id is None or user_id is None or view.conv_id is None:
            return {}
        with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "conversation_seed"):
            try:
                report = await seed_conversation(
                    store,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    agent_key=view.agent_key,
                    conv_id=view.conv_id,
                    lock=lock,
                    quota=quota,
                )
            except Exception as exc:
                conversation_seed_total.labels(outcome="failed").inc()
                logger.warning(
                    "conversation_seed.failed agent_key=%s error=%s",
                    view.agent_key,
                    type(exc).__name__,
                )
                return {}
        if report is None:
            return {}
        conversation_seed_total.labels(outcome="seeded").inc()
        logger.info(
            "conversation_seed.done agent_key=%s copied=%d too_large=%d existing=%d "
            "quota=%d skipped_entries=%d bytes=%d",
            view.agent_key,
            len(report.copied),
            len(report.skipped_too_large),
            len(report.skipped_existing),
            len(report.skipped_quota),
            report.skipped_entries,
            report.bytes_copied,
        )
        if report.skipped_quota:
            return {
                "seed_skipped_dirs": _bounded(report.skipped_quota),
                "seed_quota_blocked": True,
            }
        return {"seed_skipped_dirs": _bounded(report.skipped_too_large)}

    return conversation_seed_node


def _bounded(names: tuple[str, ...]) -> list[str]:
    return [n[:MAX_REPORTED_NAME_CHARS] for n in names[:MAX_REPORTED_DIRS]]
