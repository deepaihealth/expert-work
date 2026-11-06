"""B-278 —— NAS 上「不管模型留下了什么,都要删得掉」的 ``rm -rf``。

彻底删除智能体(control-plane)和隔离区清理(留存作业)共用。模型能在自己的工作区里留下:

* ``chmod 000`` 的目录、只读的父目录 —— 先把**我们自己拥有**的目录补回属主 ``rwx``(只加不减;
  不是我们的就补不了,留给下一步);补权限时不经过路径字符串:Linux 上 ``O_PATH|O_NOFOLLOW``
  打开、``fstat`` 核对还是同一个目录(``st_dev``/``st_ino``),再对 ``/proc/self/fd/N`` 改;
  没有 ``O_PATH`` 的系统(macOS 测试机)用 ``fchmodat(AT_SYMLINK_NOFOLLOW)``。
* 很深 / 很宽的目录树(``mkdir``/``cd`` 循环)—— 不递归、也不按深度攒 fd:每一轮把子目录
  的子项改名提到顶层、删掉变空的子目录,任意形状的树都只开着三个 fd(``shutil.rmtree`` 在
  3.12 上是递归的,1000 层就 ``RecursionError``)。
* 在我们删的同时被别人删掉的子项(janitor、留存作业、第二个清除、沙箱里的后台进程)——
  只吞 ``FileNotFoundError``,删完必须 ``lstat`` 确认**真的不在了**才算删掉;还在就再删一次。
* 别的 uid 的文件(补不了权限)—— 删不动就整个改名挪到用户根旁边的隔离区
  ``{tenant}/.purged-agents/{user}/{原名}-{秒}-{随机}``(同一个挂载,一次 ``rename``;跨设备
  ``EXDEV`` 算失败)。同名新智能体继承不到,工作区浏览 / 配额 / 按 agent 的清理也看不到它;
  留存作业满 7 天删掉。

符号链接一律不跟:路径中途某段(含用户根)是链接 → 跳过并计数;目标本身是链接 → 删链接本身。
每一步都是 ``openat`` + ``O_NOFOLLOW``,从 NAS 根往下一段一段开。
"""

from __future__ import annotations

import errno
import logging
import os
import stat
import time
from pathlib import Path
from uuid import UUID, uuid4

from expert_work.persistence.workspace.layout import PURGED_AGENTS_DIR

logger = logging.getLogger("expert_work.persistence.workspace.safe_remove")

#: :func:`remove_entry` 的结果。
ABSENT = "absent"
REMOVED = "removed"
UNLINKED = "unlinked"
SKIPPED_SYMLINK = "skipped_symlink"
QUARANTINED = "quarantined"

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_O_PATH: int | None = getattr(os, "O_PATH", None)


class NasEntryStuckError(OSError):
    """删不掉、也挪不进隔离区。只有这种情况调用方才记失败。"""


def add_owner_bits(dfd: int, name: str, expected: os.stat_result) -> bool:
    """给 ``dfd`` 下名为 ``name`` 的**目录**补上属主 ``rwx``(只加不减)。

    ``expected`` 是调用方刚 ``lstat`` 到的那个目录;改之前核对它还是同一个目录(没被换成
    符号链接或别的东西),不经路径字符串。返回是否改了。"""
    mode = stat.S_IMODE(expected.st_mode)
    if mode & 0o700 == 0o700:
        return False
    if _O_PATH is not None:
        try:
            pfd = os.open(name, _O_PATH | os.O_NOFOLLOW, dir_fd=dfd)
        except OSError:
            return False
        try:
            now = os.fstat(pfd)
            if not stat.S_ISDIR(now.st_mode) or (now.st_dev, now.st_ino) != (
                expected.st_dev,
                expected.st_ino,
            ):
                return False
            os.chmod(f"/proc/self/fd/{pfd}", mode | 0o700)
            return True
        except OSError:
            return False
        finally:
            os.close(pfd)
    # Non-Linux (macOS test hosts): no ``O_PATH``. Re-check identity, then change the
    # entry itself without following a link (``AT_SYMLINK_NOFOLLOW``). Production
    # (Linux) always takes the ``O_PATH`` path above.
    if os.chmod not in os.supports_follow_symlinks:  # pragma: no cover - not Linux, not macOS
        return False
    try:
        now = os.lstat(name, dir_fd=dfd)
        if not stat.S_ISDIR(now.st_mode) or (now.st_dev, now.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            return False
        os.chmod(name, mode | 0o700, dir_fd=dfd, follow_symlinks=False)
    except OSError:
        return False
    return True


#: 一次删除最多跑多少轮「往上提一层」;正常的树早就删完了,到顶说明有人在不停地往里写。
_MAX_ROUNDS = 100_000


def _unlink_quiet(dfd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dfd)
    except FileNotFoundError:
        pass  # 别人(janitor / 留存 / 并发的清除)先删了 —— 正是想要的结果


def _lift_children(top: int, name: str, info: os.stat_result) -> None:
    """把 ``top`` 下子目录 ``name`` 的子项全部改名挪到 ``top`` 里,再删掉变空的 ``name``。

    挪目录要能写它自己(改 ``..``),所以挪之前先给子目录补属主位。"""
    add_owner_bits(top, name, info)
    sub = os.open(name, _DIR_FLAGS, dir_fd=top)
    try:
        for child in os.listdir(sub):
            try:
                cinfo = os.lstat(child, dir_fd=sub)
                if stat.S_ISDIR(cinfo.st_mode):
                    add_owner_bits(sub, child, cinfo)
                os.rename(child, f".lift-{uuid4().hex}", src_dir_fd=sub, dst_dir_fd=top)
            except FileNotFoundError:
                continue
    finally:
        os.close(sub)
    try:
        os.rmdir(name, dir_fd=top)
    except FileNotFoundError:
        pass  # already gone (removed concurrently): that is the goal


def _flatten_remove(dfd: int, name: str) -> None:
    """``rm -rf``,但既不递归也不按深度攒 fd:每一轮把子目录的子项改名提到顶层、删掉变空的
    子目录,直到顶层空了再删它自己。任意深 / 任意宽的树都只开着三个 fd。

    在我们删的同时被别人删掉的项一律当作已删。补不了权限(别的 uid)时抛出 ——
    :func:`remove_tree_at` 再核实一次,还在就交给隔离。"""
    info = os.lstat(name, dir_fd=dfd)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        _unlink_quiet(dfd, name)
        return
    add_owner_bits(dfd, name, info)
    top = os.open(name, _DIR_FLAGS, dir_fd=dfd)
    try:
        for _round in range(_MAX_ROUNDS):
            names = os.listdir(top)
            if not names:
                break
            for child in names:
                try:
                    cinfo = os.lstat(child, dir_fd=top)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(cinfo.st_mode) or not stat.S_ISDIR(cinfo.st_mode):
                    _unlink_quiet(top, child)
                    continue
                try:
                    os.rmdir(child, dir_fd=top)
                    continue
                except FileNotFoundError:
                    continue
                except OSError:
                    pass  # 不空(或暂时删不了):把它的子项提上来,下一轮再看
                try:
                    _lift_children(top, child, cinfo)
                except FileNotFoundError:
                    continue
        else:
            raise OSError(errno.EBUSY, "directory keeps refilling while being removed")
    finally:
        os.close(top)
    try:
        os.rmdir(name, dir_fd=dfd)
    except FileNotFoundError:
        pass  # already gone (removed concurrently): that is the goal


def _present(dfd: int, name: str) -> bool:
    try:
        os.lstat(name, dir_fd=dfd)
    except FileNotFoundError:
        return False
    return True


def remove_tree_at(dfd: int, name: str) -> bool:
    """删 ``dfd`` 下的 ``name``,然后**核实不在了**;还在(或碰上 ``EBUSY`` /
    ``ENOTEMPTY`` 这类暂时性错误)就再来一次。返回最终是否删掉了。

    任何异常(包括意料之外的)都只当作「这次没删掉」,交给调用方去隔离。"""
    for _attempt in range(2):
        try:
            _flatten_remove(dfd, name)
        except FileNotFoundError:
            pass  # vanished mid-way; the lstat check below decides success
        except Exception as exc:  # anything unexpected falls back to quarantine
            logger.info(
                "safe_remove.remove_incomplete error=%s errno=%s",
                type(exc).__name__,
                errno.errorcode.get(getattr(exc, "errno", 0) or 0, "-"),
            )
        if not _present(dfd, name):
            return True
    return False


def _open_dir_chain(root: str, parts: list[str]) -> tuple[int | None, str]:
    """从 NAS 根一段一段 ``O_NOFOLLOW`` 打开到 ``parts`` 的最后一段。

    返回 ``(fd, outcome)``:``fd`` 为 ``None`` 时 ``outcome`` 是 :data:`ABSENT` /
    :data:`SKIPPED_SYMLINK`。

    途经的每一级目录都先补属主 ``rwx``(只加不减、不经链接;已经有了就什么都不做):模型能把
    ``/workspace/agents``(或 ``threads/``、``.tool_results/``)``chmod 555``,那一级照样打得开,
    但在它下面删 / 改名都会 ``EACCES``。"""
    fd = os.open(Path(root), os.O_RDONLY | os.O_DIRECTORY)
    for part in parts:
        try:
            info = os.lstat(part, dir_fd=fd)
        except FileNotFoundError:
            os.close(fd)
            return None, ABSENT
        if stat.S_ISLNK(info.st_mode):
            os.close(fd)
            return None, SKIPPED_SYMLINK
        if not stat.S_ISDIR(info.st_mode):
            os.close(fd)
            return None, ABSENT
        add_owner_bits(fd, part, info)
        try:
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)
        fd = child
    return fd, REMOVED


def _ensure_dir(dfd: int, name: str) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=dfd)
    except FileExistsError:
        pass  # already there: reuse it (opened with O_NOFOLLOW below)
    return os.open(name, _DIR_FLAGS, dir_fd=dfd)


def _quarantine(root: str, tenant_id: UUID, owner_id: UUID, src_dfd: int, name: str) -> str:
    """把 ``src_dfd`` 下的 ``name`` 挪到 ``{tenant}/.purged-agents/{owner}/``;返回新名字。"""
    parked = f"{name}-{int(time.time())}-{uuid4().hex[:8]}"
    fd, outcome = _open_dir_chain(root, [str(tenant_id)])
    if fd is None:
        raise NasEntryStuckError(errno.ENOENT, f"tenant directory unavailable: {outcome}")
    try:
        holding = _ensure_dir(fd, PURGED_AGENTS_DIR)
        try:
            owner = _ensure_dir(holding, str(owner_id))
            try:
                os.rename(name, parked, src_dir_fd=src_dfd, dst_dir_fd=owner)
            finally:
                os.close(owner)
        finally:
            os.close(holding)
    finally:
        os.close(fd)
    return parked


def remove_entry(
    root: str,
    parts: list[str],
    name: str,
    *,
    tenant_id: UUID,
    owner_id: UUID,
    quarantine: bool = True,
) -> str:
    """``rm -rf {root}/{parts…}/{name}``,按本模块开头说的规矩。

    ``parts`` 以租户 id 开头、都是固定名字 / UUID;``name`` 是 UUID 或校验过的 ``agent_key``。
    返回结果常量之一;删不掉也挪不走(或 ``quarantine=False`` 时删不掉)抛
    :class:`NasEntryStuckError`。``quarantine=False`` 用于删隔离区本身。"""
    fd, outcome = _open_dir_chain(root, parts)
    if fd is None:
        return outcome
    try:
        try:
            info = os.lstat(name, dir_fd=fd)
        except FileNotFoundError:
            return ABSENT
        if stat.S_ISLNK(info.st_mode):
            os.unlink(name, dir_fd=fd)
            return UNLINKED
        if not stat.S_ISDIR(info.st_mode):
            os.unlink(name, dir_fd=fd)
            return REMOVED
        if remove_tree_at(fd, name):
            return REMOVED
        if not quarantine:
            raise NasEntryStuckError(errno.EACCES, "cannot remove (quarantine disabled)")
        try:
            parked = _quarantine(root, tenant_id, owner_id, fd, name)
        except OSError as exc:
            raise NasEntryStuckError(
                exc.errno or errno.EIO, f"cannot remove or quarantine: {type(exc).__name__}"
            ) from exc
        logger.warning(
            "safe_remove.quarantined tenant_id=%s owner_id=%s parked=%s",
            tenant_id,
            owner_id,
            parked,
        )
        return QUARANTINED
    finally:
        os.close(fd)


def quarantine_entry(
    root: str, parts: list[str], name: str, *, tenant_id: UUID, owner_id: UUID
) -> tuple[str, str | None]:
    """B-282 —— 把 ``{root}/{parts…}/{name}`` 整个**改名**挪进隔离区(一次 ``rename``,不删)。

    返回 ``(结果, 隔离区里的新名字)``:挪走了是 ``(QUARANTINED, 新名字)``;不在是
    ``(ABSENT, None)``;路径中途有符号链接是 ``(SKIPPED_SYMLINK, None)``。挪不动抛
    :class:`NasEntryStuckError`。之后按原路径再写的东西落进一个新目录,不会被删;挪走的那棵树
    由 :func:`sweep_quarantine` 满 7 天删掉,在那之前可以用 :func:`restore_quarantined` 挪回。"""
    fd, outcome = _open_dir_chain(root, parts)
    if fd is None:
        return outcome, None
    try:
        try:
            info = os.lstat(name, dir_fd=fd)
        except FileNotFoundError:
            return ABSENT, None
        if stat.S_ISDIR(info.st_mode):
            # 跨父目录改名要改写它自己的 ``..``:目录本身得可写(模型能 ``chmod 555`` 它)。
            add_owner_bits(fd, name, info)
        try:
            parked = _quarantine(root, tenant_id, owner_id, fd, name)
        except OSError as exc:
            raise NasEntryStuckError(
                exc.errno or errno.EIO, f"cannot quarantine: {type(exc).__name__}"
            ) from exc
        return QUARANTINED, parked
    finally:
        os.close(fd)


def restore_quarantined(
    root: str, parts: list[str], name: str, *, tenant_id: UUID, owner_id: UUID, parked: str
) -> bool:
    """把 :func:`quarantine_entry` 挪走的 ``parked`` 挪回 ``{parts…}/{name}``。原名已经又有东西了
    (比如别人按原路径新建了目录)就不挪、回 ``False``:不覆盖。"""
    fd, _outcome = _open_dir_chain(root, parts)
    if fd is None:
        return False
    try:
        try:
            os.lstat(name, dir_fd=fd)
        except FileNotFoundError:
            pass
        else:
            return False
        owner, _ = _open_dir_chain(root, [str(tenant_id), PURGED_AGENTS_DIR, str(owner_id)])
        if owner is None:
            return False
        try:
            info = os.lstat(parked, dir_fd=owner)
            if stat.S_ISDIR(info.st_mode):
                add_owner_bits(owner, parked, info)  # 同上:挪回也要改写 ``..``
            os.rename(parked, name, src_dir_fd=owner, dst_dir_fd=fd)
        finally:
            os.close(owner)
        return True
    finally:
        os.close(fd)


def _parked_at(name: str) -> int | None:
    """``{原名}-{秒}-{8 位 hex}`` 里的秒;形状不对 → ``None``(不认识的不删)。"""
    head, _, rand = name.rpartition("-")
    stem, _, ts = head.rpartition("-")
    # ``isascii``: ``str.isdigit`` also accepts e.g. superscript digits, which ``int`` rejects.
    if not stem or len(rand) != 8 or not (ts.isascii() and ts.isdigit()):
        return None
    if not all(c in "0123456789abcdef" for c in rand):
        return None
    return int(ts)


def sweep_quarantine(root: str, *, older_than: float) -> tuple[int, int]:
    """删掉 ``{root}/*/.purged-agents/*/*`` 里挪进来早于 ``older_than``(unix 秒)的条目。

    返回 ``(删掉的, 删不掉的)``。只认 UUID 名的租户 / 用户目录与本模块起的名字;任何一级是
    符号链接都不碰。"""
    removed = failed = 0
    base = Path(root)
    try:
        tenants = [e.name for e in os.scandir(base) if _is_uuid(e.name)]
    except OSError:
        logger.warning("safe_remove.sweep_scan_failed")
        return 0, 0
    for tenant in tenants:
        try:
            fd, _ = _open_dir_chain(root, [tenant, PURGED_AGENTS_DIR])
        except OSError as exc:
            logger.warning(
                "safe_remove.sweep_tenant_unavailable tenant_id=%s error=%s",
                tenant,
                type(exc).__name__,
            )
            failed += 1
            continue
        if fd is None:
            continue
        try:
            for owner in [n for n in os.listdir(fd) if _is_uuid(n)]:
                r, f = _sweep_owner(fd, tenant, owner, older_than)
                removed += r
                failed += f
        except OSError as exc:
            logger.warning(
                "safe_remove.sweep_failed tenant_id=%s error=%s", tenant, type(exc).__name__
            )
            failed += 1
        finally:
            os.close(fd)
    return removed, failed


def _sweep_owner(fd: int, tenant: str, owner: str, older_than: float) -> tuple[int, int]:
    """One ``.purged-agents/{owner}`` directory: each entry handled (and failing) on its own."""
    removed = failed = 0
    try:
        info = os.lstat(owner, dir_fd=fd)
        if not stat.S_ISDIR(info.st_mode):
            return 0, 0
        add_owner_bits(fd, owner, info)
        ofd = os.open(owner, _DIR_FLAGS, dir_fd=fd)
    except OSError as exc:
        logger.warning(
            "safe_remove.sweep_owner_unavailable tenant_id=%s owner_id=%s error=%s",
            tenant,
            owner,
            type(exc).__name__,
        )
        return 0, 1
    try:
        for entry in os.listdir(ofd):
            parked = _parked_at(entry)
            if parked is None or parked >= older_than:
                continue
            try:
                einfo = os.lstat(entry, dir_fd=ofd)
                if stat.S_ISLNK(einfo.st_mode) or not stat.S_ISDIR(einfo.st_mode):
                    os.unlink(entry, dir_fd=ofd)
                    ok = True
                else:
                    ok = remove_tree_at(ofd, entry)
            except FileNotFoundError:
                ok = True
            except OSError as exc:
                logger.info("safe_remove.sweep_entry_error error=%s", type(exc).__name__)
                ok = False
            if ok:
                removed += 1
            else:
                failed += 1
                logger.warning(
                    "safe_remove.sweep_entry_failed tenant_id=%s owner_id=%s entry=%s",
                    tenant,
                    owner,
                    entry,
                )
    except OSError as exc:
        logger.warning(
            "safe_remove.sweep_owner_failed tenant_id=%s owner_id=%s error=%s",
            tenant,
            owner,
            type(exc).__name__,
        )
        failed += 1
    finally:
        os.close(ofd)
    return removed, failed


def _is_uuid(name: str) -> bool:
    try:
        return str(UUID(name)) == name
    except ValueError:
        return False


__all__ = [
    "ABSENT",
    "QUARANTINED",
    "REMOVED",
    "SKIPPED_SYMLINK",
    "UNLINKED",
    "NasEntryStuckError",
    "add_owner_bits",
    "quarantine_entry",
    "remove_entry",
    "remove_tree_at",
    "restore_quarantined",
    "sweep_quarantine",
]
