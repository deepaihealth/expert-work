"""B-297 PR2 — a stream that produced only reasoning, then stalled or errored,
is discarded and redone on the next provider group; anything visible keeps
today's behaviour. Also: the switch notice, the sink discard signal, and the
time-to-first-progress histogram.

No real waits: an idle-timeout expiry is emulated by the provider raising
``TimeoutError`` from ``__anext__`` (``asyncio.wait_for`` propagates it exactly
like its own expiry), and the E.4 middleware gets a fake sleeper.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage
from prometheus_client import REGISTRY

from expert_work.common.context import reset_current_run_id, set_current_run_id
from expert_work.runtime.middleware import (
    BreakerRegistry,
    LLMErrorHandlingMiddleware,
    LLMServerError,
    LLMStreamInterruptedError,
    LLMStreamStaleError,
    MiddlewareChain,
)
from orchestrator.llm import router as router_mod
from orchestrator.llm.failover import (
    FailoverNotice,
    StickyRouteStore,
    _failover_listener,
    failover_frames,
    failover_reason,
    plan_walk,
)
from orchestrator.llm.providers._streaming import LLMDelta, OpenAIStreamAssembler, ToolCallChunk
from orchestrator.llm.router import LLMRouter, ProviderHandle
from orchestrator.llm.stall import (
    STALLED_AFTER_S,
    LLMReasoningStallError,
    has_visible_output,
    stalled_without_answer,
)

_IDLE = 45.0


class _Hang(Exception):  # noqa: N818 - script marker, never raised
    """Script marker: the upstream goes silent (→ the idle timer fires)."""


@dataclass
class _Scripted:
    """Streaming double. Script items: an ``LLMDelta`` (yielded), ``_Hang``
    (the idle timer fires: raises ``TimeoutError``) or an exception (raised)."""

    script: list[Any]
    calls: int = 0

    async def stream(
        self, *, messages: Any, tools: Any, output_schema: Any = None
    ) -> AsyncIterator[LLMDelta]:
        del messages, tools, output_schema
        self.calls += 1
        for item in self.script:
            if item is _Hang:
                raise TimeoutError
            if isinstance(item, BaseException):
                raise item
            yield item

    def new_stream_assembler(self) -> OpenAIStreamAssembler:
        return OpenAIStreamAssembler()


_THINK_THEN_HANG = [LLMDelta(reasoning="let me "), LLMDelta(reasoning="think"), _Hang]
_ANSWER = [
    LLMDelta(reasoning="backup thinks"),
    LLMDelta(content="ok"),
    LLMDelta(finish_reason="stop"),
]


@dataclass
class _Sink:
    """Delta sink with the optional ``discard_attempt`` hook."""

    deltas: list[LLMDelta] = field(default_factory=list)
    discards: int = 0

    async def __call__(self, delta: LLMDelta) -> None:
        self.deltas.append(delta)

    async def discard_attempt(self) -> None:
        self.discards += 1


def _router(
    *handles: tuple[_Scripted, str, str],
    chain: MiddlewareChain | None = None,
    store: StickyRouteStore | None = None,
) -> LLMRouter:
    return LLMRouter(
        providers=[ProviderHandle(provider=p, key=k, group=g) for p, k, g in handles],  # type: ignore[arg-type]
        around_llm_chain=chain,
        first_token_timeout_s=60.0,
        idle_timeout_s=_IDLE,
        sticky_store=store if store is not None else StickyRouteStore(),
    )


def _e4(registry: BreakerRegistry) -> MiddlewareChain:
    async def _no_sleep(_: float) -> None:
        return None

    mw = LLMErrorHandlingMiddleware(breaker_registry=registry, sleeper=_no_sleep)
    return MiddlewareChain.from_middlewares("around_llm_call", [mw])


def _failovers(frm: str, to: str, reason: str) -> float:
    value = REGISTRY.get_sample_value(
        "expert_work_llm_failover_total",
        {"from_group": frm, "to_group": to, "reason": reason},
    )
    return value or 0.0


@pytest.fixture
def run_id() -> Iterator[UUID]:
    rid = uuid4()
    token = set_current_run_id(rid)
    yield rid
    reset_current_run_id(token)


# ---------------------------------------------------------------------------
# Reasoning only → redo on the next group
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reasoning_only_idle_stall_is_redone_on_the_backup() -> None:
    primary, backup = _Scripted(list(_THINK_THEN_HANG)), _Scripted(list(_ANSWER))
    registry = BreakerRegistry()
    router = _router(
        (primary, "ds:flash", "ds:flash"), (backup, "qw:max", "qw:max"), chain=_e4(registry)
    )
    before = _failovers("ds:flash", "qw:max", "reasoning_stall")

    msg = await router(messages=[], tools=[])

    assert msg.content == "ok"
    assert msg.additional_kwargs.get("reasoning_content") == "backup thinks"
    assert STALLED_AFTER_S not in msg.response_metadata
    # Not retried in place on the primary; counted by its breaker.
    assert (primary.calls, backup.calls) == (1, 1)
    assert (await registry.get("ds:flash")).consecutive_failures == 1
    assert _failovers("ds:flash", "qw:max", "reasoning_stall") == before + 1


@pytest.mark.asyncio
async def test_reasoning_only_error_after_first_token_is_redone_on_the_backup() -> None:
    primary = _Scripted([LLMDelta(reasoning="hmm"), LLMServerError("mid-stream 502")])
    backup = _Scripted(list(_ANSWER))
    router = _router((primary, "ds:flash", "ds:flash"), (backup, "qw:max", "qw:max"))

    msg = await router(messages=[], tools=[])

    assert msg.content == "ok"
    assert backup.calls == 1


@pytest.mark.asyncio
async def test_an_unfinished_tool_call_does_not_count_as_visible() -> None:
    # Its arguments never completed: the interrupted partial drops it, so it
    # could never run — discarding the attempt loses nothing.
    primary = _Scripted(
        [
            LLMDelta(reasoning="plan"),
            LLMDelta(content="\n "),
            LLMDelta(
                tool_calls=(ToolCallChunk(index=0, id="c1", name="search", args_fragment='{"q":'),)
            ),
            _Hang,
        ]
    )
    backup = _Scripted(list(_ANSWER))
    router = _router((primary, "ds:flash", "ds:flash"), (backup, "qw:max", "qw:max"))

    msg = await router(messages=[], tools=[])

    assert msg.content == "ok"


@pytest.mark.asyncio
async def test_the_redone_run_sticks_to_the_backup(run_id: UUID) -> None:
    del run_id
    primary, backup = _Scripted(list(_THINK_THEN_HANG)), _Scripted(list(_ANSWER))
    router = _router((primary, "ds:flash", "ds:flash"), (backup, "qw:max", "qw:max"))

    await router(messages=[], tools=[])
    await router(messages=[], tools=[])

    assert (primary.calls, backup.calls) == (1, 2)


@pytest.mark.asyncio
async def test_the_sink_is_told_to_discard_the_abandoned_attempt() -> None:
    primary, backup = _Scripted(list(_THINK_THEN_HANG)), _Scripted(list(_ANSWER))
    router = _router((primary, "ds:flash", "ds:flash"), (backup, "qw:max", "qw:max"))
    sink = _Sink()

    await router(messages=[], tools=[], on_delta=sink)

    assert sink.discards == 1
    # The discarded attempt's deltas were already delivered; the backup's follow.
    assert [d.reasoning for d in sink.deltas if d.reasoning] == [
        "let me ",
        "think",
        "backup thinks",
    ]


# ---------------------------------------------------------------------------
# Today's behaviour kept: visible output, or nowhere else to go
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_visible_text_then_stall_keeps_the_partial() -> None:
    primary = _Scripted([LLMDelta(reasoning="x"), LLMDelta(content="half an answer"), _Hang])
    backup = _Scripted(list(_ANSWER))
    router = _router((primary, "ds:flash", "ds:flash"), (backup, "qw:max", "qw:max"))
    sink = _Sink()

    msg = await router(messages=[], tools=[], on_delta=sink)

    assert msg.content == "half an answer"
    assert msg.response_metadata.get(STALLED_AFTER_S) == _IDLE
    assert (backup.calls, sink.discards) == (0, 0)


@pytest.mark.asyncio
async def test_visible_text_then_error_stays_terminal() -> None:
    primary = _Scripted([LLMDelta(content="half"), LLMServerError("mid-stream 502")])
    backup = _Scripted(list(_ANSWER))
    router = _router((primary, "ds:flash", "ds:flash"), (backup, "qw:max", "qw:max"))

    with pytest.raises(LLMStreamInterruptedError) as info:
        await router(messages=[], tools=[])

    assert isinstance(info.value.partial, AIMessage)
    assert info.value.partial.content == "half"
    assert backup.calls == 0


@pytest.mark.asyncio
async def test_a_complete_tool_call_then_stall_keeps_the_partial() -> None:
    primary = _Scripted(
        [
            LLMDelta(reasoning="plan"),
            LLMDelta(
                tool_calls=(
                    ToolCallChunk(index=0, id="c1", name="search", args_fragment='{"q": "x"}'),
                )
            ),
            _Hang,
        ]
    )
    backup = _Scripted(list(_ANSWER))
    router = _router((primary, "ds:flash", "ds:flash"), (backup, "qw:max", "qw:max"))

    msg = await router(messages=[], tools=[])

    assert [c["name"] for c in msg.tool_calls] == ["search"]
    assert backup.calls == 0


@pytest.mark.asyncio
async def test_without_a_backup_group_the_stamped_partial_comes_back() -> None:
    # Sibling keys of the same model are not a fallback: an upstream that
    # stalls mid-thought is a provider-level problem.
    k0, k1 = _Scripted(list(_THINK_THEN_HANG)), _Scripted(list(_ANSWER))
    router = _router((k0, "ds:flash#0", "ds:flash"), (k1, "ds:flash#1", "ds:flash"))
    sink = _Sink()

    msg = await router(messages=[], tools=[], on_delta=sink)

    assert stalled_without_answer(msg) == _IDLE
    assert (k1.calls, sink.discards) == (0, 0)


@pytest.mark.asyncio
async def test_without_a_backup_the_reasoning_only_error_stays_terminal() -> None:
    only = _Scripted([LLMDelta(reasoning="hmm"), LLMServerError("mid-stream 502")])
    router = _router((only, "ds:flash", "ds:flash"))

    with pytest.raises(LLMStreamInterruptedError):
        await router(messages=[], tools=[])


@pytest.mark.asyncio
async def test_a_finish_reason_before_the_stall_keeps_the_partial() -> None:
    primary = _Scripted([LLMDelta(reasoning="plan"), LLMDelta(finish_reason="length"), _Hang])
    backup = _Scripted(list(_ANSWER))
    router = _router((primary, "ds:flash", "ds:flash"), (backup, "qw:max", "qw:max"))

    msg = await router(messages=[], tools=[])

    assert msg.response_metadata.get("finish_reason") == "length"
    assert backup.calls == 0


def test_redo_needs_another_group_to_walk_to() -> None:
    plan = plan_walk(["a", "b"], None)
    assert [plan.can_redo_elsewhere(p) for p in range(2)] == [True, False]
    # Sticky on "b": the primary walk is [b], then the last-resort pass [a].
    sticky = plan_walk(["a", "b"], "b")
    assert [sticky.can_redo_elsewhere(p) for p in range(2)] == [False, False]
    siblings = plan_walk(["a", "a", "b"], None)
    assert [siblings.can_redo_elsewhere(p) for p in range(3)] == [True, True, False]
    last_resort = plan_walk(["a", "a", "b"], "b")  # order: b, a, a
    assert [last_resort.can_redo_elsewhere(p) for p in range(3)] == [False, False, False]


# ---------------------------------------------------------------------------
# Visibility predicate, error class, reason vocabulary
# ---------------------------------------------------------------------------


def test_visible_output_is_text_or_a_runnable_tool_call() -> None:
    assert not has_visible_output(AIMessage(content=""))
    assert not has_visible_output(
        AIMessage(content=" \n", additional_kwargs={"reasoning_content": "x"})
    )
    assert has_visible_output(AIMessage(content="a"))
    call = {"name": "search", "args": {}, "id": "c1", "type": "tool_call"}
    assert has_visible_output(AIMessage(content="", tool_calls=[call]))


def test_reasoning_stall_is_a_counted_not_retried_failover_error() -> None:
    exc = LLMReasoningStallError("x")
    assert isinstance(exc, LLMStreamStaleError)
    assert not isinstance(exc, LLMServerError)  # E.4 would retry it in place
    assert failover_reason(exc) == "reasoning_stall"


def test_failover_reasons_stay_a_closed_vocabulary() -> None:
    from orchestrator.llm.failover import _REASONS

    assert {reason for _, reason in _REASONS} | {"other", "sticky"} == {
        "circuit_open",
        "reasoning_stall",
        "key_unavailable",
        "auth",
        "rate_limit",
        "first_token_timeout",
        "network",
        "server_error",
        "other",
        "sticky",
    }


# ---------------------------------------------------------------------------
# Switch notice (console-only run event)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_actual_switch_publishes_one_notice_frame(run_id: UUID) -> None:
    del run_id
    primary, backup = _Scripted(list(_THINK_THEN_HANG)), _Scripted(list(_ANSWER))
    router = _router(
        (primary, "deepseek:deepseek-flash", "deepseek:deepseek-flash"),
        (backup, "qwen:qwen3.8-max", "qwen:qwen3.8-max"),
    )
    frames: list[dict[str, Any]] = []

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    with failover_frames(4, publish):
        await router(messages=[], tools=[])
        # Second call of the run starts on the backup (sticky): no switch, no notice.
        await router(messages=[], tools=[])

    assert frames == [
        {
            "step": 4,
            "from_model": "deepseek-flash",
            "to_model": "qwen3.8-max",
            "reason": "reasoning_stall",
        }
    ]
    assert _failover_listener.get() is None


@pytest.mark.asyncio
async def test_a_switch_before_the_first_token_is_announced_too() -> None:
    primary = _Scripted([LLMServerError("503")])
    backup = _Scripted(list(_ANSWER))
    router = _router((primary, "ds:flash", "ds:flash"), (backup, "qw:max", "qw:max"))
    frames: list[dict[str, Any]] = []

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    with failover_frames(1, publish):
        await router(messages=[], tools=[])

    assert [(f["reason"], f["step"]) for f in frames] == [("server_error", 1)]


@pytest.mark.asyncio
async def test_a_failing_notice_publisher_does_not_fail_the_call() -> None:
    primary, backup = _Scripted([LLMServerError("503")]), _Scripted(list(_ANSWER))
    router = _router((primary, "ds:flash", "ds:flash"), (backup, "qw:max", "qw:max"))

    async def broken(frame: dict[str, Any]) -> None:
        raise RuntimeError("bridge down")

    with failover_frames(1, broken):
        msg = await router(messages=[], tools=[])

    assert msg.content == "ok"


@pytest.mark.asyncio
async def test_the_discard_reaches_the_sink_before_the_notice() -> None:
    order: list[str] = []

    class _OrderSink(_Sink):
        async def discard_attempt(self) -> None:
            order.append("discard")

    async def publish(frame: dict[str, Any]) -> None:
        order.append("notice")

    primary, backup = _Scripted(list(_THINK_THEN_HANG)), _Scripted(list(_ANSWER))
    router = _router((primary, "ds:flash", "ds:flash"), (backup, "qw:max", "qw:max"))
    with failover_frames(1, publish):
        await router(messages=[], tools=[], on_delta=_OrderSink())

    assert order == ["discard", "notice"]


def test_notice_frame_carries_no_conversation_content() -> None:
    frame = FailoverNotice("a:m1", "b:m2", "server_error").frame(3)
    assert set(frame) == {"step", "from_model", "to_model", "reason"}


# ---------------------------------------------------------------------------
# Time to first progress (validates the PR1 first-token caps)
# ---------------------------------------------------------------------------


def _first_progress(key: str) -> tuple[float, float]:
    labels = {"provider_key": key}
    count = REGISTRY.get_sample_value("expert_work_llm_first_progress_seconds_count", labels)
    total = REGISTRY.get_sample_value("expert_work_llm_first_progress_seconds_sum", labels)
    return count or 0.0, total or 0.0


@pytest.mark.asyncio
async def test_first_progress_is_observed_per_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([100.0, 107.5])
    monkeypatch.setattr(router_mod, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    key = f"fp:{uuid4().hex[:8]}"
    router = _router(
        (_Scripted([LLMDelta(), LLMDelta(reasoning="r"), LLMDelta(content="a")]), key, key)
    )

    await router(messages=[], tools=[])

    assert _first_progress(key) == (1.0, 7.5)


@pytest.mark.asyncio
async def test_no_first_progress_observation_on_a_first_token_timeout() -> None:
    key = f"fp:{uuid4().hex[:8]}"
    backup = _Scripted(list(_ANSWER))
    router = _router((_Scripted([_Hang]), key, key), (backup, "qw:max", "qw:max"))

    await router(messages=[], tools=[])

    assert _first_progress(key) == (0.0, 0.0)
