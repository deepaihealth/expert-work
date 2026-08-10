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
from uuid import uuid4

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
