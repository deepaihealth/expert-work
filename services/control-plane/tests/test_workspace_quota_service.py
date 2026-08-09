"""``WorkspaceQuotaService`` —— 沙箱迁移波 3 PR-1 Task 4。

``tmp_path`` 当假 NAS 根;两个 store 用 in-memory 实现;``refresh_soon`` 的
防抖测试 monkeypatch ``time.monotonic``(``workspace_quota`` 模块级
``import time`` 与本文件是同一个模块对象,patch 全局生效)。
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from control_plane.workspace_quota import WorkspaceQuotaService
from expert_work.persistence.quota import InMemoryTenantQuotaStore
from expert_work.persistence.quota.base import TenantQuotaStore
from expert_work.persistence.workspace import InMemoryUserWorkspaceStore
from expert_work.persistence.workspace.base import UserWorkspaceStore
from expert_work.protocol.quota import (
    DEFAULT_WORKSPACE_BYTES_PER_USER,
    QuotaDimension,
    TenantQuotaPatch,
    TenantQuotaRecord,
)
from orchestrator.tools.nas_workspace_store import workspace_user_root
from orchestrator.tools.sandbox import WorkspaceQuotaExceededError


def _make_service(
    tmp_path_str: str,
    *,
    user_workspaces: UserWorkspaceStore | None = None,
    tenant_quotas: TenantQuotaStore | None = None,
    debounce_s: float = 60.0,
) -> WorkspaceQuotaService:
    return WorkspaceQuotaService(
        user_workspaces=user_workspaces or InMemoryUserWorkspaceStore(),
        tenant_quotas=tenant_quotas or InMemoryTenantQuotaStore(),
        workspace_root=tmp_path_str,
        debounce_s=debounce_s,
    )


async def test_effective_limit_default_when_unconfigured(tmp_path: Path) -> None:
    service = _make_service(str(tmp_path))
    assert await service.effective_limit(tenant_id=uuid4()) == DEFAULT_WORKSPACE_BYTES_PER_USER


async def test_effective_limit_reads_tenant_row(tmp_path: Path) -> None:
    tenant_quotas = InMemoryTenantQuotaStore()
    tenant_id = uuid4()
    await tenant_quotas.upsert(
        tenant_id=tenant_id,
        patch=TenantQuotaPatch(
            dimension=QuotaDimension.WORKSPACE_BYTES_PER_USER,
            limit_value=123,
        ),
        updated_by="test",
    )
    service = _make_service(str(tmp_path), tenant_quotas=tenant_quotas)
    assert await service.effective_limit(tenant_id=tenant_id) == 123


async def test_effective_limit_ignores_expired_row(tmp_path: Path) -> None:
    tenant_quotas = InMemoryTenantQuotaStore()
    tenant_id = uuid4()
    now = datetime.now(UTC)
    expired = TenantQuotaRecord(
        id=uuid4(),
        tenant_id=tenant_id,
        dimension=QuotaDimension.WORKSPACE_BYTES_PER_USER,
        scope={},
        limit_value=999,
        burst=None,
        effective_from=now - timedelta(days=2),
        effective_until=now - timedelta(days=1),
        updated_by="test",
        updated_at=now,
    )
    await tenant_quotas.insert_for_test(expired)
    service = _make_service(str(tmp_path), tenant_quotas=tenant_quotas)
    assert await service.effective_limit(tenant_id=tenant_id) == DEFAULT_WORKSPACE_BYTES_PER_USER


async def test_check_blocks_at_limit_not_below(tmp_path: Path) -> None:
    tenant_quotas = InMemoryTenantQuotaStore()
    user_workspaces = InMemoryUserWorkspaceStore()
    tenant_id, user_id = uuid4(), uuid4()
    limit = 100
    await tenant_quotas.upsert(
        tenant_id=tenant_id,
        patch=TenantQuotaPatch(
            dimension=QuotaDimension.WORKSPACE_BYTES_PER_USER, limit_value=limit
        ),
        updated_by="test",
    )
    service = _make_service(
        str(tmp_path), user_workspaces=user_workspaces, tenant_quotas=tenant_quotas
    )
    ws = await user_workspaces.resolve(tenant_id=tenant_id, user_id=user_id)

    await user_workspaces.update_size(workspace_id=ws.id, size_bytes=limit)
    with pytest.raises(WorkspaceQuotaExceededError):
        await service.check(tenant_id=tenant_id, user_id=user_id)

    await user_workspaces.update_size(workspace_id=ws.id, size_bytes=limit - 1)
    await service.check(tenant_id=tenant_id, user_id=user_id)  # must not raise


async def test_check_upload_blocks_when_sum_exceeds(tmp_path: Path) -> None:
    tenant_quotas = InMemoryTenantQuotaStore()
    user_workspaces = InMemoryUserWorkspaceStore()
    tenant_id, user_id = uuid4(), uuid4()
    limit = 100
    await tenant_quotas.upsert(
        tenant_id=tenant_id,
        patch=TenantQuotaPatch(
            dimension=QuotaDimension.WORKSPACE_BYTES_PER_USER, limit_value=limit
        ),
        updated_by="test",
    )
    service = _make_service(
        str(tmp_path), user_workspaces=user_workspaces, tenant_quotas=tenant_quotas
    )
    ws = await user_workspaces.resolve(tenant_id=tenant_id, user_id=user_id)
    await user_workspaces.update_size(workspace_id=ws.id, size_bytes=60)

    with pytest.raises(WorkspaceQuotaExceededError):
        await service.check_upload(tenant_id=tenant_id, user_id=user_id, incoming_bytes=41)

    # size(60) + incoming(40) == limit(100) — > 谓词, 不拦.
    await service.check_upload(tenant_id=tenant_id, user_id=user_id, incoming_bytes=40)


async def test_note_written_creates_row_and_accumulates(tmp_path: Path) -> None:
    user_workspaces = InMemoryUserWorkspaceStore()
    tenant_id, user_id = uuid4(), uuid4()
    service = _make_service(str(tmp_path), user_workspaces=user_workspaces)

    assert await user_workspaces.get(tenant_id=tenant_id, user_id=user_id) is None

    await service.note_written(tenant_id=tenant_id, user_id=user_id, delta_bytes=50)
    ws = await user_workspaces.get(tenant_id=tenant_id, user_id=user_id)
    assert ws is not None
    assert ws.size_bytes == 50

    await service.note_written(tenant_id=tenant_id, user_id=user_id, delta_bytes=30)
    ws2 = await user_workspaces.get(tenant_id=tenant_id, user_id=user_id)
    assert ws2 is not None
    assert ws2.size_bytes == 80


async def test_refresh_walks_dir_lstat_no_symlink_follow(tmp_path: Path) -> None:
    workspace_root = str(tmp_path)
    tenant_id, user_id = uuid4(), uuid4()
    root = workspace_user_root(workspace_root, tenant_id, user_id)
    root.mkdir(parents=True)
    (root / "normal.txt").write_bytes(b"x" * 100)

    big_target = tmp_path / "big_target.bin"
    big_target.write_bytes(b"y" * 5000)
    link_path = root / "link_to_big"
    link_path.symlink_to(big_target)
    link_own_size = os.lstat(link_path).st_size

    user_workspaces = InMemoryUserWorkspaceStore()
    service = _make_service(workspace_root, user_workspaces=user_workspaces)

    await service.refresh(tenant_id=tenant_id, user_id=user_id)

    ws = await user_workspaces.get(tenant_id=tenant_id, user_id=user_id)
    assert ws is not None
    assert ws.size_bytes == 100 + link_own_size
    assert ws.size_bytes < 5000  # never followed the symlink into the 5000-byte target


async def test_refresh_soon_debounces_60s(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service(str(tmp_path))
    calls: list[tuple[UUID, UUID]] = []

    async def fake_refresh(*, tenant_id: UUID, user_id: UUID) -> None:
        calls.append((tenant_id, user_id))

    monkeypatch.setattr(service, "refresh", fake_refresh)

    fake_now = [1_000.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_now[0])

    tenant_id = uuid4()
    user_a, user_b = uuid4(), uuid4()

    service.refresh_soon(tenant_id=tenant_id, user_id=user_a)
    await asyncio.sleep(0)
    assert calls == [(tenant_id, user_a)]

    # Same user, 30s later — still inside the 60s debounce window: no-op.
    fake_now[0] += 30
    service.refresh_soon(tenant_id=tenant_id, user_id=user_a)
    await asyncio.sleep(0)
    assert calls == [(tenant_id, user_a)]

    # A different user isn't gated by user_a's debounce window.
    service.refresh_soon(tenant_id=tenant_id, user_id=user_b)
    await asyncio.sleep(0)
    assert calls == [(tenant_id, user_a), (tenant_id, user_b)]

    # user_a, 61s after its first call — window elapsed, runs again.
    fake_now[0] += 61
    service.refresh_soon(tenant_id=tenant_id, user_id=user_a)
    await asyncio.sleep(0)
    assert calls == [(tenant_id, user_a), (tenant_id, user_b), (tenant_id, user_a)]
