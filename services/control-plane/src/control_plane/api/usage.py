"""Tenant usage / cost API — Stream Z (Mini-ADR Z-1).

Two tenant-facing read endpoints (``billing:read``):

* ``GET /v1/usage/cost``   — billed cost + token sums from the Y4
  ``tenant_billing_ledger`` (rollup-derived; lags the hourly rollup).
* ``GET /v1/usage/tokens`` — current-month realtime token sums straight from
  the ``token_usage`` meter (no rollup lag, no cost).

**Hard constraint (Stream Y/Z locked decision):** the tenant surface exposes
ONLY ``billed_cost_micros``. ``base_cost``/``markup`` live on the ledger row but
are NEVER projected here — they are physically absent from these response
shapes, visible only via the system_admin chargeback API (Z-2). Reads are
tenant-scoped by default; a system_admin may target another tenant via
``?tenant_id=`` (W3 read scope, see ``control_plane.tenant_scope``), applied
to both the RLS ContextVar and the store's explicit ``tenant_id`` filter, or
aggregate every tenant via ``?tenant_id=*`` (W4 — real cross-tenant readers;
grouping keys carry the tenant dimension so same-named agents/models in two
tenants never fold into one bucket, and every grouped row exposes its
``tenant_id``).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from control_plane.api._authz import console_only, require
from control_plane.tenant_scope import (
    CrossTenant,
    applied_scope,
    cross_tenant_query_enabled,
    ensure_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence import TenantBillingLedgerStore
from expert_work.persistence.token_usage_store import TokenUsageRecord, TokenUsageStore
from expert_work.protocol import Principal
from expert_work.runtime.audit.logger import AuditLogger

_GroupBy = ("agent", "model", "none")


@dataclass
class _CostGroup:
    """One aggregated cost group — BILLED ONLY (no base/markup, by design)."""

    key: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    billed_cost_micros: int = 0
    unpriced: bool = False
    # Populated only for ``group_by=none`` so the raw bucket identity is visible.
    provider: str | None = None
    model: str | None = None
    agent_name: str | None = None
    # W4 — owning tenant. Always populated: the row's tenant in the
    # ``tenant_id=*`` aggregate, the (single) scoped tenant otherwise.
    tenant_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        d = asdict(self)
        # Drop the three identity fields when unused (group_by=agent/model).
        if self.provider is None:
            del d["provider"], d["model"], d["agent_name"]
        return d


def _get_ledger_store(request: Request) -> TenantBillingLedgerStore:
    return request.app.state.tenant_billing_ledger_store  # type: ignore[no-any-return]


def _get_token_usage_store(request: Request) -> TokenUsageStore:
    return request.app.state.token_usage_store  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _parse_month(month: str | None) -> date:
    """Parse ``YYYY-MM`` into the first-of-month date; default = current month."""
    if month is None:
        today = datetime.now(tz=UTC).date()
        return today.replace(day=1)
    try:
        parsed = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_MONTH", "message": "month must be 'YYYY-MM'"},
        ) from exc
    return parsed.date().replace(day=1)


def _month_window(month: date) -> tuple[datetime, datetime]:
    """Half-open ``[month_start, next_month_start)`` as tz-aware datetimes."""
    start = datetime(month.year, month.month, 1, tzinfo=UTC)
    # Half-open end: the first instant of the next month.
    end = datetime(month.year + (month.month // 12), (month.month % 12) + 1, 1, tzinfo=UTC)
    return start, end


def _token_zero() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
    }


def _token_add(slot: dict[str, int], r: TokenUsageRecord) -> None:
    slot["input_tokens"] += r.input_tokens
    slot["output_tokens"] += r.output_tokens
    slot["cache_creation_tokens"] += r.cache_creation_tokens
    slot["cache_read_tokens"] += r.cache_read_tokens


def build_usage_router() -> APIRouter:
    router = APIRouter(prefix="/v1/usage", tags=["usage"], dependencies=[Depends(console_only())])

    @router.get("/cost")
    async def usage_cost(
        request: Request,
        principal: Annotated[Principal, Depends(require("billing", "read"))],
        store: Annotated[TenantBillingLedgerStore, Depends(_get_ledger_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        month: Annotated[str | None, Query()] = None,
        group_by: Annotated[str, Query()] = "agent",
        # W3/W4 read scope — a concrete id lets a system_admin read a foreign
        # tenant's usage from the tenant switcher; "*" aggregates every tenant.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> dict[str, object]:
        if group_by not in _GroupBy:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_GROUP_BY", "message": f"group_by ∈ {_GroupBy}"},
            )
        # W4 — real cross-tenant aggregate: "*" reads every tenant's ledger
        # buckets (Z-2 chargeback reader); a concrete UUID keeps W3 behavior.
        scope = await ensure_tenant_scope(
            principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/usage/cost",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        target = _parse_month(month)
        async with applied_scope(scope):
            if isinstance(scope, CrossTenant):
                rows = await store.list_for_month_all_tenants(month=target)
            else:
                rows = await store.list_for_tenant(tenant_id=scope.tenant_id, month=target)

        # Aggregate into groups. NEVER project base/markup — billed only.
        # The bucket key carries the tenant so the "*" aggregate can't fold
        # two tenants' same-named agents/models into one row (W4); inside a
        # single tenant every row shares one tenant_id, so behavior is
        # unchanged (plus the additive ``tenant_id`` response field).
        agg: dict[tuple[str, str], _CostGroup] = {}
        total_billed = 0
        as_of: datetime | None = None
        for r in rows:
            total_billed += r.billed_cost_micros
            if as_of is None or r.rate_card_priced_at > as_of:
                as_of = r.rate_card_priced_at
            if group_by == "agent":
                key = r.agent_name
            elif group_by == "model":
                key = r.model
            else:  # none — keep the full bucket identity
                key = f"{r.provider}:{r.model}:{r.agent_name}"
            bucket_key = (str(r.tenant_id), key)
            bucket = agg.get(bucket_key)
            if bucket is None:
                bucket = _CostGroup(key=key, tenant_id=str(r.tenant_id))
                if group_by == "none":
                    bucket.provider = r.provider
                    bucket.model = r.model
                    bucket.agent_name = r.agent_name
                agg[bucket_key] = bucket
            bucket.input_tokens += r.input_tokens
            bucket.output_tokens += r.output_tokens
            bucket.cache_creation_tokens += r.cache_creation_tokens
            bucket.cache_read_tokens += r.cache_read_tokens
            bucket.billed_cost_micros += r.billed_cost_micros
            if not r.priced:
                bucket.unpriced = True

        return {
            "success": True,
            "data": {
                "month": target.strftime("%Y-%m"),
                "group_by": group_by,
                "as_of": as_of.isoformat() if as_of is not None else None,
                "total_billed_cost_micros": total_billed,
                "groups": [g.as_dict() for g in agg.values()],
            },
            "error": None,
        }

    @router.get("/tokens")
    async def usage_tokens(
        request: Request,
        principal: Annotated[Principal, Depends(require("billing", "read"))],
        store: Annotated[TokenUsageStore, Depends(_get_token_usage_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        month: Annotated[str | None, Query()] = None,
        # Conversation-centric IA M2 — narrow to one end-user (the
        # user-detail usage tab). Endpoint already requires billing:read
        # (an admin capability), so no extra per-user gate is needed.
        user_id: Annotated[UUID | None, Query()] = None,
        # SE-16 (SE-A43) — narrow to one usage kind ('conversation' |
        # 'skill_evolution'); None keeps the full month.
        kind: Annotated[str | None, Query()] = None,
        # W3/W4 read scope — same treatment as ``/cost``.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> dict[str, object]:
        # W4 — same real "*" aggregate as ``/cost`` (all-tenants meter read).
        scope = await ensure_tenant_scope(
            principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/usage/tokens",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        target = _parse_month(month)
        start, end = _month_window(target)
        # Realtime — straight from the meter, no rollup lag.
        async with applied_scope(scope):
            if isinstance(scope, CrossTenant):
                rows = await store.list_window_all_tenants(start=start, end=end, user_id=user_id)
            else:
                rows = await store.list_for_tenant_window(
                    tenant_id=scope.tenant_id, start=start, end=end, user_id=user_id
                )
        if kind is not None:
            rows = [r for r in rows if r.usage_kind == kind]

        # Bucket keys carry the tenant (W4) so the "*" aggregate can't fold
        # two tenants' same-named agents/models/kinds into one row; inside a
        # single tenant every row shares one tenant_id, so behavior is
        # unchanged (plus the additive ``tenant_id`` response field).
        total = _token_zero()
        by_agent: dict[tuple[str, str], dict[str, int]] = defaultdict(_token_zero)
        by_model: dict[tuple[str, str], dict[str, int]] = defaultdict(_token_zero)
        by_kind: dict[tuple[str, str], dict[str, int]] = defaultdict(_token_zero)
        for r in rows:
            tid = str(r.tenant_id)
            _token_add(total, r)
            _token_add(by_agent[(tid, r.agent_name)], r)
            _token_add(by_model[(tid, r.model)], r)
            _token_add(by_kind[(tid, r.usage_kind)], r)

        return {
            "success": True,
            "data": {
                "month": target.strftime("%Y-%m"),
                "as_of": datetime.now(tz=UTC).isoformat(),
                "realtime": True,
                "total": total,
                "by_agent": [{"key": k, "tenant_id": tid, **v} for (tid, k), v in by_agent.items()],
                "by_model": [{"key": k, "tenant_id": tid, **v} for (tid, k), v in by_model.items()],
                # SE-16 (SE-A43) — evolution spend separable from conversation.
                "by_kind": [{"key": k, "tenant_id": tid, **v} for (tid, k), v in by_kind.items()],
            },
            "error": None,
        }

    return router
