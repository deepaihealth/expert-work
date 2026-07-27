"""``TenantConfigService`` — Stream C.7.

Thin wrapper around :class:`TenantConfigStore` that:

1. Caches the resolved :class:`TenantConfigRecord` per ``tenant_id``
   with a 60-second TTL (STREAM-C-DESIGN § 2.8 — keeps hot-path
   reads off the database while still bounding propagation delay
   after an admin edits the row).
2. Invalidates the cache on ``upsert`` so the next ``get`` returns
   the fresh row even within the TTL window.
3. Emits ``tenant_config:read`` / ``tenant_config:write`` audit
   events. The read audit is **rate-limited via the cache**: only
   the first read per ``ttl`` window per tenant emits a row, which
   is the right granularity for "we accessed this tenant's secret
   refs / PII fields" without producing per-request log spam.

Stream E (LLM gateway, MCP gateway) will inject this service and
call ``await config_service.get(tenant_id)`` on the request hot
path; the cache hit is the common case.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from control_plane.audit import emit
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.tenant_config import TenantConfigStore
from expert_work.protocol import (
    AuditAction,
    TenantConfigPatch,
    TenantConfigRecord,
    TenantPlan,
)
from expert_work.runtime.audit.logger import AuditLogger

logger = logging.getLogger("expert_work.control_plane.tenant_config")

_DEFAULT_TTL_S: Final[float] = 60.0


class TenantConfigNotConfiguredError(LookupError):
    """``get`` failed because the tenant has no ``tenant_config`` row yet."""

    def __init__(self, *, tenant_id: UUID) -> None:
        super().__init__(f"tenant_config not configured for tenant_id={tenant_id}")
        self.tenant_id = tenant_id


@dataclass
class _CacheEntry:
    record: TenantConfigRecord
    expires_at_monotonic: float


class TenantConfigService:
    """Cached, audit-emitting facade over :class:`TenantConfigStore`."""

    def __init__(
        self,
        *,
        store: TenantConfigStore,
        audit_logger: AuditLogger,
        ttl_s: float = _DEFAULT_TTL_S,
    ) -> None:
        if ttl_s <= 0:
            msg = "ttl_s must be positive"
            raise ValueError(msg)
        self._store = store
        self._audit = audit_logger
        self._ttl_s = ttl_s
        self._cache: dict[UUID, _CacheEntry] = {}

    async def get(
        self,
        *,
        tenant_id: UUID,
        actor_id: str | None = None,
    ) -> TenantConfigRecord:
        """Return the cached config, refreshing from the store on miss.

        ``actor_id`` is included on the audit row when supplied (HTTP
        callers always have one via the principal; internal callers
        from E may pass ``None`` if no audit attribution is required).
        Raises :class:`TenantConfigNotConfiguredError` when the row
        does not exist yet — this is the configured-but-empty signal
        a tenant gets before an admin seeds the row.
        """
        now = time.monotonic()
        entry = self._cache.get(tenant_id)
        if entry is not None and entry.expires_at_monotonic > now:
            return entry.record

        record = await self._store.get(tenant_id=tenant_id)
        if record is None:
            raise TenantConfigNotConfiguredError(tenant_id=tenant_id)

        self._cache[tenant_id] = _CacheEntry(record=record, expires_at_monotonic=now + self._ttl_s)

        # Audit only on cache-miss → bounded volume (~1 per tenant per
        # minute under load) even when consumed on the hot path.
        if actor_id is not None:
            try:
                await emit(
                    self._audit,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    action=AuditAction.TENANT_CONFIG_READ,
                    resource_type="tenant_config",
                    resource_id=str(tenant_id),
                    trace_id=current_trace_id_hex(),
                )
            except Exception:
                logger.exception("tenant_config.read.audit_emit_failed")
        return record

    async def upsert(
        self,
        *,
        tenant_id: UUID,
        patch: TenantConfigPatch,
        actor_id: str,
    ) -> TenantConfigRecord:
        """Insert or merge, invalidate the cache, audit the write."""
        record = await self._store.upsert(tenant_id=tenant_id, patch=patch, actor_id=actor_id)
        # Prime the cache with the fresh row so the very next ``get``
        # is a hit (saves one round trip on read-after-write).
        self._cache[tenant_id] = _CacheEntry(
            record=record,
            expires_at_monotonic=time.monotonic() + self._ttl_s,
        )
        try:
            await emit(
                self._audit,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action=AuditAction.TENANT_CONFIG_WRITE,
                resource_type="tenant_config",
                resource_id=str(tenant_id),
                trace_id=current_trace_id_hex(),
                details={
                    "fields": sorted(
                        k for k, v in patch.model_dump(exclude_unset=True).items() if v is not None
                    ),
                },
            )
        except Exception:
            logger.exception("tenant_config.write.audit_emit_failed")
        return record

    def invalidate(self, tenant_id: UUID) -> None:
        """Drop the cached entry. Useful for tests + admin-driven flushes."""
        self._cache.pop(tenant_id, None)


class ServiceBackedTenantConfigStore(TenantConfigStore):
    """二期 P1.2 —— 给 orchestrator 的 MemoryEnv 用的 store 适配器。

    recall 节点每次召回读一次 tenant_config(只用 memory_recall_mode 一个
    字段);裸 repo 是每次一条真 DB 读(基线 15ms/召回)。这个适配器把读
    委托给 :class:`TenantConfigService`,复用它现有的 per-tenant TTL 缓存
    与 upsert 失效/prime —— 不新建任何缓存。

    为什么不直接把共享 repo 包一层缓存:TenantStatusService(kill switch)
    用秒级 TTL 消费同一 repo,repo 层长缓存会架空急停传播。

    ``get`` 把 service 的 :class:`TenantConfigNotConfiguredError` 转回
    store 接口约定的 ``None``(recall 节点靠 None 走默认 hybrid);
    ``actor_id=None`` 使 service 不发读审计 —— 热路径零审计噪音。
    """

    def __init__(self, *, service: TenantConfigService) -> None:
        self._service = service

    async def get(self, *, tenant_id: UUID) -> TenantConfigRecord | None:
        try:
            return await self._service.get(tenant_id=tenant_id, actor_id=None)
        except TenantConfigNotConfiguredError:
            return None

    async def upsert(
        self, *, tenant_id: UUID, patch: TenantConfigPatch, actor_id: str
    ) -> TenantConfigRecord:
        return await self._service.upsert(tenant_id=tenant_id, patch=patch, actor_id=actor_id)

    # ── 以下三个方法 recall 路径用不到,但基类是 ABC,必须逐个委托底层
    # store,不留 NotImplementedError。────────────────────────────────

    async def create(
        self,
        *,
        tenant_id: UUID,
        display_name: str,
        plan: TenantPlan | None = None,
        actor_id: str,
    ) -> TenantConfigRecord:
        # 新租户首行:service 缓存里不可能有该 tenant 的条目(get miss
        # 不缓存、已有行时 create 直接抛),无需失效。
        return await self._service._store.create(
            tenant_id=tenant_id, display_name=display_name, plan=plan, actor_id=actor_id
        )

    async def set_status(
        self, *, tenant_id: UUID, status: str, actor_id: str
    ) -> TenantConfigRecord:
        record = await self._service._store.set_status(
            tenant_id=tenant_id, status=status, actor_id=actor_id
        )
        # 直写底层 store 绕过了 service 缓存 —— 立刻失效,避免 TTL 窗口内
        # 经 service 读到旧 status。
        self._service.invalidate(tenant_id)
        return record

    async def list_all(self, *, limit: int = 50, offset: int = 0) -> list[TenantConfigRecord]:
        return await self._service._store.list_all(limit=limit, offset=offset)
