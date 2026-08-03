"""Cross-tenant scope resolution — Stream N (Mini-ADR N-3, N-4, N-5).

A single, central decision point that every list/detail endpoint **must**
go through to resolve "which tenant(s) does this request operate on?"
from the route's ``tenant_id`` query parameter and the verified
:class:`Principal`.

The function returns one of two resolutions:

* :class:`SingleTenant` — the request runs against exactly one tenant
  (normal RLS path; the ``app.tenant_id`` GUC is set by the existing
  ``RLSContextMiddleware``).
* :class:`CrossTenant` — the request runs across **all** tenants
  (``bypass_rls_var=True``). Only available when
  ``principal.is_system_admin is True`` (Stream N — Mini-ADR N-3).

Both resolutions emit an audit row when warranted:

* ``tenant_id="*"`` query → ``AuditAction.SYSTEM_CROSS_TENANT_QUERY``
* ``tenant_id`` ≠ ``principal.tenant_id`` (and the caller is system_admin)
  → ``AuditAction.SYSTEM_TENANT_SWITCH``

The companion :func:`bypass_rls_session` async-context-manager wraps a
SQL store call so the ``after_begin`` RLS listener skips emitting
``set_config('app.tenant_id', ...)``. Use only inside endpoints that
have just resolved to :class:`CrossTenant`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, Request

from control_plane.audit import emit
from expert_work.persistence.rls import bypass_rls_var, current_tenant_id_var
from expert_work.protocol import AuditAction, Principal
from expert_work.runtime.audit.logger import AuditLogger

logger = logging.getLogger("expert_work.control_plane.tenant_scope")

_LOG_CTRL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _log_safe(value: object | None) -> str | None:
    """Strip control characters (CR/LF included) from principal-derived
    values before logging so a forged claim cannot inject extra log lines
    (CodeQL py/log-injection). The explicit ``.replace`` chain is the
    newline barrier CodeQL's taint model recognises; the regex sweeps the
    remaining control characters."""
    if value is None:
        return None
    text = str(value).replace("\r", "").replace("\n", "")
    return _LOG_CTRL_CHARS.sub("", text)


def cross_tenant_query_enabled(request: Request) -> bool:
    """Read the deployment-level cross-tenant switch off app settings.

    Stream HX-8 (Mini-ADR HX-H4). Defaults to ``True`` (the Stream N
    behaviour) when settings are absent — e.g. minimal test apps.
    """
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        return True
    return bool(getattr(settings, "cross_tenant_query_enabled", True))


async def _emit_blocked(
    audit: AuditLogger,
    principal: Principal,
    *,
    mode: str,
    trace_id: str | None,
    endpoint: str | None,
    target_tenant: str | None = None,
) -> None:
    details: dict[str, object] = {"mode": mode}
    if endpoint:
        details["endpoint"] = endpoint
    if target_tenant is not None:
        details["target_tenant"] = target_tenant
    await emit(
        audit,
        tenant_id=principal.tenant_id,  # home tenant — audit attribution
        actor_id=principal.subject_id,
        action=AuditAction.SYSTEM_CROSS_TENANT_BLOCKED,
        resource_type="system",
        resource_id=endpoint,
        trace_id=trace_id,
        details=details,
    )
    logger.info(
        "tenant_scope.cross_tenant_blocked",
        extra={
            "actor_id": _log_safe(principal.subject_id),
            "endpoint": _log_safe(endpoint),
            "mode": mode,
        },
    )


def _intent_from_endpoint(endpoint: str) -> Literal["read", "write"]:
    """Derive the access intent from a ``"METHOD /v1/path"`` endpoint string."""
    method = endpoint.split(" ", 1)[0].upper()
    return "read" if method in ("GET", "HEAD") else "write"


async def emit_tenant_switch(
    audit: AuditLogger,
    principal: Principal,
    target: UUID,
    *,
    trace_id: str | None = None,
    endpoint: str | None = None,
) -> None:
    """Emit the ``SYSTEM_TENANT_SWITCH`` audit row — the single write point.

    ``details`` carries ``mode="switch"`` (aligned with :func:`_emit_blocked`'s
    ``{mode: "aggregate"|"switch"}`` vocabulary), the actor's ``home_tenant``,
    and — when the ``"METHOD /v1/path"`` endpoint string is known — the
    ``endpoint`` plus the derived ``intent`` (GET/HEAD → ``"read"``, anything
    else → ``"write"``). Callers outside the resolver (e.g. ``/v1/quota``
    commit/release) reuse this instead of hand-assembling the emit.
    """
    details: dict[str, object] = {
        "mode": "switch",
        "home_tenant": str(principal.tenant_id),
    }
    if endpoint:
        details["endpoint"] = endpoint
        details["intent"] = _intent_from_endpoint(endpoint)
    await emit(
        audit,
        tenant_id=target,  # action recorded under the target tenant
        actor_id=principal.subject_id,
        action=AuditAction.SYSTEM_TENANT_SWITCH,
        resource_type="system",
        resource_id=endpoint,
        trace_id=trace_id,
        details=details,
    )
    logger.info(
        "tenant_scope.tenant_switch",
        extra={
            "actor_id": _log_safe(principal.subject_id),
            "home_tenant": _log_safe(principal.tenant_id),
            "target_tenant": _log_safe(target),
            "endpoint": _log_safe(endpoint),
        },
    )


# ---------------------------------------------------------------------------
# Resolution types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SingleTenant:
    """The request operates on exactly one tenant (normal RLS)."""

    tenant_id: UUID


@dataclass(frozen=True)
class CrossTenant:
    """The request runs across all tenants (RLS bypassed, system_admin only)."""


TenantScopeResolution = SingleTenant | CrossTenant


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


async def ensure_tenant_scope(
    principal: Principal,
    requested_tenant_id: UUID | Literal["*"] | None,
    audit: AuditLogger,
    *,
    trace_id: str | None = None,
    endpoint: str | None = None,
    cross_tenant_enabled: bool = True,
) -> TenantScopeResolution:
    """Resolve ``?tenant_id=`` against the caller's scope.

    Decision matrix:

    ====================  =================  =====================================
    requested_tenant_id   principal status   result
    ====================  =================  =====================================
    ``"*"``               system_admin       :class:`CrossTenant` + audit
    ``"*"``               NOT system_admin   403 ``CROSS_TENANT_FORBIDDEN``
    UUID = home tenant    any                :class:`SingleTenant`
    UUID = other tenant   in allowed_tenants :class:`SingleTenant` (+switch audit for sysadmin)
    UUID = other tenant   NOT allowed        403 ``TENANT_NOT_ALLOWED``
    None                  any                :class:`SingleTenant` (home tenant)
    ====================  =================  =====================================

    When ``CrossTenant`` is returned, the caller MUST wrap the SQL
    query in :func:`bypass_rls_session`; the resolver only decides
    *whether* bypass is allowed, not when to flip the ContextVar.

    All ``"*"`` queries and all explicit tenant switches (where the
    target differs from the principal's home tenant) emit an audit row.

    ``cross_tenant_enabled=False`` (the HX-8 deployment switch, Mini-ADR
    HX-H4) confines system_admin to their home tenant: both the ``"*"``
    aggregate and explicit switches to another tenant raise 403
    ``CROSS_TENANT_DISABLED`` and emit ``SYSTEM_CROSS_TENANT_BLOCKED``.
    Plain tenant users are untouched (their non-home access is already
    governed by ``allowed_tenants``). Callers pass
    ``cross_tenant_enabled=cross_tenant_query_enabled(request)``.
    """
    # --- cross-tenant aggregate path ---------------------------------
    if requested_tenant_id == "*":
        if not principal.is_system_admin:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "CROSS_TENANT_FORBIDDEN",
                    "message": "cross-tenant query (tenant_id=*) requires system_admin",
                },
            )
        if not cross_tenant_enabled:
            await _emit_blocked(
                audit, principal, mode="aggregate", trace_id=trace_id, endpoint=endpoint
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "CROSS_TENANT_DISABLED",
                    "message": "cross-tenant queries are disabled on this deployment",
                },
            )
        await emit(
            audit,
            tenant_id=principal.tenant_id,  # home tenant — audit attribution
            actor_id=principal.subject_id,
            action=AuditAction.SYSTEM_CROSS_TENANT_QUERY,
            resource_type="system",
            resource_id=endpoint,
            trace_id=trace_id,
            details={"endpoint": endpoint} if endpoint else {},
        )
        logger.info(
            "tenant_scope.cross_tenant",
            extra={"actor_id": principal.subject_id, "endpoint": endpoint},
        )
        return CrossTenant()

    # --- single-tenant path ------------------------------------------
    target: UUID = requested_tenant_id if requested_tenant_id is not None else principal.tenant_id

    # principal.allowed_tenants == "*" ⇔ system_admin
    if principal.allowed_tenants != "*" and target not in principal.allowed_tenants:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "TENANT_NOT_ALLOWED",
                "message": "the caller is not authorized for this tenant",
            },
        )

    # HX-8 deployment switch: with cross-tenant access disabled, a
    # system_admin may not switch out of their home tenant either —
    # blocking only the "*" aggregate would leave a per-tenant walk
    # as a loophole (Mini-ADR HX-H4). Plain tenant users are governed
    # by allowed_tenants above and are untouched.
    if target != principal.tenant_id and principal.is_system_admin and not cross_tenant_enabled:
        await _emit_blocked(
            audit,
            principal,
            mode="switch",
            trace_id=trace_id,
            endpoint=endpoint,
            target_tenant=str(target),
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "CROSS_TENANT_DISABLED",
                "message": "cross-tenant access is disabled on this deployment",
            },
        )

    # Tenant switch audit — system_admin (or mTLS) operating on a tenant
    # other than their home tenant. Skipped for normal tenant users
    # whose target always equals their home tenant (already enforced
    # above; the inequality is impossible for them without a 403).
    if target != principal.tenant_id and principal.is_system_admin:
        await emit_tenant_switch(audit, principal, target, trace_id=trace_id, endpoint=endpoint)

    return SingleTenant(tenant_id=target)


async def ensure_single_tenant_scope(
    principal: Principal,
    requested_tenant_id: UUID | Literal["*"] | None,
    audit: AuditLogger,
    *,
    trace_id: str | None = None,
    endpoint: str | None = None,
    cross_tenant_enabled: bool = True,
) -> SingleTenant:
    """:func:`ensure_tenant_scope` for **detail** endpoints — W2 read scope.

    A detail endpoint reads one row that lives in exactly one tenant, so the
    cross-tenant aggregate (``tenant_id=*``) is meaningless there: rejected
    with 400 ``SCOPE_ALL_NOT_SUPPORTED`` *before* resolution (no spurious
    ``SYSTEM_CROSS_TENANT_QUERY`` audit row for a request that can never
    succeed). Everything else — home fallback, 403 ``TENANT_NOT_ALLOWED``,
    the system_admin switch audit, the HX-8 deployment switch — delegates to
    :func:`ensure_tenant_scope` unchanged. Returning :class:`SingleTenant`
    (never the union) lets callers use ``scope.tenant_id`` directly.
    """
    detail = {
        "code": "SCOPE_ALL_NOT_SUPPORTED",
        "message": "detail endpoints require a concrete tenant_id",
    }
    if requested_tenant_id == "*":
        raise HTTPException(status_code=400, detail=detail)
    scope = await ensure_tenant_scope(
        principal,
        requested_tenant_id,
        audit,
        trace_id=trace_id,
        endpoint=endpoint,
        cross_tenant_enabled=cross_tenant_enabled,
    )
    if isinstance(scope, CrossTenant):
        # Unreachable — "*" is the only CrossTenant trigger and was rejected
        # above; kept as a defensive narrow (mypy + future resolver changes).
        raise HTTPException(status_code=400, detail=detail)
    return scope


async def ensure_tenant_scope_home_fallback(
    principal: Principal,
    requested_tenant_id: UUID | Literal["*"] | None,
    audit: AuditLogger,
    *,
    trace_id: str | None = None,
    endpoint: str | None = None,
    cross_tenant_enabled: bool = True,
) -> SingleTenant:
    """:func:`ensure_tenant_scope` for **list** endpoints whose stores have no
    cross-tenant aggregate reader — W3 read scope.

    Once the resolver has authorized and audited the request, a
    :class:`CrossTenant` resolution collapses to the caller's home tenant —
    the pre-scope-threading behavior, mirroring the front-end
    ``concreteTenantScope`` convention. Everything else (home fallback, 403
    ``TENANT_NOT_ALLOWED`` / ``CROSS_TENANT_FORBIDDEN``, the switch audit, the
    HX-8 deployment switch) is exactly :func:`ensure_tenant_scope`.

    W4 note: the knowledge / eval / quality / mcp lists moved off this helper
    to :func:`ensure_tenant_scope` + real ``*_all_tenants`` store branches, and
    ``GET /v1/mcp-servers/available`` to :func:`ensure_single_tenant_scope`.
    The only remaining callers are the two ``/v1/usage`` reads (their W4
    migration is a separate task); retire this helper once they move.
    """
    scope = await ensure_tenant_scope(
        principal,
        requested_tenant_id,
        audit,
        trace_id=trace_id,
        endpoint=endpoint,
        cross_tenant_enabled=cross_tenant_enabled,
    )
    if isinstance(scope, CrossTenant):
        return SingleTenant(tenant_id=principal.tenant_id)
    return scope


# ---------------------------------------------------------------------------
# bypass_rls_session — CrossTenant SQL wrapper
# ---------------------------------------------------------------------------


@asynccontextmanager
async def bypass_rls_session() -> AsyncIterator[None]:
    """Async context manager flipping ``bypass_rls_var=True`` for the body.

    Matches the existing per-worker bypass pattern
    (``CurationWorker._bypass_rls`` / ``Scheduler._bypass_rls``) but
    exposed as a public helper for HTTP endpoints that have just
    resolved to :class:`CrossTenant`. Resets ContextVar on exit so
    nested calls inherit cleanly.
    """
    bypass = bypass_rls_var.set(True)
    tenant = current_tenant_id_var.set(None)
    try:
        yield
    finally:
        current_tenant_id_var.reset(tenant)
        bypass_rls_var.reset(bypass)


@asynccontextmanager
async def applied_scope(scope: TenantScopeResolution) -> AsyncIterator[None]:
    """Apply a :class:`TenantScopeResolution` to the SQL session — Stream N.

    * :class:`CrossTenant` → ``bypass_rls_var=True`` + ``current_tenant_id_var=None``;
      the SQL store's own WHERE clauses (or ``list_all_tenants`` methods) decide
      what gets returned.
    * :class:`SingleTenant` → ``bypass_rls_var=False`` + ``current_tenant_id_var=scope.tenant_id``;
      this **rebinds** the GUC away from the request-middleware default
      (``principal.tenant_id``) so a system_admin's tenant-switch query
      filters at the RLS layer instead of relying purely on the store's
      ``WHERE tenant_id = ?`` clause (defense in depth).

    Endpoints should wrap their SQL store call inside this manager:

    .. code:: python

        scope = await ensure_tenant_scope(principal, tenant_id, audit, ...)
        async with applied_scope(scope):
            if isinstance(scope, CrossTenant):
                rows = await store.list_all_tenants(...)
            else:
                rows = await store.list_by_tenant(tenant_id=scope.tenant_id, ...)
    """
    if isinstance(scope, CrossTenant):
        b = bypass_rls_var.set(True)
        t = current_tenant_id_var.set(None)
        try:
            yield
        finally:
            current_tenant_id_var.reset(t)
            bypass_rls_var.reset(b)
    else:
        b = bypass_rls_var.set(False)
        t = current_tenant_id_var.set(scope.tenant_id)
        try:
            yield
        finally:
            current_tenant_id_var.reset(t)
            bypass_rls_var.reset(b)


__all__ = [
    "CrossTenant",
    "SingleTenant",
    "TenantScopeResolution",
    "applied_scope",
    "bypass_rls_session",
    "emit_tenant_switch",
    "ensure_single_tenant_scope",
    "ensure_tenant_scope",
    "ensure_tenant_scope_home_fallback",
]
