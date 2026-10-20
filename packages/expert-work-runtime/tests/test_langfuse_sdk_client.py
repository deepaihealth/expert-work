"""Unit tests for the Langfuse SDK adapter — Stream HX-7 (§ 8.4-PR1).

A fake SDK object is injected (no real Langfuse instance; CI has no
credentials) — these prove the protocol→SDK mapping and the factory's
degrade-to-recording behaviour.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY

from expert_work.runtime.middleware import (
    LangfuseClient,
    LangfuseSdkClient,
    RecordingLangfuseClient,
    make_langfuse_client,
)
from expert_work.runtime.middleware.langfuse_sdk import (
    FailureCountingSpanExporter,
    build_langfuse_span_exporter,
    should_export_span,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeGeneration:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.ended = False

    def update(self, **kwargs: Any) -> _FakeGeneration:
        self.updates.append(kwargs)
        return self

    def end(self) -> _FakeGeneration:
        self.ended = True
        return self


class _FakeSdk:
    def __init__(self) -> None:
        self.generations: list[dict[str, Any]] = []
        self.created: list[_FakeGeneration] = []
        self.flushed = 0
        self.shutdowns = 0

    def start_observation(self, **kwargs: Any) -> _FakeGeneration:
        self.generations.append(kwargs)
        generation = _FakeGeneration()
        self.created.append(generation)
        return generation

    def flush(self) -> None:
        self.flushed += 1

    def shutdown(self) -> None:
        self.shutdowns += 1


# ---------------------------------------------------------------------------
# Contract with the INSTALLED langfuse SDK
# ---------------------------------------------------------------------------
#
# The fakes above are what let a major SDK upgrade slip through: #1233
# (2026-08-20) bumped langfuse 3.15 → 4.14, which removed
# ``Langfuse.start_generation``. Every adapter test kept passing against the
# fake, while production raised AttributeError on every LLM call — swallowed
# by the middleware's fail-soft guard as ``langfuse.start_span_failed`` —
# and Langfuse received nothing for six weeks. These two tests run the
# adapter against the real class, so the next API change fails CI instead.


def test_adapter_only_calls_methods_the_installed_sdk_has() -> None:
    from unittest.mock import create_autospec

    from langfuse import Langfuse
    from langfuse._client.span import LangfuseGeneration

    sdk = create_autospec(Langfuse, instance=True)
    sdk.start_observation.return_value = create_autospec(LangfuseGeneration, instance=True)

    span = LangfuseSdkClient(sdk).start_span(
        name="agent", input=[{"role": "user", "content": "hi"}], metadata={"model": "glm-5.3"}
    )
    span.record_output("answer")
    span.record_usage({"input_tokens": 10, "output_tokens": 3})
    span.record_error(RuntimeError("provider down"))
    span.end()

    sdk.start_observation.assert_called_once()
    assert sdk.start_observation.call_args.kwargs["as_type"] == "generation"


def test_adapter_runs_against_a_real_langfuse_instance() -> None:
    """A real client with export switched off — no network, but every call goes
    through the SDK's own signatures and return types."""
    from langfuse import Langfuse

    sdk = Langfuse(
        public_key="pk-lf-contract-test",
        secret_key="sk-lf-contract-test",
        host="http://127.0.0.1:9",
        tracing_enabled=False,
    )
    span = LangfuseSdkClient(sdk).start_span(name="agent", input="x", metadata={"model": "m"})
    span.record_output("y")
    span.record_usage({"input_tokens": 1})
    span.record_error(RuntimeError("e"))
    span.end()


def test_model_parameters_land_on_the_langfuse_model_parameters_attribute() -> None:
    """B-187 — through the installed SDK, export captured in memory: the
    request parameters ride ``langfuse.observation.model.parameters`` (the
    SDK's ``LangfuseOtelSpanAttributes.OBSERVATION_MODEL_PARAMETERS``), which
    Langfuse shows as the generation's model parameters."""
    from langfuse import Langfuse

    exporter = InMemorySpanExporter()
    sdk = Langfuse(
        public_key="pk-lf-b187-contract-test",
        secret_key="sk-lf-b187-contract-test",
        host="http://127.0.0.1:9",
        # Own provider: the SDK must not install itself as the global one.
        tracer_provider=TracerProvider(),
        span_exporter=exporter,
    )
    params = {"temperature": 1.0, "max_tokens": 4096, "thinking.type": "enabled"}
    try:
        span = LangfuseSdkClient(sdk).start_span(
            name="llm_call", input="x", metadata={"model": "kimi-k3"}, model_parameters=params
        )
        span.end()
        sdk.flush()
    finally:
        sdk.shutdown()

    [exported] = exporter.get_finished_spans()
    attributes = exported.attributes or {}
    assert json.loads(str(attributes["langfuse.observation.model.parameters"])) == params
    assert attributes["langfuse.observation.model.name"] == "kimi-k3"


# ---------------------------------------------------------------------------
# Adapter mapping
# ---------------------------------------------------------------------------


def test_start_span_maps_to_generation_with_model() -> None:
    sdk = _FakeSdk()
    client = LangfuseSdkClient(sdk)

    span = client.start_span(
        name="my-agent",
        input=[{"role": "user", "content": "hi"}],
        metadata={"model": "qwen-max", "tenant_id": "t-1", "run_id": "r-1"},
    )

    assert isinstance(client, LangfuseClient)  # protocol conformance
    assert sdk.generations == [
        {
            "name": "my-agent",
            "as_type": "generation",
            "input": [{"role": "user", "content": "hi"}],
            "metadata": {"model": "qwen-max", "tenant_id": "t-1", "run_id": "r-1"},
            "model": "qwen-max",
            "model_parameters": None,
        }
    ]
    span.end()
    assert sdk.created[0].ended is True


def test_start_span_passes_model_parameters() -> None:
    sdk = _FakeSdk()
    params = {"temperature": 0.2, "reasoning_effort": "high"}
    LangfuseSdkClient(sdk).start_span(name="n", input=None, model_parameters=params)
    assert sdk.generations[0]["model_parameters"] == params


def test_start_span_without_model_passes_none() -> None:
    sdk = _FakeSdk()
    LangfuseSdkClient(sdk).start_span(name="n", input=None, metadata=None)
    assert sdk.generations[0]["model"] is None
    assert sdk.generations[0]["metadata"] == {}


def test_record_output_and_usage_map_to_update() -> None:
    sdk = _FakeSdk()
    span = LangfuseSdkClient(sdk).start_span(name="n", input="x", metadata={})

    span.record_output("the answer")
    span.record_usage({"input_tokens": 10, "output_tokens": 3})

    generation = sdk.created[0]
    assert generation.updates[0] == {"output": "the answer"}
    assert generation.updates[1] == {"usage_details": {"input_tokens": 10, "output_tokens": 3}}


def test_record_error_marks_level_error() -> None:
    sdk = _FakeSdk()
    span = LangfuseSdkClient(sdk).start_span(name="n", input="x", metadata={})

    span.record_error(RuntimeError("provider down"))

    assert sdk.created[0].updates == [
        {"level": "ERROR", "status_message": "RuntimeError: provider down"}
    ]


def test_flush_and_shutdown_delegate() -> None:
    sdk = _FakeSdk()
    client = LangfuseSdkClient(sdk)
    client.flush()
    client.shutdown()
    assert sdk.flushed == 1
    assert sdk.shutdowns == 1


# ---------------------------------------------------------------------------
# Factory resolution (Mini-ADR HX-G3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "public", "secret"),
    [
        (None, None, None),
        ("https://langfuse.local", None, "sk"),
        ("https://langfuse.local", "pk", None),
        (None, "pk", "sk"),
        ("", "pk", "sk"),
    ],
)
def test_factory_incomplete_settings_degrade_to_recording(
    host: str | None, public: str | None, secret: str | None
) -> None:
    client = make_langfuse_client(host=host, public_key=public, secret_key=secret)
    assert isinstance(client, RecordingLangfuseClient)


def test_factory_import_failure_degrades_to_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulate a broken install: ``import langfuse`` must fail inside the
    # factory. A None entry in sys.modules raises ImportError on import.
    monkeypatch.setitem(sys.modules, "langfuse", None)
    client = make_langfuse_client(host="https://langfuse.local", public_key="pk", secret_key="sk")
    assert isinstance(client, RecordingLangfuseClient)


def test_factory_complete_settings_build_sdk_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: list[dict[str, Any]] = []

    class _FakeLangfuse:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(kwargs)

    import types

    fake_module = types.ModuleType("langfuse")
    fake_module.Langfuse = _FakeLangfuse  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langfuse", fake_module)

    client = make_langfuse_client(host="https://langfuse.local", public_key="pk", secret_key="sk")

    assert isinstance(client, LangfuseSdkClient)
    assert len(constructed) == 1
    kwargs = constructed[0]
    # PII masking defaults on (Mini-ADR OBS-L1, decision 4 — fail-safe).
    mask = kwargs.pop("mask")
    assert callable(mask)
    # B-153 follow-up — the export-failure counting seam.
    span_exporter = kwargs.pop("span_exporter")
    assert isinstance(span_exporter, FailureCountingSpanExporter)
    # B-201 — our own spans pass the SDK's export filter.
    assert kwargs.pop("should_export_span") is should_export_span
    assert kwargs == {
        "public_key": "pk",
        "secret_key": "sk",
        "host": "https://langfuse.local",
        "tracing_enabled": True,
    }


def _capture_langfuse_kwargs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Install a fake ``langfuse.Langfuse`` that records its constructor kwargs."""
    constructed: list[dict[str, Any]] = []

    class _FakeLangfuse:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(kwargs)

    import types

    fake_module = types.ModuleType("langfuse")
    fake_module.Langfuse = _FakeLangfuse  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langfuse", fake_module)
    return constructed


def test_factory_default_mask_redacts_pii_in_nested_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default mask (Mini-ADR OBS-L1) must scrub conversational PII out of
    the nested message shapes Langfuse ingests — before they hit ClickHouse."""
    constructed = _capture_langfuse_kwargs(monkeypatch)
    make_langfuse_client(host="https://langfuse.local", public_key="pk", secret_key="sk")

    mask = constructed[0]["mask"]
    masked = mask(
        data={
            "messages": [{"role": "user", "content": "email me at alice@example.com"}],
            "model": "qwen-max",
        }
    )
    assert "alice@example.com" not in masked["messages"][0]["content"]
    assert masked["model"] == "qwen-max"  # clean leaf untouched


def test_factory_default_mask_redacts_secrets_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed = _capture_langfuse_kwargs(monkeypatch)
    make_langfuse_client(host="https://langfuse.local", public_key="pk", secret_key="sk")

    mask = constructed[0]["mask"]
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in mask(data="key sk-ABCDEFGHIJKLMNOPQRSTUVWX")


def test_factory_masking_disabled_passes_no_mask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The escape hatch: explicit opt-out drops the mask kwarg entirely."""
    constructed = _capture_langfuse_kwargs(monkeypatch)
    make_langfuse_client(
        host="https://langfuse.local",
        public_key="pk",
        secret_key="sk",
        pii_masking_enabled=False,
    )
    assert "mask" not in constructed[0]


# ---------------------------------------------------------------------------
# B-153 follow-up — export failures are counted
# ---------------------------------------------------------------------------
#
# The SDK ships spans from an OTel BatchSpanProcessor thread; it discards the
# exporter's FAILURE result and only logs (opentelemetry/sdk/_shared_internal
# ``BatchProcessor._export``). That log line shares its logger with our Tempo
# exporter, so the only Langfuse-specific seam is the exporter itself — the
# SDK's public ``span_exporter=`` argument.


def _export_failures() -> float:
    return (
        REGISTRY.get_sample_value(
            "expert_work_langfuse_delivery_failures_total", {"stage": "export"}
        )
        or 0.0
    )


def _finished_span() -> ReadableSpan:
    span = TracerProvider().get_tracer("b153-test").start_span("s")
    span.end()
    assert isinstance(span, ReadableSpan)
    return span


class _FakeExporter(SpanExporter):
    def __init__(
        self,
        result: SpanExportResult = SpanExportResult.SUCCESS,
        exc: Exception | None = None,
    ) -> None:
        self.result = result
        self.exc = exc
        self.batches: list[Sequence[ReadableSpan]] = []
        self.shutdowns = 0
        self.flush_timeouts: list[int] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.batches.append(spans)
        if self.exc is not None:
            raise self.exc
        return self.result

    def shutdown(self) -> None:
        self.shutdowns += 1

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        self.flush_timeouts.append(timeout_millis)
        return True


def test_export_failure_result_is_counted_and_passed_through() -> None:
    inner = _FakeExporter(result=SpanExportResult.FAILURE)
    spans = [_finished_span()]
    before = _export_failures()

    result = FailureCountingSpanExporter(inner).export(spans)

    assert result is SpanExportResult.FAILURE
    assert inner.batches == [spans]
    assert _export_failures() - before == 1


def test_export_exception_is_counted_and_reraised() -> None:
    inner = _FakeExporter(exc=ConnectionError("langfuse-web unreachable"))
    before = _export_failures()

    with pytest.raises(ConnectionError):
        FailureCountingSpanExporter(inner).export([_finished_span()])

    assert _export_failures() - before == 1


def test_export_success_is_not_counted() -> None:
    inner = _FakeExporter(result=SpanExportResult.SUCCESS)
    before = _export_failures()

    result = FailureCountingSpanExporter(inner).export([_finished_span()])

    assert result is SpanExportResult.SUCCESS
    assert _export_failures() == before


def test_counting_exporter_delegates_shutdown_and_flush() -> None:
    inner = _FakeExporter()
    exporter = FailureCountingSpanExporter(inner)

    assert exporter.force_flush(1234) is True
    exporter.shutdown()

    assert inner.flush_timeouts == [1234]
    assert inner.shutdowns == 1


class _Reject401(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(401)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


def test_real_otlp_rejection_is_counted() -> None:
    """The real OTLP exporter against an endpoint that answers 401 (a wrong
    key) — the failure mode the counter exists for, end to end through the
    exporter's own HTTP client."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Reject401)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_address[1]}/api/public/otel/v1/traces"
        exporter = FailureCountingSpanExporter(OTLPSpanExporter(endpoint=endpoint, timeout=5))
        before = _export_failures()

        result = exporter.export([_finished_span()])

        assert result is SpanExportResult.FAILURE
        assert _export_failures() - before == 1
        exporter.shutdown()
    finally:
        server.shutdown()
        server.server_close()


def test_real_sdk_export_failure_reaches_the_counter() -> None:
    """Whole chain through the installed SDK: ``Langfuse(span_exporter=...)``
    really ships through our exporter (inside its own media/mask wrapper), and
    a rejected batch lands in the counter. A private TracerProvider keeps the
    process-global one untouched."""
    from langfuse import Langfuse

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Reject401)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host = f"http://127.0.0.1:{server.server_address[1]}"
        public_key, secret_key = "pk-lf-b153-chain", "sk-lf-b153-chain"
        sdk = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
            tracing_enabled=True,
            tracer_provider=TracerProvider(),
            span_exporter=build_langfuse_span_exporter(
                host=host, public_key=public_key, secret_key=secret_key
            ),
        )
        before = _export_failures()

        LangfuseSdkClient(sdk).start_span(name="agent", input="x", metadata={}).end()
        sdk.flush()

        assert _export_failures() - before == 1
        sdk.shutdown()
    finally:
        server.shutdown()
        server.server_close()


def test_counting_exporter_ships_exactly_like_the_sdk_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing ``span_exporter=`` makes the SDK skip its own endpoint / auth /
    timeout wiring, so ours must match it field for field. Pinned against the
    installed SDK's own default exporter (private attributes, test-only): a
    langfuse upgrade that changes the default fails here, not in production."""
    from langfuse._client.span_processor import LangfuseSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    monkeypatch.delenv("LANGFUSE_TIMEOUT", raising=False)
    monkeypatch.delenv("LANGFUSE_OTEL_TRACES_EXPORT_PATH", raising=False)
    host, public_key, secret_key = "https://langfuse.local", "pk-lf-x", "sk-lf-x"

    # langfuse/_client/client.py resolves ``timeout or LANGFUSE_TIMEOUT or 5``
    # before handing it to the processor.
    sdk_processor = LangfuseSpanProcessor(
        public_key=public_key, secret_key=secret_key, base_url=host, timeout=5
    )
    try:
        sdk_default = sdk_processor._batch_processor._exporter
        ours = build_langfuse_span_exporter(host=host, public_key=public_key, secret_key=secret_key)
        inner = ours._inner
        assert isinstance(sdk_default, OTLPSpanExporter)
        assert isinstance(inner, OTLPSpanExporter)
        assert inner._endpoint == sdk_default._endpoint
        assert inner._client._headers == sdk_default._client._headers
        assert inner._client._timeout == sdk_default._client._timeout
        ours.shutdown()
    finally:
        sdk_processor.shutdown()  # type: ignore[no-untyped-call]


# ---------------------------------------------------------------------------
# B-201 — our own spans reach Langfuse, not only the generations under them
# ---------------------------------------------------------------------------
#
# langfuse 4.x exports only what its default filter calls LLM-related: its own
# tracer, spans with ``gen_ai.*`` attributes, and a list of known LLM
# instrumentation scopes. The ``expert-work`` tracer is none of those, so every
# request / run / node / tool span was dropped and only the generations were
# kept — each observation in Langfuse pointed at a parent that never arrived,
# and the console's trace view stayed on "not ready" forever.


def test_factory_exports_our_spans_with_the_generations_under_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chain as the control-plane wires it: a process TracerProvider (what
    ``init_tracing`` installs), the SDK client from ``make_langfuse_client``
    attaching its processor to that provider, spans from ``expert_work_span``
    and a generation from the adapter underneath them. Only the export
    endpoint is swapped for an in-memory exporter."""
    from opentelemetry import trace

    from expert_work.common.observability import ExpertWorkComponent, expert_work_span
    from expert_work.runtime.middleware import langfuse_sdk

    provider = TracerProvider()
    # Stands in for the process-global provider without replacing the real one
    # (OTel only lets it be set once per process).
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(langfuse_sdk, "build_langfuse_span_exporter", lambda **_: exporter)

    client = make_langfuse_client(
        host="http://127.0.0.1:9",
        # The SDK keeps one set of resources per public key — a fresh key keeps
        # this client from reusing another test's processor.
        public_key=f"pk-lf-b201-{uuid4().hex}",
        secret_key="sk-lf-b201",
    )
    assert isinstance(client, LangfuseSdkClient)
    try:
        with (
            expert_work_span(ExpertWorkComponent.SESSION, "run"),
            expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "llm_call"),
        ):
            client.start_span(name="llm_call", input="x", metadata={"model": "m"}).end()
        client.flush()
    finally:
        client.shutdown()

    exported = exporter.get_finished_spans()
    assert sorted(span.name for span in exported) == [
        "expert_work.orchestrator.llm_call",
        "expert_work.session.run",
        "llm_call",
    ]
    exported_ids = {span.context.span_id for span in exported}
    assert all(span.parent is None or span.parent.span_id in exported_ids for span in exported)


def _span_from(scope: str, attributes: dict[str, str] | None = None) -> ReadableSpan:
    span = TracerProvider().get_tracer(scope).start_span("s", attributes=attributes)
    span.end()
    assert isinstance(span, ReadableSpan)
    return span


def test_export_filter_adds_our_tracer_and_keeps_the_sdk_default() -> None:
    from langfuse.span_filter import is_default_export_span

    from expert_work.common.observability import TRACER_NAME

    ours = _span_from(TRACER_NAME)
    assert not is_default_export_span(ours)  # what B-201 was: the SDK drops it
    assert should_export_span(ours)
    assert should_export_span(_span_from("langfuse-sdk"))
    assert should_export_span(_span_from("some.http.client", {"gen_ai.system": "openai"}))
    # Still a filter: an unrelated library's spans stay out of Langfuse.
    assert not should_export_span(_span_from("sqlalchemy"))
