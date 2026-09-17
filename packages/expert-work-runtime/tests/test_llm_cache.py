"""Unit tests for the LLM response cache — Stream E.13.

Covers :class:`LLMResponseCache`, :func:`is_cacheable`, and the
:class:`LLMCacheLookupMiddleware` / :class:`LLMCacheStoreMiddleware`
pair. Test matrix #25 (cross-tenant miss) and #26 (high-temperature
bypass) from STREAM-E-DESIGN § 5 are exercised here; #24 (full-graph
cache hit) lives in the orchestrator integration suite.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from expert_work.protocol import StructuredOutputSpec
from expert_work.runtime.llm import (
    InMemoryRedisCache,
    LLMResponseCache,
    is_cacheable,
)
from expert_work.runtime.middleware import (
    LLMCacheLookupMiddleware,
    LLMCacheStoreMiddleware,
    MiddlewareContext,
)

_TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
_MODEL = "claude-sonnet-4-6"


def _cache() -> LLMResponseCache:
    return LLMResponseCache(redis=InMemoryRedisCache())


async def _terminal(_ctx: MiddlewareContext) -> None:
    """No-op chain terminal."""


def _lookup_mw(cache: LLMResponseCache, *, temperature: float = 0.0) -> LLMCacheLookupMiddleware:
    return LLMCacheLookupMiddleware(
        cache=cache, model=_MODEL, temperature=temperature, max_tokens=4096
    )


def _store_mw(cache: LLMResponseCache, *, temperature: float = 0.0) -> LLMCacheStoreMiddleware:
    return LLMCacheStoreMiddleware(
        cache=cache, model=_MODEL, temperature=temperature, max_tokens=4096
    )


# ---------------------------------------------------------------------------
# is_cacheable
# ---------------------------------------------------------------------------


def test_cacheable_plain_prompt_low_temperature() -> None:
    assert is_cacheable([HumanMessage(content="hi")], temperature=0.0) is True
    assert is_cacheable([HumanMessage(content="hi")], temperature=0.1) is True


def test_not_cacheable_high_temperature() -> None:
    """Test matrix #26 — temperature above the ceiling is non-deterministic."""
    assert is_cacheable([HumanMessage(content="hi")], temperature=0.5) is False
    assert is_cacheable([HumanMessage(content="hi")], temperature=0.11) is False


def test_not_cacheable_empty_messages() -> None:
    assert is_cacheable([], temperature=0.0) is False


def test_not_cacheable_with_tool_message() -> None:
    msgs = [HumanMessage(content="hi"), ToolMessage(content="result", tool_call_id="t1")]
    assert is_cacheable(msgs, temperature=0.0) is False


def test_not_cacheable_with_ai_tool_calls() -> None:
    ai = AIMessage(
        content="",
        tool_calls=[{"id": "t1", "name": "search", "args": {}, "type": "tool_call"}],
    )
    assert is_cacheable([HumanMessage(content="hi"), ai], temperature=0.0) is False


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


def _key(
    cache: LLMResponseCache,
    *,
    tenant_id: UUID = _TENANT_A,
    model: str = _MODEL,
    messages: Sequence[BaseMessage] | None = None,
    temperature: float = 0.0,
    max_tokens: int = 4096,
) -> str:
    """Typed wrapper over ``make_key`` with defaults — lets each test
    override exactly one field without ``**dict`` unpacking (which mypy
    strict rejects against the keyword-only signature)."""
    return cache.make_key(
        tenant_id=tenant_id,
        model=model,
        messages=[HumanMessage(content="hello")] if messages is None else messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def test_make_key_is_deterministic() -> None:
    cache = _cache()
    assert _key(cache) == _key(cache)


def test_make_key_differs_by_tenant() -> None:
    """Test matrix #25 root cause — tenant is part of the key."""
    cache = _cache()
    key_a = _key(cache, tenant_id=_TENANT_A)
    key_b = _key(cache, tenant_id=_TENANT_B)
    assert key_a != key_b
    assert str(_TENANT_A) in key_a
    assert str(_TENANT_B) in key_b


def test_make_key_differs_by_model_and_messages_and_params() -> None:
    cache = _cache()
    key = _key(cache)
    assert _key(cache, model="gpt-4o") != key
    assert _key(cache, messages=[HumanMessage(content="world")]) != key
    assert _key(cache, temperature=0.1) != key
    assert _key(cache, max_tokens=2048) != key


def test_make_key_ignores_message_id() -> None:
    """Two prompts differing only by message id must hit the same entry."""
    cache = _cache()
    m1 = HumanMessage(content="hello")
    m1.id = "id-1"
    m2 = HumanMessage(content="hello")
    m2.id = "id-2"
    key1 = cache.make_key(
        tenant_id=_TENANT_A, model=_MODEL, messages=[m1], temperature=0.0, max_tokens=4096
    )
    key2 = cache.make_key(
        tenant_id=_TENANT_A, model=_MODEL, messages=[m2], temperature=0.0, max_tokens=4096
    )
    assert key1 == key2


# ---------------------------------------------------------------------------
# Key derivation with output_schema (Stream RT-1 PR-2)
# ---------------------------------------------------------------------------


_SPEC = StructuredOutputSpec(
    schema={"type": "object", "properties": {"score": {"type": "integer"}}},
    name="verdict",
)


def test_make_key_without_schema_is_byte_identical_to_pre_rt1() -> None:
    """``output_schema=None`` keys must equal the pre-RT-1 derivation.

    The expected key is recomputed here from the original canonical
    form — if this ever breaks, every existing cache entry silently
    becomes unreachable (a full cache flush on deploy).
    """
    cache = _cache()
    canonical = json.dumps(
        {
            "tenant_id": str(_TENANT_A),
            "model": _MODEL,
            "messages": [{"type": "human", "content": "hello"}],
            "temperature": 0.0,
            "max_tokens": 4096,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    expected = f"llm:cache:{_TENANT_A}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
    assert _key(cache) == expected
    assert (
        cache.make_key(
            tenant_id=_TENANT_A,
            model=_MODEL,
            messages=[HumanMessage(content="hello")],
            temperature=0.0,
            max_tokens=4096,
            output_schema=None,
        )
        == expected
    )


def test_make_key_with_schema_differs_from_unstructured() -> None:
    """A structured call must never hit an unstructured entry for the
    same prompt (PR-1 known interaction)."""
    cache = _cache()
    structured = cache.make_key(
        tenant_id=_TENANT_A,
        model=_MODEL,
        messages=[HumanMessage(content="hello")],
        temperature=0.0,
        max_tokens=4096,
        output_schema=_SPEC,
    )
    assert structured != _key(cache)


def test_make_key_schema_fingerprint_covers_name_and_content() -> None:
    cache = _cache()

    def _with(spec: StructuredOutputSpec) -> str:
        return cache.make_key(
            tenant_id=_TENANT_A,
            model=_MODEL,
            messages=[HumanMessage(content="hello")],
            temperature=0.0,
            max_tokens=4096,
            output_schema=spec,
        )

    base = _with(_SPEC)
    # Same name + same schema content → same key (deterministic).
    assert _with(StructuredOutputSpec(schema=dict(_SPEC.schema), name="verdict")) == base
    # Different schema content under the same name → different key.
    other_schema = StructuredOutputSpec(
        schema={"type": "object", "properties": {"label": {"type": "string"}}},
        name="verdict",
    )
    assert _with(other_schema) != base
    # Same schema content under a different name → different key.
    assert _with(StructuredOutputSpec(schema=dict(_SPEC.schema), name="other")) != base
    # RT-1 PR-3 — ``strict`` joins the fingerprint: on the native path it
    # toggles wire-level enforcement, so responses are not interchangeable.
    relaxed = StructuredOutputSpec(schema=dict(_SPEC.schema), name="verdict", strict=False)
    assert _with(relaxed) != base


# ---------------------------------------------------------------------------
# get / put round trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_then_get_round_trip() -> None:
    cache = _cache()
    key = "llm:cache:test:abc"
    await cache.put(key, AIMessage(content="cached answer"))
    got = await cache.get(key)
    assert got is not None
    assert got.content == "cached answer"


@pytest.mark.asyncio
async def test_get_miss_returns_none() -> None:
    cache = _cache()
    assert await cache.get("llm:cache:test:missing") is None


@pytest.mark.asyncio
async def test_round_trip_preserves_tool_calls() -> None:
    cache = _cache()
    response = AIMessage(
        content="",
        tool_calls=[{"id": "t1", "name": "search", "args": {"q": "x"}, "type": "tool_call"}],
    )
    await cache.put("llm:cache:test:tc", response)
    got = await cache.get("llm:cache:test:tc")
    assert got is not None
    assert got.tool_calls == [
        {"id": "t1", "name": "search", "args": {"q": "x"}, "type": "tool_call"}
    ]


@pytest.mark.asyncio
async def test_corrupt_entry_treated_as_miss() -> None:
    """A cache must never crash the call path — bad bytes → miss."""
    redis = InMemoryRedisCache()
    redis.store["llm:cache:test:bad"] = b"{not valid json"
    cache = LLMResponseCache(redis=redis)
    assert await cache.get("llm:cache:test:bad") is None


@pytest.mark.asyncio
async def test_put_uses_default_ttl() -> None:
    """``put`` without explicit ttl falls back to the cache default."""
    captured: dict[str, int | None] = {}

    class _RecordingRedis:
        async def get(self, key: str) -> bytes | None:
            del key
            return None

        async def set(self, key: str, value: bytes, ex: int | None = None) -> None:
            del key, value
            captured["ex"] = ex

    cache = LLMResponseCache(redis=_RecordingRedis(), default_ttl_s=1800)
    await cache.put("k", AIMessage(content="x"))
    assert captured["ex"] == 1800
    await cache.put("k", AIMessage(content="x"), ttl_s=99)
    assert captured["ex"] == 99


# ---------------------------------------------------------------------------
# LLMCacheLookupMiddleware
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_miss_leaves_payload_clean() -> None:
    cache = _cache()
    mw = _lookup_mw(cache)
    ctx = MiddlewareContext(
        payload={"messages": [HumanMessage(content="hi")], "tenant_id": _TENANT_A}
    )
    await mw(ctx, _terminal)
    assert "llm_cache_hit" not in ctx.payload


@pytest.mark.asyncio
async def test_lookup_hit_sets_payload() -> None:
    cache = _cache()
    lookup = _lookup_mw(cache)
    messages = [HumanMessage(content="hi")]
    key = cache.make_key(
        tenant_id=_TENANT_A,
        model=_MODEL,
        messages=messages,
        temperature=0.0,
        max_tokens=4096,
    )
    await cache.put(key, AIMessage(content="from-cache"))

    ctx = MiddlewareContext(payload={"messages": messages, "tenant_id": _TENANT_A})
    await lookup(ctx, _terminal)
    hit = ctx.payload.get("llm_cache_hit")
    assert isinstance(hit, AIMessage)
    assert hit.content == "from-cache"


@pytest.mark.asyncio
async def test_lookup_skipped_without_tenant_id() -> None:
    cache = _cache()
    mw = _lookup_mw(cache)
    ctx = MiddlewareContext(payload={"messages": [HumanMessage(content="hi")]})
    await mw(ctx, _terminal)
    assert "llm_cache_hit" not in ctx.payload


@pytest.mark.asyncio
async def test_lookup_skipped_for_high_temperature() -> None:
    """Test matrix #26 at the middleware boundary."""
    cache = _cache()
    lookup = _lookup_mw(cache, temperature=0.7)
    messages = [HumanMessage(content="hi")]
    # Even if an entry somehow exists under a 0.7-temp key, lookup
    # never runs because is_cacheable rejects temperature 0.7.
    ctx = MiddlewareContext(payload={"messages": messages, "tenant_id": _TENANT_A})
    await lookup(ctx, _terminal)
    assert "llm_cache_hit" not in ctx.payload


# ---------------------------------------------------------------------------
# LLMCacheStoreMiddleware
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_persists_fresh_response() -> None:
    cache = _cache()
    store = _store_mw(cache)
    messages = [HumanMessage(content="hi")]
    ctx = MiddlewareContext(
        payload={
            "prompt_messages": messages,
            "response": AIMessage(content="answer"),
            "tenant_id": _TENANT_A,
            "cache_hit": False,
        }
    )
    await store(ctx, _terminal)

    key = cache.make_key(
        tenant_id=_TENANT_A,
        model=_MODEL,
        messages=messages,
        temperature=0.0,
        max_tokens=4096,
    )
    got = await cache.get(key)
    assert got is not None
    assert got.content == "answer"


@pytest.mark.asyncio
async def test_store_skips_when_cache_hit() -> None:
    """A turn served from cache must not be re-stored."""
    redis = InMemoryRedisCache()
    cache = LLMResponseCache(redis=redis)
    store = _store_mw(cache)
    ctx = MiddlewareContext(
        payload={
            "prompt_messages": [HumanMessage(content="hi")],
            "response": AIMessage(content="answer"),
            "tenant_id": _TENANT_A,
            "cache_hit": True,
        }
    )
    await store(ctx, _terminal)
    assert redis.store == {}


@pytest.mark.asyncio
async def test_store_skips_when_not_cacheable() -> None:
    redis = InMemoryRedisCache()
    cache = LLMResponseCache(redis=redis)
    store = _store_mw(cache)
    ctx = MiddlewareContext(
        payload={
            "prompt_messages": [
                HumanMessage(content="hi"),
                ToolMessage(content="r", tool_call_id="t1"),
            ],
            "response": AIMessage(content="answer"),
            "tenant_id": _TENANT_A,
            "cache_hit": False,
        }
    )
    await store(ctx, _terminal)
    assert redis.store == {}


# ---------------------------------------------------------------------------
# Cross-tenant isolation — test matrix #25
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_no_hit() -> None:
    """Tenant A stores, tenant B reads the same prompt → miss."""
    cache = _cache()
    messages = [HumanMessage(content="shared prompt")]

    store = _store_mw(cache)
    await store(
        MiddlewareContext(
            payload={
                "prompt_messages": messages,
                "response": AIMessage(content="tenant-A answer"),
                "tenant_id": _TENANT_A,
                "cache_hit": False,
            }
        ),
        _terminal,
    )

    lookup = _lookup_mw(cache)
    ctx_b = MiddlewareContext(payload={"messages": messages, "tenant_id": _TENANT_B})
    await lookup(ctx_b, _terminal)
    assert "llm_cache_hit" not in ctx_b.payload

    # ...and tenant A *does* hit.
    ctx_a = MiddlewareContext(payload={"messages": messages, "tenant_id": _TENANT_A})
    await lookup(ctx_a, _terminal)
    assert isinstance(ctx_a.payload.get("llm_cache_hit"), AIMessage)


@pytest.mark.asyncio
async def test_store_then_lookup_round_trip() -> None:
    """End-to-end at the middleware layer: store-mw writes, lookup-mw
    reads back the identical response."""
    cache = _cache()
    messages = [HumanMessage(content="round trip")]
    tenant = uuid4()

    await _store_mw(cache)(
        MiddlewareContext(
            payload={
                "prompt_messages": messages,
                "response": AIMessage(content="the answer"),
                "tenant_id": tenant,
                "cache_hit": False,
            }
        ),
        _terminal,
    )

    ctx = MiddlewareContext(payload={"messages": messages, "tenant_id": tenant})
    await _lookup_mw(cache)(ctx, _terminal)
    hit = ctx.payload.get("llm_cache_hit")
    assert isinstance(hit, AIMessage)
    assert hit.content == "the answer"


@pytest.mark.asyncio
async def test_lookup_bypass_skips_a_real_hit_and_the_fresh_answer_wins_next_time() -> None:
    """B-66 —— ``:regenerate`` 重放的 prompt 与原轮字节相同,带 ``llm_cache_bypass`` 的
    调用必须**不查**缓存(否则必然命中、把旧答案原样端回来);store 照常写,于是之后
    同样的普通调用拿到的是重新生成出来的新答案。"""
    cache = _cache()
    messages = [HumanMessage(content="regenerate me")]
    tenant = uuid4()

    async def store(answer: str) -> None:
        await _store_mw(cache)(
            MiddlewareContext(
                payload={
                    "prompt_messages": messages,
                    "response": AIMessage(content=answer),
                    "tenant_id": tenant,
                    "cache_hit": False,
                }
            ),
            _terminal,
        )

    await store("old answer")

    bypassed = MiddlewareContext(
        payload={"messages": messages, "tenant_id": tenant, "llm_cache_bypass": True}
    )
    await _lookup_mw(cache)(bypassed, _terminal)
    assert "llm_cache_hit" not in bypassed.payload

    # 显式 False 与缺省同义:照常命中。
    not_bypassed = MiddlewareContext(
        payload={"messages": messages, "tenant_id": tenant, "llm_cache_bypass": False}
    )
    await _lookup_mw(cache)(not_bypassed, _terminal)
    hit = not_bypassed.payload.get("llm_cache_hit")
    assert isinstance(hit, AIMessage) and hit.content == "old answer"

    await store("fresh answer")
    ctx = MiddlewareContext(payload={"messages": messages, "tenant_id": tenant})
    await _lookup_mw(cache)(ctx, _terminal)
    latest = ctx.payload.get("llm_cache_hit")
    assert isinstance(latest, AIMessage) and latest.content == "fresh answer"


# ---------------------------------------------------------------------------
# output_schema keying at the middleware layer (Stream RT-1 PR-2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_structured_lookup_does_not_hit_unstructured_entry() -> None:
    """Same messages, unstructured entry stored → structured lookup misses."""
    cache = _cache()
    messages = [HumanMessage(content="rate this")]

    await _store_mw(cache)(
        MiddlewareContext(
            payload={
                "prompt_messages": messages,
                "response": AIMessage(content="unstructured answer"),
                "tenant_id": _TENANT_A,
                "cache_hit": False,
            }
        ),
        _terminal,
    )

    ctx = MiddlewareContext(
        payload={"messages": messages, "tenant_id": _TENANT_A, "output_schema": _SPEC}
    )
    await _lookup_mw(cache)(ctx, _terminal)
    assert "llm_cache_hit" not in ctx.payload


@pytest.mark.asyncio
async def test_structured_store_then_lookup_round_trip() -> None:
    """Structured store + structured lookup with the same spec → hit;
    the unstructured lookup for the same messages still misses."""
    cache = _cache()
    messages = [HumanMessage(content="rate this")]

    await _store_mw(cache)(
        MiddlewareContext(
            payload={
                "prompt_messages": messages,
                "response": AIMessage(content='{"score": 5}'),
                "tenant_id": _TENANT_A,
                "cache_hit": False,
                "output_schema": _SPEC,
            }
        ),
        _terminal,
    )

    structured_ctx = MiddlewareContext(
        payload={"messages": messages, "tenant_id": _TENANT_A, "output_schema": _SPEC}
    )
    await _lookup_mw(cache)(structured_ctx, _terminal)
    hit = structured_ctx.payload.get("llm_cache_hit")
    assert isinstance(hit, AIMessage)
    assert hit.content == '{"score": 5}'

    plain_ctx = MiddlewareContext(payload={"messages": messages, "tenant_id": _TENANT_A})
    await _lookup_mw(cache)(plain_ctx, _terminal)
    assert "llm_cache_hit" not in plain_ctx.payload


@pytest.mark.asyncio
async def test_non_spec_output_schema_payload_is_ignored() -> None:
    """A summary dict (the router's around-payload shape) is NOT a spec —
    the key must fall back to the unstructured derivation."""
    cache = _cache()
    messages = [HumanMessage(content="rate this")]

    await _store_mw(cache)(
        MiddlewareContext(
            payload={
                "prompt_messages": messages,
                "response": AIMessage(content="plain answer"),
                "tenant_id": _TENANT_A,
                "cache_hit": False,
            }
        ),
        _terminal,
    )

    ctx = MiddlewareContext(
        payload={
            "messages": messages,
            "tenant_id": _TENANT_A,
            "output_schema": {"name": "verdict", "strict": True},
        }
    )
    await _lookup_mw(cache)(ctx, _terminal)
    hit = ctx.payload.get("llm_cache_hit")
    assert isinstance(hit, AIMessage)
    assert hit.content == "plain answer"


# ---------------------------------------------------------------------------
# Protocol contract
# ---------------------------------------------------------------------------


def test_middlewares_satisfy_protocol() -> None:
    from expert_work.runtime.middleware import Middleware

    cache = _cache()
    assert isinstance(_lookup_mw(cache), Middleware)
    assert isinstance(_store_mw(cache), Middleware)


def test_lookup_anchor_is_before_llm_call() -> None:
    assert _lookup_mw(_cache()).anchor == "before_llm_call"


def test_store_anchor_is_after_llm_call() -> None:
    assert _store_mw(_cache()).anchor == "after_llm_call"
