"""知识库智能体层评测的答题循环照生产加的两段提示(B-206 #2066),与工具说明一样直接取自产品代码。

* **外发参数段**(``orchestrator.graph_builder.outgoing_reference``):生产只给有外发工具的构建
  (``sends_arguments_out``),加在系统提示词里 ``PLATFORM_CONTEXT_SYSTEM_CLAUSE`` 之后;评测智能体有
  ``knowledge_search``,按同一个门控函数判。
* **答前核对**(``orchestrator.graph_builder.answer_check``):本轮有知识库结果显示了段落之后,
  每一步都把 ``answer_check_note`` 包成 ``<platform-context>``(``wrap_platform_context``)贴在那次
  调用视图最后一条消息末尾,只进视图、不存。判「显示了段落」直接调产品函数:评测把每次成功的
  检索 / 读文档按生产同样的形状(``ToolMessage(name=工具名, artifact=meta)``)交给它;报错的调用
  在生产里是 ``status="error"`` 的结果、不算,评测里干脆不交。带平台段时系统提示词同时带上解释
  这个标签的 ``PLATFORM_CONTEXT_SYSTEM_CLAUSE``(生产里一直有)。

产品模块不在(改动前的 main)时什么都不加,答题的消息与之前逐字节相同 —— 基线那一臂不受影响。
加了哪几段记进 run 行的 ``prod_prompt_parts``,报告据此说清每一臂量的是什么。
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage

_ANSWER_CHECK = "orchestrator.graph_builder.answer_check"
_PLATFORM_CONTEXT = "orchestrator.graph_builder.platform_context"
_OUTGOING = "orchestrator.graph_builder.outgoing_reference"

Importer = Callable[[str], Any]


@dataclass(frozen=True)
class ProductionPrompt:
    """这一臂从产品代码取到的提示段;``parts`` 记进 run 行。"""

    parts: tuple[str, ...] = ()
    system_suffix: str = ""
    answer_check: Callable[[Sequence[BaseMessage]], str | None] | None = None
    wrap: Callable[[str], str] | None = None

    def system(self, base: str) -> str:
        return base + self.system_suffix

    def view(
        self, messages: list[dict[str, Any]], product_view: Sequence[BaseMessage]
    ) -> list[dict[str, Any]]:
        """这次调用发给模型的消息:有答前核对就贴在最后一条用户 / 工具消息末尾(副本),
        否则原样返回 ``messages`` 本身。"""
        if self.answer_check is None or self.wrap is None:
            return messages
        note = self.answer_check(product_view)
        if note is None:
            return messages
        block = self.wrap(note)
        last = messages[-1]
        if last["role"] in ("user", "tool") and isinstance(last.get("content"), str):
            return [*messages[:-1], {**last, "content": last["content"] + "\n\n" + block}]
        return [*messages, {"role": "user", "content": block}]


BASELINE = ProductionPrompt()


def _optional(name: str, importer: Importer) -> Any | None:
    """产品模块;**它自己**不存在时 ``None``(它的依赖缺了照常报错,别把问题藏起来)。"""
    try:
        return importer(name)
    except ModuleNotFoundError as exc:
        if exc.name == name:
            return None
        raise


def load_production_prompt(
    tool_names: Iterable[str], *, importer: Importer = importlib.import_module
) -> ProductionPrompt:
    names = list(tool_names)
    parts: list[str] = []
    suffix = ""
    answer_check = wrap = None
    checker = _optional(_ANSWER_CHECK, importer)
    if checker is not None:
        platform = importer(_PLATFORM_CONTEXT)
        parts.append("answer_check")
        suffix += "\n\n" + platform.PLATFORM_CONTEXT_SYSTEM_CLAUSE
        answer_check, wrap = checker.answer_check_note, platform.wrap_platform_context
    outgoing = _optional(_OUTGOING, importer)
    if outgoing is not None and outgoing.sends_arguments_out(names):
        parts.append("outgoing_reference")
        suffix += "\n\n" + outgoing.OUTGOING_ARGUMENTS_CLAUSE
    if not parts:
        return BASELINE
    return ProductionPrompt(
        parts=tuple(parts), system_suffix=suffix, answer_check=answer_check, wrap=wrap
    )


def turn_start(question: str) -> list[BaseMessage]:
    """交给产品函数的本轮视图的开头:用户的问题。"""
    return [HumanMessage(content=question)]


def tool_result(name: str, call_id: str, content: str, meta: dict[str, Any]) -> ToolMessage:
    """一次成功的工具调用,按生产工具节点同样的形状(``artifact`` = ``ToolResult.meta``,
    空则 None)。"""
    return ToolMessage(
        content=content, tool_call_id=call_id, name=name, artifact=dict(meta) or None
    )


__all__ = ["BASELINE", "ProductionPrompt", "load_production_prompt", "tool_result", "turn_start"]
