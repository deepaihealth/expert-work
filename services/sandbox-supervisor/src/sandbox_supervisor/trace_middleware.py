"""Inbound W3C trace context middleware — Stream A.8.

Mirrors the control-plane ``ObservabilityMiddleware`` (subsystems/20 § 5.8):
extract the incoming ``traceparent`` so spans opened here continue the
caller's trace instead of starting a detached root. The orchestrator's
:class:`HTTPSupervisorRuntime` injects that header on every internal hop;
this middleware is the matching extraction side.

Deliberately leaner than control-plane's: no per-request Prometheus
metrics (that is A.9's surface and the supervisor does not expose HTTP
request metrics today). Trace continuity + ``trace_id`` log binding only.

No incoming ``traceparent`` (ops / tests calling the supervisor directly)
→ a fresh root span, fully backward compatible.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from opentelemetry import context as otel_context
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from expert_work.common.context import reset_current_trace_id, set_current_trace_id
from expert_work.common.observability import (
    ExpertWorkComponent,
    current_trace_id_hex,
    expert_work_span,
    extract_context,
)

TRACE_ID_HEADER = "X-Expert-Work-Trace-Id"


class TraceContextMiddleware(BaseHTTPMiddleware):
    """Continue the caller's W3C trace across the orchestrator → supervisor hop."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        parent_ctx = extract_context(dict(request.headers))
        ctx_token = otel_context.attach(parent_ctx)
        try:
            with expert_work_span(
                ExpertWorkComponent.SANDBOX,
                "http_request",
                attributes={
                    "http.method": request.method,
                    "http.target": request.url.path,
                },
            ) as span:
                trace_id_hex = current_trace_id_hex()
                trace_token = None
                if trace_id_hex is not None:
                    trace_token = set_current_trace_id(trace_id_hex)
                try:
                    response = await call_next(request)
                finally:
                    if trace_token is not None:
                        reset_current_trace_id(trace_token)

                span.set_attribute("http.status_code", response.status_code)
                if trace_id_hex is not None:
                    response.headers[TRACE_ID_HEADER] = trace_id_hex
                return response
        finally:
            otel_context.detach(ctx_token)
