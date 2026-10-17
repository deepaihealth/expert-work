"""LLM provider fallback router — Stream E.11 + E.12.5.

:class:`LLMRouter` implements the :class:`~orchestrator.llm.caller.LLMCaller`
protocol so the ReAct graph (E.6) treats it as a single callable; under
the hood it walks a chain of :class:`ProviderHandle` entries (primary
first, then fallbacks) and falls back on **retryable** errors only.

Fallback semantics — straight from
[STREAM-E-DESIGN § 2.4](../../../../../docs/streams/STREAM-E-DESIGN.md):

- :class:`LLMClientError` (4xx) → re-raise immediately. The caller is
  malformed; the next provider would reject it for the same reason and
  waste its rate-limit budget.
- :class:`LLMServerError` / :class:`LLMRateLimitError` /
  :class:`LLMNetworkError` / :class:`CircuitOpenError` → log + continue.
- All providers exhausted → :class:`AllProvidersExhaustedError` wrapping
  the last attempt's exception for diagnostic context.

The router **does not** retry within a single provider — that's
:class:`~expert_work.runtime.middleware.LLMErrorHandlingMiddleware`'s
job (E.4 ``around_llm_call``). E.12.5 wires the middleware chain in:

::

    LLMRouter
      → (per provider) chain.invoke("around_llm_call", ctx, terminal=provider.complete)
                       ↑
                       │  ctx.payload contains provider_key / messages /
                       │  tools / response — Mini-ADR E-13 explains why
                       │  the wrap is per-provider and not per-router
                       │  (E.4 breaker per-key isolation + langfuse
                       │  per-provider span split).

Without the middleware chain (``chain=None``), each provider gets one
attempt before fallback — the M0 unit-test path and a valid degraded
mode for early-stage runs that haven't booted the chain yet.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from expert_work.common.observability import expert_work_counter
from expert_work.protocol import StructuredOutputSpec
from expert_work.runtime.middleware import (
    CircuitOpenError,
    LLMAuthError,
    LLMClientError,
    LLMError,
    LLMKeyUnavailableError,
    LLMOutputValidationError,
    LLMRateLimitError,
    LLMStreamInterruptedError,
    LLMStreamStaleError,
    LLMUnauthorizedError,
    MiddlewareChain,
    MiddlewareContext,
)
from orchestrator.llm.oauth_provider import OAuthCapableProvider
from orchestrator.llm.providers._streaming import (
    LLMDelta,
    StreamAssembler,
    supports_streaming,
)
from orchestrator.llm.stall import STALLED_AFTER_S
from orchestrator.llm.structured_output import (
    MAX_VALIDATION_RETRIES,
    correction_message,
    validate_structured_output,
)
from orchestrator.tools.registry import ToolSpec

logger = logging.getLogger(__name__)

# Stream L.L3 — counter for provider-level stream stale timeouts. Labeled by
# ``provider_key`` so dashboards can show which upstream is hanging.
_llm_stream_stale_total = expert_work_counter(
    "expert_work_llm_stream_stale_total",
    "Provider calls that exceeded LLMRouter.first_token_timeout_s (Stream L.L3).",
    ("provider_key",),
)

# Stream L.L8 — counter for OAuth credential refresh attempts and outcomes.
# ``result`` is ``success`` when the second attempt returns a response, or
# ``fail`` when refresh itself returned False / the retry hit another 401.
_llm_auth_refresh_total = expert_work_counter(
    "expert_work_llm_auth_refresh_total",
    "Credential refreshes triggered by OAuth-capable provider 401s (Stream L.L8).",
    ("provider_key", "result"),
)

# Stream RT-1 (RT-ADR-1) — structured-output validation loop observability.
# Each validation resend multiplies the E.4 in-provider retry budget (see
# the :meth:`LLMRouter._attempt_call` worst-case note), so retry volume and
# terminal failures must be visible on dashboards. Labeled by
# ``provider_key`` only — schema names are caller-defined and unbounded.
_llm_structured_validation_retry_total = expert_work_counter(
    "expert_work_llm_structured_validation_retry_total",
    "Structured-output correction resends after an invalid response (Stream RT-1).",
    ("provider_key",),
)
_llm_structured_validation_failure_total = expert_work_counter(
    "expert_work_llm_structured_validation_failure_total",
    "Structured-output calls that exhausted the validation retries (Stream RT-1).",
    ("provider_key",),
)


@runtime_checkable
class LLMProvider(Protocol):
    """Wire-level LLM caller — one provider, one model.

    Concrete adapters
    (:class:`~orchestrator.llm.providers.anthropic.AnthropicProvider`,
    :class:`~orchestrator.llm.providers.openai.OpenAIProvider`)
    translate :class:`BaseMessage` / :class:`ToolSpec` into the provider's
    wire format and back, raising :class:`LLMError` subclasses for
    transport / vendor failures. The router treats every
    :class:`LLMProvider` interchangeably; differences between Anthropic
    and OpenAI (system prompt placement, tool schemas, etc.) are an
    adapter concern.
    """

    async def complete(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
    ) -> AIMessage:
        """Call the upstream provider and return the LLM's response.

        Implementations MUST raise :class:`LLMError` subclasses for any
        failure (transport, 4xx, 5xx, rate-limit, parse). Letting raw
        :class:`httpx.HTTPError` / :class:`ValueError` propagate would
        defeat the router's fallback classification.

        ``output_schema`` (Stream RT-1, RT-ADR-2) asks the adapter to
        enforce a JSON Schema on the response via its declared
        ``structured_output_capability`` path (native / tool_call /
        prompt). ``None`` — the default and the only value existing
        callers pass — MUST leave the request wire-identical to the
        pre-RT-1 behaviour.
        """


@dataclass(frozen=True)
class ProviderHandle:
    """One node in the router's fallback chain.

    ``key`` identifies the upstream rate-limit bucket — typically
    ``"<provider>:<model>#<key_id>"`` (e.g. ``"anthropic:claude#1"``,
    ``"anthropic:claude#2"``). It is passed downstream as the
    ``provider_key`` payload field so E.4's
    :class:`~expert_work.runtime.middleware.BreakerRegistry` builds
    per-key circuit breakers (Mini-ADR E-4: breakers are per upstream
    key, not per provider, because one tenant can hold multiple keys
    for the same vendor and they must fail in isolation).

    ``group`` (Stream Y-MK) ties together *sibling keys* of the same
    provider/model — typically ``"<provider>:<model>"``. The router's
    two-level fallback advances within a group on key-level failures
    (rate-limit / dead account / revoked key / open breaker) and skips
    the rest of a group on provider-level failures (5xx / network /
    timeout). Defaults to empty, in which case :func:`_group_of` falls
    back to ``key`` so a legacy single-key handle is its own singleton
    group — preserving the pre-Y-MK flat-chain behaviour exactly.
    """

    provider: LLMProvider
    key: str
    group: str = ""


# Stream Y-MK — key/account-level failures. The router advances to the next
# *sibling key* of the same provider/model on these before falling through to
# the next provider. ``LLMUnauthorizedError`` reaches ``__call__`` only as a
# non-OAuth revoked static key (``_call_one``'s OAuth refresh path has already
# run for OAuth providers), so a revoked key tries a sibling too.
# ``CircuitOpenError`` is per-key (E.4 breaker), so an open breaker on one key
# should try a sibling, not abandon the whole provider.
_KEY_LEVEL_ERRORS: tuple[type[LLMError], ...] = (
    LLMRateLimitError,
    LLMKeyUnavailableError,
    LLMUnauthorizedError,
    CircuitOpenError,
)


def _complete(
    provider: LLMProvider,
    *,
    messages: Sequence[BaseMessage],
    tools: Sequence[ToolSpec],
    output_schema: StructuredOutputSpec | None,
) -> Awaitable[AIMessage]:
    """Call ``provider.complete``, forwarding ``output_schema`` only when set.

    Stream RT-1 backward-compat: the kwarg is omitted on unstructured
    calls so pre-RT-1 :class:`LLMProvider` implementations (test doubles,
    third-party adapters without the parameter) keep working — and the
    ``None`` path stays call-for-call identical to pre-RT-1 behaviour.
    """
    if output_schema is None:
        return provider.complete(messages=messages, tools=tools)
    return provider.complete(messages=messages, tools=tools, output_schema=output_schema)


class _StreamEnded(Exception):  # noqa: N818 - internal control-flow sentinel, not a public error
    """Internal — the delta iterator is exhausted (StopAsyncIteration)."""


async def _next_delta(it: AsyncIterator[LLMDelta], timeout: float | None) -> LLMDelta:
    """One ``__anext__`` under an optional timeout. Raises ``_StreamEnded``
    on exhaustion, ``TimeoutError`` on expiry, or the provider's
    :class:`LLMError` on a stream fault."""
    coro = it.__anext__()
    try:
        if timeout is None or timeout <= 0:
            return await coro
        return await asyncio.wait_for(coro, timeout=timeout)
    except StopAsyncIteration as exc:
        raise _StreamEnded from exc


def _stream(
    provider: LLMProvider,
    *,
    messages: Sequence[BaseMessage],
    tools: Sequence[ToolSpec],
    output_schema: StructuredOutputSpec | None,
) -> AsyncIterator[LLMDelta]:
    """Call ``provider.stream``, forwarding ``output_schema`` only when set
    (mirrors ``_complete`` so pre-streaming doubles stay call-identical)."""
    if output_schema is None:
        return provider.stream(messages=messages, tools=tools)  # type: ignore[attr-defined, no-any-return]
    return provider.stream(  # type: ignore[attr-defined, no-any-return]
        messages=messages, tools=tools, output_schema=output_schema
    )


def _innermost(provider: object) -> Any:
    """The adapter under any ``.inner`` wrapper layers (e.g. ``RateLimitedProvider``)."""
    p: Any = provider
    seen: set[int] = set()
    while hasattr(p, "inner") and id(p) not in seen:
        seen.add(id(p))
        p = p.inner
    return p


def _provider_assembler(provider: object) -> Any:
    """The innermost provider's stream assembler (unwraps ``.inner``)."""
    return _innermost(provider).new_stream_assembler()


def _flatten_params(fields: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """Nested wire objects → dotted keys (``thinking.type``): Langfuse model
    parameters take scalar values only (the SDK's ``MapValue``)."""
    flat: dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, Mapping):
            flat.update(_flatten_params(value, f"{prefix}{key}."))
        else:
            flat[f"{prefix}{key}"] = value
    return flat


def _served_request_fields(provider: object, *, structured: bool) -> dict[str, Any]:
    """B-187 — ``model`` + ``model_parameters`` for this handle's Langfuse generation.

    Read off the innermost adapter: it is built per model entry with every
    platform rewrite already applied (catalog-fixed temperature, sampling
    params dropped, vendor effort mapping, output-cap field, Anthropic's
    default ``max_tokens``), so a fallback handle — or the CM-9 escalated
    router, which shares the primary's handle keys — records its own values,
    not the primary manifest's. ``{}`` for adapters without
    ``request_params`` (test doubles), leaving the payload unchanged.
    """
    adapter = _innermost(provider)
    request_params = getattr(adapter, "request_params", None)
    if request_params is None:
        return {}
    return {
        "model": str(adapter.model),
        "model_parameters": _flatten_params(request_params(structured=structured)),
    }


def _llm_response_payload(response: AIMessage) -> dict[str, Any]:
    """Build the ``ctx.payload["llm_response"]`` contract documented in
    :mod:`expert_work.runtime.middleware.langfuse` — ``{"output": ...,
    "usage": {...}}`` — from a provider's raw :class:`AIMessage`.

    Defensive by design: this runs on every LLM call inside the
    ``terminal`` closure, so a bug here must degrade to an empty/partial
    payload rather than break the call. ``AIMessage.text`` already
    flattens ``str`` vs. block-list ``content`` (LangChain's own
    idiom — see ``BaseMessage.text``); ``usage_metadata`` is already
    normalised to the ``{"input_tokens", "output_tokens", ...}`` shape
    by the provider adapters (``providers/anthropic.py``,
    ``providers/openai.py``).
    """
    output: str
    try:
        # ``.text`` returns a ``TextAccessor`` (a ``str`` subclass kept for
        # backward-compat callable access) — coerce to plain ``str`` so the
        # payload's declared shape stays exactly ``str``.
        output = str(response.text)
    except Exception:
        logger.warning("llm_router.llm_response_output_extraction_failed", exc_info=True)
        output = ""

    usage: dict[str, int] = {}
    try:
        usage_metadata = response.usage_metadata
        if usage_metadata:
            usage = {
                "input_tokens": int(usage_metadata["input_tokens"]),
                "output_tokens": int(usage_metadata["output_tokens"]),
            }
    except Exception:
        logger.warning("llm_router.llm_response_usage_extraction_failed", exc_info=True)
        usage = {}

    return {"output": output, "usage": usage}


def _group_of(handle: ProviderHandle) -> str:
    """The sibling-key group for a handle (Stream Y-MK).

    Falls back to ``key`` when ``group`` is empty so a legacy single-key
    handle forms its own singleton group — the pre-Y-MK flat chain behaves
    identically (skipping "the rest of the group" skips only itself).
    """
    return handle.group or handle.key


class AllProvidersExhaustedError(LLMError):
    """Every :class:`ProviderHandle` in the chain failed with a retryable
    error. Wraps the **last** attempt's exception so callers (and tests)
    can inspect what finally tripped the chain.

    Inherits :class:`LLMError` so it composes with E.4 error-handling
    middleware's exception classification — wrappers can treat
    "exhausted" as terminal rather than retryable.
    """

    def __init__(self, last_exc: BaseException) -> None:
        super().__init__(
            f"all LLM providers exhausted; last error: {type(last_exc).__name__}: {last_exc}"
        )
        self.last_exc = last_exc


# 子项目 2 — the per-call token-delta sink. Set at __call__ entry (public
# ``on_delta`` param) and read in ``_drive_stream``; a ContextVar avoids
# threading the callback through the 6-deep private call chain and is
# task-local, so concurrent runs never see each other's sink. The router stays
# LangGraph-agnostic — it only ``await``s an opaque async callback.
_delta_sink: ContextVar[Callable[[LLMDelta], Awaitable[None]] | None] = ContextVar(
    "_llm_delta_sink", default=None
)


@dataclass
class LLMRouter:
    """Try each :class:`ProviderHandle` in order; fall back on retryable errors.

    When ``around_llm_chain`` is set, each provider call is wrapped in
    ``chain.invoke("around_llm_call", ...)`` — letting E.4 retry /
    breaker, E.5 langfuse span recording, and any future
    around-LLM-call middleware run **per provider** (Mini-ADR E-13).

    See module docstring for the full fallback semantics. The router is
    stateless — all per-provider state (breaker counters, rate-limit
    tokens) lives in middleware / inside :class:`LLMProvider` adapters
    so swapping the router or wrapping it with retries is safe.
    """

    providers: Sequence[ProviderHandle]
    around_llm_chain: MiddlewareChain | None = field(default=None)
    #: Stream L (P1) — the streaming idle-timeout pair. ``first_token_timeout_s``
    #: bounds time-to-first-token (fallback-eligible on expiry); ``idle_timeout_s``
    #: bounds inter-delta silence AFTER the first token (ends the turn with the
    #: partial output). For a non-streaming provider (Anthropic until P1', test
    #: doubles) ``first_token_timeout_s`` degrades to the legacy total wall-clock
    #: cap around ``complete()``. ``None``/``0`` disables the respective timer.
    first_token_timeout_s: float | None = field(default=None)
    idle_timeout_s: float | None = field(default=None)

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
        on_delta: Callable[[LLMDelta], Awaitable[None]] | None = None,
    ) -> AIMessage:
        sink_token = _delta_sink.set(on_delta)
        try:
            return await self._dispatch(messages=messages, tools=tools, output_schema=output_schema)
        finally:
            _delta_sink.reset(sink_token)

    async def _dispatch(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
    ) -> AIMessage:
        handles = self.providers
        if not handles:
            raise AllProvidersExhaustedError(
                RuntimeError("LLMRouter constructed with empty provider chain")
            )

        # Stream Y-MK — two-level walk over a flat handle list:
        #   * key-level error  → next handle (sibling key first; once a
        #     provider's siblings are exhausted the index naturally reaches
        #     the next provider's group).
        #   * 400 malformed    → re-raise, no fallback (E.11 #21 unchanged).
        #   * provider-level   → skip the rest of THIS group's sibling keys
        #     and jump to the next provider (a 5xx/network/timeout hits every
        #     sibling key identically, so trying them wastes wall-clock).
        last_exc: LLMError | None = None
        n = len(handles)
        i = 0
        while i < n:
            handle = handles[i]
            try:
                return await self._call_one(
                    handle, messages=messages, tools=tools, output_schema=output_schema
                )
            except LLMOutputValidationError:
                # Stream RT-1 (RT-ADR-1) — a schema-validation failure is
                # model behaviour, not a key/provider fault: never rotate
                # to a sibling key, never fail over. Re-raise so the
                # caller's defensive degradation path handles it.
                raise
            except LLMStreamInterruptedError:
                # Buffer-until-first-token — a stall/error AFTER the first
                # delta commits the run to this provider (partial output
                # already streamed). No key rotation, no failover.
                raise
            except _KEY_LEVEL_ERRORS as exc:
                last_exc = exc
                logger.warning(
                    "llm_router.key_failed idx=%d key=%s err=%s",
                    i,
                    handle.key,
                    type(exc).__name__,
                )
                i += 1
            except LLMClientError:
                logger.warning(
                    "llm_router.client_error_no_fallback idx=%d key=%s",
                    i,
                    handle.key,
                )
                raise
            except LLMError as exc:
                last_exc = exc
                group = _group_of(handle)
                logger.warning(
                    "llm_router.provider_failed idx=%d key=%s err=%s",
                    i,
                    handle.key,
                    type(exc).__name__,
                )
                i += 1
                while i < n and _group_of(handles[i]) == group:
                    i += 1

        assert last_exc is not None  # noqa: S101 - loop invariant
        raise AllProvidersExhaustedError(last_exc)

    async def _call_one(
        self,
        handle: ProviderHandle,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None,
    ) -> AIMessage:
        """Invoke one provider, with the Stream L.L8 OAuth refresh hook.

        The :class:`LLMUnauthorizedError` catch is the L8 entry point —
        non-OAuth providers re-raise unchanged (existing 4xx-no-fallback
        semantics); OAuth-capable providers get one refresh + retry
        before failing. See :meth:`_handle_unauthorized`.
        """
        try:
            return await self._attempt_call(
                handle, messages=messages, tools=tools, output_schema=output_schema
            )
        except LLMUnauthorizedError as exc:
            return await self._handle_unauthorized(
                handle,
                messages=messages,
                tools=tools,
                output_schema=output_schema,
                original=exc,
            )

    async def _attempt_call(
        self,
        handle: ProviderHandle,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None,
    ) -> AIMessage:
        """One provider attempt, plus the RT-ADR-1 validation loop.

        Without ``output_schema`` this is a single :meth:`_invoke_once`
        — bit-for-bit the pre-RT-1 behaviour. With a schema, the
        response is validated against it; an invalid response is fed
        back (the raw response + a correction user message) to the SAME
        handle — same provider, same key — up to
        :data:`MAX_VALIDATION_RETRIES` resends. Still invalid →
        :class:`LLMOutputValidationError`, which the outer loop
        re-raises without key rotation or provider failover.

        Cost worst case: each of the ``1 + MAX_VALIDATION_RETRIES`` (= 3)
        validation attempts is an independent :meth:`_invoke_once`, and
        with the E.4 ``LLMErrorHandlingMiddleware`` wrapping the chain
        each attempt may itself retry transient failures up to its
        budget (1 + 3) — so one structured call can cost up to
        ``3 x 4 = 12`` real upstream calls. The
        ``expert_work_llm_structured_validation_{retry,failure}_total``
        counters make that volume observable per provider key.
        """
        if output_schema is None:
            return await self._invoke_once(
                handle, messages=messages, tools=tools, output_schema=None
            )

        current: list[BaseMessage] = list(messages)
        error_summary = ""
        for attempt in range(1 + MAX_VALIDATION_RETRIES):
            response = await self._invoke_once(
                handle, messages=current, tools=tools, output_schema=output_schema
            )
            parsed, error_summary_or_none = validate_structured_output(response, output_schema)
            if error_summary_or_none is None:
                # Carry the validated dict on the message rather than
                # mutating the provider's object in place.
                return response.model_copy(
                    update={
                        "additional_kwargs": {**response.additional_kwargs, "parsed": parsed},
                    }
                )
            error_summary = error_summary_or_none
            logger.warning(
                "llm_router.structured_output_invalid key=%s attempt=%d schema=%s",
                handle.key,
                attempt + 1,
                output_schema.name,
            )
            if attempt == MAX_VALIDATION_RETRIES:
                break
            _llm_structured_validation_retry_total.labels(provider_key=handle.key).inc()
            current = [
                *current,
                response,
                HumanMessage(content=correction_message(error_summary, output_schema)),
            ]

        _llm_structured_validation_failure_total.labels(provider_key=handle.key).inc()
        raise LLMOutputValidationError(
            f"structured output failed validation after {MAX_VALIDATION_RETRIES} retries "
            f"(schema={output_schema.name!r}): {error_summary}"
        )

    async def _invoke_once(
        self,
        handle: ProviderHandle,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None,
    ) -> AIMessage:
        """One provider call, optionally wrapped in the around-LLM chain.

        Without ``around_llm_chain`` we just delegate to ``provider.complete``
        — the M0 unit-test path. With the chain set, build a
        :class:`MiddlewareContext` carrying ``provider_key`` + the call
        inputs, let the chain run middlewares around a terminal that
        actually calls the provider and stashes the result in
        ``ctx.payload["response"]``.
        """
        if self.around_llm_chain is None:
            result = await self._invoke_provider(
                handle, messages=messages, tools=tools, output_schema=output_schema
            )
            assert isinstance(result, AIMessage)  # noqa: S101 - provider Protocol contract
            return result

        payload: dict[str, Any] = {
            "provider_key": handle.key,
            "messages": list(messages),
            "tools": list(tools),
            # B-187 — read by the Langfuse middleware: the model this handle
            # calls and the request parameters it actually sends.
            **_served_request_fields(handle.provider, structured=output_schema is not None),
        }
        if output_schema is not None:
            # RT-1 — informational for observability middlewares; absent
            # on unstructured calls so the payload stays byte-identical.
            # A plain JSON-serializable dict (a middleware may json.dumps
            # the payload), and no schema body — that can be large.
            payload["output_schema"] = {
                "name": output_schema.name,
                "strict": output_schema.strict,
            }
        ctx = MiddlewareContext(payload=payload)

        async def terminal(c: MiddlewareContext) -> None:
            response = await self._invoke_provider(
                handle,
                messages=c.payload["messages"],
                tools=c.payload["tools"],
                output_schema=output_schema,
            )
            c.payload["response"] = response
            c.payload["llm_response"] = _llm_response_payload(response)

        await self.around_llm_chain.invoke(ctx, terminal)
        response = ctx.payload.get("response")
        if not isinstance(response, AIMessage):
            # Middleware mis-handled the terminal (didn't call call_next, or
            # cleared the response) → surface clearly rather than silently
            # returning a falsy / wrong-typed value.
            raise RuntimeError(
                f"around_llm_call chain finished without populating an AIMessage "
                f"response for provider_key={handle.key!r}"
            )
        return response

    async def _invoke_provider(
        self,
        handle: ProviderHandle,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None,
    ) -> AIMessage:
        """Dispatch one provider attempt — streaming (two-threshold idle
        driver) when the provider supports it, else the legacy
        single-deadline ``complete()`` path.

        Structured output does not stream (both vendors) — its contract is
        a complete validated JSON object; token streaming has no value and
        Anthropic's tool-call structured path needs post-processing the
        streaming assembler cannot replicate.
        """
        if output_schema is None and supports_streaming(handle.provider):
            return await self._drive_stream(
                handle,
                _stream(handle.provider, messages=messages, tools=tools, output_schema=None),
                _provider_assembler(handle.provider),
            )
        result = await self._invoke_with_deadline(
            handle,
            _complete(handle.provider, messages=messages, tools=tools, output_schema=output_schema),
        )
        assert isinstance(result, AIMessage)  # noqa: S101
        return result

    async def _drive_stream(
        self,
        handle: ProviderHandle,
        stream: AsyncIterator[LLMDelta],
        assembler: StreamAssembler,
    ) -> AIMessage:
        """Consume a provider delta stream under the two-threshold policy.

        Phase 1 (until the first *progress* delta): bounded by
        ``first_token_timeout_s``; a stall or error is retryable →
        fallback. Phase 2 (after the first progress delta): bounded by
        ``idle_timeout_s``; a stall ends the turn with the partial
        output, stamped :data:`~orchestrator.llm.stall.STALLED_AFTER_S`
        unless a finish reason already arrived (B-186); an error is
        terminal (:class:`LLMStreamInterruptedError`, no fallback)."""
        it = stream.__aiter__()
        sink = _delta_sink.get()
        first_progress = False
        finished = False

        # Phase 1 — wait for the first progress delta.
        while not first_progress:
            try:
                delta = await _next_delta(it, self.first_token_timeout_s)
            except _StreamEnded:
                return assembler.build()  # ended with no progress → empty answer
            except TimeoutError as exc:
                _llm_stream_stale_total.labels(provider_key=handle.key).inc()
                logger.warning(
                    "llm_router.first_token_timeout key=%s deadline_s=%s",
                    handle.key,
                    self.first_token_timeout_s,
                )
                raise LLMStreamStaleError(
                    f"provider {handle.key!r} produced no token within "
                    f"first_token_timeout_s={self.first_token_timeout_s}"
                ) from exc
            assembler.add(delta)
            if sink is not None:
                await sink(delta)
            first_progress = delta.has_progress
            finished = finished or delta.finish_reason is not None

        # Phase 2 — consume the rest under the idle timeout.
        while True:
            try:
                delta = await _next_delta(it, self.idle_timeout_s)
            except _StreamEnded:
                return assembler.build()
            except TimeoutError:
                logger.warning(
                    "llm_router.idle_timeout key=%s deadline_s=%s (ending turn with partial)",
                    handle.key,
                    self.idle_timeout_s,
                )
                partial = assembler.build(interrupted=True)
                if finished or self.idle_timeout_s is None:
                    return partial
                # B-186 — the agent loop fails a reasoning-only partial;
                # planner / reflection keep treating it as an empty reply.
                metadata = {**partial.response_metadata, STALLED_AFTER_S: self.idle_timeout_s}
                return partial.model_copy(update={"response_metadata": metadata})
            except LLMError as exc:
                # Post-first-token hard error → terminal, no fallback.
                raise LLMStreamInterruptedError(
                    f"provider {handle.key!r} stream failed after first token: {exc}",
                    partial=assembler.build(interrupted=True),
                ) from exc
            assembler.add(delta)
            if sink is not None:
                await sink(delta)
            finished = finished or delta.finish_reason is not None

    async def _handle_unauthorized(
        self,
        handle: ProviderHandle,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None,
        original: LLMUnauthorizedError,
    ) -> AIMessage:
        """Stream L.L8 — credential refresh + at-most-one retry.

        Non-OAuth providers re-raise the original :class:`LLMUnauthorizedError`
        so the existing 4xx-no-fallback path stays intact for static API-key
        providers. OAuth-capable providers get exactly one refresh attempt:

        * ``refresh_credentials()`` returns ``True`` → retry the call. A
          successful retry returns the response. Another 401 wraps as
          :class:`LLMAuthError` (retryable) so the outer router falls
          back to the next provider.
        * ``refresh_credentials()`` returns ``False`` (or raises) → no
          retry; raise :class:`LLMAuthError` immediately so the router
          falls back.

        Mini-ADR L-8: the router (not the provider) enforces "at most
        one refresh per call" — a buggy provider implementation cannot
        loop on persistent 401.
        """
        if not isinstance(handle.provider, OAuthCapableProvider):
            # Non-OAuth provider — preserve the existing 4xx semantics:
            # the router's outer loop re-raises LLMClientError without
            # fallback (a bad API key on Anthropic / OpenAI is a real
            # auth failure, not an expired-token recoverable).
            raise original

        try:
            refreshed = await handle.provider.refresh_credentials()
        except Exception as exc:
            # A misbehaving refresh implementation must not crash the
            # run — treat as a refresh failure (per the L8 Protocol
            # contract: "MUST NOT raise on routine paths").
            logger.warning(
                "llm_router.refresh_raised key=%s err=%s",
                handle.key,
                type(exc).__name__,
            )
            refreshed = False

        if not refreshed:
            _llm_auth_refresh_total.labels(provider_key=handle.key, result="fail").inc()
            logger.info("llm_router.refresh_failed key=%s", handle.key)
            raise LLMAuthError(
                f"provider {handle.key!r} credential refresh failed; falling back"
            ) from original

        # Refresh succeeded — retry exactly once. Another 401 means the
        # refreshed credentials are also rejected; treat as a real auth
        # failure on this provider and let the router fall back.
        try:
            result = await self._attempt_call(
                handle, messages=messages, tools=tools, output_schema=output_schema
            )
        except LLMUnauthorizedError as retry_exc:
            _llm_auth_refresh_total.labels(provider_key=handle.key, result="fail").inc()
            logger.info("llm_router.refresh_retry_still_401 key=%s", handle.key)
            raise LLMAuthError(
                f"provider {handle.key!r} still unauthorized after refresh; falling back"
            ) from retry_exc

        _llm_auth_refresh_total.labels(provider_key=handle.key, result="success").inc()
        logger.info("llm_router.refresh_recovered key=%s", handle.key)
        return result

    async def _invoke_with_deadline(
        self,
        handle: ProviderHandle,
        coro: Awaitable[Any],
    ) -> Any:
        """Stream L.L3 — wrap a provider invocation in ``asyncio.wait_for``.

        ``first_token_timeout_s`` is per-provider (Mini-ADR L-3): a hung
        provider trips the timeout, raises :class:`LLMStreamStaleError`
        (a retryable :class:`LLMServerError` subclass), and the surrounding
        :meth:`__call__` loop falls back to the next provider rather than
        locking the run. When ``first_token_timeout_s`` is ``None`` or ``0``
        the call is awaited directly (dev / long-batch path).

        Non-streaming providers only (Stream L P1 — :meth:`_invoke_provider`
        routes streaming providers through :meth:`_drive_stream` instead,
        which applies the two-threshold ``first_token_timeout_s`` /
        ``idle_timeout_s`` pair directly).
        """
        deadline = self.first_token_timeout_s
        if deadline is None or deadline <= 0:
            return await coro
        try:
            return await asyncio.wait_for(coro, timeout=deadline)
        except TimeoutError as exc:
            _llm_stream_stale_total.labels(provider_key=handle.key).inc()
            logger.warning(
                "llm_router.stream_stale key=%s deadline_s=%.1f",
                handle.key,
                deadline,
            )
            raise LLMStreamStaleError(
                f"provider {handle.key!r} exceeded first_token_timeout_s={deadline:.1f}"
            ) from exc
