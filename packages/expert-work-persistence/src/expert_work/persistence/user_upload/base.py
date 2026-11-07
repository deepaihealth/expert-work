"""Abstract ``UserUploadStore`` repository —— 附件模型统一(spec 2026-08-17)。

Implementations:

* :class:`expert_work.persistence.user_upload.memory.InMemoryUserUploadStore`
* :class:`expert_work.persistence.user_upload.sql.SqlUserUploadStore`

The store is scoped by ``tenant_id`` at the application layer; the SQL
implementation also applies a tenant RLS policy (migration 0146) so a
forgotten WHERE clause cannot cross-leak.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from expert_work.protocol import UserUpload, UserUploadKind

#: B-63 —— ``last_used_at`` 每行每天最多写一次:下载 / 重新附加是热路径,只在上次记的时间
#: 已经早于一天前才真的 UPDATE。保留期按天算,一天的精度足够。
TOUCH_INTERVAL = timedelta(days=1)

#: B-63 —— 租户配置改动最多把「过期 7 天还失败就放弃」的起点往后推这么久。``tenant_config``
#: 的任何写入(MCP 白名单、显示名、状态……)都会刷新 ``updated_at``,不封顶的话一条一直删不掉的
#: 行可以被每周一次的配置改动永远推迟下去。
GIVE_UP_DEFER_CAP = timedelta(days=30)


@dataclass(frozen=True)
class ExpiredUpload:
    """B-63 —— 一条到期的上传,带它**什么时候**到期的(``最后使用 + 保留期``)。

    「最后使用」= ``created_at`` / ``last_used_at`` / 所在会话最后一次 run 的 ``created_at``
    三者中最晚的;保留期 = 租户的 ``tenant_config.upload_retention_days``,没设就用平台默认。
    ``give_up_from`` 是「过了到期日 7 天还在失败就放弃」那个窗口的起点:``max(expires_at,
    租户配置最后一次改动)``,但最晚 ``expires_at + GIVE_UP_DEFER_CAP`` —— 租户刚把天数改小时
    到期日会一下子落到很久以前,窗口从这次改动算起,第一次暂时失败不会直接放弃;封顶是因为
    配置的任何改动都会刷新那个时间。没有租户配置的(内存实现)就是 ``expires_at``。
    """

    upload: UserUpload
    expires_at: datetime
    give_up_from: datetime


class UserUploadStore(abc.ABC):
    """Per-(tenant, user) registry of landed third-party attachments.

    ``get`` intentionally filters only on ``(id, tenant_id)`` — it does
    **not** filter on ``user_id`` or ``deleted_at`` — the caller compares
    those itself (same rule as :class:`ImageUploadStore.get`).
    """

    @abc.abstractmethod
    async def insert(
        self,
        *,
        upload_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
        thread_id: UUID,
        kind: UserUploadKind,
        ref: str,
        mime_type: str,
        size_bytes: int,
        filename: str,
        sha256: str | None = None,
    ) -> UserUpload:
        """Persist one row for a successful upload.

        ``sha256`` (B-271) is the hex digest of the landed bytes; the retention
        sweep compares it before deleting the file.

        Returns the materialised row including the server-assigned
        ``created_at`` so the caller can echo it back in the API
        response without a re-read.
        """

    @abc.abstractmethod
    async def get(self, *, upload_id: UUID, tenant_id: UUID) -> UserUpload | None:
        """Return the row by id, or ``None`` when the id is unknown or
        belongs to a different tenant. Does **not** filter on ``user_id``
        or ``deleted_at`` — the caller compares both itself."""

    @abc.abstractmethod
    async def list_documents_for_thread(
        self, *, tenant_id: UUID, thread_id: UUID
    ) -> list[UserUpload]:
        """B-262 P3 —— 这个会话自己的文档类上传(没删的),按上传先后。老对话迁移按它找本对话
        的上传件。"""

    @abc.abstractmethod
    async def list_expired(
        self, *, now: datetime, default_days: int, limit: int = 1000
    ) -> list[ExpiredUpload]:
        """B-63 —— 跨租户列出没删(``deleted_at IS NULL``)、按**最后使用**已经到期的行,
        最早到期的在前。

        最后使用 = ``greatest(created_at, last_used_at, 所在会话最后一次 run 的
        created_at)``;到期 = 最后使用 + 保留期 ``< now``,保留期取这一行租户的
        ``tenant_config.upload_retention_days``,为空(或租户没有配置行)时用 ``default_days``。
        只会比按 ``created_at`` 算留得更久,不会更短。

        Caller MUST be inside an RLS bypass scope (the retention job's
        ``_bypass_rls``)。The in-memory backend has no runs and no tenant config:
        it uses ``created_at`` / ``last_used_at`` and ``default_days`` only."""

    @abc.abstractmethod
    async def list_unexpired_by_ref(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        ref: str,
        now: datetime,
        default_days: int,
    ) -> list[UserUpload]:
        """B-271 / B-63 —— 同租户、同用户、同 ``ref``、没删、按 :meth:`list_expired` 同一个
        时钟**还没到期**的行,不论会话。留存作业的引用检查用它找「还有谁可能指着同一个文件」
        (老视图同一 agent 的各对话共用 ``uploads/``,同名同内容复用让多行指向一个文件)。
        已经到期、只是这一批没轮到的行不算引用。"""

    @abc.abstractmethod
    async def touch(self, *, upload_id: UUID, tenant_id: UUID, now: datetime) -> bool:
        """B-63 —— 记一次使用:``last_used_at = now``,只在这一行没删、且上次记的时间为空或
        早于 ``now - TOUCH_INTERVAL`` 时才写(每行每天最多一次)。``True`` = 真的写了。"""

    @abc.abstractmethod
    async def soft_delete(self, *, upload_id: UUID, tenant_id: UUID, now: datetime) -> bool:
        """Flip ``deleted_at`` on an active row; ``True`` on that transition.

        ``False`` when the id is unknown for this tenant or already
        soft-deleted (idempotent). The external read surfaces already treat
        ``deleted_at IS NOT NULL`` as 404 (``external_uploads.py``), so this is
        the whole "expired" state — no new column."""

    @abc.abstractmethod
    async def hard_delete(self, *, upload_id: UUID, tenant_id: UUID) -> bool:
        """B-293 —— 删掉一行(不留墓碑);``True`` = 删到了。只给「刚登记、id 还没回给对接方」
        的行用:上传落地期间会话被撤,登记行随之撤回。"""

    @abc.abstractmethod
    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        """Phase 3a (purge_user) — hard-delete EVERY upload row for a user.

        Tenant- AND user-scoped — the ``(tenant_id, user_id)`` predicate
        never touches another tenant's or user's rows. Returns the count
        deleted (0 when none / re-purge)."""

    @abc.abstractmethod
    async def count_for_threads(self, *, tenant_id: UUID, thread_ids: Sequence[UUID]) -> int:
        """B-278 —— rows ``delete_for_threads`` would remove (agent purge preview)."""

    @abc.abstractmethod
    async def delete_for_threads(self, *, tenant_id: UUID, thread_ids: Sequence[UUID]) -> int:
        """B-278 —— hard-delete the upload registry rows of these conversations;
        tenant-scoped, returns the count deleted."""

    @abc.abstractmethod
    async def tombstone_for_threads(
        self, *, tenant_id: UUID, thread_ids: Sequence[UUID], now: datetime
    ) -> int:
        """B-280 —— conversation purge. Live **document** rows of these
        conversations become tombstones (see
        :func:`expert_work.protocol.is_session_deleted_tombstone`: content and
        identity columns scrubbed, ``deleted_at = now``) so a later reference
        from another conversation still answers ``UPLOAD_SESSION_DELETED``.
        Image rows and rows already soft-deleted (expired) are hard-deleted —
        both answered ``UPLOAD_NOT_FOUND`` before the purge and still do.
        Existing tombstones are left alone (re-runnable). Tenant-scoped;
        returns the rows tombstoned + deleted."""
