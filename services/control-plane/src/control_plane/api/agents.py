"""``/v1/agents`` CRUD — Stream B.5.

Wraps :class:`AgentSpecStore` plus :class:`ManifestLoader` (B.4) and
emits ``manifest:{read,write,delete}`` audit records on every mutation
via the per-request :class:`AuditLogger`.

Body shape: the create / update endpoints accept ``{"manifest_yaml":
"...", "template_vars": {...}}``. The control-plane never accepts a
pre-parsed AgentSpec — round-tripping YAML keeps lint enforcement at
the boundary.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from control_plane.agent_disable_status import AgentDisableService
from control_plane.api._authz import console_only, ensure_resource_access, require
from control_plane.api._external import (
    ExternalScopeError,
    lookup_external_user_id,
    reject_nul,
    reject_nul_deep,
    resolve_external_user_id,
)
from control_plane.api._idempotency import (
    IDEMPOTENCY_HEADER,
    MAX_IDEMPOTENCY_KEY_LEN,
    request_digest,
)
from control_plane.api._quota_admission import check_admission
from control_plane.api._user_scope import get_user_repo
from control_plane.api.external_events import build_events_response
from control_plane.api.runs import (
    MAX_RUN_IMAGE_REFS,
    MAX_RUN_INPUT_CHARS,
    MAX_RUN_INPUT_KEYS,
    MAX_RUN_INPUT_TOTAL_BYTES,
    MAX_RUN_INPUT_VALUE_CHARS,
    MAX_UNTRUSTED_CONTENT_BLOCK_CHARS,
    RunRequest,
    spawn_run,
)
from control_plane.api.uploads import is_safe_document_upload_id
from control_plane.audit import emit
from control_plane.auth.abac import ResourceAttrs
from control_plane.manifest import (
    ManifestError,
    ManifestLoader,
    ManifestSyntaxError,
    ManifestTemplateError,
    ManifestValidationError,
)
from control_plane.quota.base import QuotaService
from control_plane.runtime import AgentRuntime
from control_plane.tenancy import TenantConfigNotConfiguredError
from control_plane.tenant_scope import (
    CrossTenant,
    applied_scope,
    bypass_rls_session,
    cross_tenant_query_enabled,
    ensure_single_tenant_scope,
    ensure_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.common.uplift_metrics import record_manifest_provider_rejected
from expert_work.persistence import ApprovalStore, TriggerStore
from expert_work.persistence.agent_disable import AgentDisableStore
from expert_work.persistence.agent_instance import AgentInstanceStore
from expert_work.persistence.agent_spec import AgentSpecStore, DuplicateAgentSpecError
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.protocol import (
    AgentSpec,
    AgentSpecRecord,
    AgentSpecRevisionRecord,
    AgentSpecStatus,
    AuditAction,
    AuditResult,
    PlatformAgentTemplateStatus,
    Principal,
    Provider,
    TenantPlan,
    tier_satisfies,
)
from expert_work.protocol.multimodal import parse_image_ref
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.runs import RunEventStore, RunIdempotencyConflict, RunInfo, RunStore
from expert_work.runtime.stream_bridge import StreamBridge
from orchestrator import AgentFactoryError

logger = logging.getLogger("expert_work.control_plane.agents")


class ManifestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_yaml: str = Field(min_length=1)
    template_vars: dict[str, Any] | None = None


def _record_attrs(record: AgentSpecRecord) -> ResourceAttrs:
    """Stream 8.5 — ABAC attributes for a stored manifest instance."""
    return ResourceAttrs(
        resource_id=record.name,
        labels=record.spec.metadata.labels,
        owner_id=record.created_by,
    )


def _spec_attrs(spec: AgentSpec, *, owner_id: str) -> ResourceAttrs:
    """Stream 8.5 — ABAC attributes for a manifest being created (no record yet)."""
    return ResourceAttrs(
        resource_id=spec.metadata.name,
        labels=spec.metadata.labels,
        owner_id=owner_id,
    )


class AgentDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    record: AgentSpecRecord


class AgentList(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[AgentSpecRecord]
    total: int
    cross_tenant: bool = False  # Stream N — true ⇔ ?tenant_id=* response


class RevisionSummary(BaseModel):
    """One history entry, without the full spec payload (Stream HX-5).

    The list view needs actor / time / sha; the diff view fetches the
    two full snapshots it compares via ``GET .../revisions/{n}``.
    """

    model_config = ConfigDict(frozen=True)

    revision: int
    spec_sha256: str
    actor_id: str
    created_at: str


class RevisionList(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[RevisionSummary]


class RevisionDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    record: AgentSpecRevisionRecord


# ---------------------------------------------------------------------------
# Dependency injection — pulls everything from request.app.state
# ---------------------------------------------------------------------------


def _get_repo(request: Request) -> AgentSpecStore:
    return request.app.state.agent_spec_repo  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _get_loader(request: Request) -> ManifestLoader:
    return request.app.state.manifest_loader  # type: ignore[no-any-return]


def _collect_manifest_providers(spec: AgentSpec) -> set[Provider]:
    """Stream O Mini-ADR O-4 — collect every provider this manifest
    transitively references for the publish-time whitelist gate.

    Mirrors :func:`control_plane.api.tenant_config._providers_referenced_by`
    but operates on a single :class:`AgentSpec` rather than an iterable
    of stored records. Includes the primary model + its fallback chain,
    vision model + its fallbacks, and the memory_consolidation aux
    model (Sprint #7).
    """
    referenced: set[Provider] = set()
    stack = [spec.spec.model]
    if spec.spec.vision is not None:
        stack.append(spec.spec.vision.model)
        stack.extend(spec.spec.vision.fallbacks)
    consolidation = spec.spec.policies.memory_consolidation
    if consolidation.aux_model is not None:
        stack.append(consolidation.aux_model)
    while stack:
        current = stack.pop()
        referenced.add(current.provider)  # type: ignore[arg-type]
        stack.extend(current.fallback)
    return referenced


def _envelope_error(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "data": None,
            "error": {"code": code, "message": message},
        },
    )


def _idempotent_run_response(
    run: RunInfo,
    *,
    mode: Literal["stream", "queue"],
    event_store: RunEventStore | None,
    stream_bridge: StreamBridge,
) -> StreamingResponse | JSONResponse:
    """Render an idempotency-hit ``run`` back in the shape its ``mode`` expects.

    External-API-v1 P2-a Task 14 — stream mode replays the run's event
    stream via ``build_events_response`` (the same wire format ``GET
    .../runs/{id}/events`` produces) instead of the ``422
    IDEMPOTENCY_NOT_SUPPORTED_FOR_STREAM`` Task 13 returned. Shared by both
    idempotency-hit call sites in ``run_agent_for_user`` below: the
    pre-``spawn_run`` cache-hit check, and the post-``spawn_run``
    conflict-loser requery.

    Task 15 — queue mode's 202 body is always the ``{success, data, error}``
    envelope, matching ``spawn_run``'s own queue-mode branch called with
    ``envelope=True`` (this helper has no console caller — both call sites
    below sit inside the external ``run_agent_for_user`` endpoint — so unlike
    ``spawn_run`` there is no flat-body branch to preserve).
    """
    if mode == "stream":
        return build_events_response(run=run, event_store=event_store, stream_bridge=stream_bridge)
    return JSONResponse(
        status_code=202,
        content={
            "success": True,
            "data": {
                "run_id": str(run.run_id),
                "thread_id": str(run.thread_id),
                "status": run.status.value,
            },
            "error": None,
        },
    )


def _safe_document_name_or_422(name: str) -> str:
    """校验第三方回填的文档 ``upload_id``。

    修复轮 1(原顾虑 1)——早期实现按 brief 字面"只接受纯文件名,含 `/` 一律
    拒",但 ``uploads.py`` 的 ``_safe_workspace_name`` 恒定产出
    ``uploads/<stem><ext>``(带 ``uploads/`` 前缀),那条规则会把**唯一合法
    的真实 upload_id 全部拒掉**,整条"上传 → run"流程端到端死——这是 brief
    本身的规则错,不是"再加一层防御"的问题。

    正确规则不是"拒绝一切 `/`",而是"必须正好是 ``_safe_workspace_name``
    有可能生成的形状"——``is_safe_document_upload_id``(与 `_safe_workspace_name``
    同文件、同源字符集)。上传与 run 是两个独立请求,即便生成规则收紧了,
    run 这一侧仍然必须独立复核这个字符串(客户端给的是不可信输入,攻击者
    可以直接调 run,不必经过上传路径)。

    失败走 ``HTTPException``(结构化 ``detail``),调用方 ``run_agent_for_user``
    在端点边界转译成对外 ``{success, data, error}`` 信封 —— 不放行裸
    ``{"detail": ...}``。
    """
    cleaned = name.strip()
    if not is_safe_document_upload_id(cleaned):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_FILE_REF",
                "message": (
                    "document upload_id must be a workspace ref returned by "
                    "POST /v1/agents/{agent_code}/uploads (uploads/<name>)"
                ),
            },
        )
    return cleaned


def _spec_sha256(spec_json: Mapping[str, Any]) -> str:
    canonical = json.dumps(spec_json, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ForkTemplateRequest(BaseModel):
    """Body for ``POST /v1/agents/fork`` — Stream Agent-Templates (M1-4).

    Forks a published platform template into a tenant-owned agent. ``name`` is the
    new agent's identifier (its ``agent_code``), unique within the tenant.
    ``template_version`` may be the literal ``"latest"`` (resolved to the newest
    published version and **pinned** in the fork's ``extends``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    template_name: str = Field(min_length=1)
    template_version: str = Field(default="latest", min_length=1)
    name: str = Field(min_length=1, max_length=128)


async def _resolve_plan(tenant_config_service: object, tenant_id: UUID) -> TenantPlan:
    """Tenant plan tier for template entitlement (FREE when unwired / unseeded)."""
    if tenant_config_service is None:
        return TenantPlan.FREE
    try:
        cfg = await tenant_config_service.get(tenant_id=tenant_id)  # type: ignore[attr-defined]
    except TenantConfigNotConfiguredError:
        return TenantPlan.FREE
    return cfg.plan  # type: ignore[no-any-return]


def _get_thread_repo(request: Request) -> ThreadMetaStore:
    return request.app.state.thread_meta_repo  # type: ignore[no-any-return]


def _get_instance_store(request: Request) -> AgentInstanceStore:
    return request.app.state.agent_instance_store  # type: ignore[no-any-return]


def _get_runtime(request: Request) -> AgentRuntime:
    return request.app.state.agent_runtime  # type: ignore[no-any-return]


def _invalidate_agent_build_cache(request: Request, tenant_id: UUID) -> None:
    """Evict the tenant's built-agent cache after a manifest write.

    :class:`AgentRuntime` keys built agents on ``(tenant, name, version)`` and
    only consults the spec on a miss, so an in-place edit (same version — an
    approval-gate / tool / model / prompt change from the form editor) is
    invisible to new runs until the stale build is dropped. Every manifest
    write path (PUT / rollback / delete) funnels through here. ``getattr``
    guards the handful of test setups that build routers without a runtime.
    """
    runtime = getattr(request.app.state, "agent_runtime", None)
    if runtime is not None:
        runtime.invalidate_tenant(tenant_id)


def _get_approvals(request: Request) -> ApprovalStore:
    return request.app.state.approval_store  # type: ignore[no-any-return]


def _get_quota(request: Request) -> QuotaService:
    return request.app.state.quota_service  # type: ignore[no-any-return]


def _get_agent_disable_repo(request: Request) -> AgentDisableStore:
    return request.app.state.agent_disable_repo  # type: ignore[no-any-return]


def _get_agent_disable_service(request: Request) -> AgentDisableService:
    return request.app.state.agent_disable_service  # type: ignore[no-any-return]


def _get_run_store(request: Request) -> RunStore:
    return request.app.state.run_store  # type: ignore[no-any-return]


def _get_run_event_store(request: Request) -> RunEventStore | None:
    # External-API-v1 P2-a Task 14 — same accessor as external_events.py /
    # runs.py (each file already carries its own copy of this one-liner;
    # not worth a shared import for something this small).
    store: RunEventStore | None = getattr(request.app.state, "run_event_store", None)
    return store


def _get_trigger_store(request: Request) -> TriggerStore:
    return request.app.state.trigger_store  # type: ignore[no-any-return]


class _SessionError(Exception):
    """Internal control-flow signal for the external session/run helpers; the
    endpoints convert it to an envelope error."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


async def _resolve_session(
    *,
    tenant_id: UUID,
    agent_code: str,
    actor_id: str,
    user_id: str,
    session_id: UUID | None,
    repo: AgentSpecStore,
    threads: ThreadMetaStore,
    users: TenantUserStore,
    instances: AgentInstanceStore,
    disable_service: AgentDisableService,
) -> tuple[AgentSpecRecord, UUID, UUID]:
    """Resolve agent_code → active record, mint the end-user, create / continue
    the session thread, and touch the per-user instance binding. Returns
    ``(record, thread_id, end_user_id)``. Raises :class:`_SessionError`.

    Shared by the external session-bind and run endpoints (M1-5b)."""
    # Stream RT-4 (RT-ADR-16) — kill switch gate: a disabled agent accepts no
    # new sessions / runs. Checked before resolving the record so a disabled
    # agent is opaque regardless of which version is active.
    if await disable_service.is_disabled(tenant_id, agent_code):
        raise _SessionError("AGENT_DISABLED", f"agent {agent_code!r} is disabled", 403)
    active = await repo.list_by_tenant(
        tenant_id=tenant_id, status=AgentSpecStatus.ACTIVE, name=agent_code, limit=1
    )
    if not active:
        raise _SessionError(
            "AGENT_NOT_FOUND", f"no active agent {agent_code!r} for this tenant", 404
        )
    record = active[0]

    # Resolve the end-user. The app owns its user_id namespace; the id is
    # namespaced with the `ext:` prefix before it becomes subject_id so it can
    # never collide with an employee's bare Keycloak sub — see
    # `_external.EXTERNAL_SUBJECT_PREFIX` for the full rationale. (Any valid
    # tenant key may act for any of its users — network-layer hardening is a
    # later addition; every call is audited with on_behalf_of.) Both helpers
    # normalize (strip) `user_id` through the same `external_subject_id`, so a
    # space-suffixed id mints/finds the same identity on every path
    # (External-API-v1 P1 review, Important), and both surface a blank one as
    # 422 `INVALID_USER_ID` rather than minting a namespaced empty identity.
    #
    # WHICH semantics apply is decided by `session_id`, and the dividing line is
    # the one `_external.load_owned_session` documents: *does this call create
    # the session it addresses* — not read-vs-write (both branches here are
    # writes).
    #
    # - `session_id is None` → this call creates the session, so mint-on-use is
    #   deliberate product behavior: a third party never pre-registers its end
    #   users, and the first `POST .../runs` or `POST .../sessions` under a
    #   fresh `user_id` must bring the `tenant_user` row into existence. Do not
    #   "harden" this into a lookup — it would break the integration model.
    # - `session_id is not None` → this call addresses an **already-existing**
    #   session, whose owner therefore already has a row. The only row minting
    #   could add here is one for a `user_id` that by definition does NOT own
    #   the session — i.e. exactly the case that must 404. Resolving first and
    #   checking ownership after is how pointing one known `session_id` at
    #   enumerated `user_id`s left a ghost row per rejected attempt on the
    #   user-dimension ops page, and how a *rejected* call still cleared a
    #   purged identity's `deleted_at` (resurrecting it, and making it
    #   permanently uncollectable by the `deleted_at`-driven Phase-3b hard
    #   delete). Same defect and same reasoning as `load_owned_run` and the
    #   upload path (P1 final review C1 + wrap-up N1); these two endpoints were
    #   the last two members of the family.
    try:
        if session_id is None:
            end_user_id = await resolve_external_user_id(
                tenant_id=tenant_id, user_id=user_id, users=users
            )
        else:
            looked_up = await lookup_external_user_id(
                tenant_id=tenant_id, user_id=user_id, users=users
            )
            if looked_up is None:
                # Unknown (or soft-deleted) end user + a session id they cannot
                # own — indistinguishable from "no such session", and reported
                # as such so the response leaks no existence information.
                raise ExternalScopeError(
                    "SESSION_NOT_FOUND", "session not found for this user / agent", 404
                )
            end_user_id = looked_up
    except ExternalScopeError as exc:
        raise _SessionError(exc.code, exc.message, exc.status_code) from exc

    if session_id is not None:
        meta = await threads.get(session_id, tenant_id=tenant_id)
        if meta is None or meta.user_id != end_user_id or meta.agent_name != agent_code:
            raise _SessionError("SESSION_NOT_FOUND", "session not found for this user / agent", 404)
        thread_id = session_id
    else:
        thread_id = uuid4()
        await threads.create(
            thread_id=thread_id,
            tenant_id=tenant_id,
            created_by=actor_id,
            user_id=end_user_id,
            agent_name=agent_code,
            agent_version=record.version,
        )

    await instances.touch(tenant_id=tenant_id, agent_code=agent_code, user_id=end_user_id)
    return record, thread_id, end_user_id


class BindSessionRequest(BaseModel):
    """Body for ``POST /v1/agents/{agent_code}/sessions`` — Stream Agent-Templates
    (M1-5b). An external app (tenant API-key) binds / continues a per-user session.

    ``user_id`` is the app's own identifier for its end-user; it is minted into a
    ``tenant_user`` on first use (the app does not pre-onboard users). ``session_id``
    continues an existing conversation; omit it to start a new one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1, max_length=255)
    session_id: UUID | None = None


class ExternalFileRef(BaseModel):
    """一条附件引用。``upload_id`` 是 ``POST /v1/agents/{code}/uploads`` 的返回值。

    ``transfer_method`` 目前只有 ``local_file``。字段现在就存在是为了日后加
    ``remote_url`` 时只是扩枚举(向后兼容),而不是改形状(破坏性)。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["image", "document"]
    transfer_method: Literal["local_file"] = "local_file"
    upload_id: str = Field(min_length=1, max_length=1024)


class ExternalRunRequest(BaseModel):
    """Body for ``POST /v1/agents/{agent_code}/runs`` — Stream Agent-Templates
    (M1-5b-2). Binds / continues a per-user session and runs in one call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1, max_length=255)
    session_id: UUID | None = None
    input: str | None = Field(default=None, max_length=MAX_RUN_INPUT_CHARS)
    mode: Literal["stream", "queue"] = "stream"
    image_refs: list[str] = Field(default_factory=list, max_length=64)
    #: 单块长度上限(``MAX_UNTRUSTED_CONTENT_BLOCK_CHARS``,与内部
    #: ``RunRequest._bound_untrusted_blocks`` 同值)未在这个字段声明里体现
    #: ——同 ``inputs`` 一样,必须在 ``run_agent_for_user`` 里手工预检(P2-a
    #: 安全修复,Critical——否则一个超长块会在下方手工构造 ``RunRequest``
    #: 时炸出裸 ``pydantic.ValidationError``,即 500,而不是 422)。
    untrusted_content: list[str] = Field(default_factory=list, max_length=16)
    #: P2 —— 提示词模板变量,与内部 ``RunRequest.inputs`` 同语义(未声明键 422、
    #: 必填缺失 422)。未声明键 / 必填缺失校验在 ``spawn_run`` 内部由
    #: ``validate_prompt_inputs`` 统一执行,此处不重复;但 64 键 / 单值 8192
    #: 字符 / 序列化后总字节数这三条上限(``RunRequest._bound_inputs`` 前两条 +
    #: ``MAX_RUN_INPUT_TOTAL_BYTES`` 第三条,P2-a 安全修复,Important——单值
    #: 长度检查只认 ``str``,包一层 list/dict 就绕过)必须在这个端点里手工
    #: 预检——见下方 ``run_agent_for_user`` 里 ``RunRequest`` 手工构造前的
    #: 检查,原因同 ``TOO_MANY_IMAGE_REFS``。
    inputs: dict[str, Any] = Field(default_factory=dict)
    #: P2 块 1 —— 统一附件引用。``type == "image"`` 的条目合并进
    #: ``image_refs`` 交给 ``spawn_run`` 里现成的 ``_validate_image_refs``
    #: 做 thread 绑定 / 条数上限 / ``supports_vision`` 三重校验(见
    #: ``run_agent_for_user``)。``type == "document"`` 的条目在同一处过
    #: ``_safe_document_name_or_422`` 净化后并入 ``RunRequest.document_names``
    #: (Task 11)。
    files: list[ExternalFileRef] = Field(default_factory=list, max_length=64)

    # External-API-v1 P2-b NUL-byte hardening — ``input`` lands in
    # ``agent_run.enqueued_input`` (queue mode) verbatim; ``untrusted_content``
    # / ``inputs`` land there too, and ``inputs`` also reaches
    # ``validate_prompt_inputs``'s Jinja render. All three are JSONB, which —
    # like ``text`` — rejects an embedded NUL byte (``\x00``) with a bare
    # asyncpg ``CharacterNotInRepertoireError`` there is no fallback exception
    # handler for (see ``_external.py``'s ``_NUL`` doc comment). Checked here,
    # on the FastAPI request-body model itself, rather than as a hand-rolled
    # pre-check next to the other bounds below (``TOO_MANY_IMAGE_REFS`` etc.):
    # those exist because ``RunRequest`` is hand-constructed past FastAPI's
    # validation path, but ``ExternalRunRequest`` *is* that path, so a
    # ``field_validator`` here 422s through the existing
    # ``RequestValidationError`` handler with no extra wiring.
    @field_validator("input")
    @classmethod
    def _no_nul_input(cls, value: str | None) -> str | None:
        return value if value is None else reject_nul(value, field="input")

    @field_validator("untrusted_content")
    @classmethod
    def _no_nul_untrusted_content(cls, value: list[str]) -> list[str]:
        for block in value:
            reject_nul(block, field="untrusted_content")
        return value

    @field_validator("inputs")
    @classmethod
    def _no_nul_inputs(cls, value: dict[str, Any]) -> dict[str, Any]:
        return reject_nul_deep(value, field="inputs")


class AgentDisableRequest(BaseModel):
    """Body for ``POST /v1/agents/{name}/disable|enable`` — Stream RT-4 (RT-ADR-16).

    ``reason`` is an optional free-text note captured on the audit row + the
    ``agent_disable`` record (shown in the UI). Both endpoints accept it; enable
    clears the stored reason regardless."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str | None = Field(default=None, max_length=500)

    # External-API-v1 P2-b NUL-byte hardening — ``reason`` lands in
    # ``agent_disable.reason`` (a ``Text`` column) verbatim via
    # ``AgentDisableStore.set_disabled``. Both routes gate on
    # ``require("manifest", "write")``, not ``console_only()`` — a
    # third-party API key minted with ``write`` scope maps to the OPERATOR
    # role, which is granted ``manifest: {read, write}`` (``rbac.py``), so
    # these two endpoints are reachable by the same external caller class as
    # every other field this pass hardens, even though they predate
    # External-API-v1 and live outside ``external_*.py``.
    @field_validator("reason")
    @classmethod
    def _no_nul_reason(cls, value: str | None) -> str | None:
        return value if value is None else reject_nul(value, field="reason")


async def _load_manifest(
    payload: ManifestPayload,
    loader: ManifestLoader,
) -> tuple[Any, str]:
    """Parse the request body into an ``AgentSpec`` + canonical sha256."""
    spec = loader.load_from_string(
        payload.manifest_yaml,
        template_vars=payload.template_vars,
    )
    spec_json = spec.model_dump(by_alias=True, mode="json")
    return spec, _spec_sha256(spec_json)


def _manifest_error_to_response(exc: ManifestError) -> JSONResponse:
    """Map a parse / lint error to the public envelope.

    The raw exception text is logged server-side but **never** echoed to
    the API caller (CodeQL ``py/stack-trace-exposure``). The structured
    ``exc.errors`` list from :class:`ManifestValidationError` is field-
    level info we have already produced ourselves, so it's safe to
    surface.
    """
    # ``exc_info`` is intentionally False: passing the raw exception makes
    # CodeQL flag this site as forwarding traceback info to log handlers
    # that the API response code also touches. The exception ``type`` /
    # ``message`` already captured below give operators what they need.
    logger.info(
        "manifest.load_failed exc_type=%s",
        type(exc).__name__,
    )

    if isinstance(exc, ManifestValidationError):
        # ``exc.errors`` came from a hand-curated whitelist built inside
        # ``loader._validate`` (loc / type / msg only); no traceback or
        # Pydantic-internal data reaches the response body.
        sanitized_errors = list(exc.errors)
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "data": None,
                "error": {
                    "code": "MANIFEST_INVALID",
                    "message": "manifest failed validation",
                    "errors": sanitized_errors,
                },
            },
        )
    if isinstance(exc, ManifestTemplateError):
        return _envelope_error(
            "MANIFEST_TEMPLATE",
            "manifest template rendering failed",
            400,
        )
    if isinstance(exc, ManifestSyntaxError):
        return _envelope_error("MANIFEST_SYNTAX", "manifest is not valid YAML", 400)
    return _envelope_error("MANIFEST_ERROR", "manifest could not be parsed", 400)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


#: ``console_only()`` attached **per route**, not per prefix.
#:
#: ``/v1/agents`` is the one mount point the console plane and the external
#: (third-party) plane share, so the prefix-driven lockdown that closed
#: ``/v1/sessions``, ``/v1/users``, … to API keys structurally cannot be
#: applied here: it would also 403 the eight ``/v1/agents/{agent_code}/…``
#: routes that ARE the third-party surface. That is precisely why these
#: routes were missed — a prefix sweep cannot see them (P1 final review,
#: Critical C2). Routes carrying it below are console-only reads/writes of
#: tenant-wide manifest + end-user data; each one measurably answered 200 to
#: a **zero-scope** service-account key before this, since none of them has
#: ``require(...)`` or an in-handler ``ensure_resource_access`` either.
#: ``console_only`` only rejects ``subject_type == "service_account"`` —
#: employee JWTs and mTLS service principals are untouched.
_CONSOLE_ONLY = [Depends(console_only())]


def build_agents_router() -> APIRouter:
    router = APIRouter(prefix="/v1/agents", tags=["agents"])

    @router.post("", status_code=201)
    async def create_agent(
        payload: ManifestPayload,
        request: Request,
        repo: Annotated[AgentSpecStore, Depends(_get_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        loader: Annotated[ManifestLoader, Depends(_get_loader)],
    ) -> JSONResponse:
        tenant_id = request.state.tenant_id
        actor_id = request.state.actor_id
        trace_id = current_trace_id_hex()

        try:
            spec, sha = await _load_manifest(payload, loader)
        except ManifestError as exc:
            await emit(
                audit,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action=AuditAction.MANIFEST_WRITE,
                resource_type="manifest",
                resource_id=None,
                result=AuditResult.ERROR,
                reason=type(exc).__name__,
                trace_id=trace_id,
            )
            return _manifest_error_to_response(exc)

        # Stream 8.5 — instance-level RBAC + ABAC on the create. A conditioned
        # binding (e.g. operator restricted to resource_ids / a label) may only
        # create matching manifests; the creator is the owner for owner_only.
        await ensure_resource_access(
            request,
            resource="manifest",
            action="write",
            attrs=_spec_attrs(spec, owner_id=actor_id),
        )

        # Stream O Mini-ADR O-4 — manifest publish provider whitelist gate.
        # Reject if the spec references a provider the platform does not
        # support. Runtime LLMRouter would also reject (build_llm_router
        # uses these providers), but the manifest-time gate gives a
        # clean 403 with the offending provider list rather than a
        # late agent-build error.
        #
        # Empty ``supported_providers`` = deployment hasn't opted into
        # Stream O yet (legacy / dev mode); the gate is a no-op so
        # existing manifests keep working. Operators opt in by setting
        # ``EXPERT_WORK_SUPPORTED_PROVIDERS`` env, which activates the
        # whitelist enforcement.
        settings = request.app.state.settings
        supported = set(settings.supported_providers)
        referenced = _collect_manifest_providers(spec)
        invalid = sorted(referenced - supported) if supported else []
        if invalid:
            for provider in invalid:
                record_manifest_provider_rejected(provider=provider)
            await emit(
                audit,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action=AuditAction.MANIFEST_WRITE,
                resource_type="manifest",
                resource_id=f"{spec.metadata.name}/{spec.metadata.version}",
                result=AuditResult.DENIED,
                reason="provider_not_supported",
                trace_id=trace_id,
                details={"unsupported_providers": invalid},
            )
            return _envelope_error(
                "MANIFEST_PROVIDER_NOT_SUPPORTED",
                f"manifest references providers not in the platform's "
                f"supported_providers list: {invalid}",
                403,
            )

        try:
            record = await repo.create(
                tenant_id=tenant_id,
                spec=spec,
                spec_sha256=sha,
                created_by=actor_id,
            )
        except DuplicateAgentSpecError:
            logger.info(
                "manifest.create_duplicate name=%s version=%s",
                spec.metadata.name,
                spec.metadata.version,
            )
            await emit(
                audit,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action=AuditAction.MANIFEST_WRITE,
                resource_type="manifest",
                resource_id=f"{spec.metadata.name}/{spec.metadata.version}",
                result=AuditResult.ERROR,
                reason="duplicate",
                trace_id=trace_id,
            )
            return _envelope_error(
                "MANIFEST_DUPLICATE",
                "an agent with this name and version already exists",
                409,
            )

        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.MANIFEST_WRITE,
            resource_type="manifest",
            resource_id=f"{record.name}/{record.version}",
            trace_id=trace_id,
            details={"spec_sha256": sha, "revision": 1},
        )
        return JSONResponse(
            status_code=201,
            content={"success": True, "data": AgentDetail(record=record).model_dump(mode="json")},
        )

    @router.get("/templates")
    async def list_templates(
        request: Request,
        principal: Annotated[Principal, Depends(require("manifest", "read"))],
        category: str | None = None,
    ) -> JSONResponse:
        """List the platform Agent templates a tenant may fork (M1-6).

        Tenant-facing marketplace browse: returns published + enabled templates,
        deduped to the newest published version per ``name``, each annotated with
        ``can_fork`` (whether the tenant's plan satisfies the template's tier).
        Lighter than the system-admin catalog — no base manifest in the payload.
        """
        tenant_id = request.state.tenant_id
        template_store = request.app.state.platform_agent_template_store
        tcs = getattr(request.app.state, "tenant_config_service", None)
        plan = await _resolve_plan(tcs, tenant_id)

        # NULL-tenant catalog rows → bypass_rls. ``list`` is ordered by name then
        # newest version first, so the first row seen per name is its latest.
        async with bypass_rls_session():
            rows = await template_store.list(
                category=category, status=PlatformAgentTemplateStatus.PUBLISHED
            )

        seen: set[str] = set()
        items: list[dict[str, object]] = []
        for row in rows:
            if not row.enabled or row.name in seen:
                continue
            seen.add(row.name)
            items.append(
                {
                    "name": row.name,
                    "version": row.version,
                    "display_name": row.display_name,
                    "description": row.description,
                    "category": row.category,
                    "icon": row.icon,
                    "required_tier": row.required_tier.value,
                    "can_fork": tier_satisfies(plan, row.required_tier),
                }
            )
        return JSONResponse(content={"success": True, "data": items})

    @router.post("/fork", status_code=201)
    async def fork_template(
        payload: ForkTemplateRequest,
        request: Request,
        repo: Annotated[AgentSpecStore, Depends(_get_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        """Fork a published platform template into a tenant-owned agent (M1-4).

        Materializes a copy of the template base manifest, pins ``extends`` to the
        resolved template version (so the tier① security floor re-applies at build),
        renames it to the tenant's ``agent_code``, and persists it as an ordinary
        tenant ``agent_spec`` — editable thereafter via the normal agent CRUD.
        """
        tenant_id = request.state.tenant_id
        actor_id = request.state.actor_id
        trace_id = current_trace_id_hex()
        template_store = request.app.state.platform_agent_template_store
        tcs = getattr(request.app.state, "tenant_config_service", None)

        # 1. Load the platform base template (NULL-tenant rows → bypass_rls).
        async with bypass_rls_session():
            if payload.template_version == "latest":
                base = await template_store.get_latest(
                    name=payload.template_name,
                    status=PlatformAgentTemplateStatus.PUBLISHED,
                )
            else:
                base = await template_store.get(
                    name=payload.template_name, version=payload.template_version
                )
        if (
            base is None
            or base.status is not PlatformAgentTemplateStatus.PUBLISHED
            or not base.enabled
        ):
            return _envelope_error(
                "TEMPLATE_NOT_AVAILABLE",
                "template not found, not published, or disabled",
                404,
            )

        # 2. Entitlement — the tenant's plan must satisfy the template's tier.
        plan = await _resolve_plan(tcs, tenant_id)
        if not tier_satisfies(plan, base.required_tier):
            return _envelope_error(
                "TEMPLATE_TIER_FORBIDDEN",
                f"forking this template requires the {base.required_tier.value} plan",
                403,
            )

        # 3. Materialize the fork: copy the base manifest, pin extends to the
        #    resolved concrete version, rename to the tenant's agent_code.
        pinned = f"{base.name}@{base.version}"
        doc = base.spec.model_dump(by_alias=True, mode="json")
        doc["metadata"]["name"] = payload.name
        doc["metadata"]["tenant"] = str(tenant_id)
        doc["spec"]["extends"] = pinned
        try:
            fork_spec = AgentSpec.model_validate(doc)
        except ValidationError as exc:
            return _envelope_error("FORK_INVALID", str(exc), 422)

        # 4. ABAC + provider whitelist gate (parity with create_agent).
        await ensure_resource_access(
            request,
            resource="manifest",
            action="write",
            attrs=_spec_attrs(fork_spec, owner_id=actor_id),
        )
        settings = request.app.state.settings
        supported = set(settings.supported_providers)
        referenced = _collect_manifest_providers(fork_spec)
        invalid = sorted(referenced - supported) if supported else []
        if invalid:
            return _envelope_error(
                "MANIFEST_PROVIDER_NOT_SUPPORTED",
                f"template references providers not in the platform's "
                f"supported_providers list: {invalid}",
                403,
            )

        # 5. Persist as an ordinary tenant agent_spec.
        sha = _spec_sha256(doc)
        try:
            record = await repo.create(
                tenant_id=tenant_id, spec=fork_spec, spec_sha256=sha, created_by=actor_id
            )
        except DuplicateAgentSpecError:
            return _envelope_error(
                "MANIFEST_DUPLICATE",
                "an agent with this name and version already exists",
                409,
            )

        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.MANIFEST_WRITE,
            resource_type="manifest",
            resource_id=f"{record.name}/{record.version}",
            trace_id=trace_id,
            details={"forked_from": pinned, "revision": 1},
        )
        return JSONResponse(
            status_code=201,
            content={"success": True, "data": AgentDetail(record=record).model_dump(mode="json")},
        )

    @router.post("/{agent_code}/sessions", status_code=201)
    async def bind_session(
        agent_code: str,
        payload: BindSessionRequest,
        request: Request,
        principal: Annotated[Principal, Depends(require("session", "write"))],
        repo: Annotated[AgentSpecStore, Depends(_get_repo)],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        instances: Annotated[AgentInstanceStore, Depends(_get_instance_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        disable_service: Annotated[AgentDisableService, Depends(_get_agent_disable_service)],
    ) -> JSONResponse:
        """Bind / continue a per-user session for an external-app end-user (M1-5b).

        Mints the end-user (``tenant_user``) from the app's ``user_id`` on first
        use, resolves ``agent_code`` to its latest active version, and creates a
        new conversation thread (or continues ``session_id``). Records the per-user
        ``agent_instance`` binding + an ``on_behalf_of`` audit. The agent
        *definition* is shared; per-user memory / workspace / threads provide
        isolation. (The run itself is M1-5b-2.)
        """
        tenant_id = request.state.tenant_id
        actor_id = request.state.actor_id
        trace_id = current_trace_id_hex()
        try:
            record, thread_id, end_user_id = await _resolve_session(
                tenant_id=tenant_id,
                agent_code=agent_code,
                actor_id=actor_id,
                user_id=payload.user_id,
                session_id=payload.session_id,
                repo=repo,
                threads=threads,
                users=users,
                instances=instances,
                disable_service=disable_service,
            )
        except _SessionError as exc:
            return _envelope_error(exc.code, exc.message, exc.status_code)

        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.SESSION_WRITE,
            resource_type="session",
            resource_id=str(thread_id),
            trace_id=trace_id,
            details={"agent_code": agent_code, "agent_version": record.version},
            on_behalf_of=str(end_user_id),
        )
        return JSONResponse(
            status_code=201,
            content={
                "success": True,
                "data": {
                    "session_id": str(thread_id),
                    "agent_code": agent_code,
                    "agent_version": record.version,
                    "user_id": str(end_user_id),
                },
            },
        )

    @router.post("/{agent_code}/runs", response_model=None)
    async def run_agent_for_user(
        agent_code: str,
        payload: ExternalRunRequest,
        request: Request,
        principal: Annotated[Principal, Depends(require("session", "write"))],
        repo: Annotated[AgentSpecStore, Depends(_get_repo)],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        instances: Annotated[AgentInstanceStore, Depends(_get_instance_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        runtime: Annotated[AgentRuntime, Depends(_get_runtime)],
        approvals: Annotated[ApprovalStore, Depends(_get_approvals)],
        quota: Annotated[QuotaService, Depends(_get_quota)],
        disable_service: Annotated[AgentDisableService, Depends(_get_agent_disable_service)],
        run_store: Annotated[RunStore, Depends(_get_run_store)],
        event_store: Annotated[RunEventStore | None, Depends(_get_run_event_store)],
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
    ) -> StreamingResponse | JSONResponse:
        """Run an agent on behalf of an external-app end-user (M1-5b-2).

        One call: mints the end-user, resolves ``agent_code``, binds / continues a
        session, then spawns the run **scoped to the end-user** — long-term memory,
        the workspace volume, and per-user token cost all key on the minted user, not
        the API-key caller. Returns the SSE stream (``X-Expert-Work-Session-Id`` header) or
        202 for queue mode. The agent definition is shared across the tenant's users;
        per-user isolation is the user-scoped state.

        External-API-v1 P2-a Task 13 / Task 14 — an ``Idempotency-Key`` header
        makes a retried call return the original run instead of spawning a
        duplicate, for BOTH modes. See ``_idempotency.request_digest`` for why
        the fingerprint folds in ``agent_code``, and the block right below for
        the branch (blank/oversized key, cache hit / mismatch, miss). Checked
        before any side effect — no session/thread minted, no admission
        charged — so a rejected call never mutates state. A cache hit renders
        via ``_idempotent_run_response`` — the ``{success, data, error}``
        envelope for queue mode (Task 15), the same SSE replay/live-attach
        ``GET .../runs/{id}/events`` would produce for stream mode (Task 14 —
        Task 13 rejected a stream-mode key with 422
        ``IDEMPOTENCY_NOT_SUPPORTED_FOR_STREAM`` instead; that branch is gone).
        """
        tenant_id = request.state.tenant_id
        actor_id = request.state.actor_id
        trace_id = current_trace_id_hex()

        key: str | None = None
        digest: str | None = None
        if idempotency_key is not None:
            key = idempotency_key.strip()
            if not key or len(key) > MAX_IDEMPOTENCY_KEY_LEN:
                return _envelope_error(
                    "INVALID_IDEMPOTENCY_KEY",
                    f"Idempotency-Key must be 1-{MAX_IDEMPOTENCY_KEY_LEN} non-blank characters",
                    422,
                )
            # External-API-v1 P2-b NUL-byte hardening — ``key`` lands in
            # ``agent_run.idempotency_key`` (a ``Text`` column) verbatim, for
            # BOTH modes (``RunManager.enqueue`` / ``.create``, ``runs.py``).
            # A header — not a pydantic field — so there is no
            # ``field_validator`` to attach ``reject_nul`` to; check it here,
            # same spot as the existing blank/oversize check above.
            try:
                reject_nul(key, field="Idempotency-Key")
            except ValueError as exc:
                return _envelope_error("INVALID_IDEMPOTENCY_KEY", str(exc), 422)
            digest = request_digest(payload, agent_code=agent_code)
            existing = await run_store.find_by_idempotency_key(tenant_id=tenant_id, key=key)
            if existing is not None:
                if existing.request_digest != digest:
                    return _envelope_error(
                        "IDEMPOTENCY_KEY_REUSED",
                        "this Idempotency-Key was already used with a different request",
                        422,
                    )
                # Same key, same fingerprint — hand back the original run
                # untouched rather than spawning a duplicate.
                return _idempotent_run_response(
                    existing,
                    mode=payload.mode,
                    event_store=event_store,
                    stream_bridge=runtime.stream_bridge,
                )

        try:
            record, thread_id, end_user_id = await _resolve_session(
                tenant_id=tenant_id,
                agent_code=agent_code,
                actor_id=actor_id,
                user_id=payload.user_id,
                session_id=payload.session_id,
                repo=repo,
                threads=threads,
                users=users,
                instances=instances,
                disable_service=disable_service,
            )
        except _SessionError as exc:
            return _envelope_error(exc.code, exc.message, exc.status_code)

        # Admission (Stream C.5b) — bucket the run against the agent.
        denial = await check_admission(
            quota=quota,
            audit=audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            agent=agent_code,
            resource_kind="run",
        )
        if denial is not None:
            return denial

        # Build (cache-hit) the agent. The end-user has no OAuth subject of its
        # own (minted, not a Keycloak login), so the OAuth pool keys on its id and
        # resolves empty — the build stays shared.
        try:
            built = await runtime.get_agent(
                tenant_id=tenant_id,
                name=agent_code,
                version=record.version,
                spec=record.spec,
                user_id=str(end_user_id),
            )
        except AgentFactoryError as exc:
            return _envelope_error("AGENT_BUILD_FAILED", f"agent cannot be built: {exc}", 422)

        # P2 块 1 —— files[] 的 image 条目并进 image_refs;_validate_image_refs
        # (spawn_run 内部)对合并后的完整列表做 thread 绑定 / 条数上限 /
        # supports_vision 三重校验,files[] 与既有 image_refs 都过同一道闸。
        image_refs = [
            *payload.image_refs,
            *(f.upload_id for f in payload.files if f.type == "image"),
        ]
        # RunRequest is hand-constructed below (not the FastAPI request body),
        # so a merged list past its own image_refs max_length never reaches
        # the RequestValidationError → 422 path — it would raise an uncaught
        # pydantic ValidationError (500) instead. Pre-check explicitly.
        if len(image_refs) > MAX_RUN_IMAGE_REFS:
            return _envelope_error(
                "TOO_MANY_IMAGE_REFS",
                f"files[] 与 image_refs 合计不能超过 {MAX_RUN_IMAGE_REFS} 张图片",
                422,
            )
        # P2-a 安全修复(Critical)—— 同样是"RunRequest 手工构造绕过了 FastAPI
        # 请求体校验路径"这条根因:内部 ``RunRequest._parse_image_refs``
        # (runs.py)会对每条 ref 调 ``parse_image_ref``,格式不对就 raise
        # ValueError —— 但那是一个 pydantic field_validator,手工构造时它仍
        # 会跑,只是 raise 出来的是裸 ``pydantic.ValidationError``(500),不
        # 会被下方 ``RunRequest(...)`` 调用点前的任何一道闸拦住。合并后的
        # image_refs 同时来自两个入口(顶层 ``image_refs`` 字段 + ``files[]``
        # 里 ``type == "image"`` 的条目,见上面的合并),两个入口都要覆盖 ——
        # 这里在合并之后统一校验,天然覆盖两者。
        for _ref in image_refs:
            try:
                parse_image_ref(_ref)
            except ValueError as exc:
                return _envelope_error("INVALID_IMAGE_REF", str(exc), 422)

        # P2-a 安全修复(Critical)—— 同一根因:内部 ``RunRequest.
        # _bound_untrusted_blocks``(runs.py)对每块查 <= 8192 字符,超了同样
        # 是裸 ``pydantic.ValidationError``(500)。文档站主动推荐这个字段装
        # 一封邮件正文 / 一段工单描述,8KB 是日常量级,第三方按文档使用就会
        # 撞上这个洞,必须在手工构造 ``RunRequest`` 之前拦下来。
        for _idx, _block in enumerate(payload.untrusted_content):
            if len(_block) > MAX_UNTRUSTED_CONTENT_BLOCK_CHARS:
                return _envelope_error(
                    "UNTRUSTED_CONTENT_BLOCK_TOO_LONG",
                    f"untrusted_content[{_idx}] 超过 {MAX_UNTRUSTED_CONTENT_BLOCK_CHARS} 字符",
                    422,
                )

        # P2 块 1(Task 11)—— files[] 的 document 条目逐个过路径净化闸,再并
        # 进 RunRequest.document_names。客户端给的是字符串,上传时走过的
        # _safe_workspace_name 净化只保证了上传路径,run 这一侧必须独立
        # 重新校验(否则 ../ 就能读到工作区外)。_safe_document_name_or_422
        # 抛的是结构化 HTTPException(与 spawn_run 内部沿用的裸 detail 风格
        # 不同),这里就地转译成对外信封,不让裸 {"detail": ...} 逃逸。
        try:
            document_names = [
                _safe_document_name_or_422(f.upload_id)
                for f in payload.files
                if f.type == "document"
            ]
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            return _envelope_error(
                detail.get("code", "INVALID_FILE_REF"),
                detail.get("message", "invalid file reference"),
                exc.status_code,
            )

        # RunRequest is hand-constructed below (not the FastAPI request
        # body), so ``inputs`` past ``RunRequest._bound_inputs``'s own
        # bounds never reaches the RequestValidationError → 422 path — it
        # would raise an uncaught pydantic ValidationError (500) instead.
        # Pre-check explicitly, same pattern as the ``image_refs`` check
        # above. ``validate_prompt_inputs`` (called inside ``spawn_run``)
        # covers unknown/missing-required keys but not these two bounds.
        if len(payload.inputs) > MAX_RUN_INPUT_KEYS:
            return _envelope_error(
                "TOO_MANY_INPUT_KEYS",
                f"inputs 最多 {MAX_RUN_INPUT_KEYS} 个键",
                422,
            )
        for _input_key, _input_val in payload.inputs.items():
            if isinstance(_input_val, str) and len(_input_val) > MAX_RUN_INPUT_VALUE_CHARS:
                return _envelope_error(
                    "INPUT_VALUE_TOO_LONG",
                    f"inputs['{_input_key}'] 超过 {MAX_RUN_INPUT_VALUE_CHARS} 字符",
                    422,
                )
        # P2-a 安全修复(Important)—— 上面这条单值长度检查只认 ``str``;把同一
        # 个超大值包一层 list/dict 就绕过(``{"lang": ["A"*1200000]}``),两条
        # 既有检查都不查。追加一道**序列化后总字节数**的界,堵住"值不是 str
        # 就不查长度"这个洞;既有两条(键数 / 单值 str 长度)原样保留,新界是
        # 追加不是替代——单值 8192 的界仍然有意义(限制单个变量),这条新界
        # 限制的是整个 inputs 的总量。上限复用 ``MAX_RUN_INPUT_TOTAL_BYTES``
        # (= ``MAX_RUN_INPUT_CHARS``,即 input 自由文本字段的 64KB 上限,见
        # runs.py 该常量的文档字符串)。
        _inputs_total_bytes = len(
            json.dumps(payload.inputs, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if _inputs_total_bytes > MAX_RUN_INPUT_TOTAL_BYTES:
            return _envelope_error(
                "TOO_MANY_INPUT_BYTES",
                f"inputs 序列化后总大小不能超过 {MAX_RUN_INPUT_TOTAL_BYTES} 字节",
                422,
            )

        run_payload = RunRequest(
            input=payload.input,
            mode=payload.mode,
            image_refs=image_refs,
            untrusted_content=payload.untrusted_content,
            inputs=payload.inputs,
            document_names=document_names,
        )
        try:
            return await spawn_run(
                runtime=runtime,
                audit=audit,
                approvals=approvals,
                request=request,
                settings=request.app.state.settings,
                built=built,
                record_spec=record.spec,
                thread_id=thread_id,
                tenant_id=tenant_id,
                actor_id=actor_id,
                effective_user_id=end_user_id,
                oauth_subject=str(end_user_id),
                payload=run_payload,
                trace_id=trace_id,
                extra_headers={"X-Expert-Work-Session-Id": str(thread_id)},
                on_behalf_of=str(end_user_id),
                idempotency_key=key,
                request_digest=digest,
                envelope=True,
            )
        except RunIdempotencyConflict:
            # P2-a Task 13 (queue) / Task 14 (stream) —— concurrent single
            # winner. Both requests missed each other in the
            # ``find_by_idempotency_key`` check above (race window between
            # that read and this create); the partial unique index on
            # ``agent_run`` let exactly one insert through and raised this
            # for the loser. Re-query and hand the loser's caller the
            # winner's response instead of erroring or double-creating.
            if key is None:  # pragma: no cover - unreachable: spawn_run only
                # raises this when it was itself called with a non-None key.
                raise
            winner = await run_store.find_by_idempotency_key(tenant_id=tenant_id, key=key)
            if winner is None:  # pragma: no cover - the index guarantees a
                # winner row exists the instant the conflict fires.
                raise
            # P2-a security fix (Critical) —— this requery used to hand the
            # winner's run straight back with no digest check at all, unlike
            # the pre-``spawn_run`` cache-hit branch above (which 422s on a
            # digest mismatch). Any same-tenant ``session:write`` key holder
            # could win an information leak by racing a guessable
            # Idempotency-Key against a victim's request: land inside the
            # window between the victim's ``find_by_idempotency_key`` miss
            # and its ``run_store.create`` insert (that window spans
            # ``_resolve_session`` + ``check_admission`` +
            # ``runtime.get_agent`` — tens to hundreds of ms, not
            # microseconds), lose the race, and this branch would hand back
            # the *victim's* ``run_id`` / ``thread_id`` (queue mode) or the
            # victim run's full SSE replay — including any secret content
            # already in its event stream (stream mode). Same fix, same
            # shape, as the cache-hit branch: a digest mismatch means this
            # key was reused for a genuinely different request, not a retry
            # of the request that's racing right now, so it must 422 rather
            # than disclose the winner's run. A same-digest loser (the
            # legitimate concurrent-retry case this branch exists for) still
            # gets the winner's run — that's the value idempotency exists to
            # provide, and this check must not break it.
            if winner.request_digest != digest:
                return _envelope_error(
                    "IDEMPOTENCY_KEY_REUSED",
                    "this Idempotency-Key was already used with a different request",
                    422,
                )
            return _idempotent_run_response(
                winner,
                mode=payload.mode,
                event_store=event_store,
                stream_bridge=runtime.stream_bridge,
            )

    @router.get("", dependencies=_CONSOLE_ONLY)
    async def list_agents(
        request: Request,
        repo: Annotated[AgentSpecStore, Depends(_get_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        status: AgentSpecStatus | None = None,
        name: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,  # Stream N
    ) -> JSONResponse:
        # Stream N — resolve ``?tenant_id=`` against the caller's scope.
        # ``"*"`` requires ``is_system_admin``; non-home UUID requires
        # ``allowed_tenants`` membership. See control_plane.tenant_scope.
        scope = await ensure_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/agents",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            if isinstance(scope, CrossTenant):
                items = await repo.list_all_tenants(
                    status=status, name=name, limit=limit, offset=offset
                )
            else:
                items = await repo.list_by_tenant(
                    tenant_id=scope.tenant_id,
                    status=status,
                    name=name,
                    limit=limit,
                    offset=offset,
                )
        # Manifest-read audit — recorded under the actual queried tenant for
        # SingleTenant; under principal's home for CrossTenant (the cross-tenant
        # audit was already emitted by ensure_tenant_scope).
        audit_tenant = (
            request.state.principal.tenant_id if isinstance(scope, CrossTenant) else scope.tenant_id
        )
        await emit(
            audit,
            tenant_id=audit_tenant,
            actor_id=request.state.actor_id,
            action=AuditAction.MANIFEST_READ,
            resource_type="manifest",
            trace_id=current_trace_id_hex(),
            details={"count": len(items)},
        )
        payload = AgentList(
            items=items, total=len(items), cross_tenant=isinstance(scope, CrossTenant)
        )
        return JSONResponse({"success": True, "data": payload.model_dump(mode="json")})

    @router.get("/{name}/{version}")
    async def get_agent(
        name: str,
        version: str,
        request: Request,
        repo: Annotated[AgentSpecStore, Depends(_get_repo)],
        disable_repo: Annotated[AgentDisableStore, Depends(_get_agent_disable_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W2 read scope — a concrete id lets a system_admin drill into a
        # foreign tenant's agent from the tenant switcher; "*" is meaningless
        # (an agent row belongs to one tenant).
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/agents/{name}/{version}",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        target_tenant = scope.tenant_id
        async with applied_scope(scope):
            record = await repo.get(tenant_id=target_tenant, name=name, version=version)
        if record is None:
            raise HTTPException(status_code=404, detail="agent not found")
        # Stream 8.5 — instance-level RBAC + ABAC (conditioned bindings may
        # restrict a member to specific agents by id / label / ownership).
        # Deliberately OUTSIDE applied_scope: it reads the CALLER's bindings.
        await ensure_resource_access(
            request, resource="manifest", action="read", attrs=_record_attrs(record)
        )
        await emit(
            audit,
            tenant_id=target_tenant,
            actor_id=request.state.actor_id,
            action=AuditAction.MANIFEST_READ,
            resource_type="manifest",
            resource_id=f"{name}/{version}",
            trace_id=current_trace_id_hex(),
        )
        # Stream RT-4 (RT-ADR-16) — surface the agent-level kill-switch state so
        # the detail page can render the status tag + disable/enable control. The
        # flag is per ``name`` (all versions); read it straight from the store
        # (not the hot-path TTL cache) so the UI always sees the latest write.
        data = AgentDetail(record=record).model_dump(mode="json")
        async with applied_scope(scope):
            disable_row = await disable_repo.get(tenant_id=target_tenant, agent_name=name)
        if disable_row is not None and disable_row.disabled:
            data["disabled"] = True
            data["disable"] = disable_row.model_dump(mode="json")
        else:
            data["disabled"] = False
            data["disable"] = None
        return JSONResponse({"success": True, "data": data})

    @router.put("/{name}/{version}")
    async def update_agent(
        name: str,
        version: str,
        payload: ManifestPayload,
        request: Request,
        repo: Annotated[AgentSpecStore, Depends(_get_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        loader: Annotated[ManifestLoader, Depends(_get_loader)],
    ) -> JSONResponse:
        tenant_id = request.state.tenant_id
        actor_id = request.state.actor_id
        trace_id = current_trace_id_hex()
        try:
            spec, sha = await _load_manifest(payload, loader)
        except ManifestError as exc:
            return _manifest_error_to_response(exc)

        if spec.metadata.name != name or spec.metadata.version != version:
            return _envelope_error(
                "MANIFEST_PATH_MISMATCH",
                f"path is {name}/{version} but manifest metadata is "
                f"{spec.metadata.name}/{spec.metadata.version}",
                422,
            )

        # Stream 8.5 — authorize against the EXISTING instance's attributes
        # (owner / labels) before mutating it. 404 stays 404 for unknown names.
        existing = await repo.get(tenant_id=tenant_id, name=name, version=version)
        if existing is None:
            raise HTTPException(status_code=404, detail="agent not found")
        await ensure_resource_access(
            request, resource="manifest", action="write", attrs=_record_attrs(existing)
        )

        result = await repo.update_spec(
            tenant_id=tenant_id,
            name=name,
            version=version,
            spec=spec,
            spec_sha256=sha,
            updated_by=actor_id,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="agent not found")
        # The edited spec must reach new runs without a restart.
        _invalidate_agent_build_cache(request, tenant_id)
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.MANIFEST_WRITE,
            resource_type="manifest",
            resource_id=f"{name}/{version}",
            trace_id=trace_id,
            # Stream HX-5 -- before/after pair + the history row this
            # write appended (null = same-sha no-op, nothing recorded).
            details={
                "spec_sha256": sha,
                "prev_sha256": result.prev_sha256,
                "revision": result.revision,
            },
        )
        return JSONResponse(
            {"success": True, "data": AgentDetail(record=result.record).model_dump(mode="json")}
        )

    @router.get("/{name}/{version}/revisions", dependencies=_CONSOLE_ONLY)
    async def list_revisions(
        name: str,
        version: str,
        request: Request,
        repo: Annotated[AgentSpecStore, Depends(_get_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        limit: int = 50,
        offset: int = 0,
        # W2 read scope — see ``get_agent``.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        """Stream HX-5 — revision history, newest first (summaries only)."""
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/agents/{name}/{version}/revisions",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        target_tenant = scope.tenant_id
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        async with applied_scope(scope):
            # 404 for an unknown manifest, [] for a known one with a short
            # history window — the UI distinguishes the two.
            record = await repo.get(tenant_id=target_tenant, name=name, version=version)
            if record is None:
                raise HTTPException(status_code=404, detail="agent not found")
            revisions = await repo.list_revisions(
                tenant_id=target_tenant, name=name, version=version, limit=limit, offset=offset
            )
        items = [
            RevisionSummary(
                revision=r.revision,
                spec_sha256=r.spec_sha256,
                actor_id=r.actor_id,
                created_at=r.created_at.isoformat(),
            )
            for r in revisions
        ]
        return JSONResponse(
            {"success": True, "data": RevisionList(items=items).model_dump(mode="json")}
        )

    @router.get("/{name}/{version}/revisions/{revision}", dependencies=_CONSOLE_ONLY)
    async def get_revision(
        name: str,
        version: str,
        revision: int,
        request: Request,
        repo: Annotated[AgentSpecStore, Depends(_get_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W2 read scope — see ``get_agent``.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        """Stream HX-5 — one full revision snapshot (the diff view's input)."""
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/agents/{name}/{version}/revisions/{revision}",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            snapshot = await repo.get_revision(
                tenant_id=scope.tenant_id, name=name, version=version, revision=revision
            )
        if snapshot is None:
            raise HTTPException(status_code=404, detail="revision not found")
        return JSONResponse(
            {"success": True, "data": RevisionDetail(record=snapshot).model_dump(mode="json")}
        )

    # ``console_only()`` first, so an **under-scoped** API key (zero-scope or
    # ``read``-scope — anything that fails the ``manifest:write`` role check)
    # keeps getting the console-plane pointer message rather than a role
    # denial. An ``admin``- or ``write``-scope key passes the role check
    # either way, so ordering makes no difference to what it sees (both
    # dependencies deny it, same message) — only an under-scoped key is
    # affected. ``require(...)`` second — a route-level role gate, per the
    # 2026-08-12 user ruling: of the five console routes closed to API keys in
    # C2, only this one is a **write**, and it had no employee-side RBAC at all,
    # so any logged-in VIEWER could roll a tenant's manifest back to an
    # arbitrary older snapshot. The three reads in the same group stay open to
    # every employee on purpose (not blocking existing habits); a systematic
    # employee-role sweep is a separate round. ``manifest:write`` is the same
    # ``(resource, action)`` PUT authorizes with — this IS the PUT write path,
    # appending a revision — except PUT uses the instance-level
    # ``ensure_resource_access`` after loading the record, so a caller whose
    # only grant is a *conditioned* binding can PUT but not roll back. That
    # narrowing is deliberate for now: the ruling asked for a role gate, and a
    # conditioned-binding holder can still reach the same end state via PUT.
    @router.post(
        "/{name}/{version}/revisions/{revision}/rollback",
        dependencies=[*_CONSOLE_ONLY, Depends(require("manifest", "write"))],
    )
    async def rollback_to_revision(
        name: str,
        version: str,
        revision: int,
        request: Request,
        repo: Annotated[AgentSpecStore, Depends(_get_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        """Stream HX-5 (Mini-ADR HX-E2) — roll the manifest back to an
        older snapshot by *appending* a new revision with its content.

        Same write path as PUT (``update_spec``): the snapshot was
        schema-validated at write time and re-validates on read; a
        rollback to the current content is a recorded no-op.
        """
        tenant_id = request.state.tenant_id
        actor_id = request.state.actor_id
        trace_id = current_trace_id_hex()
        snapshot = await repo.get_revision(
            tenant_id=tenant_id, name=name, version=version, revision=revision
        )
        if snapshot is None:
            raise HTTPException(status_code=404, detail="revision not found")
        result = await repo.update_spec(
            tenant_id=tenant_id,
            name=name,
            version=version,
            spec=snapshot.spec,
            spec_sha256=snapshot.spec_sha256,
            updated_by=actor_id,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="agent not found")
        # Rolled-back content must reach new runs without a restart.
        _invalidate_agent_build_cache(request, tenant_id)
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.MANIFEST_WRITE,
            resource_type="manifest",
            resource_id=f"{name}/{version}",
            trace_id=trace_id,
            details={
                "spec_sha256": snapshot.spec_sha256,
                "prev_sha256": result.prev_sha256,
                "revision": result.revision,
                "rolled_back_to": revision,
            },
        )
        return JSONResponse(
            {
                "success": True,
                "data": {
                    "record": AgentDetail(record=result.record).model_dump(mode="json")["record"],
                    "revision": result.revision,
                    "rolled_back_to": revision,
                },
            }
        )

    @router.delete("/{name}/{version}", status_code=204)
    async def delete_agent(
        name: str,
        version: str,
        request: Request,
        repo: Annotated[AgentSpecStore, Depends(_get_repo)],
        run_store: Annotated[RunStore, Depends(_get_run_store)],
        runtime: Annotated[AgentRuntime, Depends(_get_runtime)],
        triggers: Annotated[TriggerStore, Depends(_get_trigger_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id = request.state.tenant_id
        actor_id = request.state.actor_id
        trace_id = current_trace_id_hex()
        # Stream 8.5 — authorize against the existing instance before deleting.
        existing = await repo.get(tenant_id=tenant_id, name=name, version=version)
        if existing is None:
            raise HTTPException(status_code=404, detail="agent not found")
        await ensure_resource_access(
            request, resource="manifest", action="delete", attrs=_record_attrs(existing)
        )
        record = await repo.update_status(
            tenant_id=tenant_id,
            name=name,
            version=version,
            status=AgentSpecStatus.DELETED,
        )
        if record is None:
            raise HTTPException(status_code=404, detail="agent not found")
        # Drop the deleted build so a re-register at the same version rebuilds.
        _invalidate_agent_build_cache(request, tenant_id)

        # Deletion hygiene PR4 — cascade, best-effort with audit-visible
        # failures. Cancel the agent's in-flight runs (same RT-ADR-17 loop as
        # ``disable_agent``, but narrowed to this version via the session's
        # ``thread_meta.agent_version`` — deleting v1 must not kill a live v2
        # session), then disable this version's triggers so they stop firing
        # against a deleted agent.
        details: dict[str, object] = {}
        cancelled = 0
        try:
            running = await run_store.list_running_for_agent(
                tenant_id=tenant_id, agent_name=name, agent_version=version
            )
            now = datetime.now(UTC)
            for run in running:
                stopped = await runtime.run_manager.cancel(
                    run.run_id
                ) or await run_store.request_cancel(
                    run_id=run.run_id, tenant_id=tenant_id, updated_at=now
                )
                if stopped:
                    cancelled += 1
                    await emit(
                        audit,
                        tenant_id=tenant_id,
                        actor_id=actor_id,
                        action=AuditAction.SESSION_CANCEL,
                        resource_type="run",
                        resource_id=str(run.run_id),
                        trace_id=trace_id,
                        reason="agent_deleted",
                    )
        except Exception:
            logger.warning("agent_delete.runs_cancel_failed", exc_info=True)
            details["runs_cancel_failed"] = True
        details["runs_cancelled"] = cancelled

        disabled = 0
        try:
            disabled = await triggers.disable_for_agent(
                agent_name=name, agent_version=version, tenant_id=tenant_id
            )
        except Exception:
            logger.warning("agent_delete.triggers_disable_failed", exc_info=True)
            details["triggers_disable_failed"] = True
        details["triggers_disabled"] = disabled

        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.MANIFEST_DELETE,
            resource_type="manifest",
            resource_id=f"{name}/{version}",
            trace_id=trace_id,
            details=details,
        )
        return JSONResponse(status_code=204, content=None)

    async def _agent_exists(repo: AgentSpecStore, tenant_id: UUID, name: str) -> bool:
        """``True`` iff the tenant has any version of ``name`` (any lifecycle status)."""
        rows = await repo.list_by_tenant(tenant_id=tenant_id, name=name, limit=1)
        return bool(rows)

    @router.post("/{name}/disable")
    async def disable_agent(
        name: str,
        payload: AgentDisableRequest,
        request: Request,
        principal: Annotated[Principal, Depends(require("manifest", "write"))],
        repo: Annotated[AgentSpecStore, Depends(_get_repo)],
        disable_repo: Annotated[AgentDisableStore, Depends(_get_agent_disable_repo)],
        disable_service: Annotated[AgentDisableService, Depends(_get_agent_disable_service)],
        run_store: Annotated[RunStore, Depends(_get_run_store)],
        runtime: Annotated[AgentRuntime, Depends(_get_runtime)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        """Stream RT-4 (RT-ADR-16) — engage the agent kill switch.

        Sets the ``agent_disable`` flag (covers all versions of ``name``),
        invalidates the TTL cache for immediate effect, then bulk-cancels the
        agent's in-flight runs (RT-ADR-17: enumerate RUNNING runs → per-run
        ``RunManager.cancel`` → the abort_event chain terminates them in
        seconds). New runs / sessions are rejected by the ``_resolve_session``
        and admission gates; queued runs are refused by the run-queue worker.
        Reversible via :func:`enable_agent`.
        """
        tenant_id = request.state.tenant_id
        actor_id = request.state.actor_id
        trace_id = current_trace_id_hex()
        if not await _agent_exists(repo, tenant_id, name):
            raise HTTPException(status_code=404, detail="agent not found")

        await disable_repo.set_disabled(
            tenant_id=tenant_id,
            agent_name=name,
            disabled=True,
            reason=payload.reason,
            disabled_by=actor_id,
        )
        # Immediate effect on this instance; peers pick it up within the TTL.
        disable_service.invalidate(tenant_id, name)

        # RT-ADR-17 — bulk-cancel the agent's in-flight runs. A run THIS instance
        # owns aborts immediately (abort_event); a run held by another instance is
        # guarded-cancelled in the store (running/pending → interrupted), so that
        # worker's next lease heartbeat CAS fails and it stops within one heartbeat
        # interval. ``request_cancel`` never clobbers a run that just finished.
        running = await run_store.list_running_for_agent(tenant_id=tenant_id, agent_name=name)
        cancelled = 0
        now = datetime.now(UTC)
        for run in running:
            stopped = await runtime.run_manager.cancel(
                run.run_id
            ) or await run_store.request_cancel(
                run_id=run.run_id, tenant_id=tenant_id, updated_at=now
            )
            if stopped:
                cancelled += 1
                await emit(
                    audit,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    action=AuditAction.SESSION_CANCEL,
                    resource_type="run",
                    resource_id=str(run.run_id),
                    trace_id=trace_id,
                    reason="agent_disabled",
                )

        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.AGENT_DISABLED,
            resource_type="agent",
            resource_id=name,
            trace_id=trace_id,
            details={"reason": payload.reason, "cancelled_runs": cancelled},
        )
        return JSONResponse(
            {
                "success": True,
                "data": {"name": name, "disabled": True, "cancelled_runs": cancelled},
                "error": None,
            }
        )

    @router.post("/{name}/enable")
    async def enable_agent(
        name: str,
        payload: AgentDisableRequest,
        request: Request,
        principal: Annotated[Principal, Depends(require("manifest", "write"))],
        repo: Annotated[AgentSpecStore, Depends(_get_repo)],
        disable_repo: Annotated[AgentDisableStore, Depends(_get_agent_disable_repo)],
        disable_service: Annotated[AgentDisableService, Depends(_get_agent_disable_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        """Stream RT-4 (RT-ADR-16) — release the agent kill switch.

        Clears the ``agent_disable`` flag (and its stored reason) and
        invalidates the cache. New runs / sessions resume immediately; nothing
        auto-restarts the runs the disable cancelled."""
        tenant_id = request.state.tenant_id
        actor_id = request.state.actor_id
        trace_id = current_trace_id_hex()
        if not await _agent_exists(repo, tenant_id, name):
            raise HTTPException(status_code=404, detail="agent not found")

        await disable_repo.set_disabled(
            tenant_id=tenant_id,
            agent_name=name,
            disabled=False,
            reason=payload.reason,
            disabled_by=actor_id,
        )
        disable_service.invalidate(tenant_id, name)
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.AGENT_ENABLED,
            resource_type="agent",
            resource_id=name,
            trace_id=trace_id,
            details={"reason": payload.reason},
        )
        return JSONResponse(
            {
                "success": True,
                "data": {"name": name, "disabled": False},
                "error": None,
            }
        )

    return router
