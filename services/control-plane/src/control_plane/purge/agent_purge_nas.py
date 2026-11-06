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
counted — never followed), and files another uid owns (the entry is renamed into
the holding area ``{tenant}/.purged-agents/{owner}/`` outside the user root — a
future same-name agent cannot inherit it; counted as ``nas_quarantined``; the
retention job deletes it after 7 days). The removal itself lives in
:mod:`expert_work.persistence.workspace.safe_remove`, shared with that job.

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
from expert_work.persistence.workspace.safe_remove import (
    ABSENT,
    QUARANTINED,
    REMOVED,
    SKIPPED_SYMLINK,
    UNLINKED,
    remove_entry,
)
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


async def remove_nas_entry(
    root: str, parts: list[str], name: str, *, tenant_id: UUID, owner_id: UUID
) -> str:
    """``rm -rf {root}/{parts…}/{name}`` the way the purge needs it — see
    :mod:`expert_work.persistence.workspace.safe_remove` (owner bits back on
    ``chmod 000`` directories without following links, iterative removal verified by
    ``lstat``, a symlink target unlinked / a symlink on the way skipped, quarantine to
    ``{tenant}/.purged-agents/{owner}/`` outside the user root). Raises
    :class:`~expert_work.persistence.workspace.safe_remove.NasEntryStuckError` only
    when the entry can be neither removed nor quarantined."""
    return await asyncio.to_thread(
        remove_entry, root, parts, name, tenant_id=tenant_id, owner_id=owner_id
    )


__all__ = [
    "ABSENT",
    "QUARANTINED",
    "REMOVED",
    "SKIPPED_SYMLINK",
    "UNLINKED",
    "archived_users",
    "remove_nas_entry",
    "scan_agent_dirs",
]
