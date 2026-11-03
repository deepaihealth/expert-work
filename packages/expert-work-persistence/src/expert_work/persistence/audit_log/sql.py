"""SQLAlchemy-backed ``AuditLogStore`` (Postgres / asyncpg)."""

from __future__ import annotations

from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.audit_log.base import AuditLogStore
from expert_work.persistence.audit_log.cursor import decode_cursor, encode_cursor
from expert_work.persistence.models import AuditLogRow
from expert_work.protocol import AuditAction, AuditEntry, AuditPage, AuditQuery, AuditResult

# Per migration 0008 + STREAM-D-DESIGN § 2.2 / Mini-ADR D-1: the
# application's main connection role does not hold ``INSERT`` on
# ``audit_log``. The append path must temporarily ``SET LOCAL ROLE``
# to the dedicated ``audit_writer`` (NOLOGIN, INSERT + SELECT only)
# inside the transaction. ``SET LOCAL`` is scoped to the current
# transaction, so commit / rollback automatically resets it.
_AUDIT_WRITER_ROLE: Final[str] = "audit_writer"
_SET_AUDIT_WRITER_ROLE = text(f"SET LOCAL ROLE {_AUDIT_WRITER_ROLE}")

# Cross-tenant admin read (``query(tenant_id="*")`` under
# ``applied_scope(CrossTenant)``) runs with ``app.tenant_id`` unset. ``audit_log``
# is FORCE ROW LEVEL SECURITY, so the non-BYPASSRLS app role would see ZERO rows
# (policy ``tenant_id = NULL`` → false). The read must ``SET LOCAL ROLE`` to the
# ``audit_reader`` (NOLOGIN BYPASSRLS, migration 0005, already GRANTed SELECT on
# audit_log) — the same idiom as the writer path. Single-tenant queries keep the
# app role + the tenant GUC, so the per-tenant RLS policy still scopes them.
_AUDIT_READER_ROLE: Final[str] = "audit_reader"
_SET_AUDIT_READER_ROLE = text(f"SET LOCAL ROLE {_AUDIT_READER_ROLE}")


def _row_to_entry(row: AuditLogRow) -> AuditEntry:
    return AuditEntry(
        id=row.id,
        tenant_id=row.tenant_id,
        actor_type=row.actor_type,  # type: ignore[arg-type]
        actor_id=row.actor_id,
        on_behalf_of=row.on_behalf_of,
        action=AuditAction(row.action),
        resource_type=row.resource_type,  # type: ignore[arg-type]
        resource_id=row.resource_id,
        result=AuditResult(row.result),
        reason=row.reason,
        ip=row.ip,
        user_agent=row.user_agent,
        request_id=row.request_id,
        trace_id=row.trace_id,
        details=row.details,
        occurred_at=row.occurred_at,
    )


class SqlAuditLogStore(AuditLogStore):
    """Postgres-backed audit log repository."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def append(self, entry: AuditEntry) -> AuditEntry:
        row = AuditLogRow(
            tenant_id=entry.tenant_id,
            actor_type=entry.actor_type,
            actor_id=entry.actor_id,
            on_behalf_of=entry.on_behalf_of,
            action=entry.action.value,
            resource_type=entry.resource_type,
            resource_id=entry.resource_id,
            result=entry.result.value,
            reason=entry.reason,
            ip=entry.ip,
            user_agent=entry.user_agent,
            request_id=entry.request_id,
            trace_id=entry.trace_id,
            details=entry.details,
        )
        async with self._sf() as session:
            # ``SET LOCAL`` must run *inside* the open transaction; the
            # first ``execute`` here also implicitly begins it. The
            # role lifts on commit/rollback, so subsequent statements
            # outside this block use the connection's default role.
            await session.execute(_SET_AUDIT_WRITER_ROLE)
            session.add(row)
            # Flush so RETURNING populates the autoincrement ``id``,
            # then refresh so server defaults (``occurred_at``,
            # ``backup_acked``) load — both happen while the
            # ``audit_writer`` role is still active (BYPASSRLS, has
            # SELECT). Snapshot into a Pydantic entry BEFORE commit;
            # otherwise ``expire_on_commit`` re-fires SELECTs under
            # the connection's default role which can't see the row
            # under RLS.
            await session.flush()
            await session.refresh(row)
            entry = _row_to_entry(row)
            await session.commit()
            return entry

    async def get_by_id(self, audit_id: int, *, tenant_id: UUID) -> AuditEntry | None:
        async with self._sf() as session:
            row = await session.get(AuditLogRow, audit_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return _row_to_entry(row)

    async def list_document_uploads_for_thread(
        self, *, tenant_id: UUID, thread_id: UUID, since: datetime | None = None
    ) -> list[AuditEntry]:
        # ``audit_log_action_idx (tenant_id, action, occurred_at)`` 把扫描收在本租户、本动作、
        # ``since`` 之后;``details->>'thread_id'`` 没有索引,在那批行上过滤。
        stmt = select(AuditLogRow).where(
            AuditLogRow.tenant_id == tenant_id,
            AuditLogRow.action == AuditAction.DOCUMENT_UPLOAD.value,
            AuditLogRow.result == AuditResult.SUCCESS.value,
            AuditLogRow.details["thread_id"].astext == str(thread_id),
        )
        if since is not None:
            stmt = stmt.where(AuditLogRow.occurred_at >= since)
        stmt = stmt.order_by(AuditLogRow.id.asc())
        async with self._sf() as session:
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_entry(r) for r in rows]

    async def query(self, query: AuditQuery) -> AuditPage:
        stmt = select(AuditLogRow)
        if query.tenant_id != "*":
            stmt = stmt.where(AuditLogRow.tenant_id == query.tenant_id)
        if query.actor_id is not None:
            stmt = stmt.where(AuditLogRow.actor_id == query.actor_id)
        if query.action is not None:
            stmt = stmt.where(AuditLogRow.action == query.action.value)
        if query.resource_type is not None:
            stmt = stmt.where(AuditLogRow.resource_type == query.resource_type)
        if query.resource_id is not None:
            stmt = stmt.where(AuditLogRow.resource_id == query.resource_id)
        if query.result is not None:
            stmt = stmt.where(AuditLogRow.result == query.result.value)
        if query.from_ts is not None:
            stmt = stmt.where(AuditLogRow.occurred_at >= query.from_ts)
        if query.to_ts is not None:
            stmt = stmt.where(AuditLogRow.occurred_at <= query.to_ts)
        if query.cursor is not None:
            cursor_id = decode_cursor(query.cursor)
            stmt = stmt.where(AuditLogRow.id < cursor_id)

        # Fetch limit + 1 to know whether a next page exists without a
        # follow-up COUNT query.
        stmt = stmt.order_by(AuditLogRow.id.desc()).limit(query.limit + 1)

        async with self._sf() as session:
            if query.tenant_id == "*":
                # Cross-tenant admin read: assume the BYPASSRLS reader role so
                # FORCE RLS doesn't filter every row to zero (the GUC is unset
                # on this path). ``SET LOCAL`` lifts on commit/rollback.
                await session.execute(_SET_AUDIT_READER_ROLE)
            rows = list((await session.execute(stmt)).scalars().all())

        has_more = len(rows) > query.limit
        page_rows = rows[: query.limit]
        entries = [_row_to_entry(r) for r in page_rows]
        next_cursor: str | None = None
        if has_more and page_rows:
            last_id = page_rows[-1].id
            if last_id is not None:
                next_cursor = encode_cursor(last_id)
        return AuditPage(entries=entries, next_cursor=next_cursor)
