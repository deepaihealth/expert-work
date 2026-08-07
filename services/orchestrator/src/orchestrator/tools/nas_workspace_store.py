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
``/mnt/workspaces``) with plain :mod:`pathlib` calls; per-tenant/per-user
layout is ``{root}/{tenant_id}/{user_id}/...``, matching the sandbox side's
``subPath: "<tenant_id>/<user_id>"`` projection of the same volume (wave 2
Task 4/6) — a sandbox writing under ``/workspace`` and this store reading
``{root}/{tenant_id}/{user_id}`` see the same files.

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
*soft*-delete: it drops an empty :data:`DELETED_MARKER` sentinel file at the
user's workspace root and nothing else — no file is removed, no bytes are
freed. This mirrors the supervisor's ``mark_workspace_deleted`` (Mini-ADR
J-36): the marker is what lets a later sweep recognise "this workspace was
soft-deleted" before it actually reclaims the storage. That hard-delete /
archive step is wave 3's job, not this store's — :meth:`list_files` hides
the marker (and the reserved ``skills/`` / ``uploads/`` prefixes) from the
browse view, but the underlying files stay on disk until the archive chain
runs. Because :data:`DELETED_MARKER` is a plain filename inside the tree
this store otherwise treats as agent-writable, :meth:`write_file` and
:meth:`delete_file` both explicitly refuse a request whose path equals it
— an agent (or a caller replaying an untrusted path) could otherwise
directly delete the marker (silently undoing a soft-delete outside the
purge flow) or fabricate it (forging "this workspace was soft-deleted"
without ever calling :meth:`mark_deleted`). Neither of those is the
``is_reserved_workspace_path`` prefix check's job — the marker lives at the
workspace *root*, not under a reserved directory prefix.

**TOCTOU note.** :meth:`_resolve_user_path` validates a path once, at check
time; every method except :meth:`list_files` then performs at least one more
filesystem call afterwards (``mkdir``, ``open``) that a concurrent writer
could race against. The threat is concrete, not theoretical: this NAS
volume is the same tree a sandbox mounts (subPath-scoped to its own
``{tenant_id}/{user_id}``) and *runs untrusted code against* — a malicious
run sharing this control-plane's view of the wider tree could plant a
symlink in the checked-but-not-yet-used window to redirect a write or read
outside the caller's own subtree (a cross-tenant escape, not just a
same-user footgun). :meth:`write_file` re-resolves and re-validates the
parent directory *after* ``mkdir`` and *before* opening the target (closing
the window ``mkdir``'s symlink-following parent walk could otherwise open),
and both :meth:`write_file` and :meth:`read_file` open the final path
component with ``os.O_NOFOLLOW`` so a symlink swapped in for the exact
target between the check and the open causes the open itself to fail rather
than silently follow it. :meth:`delete_file` needs neither: ``unlink()``
never dereferences a symlink at its final path component — it removes the
link entry itself — so there is no equivalent "write/read through a
final-segment symlink" primitive to close there. None of this is airtight
on NFS (no cross-process advisory lock is taken), and re-validating after
``mkdir`` doesn't undo a directory ``mkdir`` may have already created inside
a symlinked-elsewhere target during its parents-walk — this closes the
specific reachable exploit (a write actually landing outside the tree with
attacker-chosen bytes), not every theoretical race.
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

from expert_work.persistence import is_reserved_workspace_path
from orchestrator.tools.sandbox import SandboxSupervisorError
from orchestrator.tools.workspace_store import WorkspaceFileEntry

if TYPE_CHECKING:
    # Only used for the ``runtime`` field's type — wave 2 Task 4 wires it up
    # (mark_deleted tearing down a warm sandbox session). Deferred behind
    # TYPE_CHECKING so this module never needs a real import path into
    # ``orchestrator.tools.sandbox`` at runtime, keeping the two modules free
    # to evolve independently.
    from orchestrator.tools.sandbox import SandboxRuntime

logger = logging.getLogger(__name__)

#: Soft-delete sentinel (see module docstring "Marker semantics"). An empty
#: file with this name at a user's workspace root means "this workspace was
#: soft-deleted" — referenced by wave 2 Task 4 (writes it) and Task 7
#: (contract tests both implementations against it).
DELETED_MARKER = ".ew-workspace-deleted"

#: Per-file download cap — mirrors
#: ``sandbox_supervisor.supervisor._MAX_ARTIFACT_BYTES``.
_MAX_READ_BYTES = 10 * 1024 * 1024

#: Document-upload write cap — mirrors
#: ``sandbox_supervisor.supervisor._MAX_WORKSPACE_WRITE_BYTES``.
_MAX_WRITE_BYTES = 25 * 1024 * 1024

#: Workspace-browse listing cap — mirrors
#: ``sandbox_supervisor.supervisor._MAX_WORKSPACE_LIST_ENTRIES``.
_MAX_LIST_ENTRIES = 2000


@dataclass
class NasWorkspaceStore:
    """Production :class:`WorkspaceStore` (wave 2) — reads/writes the NAS mount directly.

    ``root`` is the control-plane Pod's local mount point for the shared NAS
    volume (e.g. ``/mnt/workspaces``); every method scopes its filesystem
    access under ``{root}/{tenant_id}/{user_id}`` via
    :meth:`_resolve_user_path`, which is the sole path-traversal guard (see
    that method's docstring). All I/O is dispatched through
    :func:`asyncio.to_thread` — NFS-backed synchronous I/O can block for the
    duration of a network round-trip, and doing that on the event loop would
    stall every other in-flight run.
    """

    root: str
    #: Wave 2 Task 4 wiring — ``mark_deleted`` will use this to tear down a
    #: warm sandbox session before marking the workspace deleted (mirroring
    #: the supervisor's own destroy-then-mark sequencing). This task never
    #: reads it; it stays ``None``.
    runtime: SandboxRuntime | None = None

    def _user_root(self, tenant_id: UUID, user_id: UUID) -> Path:
        return (Path(self.root) / str(tenant_id) / str(user_id)).resolve()

    def _resolve_user_path(self, tenant_id: UUID, user_id: UUID, path: str) -> Path:
        """Resolve ``path`` to an absolute path inside the user's workspace, or raise.

        ``path`` must be relative and free of ``..`` path segments — checked
        against the *literal* path text, so a URL-encoded traversal attempt
        (``%2e%2e%2f``) is never decoded; it is just an odd filename. The
        resolved candidate must additionally stay inside the user's root
        after ``Path.resolve()`` expands any symlink in the chain — this is
        what stops a symlink planted inside the workspace (e.g. by a prior
        sandbox run) from being used to read/write outside it.
        """
        cleaned = path.strip()
        if not cleaned or cleaned.startswith("/") or ".." in PurePosixPath(cleaned).parts:
            raise SandboxSupervisorError(
                f"workspace path must be relative and free of '..': {path!r}"
            )
        user_root = self._user_root(tenant_id, user_id)
        candidate = (user_root / cleaned).resolve()
        if not candidate.is_relative_to(user_root):
            raise SandboxSupervisorError(f"workspace path escapes the user root: {path!r}")
        return candidate

    async def read_file(self, *, tenant_id: UUID, user_id: UUID, path: str) -> bytes:
        def _read() -> bytes:
            candidate = self._resolve_user_path(tenant_id, user_id, path)
            # O_NOFOLLOW — see module docstring "TOCTOU note". A concurrent
            # writer could have swapped ``candidate`` for a symlink to
            # somewhere outside the user's root in the window between the
            # check above and this open; O_NOFOLLOW makes that open fail
            # (ELOOP) instead of silently reading through it.
            try:
                fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise SandboxSupervisorError(
                        f"workspace path escapes the user root: {path!r}"
                    ) from exc
                raise SandboxSupervisorError(f"workspace file not found: {path!r}") from exc
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
                    # e.g. IsADirectoryError — ``candidate`` resolved to a
                    # directory, not a file.
                    raise SandboxSupervisorError(f"workspace file not found: {path!r}") from exc

        return await asyncio.to_thread(_read)

    async def list_files(self, *, tenant_id: UUID, user_id: UUID) -> list[WorkspaceFileEntry]:
        def _list() -> list[WorkspaceFileEntry]:
            user_root = self._user_root(tenant_id, user_id)
            if not user_root.is_dir():
                return []
            entries: list[WorkspaceFileEntry] = []
            for dirpath, _dirnames, filenames in os.walk(user_root):
                for name in filenames:
                    full = Path(dirpath) / name
                    rel = full.relative_to(user_root).as_posix()
                    if rel == DELETED_MARKER or is_reserved_workspace_path(rel):
                        continue
                    entries.append(WorkspaceFileEntry(path=rel, size=full.stat().st_size))
            entries.sort(key=lambda entry: entry.path)
            return entries[:_MAX_LIST_ENTRIES]

        return await asyncio.to_thread(_list)

    async def write_file(self, *, tenant_id: UUID, user_id: UUID, path: str, data: bytes) -> None:
        def _write() -> None:
            cleaned = path.strip()
            if cleaned == DELETED_MARKER:
                raise SandboxSupervisorError(f"path {path!r} is reserved and cannot be written")
            if len(data) > _MAX_WRITE_BYTES:
                msg = f"upload {path!r} exceeds the {_MAX_WRITE_BYTES}-byte write cap"
                raise SandboxSupervisorError(msg)
            candidate = self._resolve_user_path(tenant_id, user_id, path)
            user_root = self._user_root(tenant_id, user_id)
            candidate.parent.mkdir(parents=True, exist_ok=True)
            # TOCTOU re-check — see module docstring "TOCTOU note".
            # ``mkdir(parents=True)`` silently accepts (and walks through) a
            # pre-existing symlink at any intermediate component; a
            # concurrent untrusted writer sharing this tree could have
            # swapped one in between the initial ``_resolve_user_path``
            # check above and this ``mkdir`` call. Re-resolve the parent now
            # that it exists and re-verify it is still inside the user's
            # root before any byte is written.
            real_parent = candidate.parent.resolve()
            if not real_parent.is_relative_to(user_root):
                raise SandboxSupervisorError(f"workspace path escapes the user root: {path!r}")
            target = real_parent / candidate.name
            # O_NOFOLLOW — refuse to write through a symlink swapped in for
            # the exact target between the check above and this open.
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise SandboxSupervisorError(
                        f"workspace path escapes the user root: {path!r}"
                    ) from exc
                raise
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)

        await asyncio.to_thread(_write)

    async def delete_file(self, *, tenant_id: UUID, user_id: UUID, path: str) -> None:
        def _delete() -> None:
            candidate = self._resolve_user_path(tenant_id, user_id, path)
            cleaned = path.strip()
            if cleaned == DELETED_MARKER or is_reserved_workspace_path(cleaned):
                raise SandboxSupervisorError(f"path {path!r} is reserved and cannot be deleted")
            # No O_NOFOLLOW-equivalent needed here (see module docstring
            # "TOCTOU note") — ``unlink()`` never dereferences a symlink at
            # its final path component, it removes the link entry itself,
            # so a final-segment swap can't be abused to delete something
            # outside the tree the way write/read could be abused to
            # write/read one.
            candidate.unlink(missing_ok=True)  # rm -f semantics — missing is not an error

        await asyncio.to_thread(_delete)

    async def mark_deleted(self, *, tenant_id: UUID, user_id: UUID) -> None:
        def _mark() -> None:
            user_root = self._user_root(tenant_id, user_id)
            user_root.mkdir(parents=True, exist_ok=True)
            (user_root / DELETED_MARKER).touch(exist_ok=True)

        await asyncio.to_thread(_mark)
        logger.info(
            "nas_workspace_store.marked_deleted tenant_id=%s user_id=%s", tenant_id, user_id
        )
