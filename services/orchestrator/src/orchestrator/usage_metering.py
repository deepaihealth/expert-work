"""B-104 —— run 内模型调用的通用记账:落 ``token_usage`` 一行 + 扣全树 token 池。

主循环(agent 节点)的用量由 :class:`~expert_work.runtime.middleware.TokenUsageMiddleware`
落行:它挂在 ``after_llm_call`` 链上,而这条链只有 agent 节点在**它自己那次** LLM
调用之后才跑(``graph_builder/builder.py`` 的 agent 节点),token 池也在那里扣。其余
在 run 里调模型的地方 —— 看图(``ask_image``,B-64 Task 9)、任务规划、反思评判、对话
压缩摘要、记忆读时校验 / 查询改写 / 写回抽取 / 写回归并、输出 / 工具调用安全评审、
知识库 / 记忆重排序 —— 都不经过它,原先一行不落、池也不扣。

这里是这些调用点**共用的一套**规则,不给每个调用点各写一份:

* 落行的仍是同一个 ``TokenUsageMiddleware`` 类(同样的抽取口径、同样的「没有用量就
  不落」、同样的失败吞掉、``trace_id`` 同样取当前 span),只把 ``provider`` / ``model``
  换成**这一次实际应答**的那个模型、``usage_kind`` 换成调用点的口径。
* 扣池用与主循环同源的 :func:`~orchestrator.tools._guards.usage_total`,在调用**成功
  返回之后**扣;是否超额仍由下一次进入 agent 节点时检查(不改熔断语义)。
* 调用抛错(含取消)时既不落行也不扣池 —— 与主循环一致。

**主循环不走这里**:它的记账与扣池留在 agent 节点里原样不动(那里还有缓存命中、结构化
重发、拒答替换、估算漂移这些只属于主循环的规则)。本模块的包装只套在上面那些调用点各自
拿到的 caller 上,主循环用的 caller 实例不套,所以不会重复计。

「实际应答的是谁」只有路由知道:它按句柄逐个试,每个句柄的调用各包一层
``around_llm_call`` 链,``payload["provider_key"]`` 就是句柄 key(``provider:model``,
多 key 时带 ``#序号``)。:class:`ServedByStamp` 在这一层把 key 盖到响应的
``response_metadata`` 上,:class:`ServedModelResolver` 读它换回该模型条目的**配置**
``(provider, model)``(B-102:不用厂商回显的 ``model_name``,那会带别名);没有盖章
的响应记在该调用点配置的主模型名下。主循环的 ``TokenUsageMiddleware`` 用同一个解析器
(``served_by``),所以同一张表只有一种含义:实际应答的模型。

run 的上下文(tenant / user / token 池)怎么来:

* ``ask_image`` 是工具,从 ``ToolContext`` 显式传进 :meth:`UsageMeter.__call__`。
* 其余调用点一律套 :class:`MeteredLLMCaller`:调用成功后从**当前 LangGraph 节点的
  RunnableConfig**(``configurable`` 里的 ``tenant_id`` / ``user_id`` / token 池,与
  agent 节点读的是同一份)取。不在任何 run 里(没有节点 config)时什么都不记 —— 那就
  不是 run 内调用。
* 重排序器是进程级对象、它的路由在每次 rerank 里现建,构建期够不着;agent 构建给它套
  :class:`ScopedReranker`,在 rerank 期间把本 agent 的记账身份放进一个 contextvar,
  重排序器的 LLM 分支用 :func:`current_usage_identity` 取出来包自己的路由。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables.config import var_child_runnable_config

from expert_work.persistence.token_usage_store import PLATFORM_OVERHEAD_USAGE_KIND, TokenUsageStore
from expert_work.runtime.middleware import (
    CallNext,
    MiddlewareChain,
    MiddlewareContext,
    TokenUsageMiddleware,
)
from orchestrator.tools._guards import TOKEN_BUDGET_KEY, TokenBudget, usage_total

if TYPE_CHECKING:
    from expert_work.protocol import ModelSpec, StructuredOutputSpec
    from orchestrator.llm import LLMCaller
    from orchestrator.llm.providers._streaming import LLMDelta
    from orchestrator.tools.knowledge import Reranker
    from orchestrator.tools.registry import ToolSpec

#: 响应 ``response_metadata`` 上记「哪个句柄应答的」的键。只盖在带盖章层的路由的响应上。
SERVED_BY_KEY = "expert_work_served_by"


@dataclass
class ServedByStamp:
    """``around_llm_call`` —— 把应答句柄的 key 盖到响应上。

    包在原有 ``around_llm_call`` 链(重试 / 熔断 / langfuse)**外面**:原链对这个句柄
    重试到成功后,这里看到的是最终那个响应;原链抛错时这里不盖,错误原样上抛,路由照旧
    换下一个句柄。响应用 ``model_copy`` 换新,不改原对象。
    """

    inner: MiddlewareChain | None
    name: str = "served_by"
    anchor: str = "around_llm_call"
    after: tuple[str, ...] = field(default_factory=tuple)
    before: tuple[str, ...] = field(default_factory=tuple)

    async def __call__(self, ctx: MiddlewareContext, call_next: CallNext) -> None:
        if self.inner is None:
            await call_next(ctx)
        else:
            await self.inner.invoke(ctx, call_next)
        response = ctx.payload.get("response")
        key = ctx.payload.get("provider_key")
        if isinstance(response, AIMessage) and isinstance(key, str):
            ctx.payload["response"] = response.model_copy(
                update={"response_metadata": {**response.response_metadata, SERVED_BY_KEY: key}}
            )


def with_served_by(chain: MiddlewareChain | None) -> MiddlewareChain:
    """带盖章的 ``around_llm_call`` 链:原链整条包进 :class:`ServedByStamp`。"""
    return MiddlewareChain.from_middlewares("around_llm_call", [ServedByStamp(inner=chain)])


@dataclass(frozen=True)
class ServedModelResolver:
    """响应 → 实际应答模型的配置 ``(provider, model)``。

    ``models``:句柄 group(``provider:model``,不带 ``#序号``)→ ``(provider, model)``,
    覆盖该路由的整条链(主模型 + 它的备用)。响应上没有盖章、或盖的 key 不在表里,记
    ``default``(该路由配置的主模型)。
    """

    default: tuple[str, str]
    models: Mapping[str, tuple[str, str]] = field(default_factory=dict)

    def __call__(self, response: AIMessage) -> tuple[str, str]:
        key = response.response_metadata.get(SERVED_BY_KEY)
        if isinstance(key, str):
            group = key if key in self.models else key.rpartition("#")[0]
            found = self.models.get(group)
            if found is not None:
                return found
        return self.default


def chain_models(model: ModelSpec) -> dict[str, tuple[str, str]]:
    """``model`` 与它整棵备用树上每个条目的 ``group → (provider, model)``。"""
    models: dict[str, tuple[str, str]] = {}
    pending = [model]
    while pending:
        entry = pending.pop()
        models[f"{entry.provider}:{entry.name}"] = (entry.provider, entry.name)
        pending.extend(entry.fallback)
    return models


async def _noop(_ctx: MiddlewareContext) -> None:
    return None


@dataclass(frozen=True)
class UsageMeter:
    """一次调用落一行 ``token_usage`` —— 与主模型同维度、同一个 trace。

    ``agent_name`` / ``agent_version`` 取自这次构建,与主模型那一行逐字相同(worker 构建
    就是 worker 的名字);``usage_kind`` 是调用点的口径(用 Agent 自己模型的调用 = 构建
    的 kind,平台模型的调用 = ``platform_overhead``)。``trace_id`` 由
    ``TokenUsageMiddleware`` 在写入时从当前 span 取 —— 调用都在 run 的 span 下执行,
    所以与主模型的行同一个 trace,能按 run 连表。

    ``store`` 为 ``None``(测试 / 无控制面)时不落行,只扣池。
    """

    store: TokenUsageStore | None
    agent_name: str
    agent_version: str
    usage_kind: str
    #: 响应上没有盖章时记在谁名下:该调用点配置的主模型。
    default: tuple[str, str]
    #: 句柄 group(``provider:model``,不带 ``#序号``)→ ``(provider, model)``,
    #: 覆盖该调用点路由的整条链(主模型 + 它的备用)。
    models: Mapping[str, tuple[str, str]] = field(default_factory=dict)

    async def __call__(self, response: AIMessage, *, tenant_id: UUID, user_id: UUID | None) -> None:
        """落一行(不扣池)。``ask_image`` 用这个入口,池由工具自己按同一口径扣。"""
        if self.store is None:
            return
        provider, model = self._served(response)
        middleware = TokenUsageMiddleware(
            store=self.store,
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            model=model,
            provider=provider,
            usage_kind=self.usage_kind,
        )
        # 这些调用不走 before_llm_call,也就没有响应缓存可命中:``cache_hit`` 恒为 False,
        # 于是「上游没报用量就不落行」这一条与主模型未命中缓存时完全一致。
        payload: dict[str, Any] = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "response": response,
            "cache_hit": False,
        }
        await middleware(MiddlewareContext(payload=payload), _noop)

    async def charge(
        self,
        response: AIMessage,
        *,
        tenant_id: UUID | None,
        user_id: UUID | None,
        token_budget: TokenBudget | None,
    ) -> None:
        """一次成功调用的完整记账:先扣池,再落行(没有 tenant 就只扣池)。"""
        if token_budget is not None:
            token_budget.add(usage_total(response.usage_metadata))
        if tenant_id is not None:
            await self(response, tenant_id=tenant_id, user_id=user_id)

    def _served(self, response: AIMessage) -> tuple[str, str]:
        return ServedModelResolver(default=self.default, models=self.models)(response)


@dataclass(frozen=True)
class UsageIdentity:
    """记账身份(存储 + agent + kind),还不知道是哪个模型 —— 给构建期拿不到模型的调用点用。"""

    store: TokenUsageStore | None
    agent_name: str
    agent_version: str
    usage_kind: str

    def meter(
        self, default: tuple[str, str], models: Mapping[str, tuple[str, str]] | None = None
    ) -> UsageMeter:
        return UsageMeter(
            store=self.store,
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            usage_kind=self.usage_kind,
            default=default,
            models=dict(models or {}),
        )


def overhead_usage_kind(build_kind: str) -> str:
    """用平台模型的调用(安全评审 / 重排序)记什么 kind。

    对话构建记 ``platform_overhead``;其它构建(如 ``skill_evolution`` 回放)随构建的
    kind —— 回放整棵树的花费归同一口径,不因评审 / 重排序漏回对话侧的开销里。
    """
    return PLATFORM_OVERHEAD_USAGE_KIND if build_kind == "conversation" else build_kind


def _parse_uuid(raw: object) -> UUID | None:
    if isinstance(raw, UUID):
        return raw
    if isinstance(raw, str):
        try:
            return UUID(raw)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class RunUsageContext:
    tenant_id: UUID | None
    user_id: UUID | None
    token_budget: TokenBudget | None


def current_run_usage_context() -> RunUsageContext | None:
    """当前 LangGraph 节点的 run 上下文;不在节点里执行时为 ``None``。

    读的是 agent 节点同一份 ``configurable``(``tenant_id`` / ``user_id`` / token 池),
    子 Agent / worker 的节点读到的是 ``_child_config`` 传下来的那份(同一个池)。
    """
    config = var_child_runnable_config.get()
    if config is None:
        return None
    configurable = config.get("configurable") or {}
    budget = configurable.get(TOKEN_BUDGET_KEY)
    return RunUsageContext(
        tenant_id=_parse_uuid(configurable.get("tenant_id")),
        user_id=_parse_uuid(configurable.get("user_id")),
        token_budget=budget if isinstance(budget, TokenBudget) else None,
    )


async def charge_in_run(meter: UsageMeter, response: AIMessage) -> None:
    """一次成功调用按当前 run 的上下文记一次;不在 run 里时什么都不记。

    :class:`MeteredLLMCaller` 调用成功后用它;调用点要把计时(如反思的
    ``wait_for``)只套在模型调用上、不把记账写库算进去时,直接调它。
    """
    ctx = current_run_usage_context()
    if ctx is not None:
        await meter.charge(
            response,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            token_budget=ctx.token_budget,
        )


@dataclass(frozen=True)
class MeteredLLMCaller:
    """给一个 :class:`~orchestrator.llm.LLMCaller` 套上记账:调用成功后按 run 上下文记一次。

    调用抛错(含取消)原样上抛,不记。可选参数只在调用方给了时才往里传,不给的
    caller 实现(测试替身)照旧能用。
    """

    inner: LLMCaller
    meter: UsageMeter

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
        on_delta: Callable[[LLMDelta], Awaitable[None]] | None = None,
    ) -> AIMessage:
        kwargs: dict[str, Any] = {}
        if output_schema is not None:
            kwargs["output_schema"] = output_schema
        if on_delta is not None:
            kwargs["on_delta"] = on_delta
        response = await self.inner(messages=messages, tools=tools, **kwargs)
        await charge_in_run(self.meter, response)
        return response


_USAGE_IDENTITY: ContextVar[UsageIdentity | None] = ContextVar(
    "expert_work_usage_identity", default=None
)


def current_usage_identity() -> UsageIdentity | None:
    """:class:`ScopedReranker` 在 rerank 期间放进来的记账身份;不在其中时为 ``None``。"""
    return _USAGE_IDENTITY.get()


@contextmanager
def usage_identity_scope(identity: UsageIdentity) -> Iterator[None]:
    token = _USAGE_IDENTITY.set(identity)
    try:
        yield
    finally:
        _USAGE_IDENTITY.reset(token)


@dataclass(frozen=True)
class ScopedReranker:
    """把一个进程级 :class:`~orchestrator.tools.knowledge.Reranker` 绑到本次构建的记账身份上。

    重排序器的路由在每次 rerank 里现建(平台配置可热改),构建期没有 caller 可包;这里在
    rerank 期间把身份放进 contextvar,由重排序器的 LLM 分支取出来包自己的路由。
    """

    inner: Reranker
    identity: UsageIdentity

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        with usage_identity_scope(self.identity):
            return await self.inner.rerank(
                query=query, documents=documents, top_k=top_k, tenant_id=tenant_id
            )
