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
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from uuid import UUID

from expert_work.persistence import (
    WORKSPACE_DIR_MODE,
    WORKSPACE_FILE_MODE,
    WORKSPACE_SHARED_GID,
    is_reserved_workspace_path,
)
from orchestrator.tools.sandbox import SandboxSupervisorError, WorkspacePermissionError
from orchestrator.tools.workspace_store import WorkspaceFileEntry

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
#: ``sandbox_supervisor.supervisor._MAX_ARTIFACT_BYTES``.
_MAX_READ_BYTES = 10 * 1024 * 1024

#: Document-upload write cap — mirrors
#: ``sandbox_supervisor.supervisor._MAX_WORKSPACE_WRITE_BYTES``.
_MAX_WRITE_BYTES = 25 * 1024 * 1024

#: Workspace-browse listing cap — mirrors
#: ``sandbox_supervisor.supervisor._MAX_WORKSPACE_LIST_ENTRIES``.
_MAX_LIST_ENTRIES = 2000


def _process_is_in_shared_gid() -> bool:
    """Whether *this* process's chown-to-:data:`WORKSPACE_SHARED_GID` would land.

    Task 3 fix round 2(egid 遗漏,回应 fix round 1 自己提的 concern #3)——
    非特权 ``chown(path, -1, gid)`` 的内核判据是 ``in_group_p()``:先查
    **effective gid**,查不中才落到补充组列表。``getgroups(2)`` 本身不保证
    包含 egid(实测:Docker/runc 会把主 gid 也塞进补充组列表,但那是运行时
    的偶然行为,不是 POSIX 承诺)。只查 ``os.getgroups()`` 会在
    ``runAsGroup: 10000``(主 gid 而非 supplementalGroups)这种配置对的部署
    上误判——假阴性,而且是硬闸的假阴性,后果是把配置对的部署直接拒之门
    外,比"漏判"更糟。两个来源都要查。
    """
    return WORKSPACE_SHARED_GID in os.getgroups() or os.getegid() == WORKSPACE_SHARED_GID


def _chgrp_denied_level() -> int:
    """Log level for a tolerated chown-to-shared-gid ``PermissionError``.

    Task 3 fix round 1(Critical 1)—— 两种失败形态诊断成本天差地别,不能都
    记 ``warning``。这个进程压根不在共享 gid 里(本机/CI 直接构造
    ``NasWorkspaceStore``,跳过了 ``build_workspace_store`` 的装配期闸)是
    **预期**状态,不该在日志里制造噪音,降到 ``DEBUG``。已经在组里却还是
    被拒——生产环境靠 Deployment 的 ``supplementalGroups: [10000]`` 保证在
    组里,这种情况下 ``chown`` 还失败,只可能是这个目录本来就不是我们建
    的、属主是别人——是一次真实事故,升到 ``ERROR``。判据见
    :func:`_process_is_in_shared_gid`。
    """
    return logging.ERROR if _process_is_in_shared_gid() else logging.DEBUG


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

    Task 3 review(gid 共享,波 2 final review BUG-1 的落地)—— 一个这个分支
    带出来的目录,在刚拿到手的 fd 上先 ``fchown``(只改 group,``fd`` 已经
    握在手上,不重走字符串路径 —— 从不是 dir_fd-relative 的
    ``os.chown(name, dir_fd=dfd)``,更不是纯路径 ``chown``,那会重新引入
    整条 ``dir_fd`` 链存在的意义就是要关掉的字符串重解析 TOCTOU;
    ``os.fchown``/``os.fchmod`` 都不需要名字,只作用在已经握着的 fd 上),
    到共享 gid(:data:`WORKSPACE_SHARED_GID`),再 ``fchmod`` 到
    :data:`WORKSPACE_DIR_MODE`(``0o2770``,前导 ``2`` 是 setgid)。旧版本
    这里是纯 ``fchmod`` 到 ``0o777``:两侧 uid(control-plane 10002、沙箱
    agent 10000)都能读写,靠的是把 ``other`` 也开了,不挑 group —— 换成
    setgid + 共享 gid 之后 ``other`` 归零,"另一侧也能读写"这件事完全靠
    group 位撑住,所以 gid 必须先对上,``fchmod`` 才有意义,而不是反过来。

    顺序承重:``fchown`` 必须先于 ``fchmod``——非特权进程 ``chown`` 会清
    set-user/group-id 位(Linux 对目录网开一面,但 NFS 服务端不保证照做),
    反过来做就可能被 ``fchmod`` 悄悄抹掉自己刚设上的 setgid,集群探针实测
    走的就是这个顺序。``fchown`` 到目标 gid 只有调用方本身就是那个组的成员
    才会真的生效(生产靠 Deployment 的 ``supplementalGroups: [10000]``,
    Task 1 钉的);不是成员时(典型是本机/CI 跑测试的账户)内核报
    ``PermissionError``,这里接住只记一条日志、不向上抛——目录仍然落到正确
    的 mode,只是 group 没能变成共享的那个;不这样处理的话,一台没有 gid
    10000 的机器一跑测试,``write_file`` 但凡带一层子目录就整条写入链路带崩
    (这条 fchown 不是 belt-and-braces,是这个分支唯一会碰这个目录 gid 的
    地方)。

    ``os.mkdir``'s own ``mode=`` argument is masked by this process's
    umask before the directory is actually created (typically leaves
    ``0o755``, group 缺 ``w``) — this is why every layer needs an explicit
    ``fchmod`` rather than relying on inheritance: setgid itself and the
    group it stamps *do* get inherited by a child directory created under
    an already-setgid parent, but the permission bits never do. Reached
    this fixed mode unconditionally whenever the directory didn't already
    exist a moment ago (whether this call's own ``mkdir`` won or a
    concurrent same-process caller's did, both are "this process just
    brought it into being") — a directory that already existed before this
    call (the ``O_NOFOLLOW`` fast path above) is left untouched: fixing
    modes on file/directory *is not* what this store is responsible for,
    only what it *creates*.
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
        # 先 chown 后 chmod —— 顺序承重,见上方 docstring。chown 失败时
        # **不** chmod(Task 3 fix round 2,Residual 1)——之前这里 chown 失败
        # 只记日志,照样往下 chmod 到 WORKSPACE_DIR_MODE:那会把目录主动收紧
        # 成"错 group + other 归零"的零访问态,恰恰是这整个任务要修的那个
        # 症状,只是这次是这段代码自己造成的。原样不动(留在 os.mkdir 自己
        # 那个 umask 掩过的宽松 mode)严格好于主动收紧——至少 other 位还在,
        # 不会把仅剩的访问路径也堵死。日志级别见 :func:`_chgrp_denied_level`。
        try:
            os.fchown(fd, -1, WORKSPACE_SHARED_GID)
        except PermissionError as exc:
            logger.log(
                _chgrp_denied_level(),
                "nas_workspace_store.intermediate_dir_chgrp_denied name=%r gid=%s: %s",
                name,
                WORKSPACE_SHARED_GID,
                exc,
            )
        else:
            os.fchmod(fd, WORKSPACE_DIR_MODE)
        return fd


def _normalize_workspace_path(path: str) -> tuple[str, tuple[str, ...]]:
    """The single source of truth for "what does this workspace path mean".

    Returns ``(relpath, parts)`` where ``parts`` is what the ``dir_fd`` walk
    steps through and ``relpath`` is ``"/".join(parts)`` — the canonical
    spelling every *guard* must compare against.

    Wave 2 final review (Critical 2) — before this existed, the guards in
    :meth:`NasWorkspaceStore.write_file` / :meth:`NasWorkspaceStore.
    delete_file` compared the **raw** input string while the actual
    filesystem walk used ``PurePosixPath(cleaned).parts``, which silently
    drops ``.`` segments. The two therefore answered differently for the
    same input: ``"./uploads/a.txt"`` did not look reserved to the guard,
    but landed on exactly ``uploads/a.txt`` on disk (measured, not
    reasoned — the file really was deleted). Normalising in one place and
    letting both the guard and the walk read *that* result is what makes
    the two structurally incapable of disagreeing; re-implementing the
    normalisation next to each guard would recreate the bug.

    ``PurePosixPath`` collapses ``.`` segments and duplicate slashes but
    never ``..``, so the ``..`` rejection below still sees every climb
    attempt. A URL-encoded traversal (``%2e%2e%2f``) is not decoded — it is
    just an odd filename, and stays one.

    Empty ``parts`` (``"."``, ``"./"``, ``".//"``) raises rather than
    falling through: the walk's ``parts[-1]`` would otherwise throw a bare
    ``IndexError`` straight past this store's error boundary, and
    ``/v1/workspace/file`` — which only catches
    :class:`SandboxSupervisorError` — would answer 500 where the supervisor
    backend answers 404 (the "错误类型统一" half of the parity contract in
    the module docstring).

    A NUL byte is rejected here for exactly the same reason (wave 2 final
    re-review, New 1). CPython refuses to pass an embedded NUL to any
    syscall and raises a bare :class:`ValueError` from deep inside
    :func:`os.open` — not an :class:`OSError`, so none of the ``except
    OSError`` wrappers downstream catch it, and ``GET /v1/workspace/file
    ?path=a%00b`` answered 500 where the supervisor backend answers 400.
    Same class as the empty-``parts`` case above, same fix, same place: the
    normaliser is where "is this string a workspace path at all" is decided.
    """
    cleaned = path.strip()
    if not cleaned or cleaned.startswith("/") or "\0" in cleaned:
        raise SandboxSupervisorError(f"workspace path must be relative and free of '..': {path!r}")
    parts = PurePosixPath(cleaned).parts
    if not parts or ".." in parts:
        raise SandboxSupervisorError(f"workspace path must be relative and free of '..': {path!r}")
    return "/".join(parts), parts


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


def _raise_workspace_listing_error(exc: OSError) -> None:
    """``os.walk``'s ``onerror`` callback — Task 3 fix round 1.

    ``os.walk`` 默认 ``onerror=None``:一个扫不动的子树(典型是
    ``EACCES``)会被**静默吞掉**——那棵子树下的文件从结果里凭空消失,不报
    错也不留任何痕迹,而这恰恰是"列不动"最常见的形态,比单个文件
    ``lstat`` 失败常见得多(下面 ``_list`` 里那处 ``try/except`` 只挡得住
    后者)。``control_plane/api/workspace.py`` 的列表端点只接
    :class:`SandboxSupervisorError`,把它翻成 ``{"success": true, "files":
    []}`` —— 一次真实的权限故障被这层静默吞声悄悄变成"工作区是空的",正
    是这整个任务要根治的那类失败。传给 ``os.walk`` 的 ``onerror=`` 让这类
    错误显式地把这次调用整个炸掉,而不是悄悄漏掉一部分结果。
    """
    if isinstance(exc, PermissionError):
        raise WorkspacePermissionError(f"workspace listing not readable: {exc.filename!r}") from exc
    raise SandboxSupervisorError(f"workspace listing failed: {exc}") from exc


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
        :func:`_normalize_workspace_path` — the *same* function the callers'
        reserved-name guards read, so the guard and the walk can never
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
        _relpath, parts = _normalize_workspace_path(path)
        user_root = self._user_root(tenant_id, user_id)
        if create:
            # ``tenant_id``/``user_id`` are UUIDs from the authenticated
            # caller, not attacker path text — see module docstring — so a
            # plain path-string mkdir/open for this trusted prefix is fine;
            # only the (untrusted) ``parts`` walked below need dir_fd
            # chaining.
            #
            # Task 3(gid 共享)—— 这里现在 **也** chown+chmod 用户根自己,
            # 不再像旧版本那样把这一层完全让给
            # AgentSandboxClient._ensure_workspace_dir 收尾。生产复现的
            # W2-BUG-1 就是 agent 把 MEMORY.md 直接写在用户根这一层、不是
            # 某个子目录里 —— 子目录再怎么修都救不了它,用户根自己的
            # mode/gid 必须对。旧注释的结论("由 acquire 时的那次
            # mkdir+chmod(0o777) 兜底,这里重复没意义")是 0o777 时代的:
            # 那时谁先建都行,反正最终都是同一个宽 mode。换成 setgid + 共享
            # gid 之后,在某次 acquire 真正跑到之前,任何一次经这个 store
            # 落地的写入(例如用户在跑过 agent 之前先上传了一份文档)都会
            # 看到一个两边都还没修好的目录,同一个 bug 原样重演。
            #
            # **只在这次 mkdir 真正把目录带入存在时才 chown+chmod**(Task 3
            # fix round 1,Important 1)——``exist_ok=True`` 原来会在目录已
            # 存在时静默不报错,而下面这段却无条件跟着跑;chmod/chown 只对
            # *属主*放行,对一个我们不是属主的既存目录(CSI subPath 建的、
            # 迁移脚本建的、备份恢复出来的)会 EPERM,而这整个 try 块的
            # 唯一异常出口是把任何 OSError 都翻成 "failed to create
            # workspace directory" —— 一条谎报,目录明明建好了(或者一直都
            # 在),只是我们不该动它的属主/mode。修存量目录是 Task 7 一次性
            # 迁移 Job 的职责,这条写路径不该顺手兼职。
            #
            # **这不是幂等免打架的保证**(Task 3 fix round 1,Important 2,
            # 纠正上一版这里的错误说法):``chmod``/``chown`` 对非属主一律
            # EPERM,不看"值是不是已经对了" —— 重复调用同一个目标值不会自
            # 动豁免。而且在 Task 4 落地之前,
            # ``AgentSandboxClient._ensure_workspace_dir``
            # (``agent_sandbox.py:870``)仍然在**每次** acquire 时无条件把
            # 这同一个目录 ``chmod`` 成 ``0o777``,会把这里刚设上的 setgid
            # 位悄悄抹掉 —— 两处确实会打架,只是 Task 4 上线后这条(只在
            # 首次创建时跑一次)先于任何 acquire 落地,而 acquire 那边也改
            # 成同款 setgid+共享 gid 之后就不再互相覆盖。**Task 3 因此不能
            # 单独发布,必须与 Task 4 一起上线。**
            #
            # Wrapped (wave 2 final review, Minor 2): the most likely way
            # this fails in production is the NAS data root not having been
            # chmod'd 1777 by hand (the one manual step in the wave 2
            # release runbook) — control-plane runs as a non-root uid and
            # gets EACCES creating the first tenant subtree. Unwrapped, that
            # surfaces as a bare PermissionError crossing this store's error
            # boundary and a clueless 500 on the upload endpoint; the
            # runbook literally tells the operator "if the first upload
            # after release 500s, check this", which is exactly the signal
            # the error type should have carried in the first place.
            #
            # Task 3 fix round 2(Residual 3)—— 这句话上一版没兑现:那个
            # EACCES 之前被这里的宽 except OSError 收成普通
            # SandboxSupervisorError,不是 WorkspacePermissionError,Task 5
            # 靠后者才能把它翻成有归因的 500——runbook 让人第一个查的那个
            # 场景,反而是唯一没拿到新错误类型的权限失败。这里补一条窄的
            # except PermissionError 在前面接住(mkdir 本身的 EACCES;下面
            # chown 失败已经在内层 try 里被吞掉、不会走到这儿)。
            try:
                try:
                    user_root.mkdir(parents=True)
                    created = True
                except FileExistsError:
                    # 已经存在——不管是别的写入方先跑到,还是这棵目录本来
                    # 就在那里(CSI/迁移/恢复带来的),都不是我们创建的,
                    # mode/gid 不归这条路径管。
                    created = False
                if created:
                    # 路径版本的 chown/chmod(不是 _openat_dir 的 fd 版
                    # 本)—— user_root 是按信任前缀直接开的绝对路径,不经
                    # 过下面的 dir_fd 链;顺序承重(先 chown 后 chmod)与
                    # "非成员就降级只记日志"的理由同 _openat_dir 的
                    # docstring,日志级别见 _chgrp_denied_level。chown 失败
                    # 时不 chmod(Residual 1,理由同 _openat_dir 那段同款
                    # 注释)——原样留在 mkdir 自己 umask 掩过的宽松 mode,
                    # 严格好于主动收紧成零访问态。
                    try:
                        os.chown(user_root, -1, WORKSPACE_SHARED_GID)
                    except PermissionError as exc:
                        logger.log(
                            _chgrp_denied_level(),
                            "nas_workspace_store.user_root_chgrp_denied "
                            "tenant_id=%s user_id=%s gid=%s: %s",
                            tenant_id,
                            user_id,
                            WORKSPACE_SHARED_GID,
                            exc,
                        )
                    else:
                        os.chmod(user_root, WORKSPACE_DIR_MODE)
            except PermissionError as exc:
                raise WorkspacePermissionError(
                    f"failed to create workspace directory {user_root}: {exc}"
                ) from exc
            except OSError as exc:
                raise SandboxSupervisorError(
                    f"failed to create workspace directory {user_root}: {exc}"
                ) from exc
        try:
            dfd = os.open(user_root, os.O_RDONLY | os.O_DIRECTORY)
        except OSError as exc:
            raise _WorkspacePathNotFoundError(f"workspace path not found: {path!r}") from exc

        for component in parts[:-1]:
            try:
                nfd = _openat_dir(dfd, component, create=create)
            except OSError as exc:
                os.close(dfd)
                if exc.errno == errno.ELOOP:
                    raise SandboxSupervisorError(
                        f"workspace path escapes the user root: {path!r}"
                    ) from exc
                raise _WorkspacePathNotFoundError(f"workspace path not found: {path!r}") from exc
            os.close(dfd)
            dfd = nfd
        return dfd, parts[-1]

    async def read_file(self, *, tenant_id: UUID, user_id: UUID, path: str) -> bytes:
        def _read() -> bytes:
            dfd, name = self._open_parent_dir_fd(tenant_id, user_id, path, create=False)
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
                        raise SandboxSupervisorError(
                            f"workspace path escapes the user root: {path!r}"
                        ) from exc
                    raise SandboxSupervisorError(f"workspace file not found: {path!r}") from exc
            finally:
                os.close(dfd)
            with os.fdopen(fd, "rb") as handle:
                # Stat before reading so an over-cap file never gets fully
                # loaded into memory — the NFS mount has no equivalent to
                # the supervisor's bounded ``head -c`` subprocess trick.
                try:
                    size = os.fstat(handle.fileno()).st_size
                except PermissionError as exc:
                    raise WorkspacePermissionError(
                        f"workspace file not readable: {path!r}"
                    ) from exc
                except OSError as exc:
                    raise SandboxSupervisorError(f"workspace file not found: {path!r}") from exc
                if size > _MAX_READ_BYTES:
                    msg = f"workspace file {path!r} exceeds the {_MAX_READ_BYTES}-byte download cap"
                    raise SandboxSupervisorError(msg)
                try:
                    return handle.read()
                except PermissionError as exc:
                    raise WorkspacePermissionError(
                        f"workspace file not readable: {path!r}"
                    ) from exc
                except OSError as exc:
                    # e.g. IsADirectoryError — ``name`` resolved to a
                    # directory, not a file.
                    raise SandboxSupervisorError(f"workspace file not found: {path!r}") from exc

        return await asyncio.to_thread(_read)

    async def list_files(self, *, tenant_id: UUID, user_id: UUID) -> list[WorkspaceFileEntry]:
        def _list() -> list[WorkspaceFileEntry]:
            user_root = self._user_root(tenant_id, user_id)
            # Task 3 fix round 2(Residual 2)—— Python 3.13 起 Path.is_dir()
            # 不再无条件吞掉 OSError:它只忽略 ENOENT/ENOTDIR/EBADF/ELOOP 这
            # 一小撮"路径本来就不存在/不是目录"的错误,EACCES/EPERM 不在白
            # 名单里,会原样重新抛出(实测坐实,见该函数在这个解释器版本下
            # 的源码)。祖先目录(典型是 ``{tenant_id}/`` 本身)没有搜索权限
            # 时——同 ``_raise_workspace_listing_error`` 想防的那类
            # 故障——这一句自己就是一个未包边的 PermissionError 出口,在
            # ``onerror=`` 接手之前就先漏了。
            try:
                is_dir = user_root.is_dir()
            except PermissionError as exc:
                raise WorkspacePermissionError(
                    f"workspace listing not readable: {user_root!r}"
                ) from exc
            except OSError as exc:
                raise SandboxSupervisorError(f"workspace listing failed: {exc}") from exc
            if not is_dir:
                return []
            entries: list[WorkspaceFileEntry] = []
            # followlinks=False — see module docstring: never descend into a
            # symlinked subdirectory, so an intermediate-component escape
            # can't make this enumerate files outside the tree. onerror=
            # (Task 3 fix round 1) — see _raise_workspace_listing_error:
            # without it, a subtree this process can't scan is silently
            # dropped from the results instead of failing loudly.
            for dirpath, _dirnames, filenames in os.walk(
                user_root, followlinks=False, onerror=_raise_workspace_listing_error
            ):
                for name in filenames:
                    full = Path(dirpath) / name
                    rel = full.relative_to(user_root).as_posix()
                    if is_reserved_workspace_path(rel):
                        continue
                    # lstat, not stat — a symlink appearing as a plain file
                    # entry must report its own byte length, never a stat()
                    # of whatever it points at outside the tree (see module
                    # docstring).
                    try:
                        size = full.lstat().st_size
                    except PermissionError as exc:
                        # 同 read_file:列不动 ≠ 不存在,不能被下面吞掉。
                        raise WorkspacePermissionError(
                            f"workspace listing not readable: {rel!r}"
                        ) from exc
                    except OSError as exc:
                        raise SandboxSupervisorError(
                            f"workspace listing failed: {rel!r}: {exc}"
                        ) from exc
                    entries.append(WorkspaceFileEntry(path=rel, size=size))
            entries.sort(key=lambda entry: entry.path)
            return entries[:_MAX_LIST_ENTRIES]

        return await asyncio.to_thread(_list)

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
                # 统一")。``WORKSPACE_FILE_MODE`` 起作用要靠父目录的 setgid
                # 位把 group 继承成共享 gid(``_openat_dir``/上面的用户根
                # chown 已经做了)——这里只负责 mode,不重复 chown 一次
                # leaf 文件。
                try:
                    fd = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                        WORKSPACE_FILE_MODE,
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
                        raise SandboxSupervisorError(
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
            # The guard reads _normalize_workspace_path's output, not the raw
            # string — see that function (wave 2 final review, Critical 2):
            # "./uploads/a.txt" used to slip past this check and delete
            # exactly the file the check exists to protect.
            relpath, _parts = _normalize_workspace_path(path)
            if is_reserved_workspace_path(relpath):
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
                marker.parent.mkdir(parents=True, exist_ok=True)
                # 0o700, deliberately *unlike* the per-user workspace roots
                # ``AgentSandboxClient._ensure_workspace_dir`` creates. Those
                # have to be world-writable because two different uids
                # (control-plane 10002, the sandbox's agent 10000) both write
                # them and a non-root process cannot chown across uids. This
                # directory has exactly one writer — control-plane, always the
                # same uid across replicas — and no ``subPath`` ever projects
                # it into a sandbox, so it needs no group/other bits at all.
                # Keeping it at 0o700 means the authoritative soft-delete
                # record is protected by *ownership*, not only by the mount
                # scoping: even a hypothetically mis-scoped mount handing a
                # sandbox a wider view of the NAS could not forge or clear a
                # marker (wave 2 final re-review — this used to be 0o777 for
                # uniformity's sake, which bought nothing).
                os.chmod(marker.parent, 0o700)
                marker.touch()  # existence is all that matters, nothing to write.
            except OSError as exc:
                raise SandboxSupervisorError(f"workspace marker write failed: {exc}") from exc

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
