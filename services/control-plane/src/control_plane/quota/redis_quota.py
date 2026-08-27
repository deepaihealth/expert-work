"""Redis-backed :class:`QuotaService` — Stream C.5.

Hot-path token-bucket counters live in Redis (one Lua eval per
dimension, atomic refill + spend); reservation rows + monthly ledger
live in Postgres through the :class:`TokenReservationStore`. The Lua
script is the verbatim translation of subsystems/16 § 5.1.

The Lua eval returns ``{allowed, retry_after_ms, remaining}``; the
service maps it to :class:`CheckResult`. Bucket TTL is 30 days so an
idle tenant's keys drop out of memory; busy tenants refresh their
last_ms every check.

Redis failure modes (subsystems/16 § 6):

* **Connection refused / timeout** → ``RedisError`` propagates. The
  HTTP layer maps to 503 ``quota_engine_unavailable``; the in-process
  admission path falls back to ``fail-closed`` (return CheckResult
  denied). This is opt-in via :meth:`check`'s caller — we don't
  swallow the exception here.
* **NOSCRIPT** — Lua script evicted from Redis cache. ``redis-py``
  surfaces ``NoScriptError``; the service auto-re-EVALs and retries
  once (handled below).
"""

from __future__ import annotations

import math
import time
from typing import Any, Final
from uuid import UUID

import redis.asyncio as redis_async
from redis.exceptions import NoScriptError

from control_plane.quota.base import QuotaService
from control_plane.quota.in_memory import _bucket_key, _scope_matches
from expert_work.persistence.quota import (
    BudgetExceededError,
    TenantQuotaStore,
    TokenReservationStore,
)
from expert_work.protocol import (
    CheckRequest,
    CheckResult,
    CommitRequest,
    QuotaDimension,
    ReservationState,
    ReserveRequest,
    ReserveResult,
    TenantQuotaRecord,
    dimension_applies,
)

_QUOTA_CACHE_TTL_S: Final[float] = 60.0
_BUCKET_TTL_MS: Final[int] = 30 * 86_400 * 1_000


# Subsystems/16 § 5.1 — atomic token bucket.
# KEYS[1] = bucket key
# ARGV: 1=capacity 2=refill_per_s*1000 3=now_ms 4=cost 5=ttl_ms
_LUA_BUCKET_SOURCE = """\
-- expert-work quota bucket (subsystems/16 § 5.1)
local b = redis.call('HMGET', KEYS[1], 'tokens', 'last_ms')
local cap = tonumber(ARGV[1])
local rate_milli = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local ttl_ms = tonumber(ARGV[5])
local tokens = tonumber(b[1]) or cap
local last_ms = tonumber(b[2]) or now_ms
local elapsed = math.max(0, now_ms - last_ms)
tokens = math.min(cap, tokens + elapsed * rate_milli / 1000)
if tokens < cost then
  -- B-19: rate_milli == 0 marks a sticky ceiling (refill=0) -- no time at
  -- which a retry would succeed. Return the -1 sentinel instead of
  -- dividing by zero; the Python side maps it to retry_after_s=None.
  local retry_ms = -1
  if rate_milli > 0 then
    retry_ms = math.ceil((cost - tokens) * 1000 / rate_milli)
  end
  redis.call('HMSET', KEYS[1], 'tokens', tokens, 'last_ms', now_ms)
  redis.call('PEXPIRE', KEYS[1], ttl_ms)
  return {0, retry_ms, math.floor(tokens)}
end
tokens = tokens - cost
redis.call('HMSET', KEYS[1], 'tokens', tokens, 'last_ms', now_ms)
redis.call('PEXPIRE', KEYS[1], ttl_ms)
return {1, 0, math.floor(tokens)}
"""


class RedisQuotaService(QuotaService):
    """Production engine — Redis buckets + Postgres reservation ledger."""

    def __init__(
        self,
        *,
        redis_client: redis_async.Redis,
        quota_store: TenantQuotaStore,
        reservation_store: TokenReservationStore,
        default_qps_limit: int | None = None,
        default_qps_burst: int = 120,
    ) -> None:
        self._redis = redis_client
        self._quota_store = quota_store
        self._reservation_store = reservation_store
        self._default_qps_limit = default_qps_limit
        self._default_qps_burst = default_qps_burst
        self._quota_cache: dict[UUID, tuple[float, list[TenantQuotaRecord]]] = {}
        # Loaded lazily — the first eval populates the SHA so the rest
        # of the process can use EVALSHA for the cheaper round-trip.
        self._lua_sha: str | None = None

    # ------------------------------------------------------------------ check

    async def check(self, req: CheckRequest) -> CheckResult:
        dims = await self._resolve_dimensions(req)
        if not dims:
            return CheckResult(allowed=True, remaining={})

        dims.sort(key=lambda d: d[2])  # capacity asc
        remaining: dict[str, int] = {}
        now_ms = int(time.time() * 1000)
        for dim_name, key, capacity, refill_per_s, cost in dims:
            allowed, retry_ms, tokens_left = await self._eval_bucket(
                key=key,
                capacity=capacity,
                refill_per_s=refill_per_s,
                now_ms=now_ms,
                cost=cost,
            )
            if not allowed:
                return CheckResult(
                    allowed=False,
                    blocked_dimension=dim_name,
                    # B-19 ② — Lua returns retry_ms = -1 for sticky
                    # (refill=0) dimensions: no meaningful retry → None.
                    retry_after_s=None if retry_ms < 0 else math.ceil(retry_ms / 1000),
                    remaining=remaining | {dim_name.value: tokens_left},
                )
            remaining[dim_name.value] = tokens_left
        return CheckResult(allowed=True, remaining=remaining)

    # ------------------------------------------------------------------ reserve / commit / release

    async def reserve_tokens(self, req: ReserveRequest) -> ReserveResult:
        # The budget check + reserved_total bump happen atomically inside
        # ``reserve()`` (row-locked ledger UPDATE) — no separate
        # get_budget() pre-check here, which would race a concurrent
        # reserve() between the check and the bump (TOCTOU).
        try:
            row = await self._reservation_store.reserve(
                tenant_id=req.tenant_id,
                agent_name=req.agent,
                thread_id=req.thread_id,
                estimated=req.estimated_tokens,
                parent_thread_id=req.parent_thread_id,
                model=req.model,
            )
        except BudgetExceededError:
            return ReserveResult(granted=False, reason="over_budget")
        return ReserveResult(granted=True, reservation_id=row.id, reason="ok")

    async def commit_tokens(self, req: CommitRequest) -> None:
        await self._reservation_store.commit(
            reservation_id=req.reservation_id,
            tenant_id=req.tenant_id,
            actual_tokens=req.actual_tokens,
        )

    async def release_tokens(self, reservation_id: UUID, *, tenant_id: UUID) -> None:
        await self._reservation_store.release(
            reservation_id=reservation_id,
            tenant_id=tenant_id,
            new_state=ReservationState.RELEASED,
        )

    def invalidate_tenant(self, tenant_id: UUID) -> None:
        """Drop the tenant's cached quota rows so the next check reloads
        from the store (quota-admin writes / invalidation-bus events)."""
        self._quota_cache.pop(tenant_id, None)

    # ------------------------------------------------------------------ Redis helpers

    async def _eval_bucket(
        self,
        *,
        key: str,
        capacity: int,
        refill_per_s: float,
        now_ms: int,
        cost: int,
    ) -> tuple[bool, int, int]:
        # ``refill_per_s * 1000`` keeps the Lua script working in
        # integer arithmetic for the common case while preserving
        # sub-token-per-second rates.
        argv = [
            str(capacity),
            str(int(refill_per_s * 1000)),
            str(now_ms),
            str(cost),
            str(_BUCKET_TTL_MS),
        ]
        result: Any
        try:
            if self._lua_sha is None:
                # ``redis-py`` types script_load as Awaitable[str] | str
                # (it returns a coroutine in async mode, a string in
                # sync/pipeline mode). We always run async — cast.
                loaded = await self._redis.script_load(_LUA_BUCKET_SOURCE)  # type: ignore[misc]
                self._lua_sha = str(loaded)
            result = await self._redis.evalsha(self._lua_sha, 1, key, *argv)
        except NoScriptError:
            # Cache eviction: reload and EVAL once.
            loaded = await self._redis.script_load(_LUA_BUCKET_SOURCE)  # type: ignore[misc]
            self._lua_sha = str(loaded)
            result = await self._redis.evalsha(self._lua_sha, 1, key, *argv)

        allowed_raw, retry_ms_raw, tokens_left_raw = result
        return bool(int(allowed_raw)), int(retry_ms_raw), int(tokens_left_raw)

    async def _resolve_dimensions(
        self, req: CheckRequest
    ) -> list[tuple[QuotaDimension, str, int, float, int]]:
        rows = await self._cached_quota_rows(req.tenant_id)
        out: list[tuple[QuotaDimension, str, int, float, int]] = []
        for row in rows:
            if not _scope_matches(row.scope, agent=req.agent, user=req.user):
                continue
            if not dimension_applies(row.dimension, req.resource_kind):
                # B-19 ① — the dimension belongs to a different admission
                # surface; this check must not spend from its bucket.
                continue
            if row.dimension is QuotaDimension.QPS:
                capacity = row.burst or row.limit_value
                refill = float(row.limit_value)
                key = "qb:" + _bucket_key("qps", req.tenant_id, row.scope)
                cost = req.cost_overrides.get(QuotaDimension.QPS, req.cost)
                out.append((QuotaDimension.QPS, key, capacity, refill, cost))
            elif row.dimension is QuotaDimension.IMAGE_UPLOAD_COUNT_30D:
                # Mini-ADR J-30 — see InMemoryQuotaService for the
                # bucket-math rationale.
                capacity = row.burst or row.limit_value
                refill = float(row.limit_value) / float(30 * 86_400)
                key = "qb:" + _bucket_key("img_count_30d", req.tenant_id, row.scope)
                cost = req.cost_overrides.get(QuotaDimension.IMAGE_UPLOAD_COUNT_30D, req.cost)
                out.append((QuotaDimension.IMAGE_UPLOAD_COUNT_30D, key, capacity, refill, cost))
            elif row.dimension is QuotaDimension.IMAGE_STORAGE_BYTES:
                # Mini-ADR J-30 — sticky byte ceiling (refill=0). The
                # upload path passes ``file_size`` via ``cost_overrides``.
                capacity = row.limit_value
                key = "qb:" + _bucket_key("img_bytes", req.tenant_id, row.scope)
                cost = req.cost_overrides.get(QuotaDimension.IMAGE_STORAGE_BYTES, req.cost)
                out.append((QuotaDimension.IMAGE_STORAGE_BYTES, key, capacity, 0.0, cost))
            elif row.dimension is QuotaDimension.ARTIFACT_DOWNLOAD_COUNT_30D:
                # Mini-ADR J-25 (J.9-step2) — rolling 30-day artifact
                # download count, same slow-drip shape as the image
                # equivalent above.
                capacity = row.burst or row.limit_value
                refill = float(row.limit_value) / float(30 * 86_400)
                key = "qb:" + _bucket_key("art_dl_count_30d", req.tenant_id, row.scope)
                cost = req.cost_overrides.get(QuotaDimension.ARTIFACT_DOWNLOAD_COUNT_30D, req.cost)
                out.append(
                    (QuotaDimension.ARTIFACT_DOWNLOAD_COUNT_30D, key, capacity, refill, cost)
                )
            elif row.dimension is QuotaDimension.ARTIFACT_STORAGE_BYTES:
                # Mini-ADR J-25 (J.9-step2) — sticky artifact bytes
                # ceiling. Wired here so a future ``save_artifact`` quota
                # path has the dimension ready.
                capacity = row.limit_value
                key = "qb:" + _bucket_key("art_bytes", req.tenant_id, row.scope)
                cost = req.cost_overrides.get(QuotaDimension.ARTIFACT_STORAGE_BYTES, req.cost)
                out.append((QuotaDimension.ARTIFACT_STORAGE_BYTES, key, capacity, 0.0, cost))

        if not any(d[0] is QuotaDimension.QPS for d in out) and self._default_qps_limit:
            qps_cost = req.cost_overrides.get(QuotaDimension.QPS, req.cost)
            out.append(
                (
                    QuotaDimension.QPS,
                    "qb:" + _bucket_key("qps_default", req.tenant_id, {}),
                    self._default_qps_burst,
                    float(self._default_qps_limit),
                    qps_cost,
                )
            )
        return out

    async def _cached_quota_rows(self, tenant_id: UUID) -> list[TenantQuotaRecord]:
        now = time.monotonic()
        cached = self._quota_cache.get(tenant_id)
        if cached is not None and cached[0] > now:
            return cached[1]
        rows = await self._quota_store.list_by_tenant(tenant_id=tenant_id)
        self._quota_cache[tenant_id] = (now + _QUOTA_CACHE_TTL_S, rows)
        return rows
