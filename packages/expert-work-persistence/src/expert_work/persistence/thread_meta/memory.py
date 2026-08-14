# ============================================================
# Adapted from bytedance/deer-flow @ 813d3c94efa7fdea6aafcb4f459304db91fcaed0
# Source: backend/packages/harness/deerflow/persistence/thread_meta/memory.py
# License: MIT (see vendor LICENSE)
# Modifications:
#   - Aligned to ThreadMetaStore (expert_work.persistence.thread_meta.base)
#   - dict[str, ThreadMeta] keyed by thread_id; tenant filter happens at read
# Last sync: 2026-05-11
# ============================================================

"""In-memory ``ThreadMetaStore`` for unit tests."""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from uuid import UUID

from expert_work.persistence.thread_meta.base import ThreadMetaStore, ThreadOrder
from expert_work.protocol import ThreadMeta, ThreadStatus


class InMemoryThreadMetaStore(ThreadMetaStore):
    def __init__(self) -> None:
        self._rows: dict[UUID, ThreadMeta] = {}

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
        if thread_id in self._rows:
            msg = f"thread_meta already exists for thread_id={thread_id}"
            raise ValueError(msg)
        now = datetime.now(UTC)
        meta = ThreadMeta(
            thread_id=thread_id,
            tenant_id=tenant_id,
            user_id=user_id,
            created_by=created_by,
            status=ThreadStatus.ACTIVE,
            agent_name=agent_name,
            agent_version=agent_version,
            message_count=0,
            created_at=now,
            updated_at=now,
        )
        self._rows[thread_id] = meta
        return meta

    async def get(self, thread_id: UUID, *, tenant_id: UUID) -> ThreadMeta | None:
        row = self._rows.get(thread_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def get_many(
        self, thread_ids: Collection[UUID], *, tenant_id: UUID
    ) -> dict[UUID, ThreadMeta]:
        out: dict[UUID, ThreadMeta] = {}
        for thread_id in thread_ids:
            row = self._rows.get(thread_id)
            if row is not None and row.tenant_id == tenant_id:
                out[thread_id] = row
        return out

    def _filtered(
        self,
        *,
        tenant_id: UUID | None,
        status: ThreadStatus | None,
        user_id: UUID | None,
        agent_name: str | None,
        agent_version: str | None,
        q: str | None,
        q_thread_ids: Collection[UUID] | None,
        include_archived: bool,
        thread_ids: Collection[UUID] | None,
    ) -> list[ThreadMeta]:
        """The shared filter for list/count — one source so ``total`` can
        never drift from the page. ``nonempty`` is a no-op in-memory (no
        run store to correlate against); the SQL backend does the real
        filter."""
        rows = list(self._rows.values())
        if tenant_id is not None:
            rows = [r for r in rows if r.tenant_id == tenant_id]
        if thread_ids is not None:
            wanted = set(thread_ids)
            rows = [r for r in rows if r.thread_id in wanted]
        if status is not None:
            rows = [r for r in rows if r.status == status]
        elif not include_archived:
            rows = [r for r in rows if r.status != ThreadStatus.ARCHIVED]
        if user_id is not None:
            rows = [r for r in rows if r.user_id == user_id]
        if agent_name is not None:
            rows = [r for r in rows if r.agent_name == agent_name]
        if agent_version is not None:
            rows = [r for r in rows if r.agent_version == agent_version]
        if q:
            needle = q.lower()
            content_hits = set(q_thread_ids) if q_thread_ids else set()
            rows = [
                r
                for r in rows
                if (r.title is not None and needle in r.title.lower())
                or r.thread_id in content_hits
            ]
        rows.sort(key=lambda r: r.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return rows

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
        # ``order_by="last_activity"`` falls back to created_at here — no
        # run store to correlate against (same caveat as ``nonempty``).
        del nonempty, order_by
        rows = self._filtered(
            tenant_id=tenant_id,
            status=status,
            user_id=user_id,
            agent_name=agent_name,
            agent_version=agent_version,
            q=q,
            q_thread_ids=q_thread_ids,
            include_archived=include_archived,
            thread_ids=thread_ids,
        )
        return rows[offset : offset + limit]

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
        del nonempty, order_by  # both no-ops in-memory — see ``list_by_tenant``.
        rows = self._filtered(
            tenant_id=None,
            status=status,
            user_id=user_id,
            agent_name=agent_name,
            agent_version=agent_version,
            q=q,
            q_thread_ids=q_thread_ids,
            include_archived=include_archived,
            thread_ids=thread_ids,
        )
        return rows[offset : offset + limit]

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
        del nonempty
        return len(
            self._filtered(
                tenant_id=tenant_id,
                status=status,
                user_id=user_id,
                agent_name=agent_name,
                agent_version=agent_version,
                q=q,
                q_thread_ids=q_thread_ids,
                include_archived=include_archived,
                thread_ids=thread_ids,
            )
        )

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
        del nonempty
        return len(
            self._filtered(
                tenant_id=None,
                status=status,
                user_id=user_id,
                agent_name=agent_name,
                agent_version=agent_version,
                q=q,
                q_thread_ids=q_thread_ids,
                include_archived=include_archived,
                thread_ids=thread_ids,
            )
        )

    async def update_status(
        self,
        thread_id: UUID,
        status: ThreadStatus,
        *,
        tenant_id: UUID,
    ) -> bool:
        row = self._rows.get(thread_id)
        if row is None or row.tenant_id != tenant_id:
            return False
        self._rows[thread_id] = row.model_copy(
            update={"status": status, "updated_at": datetime.now(UTC)}
        )
        return True

    async def update_title(
        self,
        thread_id: UUID,
        title: str,
        *,
        tenant_id: UUID,
    ) -> bool:
        row = self._rows.get(thread_id)
        if row is None or row.tenant_id != tenant_id:
            return False
        self._rows[thread_id] = row.model_copy(
            update={"title": title, "updated_at": datetime.now(UTC)}
        )
        return True

    async def update_message_count(
        self,
        thread_id: UUID,
        count: int,
        *,
        tenant_id: UUID,
    ) -> bool:
        row = self._rows.get(thread_id)
        if row is None or row.tenant_id != tenant_id:
            return False
        self._rows[thread_id] = row.model_copy(
            update={"message_count": count, "updated_at": datetime.now(UTC)}
        )
        return True

    async def check_access(self, thread_id: UUID, tenant_id: UUID) -> bool:
        row = self._rows.get(thread_id)
        return row is not None and row.tenant_id == tenant_id

    async def delete(self, thread_id: UUID, *, tenant_id: UUID) -> bool:
        row = self._rows.get(thread_id)
        if row is None or row.tenant_id != tenant_id:
            return False
        del self._rows[thread_id]
        return True
