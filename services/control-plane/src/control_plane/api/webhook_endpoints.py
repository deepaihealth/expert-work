"""``/v1/webhook-endpoints`` CRUD — HX-9 outbound webhook hook (STREAM-HX § 13).

Authenticated CRUD over the ``webhook_endpoint`` table — the tenant's
registered outbound delivery targets. The delivery worker (PR3) reads
enabled endpoints and fans out agent-lifecycle events to them.

Mirrors the J.10 triggers CRUD (``api/triggers.py``): tenant-scoped reads,
Stream N cross-tenant list, audit emit, an HMAC secret shown once at
creation, and a per-tenant quota. The delivery URL is SSRF-validated at
registration (private / metadata addresses blocked) — the request the
worker later sends never carries platform credentials (Mini-ADR HX-J5).
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane._tenant_resource_lock import tenant_resource_lock
from control_plane.audit import emit
from control_plane.settings import Settings
from control_plane.tenant_scope import (
    CrossTenant,
    applied_scope,
    cross_tenant_query_enabled,
    ensure_single_tenant_scope,
    ensure_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.common.url_validation import RemoteURLError, validate_remote_url
from expert_work.persistence import WebhookDeliveryStore, WebhookEndpointStore
from expert_work.protocol import (
    AuditAction,
    WebhookEndpointRecord,
    WebhookEventType,
    WebhookPayloadFormat,
)
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.secret_store import SecretStore

#: Allowed subscription event types — must match ``WebhookEventType``.
_EVENT_TYPES: frozenset[str] = frozenset(
    (
        "run.completed",
        "run.failed",
        "approval.requested",
        "artifact.saved",
        "skill_promote.requested",
        "quality.drift",
    )
)


def _secret_ref(endpoint_id: UUID) -> str:
    """SecretStore key for an endpoint's HMAC signing secret."""
    return f"webhook-endpoint/{endpoint_id}"


def _endpoint_dict(record: WebhookEndpointRecord, *, secret: str | None = None) -> dict[str, Any]:
    """Serialise an endpoint row. ``secret`` (HMAC plaintext) is shown once
    at creation and never again (Mini-ADR HX-J5)."""
    body: dict[str, Any] = {
        "id": str(record.id),
        "name": record.name,
        "url": record.url,
        "event_types": list(record.event_types),
        "agent_name": record.agent_name,
        "enabled": record.enabled,
        "source": record.source,
        "payload_format": record.payload_format,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }
    if secret is not None:
        body["secret"] = secret
    return body


def _validate_url(url: str) -> None:
    """Reject SSRF targets at registration (private / metadata / bad scheme)."""
    try:
        validate_remote_url(url)
    except RemoteURLError as exc:
        raise HTTPException(status_code=422, detail=f"invalid webhook url: {exc}") from exc


def _validate_event_types(event_types: list[str]) -> tuple[WebhookEventType, ...]:
    """At least one subscribed type, all from the known set."""
    if not event_types:
        raise HTTPException(status_code=422, detail="event_types must be non-empty")
    unknown = [e for e in event_types if e not in _EVENT_TYPES]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown event_types: {unknown}")
    # De-dup while preserving order.
    seen: dict[str, None] = {}
    for e in event_types:
        seen.setdefault(e, None)
    return tuple(seen)  # type: ignore[arg-type]  # validated against _EVENT_TYPES above


class _CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1, max_length=2048)
    event_types: list[str] = Field(min_length=1)
    agent_name: str | None = None
    payload_format: WebhookPayloadFormat = "generic"


class _PatchBody(BaseModel):
    """All fields optional — only the present ones are applied."""

    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(default=None, min_length=1, max_length=2048)
    event_types: list[str] | None = None
    agent_name: str | None = None
    enabled: bool | None = None
    payload_format: WebhookPayloadFormat | None = None


def _get_store(request: Request) -> WebhookEndpointStore:
    return request.app.state.webhook_endpoint_store  # type: ignore[no-any-return]


def _get_delivery_store(request: Request) -> WebhookDeliveryStore:
    return request.app.state.webhook_delivery_store  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _get_secret_store(request: Request) -> SecretStore:
    return request.app.state.secret_store  # type: ignore[no-any-return]


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession] | None:
    return request.app.state.session_factory  # type: ignore[no-any-return]


def build_webhook_endpoints_router() -> APIRouter:
    """HX-9 — authenticated outbound webhook endpoint CRUD."""
    router = APIRouter(prefix="/v1/webhook-endpoints", tags=["webhook-endpoints"])

    @router.post("", response_model=None)
    async def create_endpoint(
        body: _CreateBody,
        request: Request,
        store: Annotated[WebhookEndpointStore, Depends(_get_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        settings: Annotated[Settings, Depends(_get_settings)],
        secret_store: Annotated[SecretStore, Depends(_get_secret_store)],
        session_factory: Annotated[
            async_sessionmaker[AsyncSession] | None, Depends(_get_session_factory)
        ],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = request.state.actor_id
        _validate_url(body.url)
        event_types = _validate_event_types(body.event_types)

        # Task 4 — count-then-insert is TOCTOU-vulnerable across replicas;
        # the whole check + insert is one per-tenant advisory-locked
        # critical section so two replicas can't both pass the count check
        # and both insert, overshooting the cap.
        async with tenant_resource_lock(session_factory, tenant_id, "webhook_endpoint"):
            existing = await store.count_by_tenant(tenant_id=tenant_id)
            if existing >= settings.max_webhook_endpoints_per_tenant:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        "webhook endpoint quota exhausted "
                        f"(max {settings.max_webhook_endpoints_per_tenant} per tenant)"
                    ),
                )

            # HMAC signing secret: stored in the SecretStore (encrypted at
            # rest); the row holds only a ref. The plaintext is returned once
            # here and never again (Mini-ADR HX-J5). The delivery worker
            # resolves the ref to sign outbound requests.
            endpoint_id = uuid4()
            secret = secrets.token_urlsafe(32)
            secret_ref = _secret_ref(endpoint_id)
            await secret_store.put(secret_ref, secret)
            now = datetime.now(UTC)
            record = WebhookEndpointRecord(
                id=endpoint_id,
                tenant_id=tenant_id,
                user_id=None,
                name=body.name,
                url=body.url,
                event_types=event_types,
                agent_name=body.agent_name,
                secret_ref=secret_ref,
                enabled=True,
                source="api",
                payload_format=body.payload_format,
                created_at=now,
                updated_at=now,
            )
            try:
                await store.create(record)
            except (ValueError, IntegrityError) as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"webhook endpoint {body.name!r} already exists for this tenant",
                ) from exc

        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.WEBHOOK_ENDPOINT_CREATE,
            resource_type="webhook_endpoint",
            resource_id=str(record.id),
            trace_id=current_trace_id_hex(),
            details={"name": record.name, "event_types": list(record.event_types)},
        )
        return JSONResponse(status_code=201, content=_endpoint_dict(record, secret=secret))

    @router.get("", response_model=None)
    async def list_endpoints(
        request: Request,
        store: Annotated[WebhookEndpointStore, Depends(_get_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        agent_name: Annotated[str | None, Query(min_length=1)] = None,
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,  # Stream N
    ) -> JSONResponse:
        scope = await ensure_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/webhook-endpoints",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            if isinstance(scope, CrossTenant):
                items = await store.list_all_tenants(agent_name=agent_name)
            else:
                items = await store.list_by_tenant(tenant_id=scope.tenant_id, agent_name=agent_name)
        return JSONResponse(
            content={
                "items": [_endpoint_dict(e) for e in items],
                "total": len(items),
                "cross_tenant": isinstance(scope, CrossTenant),
            }
        )

    @router.get("/{endpoint_id}", response_model=None)
    async def get_endpoint(
        endpoint_id: UUID,
        request: Request,
        store: Annotated[WebhookEndpointStore, Depends(_get_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W3 read scope — a concrete id lets a system_admin drill into a
        # foreign tenant's endpoint; "*" is meaningless (one owning tenant).
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/webhook-endpoints/{endpoint_id}",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            record = await store.get(endpoint_id=endpoint_id, tenant_id=scope.tenant_id)
        if record is None:
            raise HTTPException(status_code=404, detail="webhook endpoint not found")
        return JSONResponse(content=_endpoint_dict(record))

    @router.patch("/{endpoint_id}", response_model=None)
    async def patch_endpoint(
        endpoint_id: UUID,
        body: _PatchBody,
        request: Request,
        store: Annotated[WebhookEndpointStore, Depends(_get_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = request.state.actor_id
        record = await store.get(endpoint_id=endpoint_id, tenant_id=tenant_id)
        if record is None:
            raise HTTPException(status_code=404, detail="webhook endpoint not found")

        new_url = record.url
        if body.url is not None:
            _validate_url(body.url)
            new_url = body.url
        new_event_types = record.event_types
        if body.event_types is not None:
            new_event_types = _validate_event_types(body.event_types)

        updated = record.model_copy(
            update={
                "url": new_url,
                "event_types": new_event_types,
                "agent_name": body.agent_name if body.agent_name is not None else record.agent_name,
                "enabled": body.enabled if body.enabled is not None else record.enabled,
                "payload_format": (
                    body.payload_format
                    if body.payload_format is not None
                    else record.payload_format
                ),
                "updated_at": datetime.now(UTC),
            }
        )
        await store.update(updated)
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.WEBHOOK_ENDPOINT_UPDATE,
            resource_type="webhook_endpoint",
            resource_id=str(endpoint_id),
            trace_id=current_trace_id_hex(),
            details={"enabled": updated.enabled},
        )
        return JSONResponse(content=_endpoint_dict(updated))

    @router.delete("/{endpoint_id}", response_model=None)
    async def delete_endpoint(
        endpoint_id: UUID,
        request: Request,
        store: Annotated[WebhookEndpointStore, Depends(_get_store)],
        deliveries: Annotated[WebhookDeliveryStore, Depends(_get_delivery_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = request.state.actor_id
        # PR3 孤儿行级联 — 无 FK cascade,先删子行再删父行(崩溃安全:中途
        # 崩溃父行仍在,重删即可收敛;反序会留永久孤儿)。
        deliveries_removed = await deliveries.delete_for_endpoints(
            endpoint_ids=[endpoint_id], tenant_id=tenant_id
        )
        deleted = await store.delete(endpoint_id=endpoint_id, tenant_id=tenant_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="webhook endpoint not found")
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.WEBHOOK_ENDPOINT_DELETE,
            resource_type="webhook_endpoint",
            resource_id=str(endpoint_id),
            trace_id=current_trace_id_hex(),
            details={"deliveries_removed": deliveries_removed},
        )
        return JSONResponse(content={"deleted": True})

    return router
