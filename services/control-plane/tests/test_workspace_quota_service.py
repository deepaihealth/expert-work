"""``WorkspaceQuotaService`` —— 沙箱迁移波 3 PR-1 Task 4。

``tmp_path`` 当假 NAS 根;两个 store 用 in-memory 实现;``refresh_soon`` 的
防抖测试 monkeypatch ``time.monotonic``(``workspace_quota`` 模块级
``import time`` 与本文件是同一个模块对象,patch 全局生效)。
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

import control_plane.workspace_quota as workspace_quota_module
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


class _FakeDirEntry:
    """Minimal ``os.DirEntry`` double — just enough surface for ``_du``."""

    def __init__(self, path: str, *, is_dir: bool, size: int = 0) -> None:
        self.path = path
        self._is_dir = is_dir
        self._size = size

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        return self._is_dir

    def stat(self, *, follow_symlinks: bool = True) -> SimpleNamespace:
        return SimpleNamespace(st_size=self._size)


async def test_refresh_skips_vanished_subdir_but_keeps_scanning_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """评审 Important-1 —— 非 root 目录在遍历中途消失(NAS 并发写删的常态)
    必须 ``continue`` 跳过,不能像 root 消失那样直接终止整个遍历,否则栈上
    还没扫的兄弟目录会被静默丢掉,少算配额、削弱闸。

    用确定性的 ``os.scandir`` 替身而非真实文件系统竞态构造场景——真实竞态
    的处理顺序取决于底层文件系统未规定的目录项遍历顺序,赌不起。这里精确
    控制 root 先枚举到 "b" 再枚举到 "a":``_du`` 用栈(LIFO),后枚举的先
    弹出,所以 "a" 先被处理(随即"消失"),此时 "b" 仍留在栈上尚未扫描 ——
    正是暴露旧实现 bug(提前 return 丢失兄弟目录)的顺序。
    """
    workspace_root = str(tmp_path)
    tenant_id, user_id = uuid4(), uuid4()
    root = workspace_user_root(workspace_root, tenant_id, user_id)
    root_s = str(root)
    a_path = f"{root_s}/a"
    b_path = f"{root_s}/b"

    tree: dict[str, list[_FakeDirEntry] | None] = {
        root_s: [
            _FakeDirEntry(f"{root_s}/top.txt", is_dir=False, size=7),
            _FakeDirEntry(b_path, is_dir=True),
            _FakeDirEntry(a_path, is_dir=True),
        ],
        a_path: None,  # "a" has vanished by the time _du pops and scans it
        b_path: [_FakeDirEntry(f"{b_path}/bb.txt", is_dir=False, size=40)],
    }

    def fake_scandir(path: object) -> nullcontext[list[_FakeDirEntry]]:
        key = str(path)
        entries = tree.get(key)
        if entries is None:
            raise FileNotFoundError(key)
        return nullcontext(entries)

    monkeypatch.setattr(workspace_quota_module.os, "scandir", fake_scandir)

    user_workspaces = InMemoryUserWorkspaceStore()
    service = _make_service(workspace_root, user_workspaces=user_workspaces)
    await service.refresh(tenant_id=tenant_id, user_id=user_id)

    ws = await user_workspaces.get(tenant_id=tenant_id, user_id=user_id)
    assert ws is not None
    # top.txt(7) + b/bb.txt(40) = 47 — "a" vanished and was skipped, but "b"
    # (still on the stack when "a" errored) was still scanned. The buggy
    # implementation returns early with only 7 (top.txt), losing "b".
    assert ws.size_bytes == 47


async def test_refresh_root_missing_reports_zero(tmp_path: Path) -> None:
    """评审 Important-2 —— 根目录本身不存在(全新用户,还没写过任何东西)
    必须报 0,而不是抛异常或维持旧值。不需要 mock:压根不 mkdir 这个用户的
    根目录,真实 ``os.scandir`` 自然抛 ``FileNotFoundError``。"""
    workspace_root = str(tmp_path)
    tenant_id, user_id = uuid4(), uuid4()
    user_workspaces = InMemoryUserWorkspaceStore()
    service = _make_service(workspace_root, user_workspaces=user_workspaces)

    await service.refresh(tenant_id=tenant_id, user_id=user_id)

    ws = await user_workspaces.get(tenant_id=tenant_id, user_id=user_id)
    assert ws is not None
    assert ws.size_bytes == 0


async def test_refresh_skips_soft_deleted_workspace(tmp_path: Path) -> None:
    """评审 Important-3 —— capacity 与 lifecycle 分离:``refresh`` 命中软删
    行(``deleted_at is not None``)必须直接 return,不重算、不写
    ``update_size``,即使磁盘上确实有字节。"""
    workspace_root = str(tmp_path)
    tenant_id, user_id = uuid4(), uuid4()
    root = workspace_user_root(workspace_root, tenant_id, user_id)
    root.mkdir(parents=True)
    (root / "a.txt").write_bytes(b"x" * 500)

    user_workspaces = InMemoryUserWorkspaceStore()
    ws = await user_workspaces.resolve(tenant_id=tenant_id, user_id=user_id)
    await user_workspaces.soft_delete(workspace_id=ws.id, now=datetime.now(UTC))

    service = _make_service(workspace_root, user_workspaces=user_workspaces)
    await service.refresh(tenant_id=tenant_id, user_id=user_id)

    ws_after = await user_workspaces.get(tenant_id=tenant_id, user_id=user_id)
    assert ws_after is not None
    assert ws_after.size_bytes == 0  # untouched — the 500 bytes on disk never got read back


async def test_note_written_skips_soft_deleted_workspace(tmp_path: Path) -> None:
    """评审 Important-3 —— 同上,写路径 ``note_written`` 命中软删行同样跳过。"""
    workspace_root = str(tmp_path)
    tenant_id, user_id = uuid4(), uuid4()
    user_workspaces = InMemoryUserWorkspaceStore()
    ws = await user_workspaces.resolve(tenant_id=tenant_id, user_id=user_id)
    await user_workspaces.soft_delete(workspace_id=ws.id, now=datetime.now(UTC))

    service = _make_service(workspace_root, user_workspaces=user_workspaces)
    await service.note_written(tenant_id=tenant_id, user_id=user_id, delta_bytes=50)

    ws_after = await user_workspaces.get(tenant_id=tenant_id, user_id=user_id)
    assert ws_after is not None
    assert ws_after.size_bytes == 0


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
