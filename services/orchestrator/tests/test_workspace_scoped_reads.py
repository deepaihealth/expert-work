"""宿主 NAS 侧作用域只读的契约测试 —— B-84 PR-2b(三个实现一套用例)。

``WorkspaceStore`` 有**三个**实现,不是两个:``NasWorkspaceStore``(生产,直读
control-plane Pod 上挂着的 NAS 树)、``SupervisorWorkspaceStore``(本地/CI,代理到
sandbox-supervisor 的 HTTP API)、``RecordingWorkspaceStore``(测试替身)。三者在
Protocol 边界上必须给同一个答案 —— 这类「谓词不同义」的 bug 单看任何一个实现自己的
测试都发现不了,本仓库在「SQL 与内存 store 谓词不同义」上栽过不止一次。

与 ``test_workspace_store_contract.py`` 的分工:那份钉的是写/删/列的既有行为,整档
打 ``integration`` marker(supervisor 档要真起一个 supervisor)。本份钉的是 PR-2b 新
加的**作用域只读**,三档都不连任何外部环境 —— supervisor 档跑在一个进程内的假
supervisor 上(``httpx.MockTransport``)—— 所以**故意不打 marker**,理应在每一次
``pytest -m "not integration"`` 里就跑到。

作用域三档逐条对着 ``exec_view.EXEC_VIEW_SCRIPT`` 抄:绑了 agent 的 ``/workspace``
是 ``agents/<agent_key>/``,``/workspace/shared`` 是只读 bind 进来的 ``shared/``,
没绑 agent 时 ``/workspace`` 就是整个用户根。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import httpx
import pytest

from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from orchestrator.tools.workspace_store import (
    RecordingWorkspaceStore,
    SupervisorWorkspaceStore,
    WorkspaceFileEntry,
    WorkspaceStore,
)

if TYPE_CHECKING:
    from collections.abc import Callable

#: 真实的 agent_key 形状 —— ``sanitize_agent_key()`` 的产物是「净化后的名字 +
#: 原名 sha256 的前 8 位」。NAS 上的目录名是这个,**不是 agent 名**:手工拼
#: ``agents/<agent 名>`` 会指向一个不存在的目录,而失败形态是「目录是空的」不是报错。
AGENT_KEY = "pf-probe-33086dc0"
OTHER_AGENT_KEY = "other-agent-deadbeef"


# ---------------------------------------------------------------------------
# 三个实现共用的「种文件 + 建 store」夹具。
#
# 种文件一律按**用户根相对路径**给(``agents/<key>/style/render_plan.py`` 这种),
# 作用域是被测对象,不能让夹具替它把前缀吃掉。
# ---------------------------------------------------------------------------


def _fake_supervisor(tree: dict[str, bytes]) -> httpx.MockTransport:
    """进程内假 supervisor —— 只实现 workspace-file 的三条路由。

    存在的理由:supervisor 档的行为(尤其是「作用域前缀是怎么拼进 HTTP 路径的」)
    必须和 NAS 档逐条对齐,而真起一个 supervisor 要 docker。假的只兜住本 store
    真正依赖的那层线协议。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/files"):
            return httpx.Response(
                200,
                json={
                    "files": [
                        {"path": rel, "size": len(data)} for rel, data in sorted(tree.items())
                    ]
                },
            )
        if path.endswith("/file"):
            rel = request.url.params.get("path", "")
            if request.method == "PUT":
                tree[rel] = request.content
                return httpx.Response(204)
            if rel not in tree:
                return httpx.Response(404, text=f"not found: {rel}")
            return httpx.Response(200, content=tree[rel])
        return httpx.Response(404, text=path)

    return httpx.MockTransport(handler)


@pytest.fixture(params=["nas", "supervisor", "recording"])
def seeded(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Callable[[dict[str, bytes]], tuple[WorkspaceStore, UUID, UUID]]:
    tenant_id, user_id = uuid4(), uuid4()

    def build(tree: dict[str, bytes]) -> tuple[WorkspaceStore, UUID, UUID]:
        if request.param == "nas":
            user_root = tmp_path / str(tenant_id) / str(user_id)
            for rel, data in tree.items():
                full = user_root / rel
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_bytes(data)
            user_root.mkdir(parents=True, exist_ok=True)
            return NasWorkspaceStore(root=str(tmp_path)), tenant_id, user_id
        if request.param == "supervisor":
            return (
                SupervisorWorkspaceStore(
                    base_url="http://sup", transport=_fake_supervisor(dict(tree))
                ),
                tenant_id,
                user_id,
            )
        store = RecordingWorkspaceStore(
            workspace_files=[
                WorkspaceFileEntry(path=rel, size=len(data), mtime=_FIXED_MTIME)
                for rel, data in sorted(tree.items())
            ]
        )
        return store, tenant_id, user_id

    return build


_FIXED_MTIME = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Task 1 —— WorkspaceFileEntry.mtime
# ---------------------------------------------------------------------------


async def test_list_files_reports_mtime(tmp_path: Path) -> None:
    """NAS 档:``mtime`` 落在写入前后的时间窗内(不断言精确相等 —— 文件系统时间
    精度不保证),并且带 UTC tzinfo。"""
    tenant_id, user_id = uuid4(), uuid4()
    user_root = tmp_path / str(tenant_id) / str(user_id)
    user_root.mkdir(parents=True)

    before = datetime.now(UTC)
    (user_root / "out.txt").write_bytes(b"hello")
    after = datetime.now(UTC)

    files = await NasWorkspaceStore(root=str(tmp_path)).list_files(
        tenant_id=tenant_id, user_id=user_id
    )

    assert len(files) == 1
    mtime = files[0].mtime
    assert mtime is not None
    assert mtime.tzinfo is not None, "时间一律带 tzinfo —— 裸 datetime 比不了"
    assert before <= mtime <= after


async def test_mtime_survives_the_listing_round_trip(
    seeded: Callable[[dict[str, bytes]], tuple[WorkspaceStore, UUID, UUID]],
) -> None:
    """三个实现都必须把 ``mtime`` 带到 Protocol 边界上。

    supervisor 档今天拿不到 mtime(它的 HTTP 列表里没有这一项,见
    ``SupervisorWorkspaceStore.list_files`` 的说明),所以判据是「要么是带 tzinfo 的
    真时间,要么明说没有」,而不是「一定有值」—— 后者会逼实现去编一个假时间,那比
    ``None`` 更坏。
    """
    store, tenant_id, user_id = seeded({"out.txt": b"hello"})

    files = await store.list_files(tenant_id=tenant_id, user_id=user_id)

    assert [f.path for f in files] == ["out.txt"]
    mtime = files[0].mtime
    assert mtime is None or mtime.tzinfo is not None


def test_workspace_file_entry_carries_mtime() -> None:
    """字段本身 —— 后续所有 task 都按这个形状取时间。"""
    entry = WorkspaceFileEntry(path="a.txt", size=1, mtime=_FIXED_MTIME)
    assert entry.mtime == _FIXED_MTIME


async def test_supervisor_store_reads_mtime_when_the_wire_carries_it() -> None:
    """前向兼容:supervisor 哪天在列表里带上 ``mtime``,这个 store 不用再改。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"files": [{"path": "a.txt", "size": 1, "mtime": 1.5}]})

    store = SupervisorWorkspaceStore(base_url="http://sup", transport=httpx.MockTransport(handler))
    files = await store.list_files(tenant_id=uuid4(), user_id=uuid4())

    assert files[0].mtime == datetime.fromtimestamp(1.5, tz=UTC)
