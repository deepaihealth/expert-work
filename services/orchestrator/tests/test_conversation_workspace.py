"""B-236 —— 每个对话一个 ``/workspace``,``/agent`` 是该 agent 各对话共用的根。

同一用户、同一 agent 的两个对话并发时,固定文件名的中间文件与报告互相覆盖(可能把
两位客户的数据混在一起)。这里钉的是宿主侧的一半:一个解析器、两个新作用域、过渡期
映射、工具调用点、委派子代、工作区快照段。exec 视图(命名空间里的挂载)由
``test_exec_view.py`` 的脚本演练与 ``infra/sandbox-image/smoke_test.py`` 覆盖。

文件真落盘:``_ViewRuntime`` 照 exec 脚本的挂载表把片段里的 ``/workspace`` / ``/agent``
换成临时目录里的真实位置再执行,读侧走真的 :class:`NasWorkspaceStore` —— 「对话 B
看不见对话 A 的文件」是从落盘位置推出来的,不是从某个字符串相等推出来的。
"""

from __future__ import annotations

import contextlib
import io
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import InMemoryArtifactStore
from orchestrator.graph_builder.builder import _build_tool_context
from orchestrator.tools import (
    BashTool,
    FileOpError,
    ReadFileTool,
    SearchFilesTool,
    ToolContext,
    WriteFileTool,
)
from orchestrator.tools._child_run import _child_config
from orchestrator.tools.artifact import SaveArtifactTool
from orchestrator.tools.artifact_snapshots import NasArtifactSnapshotStore
from orchestrator.tools.file_ops import SandboxWorkspaceWriter
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from orchestrator.tools.read_page import render_source
from orchestrator.tools.sandbox import (
    EgressContext,
    RecordingSandboxRuntime,
    SandboxOutcome,
    WorkspacePathEscapeError,
)
from orchestrator.tools.sandbox_image_contract import AGENT_VIEW, EXEC_VIEW
from orchestrator.tools.workspace_paths import (
    ResolvedKind,
    WorkspaceView,
    agent_relative,
    resolve_workspace_path,
)
from orchestrator.tools.workspace_scope import (
    agent_scope,
    agent_shared_scope,
    conversation_scope,
    scoped_path,
    store_scope,
)
from orchestrator.tools.workspace_tree import (
    AGENT_SECTION,
    CONVERSATION_SECTION,
    workspace_prompt_block,
)

_TENANT, _USER = uuid4(), uuid4()
_KEY = "plan-aaaaaaaa"
_C1 = UUID("11111111-1111-1111-1111-111111111111")
_C2 = UUID("22222222-2222-2222-2222-222222222222")
_CONV1 = WorkspaceView(agent_key=_KEY, conv_id=_C1, thread_id=_C1)
_LEGACY = WorkspaceView(agent_key=_KEY, thread_id=_C1)


def _ctx(conv: UUID | None, *, layout: str = "conversation") -> ToolContext:
    return ToolContext(
        tenant_id=_TENANT,
        user_id=_USER,
        run_id=uuid4(),
        thread_id=conv,
        agent_key=_KEY,
        workspace_layout=layout,  # type: ignore[arg-type]
    )


def _agent_root(root: Path) -> Path:
    return root / str(_TENANT) / str(_USER) / "agents" / _KEY


def _work(root: Path, conv: UUID) -> Path:
    return _agent_root(root) / "threads" / str(conv) / "work"


class _ViewRuntime(RecordingSandboxRuntime):
    """照 ``EXEC_VIEW_SCRIPT`` 的挂载表执行文件工具的片段:``/agent`` = agent 根,
    ``/workspace`` = 对话目录(老视图里也是 agent 根)。"""

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root

    async def exec(
        self,
        *,
        sandbox_id: UUID,
        code: str,
        timeout_s: int | None,
        agent_key: str = "",
        run_id: UUID | None = None,
        has_skills: bool = False,
        egress: EgressContext | None = None,
        conv_id: UUID | None = None,
        workspace_thread_id: UUID | None = None,
    ) -> SandboxOutcome:
        self.exec_views.append(conv_id)
        self.exec_thread_ids.append(workspace_thread_id)
        agent = _agent_root(self.root)
        work = _work(self.root, conv_id) if conv_id is not None else agent
        for d in (agent, work):
            d.mkdir(parents=True, exist_ok=True)
        rewritten = code.replace(
            f'"ws": "{AGENT_VIEW}"', f'"ws": {json.dumps(str(agent))}'
        ).replace(f'"ws": "{EXEC_VIEW}"', f'"ws": {json.dumps(str(work))}')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(rewritten, {})  # noqa: S102 — snippet is built from a fixed template
        return SandboxOutcome(stdout=buf.getvalue(), stderr="", exit_code=0, timed_out=False)


# ---------------------------------------------------------------------------
# 解析器(计划测试 8)
# ---------------------------------------------------------------------------

_CONV_TABLE: list[tuple[str, str, str, ResolvedKind]] = [
    ("report.md", EXEC_VIEW, "report.md", "conversation"),
    ("/workspace/out/report.md", EXEC_VIEW, "out/report.md", "conversation"),
    ("/workspace", EXEC_VIEW, ".", "conversation"),
    ("threads/x.md", EXEC_VIEW, "threads/x.md", "conversation"),
    ("/agent/style/a.md", AGENT_VIEW, "style/a.md", "agent"),
    ("/agent", AGENT_VIEW, ".", "agent"),
    (f"/workspace/agents/{_KEY}/style/a.md", AGENT_VIEW, "style/a.md", "agent"),
    ("./uploads/a.pdf", AGENT_VIEW, "uploads/a.pdf", "platform"),
    ("inputs/r/inputs.json", AGENT_VIEW, "inputs/r/inputs.json", "platform"),
    ("/workspace/.tool_results/r/x.txt", AGENT_VIEW, ".tool_results/r/x.txt", "platform"),
    ("shared:legacy.md", f"{EXEC_VIEW}/shared", "legacy.md", "shared"),
]


@pytest.mark.parametrize(("raw", "ws", "rel", "kind"), _CONV_TABLE)
def test_conversation_view_resolution(raw: str, ws: str, rel: str, kind: ResolvedKind) -> None:
    target = resolve_workspace_path(raw, view=_CONV1, tool="read_file")
    assert (target.ws, target.rel, target.kind) == (ws, rel, kind)


@pytest.mark.parametrize(
    ("raw", "ws", "rel", "kind"),
    [
        ("report.md", EXEC_VIEW, "report.md", "legacy"),
        ("uploads/a.pdf", EXEC_VIEW, "uploads/a.pdf", "legacy"),
        (f"threads/{_C1}/PLAN.md", EXEC_VIEW, f"threads/{_C1}/PLAN.md", "legacy"),
        ("/agent/style/a.md", AGENT_VIEW, "style/a.md", "agent"),
        (f"/workspace/agents/{_KEY}/a.md", AGENT_VIEW, "a.md", "agent"),
    ],
)
def test_legacy_view_resolution_is_the_agent_root(
    raw: str, ws: str, rel: str, kind: ResolvedKind
) -> None:
    target = resolve_workspace_path(raw, view=_LEGACY, tool="read_file")
    assert (target.ws, target.rel, target.kind) == (ws, rel, kind)


def test_a_base_dir_name_is_this_conversations_own_copy() -> None:
    """B-236 —— agent 根的基础目录在对话开场时复制进本对话:同名相对路径就是本对话自己的
    目录,读写都落 ``work/``;要改所有对话共用的那份,写 ``/agent/<目录>/``。"""
    for tool in ("read_file", "write_file", "edit_file", "save_artifact"):
        target = resolve_workspace_path("style/a.md", view=_CONV1, tool=tool)
        assert (target.ws, target.kind) == (EXEC_VIEW, "conversation")


@pytest.mark.parametrize(
    "raw",
    [
        "/agent/threads/x",
        "/agent/agents/x",
        "/agent/shared/x",
        "agents/other-bbbbbbbb/x",
        "shared/x",
        "/etc/passwd",
        "../x",
        "a/../../x",
        "/mnt/workspace/x",
    ],
)
@pytest.mark.parametrize("view", [_CONV1, _LEGACY], ids=["conversation", "legacy"])
def test_resolver_rejects(raw: str, view: WorkspaceView) -> None:
    with pytest.raises(ValueError):
        resolve_workspace_path(raw, view=view, tool="read_file")


def test_agent_relative_puts_conversation_files_under_their_work_dir() -> None:
    target = resolve_workspace_path("out/r.md", view=_CONV1, tool="save_artifact")
    assert agent_relative(target, _CONV1) == f"threads/{_C1}/work/out/r.md"
    shared = resolve_workspace_path("/agent/tpl.md", view=_CONV1, tool="save_artifact")
    assert agent_relative(shared, _CONV1) == "tpl.md"


def test_view_is_legacy_unless_the_layout_is_conversation() -> None:
    assert _ctx(_C1).workspace_view == _CONV1
    assert _ctx(_C1, layout="agent_root").workspace_view == _LEGACY
    unbound = replace(_ctx(_C1), agent_key="")
    assert unbound.workspace_view.conv_id is None


# ---------------------------------------------------------------------------
# 作用域(计划测试 9)
# ---------------------------------------------------------------------------


def test_conversation_scope_is_the_work_dir_and_platform_dirs_point_at_the_agent_root() -> None:
    scope = conversation_scope(_KEY, _C1)
    assert scoped_path(scope, "a/b.md") == f"agents/{_KEY}/threads/{_C1}/work/a/b.md"
    assert scoped_path(scope, ".tool_results/r/p.jpg") == f"agents/{_KEY}/.tool_results/r/p.jpg"
    assert scoped_path(scope, "uploads/x.pdf") == f"agents/{_KEY}/uploads/x.pdf"


_CX = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.mark.parametrize("conv", [_CX.upper(), _CX.replace("-", ""), "{" + _CX + "}", "x", ""])
def test_non_canonical_conversation_id_is_rejected(conv: str) -> None:
    with pytest.raises(WorkspacePathEscapeError):
        scoped_path(f"conversation:{_KEY}:{conv}", "a.md")


def test_agent_shared_scope_never_addresses_threads() -> None:
    with pytest.raises(WorkspacePathEscapeError):
        scoped_path(agent_shared_scope(_KEY), f"threads/{_C2}/work/secret.md")


def test_store_scope_maps_both_views() -> None:
    assert store_scope(EXEC_VIEW, agent_key=_KEY, conv_id=_C1) == conversation_scope(_KEY, _C1)
    assert store_scope(AGENT_VIEW, agent_key=_KEY, conv_id=_C1) == agent_shared_scope(_KEY, _C1)
    # 老视图的 /workspace 与 /agent 是同一个 agent 根,threads/ 只留本线程那一格
    assert store_scope(EXEC_VIEW, agent_key=_KEY, thread_id=_C2) == agent_shared_scope(_KEY, _C2)
    assert store_scope(AGENT_VIEW, agent_key=_KEY) == agent_shared_scope(_KEY)
    assert store_scope(EXEC_VIEW, agent_key="") != agent_scope(_KEY)


def _tree(root: Path) -> None:
    agent = _agent_root(root)
    for rel, text in {
        "style/anchor.md": "anchor",
        "notes.md": "root file",
        f"threads/{_C1}/PLAN.md": "plan 1",
        f"threads/{_C1}/work/mine.md": "conversation 1",
        f"threads/{_C2}/work/theirs.md": "conversation 2",
    }.items():
        (agent / rel).parent.mkdir(parents=True, exist_ok=True)
        (agent / rel).write_text(text)


async def test_agent_shared_listing_never_returns_threads(tmp_path: Path) -> None:
    _tree(tmp_path)
    store = NasWorkspaceStore(root=str(tmp_path))
    scope = agent_shared_scope(_KEY)
    files = await store.list_files(tenant_id=_TENANT, user_id=_USER, scope=scope)
    assert sorted(f.path for f in files) == ["notes.md", "style/anchor.md"]
    listing = await store.list_dir(tenant_id=_TENANT, user_id=_USER, scope=scope, path=".")
    assert "threads" not in {e.name for e in listing.entries}
    found = await store.search_files(
        tenant_id=_TENANT, user_id=_USER, scope=scope, content="conversation"
    )
    assert found.entries == ()


@pytest.mark.parametrize(
    "raw", [f"threads/{_C2}/work/theirs.md", "threads/x", "threads", f"threads/{_C2}"]
)
def test_legacy_view_never_addresses_another_conversations_thread(raw: str) -> None:
    with pytest.raises(ValueError):
        resolve_workspace_path(raw, view=_LEGACY, tool="read_file")
    with pytest.raises(ValueError):
        resolve_workspace_path(raw, view=WorkspaceView(agent_key=_KEY), tool="read_file")


async def test_a_legacy_conversation_cannot_see_new_conversations(tmp_path: Path) -> None:
    """发布前开的对话(老视图)搜、读、列都碰不到发布后新对话的工作目录;自己那一格
    ``threads/<本线程>/`` 仍可读(状态投影在那儿)。"""
    from orchestrator.tools import ListDirTool

    _tree(tmp_path)
    store = NasWorkspaceStore(root=str(tmp_path))
    legacy = _ctx(_C1, layout="agent_root")
    found = await SearchFilesTool(store=store).call({"content": "conversation"}, ctx=legacy)
    assert found.meta["paths"] == []
    with pytest.raises(ValueError):
        await ReadFileTool(store=store).call({"path": f"threads/{_C2}/work/theirs.md"}, ctx=legacy)
    listing = await ListDirTool(store=store).call({}, ctx=legacy)
    assert "threads" not in {e["name"] for e in listing.meta["entries"]}
    own = await ReadFileTool(store=store).call({"path": f"threads/{_C1}/PLAN.md"}, ctx=legacy)
    assert own.content == "plan 1"
    with pytest.raises(WorkspacePathEscapeError):
        scoped_path(agent_shared_scope(_KEY, _C1), f"threads/{_C2}/work/theirs.md")


async def test_a_legacy_exec_carries_its_own_thread_and_no_conversation(tmp_path: Path) -> None:
    runtime = _ViewRuntime(tmp_path)
    await WriteFileTool(client=runtime).call(
        {"path": "a.md", "content": "x"}, ctx=_ctx(_C1, layout="agent_root")
    )
    assert runtime.exec_views[-1] is None
    assert runtime.exec_thread_ids[-1] == _C1


# ---------------------------------------------------------------------------
# 工具调用点(计划测试 11-16)
# ---------------------------------------------------------------------------


async def test_a_conversation_never_sees_another_conversations_files(tmp_path: Path) -> None:
    runtime = _ViewRuntime(tmp_path)
    store = NasWorkspaceStore(root=str(tmp_path))
    await WriteFileTool(client=runtime).call({"path": "report.md", "content": "A"}, ctx=_ctx(_C1))
    await WriteFileTool(client=runtime).call({"path": "report.md", "content": "B"}, ctx=_ctx(_C2))
    await WriteFileTool(client=runtime).call(
        {"path": "/agent/tpl.md", "content": "shared"}, ctx=_ctx(_C1)
    )

    assert (_work(tmp_path, _C1) / "report.md").read_text() == "A"
    assert (_work(tmp_path, _C2) / "report.md").read_text() == "B"
    assert runtime.exec_views[0] == _C1
    read = ReadFileTool(store=store)
    assert (await read.call({"path": "report.md"}, ctx=_ctx(_C1))).content == "A"
    assert (await read.call({"path": "report.md"}, ctx=_ctx(_C2))).content == "B"
    for conv in (_C1, _C2):
        assert (await read.call({"path": "/agent/tpl.md"}, ctx=_ctx(conv))).content == "shared"
    with pytest.raises(ValueError):
        await read.call({"path": f"/agent/threads/{_C1}/work/report.md"}, ctx=_ctx(_C2))


async def test_a_new_conversation_lists_and_searches_an_empty_workspace(tmp_path: Path) -> None:
    from orchestrator.tools import ListDirTool

    _tree(tmp_path)
    store = NasWorkspaceStore(root=str(tmp_path))
    fresh = _ctx(uuid4())
    listing = await ListDirTool(store=store).call({}, ctx=fresh)
    assert listing.meta["entries"] == []
    found = await SearchFilesTool(store=store).call({"content": "anchor"}, ctx=fresh)
    assert found.meta["paths"] == ["/agent/style/anchor.md"]


async def test_platform_paths_read_the_agent_root_from_a_conversation(tmp_path: Path) -> None:
    """上传件、本轮 inputs.json、超长工具结果的回读路径都在 agent 根。"""
    agent = _agent_root(tmp_path)
    for rel in ("uploads/a.txt", "inputs/r1/inputs.json", ".tool_results/r1/out.txt"):
        (agent / rel).parent.mkdir(parents=True, exist_ok=True)
        (agent / rel).write_text(rel)
    read = ReadFileTool(store=NasWorkspaceStore(root=str(tmp_path)))
    for rel in ("uploads/a.txt", "inputs/r1/inputs.json", ".tool_results/r1/out.txt"):
        assert (await read.call({"path": rel}, ctx=_ctx(_C1))).content == rel


async def test_save_artifact_registers_the_physical_location(tmp_path: Path) -> None:
    runtime = _ViewRuntime(tmp_path)
    store = InMemoryArtifactStore()
    snapshots = NasArtifactSnapshotStore(workspace=NasWorkspaceStore(root=str(tmp_path)))
    tool = SaveArtifactTool(store=store, client=runtime, snapshots=snapshots)
    await WriteFileTool(client=runtime).call({"path": "r.md", "content": "A"}, ctx=_ctx(_C1))
    await WriteFileTool(client=runtime).call({"path": "/agent/t.md", "content": "T"}, ctx=_ctx(_C1))

    await tool.call({"name": "r.md"}, ctx=_ctx(_C1))
    await tool.call({"name": "t.md", "path": "/agent/t.md"}, ctx=_ctx(_C1))

    r = await store.get_latest_version(
        tenant_id=_TENANT, user_id=_USER, agent_key=_KEY, name="r.md"
    )
    t = await store.get_latest_version(
        tenant_id=_TENANT, user_id=_USER, agent_key=_KEY, name="t.md"
    )
    assert r is not None and r.path_in_workspace == f"agents/{_KEY}/threads/{_C1}/work/r.md"
    assert t is not None and t.path_in_workspace == f"agents/{_KEY}/t.md"


async def test_save_artifact_of_another_conversations_file_is_not_found(tmp_path: Path) -> None:
    runtime = _ViewRuntime(tmp_path)
    snapshots = NasArtifactSnapshotStore(workspace=NasWorkspaceStore(root=str(tmp_path)))
    await WriteFileTool(client=runtime).call({"path": "r.md", "content": "A"}, ctx=_ctx(_C1))
    with pytest.raises(FileOpError, match="no file"):
        await SaveArtifactTool(
            store=InMemoryArtifactStore(), client=runtime, snapshots=snapshots
        ).call({"name": "r.md"}, ctx=_ctx(_C2))


async def test_projection_lands_beside_the_work_dir_not_inside_it(tmp_path: Path) -> None:
    runtime = _ViewRuntime(tmp_path)
    writer = SandboxWorkspaceWriter(client=runtime, ctx=_ctx(_C1))
    await writer.write(rel=f"threads/{_C1}/PLAN.md", content="plan")
    assert (_agent_root(tmp_path) / "threads" / str(_C1) / "PLAN.md").read_text() == "plan"
    assert not (_work(tmp_path, _C1) / "threads").exists()


def test_render_source_keys_documents_by_their_physical_location() -> None:
    sha1, rel1 = render_source("d.pptx", view=_CONV1)
    sha2, rel2 = render_source("d.pptx", view=WorkspaceView(agent_key=_KEY, conv_id=_C2))
    legacy_sha, legacy_rel = render_source("d.pptx", view=_LEGACY)
    assert rel1 == f"threads/{_C1}/work/d.pptx"
    assert rel2 == f"threads/{_C2}/work/d.pptx"
    assert legacy_rel == "d.pptx"
    # 两个对话里同名的文档不共用渲染目录;上传件是同一份,共用。
    assert len({sha1, sha2, legacy_sha}) == 3
    up1, up_rel = render_source("uploads/d.pptx", view=_CONV1)
    up2, _ = render_source("uploads/d.pptx", view=WorkspaceView(agent_key=_KEY, conv_id=_C2))
    assert up1 == up2 and up_rel == "uploads/d.pptx"


# ---------------------------------------------------------------------------
# 委派子代(计划测试 18)
# ---------------------------------------------------------------------------


async def test_children_and_grandchildren_share_the_root_conversation(tmp_path: Path) -> None:
    parent = _ctx(_C1)
    child = _build_tool_context(_child_config(parent, sub_thread_id=uuid4(), sub_run_id=uuid4()))
    grandchild = _build_tool_context(
        _child_config(child, sub_thread_id=uuid4(), sub_run_id=uuid4())
    )
    assert child.workspace_view.conv_id == _C1
    assert grandchild.workspace_view.conv_id == _C1

    runtime = _ViewRuntime(tmp_path)
    await WriteFileTool(client=runtime).call({"path": "w.txt", "content": "w"}, ctx=child)
    read = ReadFileTool(store=NasWorkspaceStore(root=str(tmp_path)))
    assert (await read.call({"path": "w.txt"}, ctx=parent)).content == "w"


def test_children_of_a_legacy_conversation_stay_legacy() -> None:
    parent = _ctx(_C1, layout="agent_root")
    child = _build_tool_context(_child_config(parent, sub_thread_id=uuid4(), sub_run_id=uuid4()))
    assert child.workspace_view == _LEGACY


# ---------------------------------------------------------------------------
# 工作区快照段(计划测试 21)
# ---------------------------------------------------------------------------


async def test_the_block_lists_this_conversation_and_the_agent_root_only(tmp_path: Path) -> None:
    _tree(tmp_path)
    block = await workspace_prompt_block(
        NasWorkspaceStore(root=str(tmp_path)),
        tenant_id=_TENANT,
        user_id=_USER,
        view=_CONV1,
    )
    assert block.index(CONVERSATION_SECTION) < block.index(AGENT_SECTION)
    assert "mine.md" in block
    assert "anchor.md" in block
    assert "theirs.md" not in block
    assert "PLAN.md" not in block
    assert "threads" not in block


def _entry(path: str, size: int = 1, day: int = 1) -> Any:
    from datetime import UTC, datetime

    from orchestrator.tools.workspace_store import WorkspaceFileEntry

    return WorkspaceFileEntry(path=path, size=size, mtime=datetime(2026, 10, day, tzinfo=UTC))


def test_an_unchanged_seed_copy_takes_one_line_and_no_budget() -> None:
    """B-236 —— 开场复制进来、一个文件都没动过的目录:本对话那一节只占一行(不占展开预算,
    它的文件在 ``/agent`` 一节已经列着);改过的副本照常展开 —— 那是本对话自己的改动。"""
    from orchestrator.tools.workspace_tree import render_conversation_block

    agent = [_entry(f"style/s{i}.md") for i in range(20)] + [_entry("tpl/t.md")]
    conv = [
        *(_entry(f"style/s{i}.md") for i in range(20)),
        _entry("tpl/t.md", size=2, day=9),  # 本对话改过
        _entry("tpl/u.md"),  # 与原样副本同目录:整个目录照常展开
        _entry("mine.md"),
    ]
    pristine = frozenset({*(f"style/s{i}.md" for i in range(20)), "tpl/u.md"})
    block = render_conversation_block(conv, agent, pristine=pristine, max_expanded=24)
    ours = block.split(CONVERSATION_SECTION + "\n", 1)[1].split("\n\n", 1)[0]
    assert "  style/" in ours and "unchanged copy of /agent/style/" in ours
    assert "s0.md" not in ours  # 未改动的副本不展开
    assert "t.md" in ours and "u.md" in ours and "mine.md" in ours  # 改过的目录照常展开
    theirs = block.split(AGENT_SECTION + "\n", 1)[1]
    assert "s0.md" in theirs  # 预算没被副本吃掉,/agent 一节照样展开


def test_dirs_too_large_to_copy_are_named_with_their_agent_path() -> None:
    from orchestrator.tools.workspace_tree import render_conversation_block

    block = render_conversation_block([], [_entry("big/x.bin")], seed_skipped=("big", "大目录"))
    ours = block.split(CONVERSATION_SECTION + "\n", 1)[1].split("\n\n", 1)[0]
    assert ours == (
        "  Too large to copy into this conversation — use in place: /agent/big/, /agent/大目录/"
    )
    assert render_conversation_block([], [], seed_skipped=("big",)) != ""
    assert render_conversation_block([], []) == ""


async def test_the_legacy_block_no_longer_lists_other_conversations_projections(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    block = await workspace_prompt_block(
        NasWorkspaceStore(root=str(tmp_path)), tenant_id=_TENANT, user_id=_USER, view=_LEGACY
    )
    assert "anchor.md" in block
    assert "PLAN.md" not in block
    assert CONVERSATION_SECTION not in block


# ---------------------------------------------------------------------------
# 平台文本(计划测试 23)
# ---------------------------------------------------------------------------


def _all_text(spec: Any) -> str:
    return json.dumps({"d": spec.description, "p": spec.parameters}, ensure_ascii=False)


def test_tool_text_states_the_two_views_once_and_never_the_old_alias() -> None:
    from orchestrator.tools import EditFileTool, ExecPythonTool, ListDirTool
    from orchestrator.tools.read_document import ReadDocumentTool
    from orchestrator.tools.read_page import ReadPageTool
    from orchestrator.tools.workspace_paths import EXEC_WORKSPACE_NOTE, WORKSPACE_PATH_RULE

    store = NasWorkspaceStore(root="/nonexistent")
    client = RecordingSandboxRuntime()
    file_tools: list[Any] = [
        ReadFileTool(store=store),
        WriteFileTool(client=client),
        ListDirTool(store=store),
        SearchFilesTool(store=store),
        EditFileTool(client=client),
        ReadDocumentTool(client=client),
        ReadPageTool(client=client),
        SaveArtifactTool(
            store=InMemoryArtifactStore(),
            client=client,
            snapshots=NasArtifactSnapshotStore(workspace=store),
        ),
    ]
    for tool in file_tools:
        text = tool.spec.description
        assert text.count(WORKSPACE_PATH_RULE) == 1, tool.spec.name
    for exec_tool in (BashTool(client=client), ExecPythonTool(client=client)):
        assert exec_tool.spec.description.count(EXEC_WORKSPACE_NOTE) == 1
    for tool in [*file_tools, BashTool(client=client), ExecPythonTool(client=client)]:
        text = _all_text(tool.spec)
        assert "/workspace/agents/" not in text, tool.spec.name
        assert "agent's own directory" not in text, tool.spec.name
        # 描述随 agent 构建缓存、不分对话:老视图的对话读到的是同一句,所以不许无条件说
        # 「/workspace 就是本对话的目录」,也不许说「别人看不见 / 够不着」(B-60 §五 口径)。
        assert "/workspace is that conversation's own" not in text or "older" in text
        for claim in (
            "do not see it",
            "/workspace — this conversation",
            "this conversation's working",
        ):
            assert claim not in text, (tool.spec.name, claim)


def test_children_inherit_the_parents_effective_view_not_its_raw_layout() -> None:
    """父的布局写着 ``conversation`` 却没有对话(没有线程 id)时,父实际是老视图 ——
    子代必须是同一个,不能拿到一个父自己都没有的对话视图。"""
    parent = replace(_ctx(_C1), thread_id=None)
    assert not parent.workspace_view.per_conversation
    config = _child_config(parent, sub_thread_id=uuid4(), sub_run_id=uuid4())
    assert config["configurable"]["workspace_layout"] == "agent_root"
    assert not _build_tool_context(config).workspace_view.per_conversation
