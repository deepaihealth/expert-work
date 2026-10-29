"""B-235 —— 产物版本副本:NAS 实现(真目录)+ 内存实现。

每次 ``save_artifact`` 把那一刻的字节复制一份,以版本行 id 命名,放在
``{root}/{tenant}/.artifact-versions/{user}/`` —— 用户根旁边,沙箱挂不到。这里钉:
副本在哪、之后源文件怎么变都不影响它、读的过程中文件变了不收半截、写到一半失败不
留下以最终名字命名的残件、删一个用户的副本不碰别人也不碰工作区。
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import artifact_snapshot_dir
from orchestrator.tools import artifact_snapshots as snapshots_mod
from orchestrator.tools import nas_workspace_store as nas_mod
from orchestrator.tools.artifact_snapshots import (
    MAX_SNAPSHOT_BYTES,
    InMemoryArtifactSnapshotStore,
    NasArtifactSnapshotStore,
    SnapshotDigest,
)
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore, workspace_user_root
from orchestrator.tools.sandbox import (
    WorkspaceFileChangedError,
    WorkspaceFileNotFoundError,
    WorkspaceFileTooLargeError,
    WorkspacePathEscapeError,
)
from orchestrator.tools.workspace_store import RecordingWorkspaceStore

_REL = "agents/writer-1a2b3c4d/report.txt"


def _write(root: Path, tenant: UUID, user: UUID, rel: str, data: bytes) -> Path:
    target = root / str(tenant) / str(user) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


@pytest.fixture
def ids() -> tuple[UUID, UUID]:
    return uuid4(), uuid4()


@pytest.fixture
def nas(tmp_path: Path) -> NasArtifactSnapshotStore:
    return NasArtifactSnapshotStore(workspace=NasWorkspaceStore(root=str(tmp_path)))


async def test_capture_keeps_the_bytes_of_that_moment_outside_the_user_root(
    tmp_path: Path, nas: NasArtifactSnapshotStore, ids: tuple[UUID, UUID]
) -> None:
    tenant, user = ids
    source = _write(tmp_path, tenant, user, _REL, b"AAA")
    key = str(uuid4())

    digest = await nas.capture(tenant_id=tenant, user_id=user, path=_REL, snapshot_key=key)

    assert digest == SnapshotDigest(size_bytes=3, sha256=hashlib.sha256(b"AAA").hexdigest())
    copy = artifact_snapshot_dir(str(tmp_path), tenant, user) / key
    assert copy.read_bytes() == b"AAA"
    user_root = workspace_user_root(str(tmp_path), tenant, user)
    assert not copy.resolve().is_relative_to(user_root), "副本落在了沙箱挂得到的用户根里"

    source.write_bytes(b"BBB-overwritten")
    assert await nas.read(tenant_id=tenant, user_id=user, snapshot_key=key) == b"AAA"


async def test_copy_and_its_directories_are_owner_only(
    tmp_path: Path, nas: NasArtifactSnapshotStore, ids: tuple[UUID, UUID]
) -> None:
    tenant, user = ids
    _write(tmp_path, tenant, user, _REL, b"AAA")
    key = str(uuid4())

    await nas.capture(tenant_id=tenant, user_id=user, path=_REL, snapshot_key=key)

    user_dir = artifact_snapshot_dir(str(tmp_path), tenant, user)
    assert stat.S_IMODE(os.stat(user_dir / key).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(user_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(user_dir.parent).st_mode) == 0o700


async def test_capture_refuses_a_file_over_the_snapshot_cap(
    tmp_path: Path,
    nas: NasArtifactSnapshotStore,
    ids: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant, user = ids
    monkeypatch.setattr(snapshots_mod, "MAX_SNAPSHOT_BYTES", 10)
    _write(tmp_path, tenant, user, _REL, b"x" * 11)
    key = str(uuid4())

    with pytest.raises(WorkspaceFileTooLargeError):
        await nas.capture(tenant_id=tenant, user_id=user, path=_REL, snapshot_key=key)

    assert not (artifact_snapshot_dir(str(tmp_path), tenant, user) / key).exists()


def test_snapshot_cap_equals_the_download_cap() -> None:
    """副本上限 = 下载上限:存下来的每一版都下得出去。"""
    assert MAX_SNAPSHOT_BYTES == nas_mod._MAX_READ_BYTES


async def test_capture_follows_no_symlink(
    tmp_path: Path, nas: NasArtifactSnapshotStore, ids: tuple[UUID, UUID]
) -> None:
    tenant, user = ids
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"other tenant")
    link = tmp_path / str(tenant) / str(user) / _REL
    link.parent.mkdir(parents=True)
    link.symlink_to(secret)

    with pytest.raises(WorkspacePathEscapeError):
        await nas.capture(tenant_id=tenant, user_id=user, path=_REL, snapshot_key=str(uuid4()))


def _changes_on_reads(monkeypatch: pytest.MonkeyPatch, changed_reads: set[int]) -> None:
    """让第 N 次 ``read_file(stable=True)`` 看到「读后的 fstat 与读前不同」。

    每次读调两次 ``_identity``(读后、读前各一次);``{1}`` = 第一次读的过程中文件变了,
    ``{1, 2}`` = 重读时又变了。
    """
    real = nas_mod._identity
    calls: list[int] = []

    def fake(info: os.stat_result) -> tuple[int, int, int]:
        calls.append(1)
        read_no = (len(calls) + 1) // 2
        size, mtime_ns, ino = real(info)
        first_of_pair = len(calls) % 2 == 1
        if read_no in changed_reads and first_of_pair:
            return (size, mtime_ns + 7_000_000_000, ino)
        return (size, mtime_ns, ino)

    monkeypatch.setattr(nas_mod, "_identity", fake)


async def test_a_file_that_changes_while_read_is_not_kept(
    tmp_path: Path,
    nas: NasArtifactSnapshotStore,
    ids: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读前读后的 fstat 对不上(另一个写入者写到一半)—— 重读一次仍变 → 报错不存。"""
    tenant, user = ids
    _write(tmp_path, tenant, user, _REL, b"AAA")
    _changes_on_reads(monkeypatch, {1, 2})
    key = str(uuid4())

    with pytest.raises(WorkspaceFileChangedError):
        await nas.capture(tenant_id=tenant, user_id=user, path=_REL, snapshot_key=key)

    assert not (artifact_snapshot_dir(str(tmp_path), tenant, user) / key).exists()


async def test_a_change_during_the_first_read_is_retried_once(
    tmp_path: Path,
    nas: NasArtifactSnapshotStore,
    ids: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant, user = ids
    _write(tmp_path, tenant, user, _REL, b"AAA")
    _changes_on_reads(monkeypatch, {1})
    key = str(uuid4())

    digest = await nas.capture(tenant_id=tenant, user_id=user, path=_REL, snapshot_key=key)

    assert digest.size_bytes == 3


async def test_a_failed_write_leaves_no_file_under_any_name(
    tmp_path: Path,
    nas: NasArtifactSnapshotStore,
    ids: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant, user = ids
    _write(tmp_path, tenant, user, _REL, b"AAA")

    def broken_fsync(fd: int) -> None:
        raise OSError(5, "I/O error")

    monkeypatch.setattr(snapshots_mod.os, "fsync", broken_fsync)
    key = str(uuid4())

    with pytest.raises(OSError):
        await nas.capture(tenant_id=tenant, user_id=user, path=_REL, snapshot_key=key)

    assert list(artifact_snapshot_dir(str(tmp_path), tenant, user).iterdir()) == []


async def test_read_missing_and_symlinked_copies(
    tmp_path: Path, nas: NasArtifactSnapshotStore, ids: tuple[UUID, UUID]
) -> None:
    tenant, user = ids
    with pytest.raises(WorkspaceFileNotFoundError):
        await nas.read(tenant_id=tenant, user_id=user, snapshot_key=str(uuid4()))

    user_dir = artifact_snapshot_dir(str(tmp_path), tenant, user)
    user_dir.mkdir(parents=True)
    (tmp_path / "elsewhere").write_bytes(b"not a copy")
    key = str(uuid4())
    (user_dir / key).symlink_to(tmp_path / "elsewhere")
    with pytest.raises(WorkspacePathEscapeError):
        await nas.read(tenant_id=tenant, user_id=user, snapshot_key=key)


async def test_snapshot_key_must_be_a_uuid(
    nas: NasArtifactSnapshotStore, ids: tuple[UUID, UUID]
) -> None:
    tenant, user = ids
    with pytest.raises(WorkspacePathEscapeError):
        await nas.read(tenant_id=tenant, user_id=user, snapshot_key="../../etc/passwd")


async def test_delete_removes_only_that_copy(
    tmp_path: Path, nas: NasArtifactSnapshotStore, ids: tuple[UUID, UUID]
) -> None:
    tenant, user = ids
    _write(tmp_path, tenant, user, _REL, b"AAA")
    keep, drop = str(uuid4()), str(uuid4())
    await nas.capture(tenant_id=tenant, user_id=user, path=_REL, snapshot_key=keep)
    await nas.capture(tenant_id=tenant, user_id=user, path=_REL, snapshot_key=drop)

    assert await nas.delete(tenant_id=tenant, user_id=user, snapshot_key=drop) is True
    assert await nas.delete(tenant_id=tenant, user_id=user, snapshot_key=drop) is False

    assert [p.name for p in artifact_snapshot_dir(str(tmp_path), tenant, user).iterdir()] == [keep]


async def test_delete_all_for_user_touches_nobody_else(
    tmp_path: Path, nas: NasArtifactSnapshotStore, ids: tuple[UUID, UUID]
) -> None:
    tenant, user = ids
    other = uuid4()
    source = _write(tmp_path, tenant, user, _REL, b"AAA")
    _write(tmp_path, tenant, other, _REL, b"BBB")
    await nas.capture(tenant_id=tenant, user_id=user, path=_REL, snapshot_key=str(uuid4()))
    await nas.capture(tenant_id=tenant, user_id=user, path=_REL, snapshot_key=str(uuid4()))
    await nas.capture(tenant_id=tenant, user_id=other, path=_REL, snapshot_key=str(uuid4()))

    assert await nas.delete_all_for_user(tenant_id=tenant, user_id=user) == 2

    assert not artifact_snapshot_dir(str(tmp_path), tenant, user).exists()
    assert len(list(artifact_snapshot_dir(str(tmp_path), tenant, other).iterdir())) == 1
    assert source.read_bytes() == b"AAA", "清副本动了工作区里的文件"
    assert await nas.delete_all_for_user(tenant_id=tenant, user_id=user) == 0


async def test_in_memory_store_copies_through_the_workspace_store(ids: tuple[UUID, UUID]) -> None:
    tenant, user = ids
    workspace = RecordingWorkspaceStore(workspace_file_contents={_REL: b"AAA"})
    store = InMemoryArtifactSnapshotStore(workspace=workspace)
    key = str(uuid4())

    digest = await store.capture(tenant_id=tenant, user_id=user, path=_REL, snapshot_key=key)
    workspace.workspace_file_contents[_REL] = b"BBB"

    assert digest.sha256 == hashlib.sha256(b"AAA").hexdigest()
    assert await store.read(tenant_id=tenant, user_id=user, snapshot_key=key) == b"AAA"
    with pytest.raises(WorkspaceFileNotFoundError):
        await store.read(tenant_id=uuid4(), user_id=user, snapshot_key=key)
    assert await store.delete_all_for_user(tenant_id=tenant, user_id=user) == 1
    with pytest.raises(WorkspaceFileNotFoundError):
        await store.read(tenant_id=tenant, user_id=user, snapshot_key=key)
