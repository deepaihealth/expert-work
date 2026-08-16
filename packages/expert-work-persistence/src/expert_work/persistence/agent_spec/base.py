"""Abstract :class:`AgentSpecStore` — Stream B.5."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from uuid import UUID

from expert_work.protocol import (
    AgentSpec,
    AgentSpecRecord,
    AgentSpecRevisionRecord,
    AgentSpecStatus,
)


@dataclass(frozen=True)
class AgentSpecUpdateResult:
    """Outcome of :meth:`AgentSpecStore.update_spec` — Stream HX-5.

    ``revision`` is the history row this update appended, or ``None``
    for a no-op (the new sha equals the stored one — nothing changed,
    nothing recorded). ``prev_sha256`` feeds the MANIFEST_WRITE audit
    so every change carries its before/after pair.
    """

    record: AgentSpecRecord
    revision: int | None
    prev_sha256: str


class DuplicateAgentSpecError(Exception):
    """Raised when the unique ``(tenant_id, name, version)`` index trips
    on insert. The API layer maps it to ``HTTP 409``."""

    def __init__(self, *, tenant_id: UUID, name: str, version: str) -> None:
        super().__init__(
            f"agent_spec already exists: tenant_id={tenant_id} name={name} version={version}"
        )
        self.tenant_id = tenant_id
        self.name = name
        self.version = version


class AgentSpecStore(abc.ABC):
    """Per-tenant manifest registry.

    Every method takes ``tenant_id`` explicitly. Reads / writes filtered
    on a different tenant return ``None`` / ``False`` rather than the
    record, so cross-tenant existence never leaks.
    """

    @abc.abstractmethod
    async def create(
        self,
        *,
        tenant_id: UUID,
        spec: AgentSpec,
        spec_sha256: str,
        created_by: str,
    ) -> AgentSpecRecord:
        """Insert a new ``ACTIVE`` row; raises :class:`DuplicateAgentSpecError`
        if ``(tenant_id, name, version)`` already exists."""

    @abc.abstractmethod
    async def get(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        include_deleted: bool = False,
    ) -> AgentSpecRecord | None:
        """Fetch one row; defaults skip soft-deleted entries."""

    @abc.abstractmethod
    async def list_by_tenant(
        self,
        *,
        tenant_id: UUID,
        status: AgentSpecStatus | None = None,
        name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AgentSpecRecord]:
        """Paginated list, newest first."""

    @abc.abstractmethod
    async def list_distinct_active_by_tenant(
        self, *, tenant_id: UUID, limit: int = 100, offset: int = 0
    ) -> list[AgentSpecRecord]:
        """每个 name 一条(最新的 ACTIVE 版本),按 name 排序,分页。

        阶段 3 (3.1) —— ``GET /v1/agent-catalog`` 列的是 **agent**,而
        :meth:`list_by_tenant` 分页的是**版本行**。一个 name 有多个 ACTIVE
        版本行是常态(发新版本只 create 一行,没有代码把旧版本降级),所以
        「先分页、再按 name 去重」会让去重后的页短于 ``limit`` —— 客户端按
        「不足一页即最后一页」的标准循环会**静默漏掉**后面的 agent,而响应里
        没有 ``total``,它无从察觉。

        每个 name 保留 ``created_at`` 最新的那一行——绝大多数情况下和
        ``agents.py:_resolve_session`` 用 ``limit=1``(即 ``list_by_tenant``,
        只 ``ORDER BY created_at DESC``,**没有** ``id`` 次级键)取到的是同一
        行。但 ``created_at`` 完全相同时,本方法用 ``id DESC`` 定序而
        ``_resolve_session`` 未定序,两者选出的行**可能不是同一个**——目录
        显示的 ``display_name`` / ``description`` 会跟实际被 run 端点执行的
        版本对不上。这是既有行为,不在本方法要解决的问题范围内。

        排序键是 ``name``(不是 ``created_at``):分页要求全序稳定,而
        ``created_at`` 在去重后不再唯一。同一 name 的 ``created_at`` 撞车时
        用 ``id`` 降序做次级 tie-break——两个后端必须用同一个 tie-break 键,
        否则同一批数据在两边可能选出不同的"最新"行。
        """

    @abc.abstractmethod
    async def count_distinct_active_by_tenant(self, *, tenant_id: UUID) -> int:
        """去重后(按 name)这个租户有多少个 ACTIVE agent——不分页。

        配 :meth:`list_distinct_active_by_tenant` 使用,让 ``GET
        /v1/agent-catalog`` 能在响应里给出 ``total``,客户端才有办法判断
        "还有没有下一页",而不是只能靠"这页数量 < limit"去猜——那个启发式
        在最后一页恰好凑满 limit 时会给出错误的"还有更多"信号。
        """

    @abc.abstractmethod
    async def list_all_tenants(
        self,
        *,
        status: AgentSpecStatus | None = None,
        name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AgentSpecRecord]:
        """Cross-tenant list — Stream N (Mini-ADR N-4).

        Caller MUST be inside a ``bypass_rls_session()`` (or ``applied_scope``
        with a :class:`CrossTenant` resolution); otherwise RLS filters
        all rows out. Used only by ``system_admin`` requests with
        ``tenant_id=*``.
        """

    @abc.abstractmethod
    async def update_spec(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        spec: AgentSpec,
        spec_sha256: str,
        updated_by: str,
    ) -> AgentSpecUpdateResult | None:
        """Replace the spec payload + sha256 in place, appending one
        ``agent_spec_revision`` history row in the same transaction
        (Stream HX-5; a same-sha update is a recorded no-op — no new
        revision). Returns the result, or ``None`` if no row matched
        (404)."""

    @abc.abstractmethod
    async def list_revisions(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AgentSpecRevisionRecord]:
        """Revision history for one manifest, newest first — Stream HX-5."""

    @abc.abstractmethod
    async def get_revision(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        revision: int,
    ) -> AgentSpecRevisionRecord | None:
        """One revision snapshot, or ``None`` (unknown / cross-tenant)."""

    @abc.abstractmethod
    async def update_status(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        status: AgentSpecStatus,
    ) -> AgentSpecRecord | None:
        """Update status. Returns the updated record, or ``None`` if no
        row matched. Used by the soft-delete path and the deprecate UI."""
