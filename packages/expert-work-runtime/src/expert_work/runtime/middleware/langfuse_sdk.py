"""Langfuse SDK adapter — Stream HX-7 (STREAM-HX-DESIGN § 8.2-①).

Implements the E.5 :class:`LangfuseClient` protocol over the real
``langfuse`` v3 SDK, replacing the M0 ``RecordingLangfuseClient`` when
the deployment configures a Langfuse instance (ADR-0005). The
middleware is untouched — the protocol is the seam (Mini-ADR HX-G1).

v3 is OTel-based: SDK spans join the active OpenTelemetry context, so
they share the trace id our ``expert_work_span`` / W3C-propagation layer
already carries — the ADR-0005 "trace_id 共享、Langfuse ↔ Tempo 互跳"
data flow needs no extra code.

Each LLM call maps to a Langfuse *generation* (the LLM-typed
observation, so token usage and model cost land in Langfuse's
accounting) — created un-nested via ``start_observation(as_type="generation")``
(langfuse 4.x; 3.x's ``start_generation`` is gone); submission is
the SDK's own bounded background queue, matching the protocol's
"must not block" contract. Failures are already fail-soft at the
middleware layer; this module adds no second try/except blanket.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from expert_work.runtime.audit.redactor import (
    DEFAULT_PATTERNS,
    PII_PATTERNS,
    DefaultSecretRedactor,
)
from expert_work.runtime.middleware.langfuse import LangfuseClient, RecordingLangfuseClient

if TYPE_CHECKING:  # pragma: no cover — typing only, avoids a hard import
    from langfuse._client.span import LangfuseGeneration

logger = logging.getLogger(__name__)


def _build_pii_mask() -> Callable[..., Any]:
    """The Langfuse ``mask`` callable — Mini-ADR OBS-L1.

    Runs the ``{**DEFAULT_PATTERNS, **PII_PATTERNS}`` union (secrets +
    conversational PII) over every string leaf of the input/output/metadata
    Langfuse ingests, BEFORE it lands in ClickHouse. ``mask`` is called by
    the SDK as ``mask(data=...)`` and must return JSON-serialisable data.
    """
    redactor = DefaultSecretRedactor(patterns={**DEFAULT_PATTERNS, **PII_PATTERNS})

    def mask(data: Any = None, **_kwargs: Any) -> Any:
        # Accept positional or keyword ``data`` — the SDK calls ``mask(data=...)``
        # but stay robust to either convention.
        return redactor.redact_tree(data)

    return mask


class _SdkSpan:
    """One LLM call's generation handle — the :class:`LangfuseSpan` shape."""

    def __init__(self, generation: LangfuseGeneration) -> None:
        self._generation = generation

    def record_output(self, output: Any) -> None:
        self._generation.update(output=output)

    def record_usage(self, usage: Mapping[str, int]) -> None:
        # Langfuse v3 takes usage as ``usage_details`` (str → int); our
        # middleware already passes that shape (input_tokens / output_tokens
        # / cache_read_input_tokens ...).
        self._generation.update(usage_details={k: int(v) for k, v in usage.items()})

    def record_error(self, exception: BaseException) -> None:
        self._generation.update(
            level="ERROR",
            status_message=f"{type(exception).__name__}: {exception}",
        )

    def end(self) -> None:
        self._generation.end()


class LangfuseSdkClient:
    """:class:`LangfuseClient` over the ``langfuse`` v3 SDK.

    ``sdk_client`` is the constructed ``langfuse.Langfuse`` instance —
    injected so tests substitute a fake without monkeypatching the SDK.
    """

    def __init__(self, sdk_client: Any) -> None:
        self._sdk = sdk_client

    def start_span(
        self,
        *,
        name: str,
        input: Any,
        metadata: Mapping[str, Any] | None = None,
    ) -> _SdkSpan:
        meta = dict(metadata or {})
        model = meta.get("model")
        # langfuse 4.x removed ``start_generation``; a generation is now an
        # observation of type "generation" (same LLM-typed record, same
        # update / end handle).
        generation = self._sdk.start_observation(
            name=name,
            as_type="generation",
            input=input,
            metadata=meta,
            model=str(model) if model is not None else None,
        )
        return _SdkSpan(generation)

    def flush(self) -> None:
        """Drain the SDK's background queue — call at lifespan teardown."""
        self._sdk.flush()

    def shutdown(self) -> None:
        """Flush + stop the SDK's background workers."""
        self._sdk.shutdown()


def make_langfuse_client(
    *,
    host: str | None,
    public_key: str | None,
    secret_key: str | None,
    pii_masking_enabled: bool = True,
) -> LangfuseClient:
    """Resolve the deployment's Langfuse client (Mini-ADR HX-G3 / OBS-L1).

    All three settings present → the SDK-backed client. Anything
    missing — including an SDK import failure on a broken install —
    degrades to :class:`RecordingLangfuseClient`: tracing config must
    never take the service down, and the no-credential deployment
    (dev / CI) keeps the M0 behaviour byte-identical.

    ``pii_masking_enabled`` (default ``True``, Mini-ADR OBS-L1 decision 4)
    installs the SDK ``mask`` callback so prompts/completions are scrubbed
    of secrets + conversational PII at ingestion, before ClickHouse. The
    default-on stance is fail-safe: a mis-config never leaks PII, it only
    over-redacts. The escape hatch is an explicit ``False``.
    """
    if not (host and public_key and secret_key):
        logger.info("langfuse.disabled — settings incomplete, using the recording client")
        return RecordingLangfuseClient()
    try:
        from langfuse import Langfuse
    except ImportError:
        logger.warning(
            "langfuse.sdk_import_failed — falling back to the recording client",
            exc_info=True,
        )
        return RecordingLangfuseClient()
    kwargs: dict[str, Any] = {
        "public_key": public_key,
        "secret_key": secret_key,
        "host": host,
        "tracing_enabled": True,
    }
    if pii_masking_enabled:
        kwargs["mask"] = _build_pii_mask()
    sdk = Langfuse(**kwargs)
    logger.info("langfuse.enabled host=%s pii_masking=%s", host, pii_masking_enabled)
    return LangfuseSdkClient(sdk)
