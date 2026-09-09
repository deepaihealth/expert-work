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


@pytest.mark.asyncio
async def test_recording_store_records_delete_tree_and_can_fail() -> None:
    from uuid import uuid4

    from orchestrator.tools.workspace_store import RecordingWorkspaceStore

    store = RecordingWorkspaceStore()
    tenant, user = uuid4(), uuid4()
    await store.delete_tree(tenant_id=tenant, user_id=user, path="threads/x")
    assert store.workspace_tree_deletes == [(tenant, user, "threads/x")]

    store.workspace_tree_delete_error = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await store.delete_tree(tenant_id=tenant, user_id=user, path="threads/y")


@pytest.mark.asyncio
async def test_supervisor_store_has_no_recursive_delete() -> None:
    """The supervisor HTTP backend (local dev / CI only) raises rather than
    pretending — the purge hook records the failure, the retention job's
    orphan scan is the NAS-side backstop."""
    from uuid import uuid4

    from orchestrator.tools import SandboxSupervisorError
    from orchestrator.tools.workspace_store import SupervisorWorkspaceStore

    store = SupervisorWorkspaceStore(base_url="http://supervisor.invalid")
    with pytest.raises(SandboxSupervisorError):
        await store.delete_tree(tenant_id=uuid4(), user_id=uuid4(), path="threads/x")
