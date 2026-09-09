"""Tests for :class:`control_plane.middleware.ObservabilityMiddleware`."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.responses import JSONResponse

from control_plane.middleware import ObservabilityMiddleware
from control_plane.middleware.observability import TRACE_ID_HEADER, _request_total
from expert_work.common.observability import init_tracing


@pytest.fixture(scope="module")
def tracer_provider() -> TracerProvider:
    exporter = InMemorySpanExporter()
    provider: TracerProvider = init_tracing(
        service_name="control_plane_test",
        env="dev",
        span_processor=SimpleSpanProcessor(exporter),
    )
    # The exporter ref lives on the processor; tests pull it off via globals.
    provider.test_exporter = exporter  # type: ignore[attr-defined]
    return provider


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)

    @app.get("/ping")
    async def ping() -> JSONResponse:
        return JSONResponse({"pong": True})

    return app


@pytest.mark.asyncio
async def test_response_carries_trace_id_header(tracer_provider: TracerProvider) -> None:
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ping")
    assert response.status_code == 200
    assert TRACE_ID_HEADER in response.headers
    # Hex trace id is 32 chars per W3C spec.
    assert len(response.headers[TRACE_ID_HEADER]) == 32


@pytest.mark.asyncio
async def test_incoming_traceparent_is_honoured(tracer_provider: TracerProvider) -> None:
    """A request that arrives with a ``traceparent`` header must reuse that
    trace id (so distributed traces stitch together)."""
    incoming_trace_id = "0af7651916cd43dd8448eb211c80319c"
    traceparent = f"00-{incoming_trace_id}-b7ad6b7169203331-01"

    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ping", headers={"traceparent": traceparent})
    assert response.headers[TRACE_ID_HEADER] == incoming_trace_id


@pytest.mark.asyncio
async def test_span_records_status_code(tracer_provider: TracerProvider) -> None:
    app = _build_app()
    exporter: InMemorySpanExporter = tracer_provider.test_exporter  # type: ignore[attr-defined]
    exporter.clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/ping")

    spans = exporter.get_finished_spans()
    http_spans = [s for s in spans if s.name == "expert_work.control_plane.http_request"]
    assert http_spans, "expected at least one expert_work.control_plane.http_request span"
    last = http_spans[-1]
    assert last.attributes is not None
    assert last.attributes.get("http.method") == "GET"
    assert last.attributes.get("http.status_code") == 200


def _build_probe_app() -> FastAPI:
    """App shaped like the real control plane: probes + scrape + one API route."""
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)

    @app.get("/healthz/live")
    async def live() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/healthz/ready")
    async def ready() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/metrics")
    async def metrics() -> JSONResponse:
        return JSONResponse({"scrape": True})

    @app.get("/ping")
    async def ping() -> JSONResponse:
        return JSONResponse({"pong": True})

    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/healthz/live", "/healthz/ready", "/metrics"])
async def test_probe_and_scrape_paths_open_no_span(
    tracer_provider: TracerProvider, path: str
) -> None:
    """X-7 ④ — kubelet probes + Prometheus scrapes must not create traces.

    Each probe hit used to open an ``http_request`` root span — thousands a
    day drowning the real runs in Tempo. Only the span (and the trace-id
    echo) is skipped; the request counter/histogram still see the hit.
    """
    app = _build_probe_app()
    exporter: InMemorySpanExporter = tracer_provider.test_exporter  # type: ignore[attr-defined]
    exporter.clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)

    assert response.status_code == 200
    assert TRACE_ID_HEADER not in response.headers
    assert exporter.get_finished_spans() == ()


@pytest.mark.asyncio
async def test_probe_exclusion_does_not_leak_to_api_routes(
    tracer_provider: TracerProvider,
) -> None:
    """Sibling API routes still get their span after a probe hit."""
    app = _build_probe_app()
    exporter: InMemorySpanExporter = tracer_provider.test_exporter  # type: ignore[attr-defined]
    exporter.clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/healthz/live")
        response = await client.get("/ping")

    assert TRACE_ID_HEADER in response.headers
    targets = [
        s.attributes.get("http.target")
        for s in exporter.get_finished_spans()
        if s.name == "expert_work.control_plane.http_request" and s.attributes is not None
    ]
    assert targets == ["/ping"]


@pytest.mark.asyncio
async def test_probe_paths_still_counted_in_request_metrics(
    tracer_provider: TracerProvider,
) -> None:
    """No span is not no metric: the SLO counter must keep seeing probe hits."""
    app = _build_probe_app()
    labels = {"method": "GET", "route": "/healthz/live", "status_code": "200"}
    before = _request_total.labels(**labels)._value.get()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/healthz/live")

    assert _request_total.labels(**labels)._value.get() == before + 1
