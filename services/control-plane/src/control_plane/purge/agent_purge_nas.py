"""B-278 —— which users have an ``agents/<key>`` folder on the NAS.

The database only knows the users who left a row (a conversation, an
instance binding, an artifact). A user can also have the agent's subtree
without any surviving row — files the agent wrote before its conversations
were purged one by one — so on a NAS deployment the purge also looks at the
disk:

* ``{root}/{tenant}/<user>/agents/<key>/`` — the agent's workspace subtree;
* ``{root}/{tenant}/.agent-file-versions/<user>/<key>/`` — its ``/agent``
  file history (B-236), next to the user root.

:func:`remove_nas_entry` is the purge's own ``rm -rf`` for one entry on the NAS,
built for what a model can leave behind in its workspace: directories it
``chmod 000``-ed (owner bits are added back on directories we own, never
removed), a symlink where a directory was expected (the link itself is unlinked
at the end of the path; one on the way, or a symlinked user root, is skipped and
counted — never followed), and files another uid owns (the entry is renamed out
of the way to ``.purged-<name>-…`` in the same parent — a future same-name agent
cannot inherit it; counted as ``nas_quarantined`` for ops). Every step is
``openat`` + ``O_NOFOLLOW`` from the NAS root.

Only ``lstat`` is used here (nothing is followed or opened as a path to
delete): the deletes themselves go through the stores' ``openat`` /
``O_NOFOLLOW`` chains. ``tenant_id`` is a UUID, ``agent_key`` has passed
:func:`~expert_work.protocol.agent_key.require_safe_key`, and only entries
whose name is a canonical UUID are taken as users.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
import stat
import time
from pathlib import Path
from uuid import UUID, uuid4

from expert_work.persistence import AGENT_FILE_VERSIONS_DIR, WORKSPACE_AGENTS_DIR
from expert_work.protocol.agent_key import require_safe_key
from orchestrator.tools.nas_workspace_store import DELETED_DIR


def _as_user(name: str) -> UUID | None:
    try:
        user = UUID(name)
    except ValueError:
        return None
    return user if str(user) == name else None


def _is_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _scan(root: str, tenant_id: UUID, agent_key: str) -> set[UUID]:
    tenant_dir = Path(root) / str(tenant_id)
    found: set[UUID] = set()
    for parent, tail in (
        (tenant_dir, (WORKSPACE_AGENTS_DIR, agent_key)),
        (tenant_dir / AGENT_FILE_VERSIONS_DIR, (agent_key,)),
    ):
        if not _is_dir(parent):
            continue
        with os.scandir(parent) as entries:
            for entry in entries:
                user = _as_user(entry.name)
                if user is None or not entry.is_dir(follow_symlinks=False):
                    continue
                if _is_dir(Path(entry.path).joinpath(*tail)):
                    found.add(user)
    return found


async def scan_agent_dirs(root: str, tenant_id: UUID, agent_key: str) -> set[UUID]:
    """Users with an ``agents/<key>`` subtree or ``/agent`` history for this agent."""
    require_safe_key(agent_key)
    return await asyncio.to_thread(_scan, root, tenant_id, agent_key)


async def archived_users(root: str, tenant_id: UUID, users: set[UUID]) -> set[UUID]:
    """The subset already purged as users (soft-delete marker present): their
    whole volume is on its way to the archive, so the agent purge skips them."""
    deleted = Path(root) / str(tenant_id) / DELETED_DIR

    def _check() -> set[UUID]:
        return {u for u in users if os.path.lexists(deleted / str(u))}

    return await asyncio.to_thread(_check)


logger = logging.getLogger("expert_work.control_plane.purge.agent")

#: :func:`remove_nas_entry` outcomes.
ABSENT = "absent"
REMOVED = "removed"
UNLINKED = "unlinked"
SKIPPED_SYMLINK = "skipped_symlink"
QUARANTINED = "quarantined"

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _add_owner_bits(dfd: int, name: str, info: os.stat_result) -> None:
    """``u+rwx`` on a directory (only adds bits; a dir another uid owns stays as is)."""
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o700 != 0o700:
        try:
            os.chmod(name, mode | 0o700, dir_fd=dfd)
        except OSError as exc:
            # Not ours (another uid) or a read-only mount: leave it; rmtree then fails
            # and the entry is quarantined by rename instead.
            logger.info("purge_agent.nas_chmod_refused error=%s", type(exc).__name__)


def _open_dir(dfd: int, name: str) -> int | None:
    """Open ``name`` (a real directory) under ``dfd``; ``None`` if it is absent,
    not a directory, or a symlink (callers check that first)."""
    try:
        info = os.lstat(name, dir_fd=dfd)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=dfd)
    except PermissionError:
        _add_owner_bits(dfd, name, info)
        return os.open(name, _DIR_FLAGS, dir_fd=dfd)


def _make_removable(dfd: int, name: str) -> None:
    """Walk ``name`` (by fd, symlinks never followed) adding owner rwx to every
    directory so ``rmtree`` can list and empty it. Best-effort: what cannot be
    fixed is left for the quarantine rename."""
    try:
        info = os.lstat(name, dir_fd=dfd)
    except OSError:
        return
    if not stat.S_ISDIR(info.st_mode):
        return
    _add_owner_bits(dfd, name, info)
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=dfd)
    except OSError:
        return
    try:
        for child in os.listdir(fd):
            _make_removable(fd, child)
    except OSError as exc:
        # Best-effort walk: whatever stays unlistable makes rmtree fail, and the
        # caller falls back to the quarantine rename.
        logger.info("purge_agent.nas_walk_incomplete error=%s", type(exc).__name__)
    finally:
        os.close(fd)


def _remove(root: str, parts: list[str], name: str) -> str:
    fd = os.open(Path(root), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            try:
                kind = os.lstat(part, dir_fd=fd).st_mode
            except FileNotFoundError:
                return ABSENT
            if stat.S_ISLNK(kind):
                return SKIPPED_SYMLINK
            child = _open_dir(fd, part)
            if child is None:
                return ABSENT
            os.close(fd)
            fd = child
        try:
            info = os.lstat(name, dir_fd=fd)
        except FileNotFoundError:
            return ABSENT
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            os.unlink(name, dir_fd=fd)
            return UNLINKED if stat.S_ISLNK(info.st_mode) else REMOVED
        _make_removable(fd, name)
        try:
            shutil.rmtree(name, dir_fd=fd)
            return REMOVED
        except FileNotFoundError:
            return REMOVED
        except OSError as exc:
            parked = f".purged-{name}-{int(time.time())}-{uuid4().hex[:8]}"
            # Raises when even the rename is refused: genuinely stuck, the caller records it.
            os.rename(name, parked, src_dir_fd=fd, dst_dir_fd=fd)
            logger.warning(
                "purge_agent.nas_quarantined errno=%s parked=%s",
                errno.errorcode.get(exc.errno or 0, "?"),
                parked,
            )
            return QUARANTINED
    finally:
        os.close(fd)


async def remove_nas_entry(root: str, parts: list[str], name: str) -> str:
    """``rm -rf {root}/{parts…}/{name}`` the way the purge needs it (see the module
    docstring). ``parts`` start with the tenant id and are fixed names / UUIDs;
    ``name`` is a UUID or a checked ``agent_key``. Returns one of the outcome
    constants; raises ``OSError`` only when the entry can be neither removed nor
    renamed."""
    return await asyncio.to_thread(_remove, root, parts, name)
