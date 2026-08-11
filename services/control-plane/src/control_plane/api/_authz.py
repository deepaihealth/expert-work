"""FastAPI dependencies that enforce :mod:`control_plane.auth.rbac` — Stream C.3.

Centralises the ``authorize`` pattern used by admin routers so each
handler can declare its required ``(resource, action)`` without touching
audit emission.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request

from control_plane.audit import emit
from control_plane.auth.abac import ResourceAttrs, authorize_resource
from control_plane.auth.rbac import Action, Resource, collect_roles_for_audit, is_allowed
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.auth import RoleBindingStore
from expert_work.protocol import AuditAction, AuditResult, Principal, RoleBinding
from expert_work.runtime.audit.logger import AuditLogger

logger = logging.getLogger("expert_work.control_plane.api.authz")


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _principal(request: Request) -> Principal:
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        # AuthMiddleware should have already 401'd, but belt-and-braces.
        raise HTTPException(status_code=401, detail="unauthenticated")
    return principal


def require(resource: Resource, action: Action) -> Callable[..., Awaitable[Principal]]:
    """Return a FastAPI dependency that 403s if the principal lacks ``(resource, action)``."""

    async def _dep(
        request: Request,
        principal: Annotated[Principal, Depends(_principal)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> Principal:
        if is_allowed(principal, resource=resource, action=action):
            return principal
        try:
            await emit(
                audit,
                tenant_id=principal.tenant_id,
                actor_id=principal.subject_id,
                action=AuditAction.AUTH_LOGIN_FAILED,
                resource_type="user",
                resource_id=f"{resource}:{action}",
                result=AuditResult.DENIED,
                reason="RBAC_FORBIDDEN",
                trace_id=current_trace_id_hex(),
                details={
                    "resource": resource,
                    "action": action,
                    "roles": list(collect_roles_for_audit(principal)),
                    "subject_type": principal.subject_type,
                },
            )
        except Exception:
            # Never block the 403 on audit failure; record it and proceed.
            logger.exception("authz.deny_audit_emit_failed")
        raise HTTPException(
            status_code=403,
            detail={"code": "FORBIDDEN", "message": "principal lacks required role"},
        )

    return _dep


def require_key_scope(action: Action) -> Callable[..., Awaitable[None]]:
    """Route dependency — 403 a service-account (API-key) principal whose
    scopes don't cover ``("session", action)``.

    The session plane (``/v1/sessions`` / ``/v1/approvals`` / ``/v1/runs`` /
    uploads) predates ``require(...)`` and carried no scope enforcement at
    all: any valid same-tenant key — including one minted with zero scopes —
    could read run output, start or resume runs (bypassing the
    ``require("session", "write")`` gate on
    ``POST /v1/agents/{agent_code}/runs``), decide approvals and purge
    sessions. Human JWTs and mTLS service principals are deliberately NOT
    gated here — their behavior on these routers is unchanged. Scope
    semantics follow the key fallback in :func:`is_allowed`
    (``admin`` → ADMIN, ``write`` → OPERATOR, ``read`` → VIEWER), so
    ``write`` keys keep the documented "write includes read" behavior.
    """

    async def _dep(
        request: Request,
        principal: Annotated[Principal, Depends(_principal)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> None:
        if principal.subject_type != "service_account":
            return
        if is_allowed(principal, resource="session", action=action):
            return
        try:
            await emit(
                audit,
                tenant_id=principal.tenant_id,
                actor_id=principal.subject_id,
                action=AuditAction.AUTH_LOGIN_FAILED,
                resource_type="user",
                resource_id=f"session:{action}",
                result=AuditResult.DENIED,
                reason="API_KEY_SCOPE_FORBIDDEN",
                trace_id=current_trace_id_hex(),
                details={
                    "resource": "session",
                    "action": action,
                    "scopes": list(principal.scopes),
                    "subject_type": principal.subject_type,
                },
            )
        except Exception:
            # Never block the 403 on audit failure; record it and proceed.
            logger.exception("authz.deny_audit_emit_failed")
        raise HTTPException(
            status_code=403,
            detail={
                "code": "FORBIDDEN",
                "message": "API key scopes do not cover this operation",
            },
        )

    return _dep


async def _conditioned_bindings(request: Request, principal: Principal) -> list[RoleBinding]:
    """The principal's conditioned tenant bindings (slow-path ABAC source).

    Returns ``[]`` when no binding store is wired, the subject is not a user,
    or the tenant / subject id is unusable — the caller then denies (the RBAC
    fast path already failed).
    """
    store: RoleBindingStore | None = getattr(request.app.state, "role_binding_repo", None)
    if store is None or principal.subject_type != "user" or principal.tenant_id is None:
        return []
    try:
        subject_uuid = UUID(principal.subject_id)
    except (ValueError, AttributeError):
        return []
    bindings = await store.list_for_subject(
        subject_type="user", subject_id=subject_uuid, tenant_id=principal.tenant_id
    )
    return [b for b in bindings if b.has_conditions]


async def ensure_resource_access(
    request: Request,
    *,
    resource: Resource,
    action: Action,
    attrs: ResourceAttrs,
) -> Principal:
    """Stream 8.5 — instance-level (RBAC + ABAC) authorization for one resource.

    Call this from a handler AFTER it has loaded the resource, passing the
    instance :class:`ResourceAttrs`. Decision (additive / most-permissive):

    1. ``is_allowed`` — an unconditioned grant (JWT realm role, system_admin, or
       an unconditioned binding) authorises any instance → return.
    2. otherwise, a conditioned binding whose role grants ``(resource, action)``
       AND whose conditions match ``attrs`` authorises this instance → return.
    3. otherwise 403 (with a denial audit row, like :func:`require`).
    """
    principal = _principal(request)
    if is_allowed(principal, resource=resource, action=action):
        return principal

    bindings = await _conditioned_bindings(request, principal)
    if authorize_resource(
        resource=resource, action=action, attrs=attrs, conditioned_bindings=bindings
    ):
        return principal

    audit = _get_audit(request)
    try:
        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.AUTH_LOGIN_FAILED,
            resource_type="user",
            resource_id=f"{resource}:{action}",
            result=AuditResult.DENIED,
            reason="ABAC_FORBIDDEN",
            trace_id=current_trace_id_hex(),
            details={
                "resource": resource,
                "action": action,
                "resource_id": attrs.resource_id,
                "roles": list(collect_roles_for_audit(principal)),
                "subject_type": principal.subject_type,
            },
        )
    except Exception:
        logger.exception("authz.deny_audit_emit_failed")
    raise HTTPException(
        status_code=403,
        detail={"code": "FORBIDDEN", "message": "principal lacks access to this resource"},
    )
