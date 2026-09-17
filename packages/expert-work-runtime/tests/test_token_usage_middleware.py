"""Tests for :class:`TokenUsageMiddleware` — Stream G.9.

The middleware is a thin observability layer:

  - reads ``usage_metadata`` off the LLM response
  - increments ``expert_work_llm_token_usage_total`` per type
  - persists one ``TokenUsageRecord`` per call
  - never lets a counter / DB error bubble up to the LLM call

The Prometheus counter is process-global; we read its sample values
directly from the default registry rather than mocking the helper.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from prometheus_client import REGISTRY

from expert_work.persistence.token_usage_store import (
    InMemoryTokenUsageStore,
    TokenUsageRecord,
)
from expert_work.runtime.middleware import MiddlewareContext
from expert_work.runtime.middleware.token_usage import TokenUsageMiddleware


def _counter_sample(tenant_id: str, agent_name: str, model: str, type_: str) -> float:
    value = REGISTRY.get_sample_value(
        "expert_work_llm_token_usage_total",
        {
            "tenant_id": tenant_id,
            "agent_name": agent_name,
            "model": model,
            "type": type_,
        },
    )
    return value or 0.0


async def _noop(_ctx: MiddlewareContext) -> None:
    return None


@pytest.mark.asyncio
async def test_persists_and_increments_for_basic_usage() -> None:
    store = InMemoryTokenUsageStore()
    mw = TokenUsageMiddleware(
        store=store,
        agent_name="g9-test-basic",
        agent_version="1.0.0",
        model="claude-sonnet-4-6",
    )
    tenant_id = uuid4()
    ctx = MiddlewareContext(
        payload={
            "tenant_id": tenant_id,
            "response": AIMessage(
                content="ok",
                usage_metadata={
                    "input_tokens": 500,
                    "output_tokens": 100,
                    "total_tokens": 600,
                },
            ),
        }
    )

    before_input = _counter_sample(str(tenant_id), "g9-test-basic", "claude-sonnet-4-6", "input")
    before_output = _counter_sample(str(tenant_id), "g9-test-basic", "claude-sonnet-4-6", "output")

    await mw(ctx, _noop)

    rows = list(await store.list_for_tenant(tenant_id=tenant_id))
    assert len(rows) == 1
    assert rows[0].input_tokens == 500
    assert rows[0].output_tokens == 100
    assert rows[0].cache_creation_tokens == 0

    assert (
        _counter_sample(str(tenant_id), "g9-test-basic", "claude-sonnet-4-6", "input")
        - before_input
        == 500
    )
    assert (
        _counter_sample(str(tenant_id), "g9-test-basic", "claude-sonnet-4-6", "output")
        - before_output
        == 100
    )


@pytest.mark.asyncio
async def test_records_user_id_for_per_user_cost() -> None:
    # Stream Agent-Templates (M1-5a) — the end-user threaded via payload is
    # persisted for per-user cost attribution.
    store = InMemoryTokenUsageStore()
    mw = TokenUsageMiddleware(store=store, agent_name="m5a", agent_version="1.0.0", model="m")
    tenant_id, user_id = uuid4(), uuid4()
    ctx = MiddlewareContext(
        payload={
            "tenant_id": tenant_id,
            "user_id": user_id,
            "response": AIMessage(
                content="ok",
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            ),
        }
    )
    await mw(ctx, _noop)
    rows = list(await store.list_for_tenant(tenant_id=tenant_id))
    assert rows[0].user_id == user_id


@pytest.mark.asyncio
async def test_user_id_absent_records_none() -> None:
    store = InMemoryTokenUsageStore()
    mw = TokenUsageMiddleware(store=store, agent_name="m5a-none", agent_version="1.0.0", model="m")
    tenant_id = uuid4()
    ctx = MiddlewareContext(
        payload={
            "tenant_id": tenant_id,
            "response": AIMessage(
                content="ok",
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            ),
        }
    )
    await mw(ctx, _noop)
    rows = list(await store.list_for_tenant(tenant_id=tenant_id))
    assert rows[0].user_id is None


@pytest.mark.asyncio
async def test_extracts_cache_counters_from_input_token_details() -> None:
    store = InMemoryTokenUsageStore()
    mw = TokenUsageMiddleware(
        store=store,
        agent_name="g9-test-cache",
        agent_version="1.0.0",
        model="claude-sonnet-4-6",
    )
    tenant_id = uuid4()
    ctx = MiddlewareContext(
        payload={
            "tenant_id": tenant_id,
            "response": AIMessage(
                content="ok",
                usage_metadata={
                    "input_tokens": 200,
                    "output_tokens": 50,
                    "total_tokens": 250,
                    "input_token_details": {
                        "cache_creation": 800,
                        "cache_read": 1200,
                    },
                },
            ),
        }
    )

    before_creation = _counter_sample(
        str(tenant_id), "g9-test-cache", "claude-sonnet-4-6", "cache_creation"
    )
    before_read = _counter_sample(
        str(tenant_id), "g9-test-cache", "claude-sonnet-4-6", "cache_read"
    )

    await mw(ctx, _noop)

    rows = list(await store.list_for_tenant(tenant_id=tenant_id))
    assert rows[0].cache_creation_tokens == 800
    assert rows[0].cache_read_tokens == 1200

    assert (
        _counter_sample(str(tenant_id), "g9-test-cache", "claude-sonnet-4-6", "cache_creation")
        - before_creation
        == 800
    )
    assert (
        _counter_sample(str(tenant_id), "g9-test-cache", "claude-sonnet-4-6", "cache_read")
        - before_read
        == 1200
    )


@pytest.mark.asyncio
async def test_no_persist_when_response_missing_usage() -> None:
    store = InMemoryTokenUsageStore()
    mw = TokenUsageMiddleware(
        store=store,
        agent_name="g9-no-usage",
        agent_version="1.0.0",
        model="claude-sonnet-4-6",
    )
    tenant_id = uuid4()
    ctx = MiddlewareContext(
        payload={
            "tenant_id": tenant_id,
            "response": AIMessage(content="ok"),
        }
    )
    await mw(ctx, _noop)
    assert list(await store.list_for_tenant(tenant_id=tenant_id)) == []


@pytest.mark.asyncio
async def test_cache_hit_records_a_zero_row_even_though_the_cached_message_has_no_usage() -> None:
    """B-66 —— E.13 缓存条目只存 content / tool_calls / id,读回来的 AIMessage **没有**
    ``usage_metadata``(这里走真缓存往返钉住这个前提)。命中时仍要落一行全 0:否则
    对外用量把「命中缓存、零上游开销」报成「无记录」(``_run_usage``:``[]`` vs ``None``)。
    未命中且没有用量的调用仍然不落行(上一条用例)。"""
    from expert_work.runtime.llm import InMemoryRedisCache, LLMResponseCache

    cache = LLMResponseCache(redis=InMemoryRedisCache())
    await cache.put(
        "k",
        AIMessage(
            content="cached",
            usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
        ),
    )
    cached = await cache.get("k")
    assert cached is not None
    assert cached.usage_metadata is None

    store = InMemoryTokenUsageStore()
    mw = TokenUsageMiddleware(
        store=store,
        agent_name="b66-cache-hit",
        agent_version="1.0.0",
        model="claude-sonnet-4-6",
    )
    tenant_id = uuid4()
    ctx = MiddlewareContext(payload={"tenant_id": tenant_id, "response": cached, "cache_hit": True})
    await mw(ctx, _noop)
    rows = list(await store.list_for_tenant(tenant_id=tenant_id))
    assert [
        (r.input_tokens, r.output_tokens, r.cache_creation_tokens, r.cache_read_tokens)
        for r in rows
    ] == [(0, 0, 0, 0)]
    assert rows[0].usage_kind == "conversation"


@pytest.mark.asyncio
async def test_no_persist_when_tenant_missing() -> None:
    store = InMemoryTokenUsageStore()
    mw = TokenUsageMiddleware(
        store=store,
        agent_name="g9-no-tenant",
        agent_version="1.0.0",
        model="m",
    )
    ctx = MiddlewareContext(
        payload={
            "response": AIMessage(
                content="ok",
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            ),
        }
    )
    await mw(ctx, _noop)
    # Without tenant_id, persistence is skipped entirely.
    assert list(await store.list_for_tenant(tenant_id=uuid4())) == []


@pytest.mark.asyncio
async def test_persist_failure_does_not_propagate() -> None:
    class _FailingStore(InMemoryTokenUsageStore):
        async def insert(self, record: TokenUsageRecord) -> TokenUsageRecord:
            raise RuntimeError("simulated DB outage")

    store = _FailingStore()
    mw = TokenUsageMiddleware(
        store=store,
        agent_name="g9-fail",
        agent_version="1.0.0",
        model="m",
    )
    tenant_id = uuid4()
    ctx = MiddlewareContext(
        payload={
            "tenant_id": tenant_id,
            "response": AIMessage(
                content="ok",
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            ),
        }
    )
    # Must not raise.
    await mw(ctx, _noop)


# ---------------------------------------------------------------------------
# Stream HX-1 — estimator drift counter (expert_work_ew_token_estimated_total)
# ---------------------------------------------------------------------------


def _estimated_sample(tenant_id: str, agent_name: str, model: str) -> float:
    value = REGISTRY.get_sample_value(
        "expert_work_ew_token_estimated_total",
        {"tenant_id": tenant_id, "agent_name": agent_name, "model": model},
    )
    return value or 0.0


class _FixedEstimator:
    """One token per character — keeps expected sums trivial."""

    def count(self, text: str) -> int:
        return len(text)


def _usage_ctx(
    tenant_id: UUID, *, prompt: list[Any] | None = None, cache_hit: bool = False
) -> MiddlewareContext:
    payload: dict[str, Any] = {
        "tenant_id": tenant_id,
        "response": AIMessage(
            content="ok",
            usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
        ),
    }
    if prompt is not None:
        payload["prompt_messages"] = prompt
    if cache_hit:
        payload["cache_hit"] = True
    return MiddlewareContext(payload=payload)


@pytest.mark.asyncio
async def test_hx1_drift_counter_accumulates_estimated_prompt_tokens() -> None:
    mw = TokenUsageMiddleware(
        store=InMemoryTokenUsageStore(),
        agent_name="hx1-drift",
        agent_version="1.0.0",
        model="m",
        estimator=_FixedEstimator(),
    )
    tenant_id = uuid4()
    prompt = [HumanMessage(content="abcd"), AIMessage(content="efghij")]
    before = _estimated_sample(str(tenant_id), "hx1-drift", "m")

    await mw(_usage_ctx(tenant_id, prompt=prompt), _noop)

    assert _estimated_sample(str(tenant_id), "hx1-drift", "m") - before == 10  # 4 + 6


@pytest.mark.asyncio
async def test_hx1_drift_counter_skipped_on_cache_hit_and_without_estimator() -> None:
    tenant_id = uuid4()
    prompt = [HumanMessage(content="abcd")]
    before = _estimated_sample(str(tenant_id), "hx1-skip", "m")

    with_est = TokenUsageMiddleware(
        store=InMemoryTokenUsageStore(),
        agent_name="hx1-skip",
        agent_version="1.0.0",
        model="m",
        estimator=_FixedEstimator(),
    )
    await with_est(_usage_ctx(tenant_id, prompt=prompt, cache_hit=True), _noop)

    without_est = TokenUsageMiddleware(
        store=InMemoryTokenUsageStore(),
        agent_name="hx1-skip",
        agent_version="1.0.0",
        model="m",
    )
    await without_est(_usage_ctx(tenant_id, prompt=prompt), _noop)

    assert _estimated_sample(str(tenant_id), "hx1-skip", "m") - before == 0


@pytest.mark.asyncio
async def test_hx1_drift_counter_failure_never_bubbles() -> None:
    class _Exploding:
        def count(self, text: str) -> int:
            raise RuntimeError("boom")

    mw = TokenUsageMiddleware(
        store=InMemoryTokenUsageStore(),
        agent_name="hx1-explode",
        agent_version="1.0.0",
        model="m",
        estimator=_Exploding(),
    )
    tenant_id = uuid4()
    # Must not raise — same never-fail contract as the G.9 counters.
    await mw(_usage_ctx(tenant_id, prompt=[HumanMessage(content="abcd")]), _noop)


@pytest.mark.asyncio
async def test_usage_kind_defaults_conversation_and_overrides() -> None:
    """SE-16 (SE-A43) — the persisted row carries the build's usage kind."""
    store = InMemoryTokenUsageStore()
    tenant_id = uuid4()

    def _ctx() -> MiddlewareContext:
        return MiddlewareContext(
            payload={
                "tenant_id": tenant_id,
                "response": AIMessage(
                    content="ok",
                    usage_metadata={"input_tokens": 5, "output_tokens": 1, "total_tokens": 6},
                ),
            }
        )

    default_mw = TokenUsageMiddleware(
        store=store, agent_name="kind-test", agent_version="1", model="m"
    )
    await default_mw(_ctx(), _noop)
    replay_mw = TokenUsageMiddleware(
        store=store,
        agent_name="kind-test",
        agent_version="1",
        model="m",
        usage_kind="skill_evolution",
    )
    await replay_mw(_ctx(), _noop)

    kinds = [r.usage_kind for r in await store.list_for_tenant(tenant_id=tenant_id)]
    assert sorted(kinds) == ["conversation", "skill_evolution"]
