"""OTel SDK initialization + ``expert_work_span`` helper — Stream A.8.

Design: subsystems/20-observability § 4.1 + § 5.1.

Two public surfaces:

- :func:`init_tracing` — process-wide setup. Wires the SDK to an OTLP
  exporter when an endpoint is configured (``otlp_endpoint`` param or
  ``$OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``); no endpoint → no exporter.
  Idempotent across re-invocations.
- :func:`expert_work_span` — context manager that creates a span with the
  ``expert_work.{component}.{action}`` naming contract + auto-injected
  ``tenant`` / ``service`` / ``env`` attributes.

W3C Trace Context extract/inject lives in :mod:`.propagation` so the
naming + propagation concerns stay independently testable.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from enum import StrEnum
from typing import Any, Final

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode, Tracer

from expert_work.common import __version__
from expert_work.common.context import get_current_tenant

logger = logging.getLogger("expert_work.observability.tracing")


class ExpertWorkComponent(StrEnum):
    """Fixed enum of components allowed in ``expert_work.{component}.{action}``.

    Source: subsystems/20-observability § 5.1. Adding a new component
    requires a design-doc update + a PR to this enum so the naming lint
    keeps catching typos.
    """

    CONTROL_PLANE = "control_plane"
    ORCHESTRATOR = "orchestrator"
    SANDBOX = "sandbox"
    CREDENTIAL_PROXY = "credential_proxy"
    MCP_GATEWAY = "mcp_gateway"
    LLM_GATEWAY = "llm_gateway"
    MEMORY = "memory"
    SUBAGENT = "subagent"
    HITL = "hitl"
    EVAL = "eval"
    DR = "dr"
    DB = "db"
    SESSION = "session"
    DURABLE = "durable"
    QUOTA = "quota"
    OBSERVABILITY = "observability"


def _llm_span_name(component: ExpertWorkComponent, action: str) -> str:
    """Build the span name ``_expert_work_span_cm`` would emit for this call.

    Mirrors that function's ``f"expert_work.{component}.{action}"`` format in one
    place so :data:`LLM_SPAN_PURPOSES` keys can never drift from real spans.
    """
    return f"expert_work.{component}.{action}"


#: SINGLE SOURCE OF TRUTH for the debug console's LLM-call purpose contract.
#: Maps every purpose-wrapped LLM span the orchestrator emits (via
#: ``expert_work_span(component, action)``) to a machine ``purpose`` key. The
#: control-plane trace facade attaches a human label per name (``_LLM_LABELS``)
#: and forwards this ``purpose`` to the admin-ui console, which renders
#: auxiliary calls (purpose != "main") apart from the main agent turn.
#: Producers: orchestrator graph_builder/{memory,planner,reflect},
#: context/compressor, output_judge, tools/{vision,knowledge}, plus the
#: control-plane agents API (delegation-policy draft). Any new
#: purpose-wrapped LLM call MUST be added here — ``test_aux_llm_spans`` asserts
#: every emitted span name is a key, and the facade tests assert label parity.
LLM_SPAN_PURPOSES: dict[str, str] = {
    _llm_span_name(ExpertWorkComponent.ORCHESTRATOR, "llm_call"): "main",
    _llm_span_name(ExpertWorkComponent.MEMORY, "extract"): "memory",
    _llm_span_name(ExpertWorkComponent.MEMORY, "verify"): "memory",
    _llm_span_name(ExpertWorkComponent.MEMORY, "reconcile"): "memory",
    _llm_span_name(ExpertWorkComponent.ORCHESTRATOR, "planner"): "planner",
    _llm_span_name(ExpertWorkComponent.ORCHESTRATOR, "reflect"): "reflect",
    _llm_span_name(ExpertWorkComponent.ORCHESTRATOR, "compress"): "compress",
    _llm_span_name(ExpertWorkComponent.ORCHESTRATOR, "judge"): "judge",
    _llm_span_name(ExpertWorkComponent.ORCHESTRATOR, "judge_action"): "judge",
    _llm_span_name(ExpertWorkComponent.ORCHESTRATOR, "vision"): "vision",
    _llm_span_name(ExpertWorkComponent.ORCHESTRATOR, "rerank"): "rerank",
    # 委派增强层 3 — 配置页「生成委派策略」端点的辅助 LLM 起草调用
    # (control-plane api/agents.py::generate_delegation_policy)。
    _llm_span_name(ExpertWorkComponent.CONTROL_PLANE, "delegation_policy"): "delegation_policy",
}


#: SINGLE SOURCE OF TRUTH for the non-LLM traced spans the trace facade
#: labels. Sibling of :data:`LLM_SPAN_PURPOSES`, deliberately separate: that
#: one's values are *LLM call purposes*, and these spans wrap DB / HTTP /
#: pure-CPU work, not LLM calls. The control-plane facade keeps a Chinese
#: label per name (``_SPAN_LABELS``) and its tests assert parity against this
#: set — without the single source, renaming a span here would silently make
#: the console fall back to the raw English name (``_classify``'s default
#: branch) with nothing failing CI.
#:
#: ``_llm_span_name``'s name carries "llm" only as historical baggage — it is
#: simply ``f"expert_work.{component}.{action}"`` — so reusing it here keeps
#: both contracts' name construction identical without adding a second
#: single-purpose alias.
TRACED_SPANS: frozenset[str] = frozenset(
    {
        _llm_span_name(ExpertWorkComponent.MEMORY, "recall"),
        _llm_span_name(ExpertWorkComponent.MEMORY, "resolve_mode"),
        _llm_span_name(ExpertWorkComponent.MEMORY, "embed"),
        _llm_span_name(ExpertWorkComponent.MEMORY, "retrieve"),
        _llm_span_name(ExpertWorkComponent.MEMORY, "rerank"),
        _llm_span_name(ExpertWorkComponent.MEMORY, "bump_access"),
        _llm_span_name(ExpertWorkComponent.ORCHESTRATOR, "workspace_ingest"),
        _llm_span_name(ExpertWorkComponent.ORCHESTRATOR, "context_gates"),
    }
)


_TRACER_NAME: Final[str] = "expert-work"

# Mutable process-wide state populated by ``init_tracing``. The
# ``expert_work_span`` context manager reads these to auto-inject service / env
# attrs without each caller having to pass them.
_service_name: str | None = None
_env: str | None = None


def init_tracing(
    *,
    service_name: str,
    env: str,
    otlp_endpoint: str | None = None,
    span_processor: SpanProcessor | None = None,
) -> TracerProvider:
    """Install a :class:`TracerProvider` for the current process.

    **First call wins.** OTel's ``set_tracer_provider`` is a one-shot —
    subsequent calls only refresh ``service_name`` / ``env`` (so the
    JSON formatter's ``service`` / ``env`` log fields stay correct) and
    add the supplied processor to the existing provider; the provider
    object itself is not replaced.

    In production this matches the intended lifecycle: ``init_tracing``
    runs exactly once at app startup. In tests we use a session-scoped
    fixture for the same reason.

    :param service_name: Logical service (``control_plane`` /
        ``orchestrator``).
    :param env: ``dev`` / ``staging`` / ``prod``.
    :param otlp_endpoint: OTLP HTTP endpoint to export spans to. Falls
        back to ``$OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``. When neither is
        set (and no ``span_processor`` is injected), **no exporter is
        installed** — spans are still created, just not exported.
    :param span_processor: Inject a custom processor (tests pass an
        ``InMemorySpanExporter`` here). When ``None``, a
        :class:`BatchSpanProcessor` + OTLP HTTP exporter is built if an
        endpoint is configured.
    :returns: The active provider (so callers can ``provider.shutdown()``
        on teardown).
    """
    global _service_name, _env
    _service_name = service_name
    _env = env

    if span_processor is None:
        endpoint = otlp_endpoint or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        if endpoint:
            # Lazy import — the OTLP exporter pulls in protobuf + requests,
            # which tests using in-memory exporters don't need.
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            span_processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        else:
            # No collector configured → install no exporter. The old
            # ``http://localhost:4318/v1/traces`` fallback left
            # BatchSpanProcessor's background thread retrying forever in
            # collector-less environments (CI, bare runs), spamming
            # "Transient error ... retrying" WARNINGs on the root logger.
            logger.info(
                "tracing.export_not_configured service=%s env=%s "
                "(set otlp_endpoint or $OTEL_EXPORTER_OTLP_TRACES_ENDPOINT "
                "to export spans)",
                service_name,
                env,
            )

    existing = trace.get_tracer_provider()
    if isinstance(existing, TracerProvider):
        # Re-init path: attach the new processor (if any) to the live provider.
        if span_processor is not None:
            existing.add_span_processor(span_processor)
        logger.info("tracing.reinit service=%s env=%s", service_name, env)
        return existing

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "expert_work",
            "service.version": __version__,
            "deployment.environment": env,
        }
    )
    provider = TracerProvider(resource=resource)
    if span_processor is not None:
        provider.add_span_processor(span_processor)
    trace.set_tracer_provider(provider)
    logger.info("tracing.initialized service=%s env=%s", service_name, env)
    return provider


def get_tracer() -> Tracer:
    """Return the canonical Expert Work tracer.

    Safe to call before :func:`init_tracing` — the API falls back to a
    no-op tracer so unit tests can import code that creates spans
    without a global provider.
    """
    return trace.get_tracer(_TRACER_NAME)


def expert_work_span(
    component: ExpertWorkComponent | str,
    action: str,
    *,
    attributes: Mapping[str, Any] | None = None,
    links: Sequence[trace.Link] | None = None,
) -> AbstractContextManager[Span]:
    """Open a span named ``expert_work.{component}.{action}``.

    Auto-injects ``tenant``, ``service``, ``env`` from contextvars + init
    parameters. Caller-supplied ``attributes`` win on key collision.

    ``links`` attaches OTel Span Links to the new span — used to relate a
    span to a *different* trace (subsystems/20 § 5.5): a subagent's root
    span links back to its parent run, a durable-resume span links to the
    checkpoint it resumed from. Linked spans keep their own trace id.

    The context manager sets the span status to ``ERROR`` automatically
    if the wrapped block raises — matching subsystems/20 § 5.5 (root
    span status carries the failure reason).

    :raises ValueError: if ``component`` is a string outside
        :class:`ExpertWorkComponent` (typos surface **at call time**, not at
        ``__enter__`` — so callers can ``pytest.raises(...)`` directly
        on the constructor).
    """
    comp_value = _validate_component(component)
    return _expert_work_span_cm(comp_value, action, attributes, links)


@contextmanager
def _expert_work_span_cm(
    comp_value: str,
    action: str,
    attributes: Mapping[str, Any] | None,
    links: Sequence[trace.Link] | None = None,
) -> Iterator[Span]:
    span_name = f"expert_work.{comp_value}.{action}"

    attrs: dict[str, Any] = {}
    tenant = get_current_tenant()
    if tenant is not None:
        attrs["tenant"] = str(tenant)
    if _service_name is not None:
        attrs["service"] = _service_name
    if _env is not None:
        attrs["env"] = _env
    if attributes:
        attrs.update(attributes)

    tracer = get_tracer()
    with tracer.start_as_current_span(
        span_name, attributes=attrs, links=list(links) if links else None
    ) as span:
        try:
            yield span
        except BaseException as exc:
            # Use BaseException so KeyboardInterrupt / SystemExit also
            # tag the span — operators want to see those, not just
            # regular exceptions.
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            span.record_exception(exc)
            raise


def _validate_component(component: ExpertWorkComponent | str) -> str:
    if isinstance(component, ExpertWorkComponent):
        return component.value
    try:
        return ExpertWorkComponent(component).value
    except ValueError as exc:
        valid = ", ".join(sorted(c.value for c in ExpertWorkComponent))
        msg = (
            f"unknown expert_work component {component!r}; must be one of: {valid}. "
            "Add a new component to ExpertWorkComponent + subsystems/20 § 5.1 first."
        )
        raise ValueError(msg) from exc
