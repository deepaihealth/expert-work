"""知识库智能体层评测的答题循环:与生产同一个 ``knowledge_search`` / ``knowledge_read``,结果经
工具节点同一段代码交给模型;每题最多搜 / 读几次。从 ``kb_agent_eval.py`` 拆出(B-206 11-09)。"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parent))

from kb_agent_cost import Spend  # type: ignore[import-not-found]
from kb_agent_models import READ_TOOL, Models  # type: ignore[import-not-found]
from kb_agent_prod_prompt import (  # type: ignore[import-not-found]
    ProductionPrompt,
    load_production_prompt,
    tool_result,
    turn_start,
)
from kb_eval import tool_message_text  # type: ignore[import-not-found]

from expert_work.common.spotlight import SPOTLIGHT_SYSTEM_CLAUSE
from orchestrator.llm.providers.openai import _to_openai_tool
from orchestrator.tools.registry import ToolContext, ToolResult, ToolSpec

AGENT_SYSTEM = (
    "你是一家公司的客服助手。回答客户的问题时,先用 knowledge_search 查公司知识库,再回答。"
)
MAX_SEARCHES = 3
#: B-206 K-2: knowledge_read 单独计数, 不占检索次数。
MAX_READS = 3
#: 模型最多回几轮(一轮可并发多个工具调用);B-206 K-2 起 6 -> 8, 容得下 3 次检索 + 3 次读文档。
MAX_CALLS = 8
#: B-206 11-09:只说上限到了、这次没执行,不再加「answer with what you have」——生产里没有这条
#: 消息,那句话是评测独有的「有就答」压力(11-05 研究记录)。
SEARCH_LIMIT_NOTE = "[search limit reached in this evaluation; this search was not run]"
READ_LIMIT_NOTE = "[read limit reached in this evaluation; this read was not run]"


# ---------------------------------------------------------------------------
# 答题
# ---------------------------------------------------------------------------


class Tool(Protocol):
    """``knowledge_search`` / ``knowledge_read``(或测试替身)。"""

    @property
    def spec(self) -> ToolSpec:
        """工具声明(名字、说明、参数)。"""

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        """执行一次调用。"""


@dataclass
class _RecordingTool:
    """把工具的 ``ToolResult`` 留一份(判分与结果文件用);模型看到的仍是工具节点渲染的那段。"""

    tool: Tool
    results: list[ToolResult] = field(default_factory=list)

    @property
    def spec(self) -> ToolSpec:
        return self.tool.spec

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        result = await self.tool.call(args, ctx=ctx)
        self.results.append(result)
        return result


def _call_args(call: Mapping[str, Any]) -> dict[str, Any]:
    try:
        args = json.loads(call["function"]["arguments"] or "{}")
    except json.JSONDecodeError:
        args = {}
    return args if isinstance(args, dict) else {}


async def _search(
    recorder: _RecordingTool, call: Mapping[str, Any], *, done: int, tenant_id: UUID
) -> tuple[str, dict[str, Any]]:
    """一次工具调用:``(给模型的文本, 记进结果文件的这次检索)``。"""
    args = _call_args(call)
    if done >= MAX_SEARCHES:
        return SEARCH_LIMIT_NOTE, {"query": args.get("query"), "skipped": True}
    before = len(recorder.results)
    rendered = await tool_message_text(
        recorder, args, tenant_id=tenant_id, call_id=str(call.get("id") or "call")
    )
    if len(recorder.results) == before:  # 参数不合格 / 工具报错:模型看到的就是那条报错
        return rendered, {"query": args.get("query"), "error": rendered}
    result = recorder.results[-1]
    return rendered, {
        "query": args.get("query"),
        "limit": args.get("limit"),
        "status": result.meta.get("status"),
        "degraded": bool(result.meta.get("rerank_degraded")),
        # B-206 K-8: the query was not embedded, this search ran on keywords alone.
        "embedding_degraded": bool(result.meta.get("embedding_degraded")),
        # B-206 K-22: the keyword path failed, this search ran on vectors alone.
        "keyword_degraded": bool(result.meta.get("keyword_degraded")),
        # B-206 K-7: how the rerank ended. The eval's base always asks for rerank, so a
        # result without the key ended ``ok``; K-7b: the base's threshold left out
        # (set under another model).
        "rerank_outcome": result.meta.get("rerank_outcome", "ok"),
        "rerank_threshold_paused": bool(result.meta.get("rerank_threshold_paused")),
        "content": result.content,
    }


async def _read(
    recorder: _RecordingTool, call: Mapping[str, Any], *, done: int, tenant_id: UUID
) -> tuple[str, dict[str, Any]]:
    """一次 knowledge_read(B-206 K-2):``(给模型的文本, 记进结果文件的这次读取)``。"""
    args = _call_args(call)
    if done >= MAX_READS:
        return READ_LIMIT_NOTE, {"args": args, "skipped": True}
    before = len(recorder.results)
    rendered = await tool_message_text(
        recorder, args, tenant_id=tenant_id, call_id=str(call.get("id") or "call")
    )
    if len(recorder.results) == before:
        return rendered, {"args": args, "error": rendered}
    meta = recorder.results[-1].meta
    return rendered, {
        "args": args,
        "status": meta.get("status"),
        "mode": meta.get("mode"),
        "page": meta.get("page"),
        "pages": meta.get("pages"),
        "passages": len(meta.get("passages") or ()),
        "content": recorder.results[-1].content,
    }


async def run_agent(
    models: Models,
    tool: Tool,
    *,
    question: str,
    tenant_id: UUID,
    spend: Spend,
    reader: Tool | None = None,
    prompt: ProductionPrompt | None = None,
) -> tuple[dict[str, Any], float]:
    """答一道题:``({answer, searches, reads}, 答题花费)``。``reader``(B-206 K-2)给了就把
    knowledge_read 一起给模型:调用名是它的走它、记进 ``reads``, 其余都当检索(与之前一样)。
    两种各有各的次数上限。``prompt``:照生产加的提示段(``kb_agent_prod_prompt``,不给就按本分支的
    产品代码现取;产品模块不在时什么都不加)。"""
    recorder = _RecordingTool(tool)
    reading = _RecordingTool(reader) if reader is not None else None
    tools = [_to_openai_tool(tool.spec)]
    if reader is not None:
        tools.append(_to_openai_tool(reader.spec))
    if prompt is None:
        prompt = load_production_prompt(t["function"]["name"] for t in tools)
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": prompt.system(AGENT_SYSTEM + "\n\n" + SPOTLIGHT_SYSTEM_CLAUSE),
        },
        {"role": "user", "content": question},
    ]
    # 交给产品「答前核对」函数的本轮视图:问题 + 每次成功的知识库结果(生产的形状)。
    product_view = turn_start(question)
    searches: list[dict[str, Any]] = []
    reads: list[dict[str, Any]] = []
    answer = ""
    cost = 0.0
    for _ in range(MAX_CALLS):
        message, call_cny = await models.agent(prompt.view(messages, product_view), tools)
        cost += call_cny
        spend.answer_cny += call_cny
        spend.answer_calls += 1
        calls = message.get("tool_calls") or []
        if not calls:
            answer = message.get("content") or ""
            break
        messages.append(
            {"role": "assistant", "content": message.get("content") or "", "tool_calls": calls}
        )
        for call in calls:
            if reading is not None and call["function"]["name"] == READ_TOOL:
                used, done = reading, len(reading.results)
                rendered, record = await _read(reading, call, done=len(reads), tenant_id=tenant_id)
                reads.append(record)
            else:
                used, done = recorder, len(recorder.results)
                rendered, record = await _search(
                    recorder, call, done=len(searches), tenant_id=tenant_id
                )
                searches.append(record)
            if len(used.results) > done:  # 工具真跑了、返回了结果(报错 / 超次数的不算)
                product_view.append(
                    tool_result(used.spec.name, str(call["id"]), rendered, used.results[-1].meta)
                )
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": rendered})
    return {"answer": answer, "searches": searches, "reads": reads}, cost
