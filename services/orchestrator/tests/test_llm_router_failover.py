"""B-297 — LLMRouter fast failover: ``has_fallback`` hint, per-run stickiness,
breaker key, failover metric, and the end-to-end behaviour with the real E.4
middleware (fake sleeper, no real waits)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from prometheus_client import REGISTRY

from expert_work.common.context import reset_current_run_id, set_current_run_id
from expert_work.runtime.middleware import (
    BreakerRegistry,
    CallNext,
    LLMClientError,
    LLMErrorHandlingMiddleware,
    LLMRateLimitError,
    LLMServerError,
    MiddlewareChain,
    MiddlewareContext,
)
from orchestrator.llm import AllProvidersExhaustedError, LLMRouter, ProviderHandle
from orchestrator.llm.failover import StickyRouteStore, breaker_key_for, plan_walk
from orchestrator.tools.registry import ToolSpec


@dataclass
class _Provider:
    """Raises ``fail_with`` while it is set, else answers with ``name``."""

    name: str
    fail_with: BaseException | None = None
    calls: int = 0

    async def complete(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return AIMessage(content=self.name)


@dataclass
class _Spy:
    """around_llm_call middleware recording selected payload fields."""

    seen: list[dict[str, object]] = field(default_factory=list)
    name: str = "spy"
    anchor: str = "around_llm_call"
    after: tuple[str, ...] = ()
    before: tuple[str, ...] = ()

    async def __call__(self, ctx: MiddlewareContext, call_next: CallNext) -> None:
        self.seen.append(
            {
                k: ctx.payload.get(k)
                for k in ("provider_key", "breaker_key", "has_fallback", "has_alternative")
            }
        )
        await call_next(ctx)


def _router(
    providers: list[tuple[_Provider, str, str]],
    *,
    store: StickyRouteStore | None = None,
    chain: MiddlewareChain | None = None,
) -> LLMRouter:
    return LLMRouter(
        providers=[ProviderHandle(provider=p, key=k, group=g) for p, k, g in providers],
        around_llm_chain=chain,
        sticky_store=store if store is not None else StickyRouteStore(),
    )


async def _call(router: LLMRouter) -> str:
    result = await router(messages=[HumanMessage(content="hi")], tools=[])
    return str(result.content)


@pytest.fixture
def run_id() -> Iterator[UUID]:
    rid = uuid4()
    token = set_current_run_id(rid)
    yield rid
    reset_current_run_id(token)


def _spy_chain() -> tuple[_Spy, MiddlewareChain]:
    spy = _Spy()
    return spy, MiddlewareChain.from_middlewares("around_llm_call", [spy])


# ---------------------------------------------------------------------------
# has_fallback hint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_has_fallback_true_only_while_another_group_follows() -> None:
    spy, chain = _spy_chain()
    a = _Provider("a", fail_with=LLMServerError("down"))
    b = _Provider("b")
    router = _router([(a, "a:m", "a:m"), (b, "b:m", "b:m")], chain=chain)
    assert await _call(router) == "b"
    assert [s["has_fallback"] for s in spy.seen] == [True, False]


@pytest.mark.asyncio
async def test_sibling_keys_do_not_count_as_fallback() -> None:
    spy, chain = _spy_chain()
    k0 = _Provider("k0", fail_with=LLMRateLimitError("429"))
    k1 = _Provider("k1")
    router = _router([(k0, "a:m#0", "a:m"), (k1, "a:m#1", "a:m")], chain=chain)
    assert await _call(router) == "k1"
    assert [s["has_fallback"] for s in spy.seen] == [False, False]


@pytest.mark.asyncio
async def test_breaker_key_in_payload_when_handle_carries_one() -> None:
    spy, chain = _spy_chain()
    router = LLMRouter(
        providers=[ProviderHandle(provider=_Provider("a"), key="a:m", breaker_key="a:m@f00")],
        around_llm_chain=chain,
    )
    await _call(router)
    assert spy.seen[0]["breaker_key"] == "a:m@f00"
    assert spy.seen[0]["provider_key"] == "a:m"


# ---------------------------------------------------------------------------
# Per-run stickiness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_sticks_to_the_group_that_served_after_a_failover(run_id: UUID) -> None:
    a = _Provider("a", fail_with=LLMServerError("down"))
    b = _Provider("b")
    router = _router([(a, "a:m", "a:m"), (b, "b:m", "b:m")])
    assert await _call(router) == "b"
    assert a.calls == 1
    assert await _call(router) == "b"
    assert await _call(router) == "b"
    assert a.calls == 1, "later calls of the run must not start on the failed primary"


@pytest.mark.asyncio
async def test_new_run_starts_at_the_primary_again() -> None:
    store = StickyRouteStore()
    a = _Provider("a", fail_with=LLMServerError("down"))
    b = _Provider("b")
    router = _router([(a, "a:m", "a:m"), (b, "b:m", "b:m")], store=store)
    token = set_current_run_id(uuid4())
    try:
        await _call(router)
        await _call(router)
    finally:
        reset_current_run_id(token)
    assert a.calls == 1
    a.fail_with = None
    token = set_current_run_id(uuid4())
    try:
        assert await _call(router) == "a"
    finally:
        reset_current_run_id(token)


@pytest.mark.asyncio
async def test_no_run_id_means_no_stickiness() -> None:
    a = _Provider("a", fail_with=LLMServerError("down"))
    b = _Provider("b")
    router = _router([(a, "a:m", "a:m"), (b, "b:m", "b:m")])
    await _call(router)
    await _call(router)
    assert a.calls == 2


@pytest.mark.asyncio
async def test_sticky_group_failing_moves_on_then_last_resort(run_id: UUID) -> None:
    spy, chain = _spy_chain()
    a = _Provider("a", fail_with=LLMServerError("down"))
    b = _Provider("b")
    router = _router([(a, "a:m", "a:m"), (b, "b:m", "b:m")], chain=chain)
    assert await _call(router) == "b"
    # Now the backup breaks and the primary has recovered: B first (sticky),
    # then the skipped primary as the last resort.
    b.fail_with = LLMServerError("down too")
    a.fail_with = None
    spy.seen.clear()
    assert await _call(router) == "a"
    assert [s["provider_key"] for s in spy.seen] == ["b:m", "a:m"]
    # B was the last healthy option → full budget; A is the last-resort pass →
    # fast budget.
    assert [s["has_fallback"] for s in spy.seen] == [False, True]
    # Served by the primary again → stickiness cleared.
    spy.seen.clear()
    assert await _call(router) == "a"
    assert [s["provider_key"] for s in spy.seen] == ["a:m"]


@pytest.mark.asyncio
async def test_sticky_moves_down_the_chain(run_id: UUID) -> None:
    a = _Provider("a", fail_with=LLMServerError("down"))
    b = _Provider("b")
    c = _Provider("c")
    router = _router([(a, "a:m", "a:m"), (b, "b:m", "b:m"), (c, "c:m", "c:m")])
    assert await _call(router) == "b"
    b.fail_with = LLMServerError("down")
    assert await _call(router) == "c"
    calls = (a.calls, b.calls)
    assert await _call(router) == "c"
    assert (a.calls, b.calls) == calls


@pytest.mark.asyncio
async def test_all_failing_still_raises_exhausted(run_id: UUID) -> None:
    a = _Provider("a", fail_with=LLMServerError("down"))
    b = _Provider("b")
    router = _router([(a, "a:m", "a:m"), (b, "b:m", "b:m")])
    await _call(router)
    b.fail_with = LLMServerError("down")
    with pytest.raises(AllProvidersExhaustedError):
        await _call(router)
    assert a.calls == 2  # the last-resort pass did try it


@pytest.mark.asyncio
async def test_client_error_does_not_change_stickiness(run_id: UUID) -> None:
    a = _Provider("a", fail_with=LLMClientError("400"))
    b = _Provider("b")
    router = _router([(a, "a:m", "a:m"), (b, "b:m", "b:m")])
    with pytest.raises(LLMClientError):
        await _call(router)
    a.fail_with = None
    assert await _call(router) == "a"


@pytest.mark.asyncio
async def test_stickiness_is_per_chain(run_id: UUID) -> None:
    store = StickyRouteStore()
    a = _Provider("a", fail_with=LLMServerError("down"))
    b = _Provider("b")
    main = _router([(a, "a:m", "a:m"), (b, "b:m", "b:m")], store=store)
    await _call(main)
    # A different chain in the same run (e.g. a step router) is unaffected.
    a2 = _Provider("a2")
    other = _router([(a2, "a:m", "a:m"), (_Provider("x"), "x:m", "x:m")], store=store)
    assert await _call(other) == "a2"
    # A second router instance with the same chain (rebuilt agent) shares it.
    a3 = _Provider("a3")
    same = _router([(a3, "a:m", "a:m"), (_Provider("b3"), "b:m", "b:m")], store=store)
    assert await _call(same) == "b3"
    assert a3.calls == 0


@pytest.mark.asyncio
async def test_failover_metric_counts_transitions_and_sticky_starts(run_id: UUID) -> None:
    def _value(reason: str) -> float:
        v = REGISTRY.get_sample_value(
            "expert_work_llm_failover_total",
            {"from_group": "mx:a", "to_group": "mx:b", "reason": reason},
        )
        return v or 0.0

    before_err, before_sticky = _value("server_error"), _value("sticky")
    a = _Provider("a", fail_with=LLMServerError("down"))
    router = _router([(a, "mx:a", "mx:a"), (_Provider("b"), "mx:b", "mx:b")])
    await _call(router)
    await _call(router)
    assert _value("server_error") == before_err + 1
    assert _value("sticky") == before_sticky + 1


# ---------------------------------------------------------------------------
# End to end with the real E.4 middleware
# ---------------------------------------------------------------------------


async def _no_sleep(_s: float) -> None:
    return None


def _real_chain(registry: BreakerRegistry) -> MiddlewareChain:
    return MiddlewareChain.from_middlewares(
        "around_llm_call",
        [LLMErrorHandlingMiddleware(breaker_registry=registry, sleeper=_no_sleep)],
    )


@pytest.mark.asyncio
async def test_dead_primary_costs_two_attempts_then_breaker_skips_it() -> None:
    registry = BreakerRegistry()
    a = _Provider("a", fail_with=LLMServerError("503"))
    b = _Provider("b")
    router = _router([(a, "a:m", "a:m"), (b, "b:m", "b:m")], chain=_real_chain(registry))
    assert await _call(router) == "b"
    assert a.calls == 2  # 1 attempt + 1 fast retry (was 4 before B-297)
    assert await _call(router) == "b"
    assert a.calls == 3  # 3rd consecutive failure opens the breaker, no retry
    assert await _call(router) == "b"
    assert a.calls == 3  # breaker OPEN → skipped without an upstream hit
    assert await (await registry.get("a:m")).check_state() == "OPEN"


@pytest.mark.asyncio
async def test_single_model_chain_keeps_full_retry_budget() -> None:
    registry = BreakerRegistry()
    flaky = _Provider("only", fail_with=LLMServerError("503"))
    router = _router([(flaky, "o:m", "o:m")], chain=_real_chain(registry))
    with pytest.raises(AllProvidersExhaustedError):
        await _call(router)
    assert flaky.calls == 4


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


def test_plan_walk_rotates_from_sticky_group() -> None:
    plan = plan_walk(["a", "a", "b", "c"], "b")
    assert plan.order == (2, 3, 0, 1)
    assert plan.boundary == 2
    assert plan.next_group(0) == 1  # b → c
    assert plan.next_group(1) == 2  # c → the last-resort a's
    assert plan.next_group(2) == 4  # a#0's sibling a#1 is skipped
    assert [plan.has_fallback(p) for p in range(4)] == [True, False, True, True]


def test_plan_walk_ignores_unknown_or_primary_sticky() -> None:
    assert plan_walk(["a", "b"], "zz").order == (0, 1)
    assert plan_walk(["a", "b"], "a").order == (0, 1)
    assert plan_walk(["a", "b"], None).order == (0, 1)


@dataclass
class _Clock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


def test_sticky_store_idle_ttl() -> None:
    clock = _Clock()
    store = StickyRouteStore(ttl_s=100.0, clock=clock)
    rid = uuid4()
    store.set(rid, ("a", "b"), "b")
    clock.now = 99.0
    assert store.get(rid, ("a", "b")) == "b"  # read refreshes the TTL
    clock.now = 198.0
    assert store.get(rid, ("a", "b")) == "b"
    clock.now = 299.0
    assert store.get(rid, ("a", "b")) is None
    assert len(store) == 0


def test_sticky_store_size_cap_evicts_least_recent() -> None:
    store = StickyRouteStore(max_entries=3)
    rids = [uuid4() for _ in range(4)]
    for rid in rids[:3]:
        store.set(rid, ("a",), "b")
    assert store.get(rids[0], ("a",)) == "b"  # touch → most recent
    store.set(rids[3], ("a",), "b")
    assert len(store) == 3
    assert store.get(rids[1], ("a",)) is None
    assert store.get(rids[0], ("a",)) == "b"


def test_sticky_store_expired_entries_evicted_on_write() -> None:
    clock = _Clock()
    store = StickyRouteStore(ttl_s=10.0, clock=clock)
    store.set(uuid4(), ("a",), "b")
    clock.now = 11.0
    store.set(uuid4(), ("a",), "b")
    assert len(store) == 1


def test_breaker_key_fingerprint_separates_credentials_and_endpoints() -> None:
    k1 = breaker_key_for("deepseek:m", "secret://platform/deepseek", None)
    k2 = breaker_key_for("deepseek:m", "secret://tenant-override/deepseek", None)
    k3 = breaker_key_for("self-hosted:m", "secret://x", "http://host-a")
    k4 = breaker_key_for("self-hosted:m", "secret://x", "http://host-b")
    assert k1.startswith("deepseek:m@")
    assert k1 != k2
    assert k3 != k4
    assert k1 == breaker_key_for("deepseek:m", "secret://platform/deepseek", None)
    assert "secret" not in k1
