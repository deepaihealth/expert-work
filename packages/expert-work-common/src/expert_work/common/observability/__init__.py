"""Expert Work observability primitives.

Stream A.7 — structured JSON logging.
Stream A.8 — OTel SDK / W3C Trace Context.
Stream A.9 — Prometheus metrics + CI lint (this batch).
"""

from expert_work.common.observability.log import (
    ExpertWorkJsonFormatter as ExpertWorkJsonFormatter,
)
from expert_work.common.observability.log import (
    ExtrasRedactor as ExtrasRedactor,
)
from expert_work.common.observability.log import (
    get_logger as get_logger,
)
from expert_work.common.observability.log import (
    init_logging as init_logging,
)
from expert_work.common.observability.metrics import (
    BANNED_LABEL_NAMES as BANNED_LABEL_NAMES,
)
from expert_work.common.observability.metrics import (
    MetricNamingError as MetricNamingError,
)
from expert_work.common.observability.metrics import (
    expert_work_counter as expert_work_counter,
)
from expert_work.common.observability.metrics import (
    expert_work_gauge as expert_work_gauge,
)
from expert_work.common.observability.metrics import (
    expert_work_histogram as expert_work_histogram,
)
from expert_work.common.observability.metrics import (
    metrics_text as metrics_text,
)
from expert_work.common.observability.metrics import (
    validate_label_names as validate_label_names,
)
from expert_work.common.observability.metrics import (
    validate_metric_name as validate_metric_name,
)
from expert_work.common.observability.propagation import (
    TRACEPARENT_HEADER as TRACEPARENT_HEADER,
)
from expert_work.common.observability.propagation import (
    TRACESTATE_HEADER as TRACESTATE_HEADER,
)
from expert_work.common.observability.propagation import (
    current_span_id_hex as current_span_id_hex,
)
from expert_work.common.observability.propagation import (
    current_trace_id_hex as current_trace_id_hex,
)
from expert_work.common.observability.propagation import (
    extract_context as extract_context,
)
from expert_work.common.observability.propagation import (
    inject_context as inject_context,
)
from expert_work.common.observability.tracing import (
    LLM_SPAN_PURPOSES as LLM_SPAN_PURPOSES,
)
from expert_work.common.observability.tracing import (
    TRACED_SPANS as TRACED_SPANS,
)
from expert_work.common.observability.tracing import (
    ExpertWorkComponent as ExpertWorkComponent,
)
from expert_work.common.observability.tracing import (
    expert_work_span as expert_work_span,
)
from expert_work.common.observability.tracing import (
    get_tracer as get_tracer,
)
from expert_work.common.observability.tracing import (
    init_tracing as init_tracing,
)

__all__ = [
    "BANNED_LABEL_NAMES",
    "LLM_SPAN_PURPOSES",
    "TRACED_SPANS",
    "TRACEPARENT_HEADER",
    "TRACESTATE_HEADER",
    "ExpertWorkComponent",
    "ExpertWorkJsonFormatter",
    "ExtrasRedactor",
    "MetricNamingError",
    "current_span_id_hex",
    "current_trace_id_hex",
    "expert_work_counter",
    "expert_work_gauge",
    "expert_work_histogram",
    "expert_work_span",
    "extract_context",
    "get_logger",
    "get_tracer",
    "init_logging",
    "init_tracing",
    "inject_context",
    "metrics_text",
    "metrics_text",
    "validate_label_names",
    "validate_metric_name",
]
