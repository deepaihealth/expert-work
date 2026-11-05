"""Abstract ``AuditLogStore`` repository.

Design: [subsystems/17-audit-log § 4.1](../../../../docs/architecture/subsystems/17-audit-log.md).

Implementations:

- :class:`expert_work.persistence.audit_log.memory.InMemoryAuditLogStore`
- :class:`expert_work.persistence.audit_log.sql.SqlAuditLogStore`

This is the data-layer Repository. The higher-level ``AuditLogger`` service
(Stream A.4 batch 2) wraps a store with PII redaction, fallback queue, and
self-audit on read.
"""

from __future__ import annotations

import abc
from collections.abc import Collection
from datetime import datetime
from uuid import UUID

from expert_work.protocol import AuditEntry, AuditPage, AuditQuery


class AuditLogStore(abc.ABC):
    """Append-only audit log repository.

    The store **does not** enforce who may write or query; that is the job
    of :class:`AuditLogger` (Stream A.4 batch 2). The store assumes its
    caller has already done tenant + role checks.

    Append-only: there is no ``update`` / ``delete`` method. M1 layers a DB
    role with ``REVOKE UPDATE, DELETE`` on top to enforce this at the
    database level (per subsystems/17 § 9 M1).
    """

    @abc.abstractmethod
    async def append(self, entry: AuditEntry) -> AuditEntry:
        """Insert one row and return the entry with ``id`` + ``occurred_at`` set.

        Pre-conditions:
        - Caller has already redacted PII / secrets from ``entry.details``.
        - ``entry.action`` is in :class:`AuditAction` (Pydantic guarantees this).
        """

    @abc.abstractmethod
    async def get_by_id(self, audit_id: int, *, tenant_id: UUID) -> AuditEntry | None:
        """Read one row by id, filtered to ``tenant_id``.

        Returns ``None`` when the row does not exist or belongs to a
        different tenant — never reveals cross-tenant existence.
        """

    @abc.abstractmethod
    async def list_document_uploads_for_thread(
        self, *, tenant_id: UUID, thread_id: UUID, since: datetime | None = None
    ) -> list[AuditEntry]:
        """B-262 P3 —— 这个会话的成功 ``document:upload`` 审计行(``details.thread_id``),
        ``since`` 起,按发生先后。老对话迁移按它找控制台上传(那些上传没有登记行)。"""

    @abc.abstractmethod
    async def list_document_uploads_for_threads(
        self,
        *,
        tenant_id: UUID,
        thread_ids: Collection[UUID],
        since: datetime,
        until: datetime,
    ) -> list[AuditEntry]:
        """B-271 —— 一批会话的成功 ``document:upload`` 审计行(``details.thread_id`` 在
        ``thread_ids`` 里,``since <= occurred_at <= until``),按发生先后。留存作业按它给没有
        sha256 的老上传行找登记时的 sha256 —— 一个租户一次读,不逐行查。"""

    @abc.abstractmethod
    async def query(self, query: AuditQuery) -> AuditPage:
        """Paginated query, newest-first.

        ``query.tenant_id='*'`` returns rows across every tenant; the
        caller (``AuditLogger.query``) is responsible for verifying the
        principal holds the admin role *before* invoking this code path.
        """
