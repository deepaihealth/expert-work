"""Abstract :class:`TenantConfigStore` — Stream C.7."""

from __future__ import annotations

import abc
from uuid import UUID

from expert_work.protocol import TenantConfigPatch, TenantConfigRecord, TenantPlan


class TenantConfigNotFoundError(Exception):
    """No ``tenant_config`` row exists for the requested tenant."""

    def __init__(self, *, tenant_id: UUID) -> None:
        super().__init__(f"tenant_config not found for tenant_id={tenant_id}")
        self.tenant_id = tenant_id


class TenantConfigAlreadyExistsError(Exception):
    """A ``tenant_config`` row already exists for the requested tenant.

    Raised by :meth:`TenantConfigStore.create` — Stream P (Mini-ADR P-3).
    ``create`` is the explicit "provision a new tenant" path and must fail
    loudly on a pre-existing tenant rather than silently overwriting it the
    way :meth:`upsert` would.
    """

    def __init__(self, *, tenant_id: UUID) -> None:
        super().__init__(f"tenant_config already exists for tenant_id={tenant_id}")
        self.tenant_id = tenant_id


class RetentionDecreaseNotConfirmedError(Exception):
    """B-63 —— 这次写入会把附件 / 产物保留期改小,而调用方没有确认。

    比较在 :meth:`TenantConfigStore.upsert` 里、锁住那一行之后做(与写入同一个事务),所以
    并发的另一次改动不会让一次没确认的改小漏过去。``decreases`` = ``[{field, from, to}]``。
    """

    def __init__(self, decreases: list[dict[str, object]]) -> None:
        super().__init__(f"unconfirmed retention decrease: {decreases}")
        self.decreases = decreases


class TenantConfigStore(abc.ABC):
    """Persistence Protocol for the per-tenant runtime config row."""

    @abc.abstractmethod
    async def get(self, *, tenant_id: UUID) -> TenantConfigRecord | None:
        """Return the row, or None if no config has been seeded yet."""

    @abc.abstractmethod
    async def create(
        self,
        *,
        tenant_id: UUID,
        display_name: str,
        plan: TenantPlan | None = None,
        actor_id: str,
    ) -> TenantConfigRecord:
        """Provision a new tenant — write the first ``tenant_config`` row.

        Stream P (Mini-ADR P-1/P-3): the explicit tenant-creation path behind
        ``POST /v1/tenants``. Only ``display_name`` (and optionally ``plan``)
        are set; every other field takes its column default and is tuned
        later via :meth:`upsert`.

        Raises :class:`TenantConfigAlreadyExistsError` if a row already exists
        for ``tenant_id`` (unlike :meth:`upsert`, which merges).
        """

    @abc.abstractmethod
    async def upsert(
        self,
        *,
        tenant_id: UUID,
        patch: TenantConfigPatch,
        actor_id: str,
        reject_unconfirmed_decrease: bool = False,
    ) -> TenantConfigRecord:
        """Insert-or-merge the patch. ``display_name`` is required for first insert.

        B-63 —— ``reject_unconfirmed_decrease=True``:这个 patch 若把保留期改小(按锁住的现值比,
        见 :meth:`TenantConfigPatch.retention_decreases`),抛
        :class:`RetentionDecreaseNotConfirmedError`、什么都不写。"""

    @abc.abstractmethod
    async def add_mcp_allowlist_name(
        self, *, tenant_id: UUID, name: str, actor_id: str
    ) -> tuple[TenantConfigRecord, bool]:
        """Atomically append ``name`` to ``mcp_allowlist`` (idempotent).

        BUG-1(2026-08-24):enable/disable 端点原先「读(可能是 60s 陈旧缓存)
        →改→整表覆盖写」,多副本下互相抹掉对方刚加的名字。合并语义必须在
        store 内原子完成(SQL 走行锁事务,in-memory 走 store 锁),调用方
        永远不携带自己读到的旧列表。

        Returns ``(record, changed)`` — ``changed`` is False when the name
        was already present. Raises :class:`TenantConfigNotFoundError` if the
        tenant has no config row.
        """

    @abc.abstractmethod
    async def remove_mcp_allowlist_name(
        self, *, tenant_id: UUID, name: str, actor_id: str
    ) -> tuple[TenantConfigRecord, bool]:
        """Atomically remove ``name`` from ``mcp_allowlist`` (idempotent).

        Mirror of :meth:`add_mcp_allowlist_name` — same atomicity contract,
        ``changed`` is False when the name was already absent.
        """

    @abc.abstractmethod
    async def set_status(
        self, *, tenant_id: UUID, status: str, actor_id: str
    ) -> TenantConfigRecord:
        """Set tenant lifecycle status ('active'|'suspended'). Raises
        TenantConfigNotFoundError if the tenant has no config row."""

    @abc.abstractmethod
    async def list_all(self, *, limit: int = 50, offset: int = 0) -> list[TenantConfigRecord]:
        """Return tenant config rows ordered by ``created_at`` (oldest first).

        Platform-level cross-tenant read behind ``GET /v1/tenants``
        (system_admin only). Paginated via ``limit``/``offset``.
        """

    @abc.abstractmethod
    async def clear_default_agent_name(self, *, tenant_id: UUID, agent_name: str) -> bool:
        """B-278 —— set ``default_agent_name`` to NULL **only if** it is still
        ``agent_name`` (the agent being purged). Conditional in one statement so
        an admin's concurrent switch to another default is never undone.
        Returns whether the row changed; no row / another default → ``False``."""
