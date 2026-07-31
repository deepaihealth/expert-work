"""Unit tests for :mod:`expert_work.common.observability.tracing`.

OTel's ``set_tracer_provider`` is a process-wide one-shot, so we install
the provider once at session scope and reset the in-memory exporter at
each test.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from uuid import UUID

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from expert_work.common.context import (
    reset_current_tenant,
    set_current_tenant,
)
from expert_work.common.observability import (
    ExpertWorkComponent,
    expert_work_span,
    init_tracing,
)


@pytest.fixture(scope="session")
def _shared_exporter() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = init_tracing(
        service_name="test-service",
        env="test",
        span_processor=SimpleSpanProcessor(exporter),
    )
    try:
        yield exporter
    finally:
        provider.shutdown()


@pytest.fixture
def exporter(_shared_exporter: InMemorySpanExporter) -> Iterator[InMemorySpanExporter]:
    """Fresh empty exporter view per test."""
    _shared_exporter.clear()
    yield _shared_exporter
    _shared_exporter.clear()


def test_expert_work_span_uses_canonical_naming(exporter: InMemorySpanExporter) -> None:
    with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "session_run"):
        pass
    spans = list(exporter.get_finished_spans())
    assert [s.name for s in spans] == ["expert_work.orchestrator.session_run"]


def test_expert_work_span_accepts_string_component(exporter: InMemorySpanExporter) -> None:
    with expert_work_span("control_plane", "manifest_create"):
        pass
    spans = list(exporter.get_finished_spans())
    assert [s.name for s in spans] == ["expert_work.control_plane.manifest_create"]


def test_expert_work_span_rejects_unknown_component(exporter: InMemorySpanExporter) -> None:
    # Validation is eager — the constructor raises, not __enter__. Callers
    # can pytest.raises() directly on expert_work_span(...) without entering the
    # context manager (also keeps CodeQL's unreachable-statement check happy).
    with pytest.raises(ValueError, match="unknown expert_work component"):
        expert_work_span("ufo", "anything")


def test_expert_work_span_injects_tenant_from_contextvar(exporter: InMemorySpanExporter) -> None:
    tenant = UUID("00000000-0000-0000-0000-000000000123")
    token = set_current_tenant(tenant)
    try:
        with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "session_run"):
            pass
    finally:
        reset_current_tenant(token)

    [span] = list(exporter.get_finished_spans())
    assert span.attributes is not None
    assert span.attributes["tenant"] == str(tenant)
    assert span.attributes["service"] == "test-service"
    assert span.attributes["env"] == "test"


def test_expert_work_span_caller_attrs_win_on_collision(exporter: InMemorySpanExporter) -> None:
    with expert_work_span(
        ExpertWorkComponent.LLM_GATEWAY,
        "provider_request",
        attributes={"service": "overridden", "model": "claude-opus-4-5"},
    ):
        pass

    [span] = list(exporter.get_finished_spans())
    assert span.attributes is not None
    assert span.attributes["service"] == "overridden"
    assert span.attributes["model"] == "claude-opus-4-5"


def test_expert_work_span_records_exception_and_sets_error_status(
    exporter: InMemorySpanExporter,
) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "session_run"):
            raise RuntimeError("boom")

    [span] = list(exporter.get_finished_spans())
    assert span.status.is_ok is False
    assert "RuntimeError" in (span.status.description or "")
    # OTel records the exception as a span event automatically.
    assert any("exception" in event.name for event in span.events)


def test_reinit_attaches_new_processor_to_existing_provider(
    exporter: InMemorySpanExporter,
) -> None:
    """A second ``init_tracing`` re-uses the live provider and adds the
    new processor — both exporters then see subsequent spans."""
    second_exporter = InMemorySpanExporter()
    init_tracing(
        service_name="test-service-reinit",
        env="test",
        span_processor=SimpleSpanProcessor(second_exporter),
    )

    with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "session_run"):
        pass

    assert len(exporter.get_finished_spans()) == 1
    assert len(second_exporter.get_finished_spans()) == 1


def test_expert_work_span_attaches_links(exporter: InMemorySpanExporter) -> None:
    """``links=`` attaches OTel Span Links so a span can relate to a span
    in a *different* trace (10.1 — subagent / durable-resume linkage)."""
    with expert_work_span(ExpertWorkComponent.SESSION, "run") as parent:
        parent_ctx = parent.get_span_context()

    with expert_work_span(ExpertWorkComponent.SUBAGENT, "run", links=[trace.Link(parent_ctx)]):
        pass

    spans = {s.name: s for s in exporter.get_finished_spans()}
    child = spans["expert_work.subagent.run"]
    assert len(child.links) == 1
    assert child.links[0].context.trace_id == parent_ctx.trace_id


def test_expert_work_span_without_links_has_none(exporter: InMemorySpanExporter) -> None:
    """The default path attaches no links (regression guard for the new
    optional parameter)."""
    with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "llm_call"):
        pass

    [span] = list(exporter.get_finished_spans())
    assert not span.links


# ---------------------------------------------------------------------------
# OTLP exporter opt-in — ``init_tracing`` must not build an exporter when no
# endpoint is configured (a localhost default left BatchSpanProcessor's
# background thread retrying forever in collector-less environments, spamming
# "Transient error ... retrying" WARNINGs on the root logger).
#
# ``trace.set_tracer_provider`` is a process-wide one-shot, so these tests
# never touch the session-scoped global provider: they route ``init_tracing``'s
# re-init path onto a throwaway ``TracerProvider`` and spy on the public
# ``add_span_processor`` API instead of poking at private processor lists.
# ---------------------------------------------------------------------------


def _fresh_provider(monkeypatch: pytest.MonkeyPatch) -> TracerProvider:
    provider = TracerProvider()

    def _get_provider() -> TracerProvider:
        return provider

    monkeypatch.setattr(trace, "get_tracer_provider", _get_provider)
    return provider


def _spy_added_processors(
    provider: TracerProvider, monkeypatch: pytest.MonkeyPatch
) -> list[SpanProcessor]:
    added: list[SpanProcessor] = []
    real_add = provider.add_span_processor

    def _record(span_processor: SpanProcessor) -> None:
        added.append(span_processor)
        real_add(span_processor)

    monkeypatch.setattr(provider, "add_span_processor", _record)
    return added


def test_init_tracing_without_endpoint_installs_no_exporter(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No ``otlp_endpoint`` param + no env var → no processor at all, and an
    INFO breadcrumb explains why no traces will show up."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    provider = _fresh_provider(monkeypatch)
    added = _spy_added_processors(provider, monkeypatch)

    with caplog.at_level(logging.INFO, logger="expert_work.observability.tracing"):
        result = init_tracing(service_name="test-service", env="test")

    assert result is provider
    assert added == []
    own_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "expert_work.observability.tracing"
    ]
    assert any("tracing.export_not_configured" in message for message in own_messages)


def test_init_tracing_with_explicit_endpoint_installs_batch_processor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    provider = _fresh_provider(monkeypatch)
    added = _spy_added_processors(provider, monkeypatch)

    init_tracing(
        service_name="test-service",
        env="test",
        otlp_endpoint="http://127.0.0.1:9/v1/traces",
    )

    assert len(added) == 1
    assert isinstance(added[0], BatchSpanProcessor)
    provider.shutdown()


def test_init_tracing_with_env_endpoint_installs_batch_processor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:9/v1/traces")
    provider = _fresh_provider(monkeypatch)
    added = _spy_added_processors(provider, monkeypatch)

    init_tracing(service_name="test-service", env="test")

    assert len(added) == 1
    assert isinstance(added[0], BatchSpanProcessor)
    provider.shutdown()
