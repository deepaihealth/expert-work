"""B-236 第三层 —— ``/agent`` 里文件的历史版本:每次运行结束看一眼,改过的留一份。

agent 自己的记忆(偏好、模板、脚本、索引 ``AGENT_NOTES.md``)住在 ``/agent``,模型用文件工具
或 bash 改它。改错了、被一次坏的 run 覆盖了,要能找回上一版。这里不拦每一次工具调用(bash
改文件根本不经过工具层),而是**按观察**:主 run 结束时扫一遍 ``/agent``,和上一次留的版本比,
变了就复制一份。

* **扫什么。** agent 根下一级的非保留文件与目录(含索引),不进 ``threads`` / ``uploads`` /
  ``inputs`` / ``.tool_results`` / ``skills`` / ``shared`` 与点开头的名字;深度、条目数与读的
  总字节都有上限(沿用开场复制的那组数),单个文件超过 :data:`MAX_VERSIONED_FILE_BYTES` 不留。
* **怎么判变没变。** 先比大小 + mtime(副本的 mtime 设成源文件的),一样就不读;不一样再算
  sha256,内容相同只把副本的 mtime 对齐。
* **留在哪。** ``{root}/{tenant}/.agent-file-versions/{user}/{agent_key}/{sha256(路径)[:32]}/``:
  用户根的旁边,沙箱挂不到(同产物版本副本的位置理由)。每个文件一个目录,``path`` 记它的
  路径,版本文件名 ``v<序号>-<sha256>``;每个文件只留最近 :data:`MAX_VERSIONS_PER_FILE` 份,
  且每个 agent 的全部版本合计不超过它自己的 :data:`MAX_AGENT_VERSION_BYTES`(review 10:只删
  这个 agent 自己的版本,各 agent 之间按构造隔离)。受保护的最新几份只在自己到期时删;受保护的
  部分本身超了预算(跨副本同时观察、恢复了大文件)就一直超着(``still_over``),不删它们。
* **失败不连累 run。** 整个观察在 run 结束之后 fire-and-forget,任何异常只记日志(异常类型)
  和计数。清除用户时整个用户目录删掉(``purge_user`` / 留存作业)。

所有路径都按目录 fd 走:源头经 :class:`NasWorkspaceStore` 那道防穿越闸打开 agent 根,子目录
``O_NOFOLLOW``,文件 ``O_NOFOLLOW|O_NONBLOCK`` 后核对 ``S_ISREG``;符号链接与特殊文件跳过。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import shutil
import stat
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from expert_work.common.observability import expert_work_counter
from expert_work.persistence import AGENT_FILE_VERSIONS_DIR
from expert_work.protocol.agent_key import require_safe_key
from orchestrator.tools.conversation_seed import MAX_SEED_DEPTH, MAX_SEED_ENTRIES, MAX_SEED_VISITS
from orchestrator.tools.locks import KeyedLock, ProcessKeyedLock
from orchestrator.tools.nas_workspace_store import (
    MISSING_DIR_ERRORS,
    NasWorkspaceStore,
    _openat_dir,
    workspace_deleted_marker,
)
from orchestrator.tools.sandbox import WorkspacePathEscapeError
from orchestrator.tools.workspace_paths import AGENT_NOTES_FILE, AGENT_ROOT_RESERVED, AGENTS_DIR

logger = logging.getLogger(__name__)

#: 每个文件留最近几份(用户 10-30 拍板 20)。
MAX_VERSIONS_PER_FILE = 20

#: 单个文件超过这么大不留版本。测试环境实测 agent 根里最大的单个文件 4.0 MB;8 MiB 留余量,
#: 又不让一个视频把版本区撑爆。
MAX_VERSIONED_FILE_BYTES = 8 * 1024 * 1024

#: 一次观察最多读这么多字节(只读大小 / mtime 变了的文件)。读不完的留到下一次 run 结束。
MAX_OBSERVE_READ_BYTES = 20 * 1024 * 1024

#: review 10 —— 每个(用户, agent)自己的版本字节预算,**各文件最新那份也算在内**。删只删这个
#: agent 自己的版本,别的 agent 再大也碰不到这里(取代 review 7~9 的用户合计 200 MiB 跨 agent
#: 删除:「谁来付」连着三轮审查都有洞,改成按构造隔离)。100 MiB 是用户 10-30 拍板的数;测试环境
#: 实测最大的 agent 根 25.5 MB,放得下它当前全部文件一份再加约三倍的历史。一个用户的合计因此不超过
#: 「agent 数乘 100 MiB」,且照旧计入这个用户的工作区配额(``WorkspaceQuotaService.refresh``)。
MAX_AGENT_VERSION_BYTES = 100 * 1024 * 1024

#: review 8 —— 每个 agent 最多这么多个版本目录(每个被留过版本的路径一个)。字节预算管不住
#: 「换着名字写空文件」:每次观察最多 500 个新路径,不封顶的话目录与 inode 无限涨。实测每个
#: agent 根最多 77 个文件,2000 留足余量。review 12 起同字节预算一个原则:满了就不建新的版本目录
#: (那一份不留,记 ``over_budget``),从不为腾地方删已有的目录 —— 删目录会删掉受保护的版本
#: (空文件能把目录数顶满,字节预算挡不住,索引的历史就这样被整个删掉过)。
MAX_VERSION_DIRS_PER_AGENT = 2000

#: review 10/14 —— 一份版本都没有的版本目录(孤儿),建了满这么久还这样就删掉(正常写入是建目录、
#: 写 ``path``、写版本一气呵成;留一小时是不跟另一次同时进行的观察抢)。有版本、只是 ``path`` 坏了的
#: 不是孤儿。review 15 —— 版本目录里写到一半被杀留下的 ``.tmp-*`` 也按这个时长清掉。
ORPHAN_DIR_GRACE_S = 3600

#: review 11/13 —— 受保护、预算吃紧也不删的最新几份:活的(或这次没法确认的)文件 2 份,源文件
#: 已不见(30 天宽限期内)的 1 份,索引 ``AGENT_NOTES.md`` 不论在不在都是 3 份。预算之内每个文件仍
#: 最多留 :data:`MAX_VERSIONS_PER_FILE` 份。受保护的版本只在自己到期时删(已不见满 30 天、工作区
#: 删文件接口、清除用户),别的文件做什么都删不到它;装不下新的就不留新的。
MIN_KEPT_LIVE_VERSIONS = 2
MIN_KEPT_MISSING_VERSIONS = 1
MIN_KEPT_INDEX_VERSIONS = 3

#: review 13/14 —— 一次观察遍历最多看多少个名字(所有目录合计,含跳过的点开头 / 保留 / 非 UTF-8
#: 名字)。到顶就不再读新名字;已经读到的照常处理,到顶时正在读的目录和之后才打开的目录算「没列全」
#: (里面没看到的文件「不知道」)。与开场复制同一个数。
MAX_OBSERVE_VISITS = MAX_SEED_VISITS

#: review 7 —— 源文件从 ``/agent`` 消失满这么久,它的版本目录整个删掉。「从哪天起不见」记在
#: 版本目录里的 ``missing`` 标记上(第一次发现不见时建,文件回来就删)。
MISSING_SOURCE_GRACE_S = 30 * 24 * 3600

_PATH_FILE = "path"
_MISSING_FILE = "missing"
_VERSION_RE = re.compile(r"^v(\d{8})-([0-9a-f]{64})$")
_FILE_MODE = 0o600
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

#: review 11/12 —— 记了 ``over_budget`` 的观察,按租户计(用户 id 只进日志:按用户打标签基数无
#: 上限)。两种原因:有新版本因字节预算或版本目录数上限没留;或者删到底仍超预算(并发观察、
#: 恢复了一个大文件之类,受保护的版本不删)。
agent_file_versions_over_budget_total = expert_work_counter(
    "expert_work_agent_file_versions_over_budget_total",
    "B-236 run-end observations of /agent files that hit the agent's version limits, by "
    "tenant: a new version was skipped (byte budget or version-dir cap), or the store "
    "stayed over budget after pruning (concurrent observations or a restored file; "
    "protected versions are never pruned).",
    ("tenant_id",),
)

agent_file_versions_total = expert_work_counter(
    "expert_work_agent_file_versions_total",
    "B-236 run-end observation of /agent files: snapshots written, observations that hit "
    "the agent's version limits (a new version skipped, or still over budget after "
    "pruning), observations whose walk left some directory not fully listed, and "
    "observations that failed (logged, never fail the run).",
    ("outcome",),
)


@dataclass(frozen=True)
class ObserveReport:
    """一次观察的结果(只有个数,日志里不出现文件名)。"""

    snapshotted: int = 0
    unchanged: int = 0
    skipped: int = 0
    pruned: int = 0
    #: 整个删掉的版本目录数:源文件确认不见满 30 天的,和孤儿(一份版本都没有、建了满 1 小时)。
    dropped: int = 0
    #: review 11~13 —— 碰到了这个 agent 的上限:有新版本没留(字节预算 ``bytes`` / 版本目录数
    #: ``dir_cap``),或删完所有能删的仍超预算(``still_over``:受保护的版本不删)。
    over_budget: bool = False
    #: 上面三种原因里出现了哪些(排好序)。
    over_budget_reasons: tuple[str, ...] = ()
    #: review 14 —— 这次遍历有目录没列全(名字数 / 条目数到顶、打不开、太深、读的时候名字没了):
    #: 那些目录里没看到的文件这次「不知道」在不在。
    walk_incomplete: bool = False


@runtime_checkable
class AgentFileVersionStore(Protocol):
    """``/agent`` 文件版本:观察一次 / 删一个用户的全部。"""

    async def observe(self, *, tenant_id: UUID, user_id: UUID, agent_key: str) -> ObserveReport:
        """扫一遍该 agent 的 ``/agent``,改过的文件各留一份。"""

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        """删一个用户的全部版本(清除用户数据);返回删掉的版本文件数。"""

    async def delete_for_path(self, *, tenant_id: UUID, user_id: UUID, path: str) -> int:
        """工作区删文件接口删掉 ``path``(用户根相对)时,连它的版本一起删;返回版本文件数。"""


@dataclass
class _Scan:
    entries: int = 0
    #: review 13 —— 看过的名字总数(含跳过的),封顶 :data:`MAX_OBSERVE_VISITS`。
    visits: int = 0
    #: 条目数到顶,遍历停了(之后的名字一个都不处理)。
    stopped: bool = False
    #: review 14 —— 名字数到顶:不再读新的名字,已经读到的照常处理。
    visits_exhausted: bool = False
    #: review 14 —— 有目录没列全。
    incomplete: bool = False
    read_bytes: int = 0
    snapshotted: int = 0
    unchanged: int = 0
    skipped: int = 0
    pruned: int = 0
    dropped: int = 0
    #: 这次看到的普通文件(agent 根相对)。
    seen: set[str] = field(default_factory=set)
    #: review 13 —— 这次**列全了**的目录(agent 根相对,根是 ``""``):里面每个名字都看过、处理过。
    listed: set[str] = field(default_factory=set)
    #: 这次在某个目录里看到的子目录(不管列没列全)。
    present_dirs: set[str] = field(default_factory=set)
    #: review 13 —— 索引在不在,单独查的(不受条目上限影响);``None`` = 没查。
    index_present: bool | None = None
    #: review 15 —— 单独查索引时出了「不存在」以外的错(根目录可读不可进 ``0644`` 之类):索引这次
    #: 「不知道」。不能只留 ``index_present=None`` —— 那样根目录里只有索引和点开头的名字时,根目录
    #: 算「列全了」,一个好好在着的索引会被判不见。
    index_unknown: bool = False
    over_reasons: set[str] = field(default_factory=set)

    def status(self, rel: str) -> str:
        """``live`` / ``missing`` / ``unknown`` —— 源文件这次在不在(review 13,按目录判)。

        看到了就是在。没看到:一路往下,每一层都列全了、下一段却不在那一层里(或那一层列全了却
        没有这个文件),才算不见;中间哪一层没列全(打不开、读不了、太深、上限到顶)就是不知道 ——
        不知道的按在算(全额保护),并撤掉它的 ``missing`` 标记(review 14,见 :func:`_sweep`)。索引
        单独查过,查的结果说了算(查出错就是不知道)。
        """
        if rel in self.seen:
            return "live"
        if rel == AGENT_NOTES_FILE and self.index_unknown:
            return "unknown"
        if rel == AGENT_NOTES_FILE and self.index_present is not None:
            return "live" if self.index_present else "missing"
        parts = rel.split("/")
        for i in range(len(parts)):
            if "/".join(parts[:i]) not in self.listed:
                return "unknown"
            if i == len(parts) - 1:
                return "missing"
            if "/".join(parts[: i + 1]) not in self.present_dirs:
                return "missing"  # 那一层列全了,这个子目录不在
        return "unknown"  # pragma: no cover - 循环里一定返回


@dataclass(frozen=True)
class _Pending:
    """读好了、还没写的一份新版本(review 10:先全读完,再按预算决定写不写)。"""

    rel: str
    key: str
    data: bytes
    info: os.stat_result
    digest: str
    seq: int


@dataclass
class _DirUsage:
    """一个版本目录:源路径、源文件这次在不在(``live`` / ``missing`` / ``unknown``)、各版本
    ``(ctime, 文件名, 大小)`` 按序号升序。"""

    rel: str | None
    status: str
    versions: list[tuple[int, str, int]]
    #: review 14 —— 版本目录名(``sha256(路径)[:32]``)就是索引的那个:``path`` 坏了也认得出索引。
    is_index: bool = False

    @property
    def keep(self) -> int:
        """受保护的最新几份(见 :data:`MIN_KEPT_LIVE_VERSIONS`)。"""
        if self.is_index or self.rel == AGENT_NOTES_FILE:
            return MIN_KEPT_INDEX_VERSIONS
        if self.status == "missing":
            return MIN_KEPT_MISSING_VERSIONS
        return MIN_KEPT_LIVE_VERSIONS

    def floor(self) -> int:
        """受保护那几份合计多大。"""
        return sum(size for _c, _n, size in self.versions[-self.keep :]) if self.keep > 0 else 0


def _utf8(name: str) -> bool:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _files(dir_fd: int, prefix: str, depth: int, scan: _Scan) -> Iterator[tuple[str, int]]:
    """``(agent 根相对路径, mtime 纳秒)``,一路 ``O_NOFOLLOW``;顶层跳过保留名与点开头的名字。

    review 13/14 —— 用 ``os.scandir`` 边读边数,每个名字(含跳过的)都算进
    :data:`MAX_OBSERVE_VISITS`,到顶就不再读新名字,只给读到的这一批排序;读到的照常处理,只有
    到顶时正在读的那个目录算没列全。一个目录的名字全读到、也都处理过,才记进 ``scan.listed``。
    索引在调用方单独查过,这里跳过它。
    """
    names: list[str] = []
    complete = True
    if scan.visits_exhausted:
        complete = False
    else:
        try:
            with os.scandir(dir_fd) as it:
                for entry in it:
                    scan.visits += 1
                    if scan.visits > MAX_OBSERVE_VISITS:
                        scan.visits_exhausted = True
                        complete = False
                        break
                    names.append(entry.name)
        except OSError:
            complete = False  # review 15 —— 读目录出错:读到的照常处理,这一层没列全

    for name in sorted(names):
        if scan.stopped:
            complete = False
            break
        if depth == 0 and (
            name.startswith(".") or name in AGENT_ROOT_RESERVED or name == AGENT_NOTES_FILE
        ):
            continue
        if not _utf8(name):
            scan.skipped += 1
            continue
        scan.entries += 1
        if scan.entries > MAX_SEED_ENTRIES:
            scan.skipped += 1
            scan.stopped = True
            complete = False
            break
        try:
            info = os.lstat(name, dir_fd=dir_fd)
        except OSError:
            # review 14/15 —— 读目录时还在、``lstat`` 时没了(正被改名),或 ``lstat`` 不让(目录可读
            # 不可进 ``0644``:沙箱与控制面同一个 uid,它能这么设):这一层算没列全,不据此判下面
            # 任何文件「不见」,整个观察也不因此失败。
            complete = False
            continue
        rel = f"{prefix}{name}"
        if stat.S_ISREG(info.st_mode):
            scan.seen.add(rel)
            yield rel, info.st_mtime_ns
        elif stat.S_ISDIR(info.st_mode):
            scan.present_dirs.add(rel)
            if depth + 1 >= MAX_SEED_DEPTH:
                scan.skipped += 1  # 深度到顶:下面没看,里面的文件「不知道」
                scan.incomplete = True
                continue
            try:
                sub = _openat_dir(dir_fd, name, create=False)
            except OSError:
                # review 8 —— 打不开(``chmod 000`` / 刚被换成链接)的目录:里面的文件「不知道」。
                scan.skipped += 1
                scan.incomplete = True
                continue
            try:
                yield from _files(sub, f"{rel}/", depth + 1, scan)
            finally:
                os.close(sub)
        else:
            scan.skipped += 1
    if complete:
        scan.listed.add(prefix.rstrip("/"))
    else:
        scan.incomplete = True


def _check_index(agent_fd: int, scan: _Scan) -> Iterator[tuple[str, int]]:
    """review 13 —— 索引在遍历之前单独查(``lstat`` + 普通文件),不受条目 / 名字上限影响:根目录
    塞再多名字,也不能让索引「不知道」或「不见」。之后照常经 ``O_NOFOLLOW`` 打开、再核对普通文件。"""
    try:
        info = os.lstat(AGENT_NOTES_FILE, dir_fd=agent_fd)
    except FileNotFoundError:
        scan.index_present = False
        return
    except OSError:
        # review 15 —— 查不了(根目录可读不可进之类):索引这次「不知道」,不是「不见」。
        scan.index_unknown = True
        scan.incomplete = True
        return
    scan.index_present = stat.S_ISREG(info.st_mode)
    if scan.index_present:
        scan.seen.add(AGENT_NOTES_FILE)
        yield AGENT_NOTES_FILE, info.st_mtime_ns


def _versions(file_dir: int) -> list[tuple[int, str, str]]:
    """``(序号, sha256, 文件名)``,按序号升序。"""
    out: list[tuple[int, str, str]] = []
    for name in os.listdir(file_dir):
        m = _VERSION_RE.match(name)
        if m:
            out.append((int(m.group(1)), m.group(2), name))
    return sorted(out)


def _open_versions_root(root: str, tenant_id: UUID, user_id: UUID, *, create: bool) -> int:
    """``{root}/{tenant}/.agent-file-versions/{user}`` 的目录 fd(每级 ``O_NOFOLLOW``)。"""
    fd = os.open(Path(root), os.O_RDONLY | os.O_DIRECTORY)
    for part in (str(tenant_id), AGENT_FILE_VERSIONS_DIR, str(user_id)):
        try:
            nfd = _openat_dir(fd, part, create=create)
        finally:
            os.close(fd)
        fd = nfd
    return fd


def _write_version(file_dir: int, data: bytes, info: os.stat_result, digest: str, seq: int) -> None:
    """先写临时文件、fsync,再 ``link`` 成 ``v<序号>-<sha>``(已存在就往后挪一号)。"""
    tmp = f".tmp-{uuid4().hex}"
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        _FILE_MODE,
        dir_fd=file_dir,
    )
    try:
        try:
            os.fchmod(fd, _FILE_MODE)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
            os.utime(fd, ns=(info.st_atime_ns, info.st_mtime_ns))
        finally:
            os.close(fd)
        for attempt in range(3):
            try:
                os.link(
                    tmp,
                    f"v{seq + attempt:08d}-{digest}",
                    src_dir_fd=file_dir,
                    dst_dir_fd=file_dir,
                    follow_symlinks=False,
                )
                return
            except FileExistsError:
                continue  # 另一次观察同时写了同一号
        raise FileExistsError(f"no free version slot after v{seq:08d}")
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=file_dir)


def _prune(file_dir: int, scan: _Scan) -> None:
    versions = _versions(file_dir)
    for _seq, _sha, name in versions[: max(0, len(versions) - MAX_VERSIONS_PER_FILE)]:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=file_dir)
            scan.pruned += 1


def _observe_file(
    agent_dir: int | None, src_dir: int, name: str, rel: str, scan: _Scan
) -> _Pending | None:
    """看一个文件:没变 → ``None``;变了 → 读好的一份待写版本。

    review 10 —— 这里**不建**版本目录:读预算不够、读失败、内容其实没变,都不该留下一个空的、
    没有 ``path`` 的目录(那样换着名字就能绕过目录数上限)。目录在真正写版本时才建。
    """
    key = _key_of(rel)
    src = os.open(name, _READ_FLAGS, dir_fd=src_dir)
    try:
        info = os.fstat(src)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_VERSIONED_FILE_BYTES:
            scan.skipped += 1
            return None
        file_dir: int | None = None
        if agent_dir is not None:
            with contextlib.suppress(FileNotFoundError):
                file_dir = _openat_dir(agent_dir, key, create=False)
        try:
            versions = _versions(file_dir) if file_dir is not None else []
            latest = versions[-1] if versions else None
            if latest is not None and file_dir is not None:
                copy = os.lstat(latest[2], dir_fd=file_dir)
                if (copy.st_size, copy.st_mtime_ns) == (info.st_size, info.st_mtime_ns):
                    scan.unchanged += 1
                    return None
            if scan.read_bytes + info.st_size > MAX_OBSERVE_READ_BYTES:
                scan.skipped += 1  # 这一次读不下了,下一次 run 结束再看
                return None
            data = _read_all(src, info.st_size)
            scan.read_bytes += len(data)
            digest = hashlib.sha256(data).hexdigest()
            if latest is not None and file_dir is not None and latest[1] == digest:
                os.utime(latest[2], ns=(info.st_atime_ns, info.st_mtime_ns), dir_fd=file_dir)
                scan.unchanged += 1
                return None
            return _Pending(rel, key, data, info, digest, (latest[0] + 1) if latest else 1)
        finally:
            if file_dir is not None:
                os.close(file_dir)
    finally:
        os.close(src)


def _write_path(file_dir: int, rel: str) -> None:
    """``path`` 原子地写(review 14):先写临时文件、fsync,再 ``rename`` 盖上去 —— 不会留下写了
    一半 / 空的 ``path``;已有的(哪怕坏了)也一并换掉。"""
    tmp = f".tmp-path-{uuid4().hex}"
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        _FILE_MODE,
        dir_fd=file_dir,
    )
    try:
        try:
            view = memoryview(rel.encode())
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, _PATH_FILE, src_dir_fd=file_dir, dst_dir_fd=file_dir)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=file_dir)


def _key_of(rel: str) -> str:
    return hashlib.sha256(rel.encode()).hexdigest()[:32]


_INDEX_KEY = _key_of(AGENT_NOTES_FILE)


def _valid_path(file_dir: int, key: str) -> str | None:
    """这个版本目录记的源路径;没有 ``path``、或内容和目录名对不上(空的、写了一半的)→ ``None``。

    review 14 —— 只有「不存在」才是 ``None``;别的读错误(EIO / ESTALE / EMFILE…)原样抛出:
    :func:`_sweep` 这一次跳过这个目录(不删、不动标记),:func:`_dir_usage` 把它当「不知道」(按在算、
    全额保护)。
    """
    rel = _read_path(file_dir)
    if rel is None or _key_of(rel) != key:
        return None
    return rel


def observe_sync(
    workspace: NasWorkspaceStore, tenant_id: UUID, user_id: UUID, agent_key: str
) -> ObserveReport:
    """扫一遍该 agent 的 ``/agent``,改过的文件各留一份。

    review 7 —— 用户已被清除(``.deleted`` 软删标记在)就什么都不做:清除之后才结束的 run 不能
    把版本又建回来。review 10 —— 先把变了的文件读好,再在 :func:`_housekeep` 里按这个 agent 的
    字节预算决定写不写、删哪些;版本目录只在真正写版本时才建。
    """
    require_safe_key(agent_key)
    if os.path.lexists(workspace_deleted_marker(workspace.root, tenant_id, user_id)):
        return ObserveReport()
    scan = _Scan()
    pending: list[_Pending] = []
    try:
        agent_fd = workspace._open_scope_dir_fd(tenant_id, user_id, f"{AGENTS_DIR}/{agent_key}")
    except MISSING_DIR_ERRORS:
        return ObserveReport()  # agent 根还没建:没有可留的
    except BaseException:
        _after_failed_walk(workspace.root, tenant_id, user_id, agent_key)
        raise
    try:
        _walk_and_read(workspace.root, tenant_id, user_id, agent_key, agent_fd, scan, pending)
    except BaseException:
        _after_failed_walk(workspace.root, tenant_id, user_id, agent_key)
        raise
    finally:
        os.close(agent_fd)
    _housekeep(workspace.root, tenant_id, user_id, agent_key, scan, pending)
    return ObserveReport(
        snapshotted=scan.snapshotted,
        unchanged=scan.unchanged,
        skipped=scan.skipped,
        pruned=scan.pruned,
        dropped=scan.dropped,
        over_budget=bool(scan.over_reasons),
        over_budget_reasons=tuple(sorted(scan.over_reasons)),
        walk_incomplete=scan.incomplete,
    )


def _walk_and_read(
    root: str,
    tenant_id: UUID,
    user_id: UUID,
    agent_key: str,
    agent_fd: int,
    scan: _Scan,
    pending: list[_Pending],
) -> None:
    """遍历 + 把变了的文件读进 ``pending``(单个文件的错误只跳过那一个)。"""
    agent_dir = _open_agent_versions(root, tenant_id, user_id, agent_key, create=False)
    try:
        walked = [*_check_index(agent_fd, scan), *_files(agent_fd, "", 0, scan)]
        for rel in _observe_order(iter(walked)):
            try:
                found = _observe_at(agent_fd, agent_dir, rel, scan)
            except OSError:
                scan.skipped += 1  # 刚被换成符号链接 / 删掉了:这一个跳过
                continue
            if found is not None:
                pending.append(found)
    finally:
        if agent_dir is not None:
            os.close(agent_dir)


def _after_failed_walk(root: str, tenant_id: UUID, user_id: UUID, agent_key: str) -> None:
    """review 15 —— 打不开 agent 根、或遍历本身抛了异常:照样用一个**空的**扫描结果收一次尾再让
    异常往外抛。空扫描里每个文件都是「不知道」,所以这一遍只会撤掉各目录的 ``missing`` 标记、删
    一份版本都没有的孤儿;``pending`` 是空的,不写、不按预算删、不按「不见满 30 天」删。

    就是要守住:**没有哪种失败能让 T0 的旧标记一直作数**(否则「确认不见 → 一个月里每次观察都
    失败 → 藏一次」就能把整个目录一下删掉)。直接复用 :func:`_housekeep`(而不是另写一个只撤标记
    的分支):同一条路径,软删标记的检查也一起有;它自己的错误只记日志,不盖住原来的异常。
    """
    try:
        _housekeep(root, tenant_id, user_id, agent_key, _Scan(), [])
    except Exception as exc:
        logger.warning(
            "agent_file_versions.failed_walk_sweep_failed agent_key=%s error=%s",
            agent_key,
            type(exc).__name__,
        )


def _observe_order(found: Iterator[tuple[str, int]]) -> list[str]:
    """先看完整棵树,再定读的先后(review 10):索引 ``AGENT_NOTES.md`` 永远第一个,其余按 mtime
    从新到旧。

    一次观察最多读 :data:`MAX_OBSERVE_READ_BYTES`。按名字排时,排在前面、每轮只被 ``touch``(内容
    没变、mtime 变了 —— 只能读一遍算 sha256 才知道没变)的大文件能把预算用光,排在后面的真改动
    (包括索引)永远轮不上。大小与 mtime 都没变的文件本来就不读、不花预算。
    """
    entries = list(found)
    return [
        rel
        for rel, _mtime in sorted(
            entries, key=lambda item: (item[0] != AGENT_NOTES_FILE, -item[1], item[0])
        )
    ]


def _observe_at(agent_fd: int, agent_dir: int | None, rel: str, scan: _Scan) -> _Pending | None:
    """按 ``rel`` 从 agent 根 fd 逐段 ``O_NOFOLLOW`` 打开所在目录再看这个文件(遍历时的目录 fd
    已经关了;中间某段被换成链接 → ``OSError``,调用方跳过)。"""
    *parents, name = rel.split("/")
    dir_fd = os.dup(agent_fd)
    try:
        for part in parents:
            nfd = _openat_dir(dir_fd, part, create=False)
            os.close(dir_fd)
            dir_fd = nfd
        return _observe_file(agent_dir, dir_fd, name, rel, scan)
    finally:
        os.close(dir_fd)


def _open_agent_versions(
    root: str, tenant_id: UUID, user_id: UUID, agent_key: str, *, create: bool
) -> int | None:
    """``.agent-file-versions/{user}/{agent_key}`` 的目录 fd;不建时不存在 → ``None``。"""
    try:
        user_fd = _open_versions_root(root, tenant_id, user_id, create=create)
    except FileNotFoundError:
        return None
    try:
        return _openat_dir(user_fd, agent_key, create=create)
    except FileNotFoundError:
        return None
    finally:
        os.close(user_fd)


def _housekeep(
    root: str,
    tenant_id: UUID,
    user_id: UUID,
    agent_key: str,
    scan: _Scan,
    pending: list[_Pending],
) -> None:
    """观察之后的收尾,只动**这个 agent 自己**的版本目录(不存在、也没有要写的就什么都不做):

    1. 扫一遍各版本目录(:func:`_sweep`):在的撤「不见」标记、确认不见的记下 / 满 30 天删掉、
       不知道的撤掉标记(下一次确认不见重新计时);一份版本都没有的孤儿满 1 小时删掉;
    2. 按字节预算与版本目录数上限写这次读好的新版本(:func:`_write_pending`);
    3. 写了新版本就把总量收回预算内(:func:`_enforce_budget`)。2、3 共用一次 :func:`_usage`
       (写了哪个目录就只重读那一个)。

    review 12 —— 开始前与最后一次写之后都看一眼清除用户的软删标记:开始前在就什么都不做;写完
    才在(清除的第二遍删除可能已经跑完)就把这个用户的版本整个删掉。
    """
    marker = workspace_deleted_marker(root, tenant_id, user_id)
    if os.path.lexists(marker):
        return  # review 12 —— 观察开始之后用户被清除了:什么都不建、不写
    try:
        agent_dir = _open_agent_versions(root, tenant_id, user_id, agent_key, create=bool(pending))
        if agent_dir is None:
            return
        try:
            _sweep(agent_dir, scan, time.time())
            if pending:
                usage = _usage(agent_dir, scan)
                _write_pending(agent_dir, pending, scan, usage)
                if scan.snapshotted:
                    # review 15 —— 写过之后重读一次:别的副本同时写下的版本也算进去。
                    _enforce_budget(agent_dir, scan, _usage(agent_dir, scan))
        finally:
            os.close(agent_dir)
    finally:
        # review 12/13 —— 写的过程中清除落下了(它的第二遍删除可能已经跑完):这个用户的版本整个
        # 删掉。放在 ``finally`` 里:收尾中途抛了异常,已经写下的也要删。
        if os.path.lexists(marker):
            _delete_user_versions(root, tenant_id, user_id)


def _usage(agent_dir: int, scan: _Scan) -> dict[str, _DirUsage]:
    """这个 agent 每个版本目录的现状(打不开的跳过);在不在按这次扫描判(:meth:`_Scan.status`)。"""
    out: dict[str, _DirUsage] = {}
    for key in os.listdir(agent_dir):
        try:
            file_dir = _openat_dir(agent_dir, key, create=False)
        except OSError:
            continue
        try:
            out[key] = _dir_usage(file_dir, key, scan)
        finally:
            os.close(file_dir)
    return out


def _dir_usage(file_dir: int, key: str, scan: _Scan) -> _DirUsage:
    """一个版本目录的现状。``path`` 读不动或坏了 → 「不知道」(按在算、全额保护,review 14)。"""
    versions: list[tuple[int, str, int]] = []
    for _seq, _sha, name in _versions(file_dir):
        with contextlib.suppress(FileNotFoundError):
            info = os.lstat(name, dir_fd=file_dir)
            versions.append((info.st_ctime_ns, name, info.st_size))
    try:
        rel = _valid_path(file_dir, key)
    except OSError:
        rel = None
    status = scan.status(rel) if rel is not None else "unknown"
    return _DirUsage(rel, status, versions, is_index=key == _INDEX_KEY)


def _missing_since(file_dir: int) -> float | None:
    try:
        return os.lstat(_MISSING_FILE, dir_fd=file_dir).st_mtime
    except FileNotFoundError:
        return None


def _write_pending(
    agent_dir: int, pending: list[_Pending], scan: _Scan, usage: dict[str, _DirUsage]
) -> None:
    """按预算写这次读好的新版本(``usage`` 就地更新:写了哪个目录就重读哪一个)。

    review 11/13 —— 写之前算受保护的部分(:meth:`_DirUsage.floor`:在的或不知道的文件最新 2 份、
    已不见的在宽限期内最新 1 份、索引最新 3 份):写了这一份之后它会超预算,就**跳过这一份**
    (记 ``bytes``),后面放得下的照写。要新建版本目录而已到目录数上限,也跳过(``dir_cap``;
    索引总留一格)。两个都碰上两个都记。绝不靠删受保护的版本或目录腾地方。

    review 14 —— 已有目录的 ``path`` 和这个文件对不上(空的、写了一半的)就原子地重写。
    """
    floor = sum(d.floor() for d in usage.values())
    dirs = len(os.listdir(agent_dir))
    for item in pending:
        current = usage.get(item.key) or _DirUsage(item.rel, "live", [])
        after = _DirUsage(
            item.rel, "live", [*current.versions, (time.time_ns(), "", len(item.data))]
        )
        grown = floor - current.floor() + after.floor()
        new_dir = item.key not in usage
        # review 13 —— 索引的版本目录总有一格:目录数顶满(哪怕全是已不见文件的)也建得出来。
        dir_limit = MAX_VERSION_DIRS_PER_AGENT + (1 if item.rel == AGENT_NOTES_FILE else 0)
        reasons = set()
        if grown > MAX_AGENT_VERSION_BYTES:
            reasons.add("bytes")
        if new_dir and dirs >= dir_limit:
            reasons.add("dir_cap")
        if reasons:
            # review 12/13 —— 满了就不留新的,绝不为腾地方删受保护的版本或目录。
            scan.over_reasons |= reasons
            scan.skipped += 1
            continue
        try:
            file_dir = _openat_dir(agent_dir, item.key, create=True)
        except OSError:
            scan.skipped += 1
            continue
        dirs += 1 if new_dir else 0
        try:
            try:
                if new_dir or current.rel != item.rel:
                    _write_path(file_dir, item.rel)  # 新目录,或 ``path`` 坏了:原子地(重)写
                _write_version(file_dir, item.data, item.info, item.digest, item.seq)
            except OSError:
                scan.skipped += 1
                continue
            scan.snapshotted += 1
            # review 15 —— 账先按保守的记上(这一份已经写下了),再做可能出错的清理与重读:清理
            # 出错时后面的放行判断仍按「多了这一份」算,不会把预算算松。
            usage[item.key] = after
            floor = grown
            with contextlib.suppress(OSError):
                _prune(file_dir, scan)
                fresh = _dir_usage(file_dir, item.key, scan)
                floor += fresh.floor() - after.floor()
                usage[item.key] = fresh
        finally:
            os.close(file_dir)


def _prune_plan(usage: dict[str, _DirUsage]) -> list[tuple[str, str, int]]:
    """预算吃紧时删的先后,``(版本目录, 版本文件名, 大小)``,**只含不受保护的版本**(review 13):

    按「在自己文件里排第几新」(rank,最新的是 1)从大到小,跨所有文件:每个文件的第 20 新都排在
    任何文件的第 19 新前面,依此类推;同 rank 的先删大的,再先删早写下的。受保护的最新几份
    (:meth:`_DirUsage.keep`)不在名单里 —— 已不见的文件在 30 天宽限期内也一样。
    """
    ranked: list[tuple[int, int, int, str, str, int]] = []
    for key, d in usage.items():
        n = len(d.versions)
        for i, (ctime, name, size) in enumerate(d.versions):
            rank = n - i
            if rank > d.keep:
                ranked.append((-rank, -size, ctime, key, name, size))
    return [(key, name, size) for *_order, key, name, size in sorted(ranked)]


def _enforce_budget(agent_dir: int, scan: _Scan, usage: dict[str, _DirUsage]) -> None:
    """这个 agent 的版本总量超过 :data:`MAX_AGENT_VERSION_BYTES` 就按 :func:`_prune_plan` 的先后删,
    直到不超。只碰这个 agent 自己的、不受保护的版本。

    :func:`_write_pending` 保证受保护的那部分不超预算,所以单个观察删完一定回到预算之内;受保护的
    部分本身已超(跨副本同时观察、恢复了大文件)时删完仍超,记 ``still_over``。
    """
    total = sum(size for d in usage.values() for _c, _n, size in d.versions)
    if total <= MAX_AGENT_VERSION_BYTES:
        return
    for key, name, size in _prune_plan(usage):
        if total <= MAX_AGENT_VERSION_BYTES:
            return
        try:
            file_dir = _openat_dir(agent_dir, key, create=False)
        except OSError:
            continue
        try:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name, dir_fd=file_dir)
                scan.pruned += 1
        finally:
            os.close(file_dir)
        total -= size
    if total > MAX_AGENT_VERSION_BYTES:
        scan.over_reasons.add("still_over")


def _read_path(file_dir: int) -> str | None:
    """``path`` 的内容;不存在 → ``None``,不是普通文件 → ``None``(当它坏了);别的 ``OSError``
    原样抛出(review 14:一次瞬时的 EIO / ESTALE 不能让一个好好的版本目录被当成孤儿删掉)。"""
    try:
        fd = os.open(_PATH_FILE, _READ_FLAGS, dir_fd=file_dir)
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, 4096).decode("utf-8", errors="replace")
    finally:
        os.close(fd)


def _sweep(agent_dir: int, scan: _Scan, now: float) -> None:
    """扫一遍这个 agent 的版本目录。

    * 孤儿 —— **一份版本都没有**的目录 —— 建了满 :data:`ORPHAN_DIR_GRACE_S` 就删(review 10/14:
      有版本的目录从不当孤儿;``path`` 没了或坏了只算「不知道」)。
    * ``path`` 读不动(EIO / ESTALE…,review 14):这次跳过这个目录,什么都不动。
    * 源文件在(``live``):撤掉 ``missing`` 标记。
    * 确认不见(``missing``:一路上的目录都列全了):第一次发现时记下标记,满 30 天删掉整个版本目录。
    * 不知道(``unknown``:某一层没列全,或 ``path`` 坏了):撤掉标记(review 14)—— 之后要再确认一次
      不见、从那时重新算 30 天;不然「确认不见 → 一个月都看不清 → 藏一次」就能把整个目录一下删掉。
    """
    seen_by_key = {_key_of(rel): rel for rel in scan.seen}
    for key in os.listdir(agent_dir):
        try:
            file_dir = _openat_dir(agent_dir, key, create=False)
        except OSError:
            continue
        drop = False
        try:
            _sweep_tmp(file_dir, now)
            if not _versions(file_dir):
                drop = now - os.fstat(file_dir).st_mtime >= ORPHAN_DIR_GRACE_S
                continue
            try:
                rel = _valid_path(file_dir, key)
            except OSError:
                continue  # 读不动:这一次跳过,不当孤儿、不判不见
            if rel is None and key in seen_by_key:
                # review 15 —— ``path`` 坏了、源文件这次看到了(没改,所以不会走写入那条修复):在这里
                # 原子地修好,不然它一直「不知道」、删掉后也永远不会到期。
                with contextlib.suppress(OSError):
                    _write_path(file_dir, seen_by_key[key])
                    rel = seen_by_key[key]
            status = scan.status(rel) if rel is not None else "unknown"
            if status == "missing":
                drop = _note_missing(file_dir, now)
            else:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(_MISSING_FILE, dir_fd=file_dir)
        finally:
            os.close(file_dir)
            if drop:
                _drop_dir(agent_dir, key, scan)


def _sweep_tmp(file_dir: int, now: float) -> None:
    """review 15 —— 写版本 / ``path`` 写到一半被杀留下的 ``.tmp-*``(不算版本、不进预算,目录有版本
    又不是孤儿,所以没人清):建了满 :data:`ORPHAN_DIR_GRACE_S` 就删(不跟正在写的观察抢)。"""
    for name in os.listdir(file_dir):
        if not name.startswith(".tmp-"):
            continue
        with contextlib.suppress(OSError):
            if now - os.lstat(name, dir_fd=file_dir).st_mtime >= ORPHAN_DIR_GRACE_S:
                os.unlink(name, dir_fd=file_dir)


def _drop_dir(agent_dir: int, key: str, scan: _Scan) -> None:
    """整个删掉一个版本目录。另一次观察同时往里写(``ENOTEMPTY``)、已经被删(``ENOENT``)等任何
    ``OSError`` 都只跳过这一个,不让整次观察失败(review 11)。"""
    try:
        shutil.rmtree(key, dir_fd=agent_dir)
    except OSError as exc:
        logger.debug("agent_file_versions.drop_dir_skipped error=%s", type(exc).__name__)
        return
    scan.dropped += 1


def _note_missing(file_dir: int, now: float) -> bool:
    """源文件确认不见了:第一次发现时记 ``missing`` 标记;已不见满 :data:`MISSING_SOURCE_GRACE_S`
    时回 ``True``。"""
    since = _missing_since(file_dir)
    if since is None:
        with contextlib.suppress(FileExistsError):
            os.close(
                os.open(
                    _MISSING_FILE,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    _FILE_MODE,
                    dir_fd=file_dir,
                )
            )
        return False
    return now - since >= MISSING_SOURCE_GRACE_S


def _read_all(fd: int, size: int) -> bytes:
    """读打开时的大小那么多(之后追加的不算)。"""
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = os.read(fd, min(remaining, 1024 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _delete_user_versions(root: str, tenant_id: UUID, user_id: UUID) -> int:
    try:
        parent = _open_versions_root_parent(root, tenant_id)
    except FileNotFoundError:
        return 0
    try:
        name = str(user_id)
        try:
            info = os.lstat(name, dir_fd=parent)
        except FileNotFoundError:
            return 0
        if not stat.S_ISDIR(info.st_mode):
            msg = f"agent file version entry is not a directory: {name!r}"
            raise WorkspacePathEscapeError(msg)
        count = 0
        user_fd = _openat_dir(parent, name, create=False)
        try:
            for _base, _dirs, files, _dfd in os.fwalk(dir_fd=user_fd):
                count += sum(1 for f in files if _VERSION_RE.match(f))
        finally:
            os.close(user_fd)
        shutil.rmtree(name, dir_fd=parent)
        return count
    finally:
        os.close(parent)


def _open_versions_root_parent(root: str, tenant_id: UUID) -> int:
    """``{root}/{tenant}/.agent-file-versions`` 的目录 fd(不建)。"""
    fd = os.open(Path(root), os.O_RDONLY | os.O_DIRECTORY)
    for part in (str(tenant_id), AGENT_FILE_VERSIONS_DIR):
        try:
            nfd = _openat_dir(fd, part, create=False)
        finally:
            os.close(fd)
        fd = nfd
    return fd


@dataclass
class NasAgentFileVersionStore:
    """生产实现 —— 版本放在 NAS 上用户根旁边、沙箱挂不到的地方。"""

    workspace: NasWorkspaceStore
    #: review 12 —— 同一个(租户, 用户, agent)的观察在本进程里一个一个来:两个对话同时结束时
    #: 各自按旧的现状算预算,受保护的部分合计会冲过预算。只管本进程;跨副本的同时观察见 spec。
    lock: KeyedLock = field(default_factory=ProcessKeyedLock)

    async def observe(self, *, tenant_id: UUID, user_id: UUID, agent_key: str) -> ObserveReport:
        async with self.lock.acquire(key=f"{tenant_id}:{user_id}:{agent_key}"):
            work = asyncio.ensure_future(
                asyncio.to_thread(observe_sync, self.workspace, tenant_id, user_id, agent_key)
            )
            try:
                return await asyncio.shield(work)
            except asyncio.CancelledError:
                # review 13/14 —— 线程取消不了:等它真跑完再放锁,否则下一次观察会和它重叠;再被
                # 取消也接着等。它要是抛了异常,只记类型并计失败(没人会再取这个结果)。
                while not work.done():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.wait([work])
                exc = work.exception()
                if exc is not None:
                    agent_file_versions_total.labels(outcome="failed").inc()
                    logger.warning(
                        "agent_file_versions.observe_failed agent_key=%s error=%s",
                        agent_key,
                        type(exc).__name__,
                    )
                raise

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        return await asyncio.to_thread(
            _delete_user_versions, self.workspace.root, tenant_id, user_id
        )

    async def delete_for_path(self, *, tenant_id: UUID, user_id: UUID, path: str) -> int:
        return await asyncio.to_thread(
            _delete_path_versions, self.workspace.root, tenant_id, user_id, path
        )


def _delete_path_versions(root: str, tenant_id: UUID, user_id: UUID, path: str) -> int:
    """``agents/<key>/<rel>``(用户根相对)的版本目录整个删掉。别的路径(对话目录、平台目录、
    老布局的扁平路径)没有版本,返回 0。"""
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if len(parts) < 3 or parts[0] != AGENTS_DIR or ".." in parts:
        return 0
    agent_key, rel = parts[1], "/".join(parts[2:])
    if parts[2].startswith(".") or parts[2] in AGENT_ROOT_RESERVED:
        return 0
    require_safe_key(agent_key)
    key = _key_of(rel)
    try:
        user_fd = _open_versions_root(root, tenant_id, user_id, create=False)
    except FileNotFoundError:
        return 0
    try:
        try:
            agent_dir = _openat_dir(user_fd, agent_key, create=False)
        except FileNotFoundError:
            return 0
        try:
            try:
                file_dir = _openat_dir(agent_dir, key, create=False)
            except FileNotFoundError:
                return 0
            try:
                count = sum(1 for n in os.listdir(file_dir) if _VERSION_RE.match(n))
            finally:
                os.close(file_dir)
            shutil.rmtree(key, dir_fd=agent_dir)
            return count
        finally:
            os.close(agent_dir)
    finally:
        os.close(user_fd)


#: fire-and-forget 观察任务的引用(不留引用,任务可能在跑完之前被回收)。
_BACKGROUND: set[asyncio.Task[None]] = set()


async def _observe_logged(
    store: AgentFileVersionStore, tenant_id: UUID, user_id: UUID, agent_key: str
) -> None:
    try:
        report = await store.observe(tenant_id=tenant_id, user_id=user_id, agent_key=agent_key)
    except Exception as exc:
        agent_file_versions_total.labels(outcome="failed").inc()
        logger.warning(
            "agent_file_versions.observe_failed agent_key=%s error=%s",
            agent_key,
            type(exc).__name__,
        )
        return
    if report.snapshotted:
        agent_file_versions_total.labels(outcome="snapshotted").inc(report.snapshotted)
    if report.over_budget:
        # review 11~13 —— 碰到了这个 agent 的上限(原因见 ``ObserveReport.over_budget``):有新版本
        # 没留,或删完能删的仍超预算;受保护的版本从不为此删。运维要能找到是谁、为什么:日志带租户
        # 与用户 id 和原因,计数按租户。
        agent_file_versions_total.labels(outcome="over_budget").inc()
        agent_file_versions_over_budget_total.labels(tenant_id=str(tenant_id)).inc()
        logger.warning(
            "agent_file_versions.over_budget tenant_id=%s user_id=%s agent_key=%s reason=%s "
            "budget_bytes=%d",
            tenant_id,
            user_id,
            agent_key,
            ",".join(report.over_budget_reasons),
            MAX_AGENT_VERSION_BYTES,
        )
    if report.walk_incomplete:
        # review 14 —— 有目录没列全:那些目录里没看到的文件这次既不算在也不算不见(不开始、也不
        # 继续 30 天倒计时)。名字数 / 条目数到顶时整个根目录都可能没列全。
        agent_file_versions_total.labels(outcome="walk_incomplete").inc()
        logger.warning(
            "agent_file_versions.walk_incomplete tenant_id=%s user_id=%s agent_key=%s",
            tenant_id,
            user_id,
            agent_key,
        )
    logger.info(
        "agent_file_versions.observed agent_key=%s snapshotted=%d unchanged=%d skipped=%d "
        "pruned=%d",
        agent_key,
        report.snapshotted,
        report.unchanged,
        report.skipped,
        report.pruned,
    )


def dispatch_observation(
    store: AgentFileVersionStore | None, configurable: Mapping[str, Any]
) -> asyncio.Task[None] | None:
    """run 结束时调(``sse.run_agent`` 的 ``finally``):对话视图的主 run 才观察,不等它跑完。

    委派子代、老视图、没绑 agent / 用户都不观察(老视图里 ``/agent`` 就是工作目录,每个产出都
    会被当成「记忆」留版本)。
    """
    if store is None or configurable.get("child_run"):
        return None
    from orchestrator.tools.registry import ToolContext, workspace_view_fields

    ctx = ToolContext(**workspace_view_fields(configurable))
    tenant_id, user_id = _uuid(configurable.get("tenant_id")), _uuid(configurable.get("user_id"))
    if tenant_id is None or user_id is None or not ctx.workspace_view.per_conversation:
        return None
    task = asyncio.create_task(_observe_logged(store, tenant_id, user_id, ctx.agent_key))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task


def _uuid(raw: object) -> UUID | None:
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw)) if isinstance(raw, str) else None
    except ValueError:
        return None
