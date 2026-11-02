"""B-262 P3 —— 老对话的平台文件迁进本对话的 ``/workspace``:宿主侧复制。

B-262 P1 之后,对话视图里 ``uploads/`` / ``inputs/`` / ``.tool_results/`` 是本对话
``threads/<conv>/work/`` 下的普通子目录,agent 根下的同名目录在 ``/agent`` 里被盖住。之前
开的对话,它自己的上传与工具结果还在 agent 根;这里在它下一次 run 开始前把**本对话自己的**
那部分复制回 ``work/``(调用方 —— 控制面的 ``platform_migration`` —— 决定复制哪些、何时复制、
加锁、翻布局、写标记)。

* **只碰目录 fd、不跟符号链接**:起点是 :class:`NasWorkspaceStore` 自己的防穿越闸,一路
  ``_openat_dir``(``O_NOFOLLOW``);文件 ``O_NOFOLLOW|O_NONBLOCK`` 打开后 ``fstat`` 核对是
  普通文件(做法同 :mod:`orchestrator.tools.conversation_seed`)。
* **从不覆盖**:目标名已存在就跳过(新对话自己的同名上传、上次迁移复制过的都在这里)。文件先
  写进对话目录下的临时目录,再 ``link`` 进 ``work/uploads/``(名字被占就失败,不覆盖);工具
  结果整棵复制进临时目录,再改名进 ``work/.tool_results/<run>``。中途崩溃最多留一个临时目录
  (下次清掉),不会留一份写了一半的同名文件让下次当成「已存在」跳过。
* **上传件核对内容**:P0 之前同名上传会覆盖,agent 根里的 ``uploads/report.pdf`` 可能已经
  是别的客户的了。调用方给出这个名字在本对话上传时记下的 sha256(审计)/ 大小(登记行),
  对不上就不复制 —— 宁可这一份没迁过来,不把别人的件放进本对话。
* **上限**:一个对话合计 :data:`MAX_MIGRATION_BYTES`,另有条目数与深度上限(只防攻击形态);
  放不下的那一项整个跳过、记数。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import stat
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from expert_work.persistence import WORKSPACE_OVERFLOW_DIR, WORKSPACE_UPLOADS_DIR
from expert_work.persistence.workspace.layout import WORKSPACE_PLATFORM_MIGRATED_MARKER
from expert_work.protocol.agent_key import require_safe_key
from orchestrator.tools.nas_workspace_store import (
    NasWorkspaceStore,
    _openat_dir,
    _WorkspacePathNotFoundError,
)
from orchestrator.tools.workspace_paths import AGENTS_DIR, CONVERSATION_WORK_DIR, THREADS_DIR

logger = logging.getLogger(__name__)

#: 一个对话迁移合计复制的字节上限。上传件单个 ≤ 25 MiB(上传闸),工具结果每个 run 一般几十
#: KB 到几 MB(渲染页每页 ~100 KB);100 MiB 盖得住常见的长对话,又不让一次 run 开场卡太久。
MAX_MIGRATION_BYTES = 100 * 1024 * 1024
#: 条目(文件 + 目录)总数上限 —— 只给系统调用数封顶。渲染页一页 4 层目录 + 1 个文件。
MAX_MIGRATION_ENTRIES = 20_000
#: ``.tool_results/<run>/`` 下的目录深度上限(渲染页是 4 层:figures/<doc>/<render>/_u<n>)。
MAX_TREE_DEPTH = 8

#: 迁移做完的标记,在 ``threads/<conv>/`` 下(不在 ``work/`` 里),最后写。
MIGRATED_MARKER = WORKSPACE_PLATFORM_MIGRATED_MARKER
#: 复制进行中的临时目录名前缀(``threads/<conv>/.migrate-tmp-<随机>/``)。
MIGRATE_TMP_PREFIX = ".migrate-tmp-"

_FILE_MODE = 0o600
_CHUNK = 1024 * 1024
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


@dataclass(frozen=True)
class ExpectedUpload:
    """本对话上传过的一个文件:``uploads/<name>``,以及上传时记下的内容指纹。

    ``sha256s`` 非空时按 sha256 核对(审计行有),否则按 ``sizes``(登记行有);两样都没有的
    不复制(无从确认它还是本对话那一份)。同一个名字上传过几次就有几个取值,对上任意一个即可。
    """

    name: str
    sizes: frozenset[int] = frozenset()
    sha256s: frozenset[str] = frozenset()


@dataclass(frozen=True)
class MigrationCopyReport:
    """一次复制的计数。日志与标记只记这些数,不记名字(名字是客户内容)。"""

    files_copied: int = 0
    bytes_copied: int = 0
    uploads_copied: int = 0
    uploads_existing: int = 0
    uploads_missing: int = 0
    uploads_mismatched: int = 0
    uploads_over_cap: int = 0
    runs_copied: int = 0
    runs_existing: int = 0
    runs_missing: int = 0
    runs_over_cap: int = 0
    skipped_entries: int = 0


class _TooLargeError(Exception):
    """这一项超出了剩下的额度(字节 / 条目 / 深度)。"""


@dataclass
class _Budget:
    bytes_left: int
    entries_left: int = MAX_MIGRATION_ENTRIES
    files: int = 0
    bytes: int = 0
    skipped: int = 0
    counts: dict[str, int] = field(default_factory=dict)

    def bump(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    def spend(self, *, size: int, files: int, entries: int) -> None:
        self.bytes_left -= size
        self.entries_left -= entries
        self.bytes += size
        self.files += files

    def report(self) -> MigrationCopyReport:
        c = self.counts
        return MigrationCopyReport(
            files_copied=self.files,
            bytes_copied=self.bytes,
            uploads_copied=c.get("uploads_copied", 0),
            uploads_existing=c.get("uploads_existing", 0),
            uploads_missing=c.get("uploads_missing", 0),
            uploads_mismatched=c.get("uploads_mismatched", 0),
            uploads_over_cap=c.get("uploads_over_cap", 0),
            runs_copied=c.get("runs_copied", 0),
            runs_existing=c.get("runs_existing", 0),
            runs_missing=c.get("runs_missing", 0),
            runs_over_cap=c.get("runs_over_cap", 0),
            skipped_entries=self.skipped,
        )


def _safe_leaf(name: str) -> bool:
    """单独一段、不是点开头(也挡住了临时目录名与 ``.`` / ``..``)、没有 NUL。"""
    return bool(name) and "/" not in name and "\x00" not in name and not name.startswith(".")


def _exists(dir_fd: int, name: str) -> bool:
    try:
        os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return False
    return True


def _optional_dir(dir_fd: int, name: str) -> int | None:
    """``name`` 的目录 fd;不存在、是符号链接、不是目录都当没有。"""
    try:
        return _openat_dir(dir_fd, name, create=False)
    except OSError:
        return None


def _conv_dir(
    store: NasWorkspaceStore,
    tenant_id: UUID,
    user_id: UUID,
    agent_key: str,
    conv_id: UUID,
    *,
    create: bool,
) -> int:
    """``agents/<key>/threads/<conv>/`` 的目录 fd —— 经 store 自己的那道防穿越闸。"""
    require_safe_key(agent_key)
    rel = f"{AGENTS_DIR}/{agent_key}/{THREADS_DIR}/{conv_id}/{MIGRATED_MARKER}"
    dfd, _name = store._open_parent_dir_fd(tenant_id, user_id, rel, create=create)
    return dfd


def platform_migrated(
    store: NasWorkspaceStore, tenant_id: UUID, user_id: UUID, agent_key: str, conv_id: UUID
) -> bool:
    """本对话是不是已经迁移过(不建任何目录)。"""
    try:
        conv_fd = _conv_dir(store, tenant_id, user_id, agent_key, conv_id, create=False)
    except _WorkspacePathNotFoundError:
        return False
    try:
        return _exists(conv_fd, MIGRATED_MARKER)
    finally:
        os.close(conv_fd)


def write_migrated_marker(
    store: NasWorkspaceStore,
    tenant_id: UUID,
    user_id: UUID,
    agent_key: str,
    conv_id: UUID,
    report: MigrationCopyReport,
) -> None:
    """写标记(已经有就算了 —— 别的副本刚写完,结果同样成立)。"""
    body = json.dumps({"version": 1, **report.__dict__}).encode()
    conv_fd = _conv_dir(store, tenant_id, user_id, agent_key, conv_id, create=True)
    try:
        try:
            fd = os.open(MIGRATED_MARKER, _CREATE_FLAGS, _FILE_MODE, dir_fd=conv_fd)
        except FileExistsError:
            return
        try:
            os.write(fd, body)
        finally:
            os.close(fd)
    finally:
        os.close(conv_fd)


def _write_copy(src: int, size: int, dst_dir: int, name: str, info: os.stat_result) -> str:
    """把已打开的 ``src`` 的前 ``size`` 个字节写成 ``dst_dir/name``(必须不存在),回 sha256。"""
    digest = hashlib.sha256()
    mode = _FILE_MODE | (info.st_mode & stat.S_IXUSR)
    dst = os.open(name, _CREATE_FLAGS, mode, dir_fd=dst_dir)
    try:
        os.fchmod(dst, mode)
        remaining = size
        while remaining > 0:
            chunk = os.read(src, min(_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                view = view[os.write(dst, view) :]
        os.utime(dst, ns=(info.st_atime_ns, info.st_mtime_ns))
    finally:
        os.close(dst)
    return digest.hexdigest()


def _copy_upload(
    src_dir: int | None, dst_dir: int, tmp_dir: int, expected: ExpectedUpload, budget: _Budget
) -> str:
    """一个上传件;回计数键的后缀(``copied`` / ``existing`` / ``missing`` / ``mismatched`` /
    ``over_cap``)。"""
    name = expected.name
    if not _safe_leaf(name) or src_dir is None:
        return "missing"
    if _exists(dst_dir, name):
        return "existing"
    if not expected.sha256s and not expected.sizes:
        return "mismatched"
    try:
        src = os.open(name, _READ_FLAGS, dir_fd=src_dir)
    except OSError:
        return "missing"  # 不存在 / 是符号链接(ELOOP)/ socket(ENXIO)
    try:
        info = os.fstat(src)
        if not stat.S_ISREG(info.st_mode):
            return "missing"
        if expected.sizes and not expected.sha256s and info.st_size not in expected.sizes:
            return "mismatched"
        if info.st_size > budget.bytes_left or budget.entries_left < 1:
            return "over_cap"
        tmp_name = uuid4().hex
        digest = _write_copy(src, info.st_size, tmp_dir, tmp_name, info)
    finally:
        os.close(src)
    try:
        if expected.sha256s and digest not in expected.sha256s:
            return "mismatched"
        try:
            os.link(tmp_name, name, src_dir_fd=tmp_dir, dst_dir_fd=dst_dir, follow_symlinks=False)
        except FileExistsError:
            return "existing"
        budget.spend(size=info.st_size, files=1, entries=1)
        return "copied"
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name, dir_fd=tmp_dir)


def _children(dir_fd: int, budget: _Budget) -> list[tuple[str, os.stat_result]]:
    out: list[tuple[str, os.stat_result]] = []
    for name in sorted(os.listdir(dir_fd)):
        try:
            info = os.lstat(name, dir_fd=dir_fd)
        except OSError:
            budget.skipped += 1
            continue
        if stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
            out.append((name, info))
        else:
            budget.skipped += 1  # 符号链接 / FIFO / 设备 / socket
    return out


def _measure(dir_fd: int, depth: int, budget: _Budget, tally: list[int]) -> None:
    """只 ``lstat``:``tally = [字节, 条目]``;超出剩余额度或深度就抛。"""
    for name, info in _children(dir_fd, budget):
        tally[1] += 1
        if stat.S_ISREG(info.st_mode):
            tally[0] += info.st_size
        else:
            if depth >= MAX_TREE_DEPTH:
                raise _TooLargeError
            sub = _optional_dir(dir_fd, name)
            if sub is None:
                continue
            try:
                _measure(sub, depth + 1, budget, tally)
            finally:
                os.close(sub)
        if tally[0] > budget.bytes_left or tally[1] > budget.entries_left:
            raise _TooLargeError


def _copy_tree(src_dir: int, dst_dir: int, depth: int, budget: _Budget, tally: list[int]) -> None:
    """``tally = [字节, 文件, 条目]``(实际写出的)。"""
    for name, info in _children(src_dir, budget):
        if stat.S_ISREG(info.st_mode):
            try:
                src = os.open(name, _READ_FLAGS, dir_fd=src_dir)
            except OSError:
                budget.skipped += 1
                continue
            try:
                opened = os.fstat(src)
                if not stat.S_ISREG(opened.st_mode):
                    budget.skipped += 1
                    continue
                _write_copy(src, opened.st_size, dst_dir, name, opened)
            finally:
                os.close(src)
            tally[0] += opened.st_size
            tally[1] += 1
            tally[2] += 1
            continue
        if depth >= MAX_TREE_DEPTH:
            raise _TooLargeError
        sub = _optional_dir(src_dir, name)
        if sub is None:
            budget.skipped += 1
            continue
        try:
            out = _openat_dir(dst_dir, name, create=True)
            try:
                _copy_tree(sub, out, depth + 1, budget, tally)
                st = os.fstat(sub)
                os.utime(out, ns=(st.st_atime_ns, st.st_mtime_ns))
            finally:
                os.close(out)
        finally:
            os.close(sub)
        tally[2] += 1
    if tally[0] > budget.bytes_left or tally[2] > budget.entries_left:
        raise _TooLargeError  # 量完之后又变大了


def _copy_run(
    src_dir: int | None, dst_dir: int, tmp_dir: int, run_id: UUID, budget: _Budget
) -> str:
    """一个 run 的 ``.tool_results/<run>/`` 整棵;回计数键的后缀。"""
    name = str(run_id)
    if _exists(dst_dir, name):
        return "existing"
    src = _optional_dir(src_dir, name) if src_dir is not None else None
    if src is None:
        return "missing"
    try:
        try:
            _measure(src, 1, budget, [0, 0])
        except _TooLargeError:
            return "over_cap"
        tmp_name = uuid4().hex
        out = _openat_dir(tmp_dir, tmp_name, create=True)
        tally = [0, 0, 1]
        try:
            _copy_tree(src, out, 1, budget, tally)
            st = os.fstat(src)
            os.utime(out, ns=(st.st_atime_ns, st.st_mtime_ns))
        except _TooLargeError:
            shutil.rmtree(tmp_name, dir_fd=tmp_dir)
            return "over_cap"
        finally:
            os.close(out)
    finally:
        os.close(src)
    if _exists(dst_dir, name):
        shutil.rmtree(tmp_name, dir_fd=tmp_dir)
        return "existing"
    os.rename(tmp_name, name, src_dir_fd=tmp_dir, dst_dir_fd=dst_dir)
    budget.spend(size=tally[0], files=tally[1], entries=tally[2])
    return "copied"


def copy_platform_files(
    store: NasWorkspaceStore,
    tenant_id: UUID,
    user_id: UUID,
    agent_key: str,
    conv_id: UUID,
    *,
    uploads: Sequence[ExpectedUpload],
    run_ids: Sequence[UUID],
    max_bytes: int = MAX_MIGRATION_BYTES,
) -> MigrationCopyReport:
    """把本对话自己的上传件与 run 的工具结果从 agent 根复制进 ``threads/<conv>/work/``。

    同步、阻塞,调用方放进线程里跑。上传件在前(它们比工具结果要紧)。目标端
    ``work/uploads`` / ``work/.tool_results`` 被换成符号链接之类会抛(不写标记、下次再试);
    源端缺失或不合格的那一项只记数。
    """
    budget = _Budget(bytes_left=max_bytes)
    conv_fd = _conv_dir(store, tenant_id, user_id, agent_key, conv_id, create=True)
    try:
        try:
            agent_fd = store._open_scope_dir_fd(tenant_id, user_id, f"{AGENTS_DIR}/{agent_key}")
        except _WorkspacePathNotFoundError:
            agent_fd = None
        work_fd = _openat_dir(conv_fd, CONVERSATION_WORK_DIR, create=True)
        tmp = f"{MIGRATE_TMP_PREFIX}{uuid4().hex}"
        tmp_fd = _openat_dir(conv_fd, tmp, create=True)
        try:
            if uploads:
                _copy_uploads(agent_fd, work_fd, tmp_fd, uploads, budget)
            if run_ids:
                _copy_runs(agent_fd, work_fd, tmp_fd, run_ids, budget)
        finally:
            os.close(tmp_fd)
            with contextlib.suppress(FileNotFoundError):
                shutil.rmtree(tmp, dir_fd=conv_fd)
            os.close(work_fd)
            if agent_fd is not None:
                os.close(agent_fd)
    finally:
        os.close(conv_fd)
    return budget.report()


def _copy_uploads(
    agent_fd: int | None,
    work_fd: int,
    tmp_fd: int,
    uploads: Sequence[ExpectedUpload],
    budget: _Budget,
) -> None:
    src = _optional_dir(agent_fd, WORKSPACE_UPLOADS_DIR) if agent_fd is not None else None
    dst = _openat_dir(work_fd, WORKSPACE_UPLOADS_DIR, create=True)
    try:
        for expected in uploads:
            budget.bump("uploads_" + _copy_upload(src, dst, tmp_fd, expected, budget))
    finally:
        os.close(dst)
        if src is not None:
            os.close(src)


def _copy_runs(
    agent_fd: int | None, work_fd: int, tmp_fd: int, run_ids: Sequence[UUID], budget: _Budget
) -> None:
    src = _optional_dir(agent_fd, WORKSPACE_OVERFLOW_DIR) if agent_fd is not None else None
    dst = _openat_dir(work_fd, WORKSPACE_OVERFLOW_DIR, create=True)
    try:
        for run_id in run_ids:
            budget.bump("runs_" + _copy_run(src, dst, tmp_fd, run_id, budget))
    finally:
        os.close(dst)
        if src is not None:
            os.close(src)
