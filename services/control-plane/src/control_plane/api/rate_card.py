"""Platform model rate-card CRUD API — Stream Y (Mini-ADR Y-3).

system_admin-only CRUD over the platform-curated model rate card. Every handler:

* first gates on the RBAC matrix via ``require("billing", <action>)``
  (system_admin auto-gets tenant-ADMIN authority there), then re-checks
  ``principal.is_system_admin`` inline — defense in depth: this is a *platform*
  surface (NULL-tenant rows), same precedent as ``mcp_catalog.py``;
* drives every store call inside ``bypass_rls_session()`` because the rate-card
  rows are tenant-less and the RLS policy would otherwise hide them from a
  normally-scoped session;
* audits ``provider``/``model``/prices — these are platform pricing facts, not
  tenant secrets (per [memory:billing-meter-and-entitlement]).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from control_plane.api._authz import platform_only, require
from control_plane.audit import emit
from control_plane.tenant_scope import bypass_rls_session
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence import (
    ModelRateCardConflictError,
    ModelRateCardNotFoundError,
    ModelRateCardStore,
)
from expert_work.protocol import (
    AuditAction,
    ModelRateCardPatch,
    ModelRateCardRecord,
    ModelRateCardUpsert,
    Principal,
)
from expert_work.runtime.audit.logger import AuditLogger

#: 403 body for a non-system-admin caller. Per-router on purpose: the message
#: names the resource being protected, which is what makes the refusal actionable.
_PLATFORM_SCOPE_MESSAGE = "only a system admin may manage the model rate card"


def _get_rate_card_store(request: Request) -> ModelRateCardStore:
    return request.app.state.model_rate_card_store  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _price_details(record: ModelRateCardRecord) -> dict[str, object]:
    """Audit the pricing facts (platform data, no secrets)."""
    return {
        "provider": record.provider,
        "model": record.model,
        "input_per_mtok_micros": record.input_per_mtok_micros,
        "output_per_mtok_micros": record.output_per_mtok_micros,
    }


def build_rate_card_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/platform/rate-card",
        tags=["rate_card"],
        dependencies=[Depends(platform_only(_PLATFORM_SCOPE_MESSAGE))],
    )

    @router.post("", status_code=201)
    async def create_rate_card(
        payload: ModelRateCardUpsert,
        principal: Annotated[Principal, Depends(require("billing", "write"))],
        store: Annotated[ModelRateCardStore, Depends(_get_rate_card_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> dict[str, object]:
        try:
            async with bypass_rls_session():
                record = await store.create(upsert=payload, actor_id=principal.subject_id)
        except ModelRateCardConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "RATE_CARD_DUPLICATE",
                    "message": "a price already exists for this provider/model",
                },
            ) from exc
        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.RATE_CARD_CREATE,
            resource_type="model_rate_card",
            resource_id=str(record.id),
            trace_id=current_trace_id_hex(),
            details=_price_details(record),
        )
        return {"success": True, "data": record.model_dump(mode="json"), "error": None}

    @router.get("")
    async def list_rate_cards(
        principal: Annotated[Principal, Depends(require("billing", "read"))],
        store: Annotated[ModelRateCardStore, Depends(_get_rate_card_store)],
        provider: Annotated[str | None, Query()] = None,
        model: Annotated[str | None, Query()] = None,
    ) -> dict[str, object]:
        async with bypass_rls_session():
            rows = await store.list(provider=provider, model=model)
        return {
            "success": True,
            "data": [r.model_dump(mode="json") for r in rows],
            "error": None,
        }

    @router.get("/{rate_card_id}")
    async def get_rate_card(
        rate_card_id: Annotated[UUID, Path()],
        principal: Annotated[Principal, Depends(require("billing", "read"))],
        store: Annotated[ModelRateCardStore, Depends(_get_rate_card_store)],
    ) -> dict[str, object]:
        async with bypass_rls_session():
            record = await store.get(rate_card_id)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "RATE_CARD_NOT_FOUND", "message": "not found"},
            )
        return {"success": True, "data": record.model_dump(mode="json"), "error": None}

    @router.patch("/{rate_card_id}")
    async def update_rate_card(
        rate_card_id: Annotated[UUID, Path()],
        payload: ModelRateCardPatch,
        principal: Annotated[Principal, Depends(require("billing", "write"))],
        store: Annotated[ModelRateCardStore, Depends(_get_rate_card_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> dict[str, object]:
        try:
            async with bypass_rls_session():
                record = await store.patch(rate_card_id=rate_card_id, patch=payload)
        except ModelRateCardNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "RATE_CARD_NOT_FOUND", "message": "not found"},
            ) from exc
        except ValueError as exc:
            # The merged record failed re-validation (the record validator
            # re-checks provider/model membership in MODEL_CATALOG).
            raise HTTPException(
                status_code=422,
                detail={"code": "RATE_CARD_INVALID", "message": str(exc)},
            ) from exc
        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.RATE_CARD_UPDATE,
            resource_type="model_rate_card",
            resource_id=str(record.id),
            trace_id=current_trace_id_hex(),
            details=_price_details(record),
        )
        return {"success": True, "data": record.model_dump(mode="json"), "error": None}

    @router.delete("/{rate_card_id}", status_code=204)
    async def delete_rate_card(
        rate_card_id: Annotated[UUID, Path()],
        principal: Annotated[Principal, Depends(require("billing", "delete"))],
        store: Annotated[ModelRateCardStore, Depends(_get_rate_card_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> None:
        # Resolve the row first so the audit record carries the pricing facts.
        async with bypass_rls_session():
            existing = await store.get(rate_card_id)
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "RATE_CARD_NOT_FOUND", "message": "not found"},
            )
        try:
            async with bypass_rls_session():
                await store.delete(rate_card_id)
        except ModelRateCardNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "RATE_CARD_NOT_FOUND", "message": "not found"},
            ) from exc
        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.RATE_CARD_DELETE,
            resource_type="model_rate_card",
            resource_id=str(existing.id),
            trace_id=current_trace_id_hex(),
            details=_price_details(existing),
        )

    return router
