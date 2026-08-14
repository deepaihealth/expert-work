"""Platform billing chargeback API — Stream Z (Mini-ADR Z-2).

system_admin-only, cross-tenant view of the Y4 ``tenant_billing_ledger``. Unlike
the tenant-facing ``/v1/usage`` surface (billed only), this admin surface shows
the FULL split — ``base_cost``/``markup``/``billed`` + ``margin`` — per tenant,
so the platform can see its cost vs gross margin. Every handler:

* gates on ``require("billing", "read")`` then re-checks ``is_system_admin``
  inline (defense in depth — platform surface, same precedent as rate_card.py);
* reads the ledger inside ``bypass_rls_session()`` (the ledger is tenant-scoped
  RLS, so a normally-scoped session would see only one tenant).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from control_plane.api._authz import platform_only, require
from control_plane.tenant_scope import bypass_rls_session
from expert_work.persistence import TenantBillingLedgerStore
from expert_work.protocol import Principal

from .usage import _parse_month  # shared YYYY-MM parser (422 on bad input)

#: 403 body for a non-system-admin caller. Per-router on purpose: the message
#: names the resource being protected, which is what makes the refusal actionable.
_PLATFORM_SCOPE_MESSAGE = "only a system admin may view cross-tenant chargeback"


def _get_ledger_store(request: Request) -> TenantBillingLedgerStore:
    return request.app.state.tenant_billing_ledger_store  # type: ignore[no-any-return]


@dataclass
class _TenantCharge:
    """One tenant's month rollup — admin view, FULL split + margin."""

    tenant_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    base_cost_micros: int = 0
    markup_cost_micros: int = 0
    billed_cost_micros: int = 0
    margin_micros: int = 0  # == markup_cost_micros (platform gross margin)
    unpriced_buckets: int = 0


@dataclass
class _AgentCharge:
    """One agent's month rollup within a tenant — admin view, FULL split.

    Stream 12.4 — the ``tenant_billing_ledger`` is already bucketed by
    ``agent_name``; this surfaces that dimension so the platform can see
    per-agent token + cost when drilling into one tenant.
    """

    agent_name: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    base_cost_micros: int = 0
    markup_cost_micros: int = 0
    billed_cost_micros: int = 0
    margin_micros: int = 0
    unpriced_buckets: int = 0


def build_billing_admin_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/admin/billing",
        tags=["billing_admin"],
        dependencies=[Depends(platform_only(_PLATFORM_SCOPE_MESSAGE))],
    )

    @router.get("/chargeback")
    async def chargeback(
        principal: Annotated[Principal, Depends(require("billing", "read"))],
        store: Annotated[TenantBillingLedgerStore, Depends(_get_ledger_store)],
        month: Annotated[str | None, Query()] = None,
        tenant_id: Annotated[UUID | None, Query()] = None,
    ) -> dict[str, object]:
        target = _parse_month(month)
        async with bypass_rls_session():
            rows = await store.list_for_month_all_tenants(month=target)

        charges: dict[str, _TenantCharge] = defaultdict(lambda: _TenantCharge(tenant_id=""))
        for r in rows:
            if tenant_id is not None and r.tenant_id != tenant_id:
                continue
            key = str(r.tenant_id)
            c = charges[key]
            c.tenant_id = key
            c.input_tokens += r.input_tokens
            c.output_tokens += r.output_tokens
            c.cache_creation_tokens += r.cache_creation_tokens
            c.cache_read_tokens += r.cache_read_tokens
            c.base_cost_micros += r.base_cost_micros
            c.markup_cost_micros += r.markup_cost_micros
            c.billed_cost_micros += r.billed_cost_micros
            c.margin_micros += r.markup_cost_micros
            if not r.priced:
                c.unpriced_buckets += 1

        tenants = sorted(charges.values(), key=lambda c: c.tenant_id)
        data: dict[str, object] = {
            "month": target.strftime("%Y-%m"),
            "as_of": datetime.now(tz=UTC).isoformat(),
            "total_base_cost_micros": sum(c.base_cost_micros for c in tenants),
            "total_billed_cost_micros": sum(c.billed_cost_micros for c in tenants),
            "total_margin_micros": sum(c.margin_micros for c in tenants),
            "tenants": [asdict(c) for c in tenants],
        }
        # Stream 12.4 — when scoped to one tenant, also surface the per-agent
        # split (the ledger already buckets by agent_name). Drives the admin-ui
        # drill-down; omitted for the cross-tenant view to keep it lean.
        if tenant_id is not None:
            agents: dict[str, _AgentCharge] = defaultdict(lambda: _AgentCharge(agent_name=""))
            for r in rows:
                if r.tenant_id != tenant_id:
                    continue
                a = agents[r.agent_name]
                a.agent_name = r.agent_name
                a.input_tokens += r.input_tokens
                a.output_tokens += r.output_tokens
                a.cache_creation_tokens += r.cache_creation_tokens
                a.cache_read_tokens += r.cache_read_tokens
                a.base_cost_micros += r.base_cost_micros
                a.markup_cost_micros += r.markup_cost_micros
                a.billed_cost_micros += r.billed_cost_micros
                a.margin_micros += r.markup_cost_micros
                if not r.priced:
                    a.unpriced_buckets += 1
            ordered = sorted(agents.values(), key=lambda a: a.agent_name)
            data["agents"] = [asdict(a) for a in ordered]
        return {"success": True, "data": data, "error": None}

    return router
