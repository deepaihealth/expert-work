"""Observability middleware — Stream B.1.

Per [STREAM-B-DESIGN § 2.2](../../../../docs/streams/STREAM-B-DESIGN.md):

* extract incoming W3C trace context (``traceparent`` / ``tracestate``)
* open a ``expert_work.control_plane.http_request`` span (subsystems/20 § 5.1
  naming) covering ``call_next``
* bind ``trace_id`` into :mod:`expert_work.common.context` so structured
  logging picks it up
* record per-request count + duration via the Prometheus registry from
  Stream A.9
* echo the trace id back in ``X-Expert-Work-Trace-Id`` so clients can
  correlate without parsing ``traceparent``
"""

from __future__ import annotations

import time
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
    expert_work_counter,
    expert_work_histogram,
    expert_work_span,
    extract_context,
)

TRACE_ID_HEADER = "X-Expert-Work-Trace-Id"

#: Paths that never open a span (X-7 ④). kubelet hits the liveness/readiness
#: probes every few seconds and Prometheus scrapes ``/metrics`` every 15s —
#: each used to become an empty root trace, thousands a day drowning the real
#: runs in Tempo. Exact-path match on the raw request path (the route is not
#: resolved yet when the outermost middleware runs). The request counter /
#: histogram still record these hits — the availability SLO in
#: tools/observability/rules/sli.yml counts every request.
TRACE_EXCLUDED_PATHS: frozenset[str] = frozenset({"/healthz/live", "/healthz/ready", "/metrics"})

# ---------------------------------------------------------------------------
# Metrics — registered exactly once at import time (Stream A.9 helpers handle
# duplicate registration by raising, which is fine: each control_plane process
# imports this module once).
# ---------------------------------------------------------------------------
_request_total = expert_work_counter(
    "expert_work_control_plane_http_requests_total",
    "Control Plane HTTP requests, partitioned by method/route/status_code.",
    ("method", "route", "status_code"),
)
_request_duration = expert_work_histogram(
    "expert_work_control_plane_http_request_duration_seconds",
    "Control Plane HTTP request handler wall-clock duration.",
    ("method", "route", "status_code"),
)


def _route_template(request: Request) -> str:
    """Return the matched route template or ``_unmatched`` (low cardinality)."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return "_unmatched"


def _record_request(request: Request, status_code: int, start: float) -> None:
    duration = time.perf_counter() - start
    labels = {
        "method": request.method,
        "route": _route_template(request),
        "status_code": str(status_code),
    }
    _request_total.labels(**labels).inc()
    _request_duration.labels(**labels).observe(duration)


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Outermost middleware (sees raw request / final response)."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path in TRACE_EXCLUDED_PATHS:
            return await self._dispatch_untraced(request, call_next)

        parent_ctx = extract_context(dict(request.headers))
        ctx_token = otel_context.attach(parent_ctx)

        start = time.perf_counter()
        method = request.method
        status_code = 500  # default if call_next blows up before producing a response

        try:
            with expert_work_span(
                ExpertWorkComponent.CONTROL_PLANE,
                "http_request",
                attributes={
                    "http.method": method,
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

                status_code = response.status_code
                span.set_attribute("http.status_code", status_code)
                span.set_attribute("http.route", _route_template(request))
                if trace_id_hex is not None:
                    response.headers[TRACE_ID_HEADER] = trace_id_hex
                return response
        finally:
            _record_request(request, status_code, start)
            otel_context.detach(ctx_token)

    async def _dispatch_untraced(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Probe / scrape path: metrics only, no span, no trace-id binding."""
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            _record_request(request, status_code, start)
