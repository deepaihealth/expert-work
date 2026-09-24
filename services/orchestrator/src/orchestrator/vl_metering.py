"""B-64 Task 9 —— ``ask_image`` 的看图模型(VL)调用记账。

主模型的用量由 :class:`~expert_work.runtime.middleware.TokenUsageMiddleware`
落进 ``token_usage``:它挂在 ``after_llm_call`` 链上,而这条链只有 agent 节点在
**它自己那次** LLM 调用之后才跑(``graph_builder/builder.py`` 的 agent 节点)。
``ask_image`` 在工具里直接调 VL 路由,不经过 agent 节点,所以 VL 的调用一行都没落过
—— 用量只剩 ToolMessage 上的 ``artifact.vl_usage``,对外用量、账单、token 熔断全都看不见。

这里不另写一套记账:落行的仍是同一个 ``TokenUsageMiddleware`` 类(同样的抽取口径、
同样的「没有用量就不落」、同样的失败吞掉),只把 ``provider`` / ``model`` 换成**这一次
实际应答**的那个 VL 模型 —— VL 声明了备用链,备用接管时账要记在备用名下。

「实际应答的是谁」只有路由知道:它按句柄逐个试,每个句柄的调用各包一层
``around_llm_call`` 链,``payload["provider_key"]`` 就是句柄 key(``provider:model``,
多 key 时带 ``#序号``)。:class:`ServedByStamp` 在这一层把 key 盖到响应的
``response_metadata`` 上,:class:`VLUsageRecorder` 读它换回 ``(provider, model)``。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from langchain_core.messages import AIMessage

from expert_work.persistence.token_usage_store import TokenUsageStore
from expert_work.runtime.middleware import (
    CallNext,
    MiddlewareChain,
    MiddlewareContext,
    TokenUsageMiddleware,
)

#: 响应 ``response_metadata`` 上记「哪个句柄应答的」的键。只盖在 VL 路由的响应上。
SERVED_BY_KEY = "expert_work_served_by"


@dataclass
class ServedByStamp:
    """``around_llm_call`` —— 把应答句柄的 key 盖到响应上。

    包在原有 ``around_llm_call`` 链(重试 / 熔断 / langfuse)**外面**:原链对这个句柄
    重试到成功后,这里看到的是最终那个响应;原链抛错时这里不盖,错误原样上抛,路由照旧
    换下一个句柄。响应用 ``model_copy`` 换新,不改原对象。
    """

    inner: MiddlewareChain | None
    name: str = "vl_served_by"
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
    """VL 路由用的 ``around_llm_call`` 链:原链整条包进 :class:`ServedByStamp`。"""
    return MiddlewareChain.from_middlewares("around_llm_call", [ServedByStamp(inner=chain)])


async def _noop(_ctx: MiddlewareContext) -> None:
    return None


@dataclass(frozen=True)
class VLUsageRecorder:
    """一次 VL 调用落一行 ``token_usage`` —— 与主模型同维度、同一个 trace。

    ``agent_name`` / ``agent_version`` / ``usage_kind`` 取自这次构建,与主模型那一行
    逐字相同(worker 构建就是 worker 的名字,评估回放就是回放的 kind)。``trace_id``
    由 ``TokenUsageMiddleware`` 在写入时从当前 span 取 —— 工具在 run 的 span 下执行,
    所以与主模型的行同一个 trace,能按 run 连表。
    """

    store: TokenUsageStore
    agent_name: str
    agent_version: str
    usage_kind: str
    #: 句柄 group(``provider:model``,不带 ``#序号``)→ ``(provider, model)``,
    #: 覆盖 VL 路由的整条链(主 VL 模型 + 它的备用)。
    models: Mapping[str, tuple[str, str]]
    #: 响应上没有盖章时记在谁名下:VL 主模型。只有不经过路由的调用方(测试替身)才会走到。
    default: tuple[str, str]

    async def __call__(self, response: AIMessage, *, tenant_id: UUID, user_id: UUID | None) -> None:
        provider, model = self._served(response)
        middleware = TokenUsageMiddleware(
            store=self.store,
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            model=model,
            provider=provider,
            usage_kind=self.usage_kind,
        )
        # VL 调用不走 before_llm_call,也就没有响应缓存可命中:``cache_hit`` 恒为 False,
        # 于是「上游没报用量就不落行」这一条与主模型未命中缓存时完全一致。
        payload: dict[str, Any] = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "response": response,
            "cache_hit": False,
        }
        await middleware(MiddlewareContext(payload=payload), _noop)

    def _served(self, response: AIMessage) -> tuple[str, str]:
        key = response.response_metadata.get(SERVED_BY_KEY)
        if isinstance(key, str):
            group = key if key in self.models else key.rpartition("#")[0]
            found = self.models.get(group)
            if found is not None:
                return found
        return self.default
