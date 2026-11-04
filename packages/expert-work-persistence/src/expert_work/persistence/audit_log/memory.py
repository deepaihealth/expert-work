"""In-memory ``AuditLogStore`` for unit tests."""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Iterable
from datetime import UTC, datetime
from uuid import UUID

from expert_work.persistence.audit_log.base import AuditLogStore
from expert_work.persistence.audit_log.cursor import decode_cursor, encode_cursor
from expert_work.protocol import AuditAction, AuditEntry, AuditPage, AuditQuery, AuditResult


class InMemoryAuditLogStore(AuditLogStore):
    """Process-local store, sorted by insertion id (newest first on query).

    Thread-safe across asyncio tasks via :class:`asyncio.Lock` around the
    ``append`` path — concurrent appends must not race on ``_next_id``.
    """

    def __init__(self) -> None:
        self._rows: dict[int, AuditEntry] = {}
        self._next_id: int = 1
        self._lock = asyncio.Lock()

    async def append(self, entry: AuditEntry) -> AuditEntry:
        async with self._lock:
            audit_id = self._next_id
            self._next_id += 1
            stamped = entry.model_copy(
                update={
                    "id": audit_id,
                    "occurred_at": entry.occurred_at or datetime.now(UTC),
                }
            )
            self._rows[audit_id] = stamped
            return stamped

    async def get_by_id(self, audit_id: int, *, tenant_id: UUID) -> AuditEntry | None:
        row = self._rows.get(audit_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def list_document_uploads_for_thread(
        self, *, tenant_id: UUID, thread_id: UUID, since: datetime | None = None
    ) -> list[AuditEntry]:
        rows = [
            r
            for r in self._rows.values()
            if r.tenant_id == tenant_id
            and r.action == AuditAction.DOCUMENT_UPLOAD
            and r.result == AuditResult.SUCCESS
            and (r.details or {}).get("thread_id") == str(thread_id)
            and (since is None or (r.occurred_at is not None and r.occurred_at >= since))
        ]
        return sorted(rows, key=lambda r: r.id or 0)

    async def list_document_uploads_for_threads(
        self,
        *,
        tenant_id: UUID,
        thread_ids: Collection[UUID],
        since: datetime,
        until: datetime,
    ) -> list[AuditEntry]:
        wanted = {str(t) for t in thread_ids}
        rows = [
            r
            for r in self._rows.values()
            if r.tenant_id == tenant_id
            and r.action == AuditAction.DOCUMENT_UPLOAD
            and r.result == AuditResult.SUCCESS
            and (r.details or {}).get("thread_id") in wanted
            and r.occurred_at is not None
            and since <= r.occurred_at <= until
        ]
        return sorted(rows, key=lambda r: r.id or 0)

    async def query(self, query: AuditQuery) -> AuditPage:
        cursor_id = decode_cursor(query.cursor) if query.cursor else None

        matches = list(self._filter(self._rows.values(), query, cursor_id))
        matches.sort(key=lambda r: r.id or 0, reverse=True)

        page = matches[: query.limit]
        next_cursor: str | None = None
        if len(matches) > query.limit:
            last_id = page[-1].id
            if last_id is not None:
                next_cursor = encode_cursor(last_id)

        return AuditPage(entries=page, next_cursor=next_cursor)

    @staticmethod
    def _filter(
        rows: Iterable[AuditEntry],
        query: AuditQuery,
        cursor_id: int | None,
    ) -> Iterable[AuditEntry]:
        for row in rows:
            if query.tenant_id != "*" and row.tenant_id != query.tenant_id:
                continue
            if query.actor_id is not None and row.actor_id != query.actor_id:
                continue
            if query.action is not None and row.action != query.action:
                continue
            if query.resource_type is not None and row.resource_type != query.resource_type:
                continue
            if query.resource_id is not None and row.resource_id != query.resource_id:
                continue
            if query.result is not None and row.result != query.result:
                continue
            if query.from_ts is not None and (
                row.occurred_at is None or row.occurred_at < query.from_ts
            ):
                continue
            if query.to_ts is not None and (
                row.occurred_at is None or row.occurred_at > query.to_ts
            ):
                continue
            if cursor_id is not None and (row.id is None or row.id >= cursor_id):
                continue
            yield row
