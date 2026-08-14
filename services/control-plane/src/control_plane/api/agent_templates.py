"""Platform Agent template catalog CRUD API — Stream Agent-Templates (M1-3).

system_admin-only CRUD over the platform-curated Agent template catalog (the base
manifests tenants ``fork`` via ``extends``). Mirrors ``mcp_catalog.py``: every
handler

* gates on the RBAC matrix via ``require("agent_template", <action>)`` (system_admin
  auto-gets tenant-ADMIN there), then re-checks ``principal.is_system_admin`` inline
  — defense in depth for a *platform* (NULL-tenant) surface;
* drives every store call inside ``bypass_rls_session()`` (NULL-tenant rows would
  otherwise be hidden by RLS — the W-8 trap);
* on any change, invalidates every cached built-agent so inheriting forks re-resolve
  against the updated base on their next build (the security floor re-applies).

Templates are versioned by ``(name, version)`` (from ``spec.metadata``), so a tenant
can pin ``extends: name@1.2.0``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from control_plane.api._authz import platform_only, require
from control_plane.audit import emit
from control_plane.tenant_scope import bypass_rls_session
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence import (
    PlatformAgentTemplateAlreadyExistsError,
    PlatformAgentTemplateNotFoundError,
    PlatformAgentTemplateStore,
)
from expert_work.persistence.agent_spec import AgentSpecStore
from expert_work.protocol import (
    AgentSpec,
    AgentSpecStatus,
    AuditAction,
    PlatformAgentTemplatePatch,
    PlatformAgentTemplateRecord,
    PlatformAgentTemplateStatus,
    PlatformAgentTemplateUpsert,
    Principal,
    parse_extends_ref,
)
from expert_work.runtime.audit.logger import AuditLogger

# Page size for the cross-tenant dependents scan (delete pre-check, PR4 Task 2).
_DEPENDENT_PAGE_SIZE = 200
# The 409 body lists at most this many dependents; ``dependents_total`` is exact.
_DEPENDENT_LIST_CAP = 20


#: 403 body for a non-system-admin caller. Per-router on purpose: the message
#: names the resource being protected, which is what makes the refusal actionable.
_PLATFORM_SCOPE_MESSAGE = "only a system admin may manage the Agent template catalog"


def _get_template_store(request: Request) -> PlatformAgentTemplateStore:
    return request.app.state.platform_agent_template_store  # type: ignore[no-any-return]


def _get_agent_spec_store(request: Request) -> AgentSpecStore:
    return request.app.state.agent_spec_repo  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _get_agent_runtime(request: Request) -> object:
    return getattr(request.app.state, "agent_runtime", None)


def _invalidate_agents(agent_runtime: object) -> None:
    """Evict every cached built-agent so inheriting forks re-resolve against the
    updated template base (and re-apply the security floor) on next build."""
    if agent_runtime is not None:
        agent_runtime.invalidate_all()  # type: ignore[attr-defined]


def _public(record: PlatformAgentTemplateRecord) -> dict[str, object]:
    """Response projection — the full base manifest + marketplace metadata."""
    return record.model_dump(mode="json")


async def _find_extends_dependents(
    *,
    template_store: PlatformAgentTemplateStore,
    agent_spec: AgentSpecStore,
    name: str,
    version: str,
) -> list[dict[str, str]]:
    """Tenant agent_specs whose ``extends`` would break if ``name@version`` were
    deleted (their next build 422s on an unresolvable base) — PR4 Task 2 (D1).

    Caller MUST hold ``bypass_rls_session()`` (cross-tenant spec list + NULL-tenant
    template rows). Store errors propagate — fail-closed: an unverifiable delete is
    a blocked delete.

    A pinned ``extends=name@version`` depends on that exact version. An
    ``extends=name@latest`` depends on it only when no *other* PUBLISHED version
    would remain — the same predicate the build-time resolver applies to
    ``@latest`` (``app.py`` ``_platform_template_resolver`` → ``get_latest(status=
    PUBLISHED)``, consumed by ``runtime._resolve_template_extends``)."""
    versions = await template_store.list_versions(name=name)
    # 404 before 409 — "TEMPLATE_IN_USE" asserts the resource exists; a ghost
    # target (typo'd version, orphaned pin) must surface as not-found, and its
    # dangling dependents are already broken whether or not we delete (review
    # T2 ruling).
    if not any(r.version == version for r in versions):
        raise HTTPException(
            status_code=404,
            detail={"code": "TEMPLATE_NOT_FOUND", "message": "not found"},
        )
    target_is_last_resolvable = not any(
        r.version != version and r.status is PlatformAgentTemplateStatus.PUBLISHED for r in versions
    )
    dependents: list[dict[str, str]] = []
    offset = 0
    while True:
        page = await agent_spec.list_all_tenants(limit=_DEPENDENT_PAGE_SIZE, offset=offset)
        for s in page:
            if s.status is AgentSpecStatus.DELETED:
                continue
            ref = s.spec.spec.extends
            if ref is None:
                continue
            try:
                ref_name, ref_version = parse_extends_ref(ref)
            except ValueError:
                continue
            if ref_name != name:
                continue
            if ref_version == version or (ref_version == "latest" and target_is_last_resolvable):
                dependents.append({"tenant_id": str(s.tenant_id), "agent": f"{s.name}@{s.version}"})
        if len(page) < _DEPENDENT_PAGE_SIZE:
            break
        offset += _DEPENDENT_PAGE_SIZE
    return dependents


def _reject_extends(spec: AgentSpec) -> None:
    """A platform template IS a base — it cannot itself ``extends`` another."""
    if spec.spec.extends is not None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "TEMPLATE_CANNOT_EXTEND",
                "message": "a platform template is a base manifest and cannot declare extends",
            },
        )


def build_agent_templates_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/platform/agent-templates",
        tags=["agent_templates"],
        dependencies=[Depends(platform_only(_PLATFORM_SCOPE_MESSAGE))],
    )

    @router.post("", status_code=201)
    async def create_template(
        payload: PlatformAgentTemplateUpsert,
        principal: Annotated[Principal, Depends(require("agent_template", "write"))],
        store: Annotated[PlatformAgentTemplateStore, Depends(_get_template_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        agent_runtime: Annotated[object, Depends(_get_agent_runtime)],
    ) -> dict[str, object]:
        _reject_extends(payload.spec)
        try:
            async with bypass_rls_session():
                record = await store.create(upsert=payload, created_by=principal.subject_id)
        except PlatformAgentTemplateAlreadyExistsError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "TEMPLATE_DUPLICATE",
                    "message": "name@version already registered",
                },
            ) from exc
        await _emit(audit, principal, AuditAction.AGENT_TEMPLATE_CREATE, record)
        _invalidate_agents(agent_runtime)
        return {"success": True, "data": _public(record), "error": None}

    @router.get("")
    async def list_templates(
        principal: Annotated[Principal, Depends(require("agent_template", "read"))],
        store: Annotated[PlatformAgentTemplateStore, Depends(_get_template_store)],
        category: Annotated[str | None, Query()] = None,
        status: Annotated[PlatformAgentTemplateStatus | None, Query()] = None,
    ) -> dict[str, object]:
        async with bypass_rls_session():
            rows = await store.list(category=category, status=status)
        return {"success": True, "data": [_public(r) for r in rows], "error": None}

    @router.get("/{name}/{version}")
    async def get_template(
        name: Annotated[str, Path()],
        version: Annotated[str, Path()],
        principal: Annotated[Principal, Depends(require("agent_template", "read"))],
        store: Annotated[PlatformAgentTemplateStore, Depends(_get_template_store)],
    ) -> dict[str, object]:
        async with bypass_rls_session():
            record = await store.get(name=name, version=version)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "TEMPLATE_NOT_FOUND", "message": "not found"},
            )
        return {"success": True, "data": _public(record), "error": None}

    @router.put("/{name}/{version}")
    async def update_template_spec(
        name: Annotated[str, Path()],
        version: Annotated[str, Path()],
        payload: AgentSpec,
        principal: Annotated[Principal, Depends(require("agent_template", "write"))],
        store: Annotated[PlatformAgentTemplateStore, Depends(_get_template_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        agent_runtime: Annotated[object, Depends(_get_agent_runtime)],
    ) -> dict[str, object]:
        _reject_extends(payload)
        if payload.metadata.name != name or payload.metadata.version != version:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "TEMPLATE_IDENTITY_MISMATCH",
                    "message": "manifest metadata name/version must match the path",
                },
            )
        async with bypass_rls_session():
            record = await store.update_spec(
                name=name, version=version, spec=payload, updated_by=principal.subject_id
            )
        if record is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "TEMPLATE_NOT_FOUND", "message": "not found"},
            )
        await _emit(audit, principal, AuditAction.AGENT_TEMPLATE_UPDATE, record)
        _invalidate_agents(agent_runtime)
        return {"success": True, "data": _public(record), "error": None}

    @router.patch("/{name}/{version}")
    async def patch_template_meta(
        name: Annotated[str, Path()],
        version: Annotated[str, Path()],
        patch: PlatformAgentTemplatePatch,
        principal: Annotated[Principal, Depends(require("agent_template", "write"))],
        store: Annotated[PlatformAgentTemplateStore, Depends(_get_template_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        agent_runtime: Annotated[object, Depends(_get_agent_runtime)],
    ) -> dict[str, object]:
        async with bypass_rls_session():
            record = await store.update_meta(name=name, version=version, patch=patch)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "TEMPLATE_NOT_FOUND", "message": "not found"},
            )
        await _emit(audit, principal, AuditAction.AGENT_TEMPLATE_UPDATE, record)
        # A status flip (publish/unpublish) changes @latest resolution → invalidate.
        _invalidate_agents(agent_runtime)
        return {"success": True, "data": _public(record), "error": None}

    @router.delete("/{name}/{version}", status_code=204)
    async def delete_template(
        name: Annotated[str, Path()],
        version: Annotated[str, Path()],
        principal: Annotated[Principal, Depends(require("agent_template", "delete"))],
        store: Annotated[PlatformAgentTemplateStore, Depends(_get_template_store)],
        agent_spec: Annotated[AgentSpecStore, Depends(_get_agent_spec_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        agent_runtime: Annotated[object, Depends(_get_agent_runtime)],
    ) -> None:
        try:
            async with bypass_rls_session():
                # PR4 Task 2 (D1, no force): block the delete while live tenant
                # specs still extend this version — deleting would 422 their
                # next build. The 409 path emits no delete audit.
                dependents = await _find_extends_dependents(
                    template_store=store, agent_spec=agent_spec, name=name, version=version
                )
                if dependents:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "TEMPLATE_IN_USE",
                            "message": f"extended by {len(dependents)} tenant agent(s)",
                            "dependents_total": len(dependents),
                            "dependents": dependents[:_DEPENDENT_LIST_CAP],
                        },
                    )
                await store.delete(name=name, version=version)
        except PlatformAgentTemplateNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "TEMPLATE_NOT_FOUND", "message": "not found"},
            ) from exc
        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.AGENT_TEMPLATE_DELETE,
            resource_type="platform_agent_template",
            resource_id=f"{name}@{version}",
            trace_id=current_trace_id_hex(),
            details={"name": name, "version": version, "dependents_checked": True},
        )
        _invalidate_agents(agent_runtime)

    return router


async def _emit(
    audit: AuditLogger,
    principal: Principal,
    action: AuditAction,
    record: PlatformAgentTemplateRecord,
) -> None:
    await emit(
        audit,
        tenant_id=principal.tenant_id,
        actor_id=principal.subject_id,
        action=action,
        resource_type="platform_agent_template",
        resource_id=f"{record.name}@{record.version}",
        trace_id=current_trace_id_hex(),
        details={
            "name": record.name,
            "version": record.version,
            "category": record.category,
            "required_tier": record.required_tier.value,
            "status": record.status.value,
        },
    )
