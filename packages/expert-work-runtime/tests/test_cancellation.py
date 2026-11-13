"""Unit tests for :class:`CancellationToken` — Stream E.15."""

from __future__ import annotations

import asyncio
import time

import pytest

from expert_work.runtime.cancellation import (
    CANCELLATION_TOKEN_KEY,
    CancellationToken,
    RunCancelledError,
)

# ---------------------------------------------------------------------------
# Basic signalling
# ---------------------------------------------------------------------------


def test_fresh_token_not_cancelled() -> None:
    assert CancellationToken().cancelled() is False


def test_cancel_sets_cancelled() -> None:
    token = CancellationToken()
    token.cancel()
    assert token.cancelled() is True


def test_cancel_is_idempotent() -> None:
    token = CancellationToken()
    token.cancel()
    token.cancel()
    assert token.cancelled() is True


def test_raise_if_cancelled_noop_when_active() -> None:
    CancellationToken().raise_if_cancelled()  # must not raise


def test_raise_if_cancelled_raises_when_cancelled() -> None:
    token = CancellationToken()
    token.cancel()
    with pytest.raises(RunCancelledError):
        token.raise_if_cancelled()


# ---------------------------------------------------------------------------
# from_event — shared signal
# ---------------------------------------------------------------------------


def test_from_event_shares_signal() -> None:
    event = asyncio.Event()
    token = CancellationToken.from_event(event)
    assert token.cancelled() is False
    event.set()
    assert token.cancelled() is True


def test_token_cancel_sets_wrapped_event() -> None:
    event = asyncio.Event()
    token = CancellationToken.from_event(event)
    token.cancel()
    assert event.is_set()


# ---------------------------------------------------------------------------
# Test matrix #32 — cancellation does not bleed across runs
# ---------------------------------------------------------------------------


def test_separate_tokens_are_independent() -> None:
    """A cancelled run's token must not affect a freshly created one."""
    run_1 = CancellationToken()
    run_1.cancel()
    run_2 = CancellationToken()
    assert run_1.cancelled() is True
    assert run_2.cancelled() is False


def test_from_event_separate_events_independent() -> None:
    event_1, event_2 = asyncio.Event(), asyncio.Event()
    token_1 = CancellationToken.from_event(event_1)
    token_2 = CancellationToken.from_event(event_2)
    token_1.cancel()
    assert token_2.cancelled() is False


# ---------------------------------------------------------------------------
# run_cancellable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_cancellable_returns_result_on_completion() -> None:
    token = CancellationToken()

    async def _work() -> str:
        await asyncio.sleep(0.01)
        return "done"

    assert await token.run_cancellable(_work()) == "done"


@pytest.mark.asyncio
async def test_run_cancellable_already_cancelled_raises_immediately() -> None:
    token = CancellationToken()
    token.cancel()

    ran = False

    async def _work() -> None:
        nonlocal ran
        ran = True

    with pytest.raises(RunCancelledError):
        await token.run_cancellable(_work())
    # The coroutine was closed, never executed.
    assert ran is False


@pytest.mark.asyncio
async def test_run_cancellable_interrupts_inflight_call() -> None:
    """Test matrix #30 mechanics — a cancel mid-await aborts the slow
    coroutine within a tick, not after it would have finished."""
    token = CancellationToken()
    work_completed = False

    async def _slow_work() -> str:
        nonlocal work_completed
        await asyncio.sleep(5.0)  # would dwarf the test timeout
        work_completed = True
        return "should-not-return"

    async def _cancel_soon() -> None:
        await asyncio.sleep(0.05)
        token.cancel()

    start = time.monotonic()
    with pytest.raises(RunCancelledError):
        await asyncio.gather(
            token.run_cancellable(_slow_work()),
            _cancel_soon(),
        )
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"cancel took {elapsed:.3f}s — should be ~0.05s"
    assert work_completed is False


@pytest.mark.asyncio
async def test_run_cancellable_propagates_inner_exception() -> None:
    """A genuine error inside the coroutine is not masked as a cancel."""
    token = CancellationToken()

    async def _boom() -> None:
        raise ValueError("inner failure")

    with pytest.raises(ValueError, match="inner failure"):
        await token.run_cancellable(_boom())


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_cancellation_token_key_is_stable() -> None:
    """The config key is part of the node ↔ worker contract."""
    assert CANCELLATION_TOKEN_KEY == "cancellation_token"


@pytest.mark.asyncio
async def test_run_cancellable_cancels_the_inner_call_when_the_caller_is_cancelled() -> None:
    """B-300 review L3 —— 外层任务被取消(例如并发的另一件事先失败、TaskGroup 收摊)时,里面那次
    调用也要跟着取消,不能留一个没人等的 LLM / 工具调用在后台跑完。"""
    token = CancellationToken()
    inner_cancelled = asyncio.Event()

    async def slow() -> bool:
        try:
            return await asyncio.Event().wait()
        except asyncio.CancelledError:
            inner_cancelled.set()
            raise

    outer = asyncio.ensure_future(token.run_cancellable(slow()))
    await asyncio.sleep(0.01)
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await outer
    assert inner_cancelled.is_set()
