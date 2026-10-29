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
import os
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
from orchestrator.tools.workspace_compat import (
    COMPAT_UNTIL_ENV,
    compat_dir_hits_total,
    discover_compat_dirs,
    name_shape,
)
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


def _ctx(
    conv: UUID | None, *, layout: str = "conversation", compat: tuple[str, ...] = ()
) -> ToolContext:
    return ToolContext(
        tenant_id=_TENANT,
        user_id=_USER,
        run_id=uuid4(),
        thread_id=conv,
        agent_key=_KEY,
        workspace_layout=layout,  # type: ignore[arg-type]
        workspace_compat_dirs=compat,
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
        compat_dirs: tuple[str, ...] = (),
        workspace_thread_id: UUID | None = None,
    ) -> SandboxOutcome:
        self.exec_views.append((conv_id, compat_dirs))
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


def test_compat_dir_resolves_to_the_agent_root_only_when_listed() -> None:
    mapped = replace(_CONV1, compat_dirs=("style",))
    assert resolve_workspace_path("style/a.md", view=mapped, tool="read_file").kind == "compat"
    assert resolve_workspace_path("style/a.md", view=_CONV1, tool="read_file").kind == (
        "conversation"
    )
    # 根下单个文件从不映射 —— 名单里只会有目录名,同名文件也照样落本对话。
    assert resolve_workspace_path("style", view=mapped, tool="read_file").kind == "compat"


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
    # 过渡期名单只在对话视图里生效
    assert _ctx(_C1, layout="agent_root", compat=("style",)).workspace_view.compat_dirs == ()


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
    assert runtime.exec_views[-1] == (None, ())
    assert runtime.exec_thread_ids[-1] == _C1


# ---------------------------------------------------------------------------
# 过渡期映射(计划测试 10 宿主侧 / 22)
# ---------------------------------------------------------------------------


def _compat_tree(root: Path) -> None:
    agent = _agent_root(root)
    work = _work(root, _C1)
    for d in ("style", "own", "stub", ".hidden", "uploads", "threads"):
        (agent / d).mkdir(parents=True, exist_ok=True)
    (agent / "style" / "a.md").write_text("a")
    (agent / "notes.md").write_text("root file")
    (work / "own").mkdir(parents=True)
    (work / "own" / "x").write_text("mine")
    (work / "stub").mkdir()


async def test_compat_dirs_are_existing_root_dirs_the_conversation_lacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _compat_tree(tmp_path)
    monkeypatch.setenv(COMPAT_UNTIL_ENV, "2099-12-31")
    dirs = await discover_compat_dirs(
        NasWorkspaceStore(root=str(tmp_path)), tenant_id=_TENANT, user_id=_USER, view=_CONV1
    )
    # own/ 本对话自己有内容;notes.md 是文件;.hidden / uploads / threads 不映射
    assert dirs == ("stub", "style")


async def test_a_root_dir_whose_name_is_not_utf8_is_never_mapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review 4 —— 老对话在 agent 根解压过 Windows zip,留下 GBK 名字的目录(``资`` 的 GBK
    字节 ``\\xd7\\xca``)。``os.listdir`` 给的是孤立代理字符;映射它会让 ACS 命令串在 e2b
    的 protobuf 编码上 ``UnicodeEncodeError``,这个 agent 的每次 exec 都失败。跳过它,其余照映射。"""
    _compat_tree(tmp_path)
    bad = os.fsencode(_agent_root(tmp_path)) + b"/\xd7\xca"
    try:
        os.mkdir(bad)
    except OSError as exc:  # macOS APFS 拒绝非 UTF-8 文件名
        pytest.skip(f"filesystem refuses a non-UTF-8 name: {exc}")
    monkeypatch.setenv(COMPAT_UNTIL_ENV, "2099-12-31")
    dirs = await discover_compat_dirs(
        NasWorkspaceStore(root=str(tmp_path)), tenant_id=_TENANT, user_id=_USER, view=_CONV1
    )
    assert dirs == ("stub", "style")
    assert all(d.encode("utf-8") for d in dirs)


@pytest.mark.parametrize("name", ["\udcd7\udcca", "a\udcff"])
def test_a_name_that_is_not_utf8_is_not_a_compat_dir_name(name: str) -> None:
    """平台无关的那一半:名单校验(宿主侧发现与 ``exec_view_argv`` 共用)拒绝它。"""
    from orchestrator.tools.exec_view import exec_view_argv
    from orchestrator.tools.workspace_compat import _mappable
    from orchestrator.tools.workspace_paths import require_compat_dir_name

    with pytest.raises(ValueError, match="UTF-8"):
        require_compat_dir_name(name)
    assert _mappable(name) is False
    with pytest.raises(ValueError, match="UTF-8"):
        exec_view_argv(_KEY, ["python"], conv_id=_C1, compat_dirs=(name,))


@pytest.mark.parametrize("until", ["2000-01-01", "", "not-a-date"])
async def test_closed_or_unset_window_maps_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, until: str
) -> None:
    _compat_tree(tmp_path)
    monkeypatch.setenv(COMPAT_UNTIL_ENV, until)
    dirs = await discover_compat_dirs(
        NasWorkspaceStore(root=str(tmp_path)), tenant_id=_TENANT, user_id=_USER, view=_CONV1
    )
    assert dirs == ()


async def test_legacy_view_maps_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _compat_tree(tmp_path)
    monkeypatch.setenv(COMPAT_UNTIL_ENV, "2099-12-31")
    dirs = await discover_compat_dirs(
        NasWorkspaceStore(root=str(tmp_path)), tenant_id=_TENANT, user_id=_USER, view=_LEGACY
    )
    assert dirs == ()


async def test_compat_dirs_are_read_only_for_the_file_tools(tmp_path: Path) -> None:
    """B-236 M4 —— 过渡期目录只读,宿主侧与 exec 一致:``write_file`` / ``edit_file`` 写
    ``style/…`` 被拒,拒绝说明里给出两条能走的路(本对话自己的目录 / ``/agent/style/…``);
    读照常;改走 ``/agent/style/…`` 的写照样落 agent 根。"""
    from orchestrator.tools import EditFileTool

    _compat_tree(tmp_path)
    runtime = _ViewRuntime(tmp_path)
    store = NasWorkspaceStore(root=str(tmp_path))
    ctx = _ctx(_C1, compat=("style",))
    anchor = _agent_root(tmp_path) / "style" / "a.md"

    for tool, args in (
        (WriteFileTool(client=runtime), {"path": "style/a.md", "content": "B"}),
        (WriteFileTool(client=runtime), {"path": "/workspace/style/new.md", "content": "B"}),
        (
            EditFileTool(client=runtime),
            {"path": "style/a.md", "old_string": "a", "new_string": "B"},
        ),
    ):
        with pytest.raises(ValueError, match="read-only") as refused:
            await tool.call(args, ctx=ctx)
        assert "/agent/style/" in str(refused.value)
        assert "conversation's own directory" in str(refused.value)
    assert runtime.exec_views == []  # 被拒的写一次 exec 都没发
    assert anchor.read_text() == "a"
    assert not (_work(tmp_path, _C1) / "style").exists()

    assert (await ReadFileTool(store=store).call({"path": "style/a.md"}, ctx=ctx)).content == "a"
    await WriteFileTool(client=runtime).call({"path": "/agent/style/a.md", "content": "B"}, ctx=ctx)
    assert anchor.read_text() == "B"


def test_compat_read_only_rule_is_only_for_the_writing_file_tools() -> None:
    mapped = replace(_CONV1, compat_dirs=("style",))
    for tool in ("read_file", "list_dir", "read_document", "read_page", "save_artifact"):
        assert resolve_workspace_path("style/a.md", view=mapped, tool=tool).kind == "compat"
    for tool in ("write_file", "edit_file"):
        with pytest.raises(ValueError, match="read-only"):
            resolve_workspace_path("style/a.md", view=mapped, tool=tool)
        # 未映射时同名路径是本对话自己的目录,照常可写
        assert resolve_workspace_path("style/a.md", view=_CONV1, tool=tool).kind == "conversation"


def test_a_malformed_window_is_warned_about_once(caplog: pytest.LogCaptureFixture) -> None:
    from orchestrator.tools.workspace_compat import compat_until

    with caplog.at_level("WARNING", logger="orchestrator.tools.workspace_compat"):
        for _ in range(3):
            assert compat_until("2026-13-99") is None
        assert compat_until("2026-12-10") is not None
    assert [r.getMessage().count("2026-13-99") for r in caplog.records] == [1]


def _hits(channel: str) -> float:
    return float(compat_dir_hits_total.labels(channel=channel, agent_key=_KEY)._value.get())


async def test_compat_hits_are_counted_for_file_tools_and_exec_refs(tmp_path: Path) -> None:
    _compat_tree(tmp_path)
    store = NasWorkspaceStore(root=str(tmp_path))
    ctx = _ctx(_C1, compat=("style",))
    before_file, before_exec = _hits("file_tool"), _hits("exec_ref")

    result = await ReadFileTool(store=store).call({"path": "style/a.md"}, ctx=ctx)
    await BashTool(client=RecordingSandboxRuntime()).call(
        {"command": "ls style/ && cat notes.md"}, ctx=ctx
    )

    assert result.content == "a"
    assert _hits("file_tool") == before_file + 1
    assert _hits("exec_ref") == before_exec + 1


def test_logged_dir_names_keep_only_their_shape() -> None:
    assert name_shape("style") == "<A>"
    assert name_shape("客户案例2024") == "<H><N>"
    assert name_shape("v2_draft") == "<A><N>_<A>"


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
    assert runtime.exec_views[0] == (_C1, ())
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
        view=replace(_CONV1, compat_dirs=("style",)),
    )
    assert block.index(CONVERSATION_SECTION) < block.index(AGENT_SECTION)
    assert "mine.md" in block
    assert "anchor.md" in block
    assert "Also readable, read-only, by their old relative path" in block
    assert block.rstrip().endswith(": style/")
    assert "theirs.md" not in block
    assert "PLAN.md" not in block
    assert "threads" not in block


def test_every_compat_dir_gets_the_read_only_note_whatever_its_shape() -> None:
    """review 4 LOW-1 —— 目录行按文件的父路径分组:空的过渡期目录、只有更深层文件的过渡期
    目录(``output/2026-10/r.docx``)都没有自己那一行,说明不能挂在目录行上。"""
    from orchestrator.tools.workspace_store import WorkspaceFileEntry
    from orchestrator.tools.workspace_tree import render_conversation_block

    agent = [
        WorkspaceFileEntry(path="style/anchor.md", size=1, mtime=None),
        WorkspaceFileEntry(path="output/2026-10/r.docx", size=1, mtime=None),
    ]
    block = render_conversation_block(
        [], agent, compat_dirs=("empty", "output", "style"), compat_until_text="2026-12-10"
    )
    note = block.rsplit("\n", 1)[1]
    assert note == (
        "  Also readable, read-only, by their old relative path until 2026-12-10: "
        "empty/, output/, style/"
    )
    assert "read-only" not in render_conversation_block([], agent)


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


async def test_compat_discovery_on_a_brand_new_agent_is_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(COMPAT_UNTIL_ENV, "2099-12-31")
    with caplog.at_level("WARNING"):
        dirs = await discover_compat_dirs(
            NasWorkspaceStore(root=str(tmp_path)), tenant_id=_TENANT, user_id=_USER, view=_CONV1
        )
    assert dirs == ()
    assert caplog.records == []


async def test_compat_discovery_failure_logs_the_error_type_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from orchestrator.tools.sandbox import SandboxSupervisorError
    from orchestrator.tools.workspace_store import RecordingWorkspaceStore

    monkeypatch.setenv(COMPAT_UNTIL_ENV, "2099-12-31")
    store = RecordingWorkspaceStore(
        workspace_list_error=SandboxSupervisorError("listing failed: 'agents/k/客户甲'")
    )
    with caplog.at_level("WARNING"):
        dirs = await discover_compat_dirs(store, tenant_id=_TENANT, user_id=_USER, view=_CONV1)
    assert dirs == ()
    (record,) = caplog.records
    assert record.exc_info is None
    assert "客户甲" not in record.getMessage()
    assert "SandboxSupervisorError" in record.getMessage()
