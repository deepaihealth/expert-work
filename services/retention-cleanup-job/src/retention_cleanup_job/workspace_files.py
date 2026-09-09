"""留存 job 碰 NAS 工作区文件的全部入口 —— 三条规则共用的路径闸(波 3 线 R PR2)。

布局 ``{root}/{tenant_id}/{user_id}/...`` 与
``orchestrator.tools.nas_workspace_store.workspace_user_root`` 同一拼法。这里
不 import 它:orchestrator 会把 langchain 整套拖进这个 5 模块的 job,而拼法只有
一行;两边的 parity 由 ``tests/test_workspace_files.py`` 钉住。

**不变式**(PR 正文 + ``tests/test_workspace_invariant.py``):对活着的工作区,
job 只删 (a) 登记过的产物文件 (b) ``uploads/`` 下登记过的文件 (c) 孤儿
``threads/<thread_id>/``;根目录任何其它文件/目录 —— ``style/``、MEMORY.md、
未登记的文件 —— 永不触碰。这个模块的形状就是不变式本身:没有任何「按时间扫
目录删文件」的函数,只有「按登记路径删一个文件」和「按 thread_id 删一个目录」;
删文件只 ``unlink`` 一个普通文件,从不删它所在的目录;删目录只认
``threads/<uuid>``。

**symlink 三处都不跟**(审查补):``root`` 自身(:func:`validate_workspace_root`,
启动时一次)、``{tenant}`` / ``{user}`` 两级目录(:func:`_ensure_plain_dirs`,每次
删之前)、叶子(``lstat``)。NAS store 把 tenant / user 当可信前缀直接 ``resolve``,
这里不这么做 —— 删除是不可逆的,多一次 ``lstat`` 便宜。
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import UUID

logger = logging.getLogger(__name__)

#: 与 ``orchestrator.context.workspace_projection.THREADS_DIR`` 同值(不跨包 import,
#: 理由见模块头)。
THREADS_DIR = "threads"

#: 与 ``orchestrator.tools.nas_workspace_store.DELETED_DIR`` 同值:租户目录下的
#: 软删标记目录,``{root}/{tenant}/.deleted/{user}`` 一个空文件一个已软删用户。
DELETED_DIR = ".deleted"


class UnsafeWorkspacePathError(ValueError):
    """登记的路径不是「用户工作区内的一个普通文件/目录」—— 绝对路径、``..``、
    symlink(叶子或 tenant / user 两级)、逃出用户根。这类行**不删**(留着让人
    看),文件更不删。"""


@dataclass(frozen=True)
class ThreadDir:
    tenant_id: UUID
    user_id: UUID
    thread_id: UUID
    path: Path
    #: 目录本身与它直接子项里最新的 mtime —— 孤儿宽限按这个算(投影落地时
    #: 写的是目录里的文件,目录 mtime 只在增删子项时变)。
    newest_mtime: float


def user_root(root: str, tenant_id: UUID, user_id: UUID) -> Path:
    """``{root}/{tenant_id}/{user_id}`` —— 同 ``workspace_user_root`` 的拼法。

    只 ``resolve`` ``root``(启动时已校验非 symlink),tenant / user 两级**不**
    ``resolve``:它们是否是 symlink 由 :func:`_ensure_plain_dirs` 用 ``lstat`` 判,
    跟着链接走到的「真实路径」正是要拒绝的东西。
    """
    return Path(root).resolve() / str(tenant_id) / str(user_id)


def deleted_marker(root: str, tenant_id: UUID, user_id: UUID) -> Path:
    """``{root}/{tenant_id}/.deleted/{user_id}`` —— 同 ``workspace_deleted_marker``。"""
    return Path(root).resolve() / str(tenant_id) / DELETED_DIR / str(user_id)


def validate_workspace_root(root: str) -> Path:
    """启动时校验 ``workspace_root``:必须存在、是目录、不是 symlink。

    NAS 没挂上(目录不存在)是最危险的形态:每个登记文件都会「看起来不在」,
    行被删、字节永远留在没挂上的卷里 —— 所以缺失不是「跳过」而是拒跑。
    """
    path = Path(root)
    try:
        st = os.lstat(path)
    except FileNotFoundError as exc:
        raise ValueError(f"workspace_root does not exist: {root!r}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise ValueError(f"workspace_root must not be a symlink: {root!r}")
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError(f"workspace_root is not a directory: {root!r}")
    return path.resolve()


def _ensure_plain_dirs(root: str, tenant_id: UUID, user_id: UUID) -> None:
    """``{root}/{tenant}`` 与 ``{root}/{tenant}/{user}`` 都不能是 symlink。

    不存在不算错(调用方按「文件不在」处理);存在但是链接 →
    :class:`UnsafeWorkspacePathError`。词法上仍在用户根下的登记路径,真实落点
    可能在工作区外面 —— 这一步挡的就是那种情况(审查补,有失败在先的测试)。
    """
    base = Path(root).resolve()
    for candidate in (base / str(tenant_id), base / str(tenant_id) / str(user_id)):
        try:
            st = os.lstat(candidate)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(st.st_mode):
            raise UnsafeWorkspacePathError(f"workspace dir is a symlink: {candidate.name}")


def _registered_target(root: str, tenant_id: UUID, user_id: UUID, relpath: str) -> Path:
    """把一条登记的工作区相对路径落到用户根下,拒绝一切能逃出去的形状。"""
    cleaned = relpath.strip()
    if not cleaned or cleaned.startswith("/") or "\0" in cleaned:
        raise UnsafeWorkspacePathError(f"not a relative workspace path: {relpath!r}")
    parts = PurePosixPath(cleaned).parts
    if not parts or ".." in parts:
        raise UnsafeWorkspacePathError(f"not a relative workspace path: {relpath!r}")
    _ensure_plain_dirs(root, tenant_id, user_id)
    base = user_root(root, tenant_id, user_id)
    target = base.joinpath(*parts)
    # ``resolve()`` 跟随中间层的 symlink:某一级目录被换成指向工作区外的链接时,
    # 解析结果就落到根外面 —— 这一步挡的正是这种情况。叶子自己是否 symlink 由
    # 调用方 ``lstat`` 判。
    try:
        resolved = target.resolve(strict=False)
    except OSError as exc:
        raise UnsafeWorkspacePathError(f"cannot resolve workspace path: {relpath!r}") from exc
    if not resolved.is_relative_to(base):
        raise UnsafeWorkspacePathError(f"workspace path escapes the user root: {relpath!r}")
    return target


def unlink_registered_file(root: str, tenant_id: UUID, user_id: UUID, relpath: str) -> bool:
    """删掉一条登记路径指向的**普通文件**。返回是否真的删了(不存在 → ``False``)。

    只 ``unlink`` 那一个文件,永不删它所在的目录(``高血压21天随访/day1.md`` 到期
    只删 ``day1.md``,目录留着)。叶子是 symlink / 目录 / 其它非普通文件 →
    :class:`UnsafeWorkspacePathError`,调用方保留登记行不删。用户根不存在(工作区
    已被 janitor 归档并 rmtree)→ 视同文件不存在。
    """
    target = _registered_target(root, tenant_id, user_id, relpath)
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise UnsafeWorkspacePathError(f"registered path is not a regular file: {relpath!r}")
    try:
        os.unlink(target)
    except FileNotFoundError:
        return False
    return True


def remove_thread_dir(root: str, tenant_id: UUID, user_id: UUID, thread_id: UUID) -> bool:
    """``rm -rf {user_root}/threads/{thread_id}``。返回是否真的删了。

    只认 ``threads/<uuid>`` 这一种形状 —— ``thread_id`` 是 UUID 类型,拼不出别的
    路径。tenant / user 目录或目录本身是 symlink → :class:`UnsafeWorkspacePathError`
    (不顺着链接删)。
    """
    _ensure_plain_dirs(root, tenant_id, user_id)
    target = user_root(root, tenant_id, user_id) / THREADS_DIR / str(thread_id)
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise UnsafeWorkspacePathError(f"thread dir is not a directory: {thread_id}")
    shutil.rmtree(target)
    return True


def _uuid_dirs(path: Path) -> list[tuple[UUID, Path]]:
    """``path`` 下名字能解析成 UUID 的子目录 —— 布局约定之外的东西
    (``.deleted`` / ``_scratch`` / lost+found / 垃圾)天然被挡掉;symlink 不算目录
    (``follow_symlinks=False``);单目录扫描失败 log + 返回已收集的部分(照
    workspace_janitor._list_uuid_dirs)。"""
    out: list[tuple[UUID, Path]] = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                try:
                    out.append((UUID(entry.name), Path(entry.path)))
                except ValueError:
                    continue
    except FileNotFoundError:
        return []
    except OSError:
        logger.warning("retention.workspace_scan_failed path=%s", path)
        return out
    return sorted(out, key=lambda t: str(t[0]))


def _newest_mtime(path: Path) -> float:
    """目录本身与直接子项里最新的 mtime(子项不跟 symlink)。"""
    newest = os.lstat(path).st_mtime
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    return newest


def iter_thread_dirs(root: str) -> Iterator[ThreadDir]:
    """枚举 ``{root}/<tenant>/<user>/threads/<thread_id>/`` —— 三层都必须是 UUID 名。

    已软删的用户(``.deleted/<user>`` 标记在)整体跳过:那棵树归 janitor 归档 +
    rmtree,留存 job 不在归档前改动它的内容。
    """
    for tenant_id, tenant_dir in _uuid_dirs(Path(root)):
        for user_id, user_dir in _uuid_dirs(tenant_dir):
            if deleted_marker(root, tenant_id, user_id).exists():
                continue
            for thread_id, thread_dir in _uuid_dirs(user_dir / THREADS_DIR):
                try:
                    newest = _newest_mtime(thread_dir)
                except OSError:
                    continue
                yield ThreadDir(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    thread_id=thread_id,
                    path=thread_dir,
                    newest_mtime=newest,
                )


__all__ = [
    "DELETED_DIR",
    "THREADS_DIR",
    "ThreadDir",
    "UnsafeWorkspacePathError",
    "deleted_marker",
    "iter_thread_dirs",
    "remove_thread_dir",
    "unlink_registered_file",
    "user_root",
    "validate_workspace_root",
]
