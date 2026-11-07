"""B-297 review fixes — router + real E.4 middleware:

* sibling keys (Y-MK): an OPEN breaker on key #0 is skipped for key #1, and a
  429 is not retried in place while another key / group can serve;
* the first-token deadline is capped (60 s, 120 s for large contexts) only
  when a backup group exists and the call streams;
* a half-open probe resolves at its first progress delta;
* the last-resort pass keeps the real upstream error as the exhausted cause.

Fake sleepers / patched timers — no real waits.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from expert_work.common.context import reset_current_run_id, set_current_run_id
from expert_work.runtime.middleware import (
    BreakerRegistry,
    CallNext,
    LLMErrorHandlingMiddleware,
    LLMKeyUnavailableError,
    LLMRateLimitError,
    LLMServerError,
    MiddlewareChain,
    MiddlewareContext,
)
from orchestrator.llm import AllProvidersExhaustedError, LLMRouter, ProviderHandle
from orchestrator.llm import router as router_module
from orchestrator.llm.failover import (
    FALLBACK_FIRST_TOKEN_TIMEOUT_LARGE_CONTEXT_S,
    FALLBACK_FIRST_TOKEN_TIMEOUT_S,
    StickyRouteStore,
)
from orchestrator.llm.providers._streaming import LLMDelta
from orchestrator.tools.registry import ToolSpec


@dataclass
class _Provider:
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
class _Sleeps:
    seen: list[float] = field(default_factory=list)

    async def __call__(self, delay: float) -> None:
        self.seen.append(delay)


def _real_chain(registry: BreakerRegistry, sleeps: _Sleeps) -> MiddlewareChain:
    return MiddlewareChain.from_middlewares(
        "around_llm_call",
        [LLMErrorHandlingMiddleware(breaker_registry=registry, sleeper=sleeps)],
    )


def _router(
    handles: list[tuple[Any, str, str]],
    *,
    chain: MiddlewareChain | None = None,
    store: StickyRouteStore | None = None,
    **kwargs: Any,
) -> LLMRouter:
    return LLMRouter(
        providers=[ProviderHandle(provider=p, key=k, group=g) for p, k, g in handles],
        around_llm_chain=chain,
        sticky_store=store if store is not None else StickyRouteStore(),
        **kwargs,
    )


async def _call(router: LLMRouter) -> str:
    return str((await router(messages=[HumanMessage(content="hi")], tools=[])).content)


async def _force_open(registry: BreakerRegistry, key: str) -> None:
    breaker = await registry.get(key)
    for _ in range(3):
        await breaker.acquire()
        await breaker.record_failure()


@pytest.fixture
def run_id() -> Iterator[UUID]:
    rid = uuid4()
    token = set_current_run_id(rid)
    yield rid
    reset_current_run_id(token)


# ---------------------------------------------------------------------------
# Sibling keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_sibling_key_is_skipped_for_the_next_key() -> None:
    registry, sleeps = BreakerRegistry(), _Sleeps()
    await _force_open(registry, "a:m#0")
    k0, k1 = _Provider("k0"), _Provider("k1")
    router = _router(
        [(k0, "a:m#0", "a:m"), (k1, "a:m#1", "a:m")], chain=_real_chain(registry, sleeps)
    )
    for _ in range(5):
        assert await _call(router) == "k1"
    assert k0.calls == 0
    assert sleeps.seen == []


@pytest.mark.parametrize("exc", [LLMRateLimitError("429"), LLMKeyUnavailableError("402")])
@pytest.mark.asyncio
async def test_key_level_failure_moves_to_sibling_without_waiting(exc: Exception) -> None:
    registry, sleeps = BreakerRegistry(), _Sleeps()
    k0, k1 = _Provider("k0", fail_with=exc), _Provider("k1")
    router = _router(
        [(k0, "a:m#0", "a:m"), (k1, "a:m#1", "a:m")], chain=_real_chain(registry, sleeps)
    )
    for _ in range(5):
        assert await _call(router) == "k1"
    assert k0.calls == 3  # the 3rd failure opens its breaker; then skipped
    assert sleeps.seen == []


@pytest.mark.asyncio
async def test_open_key_in_a_multi_key_backup_group_is_skipped() -> None:
    registry, sleeps = BreakerRegistry(), _Sleeps()
    await _force_open(registry, "b:m#0")
    a = _Provider("a", fail_with=LLMServerError("503"))
    b0, b1 = _Provider("b0"), _Provider("b1")
    router = _router(
        [(a, "a:m", "a:m"), (b0, "b:m#0", "b:m"), (b1, "b:m#1", "b:m")],
        chain=_real_chain(registry, sleeps),
    )
    assert await _call(router) == "b1"
    assert b0.calls == 0


@pytest.mark.asyncio
async def test_last_resort_pass_skips_an_open_sibling(run_id: UUID) -> None:
    registry, sleeps = BreakerRegistry(), _Sleeps()
    a0 = _Provider("a0", fail_with=LLMServerError("503"))
    a1 = _Provider("a1")
    b = _Provider("b")
    router = _router(
        [(a0, "a:m#0", "a:m"), (a1, "a:m#1", "a:m"), (b, "b:m", "b:m")],
        chain=_real_chain(registry, sleeps),
    )
    assert await _call(router) == "b"  # a#0 5xx skips the whole a group → sticky b
    await _force_open(registry, "a:m#0")
    calls_a0 = a0.calls
    b.fail_with = LLMServerError("503")
    assert await _call(router) == "a1"
    assert a0.calls == calls_a0


@pytest.mark.asyncio
async def test_payload_hints_distinguish_fallback_from_alternative() -> None:
    seen: list[tuple[object, object]] = []

    @dataclass
    class _Spy:
        name: str = "spy"
        anchor: str = "around_llm_call"
        after: tuple[str, ...] = ()
        before: tuple[str, ...] = ()

        async def __call__(self, ctx: MiddlewareContext, call_next: CallNext) -> None:
            seen.append((ctx.payload.get("has_fallback"), ctx.payload.get("has_alternative")))
            await call_next(ctx)

    chain = MiddlewareChain.from_middlewares("around_llm_call", [_Spy()])
    exc = LLMRateLimitError("429")
    router = _router(
        [
            (_Provider("a0", fail_with=exc), "a:m#0", "a:m"),
            (_Provider("a1", fail_with=exc), "a:m#1", "a:m"),
            (_Provider("b"), "b:m", "b:m"),
        ],
        chain=chain,
    )
    assert await _call(router) == "b"
    assert seen == [(True, True), (True, True), (False, False)]


@pytest.mark.asyncio
async def test_exhausted_after_last_resort_reports_the_real_upstream_error(
    run_id: UUID,
) -> None:
    registry, sleeps = BreakerRegistry(), _Sleeps()
    a = _Provider("a", fail_with=LLMServerError("primary 503"))
    b = _Provider("b")
    router = _router([(a, "a:m", "a:m"), (b, "b:m", "b:m")], chain=_real_chain(registry, sleeps))
    await _call(router)  # sticky b
    await _force_open(registry, "a:m")
    b.fail_with = LLMServerError("backup 503")
    calls_a = a.calls
    with pytest.raises(AllProvidersExhaustedError) as info:
        await _call(router)
    # The last handle of the walk has no alternative, so an open breaker does
    # not refuse it: it gets a real attempt and the cause is a real upstream
    # error, never a CircuitOpenError.
    assert a.calls > calls_a
    assert isinstance(info.value.last_exc, LLMServerError)
    assert "primary 503" in str(info.value.last_exc)


@pytest.mark.asyncio
async def test_refused_handle_does_not_mask_the_real_error(run_id: UUID) -> None:
    """[a, b, c] sticky on c: c fails, a is OPEN (refused), b fails for real —
    the exhausted cause is b's error, not a's CircuitOpenError."""
    registry, sleeps = BreakerRegistry(), _Sleeps()
    a = _Provider("a", fail_with=LLMServerError("a 503"))
    b = _Provider("b", fail_with=LLMServerError("b 503"))
    c = _Provider("c")
    router = _router(
        [(a, "a:m", "a:m"), (b, "b:m", "b:m"), (c, "c:m", "c:m")],
        chain=_real_chain(registry, sleeps),
    )
    assert await _call(router) == "c"
    await _force_open(registry, "a:m")
    c.fail_with = LLMServerError("c 503")
    with pytest.raises(AllProvidersExhaustedError) as info:
        await _call(router)
    assert "b 503" in str(info.value.last_exc)


# ---------------------------------------------------------------------------
# First-token deadline with a backup
# ---------------------------------------------------------------------------


class _Streamer:
    """Streaming double: one content delta, then finish. ``on_second`` runs
    between the deltas (after the router has seen the first one)."""

    def __init__(self, on_second: Any = None) -> None:
        self.on_second = on_second

    async def stream(self, *, messages, tools, output_schema=None) -> AsyncIterator[LLMDelta]:  # type: ignore[no-untyped-def]
        yield LLMDelta(content="ok")
        if self.on_second is not None:
            await self.on_second()
        yield LLMDelta(finish_reason="stop")

    def new_stream_assembler(self):  # type: ignore[no-untyped-def]
        from orchestrator.llm.providers._streaming import OpenAIStreamAssembler

        return OpenAIStreamAssembler()


@dataclass
class _FixedEstimator:
    tokens_per_message: int

    def count(self, text: str) -> int:
        return self.tokens_per_message


def _capture_first_token_timeouts(monkeypatch: pytest.MonkeyPatch) -> list[float | None]:
    captured: list[float | None] = []
    real = router_module._next_delta

    async def _spy(it: AsyncIterator[LLMDelta], timeout: float | None) -> LLMDelta:
        captured.append(timeout)
        return await real(it, timeout)

    monkeypatch.setattr(router_module, "_next_delta", _spy)
    return captured


@pytest.mark.asyncio
async def test_first_token_deadline_capped_when_a_backup_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_first_token_timeouts(monkeypatch)
    router = _router(
        [(_Streamer(), "a:m", "a:m"), (_Provider("b"), "b:m", "b:m")],
        first_token_timeout_s=180.0,
        token_estimator=_FixedEstimator(10),
    )
    assert await _call(router) == "ok"
    assert captured[0] == FALLBACK_FIRST_TOKEN_TIMEOUT_S == 60.0


@pytest.mark.asyncio
async def test_first_token_deadline_120_for_large_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_first_token_timeouts(monkeypatch)
    router = _router(
        [(_Streamer(), "a:m", "a:m"), (_Provider("b"), "b:m", "b:m")],
        first_token_timeout_s=180.0,
        token_estimator=_FixedEstimator(100_001),
    )
    assert await _call(router) == "ok"
    assert captured[0] == FALLBACK_FIRST_TOKEN_TIMEOUT_LARGE_CONTEXT_S == 120.0


@pytest.mark.parametrize(
    ("configured", "expected"),
    [(180.0, 180.0), (None, None)],
)
@pytest.mark.asyncio
async def test_single_model_chain_keeps_its_first_token_deadline(
    monkeypatch: pytest.MonkeyPatch, configured: float | None, expected: float | None
) -> None:
    captured = _capture_first_token_timeouts(monkeypatch)
    router = _router(
        [(_Streamer(), "a:m#0", "a:m"), (_Provider("x"), "a:m#1", "a:m")],
        first_token_timeout_s=configured,
        token_estimator=_FixedEstimator(10),
    )
    assert await _call(router) == "ok"
    assert captured[0] == expected


@pytest.mark.asyncio
async def test_disabled_deadline_stays_disabled_with_a_backup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_first_token_timeouts(monkeypatch)
    router = _router(
        [(_Streamer(), "a:m", "a:m"), (_Provider("b"), "b:m", "b:m")],
        first_token_timeout_s=None,
        token_estimator=_FixedEstimator(10),
    )
    await _call(router)
    assert captured[0] is None


@pytest.mark.asyncio
async def test_shorter_configured_deadline_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_first_token_timeouts(monkeypatch)
    router = _router(
        [(_Streamer(), "a:m", "a:m"), (_Provider("b"), "b:m", "b:m")],
        first_token_timeout_s=30.0,
        token_estimator=_FixedEstimator(10),
    )
    await _call(router)
    assert captured[0] == 30.0


# ---------------------------------------------------------------------------
# Half-open probe resolves at the first progress delta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_probe_closes_the_breaker_at_its_first_token() -> None:
    clock = [0.0]
    registry = BreakerRegistry(clock=lambda: clock[0])
    await _force_open(registry, "a:m")
    clock[0] = 31.0
    states: list[str] = []

    async def _between() -> None:
        breaker = await registry.get("a:m")
        states.append(await breaker.check_state())

    router = _router(
        [(_Streamer(on_second=_between), "a:m", "a:m"), (_Provider("b"), "b:m", "b:m")],
        chain=_real_chain(registry, _Sleeps()),
        token_estimator=_FixedEstimator(10),
    )
    assert await _call(router) == "ok"
    assert states == ["CLOSED"], "the probe must resolve before the stream ends"


@pytest.mark.asyncio
async def test_payload_carries_the_attempt_deadline_for_the_probe_lease() -> None:
    """Streaming with a backup → the capped first-token deadline; a
    non-streaming attempt → the whole-answer deadline it really runs under."""
    seen: list[object] = []

    @dataclass
    class _Spy:
        name: str = "spy"
        anchor: str = "around_llm_call"
        after: tuple[str, ...] = ()
        before: tuple[str, ...] = ()

        async def __call__(self, ctx: MiddlewareContext, call_next: CallNext) -> None:
            seen.append(ctx.payload.get("attempt_deadline_s"))
            await call_next(ctx)

    chain = MiddlewareChain.from_middlewares("around_llm_call", [_Spy()])
    streaming = _router(
        [(_Streamer(), "a:m", "a:m"), (_Provider("b"), "b:m", "b:m")],
        chain=chain,
        first_token_timeout_s=180.0,
        token_estimator=_FixedEstimator(200_000),
    )
    await _call(streaming)
    buffered = _router(
        [(_Provider("a"), "a:m", "a:m"), (_Provider("b"), "b:m", "b:m")],
        chain=chain,
        first_token_timeout_s=180.0,
        token_estimator=_FixedEstimator(10),
    )
    await _call(buffered)
    assert seen == [120.0, 180.0]
