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
  路径,版本文件名 ``v<序号>-<sha256>``;每个文件只留最近 :data:`MAX_VERSIONS_PER_FILE` 份。
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
from orchestrator.tools.workspace_paths import AGENT_ROOT_RESERVED, AGENTS_DIR

logger = logging.getLogger(__name__)

#: 每个文件留最近几份(用户 10-30 拍板 20)。
MAX_VERSIONS_PER_FILE = 20

#: 单个文件超过这么大不留版本。测试环境实测 agent 根里最大的单个文件 4.0 MB;8 MiB 留余量,
#: 又不让一个视频把版本区撑爆。
MAX_VERSIONED_FILE_BYTES = 8 * 1024 * 1024

#: 一次观察最多读这么多字节(只读大小 / mtime 变了的文件)。读不完的留到下一次 run 结束。
MAX_OBSERVE_READ_BYTES = 20 * 1024 * 1024

#: review 7 —— 一个用户全部版本(所有 agent)合计的上限。超了从最早写下的版本删起,跨文件、
#: 跨 agent。版本也计入这个用户的工作区配额(``WorkspaceQuotaService.refresh``)。
MAX_USER_VERSION_BYTES = 200 * 1024 * 1024

#: review 7 —— 源文件从 ``/agent`` 消失满这么久,它的版本目录整个删掉。「从哪天起不见」记在
#: 版本目录里的 ``missing`` 标记上(第一次发现不见时建,文件回来就删)。
MISSING_SOURCE_GRACE_S = 30 * 24 * 3600

_PATH_FILE = "path"
_MISSING_FILE = "missing"
_VERSION_RE = re.compile(r"^v(\d{8})-([0-9a-f]{64})$")
_FILE_MODE = 0o600
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

agent_file_versions_total = expert_work_counter(
    "expert_work_agent_file_versions_total",
    "B-236 run-end observation of /agent files: snapshots written, and observations "
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


def _utf8(name: str) -> bool:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _files(dir_fd: int, prefix: str, depth: int, scan: _Scan) -> Iterator[tuple[int, str, str]]:
    """``(所在目录 fd, 名字, agent 根相对路径)``,一路 ``O_NOFOLLOW``;顶层跳过保留名。"""
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
            yield dir_fd, name, rel
        elif stat.S_ISDIR(info.st_mode) and depth + 1 < MAX_SEED_DEPTH:
            try:
                sub = _openat_dir(dir_fd, name, create=False)
            except OSError:
                scan.skipped += 1
                continue
            try:
                yield from _files(sub, f"{rel}/", depth + 1, scan)
            finally:
                os.close(sub)
        else:
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


def _observe_file(agent_dir: int, src_dir: int, name: str, rel: str, scan: _Scan) -> None:
    key = hashlib.sha256(rel.encode()).hexdigest()[:32]
    src = os.open(name, _READ_FLAGS, dir_fd=src_dir)
    try:
        info = os.fstat(src)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_VERSIONED_FILE_BYTES:
            scan.skipped += 1
            return
        file_dir = _openat_dir(agent_dir, key, create=True)
        try:
            versions = _versions(file_dir)
            latest = versions[-1] if versions else None
            if latest is not None:
                copy = os.lstat(latest[2], dir_fd=file_dir)
                if (copy.st_size, copy.st_mtime_ns) == (info.st_size, info.st_mtime_ns):
                    scan.unchanged += 1
                    return
            if scan.read_bytes + info.st_size > MAX_OBSERVE_READ_BYTES:
                scan.skipped += 1  # 这一次读不下了,下一次 run 结束再看
                return
            data = _read_all(src, info.st_size)
            scan.read_bytes += len(data)
            digest = hashlib.sha256(data).hexdigest()
            if latest is not None and latest[1] == digest:
                os.utime(latest[2], ns=(info.st_atime_ns, info.st_mtime_ns), dir_fd=file_dir)
                scan.unchanged += 1
                return
            if not versions:
                _write_path(file_dir, rel)
            _write_version(file_dir, data, info, digest, (latest[0] + 1) if latest else 1)
            scan.snapshotted += 1
            _prune(file_dir, scan)
        finally:
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
    """扫一遍该 agent 的 ``/agent``,改过的文件各留一份。版本目录在遇到第一个文件时才建。

    review 7 —— 用户已被清除(``.deleted`` 软删标记在)就什么都不做:清除之后才结束的 run 不能
    把版本又建回来。之后两件收尾:源文件不见满 30 天的版本目录删掉;本次写了新版本时,用户
    合计超过 :data:`MAX_USER_VERSION_BYTES` 就从最早的版本删起。
    """
    require_safe_key(agent_key)
    if os.path.lexists(workspace_deleted_marker(workspace.root, tenant_id, user_id)):
        return ObserveReport()
    try:
        agent_fd = workspace._open_scope_dir_fd(tenant_id, user_id, f"{AGENTS_DIR}/{agent_key}")
    except MISSING_DIR_ERRORS:
        return ObserveReport()  # agent 根还没建:没有可留的
    scan = _Scan()
    agent_dir: int | None = None
    try:
        for src_dir, name, rel in _files(agent_fd, "", 0, scan):
            if agent_dir is None:
                versions_fd = _open_versions_root(workspace.root, tenant_id, user_id, create=True)
                try:
                    agent_dir = _openat_dir(versions_fd, agent_key, create=True)
                finally:
                    os.close(versions_fd)
            try:
                _observe_file(agent_dir, src_dir, name, rel, scan)
            except OSError:
                scan.skipped += 1  # 刚被换成符号链接 / 删掉了:这一个跳过
    finally:
        if agent_dir is not None:
            os.close(agent_dir)
        os.close(agent_fd)
    _housekeep(workspace.root, tenant_id, user_id, agent_key, scan)
    return ObserveReport(
        snapshotted=scan.snapshotted,
        unchanged=scan.unchanged,
        skipped=scan.skipped,
        pruned=scan.pruned,
        dropped=scan.dropped,
    )


def _housekeep(root: str, tenant_id: UUID, user_id: UUID, agent_key: str, scan: _Scan) -> None:
    """观察之后的两件收尾(版本目录不存在就什么都不做)。"""
    try:
        user_fd = _open_versions_root(root, tenant_id, user_id, create=False)
    except FileNotFoundError:
        return
    try:
        if scan.complete:
            try:
                agent_dir = _openat_dir(user_fd, agent_key, create=False)
            except FileNotFoundError:
                agent_dir = None
            if agent_dir is not None:
                try:
                    _sweep_missing(agent_dir, scan, time.time())
                finally:
                    os.close(agent_dir)
        if scan.snapshotted:
            _enforce_user_cap(user_fd, scan)
    finally:
        os.close(user_fd)


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


def _sweep_missing(agent_dir: int, scan: _Scan, now: float) -> None:
    """源文件不在了:第一次发现时记下(``missing`` 标记),满 30 天删掉整个版本目录;
    源文件回来了就撤掉标记。"""
    for key in os.listdir(agent_dir):
        try:
            file_dir = _openat_dir(agent_dir, key, create=False)
        except OSError:
            continue
        try:
            rel = _read_path(file_dir)
            if rel is None:
                continue
            if rel in scan.seen:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(_MISSING_FILE, dir_fd=file_dir)
                continue
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
                continue
        finally:
            os.close(file_dir)
        if now - since >= MISSING_SOURCE_GRACE_S:
            shutil.rmtree(key, dir_fd=agent_dir)
            scan.dropped += 1


def _enforce_user_cap(user_fd: int, scan: _Scan) -> None:
    """用户合计超过上限:按写下的先后(ctime)从最早的版本删起,跨文件、跨 agent。"""
    found: list[tuple[int, str, str, str, int]] = []  # (ctime, agent, key, name, size)
    total = 0
    for agent_key in os.listdir(user_fd):
        try:
            agent_dir = _openat_dir(user_fd, agent_key, create=False)
        except OSError:
            continue
        try:
            for key in os.listdir(agent_dir):
                try:
                    file_dir = _openat_dir(agent_dir, key, create=False)
                except OSError:
                    continue
                try:
                    for name in os.listdir(file_dir):
                        if not _VERSION_RE.match(name):
                            continue
                        info = os.lstat(name, dir_fd=file_dir)
                        found.append((info.st_ctime_ns, agent_key, key, name, info.st_size))
                        total += info.st_size
                finally:
                    os.close(file_dir)
        finally:
            os.close(agent_dir)
    for _ctime, agent_key, key, name, size in sorted(found):
        if total <= MAX_USER_VERSION_BYTES:
            return
        agent_dir = _openat_dir(user_fd, agent_key, create=False)
        try:
            file_dir = _openat_dir(agent_dir, key, create=False)
            try:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=file_dir)
                    scan.pruned += 1
            finally:
                os.close(file_dir)
        finally:
            os.close(agent_dir)
        total -= size


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
