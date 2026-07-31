"""Tenant usage / cost API — Stream Z (Mini-ADR Z-1).

Two tenant-facing, RLS-self-isolated read endpoints (``billing:read``):

* ``GET /v1/usage/cost``   — billed cost + token sums from the Y4
  ``tenant_billing_ledger`` (rollup-derived; lags the hourly rollup).
* ``GET /v1/usage/tokens`` — current-month realtime token sums straight from
  the ``token_usage`` meter (no rollup lag, no cost).

**Hard constraint (Stream Y/Z locked decision):** the tenant surface exposes
ONLY ``billed_cost_micros``. ``base_cost``/``markup`` live on the ledger row but
are NEVER projected here — they are physically absent from these response
shapes, visible only via the system_admin chargeback API (Z-2). Tenant scoping
rides on the RLS ContextVar (``RLSContextMiddleware`` projects
``principal.tenant_id``), so a plain ``list_for_tenant`` is self-isolating.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from control_plane.api._authz import require
from control_plane.tenant_scope import (
    CrossTenant,
    SingleTenant,
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


async def _resolve_usage_scope(
    request: Request,
    principal: Principal,
    tenant_id: UUID | Literal["*"] | None,
    audit: AuditLogger,
    *,
    endpoint: str,
) -> SingleTenant:
    """W3 read scope for the two usage reads.

    Neither store has a cross-tenant aggregate reader and the "*" aggregate is
    a spec non-goal here — a resolved :class:`CrossTenant` falls back to the
    caller's home tenant (the pre-scope-threading behavior, mirroring the
    front-end ``concreteTenantScope`` collapse).
    """
    scope = await ensure_tenant_scope(
        principal,
        tenant_id,
        audit,
        trace_id=current_trace_id_hex(),
        endpoint=endpoint,
        cross_tenant_enabled=cross_tenant_query_enabled(request),
    )
    if isinstance(scope, CrossTenant):
        return SingleTenant(tenant_id=principal.tenant_id)
    return scope


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
    router = APIRouter(prefix="/v1/usage", tags=["usage"])

    @router.get("/cost")
    async def usage_cost(
        request: Request,
        principal: Annotated[Principal, Depends(require("billing", "read"))],
        store: Annotated[TenantBillingLedgerStore, Depends(_get_ledger_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        month: Annotated[str | None, Query()] = None,
        group_by: Annotated[str, Query()] = "agent",
        # W3 read scope — a concrete id lets a system_admin read a foreign
        # tenant's usage from the tenant switcher.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> dict[str, object]:
        if group_by not in _GroupBy:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_GROUP_BY", "message": f"group_by ∈ {_GroupBy}"},
            )
        scope = await _resolve_usage_scope(
            request, principal, tenant_id, audit, endpoint="GET /v1/usage/cost"
        )
        target = _parse_month(month)
        async with applied_scope(scope):
            rows = await store.list_for_tenant(tenant_id=scope.tenant_id, month=target)

        # Aggregate into groups. NEVER project base/markup — billed only.
        agg: dict[str, _CostGroup] = {}
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
            bucket = agg.get(key)
            if bucket is None:
                bucket = _CostGroup(key=key)
                if group_by == "none":
                    bucket.provider = r.provider
                    bucket.model = r.model
                    bucket.agent_name = r.agent_name
                agg[key] = bucket
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
        # W3 read scope — same treatment as ``/cost``.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> dict[str, object]:
        scope = await _resolve_usage_scope(
            request, principal, tenant_id, audit, endpoint="GET /v1/usage/tokens"
        )
        target = _parse_month(month)
        start, end = _month_window(target)
        # Realtime — straight from the meter, no rollup lag.
        async with applied_scope(scope):
            rows = await store.list_for_tenant_window(
                tenant_id=scope.tenant_id, start=start, end=end, user_id=user_id
            )
        if kind is not None:
            rows = [r for r in rows if r.usage_kind == kind]

        total = _token_zero()
        by_agent: dict[str, dict[str, int]] = defaultdict(_token_zero)
        by_model: dict[str, dict[str, int]] = defaultdict(_token_zero)
        by_kind: dict[str, dict[str, int]] = defaultdict(_token_zero)
        for r in rows:
            _token_add(total, r)
            _token_add(by_agent[r.agent_name], r)
            _token_add(by_model[r.model], r)
            _token_add(by_kind[r.usage_kind], r)

        return {
            "success": True,
            "data": {
                "month": target.strftime("%Y-%m"),
                "as_of": datetime.now(tz=UTC).isoformat(),
                "realtime": True,
                "total": total,
                "by_agent": [{"key": k, **v} for k, v in by_agent.items()],
                "by_model": [{"key": k, **v} for k, v in by_model.items()],
                # SE-16 (SE-A43) — evolution spend separable from conversation.
                "by_kind": [{"key": k, **v} for k, v in by_kind.items()],
            },
            "error": None,
        }

    return router
