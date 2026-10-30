"""B-236 review 7 —— 沙箱能在工作区里种 FIFO:宿主侧的读 / 写 / 搜都不能被它卡住。

不带 ``O_NONBLOCK`` 时 ``open`` 一直等另一端,一条执行器线程就此卡死(每个新对话开场读一次
记忆索引就卡一条)。用例都有超时;超时时由测试自己打开 FIFO 的另一端把卡住的线程放掉,再判红。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import HumanMessage

from orchestrator.graph_builder.agent_notes import make_agent_notes_node
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from orchestrator.tools.sandbox import SandboxSupervisorError, WorkspaceNotAFileError

_TENANT, _USER = uuid4(), uuid4()
_KEY = "plan-aaaaaaaa"


def _fifo(root: Path, rel: str) -> Path:
    path = root / str(_TENANT) / str(_USER) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(path)
    return path


async def _promptly[T](fifo: Path, awaitable: Awaitable[T]) -> T:
    try:
        return await asyncio.wait_for(awaitable, timeout=3)
    except TimeoutError:
        for flags in (os.O_WRONLY | os.O_NONBLOCK, os.O_RDONLY | os.O_NONBLOCK):
            with contextlib.suppress(OSError):
                os.close(os.open(fifo, flags))  # 放掉卡住的线程
        pytest.fail("a FIFO in the workspace blocked a host-side open")


async def test_reading_a_fifo_is_refused_promptly(tmp_path: Path) -> None:
    fifo = _fifo(tmp_path, f"agents/{_KEY}/AGENT_NOTES.md")
    store = NasWorkspaceStore(root=str(tmp_path))
    for stable in (False, True):
        with pytest.raises(WorkspaceNotAFileError):
            await _promptly(
                fifo,
                store.read_file(
                    tenant_id=_TENANT,
                    user_id=_USER,
                    path=f"agents/{_KEY}/AGENT_NOTES.md",
                    stable=stable,
                ),
            )


async def test_writing_over_a_fifo_fails_promptly(tmp_path: Path) -> None:
    fifo = _fifo(tmp_path, "uploads/report.pdf")
    store = NasWorkspaceStore(root=str(tmp_path))
    with pytest.raises(SandboxSupervisorError):
        await _promptly(
            fifo,
            store.write_file(
                tenant_id=_TENANT, user_id=_USER, path="uploads/report.pdf", data=b"x"
            ),
        )


async def test_content_search_skips_a_fifo(tmp_path: Path) -> None:
    fifo = _fifo(tmp_path, "notes/pipe.md")
    (fifo.parent / "real.md").write_text("needle")
    store = NasWorkspaceStore(root=str(tmp_path))
    result = await _promptly(
        fifo, store.search_files(tenant_id=_TENANT, user_id=_USER, content="needle")
    )
    assert [e.path for e in result.entries] == ["notes/real.md"]


async def test_a_fifo_index_never_hangs_the_notes_node(tmp_path: Path) -> None:
    fifo = _fifo(tmp_path, f"agents/{_KEY}/AGENT_NOTES.md")
    node = make_agent_notes_node(store=NasWorkspaceStore(root=str(tmp_path)))
    config: dict[str, Any] = {
        "configurable": {
            "tenant_id": str(_TENANT),
            "user_id": str(_USER),
            "agent_key": _KEY,
            "thread_id": str(UUID(int=1)),
            "workspace_layout": "conversation",
        }
    }
    state: Any = {"messages": [HumanMessage(content="hi")]}
    assert await _promptly(fifo, node(state, config)) == {}  # type: ignore[arg-type]
