"""B-84 PR-2 —— 工作区快照段怎么进提示词(接线层)。

渲染本身归 ``test_workspace_tree``;这一份钉的是**位置与生命周期**,五条硬要求各
一组断言:

1. 每轮重取,且提示词里只留最新一段 —— 更早那份逐字声称的是"现在有什么",而它已经
   不是现在了。
2. 逐字不进系统提示词 —— 系统提示词是前缀缓存那一段,而且构建缓存不含 user。
3. 挂在尾部,绝不插进 ``tool_call`` ↔ ``tool_result`` 之间。
4. 两个用户共享同一份构建时各看各的树。
5. 空工作区 / 列目录失败 = 一条消息都不注入(半截块会被读成"工作区是空的")。

外加两条同样是可证伪形状的不变式:检查点里一条都不留(CM-C4);**委派子代照样看得
到同一份块** —— 接线点在 ``agent_node`` 里,不加任何入口判断,所以七个 configurable
构造点自动全覆盖。加一道"只有主 run 才注入"的条件,最后那条会红。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig

from expert_work.common.conversation_channel import HIDE_FROM_UI, WORKSPACE_BLOCK_MARK
from expert_work.protocol import StructuredOutputSpec
from expert_work.runtime.cancellation import CancellationToken
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import (
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
)
from orchestrator.built_agent import BuiltAgent
from orchestrator.llm.providers._streaming import LLMDelta
from orchestrator.tools._child_run import run_child_to_result
from orchestrator.tools.skill_seed import sanitize_agent_key
from orchestrator.tools.workspace_scope import SCOPE_USER_ROOT
from orchestrator.tools.workspace_store import (
    RecordingWorkspaceStore,
    WorkspaceFileEntry,
    scope_view,
)
from orchestrator.tools.workspace_tree import WORKSPACE_BLOCK_HEADING, render_workspace_block

_TENANT = uuid4()
_USER = uuid4()
_AGENT_KEY = sanitize_agent_key("pf-probe")


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


@dataclass
class _RecordingLLM:
    """记下每一次收到的提示词;脚本用完就不带工具地答一句, run 于是收尾。"""

    responses: list[AIMessage] = field(default_factory=list)
    seen_prompts: list[list[BaseMessage]] = field(default_factory=list)
    calls: int = 0

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
        on_delta: Callable[[LLMDelta], Awaitable[None]] | None = None,
    ) -> AIMessage:
        del tools, output_schema, on_delta
        self.seen_prompts.append(list(messages))
        idx = self.calls
        self.calls += 1
        if idx < len(self.responses):
            return self.responses[idx]
        return AIMessage(content="done")


@dataclass
class _StubTool:
    """什么也不干的工具 —— 只为把一轮拆成 agent → tools → agent 两段。"""

    name: str = "noop"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description="stub")

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        return ToolResult(content="ok")


@dataclass
class _PerUserStore(RecordingWorkspaceStore):
    """按 user 给不同的树。

    :class:`RecordingWorkspaceStore` 自己对每个 user 回同一份清单 —— 用它写"跨用户
    隔离"会在不可能失败的条件下变绿(两个用户看到的当然一样)。
    """

    by_user: dict[UUID, list[WorkspaceFileEntry]] = field(default_factory=dict)
    list_scopes: list[tuple[UUID, UUID, str]] = field(default_factory=list)

    async def list_files(
        self, *, tenant_id: UUID, user_id: UUID, scope: str = SCOPE_USER_ROOT
    ) -> list[WorkspaceFileEntry]:
        self.list_scopes.append((tenant_id, user_id, scope))
        return scope_view(self.by_user.get(user_id, []), scope)


@dataclass
class _TurnVaryingStore(RecordingWorkspaceStore):
    """每被列一次就换一棵树 —— 钉"每轮重取"用。"""

    trees: list[list[WorkspaceFileEntry]] = field(default_factory=list)
    served: int = 0

    async def list_files(
        self, *, tenant_id: UUID, user_id: UUID, scope: str = SCOPE_USER_ROOT
    ) -> list[WorkspaceFileEntry]:
        del tenant_id, user_id
        idx = min(self.served, len(self.trees) - 1)
        self.served += 1
        return scope_view(self.trees[idx], scope)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _entry(name: str, size: int = 21173) -> WorkspaceFileEntry:
    """一条 agent 自己那一层下的文件(路径是**用户根相对**的, 替身照此裁剪)。"""
    return WorkspaceFileEntry(
        path=f"agents/{_AGENT_KEY}/{name}", size=size, mtime=datetime(2026, 9, 16, tzinfo=UTC)
    )


def _store(*names: str) -> _PerUserStore:
    return _PerUserStore(by_user={_USER: [_entry(n) for n in names]})


def _block_message(text: str) -> HumanMessage:
    """``builder._workspace_block_tail`` 注入的那个形状(标记住在 common)。"""
    return HumanMessage(
        content=text, additional_kwargs={HIDE_FROM_UI: True, WORKSPACE_BLOCK_MARK: True}
    )


def _texts(messages: Sequence[BaseMessage]) -> str:
    return "\n".join(str(m.content) for m in messages)


def _blocks(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    return [m for m in messages if (m.additional_kwargs or {}).get(WORKSPACE_BLOCK_MARK)]


def _configurable(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "thread_id": str(uuid4()),
        "tenant_id": str(_TENANT),
        "user_id": str(_USER),
        "agent_key": _AGENT_KEY,
    }
    cfg.update(overrides)
    return cfg


@asynccontextmanager
async def _graph(
    store: Any, *, llm: _RecordingLLM, registry: ToolRegistry | None = None
) -> AsyncIterator[Any]:
    async with make_checkpointer("memory") as cp:
        yield GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=llm,
                tool_registry=registry or ToolRegistry(),
                workspace_store=store,
            )
        )


async def _invoke(compiled: Any, messages: list[BaseMessage], **cfg: Any) -> dict[str, Any]:
    config: RunnableConfig = {"configurable": _configurable(**cfg)}
    result = await compiled.ainvoke(
        {"messages": messages, "step_count": 0, "max_steps": 5}, config=config
    )
    return {"result": result, "config": config}


async def _run(
    store: Any,
    messages: list[BaseMessage] | None = None,
    *,
    llm: _RecordingLLM | None = None,
    registry: ToolRegistry | None = None,
    **cfg: Any,
) -> _RecordingLLM:
    caller = llm or _RecordingLLM()
    async with _graph(store, llm=caller, registry=registry) as compiled:
        await _invoke(compiled, messages or [HumanMessage(content="做一版海报")], **cfg)
    return caller


# ---------------------------------------------------------------------------
# 1. 只留最新一段
# ---------------------------------------------------------------------------


async def test_only_the_latest_snapshot_reaches_the_model() -> None:
    """更早轮次的快照必须消失, 不是"排在前面"。

    它逐字声称的是「/workspace 现在有什么」。两段同时在场, 模型读到的是两份互相
    矛盾的「现在」, 而最旧的那份排在最前面。
    """
    stale = _block_message(render_workspace_block([_entry("style/ghost_from_last_turn.py")]))
    # 先证明旧那段里真有这个名字 —— 否则"不在提示词里"可以因为拼错而白绿。
    assert "ghost_from_last_turn.py" in str(stale.content)

    llm = await _run(
        _store("style/render_plan.py"),
        [
            HumanMessage(content="做一版海报"),
            stale,
            AIMessage(content="好的"),
            HumanMessage(content="换个配色"),
        ],
    )
    prompt = llm.seen_prompts[0]
    text = _texts(prompt)
    assert "render_plan.py" in text
    assert "ghost_from_last_turn.py" not in text
    assert len(_blocks(prompt)) == 1


async def test_the_snapshot_is_refetched_every_turn() -> None:
    """不是 run 起点取一次:第二轮看到的是第二棵树, 第一棵一个字都不剩。"""
    store = _TurnVaryingStore(
        trees=[[_entry("first_turn_only.py")], [_entry("second_turn_only.py")]]
    )
    registry = ToolRegistry()
    registry.register(_StubTool())
    llm = _RecordingLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "noop", "args": {}, "id": "c1", "type": "tool_call"}],
            )
        ]
    )
    await _run(store, llm=llm, registry=registry)

    assert store.served == 2, "每个 agent 轮各列一次 —— 少于两次说明块是 run 级缓存的"
    first, second = _texts(llm.seen_prompts[0]), _texts(llm.seen_prompts[1])
    assert "first_turn_only.py" in first
    assert "second_turn_only.py" in second
    assert "first_turn_only.py" not in second


# ---------------------------------------------------------------------------
# 2. 逐字不进系统提示词
# ---------------------------------------------------------------------------


async def test_the_snapshot_never_enters_the_system_prompt() -> None:
    """系统提示词是 Anthropic 的前缀缓存那一段, 而且 ``AgentRuntime.get_agent`` 的
    构建缓存键不含 user —— 快照进系统提示词既每轮打掉缓存, 又把 A 的文件名发给 B。"""
    llm = await _run(
        _store("style/render_plan.py"),
        [SystemMessage(content="you are a test agent"), HumanMessage(content="做一版海报")],
    )
    prompt = llm.seen_prompts[0]
    # 块确实注入了 —— 否则下面那条在"根本没有块"的条件下也绿。
    assert len(_blocks(prompt)) == 1
    systems = [m for m in prompt if isinstance(m, SystemMessage)]
    assert [str(m.content) for m in systems] == ["you are a test agent"]
    assert all(WORKSPACE_BLOCK_HEADING not in str(m.content) for m in systems)
    assert isinstance(_blocks(prompt)[0], HumanMessage)


# ---------------------------------------------------------------------------
# 3. 挂尾部, 不拆 tool_call ↔ tool_result
# ---------------------------------------------------------------------------


def _assert_tool_pairs_intact(prompt: Sequence[BaseMessage]) -> int:
    """每条带 ``tool_calls`` 的助手消息后面紧跟的必须是它那几条 ``ToolMessage``。

    返回检查到的配对数, 调用方据此确认这一趟真有可拆的东西。
    """
    pairs = 0
    for i, msg in enumerate(prompt):
        calls = list(getattr(msg, "tool_calls", None) or [])
        if not calls:
            continue
        pairs += 1
        followers = prompt[i + 1 : i + 1 + len(calls)]
        assert len(followers) == len(calls), f"tool_calls 后面少了结果: {followers!r}"
        for follower in followers:
            assert isinstance(follower, ToolMessage), (
                f"有东西插进了 tool_call 与 tool_result 之间: {follower!r}"
            )
    return pairs


async def test_the_block_never_splits_a_tool_call_from_its_result() -> None:
    registry = ToolRegistry()
    registry.register(_StubTool())
    llm = _RecordingLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "noop", "args": {}, "id": "c1", "type": "tool_call"}],
            )
        ]
    )
    await _run(_store("style/render_plan.py"), llm=llm, registry=registry)

    second = llm.seen_prompts[1]
    assert _assert_tool_pairs_intact(second) == 1, "这一趟没有工具配对, 这条测试拆不到东西"
    assert len(_blocks(second)) == 1
    # 尾部 —— 它是最后一条, 结构上就不可能夹在任何配对中间。
    assert second[-1] is _blocks(second)[0]


# ---------------------------------------------------------------------------
# 4. 跨用户隔离
# ---------------------------------------------------------------------------


async def test_two_users_of_one_build_each_see_only_their_own_files() -> None:
    """同一张编译好的图跑两个用户 —— 这正是没接 OAuth 的 agent 在生产里的形态。"""
    user_b = uuid4()
    store = _PerUserStore(
        by_user={
            _USER: [_entry("style/alice_plan.py")],
            user_b: [_entry("style/bob_plan.py")],
        }
    )
    llm = _RecordingLLM()
    async with _graph(store, llm=llm) as compiled:
        await _invoke(compiled, [HumanMessage(content="A 的活")])
        await _invoke(compiled, [HumanMessage(content="B 的活")], user_id=str(user_b))

    a_text, b_text = _texts(llm.seen_prompts[0]), _texts(llm.seen_prompts[1])
    assert "alice_plan.py" in a_text
    assert "bob_plan.py" not in a_text
    assert "bob_plan.py" in b_text
    assert "alice_plan.py" not in b_text


async def test_the_listing_is_scoped_to_the_agents_own_layer() -> None:
    """作用域经 ``store_scope`` 拼, 不手工拼 ``agents/<agent 名>``。

    诱饵用 agent **名字**做目录名:手工拼的失败形态是"目录是空的"不是报错, 所以
    只有摆一个"拼错了才会被列出来"的文件才分得出来。
    """
    store = _PerUserStore(
        by_user={
            _USER: [
                _entry("style/anchor.py"),
                WorkspaceFileEntry(path="agents/pf-probe/style/decoy.py", size=10),
            ]
        }
    )
    llm = await _run(store)
    text = _texts(llm.seen_prompts[0])
    assert "anchor.py" in text
    assert "decoy.py" not in text
    assert store.list_scopes[0][2] == f"agent:{_AGENT_KEY}"


# ---------------------------------------------------------------------------
# 5. 空工作区 / 列目录失败 = 什么也不注入
# ---------------------------------------------------------------------------


async def test_an_empty_workspace_injects_nothing() -> None:
    """不注入一个空块 —— 半截块会被模型读成"工作区是空的", 正是要治的那个误判。"""
    seed = [HumanMessage(content="做一版海报")]
    llm = await _run(_PerUserStore(by_user={_USER: []}), list(seed))
    prompt = llm.seen_prompts[0]
    assert _blocks(prompt) == []
    assert len(prompt) == len(seed)


async def test_a_listing_failure_omits_the_block_and_the_turn_still_runs() -> None:
    """块是补充, 不是运行前提:一次列目录失败不该让这一轮死掉。"""
    store = _PerUserStore(workspace_list_error=RuntimeError("NAS is down"))
    llm = await _run(store)
    assert _blocks(llm.seen_prompts[0]) == []
    assert WORKSPACE_BLOCK_HEADING not in _texts(llm.seen_prompts[0])


async def test_a_stale_block_goes_even_when_the_new_listing_is_empty() -> None:
    """工作区被清空之后, 上一轮那份快照不许留下来顶班。"""
    stale = _block_message(render_workspace_block([_entry("style/ghost.py")]))
    llm = await _run(
        _PerUserStore(by_user={_USER: []}),
        [HumanMessage(content="做一版海报"), stale, AIMessage(content="ok")],
    )
    prompt = llm.seen_prompts[0]
    assert _blocks(prompt) == []
    assert "ghost.py" not in _texts(prompt)


# ---------------------------------------------------------------------------
# 检查点不动(CM-C4)
# ---------------------------------------------------------------------------


async def test_the_block_never_lands_in_the_checkpoint() -> None:
    """只改这一次的提示词视图 —— 落库了就等于把一份会过期的快照写进会话历史。"""
    llm = _RecordingLLM()
    async with _graph(_store("style/render_plan.py"), llm=llm) as compiled:
        run = await _invoke(compiled, [HumanMessage(content="做一版海报")])
        snapshot = await compiled.aget_state(run["config"])
    assert len(_blocks(llm.seen_prompts[0])) == 1, "块没注入, 这条测试就什么也没验"
    assert _blocks(snapshot.values["messages"]) == []


# ---------------------------------------------------------------------------
# 不加入口判断 —— 委派子代照样看得到
# ---------------------------------------------------------------------------


async def test_a_delegated_child_sees_the_same_block_as_its_parent() -> None:
    """接线点在 ``agent_node`` 里, 所以走 agent 图的每一条路径自动全覆盖。

    委派子代透传的是**父的** ``agent_key``(``_child_run._child_config``), 与父共享
    同一个工作区作用域 —— 块内容对两者是同一份。真实 worker 事件里父代正是在任务正文
    里手抄了一份 ``【必读文件】`` 清单给 worker, 那份清单就是这个块要自动化掉的东西。

    变异自证的形态:在实现里加一句"只有主 run 才注入", 这条必须红。
    """
    store = _store("style/render_plan.py")

    parent_llm = _RecordingLLM()
    async with _graph(store, llm=parent_llm) as compiled:
        await _invoke(compiled, [HumanMessage(content="做一版海报")])
    parent_block = str(_blocks(parent_llm.seen_prompts[0])[0].content)
    assert "render_plan.py" in parent_block

    child_llm = _RecordingLLM()
    async with _graph(store, llm=child_llm) as child_graph:
        child = BuiltAgent(
            graph=child_graph,
            system_prompt="child prompt",
            max_steps=5,
        )
        result = await run_child_to_result(
            child=child,
            task="把海报渲染出来",
            ctx=ToolContext(
                tenant_id=_TENANT,
                user_id=_USER,
                agent_key=_AGENT_KEY,
                cancellation_token=CancellationToken(),
            ),
            child_depth=1,
            label="renderer",
            agent_ref="dynamic:renderer",
            trajectory_recorder=None,
            trajectory_metadata={},
        )
    assert result.content
    child_prompt = child_llm.seen_prompts[0]
    assert len(_blocks(child_prompt)) == 1
    assert str(_blocks(child_prompt)[0].content) == parent_block
