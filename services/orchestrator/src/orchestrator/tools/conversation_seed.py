"""B-236 第二层 —— 对话开场时把 agent 根的基础目录复制进本对话的 ``/workspace``。

每个对话有自己的 ``/workspace``(``agents/<key>/threads/<conv>/work``)之后,存量 agent 的
提示词与脚本按相对路径找的长期目录(风格锚、模板、脚本……)还在 agent 根。这里在**每个
对话第一次 run 开始时**,由控制面在宿主侧把 agent 根下已有的一级目录复制进本对话的
``work/``:之后本对话按原来的相对路径读写的是自己那份,两个对话不会再经由 agent 根的同一个
目录互相覆盖;要改所有对话共用的那份,写 ``/agent/<目录>/``。

**每个对话只复制一次。** 标记文件 ``threads/<conv>/.seeded`` 最后写:有它就不再复制;复制
中途失败不写标记、清掉临时目录,下一次 run 再试(已经搬进 ``work/`` 的目录按「同名不覆盖」
跳过,不会重复)。并发的两个 run 由按对话加的锁串行(:class:`~orchestrator.tools.locks.KeyedLock`,
生产是 Postgres advisory lock),锁内再查一次标记。

**只碰目录 fd,不重走字符串路径。** 沙箱对 agent 根有写权限,上一次 exec 留下的后台进程
随时可能把某个名字换成符号链接。所以:

* 一路 ``openat(O_RDONLY|O_DIRECTORY|O_NOFOLLOW)``(``nas_workspace_store._openat_dir``)拿
  目录 fd,起点是 :class:`~orchestrator.tools.nas_workspace_store.NasWorkspaceStore` 自己的
  那道防穿越闸(``_open_parent_dir_fd`` / ``_open_scope_dir_fd``),不另写一份;
* 文件 ``O_RDONLY|O_NOFOLLOW|O_NONBLOCK`` 打开后 ``fstat`` 核对 ``S_ISREG``(``O_NONBLOCK``:
  被换成 FIFO 时不会卡住);符号链接、FIFO、设备、socket 与编码不成 UTF-8 的名字一律跳过并计数;
* 目标端在已钉住的目录 fd 上 ``mkdirat`` / ``O_CREAT|O_EXCL|O_NOFOLLOW``,保留 mtime
  (工作区快照段据此认出「原样未改的副本」)。

**上限。** 一个对话合计 :data:`MAX_SEED_BYTES`(用户拍板 20 MB);会超出的目录**整个**不复制,
在工作区快照段里写明它只在 ``/agent/<目录>/``。另有条目数与深度两道闸,只为给一次复制的系统
调用数与递归深度封顶 —— 取值见常量注释(依据测试环境 2,079 个 agent 根的实测)。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from expert_work.protocol.agent_key import require_safe_key
from orchestrator.tools.locks import KeyedLock
from orchestrator.tools.nas_workspace_store import (
    NasWorkspaceStore,
    _openat_dir,
    _WorkspacePathNotFoundError,
)
from orchestrator.tools.workspace_paths import (
    AGENT_ROOT_RESERVED,
    AGENTS_DIR,
    CONVERSATION_WORK_DIR,
    THREADS_DIR,
)

logger = logging.getLogger(__name__)

#: 一个对话开场复制的总字节上限(用户 10-30 拍板 20 MB)。测试环境实测:agent 根里可复制的
#: 目录合计 p99 0.53 MB、最大 25.5 MB —— 2,079 个根里只有 1 个超过它。
MAX_SEED_BYTES = 20 * 1024 * 1024

#: 条目(文件 + 子目录)总数上限。实测每个根最多 77 个文件(p99 22),500 留了 6 倍余量;
#: 它只防一个堆了成千上万个小文件的目录把开场拖成几万次 NAS 往返。
MAX_SEED_ENTRIES = 500

#: 目录深度上限(一级目录本身是第 1 层)。实测最深 5 层,12 留了两倍以上余量;它给递归
#: 与同时打开的目录 fd 数封顶。
MAX_SEED_DEPTH = 12

#: 本对话复制做完的标记,最后写。在 ``threads/<conv>/`` 下,不在 ``work/`` 里。
SEED_MARKER = ".seeded"

#: 复制进行中的临时目录名前缀(``threads/<conv>/.seed-tmp-<随机>/``)。
SEED_TMP_PREFIX = ".seed-tmp-"

_DIR_MODE = 0o700
_FILE_MODE = 0o600
_CHUNK = 1024 * 1024
#: 源文件:不跟符号链接;``O_NONBLOCK`` 让被换成 FIFO 的名字打开时不卡住(随后 ``fstat`` 拒)。
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class _TooLargeError(Exception):
    """这个目录放不进剩下的额度(字节 / 条目 / 深度):整个跳过。"""


@dataclass
class _Tally:
    """一个候选目录的计数,超出剩余额度就抛 :class:`_TooLargeError`。"""

    max_bytes: int
    max_entries: int
    bytes: int = 0
    entries: int = 0
    #: 跳过的条目:符号链接、FIFO / 设备 / socket、编码不成 UTF-8 的名字。
    skipped: int = 0

    def take(self, *, size: int = 0, entries: int = 1) -> None:
        self.bytes += size
        self.entries += entries
        if self.bytes > self.max_bytes or self.entries > self.max_entries:
            raise _TooLargeError


@dataclass(frozen=True)
class SeedReport:
    """一次复制的结果。目录名只给调用方用(写进状态、告诉模型),日志里只记个数。"""

    copied: tuple[str, ...] = ()
    #: 超出上限、整个没复制的目录 —— 只在 ``/agent/<目录>/``。
    skipped_too_large: tuple[str, ...] = ()
    #: 本对话 ``work/`` 里已经有同名条目、不覆盖的目录。
    skipped_existing: tuple[str, ...] = ()
    #: 跳过的条目数(符号链接、特殊文件、编码不成 UTF-8 的名字)。
    skipped_entries: int = 0
    bytes_copied: int = 0
    entries_copied: int = 0


def _max_bytes() -> int:
    return MAX_SEED_BYTES


def _max_entries() -> int:
    return MAX_SEED_ENTRIES


@dataclass
class _Budget:
    """整个对话剩下的额度(上限在建对象时读,测试可以改模块常量)。"""

    bytes: int = field(default_factory=_max_bytes)
    entries: int = field(default_factory=_max_entries)
    skipped: int = 0
    copied: list[str] = field(default_factory=list)
    too_large: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)

    def tally(self) -> _Tally:
        return _Tally(max_bytes=self.bytes, max_entries=self.entries)

    def spend(self, tally: _Tally) -> None:
        self.bytes -= tally.bytes
        self.entries -= tally.entries


def _utf8(name: str) -> bool:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _children(dir_fd: int, tally: _Tally) -> Iterator[tuple[str, os.stat_result]]:
    """``dir_fd`` 下的子目录与普通文件(按名字排序);其余一律计入 ``tally.skipped``。"""
    for name in sorted(os.listdir(dir_fd)):
        if not _utf8(name):
            tally.skipped += 1
            continue
        try:
            info = os.lstat(name, dir_fd=dir_fd)
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
            yield name, info
        else:
            tally.skipped += 1


def _open_child_dir(dir_fd: int, name: str, tally: _Tally) -> int | None:
    """``name`` 的目录 fd;刚被换成符号链接 / 别的东西就跳过(计数)。"""
    try:
        return _openat_dir(dir_fd, name, create=False)
    except OSError:
        tally.skipped += 1
        return None


def _measure(dir_fd: int, depth: int, tally: _Tally) -> None:
    """只 ``lstat`` 不读内容,先算一遍这个目录要多少额度 —— 超了就不必读它的字节。"""
    for name, info in _children(dir_fd, tally):
        if stat.S_ISREG(info.st_mode):
            tally.take(size=info.st_size)
            continue
        if depth >= MAX_SEED_DEPTH:
            raise _TooLargeError
        tally.take()
        sub = _open_child_dir(dir_fd, name, tally)
        if sub is None:
            continue
        try:
            _measure(sub, depth + 1, tally)
        finally:
            os.close(sub)


def _keep_times(fd: int, info: os.stat_result) -> None:
    os.utime(fd, ns=(info.st_atime_ns, info.st_mtime_ns))


def _copy_file(src_dir: int, dst_dir: int, name: str, tally: _Tally) -> None:
    try:
        src = os.open(name, _READ_FLAGS, dir_fd=src_dir)
    except OSError:
        tally.skipped += 1  # 刚被换成符号链接(ELOOP)/ socket(ENXIO)/ 删掉了
        return
    try:
        info = os.fstat(src)
        if not stat.S_ISREG(info.st_mode):
            tally.skipped += 1
            return
        tally.take(size=info.st_size)
        mode = _FILE_MODE | (info.st_mode & stat.S_IXUSR)
        dst = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            mode,
            dir_fd=dst_dir,
        )
        try:
            os.fchmod(dst, mode)  # umask 会吃掉 open 的 mode 位
            remaining = info.st_size  # 只复制打开时的大小:一个一直在追加的文件不会让这里停不下来
            while remaining > 0:
                chunk = os.read(src, min(_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[os.write(dst, view) :]
            _keep_times(dst, info)
        finally:
            os.close(dst)
    finally:
        os.close(src)


def _copy_dir(src_dir: int, dst_dir: int, depth: int, tally: _Tally) -> None:
    for name, info in _children(src_dir, tally):
        if stat.S_ISREG(info.st_mode):
            _copy_file(src_dir, dst_dir, name, tally)
            continue
        if depth >= MAX_SEED_DEPTH:
            raise _TooLargeError
        sub = _open_child_dir(src_dir, name, tally)
        if sub is None:
            continue
        try:
            tally.take()
            out = _openat_dir(dst_dir, name, create=True)
            try:
                _copy_dir(sub, out, depth + 1, tally)
                _keep_times(out, os.fstat(sub))
            finally:
                os.close(out)
        finally:
            os.close(sub)


def _exists(dir_fd: int, name: str) -> bool:
    try:
        os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return False
    return True


def _candidates(agent_fd: int, budget: _Budget) -> list[str]:
    """agent 根下可复制的一级目录:不是点开头、不是布局 / 平台保留名、是真目录。"""
    out: list[str] = []
    for name in sorted(os.listdir(agent_fd)):
        if name.startswith(".") or name in AGENT_ROOT_RESERVED:
            continue
        if not _utf8(name):
            budget.skipped += 1
            continue
        try:
            info = os.lstat(name, dir_fd=agent_fd)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            budget.skipped += 1
        elif stat.S_ISDIR(info.st_mode):
            out.append(name)
    return out


def _seed_one(agent_fd: int, tmp_fd: int, work_fd: int, name: str, budget: _Budget) -> None:
    """量 → 复制进临时目录 → 改名进 ``work/``(同名已存在就不动)。"""
    src = _open_child_dir(agent_fd, name, budget.tally())
    if src is None:
        budget.skipped += 1
        return
    try:
        measured = budget.tally()
        try:
            measured.take()
            _measure(src, 1, measured)
        except _TooLargeError:
            budget.too_large.append(name)
            return
        copied = budget.tally()
        try:
            copied.take()
            out = _openat_dir(tmp_fd, name, create=True)
            try:
                _copy_dir(src, out, 1, copied)
                _keep_times(out, os.fstat(src))
            finally:
                os.close(out)
        except _TooLargeError:  # 量完之后又变大了
            shutil.rmtree(name, dir_fd=tmp_fd)
            budget.too_large.append(name)
            return
        finally:
            budget.skipped += copied.skipped
    finally:
        os.close(src)
    if _exists(work_fd, name):
        budget.existing.append(name)
        return
    try:
        os.rename(name, name, src_dir_fd=tmp_fd, dst_dir_fd=work_fd)
    except OSError:
        # 判完「不存在」之后别人建了同名条目(非空目录 / 文件):不覆盖,算已存在。
        budget.existing.append(name)
        return
    budget.spend(copied)
    budget.copied.append(name)


def _conversation_dir(
    store: NasWorkspaceStore,
    tenant_id: UUID,
    user_id: UUID,
    agent_key: str,
    conv_id: UUID,
    *,
    create: bool,
) -> int:
    """``agents/<key>/threads/<conv>/`` 的目录 fd —— 经 store 自己的那道防穿越闸。"""
    rel = f"{AGENTS_DIR}/{agent_key}/{THREADS_DIR}/{conv_id}/{SEED_MARKER}"
    dfd, _name = store._open_parent_dir_fd(tenant_id, user_id, rel, create=create)
    return dfd


def seeded(
    store: NasWorkspaceStore, tenant_id: UUID, user_id: UUID, agent_key: str, conv_id: UUID
) -> bool:
    """本对话是不是已经复制过(不建任何目录)。"""
    try:
        conv_fd = _conversation_dir(store, tenant_id, user_id, agent_key, conv_id, create=False)
    except _WorkspacePathNotFoundError:
        return False
    try:
        return _exists(conv_fd, SEED_MARKER)
    finally:
        os.close(conv_fd)


def _write_marker(conv_fd: int, report: SeedReport) -> None:
    body = json.dumps(
        {
            "version": 1,
            "copied": list(report.copied),
            "skipped_too_large": list(report.skipped_too_large),
            "skipped_existing": list(report.skipped_existing),
            "skipped_entries": report.skipped_entries,
            "bytes_copied": report.bytes_copied,
        },
        ensure_ascii=False,
    ).encode()
    try:
        fd = os.open(
            SEED_MARKER,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            _FILE_MODE,
            dir_fd=conv_fd,
        )
    except FileExistsError:
        return  # 别的副本刚写完(锁失效 / 本地无锁时的并发):它的结果同样成立
    try:
        os.write(fd, body)
    finally:
        os.close(fd)


def seed_conversation_sync(
    store: NasWorkspaceStore, tenant_id: UUID, user_id: UUID, agent_key: str, conv_id: UUID
) -> SeedReport | None:
    """复制本对话的基础目录;已经复制过返回 ``None``。失败抛,不写标记、不留临时目录。"""
    conv_fd = _conversation_dir(store, tenant_id, user_id, agent_key, conv_id, create=True)
    try:
        if _exists(conv_fd, SEED_MARKER):
            return None
        agent_fd = store._open_scope_dir_fd(tenant_id, user_id, f"{AGENTS_DIR}/{agent_key}")
        try:
            budget = _Budget()
            names = _candidates(agent_fd, budget)
            work_fd = _openat_dir(conv_fd, CONVERSATION_WORK_DIR, create=True)
            try:
                tmp = f"{SEED_TMP_PREFIX}{uuid4().hex}"
                tmp_fd = _openat_dir(conv_fd, tmp, create=True)
                try:
                    for name in names:
                        if _exists(work_fd, name):
                            budget.existing.append(name)
                            continue
                        _seed_one(agent_fd, tmp_fd, work_fd, name, budget)
                finally:
                    os.close(tmp_fd)
                    with contextlib.suppress(FileNotFoundError):
                        shutil.rmtree(tmp, dir_fd=conv_fd)
            finally:
                os.close(work_fd)
        finally:
            os.close(agent_fd)
        report = SeedReport(
            copied=tuple(budget.copied),
            skipped_too_large=tuple(budget.too_large),
            skipped_existing=tuple(budget.existing),
            skipped_entries=budget.skipped,
            bytes_copied=_max_bytes() - budget.bytes,
            entries_copied=_max_entries() - budget.entries,
        )
        _write_marker(conv_fd, report)
        return report
    finally:
        os.close(conv_fd)


async def seed_conversation(
    store: NasWorkspaceStore,
    *,
    tenant_id: UUID,
    user_id: UUID,
    agent_key: str,
    conv_id: UUID,
    lock: KeyedLock,
) -> SeedReport | None:
    """对话开场的复制;已经复制过返回 ``None``(先不加锁看一眼标记,锁内再看一次)。"""
    require_safe_key(agent_key)
    if await asyncio.to_thread(seeded, store, tenant_id, user_id, agent_key, conv_id):
        return None
    async with lock.acquire(key=f"{tenant_id}:{conv_id}"):
        return await asyncio.to_thread(
            seed_conversation_sync, store, tenant_id, user_id, agent_key, conv_id
        )
