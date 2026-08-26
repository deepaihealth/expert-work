"""``/v1/triggers`` CRUD + ``/v1/webhooks`` ingest — Stream J.10 (Mini-ADR J-26 / J-42).

Two routers:

* :func:`build_triggers_router` — authenticated CRUD over the
  ``agent_trigger`` table (``/v1/triggers``).
* :func:`build_webhooks_router` — the inbound webhook endpoint
  (``/v1/webhooks/{trigger_id}``). Exempt from ``AuthMiddleware`` — an
  external caller has no expert_work principal — and authenticated instead by
  a per-trigger secret token (Mini-ADR J-42). A leaked secret can fire
  only its own trigger.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from croniter import croniter
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane._tenant_resource_lock import tenant_resource_lock
from control_plane.agent_disable_status import AgentDisableService
from control_plane.api._authz import console_only
from control_plane.api._user_scope import (
    get_user_repo,
    resolve_caller_user_id,
    resolve_target_user_id,
)
from control_plane.audit import emit
from control_plane.auth.rbac import is_admin
from control_plane.runtime import AgentRuntime
from control_plane.settings import Settings
from control_plane.tenant_scope import (
    CrossTenant,
    applied_scope,
    cross_tenant_query_enabled,
    ensure_single_tenant_scope,
    ensure_tenant_scope,
)
from control_plane.tenant_status import TenantStatusService
from control_plane.trigger_delivery import deliver_run_result
from control_plane.trigger_firing import fire_trigger
from control_plane.uplift.threat_metrics import (
    record_threat_pattern_hits,
    record_threat_scan,
    record_trigger_blocked,
)
from control_plane.uplift.threat_scan import FieldTooLargeError, scan_payload_strict
from expert_work.common.observability import current_trace_id_hex
from expert_work.common.threat_patterns import ThreatFinding
from expert_work.persistence import (
    ApprovalStore,
    ThreadMessageStore,
    ThreadMetaStore,
    TriggerRunStore,
    TriggerStore,
)
from expert_work.persistence.agent_spec import AgentSpecStore
from expert_work.persistence.rls import (
    bypass_rls_var,
    current_tenant_id_var,
    current_user_id_var,
)
from expert_work.persistence.tenant_config import TenantConfigStore
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.protocol import (
    AuditAction,
    TriggerKind,
    TriggerRecord,
    TriggerRunRecord,
    TriggerRunStatus,
    TriggerSpec,
)
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.runs import RunInfo, RunStore
from expert_work.runtime.runs.schemas import TERMINAL_RUN_STATUSES, RunStatus

logger = logging.getLogger("expert_work.control_plane.triggers")

_WEBHOOK_HEADER_NAME = "X-Expert-Work-Webhook-Secret"


def _hash_secret(secret: str) -> str:
    """SHA-256 of a webhook secret — the token is high-entropy random."""
    return hashlib.sha256(secret.encode()).hexdigest()


def _trigger_dict(record: TriggerRecord, *, secret: str | None = None) -> dict[str, Any]:
    """Serialise a trigger row. ``secret`` (webhook plaintext) is shown
    once at creation and never again."""
    body: dict[str, Any] = {
        "id": str(record.id),
        "agent_name": record.agent_name,
        "agent_version": record.agent_version,
        "name": record.name,
        "kind": record.kind,
        "config": record.config,
        "enabled": record.enabled,
        "source": record.source,
        "last_fired_at": record.last_fired_at.isoformat() if record.last_fired_at else None,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }
    if secret is not None:
        body["webhook_secret"] = secret
    return body


def _validate_config(kind: TriggerKind, name: str, config: dict[str, Any]) -> None:
    """Reject a malformed trigger config — 422 on failure.

    ``TriggerSpec`` checks the shape (a ``cron`` trigger has an
    ``expr``); ``croniter.is_valid`` then checks the cron grammar so a
    bad expression never reaches the scheduler.
    """
    try:
        TriggerSpec(name=name, kind=kind, config=config)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if kind == "cron" and not croniter.is_valid(str(config.get("expr", ""))):
        raise HTTPException(status_code=422, detail="config['expr'] is not a valid cron expression")


# Capability Uplift Sprint #1 (Mini-ADR U-2 Layer A) — strict scan.


async def _scan_trigger_strict(
    *,
    name: str,
    config: dict[str, Any],
    tenant_id: UUID,
    actor_id: str,
    audit: AuditLogger,
) -> None:
    """Strict-scope scan of operator-authored trigger config — flag + audit, do
    NOT block on a pattern hit (audit-eval Phase 3); only an oversized,
    unscannable field still raises ``HTTPException(422)``.

    A pattern hit emits ``TRIGGER_PROMPT_INJECTION_WARN`` + bumps Prometheus
    counters and lets the create/patch proceed. Log lines deliberately omit the
    matched pattern_id to avoid log poisoning.
    """
    try:
        result = scan_payload_strict(name=name, config=config)
    except FieldTooLargeError as exc:
        # Generic message; no field content in the response body.
        record_threat_scan(scope="strict", result="blocked")
        record_trigger_blocked(phase="create")
        raise HTTPException(
            status_code=422,
            detail=f"trigger field too large for security scan (path={exc.path}, "
            f"limit={exc.length} bytes)",
        ) from exc
    if result is None:
        record_threat_scan(scope="strict", result="clean")
        return
    field_path, findings = result
    # audit-eval Phase 3 — an operator authoring a trigger in their own tenant
    # should not have it rejected because a strict pattern appears in legit
    # config; flag + audit, do not block. (The oversized-field guard above still
    # blocks — an unscannable field is a different concern.) The fire-time scan
    # already defaults to warn; this aligns the create path.
    record_threat_scan(scope="strict", result="warn")
    record_threat_pattern_hits(findings, scope="strict")
    # The audit row carries tenant_id / field / pattern_count / findings —
    # a parallel ``logger.warning`` would inject user-controlled values
    # into structured log fields (CodeQL py/log-injection).  Skip it; the
    # audit middleware is the system of record for security events.
    await emit(
        audit,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action=AuditAction.TRIGGER_PROMPT_INJECTION_WARN,
        resource_type="trigger",
        trace_id=current_trace_id_hex(),
        details={
            "scope": "strict",
            "field": field_path,
            "pattern_count": len(findings),
            "findings": [_finding_to_dict(f) for f in findings],
        },
    )


def _finding_to_dict(f: ThreatFinding) -> dict[str, Any]:
    return {
        "pattern_id": f.pattern_id,
        "category": f.category,
        "severity": f.severity,
        "excerpt": f.excerpt,
    }


class _CreateTriggerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_name: str = Field(min_length=1)
    agent_version: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=64)
    kind: TriggerKind
    config: dict[str, Any] = Field(default_factory=dict)


class _PatchTriggerBody(BaseModel):
    """All fields optional — only the present ones are applied."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    config: dict[str, Any] | None = None


class _FireNowResponse(BaseModel):
    """Spec 1 PR4 Task 3 — the debug console's "fire now" synchronous result."""

    run_id: str
    thread_id: str
    run_status: str
    trigger_run_status: str
    delivery: str
    delivered_text: str | None = None


def _get_trigger_store(request: Request) -> TriggerStore:
    return request.app.state.trigger_store  # type: ignore[no-any-return]


def _get_trigger_run_store(request: Request) -> TriggerRunStore:
    return request.app.state.trigger_run_store  # type: ignore[no-any-return]


def _get_agent_spec_store(request: Request) -> AgentSpecStore:
    return request.app.state.agent_spec_repo  # type: ignore[no-any-return]


def _get_thread_store(request: Request) -> ThreadMetaStore:
    return request.app.state.thread_meta_repo  # type: ignore[no-any-return]


def _get_runtime(request: Request) -> AgentRuntime:
    return request.app.state.agent_runtime  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _get_approval_store(request: Request) -> ApprovalStore:
    return request.app.state.approval_store  # type: ignore[no-any-return]


def _get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _get_tenant_config_store(request: Request) -> TenantConfigStore:
    return request.app.state.tenant_config_repo  # type: ignore[no-any-return]


def _get_agent_disable_service(request: Request) -> AgentDisableService:
    return request.app.state.agent_disable_service  # type: ignore[no-any-return]


def _get_tenant_status_service(request: Request) -> TenantStatusService:
    return request.app.state.tenant_status_service  # type: ignore[no-any-return]


def _get_run_store(request: Request) -> RunStore:
    return request.app.state.run_store  # type: ignore[no-any-return]


def _get_thread_message_store(request: Request) -> ThreadMessageStore:
    return request.app.state.thread_message_store  # type: ignore[no-any-return]


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession] | None:
    return request.app.state.session_factory  # type: ignore[no-any-return]


def build_triggers_router() -> APIRouter:
    """Stream J.10 — authenticated trigger CRUD."""
    router = APIRouter(prefix="/v1/triggers", tags=["triggers"])

    @router.post("", response_model=None, dependencies=[Depends(console_only())])
    async def create_trigger(
        body: _CreateTriggerBody,
        request: Request,
        triggers: Annotated[TriggerStore, Depends(_get_trigger_store)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        settings: Annotated[Settings, Depends(_get_settings)],
        session_factory: Annotated[
            async_sessionmaker[AsyncSession] | None, Depends(_get_session_factory)
        ],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = request.state.actor_id
        _validate_config(body.kind, body.name, body.config)
        await _scan_trigger_strict(
            name=body.name,
            config=body.config,
            tenant_id=tenant_id,
            actor_id=actor_id,
            audit=audit,
        )

        user_id = await resolve_caller_user_id(request, users)
        now = datetime.now(UTC)
        secret: str | None = None
        secret_hash: str | None = None
        if body.kind == "webhook":
            secret = secrets.token_urlsafe(32)
            secret_hash = _hash_secret(secret)

        record = TriggerRecord(
            id=uuid4(),
            tenant_id=tenant_id,
            user_id=user_id,
            agent_name=body.agent_name,
            agent_version=body.agent_version,
            name=body.name,
            kind=body.kind,
            config=body.config,
            enabled=True,
            source="api",
            webhook_secret_hash=secret_hash,
            created_at=now,
            updated_at=now,
        )

        async def _insert() -> None:
            try:
                await triggers.create(record)
            except (ValueError, IntegrityError) as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"trigger {body.name!r} already exists for agent {body.agent_name!r}",
                ) from exc

        if body.kind == "cron":
            # Task 4 — count-then-insert is TOCTOU-vulnerable across
            # replicas; the scheduler quota check (Mini-ADR J-26 (2), caps a
            # tenant's cron triggers so a runaway client cannot flood the
            # scheduler) and the insert are one per-tenant advisory-locked
            # critical section so two replicas can't both pass the count
            # check and both insert, overshooting the cap.
            async with tenant_resource_lock(session_factory, tenant_id, "cron_trigger"):
                existing = await triggers.count_cron_by_tenant(tenant_id=tenant_id)
                if existing >= settings.max_cron_triggers_per_tenant:
                    raise HTTPException(
                        status_code=429,
                        detail=(
                            "cron trigger quota exhausted "
                            f"(max {settings.max_cron_triggers_per_tenant} per tenant)"
                        ),
                    )
                await _insert()
        else:
            await _insert()

        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.TRIGGER_CREATE,
            resource_type="trigger",
            resource_id=str(record.id),
            trace_id=current_trace_id_hex(),
            details={"name": record.name, "kind": record.kind},
        )
        return JSONResponse(status_code=201, content=_trigger_dict(record, secret=secret))

    @router.get("", response_model=None, dependencies=[Depends(console_only())])
    async def list_triggers(
        request: Request,
        triggers: Annotated[TriggerStore, Depends(_get_trigger_store)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        agent_name: Annotated[str | None, Query(min_length=1)] = None,
        agent_version: Annotated[str | None, Query(min_length=1)] = None,
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,  # Stream N
    ) -> JSONResponse:
        # Stream H.6 (Mini-ADR H-12) — a bare version filter is meaningless.
        if agent_version is not None and agent_name is None:
            raise HTTPException(
                status_code=422,
                detail="agent_version requires agent_name",
            )
        # H.8-F1 (Task 6):非 admin 只见自己的触发器;admin 走原租户/跨租户列表。
        if not is_admin(request.state.principal):
            caller_user_id = await resolve_caller_user_id(request, users)
            if caller_user_id is None:
                return JSONResponse(content={"items": [], "total": 0, "cross_tenant": False})
            items = await triggers.list_by_user(
                tenant_id=request.state.tenant_id,
                user_id=caller_user_id,
                agent_name=agent_name,
            )
            return JSONResponse(
                content={
                    "items": [_trigger_dict(t) for t in items],
                    "total": len(items),
                    "cross_tenant": False,
                }
            )
        scope = await ensure_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/triggers",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            if isinstance(scope, CrossTenant):
                items = await triggers.list_all_tenants(
                    agent_name=agent_name, agent_version=agent_version
                )
            else:
                items = await triggers.list_by_tenant(
                    tenant_id=scope.tenant_id,
                    agent_name=agent_name,
                    agent_version=agent_version,
                )
        return JSONResponse(
            content={
                "items": [_trigger_dict(t) for t in items],
                "total": len(items),
                "cross_tenant": isinstance(scope, CrossTenant),
            }
        )

    @router.get("/{trigger_id}", response_model=None, dependencies=[Depends(console_only())])
    async def get_trigger(
        trigger_id: UUID,
        request: Request,
        triggers: Annotated[TriggerStore, Depends(_get_trigger_store)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W3 read scope — a concrete id lets a system_admin drill into a
        # foreign tenant's trigger; "*" is meaningless (one owning tenant).
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/triggers/{trigger_id}",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            record = await triggers.get(trigger_id=trigger_id, tenant_id=scope.tenant_id)
        if record is None:
            raise HTTPException(status_code=404, detail="trigger not found")
        # H.8-F1 所有权闸:有主触发器仅 owner / admin 可读;resolve 对越权抛 403。
        # 终审 Important#1:record.user_id is None(manifest/service 建的无主触发器)时
        # resolve_target_user_id 会把 requested=None 直接判成"caller 自己"放行——非
        # admin 只要知道 id 就能读无主触发器。无主触发器只许 admin 操作。
        if record.user_id is None:
            if not is_admin(request.state.principal):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "USER_SCOPE_FORBIDDEN",
                        "message": "only tenant admins may act on unowned triggers",
                    },
                )
        else:
            await resolve_target_user_id(request, users, requested=record.user_id)
        return JSONResponse(content=_trigger_dict(record))

    @router.patch("/{trigger_id}", response_model=None, dependencies=[Depends(console_only())])
    async def patch_trigger(
        trigger_id: UUID,
        body: _PatchTriggerBody,
        request: Request,
        triggers: Annotated[TriggerStore, Depends(_get_trigger_store)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = request.state.actor_id
        record = await triggers.get(trigger_id=trigger_id, tenant_id=tenant_id)
        if record is None:
            raise HTTPException(status_code=404, detail="trigger not found")
        if record.user_id is None:
            if not is_admin(request.state.principal):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "USER_SCOPE_FORBIDDEN",
                        "message": "only tenant admins may act on unowned triggers",
                    },
                )
        else:
            await resolve_target_user_id(request, users, requested=record.user_id)

        new_config = body.config if body.config is not None else record.config
        if body.config is not None:
            _validate_config(record.kind, record.name, new_config)
            await _scan_trigger_strict(
                name=record.name,
                config=new_config,
                tenant_id=tenant_id,
                actor_id=actor_id,
                audit=audit,
            )
        updated = record.model_copy(
            update={
                "enabled": body.enabled if body.enabled is not None else record.enabled,
                "config": new_config,
                "updated_at": datetime.now(UTC),
            }
        )
        await triggers.update(updated)
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.TRIGGER_UPDATE,
            resource_type="trigger",
            resource_id=str(trigger_id),
            trace_id=current_trace_id_hex(),
            details={"enabled": updated.enabled},
        )
        return JSONResponse(content=_trigger_dict(updated))

    @router.delete("/{trigger_id}", response_model=None, dependencies=[Depends(console_only())])
    async def delete_trigger(
        trigger_id: UUID,
        request: Request,
        triggers: Annotated[TriggerStore, Depends(_get_trigger_store)],
        trigger_runs: Annotated[TriggerRunStore, Depends(_get_trigger_run_store)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = request.state.actor_id
        record = await triggers.get(trigger_id=trigger_id, tenant_id=tenant_id)
        if record is None:
            raise HTTPException(status_code=404, detail="trigger not found")
        if record.user_id is None:
            if not is_admin(request.state.principal):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "USER_SCOPE_FORBIDDEN",
                        "message": "only tenant admins may act on unowned triggers",
                    },
                )
        else:
            await resolve_target_user_id(request, users, requested=record.user_id)
        # PR3 孤儿行级联 — 无 FK cascade,先删子行再删父行(崩溃安全:中途
        # 崩溃父行仍在,重删即可收敛;反序会留永久孤儿)。
        runs_removed = await trigger_runs.delete_for_triggers(
            trigger_ids=[trigger_id], tenant_id=tenant_id
        )
        deleted = await triggers.delete(trigger_id=trigger_id, tenant_id=tenant_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="trigger not found")
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.TRIGGER_DELETE,
            resource_type="trigger",
            resource_id=str(trigger_id),
            trace_id=current_trace_id_hex(),
            details={"runs_removed": runs_removed},
        )
        return JSONResponse(content={"deleted": True})

    @router.post("/{trigger_id}:fire", response_model=None, dependencies=[Depends(console_only())])
    async def fire_trigger_now(
        trigger_id: UUID,
        request: Request,
        triggers: Annotated[TriggerStore, Depends(_get_trigger_store)],
        trigger_runs: Annotated[TriggerRunStore, Depends(_get_trigger_run_store)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
        agents: Annotated[AgentSpecStore, Depends(_get_agent_spec_store)],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_store)],
        runtime: Annotated[AgentRuntime, Depends(_get_runtime)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        approvals: Annotated[ApprovalStore, Depends(_get_approval_store)],
        tenant_configs: Annotated[TenantConfigStore, Depends(_get_tenant_config_store)],
        disable_service: Annotated[AgentDisableService, Depends(_get_agent_disable_service)],
        tenant_status: Annotated[TenantStatusService, Depends(_get_tenant_status_service)],
        thread_messages: Annotated[ThreadMessageStore, Depends(_get_thread_message_store)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        settings: Annotated[Settings, Depends(_get_settings)],
        session_factory: Annotated[
            async_sessionmaker[AsyncSession] | None, Depends(_get_session_factory)
        ] = None,
    ) -> _FireNowResponse:
        """调试台「立即触发」:发射一次 + 有界轮询到终态 + 成功则投递回原对话。"""
        tenant_id: UUID = request.state.tenant_id
        record = await triggers.get(trigger_id=trigger_id, tenant_id=tenant_id)
        if record is None:
            raise HTTPException(status_code=404, detail="trigger not found")
        # 所有权闸——与 get_trigger 逐字一致。
        if record.user_id is None:
            if not is_admin(request.state.principal):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "USER_SCOPE_FORBIDDEN",
                        "message": "only tenant admins may act on unowned triggers",
                    },
                )
        else:
            await resolve_target_user_id(request, users, requested=record.user_id)
        if record.kind != "cron":
            raise HTTPException(status_code=400, detail="only cron tasks support manual fire")

        now = datetime.now(UTC)
        # 在触发器自己的 user RLS scope 里发射(webhook path 先例:triggers.py:569-585)。
        user_tok = current_user_id_var.set(record.user_id)
        try:
            run_id = await fire_trigger(
                record,
                now=now,
                agent_spec_store=agents,
                runtime=runtime,
                thread_store=threads,
                audit_logger=audit,
                approval_store=approvals,
                trigger_store=triggers,
                tenant_config_store=tenant_configs,
                agent_disable_service=disable_service,
                tenant_status_service=tenant_status,
            )
            if run_id is None:
                raise HTTPException(status_code=409, detail="trigger agent unavailable")
            fired = TriggerRunRecord(
                id=uuid4(),
                tenant_id=tenant_id,
                trigger_id=record.id,
                run_id=run_id,
                status=TriggerRunStatus.FIRED,
                attempt=1,
                triggered_at=now,
            )
            await trigger_runs.create(fired)
        finally:
            current_user_id_var.reset(user_tok)

        # 有界轮询 run 到终态。
        deadline = now + timedelta(seconds=settings.trigger_fire_now_timeout_s)
        run: RunInfo | None = None
        while True:
            run = await runs.get(run_id=run_id, tenant_id=tenant_id)
            if run is not None and run.status is RunStatus.PAUSED:
                # PAUSED = live run waiting on a human approval gate, NOT an
                # outcome. Mirror scheduler._reconcile_one: leave the trigger_run
                # FIRED so a later sweep delivers once the human approves; never
                # mark it FAILED (list_fired filters status==FIRED, so a FAILED
                # mismark would permanently orphan the eventual delivery).
                return _FireNowResponse(
                    run_id=str(run_id),
                    thread_id=str(run.thread_id),
                    run_status=run.status.value,
                    trigger_run_status=TriggerRunStatus.FIRED.value,
                    delivery="pending",
                )
            if run is not None and run.status in TERMINAL_RUN_STATUSES:
                break
            if datetime.now(UTC) >= deadline:
                return _FireNowResponse(
                    run_id=str(run_id),
                    thread_id=str(run.thread_id) if run else "",
                    run_status=run.status.value if run else "running",
                    trigger_run_status=TriggerRunStatus.FIRED.value,
                    delivery="pending",
                )
            await asyncio.sleep(1)

        # 终态处置——SUCCESS 投递,失败转终态(与 scheduler _reconcile_one 语义一致)。
        if run.status is RunStatus.SUCCESS:
            outcome = await deliver_run_result(
                trigger=record,
                run=run,
                runtime=runtime,
                agent_spec_store=agents,
                thread_message_store=thread_messages,
                now=datetime.now(UTC),
                # PROD-9 —— 与 scheduler reconcile 抢同一 thread 的投递,同一把锁。
                session_factory=session_factory,
            )
            won = await trigger_runs.claim_reconcile(
                fired.model_copy(update={"status": TriggerRunStatus.SUCCEEDED})
            )
            if won:
                await emit(
                    audit,
                    tenant_id=tenant_id,
                    actor_id=request.state.actor_id,
                    action=AuditAction.TRIGGER_COMPLETED,
                    resource_type="trigger",
                    resource_id=str(record.id),
                    trace_id=current_trace_id_hex(),
                    details={"run_id": str(run_id), "delivery": outcome.status, "manual": True},
                )
            return _FireNowResponse(
                run_id=str(run_id),
                thread_id=str(run.thread_id),
                run_status=run.status.value,
                trigger_run_status=TriggerRunStatus.SUCCEEDED.value,
                delivery=outcome.status,
                delivered_text=outcome.text,
            )
        # 失败:一次性手动触发不做退避重试,标 FAILED。经 claim_reconcile 与
        # scheduler reconcile 收敛——先 CAS 者定终态;输家读实际状态回填响应。
        error = run.error or "run failed"
        won = await trigger_runs.claim_reconcile(
            fired.model_copy(update={"status": TriggerRunStatus.FAILED, "error": error})
        )
        if won:
            final_status = TriggerRunStatus.FAILED.value
            await emit(
                audit,
                tenant_id=tenant_id,
                actor_id=request.state.actor_id,
                action=AuditAction.TRIGGER_FAILED,
                resource_type="trigger",
                resource_id=str(record.id),
                trace_id=current_trace_id_hex(),
                details={"run_id": str(run_id), "error": error, "manual": True},
            )
        else:
            # scheduler 已先终态化(如置 RETRYING);报实际状态,不重复审计。
            current = await trigger_runs.get(trigger_run_id=fired.id, tenant_id=tenant_id)
            final_status = current.status.value if current else TriggerRunStatus.FAILED.value
        return _FireNowResponse(
            run_id=str(run_id),
            thread_id=str(run.thread_id),
            run_status=run.status.value,
            trigger_run_status=final_status,
            delivery="skipped",
        )

    return router


def build_webhooks_router() -> APIRouter:
    """Stream J.10 — inbound webhook ingest (exempt from AuthMiddleware)."""
    router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])

    @router.post("/{trigger_id}", response_model=None)
    async def receive_webhook(
        trigger_id: UUID,
        request: Request,
        triggers: Annotated[TriggerStore, Depends(_get_trigger_store)],
        trigger_runs: Annotated[TriggerRunStore, Depends(_get_trigger_run_store)],
        agents: Annotated[AgentSpecStore, Depends(_get_agent_spec_store)],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_store)],
        runtime: Annotated[AgentRuntime, Depends(_get_runtime)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        approvals: Annotated[ApprovalStore, Depends(_get_approval_store)],
        tenant_configs: Annotated[TenantConfigStore, Depends(_get_tenant_config_store)],
        disable_service: Annotated[AgentDisableService, Depends(_get_agent_disable_service)],
        tenant_status: Annotated[TenantStatusService, Depends(_get_tenant_status_service)],
        secret: Annotated[str | None, Header(alias=_WEBHOOK_HEADER_NAME)] = None,
    ) -> JSONResponse:
        """Fire a webhook trigger. Auth = the per-trigger secret token."""
        if not secret:
            raise HTTPException(status_code=401, detail="missing webhook secret")

        # The caller has no tenant context — resolve the trigger by id
        # alone under an RLS-bypass scope (Mini-ADR J-42).
        bypass = bypass_rls_var.set(True)
        tenant_scope = current_tenant_id_var.set(None)
        try:
            trigger = await triggers.get_for_webhook(trigger_id=trigger_id)
        finally:
            current_tenant_id_var.reset(tenant_scope)
            bypass_rls_var.reset(bypass)

        # 404 (not 403) for a missing / wrong-kind / disabled trigger so
        # the endpoint never confirms a trigger id's existence.
        if trigger is None or trigger.kind != "webhook" or not trigger.enabled:
            raise HTTPException(status_code=404, detail="webhook not found")
        if trigger.webhook_secret_hash is None or not hmac.compare_digest(
            _hash_secret(secret), trigger.webhook_secret_hash
        ):
            raise HTTPException(status_code=403, detail="invalid webhook secret")

        # Fire inside the trigger's own tenant (+ user) RLS scope.
        now = datetime.now(UTC)
        tenant_tok = current_tenant_id_var.set(trigger.tenant_id)
        bypass_tok = bypass_rls_var.set(False)
        user_tok = current_user_id_var.set(trigger.user_id)
        try:
            run_id = await fire_trigger(
                trigger,
                now=now,
                agent_spec_store=agents,
                runtime=runtime,
                thread_store=threads,
                audit_logger=audit,
                approval_store=approvals,
                trigger_store=triggers,
                tenant_config_store=tenant_configs,
                agent_disable_service=disable_service,
                tenant_status_service=tenant_status,
            )
            if run_id is None:
                raise HTTPException(status_code=503, detail="trigger agent unavailable")
            await trigger_runs.create(
                TriggerRunRecord(
                    id=uuid4(),
                    tenant_id=trigger.tenant_id,
                    trigger_id=trigger.id,
                    run_id=run_id,
                    status=TriggerRunStatus.FIRED,
                    attempt=1,
                    triggered_at=now,
                )
            )
        finally:
            current_user_id_var.reset(user_tok)
            bypass_rls_var.reset(bypass_tok)
            current_tenant_id_var.reset(tenant_tok)

        return JSONResponse(status_code=202, content={"status": "accepted"})

    return router
