"""WorkspaceJanitorWorker —— tmp_path 假 NAS 树;in-memory 全套 store。

harness 造树布局(照 nas_workspace_store.workspace_user_root 口径):
    {root}/{tenant}/{user}/...      用户目录
    {root}/{tenant}/.deleted/{user} 软删标记(Task 5 用)
    {root}/_scratch/{sandbox_id}    临时沙箱目录

B-61 T12 的回收面再往下一层(``_agent_dir`` 造):

    {root}/{tenant}/{user}/agents/{agent_key}/inputs/cache/{hash}{ext}  预拉缓存条目
    {root}/{tenant}/{user}/agents/{agent_key}/inputs/{run_id}/inputs.json  本轮注入变量
    {root}/{tenant}/{user}/agents/{agent_key}/uploads/{name}            用户上传(本批不收)
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tarfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from control_plane.advisory_locks import WORKSPACE_JANITOR_LOCK_CLASSID
from control_plane.workspace_janitor import (
    _POLICIES,
    _SCRATCH_MAX_AGE_S,
    _TARGETS,
    JanitorRunStats,
    WorkspaceJanitorWorker,
)
from control_plane.workspace_quota import WorkspaceQuotaService
from expert_work.persistence import InMemoryTenantQuotaStore
from expert_work.persistence.workspace.memory import InMemoryUserWorkspaceStore
from expert_work.runtime.storage import InMemoryObjectStore, ObjectStoreError
from orchestrator.tools.prefetch_script import CACHE_TTL_S
from tests.fake_advisory_lock import FakeAdvisoryLockSessionFactory


def _build(
    tmp_path: Path, *, archive_enabled: bool = True
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
        archive_enabled=archive_enabled,
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
            {"cid": WORKSPACE_JANITOR_LOCK_CLASSID, "k": "workspace_janitor"},
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
    from control_plane import workspace_janitor as mod

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


def _mark_deleted(root: Path, tenant: UUID, user: UUID) -> None:
    from orchestrator.tools.nas_workspace_store import workspace_deleted_marker

    marker = workspace_deleted_marker(str(root), tenant, user)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


def _tar_names(payload: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        return {m.name for m in tar.getmembers() if m.isfile()}


class _CountingObjectStore(InMemoryObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.stream_puts: list[str] = []
        self.puts: list[str] = []

    async def put_stream(
        self, key: str, chunks: AsyncIterator[bytes], *, content_type: str | None = None
    ) -> None:
        self.stream_puts.append(key)
        await super().put_stream(key, chunks, content_type=content_type)

    async def put(self, key: str, data: bytes, **kw: Any) -> None:
        self.puts.append(key)
        await super().put(key, data, **kw)


def _build_counting(
    tmp_path: Path,
) -> tuple[WorkspaceJanitorWorker, InMemoryUserWorkspaceStore, _CountingObjectStore]:
    workspaces = InMemoryUserWorkspaceStore()
    service = WorkspaceQuotaService(
        user_workspaces=workspaces,
        tenant_quotas=InMemoryTenantQuotaStore(),
        workspace_root=str(tmp_path),
    )
    store = _CountingObjectStore()
    worker = WorkspaceJanitorWorker(
        user_workspaces=workspaces,
        quota_service=service,
        object_store=store,
        workspace_root=str(tmp_path),
    )
    return worker, workspaces, store


@pytest.mark.asyncio
async def test_archive_happy_path(tmp_path: Path) -> None:
    """标记+目录+无行 → 建行软删、OSS 出现确定性 key 档案、目录删、标记留。"""
    tenant, user = uuid4(), uuid4()
    user_dir = tmp_path / str(tenant) / str(user)
    user_dir.mkdir(parents=True)
    (user_dir / "keep.txt").write_bytes(b"data")
    _mark_deleted(tmp_path, tenant, user)

    worker, workspaces, store = _build_counting(tmp_path)
    stats = await worker.run_once()

    assert stats.archived == 1
    row = await workspaces.get(tenant_id=tenant, user_id=user)
    assert row is not None and row.deleted_at is not None
    key = f"workspace-archives/{tenant}/{user}/{row.id}.tar.gz"
    assert row.archived_object_key == key
    names = _tar_names(await store.get(key))
    # 档案含用户文件(arcname="." 下成员名形如 ./keep.txt)
    assert any(n.endswith("keep.txt") for n in names)
    assert not user_dir.exists()
    from orchestrator.tools.nas_workspace_store import workspace_deleted_marker

    assert workspace_deleted_marker(str(tmp_path), tenant, user).exists()  # 墓碑留


@pytest.mark.asyncio
async def test_archive_rerun_is_zero_op(tmp_path: Path) -> None:
    """稳态墓碑:已 mark + 目录不在 → 第二轮零上传零标记。"""
    tenant, user = uuid4(), uuid4()
    (tmp_path / str(tenant) / str(user)).mkdir(parents=True)
    _mark_deleted(tmp_path, tenant, user)
    worker, _, store = _build_counting(tmp_path)
    await worker.run_once()
    uploads_before = len(store.stream_puts) + len(store.puts)

    stats = await worker.run_once()
    assert stats.archived == 0 and stats.reharvested == 0
    assert len(store.stream_puts) + len(store.puts) == uploads_before


@pytest.mark.asyncio
async def test_resurrected_dir_is_reharvested(tmp_path: Path) -> None:
    """硬要求①:已 mark 后目录复活 → 覆盖上传 + 再删目录,不重 mark。"""
    tenant, user = uuid4(), uuid4()
    user_dir = tmp_path / str(tenant) / str(user)
    user_dir.mkdir(parents=True)
    (user_dir / "original.txt").write_bytes(b"v1")
    _mark_deleted(tmp_path, tenant, user)
    worker, workspaces, store = _build_counting(tmp_path)
    await worker.run_once()
    row = await workspaces.get(tenant_id=tenant, user_id=user)
    assert row is not None and row.archived_object_key is not None

    # 复活:上传路径不查软删标记(W2 既有设计)——直接造目录
    user_dir.mkdir(parents=True)
    (user_dir / "stray.txt").write_bytes(b"post-purge")

    stats = await worker.run_once()
    assert stats.reharvested == 1 and stats.archived == 0
    assert not user_dir.exists()
    names = _tar_names(await store.get(row.archived_object_key))
    assert any(n.endswith("stray.txt") for n in names)  # 档案被复活内容覆盖(runbook 有言在先)
    row2 = await workspaces.get(tenant_id=tenant, user_id=user)
    assert row2 is not None and row2.archived_object_key == row.archived_object_key


@pytest.mark.asyncio
async def test_reentry_dir_gone_object_present_marks_only(tmp_path: Path) -> None:
    """删后崩重入:目录不在 + 对象在 + 行软删未 mark → 只 mark,零上传。"""
    tenant, user = uuid4(), uuid4()
    _mark_deleted(tmp_path, tenant, user)
    worker, workspaces, store = _build_counting(tmp_path)
    row = await workspaces.resolve(tenant_id=tenant, user_id=user)
    from datetime import UTC, datetime

    await workspaces.soft_delete(workspace_id=row.id, now=datetime.now(UTC))
    key = f"workspace-archives/{tenant}/{user}/{row.id}.tar.gz"
    await store.put(key, b"pre-existing")
    store.puts.clear()

    stats = await worker.run_once()
    assert stats.archived == 1
    assert store.stream_puts == [] and store.puts == []
    row2 = await workspaces.get(tenant_id=tenant, user_id=user)
    assert row2 is not None and row2.archived_object_key == key


@pytest.mark.asyncio
async def test_reentry_no_dir_no_object_uploads_empty_archive(tmp_path: Path) -> None:
    """生前无目录:统一产出空档案再 mark(恢复侧无需分叉)。"""
    tenant, user = uuid4(), uuid4()
    _mark_deleted(tmp_path, tenant, user)
    worker, workspaces, store = _build_counting(tmp_path)

    stats = await worker.run_once()
    assert stats.archived == 1
    row = await workspaces.get(tenant_id=tenant, user_id=user)
    assert row is not None and row.archived_object_key is not None
    assert _tar_names(await store.get(row.archived_object_key)) == set()


@pytest.mark.asyncio
async def test_archive_upload_failure_isolates_and_retries_next_round(tmp_path: Path) -> None:
    """A 用户上传炸 → B 照常归档;下轮 A 重试成功(幂等重入)。"""
    tenant, user_a, user_b = uuid4(), uuid4(), uuid4()
    for u in (user_a, user_b):
        d = tmp_path / str(tenant) / str(u)
        d.mkdir(parents=True)
        (d / "f").write_bytes(b"z")
        _mark_deleted(tmp_path, tenant, u)

    worker, workspaces, store = _build_counting(tmp_path)
    real_put_stream = store.put_stream
    fail_once: list[str] = []

    async def _flaky(
        key: str, chunks: AsyncIterator[bytes], *, content_type: str | None = None
    ) -> None:
        if str(user_a) in key and not fail_once:
            fail_once.append(key)
            raise ObjectStoreError("injected")
        await real_put_stream(key, chunks, content_type=content_type)

    store.put_stream = _flaky  # type: ignore[method-assign]
    stats = await worker.run_once()
    assert stats.archived == 1  # 只有 B
    row_a = await workspaces.get(tenant_id=tenant, user_id=user_a)
    assert row_a is not None and row_a.archived_object_key is None
    assert (tmp_path / str(tenant) / str(user_a)).exists()  # 先传后删:没传成就没删

    stats2 = await worker.run_once()
    assert stats2.archived == 1  # A 补上


@pytest.mark.asyncio
async def test_archive_marker_scan_failure_does_not_stop_other_tenants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一个租户的 ``.deleted`` 目录扫描失败(NFS ESTALE/权限等 OSError)不
    该炸掉整轮归档——其余租户照常被归档,异常不逃逸出 ``run_once``。

    照 ``test_full_scan_tenant_listing_failure_does_not_stop_other_tenants``
    的同款配方:``bad_tenant`` 钉成全零 UUID,排序必然排在随机
    ``good_tenant`` 前面(``_list_uuid_dirs`` 按 UUID 字符串排序遍历租
    户)——否则若 ``good_tenant`` 恰好先被处理完,即使少了本次要测的
    ``except OSError`` 兜底,``_run_cycle`` 那层粗粒度的按阶段 catch 也会
    让本断言巧合通过,测不出真正要防的回归(单个坏租户的 marker 扫描失
    败拖累排在它*之后*的所有租户)。
    """
    from orchestrator.tools.nas_workspace_store import DELETED_DIR

    bad_tenant, bad_user = UUID(int=0), uuid4()
    good_tenant, good_user = uuid4(), uuid4()
    for tenant, user in ((bad_tenant, bad_user), (good_tenant, good_user)):
        d = tmp_path / str(tenant) / str(user)
        d.mkdir(parents=True)
        (d / "f").write_bytes(b"z")
        _mark_deleted(tmp_path, tenant, user)

    bad_marker_dir = tmp_path / str(bad_tenant) / DELETED_DIR
    real_scandir = os.scandir

    def _flaky_scandir(path: Any) -> Any:
        # ``shutil.rmtree`` internally re-enters ``os.scandir`` with a raw
        # fd (the safe-fd variant) for the *good* tenant's directory
        # removal — only compare when ``path`` is actually path-like, else
        # the good tenant's own successful archive would trip a spurious
        # ``TypeError`` from ``Path(path)`` on an int fd.
        if isinstance(path, str | os.PathLike) and Path(path) == bad_marker_dir:
            raise PermissionError("denied")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", _flaky_scandir)

    worker, workspaces, _ = _build_counting(tmp_path)
    stats = await worker.run_once()
    assert stats.archived == 1
    row_good = await workspaces.get(tenant_id=good_tenant, user_id=good_user)
    assert row_good is not None and row_good.archived_object_key is not None


# --- B-61 T12:agents/<agent_key>/inputs/ 的 TTL 回收 -------------------------

#: 按 label 取 TTL —— 测试跟着策略表走,改表不用同步改这一堆魔数。
_TTL_S = {policy.label: policy.ttl_s for policy in _POLICIES}


def _agent_dir(tmp_path: Path, tenant: UUID, user: UUID, *, key: str = "demo-agent") -> Path:
    """造并返回 ``{root}/{tenant}/{user}/agents/{key}`` —— 沙箱里 ``/workspace``
    在 NAS 上对应的那一层(B-50 起每个 agent 一棵子树)。"""
    path = tmp_path / str(tenant) / str(user) / "agents" / key
    path.mkdir(parents=True)
    return path


@pytest.mark.asyncio
async def test_sweep_removes_expired_cache_files_and_run_dirs(tmp_path: Path) -> None:
    """超期的缓存条目按文件收、超期的 per-run 目录整棵收;新鲜的一律留着。"""
    tenant, user = uuid4(), uuid4()
    inputs = _agent_dir(tmp_path, tenant, user) / "inputs"
    cache = inputs / "cache"
    cache.mkdir(parents=True)
    stale_entry = cache / f"{'a' * 32}.png"
    fresh_entry = cache / f"{'b' * 32}.png"
    stale_entry.write_bytes(b"x" * 10)
    fresh_entry.write_bytes(b"y" * 10)
    _age(stale_entry, seconds=_TTL_S["inputs_cache"] + 60)
    _age(fresh_entry, seconds=_TTL_S["inputs_cache"] - 3600)

    stale_run, fresh_run = inputs / str(uuid4()), inputs / str(uuid4())
    for run_dir in (stale_run, fresh_run):
        run_dir.mkdir()
        (run_dir / "inputs.json").write_text("{}")
    # 先写内容再改目录 mtime:往目录里写文件会把父目录的 mtime 顶成「现在」。
    _age(stale_run, seconds=_TTL_S["inputs_run_dir"] + 60)
    _age(fresh_run, seconds=_TTL_S["inputs_run_dir"] - 3600)

    worker, _, _ = _build(tmp_path)
    stats = await worker.run_once()
    assert (stats.reclaim_files_removed, stats.reclaim_dirs_removed) == (1, 1)
    assert not stale_entry.exists() and fresh_entry.exists()
    assert not stale_run.exists() and fresh_run.exists()


@pytest.mark.asyncio
async def test_sweep_never_touches_a_recently_written_run_dir(tmp_path: Path) -> None:
    """活跃 run 的目录 mtime 是新的,必须留下 —— 删错它就打断了正在跑的那一轮。

    目录里的 ``inputs.json`` 故意造得比 TTL 还老:判据必须是**目录自己**的
    mtime(T11 每拉完一个 URL 就 ``os.replace`` 改写一次 inputs.json,目录项因此
    被顶新),拿内容里最老的文件当判据会把活跃 run 的目录收掉。
    这条是「删错会打断执行」的反向实证,不是锦上添花。
    """
    tenant, user = uuid4(), uuid4()
    inputs = _agent_dir(tmp_path, tenant, user) / "inputs"
    live = inputs / str(uuid4())
    live.mkdir(parents=True)
    doc = live / "inputs.json"
    doc.write_text("{}")
    _age(doc, seconds=_TTL_S["inputs_run_dir"] * 4)
    _age(live, seconds=60)

    worker, _, _ = _build(tmp_path)
    stats = await worker.run_once()
    assert stats.reclaim_dirs_removed == 0
    assert doc.exists()


@pytest.mark.asyncio
async def test_sweep_ignores_dirs_that_are_not_uuid_shaped(tmp_path: Path) -> None:
    """只认 ``inputs/<uuid>/`` 与 ``inputs/cache/``,其它一律不碰 —— 包括空着
    的 ``cache/`` 自己(它是目录但名字不是 UUID,不该被当成过期的 run 目录)。

    ``hexish`` 是 32 位无横杠 hex:``UUID(name)`` **接受**它,而生产者只会写
    ``str(run_id)``。判据松一位,别人建的目录就被整棵 rmtree 掉。
    """
    tenant, user = uuid4(), uuid4()
    inputs = _agent_dir(tmp_path, tenant, user) / "inputs"
    stranger = inputs / "notes"
    stranger.mkdir(parents=True)
    kept = stranger / "keep.md"
    kept.write_text("x")
    hexish = inputs / ("a" * 32)
    hexish.mkdir()
    loose = inputs / "README.md"
    loose.write_text("x")
    cache = inputs / "cache"
    cache.mkdir()
    for path in (kept, stranger, hexish, loose, cache, inputs):
        _age(path, seconds=_TTL_S["inputs_run_dir"] * 4)

    worker, _, _ = _build(tmp_path)
    stats = await worker.run_once()
    assert (stats.reclaim_files_removed, stats.reclaim_dirs_removed) == (0, 0)
    assert stranger.is_dir() and kept.exists() and loose.exists() and cache.is_dir()
    assert hexish.is_dir()


@pytest.mark.asyncio
async def test_reclaim_lands_in_the_same_cycle_size_accounting(tmp_path: Path) -> None:
    """回收发生在 ``_sweep_sizes`` 之前:同一轮 ``run_once`` 之后记下的体积必须
    已经是回收后的。这条是 phase 顺序的实证 —— 把新 phase 挪到 ``_sweep_sizes``
    之后它就会红(那一轮记的是回收前的体积,要等下一轮才追平)。"""
    tenant, user = uuid4(), uuid4()
    agent = _agent_dir(tmp_path, tenant, user)
    (agent / "out.pdf").write_bytes(b"k" * 100)
    stale_run = agent / "inputs" / str(uuid4())
    stale_run.mkdir(parents=True)
    (stale_run / "inputs.json").write_bytes(b"x" * 5000)
    _age(stale_run, seconds=_TTL_S["inputs_run_dir"] + 60)

    worker, workspaces, _ = _build(tmp_path)
    stats = await worker.run_once()
    assert stats.reclaim_dirs_removed == 1
    row = await workspaces.get(tenant_id=tenant, user_id=user)
    assert row is not None and row.size_bytes == 100  # 那 5000 字节同一轮里就没了


@pytest.mark.asyncio
async def test_disabled_policies_delete_nothing(tmp_path: Path) -> None:
    """uploads / 产物的条目本批是关着的:造出超期的 uploads 文件,扫完必须还在。

    这条挡的是「以后有人顺手把 enabled 改成 True 就上线了」—— 那两条删的是用户
    数据,需要产品定 N、需要发布前告知、还要有对外删除端点当自救出口(B-63)。
    """
    tenant, user = uuid4(), uuid4()
    upload = _agent_dir(tmp_path, tenant, user) / "uploads" / "合同.docx"
    upload.parent.mkdir(parents=True)
    upload.write_bytes(b"x" * 10)
    _age(upload, seconds=_TTL_S["uploads"] + 86400)

    worker, _, _ = _build(tmp_path)
    stats = await worker.run_once()
    assert (stats.reclaim_files_removed, stats.reclaim_dirs_removed) == (0, 0)
    assert upload.exists()


@pytest.mark.asyncio
async def test_a_leftover_tmp_file_is_reclaimed(tmp_path: Path) -> None:
    """``inputs/`` 这一层的 ``inputs.json.tmp`` 残留按同一条 TTL 收掉。

    **这个名字今天打不到东西**,测试造的是一个不会自然出现的形状:预拉的
    ``_rewrite`` 写的是 ``inputs/<run_id>/inputs.json.tmp``(在 run 目录里,随整棵目录被
    收),不是这一层 —— brief 当初给的理由是错的,实现注释(``_INPUTS_TMP_NAME``)已经
    如实纠正,这条 docstring 跟上,免得读代码的人先信了测试。
    留着的是**机制**:``file_names`` 非 ``None`` = 「只收名单里的」,正是它让
    ``inputs/README.md`` 这类别人的文件活下来(见上一条测试)。
    """
    tenant, user = uuid4(), uuid4()
    inputs = _agent_dir(tmp_path, tenant, user) / "inputs"
    inputs.mkdir(parents=True)
    leftover = inputs / "inputs.json.tmp"
    leftover.write_bytes(b"{}")
    _age(leftover, seconds=_TTL_S["inputs_run_dir"] + 60)

    worker, _, _ = _build(tmp_path)
    stats = await worker.run_once()
    assert stats.reclaim_files_removed == 1
    assert not leftover.exists()


@pytest.mark.asyncio
async def test_inputs_scan_failure_does_not_stop_other_users(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一个 agent 子树打不开(权限 / NFS ESTALE)不该带走整个 phase。

    注入点是 ``os.open``(不是 ``os.scandir``):回收链路整条走 fd —— 先
    ``_open_dir`` 拿目录 fd、再 ``os.scandir(fd)``,``scandir`` 拿到的是 int,按路径
    打桩根本打不中(改成 fd 版之后,旧写法会静默变成一条空测试)。按**唯一的 agent
    key** 拦,不会误伤别处的 ``os.open``。

    隔离要求是**逐 agent**,所以坏 agent 必须有个**同一用户下的兄弟 agent**:少了它,
    异常被用户那一层的兜底接住也看不出区别(实测:第一版没有兄弟,去掉 per-agent 的
    catch 一条测试都不红)。``agents/`` 下按名字排序遍历,``bad-agent`` 必在
    ``sibling-agent`` 之前。坏租户同理钉 ``UUID(int=0)``(``_list_uuid_dirs`` 按 UUID
    字符串排序),保证它排在随机的好租户之前 —— 否则好租户恰好先被处理完的话,
    ``_run_cycle`` 那层按阶段的粗粒度 catch 也会让断言巧合通过。
    """
    bad_tenant, good_tenant, bad_user = UUID(int=0), uuid4(), uuid4()
    bad_run = _agent_dir(tmp_path, bad_tenant, bad_user, key="bad-agent") / "inputs" / str(uuid4())
    bad_run.mkdir(parents=True)
    sibling_run = (
        tmp_path / str(bad_tenant) / str(bad_user) / "agents" / "sibling-agent" / "inputs"
    ) / str(uuid4())
    sibling_run.mkdir(parents=True)
    good_run = (
        _agent_dir(tmp_path, good_tenant, uuid4(), key="good-agent") / "inputs" / str(uuid4())
    )
    good_run.mkdir(parents=True)
    for path in (bad_run, sibling_run, good_run):
        _age(path, seconds=_TTL_S["inputs_run_dir"] + 60)

    real_open = os.open

    def _flaky_open(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if path == "bad-agent":
            raise PermissionError("denied")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", _flaky_open)

    worker, _, _ = _build(tmp_path)
    stats = await worker.run_once()
    assert stats.reclaim_dirs_removed == 2  # 同用户的兄弟 agent + 另一个租户的用户
    assert not sibling_run.exists() and not good_run.exists()
    assert bad_run.exists()  # 打不开的那棵原样留着,不是「删不掉就当没有」


@pytest.mark.asyncio
async def test_sweep_does_not_follow_a_symlinked_scan_base(tmp_path: Path) -> None:
    """``inputs`` / ``inputs/cache`` 被换成软链时,janitor 不许跟过去删。

    不是理论风险:B-60 之后每次 exec 把 ``agents/<key>/`` bind 成 ``/workspace``,
    沙箱里由模型驱动的代码可以 ``rm -rf inputs && ln -s <任意目标> inputs``;而这个
    phase 以控制面身份跑、对整棵 NAS 有写权限。跟过去一次就是删别人的目录(评审
    PoC:``inputs`` 指向租户目录时,另一个用户的整棵工作区被 rmtree)。

    两条策略各覆盖一个落点:``inputs_run_dir`` 的落点是 ``inputs/``(软链换掉它),
    ``inputs_cache`` 的落点是 ``inputs/cache/``(第二级也要挡,而且它
    ``file_names=None`` —— 跟过去就是「这一层文件全收」)。
    """
    tenant = uuid4()
    victim_dir = tmp_path / "victim" / str(uuid4())  # UUID 形状 + 超期 = 跟过去必被收
    victim_dir.mkdir(parents=True)
    victim_file = tmp_path / "victim-cache" / "old.png"
    victim_file.parent.mkdir(parents=True)
    victim_file.write_bytes(b"x" * 10)
    for path in (victim_dir, victim_file):
        _age(path, seconds=_TTL_S["inputs_run_dir"] * 4)

    swapped = _agent_dir(tmp_path, tenant, uuid4()) / "inputs"
    swapped.symlink_to(victim_dir.parent)

    cache_swapped = _agent_dir(tmp_path, tenant, uuid4()) / "inputs"
    cache_swapped.mkdir()
    (cache_swapped / "cache").symlink_to(victim_file.parent)

    innocent_run = _agent_dir(tmp_path, tenant, uuid4()) / "inputs" / str(uuid4())
    innocent_run.mkdir(parents=True)
    _age(innocent_run, seconds=_TTL_S["inputs_run_dir"] + 60)

    worker, _, _ = _build(tmp_path)
    stats = await worker.run_once()
    assert victim_dir.is_dir() and victim_file.exists()  # 一个都没少
    assert swapped.is_symlink() and (cache_swapped / "cache").is_symlink()  # 软链自己也不删
    # phase 没被这两条带走:同一轮里正常用户的过期目录照收(且只收了它)
    assert (stats.reclaim_files_removed, stats.reclaim_dirs_removed) == (0, 1)
    assert not innocent_run.exists()


@pytest.mark.asyncio
async def test_sweep_does_not_traverse_a_symlinked_intermediate_segment(tmp_path: Path) -> None:
    """两级落点(``inputs/cache``)的**中间那一段**也必须自己挡住软链。

    ``O_NOFOLLOW`` 只管**最后**一段。把逐段下降折成一次
    ``openat(agent_fd, "inputs/cache", O_NOFOLLOW)``,``inputs`` 就成了中间段、照样被
    跟随 —— 于是「``inputs`` 是软链,而它指向的目录里恰好有个**真的** ``cache/``」时,
    别人的过期缓存条目就被删了。这里造的正是那个形状 —— 受害目录**故意放在 janitor
    正常遍历够不到的地方**(``tmp_path`` 下,不是 ``<tenant>/<user>/`` 那两层 UUID):
    否则它会被自己那条合法路径正常收掉,断言就分不清「没被跟过去」与「被自己收了」。
    第一版就踩了这个,测试当场红。

    上一条软链测试打不到这里:它的软链指向的目录里**没有 ``cache/`` 这一层**,
    ``inputs/cache`` 直接 ENOENT,折不折成一次看不出区别。
    """
    tenant = uuid4()
    victim_inputs = tmp_path / "victim" / "inputs"  # 形状像 agent 子树,但不在遍历面上
    victim_cache = victim_inputs / "cache"
    victim_cache.mkdir(parents=True)
    victim_entry = victim_cache / f"{'c' * 32}.png"
    victim_entry.write_bytes(b"x" * 10)
    _age(victim_entry, seconds=_TTL_S["inputs_cache"] + 60)

    evil = _agent_dir(tmp_path, tenant, uuid4(), key="evil-agent") / "inputs"
    evil.symlink_to(victim_inputs)  # inputs -> <别人的 inputs>(里面有真的 cache/)

    sibling_cache = _agent_dir(tmp_path, tenant, uuid4(), key="sibling") / "inputs" / "cache"
    sibling_cache.mkdir(parents=True)
    sibling_entry = sibling_cache / f"{'d' * 32}.png"
    sibling_entry.write_bytes(b"y" * 10)
    _age(sibling_entry, seconds=_TTL_S["inputs_cache"] + 60)

    worker, _, _ = _build(tmp_path)
    stats = await worker.run_once()
    assert victim_entry.exists()  # 中间段是软链 → 一步都不许进去
    assert evil.is_symlink()  # 软链自己也不删
    assert not sibling_entry.exists()  # 同一轮里正常 agent 照收 —— phase 没被带走
    assert (stats.reclaim_files_removed, stats.reclaim_dirs_removed) == (1, 0)


def test_every_policy_has_an_explicit_target_entry() -> None:
    """策略表与落点表必须逐条对上 —— 「暂时没有落点」要显式写 ``None``。

    漏配不会报错,只会让那条策略拨开 ``enabled`` 之后一声不吭地什么都不收
    (实现里另有一条导入期的硬检查,这条测试是它的可读版本)。
    """
    assert {policy.label for policy in _POLICIES} == set(_TARGETS)
    assert _TARGETS["artifacts"] is None  # 产物还没有落点,B-63 定义完再补


@pytest.mark.asyncio
async def test_reclaim_logs_counts_but_never_names(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """不可逆的删除要留得下痕迹:逐 ``(tenant, user, agent, policy)`` 记计数,外加每轮
    一条汇总 —— 事后要答得出「谁的哪个 agent、按哪条策略、丢了几个」。

    但**只记标识与计数**:条目名是租户内容(上传件的文件名、产物名可能是客户的人名),
    一个字都不许进日志。空轮不产生明细行,只有汇总。
    """
    tenant, user = uuid4(), uuid4()
    cache = _agent_dir(tmp_path, tenant, user, key="demo-agent") / "inputs" / "cache"
    cache.mkdir(parents=True)
    named = cache / "张三的体检报告.pdf"
    named.write_bytes(b"x")
    _age(named, seconds=_TTL_S["inputs_cache"] + 60)

    worker, _, _ = _build(tmp_path)
    with caplog.at_level(logging.INFO, logger="control_plane.workspace_janitor"):
        await worker.run_once()

    messages = [record.getMessage() for record in caplog.records]
    detail = [m for m in messages if m.startswith("workspace_janitor.reclaimed ")]
    summary = [m for m in messages if m.startswith("workspace_janitor.reclaim_summary ")]
    assert len(detail) == 1 and len(summary) == 1
    assert f"tenant={tenant}" in detail[0] and f"user={user}" in detail[0]
    assert "agent=demo-agent" in detail[0] and "policy=inputs_cache" in detail[0]
    assert "files=1 dirs=0" in detail[0]
    assert "files=1 dirs=0" in summary[0] and "inputs_cache:1/0" in summary[0]
    assert not any("张三" in m for m in messages)  # 租户内容一个字都不进日志


@pytest.mark.asyncio
async def test_archiving_is_skipped_on_a_non_durable_object_store_but_reclaim_still_runs(
    tmp_path: Path,
) -> None:
    """``archive_enabled=False`` 只关归档那一个 phase,回收照跑。

    「对象存储必须是持久后端」是**归档自己的**前提(归档会 rm -rf 用户目录,内存
    object store 重启即丢 = 数据删了却没有档案)。回收一个字节都不碰对象存储,跟着
    一起关掉的后果是:配了 NAS + 配额、对象存储走内存后端的部署完全没有垃圾回收,
    一路涨到配额闸把沙箱类工具全挡掉 —— 正是 T11/T12 存在的理由。
    """
    tenant, user = uuid4(), uuid4()
    doomed = tmp_path / str(tenant) / str(user)
    doomed.mkdir(parents=True)
    (doomed / "keep.txt").write_bytes(b"data")
    _mark_deleted(tmp_path, tenant, user)

    stale_run = _agent_dir(tmp_path, uuid4(), uuid4()) / "inputs" / str(uuid4())
    stale_run.mkdir(parents=True)
    _age(stale_run, seconds=_TTL_S["inputs_run_dir"] + 60)

    worker, workspaces, _ = _build(tmp_path, archive_enabled=False)
    stats = await worker.run_once()

    assert stats.archived == 0 and stats.reharvested == 0
    assert doomed.exists() and (doomed / "keep.txt").exists()  # 没档案就别删数据
    row = await workspaces.get(tenant_id=tenant, user_id=user)
    assert row is None or row.archived_object_key is None
    assert stats.reclaim_dirs_removed == 1 and not stale_run.exists()  # 回收照跑


def test_cache_ttl_outlives_the_run_dir_that_names_it() -> None:
    """不变式:``inputs_cache.ttl > inputs_run_dir.ttl + CACHE_TTL_S``。

    命中**不 touch**(T11 裁定 A),所以缓存条目的 mtime 最多比「最近一次被引用」早
    ``CACHE_TTL_S``(24h),而 run 目录的 mtime 就是那次引用的时刻。两条 TTL 相等时,
    缓存条目会比引用它的 ``inputs.json`` **先死最多 24 小时** —— 那段时间里
    ``local_path`` 非空却指向一个已被删掉的文件,而工具描述对模型的承诺是「非空 = 平台
    已经下好了,直接用,不用再联网」。

    把不等式本身钉在这里,而不是钉两个具体天数:谁把缓存 TTL 往 run 目录那边调,
    这条就红。
    """
    ttl = {policy.label: policy.ttl_s for policy in _POLICIES}
    assert ttl["inputs_cache"] > ttl["inputs_run_dir"] + CACHE_TTL_S
