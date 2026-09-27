"""B-124 —— 委派出去的子代(worker / 静态子 Agent)在沙箱里用**父的** agent_key。

线上现象(测试集群 2026-09-27):worker ``write_file("layout_design.md")`` 落在
``agents/<父名>-worker-<hash>/``,父随后 ``read_file`` 同名文件 not_found;而同一个
worker ``read_file("style/PLAN_STYLE.md")`` 读到的却是父的目录。

根因是一个子 run 里有两个 key:宿主侧读工具用 ``ctx.agent_key``(子代透传父的,
``_child_run._child_config``),沙箱侧 exec 用构建期绑死的 ``sanitize_agent_key(子代
spec 名)``(``agent_factory`` → ``bind_agent_key``)。修法:exec 走 ``ctx.agent_key``,
技能种子按同一个 key 落盘,``EXPERT_WORK_SKILLS_DIR`` 于是还指得到它们。
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import SANDBOX_SKILLS_ROOT
from orchestrator.graph_builder.builder import _build_tool_context
from orchestrator.tools import ToolContext
from orchestrator.tools._child_run import _child_config
from orchestrator.tools.file_ops import ReadFileTool, WriteFileTool
from orchestrator.tools.sandbox import (
    RecordingSandboxRuntime,
    SandboxOutcome,
    agent_key_envs,
    bind_agent_key,
    run_in_sandbox,
)
from orchestrator.tools.workspace_store import RecordingWorkspaceStore, WorkspaceFileEntry

_PARENT_KEY = "ai-health-plan-30817804"
_CHILD_KEY = "ai-health-plan-worker-d5acbc25"
_OK_WRITE = json.dumps({"ok": True, "content_hash": "h", "size": 2, "path": "x"})


def _parent_ctx() -> ToolContext:
    return ToolContext(tenant_id=uuid4(), run_id=uuid4(), user_id=uuid4(), agent_key=_PARENT_KEY)


def _child_ctx() -> ToolContext:
    """真实的委派通道造出来的子代 ctx —— 不是手填 agent_key 的假 ctx。"""
    return _build_tool_context(
        _child_config(_parent_ctx(), sub_thread_id=uuid4(), sub_run_id=uuid4())
    )


def _recording(stdout: str = _OK_WRITE) -> RecordingSandboxRuntime:
    return RecordingSandboxRuntime(
        outcome=SandboxOutcome(stdout=stdout, stderr="", exit_code=0, timed_out=False)
    )


# --- 1. binding client -------------------------------------------------------


@pytest.mark.asyncio
async def test_binding_client_uses_caller_key_when_given() -> None:
    inner = _recording()
    client = bind_agent_key(inner, _CHILD_KEY)
    sid = await client.acquire(tenant_id=uuid4(), thread_id="t")

    await client.exec(sandbox_id=sid, code="pass", timeout_s=5, agent_key=_PARENT_KEY)

    assert inner.exec_agent_keys == [_PARENT_KEY]


@pytest.mark.asyncio
async def test_binding_client_falls_back_to_bound_key_on_empty_caller_key() -> None:
    inner = _recording()
    client = bind_agent_key(inner, _CHILD_KEY)
    sid = await client.acquire(tenant_id=uuid4(), thread_id="t")

    await client.exec(sandbox_id=sid, code="pass", timeout_s=5, agent_key="")

    assert inner.exec_agent_keys == [_CHILD_KEY]


# --- 2. run_in_sandbox -------------------------------------------------------


@pytest.mark.asyncio
async def test_run_in_sandbox_passes_ctx_agent_key_to_exec() -> None:
    client = _recording()
    ctx = _parent_ctx()

    await run_in_sandbox(
        client, code="pass", timeout_s=5, ctx=ctx, tool_label="t", fallback_thread_id="t"
    )

    assert client.exec_agent_keys == [_PARENT_KEY]


# --- 3. the bug at the tool layer -------------------------------------------


@pytest.mark.asyncio
async def test_child_write_file_execs_under_the_parent_key() -> None:
    """子代构建绑的是自己的 key;经委派通道拿到的 ctx 带父的 key —— exec 必须用后者。"""
    inner = _recording()
    child_client = bind_agent_key(inner, _CHILD_KEY)

    await WriteFileTool(client=child_client).call(
        {"path": "layout_design.md", "content": "hi"}, ctx=_child_ctx()
    )

    assert inner.exec_agent_keys == [_PARENT_KEY]


class _ViewRuntime(RecordingSandboxRuntime):
    """模拟 B-60 的逐 exec 视图:``/workspace`` = 用户根下的 ``agents/<exec 的 agent_key>``。

    真的执行 write 片段,把文件写到磁盘上 —— 这样「父读得到子写的文件」是
    从落盘位置推出来的,不是从某个 key 字符串相等推出来的。
    """

    def __init__(self, user_root: Path) -> None:
        super().__init__()
        self.user_root = user_root

    async def exec(
        self,
        *,
        sandbox_id: UUID,
        code: str,
        timeout_s: int | None,
        agent_key: str = "",
        run_id: UUID | None = None,
    ) -> SandboxOutcome:
        self.exec_agent_keys.append(agent_key)
        view = self.user_root / "agents" / agent_key
        view.mkdir(parents=True, exist_ok=True)
        marker = '"ws": "/workspace"'
        assert marker in code
        rewritten = code.replace(marker, f'"ws": {json.dumps(str(view))}')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(rewritten, {})  # noqa: S102 — snippet is built from a fixed template
        return SandboxOutcome(stdout=buf.getvalue(), stderr="", exit_code=0, timed_out=False)


def _store_over(user_root: Path) -> RecordingWorkspaceStore:
    files: dict[str, bytes] = {
        p.relative_to(user_root).as_posix(): p.read_bytes()
        for p in user_root.rglob("*")
        if p.is_file()
    }
    return RecordingWorkspaceStore(
        workspace_files=[WorkspaceFileEntry(path=k, size=len(v)) for k, v in sorted(files.items())],
        workspace_file_contents=files,
    )


@pytest.mark.asyncio
async def test_parent_reads_the_file_its_child_wrote(tmp_path: Path) -> None:
    runtime = _ViewRuntime(tmp_path)
    child_client = bind_agent_key(runtime, _CHILD_KEY)

    await WriteFileTool(client=child_client).call(
        {"path": "layout_design.md", "content": "layout"}, ctx=_child_ctx()
    )
    result = await ReadFileTool(store=_store_over(tmp_path)).call(
        {"path": "layout_design.md"}, ctx=_parent_ctx()
    )

    assert result.content == "layout"
    assert not (tmp_path / "agents" / _CHILD_KEY).exists()


# --- 4. main-run invariance --------------------------------------------------


@pytest.mark.asyncio
async def test_main_run_exec_key_and_seeds_unchanged() -> None:
    """主 run:ctx.agent_key == 构建期绑的 key → exec key 与种子逐字节不变。"""
    inner = _recording()
    client = bind_agent_key(inner, _PARENT_KEY)
    seeds = ((f"{_PARENT_KEY}/pptx/SKILL.md", b"s"), (f"{_PARENT_KEY}/pptx/a/b.py", b"b"))

    await WriteFileTool(client=client, skill_seed_files=seeds).call(
        {"path": "x.md", "content": "hi"}, ctx=_parent_ctx()
    )

    assert inner.exec_agent_keys == [_PARENT_KEY]
    assert inner.acquired[0][3] == seeds


@pytest.mark.asyncio
async def test_unbound_ctx_keeps_bound_key_and_seeds() -> None:
    """没有 ctx.agent_key 的调用方(inputs 节点 / 合成评测)仍走构建期绑的 key。"""
    inner = _recording()
    client = bind_agent_key(inner, _CHILD_KEY)
    seeds = ((f"{_CHILD_KEY}/pptx/SKILL.md", b"s"),)
    ctx = ToolContext(tenant_id=uuid4(), run_id=uuid4(), user_id=uuid4())

    await WriteFileTool(client=client, skill_seed_files=seeds).call(
        {"path": "x.md", "content": "hi"}, ctx=ctx
    )

    assert inner.exec_agent_keys == [_CHILD_KEY]
    assert inner.acquired[0][3] == seeds


# --- 5. skills consistency for a child ---------------------------------------


def _skills_dir(envs: dict[str, Any]) -> str:
    return str(envs["EXPERT_WORK_SKILLS_DIR"])


@pytest.mark.asyncio
async def test_child_skill_seeds_land_where_its_skills_dir_points() -> None:
    """子代构建按自己的 key 生成种子(``build_skill_seed_files(agent_key=子 key)``);
    exec 换成父 key 之后 ``EXPERT_WORK_SKILLS_DIR`` 指父 key —— 种子必须跟着落到那里,
    否则 ``python "$EXPERT_WORK_SKILLS_DIR/pptx/scripts/x.py"`` 就是 No such file。"""
    inner = _recording()
    child_client = bind_agent_key(inner, _CHILD_KEY)
    seeds = ((f"{_CHILD_KEY}/pptx/SKILL.md", b"s"), (f"{_CHILD_KEY}/pptx/scripts/x.py", b"x"))

    await WriteFileTool(client=child_client, skill_seed_files=seeds).call(
        {"path": "x.md", "content": "hi"}, ctx=_child_ctx()
    )

    skills_dir = _skills_dir(agent_key_envs(inner.exec_agent_keys[0]))
    assert skills_dir == f"{SANDBOX_SKILLS_ROOT}/{_PARENT_KEY}"
    assert inner.acquired[0][3] == (
        (f"{_PARENT_KEY}/pptx/SKILL.md", b"s"),
        (f"{_PARENT_KEY}/pptx/scripts/x.py", b"x"),
    )
    for rel, _ in inner.acquired[0][3]:
        assert f"{SANDBOX_SKILLS_ROOT}/{rel}".startswith(skills_dir + "/")
