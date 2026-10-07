"""A.8 — ``TraceContextMiddleware`` continues the caller's W3C trace.

The extract half of the cross-service propagation contract (subsystems/20
§ 5.8). The matching inject half lives in the orchestrator suite
(``test_sandbox_trace_propagation.py``); both meet at the ``traceparent``
wire format.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from expert_work.common.observability import (
    ExpertWorkComponent,
    current_trace_id_hex,
    expert_work_span,
    init_tracing,
    inject_context,
)
from sandbox_supervisor.app import create_app
from sandbox_supervisor.settings import SandboxSupervisorSettings
from sandbox_supervisor.trace_middleware import (
    NATIVE_TELEMETRY_OFF,
    TRACE_ID_HEADER,
    TraceContextMiddleware,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def tracing_setup() -> Iterator[None]:
    provider = init_tracing(
        service_name="supervisor-trace-test",
        env="test",
        span_processor=SimpleSpanProcessor(InMemorySpanExporter()),
    )
    try:
        yield
    finally:
        provider.shutdown()


def _probe_app() -> FastAPI:
    app = FastAPI(telemetry=NATIVE_TELEMETRY_OFF)
    app.add_middleware(TraceContextMiddleware)

    @app.get("/probe")
    async def probe() -> dict[str, str | None]:
        # Runs inside the middleware span → reports the server-side trace_id.
        return {"trace_id": current_trace_id_hex()}

    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://supervisor")


async def test_incoming_traceparent_continues_trace(tracing_setup: None) -> None:
    # Craft a real incoming traceparent the way the orchestrator would emit it.
    with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "outbound"):
        upstream_trace_id = current_trace_id_hex()
        headers: dict[str, str] = {}
        inject_context(headers)
    assert upstream_trace_id is not None

    async with _client(_probe_app()) as client:
        resp = await client.get("/probe", headers=headers)

    assert resp.status_code == 200
    # Server-side span joined the caller's trace.
    assert resp.json()["trace_id"] == upstream_trace_id
    assert resp.headers[TRACE_ID_HEADER] == upstream_trace_id


async def test_no_traceparent_starts_fresh_root(tracing_setup: None) -> None:
    # Direct ops / test call with no header → a fresh root span, no error.
    async with _client(_probe_app()) as client:
        resp = await client.get("/probe")

    assert resp.status_code == 200
    server_trace_id = resp.json()["trace_id"]
    assert server_trace_id is not None and server_trace_id != "0" * 32
    assert resp.headers[TRACE_ID_HEADER] == server_trace_id


async def test_real_app_opens_no_fastapi_native_spans() -> None:
    """FastAPI 0.142 instruments itself once a global tracer provider exists;
    ``create_app`` turns that off (our ``TraceContextMiddleware`` owns the span)."""
    exporter = InMemorySpanExporter()
    provider = init_tracing(
        service_name="supervisor-trace-test",
        env="test",
        span_processor=SimpleSpanProcessor(exporter),
    )
    try:
        app = create_app(SandboxSupervisorSettings(), enable_reaper=False)
        async with _client(app) as client:
            await client.get("/no-such-route")
        scopes = {
            s.instrumentation_scope.name
            for s in exporter.get_finished_spans()
            if s.instrumentation_scope is not None
        }
        assert "fastapi" not in scopes
    finally:
        provider.shutdown()
