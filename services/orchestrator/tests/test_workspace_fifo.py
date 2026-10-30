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


async def test_read_file_tool_calls_a_fifo_not_a_regular_file(tmp_path: Path) -> None:
    """review 8 —— 说它是目录是假话(模型会去 list_dir):如实说不是普通文件。"""
    from orchestrator.tools import FileOpError, ReadFileTool, ToolContext

    fifo = _fifo(tmp_path, f"agents/{_KEY}/notes.md")
    ctx = ToolContext(tenant_id=_TENANT, user_id=_USER, agent_key=_KEY)
    with pytest.raises(FileOpError, match="not_a_regular_file"):
        await _promptly(
            fifo,
            ReadFileTool(store=NasWorkspaceStore(root=str(tmp_path))).call(
                {"path": "notes.md"}, ctx=ctx
            ),
        )


# ---------------------------------------------------------------------------
# review 9 —— 遍历深度封顶、write_file 的 fstat 失败不漏 fd
# ---------------------------------------------------------------------------


def _deep(root: Path, depth: int) -> None:
    d = root / str(_TENANT) / str(_USER) / "deep"
    for _ in range(depth):
        d = d / "d"
    d.mkdir(parents=True)
    (d / "leaf.txt").write_text("x")
    (root / str(_TENANT) / str(_USER) / "shallow.txt").write_text("y")


async def test_listing_stops_descending_past_the_depth_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestrator.tools import nas_workspace_store as nws

    monkeypatch.setattr(nws, "MAX_WALK_DEPTH", 8)
    _deep(tmp_path, 20)
    files = await NasWorkspaceStore(root=str(tmp_path)).list_files(tenant_id=_TENANT, user_id=_USER)
    assert [e.path for e in files] == ["shallow.txt"]


async def test_search_past_the_depth_cap_is_reported_too_deep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestrator.tools import nas_workspace_store as nws

    monkeypatch.setattr(nws, "MAX_WALK_DEPTH", 8)
    _deep(tmp_path, 20)
    result = await NasWorkspaceStore(root=str(tmp_path)).search_files(
        tenant_id=_TENANT, user_id=_USER, name_glob="*.txt"
    )
    assert [e.path for e in result.entries] == ["shallow.txt"]
    assert result.too_deep is True
    assert result.truncated is False  # 「命中太多没列」是另一回事


@pytest.mark.parametrize("hits", [0, 1])
def test_search_text_says_too_deep_apart_from_more_matches(hits: int) -> None:
    """review 10 —— 太深没搜单独一行,零命中时也说(那时「没找到」≠「没有」)。"""
    from orchestrator.tools.file_ops import _format_search
    from orchestrator.tools.workspace_store import WorkspaceFileEntry, WorkspaceSearchResult

    entries = tuple(WorkspaceFileEntry(path="a.txt", size=1) for _ in range(hits))
    text = _format_search(WorkspaceSearchResult(entries=entries, truncated=False, too_deep=True))

    assert "nested too deep to search" in text
    assert "more matches not listed" not in text


async def test_write_file_closes_its_fd_when_fstat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestrator.tools import nas_workspace_store as nws

    opened: list[int] = []
    closed: list[int] = []
    real_open, real_close, real_fstat = os.open, os.close, os.fstat

    def spy_open(*args: Any, **kwargs: Any) -> int:
        fd = real_open(*args, **kwargs)
        if args and args[0] == "out.txt":
            opened.append(fd)
        return fd

    def spy_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    def failing_fstat(fd: int) -> os.stat_result:
        if fd in opened:
            raise OSError(5, "EIO")
        return real_fstat(fd)

    monkeypatch.setattr(nws.os, "open", spy_open)
    monkeypatch.setattr(nws.os, "close", spy_close)
    monkeypatch.setattr(nws.os, "fstat", failing_fstat)
    with pytest.raises(SandboxSupervisorError):
        await NasWorkspaceStore(root=str(tmp_path)).write_file(
            tenant_id=_TENANT, user_id=_USER, path="out.txt", data=b"x"
        )
    assert opened and opened[0] in closed


async def test_read_file_never_returns_more_than_the_cap_of_a_growing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review 10 —— fstat 之后文件还在长:最多读 cap + 1,超了照样报太大,不把整个文件读进来。"""
    from orchestrator.tools import nas_workspace_store as nws
    from orchestrator.tools.sandbox import WorkspaceFileTooLargeError

    f = tmp_path / str(_TENANT) / str(_USER) / "grow.txt"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x" * 5_000)
    real_fstat = os.fstat

    def stale_fstat(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        if info.st_size == 5_000:  # 假装 fstat 那一刻文件还只有 10 字节
            fields = list(info)
            fields[6] = 10
            return os.stat_result(fields)
        return info

    sizes: list[int] = []
    real_fdopen = os.fdopen

    class _Spy:
        def __init__(self, handle: Any) -> None:
            self._h = handle

        def __enter__(self) -> _Spy:
            self._h.__enter__()
            return self

        def __exit__(self, *exc: object) -> None:
            self._h.__exit__(*exc)

        def fileno(self) -> int:
            return int(self._h.fileno())

        def read(self, size: int = -1) -> bytes:
            sizes.append(size)
            return bytes(self._h.read(size))

    monkeypatch.setattr(nws.os, "fstat", stale_fstat)
    monkeypatch.setattr(nws.os, "fdopen", lambda *a, **k: _Spy(real_fdopen(*a, **k)))
    with pytest.raises(WorkspaceFileTooLargeError):
        await NasWorkspaceStore(root=str(tmp_path)).read_file(
            tenant_id=_TENANT, user_id=_USER, path="grow.txt", max_bytes=100
        )
    assert sizes == [101]  # 只读上限加一个字节,不读整个文件
