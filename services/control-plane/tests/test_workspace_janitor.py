"""WorkspaceJanitorWorker —— tmp_path 假 NAS 树;in-memory 全套 store。

harness 造树布局(照 nas_workspace_store.workspace_user_root 口径):
    {root}/{tenant}/{user}/...      用户目录
    {root}/{tenant}/.deleted/{user} 软删标记(Task 5 用)
    {root}/_scratch/{sandbox_id}    临时沙箱目录
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from control_plane.workspace_janitor import (
    _JANITOR_LOCK_CLASSID,
    _SCRATCH_MAX_AGE_S,
    JanitorRunStats,
    WorkspaceJanitorWorker,
)
from control_plane.workspace_quota import WorkspaceQuotaService
from expert_work.persistence import InMemoryTenantQuotaStore
from expert_work.persistence.workspace.memory import InMemoryUserWorkspaceStore
from expert_work.runtime.storage import InMemoryObjectStore
from tests.fake_advisory_lock import FakeAdvisoryLockSessionFactory


def _build(
    tmp_path: Path,
) -> tuple[WorkspaceJanitorWorker, InMemoryUserWorkspaceStore, InMemoryObjectStore]:
    workspaces = InMemoryUserWorkspaceStore()
    quotas = InMemoryTenantQuotaStore()
    service = WorkspaceQuotaService(
        user_workspaces=workspaces, tenant_quotas=quotas, workspace_root=str(tmp_path)
    )
    store = InMemoryObjectStore()
    worker = WorkspaceJanitorWorker(
        user_workspaces=workspaces,
        quota_service=service,
        object_store=store,
        workspace_root=str(tmp_path),
    )
    return worker, workspaces, store


def _age(path: Path, *, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


@pytest.mark.asyncio
async def test_scratch_stale_removed_fresh_kept(tmp_path: Path) -> None:
    stale = tmp_path / "_scratch" / str(uuid4())
    fresh = tmp_path / "_scratch" / str(uuid4())
    (stale / "junk").mkdir(parents=True)
    fresh.mkdir(parents=True)
    _age(stale, seconds=_SCRATCH_MAX_AGE_S + 60)
    _age(fresh, seconds=_SCRATCH_MAX_AGE_S - 3600)

    worker, _, _ = _build(tmp_path)
    stats = await worker.run_once()
    assert stats.scratch_removed == 1
    assert not stale.exists()
    assert fresh.exists()


@pytest.mark.asyncio
async def test_scratch_missing_root_is_noop(tmp_path: Path) -> None:
    worker, _, _ = _build(tmp_path)
    stats = await worker.run_once()
    assert stats.scratch_removed == 0


@pytest.mark.asyncio
async def test_lock_loser_skips_cycle(tmp_path: Path) -> None:
    """``FakeAdvisoryLockSessionFactory`` (see ``tests/fake_advisory_lock.py``)
    takes no ``granted=`` kwarg — the brief's sketch assumed one, but the real
    fixture only models "whichever session executes the lock SELECT first
    holds it until rollback" (same shape ``test_tenant_resource_lock.py``
    uses). To force a loss deterministically we pre-acquire the exact
    ``(classid, key)`` pair the worker will race for, via a session pulled
    from the same shared factory, and never roll it back before
    ``run_once()`` runs."""
    (tmp_path / "_scratch" / str(uuid4())).mkdir(parents=True)
    workspaces = InMemoryUserWorkspaceStore()
    service = WorkspaceQuotaService(
        user_workspaces=workspaces,
        tenant_quotas=InMemoryTenantQuotaStore(),
        workspace_root=str(tmp_path),
    )
    factory = FakeAdvisoryLockSessionFactory()
    holder = factory()
    got = (
        await holder.execute(
            text("SELECT pg_try_advisory_xact_lock(:cid, hashtext(:k))"),
            {"cid": _JANITOR_LOCK_CLASSID, "k": "workspace_janitor"},
        )
    ).scalar_one()
    assert got, "test setup: holder must win the lock first"

    worker = WorkspaceJanitorWorker(
        user_workspaces=workspaces,
        quota_service=service,
        object_store=InMemoryObjectStore(),
        workspace_root=str(tmp_path),
        session_factory=factory,
    )
    stats = await worker.run_once()
    assert stats.skipped
    assert stats.scratch_removed == 0


@pytest.mark.asyncio
async def test_stop_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import control_plane.workspace_janitor as mod

    worker, _, _ = _build(tmp_path)

    async def _never_returns() -> JanitorRunStats:
        await asyncio.sleep(3600)
        raise AssertionError

    monkeypatch.setattr(worker, "run_once", _never_returns)
    monkeypatch.setattr(mod, "_STOP_TIMEOUT_S", 0.05, raising=False)
    worker.interval_s = 0.01
    worker.start()
    await asyncio.sleep(0.05)  # 让循环进入 run_once
    await asyncio.wait_for(worker.stop(), timeout=2)


@pytest.mark.asyncio
async def test_full_scan_discovers_from_filesystem_and_writes_sizes(tmp_path: Path) -> None:
    """行不存在也扫(FS 为发现源,refresh 建行);字节数 = du 真值。"""
    tenant, user_a, user_b = uuid4(), uuid4(), uuid4()
    (tmp_path / str(tenant) / str(user_a)).mkdir(parents=True)
    (tmp_path / str(tenant) / str(user_a) / "f1").write_bytes(b"x" * 100)
    (tmp_path / str(tenant) / str(user_b) / "sub").mkdir(parents=True)
    (tmp_path / str(tenant) / str(user_b) / "sub" / "f2").write_bytes(b"y" * 250)

    worker, workspaces, _ = _build(tmp_path)
    stats = await worker.run_once()
    assert stats.refreshed == 2
    row_a = await workspaces.get(tenant_id=tenant, user_id=user_a)
    row_b = await workspaces.get(tenant_id=tenant, user_id=user_b)
    assert row_a is not None and row_a.size_bytes == 100
    assert row_b is not None and row_b.size_bytes == 250


@pytest.mark.asyncio
async def test_full_scan_skips_junk_and_special_dirs(tmp_path: Path) -> None:
    """非 UUID 目录、.deleted、_scratch 都不进扫描;坏目录不炸整轮。"""
    tenant = uuid4()
    user = uuid4()
    (tmp_path / str(tenant) / str(user)).mkdir(parents=True)
    (tmp_path / str(tenant) / ".deleted").mkdir()
    (tmp_path / str(tenant) / "not-a-uuid").mkdir()
    (tmp_path / "_scratch" / str(uuid4())).mkdir(parents=True)
    (tmp_path / "lost+found").mkdir()

    worker, workspaces, _ = _build(tmp_path)
    stats = await worker.run_once()
    assert stats.refreshed == 1
    assert await workspaces.get(tenant_id=tenant, user_id=user) is not None


@pytest.mark.asyncio
async def test_full_scan_one_user_failure_does_not_stop_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, user_a, user_b = uuid4(), uuid4(), uuid4()
    (tmp_path / str(tenant) / str(user_a)).mkdir(parents=True)
    (tmp_path / str(tenant) / str(user_b)).mkdir(parents=True)

    worker, _workspaces, _ = _build(tmp_path)
    real_refresh = worker._quota_service.refresh
    calls: list[UUID] = []

    async def _flaky(*, tenant_id, user_id):  # 第一个用户炸,其余照常
        calls.append(user_id)
        if len(calls) == 1:
            raise RuntimeError("boom")
        await real_refresh(tenant_id=tenant_id, user_id=user_id)

    monkeypatch.setattr(worker._quota_service, "refresh", _flaky)
    stats = await worker.run_once()
    assert stats.refreshed == 1
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_full_scan_tenant_listing_failure_does_not_stop_other_tenants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一个租户目录扫描失败(NFS ESTALE/权限等 OSError)不该炸掉整轮——
    其余租户照常被扫,异常不逃逸出 ``run_once``。

    ``_list_uuid_dirs(root)`` 按 UUID 字符串排序遍历租户,``bad_tenant``
    故意钉成全零 UUID(排序必然排在随机 ``good_tenant`` 前面)——否则若
    ``good_tenant`` 恰好先被处理完,即使少了本次要测的 ``except OSError``
    兜底,``_run_cycle`` 那层粗粒度的按阶段 catch 也会让本断言巧合通过,
    测不出真正要防的回归(单个坏租户拖累排在它*之后*的所有租户)。
    """
    bad_tenant, good_tenant, good_user = UUID(int=0), uuid4(), uuid4()
    bad_tenant_dir = tmp_path / str(bad_tenant)
    bad_tenant_dir.mkdir(parents=True)
    (tmp_path / str(good_tenant) / str(good_user)).mkdir(parents=True)

    real_scandir = os.scandir

    def _flaky_scandir(path):
        if Path(path) == bad_tenant_dir:
            raise PermissionError("denied")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", _flaky_scandir)

    worker, workspaces, _ = _build(tmp_path)
    stats = await worker.run_once()
    assert stats.refreshed == 1
    assert await workspaces.get(tenant_id=good_tenant, user_id=good_user) is not None
