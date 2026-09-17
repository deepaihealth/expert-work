"""Integration test for the LLM response cache in the live ReAct graph
— Stream E.13, test matrix #24.

Drives ``build_react_graph`` with the E.13 cache middlewares registered
on the ``before_llm_call`` / ``after_llm_call`` anchors and verifies
that an identical second run is served from cache — the underlying LLM
provider is called exactly once across two runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.llm import InMemoryRedisCache, LLMResponseCache
from expert_work.runtime.middleware import (
    LLMCacheLookupMiddleware,
    LLMCacheStoreMiddleware,
    MiddlewareChain,
)
from orchestrator import (
    LLM_CACHE_BYPASS_KEY,
    GraphRunner,
    LLMRouter,
    ProviderHandle,
    ToolRegistry,
    ToolSpec,
    build_react_graph,
)

_MODEL = "claude-sonnet-4-6"


@dataclass
class _CountingProvider:
    """LLMProvider that counts how many times the LLM was really hit."""

    reply: str = "the answer"
    calls: int = 0
    seen_messages: list[list[BaseMessage]] = field(default_factory=list)

    async def complete(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
    ) -> AIMessage:
        del tools
        self.calls += 1
        self.seen_messages.append(list(messages))
        return AIMessage(content=self.reply, id=f"ai-{self.calls}")


def _chains(cache: LLMResponseCache) -> tuple[MiddlewareChain, MiddlewareChain]:
    before = MiddlewareChain.from_middlewares(
        "before_llm_call",
        [LLMCacheLookupMiddleware(cache=cache, model=_MODEL, temperature=0.0, max_tokens=4096)],
    )
    after = MiddlewareChain.from_middlewares(
        "after_llm_call",
        [LLMCacheStoreMiddleware(cache=cache, model=_MODEL, temperature=0.0, max_tokens=4096)],
    )
    return before, after


async def _run_once(
    *,
    provider: _CountingProvider,
    cache: LLMResponseCache,
    tenant_id: str,
    prompt: str,
    bypass: bool = False,
) -> AIMessage:
    before, after = _chains(cache)
    router = LLMRouter(providers=[ProviderHandle(provider=provider, key="anthropic:primary")])
    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(
            build_react_graph(
                llm_caller=router,
                tool_registry=ToolRegistry(),
                before_llm_chain=before,
                after_llm_chain=after,
            )
        )
        configurable: dict[str, object] = {"thread_id": uuid4().hex, "tenant_id": tenant_id}
        if bypass:
            configurable[LLM_CACHE_BYPASS_KEY] = True
        config: RunnableConfig = {"configurable": configurable}
        final = await compiled.ainvoke(
            {
                "messages": [HumanMessage(content=prompt)],
                "step_count": 0,
                "max_steps": 5,
            },
            config=config,
        )
    last = final["messages"][-1]
    assert isinstance(last, AIMessage)
    return last


@pytest.mark.asyncio
async def test_identical_second_run_served_from_cache() -> None:
    """Test matrix #24 — same tenant + prompt twice; the LLM provider
    is hit only on the first run, the second is a cache hit."""
    provider = _CountingProvider(reply="42 is the answer")
    cache = LLMResponseCache(redis=InMemoryRedisCache())
    tenant = str(uuid4())

    first = await _run_once(
        provider=provider, cache=cache, tenant_id=tenant, prompt="what is 6 * 7?"
    )
    assert first.content == "42 is the answer"
    assert provider.calls == 1

    second = await _run_once(
        provider=provider, cache=cache, tenant_id=tenant, prompt="what is 6 * 7?"
    )
    assert second.content == "42 is the answer"
    # The LLM provider was NOT called again — served from cache.
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_bypass_run_calls_the_llm_and_its_answer_replaces_the_cached_one() -> None:
    """B-66 —— ``:regenerate`` 的 run 带 ``LLM_CACHE_BYPASS_KEY``:同一 prompt 也必须真调
    模型(用户按「重新生成」就是要一个新回答);新回答照常写回缓存,之后同样的普通
    run 命中的是它,不是旧的。"""
    provider = _CountingProvider(reply="first draft")
    cache = LLMResponseCache(redis=InMemoryRedisCache())
    tenant = str(uuid4())

    await _run_once(provider=provider, cache=cache, tenant_id=tenant, prompt="write a haiku")
    assert provider.calls == 1

    provider.reply = "second draft"
    regenerated = await _run_once(
        provider=provider, cache=cache, tenant_id=tenant, prompt="write a haiku", bypass=True
    )
    assert provider.calls == 2
    assert regenerated.content == "second draft"

    again = await _run_once(
        provider=provider, cache=cache, tenant_id=tenant, prompt="write a haiku"
    )
    assert provider.calls == 2
    assert again.content == "second draft"


@pytest.mark.asyncio
async def test_different_prompt_misses_cache() -> None:
    provider = _CountingProvider()
    cache = LLMResponseCache(redis=InMemoryRedisCache())
    tenant = str(uuid4())

    await _run_once(provider=provider, cache=cache, tenant_id=tenant, prompt="prompt one")
    await _run_once(provider=provider, cache=cache, tenant_id=tenant, prompt="prompt two")

    # Two distinct prompts → two real LLM calls.
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_different_tenant_misses_cache() -> None:
    """Test matrix #25 at the graph level — tenant B does not read
    tenant A's cached answer."""
    provider = _CountingProvider()
    cache = LLMResponseCache(redis=InMemoryRedisCache())

    await _run_once(provider=provider, cache=cache, tenant_id=str(uuid4()), prompt="same prompt")
    await _run_once(provider=provider, cache=cache, tenant_id=str(uuid4()), prompt="same prompt")

    assert provider.calls == 2


@pytest.mark.asyncio
async def test_no_tenant_id_disables_cache() -> None:
    """Without a tenant binding the cache is inert — every run hits
    the LLM (dev / unbound path must still work)."""
    provider = _CountingProvider()
    cache = LLMResponseCache(redis=InMemoryRedisCache())
    before, after = _chains(cache)
    router = LLMRouter(providers=[ProviderHandle(provider=provider, key="anthropic:primary")])

    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(
            build_react_graph(
                llm_caller=router,
                tool_registry=ToolRegistry(),
                before_llm_chain=before,
                after_llm_chain=after,
            )
        )
        for _ in range(2):
            await compiled.ainvoke(
                {
                    "messages": [HumanMessage(content="no tenant")],
                    "step_count": 0,
                    "max_steps": 5,
                },
                config={"configurable": {"thread_id": uuid4().hex}},
            )

    assert provider.calls == 2
