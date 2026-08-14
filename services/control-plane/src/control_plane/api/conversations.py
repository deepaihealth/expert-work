"""``GET /v1/conversations`` — the conversation-centric operations view.

A *conversation* is a ``thread_meta`` row (the ``(agent, user_id,
session_id=thread_id)`` unit) enriched with a rollup of its ``agent_run``
rows: how many runs, whether any errored or is awaiting a human, when it
was last active, and the token totals joined from ``token_usage`` (which
keys on ``trace_id``, not ``run_id``).

This is an **operations** surface, not a per-user one: like ``GET
/v1/runs`` it is tenant-scoped (``ensure_tenant_scope``) and shows every
user's conversations in the tenant, so an operator can answer "what
happened in user X's conversation" without owning the thread. Deep
per-LLM-call traces stay in Langfuse (system_admin only, cross-tenant
red line — see ADR-0005); this reads expert_work's own tenant-isolated tables.

Two endpoints:
  - ``GET /v1/conversations`` — the list (agent / user / status / q filters),
    the spine of the drill-down and the global conversation browser.
  - ``GET /v1/conversations/{thread_id}`` — one conversation's run list +
    aggregate summary. Per-run detail stays at ``GET /v1/sessions/{tid}/runs/{rid}``.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from control_plane.api._authz import console_only, require
from control_plane.audit import emit
from control_plane.tenant_scope import (
    CrossTenant,
    SingleTenant,
    applied_scope,
    cross_tenant_query_enabled,
    ensure_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.thread_message import ThreadMessageStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.persistence.token_usage_store import TokenTotals, TokenUsageStore
from expert_work.protocol import AuditAction, AuditResult, ThreadStatus
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.runs import RunStore
from expert_work.runtime.runs.schemas import RunInfo, ThreadRunAggregate
from expert_work.runtime.runs.store import MAX_LIST_LIMIT, _clamp_limit


def _get_thread_repo(request: Request) -> ThreadMetaStore:
    return request.app.state.thread_meta_repo  # type: ignore[no-any-return]


def _get_run_store(request: Request) -> RunStore:
    return request.app.state.run_store  # type: ignore[no-any-return]


def _get_token_usage_store(request: Request) -> TokenUsageStore:
    return request.app.state.token_usage_store  # type: ignore[no-any-return]


def _get_thread_message_store(request: Request) -> ThreadMessageStore:
    return request.app.state.thread_message_store  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _sum_totals(totals: list[TokenTotals]) -> dict[str, Any] | None:
    """Roll several per-``trace_id`` :class:`TokenTotals` into one summary.

    ``None`` when the conversation has no recorded usage (every run legacy /
    auto-triggered with no ``trace_id``), so the client renders "—" not 0.
    """
    if not totals:
        return None
    input_tokens = sum(t.input_tokens for t in totals)
    output_tokens = sum(t.output_tokens for t in totals)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": sum(t.cache_creation_tokens for t in totals),
        "cache_read_tokens": sum(t.cache_read_tokens for t in totals),
        "total_tokens": input_tokens + output_tokens,
        "llm_calls": sum(t.llm_calls for t in totals),
        "models": sorted({m for t in totals for m in t.models}),
    }


def _tokens_for_thread(
    agg: ThreadRunAggregate | None,
    by_trace: dict[str, TokenTotals],
) -> dict[str, Any] | None:
    """Sum the token usage across a thread's runs (joined by ``trace_id``)."""
    if agg is None:
        return None
    return _sum_totals([by_trace[t] for t in agg.trace_ids if t in by_trace])


def _conversation_to_dict(
    meta: Any,
    agg: ThreadRunAggregate | None,
    by_trace: dict[str, TokenTotals],
) -> dict[str, Any]:
    """Serialise a ``ThreadMeta`` + its run rollup to the list-item JSON."""
    last_run_at = agg.last_run_at if agg is not None else None
    return {
        "thread_id": str(meta.thread_id),
        "tenant_id": str(meta.tenant_id),
        "user_id": str(meta.user_id) if meta.user_id is not None else None,
        "agent_name": meta.agent_name,
        "agent_version": meta.agent_version,
        "title": meta.title,
        "status": meta.status.value,
        "created_at": meta.created_at.isoformat() if meta.created_at is not None else None,
        "updated_at": meta.updated_at.isoformat() if meta.updated_at is not None else None,
        "run_count": agg.run_count if agg is not None else 0,
        "error_count": agg.error_count if agg is not None else 0,
        "pending_count": agg.pending_count if agg is not None else 0,
        "last_run_at": last_run_at.isoformat() if last_run_at is not None else None,
        "tokens": _tokens_for_thread(agg, by_trace),
    }


def _run_to_dict(info: RunInfo, tokens: dict[str, Any] | None) -> dict[str, Any]:
    """Serialise one run inside a conversation-detail run list."""
    return {
        "run_id": str(info.run_id),
        "thread_id": str(info.thread_id),
        "user_id": str(info.user_id) if info.user_id is not None else None,
        "status": info.status.value,
        "is_resume": info.is_resume,
        "error": info.error,
        "created_at": info.created_at.isoformat(),
        "updated_at": info.updated_at.isoformat(),
        "finished_at": info.finished_at.isoformat() if info.finished_at is not None else None,
        "trace_id": info.trace_id,
        "tokens": tokens,
    }


def build_conversations_router() -> APIRouter:
    """Mount ``GET /v1/conversations`` (list) + ``/{thread_id}`` (detail)."""
    router = APIRouter(prefix="/v1/conversations", tags=["conversations"])

    # ``console_only`` blocks a third-party API key (plane isolation); it says
    # nothing about which employee may read. ``user_id`` is a query parameter
    # and ``q`` is full-text over transcript content, so without a role gate any
    # authenticated employee could search any end user's conversation text.
    # Ruling (2026-08-14): a conversation is an operations object (QA review,
    # eval curation), so reads stay open to every employee — viewer / operator /
    # admin all hold ``session:read``. The gate's real job is to deny a
    # principal carrying no role at all.
    @router.get(
        "",
        response_model=None,
        dependencies=[Depends(console_only()), Depends(require("session", "read"))],
    )
    async def list_conversations(
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
        token_usage: Annotated[TokenUsageStore, Depends(_get_token_usage_store)],
        messages: Annotated[ThreadMessageStore, Depends(_get_thread_message_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        agent_name: Annotated[str | None, Query(min_length=1)] = None,
        agent_version: Annotated[str | None, Query(min_length=1)] = None,
        user_id: Annotated[UUID | None, Query()] = None,
        status: Annotated[ThreadStatus | None, Query()] = None,
        q: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        # Operations filter — only conversations with ≥1 failed run
        # (error / timeout). Distinct from ``status=failed``: that is the
        # thread's lifecycle state; an *active* thread can carry errored runs.
        has_error: Annotated[bool, Query()] = False,
        # Only conversations with ≥1 run paused at an approval gate — the
        # "needs a human" queue, in conversation context.
        has_pending: Annotated[bool, Query()] = False,
        # Activity window — only conversations with ≥1 run created at or
        # after this instant ("active in the last N hours"). Composes with
        # ``has_error`` ("what broke today"). Naive datetimes are read as UTC.
        since: Annotated[datetime | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=10000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        trace_id = current_trace_id_hex()
        start = time.monotonic()

        # A bare version filter is meaningless (mirrors GET /v1/runs, H-12).
        if agent_version is not None and agent_name is None:
            raise HTTPException(status_code=422, detail="agent_version requires agent_name")

        scope = await ensure_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=trace_id,
            endpoint="GET /v1/conversations",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )

        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=UTC)

        clamped = _clamp_limit(limit)
        set_capped = False
        async with applied_scope(scope):
            # has_error / has_pending / since resolve the matching thread set
            # from the run store first, then read their metadata — each store
            # keeps to its own table (same composition as the users rollup).
            # Several active filters intersect: a thread must satisfy each
            # one (possibly via different runs).
            narrowed_ids: set[UUID] | None = None
            if has_error or has_pending or since is not None:
                agg_scope = None if isinstance(scope, CrossTenant) else scope.tenant_id
                only_filters: list[Literal["failed", "pending"] | None] = []
                if has_error:
                    only_filters.append("failed")
                if has_pending:
                    only_filters.append("pending")
                if not only_filters:
                    only_filters.append(None)  # bare ``since`` window
                id_sets = [
                    await runs.thread_ids_with_runs(tenant_id=agg_scope, since=since, only=f)
                    for f in only_filters
                ]
                set_capped = any(len(s) >= 500 for s in id_sets)
                narrowed_ids = set.intersection(*id_sets)

            # Content search (IA M4): resolve message-content matches from
            # the transcript mirror; the metadata query then treats a ``q``
            # hit as "title matches OR content matched" (q_thread_ids), so
            # one search box spans both.
            content_hits: set[UUID] | None = None
            if q:
                msg_scope = None if isinstance(scope, CrossTenant) else scope.tenant_id
                content_hits = await messages.search_thread_ids(tenant_id=msg_scope, q=q)
                set_capped = set_capped or len(content_hits) >= 500
            if isinstance(scope, CrossTenant):
                metas = await threads.list_all_tenants(
                    agent_name=agent_name,
                    agent_version=agent_version,
                    user_id=user_id,
                    status=status,
                    nonempty=True,
                    q=q,
                    q_thread_ids=content_hits,
                    thread_ids=narrowed_ids,
                    order_by="last_activity",
                    limit=clamped,
                    offset=offset,
                )
                total = await threads.count_all_tenants(
                    agent_name=agent_name,
                    agent_version=agent_version,
                    user_id=user_id,
                    status=status,
                    nonempty=True,
                    q=q,
                    q_thread_ids=content_hits,
                    thread_ids=narrowed_ids,
                )
                agg_tenant: UUID | None = None
            else:
                metas = await threads.list_by_tenant(
                    scope.tenant_id,
                    agent_name=agent_name,
                    agent_version=agent_version,
                    user_id=user_id,
                    status=status,
                    nonempty=True,
                    q=q,
                    q_thread_ids=content_hits,
                    thread_ids=narrowed_ids,
                    order_by="last_activity",
                    limit=clamped,
                    offset=offset,
                )
                total = await threads.count_by_tenant(
                    scope.tenant_id,
                    agent_name=agent_name,
                    agent_version=agent_version,
                    user_id=user_id,
                    status=status,
                    nonempty=True,
                    q=q,
                    q_thread_ids=content_hits,
                    thread_ids=narrowed_ids,
                )
                agg_tenant = scope.tenant_id

            thread_ids = [m.thread_id for m in metas]
            aggs = (
                await runs.aggregate_by_threads(thread_ids=thread_ids, tenant_id=agg_tenant)
                if thread_ids
                else {}
            )
            all_traces = sorted({t for a in aggs.values() for t in a.trace_ids})
            by_trace = await token_usage.totals_by_trace_ids(all_traces) if all_traces else {}

        items = [_conversation_to_dict(m, aggs.get(m.thread_id), by_trace) for m in metas]

        await emit(
            audit,
            tenant_id=request.state.tenant_id,
            actor_id=request.state.actor_id,
            action=AuditAction.RUN_LIST_READ,
            resource_type="run",
            resource_id=None,
            result=AuditResult.SUCCESS,
            trace_id=trace_id,
            details={
                "view": "conversations",
                "agent_name": agent_name,
                "q_content_search": bool(q),
                "cross_tenant": isinstance(scope, CrossTenant),
                "count": len(items),
                "limit": clamped,
                "offset": offset,
                "elapsed_ms": round((time.monotonic() - start) * 1000, 1),
            },
        )

        headers = {"X-Limit-Capped": "true"} if (limit > MAX_LIST_LIMIT or set_capped) else None
        return JSONResponse(
            content={
                "success": True,
                "data": {
                    "items": items,
                    "total": total,
                    "cross_tenant": isinstance(scope, CrossTenant),
                },
                "error": None,
            },
            headers=headers,
        )

    @router.get(
        "/{thread_id}",
        response_model=None,
        dependencies=[Depends(console_only()), Depends(require("session", "read"))],
    )
    async def get_conversation(
        thread_id: UUID,
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
        token_usage: Annotated[TokenUsageStore, Depends(_get_token_usage_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # A thread belongs to exactly one tenant — a concrete id lets a
        # system_admin drill from the cross-tenant browser; "*" is meaningless.
        tenant_id: Annotated[UUID | None, Query()] = None,
    ) -> JSONResponse:
        trace_id = current_trace_id_hex()
        scope = await ensure_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=trace_id,
            endpoint="GET /v1/conversations/{thread_id}",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        if isinstance(scope, CrossTenant):
            raise HTTPException(
                status_code=422,
                detail="a conversation belongs to one tenant; pass a concrete tenant_id",
            )
        target = scope.tenant_id

        meta = await threads.get(thread_id, tenant_id=target)
        if meta is None:
            raise HTTPException(status_code=404, detail="conversation not found")

        run_list = await runs.list_by_thread(thread_id=thread_id, tenant_id=target)
        aggs = await runs.aggregate_by_threads(thread_ids=[thread_id], tenant_id=target)
        agg = aggs.get(thread_id)

        async with applied_scope(SingleTenant(tenant_id=target)):
            trace_ids = sorted({r.trace_id for r in run_list if r.trace_id is not None})
            by_trace = await token_usage.totals_by_trace_ids(trace_ids) if trace_ids else {}

        runs_json = [
            _run_to_dict(
                r,
                _sum_totals([by_trace[r.trace_id]])
                if r.trace_id is not None and r.trace_id in by_trace
                else None,
            )
            for r in run_list
        ]

        await emit(
            audit,
            tenant_id=request.state.tenant_id,
            actor_id=request.state.actor_id,
            action=AuditAction.SESSION_READ,
            resource_type="session",
            resource_id=str(thread_id),
            result=AuditResult.SUCCESS,
            trace_id=trace_id,
            details={"view": "conversation_detail", "run_count": len(runs_json)},
        )

        summary = _conversation_to_dict(meta, agg, by_trace)
        summary["runs"] = runs_json
        return JSONResponse(content={"success": True, "data": summary, "error": None})

    return router
