"""B-284 —— ``/agent`` 文件历史版本的到期(以前只在对话视图的 run 结束时做,闲着的 agent 从不到期)。

版本放在 ``{root}/{tenant}/.agent-file-versions/{user}/{agent_key}/{sha256(路径)[:32]}/``
(沙箱挂不到):``path`` 记源文件的 agent 根相对路径,``missing`` 是「源文件确认不见」的标记
(内容 = 第一次确认不见的时间,mtime = 最近一次确认的时间 —— 与
``orchestrator.tools.agent_file_versions`` 同一个格式,run 结束时的观察照旧记 / 撤这个标记),
版本文件 ``v<序号>-<sha256>``。每晚:

* **源文件不见满 30 天 → 整个版本目录删掉。** 留存作业按 ``path`` 逐段 ``lstat`` 自己确认:任何一段
  不存在 = 不见(记 / 刷新标记);叶子是普通文件 = 在(撤标记);中途有符号链接、叶子不是普通文件、
  读不了 = 不知道(什么都不动,不会按它到期)。标记离上一次确认超过 7 天(中间没人看过,文件可能
  回来过)从这次重新算。
* **每个文件最新 3 份之外、写下满 90 天的版本删掉**(按 ctime:mtime 是抄的源文件的)。留 3 份:
  与索引 ``AGENT_NOTES.md`` 受保护的份数一致,比普通文件受保护的 2 份多一份 —— 一次坏的覆盖之后
  再被观察一次,手里还有一份好的。

用户已软删(``{tenant}/.deleted/{user}``)整个跳过(清除会把版本连根删掉)。每个版本目录单独
``try``,失败计数、这条 pass 记失败,不影响别的目录。目录一律 ``O_NOFOLLOW`` 逐段打开。
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import logging
import math
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from expert_work.persistence import AGENT_FILE_VERSIONS_DIR, WORKSPACE_AGENTS_DIR
from expert_work.persistence.workspace.safe_remove import (
    QUARANTINED,
    REMOVED,
    UNLINKED,
    remove_entry,
)
from retention_cleanup_job.workspace_files import DELETED_DIR

logger = logging.getLogger(__name__)

#: 源文件确认不见满这么久,版本目录整个删掉(同 orchestrator 的 ``MISSING_SOURCE_GRACE_S``)。
MISSING_SOURCE_GRACE_S = 30 * 24 * 3600
#: 两次确认不见之间隔了这么久,从这一次重新算(同 orchestrator 的 ``MISSING_CONFIRM_GAP_S``)。
MISSING_CONFIRM_GAP_S = 7 * 24 * 3600
#: 写下满这么久的版本(最新几份除外)删掉。
VERSION_MAX_AGE_S = 90 * 24 * 3600
#: 写到一半留下的 ``.tmp-*`` 满这么久删掉(同 orchestrator 的 ``ORPHAN_DIR_GRACE_S``)。
TMP_GRACE_S = 3600
#: 每个文件无论多老都留的最新几份。
KEEP_NEWEST = 3
#: 早于 2020-01-01 的「第一次确认不见」不可能是真的:当作没有标记。
_EARLIEST_PLAUSIBLE_MARKER = 1_577_836_800.0

_PATH_FILE = "path"
_MISSING_FILE = "missing"
_VERSION_RE = re.compile(r"^v(\d{8})-([0-9a-f]{64})$")
_FILE_DIR_RE = re.compile(r"^[0-9a-f]{32}$")
_AGENT_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


@dataclass
class VersionSweepTally:
    #: 源文件不见满 30 天、整个删掉的版本目录。
    dirs_dropped: int = 0
    #: 最新 3 份之外、满 90 天删掉的版本文件。
    versions_capped: int = 0
    #: 处理出错的版本目录(下一轮再试)。
    failed: int = 0

    @property
    def needs_attention(self) -> bool:
        return self.failed > 0


def _key_of(rel: str) -> str:
    """版本目录名 = 源路径 sha256 的前 32 位(同 orchestrator 的 ``_key_of``)。"""
    return hashlib.sha256(rel.encode()).hexdigest()[:32]


def _children(fd: int, pattern: re.Pattern[str]) -> list[str]:
    return sorted(n for n in os.listdir(fd) if pattern.fullmatch(n))


def _open_dir(parent: int, name: str) -> int:
    return os.open(name, _DIR_FLAGS, dir_fd=parent)


def _read_small(dir_fd: int, name: str) -> tuple[bytes, os.stat_result] | None:
    """``name`` 是普通文件就读前 4 KiB(与它的 ``fstat``);不存在 → ``None``。"""
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        return os.read(fd, 4096), info
    finally:
        os.close(fd)


def _source_status(root: str, tenant: str, user: str, key: str, rel: str) -> str:
    """``live`` / ``missing`` / ``unknown`` —— 按 ``path`` 从 NAS 根逐段 ``lstat``(不跟链接)。"""
    parts = PurePosixPath(rel).parts
    if not parts or rel.startswith("/") or ".." in parts or "." in parts:
        return "unknown"
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return "unknown"
    try:
        for name in (tenant, user, WORKSPACE_AGENTS_DIR, key, *parts[:-1]):
            try:
                info = os.lstat(name, dir_fd=fd)
            except FileNotFoundError:
                return "missing"
            if stat.S_ISLNK(info.st_mode):
                return "unknown"
            if not stat.S_ISDIR(info.st_mode):
                return "missing"  # 该是目录的地方是个文件:这条路径不存在
            nfd = _open_dir(fd, name)
            os.close(fd)
            fd = nfd
        try:
            leaf = os.lstat(parts[-1], dir_fd=fd)
        except FileNotFoundError:
            return "missing"
        return "live" if stat.S_ISREG(leaf.st_mode) else "unknown"
    except OSError:
        return "unknown"
    finally:
        os.close(fd)


def _atomic_write(dir_fd: int, name: str, data: bytes) -> None:
    tmp = f".tmp-{uuid4().hex}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def _note_missing(file_dir: int, now: float) -> bool:
    """记 / 刷新标记;从第一次确认不见满 30 天回 ``True``。规则同 orchestrator 的
    ``_note_missing``。"""
    found = _read_small(file_dir, _MISSING_FILE)
    if found is not None:
        raw, info = found
        try:
            first = float(raw.decode("ascii").strip())
        except (UnicodeDecodeError, ValueError):
            first = math.nan
        if (
            math.isfinite(first)
            and _EARLIEST_PLAUSIBLE_MARKER <= first <= now
            and now - info.st_mtime <= MISSING_CONFIRM_GAP_S
        ):
            os.utime(_MISSING_FILE, (now, now), dir_fd=file_dir, follow_symlinks=False)
            return now - first >= MISSING_SOURCE_GRACE_S
    _atomic_write(file_dir, _MISSING_FILE, f"{now:.0f}".encode())
    return False


def _cap(file_dir: int, names: list[str], now: float) -> int:
    """最新 :data:`KEEP_NEWEST` 份之外、ctime 早于 90 天前的版本删掉;返回删了几份。"""
    versions = sorted((int(m.group(1)), n) for n in names if (m := _VERSION_RE.fullmatch(n)))
    removed = 0
    for _seq, name in versions[: max(len(versions) - KEEP_NEWEST, 0)]:
        try:
            if now - os.lstat(name, dir_fd=file_dir).st_ctime >= VERSION_MAX_AGE_S:
                os.unlink(name, dir_fd=file_dir)
                removed += 1
        except FileNotFoundError:
            continue
    return removed


def _sweep_tmp(file_dir: int, names: list[str], now: float) -> None:
    """写版本 / ``path`` / 标记写到一半被杀留下的 ``.tmp-*``:按 ctime 满 1 小时删(同 orchestrator 的
    ``_sweep_tmp``;mtime 是抄的源文件的,不可信)。"""
    for name in names:
        if not name.startswith(".tmp-"):
            continue
        try:
            if now - os.lstat(name, dir_fd=file_dir).st_ctime >= TMP_GRACE_S:
                os.unlink(name, dir_fd=file_dir)
        except FileNotFoundError:
            continue


def _one_file_dir(
    root: str, tenant: str, user: str, key: str, agent_fd: int, name: str, now: float
) -> tuple[bool, int]:
    """一个版本目录:``(整个删掉了吗, 删了几份老版本)``。"""
    drop = False
    file_dir = _open_dir(agent_fd, name)
    try:
        names = os.listdir(file_dir)
        found = _read_small(file_dir, _PATH_FILE)
        rel = found[0].decode("utf-8", errors="replace") if found is not None else ""
        # 原样用(不 ``strip``:文件名首尾可以是空格);和目录名对不上(写了一半、坏了)就当「不知道」,
        # 与 orchestrator 的 ``_valid_path`` 同一条规矩 —— 拿一条错的路径去查只会把活文件判成不见。
        valid = bool(rel) and _key_of(rel) == name
        status = _source_status(root, tenant, user, key, rel) if valid else "unknown"
        if status == "live":
            with contextlib.suppress(FileNotFoundError):
                os.unlink(_MISSING_FILE, dir_fd=file_dir)
        elif status == "missing":
            drop = _note_missing(file_dir, now)
        capped = 0 if drop else _cap(file_dir, names, now)
        _sweep_tmp(file_dir, names, now)
    finally:
        os.close(file_dir)
    if drop:
        outcome = remove_entry(
            root,
            [tenant, AGENT_FILE_VERSIONS_DIR, user, key],
            name,
            tenant_id=UUID(tenant),
            owner_id=UUID(user),
        )
        return outcome in (REMOVED, UNLINKED, QUARANTINED), capped
    return False, capped


def sweep_agent_file_versions(root: str, *, now: float | None = None) -> VersionSweepTally:
    """同步(调用方 ``asyncio.to_thread``):扫全部租户的版本区。"""
    now = time.time() if now is None else now
    tally = VersionSweepTally()
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for tenant in _children(root_fd, _UUID_RE):
            try:
                tenant_fd = _open_dir(root_fd, tenant)
            except OSError:
                continue
            try:
                _sweep_tenant(root, tenant, tenant_fd, now, tally)
            finally:
                os.close(tenant_fd)
    finally:
        os.close(root_fd)
    return tally


def _sweep_tenant(
    root: str, tenant: str, tenant_fd: int, now: float, tally: VersionSweepTally
) -> None:
    try:
        versions_fd = _open_dir(tenant_fd, AGENT_FILE_VERSIONS_DIR)
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            logger.warning("retention.agent_file_versions_root_unsafe tenant=%s", tenant)
        tally.failed += 1
        return
    try:
        for user in _children(versions_fd, _UUID_RE):
            if os.path.lexists(os.path.join(root, tenant, DELETED_DIR, user)):
                continue  # 清除用户会把版本连根删掉
            try:
                user_fd = _open_dir(versions_fd, user)
            except OSError:
                tally.failed += 1
                continue
            try:
                for key in _children(user_fd, _AGENT_KEY_RE):
                    if key in (".", ".."):
                        continue
                    _sweep_agent(root, tenant, user, key, user_fd, now, tally)
            finally:
                os.close(user_fd)
    finally:
        os.close(versions_fd)


def _sweep_agent(
    root: str, tenant: str, user: str, key: str, user_fd: int, now: float, tally: VersionSweepTally
) -> None:
    try:
        agent_fd = _open_dir(user_fd, key)
    except OSError:
        tally.failed += 1
        return
    try:
        for name in _children(agent_fd, _FILE_DIR_RE):
            try:
                dropped, capped = _one_file_dir(root, tenant, user, key, agent_fd, name, now)
            except Exception as exc:
                tally.failed += 1
                logger.warning(
                    "retention.agent_file_version_dir_failed tenant=%s user=%s error=%s",
                    tenant,
                    user,
                    type(exc).__name__,
                )
                continue
            tally.dirs_dropped += int(dropped)
            tally.versions_capped += capped
    finally:
        os.close(agent_fd)


__all__ = [
    "KEEP_NEWEST",
    "MISSING_SOURCE_GRACE_S",
    "VERSION_MAX_AGE_S",
    "VersionSweepTally",
    "sweep_agent_file_versions",
]
