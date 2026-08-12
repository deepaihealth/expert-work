# ============================================================
# Adapted from bytedance/deer-flow @ 813d3c94efa7fdea6aafcb4f459304db91fcaed0
# Source: backend/packages/harness/deerflow/persistence/thread_meta/base.py
# License: MIT (see vendor LICENSE)
# Modifications:
#   - tenant_id (UUID) is a required first-class parameter; DeerFlow's
#     three-state user_id AUTO sentinel collapsed to "tenant_id required"
#     (Stream C wires the contextvar at call sites instead)
#   - Returns Pydantic ThreadMeta (expert-work-protocol) instead of dict
#   - Dropped DeerFlow-specific fields: assistant_id / display_name /
#     metadata_json (we may add them later if a use case appears)
#   - check_access semantics simplified: 1 mode (existing-or-bypass) instead
#     of DeerFlow's permissive / strict pair
# Last sync: 2026-05-11
# ============================================================

"""Abstract ``ThreadMetaStore`` repository — ADR-0002 schema.

Implementations:
- :class:`expert_work.persistence.thread_meta.memory.InMemoryThreadMetaStore`
- :class:`expert_work.persistence.thread_meta.sql.SqlThreadMetaStore`
"""

from __future__ import annotations

import abc
from collections.abc import Collection
from typing import Literal
from uuid import UUID

from expert_work.protocol import ThreadMeta, ThreadStatus

# List ordering — "created_at" is thread creation (session-history default);
# "last_activity" is the newest run's created_at (conversation-browser
# monitoring default: an old thread that just errored surfaces on top).
ThreadOrder = Literal["created_at", "last_activity"]


class ThreadMetaStore(abc.ABC):
    """Per-thread metadata repository.

    Every mutating / read method takes ``tenant_id`` explicitly. There is
    no AUTO sentinel — Stream C wires tenant context at the call site
    (Control Plane handlers, Orchestrator worker pickup, etc.).
    """

    @abc.abstractmethod
    async def create(
        self,
        *,
        thread_id: UUID,
        tenant_id: UUID,
        created_by: str,
        user_id: UUID | None = None,
        agent_name: str | None = None,
        agent_version: str | None = None,
    ) -> ThreadMeta:
        """Insert a new thread; ``thread_id`` must be unique.

        ``user_id`` records the owning :class:`~expert_work.protocol.TenantUser`
        (Stream J.14). ``None`` for machine-triggered threads with no
        per-user instance.
        """

    @abc.abstractmethod
    async def get(self, thread_id: UUID, *, tenant_id: UUID) -> ThreadMeta | None:
        """Read a thread, filtered to ``tenant_id``.

        Returns ``None`` when the row does not exist or belongs to a
        different tenant — never reveals cross-tenant existence.
        """

    @abc.abstractmethod
    async def get_many(
        self, thread_ids: Collection[UUID], *, tenant_id: UUID
    ) -> dict[UUID, ThreadMeta]:
        """Batch :meth:`get` — read many threads filtered to ``tenant_id`` in
        one round trip. Returns a ``{thread_id: ThreadMeta}`` map holding only
        rows that exist and belong to ``tenant_id`` (missing / cross-tenant ids
        are absent). Replaces a per-id ``get`` loop (avoids an N+1)."""

    @abc.abstractmethod
    async def list_by_tenant(
        self,
        tenant_id: UUID,
        *,
        status: ThreadStatus | None = None,
        user_id: UUID | None = None,
        agent_name: str | None = None,
        agent_version: str | None = None,
        nonempty: bool = False,
        q: str | None = None,
        q_thread_ids: Collection[UUID] | None = None,
        include_archived: bool = False,
        thread_ids: Collection[UUID] | None = None,
        order_by: ThreadOrder = "created_at",
        limit: int = 100,
        offset: int = 0,
    ) -> list[ThreadMeta]:
        """Paginated list of threads for ``tenant_id``, newest first.

        When ``user_id`` is given, only that user's threads are returned
        (Stream J.14 per-user scoping). ``agent_name`` / ``agent_version``
        narrow to threads bound to that agent (Stream H.6 Mini-ADR H-10 —
        feeds the per-agent Runs tab's thread-window resolve step).

        ``nonempty`` filters out threads that never had a run — the empty
        throwaway sessions eager thread-creation used to leave behind. Applied
        in the query so pagination counts only real threads. (An in-memory
        backend with no run store treats it as a no-op.)

        ``q`` is a case-insensitive substring match on ``title`` (the
        session-history search box); ``q_thread_ids`` widens a ``q`` hit
        to "title matches OR thread_id in this set" — the conversation
        browser resolves message-content matches to ids first (IA M4),
        so one search box spans title and content. Ignored without ``q``.

        ``include_archived`` controls the
        soft-deleted ``ARCHIVED`` threads: when ``False`` (default) they are
        excluded — UNLESS ``status`` explicitly asks for ``ARCHIVED``, which
        always wins (an explicit status is an exact filter).

        ``thread_ids`` narrows to an explicit id set (conversation-centric
        IA — the browser's has-error filter resolves failing threads from
        the run store first, then reads their metadata here). ``None``
        means no narrowing; an empty collection returns ``[]``.

        ``order_by="last_activity"`` sorts by the newest run's
        ``created_at`` (falling back to thread creation for run-less
        threads) — the conversation browser's monitoring order. The
        in-memory backend has no run store and falls back to
        ``created_at`` (same caveat as ``nonempty``); the SQL backend
        does the real sort.
        """

    @abc.abstractmethod
    async def list_all_tenants(
        self,
        *,
        status: ThreadStatus | None = None,
        user_id: UUID | None = None,
        agent_name: str | None = None,
        agent_version: str | None = None,
        nonempty: bool = False,
        q: str | None = None,
        q_thread_ids: Collection[UUID] | None = None,
        include_archived: bool = False,
        thread_ids: Collection[UUID] | None = None,
        order_by: ThreadOrder = "created_at",
        limit: int = 100,
        offset: int = 0,
    ) -> list[ThreadMeta]:
        """Cross-tenant thread list — Stream N (Mini-ADR N-4).

        Caller MUST be inside ``bypass_rls_session()``. Newest first.
        ``user_id`` narrows to one end-user's threads (user ids are
        globally unique UUIDs, so this is well-defined across tenants —
        the conversation browser's member filter). ``agent_name`` /
        ``agent_version`` as in :meth:`list_by_tenant` (Mini-ADR H-10).
        ``nonempty`` / ``q`` / ``include_archived`` / ``thread_ids`` /
        ``order_by`` as in :meth:`list_by_tenant`.
        """

    @abc.abstractmethod
    async def count_by_tenant(
        self,
        tenant_id: UUID,
        *,
        status: ThreadStatus | None = None,
        user_id: UUID | None = None,
        agent_name: str | None = None,
        agent_version: str | None = None,
        nonempty: bool = False,
        q: str | None = None,
        q_thread_ids: Collection[UUID] | None = None,
        include_archived: bool = False,
        thread_ids: Collection[UUID] | None = None,
    ) -> int:
        """Count the threads :meth:`list_by_tenant` would return.

        Same filter semantics, no pagination — the true ``total`` for the
        conversation browser's server-side pager (``len(items)`` of one
        page is not a total).
        """

    @abc.abstractmethod
    async def count_all_tenants(
        self,
        *,
        status: ThreadStatus | None = None,
        user_id: UUID | None = None,
        agent_name: str | None = None,
        agent_version: str | None = None,
        nonempty: bool = False,
        q: str | None = None,
        q_thread_ids: Collection[UUID] | None = None,
        include_archived: bool = False,
        thread_ids: Collection[UUID] | None = None,
    ) -> int:
        """Cross-tenant :meth:`count_by_tenant` — same
        ``bypass_rls_session()`` contract as :meth:`list_all_tenants`."""

    @abc.abstractmethod
    async def update_title(
        self,
        thread_id: UUID,
        title: str,
        *,
        tenant_id: UUID,
    ) -> bool:
        """Set the human ``title``; returns True if a row matched the tenant.

        Used both for auto-titling (first user message) and manual rename.
        """

    @abc.abstractmethod
    async def update_status(
        self,
        thread_id: UUID,
        status: ThreadStatus,
        *,
        tenant_id: UUID,
    ) -> bool:
        """Update status; returns True if a row matched the tenant filter."""

    @abc.abstractmethod
    async def update_message_count(
        self,
        thread_id: UUID,
        count: int,
        *,
        tenant_id: UUID,
    ) -> bool:
        """Set the third-party-visible message count; returns True if a row
        matched the tenant filter.

        ``count`` is the ``include_hidden=False`` message tally, recomputed
        by run finalization. The column starts ``NULL`` ("not yet computed")
        for pre-existing rows — this method is the only writer, and callers
        should never write ``0`` to mean "not yet computed".
        """

    @abc.abstractmethod
    async def check_access(self, thread_id: UUID, tenant_id: UUID) -> bool:
        """``True`` iff the thread exists and belongs to ``tenant_id``."""

    @abc.abstractmethod
    async def delete(self, thread_id: UUID, *, tenant_id: UUID) -> bool:
        """Delete the thread; returns True if a row matched the tenant filter.

        Test / admin only. Production retention is driven by Stream D.3 TTL job.
        """
