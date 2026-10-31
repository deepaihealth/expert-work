"""``NasWorkspaceStore`` — NAS-mounted :class:`WorkspaceStore` (sandbox migration wave 2).

**Why a direct filesystem implementation.** Wave 1's ``SupervisorWorkspaceStore``
proxies every workspace-file operation over HTTP because the control-plane
process cannot otherwise reach a per-user docker volume — only the
sandbox-supervisor host can. Wave 2 replaces the docker-volume workspace
with a shared NAS volume (Alibaba Cloud NAS via the CSI driver) that is
mounted **whole-tree** into the control-plane Pod itself (see wave 2 Task 2's
``workspace-nas`` PV/PVC + the control-plane Deployment's volume mount) —
so the control-plane no longer needs a network hop to read or write a
user's files. This store implements the same :class:`WorkspaceStore`
Protocol by operating on ``self.root`` (the Pod-local mount point, e.g.
``/mnt/workspaces``) with :mod:`os` ``dir_fd``-relative syscalls; per-tenant
/per-user layout is ``{root}/{tenant_id}/{user_id}/...``, matching the
sandbox side's ``subPath: "<tenant_id>/<user_id>"`` projection of the same
volume (wave 2 Task 4/6) — a sandbox writing under ``/workspace`` and this
store reading ``{root}/{tenant_id}/{user_id}`` see the same files.

**Parity contract with SupervisorWorkspaceStore.** Both implementations must
behave identically at the :class:`WorkspaceStore` Protocol boundary — same
error type (:class:`SandboxSupervisorError`), same path-validation rules,
same size caps, same reserved-prefix filtering — so that swapping the
factory's choice of backend (``build_workspace_store``, keyed off
``Settings.workspace_nas_root``) never changes agent-visible behaviour. The
cap / filter constants below intentionally mirror
``sandbox_supervisor.supervisor``'s ``_MAX_ARTIFACT_BYTES`` /
``_MAX_WORKSPACE_WRITE_BYTES`` / ``_MAX_WORKSPACE_LIST_ENTRIES`` — they are
re-declared here (not imported) because ``orchestrator`` and
``sandbox-supervisor`` are independent services with no runtime dependency
on each other; wave 2 Task 7's contract-test suite is what pins the two
implementations together and would catch a drift.

**Known deferred asymmetry (workspace-gid-sharing direction change).**
``SupervisorWorkspaceStore`` never raises :class:`WorkspacePermissionError` —
every supervisor-side failure, permission-related or not, still surfaces as
the generic :class:`SandboxSupervisorError` a caller cannot tell apart from
"doesn't exist". A permission failure is therefore a 500 on this (NAS) store
but still a 404 "file not found" on the supervisor backend; the contract-test
leg that would otherwise flag this drift is deliberately weakened for an
unrelated, documented reason (``test_sandbox_runtime_contract.py``'s
``test_written_file_is_readable_by_the_control_plane_identity``, supervisor
leg). Not fixed here — recorded so the next reader doesn't take "same error
type" above as still true of this one dimension.

**Marker semantics.** :meth:`NasWorkspaceStore.mark_deleted` is a
*soft*-delete: it drops an empty sentinel file at
:func:`workspace_deleted_marker`'s path and nothing else — no file is
removed, no bytes are freed. This mirrors the supervisor's
``mark_workspace_deleted`` (Mini-ADR J-36): the marker is what lets a later
sweep recognise "this workspace was soft-deleted" before it actually
reclaims the storage. That hard-delete / archive step is wave 3's job, not
this store's — the underlying files stay on disk until the archive chain
runs.

**Why the marker is NOT in the user's tree** (wave 2 final review, Critical
1). It used to be ``{root}/{tenant}/{user}/.ew-workspace-deleted`` — the
same subtree the sandbox mounts at ``/workspace`` via ``subPath:
"{tenant}/{user}"``. That made the *authoritative record of "this workspace
was soft-deleted"* a file the sandbox itself can create: an agent running
LLM-generated code (or processing an upload carrying a prompt injection)
only had to write a file with that name into its own working directory, and
from then on every ``acquire`` for that ``(tenant, user)`` — including warm
reuse — was refused by ``AgentSandboxClient``'s soft-delete gate, with wave
3's archive/hard-delete sweep treating the workspace as reclaimable. A
filename blacklist on :meth:`write_file` / :meth:`delete_file` (which this
module used to carry) cannot close that: the sandbox writes the NAS tree
*directly over NFS* and never passes through this store at all. The only
structural fix is for the marker to live somewhere no ``subPath`` ever
projects into a sandbox, so :func:`workspace_deleted_marker` puts it at
``{root}/{tenant}/{DELETED_DIR}/{user}`` — a sibling of the per-user
directories, one level up from anything mounted. With the marker out of
reach, the blacklist is gone too: a file named ``.ew-workspace-deleted``
inside a user's workspace is now an ordinary file with an odd name, and
refusing to write or delete it would only be a behaviour divergence from
``SupervisorWorkspaceStore`` (which has no such rule) for no protection in
return. :meth:`list_files` does not hide it either, for the same reason
(wave 2 final re-review, New 2): this store carried a browse-view filter on
that one name that ``SupervisorWorkspaceStore`` never had, so the *same*
user file was visible on the docker backend and silently invisible on the
NAS one. Hiding a user's own file to keep a platform-looking name off the
screen is the weaker half of that trade — the name carries no meaning any
more — and a per-backend browse filter is exactly the kind of split this
module's parity contract exists to forbid. Only the reserved ``skills/`` /
``uploads/`` prefixes are filtered, and both backends filter those through
the same :func:`is_reserved_workspace_path`.

**TOCTOU note.** The NAS volume is the same tree a sandbox mounts (subPath-
scoped to its own ``{tenant_id}/{user_id}``) and *runs untrusted code
against* — a malicious run sharing this control-plane's view of the wider
tree can plant a symlink anywhere under its own subtree to redirect a
later operation outside it (a cross-tenant escape, not just a same-user
footgun). An earlier version of this module validated a path once with
``Path.resolve()`` (following symlinks) and then reopened it by
**re-walking the same string path** for the actual ``mkdir`` / ``open`` /
``unlink`` — even with a freshly-repeated check immediately beforehand, the
kernel still resolves *every* intermediate component of that string from
scratch on the follow-up syscall, so a concurrent writer racing in a
symlink for *any* intermediate component (not just the final one) between
the check and the operation was never actually closed off; a symlink at the
final component only narrows the window, it does not eliminate it. That
includes ``delete_file``: ``unlink()`` never dereferences a symlink at its
*final* component, but it does dereference symlinks in every component
*before* the final one while resolving the string path — so a mid-chain
swap turns ``delete_file`` into a cross-tenant arbitrary-delete primitive
just as surely as it turns ``write_file``/``read_file`` into a cross-tenant
arbitrary-write/read primitive. (An earlier revision of this note claimed
``delete_file`` was structurally immune for this reason; that reasoning
only covered the final component and was wrong about the intermediate
ones — corrected here.)

The actual fix is to never re-walk a string path at all.
:meth:`_open_parent_dir_fd` resolves ``path`` one component at a time using
``dir_fd``-relative ``openat()`` (:func:`os.open` with ``dir_fd=``),
starting from a directory fd opened for the trusted ``{root}/{tenant_id}/
{user_id}`` prefix (``tenant_id``/``user_id`` are UUIDs from the
authenticated caller, never attacker-controlled path text, so opening that
prefix via a plain path string needs no extra guarding — matching how the
sandbox's own subPath mount is scoped to exactly this same prefix). Each
step opens with ``O_NOFOLLOW`` — a symlink at *that* component makes the
``openat()`` itself fail (``ELOOP``) rather than being followed — and, once
opened, a directory fd is *pinned to the inode it was opened from*: nothing
that happens afterwards to that name in its parent (a rename, an unlink, a
symlink swapped in under the same name) can redirect operations already
using that fd. The final read / write / delete all happen relative to the
last fd in the chain (``os.open(name, ..., dir_fd=parent_fd)`` /
``os.unlink(name, dir_fd=parent_fd)``), so there is no remaining step that
re-resolves a string path — the class of race this note describes has no
foothold left, for any of read/write/delete, at any path depth. This is not
airtight against every conceivable race (e.g. a mkdir-then-immediate-reopen
retry inside :meth:`_openat_dir` when creating a missing directory is two
syscalls, not one — but that reopen also carries ``O_NOFOLLOW``, so even
that narrow window fails closed rather than open), but it eliminates the
specific mechanism (re-walking a string path) that made the previous
version's re-checks ineffective.

:meth:`list_files` only reads metadata, but it needs the same pinning. A
path-based walk (:func:`os.walk`, even with ``followlinks=False``) checks
``islink`` on a subdirectory and *later* lists it by name, and a payload that
keeps swapping a directory in its own workspace for a symlink wins that gap:
the walk then lists whatever the symlink points at, up to the root of the NAS
volume, i.e. other tenants' file names. So it walks with :func:`os.fwalk`
from the scope's own fd with ``follow_symlinks=False``: each subdirectory is
``lstat``-ed, opened, and dropped unless the opened fd is the same inode.
A symlink placed as a plain file entry still appears under its own in-tree
relative path, with its own :func:`os.lstat` size, never a stat of its target.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
import shutil
import stat
from collections.abc import Container, Generator, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

from expert_work.persistence import (
    WORKSPACE_AGENTS_DIR,
    is_delete_protected_workspace_path,
    is_reserved_workspace_path,
)
from expert_work.persistence.workspace.layout import (
    WORKSPACE_CONVERSATION_WORK_DIR,
    WORKSPACE_SEED_MARKER,
    WORKSPACE_THREADS_DIR,
)
from orchestrator.tools.sandbox import (
    SandboxSupervisorError,
    WorkspaceFileChangedError,
    WorkspaceFileExistsError,
    WorkspaceFileNotFoundError,
    WorkspaceFileTooLargeError,
    WorkspaceNotADirectoryError,
    WorkspaceNotAFileError,
    WorkspaceNotARegularFileError,
    WorkspacePathEscapeError,
    WorkspacePermissionError,
)
from orchestrator.tools.workspace_scope import (
    SCOPE_USER_ROOT,
    normalize_workspace_path,
    scope_hidden_heads,
    scope_parts,
    scoped_dir,
    scoped_path,
)
from orchestrator.tools.workspace_store import (
    WorkspaceDirEntry,
    WorkspaceDirListing,
    WorkspaceFileEntry,
    WorkspaceFileListing,
    WorkspaceSearchResult,
)

if TYPE_CHECKING:
    # Only used for the ``runtime``/``instance_store`` fields' types — wave 2
    # Task 4 wires them up (mark_deleted tearing down a warm sandbox
    # session). Deferred behind TYPE_CHECKING so this module never needs a
    # real import path into ``orchestrator.tools.sandbox`` /
    # ``orchestrator.tools.sandbox_instance_store`` at runtime, keeping the
    # modules free to evolve independently.
    from orchestrator.tools.sandbox import SandboxRuntime
    from orchestrator.tools.sandbox_instance_store import SandboxInstanceStore

logger = logging.getLogger(__name__)

#: Per-tenant soft-delete marker directory (see module docstring "Why the
#: marker is NOT in the user's tree"). One empty file per soft-deleted user:
#: ``{root}/{tenant_id}/{DELETED_DIR}/{user_id}``. Deliberately not a UUID
#: and not a sandbox mount target — it sits *beside* the per-user
#: directories, which are the only thing ``subPath`` ever projects into a
#: sandbox, so nothing running inside a sandbox can reach it. Wave 3's
#: archive / hard-delete sweep reads this directory, not the user tree.
DELETED_DIR = ".deleted"

#: Per-file download cap — mirrors
#: ``sandbox_supervisor.supervisor._MAX_ARTIFACT_BYTES``. 64 MiB(原 10 MiB):
#: 沙箱经 NFS 直写工作区没有大小限制,内嵌视频的 pptx 一类产物轻松越过
#: 10 MiB,而下载是这类文件离开工作区的唯一通道 —— 闸必须容得下 agent 实际
#: 会产出的东西。64 MiB 对齐 W3 归档链 put_stream 的单段上限;整个文件仍是
#: 一次性读进内存再回给客户端,再往上调之前先把这条路径改成流式。
_MAX_READ_BYTES = 64 * 1024 * 1024

#: Document-upload write cap — mirrors
#: ``sandbox_supervisor.supervisor._MAX_WORKSPACE_WRITE_BYTES``.
_MAX_WRITE_BYTES = 25 * 1024 * 1024

#: Workspace-browse listing cap — mirrors
#: ``sandbox_supervisor.supervisor._MAX_WORKSPACE_LIST_ENTRIES``.
_MAX_LIST_ENTRIES = 2000

#: ``search_files`` 默认返回多少条 —— 超了带 ``truncated=True``。
DEFAULT_SEARCH_RESULTS = 50

#: ``search_files`` 按内容搜时的单文件读取上限(1 MiB)。超过这个大小的文件跳过
#: 而不是读进来:一次搜索会打开一整棵子树, 没有这条闸一个大文件就能把 control
#: plane 的内存吃掉, 而按内容搜产物脚本这类东西 1 MiB 绰绰有余。
_MAX_SEARCH_FILE_BYTES = 1024 * 1024

#: Mode for every directory this store creates — ``rwx------``. Both readers
#: of this tree (control-plane and the sandbox's ``agent`` process) now run
#: as the same uid (workspace-gid-sharing design § 六 "方向变更"), so the
#: owner bits alone are enough; there is no other uid left to grant access to.
_DIR_MODE = 0o700

#: Mode for new leaf files written through :meth:`NasWorkspaceStore.write_file`
#: — ``rw-------``. Same reasoning as :data:`_DIR_MODE`: the only reader is
#: the process that wrote it (or, now, the same uid running as the other
#: service). **Not** used by :meth:`NasWorkspaceStore.mark_deleted` — the
#: soft-delete marker is created with ``Path.touch()`` (process umask
#: applies, typically ``0o644``), not through this constant; its
#: reachability is protected by the *parent directory*'s ``0o700`` instead
#: (owner-only — see that method), not by the leaf file's own mode.
_LEAF_FILE_MODE = 0o600


class _WorkspacePathNotFoundError(SandboxSupervisorError):
    """A path component genuinely doesn't exist — distinct from an escape attempt.

    Internal to this module. :meth:`NasWorkspaceStore._open_parent_dir_fd`
    raises this (rather than a bare :class:`SandboxSupervisorError`) when a
    component is simply missing, so :meth:`NasWorkspaceStore.delete_file`
    can catch *specifically this* to implement ``rm -f`` semantics without
    also swallowing an escape attempt (which raises the plain
    :class:`SandboxSupervisorError` this subclasses, and must still
    propagate). Every other caller doesn't need to tell the two apart — this
    is still an ordinary :class:`SandboxSupervisorError` to them.
    """


#: B-236 —— 「目录不存在」在各个 store 里的两种形态:NAS 对不存在的目录抛本模块内部的
#: :class:`_WorkspacePathNotFoundError`,supervisor 后端抛 ``WorkspaceFileNotFoundError``。
#: 新对话的目录要到第一次 exec(或开场的基础目录复制)才建出来,它的根是空的,不是故障。
MISSING_DIR_ERRORS = (WorkspaceFileNotFoundError, _WorkspacePathNotFoundError)


#: review 9 —— 按 fd 遍历(``list_files`` / ``search_files``)最多下到第几层。``fwalk`` 每深一层
#: 同时多握一个目录 fd,沙箱能造任意深的目录链;64 远超真实工作区(实测最深 5 层)。
MAX_WALK_DEPTH = 64


def _walk_depth(dirpath: str) -> int:
    """``fwalk(".")`` 给的 ``dirpath`` 有几层:``"."`` 是 0,``"./a"`` 是 1。"""
    return 0 if dirpath == "." else dirpath.count("/")


def _is_conversation_work(prefix: tuple[str, ...]) -> bool:
    """作用域是不是某个对话的 ``agents/<k>/threads/<conv>/work``。"""
    return (
        len(prefix) == 5
        and prefix[0] == WORKSPACE_AGENTS_DIR
        and prefix[2] == WORKSPACE_THREADS_DIR
        and prefix[4] == WORKSPACE_CONVERSATION_WORK_DIR
    )


def _is_uuid_name(name: str) -> bool:
    """规范写法的 UUID(对话目录名都是 ``str(UUID)``)。"""
    try:
        return str(UUID(name)) == name
    except ValueError:
        return False


def _in_conversation_work(parts: tuple[str, ...]) -> bool:
    """用户根相对路径的各段是不是 ``agents/<k>/threads/<对话 UUID>/work/<…>`` 下的一个文件。"""
    return (
        len(parts) >= 6
        and parts[0] == WORKSPACE_AGENTS_DIR
        and parts[2] == WORKSPACE_THREADS_DIR
        and parts[4] == WORKSPACE_CONVERSATION_WORK_DIR
        and _is_uuid_name(parts[3])
    )


def _is_seed_marker(path: str) -> bool:
    """``agents/<k>/threads/<conv>/.seeded`` —— 用户根相对路径。"""
    parts = path.split("/")
    return (
        len(parts) == 5
        and parts[0] == WORKSPACE_AGENTS_DIR
        and parts[2] == WORKSPACE_THREADS_DIR
        and parts[4] == WORKSPACE_SEED_MARKER
    )


#: B-236 review 9 —— ``.seeded`` 标记读多大就不读了(按 500 个文件的清单量出来的上限,留足余量)。
#: 超过 = 当没有清单,什么都不藏。
MAX_SEED_MARKER_BYTES = 256 * 1024

#: review 10 —— 一次列表最多读这么多字节的 ``.seeded`` 标记(所有对话合计)。超了之后的对话
#: 按「没有清单」处理:出错的方向是多列,不是多占内存。正常一个标记几 KB。
MAX_LISTING_MANIFEST_BYTES = 4 * 1024 * 1024

#: 对话目录 ``agents/<k>/threads/<conv>`` → 开场复制的清单:``work/`` 相对路径 → (大小, ctime)。
SeedManifests = dict[str, dict[str, tuple[int, int]]]


def parse_seed_manifest(
    data: bytes, keep: Container[str] | None = None
) -> dict[str, tuple[int, int]] | None:
    """``.seeded`` 标记里的复制清单;格式不对 / 没有清单 → ``None``(什么都不藏)。

    ``keep`` 给了就只留这些路径(review 10:列表只留这次真走到的路径,不整份留在内存里)。
    标记在沙箱够得着的地方:嵌套极深(``[[[…``)的 JSON 会 ``RecursionError``,也按坏了处理。
    """
    try:
        doc = json.loads(data)
    except (ValueError, RecursionError, TypeError):
        return None
    files = doc.get("files") if isinstance(doc, dict) else None
    if not isinstance(files, list):
        return None
    out: dict[str, tuple[int, int]] = {}
    for item in files:
        if (
            isinstance(item, list)
            and len(item) == 3
            and isinstance(item[0], str)
            and type(item[1]) is int
            and type(item[2]) is int
            and (keep is None or item[0] in keep)
        ):
            out[item[0]] = (item[1], item[2])
    return out


def _read_seed_manifest(
    conv_fd: int, keep: Container[str] | None = None, budget: list[int] | None = None
) -> dict[str, tuple[int, int]] | None:
    """在已经钉住的 ``threads/<conv>/`` 目录 fd 上读标记的清单。

    没有、不是普通文件、超过 :data:`MAX_SEED_MARKER_BYTES`、读不动、解析不了 —— 一律 ``None``:
    认副本只是为了少列,出错的方向必须是多列(review 9)。``O_NONBLOCK`` 防 FIFO。

    ``budget``(review 10)是这次列表还剩多少字节可读(单元素列表,读了就扣);标记比剩下的大就
    不读,按没有清单处理。
    """
    try:
        fd = os.open(
            WORKSPACE_SEED_MARKER,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=conv_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("nas_workspace_store.seed_marker_unreadable error=%s", exc.strerror)
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SEED_MARKER_BYTES:
            return None
        if budget is not None:
            if info.st_size > budget[0]:
                budget[0] = 0  # 之后的一律不读(先到先得,不挑小的读)
                return None
            budget[0] -= info.st_size
        data = os.read(fd, MAX_SEED_MARKER_BYTES + 1)
    except OSError as exc:
        logger.warning("nas_workspace_store.seed_marker_unreadable error=%s", exc.strerror)
        return None
    finally:
        os.close(fd)
    if len(data) > MAX_SEED_MARKER_BYTES:
        return None
    return parse_seed_manifest(data, keep)


def is_pristine_seed_copy(
    manifest: Mapping[str, tuple[int, int]], rel: str, entry: WorkspaceFileEntry
) -> bool:
    """``rel``(``work/`` 相对)在清单里,且现在的大小与 ctime 都与清单记下的相同。"""
    recorded = manifest.get(rel)
    return recorded is not None and recorded == (entry.size, entry.ctime_ns)


def _drop_seed_copies(
    prefix: tuple[str, ...],
    entries: list[WorkspaceFileEntry],
    manifests: SeedManifests,
) -> list[WorkspaceFileEntry]:
    """剔掉开场复制进对话、之后**没被碰过**的副本(review 9 的判据)。

    ``agents/<k>/threads/<conv>/work/<相对路径>`` 的文件,当且仅当它在本对话 ``.seeded`` 标记的
    复制清单里、且现在的大小与 ``st_ctime_ns`` 都与清单记下的相同,才是原样副本。清单记的是复制
    写出那一刻(保留 mtime 的 ``utime`` 之后)的 ctime;之后对它的任何写入、改权限、``touch`` 都会
    让 ctime 对不上。ctime 由文件系统(NFS 上是服务端)设,用户态改不回去。

    * 不在清单里的从不藏:复制失败之后模型自己写的、改了名的(``mv customers clients``)都照常列。
    * 不跟 ``/agent`` 的现状比:源文件之后被改,老对话的副本照样是副本;对话自己的产出被
      ``cp -a`` 进 ``/agent``,对话里那份也不会被当成副本。
    * 没有清单(没有标记、标记坏了)的对话里一个都不藏。清单在沙箱够得着的地方:伪造它只影响
      这个用户自己这个对话的列表。
    """
    if not manifests:
        return entries
    kept: list[WorkspaceFileEntry] = []
    for entry in entries:
        parts = (*prefix, *entry.path.split("/"))
        if _in_conversation_work(parts):
            manifest = manifests.get("/".join(parts[:4]))
            if manifest and is_pristine_seed_copy(manifest, "/".join(parts[5:]), entry):
                continue
        kept.append(entry)
    return kept


def _identity(info: os.stat_result) -> tuple[int, int, int]:
    """``read_file(stable=True)`` 用来判「读的过程中文件变没变」的三项:大小 / mtime / inode。"""
    return (info.st_size, info.st_mtime_ns, info.st_ino)


def _openat_dir(dfd: int, name: str, *, create: bool) -> int:
    """``openat(dfd, name, O_DIRECTORY | O_NOFOLLOW)``, optionally creating ``name`` first.

    Never follows a symlink at ``name`` — if the concurrent-writer race the
    module docstring describes has swapped it for one, this raises
    ``OSError(errno=ELOOP)``. ``create=True`` makes the directory first
    (``mkdirat``) when it doesn't exist yet, then retries the same
    ``O_NOFOLLOW`` open — so even a symlink raced in during that narrow
    create-then-reopen gap still fails closed.

    方向变更(共享 gid → 统一 uid,见
    ``docs/superpowers/specs/2026-08-08-workspace-gid-sharing-design.md``
    § 六)—— 一个这个分支带出来的目录,
    在刚拿到手的 fd 上 ``fchmod`` 到 :data:`_DIR_MODE`(``0o700``,不需要
    名字,只作用在已经握着的 fd 上,不重走字符串路径)。control-plane 与
    沙箱里的 agent 现在是同一个 uid,谁创建这个目录都是它的属主,不需要再
    对"另一侧"开任何口子 —— 不需要 ``chown``,不需要 setgid,``other`` 位
    也不需要保留。

    ``os.mkdir``'s own ``mode=`` argument is masked by this process's
    umask before the directory is actually created (typically leaves
    ``0o755``) — this is why every layer needs an explicit ``fchmod``
    rather than relying on inheritance. Reached this fixed mode
    unconditionally whenever the directory didn't already exist a moment
    ago (whether this call's own ``mkdir`` won or a concurrent same-process
    caller's did, both are "this process just brought it into being") —
    a directory that already existed before this call (the ``O_NOFOLLOW``
    fast path above) is left untouched: fixing modes on file/directory
    *is not* what this store is responsible for, only what it *creates*.
    """
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, dir_fd=dfd)
        except FileExistsError:
            # A concurrent same-process caller won the race and created it
            # between our failed open and this mkdir. Nothing to do: the
            # directory we wanted now exists, and the reopen below (still
            # ``O_NOFOLLOW``) is what decides whether it is really a
            # directory and not a symlink swapped in under the same name.
            pass
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
        # 已知不修(2026-08-08 方向变更终审 Minor):``fchmod`` 抛的话这个 ``fd``
        # 会泄。要它发生得有另一个 uid 抢先建出同名目录、让我们既 open 得到又
        # chmod 不动——统一 uid(``docs/superpowers/specs/2026-08-08-workspace-
        # gid-sharing-design.md`` § 六)之后写这棵树的只有一个 uid,这条路
        # 不可达。哪天再引入第二个写入身份,连同这里一起补 try/finally。
        os.fchmod(fd, _DIR_MODE)
        return fd


def _is_symlink_at(dfd: int, name: str) -> bool:
    """``name`` under ``dfd`` — is it a symlink? Asked once, only on the error path.

    ``O_NOFOLLOW`` 撞 symlink 时的 errno **不跨平台**:Linux 回 ``ELOOP``, 而 macOS
    对 ``O_DIRECTORY | O_NOFOLLOW`` 回 ``ENOTDIR``(2026-09-20 实测, 不是推断)。
    ``ENOTDIR`` 同时也是"``a/b`` 里的 ``a`` 其实是个普通文件"这种正常笔误的 errno,
    所以拿 errno 当判据只有两种错法:在 macOS 上把一次逃逸报成"不存在", 或者把笔误
    报成逃逸。改成问一次 ``lstat``——"它是不是一条链接"这件事两个平台答案一样。

    生产跑 Linux, 那里 ``ELOOP`` 本来就接住了;这条判据是为了让**本地与 CI 上的红
    绿**说的是同一件事 —— 一个只在 Linux 上成立的判据, 在开发机上永远验不出它想验
    的东西。
    """
    try:
        return stat.S_ISLNK(os.lstat(name, dir_fd=dfd).st_mode)
    except OSError:
        return False


def _walk_and_match(
    dfd: int,
    *,
    prefix: tuple[str, ...],
    name_glob: str | None,
    content: str | None,
    max_results: int,
    hidden: frozenset[str] = frozenset(),
) -> WorkspaceSearchResult:
    """The body of :meth:`NasWorkspaceStore.search_files` — walk under ``dfd``, match, cap.

    Every step is ``dir_fd``-relative: :func:`os.fwalk` hands back the directory
    fd it is currently in, and both the ``lstat`` and the content ``open`` ride
    it with ``O_NOFOLLOW``. 一条指向作用域外的 symlink 因此既进不了结果(它不是
    普通文件), 也读不出内容(``ELOOP``)。

    ``follow_symlinks=False`` keeps the walk itself from descending through a
    symlinked subdirectory, the same as :meth:`NasWorkspaceStore.list_files`.
    """

    def _on_error(exc: OSError) -> None:
        # 同 list_files 的 ``_on_walk_error``:扫不动的子树必须炸, 不能静默漏掉一
        # 部分结果 —— "搜不到"与"没搜到"在返回里长得一模一样, 而它们的处置相反。
        if isinstance(exc, PermissionError):
            raise WorkspacePermissionError("workspace search not readable") from exc
        raise SandboxSupervisorError(f"workspace search failed: {exc.strerror}") from exc

    hits: list[WorkspaceFileEntry] = []
    truncated = False
    too_deep = False
    # ``closing`` —— 命中上限时我们会 ``break`` 出去, 而 :func:`os.fwalk` 是个持有
    # 目录 fd 的生成器。
    #
    # **勘误(09-20 实测)**:这里原本的注释写的是"不能指望引用计数顺手回收, 一次
    # 搜索泄一把 fd, 跑够多次就是 EMFILE"。**那句话是错的** —— 照真实调用形状
    # (``os.fwalk(".", dir_fd=...)``, 首轮就 ``break`` 抛弃 walker)跑 500 次, 进程
    # 打开的 fd 数一个没涨:CPython 的引用计数在 ``break`` 那一刻就把生成器关了,
    # ``fwalk`` 自己的 ``finally`` 照常跑。
    #
    # 那为什么还留着 ``closing``:它把"提前退出要关掉 walker"写成代码而不是赌一个
    # 实现细节 —— 只要将来有人把 ``walker`` 多存一个引用(塞进 ``self``、包一层调试
    # 迭代器、挪进 ``try`` 外面), 引用计数就不再在 ``break`` 时归零, 而那种改动不会
    # 有任何测试变红。代价是零(热路径上一次 ``close``)。
    #
    # 别照着"修掉这个多余的 ``closing``"—— 它防的是未来的改动, 不是今天的泄漏。
    # ``cast`` —— typeshed 把 ``os.fwalk`` 标成 ``Iterator``, 而 CPython 给的是
    # 生成器(它有 ``close``, 这正是这里要的东西)。
    walker = cast(
        Generator[tuple[str, list[str], list[str], int], None, None],
        os.fwalk(".", dir_fd=dfd, follow_symlinks=False, onerror=_on_error),
    )
    try:
        with closing(walker):
            for dirpath, dirnames, filenames, walk_fd in walker:
                base = PurePosixPath(dirpath)
                if dirpath == ".":
                    # B-236 —— 作用域根下藏起来的首段(``agent_shared`` 的 ``threads/``)
                    # 连走都不走进去:别的对话的文件不该出现在任何一条结果里。
                    dirnames[:] = [d for d in dirnames if d not in hidden]
                if dirnames and _walk_depth(dirpath) >= MAX_WALK_DEPTH:
                    # review 9 —— 再往下不走:fwalk 每深一层多握一个目录 fd。结果标成 ``too_deep``
                    # (review 10 起与 ``truncated`` 分开;也不在这里停整个遍历,别的分支照搜)。
                    dirnames[:] = []
                    too_deep = True
                for name in sorted(filenames):
                    rel = str(base / name).removeprefix("./")
                    if is_reserved_workspace_path("/".join((*prefix, rel))):
                        continue
                    if name_glob is not None and not _name_matches(rel, name, name_glob):
                        continue
                    try:
                        info = os.lstat(name, dir_fd=walk_fd)
                    except OSError:
                        continue  # racing unlink — a miss, not a failure.
                    if not stat.S_ISREG(info.st_mode):
                        continue  # symlink / fifo / socket — never read through it.
                    if content is not None and not _content_matches(walk_fd, name, info, content):
                        continue
                    if len(hits) >= max_results:
                        truncated = True
                        break
                    hits.append(_entry(rel, info))
                if truncated:
                    break
    except PermissionError as exc:
        # 同 list_files:fwalk 对已开目录的 scandir / fstat 失败不走 onerror。
        raise WorkspacePermissionError("workspace search not readable") from exc
    except OSError as exc:
        raise SandboxSupervisorError(f"workspace search failed: {exc.strerror}") from exc
    hits.sort(key=lambda entry: entry.path)
    return WorkspaceSearchResult(entries=tuple(hits), truncated=truncated, too_deep=too_deep)


def _list_dir_entries(
    prefix: tuple[str, ...],
    dirpath: str,
    filenames: list[str],
    walk_fd: int,
    entries: list[WorkspaceFileEntry],
    *,
    marker_dirs: set[str] | None = None,
) -> None:
    """One directory of :meth:`NasWorkspaceStore.list_files`' walk, ``dir_fd``-relative.

    B-236 —— ``threads/<conv>/.seeded`` 标记不列(平台状态),它所在的对话目录记进
    ``marker_dirs``(review 10:遍历时不读,遍历完只读本次真走到 ``work/`` 文件的那些对话的)。
    """
    base = PurePosixPath(dirpath)
    for name in filenames:
        rel = str(base / name).removeprefix("./")
        # 保留前缀的判据吃的是**用户根相对**路径 ——
        # ``is_reserved_workspace_path`` 自己会剥掉 ``agents/<key>``
        # 这层容器再看头一段(见 ``persistence/workspace/layout.py``)。
        # 喂作用域相对路径进去在今天恰好也对, 但那是巧合不是契约:
        # 换一个容器层级就静默错位, 而错位的形态是"文件凭空消失"。
        if is_reserved_workspace_path("/".join((*prefix, rel))):
            continue
        # lstat, not stat — a symlink appearing as a plain file entry must
        # report its own byte length, never a stat() of whatever it points at
        # outside the tree (see module docstring). B-84 —— 一次 lstat 同时取大小
        # 与 mtime: 这个循环在一棵几千文件的树上跑, 每多一次系统调用就是一次 NFS 往返。
        try:
            info = os.lstat(name, dir_fd=walk_fd)
        except PermissionError as exc:
            # 同 read_file:列不动 ≠ 不存在,不能被下面吞掉。
            raise WorkspacePermissionError(f"workspace listing not readable: {rel!r}") from exc
        except OSError as exc:
            # 复审 N-2 —— 错误文案只带工作区相对路径, 不带 ``exc`` 自己的 filename。
            raise SandboxSupervisorError(
                f"workspace listing failed: {rel!r}: {exc.strerror}"
            ) from exc
        full = "/".join((*prefix, rel))
        if name == WORKSPACE_SEED_MARKER and _is_seed_marker(full):
            if marker_dirs is not None:
                marker_dirs.add(full.rsplit("/", 1)[0])
            continue  # B-236 —— 开场复制的标记是平台状态,不是产出
        entries.append(_entry(rel, info))


def _name_matches(rel: str, name: str, name_glob: str) -> bool:
    """``*.py`` 匹配文件名, ``style/*.py`` 匹配作用域相对路径。

    两种都认是有意的:模型写 glob 时两种都会写, 而"你的写法没命中"与"确实没有这
    个文件"在返回里长得一模一样 —— 正是 B-84 要治的那类误判。
    """
    return fnmatch(name, name_glob) or fnmatch(rel, name_glob)


def _content_matches(walk_fd: int, name: str, info: os.stat_result, needle: str) -> bool:
    """Substring match against a text file's contents, ``dir_fd``-relative.

    Binary files are skipped rather than decoded (``errors="replace"`` would
    manufacture matches that are not there), and anything over
    :data:`_MAX_SEARCH_FILE_BYTES` is skipped rather than pulled into memory —
    one pathological file must not be able to OOM the control plane.
    """
    if info.st_size > _MAX_SEARCH_FILE_BYTES:
        return False
    try:
        # review 7 —— lstat 之后名字可能被换成 FIFO:``O_NONBLOCK`` 不等写端,fstat 再核一遍。
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=walk_fd)
    except OSError:
        return False
    with os.fdopen(fd, "rb") as handle:
        try:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return False
            data = handle.read()
        except OSError:
            return False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return needle in text


def _entry(rel: str, info: os.stat_result) -> WorkspaceFileEntry:
    """One listing row out of a stat result already in hand.

    B-84 —— ``mtime`` comes from the *same* ``lstat`` the size comes from, and
    is normalised to a tz-aware UTC ``datetime``: a naive one cannot be
    compared with anything else in this codebase without a silent local-time
    assumption.
    """
    return WorkspaceFileEntry(
        path=rel,
        size=info.st_size,
        mtime=datetime.fromtimestamp(info.st_mtime, tz=UTC),
        ctime_ns=info.st_ctime_ns,
    )


def scope_root(root: str, tenant_id: UUID, user_id: UUID, scope: str) -> Path:
    """一个作用域在 NAS 上的真实目录(B-84)。

    ``{user_root}`` / ``{user_root}/agents/{agent_key}`` / ``{user_root}/shared`` ——
    前缀由 :func:`~orchestrator.tools.workspace_scope.scope_parts` 给,这里只负责拼到
    用户根上。**只给诊断与测试用**:真正的读路径不拿这个字符串去 ``open``(那就成了
    重走字符串路径,正是模块 docstring 的 TOCTOU 一节说不能做的事),而是从用户根的
    fd 开始一段一段 ``openat``。
    """
    return workspace_user_root(root, tenant_id, user_id).joinpath(*scope_parts(scope))


def workspace_deleted_marker(root: str, tenant_id: UUID, user_id: UUID) -> Path:
    """The soft-delete marker file for one ``(tenant, user)``.

    ``{root}/{tenant_id}/{DELETED_DIR}/{user_id}`` — see the module
    docstring's "Why the marker is NOT in the user's tree". Sibling of
    :func:`workspace_user_root` in every sense: same reason to exist (one
    function owns the on-disk spelling so the writer —
    :meth:`NasWorkspaceStore.mark_deleted` — and the reader —
    ``AgentSandboxClient``'s acquire-time soft-delete gate — can never drift
    apart), and the same trusted inputs (both ids are UUIDs from the
    authenticated caller, never attacker path text).
    """
    return (Path(root) / str(tenant_id) / DELETED_DIR / str(user_id)).resolve()


def workspace_user_root(root: str, tenant_id: UUID, user_id: UUID) -> Path:
    """The canonical per-``(tenant, user)`` NAS path: ``{root}/{tenant_id}/{user_id}``.

    Task 4 review (Minor) — this module owns the on-disk layout, so it also
    owns the one function that spells it out. Before this existed,
    :meth:`NasWorkspaceStore._user_root` and
    :mod:`orchestrator.tools.agent_sandbox`'s pre-mount mkdir/chmod/
    soft-delete-gate each concatenated ``root``/``tenant_id``/``user_id``
    independently — two spellings of the same path that could silently
    drift apart (e.g. one gaining a subpath-prefix segment the other never
    learns about, see that module's ``workspace_subpath_prefix`` guard).
    Both call sites now go through this one function so that class of bug
    is structurally impossible, not just currently absent.
    """
    return (Path(root) / str(tenant_id) / str(user_id)).resolve()


@dataclass
class NasWorkspaceStore:
    """Production :class:`WorkspaceStore` (wave 2) — reads/writes the NAS mount directly.

    ``root`` is the control-plane Pod's local mount point for the shared NAS
    volume (e.g. ``/mnt/workspaces``); every method scopes its filesystem
    access under ``{root}/{tenant_id}/{user_id}`` via
    :meth:`_open_parent_dir_fd`, which is the sole path-traversal guard (see
    that method's docstring and the module docstring's "TOCTOU note"). All
    I/O is dispatched through :func:`asyncio.to_thread` — NFS-backed
    synchronous I/O can block for the duration of a network round-trip, and
    doing that on the event loop would stall every other in-flight run.
    """

    root: str
    #: Wave 2 Task 4 — ``mark_deleted`` uses this (together with
    #: :attr:`instance_store`) to tear down the user's warm sandbox session
    #: after marking the workspace deleted. ``None`` (the wave 1/3 default,
    #: e.g. ``persistence_backend="memory"`` or a unit test that never wires
    #: a sandbox runtime) skips teardown entirely — the marker alone is
    #: still written, so a *later* ``acquire`` is refused (spec § 五之二's
    #: acquire-time soft-delete gate in ``AgentSandboxClient``); this field
    #: only controls whether an *already-warm* session gets pre-emptively
    #: killed.
    runtime: SandboxRuntime | None = None
    #: Wave 2 Task 4 — the same ``sandbox_instance`` store
    #: ``AgentSandboxClient`` uses for its warm-session CAS. ``mark_deleted``
    #: reads :meth:`SandboxInstanceStore.get_warm` through it to find the
    #: sandbox id :attr:`runtime` should ``destroy``. Wired as a *separate*
    #: field rather than reaching through ``runtime`` because
    #: :class:`~orchestrator.tools.sandbox.SandboxRuntime` (the Protocol
    #: ``runtime`` is typed as) has no ``get_warm`` — that method lives on
    #: the store, not the runtime. Both are supplied together by
    #: ``build_workspace_store`` in production; either being ``None`` (not
    #: just both) skips teardown — see :meth:`mark_deleted`.
    instance_store: SandboxInstanceStore | None = None

    def _user_root(self, tenant_id: UUID, user_id: UUID) -> Path:
        return workspace_user_root(self.root, tenant_id, user_id)

    def _open_parent_dir_fd(
        self, tenant_id: UUID, user_id: UUID, path: str, *, create: bool
    ) -> tuple[int, str]:
        """Walk to ``path``'s parent directory via a chain of ``dir_fd``-relative opens.

        ``path`` is validated and canonicalised by
        :func:`~orchestrator.tools.workspace_scope.normalize_workspace_path` — the
        *same* function the callers' reserved-name guards read, so the guard and the walk can never
        disagree about which file a request names (wave 2 final review,
        Critical 2). Every component except the last is then opened one at a
        time with :func:`_openat_dir`, each anchored on the *previous*
        component's already-open directory fd rather than on a re-walked
        string path — see the module docstring's "TOCTOU note" for why that
        distinction is the entire point.

        Returns ``(parent_fd, final_component_name)``; the caller owns
        ``parent_fd`` and must close it. ``create=True`` (``write_file`` /
        ``mark_deleted``) creates the user root and any missing intermediate
        directory as it walks; ``create=False`` (``read_file`` /
        ``delete_file``) never creates anything and raises
        :class:`_WorkspacePathNotFoundError` the moment a component is missing.
        """
        _relpath, parts = normalize_workspace_path(path)
        user_root = self._user_root(tenant_id, user_id)
        if create:
            # ``tenant_id``/``user_id`` are UUIDs from the authenticated
            # caller, not attacker path text — see module docstring — so a
            # plain path-string mkdir/open for this trusted prefix is fine;
            # only the (untrusted) ``parts`` walked below need dir_fd
            # chaining.
            #
            # This also chmods the user root itself, not just intermediate
            # subdirectories — the production repro of W2-BUG-1 was the
            # agent writing MEMORY.md directly at the user-root level, not
            # inside a subdirectory, so the user root's own mode has to be
            # right too.
            #
            # **只在这次 mkdir 真正把目录带入存在时才 chmod**——``exist_ok=
            # True`` 原来会在目录已存在时静默不报错,而下面这段却无条件跟着
            # 跑;``chmod`` 只对*属主*放行,对一个我们不是属主的既存目录
            # (CSI subPath 建的、迁移脚本建的、备份恢复出来的)会 EPERM,而
            # 这整个 try 块的唯一异常出口是把任何 OSError 都翻成 "failed to
            # create workspace directory" —— 一条谎报,目录明明建好了(或者
            # 一直都在),只是我们不该动它的 mode。修存量目录是一次性迁移
            # Job 的职责,这条写路径不该顺手兼职。
            #
            # Wrapped: the most likely way this fails in production is the
            # NAS data root not having been chmod'd by hand (the one manual
            # step in the wave 2 release runbook) — control-plane gets
            # EACCES creating the first tenant subtree. Unwrapped, that
            # surfaces as a bare PermissionError crossing this store's error
            # boundary and a clueless 500 on the upload endpoint; the
            # runbook literally tells the operator "if the first upload
            # after release 500s, check this", which is exactly the signal
            # the error type should have carried in the first place —
            # WorkspacePermissionError (not the generic SandboxSupervisorError)
            # is what lets Task 5's endpoint answer with a diagnosable 500.
            try:
                try:
                    user_root.mkdir(parents=True)
                    created = True
                except FileExistsError:
                    # 已经存在——不管是别的写入方先跑到,还是这棵目录本来
                    # 就在那里(CSI/迁移/恢复带来的),都不是我们创建的,
                    # mode 不归这条路径管。
                    created = False
                if created:
                    # 路径版本的 chmod(不是 _openat_dir 的 fd 版本)——
                    # user_root 是按信任前缀直接开的绝对路径,不经过下面的
                    # dir_fd 链。
                    #
                    # 只 chmod **用户根**,不 chmod 它上面那层 ``{tenant}/``
                    # ——后者由 ``parents=True`` 顺带建出,落的是 umask 决定的
                    # ``0o755``。已知不修(方向变更终审 Minor-5):租户目录里
                    # 只有 UUID 命名的用户子目录和 ``.deleted/``,自身不存任何
                    # 内容,``other`` 的 ``r-x`` 只暴露"这个租户下有哪些 user
                    # UUID",而能读到这一层的进程本来就有整棵树的挂载。真要
                    # 收紧,该在这里补一次 ``chmod(user_root.parent, 0o700)``
                    # 并同步迁移脚本,不是删掉这条注释就算数。
                    os.chmod(user_root, _DIR_MODE)
            except PermissionError as exc:
                # 复审 I-1 —— 同类型的路径泄露:之前这里裸拼 ``{user_root}``,
                # 是服务端 NAS 挂载点的真实文件系统路径。同 list_files 的
                # user_root 自检分支一样用 ``'.'`` 指代"用户自己的工作区根"。
                #
                # 复审 N-2 —— 上一轮只换掉了手拼的 ``{user_root}``,却留着
                # ``: {exc}``:``mkdir``/``chmod`` 都是普通路径调用(不是
                # dir_fd-relative),真实失败时 ``OSError`` 会自己带上
                # ``filename`` 属性,``str(exc)`` 因此原样把绝对路径缝回
                # 消息里(实测坐实:``PermissionError(13, "...", "/abs/path")``
                # 的 ``str()`` 是 ``"... : '/abs/path'"``)——换成
                # ``exc.strerror`` 只留错误原因,不带路径。
                raise WorkspacePermissionError(
                    f"failed to create workspace directory {'.'!r}: {exc.strerror}"
                ) from exc
            except OSError as exc:
                raise SandboxSupervisorError(
                    f"failed to create workspace directory {'.'!r}: {exc.strerror}"
                ) from exc
        dfd = self._open_user_root_fd(user_root, path)
        return self._walk_dir_fd(dfd, parts[:-1], path, create=create), parts[-1]

    def _open_user_root_fd(self, user_root: Path, path: str) -> int:
        """Open the trusted ``{root}/{tenant_id}/{user_id}`` prefix — the walk's anchor."""
        try:
            return os.open(user_root, os.O_RDONLY | os.O_DIRECTORY)
        except PermissionError as exc:
            # 复审 C-1 —— 这句是 read_file/write_file/delete_file 三个方法共用
            # 的入口(list_files 起点的 os.stat 不经过这个方法,
            # 上一版这里的注释把它也算进去是错的,见复审 N-1):落在这里之
            # 前 create=False 的 read_file/delete_file 从不会碰上面的 mkdir
            # 分支,直接从这里第一次触到 user_root。反过来说也一样漏:
            # PermissionError 是 OSError 的子类,顺序反了(把这条挪到下面那
            # 句宽 except 之后)这个分支永远走不到。不接住的后果按调用方分
            # 叉:read_file/write_file 把它收成 _WorkspacePathNotFoundError
            # (SandboxSupervisorError 的子类)→ 端点翻 404,用户看到"文件不
            # 存在"而它其实读不动;delete_file 更糟——它把
            # _WorkspacePathNotFoundError 当 rm -f 语义直接吞掉、返回成功,
            # 用户看到"删除成功"而文件原封不动地留在盘上。
            raise WorkspacePermissionError(f"workspace not readable: {path!r}") from exc
        except OSError as exc:
            raise _WorkspacePathNotFoundError(f"workspace path not found: {path!r}") from exc

    def _walk_dir_fd(
        self, dfd: int, components: tuple[str, ...], path: str, *, create: bool
    ) -> int:
        """Step through ``components`` one ``openat`` at a time, closing each fd behind us.

        B-84 —— 从 :meth:`_open_parent_dir_fd` 里原样提出来(一行逻辑没改),因为
        :meth:`_open_scope_dir_fd` 要走**每一段**而不是 ``parts[:-1]``。提出来而不
        是复制一份:这个循环是整棵树唯一的防穿越闸,第二份就是第二个逃逸面。
        """
        for component in components:
            try:
                nfd = _openat_dir(dfd, component, create=create)
            except PermissionError as exc:
                # 复审 N-1 —— 同上一句(C-1)的坑,只是深了一层:那句只顶住了
                # user_root **自己**打不开的情形,这个循环里 _openat_dir 抛出
                # 的 PermissionError(中间路径分量存在但不可穿透——路径深度
                # ≥ 2 时真实会撞上,比如 ``sub/a.txt`` 里的 ``sub``)之前一样
                # 被下面那句宽 ``except OSError`` 收成 _WorkspacePathNotFoundError。
                # 复现过:``delete_file("sub/a.txt")`` 在 ``sub/`` 不可穿透时
                # 直接返回成功(rm -f 语义把 _WorkspacePathNotFoundError 当
                # "本来就没有"),文件原封不动地留在盘上,而且这次连
                # read_file 事后核实都会答"不存在"——两个诊断都指向错误的
                # 结论。
                os.close(dfd)
                # 措辞刻意比 C-1 那处的 "not readable" 中性:``_openat_dir``
                # 在 ``create=True`` 时可能是 ``mkdir`` 撞 EACCES(父目录写
                # 不进),不一定是"读不动"。这个异常类型的全部意义就是可诊断
                # 性,指错权限位比不指更坏。
                raise WorkspacePermissionError(f"workspace path not accessible: {path!r}") from exc
            except OSError as exc:
                # B-84 —— 窄类型:中间某一段是 symlink 是**安全拒绝**, 不是"文件不
                # 存在"。``file_ops`` 据此翻 ToolBlockedError, 与沙箱片段的
                # ``path_escapes_workspace`` 逐字同义。判据先问再关 fd。
                escaped = exc.errno == errno.ELOOP or _is_symlink_at(dfd, component)
                os.close(dfd)
                if escaped:
                    raise WorkspacePathEscapeError(
                        f"workspace path escapes the user root: {path!r}"
                    ) from exc
                raise _WorkspacePathNotFoundError(f"workspace path not found: {path!r}") from exc
            os.close(dfd)
            dfd = nfd
        return dfd

    def _seed_manifests(
        self,
        tenant_id: UUID,
        user_id: UUID,
        prefix: tuple[str, ...],
        entries: list[WorkspaceFileEntry],
        marker_dirs: set[str],
    ) -> SeedManifests:
        """遍历完之后读开场复制清单(review 10):只读**这次走到了 ``work/`` 文件**的对话,每份只留
        走到的那些路径,所有标记合计最多读 :data:`MAX_LISTING_MANIFEST_BYTES`。

        对话目录名必须是规范写法的 UUID(载荷在 ``threads/`` 下随手建的假目录不读)。标记要么在
        这次遍历里见过(``marker_dirs``),要么作用域就是那个对话的 ``work/``(标记在遍历之外)。
        目录链经 :meth:`_open_scope_dir_fd`;读不动 / 坏了按「没有清单」处理,不放裸
        ``OSError`` 出去(review 9)。
        """
        walked: dict[str, set[str]] = {}
        for entry in entries:
            parts = (*prefix, *entry.path.split("/"))
            if _in_conversation_work(parts):
                walked.setdefault("/".join(parts[:4]), set()).add("/".join(parts[5:]))
        own = "/".join(prefix[:4]) if _is_conversation_work(prefix) else None
        budget = [MAX_LISTING_MANIFEST_BYTES]
        out: SeedManifests = {}
        for conv_dir in sorted(walked):
            if conv_dir != own and conv_dir not in marker_dirs:
                continue
            try:
                dfd = self._open_scope_dir_fd(tenant_id, user_id, conv_dir)
            except _WorkspacePathNotFoundError:
                continue
            try:
                manifest = _read_seed_manifest(dfd, walked[conv_dir], budget)
            finally:
                os.close(dfd)
            if manifest:
                out[conv_dir] = manifest
        return out

    def _open_scope_dir_fd(self, tenant_id: UUID, user_id: UUID, relpath: str) -> int:
        """A directory's **own** fd, not its parent's — backs ``list_dir`` / ``search_files``.

        ``relpath`` is already user-root-relative and already normalised (it came
        out of :func:`~orchestrator.tools.workspace_scope.scoped_dir`); an empty
        string is the user root itself. Same chain, same ``O_NOFOLLOW``, same
        error mapping as :meth:`_open_parent_dir_fd` — the caller owns the fd and
        must close it.
        """
        dfd = self._open_user_root_fd(self._user_root(tenant_id, user_id), relpath or ".")
        parts = PurePosixPath(relpath).parts if relpath else ()
        return self._walk_dir_fd(dfd, parts, relpath or ".", create=False)

    async def read_file(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        path: str,
        scope: str = SCOPE_USER_ROOT,
        max_bytes: int | None = None,
        stable: bool = False,
    ) -> bytes:
        """``stable=True``(B-235,只在本实现上,不进 Protocol)—— 读完再 ``fstat`` 一次,
        与读之前的大小 / mtime / inode 对不上、或读到的字节数不等于读前的大小,就抛
        :class:`WorkspaceFileChangedError`:产物副本不能是别的写入者写到一半的文件。
        """

        def _read() -> bytes:
            rel = scoped_path(scope, path)
            cap = _MAX_READ_BYTES if max_bytes is None else min(_MAX_READ_BYTES, max_bytes)
            dfd, name = self._open_parent_dir_fd(tenant_id, user_id, rel, create=False)
            try:
                # O_NOFOLLOW — a symlink planted for the exact leaf name
                # makes this open fail (ELOOP) instead of silently reading
                # through it. ``dfd`` is pinned to the parent directory's
                # inode (see module docstring), so nothing that happened to
                # any *earlier* path component after it was opened can
                # redirect this call.
                try:
                    # review 7 —— ``O_NONBLOCK``:沙箱能在这个名字上种一个 FIFO,不带它
                    # ``open`` 会一直等写端,把一条执行器线程永久卡住(每个新对话开场
                    # 读一次记忆索引就卡一条)。普通文件不理会这个标志;下面 ``fstat`` 拒
                    # 一切不是普通文件的东西。
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
                except PermissionError as exc:
                    # W2-BUG-1 —— 读不动 ≠ 不存在。合到下面那句 SandboxSupervisorError
                    # 里的话,端点翻成 404,用户看到"文件不存在"而它明明列在
                    # 上一屏,只能靠翻服务端日志才诊断得出来。PermissionError
                    # 是 OSError 的子类,必须先接住(见模块级 import 处
                    # WorkspacePermissionError 的说明) —— 顺序反了这句永远
                    # 走不到,下面的宽 except OSError 会先吃掉它。
                    raise WorkspacePermissionError(
                        f"workspace file not readable: {path!r}"
                    ) from exc
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        # B-84 —— 窄类型:一段是 symlink 的 ``openat`` 撞 ELOOP 是
                        # **安全拒绝**, 不是"文件不存在"。``file_ops`` 据此翻
                        # ToolBlockedError, 与沙箱片段的 ``path_escapes_workspace``
                        # 逐字同义。
                        raise WorkspacePathEscapeError(
                            f"workspace path escapes the user root: {path!r}"
                        ) from exc
                    raise WorkspaceFileNotFoundError(f"workspace file not found: {path!r}") from exc
            finally:
                os.close(dfd)
            with os.fdopen(fd, "rb") as handle:
                # Stat before reading so an over-cap file never gets fully
                # loaded into memory — the NFS mount has no equivalent to
                # the supervisor's bounded ``head -c`` subprocess trick.
                try:
                    before = os.fstat(handle.fileno())
                except OSError as exc:
                    raise WorkspaceFileNotFoundError(f"workspace file not found: {path!r}") from exc
                if stat.S_ISDIR(before.st_mode):
                    # 「它是目录」,不是「它不存在」—— 与下面 ``IsADirectoryError`` 同一个窄类型。
                    raise WorkspaceNotAFileError(f"workspace path is not a file: {path!r}")
                if not stat.S_ISREG(before.st_mode):
                    # FIFO / 设备 / socket:不读(review 8 —— 别说它是目录)。
                    raise WorkspaceNotARegularFileError(
                        f"workspace path is not a regular file: {path!r}"
                    )
                size = before.st_size
                if size > cap:
                    # 「太大」≠「不存在」—— 窄类型让下载端点能回 413 而不是把
                    # 一个明明列在产物列表里的文件谎报成 404(同
                    # WorkspacePermissionError 的拆分理由)。
                    msg = f"workspace file {path!r} exceeds the {cap}-byte read cap"
                    raise WorkspaceFileTooLargeError(msg)
                try:
                    # review 10 —— 最多读 cap + 1:fstat 之后文件还在长,也不会读进超过上限的量。
                    data = handle.read(cap + 1)
                except IsADirectoryError as exc:
                    # B-84 —— "它是个目录"不是"它不存在":模型的下一步动作不同
                    # (换个路径 vs 改用 list_dir), 沙箱片段一直把这两件事分成
                    # ``is_a_directory`` 与 ``not_found`` 两种 envelope。仍是
                    # SandboxSupervisorError 的子类, 下载端点照旧 404。
                    raise WorkspaceNotAFileError(f"workspace path is not a file: {path!r}") from exc
                except OSError as exc:
                    raise WorkspaceFileNotFoundError(f"workspace file not found: {path!r}") from exc
                if len(data) > cap:
                    msg = f"workspace file {path!r} exceeds the {cap}-byte read cap"
                    raise WorkspaceFileTooLargeError(msg)
                if stable:
                    after = os.fstat(handle.fileno())
                    if len(data) != size or _identity(after) != _identity(before):
                        msg = f"workspace file {path!r} changed while it was being read"
                        raise WorkspaceFileChangedError(msg)
                return data

        return await asyncio.to_thread(_read)

    async def list_files(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        scope: str = SCOPE_USER_ROOT,
        include_seed_copies: bool = False,
    ) -> list[WorkspaceFileEntry]:
        """B-236 —— 默认不列开场复制进对话、之后没被碰过的副本(见 :func:`_drop_seed_copies`),
        也不列 ``.seeded`` 标记;先剔再截 2000 条。``include_seed_copies=True`` 照旧全列(工作区
        快照段要拿两份清单的差认出副本)。"""

        def _list() -> WorkspaceFileListing:
            # B-84 —— 作用域只改**起点**, 不改走路的方式。遍历的根从用户根挪到
            # ``{user_root}/agents/<key>`` 之类, 其余一个字不动;``scope_parts``
            # 已经把 agent_key 过了同一道安全闸, 这里拼的每一段都是校验过的。
            # 回给调用方的 ``path`` 是**作用域相对**的 —— 默认作用域是用户根,
            # 所以浏览端点 / 产物下载那批既有调用方看到的东西逐字不变。
            prefix = scope_parts(scope)

            # review 8 —— 作用域起点只按 fd 开(逐段 O_NOFOLLOW,``_open_scope_dir_fd``),不再先
            # 拿路径字符串 ``os.stat`` 一遍:不存在 / 不是目录 → 空;某一段是符号链接 →
            # ``WorkspacePathEscapeError``(载荷把自己的 ``work/`` 换成指向上层的链接,列出来的
            # 就是别的租户的文件名);读不动 → ``WorkspacePermissionError``。
            def _on_walk_error(exc: OSError) -> None:
                """The walk's ``onerror`` callback — Task 3 fix round 1.

                ``os.walk`` / ``os.fwalk`` 默认 ``onerror=None``:一个扫不动的子树(典型是
                ``EACCES``)会被**静默吞掉**——那棵子树下的文件从结果里凭空消
                失,不报错也不留任何痕迹,而这恰恰是"列不动"最常见的形态,比
                单个文件 ``lstat`` 失败常见得多(下面那处 ``try/except`` 只挡
                得住后者)。``control_plane/api/workspace.py`` 的列表端点只接
                :class:`SandboxSupervisorError`,把它翻成 ``{"success": true,
                "files": []}`` —— 一次真实的权限故障被这层静默吞声悄悄变成
                "工作区是空的",正是这整个任务要根治的那类失败。传给
                walk 的 ``onerror=`` 让这类错误显式地把这次调用整个炸
                掉,而不是悄悄漏掉一部分结果。

                复审 I-1 —— 全局约束"用户可见的错误文案不含服务端绝对路径"。
                改成 ``os.fwalk`` 之后 ``exc.filename`` 是相对当前目录 fd 的名
                字(不再是 NAS 挂载点下的绝对路径), 直接用即可。
                """
                if isinstance(exc, PermissionError):
                    # fwalk 给的 filename 是相对当前目录 fd 的名字, 不是绝对路径。
                    rel = os.fsdecode(exc.filename) if exc.filename else "."
                    raise WorkspacePermissionError(
                        f"workspace listing not readable: {rel!r}"
                    ) from exc
                # 复审 N-2 —— 只带 strerror, 不带 ``str(exc)``(会附上 filename)。
                raise SandboxSupervisorError(f"workspace listing failed: {exc.strerror}") from exc

            try:
                dfd = self._open_scope_dir_fd(tenant_id, user_id, "/".join(prefix))
            except _WorkspacePathNotFoundError:
                return WorkspaceFileListing()
            entries: list[WorkspaceFileEntry] = []
            hidden = scope_hidden_heads(scope)
            # fwalk + follow_symlinks=False —— 见模块 docstring:子目录先 lstat 再
            # 开、开出来的 fd 与 lstat 不是同一个 inode 就跳过, 被换成 symlink 的
            # 目录名因此走不出去。os.walk 是先 islink 后按名字列, 中间那段空隙就是
            # 跨租户列出文件名的口子。onerror= (Task 3 fix round 1) —— 见
            # _on_walk_error:扫不动的子树必须炸, 不能静默漏掉。
            walker = cast(
                Generator[tuple[str, list[str], list[str], int], None, None],
                os.fwalk(".", dir_fd=dfd, follow_symlinks=False, onerror=_on_walk_error),
            )
            #: B-236 —— 这次遍历见到 ``.seeded`` 标记的对话目录。
            marker_dirs: set[str] = set()
            too_deep = False
            try:
                with closing(walker):
                    for dirpath, dirnames, filenames, walk_fd in walker:
                        if dirpath == "." and hidden:
                            # B-236 —— ``agent_shared`` 不列各对话自己的 ``threads/``。
                            dirnames[:] = [d for d in dirnames if d not in hidden]
                        if dirnames and _walk_depth(dirpath) >= MAX_WALK_DEPTH:
                            # review 9 —— 再往下不走(fwalk 每深一层多握一个目录 fd),记一条日志。
                            dirnames[:] = []
                            if not too_deep:
                                too_deep = True
                                logger.warning(
                                    "nas_workspace_store.list_files_depth_capped depth=%d",
                                    MAX_WALK_DEPTH,
                                )
                        _list_dir_entries(
                            prefix,
                            dirpath,
                            filenames,
                            walk_fd,
                            entries,
                            marker_dirs=marker_dirs,
                        )
            except PermissionError as exc:
                # fwalk 开起点(".")、对已开目录 scandir / fstat 的失败不走 onerror,
                # 直接抛 —— 照 os.walk 时代的口径收成同两类, 调用方只认这两类。
                raise WorkspacePermissionError(f"workspace listing not readable: {'.'!r}") from exc
            except OSError as exc:
                # 典型是 NFS 上列到一半目录被删(ESTALE)。
                raise SandboxSupervisorError(f"workspace listing failed: {exc.strerror}") from exc
            finally:
                os.close(dfd)
            # B-236 —— 开场复制进对话、之后没被碰过的副本不列(见 :func:`_drop_seed_copies`);
            # 先剔再截 2000 条,否则副本照样把真正的产出挤出上限。
            if not include_seed_copies:
                manifests = self._seed_manifests(tenant_id, user_id, prefix, entries, marker_dirs)
                entries = _drop_seed_copies(prefix, entries, manifests)
            entries.sort(key=lambda entry: entry.path)
            return WorkspaceFileListing(entries[:_MAX_LIST_ENTRIES], too_deep=too_deep)

        return await asyncio.to_thread(_list)

    async def list_dir(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        scope: str = SCOPE_USER_ROOT,
        path: str = ".",
        max_entries: int = _MAX_LIST_ENTRIES,
    ) -> WorkspaceDirListing:
        """List **one** directory (B-84) — backs the agent-facing ``list_dir`` tool.

        Not :meth:`list_files` with a filter: that one is recursive, hides the
        reserved prefixes and exists for the browse / download surface. This one
        shows exactly what an ``ls`` would, reserved directories included ——
        ``uploads/`` is where B-67 lands the run's inputs and the model has to be
        able to see it. 两套语义今天就并存(沙箱片段 vs 浏览端点), 合并等于把其
        中一套悄悄改掉。

        ``os.listdir(dfd)`` + ``os.lstat(name, dir_fd=dfd)`` —— 全程相对已经握在手
        里的目录 fd, 从不重走字符串路径(模块 docstring 的 TOCTOU 一节), 而且
        ``lstat`` 不跟 symlink 走:一个指向根外的链接只会以它自己的身份出现在列表
        里, 不会泄露它指向的东西的元数据。
        """

        def _list() -> WorkspaceDirListing:
            rel = scoped_dir(scope, path)
            dfd = self._open_scope_dir_fd(tenant_id, user_id, rel)
            entries: list[WorkspaceDirEntry] = []
            try:
                names = sorted(os.listdir(dfd))
                if not PurePosixPath(path.strip()).parts:
                    # B-236 —— 作用域根上藏起来的首段不出现在列表里(``agent_shared``
                    # 的 ``threads/``),与 ``scoped_path`` 拒绝寻址它同一个口径。
                    names = [n for n in names if n not in scope_hidden_heads(scope)]
                truncated = len(names) > max_entries
                for name in names[:max_entries]:
                    try:
                        info = os.lstat(name, dir_fd=dfd)
                    except OSError:
                        # Broken symlink / racing unlink — degrade gracefully,
                        # 与沙箱片段同款(它那边是 ``except OSError`` 之后
                        # ``is_dir = False; size = None``)。
                        entries.append(WorkspaceDirEntry(name=name, is_dir=False, size=None))
                        continue
                    is_dir = stat.S_ISDIR(info.st_mode)
                    entries.append(
                        WorkspaceDirEntry(
                            name=name,
                            is_dir=is_dir,
                            size=None if is_dir else info.st_size,
                            mtime=datetime.fromtimestamp(info.st_mtime, tz=UTC),
                        )
                    )
            except NotADirectoryError as exc:
                raise WorkspaceNotADirectoryError(
                    f"workspace path is not a directory: {path!r}"
                ) from exc
            except PermissionError as exc:
                raise WorkspacePermissionError(f"workspace listing not readable: {path!r}") from exc
            except OSError as exc:
                raise SandboxSupervisorError(
                    f"workspace listing failed: {path!r}: {exc.strerror}"
                ) from exc
            finally:
                os.close(dfd)
            return WorkspaceDirListing(entries=tuple(entries), truncated=truncated)

        return await asyncio.to_thread(_list)

    async def search_files(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        scope: str = SCOPE_USER_ROOT,
        name_glob: str | None = None,
        content: str | None = None,
        max_results: int = DEFAULT_SEARCH_RESULTS,
    ) -> WorkspaceSearchResult:
        """Find files under one scope by name and/or content (B-84).

        :func:`os.fwalk` with ``dir_fd=`` rather than :func:`os.walk`: this one
        actually *reads* file bytes, so every open has to be ``dir_fd``-relative
        with ``O_NOFOLLOW`` — a symlink planted as a plain file entry pointing at
        ``/etc/passwd`` must not be readable through here. :meth:`list_files` walks
        the same way.
        """
        if name_glob is None and content is None:
            msg = "search_files needs at least one of 'name_glob' / 'content'"
            raise SandboxSupervisorError(msg)

        def _search() -> WorkspaceSearchResult:
            prefix = scope_parts(scope)
            dfd = self._open_scope_dir_fd(tenant_id, user_id, "/".join(prefix))
            try:
                return _walk_and_match(
                    dfd,
                    prefix=prefix,
                    name_glob=name_glob,
                    content=content,
                    max_results=max_results,
                    hidden=scope_hidden_heads(scope),
                )
            finally:
                os.close(dfd)

        return await asyncio.to_thread(_search)

    async def write_file(self, *, tenant_id: UUID, user_id: UUID, path: str, data: bytes) -> None:
        def _write() -> None:
            if len(data) > _MAX_WRITE_BYTES:
                msg = f"upload {path!r} exceeds the {_MAX_WRITE_BYTES}-byte write cap"
                raise SandboxSupervisorError(msg)
            dfd, name = self._open_parent_dir_fd(tenant_id, user_id, path, create=True)
            try:
                # O_NOFOLLOW — see read_file and module docstring. Every
                # OSError here (not just ELOOP) is wrapped into
                # SandboxSupervisorError — a bare OSError must never leak
                # past this store's boundary (parity contract: "错误类型
                # 统一")。``_LEAF_FILE_MODE`` (``0o600``) is owner-only — the
                # only reader is the uid that wrote it, which is now the same
                # uid on both sides (workspace-gid-sharing design § 六).
                try:
                    # review 7 —— ``O_NONBLOCK``:名字上若是沙箱种下的 FIFO,没有读端时
                    # ``open`` 立刻 ENXIO 失败,不会把线程卡住;有读端时下面 fstat 拒。
                    fd = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK,
                        _LEAF_FILE_MODE,
                        dir_fd=dfd,
                    )
                    try:
                        mode = os.fstat(fd).st_mode
                    except OSError:
                        os.close(fd)  # review 9 —— fstat 失败也不能漏掉这个 fd
                        raise
                    if not stat.S_ISREG(mode):
                        os.close(fd)
                        raise WorkspaceNotARegularFileError(
                            f"workspace path is not a regular file: {path!r}"
                        )
                except PermissionError as exc:
                    # 写不动同样是"配置问题"而非"不存在"——见
                    # WorkspacePermissionError 的说明,W2-BUG-1 那一类故障
                    # 不该被下面的宽 except OSError 收成一句 "write failed"。
                    raise WorkspacePermissionError(
                        f"workspace file not writable: {path!r}"
                    ) from exc
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        # B-84 —— 窄类型:一段是 symlink 的 ``openat`` 撞 ELOOP 是
                        # **安全拒绝**, 不是"文件不存在"。``file_ops`` 据此翻
                        # ToolBlockedError, 与沙箱片段的 ``path_escapes_workspace``
                        # 逐字同义。
                        raise WorkspacePathEscapeError(
                            f"workspace path escapes the user root: {path!r}"
                        ) from exc
                    raise SandboxSupervisorError(
                        f"workspace file write failed: {path!r}: {exc}"
                    ) from exc
            finally:
                os.close(dfd)
            # Task 3 fix round 1 (Minor 2), corrected in fix round 2 (NEW-1)
            # — the open above is wrapped, but the write wasn't, and round
            # 1's fix only wrapped ``handle.write`` itself, not the ``with``
            # block's implicit close. ``os.fdopen`` hands back a buffered
            # writer (8 KiB by default); for any payload smaller than that
            # buffer — which covers this whole task's flagship repro,
            # MEMORY.md — the data never reaches the actual ``write(2)``
            # syscall until the buffer flushes at ``close()``/``__exit__``,
            # so ENOSPC/EDQUOT (NAS quota, disk full) surfaces *there*, not
            # inside ``handle.write``. The ``with`` has to be inside the
            # ``try`` for the boundary to actually hold.
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
            except OSError as exc:
                raise SandboxSupervisorError(
                    f"workspace file write failed: {path!r}: {exc}"
                ) from exc

        await asyncio.to_thread(_write)

    async def create_file(self, *, tenant_id: UUID, user_id: UUID, path: str, data: bytes) -> None:
        """B-262 —— 名字上没有东西才建,而且名字一出现内容就是完整的。

        先写同目录的 ``.tmp-<uuid>``,再 ``link`` 成目标名:``link`` 在 NFS 服务端是原子的,
        名字已被占就 ``EEXIST``(文件 / 目录 / 符号链接都算),两个同时到只有一个建得成;
        别人永远看不到写了一半的文件。最后删临时名。同一挂载上 B-236 的文件版本库用的是
        同一做法(手册 §73.8 真 NFS 过)。写失败时目标名从没出现过,不用回删。"""

        def _create() -> None:
            if len(data) > _MAX_WRITE_BYTES:
                msg = f"upload {path!r} exceeds the {_MAX_WRITE_BYTES}-byte write cap"
                raise SandboxSupervisorError(msg)
            dfd, name = self._open_parent_dir_fd(tenant_id, user_id, path, create=True)
            tmp = f".tmp-{uuid4().hex}"
            try:
                try:
                    fd = os.open(
                        tmp,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        _LEAF_FILE_MODE,
                        dir_fd=dfd,
                    )
                    try:
                        view = memoryview(data)
                        while view:
                            view = view[os.write(fd, view) :]
                    finally:
                        os.close(fd)
                    os.link(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
                except FileExistsError as exc:
                    raise WorkspaceFileExistsError(
                        f"workspace file already exists: {path!r}"
                    ) from exc
                except PermissionError as exc:
                    raise WorkspacePermissionError(
                        f"workspace file not writable: {path!r}"
                    ) from exc
                except OSError as exc:
                    raise SandboxSupervisorError(
                        f"workspace file write failed: {path!r}: {exc}"
                    ) from exc
                finally:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp, dir_fd=dfd)
            finally:
                os.close(dfd)

        await asyncio.to_thread(_create)

    async def delete_file(self, *, tenant_id: UUID, user_id: UUID, path: str) -> None:
        def _delete() -> None:
            # The guard reads normalize_workspace_path's output, not the raw
            # string — see that function (wave 2 final review, Critical 2):
            # "./uploads/a.txt" used to slip past this check and delete
            # exactly the file the check exists to protect.
            relpath, _parts = normalize_workspace_path(path)
            if is_delete_protected_workspace_path(relpath):
                raise SandboxSupervisorError(f"path {path!r} is reserved and cannot be deleted")
            try:
                dfd, name = self._open_parent_dir_fd(tenant_id, user_id, path, create=False)
            except _WorkspacePathNotFoundError:
                return  # rm -f semantics — the parent chain doesn't exist, nothing to delete.
            try:
                try:
                    os.unlink(name, dir_fd=dfd)
                except FileNotFoundError:
                    pass  # rm -f semantics — the leaf itself is already gone.
                except PermissionError as exc:
                    # 删不动同样是"配置问题",不是"不存在"——同 read_file/
                    # write_file,PermissionError 先接住,别被下面吞成一句
                    # 含混的失败。
                    raise WorkspacePermissionError(
                        f"workspace file not deletable: {path!r}"
                    ) from exc
                except OSError as exc:
                    raise SandboxSupervisorError(
                        f"workspace file delete failed: {path!r}: {exc}"
                    ) from exc
            finally:
                os.close(dfd)

        await asyncio.to_thread(_delete)

    async def delete_tree(self, *, tenant_id: UUID, user_id: UUID, path: str) -> None:
        """留存链 B-27 —— ``rm -rf`` 用户工作区下的一个子目录(会话 purge 钩子删
        ``threads/<thread_id>/`` 用)。

        与 :meth:`delete_file` 同一套闸:先过
        :func:`~orchestrator.tools.workspace_scope.normalize_workspace_path` 再判保留
        前缀,父链走 dir_fd;最后一段交给 ``shutil.rmtree(name, dir_fd=...)``
        (3.11 起的 fd 版)—— 它先 ``lstat`` 再 ``openat``,``name``
        本身是 symlink 时直接拒绝,这里翻成 :class:`SandboxSupervisorError`,
        绝不顺着链接删到工作区外面。目标不存在同 ``rm -rf``,静默返回。
        """

        def _rmtree() -> None:
            relpath, _parts = normalize_workspace_path(path)
            if is_delete_protected_workspace_path(relpath):
                raise SandboxSupervisorError(f"path {path!r} is reserved and cannot be deleted")
            try:
                dfd, name = self._open_parent_dir_fd(tenant_id, user_id, path, create=False)
            except _WorkspacePathNotFoundError:
                return  # rm -rf semantics — the parent chain doesn't exist, nothing to delete.
            try:
                try:
                    shutil.rmtree(name, dir_fd=dfd)
                except FileNotFoundError:
                    pass  # rm -rf semantics — already gone.
                except NotADirectoryError as exc:
                    raise SandboxSupervisorError(
                        f"workspace path is not a directory: {path!r}"
                    ) from exc
                except PermissionError as exc:
                    raise WorkspacePermissionError(
                        f"workspace directory not deletable: {path!r}"
                    ) from exc
                except OSError as exc:
                    # 含 rmtree 对 symlink 的拒绝("Cannot call rmtree on a
                    # symbolic link")—— 与 delete_file 同款,只留 strerror 不带路径。
                    raise SandboxSupervisorError(
                        f"workspace directory delete failed: {path!r}: {exc.strerror or exc}"
                    ) from exc
            finally:
                os.close(dfd)

        await asyncio.to_thread(_rmtree)

    async def mark_deleted(self, *, tenant_id: UUID, user_id: UUID) -> None:
        """Soft-delete the workspace, then tear down any warm sandbox session.

        Wave 2 Task 4 addition to the marker-write this method already did
        (see module docstring "Marker semantics"): once the marker is on
        disk, a sandbox the user is *currently* using should not keep
        running against a workspace that just got cut loose from purge —
        it would otherwise sit warm (spec's default idle TTL is 15 minutes)
        with no user around to notice, and the next ``acquire`` for this
        ``(tenant, user)`` is refused by ``AgentSandboxClient``'s own
        soft-delete gate anyway, so leaving the *existing* session alive
        would just be an inconsistency window, not a real capability.

        Ordering is deliberate: the marker write happens first and is not
        undone if the teardown below fails. ``mark_deleted``'s only durable
        side effect that matters for correctness is the marker (it is what
        blocks future ``acquire`` calls); the teardown is a best-effort
        cleanup of a session that may not even exist. Letting a teardown
        failure propagate — rather than swallowing it — matters for a
        different reason: ``user_purge.py`` records this step's outcome in
        its per-step failure summary and audits it, and a swallowed
        exception would report success while a running microVM with a stale
        ``EgressContext`` for a purged user's workspace stays up until the
        20-minute platform timeout. The marker having already landed makes
        this safe to retry — retrying only repeats the (idempotent) marker
        write and the teardown lookup, never re-does anything destructive.

        ``runtime``/``instance_store`` both being unset (wave 1/3 default —
        no sandbox runtime wired, e.g. ``persistence_backend="memory"`` or a
        unit test) skips teardown entirely; the marker write above still
        ran. Requiring *both* rather than just ``runtime`` is deliberate:
        ``get_warm`` lives on :attr:`instance_store`, not on
        :attr:`runtime` (:class:`~orchestrator.tools.sandbox.SandboxRuntime`
        has no such method) — one configured without the other is a
        wiring bug this store has no way to recover from, so it degrades
        the same way "neither configured" does rather than raising an
        ``AttributeError`` that would look like a filesystem failure.
        """

        def _mark() -> None:
            # No dir_fd walk here, and no user-root mkdir either: every
            # component of this path comes from an authenticated caller's
            # UUIDs (module docstring "Why the marker is NOT in the user's
            # tree"), there is no attacker-controlled path text to guard,
            # and the marker deliberately lives *outside* the subtree the
            # dir_fd machinery is scoped to. Not creating the user root as a
            # side effect is a small improvement over the old in-tree write:
            # soft-deleting a user who never had a workspace no longer
            # conjures an empty directory for wave 3's sweep to find.
            marker = workspace_deleted_marker(self.root, tenant_id, user_id)
            try:
                # 复审 N-5 —— 第三处 chmod site,之前跟另外两处(_openat_dir /
                # 用户根创建处)政策不一致:那两处只在"这次调用真正把目录带
                # 入存在"时才 chmod(``exist_ok=True``/``FileExistsError``
                # 吞掉的既存目录不归这条写路径管——修存量目录是一次性迁移
                # Job 的职责,同一句理由这里第三次成立)。这里改成同款
                # ``created`` 判据,不再无条件 chmod 一个我们可能不是属主
                # 的既存 ``.deleted/``(uid 迁移落地当天,这棵目录如果是老
                # control-plane(uid 10002)建的,新进程 chmod 会 EPERM)。
                try:
                    marker.parent.mkdir(parents=True)
                    created = True
                except FileExistsError:
                    created = False
                if created:
                    # 0o700 — this directory has exactly one writer (control-plane,
                    # always the same uid across replicas) and no ``subPath`` ever
                    # projects it into a sandbox, so it needs no group/other bits
                    # at all. Keeping it at 0o700 means the authoritative
                    # soft-delete record is protected by *ownership*, not only by
                    # the mount scoping: even a hypothetically mis-scoped mount
                    # handing a sandbox a wider view of the NAS could not forge or
                    # clear a marker.
                    os.chmod(marker.parent, 0o700)
                marker.touch()  # existence is all that matters, nothing to write.
            except PermissionError as exc:
                # 复审 N-5 —— 这个方法之前完全没有
                # ``docs/superpowers/plans/2026-08-08-workspace-gid-sharing.md``
                # Task A Step 7 保留清单 item 1 那条窄类型归因(read/write/
                # delete/list_files 建 tenant 子树那半边都有,这里漏了):
                # 跳过 chmod 并不能真的解除访问问题——如果
                # ``.deleted/`` 属主还是旧 uid,``marker.touch()`` 本身也会
                # 被同一个 EPERM 挡住(mode 0700 对非属主零访问,chmod 与否
                # 都救不了它),真正能解除的只有迁移 Job 把它 chown 回来。
                # 这条分支因此不是"容忍"——它跟其它三个方法一样,把这个预
                # 期中的过渡态翻成窄类型 WorkspacePermissionError,而不是
                # 一句不带归因的 SandboxSupervisorError。``exc.strerror`` 只
                # 留错误原因,不带 marker 的绝对路径(同 N-2)。
                raise WorkspacePermissionError(
                    f"workspace marker write not permitted: {exc.strerror}"
                ) from exc
            except OSError as exc:
                raise SandboxSupervisorError(
                    f"workspace marker write failed: {exc.strerror}"
                ) from exc

        await asyncio.to_thread(_mark)
        logger.info(
            "nas_workspace_store.marked_deleted tenant_id=%s user_id=%s", tenant_id, user_id
        )

        if self.runtime is None or self.instance_store is None:
            return
        warm = await self.instance_store.get_warm(tenant_id=tenant_id, user_id=user_id)
        if warm is None:
            return
        sandbox_id, _container_id = warm
        await self.runtime.destroy(sandbox_id=sandbox_id, reason="workspace_deleted")
        logger.info(
            "nas_workspace_store.destroyed_warm_session_on_delete "
            "tenant_id=%s user_id=%s sandbox_id=%s",
            tenant_id,
            user_id,
            sandbox_id,
        )
