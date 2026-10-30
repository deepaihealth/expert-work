"""B-236 follow-up — the write lock's scope follows the conversation's workspace layout.

A conversation on the new layout has its own ``/workspace``; its writes are locked per
conversation (``conversation_id`` = the conversation that owns the directory). An old-layout
conversation shares the agent root with every other old-layout conversation of the user, so it
keeps the per-user lock (``conversation_id=None``). A delegated child writes into its parent's
directory, so it locks on the parent conversation, not on its own sub-thread.

The real Postgres behaviour (two conversations of one user hold the lock at once) is in the
control-plane integration test; here a recording lock checks what each write tool passes.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from orchestrator.graph_builder.builder import _build_tool_context
from orchestrator.tools import (
    BashTool,
    EditFileTool,
    RecordingSandboxRuntime,
    RecordingWorkspaceLock,
    SandboxOutcome,
    ToolContext,
    WriteFileTool,
)
from orchestrator.tools._child_run import _child_config
from orchestrator.tools.locks import NullWorkspaceLock, write_lock_conversation

_OK_WRITE = '{"ok": true, "content_hash": "h", "size": 1, "path": "a.txt"}'


def _ctx(*, layout: str, agent_key: str = "report-agent", **kw: Any) -> ToolContext:
    return ToolContext(
        tenant_id=uuid4(),
        run_id=uuid4(),
        user_id=uuid4(),
        thread_id=kw.pop("thread_id", uuid4()),
        agent_key=agent_key,
        workspace_layout=layout,  # type: ignore[arg-type]
        **kw,
    )


async def _call(
    name: str, lock: RecordingWorkspaceLock, ctx: ToolContext, *, path: str = "a.txt"
) -> RecordingSandboxRuntime:
    client = RecordingSandboxRuntime()
    client.outcome = SandboxOutcome(stdout=_OK_WRITE, stderr="", exit_code=0, timed_out=False)
    if name == "bash":
        await BashTool(client=client, workspace_lock=lock).call({"command": "true"}, ctx=ctx)
    elif name == "write_file":
        await WriteFileTool(client=client, workspace_lock=lock).call(
            {"path": path, "content": "x"}, ctx=ctx
        )
    else:
        await EditFileTool(client=client, workspace_lock=lock).call(
            {"path": path, "old_string": "a", "new_string": "b"}, ctx=ctx
        )
    return client


_WRITE_TOOLS = ("bash", "write_file", "edit_file")


@pytest.mark.parametrize("name", _WRITE_TOOLS)
async def test_new_layout_locks_on_its_conversation(name: str) -> None:
    lock = RecordingWorkspaceLock()
    ctx = _ctx(layout="conversation")
    client = await _call(name, lock, ctx)
    assert lock.acquired == [(ctx.tenant_id, ctx.user_id)]
    assert lock.conversations == [ctx.thread_id]
    # the lock covers the directory the exec actually wrote into
    assert lock.conversations == client.exec_views


@pytest.mark.parametrize("name", _WRITE_TOOLS)
async def test_old_layout_keeps_the_per_user_lock(name: str) -> None:
    lock = RecordingWorkspaceLock()
    ctx = _ctx(layout="agent_root")
    await _call(name, lock, ctx)
    assert lock.acquired == [(ctx.tenant_id, ctx.user_id)]
    assert lock.conversations == [None]


@pytest.mark.parametrize("name", _WRITE_TOOLS)
async def test_new_layout_without_an_agent_is_the_old_view(name: str) -> None:
    # 没绑 agent 时 exec 落的是老视图(整个用户根),锁也必须按用户。
    lock = RecordingWorkspaceLock()
    ctx = _ctx(layout="conversation", agent_key="")
    await _call(name, lock, ctx)
    assert lock.conversations == [None]


@pytest.mark.parametrize("name", _WRITE_TOOLS)
async def test_child_of_new_layout_locks_on_the_parent_conversation(name: str) -> None:
    parent = _ctx(layout="conversation")
    child = _build_tool_context(_child_config(parent, sub_thread_id=uuid4(), sub_run_id=uuid4()))
    grandchild = _build_tool_context(
        _child_config(child, sub_thread_id=uuid4(), sub_run_id=uuid4())
    )
    assert child.thread_id != parent.thread_id  # the child has its own sub-thread
    lock = RecordingWorkspaceLock()
    await _call(name, lock, child)
    await _call(name, lock, grandchild)
    assert lock.conversations == [parent.thread_id, parent.thread_id]
    assert lock.acquired == [(parent.tenant_id, parent.user_id)] * 2


@pytest.mark.parametrize("name", _WRITE_TOOLS)
async def test_child_of_old_layout_keeps_the_per_user_lock(name: str) -> None:
    parent = _ctx(layout="agent_root")
    child = _build_tool_context(_child_config(parent, sub_thread_id=uuid4(), sub_run_id=uuid4()))
    lock = RecordingWorkspaceLock()
    await _call(name, lock, child)
    assert lock.conversations == [None]


def test_lock_scope_is_the_exec_directory_owner() -> None:
    # 锁的对话 id 与 exec 落盘的目录取自同一个视图 —— 不会锁一个目录、写另一个。
    root = uuid4()
    ctx = _ctx(layout="conversation", thread_id=uuid4(), workspace_thread_id=root)
    assert write_lock_conversation(ctx.workspace_view) == ctx.workspace_view.conv_id == root
    assert write_lock_conversation(_ctx(layout="agent_root").workspace_view) is None


async def test_null_lock_accepts_a_conversation() -> None:
    conv: UUID = uuid4()
    async with NullWorkspaceLock().acquire(
        tenant_id=uuid4(), user_id=uuid4(), conversation_id=conv
    ):
        pass


# ---------------------------------------------------------------------------
# The agent-wide area. write_file / edit_file into a file every conversation of the
# agent can reach also take the agent key, so two conversations' edits of one
# ``/agent`` file cannot silently lose one. bash never takes it.
# ---------------------------------------------------------------------------

_FILE_TOOLS = ("write_file", "edit_file")


@pytest.mark.parametrize("name", _FILE_TOOLS)
@pytest.mark.parametrize("path", ["/agent/AGENT_NOTES.md", "/agent/templates/a.md", "uploads/a.md"])
async def test_new_layout_file_write_into_the_agent_area_adds_the_agent_key(
    name: str, path: str
) -> None:
    lock = RecordingWorkspaceLock()
    ctx = _ctx(layout="conversation")
    await _call(name, lock, ctx, path=path)
    assert lock.conversations == [ctx.thread_id]
    assert lock.agent_keys == ["report-agent"]


@pytest.mark.parametrize("name", _FILE_TOOLS)
@pytest.mark.parametrize("path", ["a.md", "/workspace/out/a.md"])
async def test_new_layout_file_write_into_its_own_workspace_takes_no_agent_key(
    name: str, path: str
) -> None:
    lock = RecordingWorkspaceLock()
    ctx = _ctx(layout="conversation")
    await _call(name, lock, ctx, path=path)
    assert lock.agent_keys == [None]


@pytest.mark.parametrize("name", _FILE_TOOLS)
@pytest.mark.parametrize("path", ["a.md", "/agent/a.md"])
async def test_old_layout_file_write_adds_the_agent_key(name: str, path: str) -> None:
    # 老视图的 /workspace 就是 agent 根 —— 与新视图的 /agent 是同一批文件。
    lock = RecordingWorkspaceLock()
    ctx = _ctx(layout="agent_root")
    await _call(name, lock, ctx, path=path)
    assert lock.conversations == [None]
    assert lock.agent_keys == ["report-agent"]


@pytest.mark.parametrize("layout", ["conversation", "agent_root"])
async def test_bash_never_takes_the_agent_key(layout: str) -> None:
    lock = RecordingWorkspaceLock()
    await _call("bash", lock, _ctx(layout=layout))
    assert lock.agent_keys == [None]


@pytest.mark.parametrize("name", _FILE_TOOLS)
async def test_no_agent_bound_takes_no_agent_key(name: str) -> None:
    lock = RecordingWorkspaceLock()
    await _call(name, lock, _ctx(layout="conversation", agent_key=""))
    assert lock.agent_keys == [None]
