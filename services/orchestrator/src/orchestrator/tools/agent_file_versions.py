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
  这个 agent 自己的版本,各 agent 之间按构造隔离)。
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
from orchestrator.tools.conversation_seed import MAX_SEED_DEPTH, MAX_SEED_ENTRIES
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
#: agent 根最多 77 个文件,2000 留足余量。review 10 起每次观察都查(不只在写了新版本时)。
MAX_VERSION_DIRS_PER_AGENT = 2000

#: review 10 —— 没有 ``path`` 或一份版本都没有的版本目录,建了满这么久还这样就删掉(正常写入
#: 是建目录、写 ``path``、写版本一气呵成;留一小时是不跟另一次同时进行的观察抢)。
ORPHAN_DIR_GRACE_S = 3600

#: review 11 —— 预算吃紧时,活文件最新的这么多份从不删(索引 ``AGENT_NOTES.md`` 是
#: :data:`MIN_KEPT_INDEX_VERSIONS` 份)。预算之内每个文件仍最多留
#: :data:`MAX_VERSIONS_PER_FILE` 份。
MIN_KEPT_LIVE_VERSIONS = 2
MIN_KEPT_INDEX_VERSIONS = 3

#: review 7 —— 源文件从 ``/agent`` 消失满这么久,它的版本目录整个删掉。「从哪天起不见」记在
#: 版本目录里的 ``missing`` 标记上(第一次发现不见时建,文件回来就删)。
MISSING_SOURCE_GRACE_S = 30 * 24 * 3600

_PATH_FILE = "path"
_MISSING_FILE = "missing"
_VERSION_RE = re.compile(r"^v(\d{8})-([0-9a-f]{64})$")
_FILE_MODE = 0o600
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

#: review 11 —— 停了留新版本的观察,按租户计(用户 id 只进日志:按用户打标签基数无上限)。
agent_file_versions_over_budget_total = expert_work_counter(
    "expert_work_agent_file_versions_over_budget_total",
    "B-236 run-end observations of /agent files that skipped a new version because the "
    "agent's version budget could not hold it, by tenant.",
    ("tenant_id",),
)

agent_file_versions_total = expert_work_counter(
    "expert_work_agent_file_versions_total",
    "B-236 run-end observation of /agent files: snapshots written, observations that "
    "stopped snapshotting because the agent's version budget is full, and observations "
    "that failed (logged, never fail the run).",
    ("outcome",),
)


@dataclass(frozen=True)
class ObserveReport:
    """一次观察的结果(只有个数,日志里不出现文件名)。"""

    snapshotted: int = 0
    unchanged: int = 0
    skipped: int = 0
    pruned: int = 0
    #: 源文件不见满 30 天、整个删掉的版本目录数。
    dropped: int = 0
    #: review 10 —— 这个 agent 各活文件最新那份加起来已经顶到预算:这次停了留新版本。
    over_budget: bool = False


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
    read_bytes: int = 0
    snapshotted: int = 0
    unchanged: int = 0
    skipped: int = 0
    pruned: int = 0
    dropped: int = 0
    #: 这次看到的普通文件(agent 根相对)—— 判「源文件还在不在」用。
    seen: set[str] = field(default_factory=set)
    #: 条目数上限截断了这次遍历:``seen`` 不全,不判「不见」。
    complete: bool = True
    over_budget: bool = False
    #: review 11 —— 这次观察开始的时间:索引在这之后才被记「不见」的,这一次仍按活文件算。
    started: float = field(default_factory=time.time)


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
    """一个版本目录:源路径、源文件从什么时候起不见(``missing`` 标记的 mtime;``None`` = 还在)、
    各版本 ``(ctime, 文件名, 大小)`` 按序号升序。"""

    rel: str | None
    missing_since: float | None
    versions: list[tuple[int, str, int]]

    @property
    def missing(self) -> bool:
        return self.missing_since is not None

    @property
    def keep(self) -> int:
        """预算吃紧时也不删的最新几份:活文件 2 份、活的索引 3 份;源文件已不见的 0 份(它的最新
        那份排在最后删,见 :func:`_prune_plan`)。"""
        if self.missing:
            return 0
        return MIN_KEPT_INDEX_VERSIONS if self.rel == AGENT_NOTES_FILE else MIN_KEPT_LIVE_VERSIONS

    def floor(self) -> int:
        """不能删的那几份合计多大。"""
        return sum(size for _c, _n, size in self.versions[-self.keep :]) if self.keep else 0


def _utf8(name: str) -> bool:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _files(dir_fd: int, prefix: str, depth: int, scan: _Scan) -> Iterator[tuple[str, int]]:
    """``(agent 根相对路径, mtime 纳秒)``,一路 ``O_NOFOLLOW``;顶层跳过保留名。"""
    for name in sorted(os.listdir(dir_fd)):
        if depth == 0 and (name.startswith(".") or name in AGENT_ROOT_RESERVED):
            continue
        if not _utf8(name):
            scan.skipped += 1
            continue
        scan.entries += 1
        if scan.entries > MAX_SEED_ENTRIES:
            scan.skipped += 1
            scan.complete = False
            return
        try:
            info = os.lstat(name, dir_fd=dir_fd)
        except FileNotFoundError:
            continue
        rel = f"{prefix}{name}"
        if stat.S_ISREG(info.st_mode):
            scan.seen.add(rel)
            yield rel, info.st_mtime_ns
        elif stat.S_ISDIR(info.st_mode) and depth + 1 < MAX_SEED_DEPTH:
            try:
                sub = _openat_dir(dir_fd, name, create=False)
            except OSError:
                # review 8 —— 打不开(``chmod 000`` / 刚被换成链接)的目录里有什么不知道:这次
                # 遍历不完整,不能据此判任何文件「不见了」开始 30 天倒计时。
                scan.skipped += 1
                scan.complete = False
                continue
            try:
                yield from _files(sub, f"{rel}/", depth + 1, scan)
            finally:
                os.close(sub)
        else:
            if stat.S_ISDIR(info.st_mode):
                scan.complete = False  # 深度到顶,下面没看(同上:不判「不见」)
            scan.skipped += 1


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
    key = hashlib.sha256(rel.encode()).hexdigest()[:32]
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
    with contextlib.suppress(FileExistsError):
        fd = os.open(
            _PATH_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            _FILE_MODE,
            dir_fd=file_dir,
        )
        try:
            os.write(fd, rel.encode())
        finally:
            os.close(fd)


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
    try:
        agent_fd = workspace._open_scope_dir_fd(tenant_id, user_id, f"{AGENTS_DIR}/{agent_key}")
    except MISSING_DIR_ERRORS:
        return ObserveReport()  # agent 根还没建:没有可留的
    scan = _Scan()
    pending: list[_Pending] = []
    agent_dir = _open_agent_versions(workspace.root, tenant_id, user_id, agent_key, create=False)
    try:
        for rel in _observe_order(_files(agent_fd, "", 0, scan)):
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
        os.close(agent_fd)
    _housekeep(workspace.root, tenant_id, user_id, agent_key, scan, pending)
    return ObserveReport(
        snapshotted=scan.snapshotted,
        unchanged=scan.unchanged,
        skipped=scan.skipped,
        pruned=scan.pruned,
        dropped=scan.dropped,
        over_budget=scan.over_budget,
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

    1. 扫一遍各版本目录:源文件不见的记下 / 满 30 天删掉(遍历完整时);没有 ``path`` 或没有
       版本的孤儿目录删掉;
    2. 按字节预算写这次读好的新版本(:func:`_write_pending`);
    3. 写了新版本就把总量收回预算内(:func:`_enforce_budget`);
    4. 版本目录数封顶(每次都查)。
    """
    agent_dir = _open_agent_versions(root, tenant_id, user_id, agent_key, create=bool(pending))
    if agent_dir is None:
        return
    try:
        _sweep(agent_dir, scan, time.time())
        if pending:
            _write_pending(agent_dir, pending, scan)
        if scan.snapshotted:
            _enforce_budget(agent_dir, scan)
        _enforce_dir_cap(agent_dir, scan)
    finally:
        os.close(agent_dir)


def _usage(agent_dir: int, started: float) -> dict[str, _DirUsage]:
    """这个 agent 每个版本目录的现状(打不开的跳过)。"""
    out: dict[str, _DirUsage] = {}
    for key in os.listdir(agent_dir):
        try:
            file_dir = _openat_dir(agent_dir, key, create=False)
        except OSError:
            continue
        try:
            versions: list[tuple[int, str, int]] = []
            for _seq, _sha, name in _versions(file_dir):
                with contextlib.suppress(FileNotFoundError):
                    info = os.lstat(name, dir_fd=file_dir)
                    versions.append((info.st_ctime_ns, name, info.st_size))
            rel = _read_path(file_dir)
            since = _missing_since(file_dir)
            if since is not None and rel == AGENT_NOTES_FILE and since >= started:
                # review 11 —— 索引这一次才不见(改名成点开头的名字之类):这一次仍按活文件护着。
                since = None
            out[key] = _DirUsage(rel, since, versions)
        finally:
            os.close(file_dir)
    return out


def _missing_since(file_dir: int) -> float | None:
    try:
        return os.lstat(_MISSING_FILE, dir_fd=file_dir).st_mtime
    except FileNotFoundError:
        return None


def _write_pending(agent_dir: int, pending: list[_Pending], scan: _Scan) -> None:
    """按预算写这次读好的新版本。

    review 11 —— 写之前算「删到底也删不掉的那部分」(各活文件最新 2 份、活的索引最新 3 份,见
    :meth:`_DirUsage.floor`):写了这一份之后它会超预算,就**跳过这一份**(记 ``over_budget``),
    后面放得下的照写(review 10 是整个停下,一份放不下就挡住后面所有的改动)。绝不靠删受保护的
    版本腾地方。
    """
    usage = _usage(agent_dir, scan.started)
    floor = sum(d.floor() for d in usage.values())
    for item in pending:
        current = usage.get(item.key) or _DirUsage(item.rel, None, [])
        after = _DirUsage(item.rel, None, [*current.versions, (time.time_ns(), "", len(item.data))])
        grown = floor - (0 if current.missing else current.floor()) + after.floor()
        if grown > MAX_AGENT_VERSION_BYTES:
            scan.over_budget = True
            continue
        try:
            file_dir = _openat_dir(agent_dir, item.key, create=True)
        except OSError:
            scan.skipped += 1
            continue
        try:
            _write_path(file_dir, item.rel)
            _write_version(file_dir, item.data, item.info, item.digest, item.seq)
            scan.snapshotted += 1
            floor = grown
            usage[item.key] = after
            _prune(file_dir, scan)
        except OSError:
            scan.skipped += 1
        finally:
            os.close(file_dir)


def _prune_plan(usage: dict[str, _DirUsage]) -> list[tuple[str, str, int]]:
    """预算吃紧时删的先后,``(版本目录, 版本文件名, 大小)``(review 11):

    1. **按「在自己文件里排第几新」(rank,最新的是 1)从大到小**,跨所有文件:每个文件的第 20 新
       都排在任何文件的第 19 新前面,依此类推;同 rank 的先删大的,再先删早写下的。一个反复改的
       大文件因此只是把自己的旧版本让出来,不会一次把别的文件的历史冲光。
    2. 活文件最新 2 份(活的索引 3 份)不在名单里。
    3. 源文件已不见的:rank ≥ 2 的跟活文件一起按 rank 排;它的最新那份排在最后 —— 所有活文件都
       只剩 2 份之后才轮到,不见得最久的先删。满 30 天整个删掉照旧(:func:`_sweep`)。
    """
    ranked: list[tuple[int, int, int, str, str, int]] = []
    last: list[tuple[float, int, str, str, int]] = []
    for key, d in usage.items():
        n = len(d.versions)
        for i, (ctime, name, size) in enumerate(d.versions):
            rank = n - i
            if rank <= d.keep:
                continue
            if d.missing_since is not None and rank == 1:
                last.append((d.missing_since, ctime, key, name, size))
            else:
                ranked.append((-rank, -size, ctime, key, name, size))
    return [(key, name, size) for *_order, key, name, size in sorted(ranked)] + [
        (key, name, size) for *_order, key, name, size in sorted(last)
    ]


def _enforce_budget(agent_dir: int, scan: _Scan) -> None:
    """这个 agent 的版本总量超过 :data:`MAX_AGENT_VERSION_BYTES` 就按 :func:`_prune_plan` 的先后删,
    直到不超。只碰这个 agent 自己的版本目录。

    :func:`_write_pending` 保证受保护的那部分不超预算,所以删完一定回到预算之内;删完仍超(并发
    写入之类)就记 ``over_budget``。
    """
    usage = _usage(agent_dir, scan.started)
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
        scan.over_budget = True


def _read_path(file_dir: int) -> str | None:
    try:
        fd = os.open(_PATH_FILE, _READ_FLAGS, dir_fd=file_dir)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, 4096).decode("utf-8", errors="replace")
    finally:
        os.close(fd)


def _sweep(agent_dir: int, scan: _Scan, now: float) -> None:
    """扫一遍这个 agent 的版本目录。

    * 孤儿(没有 ``path``,或一份版本都没有)建了满 :data:`ORPHAN_DIR_GRACE_S` 就删(review 10)。
    * 遍历完整时:源文件不在了,第一次发现时记下(``missing`` 标记),满 30 天删掉整个版本目录;
      源文件回来了就撤掉标记。
    """
    for key in os.listdir(agent_dir):
        try:
            file_dir = _openat_dir(agent_dir, key, create=False)
        except OSError:
            continue
        drop = False
        try:
            rel = _read_path(file_dir)
            if rel is None or not _versions(file_dir):
                drop = now - os.fstat(file_dir).st_mtime >= ORPHAN_DIR_GRACE_S
            elif scan.complete:
                drop = _note_missing(file_dir, rel, scan, now)
        finally:
            os.close(file_dir)
        if drop:
            _drop_dir(agent_dir, key, scan)


def _drop_dir(agent_dir: int, key: str, scan: _Scan) -> None:
    """整个删掉一个版本目录。另一次观察同时往里写(``ENOTEMPTY``)、已经被删(``ENOENT``)等任何
    ``OSError`` 都只跳过这一个,不让整次观察失败(review 11)。"""
    try:
        shutil.rmtree(key, dir_fd=agent_dir)
    except OSError as exc:
        logger.debug("agent_file_versions.drop_dir_skipped error=%s", type(exc).__name__)
        return
    scan.dropped += 1


def _note_missing(file_dir: int, rel: str, scan: _Scan, now: float) -> bool:
    """记 / 撤 ``missing`` 标记;源文件已不见满 :data:`MISSING_SOURCE_GRACE_S` 时回 ``True``。"""
    if rel in scan.seen:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(_MISSING_FILE, dir_fd=file_dir)
        return False
    try:
        since = os.lstat(_MISSING_FILE, dir_fd=file_dir).st_mtime
    except FileNotFoundError:
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


def _drop_order(agent_dir: int, key: str, now: float) -> tuple[int, int] | None:
    """版本目录的删除先后:打不开 / 没有版本 / 没有 ``path`` 的最先,其次源文件已不见(有
    ``missing`` 标记)的,再按最新那份版本写下的时间(ctime)从旧到新。

    review 11 —— 没有版本 / 没有 ``path``、但建了还不满 :data:`ORPHAN_DIR_GRACE_S` 的回 ``None``
    (不删):那多半是另一次观察刚建、正要往里写的目录。"""
    try:
        file_dir = _openat_dir(agent_dir, key, create=False)
    except OSError:
        return (0, 0)
    try:
        live = 0 if _exists_at(file_dir, _MISSING_FILE) else 1
        versions = _versions(file_dir)
        if not versions or not _exists_at(file_dir, _PATH_FILE):
            if now - os.fstat(file_dir).st_mtime < ORPHAN_DIR_GRACE_S:
                return None
            return (0, 0)
        return (live, os.lstat(versions[-1][2], dir_fd=file_dir).st_ctime_ns)
    except OSError:
        return (0, 0)
    finally:
        os.close(file_dir)


def _exists_at(dir_fd: int, name: str) -> bool:
    try:
        os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return False
    return True


def _enforce_dir_cap(agent_dir: int, scan: _Scan) -> None:
    """本 agent 的版本目录超过 :data:`MAX_VERSION_DIRS_PER_AGENT`:整个删掉一些,直到不超。

    review 9 —— 先删源文件已经不见的(``missing`` 标记在),再按目录里**最新那份版本**写下的时间
    从旧到新。不按目录 mtime:``missing`` 标记的建与撤会刷新目录 mtime,按它排,已经删掉的源文件
    反而显得「新」,还活着、只是一直没改的文件先丢了历史。"""
    keys = os.listdir(agent_dir)
    excess = len(keys) - MAX_VERSION_DIRS_PER_AGENT
    if excess <= 0:
        return
    now = time.time()
    ranked = [
        (order, key) for key in keys if (order := _drop_order(agent_dir, key, now)) is not None
    ]
    for _order, key in sorted(ranked)[:excess]:
        _drop_dir(agent_dir, key, scan)


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

    async def observe(self, *, tenant_id: UUID, user_id: UUID, agent_key: str) -> ObserveReport:
        return await asyncio.to_thread(observe_sync, self.workspace, tenant_id, user_id, agent_key)

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
    key = hashlib.sha256(rel.encode()).hexdigest()[:32]
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
        # review 10/11 —— 受保护的版本加起来顶到了这个 agent 的预算:有新版本没留(不删受保护的)。
        # 运维要能找到是谁:日志带租户与用户 id,计数按租户。
        agent_file_versions_total.labels(outcome="over_budget").inc()
        agent_file_versions_over_budget_total.labels(tenant_id=str(tenant_id)).inc()
        logger.warning(
            "agent_file_versions.over_budget tenant_id=%s user_id=%s agent_key=%s budget_bytes=%d",
            tenant_id,
            user_id,
            agent_key,
            MAX_AGENT_VERSION_BYTES,
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
