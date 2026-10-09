"""B-168 —— 后台记忆写回的处理器:一条任务 → 读本轮结束时的对话 → 调记忆模型 → 写记忆。

:class:`~control_plane.memory.writeback_worker.MemoryWritebackWorker` 在任务自己的
租户 + 用户作用域里调用它(设计稿 §3.3 / §3.4):

1. 按 ``tenant_id + agent_name + agent_version`` 取 Agent 配置(``extends`` 按构建时同一
   取法展开)。配置已删、或已没有开 ``memory.long_term.write_back``,就不做,回一个带
   原因的结果(worker 收成 ``done``)。
2. 读检查点:有 ``checkpoint_id`` 读那一个(本轮结束时);没有就读会话最新的,再按
   ``message_count`` 截前 N 条(退路,B-126 跨轮清理改写早期消息时可能对不上)。
3. 记忆模型与 run 内同一个取法(:func:`orchestrator.agent_factory.memory_model`),路由由
   注入的 caller 工厂建(生产见 :func:`make_router_caller_factory`)。
4. 调 :func:`~orchestrator.graph_builder.memory.flush_messages_with_outcome`,参数取自配置,
   与 run 内写回节点一致;写记忆之前经 ``before_store`` 问一次任务行还在不在(§3.6),
   不在就整批放弃,回 ``discarded``。

**记账**(§8 问题 3 拍板 (b)):后台记忆调用不归 run,按平台开销记 ——
``usage_kind = platform_overhead``,同重排序。run 内的记账靠 LangGraph 节点的 config
(``usage_metering.charge_in_run``),在 run 外**静默不记**,所以这里直接调
:class:`~orchestrator.usage_metering.UsageMeter`,租户 / 用户取自任务行。``trace_id``
取当前 span:处理期间把任务行的 ``trace_id`` 设成远程父 span(:func:`_under_run_trace`),
这一行于是和原 run 同一个 trace —— run 的用量接口按 kind 过滤(对外只取
``conversation``,控制台排除 ``NON_BILLABLE_USAGE_KINDS``),不会算回 run;运营用量页
按 kind 能看到。抽取 / 归并两个 span 也因此挂在原 run 的 trace 下(Langfuse)。
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from control_plane.memory.writeback_worker import WritebackOutcome
from control_plane.runtime import make_provider_key_resolver
from control_plane.transcript import read_messages
from expert_work.persistence.agent_spec import AgentSpecStore
from expert_work.persistence.memory import MemoryWritebackJob
from expert_work.persistence.token_usage_store import (
    PLATFORM_OVERHEAD_USAGE_KIND,
    TokenUsageStore,
)
from expert_work.protocol import AgentSpec, ModelSpec
from expert_work.runtime.cancellation import CancellationToken, RunCancelledError
from orchestrator import MemoryEnv, MiddlewareEnv, build_llm_router, build_middleware_chains
from orchestrator.agent_factory import memory_model
from orchestrator.graph_builder.memory import flush_messages_with_outcome
from orchestrator.llm import LLMCaller
from orchestrator.usage_metering import UsageIdentity, UsageMeter, chain_models, with_served_by

if TYPE_CHECKING:
    import httpx
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from expert_work.common.credentials import CredentialsResolver
    from expert_work.runtime.secret_store import SecretStore
    from orchestrator.llm import RateLimiterFactory
    from orchestrator.tools.registry import ToolSpec

logger = logging.getLogger("expert_work.control_plane.memory.writeback_processor")

#: ``(effective spec, memory model, tenant) → LLM caller`` —— 生产版建一条与 run 内同口径
#: 的路由(:func:`make_router_caller_factory`),测试注入假 caller。
CallerFactory = Callable[[AgentSpec, ModelSpec, UUID], Awaitable[LLMCaller]]
#: 展开 ``extends``(fork 的模板继承)—— 与构建时同一取法;``None`` = 原样用存的配置。
SpecResolver = Callable[[AgentSpec], Awaitable[AgentSpec]]


class _PlatformMeteredCaller:
    """调用成功后按任务行的租户 / 用户记一行平台开销(run 外,不扣 token 池)。"""

    def __init__(
        self, *, inner: LLMCaller, meter: UsageMeter, tenant_id: UUID, user_id: UUID
    ) -> None:
        self._inner = inner
        self._meter = meter
        self._tenant_id = tenant_id
        self._user_id = user_id

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        **kwargs: Any,
    ) -> AIMessage:
        response = await self._inner(messages=messages, tools=tools, **kwargs)
        await self._meter(response, tenant_id=self._tenant_id, user_id=self._user_id)
        return response


@contextmanager
def _under_run_trace(trace_id: str | None) -> Iterator[None]:
    """把 ``trace_id`` 设成当前的远程父 span,里面开的 span、落的用量行都归这个 trace。

    原 run 的根 span id 没有记下来,这里用一个随机 span id 当父;Langfuse 按 trace 归组,
    这几个 span 出现在原 run 的 trace 里(挂在 trace 根下,而不是某个具体步骤下)。
    ``trace_id`` 缺失或不是 32 位十六进制时什么都不做。
    """
    try:
        tid = int(trace_id, 16) if trace_id else 0
    except ValueError:
        tid = 0
    if tid == 0 or tid >= 1 << 128:
        yield
        return
    parent = NonRecordingSpan(
        SpanContext(
            trace_id=tid,
            span_id=secrets.randbits(64) | 1,
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
    )
    token = otel_context.attach(trace.set_span_in_context(parent))
    try:
        yield
    finally:
        otel_context.detach(token)


class MemoryWritebackProcessor:
    """Process one ``memory_writeback_job`` row (see module docstring)."""

    def __init__(
        self,
        *,
        agent_specs: AgentSpecStore,
        memory_env: MemoryEnv,
        checkpointer: BaseCheckpointSaver[Any],
        caller_factory: CallerFactory,
        usage_store: TokenUsageStore | None,
        resolve_spec: SpecResolver | None = None,
    ) -> None:
        self._agents = agent_specs
        self._env = memory_env
        self._checkpointer = checkpointer
        self._caller_factory = caller_factory
        self._usage_store = usage_store
        self._resolve_spec = resolve_spec

    async def __call__(
        self, job: MemoryWritebackJob, *, still_queued: Callable[[], Awaitable[bool]]
    ) -> WritebackOutcome:
        record = await self._agents.get(
            tenant_id=job.tenant_id, name=job.agent_name, version=job.agent_version
        )
        if record is None:
            return WritebackOutcome(written_count=0, failed=False, note="agent spec is gone")
        spec = record.spec if self._resolve_spec is None else await self._resolve_spec(record.spec)
        long_term = spec.spec.memory.long_term if spec.spec.memory is not None else None
        if long_term is None or not long_term.write_back:
            return WritebackOutcome(
                written_count=0, failed=False, note="long-term memory write-back is off"
            )
        if self._env.store is None or self._env.embedder is None:
            msg = "memory store / embedder not wired"
            raise RuntimeError(msg)

        messages = await read_messages(
            self._checkpointer, job.thread_id, checkpoint_id=job.checkpoint_id
        )
        if job.checkpoint_id is None and job.message_count is not None:
            messages = messages[: job.message_count]
        if not messages:
            return WritebackOutcome(written_count=0, failed=False, note="no messages to read")

        model = memory_model(spec)
        meter = UsageIdentity(
            store=self._usage_store,
            agent_name=spec.metadata.name,
            agent_version=spec.metadata.version,
            usage_kind=PLATFORM_OVERHEAD_USAGE_KIND,
        ).meter(default=(model.provider, model.name), models=chain_models(model))
        caller = _PlatformMeteredCaller(
            inner=await self._caller_factory(spec, model, job.tenant_id),
            meter=meter,
            tenant_id=job.tenant_id,
            user_id=job.user_id,
        )

        async def before_store() -> None:
            try:
                queued = await still_queued()
            except Exception:
                # 查不到就照常写:与今天「run 正在跑时被清除」同一类小窗口(§3.6)。
                logger.warning("memory.writeback_background.recheck_failed", exc_info=True)
                return
            if not queued:
                raise RunCancelledError("memory writeback job was purged")

        try:
            with _under_run_trace(job.trace_id):
                outcome = await flush_messages_with_outcome(
                    messages,
                    memory_store=self._env.store,
                    embedder=self._env.embedder,
                    llm_caller=caller,
                    tenant_id=job.tenant_id,
                    user_id=job.user_id,
                    thread_id=job.thread_id,
                    run_id=str(job.run_id),
                    token=CancellationToken(),
                    dlq=self._env.dlq,
                    log_label="memory.writeback_background",
                    reconcile=long_term.reconcile_writes,
                    agent_name=spec.metadata.name,
                    write_min_importance=long_term.write_min_importance,
                    before_store=before_store,
                )
        except RunCancelledError:
            return WritebackOutcome(written_count=0, failed=False, discarded=True)
        return WritebackOutcome(written_count=outcome.written, failed=outcome.failed)


def make_router_caller_factory(
    *,
    secret_store: SecretStore,
    credentials_resolver: CredentialsResolver | None,
    middleware_env: MiddlewareEnv | None,
    http_client: httpx.AsyncClient | None,
    rate_limiter_factory: RateLimiterFactory | None,
) -> CallerFactory:
    """生产版 caller 工厂:与 Agent 构建同口径的记忆路由。

    凭据只从平台解析(``ignore_api_key_ref=True``,同 Agent 构建);``around_llm_call``
    链(错误处理 / Langfuse)按该 Agent 的配置建,外面套「实际应答的模型」盖章层,记账
    记在备用链上真正应答的那个模型名下。单次执行的上限由 worker 的租约兜住。
    """

    async def factory(spec: AgentSpec, model: ModelSpec, tenant_id: UUID) -> LLMCaller:
        chains = build_middleware_chains(spec, env=middleware_env)
        return await build_llm_router(
            model,
            secret_store=secret_store,
            around_llm_chain=with_served_by(chains.around_llm_call),
            provider_key_resolver=(
                make_provider_key_resolver(resolver=credentials_resolver, tenant_id=tenant_id)
                if credentials_resolver is not None
                else None
            ),
            ignore_api_key_ref=True,
            http_client=http_client,
            rate_limiter_factory=rate_limiter_factory,
        )

    return factory


__all__ = ["MemoryWritebackProcessor", "make_router_caller_factory"]
