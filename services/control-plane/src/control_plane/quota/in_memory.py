"""In-memory :class:`QuotaService` implementation — Stream C.5.

Used by unit tests, by the dev default when ``quota_redis_url`` is
unset, and by the C.5 integration tests for assertions that don't
care which engine sits underneath. The bucket math mirrors
subsystems/16 § 5.1 — a Python translation of the Redis Lua atomic
script — so behavioural assertions stay portable between the two
implementations.

Limitations vs. the Redis impl:

* Bucket state lives in a process-local dict; horizontal replicas
  diverge. Production wires :class:`RedisQuotaService` instead.
* No cross-process atomicity. Tests that exercise concurrency
  semantics use ``asyncio.gather`` against a single instance.
* The monthly budget guard reads from the
  :class:`TokenReservationStore.get_budget` ledger, so reservation
  semantics are identical to the Redis impl.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from control_plane.quota.base import QuotaService
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
)

# Cache resolved quota rows so repeated checks don't hit the store on
# every request. Subsystems/16 § 5.3 specifies 60s.
_QUOTA_CACHE_TTL_S: Final[float] = 60.0


@dataclass
class _Bucket:
    """Token bucket state — one per (tenant, dimension, scope) tuple."""

    capacity: int
    refill_rate_per_s: float
    tokens: float
    last_refill_monotonic: float


@dataclass(frozen=True)
class _ResolvedDimension:
    """Per-request bucket selector: key + capacity + refill."""

    name: QuotaDimension
    key: str
    capacity: int
    refill_rate_per_s: float
    cost: int


class InMemoryQuotaService(QuotaService):
    """Single-process token bucket + ledger-driven reservation manager."""

    def __init__(
        self,
        *,
        quota_store: TenantQuotaStore,
        reservation_store: TokenReservationStore,
        default_qps_limit: int | None = None,
        default_qps_burst: int = 120,
        clock: object | None = None,
    ) -> None:
        self._quota_store = quota_store
        self._reservation_store = reservation_store
        self._default_qps_limit = default_qps_limit
        self._default_qps_burst = default_qps_burst
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()
        # Quota config cache: (tenant_id) → (expires_at_monotonic, rows)
        self._quota_cache: dict[UUID, tuple[float, list[TenantQuotaRecord]]] = {}
        self._monotonic = (
            clock.monotonic  # type: ignore[attr-defined]
            if clock is not None
            else time.monotonic
        )

    # ------------------------------------------------------------------ check

    async def check(self, req: CheckRequest) -> CheckResult:
        dims = await self._resolve_dimensions(req)
        if not dims:
            # No applicable quota → unlimited (M0 dev default).
            return CheckResult(allowed=True, remaining={})

        # Sort by capacity ascending so we reject early on the tightest
        # bucket — saves work and matches subsystems/16 § 5.2.
        dims = sorted(dims, key=lambda d: d.capacity)

        remaining: dict[str, int] = {}
        now = self._monotonic()
        async with self._lock:
            for d in dims:
                bucket = self._buckets.get(d.key)
                if bucket is None:
                    bucket = _Bucket(
                        capacity=d.capacity,
                        refill_rate_per_s=d.refill_rate_per_s,
                        tokens=float(d.capacity),
                        last_refill_monotonic=now,
                    )
                    self._buckets[d.key] = bucket
                _refill(bucket, now)
                if bucket.tokens < d.cost:
                    retry_after_s = math.ceil(
                        (d.cost - bucket.tokens) / max(bucket.refill_rate_per_s, 1e-9)
                    )
                    return CheckResult(
                        allowed=False,
                        blocked_dimension=d.name,
                        retry_after_s=retry_after_s,
                        remaining=remaining | {d.name.value: int(bucket.tokens)},
                    )
                bucket.tokens -= d.cost
                remaining[d.name.value] = int(bucket.tokens)
        return CheckResult(allowed=True, remaining=remaining)

    # ------------------------------------------------------------------ reserve / commit

    async def reserve_tokens(self, req: ReserveRequest) -> ReserveResult:
        # ``budget_total == 0`` means "no monthly budget configured" which
        # we treat as "unlimited" — same M0 dev default as QPS. The check
        # + reserved_total bump happen atomically inside ``reserve()``
        # (same ``self._lock`` critical section) — no separate
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

    # ------------------------------------------------------------------ helpers

    async def _resolve_dimensions(self, req: CheckRequest) -> list[_ResolvedDimension]:
        rows = await self._cached_quota_rows(req.tenant_id)
        out: list[_ResolvedDimension] = []
        for row in rows:
            if not _scope_matches(row.scope, agent=req.agent, user=req.user):
                continue
            if row.dimension is QuotaDimension.QPS:
                capacity = row.burst or row.limit_value
                refill = float(row.limit_value)
                key = _bucket_key("qps", req.tenant_id, row.scope)
                out.append(
                    _ResolvedDimension(
                        name=QuotaDimension.QPS,
                        key=key,
                        capacity=capacity,
                        refill_rate_per_s=refill,
                        cost=req.cost_overrides.get(QuotaDimension.QPS, req.cost),
                    )
                )
            elif row.dimension is QuotaDimension.IMAGE_UPLOAD_COUNT_30D:
                # Mini-ADR J-30 — rolling 30-day count via slow-drip bucket.
                # capacity = limit (burst-aware); refill_rate = limit /
                # (30 * 86400) so the bucket recovers one upload's worth
                # of headroom every ``30d / limit`` seconds.
                capacity = row.burst or row.limit_value
                refill = float(row.limit_value) / float(30 * 86_400)
                key = _bucket_key("img_count_30d", req.tenant_id, row.scope)
                out.append(
                    _ResolvedDimension(
                        name=QuotaDimension.IMAGE_UPLOAD_COUNT_30D,
                        key=key,
                        capacity=capacity,
                        refill_rate_per_s=refill,
                        cost=req.cost_overrides.get(
                            QuotaDimension.IMAGE_UPLOAD_COUNT_30D, req.cost
                        ),
                    )
                )
            elif row.dimension is QuotaDimension.IMAGE_STORAGE_BYTES:
                # Mini-ADR J-30 — sticky bytes ceiling (no refill in M0;
                # J.6.补强-3 / Mini-ADR J-32 lifecycle-delete will refund
                # bytes once that lands). ``cost`` defaults to ``req.cost``
                # but the upload path passes ``file_size`` via
                # ``cost_overrides``.
                capacity = row.limit_value
                key = _bucket_key("img_bytes", req.tenant_id, row.scope)
                out.append(
                    _ResolvedDimension(
                        name=QuotaDimension.IMAGE_STORAGE_BYTES,
                        key=key,
                        capacity=capacity,
                        refill_rate_per_s=0.0,
                        cost=req.cost_overrides.get(QuotaDimension.IMAGE_STORAGE_BYTES, req.cost),
                    )
                )
            elif row.dimension is QuotaDimension.ARTIFACT_DOWNLOAD_COUNT_30D:
                # Mini-ADR J-25 (J.9-step2) — rolling 30-day artifact
                # download count, same slow-drip shape as
                # ``IMAGE_UPLOAD_COUNT_30D``.
                capacity = row.burst or row.limit_value
                refill = float(row.limit_value) / float(30 * 86_400)
                key = _bucket_key("art_dl_count_30d", req.tenant_id, row.scope)
                out.append(
                    _ResolvedDimension(
                        name=QuotaDimension.ARTIFACT_DOWNLOAD_COUNT_30D,
                        key=key,
                        capacity=capacity,
                        refill_rate_per_s=refill,
                        cost=req.cost_overrides.get(
                            QuotaDimension.ARTIFACT_DOWNLOAD_COUNT_30D, req.cost
                        ),
                    )
                )
            elif row.dimension is QuotaDimension.ARTIFACT_STORAGE_BYTES:
                # Mini-ADR J-25 (J.9-step2) — sticky artifact bytes ceiling
                # (no refill in M0; per-name lifecycle hard-delete refund is
                # a future step that mirrors Mini-ADR J-32). Wired into the
                # bucket runtime so a future ``save_artifact`` quota path
                # has the dimension ready; download endpoints don't deduct
                # storage_bytes.
                capacity = row.limit_value
                key = _bucket_key("art_bytes", req.tenant_id, row.scope)
                out.append(
                    _ResolvedDimension(
                        name=QuotaDimension.ARTIFACT_STORAGE_BYTES,
                        key=key,
                        capacity=capacity,
                        refill_rate_per_s=0.0,
                        cost=req.cost_overrides.get(
                            QuotaDimension.ARTIFACT_STORAGE_BYTES, req.cost
                        ),
                    )
                )

        # Default per-tenant QPS bucket when nothing was configured.
        if not any(d.name is QuotaDimension.QPS for d in out) and self._default_qps_limit:
            out.append(
                _ResolvedDimension(
                    name=QuotaDimension.QPS,
                    key=_bucket_key("qps_default", req.tenant_id, {}),
                    capacity=self._default_qps_burst,
                    refill_rate_per_s=float(self._default_qps_limit),
                    cost=req.cost_overrides.get(QuotaDimension.QPS, req.cost),
                )
            )
        return out

    async def _cached_quota_rows(self, tenant_id: UUID) -> list[TenantQuotaRecord]:
        now = self._monotonic()
        cached = self._quota_cache.get(tenant_id)
        if cached is not None and cached[0] > now:
            return cached[1]
        rows = await self._quota_store.list_by_tenant(tenant_id=tenant_id)
        self._quota_cache[tenant_id] = (now + _QUOTA_CACHE_TTL_S, rows)
        return rows


# ---------------------------------------------------------------------------
# helpers (module-level so the Redis impl can reuse)
# ---------------------------------------------------------------------------


def _refill(bucket: _Bucket, now_monotonic: float) -> None:
    elapsed = max(0.0, now_monotonic - bucket.last_refill_monotonic)
    bucket.tokens = min(
        float(bucket.capacity),
        bucket.tokens + elapsed * bucket.refill_rate_per_s,
    )
    bucket.last_refill_monotonic = now_monotonic


def _scope_matches(
    scope: dict[str, str],
    *,
    agent: str | None,
    user: str | None,
) -> bool:
    """``scope`` row matches the request when each key is ``*`` or equal."""
    for key, expected in scope.items():
        if expected == "*":
            continue
        if key == "agent" and expected != agent:
            return False
        if key == "user" and expected != user:
            return False
    return True


def _bucket_key(prefix: str, tenant_id: UUID, scope: dict[str, str]) -> str:
    """Deterministic bucket id for a (tenant, scope) tuple."""
    parts = [prefix, str(tenant_id)]
    for k in sorted(scope.keys()):
        parts.append(f"{k}={scope[k]}")
    return ":".join(parts)
