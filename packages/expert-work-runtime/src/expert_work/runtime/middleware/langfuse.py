"""Langfuse tracing middleware — Stream E.5.

Wraps the LLM call and records prompt / completion / token usage /
latency / errors to Langfuse. Registered on the ``around_llm_call``
anchor with ``before=("llm_error_handling",)`` so Langfuse is the
**outer** wrapper — every retry of the inner ``llm_error_handling``
middleware lives inside the same Langfuse span, so the trace shows the
full attempt history (deer-flow follows the same layering).

The :class:`LangfuseClient` protocol decouples this middleware from any
specific SDK. M0 ships the in-memory :class:`RecordingLangfuseClient`
suitable for dev / tests; an SDK-backed adapter that pushes spans
to a self-hosted Langfuse instance (ADR-0005) lands in a separate
follow-up PR. The protocol is sync because real SDKs internally fan
out spans onto a bounded queue + background task — the middleware
itself must not block the LLM call (per [STREAM-E-DESIGN § 6 risk
"Langfuse 队列堆积阻塞主路径"](../../../../../../../docs/streams/STREAM-E-DESIGN.md)).

Conventions read from ``ctx.payload``:

- ``messages`` — input to the LLM (recorded as span ``input``)
- ``model`` — model identifier (span metadata)
- ``tenant_id`` — for grouping in Langfuse UI
- ``agent_name`` — span ``name`` (falls back to ``"llm_call"``)
- ``trace_id`` — optional W3C ``trace_id`` for cross-system correlation
- ``llm_response`` — set by the terminal handler after the LLM returns;
  read by this middleware to populate the span ``output``. Layout:
  ``{"output": ..., "usage": {"input_tokens": int, "output_tokens": int}}``.
  Absent ``llm_response`` is fine — span still ends with whatever data
  we have.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from expert_work.common.observability.metrics import expert_work_counter
from expert_work.runtime.middleware.base import CallNext, MiddlewareContext

logger = logging.getLogger(__name__)

#: B-153 follow-up — every place our side fails to hand data to Langfuse.
#: The four fail-soft guards below and the SDK export path
#: (``langfuse_sdk.FailureCountingSpanExporter``, stage ``export``) used to
#: only log a WARNING, and no alert can query logs: after langfuse 4.x dropped
#: ``start_generation`` every call logged ``langfuse.start_span_failed`` and
#: Langfuse received nothing for six weeks. Fixed label set, no tenant ids;
#: the ExpertWorkLangfuseDeliveryFailing alert watches this counter (a failure
#: signal, independent of traffic volume).
LANGFUSE_FAILURE_STAGES = ("start_span", "record_response", "record_error", "end", "export")

_delivery_failures_total = expert_work_counter(
    "expert_work_langfuse_delivery_failures_total",
    "Failures handing trace data to Langfuse, by stage: the middleware's "
    "swallowed client errors and failed SDK export batches (B-153).",
    ("stage",),
)
# Export every stage at 0 from import time: ``increase()`` cannot see a
# series' first sample, so a series born at 1 on the first failure would not
# alert until the second one.
for _stage in LANGFUSE_FAILURE_STAGES:
    _delivery_failures_total.labels(stage=_stage)


def record_delivery_failure(stage: str) -> None:
    """Count one failure to hand data to Langfuse (``stage`` ∈ LANGFUSE_FAILURE_STAGES)."""
    _delivery_failures_total.labels(stage=stage).inc()


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class LangfuseSpan(Protocol):
    """A single trace span. Calls are non-blocking — implementations
    enqueue + return rather than awaiting upstream IO."""

    def record_output(self, output: Any) -> None:
        """Attach the LLM completion (or any terminal output) to the span."""

    def record_usage(self, usage: Mapping[str, int]) -> None:
        """Attach token usage. Keys typically: ``input_tokens``,
        ``output_tokens``, ``total_tokens``, ``cache_read_input_tokens``."""

    def record_error(self, exception: BaseException) -> None:
        """Mark span as errored. Implementations should capture the
        exception type + str(exception); never re-raise."""

    def end(self) -> None:
        """Finalise the span and submit. After ``end`` further mutations
        are no-ops in well-behaved implementations."""


@runtime_checkable
class LangfuseClient(Protocol):
    """Span factory. Real impls fan spans onto a bounded background queue."""

    def start_span(
        self,
        *,
        name: str,
        input: Any,
        metadata: Mapping[str, Any] | None = None,
    ) -> LangfuseSpan:
        """Start a span and return its handle. Must not block."""


# ---------------------------------------------------------------------------
# Recording client (M0 default / test fixture)
# ---------------------------------------------------------------------------


@dataclass
class RecordedSpan:
    """In-memory span record used by tests + dev `RecordingLangfuseClient`."""

    name: str
    input: Any
    metadata: Mapping[str, Any]
    output: Any | None = None
    usage: Mapping[str, int] | None = None
    error: str | None = None
    ended: bool = False

    def record_output(self, output: Any) -> None:
        self.output = output

    def record_usage(self, usage: Mapping[str, int]) -> None:
        self.usage = dict(usage)

    def record_error(self, exception: BaseException) -> None:
        self.error = f"{type(exception).__name__}: {exception}"

    def end(self) -> None:
        self.ended = True


@dataclass
class RecordingLangfuseClient:
    """In-memory Langfuse stub. Captures all spans for inspection.

    Default M0 wiring — orchestrator startup swaps to an SDK-backed
    adapter in production by passing a different client to the
    middleware constructor.
    """

    spans: list[RecordedSpan] = field(default_factory=list)

    def start_span(
        self,
        *,
        name: str,
        input: Any,
        metadata: Mapping[str, Any] | None = None,
    ) -> LangfuseSpan:
        span = RecordedSpan(name=name, input=input, metadata=dict(metadata or {}))
        self.spans.append(span)
        return span


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


@dataclass
class LangfuseMiddleware:
    """Wrap the LLM call with a Langfuse span.

    Failures inside the client (``start_span`` raises, ``end`` raises,
    etc.) are swallowed with a warn log — never propagated. Tracing
    outages must not take down LLM serving. Each swallowed failure is
    also counted in ``expert_work_langfuse_delivery_failures_total``.
    """

    client: LangfuseClient

    name: str = "langfuse"
    anchor: str = "around_llm_call"
    after: tuple[str, ...] = field(default_factory=tuple)
    #: Langfuse must wrap ``llm_error_handling`` so retries live inside
    #: one span; ``before=("llm_error_handling",)`` puts us outermost.
    before: tuple[str, ...] = field(default_factory=lambda: ("llm_error_handling",))

    async def __call__(self, ctx: MiddlewareContext, call_next: CallNext) -> None:
        span = self._start_span_safe(ctx)
        try:
            await call_next(ctx)
        except BaseException as exc:
            self._record_error_safe(span, exc)
            raise
        else:
            self._record_response_safe(span, ctx)
        finally:
            self._end_safe(span)

    # ------------------------------------------------------------------
    # Fail-soft helpers — Langfuse outage must never crash an LLM call.
    # ------------------------------------------------------------------

    def _start_span_safe(self, ctx: MiddlewareContext) -> LangfuseSpan | None:
        try:
            metadata: dict[str, Any] = {}
            for key in ("model", "tenant_id", "trace_id", "session_id", "run_id"):
                if key in ctx.payload:
                    metadata[key] = ctx.payload[key]
            return self.client.start_span(
                name=str(ctx.payload.get("agent_name", "llm_call")),
                input=ctx.payload.get("messages"),
                metadata=metadata,
            )
        except Exception:
            logger.warning("langfuse.start_span_failed", exc_info=True)
            record_delivery_failure("start_span")
            return None

    def _record_response_safe(
        self,
        span: LangfuseSpan | None,
        ctx: MiddlewareContext,
    ) -> None:
        if span is None:
            return
        response = ctx.payload.get("llm_response")
        if not isinstance(response, Mapping):
            return
        try:
            if "output" in response:
                span.record_output(response["output"])
            usage = response.get("usage")
            if isinstance(usage, Mapping):
                span.record_usage(usage)
        except Exception:
            logger.warning("langfuse.record_response_failed", exc_info=True)
            record_delivery_failure("record_response")

    def _record_error_safe(
        self,
        span: LangfuseSpan | None,
        exc: BaseException,
    ) -> None:
        if span is None:
            return
        try:
            span.record_error(exc)
        except Exception:
            logger.warning("langfuse.record_error_failed", exc_info=True)
            record_delivery_failure("record_error")

    def _end_safe(self, span: LangfuseSpan | None) -> None:
        if span is None:
            return
        try:
            span.end()
        except Exception:
            logger.warning("langfuse.end_failed", exc_info=True)
            record_delivery_failure("end")
