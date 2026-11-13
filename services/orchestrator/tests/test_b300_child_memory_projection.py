"""B-300 —— 委派子代读不到主智能体的记忆投影文件 ``MEMORY.md``(用户 11-13「一起堵掉」)。

主智能体每轮结束把召回到的记忆投影成 agent 根下的 ``threads/<对话>/MEMORY.md``;BUG-10 之前
的旧投影可能还留在 agent 根的 ``MEMORY.md``。子代与主智能体共用工作区与对话那一格,两条
读法都要堵:文件工具(唯一的路径解析入口 + ``search_files``)与沙箱里跑的代码(执行视图)。

四种写法(设计 §13.2):① ``/agent/threads/<对话>/MEMORY.md``(两种视图)、
② ``/workspace/threads/<对话>/MEMORY.md``(老视图)、③ ``/agent/MEMORY.md``(两种视图)、
④ ``/workspace/MEMORY.md``(老视图)。执行视图里「打开」的真内核断言在
``test_exec_view_kernel.py``(要 Docker);这里钉文件工具、搜索与把子代标记送到两个后端的那一路。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from expert_work.persistence import InMemoryMemoryStore
from orchestrator.graph_builder.builder import _build_tool_context
from orchestrator.tools import (
    BashTool,
    ReadFileTool,
    SearchFilesTool,
    ToolContext,
    WriteFileTool,
)
from orchestrator.tools._child_run import _child_config
from orchestrator.tools.exec_view import EXEC_VIEW_ARGV_PREFIX, exec_view_argv
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from orchestrator.tools.sandbox import HTTPSupervisorRuntime, RecordingSandboxRuntime
from orchestrator.tools.workspace_paths import (
    MEMORY_PROJECTION_FILE,
    WorkspaceView,
    resolve_workspace_path,
)

_TENANT, _USER = uuid4(), uuid4()
_KEY = "plan-aaaaaaaa"
_C1 = UUID("11111111-1111-1111-1111-111111111111")

_THREAD_TEXT = "thread-projection-zq"
_ROOT_TEXT = "root-projection-zq"
_OWN_TEXT = "own-conversation-file-zq"

#: 四种写法 → 它指向的文件(``thread`` = ``threads/<对话>/MEMORY.md``,``root`` = agent 根的旧投影)。
_ONE = f"/agent/threads/{_C1}/MEMORY.md"
_TWO = f"/workspace/threads/{_C1}/MEMORY.md"
_THREE = "/agent/MEMORY.md"
_FOUR = "/workspace/MEMORY.md"


def _parent(layout: str) -> ToolContext:
    return ToolContext(
        tenant_id=_TENANT,
        user_id=_USER,
        run_id=uuid4(),
        thread_id=_C1,
        agent_key=_KEY,
        workspace_layout=layout,  # type: ignore[arg-type]
    )


def _child(layout: str) -> ToolContext:
    """真实的委派路径:``_child_config`` 写 configurable,``_build_tool_context`` 读回来。"""
    ctx = _build_tool_context(
        _child_config(_parent(layout), sub_thread_id=uuid4(), sub_run_id=uuid4())
    )
    assert ctx.child_run
    return ctx


def _agent_root(root: Path) -> Path:
    return root / str(_TENANT) / str(_USER) / "agents" / _KEY


def _plant(root: Path) -> NasWorkspaceStore:
    agent = _agent_root(root)
    thread = agent / "threads" / str(_C1)
    (thread / "work").mkdir(parents=True)
    (thread / "MEMORY.md").write_text(_THREAD_TEXT)
    (agent / "MEMORY.md").write_text(_ROOT_TEXT)
    (thread / "work" / "MEMORY.md").write_text(_OWN_TEXT)
    return NasWorkspaceStore(root=str(root))


#: 拒绝的说法:``threads/`` 整个不许寻址 / agent 根下的旧投影不给委派子代。
_THREADS_REFUSED = "'threads' is a reserved layout segment"
_PROJECTION_REFUSED = "delegated runs cannot access /agent/MEMORY.md"


async def _read(store: NasWorkspaceStore, path: str, ctx: ToolContext) -> str:
    return (await ReadFileTool(store=store).call({"path": path}, ctx=ctx)).content


async def _refusal(store: NasWorkspaceStore, path: str, ctx: ToolContext) -> str:
    """``read_file`` 在解析器那一步拒绝时的说法(读到了任何东西都让断言红)。"""
    with pytest.raises(ValueError) as caught:
        await _read(store, path, ctx)
    return str(caught.value)


# ---------------------------------------------------------------------------
# 文件工具:子代
# ---------------------------------------------------------------------------


#: ① 不在这里:它对文件工具本来就对所有运行一律拒绝(主智能体也一样),由
#: :func:`test_the_main_run_reads_what_it_reads_today` 两种身份一起钉,这里列出来证明不了新东西。
@pytest.mark.parametrize(
    ("layout", "path", "refusal"),
    [
        ("conversation", _THREE, _PROJECTION_REFUSED),
        ("agent_root", _TWO, _THREADS_REFUSED),
        ("agent_root", _THREE, _PROJECTION_REFUSED),
        ("agent_root", _FOUR, _PROJECTION_REFUSED),
        ("agent_root", "MEMORY.md", _PROJECTION_REFUSED),
    ],
)
async def test_a_delegated_run_cannot_read_the_projection(
    tmp_path: Path, layout: str, path: str, refusal: str
) -> None:
    store = _plant(tmp_path)
    assert refusal in await _refusal(store, path, _child(layout))


@pytest.mark.parametrize("layout", ["conversation", "agent_root"])
async def test_the_store_scope_of_a_delegated_run_has_no_thread_cell(
    tmp_path: Path, layout: str
) -> None:
    """存储侧第二道:委派子代的作用域里 ``threads/`` 一格都不留、根下的旧投影也不在 ——
    绕过解析器直接拿子代的作用域去读,同样读不到。"""
    from orchestrator.tools.file_ops import _store_scope
    from orchestrator.tools.sandbox import WorkspacePathEscapeError
    from orchestrator.tools.sandbox_image_contract import AGENT_VIEW

    store = _plant(tmp_path)
    scope = _store_scope(AGENT_VIEW, _child(layout))
    for rel in (f"threads/{_C1}/MEMORY.md", "MEMORY.md"):
        with pytest.raises(WorkspacePathEscapeError):
            await store.read_file(tenant_id=_TENANT, user_id=_USER, path=rel, scope=scope)
    parent_scope = _store_scope(AGENT_VIEW, _parent(layout))
    assert (
        await store.read_file(
            tenant_id=_TENANT, user_id=_USER, path="MEMORY.md", scope=parent_scope
        )
        == _ROOT_TEXT.encode()
    )


@pytest.mark.parametrize("layout", ["conversation", "agent_root"])
@pytest.mark.parametrize("path", [_ONE, _TWO, _THREE, _FOUR])
def test_the_resolver_refuses_every_spelling_that_reaches_the_projection_for_a_child(
    layout: str, path: str
) -> None:
    """单一入口:四种写法在子代的视图里要么被拒,要么指的是对话自己的文件(对话视图的 ②④)。"""
    view = _child(layout).workspace_view
    assert view.child_run
    if layout == "conversation" and path in (_TWO, _FOUR):
        assert resolve_workspace_path(path, view=view, tool="read_file").kind == "conversation"
        return
    with pytest.raises(ValueError):
        resolve_workspace_path(path, view=view, tool="read_file")


@pytest.mark.parametrize("tool", ["read_file", "write_file", "save_artifact", "read_page"])
def test_the_refusal_covers_every_tool_that_goes_through_the_resolver(tool: str) -> None:
    view = _child("agent_root").workspace_view
    for path in (_TWO, _THREE, _FOUR, "MEMORY.md", "./MEMORY.md"):
        with pytest.raises(ValueError):
            resolve_workspace_path(path, view=view, tool=tool)


async def test_a_delegated_run_keeps_its_own_conversation_memory_file(tmp_path: Path) -> None:
    """对话视图里 ``/workspace/MEMORY.md`` 是对话自己的普通文件:子代照常读、写。"""
    store = _plant(tmp_path)
    child = _child("conversation")
    assert await _read(store, _FOUR, child) == _OWN_TEXT
    assert await _read(store, "MEMORY.md", child) == _OWN_TEXT
    runtime = RecordingSandboxRuntime()
    try:
        await WriteFileTool(client=runtime).call({"path": _FOUR, "content": "x"}, ctx=child)
    except ValueError:
        pytest.fail("the conversation's own MEMORY.md must stay writable for a delegated run")
    except Exception:  # noqa: S110 — 录制假沙箱回空输出,解析报错与本测试无关
        pass
    assert len(runtime.execs) == 1


async def test_a_delegated_run_still_reaches_other_agent_root_files(tmp_path: Path) -> None:
    store = _plant(tmp_path)
    (_agent_root(tmp_path) / "style.md").write_text("style")
    assert await _read(store, "/agent/style.md", _child("conversation")) == "style"
    assert await _read(store, "style.md", _child("agent_root")) == "style"


# ---------------------------------------------------------------------------
# 文件工具:主智能体照现在的样子(设计 §13.2 的逐布局、逐读法表)
# ---------------------------------------------------------------------------


async def test_the_main_run_reads_what_it_reads_today(tmp_path: Path) -> None:
    store = _plant(tmp_path)
    conv, legacy = _parent("conversation"), _parent("agent_root")
    # 对话视图:文件工具读得到 ③;② / ④ 在这一视图里是对话自己目录下的路径。
    assert await _read(store, _THREE, conv) == _ROOT_TEXT
    assert await _read(store, _FOUR, conv) == _OWN_TEXT
    # 老视图:②③④ 都读得到。
    assert await _read(store, _TWO, legacy) == _THREAD_TEXT
    assert await _read(store, _THREE, legacy) == _ROOT_TEXT
    assert await _read(store, _FOUR, legacy) == _ROOT_TEXT
    # ① 对文件工具一律拒绝,主智能体与委派子代都一样(不变)。
    for ctx in (conv, legacy, _child("conversation"), _child("agent_root")):
        assert _THREADS_REFUSED in await _refusal(store, _ONE, ctx)


# ---------------------------------------------------------------------------
# search_files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layout", ["conversation", "agent_root"])
async def test_a_delegated_runs_search_skips_the_projection(tmp_path: Path, layout: str) -> None:
    store = _plant(tmp_path)
    search = SearchFilesTool(store=store)
    for needle in (_THREAD_TEXT, _ROOT_TEXT):
        found = await search.call({"content": needle}, ctx=_child(layout))
        assert found.meta["paths"] == [], (needle, found.meta["paths"])
    by_name = await search.call({"name_glob": "MEMORY.md"}, ctx=_child(layout))
    expected = ["MEMORY.md"] if layout == "conversation" else []  # 对话自己的那一份
    assert by_name.meta["paths"] == expected


async def test_the_main_runs_search_is_unchanged(tmp_path: Path) -> None:
    store = _plant(tmp_path)
    search = SearchFilesTool(store=store)
    conv = await search.call({"content": _ROOT_TEXT}, ctx=_parent("conversation"))
    legacy = await search.call({"content": _ROOT_TEXT}, ctx=_parent("agent_root"))
    assert conv.meta["paths"] == ["/agent/MEMORY.md"]
    assert legacy.meta["paths"] == ["MEMORY.md"]


@pytest.mark.parametrize("layout", ["conversation", "agent_root"])
async def test_a_delegated_runs_search_cannot_tell_whether_the_projection_matches(
    tmp_path: Path, layout: str
) -> None:
    """命中上限 N、N 个命中里含旧投影:结果(条数、是否截断)要与旧投影不含这个词时一模一样 ——
    否则被注入的子代能一个字一个字地试出投影的内容。"""
    store = _plant(tmp_path)
    agent = _agent_root(tmp_path)
    for name in ("a.md", "b.md"):
        (agent / name).write_text("needle")
    search = SearchFilesTool(store=store, max_results=2)
    ctx = _child(layout)
    (agent / "MEMORY.md").write_text("no match here")
    without = await search.call({"content": "needle"}, ctx=ctx)
    (agent / "MEMORY.md").write_text("needle")
    with_match = await search.call({"content": "needle"}, ctx=ctx)

    keys = ("paths", "n_results", "truncated", "too_deep")
    assert [with_match.meta[k] for k in keys] == [without.meta[k] for k in keys]
    assert with_match.content == without.content
    assert "MEMORY.md" not in " ".join(with_match.meta["paths"])


async def test_a_delegated_runs_search_finds_its_own_conversation_memory_file(
    tmp_path: Path,
) -> None:
    store = _plant(tmp_path)
    found = await SearchFilesTool(store=store).call(
        {"content": _OWN_TEXT}, ctx=_child("conversation")
    )
    assert found.meta["paths"] == ["MEMORY.md"]


# ---------------------------------------------------------------------------
# 单一入口:解析器的调用点穷举 —— 新工具绕不过去
# ---------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parents[1] / "src" / "orchestrator"

#: 调用 ``resolve_workspace_path`` 的模块。设计 §13.2 数的五个文件里 ``workspace_scope`` 只在
#: 文档字符串里提到它(没有调用),由下面那条「只在这里建视图」守住。
_RESOLVER_CALLERS = {
    "tools/artifact.py",
    "tools/file_ops.py",
    "tools/read_page.py",
    "tools/vision.py",
}


def _calls(name: str) -> dict[str, list[ast.Call]]:
    found: dict[str, list[ast.Call]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if called == name:
                found.setdefault(path.relative_to(_SRC).as_posix(), []).append(node)
    return found


def test_every_resolver_call_site_is_known_and_passes_a_view() -> None:
    """新加一个解析路径的工具:这条红,逼着作者确认它用的是 ``ctx.workspace_view``(子代标记
    就在那个值里)。"""
    calls = _calls("resolve_workspace_path")
    assert set(calls) - {"tools/workspace_paths.py"} == _RESOLVER_CALLERS, sorted(calls)
    for module, nodes in calls.items():
        for node in nodes:
            assert any(kw.arg == "view" for kw in node.keywords), (module, node.lineno)


def test_the_workspace_view_is_only_built_from_the_tool_context() -> None:
    """子代标记随视图走:视图只在 ``ToolContext.workspace_view`` 一处建出来,手搓的视图会把
    标记丢掉。"""
    assert set(_calls("WorkspaceView")) == {"tools/registry.py"}


def test_the_child_flag_rides_on_the_view() -> None:
    assert _child("conversation").workspace_view.child_run
    assert not _parent("conversation").workspace_view.child_run
    assert WorkspaceView().child_run is False


def test_the_projection_file_name_is_the_projectors() -> None:
    from orchestrator.context.workspace_projection import MEMORY_FILE

    assert MEMORY_PROJECTION_FILE == MEMORY_FILE


# ---------------------------------------------------------------------------
# 执行视图:子代标记送到两个后端
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("ctx_of", "flag"), [(_child, True), (_parent, False)])
async def test_run_in_sandbox_sends_the_child_flag(ctx_of: Any, flag: bool) -> None:
    runtime = RecordingSandboxRuntime()
    try:
        await BashTool(client=runtime).call({"command": "true"}, ctx=ctx_of("conversation"))
    except Exception:  # noqa: S110 — 录制假沙箱回空输出,与本测试无关
        pass
    assert runtime.exec_child_runs == [flag]


def test_the_exec_view_argv_carries_the_child_flag_as_the_sixth_positional() -> None:
    n = len(EXEC_VIEW_ARGV_PREFIX)
    child = exec_view_argv(_KEY, ["py"], conv_id=_C1, thread_id=_C1, child_run=True)
    parent = exec_view_argv(_KEY, ["py"], conv_id=_C1, thread_id=_C1)
    assert child[n + 5 :] == ["1", "py"]
    assert parent[n + 5 :] == ["", "py"]
    # 没绑 agent 没有对话那一格可言:标记不发。
    assert exec_view_argv("", ["py"], child_run=True)[n + 5 :] == ["", "py"]


@pytest.mark.parametrize("flag", [True, False])
async def test_the_supervisor_backend_forwards_the_child_flag(flag: bool) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"stdout": "", "stderr": "", "exit_code": 0})

    runtime = HTTPSupervisorRuntime(base_url="http://sup", transport=httpx.MockTransport(handler))
    await runtime.exec(
        sandbox_id=uuid4(),
        code="print(1)",
        timeout_s=5,
        agent_key=_KEY,
        conv_id=_C1,
        workspace_thread_id=_C1,
        child_run=flag,
    )
    # 只在为真时发:老 supervisor 不认这个字段也不至于 422 掉主智能体的每一次 exec。
    assert sent[-1].get("child_run") is (True if flag else None)


# ---------------------------------------------------------------------------
# ask_image 手写的工作区 ref:与解析器同一个口径
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("layout", "rel", "allowed"),
    [
        ("conversation", f"threads/{_C1}/work/.tool_results/r/p.png", True),
        ("conversation", f"threads/{_C1}/MEMORY.md", False),
        ("conversation", f"threads/{_C1}/fig.png", False),
        ("agent_root", f"threads/{_C1}/fig.png", False),
    ],
)
def test_a_delegated_runs_image_ref_only_reaches_the_work_dir(
    layout: str, rel: str, allowed: bool
) -> None:
    """委派子代在 ``threads/`` 底下只有对话视图的 ``threads/<对话>/work`` 可看(执行视图里放回的
    也只有这一格);主智能体照旧认本对话那一整格。"""
    from orchestrator.tools import ToolBlockedError
    from orchestrator.tools.vision import _refuse_other_conversation

    full = f"agents/{_KEY}/{rel}"
    _refuse_other_conversation(full, _parent(layout))  # 主智能体:本对话那一格照旧
    if allowed:
        _refuse_other_conversation(full, _child(layout))
    else:
        with pytest.raises(ToolBlockedError):
            _refuse_other_conversation(full, _child(layout))


# ---------------------------------------------------------------------------
# 子智能体不召回(设计 §13.2「子智能体不召回」,从 PR 1 挪到本条线)
# ---------------------------------------------------------------------------


class _CountingMemoryStore(InMemoryMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.retrievals = 0

    async def retrieve(self, **kwargs: Any) -> Any:
        self.retrievals += 1
        return await super().retrieve(**kwargs)


async def _recall(*, child: bool) -> tuple[dict[str, Any], int]:
    """照工厂接线建出召回节点(manifest 自己开了 ``memory.long_term``),用父 / 子代的
    configurable 各跑一次。子代的 configurable 就是 ``_child_config`` 产出的那一份。"""
    from langchain_core.messages import HumanMessage

    from expert_work.protocol import AgentSpec, MemoryItem
    from orchestrator import MemoryEnv
    from orchestrator.agent_factory import _build_memory_nodes
    from orchestrator.llm import FakeEmbedder

    store = _CountingMemoryStore()
    embedder = FakeEmbedder(dim=16)
    content = "the customer prefers morning calls"
    [vector] = await embedder.embed([content], tenant_id=_TENANT)
    await store.write(
        [
            MemoryItem(
                id=uuid4(),
                tenant_id=_TENANT,
                user_id=_USER,
                kind="fact",
                content=content,
                embedding=vector,
            )
        ]
    )
    spec = AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": "helper", "version": "1.0.0", "tenant": "t"},
            "spec": {
                "tenant_config": {},
                "model": {"provider": "deepseek", "name": "x"},
                "system_prompt": {"template": "hi"},
                "memory": {"long_term": {}},
                "sandbox": {
                    "resources": {"cpu": "1.0", "memory": "1Gi"},
                    "network": {"egress": "proxy", "allowlist": []},
                    "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
                },
            },
        }
    )
    recall, _, _ = _build_memory_nodes(
        spec,
        memory_env=MemoryEnv(store=store, embedder=embedder),
        llm_caller=None,  # type: ignore[arg-type]  # 读时校验 / 改写都没开,用不到
    )
    assert recall is not None
    parent = _parent("conversation")
    if child:
        configurable = _child_config(parent, sub_thread_id=uuid4(), sub_run_id=uuid4())[
            "configurable"
        ]
    else:
        configurable = {
            "tenant_id": str(_TENANT),
            "user_id": str(_USER),
            "thread_id": str(_C1),
            "run_id": str(uuid4()),
        }
    # 经 LangGraph 跑这个节点(不是直接调函数):节点要拿到 ``config``,得由 LangGraph 按签名注入。
    from langgraph.graph import END, START, StateGraph

    from orchestrator.state import AgentState

    graph = StateGraph(AgentState)
    graph.add_node("memory_recall", recall)  # type: ignore[call-overload]
    graph.add_edge(START, "memory_recall")
    graph.add_edge("memory_recall", END)
    state: Any = {"messages": [HumanMessage(content="when should we call the customer?")]}
    final = await graph.compile().ainvoke(state, config={"configurable": configurable})
    return final, store.retrievals


async def test_a_delegated_run_does_not_recall_long_term_memory() -> None:
    final, retrievals = await _recall(child=True)
    assert retrievals == 0
    assert not final.get("recalled_memories")


async def test_the_same_agent_recalls_when_talked_to_directly() -> None:
    final, retrievals = await _recall(child=False)
    assert retrievals >= 1
    recalled = final.get("recalled_memories") or []
    assert [m.content for m in recalled] == ["the customer prefers morning calls"]
