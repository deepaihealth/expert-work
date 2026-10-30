"""Cross-replica per-workspace write lock — Stream TE-8.

Workspace file writes (``write_file``) and ``bash`` must not interleave
across orchestrator replicas that mount the *same* per-user workspace
volume (Docker named volumes can be mounted by several sandboxes at once,
the supervisor is multi-replica, and there is no DB-level workspace lease).
A per-process ``asyncio.Lock`` is therefore insufficient.

TE-ADR-3 (2026-06-05 复议) — the lock is **per-workspace, exclusive, for
writes only**:

- ``bash`` has no path argument and can touch any file, so a per-path lock
  could never cover it (PG advisory locks are keyed by an exact hash and
  have no shared mode). A single per-workspace key — ``{tenant}:{user}`` —
  makes ``bash`` and every file write mutually exclusive, which is correct
  and simple. The cost (same-user writes to *different* files serialise
  across replicas) is negligible: same-turn writes already serialise via
  the L.L6 scheduler, and a per-user workspace has very low write
  concurrency.
- **B-236 follow-up — per conversation on the new layout.** A conversation
  with ``workspace_layout = "conversation"`` has its own ``/workspace``
  (``agents/<key>/threads/<conv>/work/``), so its writes take the key of
  *that* conversation (``conversation_id``, see :func:`write_lock_conversation`)
  instead of the user's. The "very low write concurrency" premise above
  failed in production on 2026-10-30: one integrator user ran 15 reports at
  once and every write queued behind the others' bash (waits up to 6 min,
  some hitting the 360 s lock statement timeout). Old-layout conversations
  still share the agent root and keep the per-user key. Writes into
  ``/agent`` (shared by all conversations of the agent) are deliberately not
  locked across conversations: concurrent writes to the same file there are
  last-writer-wins, accepted as a product decision. ``write_file`` /
  ``edit_file`` replace the file atomically (``mkstemp`` in the same
  directory + ``os.replace``), so a reader never sees a torn file — but an
  ``edit_file`` from another conversation can be overwritten, and ``bash``
  writes carry no atomicity at all.
- **Reads take no lock.** ``write_file`` writes atomically (temp file +
  ``os.replace``), so a reader always sees a complete old-or-new snapshot.
  That is what lets reads run lock-free despite PG advisory locks having no
  shared-read mode.

This module defines the orchestrator-side contract only. The Postgres
implementation (``pg_advisory_xact_lock``) lives in the control plane, which
owns the database engine; the orchestrator tool layer depends on the
:class:`WorkspaceLock` Protocol and defaults to the no-op
:class:`NullWorkspaceLock`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

if TYPE_CHECKING:
    from orchestrator.tools.workspace_paths import WorkspaceView


@runtime_checkable
class WorkspaceLock(Protocol):
    """Exclusive per-workspace write lock (Stream TE-8).

    Implementations serialise writes to one user's workspace across
    replicas for the lifetime of the returned context manager. A
    ``user_id`` of ``None`` denotes an ephemeral (non-shared) workspace and
    implementations may treat it as a no-op. A ``conversation_id`` narrows the
    scope to that conversation's own directory (B-236 new layout); ``None``
    keeps the per-user scope."""

    def acquire(
        self,
        *,
        tenant_id: UUID | None,
        user_id: UUID | None,
        conversation_id: UUID | None = None,
    ) -> AbstractAsyncContextManager[None]:
        """Hold the workspace write lock for the duration of the ``async with``
        block. Released (and any backing transaction committed/rolled back)
        on exit, including on cancellation."""


class NullWorkspaceLock:
    """No-op :class:`WorkspaceLock` — single process / unit tests.

    Provides **no** cross-replica guarantee; production wires a real
    :class:`WorkspaceLock` (Postgres advisory lock) instead."""

    @asynccontextmanager
    async def acquire(
        self,
        *,
        tenant_id: UUID | None,
        user_id: UUID | None,
        conversation_id: UUID | None = None,
    ) -> AsyncIterator[None]:
        yield


@dataclass
class RecordingWorkspaceLock:
    """Recording :class:`WorkspaceLock` for tests.

    Records each ``(tenant_id, user_id)`` acquisition (and, in step, each
    ``conversation_id`` in ``conversations``) and tracks how many holders are
    currently inside the context (``active``) so a test can assert the guarded
    work ran *while the lock was held*."""

    acquired: list[tuple[UUID | None, UUID | None]] = field(default_factory=list)
    conversations: list[UUID | None] = field(default_factory=list)
    active: int = 0

    @asynccontextmanager
    async def acquire(
        self,
        *,
        tenant_id: UUID | None,
        user_id: UUID | None,
        conversation_id: UUID | None = None,
    ) -> AsyncIterator[None]:
        self.acquired.append((tenant_id, user_id))
        self.conversations.append(conversation_id)
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1


def write_lock_conversation(view: WorkspaceView) -> UUID | None:
    """B-236 follow-up —— 写锁按哪个对话分:就是 exec 落盘那个目录的主人。

    新布局 → ``view.conv_id``(主 run 是本对话;子代是 ``_child_config`` 传下来的根对话,
    它写的就是父的目录);老布局 / 没绑 agent → ``None``,按用户锁,与改动前逐字相同。
    与 ``run_in_sandbox`` 给 exec 的 ``conv_id`` 取自同一个视图,锁的与写的不会是两个目录。
    """
    return view.conv_id if view.per_conversation else None


@runtime_checkable
class KeyedLock(Protocol):
    """B-236 —— 按任意字符串键互斥的锁(对话开场复制用,键是 ``{tenant}:{conv}``)。

    与 :class:`WorkspaceLock` 分开:那把锁按 ``(tenant, user)`` 串行一个用户的所有写,开场
    复制要的是「同一个对话只复制一次」,不该把同一用户别的对话的写挡在外面。生产实现在控制面
    (Postgres advisory lock,独立的 classid)。"""

    def acquire(self, *, key: str) -> AbstractAsyncContextManager[None]:
        """在 ``async with`` 期间独占 ``key``。"""


@dataclass
class ProcessKeyedLock:
    """进程内的 :class:`KeyedLock` —— 单进程 / 单元测试。**不跨副本**;生产接的是
    Postgres 实现。没人等的键随即删掉,字典不随对话数增长。"""

    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _holders: dict[str, int] = field(default_factory=dict)

    @asynccontextmanager
    async def acquire(self, *, key: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._holders[key] = self._holders.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._holders[key] -= 1
            if not self._holders[key]:
                del self._holders[key], self._locks[key]
