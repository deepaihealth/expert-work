"""产物版本副本 —— 每次 ``save_artifact`` 都把那一刻的字节留一份(B-235)。

``save_artifact`` 原来只登记工作区路径,不留字节。产物的身份是
``(tenant, user, agent_key, name)``,同一个用户在两段会话里用同一个智能体、存同一个名字,
两个版本就指着工作区里的同一个文件:后存的那次把前一版的内容盖掉,前一段会话的对话页
从此下载到的是后一段会话的产出。版本行上的 ``sha256`` 又是第一次下载时才补的,所以连
「这一版原来是什么」都无从核对。

这里的做法:登记前把文件当时的字节复制一份,以**版本行 id**(``save_artifact`` 自己生成、
随后原样交给 :meth:`ArtifactStore.save_version`)命名,大小与 sha256 当场算好。副本与版本
行在同一次调用里配对,版本号怎么分配都串不了;工作区里那个文件之后被谁改、被谁覆盖,都
不影响已登记的版本。

两个实现:

* :class:`NasArtifactSnapshotStore` —— 生产(测试 / 生产集群都是 NAS)。副本放在
  ``{root}/{tenant}/.artifact-versions/{user}/{version_id}``:用户根的**旁边**,沙箱只挂
  ``{tenant}/{user}`` 这个 subPath,agent 跑的代码碰不到(同 ``.deleted`` 软删标记的理由);
  control-plane 与留存作业都挂着整棵 NAS,前者写、后者删。
* :class:`InMemoryArtifactSnapshotStore` —— 本地开发(docker 卷 + supervisor 后端,集群上已
  退役)与单元测试。字节在进程内存里,进程重启就没了;那时旧版本下载如实回「内容不在」,
  不会拿工作区里的当前文件冒充。

副本上限 = 下载上限(:data:`MAX_SNAPSHOT_BYTES`):超过的文件不登记,因为它本来就下载不了
(以前是「列表里看得见、下载 413」)。
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from expert_work.persistence import ARTIFACT_SNAPSHOT_DIR
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore, _openat_dir
from orchestrator.tools.sandbox import (
    WorkspaceFileChangedError,
    WorkspaceFileNotFoundError,
    WorkspaceFileTooLargeError,
    WorkspacePathEscapeError,
    WorkspacePermissionError,
)
from orchestrator.tools.workspace_store import WorkspaceStore

#: 单个副本的上限 —— 与下载上限(``nas_workspace_store._MAX_READ_BYTES``)同值,测试钉住两者
#: 相等:存下来的每一版都必须下得出去。
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024

#: 读的过程中文件变了(另一个写入者正在写)时重读几次。一次就够分辨「碰巧撞上」与「一直
#: 在写」;后者如实报错,让模型等写完再登记。
_CHANGED_RETRIES = 1

#: 副本文件 ``rw-------``;新建的目录 ``rwx------``(由 ``_openat_dir`` 设)。只有
#: control-plane / 留存作业这一个 uid 读写它们。
_FILE_MODE = 0o600

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@dataclass(frozen=True)
class SnapshotDigest:
    """副本的大小与 sha256 —— 复制的同时算好,写进版本行。"""

    size_bytes: int
    sha256: str


@runtime_checkable
class ArtifactSnapshotStore(Protocol):
    """产物版本副本:复制 / 读 / 删一个 / 删一个用户的全部。"""

    async def capture(
        self, *, tenant_id: UUID, user_id: UUID, path: str, snapshot_key: str
    ) -> SnapshotDigest:
        """把用户根相对的 ``path`` 当前的字节复制成副本 ``snapshot_key``。

        失败一律抛 :class:`~orchestrator.tools.sandbox.SandboxSupervisorError` 家族或
        ``OSError``,且不留下以 ``snapshot_key`` 命名的文件:调用方据此不登记。
        """

    async def read(self, *, tenant_id: UUID, user_id: UUID, snapshot_key: str) -> bytes:
        """读副本。不存在 → :class:`WorkspaceFileNotFoundError`。"""

    async def delete(self, *, tenant_id: UUID, user_id: UUID, snapshot_key: str) -> bool:
        """删一个副本;返回是否真的删了(本来就不在 → ``False``)。"""

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        """删一个用户的全部副本(清除用户数据);返回删掉的文件数。"""


def _digest(data: bytes) -> SnapshotDigest:
    return SnapshotDigest(size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())


def _require_key(snapshot_key: str) -> str:
    """副本键必须是规范写法的 UUID —— 它会被拼进一条路径,别的形状一律拒。"""
    try:
        canonical = str(UUID(snapshot_key))
    except ValueError:
        canonical = ""
    if canonical != snapshot_key:
        msg = f"artifact snapshot key is not a UUID: {snapshot_key!r}"
        raise WorkspacePathEscapeError(msg)
    return snapshot_key


@dataclass
class InMemoryArtifactSnapshotStore:
    """进程内的副本 —— 本地开发(supervisor 后端)与单元测试用。"""

    workspace: WorkspaceStore
    _blobs: dict[tuple[UUID, UUID, str], bytes] = field(default_factory=dict)

    async def capture(
        self, *, tenant_id: UUID, user_id: UUID, path: str, snapshot_key: str
    ) -> SnapshotDigest:
        key = _require_key(snapshot_key)
        data = await self.workspace.read_file(
            tenant_id=tenant_id, user_id=user_id, path=path, max_bytes=MAX_SNAPSHOT_BYTES
        )
        self._blobs[(tenant_id, user_id, key)] = data
        return _digest(data)

    async def read(self, *, tenant_id: UUID, user_id: UUID, snapshot_key: str) -> bytes:
        try:
            return self._blobs[(tenant_id, user_id, _require_key(snapshot_key))]
        except KeyError:
            msg = f"artifact snapshot not found: {snapshot_key!r}"
            raise WorkspaceFileNotFoundError(msg) from None

    async def delete(self, *, tenant_id: UUID, user_id: UUID, snapshot_key: str) -> bool:
        return self._blobs.pop((tenant_id, user_id, _require_key(snapshot_key)), None) is not None

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        victims = [k for k in self._blobs if k[0] == tenant_id and k[1] == user_id]
        for k in victims:
            del self._blobs[k]
        return len(victims)


@dataclass
class NasArtifactSnapshotStore:
    """生产实现 —— 副本放在 NAS 上用户根旁边、沙箱挂不到的地方。

    读源文件走 :meth:`NasWorkspaceStore.read_file`(同一条 ``openat`` + ``O_NOFOLLOW`` 链,
    ``stable=True`` 防半截);写副本也全部相对目录 fd 做,不重走字符串路径。
    """

    workspace: NasWorkspaceStore

    async def capture(
        self, *, tenant_id: UUID, user_id: UUID, path: str, snapshot_key: str
    ) -> SnapshotDigest:
        key = _require_key(snapshot_key)
        data = await self._read_stable(tenant_id=tenant_id, user_id=user_id, path=path)
        await asyncio.to_thread(_write_copy, self.workspace.root, tenant_id, user_id, key, data)
        return _digest(data)

    async def _read_stable(self, *, tenant_id: UUID, user_id: UUID, path: str) -> bytes:
        for attempt in range(_CHANGED_RETRIES + 1):
            try:
                return await self.workspace.read_file(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    path=path,
                    max_bytes=MAX_SNAPSHOT_BYTES,
                    stable=True,
                )
            except WorkspaceFileChangedError:
                if attempt == _CHANGED_RETRIES:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises

    async def read(self, *, tenant_id: UUID, user_id: UUID, snapshot_key: str) -> bytes:
        key = _require_key(snapshot_key)
        return await asyncio.to_thread(_read_copy, self.workspace.root, tenant_id, user_id, key)

    async def delete(self, *, tenant_id: UUID, user_id: UUID, snapshot_key: str) -> bool:
        key = _require_key(snapshot_key)
        return await asyncio.to_thread(_delete_copy, self.workspace.root, tenant_id, user_id, key)

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        return await asyncio.to_thread(_delete_user_copies, self.workspace.root, tenant_id, user_id)


# --------------------------------------------------------------------------- NAS helpers
#
# ``tenant_id`` / ``user_id`` 是 UUID,``key`` 过了 :func:`_require_key` —— 三段都拼不出
# 别的路径。每一级仍用 ``O_NOFOLLOW`` 打开:这棵树挂在多租户共享的 NAS 上,别处若被
# 换成 symlink,这里宁可失败也不跟过去。


def _open_snapshot_parent(root: str, tenant_id: UUID, *, create: bool) -> int:
    """``{root}/{tenant}/.artifact-versions`` 的目录 fd。"""
    root_fd = os.open(Path(root), os.O_RDONLY | os.O_DIRECTORY)
    try:
        tenant_fd = _openat_dir(root_fd, str(tenant_id), create=create)
    finally:
        os.close(root_fd)
    try:
        return _openat_dir(tenant_fd, ARTIFACT_SNAPSHOT_DIR, create=create)
    finally:
        os.close(tenant_fd)


def _open_user_dir(root: str, tenant_id: UUID, user_id: UUID, *, create: bool) -> int:
    parent_fd = _open_snapshot_parent(root, tenant_id, create=create)
    try:
        return _openat_dir(parent_fd, str(user_id), create=create)
    finally:
        os.close(parent_fd)


def _write_copy(root: str, tenant_id: UUID, user_id: UUID, key: str, data: bytes) -> None:
    """先写 ``{key}.tmp-<随机>``,fsync,再 rename 成 ``{key}`` —— 最终名字下只会出现完整文件。"""
    user_fd = _open_user_dir(root, tenant_id, user_id, create=True)
    tmp = f"{key}.tmp-{uuid4().hex}"
    try:
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, _FILE_MODE, dir_fd=user_fd
        )
        try:
            try:
                os.fchmod(fd, _FILE_MODE)  # umask 会吃掉 open 的 mode 位
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(tmp, key, src_dir_fd=user_fd, dst_dir_fd=user_fd)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp, dir_fd=user_fd)
            raise
    finally:
        os.close(user_fd)


def _read_copy(root: str, tenant_id: UUID, user_id: UUID, key: str) -> bytes:
    try:
        user_fd = _open_user_dir(root, tenant_id, user_id, create=False)
    except FileNotFoundError as exc:
        raise WorkspaceFileNotFoundError(f"artifact snapshot not found: {key!r}") from exc
    except OSError as exc:
        raise WorkspacePathEscapeError(f"artifact snapshot directory unusable: {key!r}") from exc
    try:
        try:
            fd = os.open(key, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=user_fd)
        except FileNotFoundError as exc:
            raise WorkspaceFileNotFoundError(f"artifact snapshot not found: {key!r}") from exc
        except PermissionError as exc:
            raise WorkspacePermissionError(f"artifact snapshot not readable: {key!r}") from exc
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise WorkspacePathEscapeError(f"artifact snapshot is a symlink: {key!r}") from exc
            raise WorkspaceFileNotFoundError(f"artifact snapshot not found: {key!r}") from exc
    finally:
        os.close(user_fd)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise WorkspaceFileNotFoundError(f"artifact snapshot is not a file: {key!r}")
        if info.st_size > MAX_SNAPSHOT_BYTES:
            raise WorkspaceFileTooLargeError(f"artifact snapshot {key!r} exceeds the read cap")
        return handle.read()


def _delete_copy(root: str, tenant_id: UUID, user_id: UUID, key: str) -> bool:
    try:
        user_fd = _open_user_dir(root, tenant_id, user_id, create=False)
    except FileNotFoundError:
        return False
    try:
        os.unlink(key, dir_fd=user_fd)
    except FileNotFoundError:
        return False
    finally:
        os.close(user_fd)
    return True


def _delete_user_copies(root: str, tenant_id: UUID, user_id: UUID) -> int:
    try:
        parent_fd = _open_snapshot_parent(root, tenant_id, create=False)
    except FileNotFoundError:
        return 0
    try:
        name = str(user_id)
        try:
            info = os.lstat(name, dir_fd=parent_fd)
        except FileNotFoundError:
            return 0
        if not stat.S_ISDIR(info.st_mode):
            msg = f"artifact snapshot entry is not a directory: {name!r}"
            raise WorkspacePathEscapeError(msg)
        user_fd = _openat_dir(parent_fd, name, create=False)
        try:
            count = sum(1 for _ in os.scandir(user_fd))
        finally:
            os.close(user_fd)
        shutil.rmtree(name, dir_fd=parent_fd)
        return count
    finally:
        os.close(parent_fd)
