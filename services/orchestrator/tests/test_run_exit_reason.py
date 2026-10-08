"""B-85 ③ / B-84 第 3 条 —— run 的退出原因(``exit_reason``)与未解决工具失败通道。

**判据纪律**(spec `2026-09-20-run-completion-signal-design.md` §4):只用客观事实,
不推断模型意图。这里钉住的是「整个 run 里还有没有没被抵消掉的非 transient 失败」,
**不是**「模型是不是放弃了」—— 后者猜不准(模型合理地换个方法也长这样)。

测试照 ``test_recovery_advisory.py`` 的做法驱动**真图**:退出原因这件事的全部意义
就在于「从哪条路出去的」,用桩替掉图就等于替掉了被测对象。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.runs.schemas import compute_completed
from orchestrator import (
    AgentState,
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
)
from orchestrator.graph_builder.builder import (
    _apply_failure_ledger,
    _ledger_failure,
    _ledger_key_for,
    budget_exit_reason,
)
from orchestrator.tools.error_classifier import ClassifiedToolError, ToolErrorClass
from orchestrator.tools.file_ops import FileOpError

# ---------------------------------------------------------------------------
# 桩
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedLLM:
    responses: list[AIMessage]
    calls: int = 0

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del messages, tools
        idx = self.calls
        self.calls += 1
        # 撞预算的用例会比脚本多跑一轮(收尾轮),最后一条兜底重放。
        return self.responses[min(idx, len(self.responses) - 1)]


@dataclass
class _ScriptedTool:
    """``fail_on`` 里的第几次调用抛异常;``error`` 决定分类器判 transient 与否。"""

    name: str = "save_artifact"
    fail_on: frozenset[int] = frozenset()
    error: str = "disk full"
    #: 路径参数名 —— ``save_artifact`` 用 ``name``,工作区写工具用 ``path``。
    path_arg: str = "name"
    #: B-159 —— 声明为只读工具(``side_effect="read_only"``)。
    read_only: bool = False
    #: 失败时抛的异常类型;``FileNotFoundError`` 让分类器判 ``resource_not_found``。
    exc: type[Exception] = OSError
    calls: int = 0
    _seen: list[int] = field(default_factory=list)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description="scripted tool",
            parameters={
                "type": "object",
                "properties": {self.path_arg: {"type": "string"}},
                "required": [self.path_arg],
            },
            is_read_only=self.read_only,
            side_effect="read_only" if self.read_only else None,
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del ctx
        idx = self.calls
        self.calls += 1
        self._seen.append(idx)
        if idx in self.fail_on:
            raise self.exc(self.error)
        return ToolResult(content=f"Saved {args.get(self.path_arg)!r}.")


def _tc(name: str, call_id: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "args": {"name": "x.md"} if args is None else args,
        "id": call_id,
        "type": "tool_call",
    }


async def _run(llm: _ScriptedLLM, registry: ToolRegistry, *, max_steps: int = 5) -> AgentState:
    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(build_react_graph(llm_caller=llm, tool_registry=registry))
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        return await compiled.ainvoke(
            {"messages": [HumanMessage(content="start")], "step_count": 0, "max_steps": max_steps},
            config=cfg,
        )


def _failure_classes(state: AgentState) -> list[str]:
    return [f.error_class for f in state.get("unresolved_failures", [])]


def _failure_keys(state: AgentState) -> list[tuple[str, str | None]]:
    return [(f.tool_name, f.path) for f in state.get("unresolved_failures", [])]


# ---------------------------------------------------------------------------
# B-160 —— 欠账按轮记,不跨轮
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_ledger_outlives_a_turn_unless_the_next_input_clears_it() -> None:
    """同一对话(同一 thread)的第二轮没调任何工具。

    通道没有 reducer、``tools`` 节点只在跑过工具时才写它 —— 第二轮输入里不带这个键,
    第一轮的欠账就原样留在终局,第二轮被判没做完(B-140 h07 实测:第 2 轮
    ``edit_file`` 没还上,第 3 轮零失败也 ``completed=false``)。所以每一轮的图输入
    (control-plane ``build_run_graph_input`` / ``replay_graph_input``)都写空列表。
    """
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("save_artifact", "tc-1")]),
            AIMessage(content="没存上"),
            AIMessage(content="第二轮的答复"),
            AIMessage(content="第三轮的答复"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_ScriptedTool(fail_on=frozenset({0})))

    def _turn(text: str) -> dict[str, Any]:
        return {"messages": [HumanMessage(content=text)], "step_count": 0, "max_steps": 5}

    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=registry)
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        first = await compiled.ainvoke(_turn("a"), config=cfg)
        assert _failure_keys(first) == [("save_artifact", "x.md")]

        carried = await compiled.ainvoke(_turn("b"), config=cfg)
        assert _failure_keys(carried) == [("save_artifact", "x.md")], "前提:不清就跨轮"
        assert not compute_completed(
            exit_reason="text_response", unresolved_failures=carried["unresolved_failures"]
        )

        cleared = await compiled.ainvoke({**_turn("c"), "unresolved_failures": []}, config=cfg)
        assert _failure_keys(cleared) == []
        assert compute_completed(
            exit_reason="text_response", unresolved_failures=cleared["unresolved_failures"]
        )


# ---------------------------------------------------------------------------
# Task 1 —— unresolved_failures 通道
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_final_batch_failure_survives_to_the_end() -> None:
    """B-85 那次的形状:工具失败 → 模型不再调工具 → 图正常收尾。

    ``tool_failures`` 到这时已经被 ``agent_node`` 清空了(builder 里发完
    ``<recovery-advisory>`` 就重置),所以终局读它永远是空的 —— 这正是
    本通道存在的理由。
    """
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("save_artifact", "tc-1")]),
            AIMessage(content="我放弃了"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_ScriptedTool(fail_on=frozenset({0})))

    state = await _run(llm, registry)

    assert state.get("tool_failures", []) == [], "前提:tool_failures 按轮重置,终局拿不到"
    assert _failure_classes(state), "unresolved_failures 必须留到终局"


@pytest.mark.asyncio
async def test_a_same_key_retry_clears_the_debt() -> None:
    """批 1 失败 → 批 2 **同键**重试成功 → 结束。那条欠账必须被抵消掉。

    抵消是本条记账法的另一半:没有它,一个已经自我恢复的 run 会被一直判成
    没做成 —— **误报**。同键 = 同工具 + 同路径(这里两批都是
    ``save_artifact`` 写 ``x.md``)。
    """
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("save_artifact", "tc-1")]),
            AIMessage(content="", tool_calls=[_tc("save_artifact", "tc-2")]),
            AIMessage(content="done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_ScriptedTool(fail_on=frozenset({0})))  # 只有第一次失败

    state = await _run(llm, registry)

    assert "unresolved_failures" in state, "跑过工具就必须写,哪怕是空的"
    assert _failure_classes(state) == []
    assert (
        compute_completed(
            exit_reason=str(state.get("exit_reason")),
            unresolved_failures=state.get("unresolved_failures", []),
        )
        is True
    )


@pytest.mark.asyncio
async def test_a_rewrite_by_another_tool_clears_the_same_file() -> None:
    """**本次返工的那一格,走真图**:``edit_file`` 改 ``a.py`` 失败 →
    模型退回整份 ``write_file`` 写 ``a.py`` 成功 → 文字收尾。

    文件最后写成了,债就该还清。键按工具名记的话这里还不掉;取不到路径
    (``write_file`` / ``edit_file`` 不在 mutation 分类器里)同样还不掉 ——
    两条缺一不可,所以这一条同时钉住记账键的形状**和**路径提取的覆盖面。
    """
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("edit_file", "tc-1", {"path": "a.py"})]),
            AIMessage(content="", tool_calls=[_tc("write_file", "tc-2", {"path": "a.py"})]),
            AIMessage(content="改好了"),
        ]
    )
    registry = ToolRegistry()
    registry.register(
        _ScriptedTool(name="edit_file", path_arg="path", fail_on=frozenset({0}), error="no_match")
    )
    registry.register(_ScriptedTool(name="write_file", path_arg="path"))

    state = await _run(llm, registry)

    assert state.get("exit_reason") == "text_response"
    assert _failure_keys(state) == [], "同一份文件最后写成了,债必须还清"
    assert (
        compute_completed(
            exit_reason=str(state.get("exit_reason")),
            unresolved_failures=state.get("unresolved_failures", []),
        )
        is True
    )


@pytest.mark.asyncio
async def test_a_different_tool_succeeding_does_not_clear_the_debt() -> None:
    """**本 PR 的存在理由**:批 1 工具 A 失败 → 批 2 换工具 B 成功 → 文字收尾。

    只看「最后一批」的旧实现在这里判 ``completed=True`` —— 终局那一批确实是
    干净的,而工具 A 从头到尾没成功过。抵消必须**按键**,不是按批次:
    ``("save_artifact", "x.md")`` 的欠账,只有同键的成功才还得上,
    ``("web_search", None)`` 成功一百次也还不上。

    这正是 B-85 ③ 要防的那句「看起来完整的一段话把失败盖过去」。
    """
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("save_artifact", "tc-1")]),
            AIMessage(content="", tool_calls=[_tc("web_search", "tc-2")]),
            AIMessage(content="我已经把方案整理好了"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_ScriptedTool(fail_on=frozenset({0})))  # save_artifact,必失败
    registry.register(_ScriptedTool(name="web_search"))  # 换的那把,必成功

    state = await _run(llm, registry)

    assert state.get("exit_reason") == "text_response", "前提:模型是自然说完的"
    assert _failure_keys(state) == [("save_artifact", "x.md")]
    assert (
        compute_completed(
            exit_reason=str(state.get("exit_reason")),
            unresolved_failures=state.get("unresolved_failures", []),
        )
        is False
    )


@pytest.mark.asyncio
async def test_transient_failures_are_not_recorded() -> None:
    """transient 是可重试的抖动,不是「没做成」的证据。

    与 ``error_signal``(builder.py 的动态 effort 触发器)同一条谓词:
    ``error_class != "transient"``。

    **用只读工具**(``web_search``)而不是 ``save_artifact``:写类工具失败会被
    L-4 的 mutation 分类器先折成 ``mutation_not_landed``,那条路压根到不了
    transient 判定 —— 第一版拿 ``save_artifact`` 写,红在这里。
    """
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("web_search", "tc-1")]),
            AIMessage(content="done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(
        _ScriptedTool(name="web_search", fail_on=frozenset({0}), error="connection reset by peer")
    )

    state = await _run(llm, registry)

    assert _failure_classes(state) == []


# ---------------------------------------------------------------------------
# B-84 第 3 条 —— 记账键与记账器本身(纯函数,不必起图)
# ---------------------------------------------------------------------------


def _err(
    tool: str, path: str | None, summary: str, klass: ToolErrorClass = "unknown"
) -> ClassifiedToolError:
    return ClassifiedToolError(
        tool_name=tool,
        error_class=klass,
        summary=summary,
        retryable=False,
        advice="",
        path=path,
    )


def test_ledger_key_is_the_resource_not_the_tool() -> None:
    """键按**写的是哪份东西**算,不按哪个工具写的它。

    ``edit_file`` 与 ``write_file`` 写同一个路径 = 同一个键,所以 edit 失败、
    退回整份 write 成功,债还得清。而产物空间与工作区路径**撞名也不串**:
    两个都叫 ``a.py`` 也是两个键。
    """
    assert _ledger_key_for("edit_file", "a.py") == _ledger_key_for("write_file", "a.py")
    assert _ledger_key_for("save_artifact", "a.py") != _ledger_key_for("write_file", "a.py")
    # 取不到路径的退化成工具名那一层,且永远不跨空间。
    assert _ledger_key_for("exec_python", None) == ("tool", "exec_python")
    assert _ledger_key_for("write_file", None) == ("tool", "write_file")


def test_ledger_keeps_the_first_error_for_a_repeated_key() -> None:
    """同键再失败一次,留**先出现**的那条 —— 第一条错误信息比最后一条有用。

    照 hermes 的 ``_record_file_mutation_result``。用 ``setdefault`` 而不是
    ``[key] =``,顺带保住「按首次出现排序」。
    """
    key = _ledger_key_for("save_artifact", "a.md")
    carried = [_err("save_artifact", "a.md", "first")]
    out = _apply_failure_ledger(carried, [(key, _err("save_artifact", "a.md", "second"))])

    assert [f.summary for f in out] == ["first"]


def test_ledger_cancels_across_tools_in_the_same_resource_space() -> None:
    """**本次返工的那一格**:``edit_file`` 改 ``a.py`` 失败,``write_file``
    重写 ``a.py`` 成功 —— 同一份文件,债清。

    按工具名记账的话这里抵消不掉,而 60 天数据里 ``edit_file`` 失败率约 16%、
    模型的标准反应就是退回整份 ``write_file``:一个高频假阳性,信号会没人看。
    """
    carried = [_err("edit_file", "a.py", "no_match")]
    out = _apply_failure_ledger(carried, [(_ledger_key_for("write_file", "a.py"), None)])

    assert out == []


def test_ledger_does_not_cancel_across_resource_spaces() -> None:
    """同名不同空间不许互相抵消:存一份叫 ``a.py`` 的**产物**,还不了一次
    失败的**工作区文件**写入。撞名假抵消比漏报更坏 —— 它会编造一个成功。
    """
    carried = [_err("write_file", "a.py", "disk full")]
    out = _apply_failure_ledger(carried, [(_ledger_key_for("save_artifact", "a.py"), None)])

    assert [(f.tool_name, f.path) for f in out] == [("write_file", "a.py")]


def test_ledger_cancels_only_the_matching_key() -> None:
    """成功只还得上**同键**那一条,别的键原封不动。"""
    carried = [_err("save_artifact", "a.md", "boom"), _err("exec_python", None, "bang")]
    out = _apply_failure_ledger(carried, [(_ledger_key_for("exec_python", None), None)])

    assert [(f.tool_name, f.path) for f in out] == [("save_artifact", "a.md")]


def test_ledger_ignores_transient_in_both_directions() -> None:
    """transient 既不进账,也不抵消 —— 可重试的抖动两个方向都不是证据。

    「不抵消」这一半容易漏:一次 transient 失败**不是**成功,不能拿它去
    还同键那条真欠账。
    """
    key = _ledger_key_for("exec_python", None)
    carried = [_err("exec_python", None, "boom")]
    transient = _err("exec_python", None, "timed out", klass="transient")
    out = _apply_failure_ledger(carried, [(key, transient)])

    assert [f.summary for f in out] == ["boom"]
    assert _apply_failure_ledger([], [(_ledger_key_for("web_search", None), transient)]) == []


def test_a_transient_blip_on_a_write_tool_is_not_a_debt() -> None:
    """写类工具撞上沙箱 504 不算欠账 —— #1639 刚把它归成 ``transient``。

    ``_classify_tool_failure`` 里 mutation 分类器优先,写类工具的失败一律折成
    ``mutation_not_landed``,transient 那一位被吃掉。advisory 那侧该这么说
    (那个写确实没落地),但**记账这侧不能**:一次抖动会被记成真欠账,把一个
    只是重试一下就好了的 run 判成没做成。``_ledger_failure`` 拿催生它的那条
    原始分类兜回来。
    """
    not_landed = _err("write_file", "a.py", "504 Gateway Time-out", klass="mutation_not_landed")
    transient = _err("write_file", None, "504 Gateway Time-out", klass="transient")

    assert _ledger_failure(not_landed, transient) is transient
    # 非 transient 的原始分类不改写,advisory 侧的分类照用。
    hard = _err("write_file", None, "no_match", klass="unknown")
    assert _ledger_failure(not_landed, hard) is not_landed
    assert _ledger_failure(None, None) is None


# ---------------------------------------------------------------------------
# Task 2 —— exit_reason
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plain_stop_stamps_text_response() -> None:
    llm = _ScriptedLLM(responses=[AIMessage(content="done")])
    state = await _run(llm, ToolRegistry())
    assert state.get("exit_reason") == "text_response"


@pytest.mark.asyncio
async def test_max_steps_does_not_stamp_text_response() -> None:
    """撞预算之后走的是「一次无工具的收尾轮」,响应天然没有 tool_calls ——
    不显式排除就会被 ``text_response`` 盖掉,而那正好把「平台主动中止」
    伪装成「模型自然说完了」。"""
    llm = _ScriptedLLM(responses=[AIMessage(content="", tool_calls=[_tc("save_artifact", "tc-1")])])
    registry = ToolRegistry()
    registry.register(_ScriptedTool())

    state = await _run(llm, registry, max_steps=1)

    assert state.get("exit_reason") == "max_steps"


def test_budget_reason_precedence_is_pinned() -> None:
    """三个同时为真时的取值顺序**钉死**,否则它随分支书写顺序隐式漂移。"""
    assert (
        budget_exit_reason(max_steps=3, step_count=3, stuck=True, token_tripped=True) == "max_steps"
    )
    assert (
        budget_exit_reason(max_steps=0, step_count=0, stuck=True, token_tripped=True)
        == "no_progress"
    )
    assert (
        budget_exit_reason(max_steps=0, step_count=0, stuck=False, token_tripped=True)
        == "token_budget"
    )
    assert budget_exit_reason(max_steps=3, step_count=1, stuck=False, token_tripped=False) is None


def test_max_steps_zero_means_no_budget() -> None:
    """``max_steps=0`` 是「不设预算」,不是「预算为零、立刻用尽」。"""
    assert budget_exit_reason(max_steps=0, step_count=7, stuck=False, token_tripped=False) is None


# ---------------------------------------------------------------------------
# B-159 —— 只读工具「找不到」不是没做完
# ---------------------------------------------------------------------------


def _completed(state: AgentState) -> bool | None:
    return compute_completed(
        exit_reason=str(state.get("exit_reason")),
        unresolved_failures=state.get("unresolved_failures", []),
    )


@pytest.mark.asyncio
async def test_a_read_only_lookup_that_finds_nothing_is_not_left_unfinished() -> None:
    """B-159 实例(10-05 B-140 h10):模型猜了个不存在的技能路径,``read_file`` 回
    「找不到」,随后换 ``skill_view`` 读到同一份内容、正常交付 —— 旧账本按键抵消,
    换工具永远还不上,run 被判 ``completed=false``。

    只读工具回「找不到」是一次**有答案的**查询(目标不在那里),不会让交付件少任何
    东西,不该进「没做完」的账。
    """
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("read_file", "tc-1", {"path": "guess.md"})]),
            AIMessage(content="", tool_calls=[_tc("skill_view", "tc-2", {"path": "SKILL.md"})]),
            AIMessage(content="方案已交付"),
        ]
    )
    registry = ToolRegistry()
    registry.register(
        _ScriptedTool(
            name="read_file",
            path_arg="path",
            read_only=True,
            fail_on=frozenset({0}),
            exc=FileNotFoundError,
            error="workspace path not found: 'guess.md'",
        )
    )
    registry.register(_ScriptedTool(name="skill_view", path_arg="path", read_only=True))

    state = await _run(llm, registry)

    assert state.get("exit_reason") == "text_response", "前提:模型是自然说完的"
    assert _failure_keys(state) == []
    assert _completed(state) is True


@pytest.mark.asyncio
async def test_reading_a_file_before_creating_it_is_not_left_unfinished() -> None:
    """B-159 漏网(10-08 B-140 c02):``read_file`` 真实抛的是 ``FileOpError("read_file
    failed: not_found")`` —— 下划线, 文本兜底只认「not found」, 被判成 ``unknown`` 记了账。
    模型先读 ``notes.md``(还没有)、再把它建出来, 是正常做法, 这一轮被判成没做完。

    上面那条 B-159 用例拿 ``FileNotFoundError`` 造失败, 走的是类型分支, 碰不到这个缺口。
    """
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("read_file", "tc-1", {"path": "notes.md"})]),
            AIMessage(
                content="",
                tool_calls=[_tc("write_file", "tc-2", {"path": "notes.md", "content": "x"})],
            ),
            AIMessage(content="已写入 notes.md"),
        ]
    )
    registry = ToolRegistry()
    registry.register(
        _ScriptedTool(
            name="read_file",
            path_arg="path",
            read_only=True,
            fail_on=frozenset({0}),
            exc=FileOpError,
            error="read_file failed: not_found",
        )
    )
    registry.register(_ScriptedTool(name="write_file", path_arg="path"))

    state = await _run(llm, registry)

    assert state.get("exit_reason") == "text_response", "前提:模型是自然说完的"
    assert _failure_keys(state) == []
    assert _completed(state) is True


async def _read_then_answer(
    *,
    exc: type[Exception],
    error: str,
    tool: str = "read_file",
    read_only: bool = True,
) -> AgentState:
    """第一次调用 ``tool`` 失败(``exc(error)``),随后文字收尾。"""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc(tool, "tc-1", {"path": "a.md"})]),
            AIMessage(content="方案已交付"),
        ]
    )
    registry = ToolRegistry()
    registry.register(
        _ScriptedTool(
            name=tool,
            path_arg="path",
            read_only=read_only,
            fail_on=frozenset({0}),
            exc=exc,
            error=error,
        )
    )
    return await _run(llm, registry)


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        pytest.param(
            ValueError,
            "read_file path must be a relative workspace path without '..'",
            id="invalid_arguments",
        ),
        pytest.param(PermissionError, "permission denied", id="permission_denied"),
        pytest.param(OSError, "boom", id="unknown"),
    ],
)
@pytest.mark.asyncio
async def test_a_read_only_failure_of_any_class_is_not_left_unfinished(
    exc: type[Exception], error: str
) -> None:
    """B-164(10-08 B-140 h01 第 3 次):``read_file`` 带 ``..`` 路径被拒(``invalid_arguments``),
    模型换 ``skill_view`` 读到同一份内容、正常交付 —— 只读工具的键是 ``("tool", 名)``,
    换工具永远还不上, run 被判没做完。声明只读的工具,非瞬态失败**无论哪一类**都不进账。
    """
    state = await _read_then_answer(exc=exc, error=error)

    assert state.get("exit_reason") == "text_response", "前提:模型是自然说完的"
    assert _failure_keys(state) == []
    assert _completed(state) is True


@pytest.mark.asyncio
async def test_a_read_only_failure_still_reaches_the_error_count_and_advisory() -> None:
    """只改账本成员资格:失败照样走分类、计数(``_cm_tool_error_total``)与恢复提示。"""
    from orchestrator.graph_builder import builder as builder_mod

    counter = builder_mod._cm_tool_error_total.labels(
        error_class="invalid_arguments", tool="read_file"
    )
    before = counter._value.get()
    state = await _read_then_answer(exc=ValueError, error="read_file path must be a relative path")

    assert counter._value.get() == before + 1
    advisories = [
        m for m in state["messages"] if "recovery-advisory" in str(getattr(m, "content", ""))
    ]
    assert advisories, "恢复提示仍要注入"


@pytest.mark.asyncio
async def test_a_write_tool_invalid_arguments_still_counts() -> None:
    """只收**声明为只读**的工具:写类工具的参数错意味着那次写没落地,照旧记账。"""
    state = await _read_then_answer(
        exc=ValueError, error="write_file path must be relative", tool="write_file", read_only=False
    )

    assert [k[0] for k in _failure_keys(state)] == ["write_file"]
    assert _completed(state) is False


@pytest.mark.asyncio
async def test_an_unknown_tool_still_counts() -> None:
    """``spec is None``(模型调了没注册的工具)不是「声明只读」,照旧记账。"""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("ask_image", "tc-1", {"path": "a.png"})]),
            AIMessage(content="方案已交付"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_ScriptedTool(name="read_file", path_arg="path", read_only=True))

    state = await _run(llm, registry)

    assert _failure_classes(state) == ["unknown_tool"]
    assert _completed(state) is False


@pytest.mark.asyncio
async def test_a_transient_read_only_failure_is_unchanged() -> None:
    """transient 既不进账也不抵消:账上本来就没有它,行为不变。"""
    state = await _read_then_answer(exc=OSError, error="504 Gateway Time-out")

    assert _failure_keys(state) == []
    assert _completed(state) is True


@pytest.mark.asyncio
async def test_a_write_tool_not_found_still_counts() -> None:
    """收窄只收**只读**工具:写类工具回「找不到」(比如业务 MCP 更新一条不存在的记录)
    意味着那次写没落地,照旧记账。

    用一把不在 L-4 写类清单里的工具:清单里的 ``save_artifact`` / ``write_file`` 失败
    一律折成 ``mutation_not_landed``,到不了「找不到」这一格,咬不住只读那个条件。
    """
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("update_record", "tc-1")]),
            AIMessage(content="方案已交付"),
        ]
    )
    registry = ToolRegistry()
    registry.register(
        _ScriptedTool(
            name="update_record", fail_on=frozenset({0}), exc=FileNotFoundError, error="not found"
        )
    )

    state = await _run(llm, registry)

    assert _failure_classes(state) == ["resource_not_found"], "前提:失败被判成「找不到」"
    assert [k[0] for k in _failure_keys(state)] == ["update_record"]
    assert _completed(state) is False
