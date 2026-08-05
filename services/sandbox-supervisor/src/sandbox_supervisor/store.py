"""Persistence for ``sandbox_instance`` — the supervisor's DB access layer.

A :class:`SandboxStore` Protocol keeps the :class:`SandboxSupervisor`
logic testable with an in-memory fake; :class:`DbSandboxStore` is the
SQL-backed production implementation. The per-tenant sandbox limit also
lives here — it reads the ``tenant_quota`` row for the ``sandboxes``
dimension (Stream C.5).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models import SandboxInstanceRow, TenantQuotaRow
from expert_work.persistence.sandbox_instance_store import AGENT_SANDBOX_IMAGE_REF
from expert_work.protocol.quota import QuotaDimension
from sandbox_supervisor.domain import SandboxRecord, SandboxState

#: ``sandbox_instance`` states that count against a tenant's quota.
_ACTIVE_STATES = (SandboxState.CREATING, SandboxState.IN_USE)


class SandboxStore(Protocol):
    """The persistence operations the supervisor needs."""

    async def insert(self, record: SandboxRecord) -> None:
        """Persist a freshly-created ``CREATING`` sandbox row."""

    async def update(self, record: SandboxRecord) -> None:
        """Overwrite the mutable columns of an existing row."""

    async def get(self, sandbox_id: UUID) -> SandboxRecord | None:
        """Return one sandbox by id, or ``None``."""

    async def count_active_for_tenant(self, tenant_id: UUID) -> int:
        """Count the tenant's non-terminal sandboxes (``CREATING`` + ``IN_USE``)."""

    async def list_idle_sessions(self, *, now: datetime, idle_ttl_s: int) -> list[SandboxRecord]:
        """Return ``IN_USE`` sandboxes idle past ``last_used_at + idle_ttl_s``."""

    async def sandbox_limit_for_tenant(self, tenant_id: UUID) -> int | None:
        """Return the tenant's ``sandboxes`` quota, or ``None`` if unset."""

    async def claim_ready(self, record: SandboxRecord) -> bool:
        """CAS-rebind a ``READY`` pool row to a tenant (Stream HX-6).

        Writes ``record``'s full rebind (tenant, thread, limits, state)
        guarded on the row still being ``READY``; returns ``False`` when
        the guard lost (the row was claimed / destroyed concurrently),
        in which case the caller falls back to a cold start.
        """

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        """Phase 3a (purge_user) — hard-delete a user's ``sandbox_instance`` rows.

        Removes every row for ``(tenant_id, user_id)`` (any state) and returns
        the count deleted. Called by :meth:`SandboxSupervisor.mark_workspace_deleted`
        AFTER any live warm session was force-destroyed, so no orphaned
        container is left behind. Tenant- AND user-scoped; NULL-``user_id``
        ephemeral rows are not this user's and are left untouched.

        Unlike ``count_active_for_tenant`` / ``list_idle_sessions``, this
        deliberately does NOT exclude agent-backend rows — purge semantics
        mean deleting everything for this user; an E2B row deleted here
        loses its lease and is reclaimed by the platform's own ≤20-minute
        timeout once nothing renews it (PR-A ruling).
        """


class DbSandboxStore:
    """SQL-backed :class:`SandboxStore` over the ``sandbox_instance`` table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def insert(self, record: SandboxRecord) -> None:
        async with self._sf() as session:
            session.add(_to_row(record))
            await session.commit()

    async def update(self, record: SandboxRecord) -> None:
        async with self._sf() as session:
            await session.execute(
                update(SandboxInstanceRow)
                .where(SandboxInstanceRow.id == record.id)
                .values(
                    container_id=record.container_id,
                    state=record.state.value,
                    acquired_at=record.acquired_at,
                    last_used_at=record.last_used_at,
                    released_at=record.released_at,
                    destroyed_at=record.destroyed_at,
                    destroy_reason=record.destroy_reason,
                )
            )
            await session.commit()

    async def get(self, sandbox_id: UUID) -> SandboxRecord | None:
        async with self._sf() as session:
            row = await session.get(SandboxInstanceRow, sandbox_id)
            return _to_record(row) if row is not None else None

    async def count_active_for_tenant(self, tenant_id: UUID) -> int:
        # 表与 AgentSandboxClient 共用(PR-A):E2B 行不占 docker 配额 ——
        # 云后端的配额语义由云侧自己管(波 2)。
        async with self._sf() as session:
            result = await session.execute(
                select(SandboxInstanceRow.id).where(
                    SandboxInstanceRow.tenant_id == tenant_id,
                    SandboxInstanceRow.state.in_([s.value for s in _ACTIVE_STATES]),
                    SandboxInstanceRow.image_ref != AGENT_SANDBOX_IMAGE_REF,
                )
            )
            return len(result.fetchall())

    async def list_idle_sessions(self, *, now: datetime, idle_ttl_s: int) -> list[SandboxRecord]:
        # A warm session is reaped once idle past ``last_used_at +
        # idle_ttl_s`` (Stream J.15). M0 sandbox counts are small, so
        # fetching IN_USE rows and filtering in Python is simpler than a
        # SQL per-row interval expression.
        #
        # E2B 行交给云侧自己的周期 reap(control_plane.sandbox_reap_worker),
        # docker reaper 对它们既 stop 不动也不该记账(PR-A)。
        async with self._sf() as session:
            result = await session.execute(
                select(SandboxInstanceRow).where(
                    SandboxInstanceRow.state == SandboxState.IN_USE.value,
                    SandboxInstanceRow.image_ref != AGENT_SANDBOX_IMAGE_REF,
                )
            )
            rows = result.scalars().all()
        return [record for row in rows if _session_idle(record := _to_record(row), now, idle_ttl_s)]

    async def sandbox_limit_for_tenant(self, tenant_id: UUID) -> int | None:
        async with self._sf() as session:
            result = await session.execute(
                select(TenantQuotaRow.limit_value).where(
                    TenantQuotaRow.tenant_id == tenant_id,
                    TenantQuotaRow.dimension == QuotaDimension.SANDBOXES.value,
                )
            )
            return result.scalar_one_or_none()

    async def claim_ready(self, record: SandboxRecord) -> bool:
        # Stream HX-6 — the ``state == READY`` guard is the CAS: a row
        # already claimed (IN_USE) or torn down (DESTROYED) matches zero
        # rows and the caller falls back to a cold start. The rebind
        # writes the columns ``update`` never touches (tenant, thread,
        # per-acquire limits) — a claim re-homes the row to its tenant.
        async with self._sf() as session:
            result = await session.execute(
                update(SandboxInstanceRow)
                .where(
                    SandboxInstanceRow.id == record.id,
                    SandboxInstanceRow.state == SandboxState.READY.value,
                )
                .values(
                    tenant_id=record.tenant_id,
                    thread_id=record.thread_id,
                    state=record.state.value,
                    cpu_quota=record.cpu_quota,
                    memory_mb=record.memory_mb,
                    pids_limit=record.pids_limit,
                    timeout_s=record.timeout_s,
                    acquired_at=record.acquired_at,
                    last_used_at=record.last_used_at,
                )
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0) == 1

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        async with self._sf() as session:
            result = await session.execute(
                delete(SandboxInstanceRow).where(
                    SandboxInstanceRow.tenant_id == tenant_id,
                    SandboxInstanceRow.user_id == user_id,
                )
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0)


def _session_idle(record: SandboxRecord, now: datetime, idle_ttl_s: int) -> bool:
    """Whether an ``IN_USE`` session is idle past ``idle_ttl_s``.

    Idleness is measured from the last ``exec`` (``last_used_at``); a
    session that was acquired but never exec'd falls back to
    ``acquired_at``. A row with neither timestamp is left alone.
    """
    anchor = record.last_used_at or record.acquired_at
    return anchor is not None and anchor + timedelta(seconds=idle_ttl_s) < now


def _to_row(record: SandboxRecord) -> SandboxInstanceRow:
    return SandboxInstanceRow(
        id=record.id,
        tenant_id=record.tenant_id,
        user_id=record.user_id,
        workspace_id=record.workspace_id,
        image_ref=record.image_ref,
        node=record.node,
        container_id=record.container_id,
        state=record.state.value,
        thread_id=record.thread_id,
        cpu_quota=record.cpu_quota,
        memory_mb=record.memory_mb,
        pids_limit=record.pids_limit,
        timeout_s=record.timeout_s,
        created_at=record.created_at,
        acquired_at=record.acquired_at,
        last_used_at=record.last_used_at,
        released_at=record.released_at,
        destroyed_at=record.destroyed_at,
        destroy_reason=record.destroy_reason,
    )


def _to_record(row: SandboxInstanceRow) -> SandboxRecord:
    return SandboxRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        image_ref=row.image_ref,
        node=row.node,
        container_id=row.container_id,
        state=SandboxState(row.state),
        thread_id=row.thread_id,
        cpu_quota=float(row.cpu_quota),
        memory_mb=row.memory_mb,
        pids_limit=row.pids_limit,
        timeout_s=row.timeout_s,
        created_at=row.created_at,
        acquired_at=row.acquired_at,
        last_used_at=row.last_used_at,
        released_at=row.released_at,
        destroyed_at=row.destroyed_at,
        destroy_reason=row.destroy_reason,
    )
