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
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from uuid import UUID

from expert_work.persistence import is_reserved_workspace_path
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
        try:
            dfd = os.open(user_root, os.O_RDONLY | os.O_DIRECTORY)
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

        for component in parts[:-1]:
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
                except OSError as exc:
                    raise SandboxSupervisorError(f"workspace file not found: {path!r}") from exc
                if size > _MAX_READ_BYTES:
                    msg = f"workspace file {path!r} exceeds the {_MAX_READ_BYTES}-byte download cap"
                    raise SandboxSupervisorError(msg)
                try:
                    return handle.read()
                except OSError as exc:
                    # e.g. IsADirectoryError — ``name`` resolved to a
                    # directory, not a file.
                    raise SandboxSupervisorError(f"workspace file not found: {path!r}") from exc

        return await asyncio.to_thread(_read)

    async def list_files(self, *, tenant_id: UUID, user_id: UUID) -> list[WorkspaceFileEntry]:
        def _list() -> list[WorkspaceFileEntry]:
            user_root = self._user_root(tenant_id, user_id)
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
                is_dir = stat.S_ISDIR(os.stat(user_root).st_mode)
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
                    rel = os.path.relpath(exc.filename, user_root) if exc.filename else "."
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
                user_root, followlinks=False, onerror=_on_walk_error
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
                        # 复审 N-2(同类,这条不在复审原话点名的两处,但
                        # ``full.lstat()`` 是普通路径调用,同款泄露,顺手在
                        # 这一遍改掉)—— ``full`` 是绝对路径,``exc`` 会带
                        # ``filename``,``str(exc)`` 原样带过来。
                        raise SandboxSupervisorError(
                            f"workspace listing failed: {rel!r}: {exc.strerror}"
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
