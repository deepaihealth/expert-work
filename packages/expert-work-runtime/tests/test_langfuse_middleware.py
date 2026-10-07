"""Unit tests for :class:`LangfuseMiddleware` (Stream E.5)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest
from prometheus_client import REGISTRY

from expert_work.runtime.middleware import (
    LangfuseClient,
    LangfuseMiddleware,
    LangfuseSpan,
    Middleware,
    MiddlewareContext,
    RecordingLangfuseClient,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@dataclass
class _FailingSpan:
    raise_on_record: bool = False
    raise_on_end: bool = False
    output_recorded: Any = None
    ended: bool = False

    def record_output(self, output: Any) -> None:
        if self.raise_on_record:
            raise RuntimeError("record_output blew up")
        self.output_recorded = output

    def record_usage(self, _usage: Mapping[str, int]) -> None:
        if self.raise_on_record:
            raise RuntimeError("record_usage blew up")

    def record_error(self, _exc: BaseException) -> None:
        if self.raise_on_record:
            raise RuntimeError("record_error blew up")

    def end(self) -> None:
        if self.raise_on_end:
            raise RuntimeError("end blew up")
        self.ended = True


@dataclass
class _FailingClient:
    raise_on_start: bool = False
    span: _FailingSpan = field(default_factory=_FailingSpan)

    def start_span(
        self,
        *,
        name: str,
        input: Any,
        metadata: Mapping[str, Any] | None = None,
    ) -> LangfuseSpan:
        if self.raise_on_start:
            raise RuntimeError("start_span blew up")
        return self.span


async def _ok_terminal(_ctx: MiddlewareContext) -> None:
    pass


async def _failing_terminal(_ctx: MiddlewareContext) -> None:
    raise RuntimeError("LLM hiccup")


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_records_input_metadata_and_completion() -> None:
    client = RecordingLangfuseClient()
    mw = LangfuseMiddleware(client=client)
    ctx = MiddlewareContext(
        payload={
            "agent_name": "support-agent",
            "messages": [{"role": "user", "content": "hi"}],
            "model": "claude-sonnet",
            "tenant_id": "t-1",
            "trace_id": "trace-abc",
            "llm_response": {
                "output": "hello back",
                "usage": {"input_tokens": 12, "output_tokens": 6},
            },
        }
    )
    await mw(ctx, _ok_terminal)

    assert len(client.spans) == 1
    span = client.spans[0]
    assert span.name == "support-agent"
    assert span.input == [{"role": "user", "content": "hi"}]
    assert span.metadata["model"] == "claude-sonnet"
    assert span.metadata["tenant_id"] == "t-1"
    assert span.metadata["trace_id"] == "trace-abc"
    assert span.output == "hello back"
    assert span.usage == {"input_tokens": 12, "output_tokens": 6}
    assert span.error is None
    assert span.ended is True


@pytest.mark.asyncio
async def test_missing_agent_name_falls_back_to_llm_call() -> None:
    client = RecordingLangfuseClient()
    mw = LangfuseMiddleware(client=client)
    await mw(MiddlewareContext(payload={}), _ok_terminal)
    assert client.spans[0].name == "llm_call"


@pytest.mark.asyncio
async def test_no_llm_response_still_ends_span() -> None:
    client = RecordingLangfuseClient()
    mw = LangfuseMiddleware(client=client)
    await mw(MiddlewareContext(payload={}), _ok_terminal)
    span = client.spans[0]
    assert span.output is None
    assert span.usage is None
    assert span.ended is True


# ---------------------------------------------------------------------------
# Error path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_exception_records_error_and_reraises() -> None:
    client = RecordingLangfuseClient()
    mw = LangfuseMiddleware(client=client)
    ctx = MiddlewareContext(payload={"messages": [{"role": "user", "content": "x"}]})

    with pytest.raises(RuntimeError, match="LLM hiccup"):
        await mw(ctx, _failing_terminal)

    span = client.spans[0]
    assert span.error == "RuntimeError: LLM hiccup"
    assert span.ended is True


# ---------------------------------------------------------------------------
# Fail-soft contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_span_failure_does_not_break_chain() -> None:
    mw = LangfuseMiddleware(client=_FailingClient(raise_on_start=True))
    await mw(MiddlewareContext(payload={}), _ok_terminal)


@pytest.mark.asyncio
async def test_record_output_failure_does_not_break_chain() -> None:
    client = _FailingClient(span=_FailingSpan(raise_on_record=True))
    mw = LangfuseMiddleware(client=client)
    await mw(
        MiddlewareContext(
            payload={
                "llm_response": {"output": "ok", "usage": {"input_tokens": 1}},
            }
        ),
        _ok_terminal,
    )
    # No assertion needed — must complete without exception.


@pytest.mark.asyncio
async def test_end_failure_does_not_break_chain() -> None:
    client = _FailingClient(span=_FailingSpan(raise_on_end=True))
    mw = LangfuseMiddleware(client=client)
    await mw(MiddlewareContext(payload={}), _ok_terminal)


# ---------------------------------------------------------------------------
# Anchor wiring
# ---------------------------------------------------------------------------


def test_registers_around_llm_call_anchor() -> None:
    mw = LangfuseMiddleware(client=RecordingLangfuseClient())
    assert mw.anchor == "around_llm_call"
    assert "llm_error_handling" in mw.before


def test_satisfies_middleware_protocol() -> None:
    assert isinstance(LangfuseMiddleware(client=RecordingLangfuseClient()), Middleware)


def test_recording_client_satisfies_client_protocol() -> None:
    assert isinstance(RecordingLangfuseClient(), LangfuseClient)


# ---------------------------------------------------------------------------
# B-153 follow-up — every swallowed failure is counted
# ---------------------------------------------------------------------------
#
# The fail-soft guards above are why Langfuse received nothing for six weeks
# without anyone noticing: each failure was a WARNING log line, and no alert
# can query logs. ``expert_work_langfuse_delivery_failures_total{stage}`` is
# what the ExpertWorkLangfuseDeliveryFailing alert watches.

_FAILURE_COUNTER = "expert_work_langfuse_delivery_failures_total"
_MIDDLEWARE_STAGES = ("start_span", "record_response", "record_error", "end")


def _failure_counts() -> dict[str, float]:
    return {
        stage: REGISTRY.get_sample_value(_FAILURE_COUNTER, {"stage": stage}) or 0.0
        for stage in _MIDDLEWARE_STAGES
    }


def _deltas(before: dict[str, float], after: dict[str, float]) -> dict[str, float]:
    return {stage: after[stage] - before[stage] for stage in _MIDDLEWARE_STAGES}


def test_failure_series_exist_before_any_failure() -> None:
    """``increase()`` cannot see a series' first sample — a counter born at 1
    on the first failure would not alert until the second. Every stage must be
    exported at 0 from import time."""
    for stage in (*_MIDDLEWARE_STAGES, "export"):
        assert REGISTRY.get_sample_value(_FAILURE_COUNTER, {"stage": stage}) is not None, stage


@pytest.mark.asyncio
async def test_start_span_failure_is_counted() -> None:
    before = _failure_counts()
    mw = LangfuseMiddleware(client=_FailingClient(raise_on_start=True))
    await mw(MiddlewareContext(payload={}), _ok_terminal)
    assert _deltas(before, _failure_counts()) == {
        "start_span": 1,
        "record_response": 0,
        "record_error": 0,
        "end": 0,
    }


@pytest.mark.asyncio
async def test_record_response_failure_is_counted() -> None:
    before = _failure_counts()
    mw = LangfuseMiddleware(client=_FailingClient(span=_FailingSpan(raise_on_record=True)))
    await mw(
        MiddlewareContext(payload={"llm_response": {"output": "ok", "usage": {"input_tokens": 1}}}),
        _ok_terminal,
    )
    assert _deltas(before, _failure_counts()) == {
        "start_span": 0,
        "record_response": 1,
        "record_error": 0,
        "end": 0,
    }


@pytest.mark.asyncio
async def test_record_error_failure_is_counted() -> None:
    before = _failure_counts()
    mw = LangfuseMiddleware(client=_FailingClient(span=_FailingSpan(raise_on_record=True)))
    with pytest.raises(RuntimeError, match="LLM hiccup"):
        await mw(MiddlewareContext(payload={}), _failing_terminal)
    assert _deltas(before, _failure_counts()) == {
        "start_span": 0,
        "record_response": 0,
        "record_error": 1,
        "end": 0,
    }


@pytest.mark.asyncio
async def test_end_failure_is_counted() -> None:
    before = _failure_counts()
    mw = LangfuseMiddleware(client=_FailingClient(span=_FailingSpan(raise_on_end=True)))
    await mw(MiddlewareContext(payload={}), _ok_terminal)
    assert _deltas(before, _failure_counts()) == {
        "start_span": 0,
        "record_response": 0,
        "record_error": 0,
        "end": 1,
    }


@pytest.mark.asyncio
async def test_successful_calls_count_no_failure() -> None:
    before = _failure_counts()
    mw = LangfuseMiddleware(client=RecordingLangfuseClient())
    await mw(
        MiddlewareContext(payload={"llm_response": {"output": "ok", "usage": {"input_tokens": 1}}}),
        _ok_terminal,
    )
    with pytest.raises(RuntimeError, match="LLM hiccup"):
        await mw(MiddlewareContext(payload={}), _failing_terminal)
    assert _failure_counts() == before
