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
    ArtifactSnapshotReadError,
    InMemoryArtifactSnapshotStore,
    NasArtifactSnapshotStore,
    SnapshotDigest,
)
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore, workspace_user_root
from orchestrator.tools.sandbox import (
    SandboxSupervisorError,
    WorkspaceFileChangedError,
    WorkspaceFileNotFoundError,
    WorkspaceFileTooLargeError,
    WorkspacePathEscapeError,
    WorkspacePermissionError,
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


class _ConcurrentWriter:
    """另一个写入者在读的过程中改写同一个文件 —— 挂在 ``nas_workspace_store`` 的
    ``os.fstat`` 上,只作用于目标文件的 fd。

    每次读调两次 fstat(读前 / 读后)。``mode``:

    * ``"rewrite"`` —— 读完、读后那次 fstat 之前,把文件改写成**同样长度**的别的内容并
      推后 mtime:读到的字节数与读前大小相同,只有读前读后的 fstat 对不上。
    * ``"append"`` —— 读前那次 fstat 之后立刻往文件末尾追加,读后那次 fstat 却谎报读前
      的结果:只有「读到的字节数 ≠ 读前大小」这一条能发现。

    ``reads`` 是要动手脚的第几次读(从 1 数)。
    """

    def __init__(self, target: Path, mode: str, reads: set[int]) -> None:
        self.target = target
        self.mode = mode
        self.reads = reads
        self.calls = 0
        self._real = os.fstat
        self._before: os.stat_result | None = None

    def __call__(self, fd: int) -> os.stat_result:
        info = self._real(fd)
        if info.st_ino != self._real_target().st_ino:
            return info
        self.calls += 1
        read_no = (self.calls + 1) // 2
        is_before = self.calls % 2 == 1
        if read_no not in self.reads:
            return info
        if self.mode == "append" and is_before:
            self._before = info
            with self.target.open("ab") as handle:
                handle.write(b"-more")
            return info
        if self.mode == "append":
            assert self._before is not None
            return self._before
        if is_before:
            return info
        data = self.target.read_bytes()
        self.target.write_bytes(bytes(reversed(data)) if len(set(data)) > 1 else b"Z" * len(data))
        later = info.st_mtime_ns + 5_000_000_000
        os.utime(self.target, ns=(later, later))
        return self._real(fd)

    def _real_target(self) -> os.stat_result:
        return os.stat(self.target)


def _writer(
    monkeypatch: pytest.MonkeyPatch, target: Path, mode: str, reads: set[int]
) -> _ConcurrentWriter:
    writer = _ConcurrentWriter(target, mode, reads)
    monkeypatch.setattr(nas_mod.os, "fstat", writer)
    return writer


async def test_a_file_rewritten_while_read_is_not_kept(
    tmp_path: Path,
    nas: NasArtifactSnapshotStore,
    ids: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读的过程中文件被改写(同样长度)、重读一次仍在变 → 报错,不存半截 / 混合的内容。"""
    tenant, user = ids
    source = _write(tmp_path, tenant, user, _REL, b"ABC")
    _writer(monkeypatch, source, "rewrite", {1, 2})
    key = str(uuid4())

    with pytest.raises(WorkspaceFileChangedError):
        await nas.capture(tenant_id=tenant, user_id=user, path=_REL, snapshot_key=key)

    assert not (artifact_snapshot_dir(str(tmp_path), tenant, user) / key).exists()


async def test_a_file_growing_while_read_is_not_kept(
    tmp_path: Path,
    nas: NasArtifactSnapshotStore,
    ids: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读的过程中文件变长,而读后的 fstat 看不出来 —— 读到的字节数与读前大小对不上也算变了。"""
    tenant, user = ids
    source = _write(tmp_path, tenant, user, _REL, b"ABC")
    _writer(monkeypatch, source, "append", {1, 2})

    with pytest.raises(WorkspaceFileChangedError):
        await nas.capture(tenant_id=tenant, user_id=user, path=_REL, snapshot_key=str(uuid4()))


async def test_a_change_during_the_first_read_is_retried_once(
    tmp_path: Path,
    nas: NasArtifactSnapshotStore,
    ids: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant, user = ids
    source = _write(tmp_path, tenant, user, _REL, b"ABC")
    writer = _writer(monkeypatch, source, "rewrite", {1})
    key = str(uuid4())

    digest = await nas.capture(tenant_id=tenant, user_id=user, path=_REL, snapshot_key=key)

    assert writer.calls == 4, "第一次读发现变了之后应当重读一次"
    copy = artifact_snapshot_dir(str(tmp_path), tenant, user) / key
    assert copy.read_bytes() == source.read_bytes() == b"CBA"
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


# ---------------------------------------------------------------- 读副本的错误分类(B-235 L2)
#
# 下载端点的翻译梯子只认 ``SandboxSupervisorError`` 家族:读不动(权限)→ 500,别的读失败
# → 404。裸 ``OSError`` 漏出去就是一个没有信封的 500 + traceback。


async def _one_copy(
    tmp_path: Path, nas: NasArtifactSnapshotStore, tenant: UUID, user: UUID
) -> Path:
    _write(tmp_path, tenant, user, _REL, b"AAA")
    key = str(uuid4())
    await nas.capture(tenant_id=tenant, user_id=user, path=_REL, snapshot_key=key)
    return artifact_snapshot_dir(str(tmp_path), tenant, user) / key


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root 无视文件权限位,chmod 0o000 之后依旧读得动——这条测试的前提"
    "(权限位真的能挡住读)在 root 下不成立",
)
async def test_an_unreadable_copy_directory_is_a_permission_error(
    tmp_path: Path, nas: NasArtifactSnapshotStore, ids: tuple[UUID, UUID]
) -> None:
    tenant, user = ids
    copy = await _one_copy(tmp_path, nas, tenant, user)
    copy.parent.chmod(0o000)
    try:
        with pytest.raises(WorkspacePermissionError):
            await nas.read(tenant_id=tenant, user_id=user, snapshot_key=copy.name)
    finally:
        copy.parent.chmod(0o700)


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root 无视文件权限位,chmod 0o000 之后依旧读得动——这条测试的前提"
    "(权限位真的能挡住读)在 root 下不成立",
)
async def test_an_unreadable_copy_is_a_permission_error(
    tmp_path: Path, nas: NasArtifactSnapshotStore, ids: tuple[UUID, UUID]
) -> None:
    tenant, user = ids
    copy = await _one_copy(tmp_path, nas, tenant, user)
    copy.chmod(0o000)
    try:
        with pytest.raises(WorkspacePermissionError):
            await nas.read(tenant_id=tenant, user_id=user, snapshot_key=copy.name)
    finally:
        copy.chmod(0o600)


async def test_an_io_error_reading_a_copy_stays_in_the_supervisor_error_family(
    tmp_path: Path,
    nas: NasArtifactSnapshotStore,
    ids: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant, user = ids
    copy = await _one_copy(tmp_path, nas, tenant, user)

    target_ino = os.stat(copy).st_ino
    real_fstat = os.fstat

    def broken_fstat(fd: int) -> os.stat_result:
        # 只让这个副本的 fd 出错 —— 补丁打在全局 ``os`` 上,别的 fstat 照常。
        info = real_fstat(fd)
        if info.st_ino == target_ino:
            raise OSError(5, "Input/output error")
        return info

    monkeypatch.setattr(snapshots_mod.os, "fstat", broken_fstat)
    with pytest.raises(ArtifactSnapshotReadError) as excinfo:
        await nas.read(tenant_id=tenant, user_id=user, snapshot_key=copy.name)
    assert isinstance(excinfo.value, SandboxSupervisorError)
    assert not isinstance(excinfo.value, WorkspaceFileNotFoundError)


async def test_an_io_error_opening_the_copy_directory_is_not_called_an_escape(
    tmp_path: Path,
    nas: NasArtifactSnapshotStore,
    ids: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant, user = ids
    copy = await _one_copy(tmp_path, nas, tenant, user)

    def stale(*_: object, **__: object) -> int:
        raise OSError(70, "Stale NFS file handle")

    monkeypatch.setattr(snapshots_mod, "_open_user_dir", stale)
    with pytest.raises(ArtifactSnapshotReadError):
        await nas.read(tenant_id=tenant, user_id=user, snapshot_key=copy.name)
