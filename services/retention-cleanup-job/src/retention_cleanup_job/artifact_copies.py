"""产物版本副本的留存处理(B-235)—— 拆出 ``job.py`` 以免它越过 800 行。

``save_artifact`` 每次登记都在 ``{root}/{tenant}/.artifact-versions/{user}/{version_id}`` 留一份
那一刻的字节(路径由 ``expert_work.persistence.artifact_snapshot_dir`` 拼,control-plane 写、
这里删,同一个函数)。三件事:

* :func:`release_version_files` —— 版本行要删之前,在**没有别的版本还指着同一路径**时
  unlink 工作区文件,再删它的副本。同名重登记的各版本指着同一个工作区文件,老版本到期时
  新版本还在用它;B-235 之前这里不看,于是删掉了新版本的文件。
* :func:`remove_user_copies` —— 一个用户的副本目录整个删掉(已删工作区 90 天销账)。
* :func:`sweep_orphan_copies` —— 找不到版本行、满 24 小时的副本删掉(登记失败后没删成、
  清除用户时没删成),``.tmp-`` 写到一半的残件同样。只认 UUID 名字。

symlink 一概不跟:租户目录、``.artifact-versions``、用户目录、叶子,任何一级是链接都不碰
(同 :mod:`retention_cleanup_job.workspace_files` 的取舍 —— 删除不可逆)。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from expert_work.persistence import ARTIFACT_SNAPSHOT_DIR, artifact_snapshot_dir
from expert_work.persistence.artifact import ArtifactStore
from expert_work.protocol import ArtifactVersion
from retention_cleanup_job.workspace_files import (
    UnsafeWorkspacePathError,
    unlink_registered_file,
)

logger = logging.getLogger(__name__)

#: 孤儿副本的宽限:登记先写副本、再写版本行,两步之间的副本看起来就是「没有行」。
_ORPHAN_MIN_AGE_S = 24 * 3600
#: 一次查库问多少个 id。
_ID_BATCH = 500
#: 「大批孤儿」把关:找不到版本行的副本超过这么多、且占了多数时不删。真实的孤儿是零星的
#: (登记失败、清除用户没删成);一大批同时「找不到行」更像是库里看不见行(连库角色没有
#: BYPASSRLS、连错了库、表被清空),而删除不可逆 —— 停下来记日志等人看。
_MASS_ORPHAN_MIN = 50
_TMP_MARK = ".tmp-"


def _plain_dir(path: Path) -> bool:
    """存在、是目录、不是 symlink。不存在 → ``False``;是链接 / 别的 → 抛。"""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise UnsafeWorkspacePathError(f"artifact copy directory is not a plain dir: {path}")
    return True


def _user_copy_dir(root: str, tenant_id: UUID, user_id: UUID) -> Path | None:
    """``{root}/{tenant}/.artifact-versions/{user}``,任何一级是链接 → 抛,不存在 → ``None``。"""
    user_dir = artifact_snapshot_dir(str(Path(root).resolve()), tenant_id, user_id)
    for level in (user_dir.parent.parent, user_dir.parent, user_dir):
        if not _plain_dir(level):
            return None
    return user_dir


def unlink_copy(root: str, tenant_id: UUID, user_id: UUID, snapshot_key: str) -> bool:
    """删一个副本。返回是否真的删了(本来就不在 → ``False``);不是普通文件 → 抛。"""
    key = str(UUID(snapshot_key))
    if key != snapshot_key:
        raise UnsafeWorkspacePathError(f"artifact copy key is not a UUID: {snapshot_key!r}")
    user_dir = _user_copy_dir(root, tenant_id, user_id)
    if user_dir is None:
        return False
    target = user_dir / key
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(st.st_mode):
        raise UnsafeWorkspacePathError(f"artifact copy is not a regular file: {key}")
    try:
        os.unlink(target)
    except FileNotFoundError:
        return False
    return True


def remove_user_copies(root: str, tenant_id: UUID, user_id: UUID) -> int:
    """删一个用户的整个副本目录;返回删掉的文件数。"""
    user_dir = _user_copy_dir(root, tenant_id, user_id)
    if user_dir is None:
        return 0
    count = sum(1 for _ in os.scandir(user_dir))
    shutil.rmtree(user_dir)
    return count


async def release_version_files(
    store: ArtifactStore, root: str, versions: Sequence[ArtifactVersion]
) -> tuple[list[UUID], int]:
    """给即将删行的一批版本(同一 tenant / user)清掉它们的文件。

    返回 ``(文件都清干净、可以删行的版本 id, 删掉的工作区文件数)``。删不掉的版本不在返回
    里 —— 行留着,明天再试(行是那些字节唯一的索引)。

    顺序:先工作区文件、后副本。工作区文件删不掉时副本还在,留下的行仍下载得到;反过来
    先删副本,留下的行就指着一个没了的副本。
    """
    if not versions:
        return [], 0
    first = versions[0]
    in_use = await store.paths_still_registered(
        tenant_id=first.tenant_id,
        user_id=first.user_id,
        paths=sorted({v.path_in_workspace for v in versions}),
        excluding_version_ids=[v.id for v in versions],
    )
    deletable: list[UUID] = []
    files_removed = 0
    for version in versions:
        if version.path_in_workspace not in in_use:
            try:
                removed = await asyncio.to_thread(
                    unlink_registered_file,
                    root,
                    version.tenant_id,
                    version.user_id,
                    version.path_in_workspace,
                )
            except (OSError, UnsafeWorkspacePathError):
                logger.exception("retention.artifact_file_unlink_failed version_id=%s", version.id)
                continue
            files_removed += int(removed)
        if version.snapshot_key is not None:
            try:
                await asyncio.to_thread(
                    unlink_copy, root, version.tenant_id, version.user_id, version.snapshot_key
                )
            except (OSError, ValueError):
                logger.exception("retention.artifact_copy_unlink_failed version_id=%s", version.id)
                continue
        deletable.append(version.id)
    return deletable, files_removed


@dataclass(frozen=True)
class _Candidate:
    path: Path
    version_id: UUID | None  # ``None`` = ``.tmp-`` 残件


def _uuid(name: str) -> UUID | None:
    try:
        parsed = UUID(name)
    except ValueError:
        return None
    return parsed if str(parsed) == name else None


def _candidates(root: str, *, older_than: float) -> list[_Candidate]:
    """``{root}/<tenant>/.artifact-versions/<user>/`` 下够老的 UUID 文件与 ``.tmp-`` 残件。"""
    out: list[_Candidate] = []
    base = Path(root).resolve()
    try:
        tenants = [e for e in os.scandir(base) if _uuid(e.name) and e.is_dir(follow_symlinks=False)]
    except OSError:
        logger.warning("retention.artifact_copy_scan_failed path=%s", base)
        return out
    for tenant in tenants:
        copies = Path(tenant.path) / ARTIFACT_SNAPSHOT_DIR
        try:
            if not _plain_dir(copies):
                continue
            users = [e for e in os.scandir(copies) if _uuid(e.name)]
        except (OSError, UnsafeWorkspacePathError):
            logger.warning("retention.artifact_copy_scan_failed path=%s", copies)
            continue
        for user in users:
            if not user.is_dir(follow_symlinks=False):
                continue
            try:
                entries = list(os.scandir(user.path))
            except OSError:
                logger.warning("retention.artifact_copy_scan_failed path=%s", user.path)
                continue
            for entry in entries:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_mtime >= older_than:
                    continue
                stem, mark, _rest = entry.name.partition(_TMP_MARK)
                if mark and _uuid(stem):
                    out.append(_Candidate(path=Path(entry.path), version_id=None))
                elif (version_id := _uuid(entry.name)) is not None:
                    out.append(_Candidate(path=Path(entry.path), version_id=version_id))
    return out


async def sweep_orphan_copies(
    root: str, store: ArtifactStore, *, min_age_s: float = _ORPHAN_MIN_AGE_S
) -> int:
    """删找不到版本行的副本与 ``.tmp-`` 残件(都要满 ``min_age_s``);返回删掉的文件数。

    Caller MUST be inside an RLS bypass scope (``existing_version_ids`` is cross-tenant).
    删除不可逆,所以「找不到行」必须是真的找不到,两道把关,任一不过就一个都不删:

    1. 连库角色看得见所有租户的行(:meth:`ArtifactStore.bypasses_row_security`)——
       ``bypass_rls_var`` 只管本进程不发 GUC,管不了库;
    2. 找不到行的副本不是「一大批且占多数」(:data:`_MASS_ORPHAN_MIN`)。
    """
    if not await store.bypasses_row_security():
        logger.error(
            "retention.artifact_copy_sweep_refused reason=role_cannot_see_every_row "
            "— the DB role needs BYPASSRLS; nothing deleted"
        )
        return 0
    candidates = await asyncio.to_thread(_candidates, root, older_than=time.time() - min_age_s)
    ids = [c.version_id for c in candidates if c.version_id is not None]
    alive: set[UUID] = set()
    for start in range(0, len(ids), _ID_BATCH):
        alive |= await store.existing_version_ids(ids[start : start + _ID_BATCH])
    orphans = sum(1 for version_id in ids if version_id not in alive)
    if orphans > _MASS_ORPHAN_MIN and orphans * 2 > len(ids):
        logger.error(
            "retention.artifact_copy_sweep_refused reason=mass_orphans orphans=%d copies=%d "
            "— looks like rows are invisible rather than gone; nothing deleted",
            orphans,
            len(ids),
        )
        return 0
    removed = 0
    for candidate in candidates:
        if candidate.version_id is not None and candidate.version_id in alive:
            continue
        try:
            os.unlink(candidate.path)
        except FileNotFoundError:
            continue
        except OSError:
            logger.exception("retention.artifact_copy_orphan_unlink_failed path=%s", candidate.path)
            continue
        removed += 1
    return removed
