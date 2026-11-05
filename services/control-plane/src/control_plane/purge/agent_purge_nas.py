"""B-278 —— which users have an ``agents/<key>`` folder on the NAS.

The database only knows the users who left a row (a conversation, an
instance binding, an artifact). A user can also have the agent's subtree
without any surviving row — files the agent wrote before its conversations
were purged one by one — so on a NAS deployment the purge also looks at the
disk:

* ``{root}/{tenant}/<user>/agents/<key>/`` — the agent's workspace subtree;
* ``{root}/{tenant}/.agent-file-versions/<user>/<key>/`` — its ``/agent``
  file history (B-236), next to the user root.

:func:`has_symlink` tells the purge to skip (and count) a path whose
components include a symlink — a model can create one inside its own
workspace, and the store's ``O_NOFOLLOW`` delete would refuse it forever.

Only ``lstat`` is used here (nothing is followed or opened as a path to
delete): the deletes themselves go through the stores' ``openat`` /
``O_NOFOLLOW`` chains. ``tenant_id`` is a UUID, ``agent_key`` has passed
:func:`~expert_work.protocol.agent_key.require_safe_key`, and only entries
whose name is a canonical UUID are taken as users.
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from uuid import UUID

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


def _symlink_on(user_root: Path, relpath: str) -> bool:
    current = user_root
    for part in (p for p in relpath.split("/") if p):
        current = current / part
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return True
        except FileNotFoundError:
            return False
        except OSError:
            return False
    return False


async def has_symlink(root: str, tenant_id: UUID, user_id: UUID, relpath: str) -> bool:
    """Whether any component of ``relpath`` under ``{root}/{tenant}/{user}`` is a
    symlink. Only ``lstat`` — nothing is followed. ``relpath`` is built by the purge
    from fixed names, UUIDs and a checked ``agent_key``."""
    user_root = Path(root) / str(tenant_id) / str(user_id)
    return await asyncio.to_thread(_symlink_on, user_root, relpath)
