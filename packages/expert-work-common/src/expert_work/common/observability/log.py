"""Structured JSON logging — Stream A.7.

Design: subsystems/20-observability § 5.3.

Every INFO+ record carries the mandatory schema:

| Field | Source |
|---|---|
| timestamp | LogRecord.created (ISO 8601 UTC) |
| level | LogRecord.levelname |
| logger | LogRecord.name (must start with ``expert_work.``) |
| message | LogRecord.getMessage() — **snake_case event ID**, not free text |
| service | injected at ``init_logging`` (e.g., ``control_plane``) |
| env | injected at ``init_logging`` (``dev`` / ``staging`` / ``prod``) |
| tenant | contextvar — set by auth middleware (Stream C.1) |
| trace_id | contextvar — set by OTel SDK boundary (Stream A.8) |

Optional fields commonly used: ``session_id``, ``agent``, ``agent_version``,
``actor_id``. Pass these via ``extra={...}``.

The redactor (Stream A.4 ``DefaultSecretRedactor`` or a custom one) is
applied to the **extras** dict before serialization — never to the
mandatory schema fields. ``tenant`` / ``trace_id`` / ``service`` / ``env``
are operator metadata, not user data.

Span ID injection happens in Stream A.8 once the OTel SDK is wired; for
M0 A.7 the field is emitted as ``null`` until that batch lands.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import IO, Any
from urllib.parse import urlsplit

from expert_work.common.context import (
    get_current_run_id,
    get_current_tenant,
    get_current_trace_id,
)
from expert_work.common.observability.propagation import (
    current_span_id_hex,
    current_trace_id_hex,
)

# Mandatory keys per § 5.3. Order is preserved in JSON output for
# operator-friendly grep.
_MANDATORY_FIELDS = (
    "timestamp",
    "level",
    "logger",
    "message",
    "service",
    "env",
    "tenant",
    "trace_id",
    "span_id",
    "run_id",
)

# stdlib LogRecord attrs that the formatter consumes directly; everything
# else passed via ``extra={...}`` lands in the optional payload.
_RESERVED_LOG_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)

ExtrasRedactor = Callable[[Mapping[str, Any]], Mapping[str, Any]]
"""Strategy for redacting the ``extras`` portion of a log record.

Same shape as :class:`~expert_work.runtime.audit.AuditRedactor` so the
two can share an implementation — see Stream A.4 batch 2.
"""


class ExpertWorkJsonFormatter(logging.Formatter):
    """JSON formatter enforcing the § 5.3 mandatory schema.

    Records produced::

        {"timestamp": "2026-05-12T12:34:56.789Z", "level": "INFO",
         "logger": "expert_work.orchestrator", "message": "session.start",
         "service": "control-plane", "env": "dev",
         "tenant": "...", "trace_id": "...", "span_id": null,
         "session_id": "...", "agent": "..."}

    Mandatory schema violations (missing ``tenant`` / ``trace_id`` in a
    context that should have them) are **not** raised — the formatter
    cannot tell intent at this layer. Instead the offending field is
    emitted as ``null`` and a separate WARNING ``expert_work.log_schema_violation``
    surfaces the issue (wired in Stream A.9 metrics).
    """

    def __init__(
        self,
        *,
        service: str,
        env: str,
        redactor: ExtrasRedactor | None = None,
    ) -> None:
        super().__init__()
        self._service = service
        self._env = env
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        timestamp = (
            datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        tenant = get_current_tenant()
        # Prefer the OTel active span (Stream A.8) over the contextvar
        # fallback — the formatter sees both an init_tracing()'d process
        # and a bare unit test cleanly.
        trace_id = current_trace_id_hex() or get_current_trace_id()
        span_id = current_span_id_hex()
        # Stream HX-4 (Mini-ADR HX-D4) — run-worker scope; null outside
        # a run (HTTP handlers, background sweeps), same as trace_id.
        run_id = get_current_run_id()

        payload: dict[str, Any] = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self._service,
            "env": self._env,
            "tenant": str(tenant) if tenant is not None else None,
            "trace_id": trace_id,
            "span_id": span_id,
            "run_id": str(run_id) if run_id is not None else None,
        }

        extras = self._collect_extras(record)
        if self._redactor is not None and extras:
            extras = dict(self._redactor(extras))
        # Mandatory keys take precedence — never let an ``extra`` shadow
        # ``service`` etc. by accident.
        for key, value in extras.items():
            if key not in _MANDATORY_FIELDS:
                payload[key] = value

        if record.exc_info:
            # ``Formatter.formatException`` is sync + idempotent.
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=_json_default, ensure_ascii=False)

    @staticmethod
    def _collect_extras(record: logging.LogRecord) -> dict[str, Any]:
        return {
            k: v
            for k, v in record.__dict__.items()
            if k not in _RESERVED_LOG_RECORD_ATTRS and not k.startswith("_")
        }


def _json_default(value: Any) -> Any:
    """Fallback serializer for non-JSON types commonly in extras (UUID,
    datetime, Path)."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def init_logging(
    *,
    service: str,
    env: str,
    level: int | str = logging.INFO,
    redactor: ExtrasRedactor | None = None,
    stream: IO[str] | None = None,
) -> None:
    """Install :class:`ExpertWorkJsonFormatter` on the root logger.

    Idempotent — repeated calls replace the existing handler, so re-init
    inside tests / multi-process workers is safe.

    :param service: Logical service name (``control_plane`` / ``orchestrator``).
    :param env: Deployment environment label (``dev`` / ``staging`` / ``prod``).
    :param level: Numeric or string level (passed to ``setLevel``).
    :param redactor: Optional callable that masks sensitive keys in
        per-record extras before they reach stdout.
    :param stream: Output stream (default ``sys.stdout``). Tests inject a
        ``StringIO``.
    """
    handler = logging.StreamHandler(stream=stream or sys.stdout)
    handler.setFormatter(ExpertWorkJsonFormatter(service=service, env=env, redactor=redactor))

    root = logging.getLogger()
    # Replace any pre-existing expert_work handler so init_logging stays
    # idempotent across re-invocations (notably in test fixtures).
    for existing in list(root.handlers):
        if isinstance(existing.formatter, ExpertWorkJsonFormatter):
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    install_httpx_url_filter()


#: ``httpx`` logs one line per request with the full URL (``HTTP Request: <method>
#: <url> "<version> <status> <reason>"``).
_HTTP_URL_PREFIXES = ("http://", "https://")


class HttpxUrlFilter(logging.Filter):
    """Cut every URL argument of an ``httpx`` log record down to its origin
    (``scheme://host[:port]``) — B-308 §4.7.

    A credential's request address may carry a token in its path, and the
    platform root logger runs at INFO, where ``httpx`` prints each request's
    full URL. The host stays visible (operators still see where a call went and
    its status); nothing after it is logged. The argument is an ``httpx.URL``
    in current httpx and a string in others; both are handled.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and args:
            record.args = tuple(_origin_only(arg) for arg in args)
        return True


def _origin_only(arg: object) -> object:
    if not isinstance(arg, str) and type(arg).__name__ != "URL":
        return arg
    text = str(arg)
    if not text.startswith(_HTTP_URL_PREFIXES):
        return arg
    parts = urlsplit(text)
    return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"


def install_httpx_url_filter() -> None:
    """Install :class:`HttpxUrlFilter` on the ``httpx`` logger (once) and pin
    ``httpcore`` at WARNING — at DEBUG it logs connection detail, and a root
    level lowered to DEBUG must not bring that back.

    Safe to call repeatedly: :func:`init_logging` calls it, and so does the
    module that owns the address-checking HTTP client, which covers processes
    that never call ``init_logging`` (eval scripts, CLIs).
    """
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(existing, HttpxUrlFilter) for existing in httpx_logger.filters):
        httpx_logger.addFilter(HttpxUrlFilter())
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the ``expert_work.`` namespace.

    Raises ``ValueError`` when ``name`` doesn't start with ``expert_work.`` —
    keeps the § 5.3 ``logger`` field constraint enforced at the call
    site rather than at format time.
    """
    if not name.startswith("expert_work."):
        msg = f"logger name must start with 'expert_work.': {name!r}"
        raise ValueError(msg)
    return logging.getLogger(name)
