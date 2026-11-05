"""B-262 —— ``WorkspaceStore.create_file``:名字上没有东西才建,判在不在与创建是同一个动作。

上传用它落盘,同一智能体不同对话传同名文件时后一个换名字,不覆盖前一个。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from orchestrator.tools.sandbox import SandboxSupervisorError, WorkspaceFileExistsError
from orchestrator.tools.workspace_store import RecordingWorkspaceStore, SupervisorWorkspaceStore

_PATH = "agents/support-bot/uploads/report.pdf"


@pytest.mark.anyio
async def test_nas_create_file_writes_a_new_file(tmp_path: Path) -> None:
    store = NasWorkspaceStore(root=str(tmp_path))
    tenant, user = uuid4(), uuid4()

    await store.create_file(tenant_id=tenant, user_id=user, path=_PATH, data=b"A")

    assert (tmp_path / str(tenant) / str(user) / _PATH).read_bytes() == b"A"


@pytest.mark.anyio
async def test_nas_create_file_refuses_an_existing_name_and_leaves_it_untouched(
    tmp_path: Path,
) -> None:
    store = NasWorkspaceStore(root=str(tmp_path))
    tenant, user = uuid4(), uuid4()
    await store.create_file(tenant_id=tenant, user_id=user, path=_PATH, data=b"customer A")

    with pytest.raises(WorkspaceFileExistsError):
        await store.create_file(tenant_id=tenant, user_id=user, path=_PATH, data=b"customer B")

    assert (tmp_path / str(tenant) / str(user) / _PATH).read_bytes() == b"customer A"


@pytest.mark.anyio
async def test_nas_create_file_refuses_a_symlink_planted_on_the_name(tmp_path: Path) -> None:
    """名字上是一条符号链接(沙箱能种)—— 算「已存在」,不顺着它写到别处。"""
    store = NasWorkspaceStore(root=str(tmp_path))
    tenant, user = uuid4(), uuid4()
    target = tmp_path / "outside.txt"
    target.write_bytes(b"keep")
    leaf = tmp_path / str(tenant) / str(user) / _PATH
    leaf.parent.mkdir(parents=True)
    os.symlink(target, leaf)

    with pytest.raises(WorkspaceFileExistsError):
        await store.create_file(tenant_id=tenant, user_id=user, path=_PATH, data=b"evil")

    assert target.read_bytes() == b"keep"


@pytest.mark.anyio
async def test_nas_concurrent_creates_of_one_name_let_exactly_one_win(tmp_path: Path) -> None:
    """16 个同名同时建(各自一个线程):恰好一个成功,文件内容就是它的,别的全拿到「已存在」。"""
    store = NasWorkspaceStore(root=str(tmp_path))
    tenant, user = uuid4(), uuid4()
    payloads = [f"customer-{i}".encode() * 1000 for i in range(16)]

    results = await asyncio.gather(
        *(
            store.create_file(tenant_id=tenant, user_id=user, path=_PATH, data=data)
            for data in payloads
        ),
        return_exceptions=True,
    )

    winners = [data for data, result in zip(payloads, results, strict=True) if result is None]
    losers = [result for result in results if result is not None]
    assert len(winners) == 1
    assert all(isinstance(result, WorkspaceFileExistsError) for result in losers)
    assert (tmp_path / str(tenant) / str(user) / _PATH).read_bytes() == winners[0]


@pytest.mark.anyio
async def test_nas_create_file_name_appears_only_with_the_complete_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """名字只经 ``link`` 出现:那一刻目标名还不存在、临时文件里已是完整内容;之后临时名删掉。
    别的上传读不到写了一半的文件。"""
    store = NasWorkspaceStore(root=str(tmp_path))
    tenant, user = uuid4(), uuid4()
    target = tmp_path / str(tenant) / str(user) / _PATH
    data = b"x" * 300_000
    seen: list[tuple[bool, list[bytes]]] = []
    real_link = os.link

    def watching_link(src: str, dst: str, **kwargs: object) -> None:
        tmps = [p.read_bytes() for p in target.parent.glob(".tmp-*")]
        seen.append((target.exists(), tmps))
        real_link(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "link", watching_link)
    await store.create_file(tenant_id=tenant, user_id=user, path=_PATH, data=data)

    assert seen == [(False, [data])]
    assert target.read_bytes() == data
    assert sorted(p.name for p in target.parent.iterdir()) == ["report.pdf"]


@pytest.mark.anyio
async def test_nas_create_file_failure_leaves_neither_the_name_nor_a_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """落盘失败(这里让 ``link`` 报配额满):目标名从没出现,临时文件也删了,下一次能建。"""
    store = NasWorkspaceStore(root=str(tmp_path))
    tenant, user = uuid4(), uuid4()
    target = tmp_path / str(tenant) / str(user) / _PATH
    real_link = os.link

    def failing_link(src: str, dst: str, **kwargs: object) -> None:
        raise OSError(122, "Disk quota exceeded")

    monkeypatch.setattr(os, "link", failing_link)
    with pytest.raises(SandboxSupervisorError):
        await store.create_file(tenant_id=tenant, user_id=user, path=_PATH, data=b"A")
    monkeypatch.setattr(os, "link", real_link)

    assert list(target.parent.iterdir()) == []
    await store.create_file(tenant_id=tenant, user_id=user, path=_PATH, data=b"B")
    assert target.read_bytes() == b"B"


@pytest.mark.anyio
async def test_nas_write_file_still_overwrites(tmp_path: Path) -> None:
    """``write_file`` 的语义不变(工作区写、记忆投影都靠它覆盖)。"""
    store = NasWorkspaceStore(root=str(tmp_path))
    tenant, user = uuid4(), uuid4()
    await store.write_file(tenant_id=tenant, user_id=user, path=_PATH, data=b"old")
    await store.write_file(tenant_id=tenant, user_id=user, path=_PATH, data=b"new")
    assert (tmp_path / str(tenant) / str(user) / _PATH).read_bytes() == b"new"


@pytest.mark.anyio
async def test_recording_create_file_refuses_an_existing_name_and_reads_back() -> None:
    store = RecordingWorkspaceStore()
    tenant, user = uuid4(), uuid4()
    await store.create_file(tenant_id=tenant, user_id=user, path=_PATH, data=b"A")

    with pytest.raises(WorkspaceFileExistsError):
        await store.create_file(tenant_id=tenant, user_id=user, path=_PATH, data=b"B")

    assert await store.read_file(tenant_id=tenant, user_id=user, path=_PATH) == b"A"
    assert [w[2:] for w in store.workspace_writes] == [(_PATH, b"A")]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("get_status", "expect_put"), [(404, True), (200, False)], ids=["absent", "present"]
)
async def test_supervisor_create_file_writes_only_when_absent(
    get_status: int, expect_put: bool
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(get_status, content=b"x")
        return httpx.Response(204)

    store = SupervisorWorkspaceStore(base_url="http://sup", transport=httpx.MockTransport(handler))
    tenant, user = uuid4(), uuid4()

    if expect_put:
        await store.create_file(tenant_id=tenant, user_id=user, path=_PATH, data=b"A")
        assert calls == ["GET", "PUT"]
    else:
        with pytest.raises(WorkspaceFileExistsError):
            await store.create_file(tenant_id=tenant, user_id=user, path=_PATH, data=b"A")
        assert calls == ["GET"]
