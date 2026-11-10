"""kb_agent_prod_prompt.py:答题循环照生产加「答前核对」平台段与「外发参数」系统提示段(B-206
#2066)。产品模块不在(基线 main)时消息逐字节不变;在时只在本轮已有显示了段落的知识库结果之后
的每次调用上、贴在那次调用视图最后一条消息末尾,不进存下来的对话。全部离线,假模型。"""

from __future__ import annotations

import copy
import importlib
import json
from collections.abc import Mapping, Sequence
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from kb_agent_answer import AGENT_SYSTEM, run_agent  # type: ignore[import-not-found]
from kb_agent_cost import Spend  # type: ignore[import-not-found]
from kb_agent_prod_prompt import (  # type: ignore[import-not-found]
    BASELINE,
    ProductionPrompt,
    load_production_prompt,
)
from langchain_core.messages import BaseMessage, ToolMessage

from expert_work.common.spotlight import SPOTLIGHT_SYSTEM_CLAUSE
from orchestrator.tools.registry import ToolContext, ToolResult, ToolSpec

_NOTE = "[answer check] NOTE"
_CLAUSE = "# Tool arguments\nSELF-CONTAINED"
_PLATFORM = "PLATFORM CONTEXT CLAUSE"


def _wrap(body: str) -> str:
    return f"<platform-context>\n(label)\n{body}\n</platform-context>"


class _Product:
    """Stand-ins for the #2066 modules; the answer check records what it was given."""

    def __init__(self, *, sends_out: bool = True) -> None:
        self.seen: list[list[BaseMessage]] = []
        self.sends_out = sends_out

    def answer_check_note(self, messages: Sequence[BaseMessage]) -> str | None:
        self.seen.append(list(messages))
        for m in messages:
            artifact = m.artifact if isinstance(m, ToolMessage) else None
            if isinstance(artifact, dict) and (artifact.get("hits") or 0) > 0:
                return _NOTE
        return None

    def importer(self, name: str) -> Any:
        modules = {
            "orchestrator.graph_builder.answer_check": SimpleNamespace(
                answer_check_note=self.answer_check_note
            ),
            "orchestrator.graph_builder.platform_context": SimpleNamespace(
                PLATFORM_CONTEXT_SYSTEM_CLAUSE=_PLATFORM, wrap_platform_context=_wrap
            ),
            "orchestrator.graph_builder.outgoing_reference": SimpleNamespace(
                OUTGOING_ARGUMENTS_CLAUSE=_CLAUSE,
                sends_arguments_out=lambda names: self.sends_out and "knowledge_search" in names,
            ),
        }
        return modules[name]


def _absent(name: str) -> ModuleType:
    if name in (
        "orchestrator.graph_builder.answer_check",
        "orchestrator.graph_builder.outgoing_reference",
    ):
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)
    return importlib.import_module(name)


class _Search:
    """``knowledge_search`` stand-in: the n-th call returns ``metas[n]`` (``None`` = raise)."""

    def __init__(self, metas: list[dict[str, Any] | None]) -> None:
        self.metas = metas
        self.calls = 0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="knowledge_search", description="search", parameters={})

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        meta = self.metas[self.calls]
        self.calls += 1
        if meta is None:
            raise RuntimeError("search failed")
        return ToolResult(content=f"[D1#1] result {self.calls}", meta=meta)


class _Scripted:
    """Searches ``searches`` times, then answers; keeps a deep copy of every call's messages."""

    answer_model = "scripted"
    judge_model = "scripted"

    def __init__(self, searches: int) -> None:
        self.searches = searches
        self.views: list[list[dict[str, Any]]] = []

    async def agent(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], float]:
        self.views.append(copy.deepcopy(messages))
        n = sum(1 for m in messages if m["role"] == "tool")
        if n >= self.searches:
            return {"role": "assistant", "content": "答完了"}, 0.0
        fn = {"name": "knowledge_search", "arguments": json.dumps({"query": f"q{n}"})}
        call = {"id": f"c{n}", "type": "function", "function": fn}
        return {"role": "assistant", "content": "", "tool_calls": [call]}, 0.0

    async def judge(self, prompt: str) -> tuple[str, float]:
        return "{}", 0.0


async def _answer(
    metas: list[dict[str, Any] | None], prompt: ProductionPrompt
) -> tuple[_Scripted, dict[str, Any]]:
    models = _Scripted(searches=len(metas))
    run, _cost = await run_agent(
        models,
        _Search(metas),
        question="甲款的价格是多少?",
        tenant_id=uuid4(),
        spend=Spend(),
        prompt=prompt,
    )
    return models, run


def _count(view: list[dict[str, Any]], text: str) -> int:
    return sum(str(m.get("content") or "").count(text) for m in view)


# ---------------------------------------------------------------------------
# 基线:产品模块不在
# ---------------------------------------------------------------------------


def test_without_the_product_modules_nothing_is_added() -> None:
    prompt = load_production_prompt(["knowledge_search"], importer=_absent)
    assert prompt == BASELINE
    assert prompt.parts == ()


@pytest.mark.asyncio
async def test_the_baseline_messages_are_byte_identical_to_before() -> None:
    models, _run = await _answer([{"hits": 2, "status": "ok"}, {"hits": 1}], BASELINE)
    system = models.views[0][0]
    assert system == {"role": "system", "content": AGENT_SYSTEM + "\n\n" + SPOTLIGHT_SYSTEM_CLAUSE}
    # 每次调用看到的 = 存下来的对话本身(上一次调用的视图 + 新的助手 / 工具消息)
    for before, after in zip(models.views, models.views[1:], strict=False):
        assert after[: len(before)] == before
    assert models.views[-1][-1] == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": models.views[-1][-1]["content"],
    }
    assert all(_count(v, "platform-context") == 0 for v in models.views)


def test_an_unrelated_import_error_inside_a_product_module_is_not_swallowed() -> None:
    def broken(name: str) -> ModuleType:
        raise ModuleNotFoundError("No module named 'some_dependency'", name="some_dependency")

    with pytest.raises(ModuleNotFoundError, match="some_dependency"):
        load_production_prompt(["knowledge_search"], importer=broken)


# ---------------------------------------------------------------------------
# #2066 在:系统提示段 + 答前核对
# ---------------------------------------------------------------------------


def test_present_modules_add_the_system_sections_in_production_order() -> None:
    product = _Product()
    prompt = load_production_prompt(["knowledge_search"], importer=product.importer)
    assert prompt.parts == ("answer_check", "outgoing_reference")
    assert prompt.system("BASE") == "BASE\n\n" + _PLATFORM + "\n\n" + _CLAUSE
    # 生产按工具门控:没有外发工具的构建不带「外发参数」段
    quiet = load_production_prompt(
        ["knowledge_search"], importer=_Product(sends_out=False).importer
    )
    assert quiet.parts == ("answer_check",)
    assert quiet.system("BASE") == "BASE\n\n" + _PLATFORM


@pytest.mark.asyncio
async def test_the_answer_check_rides_on_every_call_after_a_result_that_showed_passages() -> None:
    product = _Product()
    prompt = load_production_prompt(["knowledge_search"], importer=product.importer)
    models, run = await _answer([{"hits": 2, "status": "ok"}, {"hits": 1, "status": "ok"}], prompt)
    first, second, third = models.views
    assert first[0]["content"].endswith(_CLAUSE)
    assert _count(first, _NOTE) == 0  # 还没有知识库结果
    for view in (second, third):
        assert _count(view, _NOTE) == 1  # 每次调用恰好一份
        assert view[-1]["role"] == "tool"
        assert view[-1]["content"].endswith("\n\n" + _wrap(_NOTE))
    # 只进那次调用的视图:下一次调用里先前的工具消息是原样的
    assert third[2]["content"] == second[2]["content"].removesuffix("\n\n" + _wrap(_NOTE))
    assert _NOTE not in json.dumps(run, ensure_ascii=False)
    # 交给产品函数的是生产同样的形状:轮首用户消息 + 带 artifact(= meta)的知识库结果
    given = product.seen[-1]
    assert given[0].content == "甲款的价格是多少?"
    results = [m for m in given if isinstance(m, ToolMessage)]
    assert [(m.name, m.artifact) for m in results] == [
        ("knowledge_search", {"hits": 2, "status": "ok"}),
        ("knowledge_search", {"hits": 1, "status": "ok"}),
    ]


@pytest.mark.asyncio
async def test_no_answer_check_after_an_error_or_a_search_that_found_nothing() -> None:
    product = _Product()
    prompt = load_production_prompt(["knowledge_search"], importer=product.importer)
    models, _run = await _answer([None, {"hits": 0, "status": "no_results"}], prompt)
    assert all(_count(v, _NOTE) == 0 for v in models.views)
    # 报错的调用不进交给产品函数的结果(生产里是 status=error 的 ToolMessage,同样不算)
    assert [m.artifact for m in product.seen[-1] if isinstance(m, ToolMessage)] == [
        {"hits": 0, "status": "no_results"}
    ]


# ---------------------------------------------------------------------------
# 真的 #2066 模块(合进来之后 / 用 PYTHONPATH 指到它的分支时跑)
# ---------------------------------------------------------------------------

try:
    importlib.import_module("orchestrator.graph_builder.answer_check")
    _REAL = True
except ModuleNotFoundError:
    _REAL = False


@pytest.mark.skipif(not _REAL, reason="product answer_check module not on this branch")
@pytest.mark.asyncio
async def test_with_the_real_product_modules() -> None:
    prompt = load_production_prompt(["knowledge_search", "knowledge_read"])
    assert prompt.parts == ("answer_check", "outgoing_reference")
    models, _run = await _answer([{"hits": 2, "status": "ok"}, {"hits": 0}], prompt)
    assert "# Tool arguments" in models.views[0][0]["content"]
    assert [_count(v, "[answer check]") for v in models.views] == [0, 1, 1]
    assert all(v[-1]["content"].rstrip().endswith("</platform-context>") for v in models.views[1:])
