"""WorkspaceStore 两实现 —— 拆分自 SandboxRuntime(波 1 Task 4)。"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from orchestrator.tools.workspace_store import (
    RecordingWorkspaceStore,
    SupervisorWorkspaceStore,
    WorkspaceStore,
)


def test_supervisor_store_satisfies_protocol() -> None:
    store = SupervisorWorkspaceStore(base_url="http://sup")
    assert isinstance(store, WorkspaceStore)


def test_recording_store_satisfies_protocol() -> None:
    assert isinstance(RecordingWorkspaceStore(), WorkspaceStore)


@pytest.mark.asyncio
async def test_read_file_hits_the_same_http_path_as_before() -> None:
    seen: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, content=b"hello")

    tenant_id, user_id = uuid4(), uuid4()
    store = SupervisorWorkspaceStore(base_url="http://sup", transport=httpx.MockTransport(handler))
    data = await store.read_file(tenant_id=tenant_id, user_id=user_id, path="a.txt")

    assert data == b"hello"
    # 路径与拆分前逐字相同 —— 这是"零行为变化"的锚点
    assert str(tenant_id) in seen["url"]
    assert str(user_id) in seen["url"]
