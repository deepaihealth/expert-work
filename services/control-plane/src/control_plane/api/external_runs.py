"""External run control for third-party apps — ``/v1/agents/{agent_code}/runs/...``.

Run-level cancel plus P-1's ``:regenerate`` / ``:edit`` live here. Session-level
cancel (``POST /v1/sessions/{id}:cancel``) is an irreversible close — it flips
the thread to CANCELLED so every later run is refused — and stays a console-only
operation. An end user's "stop" button wants the cancel endpoint here: it aborts
the current execution and leaves the conversation usable.

``:regenerate`` / ``:edit``(P-1)—— 对会话的**最后一轮**重来一次:旧轮原地标
「已被取代」(读面仍可见、agent 的上下文里看不见、计划回退到轮前),新一轮照常
跑。两条路由共用 :func:`_supersede_and_run`,只差输入从哪来:``:regenerate``
用旧轮的原件重放,``:edit`` 用调用方给的新 ``input``。旧轮不删、副作用不撤销、
两轮都计费(spec §6)。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from control_plane.api._authz import external_only, require
from control_plane.api._external import (
    ExternalScopeError,
    external_error,
    load_owned_run,
    load_owned_session,
    lookup_external_user_id,
    reject_nul,
    reject_nul_deep,
    reject_nul_path_params,
)
from control_plane.api._idempotency import (
    IDEMPOTENCY_HEADER,
    MAX_IDEMPOTENCY_KEY_LEN,
    request_digest,
)
from control_plane.api._quota_admission import check_admission
from control_plane.api._run_event_stream import EXTERNAL_HIDDEN_EVENTS
from control_plane.api._run_usage import make_usage_loader
from control_plane.api._user_scope import get_user_repo
from control_plane.api.agents import (
    ExternalFileRef,
    _envelope_error,
    _idempotent_run_response,
    external_run_bounds_error,
    resolve_external_files,
)
from control_plane.api.runs import MAX_RUN_INPUT_CHARS, RunRequest, SupersedeRequest, spawn_run
from control_plane.run_cancel import cancel_run_two_level
from control_plane.supersede import SupersedeError
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.persistence.token_usage_store import TokenUsageStore
from expert_work.protocol import AgentSpecStatus, Principal
from expert_work.runtime.runs import DisconnectMode, InterruptReason, RunStatus, RunStore
from expert_work.runtime.runs.schemas import TERMINAL_RUN_STATUSES
from expert_work.runtime.runs.store import RunIdempotencyConflict
from orchestrator import AgentFactoryError
from orchestrator.stream_items import STREAM_FORMAT_ITEMS, STREAM_FORMAT_LEGACY


class ExternalCancelRequest(BaseModel):
    """Body for ``POST /v1/agents/{agent_code}/runs/{run_id}:cancel``.

    ``user_id`` is the app's own end-user identifier and is verified against the
    run's session — an app cannot cancel another end user's run.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1, max_length=255)


class ExternalRegenerateRequest(BaseModel):
    """Body for ``POST /v1/agents/{agent_code}/runs/{run_id}:regenerate`` —— 同一输入
    再跑一次。

    ``input`` / ``files`` / ``inputs`` **一个都不接受**(``extra="forbid"``):这个端点
    的输入就是被取代那一轮的原件,含它当时的附件引用;要改输入用 ``:edit``。塞一个
    ``input`` 进来会是 422,而不是悄悄被忽略。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1, max_length=255)
    mode: Literal["stream", "queue"] = "stream"
    stream_format: Literal[STREAM_FORMAT_LEGACY, STREAM_FORMAT_ITEMS] = STREAM_FORMAT_LEGACY


class ExternalEditRequest(BaseModel):
    """Body for ``…:edit`` —— 改输入后再跑一次。

    每个字段的语义与 ``POST …/runs``(``agents.ExternalRunRequest``)的同名字段完全
    一致,只有 ``input`` 从选填变必填 —— 「编辑重发」没有新输入就没有意义,漏传是
    422,不是回退成 ``:regenerate``。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1, max_length=255)
    input: str = Field(min_length=1, max_length=MAX_RUN_INPUT_CHARS)
    mode: Literal["stream", "queue"] = "stream"
    stream_format: Literal[STREAM_FORMAT_LEGACY, STREAM_FORMAT_ITEMS] = STREAM_FORMAT_LEGACY
    untrusted_content: list[str] = Field(default_factory=list, max_length=16)
    inputs: dict[str, Any] = Field(default_factory=dict)
    files: list[ExternalFileRef] = Field(default_factory=list, max_length=64)

    # NUL 加固与 ``ExternalRunRequest`` 同一条路:这三个字段原样进
    # ``agent_run.enqueued_input``(jsonb),一个 NUL 就是裸 asyncpg
    # ``CharacterNotInRepertoireError`` → 500。``user_id`` 在
    # ``external_subject_id`` 里已经守过一次,不重复。
    @field_validator("input")
    @classmethod
    def _no_nul_input(cls, value: str) -> str:
        return reject_nul(value, field="input")

    @field_validator("untrusted_content")
    @classmethod
    def _no_nul_untrusted_content(cls, value: list[str]) -> list[str]:
        return reject_nul_deep(value, field="untrusted_content")  # type: ignore[no-any-return]

    @field_validator("inputs")
    @classmethod
    def _no_nul_inputs(cls, value: dict[str, Any]) -> dict[str, Any]:
        return reject_nul_deep(value, field="inputs")  # type: ignore[no-any-return]


def _get_thread_repo(request: Request) -> ThreadMetaStore:
    return request.app.state.thread_meta_repo  # type: ignore[no-any-return]


def _get_run_store(request: Request) -> RunStore:
    return request.app.state.run_store  # type: ignore[no-any-return]


def _get_token_usage_store(request: Request) -> TokenUsageStore:
    return request.app.state.token_usage_store  # type: ignore[no-any-return]


async def _supersede_and_run(
    *,
    op: Literal["regenerate", "edit"],
    agent_code: str,
    run_id: UUID,
    payload: ExternalRegenerateRequest | ExternalEditRequest,
    request: Request,
    runs: RunStore,
    threads: ThreadMetaStore,
    users: TenantUserStore,
    idempotency_key: str | None,
) -> StreamingResponse | JSONResponse:
    """``:regenerate`` / ``:edit`` 共用的主体。

    顺序照 ``agents.run_agent_for_user``:幂等命中在任何副作用之前返回 → 归属校验
    (404 不泄露存在性)→ agent 停用 / 有没有 active 版本 → 配额 → 构建 →
    (``:edit`` 才有的附件分流与上限预检)→ ``spawn_run(supersede=…)``。

    取代本身在 ``spawn_run`` 的 per-thread 锁里由 ``supersede_run`` 完成,它抛的
    :class:`SupersedeError`(``THREAD_BUSY`` / ``RUN_NOT_LAST`` / ``RUN_AWAITING_APPROVAL``
    / ``RUN_ALREADY_SUPERSEDED`` / ``RUN_INPUT_UNAVAILABLE`` / ``RUN_BOUNDARY_UNRESOLVED``)
    在这里渲染成对外信封,code 与 HTTP 码原样搬过去。

    ``on_disconnect=CONTINUE`` 与 ``POST …/runs`` 一致:断线在对外场景是意外而不是
    「我不要了」,而这一轮的旧轮已经标成被取代,取消会把一次网络抖动放大成整段
    会话没有活着的最后一轮。
    """
    state = request.app.state
    tenant_id: UUID = request.state.tenant_id
    actor_id: str = request.state.actor_id
    trace_id = current_trace_id_hex()
    runtime = state.agent_runtime
    mode = payload.mode
    stream_format = payload.stream_format

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
        try:
            reject_nul(key, field="Idempotency-Key")
        except ValueError as exc:
            return _envelope_error("INVALID_IDEMPOTENCY_KEY", str(exc), 422)
        # 指纹里折进 ``run_id`` 与操作名:同一个 key 对同一 agent 的不同目标轮、
        # 或对同一轮的不同操作,都是**不同的请求**,必须是 IDEMPOTENCY_KEY_REUSED
        # 而不是幂等命中 —— 与 ``request_digest`` 自己折进 agent_code 同理。分隔符
        # 用 NUL,它永远不可能出现在 agent_code / uuid / 操作名里面。
        #
        # 两个成分的可观测性不一样,别把它们当成同一件事:``run_id`` 是**独立
        # 生效**的(变异去掉它,``test_idempotency_key_is_scoped_to_the_target_run``
        # 立刻红);``op`` 今天**测不出来** —— ``:regenerate`` 的请求体
        # (``extra="forbid"``、无 ``input``)与 ``:edit`` 的(``input`` 必填)
        # 结构上不可能 dump 成同一份 JSON,body 自己就已经把两个操作分开了。
        # 留着它是因为它是「这是哪个请求」的正确定义:哪天 ``:edit`` 的
        # ``input`` 变成选填、或者多出第三个操作,少了它就会把两个操作合并到
        # 同一个 key 上,而那时没有任何测试会红。
        digest = request_digest(payload, agent_code=f"{agent_code}\x00{run_id}\x00{op}")
        existing = await runs.find_by_idempotency_key(tenant_id=tenant_id, key=key)
        if existing is not None:
            if existing.request_digest != digest:
                return _envelope_error(
                    "IDEMPOTENCY_KEY_REUSED",
                    "this Idempotency-Key was already used with a different request",
                    422,
                )
            return await _idempotent_run_response(
                existing,
                mode=mode,
                event_store=getattr(state, "run_event_store", None),
                stream_bridge=runtime.stream_bridge,
                run_store=runs,
                usage_store=state.token_usage_store,
                tenant_id=tenant_id,
                stream_format=stream_format,
            )

    try:
        target, meta = await load_owned_run(
            tenant_id=tenant_id,
            agent_code=agent_code,
            user_id=payload.user_id,
            run_id=run_id,
            runs=runs,
            threads=threads,
            users=users,
        )
    except ExternalScopeError as exc:
        return external_error(exc)
    # ``meta.user_id`` 声明成 ``UUID | None``,但走到这里必然非空:
    # ``load_owned_session(mint=False)`` 先把 ``user_id`` 查成一个真实的
    # ``tenant_user.id``(查不到就 404),再要求 ``meta.user_id`` 与它相等。所以这里
    # 是 ``cast`` 而不是运行时判空 —— 判空会是一条永远走不到的死分支
    # (``external_approvals`` 那边同样直接用 ``meta.user_id``,不判)。
    end_user_id = cast(UUID, meta.user_id)

    if await state.agent_disable_service.is_disabled(tenant_id, agent_code):
        return _envelope_error("AGENT_DISABLED", f"agent {agent_code!r} is disabled", 403)
    active = await state.agent_spec_repo.list_by_tenant(
        tenant_id=tenant_id, status=AgentSpecStatus.ACTIVE, name=agent_code, limit=1
    )
    if not active:
        return _envelope_error(
            "AGENT_NOT_FOUND", f"no active agent {agent_code!r} for this tenant", 404
        )
    record = active[0]

    denial = await check_admission(
        quota=state.quota_service,
        audit=state.audit_logger,
        tenant_id=tenant_id,
        actor_id=actor_id,
        agent=agent_code,
        resource_kind="run",
    )
    if denial is not None:
        return denial
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

    if isinstance(payload, ExternalEditRequest):
        try:
            image_refs, document_names = await resolve_external_files(
                files=payload.files,
                tenant_id=tenant_id,
                end_user_id=end_user_id,
                thread_id=meta.thread_id,
                uploads_store=state.user_upload_store,
            )
        except ExternalScopeError as exc:
            return external_error(exc)
        except HTTPException as exc:
            detail: Mapping[str, Any] = exc.detail if isinstance(exc.detail, dict) else {}
            return _envelope_error(
                detail.get("code", "INVALID_FILE_REF"),
                detail.get("message", "invalid file reference"),
                exc.status_code,
            )
        bounds = external_run_bounds_error(
            untrusted_content=payload.untrusted_content, inputs=payload.inputs
        )
        if bounds is not None:
            return bounds
        run_payload = RunRequest(
            input=payload.input,
            mode=mode,
            image_refs=image_refs,
            untrusted_content=payload.untrusted_content,
            inputs=payload.inputs,
            document_names=document_names,
        )
    else:
        # ``:regenerate`` —— 图输入由 ``spawn_run`` 从 ``supersede_run`` 交回的
        # ``replay_messages`` 拼(``replay_graph_input``),这里的 ``RunRequest``
        # 只承载 ``mode``。
        run_payload = RunRequest(input=None, mode=mode)

    try:
        return await spawn_run(
            runtime=runtime,
            audit=state.audit_logger,
            approvals=state.approval_store,
            request=request,
            settings=state.settings,
            built=built,
            record_spec=record.spec,
            thread_id=meta.thread_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            effective_user_id=end_user_id,
            oauth_subject=str(end_user_id),
            payload=run_payload,
            trace_id=trace_id,
            extra_headers={"X-Expert-Work-Session-Id": str(meta.thread_id)},
            on_behalf_of=str(end_user_id),
            idempotency_key=key,
            request_digest=digest,
            envelope=True,
            hide_events=EXTERNAL_HIDDEN_EVENTS,
            stream_format=stream_format,
            on_disconnect=DisconnectMode.CONTINUE,
            supersede=SupersedeRequest(target_run_id=target.run_id, replay=(op == "regenerate")),
        )
    except SupersedeError as exc:
        return _envelope_error(exc.code, exc.message, exc.status_code)
    except RunIdempotencyConflict:
        # 并发同 key 的败者:赢者那一条已经 INSERT 成功,把它的响应交回去。数字摘要
        # 不一致说明这个 key 被拿去发了另一个请求,那是 422 而不是重放(与
        # ``run_agent_for_user`` 里同一处的安全修复同形)。
        if key is None:  # pragma: no cover - spawn_run 只在带 key 时抛这个
            raise
        winner = await runs.find_by_idempotency_key(tenant_id=tenant_id, key=key)
        if winner is None:  # pragma: no cover - 冲突触发的那一刻赢者行必然存在
            raise
        if winner.request_digest != digest:
            return _envelope_error(
                "IDEMPOTENCY_KEY_REUSED",
                "this Idempotency-Key was already used with a different request",
                422,
            )
        return await _idempotent_run_response(
            winner,
            mode=mode,
            event_store=getattr(state, "run_event_store", None),
            stream_bridge=runtime.stream_bridge,
            run_store=runs,
            usage_store=state.token_usage_store,
            tenant_id=tenant_id,
            stream_format=stream_format,
        )


def build_external_runs_router() -> APIRouter:
    """Mount the external run-control endpoints."""
    router = APIRouter(
        prefix="/v1/agents",
        tags=["external"],
        dependencies=[Depends(reject_nul_path_params), Depends(external_only())],
    )

    @router.get(
        "/{agent_code}/runs",
        response_model=None,
        dependencies=[Depends(require("session", "read"))],
    )
    async def list_runs(
        agent_code: str,
        request: Request,
        runs: Annotated[RunStore, Depends(_get_run_store)],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        user_id: Annotated[str, Query(min_length=1, max_length=255)],
        session_id: Annotated[UUID | None, Query()] = None,
        status: Annotated[RunStatus | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> JSONResponse:
        """列出这个终端用户在这个 agent 上跑过的 run。

        ``user_id`` 必填、无默认 —— 漏传是 422,不是「列出整个租户」。

        ``session_id`` 选填:给了就先验它属于 ``(user, agent)``(不属于就
        404,不是空列表 —— 响应不能携带存在性信息),再按那个 thread 过滤。
        给了 ``session_id`` 时,``load_owned_session`` 返回的 ``meta.user_id``
        才是权威值(直接复用授权那次读,不再另外查一次 —— 也避免两次查询
        之间出现 TOCTOU 窗口)。

        ``mint=False``:一个这个租户从没见过的 ``user_id``,在**没给**
        ``session_id`` 时返回空列表且不建 ``tenant_user`` 行(P1 复审
        T3)——「没跑过 run」和「没这个人」对第三方是同一个事实。给了
        ``session_id`` 则统一走上面的 404(未知用户不构成特例)。

        ``error`` 与 SSE ``error`` 帧携带的是同一个字符串(两者都是
        ``str(exc)``,``orchestrator/sse.py:745``/``:779``),所以这里给出
        它没有新开任何泄露面。
        """
        tenant_id: UUID = request.state.tenant_id
        thread_ids: list[UUID] | None = None
        end_user_id: UUID | None
        if session_id is not None:
            try:
                meta = await load_owned_session(
                    tenant_id=tenant_id,
                    agent_code=agent_code,
                    user_id=user_id,
                    session_id=session_id,
                    threads=threads,
                    users=users,
                    mint=False,
                )
            except ExternalScopeError as exc:
                return external_error(exc)
            end_user_id = meta.user_id
            thread_ids = [session_id]
        else:
            try:
                end_user_id = await lookup_external_user_id(
                    tenant_id=tenant_id, user_id=user_id, users=users
                )
            except ExternalScopeError as exc:
                return external_error(exc)
            if end_user_id is None:
                return JSONResponse(
                    {
                        "success": True,
                        "data": {"runs": [], "limit": limit, "offset": offset},
                        "error": None,
                    }
                )

        rows = await runs.list_for_tenant(
            tenant_id=tenant_id,
            user_id=end_user_id,
            agent_name=agent_code,
            thread_ids=thread_ids,
            status=status,
            limit=limit,
            offset=offset,
        )
        return JSONResponse(
            {
                "success": True,
                "data": {
                    "runs": [
                        {
                            "run_id": str(r.run_id),
                            "session_id": str(r.thread_id),
                            "status": r.status.value,
                            "created_at": r.created_at.isoformat() if r.created_at else None,
                            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                            "error": r.error,
                            # 产物清单契约 —— 覆盖「没消费到 end 就终局」的
                            # 重连收尾重建;null = 历史 run 无记录。
                            "artifacts": r.artifacts,
                            # P-1 —— 被取代 / 重发链接;两者都是 null = 普通一轮。
                            "superseded_by": str(r.superseded_by_run_id)
                            if r.superseded_by_run_id
                            else None,
                            "regenerated_from": str(r.regenerated_from_run_id)
                            if r.regenerated_from_run_id
                            else None,
                        }
                        for r in rows
                    ],
                    "limit": limit,
                    "offset": offset,
                },
                "error": None,
            }
        )

    @router.get(
        "/{agent_code}/runs/{run_id}/usage",
        response_model=None,
        dependencies=[Depends(require("session", "read"))],
    )
    async def run_usage(
        agent_code: str,
        run_id: UUID,
        request: Request,
        runs: Annotated[RunStore, Depends(_get_run_store)],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        usage: Annotated[TokenUsageStore, Depends(_get_token_usage_store)],
        user_id: Annotated[str, Query(min_length=1, max_length=255)],
    ) -> JSONResponse:
        """这个 run 的 token 用量,按 ``(provider, model)`` 分桶。

        **对账兜底** —— 日常扣账走 ``end`` 帧(它带同一份数据);这里给没接住
        end 帧、要补数、或月底对账的场景。两处的 ``usage_by_model`` 逐字段同形,
        因为共用 ``_run_usage`` 那一个装配口。

        含**整棵调用树**:worker 与主线共用同一个 trace,其用量记在
        ``{parent}-worker`` 名下、同一 trace 下,按 trace 聚合天然包含且不重复
        (用量按每次 LLM 调用落一行,不是按 span 嵌套记)。

        四档 token **恒全给** —— 计价口径是调用方的事,我方只负责计量完整准确。
        ``llm_calls`` 不对外。平台自身开销(``quality_sampling`` /
        ``skill_evolution``)不计入。

        ``usage_by_model`` **缺席**表示无记录(run 尚未绑 trace / 历史 run 无用量
        行 / 取数失败),**空数组**表示确有其事的零用量 —— 两者不可混同。

        ``user_id`` 必填无默认:漏传是 422,不是「随便谁的 run 都能查」。run 不属于
        该 ``(user, agent)`` 返 404 而不是空结果 —— 响应不能携带存在性信息。

        run 未结束也可查,返回到目前为止的量;``run_status`` 让调用方自己判断要不要
        落账。
        """
        tenant_id: UUID = request.state.tenant_id
        try:
            run, _meta = await load_owned_run(
                tenant_id=tenant_id,
                agent_code=agent_code,
                user_id=user_id,
                run_id=run_id,
                runs=runs,
                threads=threads,
                users=users,
            )
        except ExternalScopeError as exc:
            return external_error(exc)

        buckets = await make_usage_loader(
            usage=usage, runs=runs, run_id=run_id, tenant_id=tenant_id
        )()
        body: dict[str, Any] = {
            "run_id": str(run_id),
            # 终态用与 ``end`` 帧同一张映射表;非终态原样给它的小写名,调用方据此
            # 知道「这个 run 还在跑,数还会变」。
            # 与 ``GET .../runs`` 的 ``status`` 同一套词表(``RunStatus`` 原值),
            # **不是** ``end`` 帧那套。对外平面这两套并存:``end`` 帧只认四个终态
            # (``EXTERNAL_END_STATUSES``,``timeout`` 被折成 ``error``),而 run
            # 列表给 ``RunStatus`` 原值。本端点是 run 资源的子资源、且能在 run
            # 未结束时被调用(``queued`` / ``running`` 在帧词表里根本没有),所以
            # 跟列表对齐 —— 否则同一个 run 在两个端点上会报出不同的状态串
            # (``timeout`` vs ``error``)。
            "run_status": run.status.value,
        }
        if buckets is not None:
            body["usage_by_model"] = buckets
        return JSONResponse(content=body)

    @router.post("/{agent_code}/runs/{run_id}:cancel", response_model=None)
    async def cancel_run(
        agent_code: str,
        run_id: UUID,
        payload: ExternalCancelRequest,
        request: Request,
        principal: Annotated[Principal, Depends(require("session", "write"))],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
    ) -> JSONResponse:
        """Abort an in-flight run. Works in both ``stream`` and ``queue`` mode.

        Two-level, reusing the primitive the tenant-suspend / agent-kill switches
        already rely on: a run owned by THIS replica is aborted immediately via
        the manager's abort event; a run owned by another replica is CAS-flipped
        to INTERRUPTED in the store, and its owner stops within one lease
        heartbeat. Idempotent — cancelling a finished run reports
        ``stopped: false`` rather than erroring.
        """
        tenant_id: UUID = request.state.tenant_id
        try:
            run, _meta = await load_owned_run(
                tenant_id=tenant_id,
                agent_code=agent_code,
                user_id=payload.user_id,
                run_id=run_id,
                runs=runs,
                threads=threads,
                users=users,
            )
        except ExternalScopeError as exc:
            return external_error(exc)

        # A terminal (SUCCESS/ERROR/TIMEOUT/INTERRUPTED) or PAUSED run must
        # report stopped=False without touching either cancel primitive.
        # RunManager.cancel() returns True iff an in-memory record exists —
        # regardless of its status — and that record lingers ~5 minutes past
        # completion (its TTL sweep delay), so calling it unconditionally
        # would report an already-finished run as freshly stopped.
        if run.status in TERMINAL_RUN_STATUSES:
            stopped = False
        else:
            runtime = request.app.state.agent_runtime
            stopped = await cancel_run_two_level(
                run_manager=runtime.run_manager,
                run_store=runs,
                bus=getattr(request.app.state, "invalidation_bus", None),
                run_id=run.run_id,
                tenant_id=tenant_id,
                reason=InterruptReason.USER_CANCEL,
            )
        return JSONResponse(
            {
                "success": True,
                "data": {"run_id": str(run.run_id), "stopped": bool(stopped)},
                "error": None,
            }
        )

    @router.post("/{agent_code}/runs/{run_id}:regenerate", response_model=None)
    async def regenerate_run(
        agent_code: str,
        run_id: UUID,
        payload: ExternalRegenerateRequest,
        request: Request,
        principal: Annotated[Principal, Depends(require("session", "write"))],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
    ) -> StreamingResponse | JSONResponse:
        """P-1 —— 同一输入再跑一次(旧轮标「已被取代」,agent 后续看不见它)。"""
        return await _supersede_and_run(
            op="regenerate",
            agent_code=agent_code,
            run_id=run_id,
            payload=payload,
            request=request,
            runs=runs,
            threads=threads,
            users=users,
            idempotency_key=idempotency_key,
        )

    @router.post("/{agent_code}/runs/{run_id}:edit", response_model=None)
    async def edit_run(
        agent_code: str,
        run_id: UUID,
        payload: ExternalEditRequest,
        request: Request,
        principal: Annotated[Principal, Depends(require("session", "write"))],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
    ) -> StreamingResponse | JSONResponse:
        """P-1 —— 改输入后再跑一次(旧轮标「已被取代」)。"""
        return await _supersede_and_run(
            op="edit",
            agent_code=agent_code,
            run_id=run_id,
            payload=payload,
            request=request,
            runs=runs,
            threads=threads,
            users=users,
            idempotency_key=idempotency_key,
        )

    return router
