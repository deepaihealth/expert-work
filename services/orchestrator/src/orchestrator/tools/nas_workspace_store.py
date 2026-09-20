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

:meth:`list_files` is a narrower case: it only reads metadata, never opens
file content, and its :func:`os.walk` call passes ``followlinks=False`` so
it never *descends into* a symlinked subdirectory (an intermediate-
component escape of the kind described above can't make it enumerate files
outside the tree). A symlink placed as a plain file entry (not a directory)
still appears in the listing under its own in-tree relative path, but its
reported size comes from :func:`os.lstat` (not :func:`os.stat`) — the
symlink's own byte length, never a stat of whatever it points at — so no
metadata about anything outside the tree is ever surfaced. Nothing here
needs ``dir_fd`` chaining: there is no content read and no follow-through
target to escape into.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
import stat
from collections.abc import Generator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast
from uuid import UUID

from expert_work.persistence import (
    is_delete_protected_workspace_path,
    is_reserved_workspace_path,
)
from orchestrator.tools.sandbox import (
    SandboxSupervisorError,
    WorkspaceFileNotFoundError,
    WorkspaceFileTooLargeError,
    WorkspaceNotADirectoryError,
    WorkspaceNotAFileError,
    WorkspacePathEscapeError,
    WorkspacePermissionError,
)
from orchestrator.tools.workspace_scope import (
    SCOPE_USER_ROOT,
    normalize_workspace_path,
    scope_parts,
    scoped_dir,
    scoped_path,
)
from orchestrator.tools.workspace_store import (
    WorkspaceDirEntry,
    WorkspaceDirListing,
    WorkspaceFileEntry,
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
) -> WorkspaceSearchResult:
    """The body of :meth:`NasWorkspaceStore.search_files` — walk under ``dfd``, match, cap.

    Every step is ``dir_fd``-relative: :func:`os.fwalk` hands back the directory
    fd it is currently in, and both the ``lstat`` and the content ``open`` ride
    it with ``O_NOFOLLOW``. 一条指向作用域外的 symlink 因此既进不了结果(它不是
    普通文件), 也读不出内容(``ELOOP``)。

    ``follow_symlinks=False`` keeps the walk itself from descending through a
    symlinked subdirectory, the same reason :meth:`NasWorkspaceStore.list_files`
    passes ``followlinks=False``.
    """

    def _on_error(exc: OSError) -> None:
        # 同 list_files 的 ``_on_walk_error``:扫不动的子树必须炸, 不能静默漏掉一
        # 部分结果 —— "搜不到"与"没搜到"在返回里长得一模一样, 而它们的处置相反。
        if isinstance(exc, PermissionError):
            raise WorkspacePermissionError("workspace search not readable") from exc
        raise SandboxSupervisorError(f"workspace search failed: {exc.strerror}") from exc

    hits: list[WorkspaceFileEntry] = []
    truncated = False
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
    with closing(walker):
        for dirpath, _dirnames, filenames, walk_fd in walker:
            base = PurePosixPath(dirpath)
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
    hits.sort(key=lambda entry: entry.path)
    return WorkspaceSearchResult(entries=tuple(hits), truncated=truncated)


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
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=walk_fd)
    except OSError:
        return False
    with os.fdopen(fd, "rb") as handle:
        try:
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
            # 的入口(list_files 走独立的 os.stat/os.walk,从不调用这个方法,
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
    ) -> bytes:
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
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
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
                    size = os.fstat(handle.fileno()).st_size
                except OSError as exc:
                    raise WorkspaceFileNotFoundError(f"workspace file not found: {path!r}") from exc
                if size > cap:
                    # 「太大」≠「不存在」—— 窄类型让下载端点能回 413 而不是把
                    # 一个明明列在产物列表里的文件谎报成 404(同
                    # WorkspacePermissionError 的拆分理由)。
                    msg = f"workspace file {path!r} exceeds the {cap}-byte read cap"
                    raise WorkspaceFileTooLargeError(msg)
                try:
                    return handle.read()
                except IsADirectoryError as exc:
                    # B-84 —— "它是个目录"不是"它不存在":模型的下一步动作不同
                    # (换个路径 vs 改用 list_dir), 沙箱片段一直把这两件事分成
                    # ``is_a_directory`` 与 ``not_found`` 两种 envelope。仍是
                    # SandboxSupervisorError 的子类, 下载端点照旧 404。
                    raise WorkspaceNotAFileError(f"workspace path is not a file: {path!r}") from exc
                except OSError as exc:
                    raise WorkspaceFileNotFoundError(f"workspace file not found: {path!r}") from exc

        return await asyncio.to_thread(_read)

    async def list_files(
        self, *, tenant_id: UUID, user_id: UUID, scope: str = SCOPE_USER_ROOT
    ) -> list[WorkspaceFileEntry]:
        def _list() -> list[WorkspaceFileEntry]:
            user_root = self._user_root(tenant_id, user_id)
            # B-84 —— 作用域只改**起点**, 不改走路的方式。遍历的根从用户根挪到
            # ``{user_root}/agents/<key>`` 之类, 其余一个字不动;``scope_parts``
            # 已经把 agent_key 过了同一道安全闸, 这里拼的每一段都是校验过的。
            # 回给调用方的 ``path`` 是**作用域相对**的 —— 默认作用域是用户根,
            # 所以浏览端点 / 产物下载那批既有调用方看到的东西逐字不变。
            prefix = scope_parts(scope)
            walk_root = user_root.joinpath(*prefix)
            # ``Path.is_dir()``'s error-swallowing behaviour is not stable
            # across CPython versions: 3.12/3.13 re-raise ``PermissionError``
            # (only ``ENOENT``/``ENOTDIR``/``EBADF``/``ELOOP`` are treated as
            # "doesn't exist"), but 3.14's default (``follow_symlinks=True``)
            # path delegates to ``os.path.isdir()``, which swallows *every*
            # ``OSError`` unconditionally — on 3.14 the ``except
            # PermissionError`` below would simply never fire, silently
            # reintroducing the exact "unreadable subtree → empty result"
            # failure this exists to close (this repo's ``pyproject.toml``
            # pins ``>=3.12``, which admits 3.14; measured against the real
            # store on 3.12.8/3.13.1 vs 3.14.0 to confirm the divergence, not
            # assumed). ``stat.S_ISDIR(os.stat(...).st_mode)`` is the
            # version-independent equivalent — a bare ``os.stat`` call whose
            # exception behaviour is a stable OS-level contract, not a
            # pathlib convenience wrapper's.
            try:
                is_dir = stat.S_ISDIR(os.stat(walk_root).st_mode)
            except (FileNotFoundError, NotADirectoryError):
                return []
            except PermissionError as exc:
                # "." — this checks user_root itself, not an entry under it,
                # so there is no meaningful relative path to name; and it
                # must not be the absolute server-side mount path (sibling
                # wraps below use the workspace-relative ``rel``, not an
                # absolute path — same reason).
                raise WorkspacePermissionError(f"workspace listing not readable: {'.'!r}") from exc
            except OSError as exc:
                # 复审 N-2 —— 同上一条 except 的路径泄露:``os.stat(user_root)``
                # 是普通路径调用,失败的 ``OSError`` 会自带绝对 ``filename``,
                # ``str(exc)`` 原样带着它。``exc.strerror`` 只留错误原因。
                raise SandboxSupervisorError(f"workspace listing failed: {exc.strerror}") from exc
            if not is_dir:
                return []

            def _on_walk_error(exc: OSError) -> None:
                """``os.walk``'s ``onerror`` callback — Task 3 fix round 1.

                ``os.walk`` 默认 ``onerror=None``:一个扫不动的子树(典型是
                ``EACCES``)会被**静默吞掉**——那棵子树下的文件从结果里凭空消
                失,不报错也不留任何痕迹,而这恰恰是"列不动"最常见的形态,比
                单个文件 ``lstat`` 失败常见得多(下面那处 ``try/except`` 只挡
                得住后者)。``control_plane/api/workspace.py`` 的列表端点只接
                :class:`SandboxSupervisorError`,把它翻成 ``{"success": true,
                "files": []}`` —— 一次真实的权限故障被这层静默吞声悄悄变成
                "工作区是空的",正是这整个任务要根治的那类失败。传给
                ``os.walk`` 的 ``onerror=`` 让这类错误显式地把这次调用整个炸
                掉,而不是悄悄漏掉一部分结果。

                复审 I-1 —— 定义成 ``_list`` 内部的闭包(而不是模块级函数)只
                为了能拿到 ``user_root`` 把 ``exc.filename``(``os.walk`` 给的
                永远是绝对路径,NAS 挂载点在服务端的真实文件系统路径)转成工
                作区相对路径。全局约束"用户可见的错误文案不含路径":下面兄弟
                分支(``full.lstat()`` 那处 ``except PermissionError``)已经在
                用相对的 ``rel``,这里之前是唯一还在裸拼 ``exc.filename!r`` 的
                地方。
                """
                if isinstance(exc, PermissionError):
                    rel = os.path.relpath(exc.filename, walk_root) if exc.filename else "."
                    raise WorkspacePermissionError(
                        f"workspace listing not readable: {rel!r}"
                    ) from exc
                # 复审 N-2 —— 同上,``exc`` 是 os.walk 内部 scandir 失败的
                # OSError,``filename`` 是绝对路径,``str(exc)`` 会带着它。
                raise SandboxSupervisorError(f"workspace listing failed: {exc.strerror}") from exc

            entries: list[WorkspaceFileEntry] = []
            # followlinks=False — see module docstring: never descend into a
            # symlinked subdirectory, so an intermediate-component escape
            # can't make this enumerate files outside the tree. onerror=
            # (Task 3 fix round 1) — see _on_walk_error: without it, a
            # subtree this process can't scan is silently dropped from the
            # results instead of failing loudly.
            for dirpath, _dirnames, filenames in os.walk(
                walk_root, followlinks=False, onerror=_on_walk_error
            ):
                for name in filenames:
                    full = Path(dirpath) / name
                    rel = full.relative_to(walk_root).as_posix()
                    # 保留前缀的判据吃的是**用户根相对**路径 ——
                    # ``is_reserved_workspace_path`` 自己会剥掉 ``agents/<key>``
                    # 这层容器再看头一段(见 ``persistence/workspace/layout.py``)。
                    # 喂作用域相对路径进去在今天恰好也对, 但那是巧合不是契约:
                    # 换一个容器层级就静默错位, 而错位的形态是"文件凭空消失"。
                    if is_reserved_workspace_path("/".join((*prefix, rel))):
                        continue
                    # lstat, not stat — a symlink appearing as a plain file
                    # entry must report its own byte length, never a stat()
                    # of whatever it points at outside the tree (see module
                    # docstring).
                    try:
                        # B-84 —— 一次 lstat 同时取大小与 mtime, 不为 mtime 多跑
                        # 一次 stat: 这个循环在一棵几千文件的树上跑, 每多一次系
                        # 统调用就是一次 NFS 往返。
                        info = full.lstat()
                    except PermissionError as exc:
                        # 同 read_file:列不动 ≠ 不存在,不能被下面吞掉。
                        raise WorkspacePermissionError(
                            f"workspace listing not readable: {rel!r}"
                        ) from exc
                    except OSError as exc:
                        # 复审 N-2(同类,这条不在复审原话点名的两处,但
                        # ``full.lstat()`` 是普通路径调用,同款泄露,顺手在
                        # 这一遍改掉)—— ``full`` 是绝对路径,``exc`` 会带
                        # ``filename``,``str(exc)`` 原样带过来。
                        raise SandboxSupervisorError(
                            f"workspace listing failed: {rel!r}: {exc.strerror}"
                        ) from exc
                    entries.append(_entry(rel, info))
            entries.sort(key=lambda entry: entry.path)
            return entries[:_MAX_LIST_ENTRIES]

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
        ``/etc/passwd`` must not be readable through here. :meth:`list_files` can
        get away with :func:`os.walk` because it only ever ``lstat``s.
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
                    fd = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                        _LEAF_FILE_MODE,
                        dir_fd=dfd,
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
