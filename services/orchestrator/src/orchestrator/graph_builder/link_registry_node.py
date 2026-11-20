"""B-330 —— run-start 节点:本轮模板变量里的链接记进对话的链接登记(``conversation_links``)。

纯计算、不碰沙箱;门在 ``agent_factory``(声明了模板变量就装,不要求接沙箱 —— 只有 MCP 的
智能体也要能用)。本节点自己再收窄:

* 委派子 run 跳过 —— 子代用父种进来的登记(``_child_run`` 的 ``child_input``);
* 编号前缀取**根对话**的 id:``workspace_thread_id or thread_id``(``_child_run`` 同一取法),
  绝不用子 run 新建的线程 id;
* 没有对话 id(定时任务的临时线程也有自己的 id,登记只在那里)或本轮没传值 → 什么都不做;
* **永不让 run 失败**:任何异常只记异常类型(不记值),返回 ``{}``。

审批续跑不经过开头节点(登记不会重算);孤儿复活再跑一次,按原文去重,结果不变。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig

from expert_work.protocol import PromptVariableSpec
from orchestrator.graph_builder._config import configurable_uuid
from orchestrator.graph_builder.memory import MemoryNode
from orchestrator.sse import PROMPT_INPUTS_KEY
from orchestrator.state import AgentState
from orchestrator.tools.link_registry import count_registration, register_turn, valid_links
from orchestrator.tools.registry import WORKSPACE_THREAD_ID_KEY

logger = logging.getLogger(__name__)


def make_link_registry_node(*, variables: tuple[PromptVariableSpec, ...]) -> MemoryNode:
    """构造开头节点。``variables`` 只用来认哪些变量是 trusted(untrusted 的不记扩展名)。"""
    trusted = frozenset(var.name for var in variables if var.trusted)

    async def link_registry_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        try:
            configurable = config.get("configurable") or {}
            if configurable.get("child_run"):
                return {}
            raw_inputs = configurable.get(PROMPT_INPUTS_KEY)
            if not isinstance(raw_inputs, Mapping) or not raw_inputs:
                return {}
            conversation = configurable_uuid(config, WORKSPACE_THREAD_ID_KEY) or configurable_uuid(
                config, "thread_id"
            )
            if conversation is None:
                return {}
            existing = valid_links(state.get("conversation_links"))
            images = state.get("turn_image_refs")
            outcome = register_turn(
                existing,
                inputs=raw_inputs,
                trusted_names=trusted,
                conversation_id=conversation,
                run_id=configurable_uuid(config, "run_id"),
                now=datetime.now(UTC),
                turn_documents=state.get("turn_documents") or (),
                turn_image_count=len(images) if isinstance(images, list) else 0,
            )
            count_registration(outcome)
            logger.info(
                "links.registered added=%d total=%d dropped_cap=%d dropped_depth=%d",
                outcome.added,
                len(outcome.links) if outcome.links is not None else len(existing),
                outcome.dropped_cap,
                outcome.dropped_depth,
            )
            if outcome.links is None:
                return {}
            return {"conversation_links": outcome.links}
        except Exception as exc:
            logger.warning("links.register_failed err=%s", type(exc).__name__)
            return {}

    return link_registry_node
