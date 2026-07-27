"""Anthropic Messages API adapter — Stream E.11.

Translates the orchestrator's :class:`BaseMessage` history +
:class:`ToolSpec` catalogue into Anthropic's Messages API wire format,
posts via :class:`AnthropicClient` (httpx in production, recording
client in tests), and maps the response back to a LangChain
:class:`AIMessage` with ``tool_calls`` populated.

Wire-format mapping (covered, deliberately minimal for M0):

- ``SystemMessage`` → top-level ``system`` field. Multiple system
  messages are concatenated with ``\\n\\n`` separators.
- ``HumanMessage`` → ``{"role": "user", "content": <text>}``; when the
  message carries ``image_ref`` blocks (J.6 Path A) ``content`` becomes a
  block list with ``{"type": "image", "source": {...}}`` entries, the
  images resolved to base64 via an :class:`ImageResolver`.
- ``AIMessage`` with text only → ``{"role": "assistant", "content": <text>}``.
- ``AIMessage`` with ``tool_calls`` → ``{"role": "assistant", "content":
  [{"type": "text", ...}, {"type": "tool_use", "id": ..., "name": ...,
  "input": ...}, ...]}``.
- ``ToolMessage`` → ``{"role": "user", "content": [{"type":
  "tool_result", "tool_use_id": ..., "content": <text>}]}``.

Out of scope for M0 (deferred to M1-D hardening):

- Streaming responses (``stream=true``). Routed through E.14 SSE later.
- Tool-level ``cache_control`` blocks. (System + message-level
  ``cache_control`` landed in Stream L.L1; tool definitions don't yet
  participate.)

Error mapping per :class:`LLMError` hierarchy (E.4):

- HTTP 429 → :class:`LLMRateLimitError`
- HTTP 4xx other → :class:`LLMClientError` (NOT retried, NOT fallback)
- HTTP 5xx → :class:`LLMServerError`
- :class:`httpx.HTTPError` (network / TLS) → :class:`LLMNetworkError`
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, Protocol, runtime_checkable

import httpx
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from expert_work.common.uplift_metrics import record_anthropic_cache_anchor
from expert_work.protocol import StructuredOutputSpec
from expert_work.runtime.middleware import (
    LLMClientError,
    LLMError,
    LLMNetworkError,
    LLMServerError,
)
from orchestrator.llm.coalesce import coalesce_system_messages
from orchestrator.llm.providers._errors import classify_http_error
from orchestrator.llm.providers._http import client_for
from orchestrator.llm.providers._metrics import disclosure_fallback_total
from orchestrator.llm.providers._streaming import (
    AnthropicStreamAssembler,
    LLMDelta,
    delta_from_anthropic_event,
)
from orchestrator.llm.structured_output import StructuredOutputCapability
from orchestrator.multimodal import ImageResolver, split_human_content
from orchestrator.tools.registry import ToolSpec

logger = logging.getLogger(__name__)

#: Stream HX-13 — Anthropic server-side tool-search beta opt-in.
_TOOL_SEARCH_BETA = "tool-search-tool-2025-10-19"


_DEFAULT_BASE_URL = "https://api.anthropic.com"
_DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MAX_TOKENS = 4096
_ANTHROPIC_VERSION = "2023-06-01"
_ERROR_BODY_CHAR_CAP = 500

#: Number of trailing non-system messages that get a ``cache_control``
#: marker. Capability Uplift Sprint #8 (Mini-ADR U-7) lowered this from
#: 3 to 2 to make room for the Sprint #8 memory-anchor breakpoint
#: while keeping the total under Anthropic's 4-breakpoint cap:
#: ``system (1) + tail (2) + memory anchor (0..1) ≤ 4``.
#: The lost tail message (typically a Tool result) costs a few hundred
#: tokens not being cached; the memory anchor saves ~25K tokens on a
#: 50-turn session with a 10-fact memory list — net positive.
_CACHE_CONTROL_TAIL_COUNT: int = 2

#: Stream L.L1 — the wire-level ``cache_control`` shape Anthropic
#: expects for ephemeral (5-minute TTL) caching.
_CACHE_CONTROL_EPHEMERAL: dict[str, str] = {"type": "ephemeral"}


def _build_messages_body(
    *,
    model: str,
    system: str | list[dict[str, Any]] | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    max_tokens: int,
    temperature: float | None,
    thinking: dict[str, Any] | None,
    output_config: dict[str, Any] | None,
    tool_choice: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the ``/v1/messages`` request body shared by the
    non-streaming and streaming paths — extracted from
    :meth:`HTTPAnthropicClient.messages` (Task 3, P1') so
    :meth:`HTTPAnthropicClient.stream_messages` doesn't duplicate the
    field-assembly logic. ``stream`` is NOT set here — the streaming
    caller adds it after this returns."""
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if system is not None:
        body["system"] = system
    if tools:
        body["tools"] = tools
    if tool_choice is not None:
        # Stream RT-1 — force the schema-carrying tool.
        body["tool_choice"] = tool_choice
    if temperature is not None:
        body["temperature"] = temperature
    # Stream CM-9 — compute-control fields (both GA, no beta header).
    if thinking is not None:
        body["thinking"] = thinking
    if output_config is not None:
        body["output_config"] = output_config
    return body


def _parse_anthropic_sse_line(line: str) -> dict[str, Any] | None:
    """Parse one Anthropic SSE line: only ``data:`` lines carry JSON (the
    JSON's own ``type`` field is authoritative; ``event:`` lines are
    ignored). Blank / comment / non-data / malformed lines → None."""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    data = line[len("data:") :].strip()
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _classify_anthropic_stream_error(event: Mapping[str, Any]) -> LLMError:
    """Map an in-band Anthropic SSE ``error`` event to an :class:`LLMError`.

    Only ``rate_limit_error`` (Anthropic's 429 analog) routes through the
    shared ``classify_http_error`` billing/quota-marker inspection —
    ``overloaded_error`` (Anthropic's 529 analog, a transient vendor-side
    capacity issue) is a plain 5xx-shaped fault and maps directly to
    :class:`LLMServerError`, which is already retried + counts toward the
    breaker."""
    err = event.get("error")
    err = err if isinstance(err, Mapping) else {}
    message = str(err.get("message") or "")
    etype = str(err.get("type") or "")
    if "rate_limit" in etype:
        return classify_http_error("anthropic", 429, message)
    return LLMServerError(f"anthropic stream error: {etype}: {message}")


def _response_to_anthropic_events(response: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Build a minimal event sequence from a whole Messages body, so a
    RecordingAnthropicClient primed with only ``response`` yields coherent
    deltas on the streaming path (the router prefers stream())."""
    blocks = response.get("content") or []
    usage_raw = response.get("usage")
    usage: Mapping[str, Any] = usage_raw if isinstance(usage_raw, Mapping) else {}
    events: list[Mapping[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "model": response.get("model"),
                "usage": {"input_tokens": usage.get("input_tokens", 0)},
            },
        }
    ]
    if isinstance(blocks, list):
        for i, b in enumerate(blocks):
            if not isinstance(b, Mapping):
                continue
            if b.get("type") == "text":
                events.append(
                    {
                        "type": "content_block_delta",
                        "index": i,
                        "delta": {"type": "text_delta", "text": b.get("text", "")},
                    }
                )
            elif b.get("type") == "tool_use":
                events.append(
                    {
                        "type": "content_block_start",
                        "index": i,
                        "content_block": {
                            "type": "tool_use",
                            "id": b.get("id"),
                            "name": b.get("name"),
                        },
                    }
                )
                events.append(
                    {
                        "type": "content_block_delta",
                        "index": i,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(b.get("input") or {}),
                        },
                    }
                )
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": response.get("stop_reason")},
            "usage": {"output_tokens": usage.get("output_tokens", 0)},
        }
    )
    events.append({"type": "message_stop"})
    return events


@runtime_checkable
class AnthropicClient(Protocol):
    """Sized to the one Messages API endpoint we use.

    Both :class:`HTTPAnthropicClient` and
    :class:`RecordingAnthropicClient` implement this so unit tests don't
    need to mock httpx. Adapters raise :class:`LLMError` subclasses
    rather than letting transport exceptions leak.
    """

    async def messages(
        self,
        *,
        model: str,
        system: str | list[dict[str, Any]] | None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        temperature: float | None = None,
        thinking: dict[str, Any] | None = None,
        output_config: dict[str, Any] | None = None,
        betas: list[str] | None = None,
        tool_choice: dict[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """POST ``/v1/messages`` and return the parsed JSON body.

        ``tool_choice`` (Stream RT-1, tool_call path) forces the
        schema-carrying tool; ``None`` omits the field entirely."""

    def stream_messages(
        self,
        *,
        model: str,
        system: str | list[dict[str, Any]] | None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        temperature: float | None = None,
        thinking: dict[str, Any] | None = None,
        output_config: dict[str, Any] | None = None,
        betas: list[str] | None = None,
        tool_choice: dict[str, Any] | None = None,
    ) -> AsyncIterator[Mapping[str, Any]]:
        """Stream ``/v1/messages`` (``stream=true``), yielding each parsed
        SSE event dict until ``message_stop``. An HTTP >= 400 status raises
        the classified :class:`LLMError` before the first event; an
        in-band ``error`` event raises mid-stream (Task 3, P1')."""


@dataclass
class HTTPAnthropicClient:
    """Production :class:`AnthropicClient` — talks to the real API.

    ``transport`` is an optional injection point for tests: pass an
    :class:`httpx.MockTransport` to exercise the status-code → error
    mapping without hitting the network. Production callers leave it
    as ``None`` so httpx uses its default transport.
    """

    api_key: str
    base_url: str = _DEFAULT_BASE_URL
    timeout_s: float = _DEFAULT_TIMEOUT_S
    transport: httpx.AsyncBaseTransport | None = None
    #: 一期 Task 5 — process-level shared client. ``None`` falls back to the
    #: original per-call ``async with httpx.AsyncClient(...)`` behaviour
    #: (tests / eval CLI / not-yet-wired production paths). When injected it
    #: must NOT be closed here: it is owned by the control-plane lifespan and
    #: outlives every individual call.
    http: httpx.AsyncClient | None = None

    async def messages(
        self,
        *,
        model: str,
        system: str | list[dict[str, Any]] | None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        temperature: float | None = None,
        thinking: dict[str, Any] | None = None,
        output_config: dict[str, Any] | None = None,
        betas: list[str] | None = None,
        tool_choice: dict[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        body = _build_messages_body(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking=thinking,
            output_config=output_config,
            tool_choice=tool_choice,
        )

        try:
            async with client_for(
                self.http, timeout=self.timeout_s, transport=self.transport
            ) as client:
                response = await client.post(
                    f"{self.base_url}/v1/messages",
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": _ANTHROPIC_VERSION,
                        "content-type": "application/json",
                        # Stream HX-13 — beta opt-ins (e.g. server-side tool
                        # search) ride a comma-joined header, only when asked.
                        **({"anthropic-beta": ",".join(betas)} if betas else {}),
                    },
                    json=body,
                    timeout=self.timeout_s,  # per-request — governs even when sharing a client
                )
        except httpx.HTTPError as exc:
            raise LLMNetworkError(f"anthropic: {exc}") from exc

        status = response.status_code
        if status >= 400:
            # Stream Y-MK — shared classifier splits account/key-level failures
            # (402 / quota / billing → LLMKeyUnavailableError, router tries a
            # sibling key) from plain 429 rate-limits, malformed 4xx, and 5xx.
            # 401 stays an LLMUnauthorizedError for the L.L8 OAuth refresh path.
            raise classify_http_error("anthropic", status, _truncate(response.text))

        data = response.json()
        if not isinstance(data, Mapping):
            raise LLMServerError(f"anthropic returned non-object body: {type(data).__name__}")
        return data

    async def stream_messages(
        self,
        *,
        model: str,
        system: str | list[dict[str, Any]] | None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        temperature: float | None = None,
        thinking: dict[str, Any] | None = None,
        output_config: dict[str, Any] | None = None,
        betas: list[str] | None = None,
        tool_choice: dict[str, Any] | None = None,
    ) -> AsyncIterator[Mapping[str, Any]]:
        body = _build_messages_body(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking=thinking,
            output_config=output_config,
            tool_choice=tool_choice,
        )
        body["stream"] = True
        # Disable httpx's read timeout on the stream — the router's
        # idle_timeout_s governs inter-event silence (Task 3, P1').
        timeout = httpx.Timeout(self.timeout_s, read=None)
        try:
            async with client_for(self.http, timeout=timeout, transport=self.transport) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/v1/messages",
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": _ANTHROPIC_VERSION,
                        "content-type": "application/json",
                        **({"anthropic-beta": ",".join(betas)} if betas else {}),
                    },
                    json=body,
                    timeout=timeout,  # per-request — the shared client's default must not win
                ) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        raise classify_http_error(
                            "anthropic", response.status_code, _truncate(response.text)
                        )
                    async for line in response.aiter_lines():
                        event = _parse_anthropic_sse_line(line)
                        if event is None:
                            continue
                        if event.get("type") == "error":
                            raise _classify_anthropic_stream_error(event)
                        yield event
        except httpx.HTTPError as exc:
            raise LLMNetworkError(f"anthropic: {exc}") from exc


@dataclass
class RecordingAnthropicClient:
    """In-memory :class:`AnthropicClient` for dev / tests.

    Returns ``response`` for every call; records every kwargs dict into
    ``calls`` so tests can assert on the request shape. If
    ``raise_with`` is set, raises that exception instead — used to
    exercise the router's fallback path.
    """

    response: Mapping[str, Any] = field(default_factory=dict)
    #: Canned SSE-event sequence for :meth:`stream_messages`. When empty,
    #: :meth:`stream_messages` synthesizes a coherent sequence from
    #: ``response`` instead (the P1 recording-double lesson —
    #: ``response=``-primed tests must still yield deltas on the
    #: streaming path).
    stream_events: list[Mapping[str, Any]] = field(default_factory=list)
    raise_with: BaseException | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def messages(
        self,
        *,
        model: str,
        system: str | list[dict[str, Any]] | None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        temperature: float | None = None,
        thinking: dict[str, Any] | None = None,
        output_config: dict[str, Any] | None = None,
        betas: list[str] | None = None,
        tool_choice: dict[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        self.calls.append(
            {
                "model": model,
                "system": system,
                "messages": messages,
                "tools": tools,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "thinking": thinking,
                "output_config": output_config,
                "betas": betas,
                "tool_choice": tool_choice,
            }
        )
        if self.raise_with is not None:
            raise self.raise_with
        return self.response

    async def stream_messages(
        self,
        *,
        model: str,
        system: str | list[dict[str, Any]] | None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        temperature: float | None = None,
        thinking: dict[str, Any] | None = None,
        output_config: dict[str, Any] | None = None,
        betas: list[str] | None = None,
        tool_choice: dict[str, Any] | None = None,
    ) -> AsyncIterator[Mapping[str, Any]]:
        self.calls.append(
            {
                "model": model,
                "system": system,
                "messages": messages,
                "tools": tools,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "thinking": thinking,
                "output_config": output_config,
                "betas": betas,
                "tool_choice": tool_choice,
                "stream": True,
            }
        )
        if self.raise_with is not None:
            raise self.raise_with
        events = list(self.stream_events) or _response_to_anthropic_events(self.response)
        for e in events:
            yield e


@dataclass
class AnthropicProvider:
    """:class:`LLMProvider` for Anthropic Messages API.

    The provider is intentionally lightweight: encode messages + tools,
    delegate to :class:`AnthropicClient`, decode the response. All
    fallback / retry / breaker logic lives in
    :class:`~orchestrator.llm.router.LLMRouter` + E.4 middleware so the
    adapter has a single responsibility and is trivial to swap.
    """

    #: Stream RT-1 (RT-ADR-2) — Anthropic enforces schemas via one
    #: forced tool call carrying the schema (the stable path; no
    #: ``response_format`` on the Messages API).
    structured_output_capability: ClassVar[StructuredOutputCapability] = "tool_call"

    client: AnthropicClient
    model: str
    max_tokens: int = DEFAULT_MAX_TOKENS
    #: Sampling temperature (``ModelSpec.temperature``). ``None`` omits
    #: it from the request so the API applies its own default.
    temperature: float | None = None
    #: Resolves ``image_ref`` content blocks to bytes at call time (J.6
    #: Path A). ``None`` → image blocks are dropped with a warning.
    image_resolver: ImageResolver | None = None
    #: Stream L.L1 — flip Anthropic prompt caching markers on. When
    #: ``True`` the adapter wraps the outbound ``system`` field and the
    #: trailing ``_CACHE_CONTROL_TAIL_COUNT`` non-system messages in
    #: ``cache_control: {"type": "ephemeral"}`` so the upstream caches
    #: the prefix (Mini-ADR L-1). Defaults ``True`` because the
    #: feature is upstream-supported and lossless when the prefix is
    #: stable. The agent factory wires this from
    #: :attr:`ModelSpec.cache_enabled`.
    cache_enabled: bool = True
    #: Stream CM-9 (Mini-ADR CM-J2) — ``output_config.effort`` level.
    #: ``None`` omits the field (API default). The factory gates this on
    #: the model catalog's ``effort`` capability bit.
    effort: str | None = None
    #: Stream CM-9 — send ``thinking: {"type": "adaptive"}`` (4.6+).
    adaptive_thinking: bool = False
    #: Stream Thinking-Toggle — tri-state on/off override. ``False`` forces
    #: ``thinking: {"type": "disabled"}`` (and drops ``output_config.effort``);
    #: ``True`` / ``None`` keep the CM-9 ``effort`` + ``adaptive_thinking``
    #: behaviour (anthropic's untouched default is already dynamic thinking).
    thinking_enabled: bool | None = None
    #: Stream HX-13 (Mini-ADR HX-J4) — set after the tool-search beta is
    #: rejected once: this provider instance falls back to the application
    #: tier (no defer markers, no beta header) for its remaining lifetime.
    #: A restart retries the native tier.
    _native_search_disabled: bool = field(default=False, init=False, repr=False)

    async def _prepare_anthropic_request(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None,
        use_native: bool,
    ) -> dict[str, Any]:
        """Translate history + tools into ``client.messages`` /
        ``client.stream_messages`` kwargs. Shared by ``complete`` and
        ``stream`` (Task 4, P1') so both put the exact same request on
        the wire — only ``stream`` differs at the client. ``use_native``
        (the HX-13 beta-header decision) is computed by the caller and
        passed in; the defer-marker strip below instead reads the
        current ``_native_search_disabled`` instance state directly, so
        a retry call made right after that flag flips strips correctly
        even though the caller's own ``use_native`` for the retry is
        simply ``False``.
        """
        # Stream RT-2 (RT-ADR-5) — fold any mid-conversation SystemMessage
        # (the L2 ``<context-summary>`` lands mid-list) into the leading
        # system before mapping. Per-request only; input never mutated.
        messages = coalesce_system_messages(messages)
        system_text, mapped, anchor_indices = await _to_anthropic_messages(
            messages, self.image_resolver
        )
        # Stream HX-13 — deferred markers ride the specs (agent_node sets
        # them on the native_search tier). Once the beta has been rejected,
        # strip the markers so every later call goes out plain.
        if self._native_search_disabled:
            tools = [
                replace(spec, defer_loading=False) if spec.defer_loading else spec for spec in tools
            ]
        tool_choice: dict[str, Any] | None = None
        if output_schema is not None:
            # Stream RT-1 (RT-ADR-2 tool_call path) — one forced tool
            # carries the schema; any regular tools are superseded for
            # this call (the schema tool is the only valid response).
            tool_payload: list[dict[str, Any]] | None = [_structured_output_tool(output_schema)]
            tool_choice = {"type": "tool", "name": output_schema.name}
        else:
            tool_payload = [_to_anthropic_tool(spec) for spec in tools] if tools else None

        # Stream L.L1 — convert ``system`` from string to block list with
        # cache_control marker, then mark the trailing messages.
        # Capability Uplift Sprint #8 (Mini-ADR U-7) — also mark any
        # messages flagged with ``expert_work_cache_anchor`` (currently only
        # the per_session memory block) so the cache covers the prefix
        # ``[system, task, memories]`` across all turns.
        # When ``cache_enabled`` is False the adapter emits the original
        # string-shaped system so a manifest-level opt-out cleanly
        # disables the feature on a per-model basis.
        system_payload: str | list[dict[str, Any]] | None
        if self.cache_enabled:
            system_payload, mapped = _apply_cache_control(
                system_text, mapped, cache_anchor_indices=anchor_indices
            )
        else:
            system_payload = system_text

        if output_schema is not None:
            # Stream RT-1 — the API rejects a forced ``tool_choice`` when
            # (extended/adaptive) thinking is active, and 4.6+ models may
            # think by default when the field is omitted. Pin thinking off
            # for structured calls; effort is moot once disabled.
            thinking_payload: dict[str, Any] | None = {"type": "disabled"}
            output_config: dict[str, Any] | None = None
        elif self.thinking_enabled is False:
            # Stream Thinking-Toggle — explicit off; effort is moot once disabled.
            thinking_payload = {"type": "disabled"}
            output_config = None
        else:
            thinking_payload = {"type": "adaptive"} if self.adaptive_thinking else None
            output_config = {"effort": self.effort} if self.effort is not None else None

        return {
            "model": self.model,
            "system": system_payload,
            "messages": mapped,
            "tools": tool_payload,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "thinking": thinking_payload,
            "output_config": output_config,
            "betas": [_TOOL_SEARCH_BETA] if use_native else None,
            "tool_choice": tool_choice,
        }

    async def complete(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
    ) -> AIMessage:
        # Stream RT-1 (§ 7.5) — a structured call forces a single tool, so
        # the tool-search beta / defer markers step aside for this request.
        use_native = (
            any(spec.defer_loading for spec in tools)
            and not self._native_search_disabled
            and output_schema is None
        )
        request = await self._prepare_anthropic_request(
            messages=messages, tools=tools, output_schema=output_schema, use_native=use_native
        )
        try:
            body = await self.client.messages(**request)
        except LLMClientError:
            if not use_native:
                raise
            # Stream HX-13 (Mini-ADR HX-J4) — the beta was rejected (or the
            # request shape with defer markers was). Fail open: drop to the
            # application tier for this provider instance and resend once.
            # A vendor-side refusal must cost tokens, never capability.
            self._native_search_disabled = True
            disclosure_fallback_total.labels(provider="anthropic").inc()
            logger.warning("anthropic.tool_search_beta_rejected — falling back to app tier")
            retry = await self._prepare_anthropic_request(
                messages=messages, tools=tools, output_schema=output_schema, use_native=False
            )
            body = await self.client.messages(**retry)

        decoded = _from_anthropic_response(body)
        if output_schema is not None:
            return _extract_structured_response(decoded, output_schema)
        return decoded

    def new_stream_assembler(self) -> AnthropicStreamAssembler:
        return AnthropicStreamAssembler()

    async def stream(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
    ) -> AsyncIterator[LLMDelta]:
        # The router never drives the stream for structured output (it
        # routes output_schema to complete() instead — T1); a defensive
        # assert documents that invariant.
        assert output_schema is None  # noqa: S101 - structured output uses complete()
        use_native = any(spec.defer_loading for spec in tools) and not self._native_search_disabled
        request = await self._prepare_anthropic_request(
            messages=messages, tools=tools, output_schema=None, use_native=use_native
        )
        try:
            async for event in self.client.stream_messages(**request):
                yield delta_from_anthropic_event(event)
            return
        except LLMClientError:
            if not use_native:
                raise
            # Stream HX-13 (Mini-ADR HX-J4) — beta-rejection 400 arrives
            # pre-first-event, so the re-stream lives OUTSIDE this loop.
            self._native_search_disabled = True
            disclosure_fallback_total.labels(provider="anthropic").inc()
            logger.warning("anthropic.tool_search_beta_rejected — falling back to app tier")
        retry = await self._prepare_anthropic_request(
            messages=messages, tools=tools, output_schema=None, use_native=False
        )
        async for event in self.client.stream_messages(**retry):
            yield delta_from_anthropic_event(event)


def _truncate(text: str) -> str:
    if len(text) <= _ERROR_BODY_CHAR_CAP:
        return text
    return text[:_ERROR_BODY_CHAR_CAP] + "...[truncated]"


def _apply_cache_control(
    system: str | None,
    mapped: list[dict[str, Any]],
    *,
    cache_anchor_indices: Sequence[int] = (),
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
    """Stream L.L1 — annotate the outbound payload for prompt caching.

    Rewrites ``system`` from string form to a one-element block list
    with ``cache_control`` on the block, and adds ``cache_control`` to
    the last content block of each of the trailing
    ``_CACHE_CONTROL_TAIL_COUNT`` non-system messages. The block-list
    shape is what Anthropic requires when marking ``system`` for
    caching; the per-message marker lets the upstream cache
    progressively longer prefixes as the conversation grows. See
    Mini-ADR L-1.

    Capability Uplift Sprint #8 (Mini-ADR U-7): also marks any
    messages whose index appears in ``cache_anchor_indices`` —
    currently the ``per_session`` memory block at position 1. These
    are deduplicated against the trailing window so a message that
    is both an anchor and in the tail only gets one marker. Anthropic
    caps total ``cache_control`` markers at 4 per request
    (``system + _CACHE_CONTROL_TAIL_COUNT (2) + anchors (≤ 1) = ≤ 4``
    in Sprint #8 setup).

    Returns ``(system_payload, mapped)`` — ``mapped`` is mutated in
    place for clarity but also returned so callers can keep a
    single-expression assignment.
    """
    system_payload: list[dict[str, Any]] | None = None
    if system:
        system_payload = [
            {"type": "text", "text": system, "cache_control": dict(_CACHE_CONTROL_EPHEMERAL)}
        ]

    if not mapped:
        return system_payload, mapped

    # Build the set of indices to mark — union of the tail window and
    # the explicit anchor list. ``set`` collapses overlap (an anchor
    # already inside the tail window only gets one marker).
    tail_start = max(0, len(mapped) - _CACHE_CONTROL_TAIL_COUNT)
    indices_to_mark: set[int] = set(range(tail_start, len(mapped)))
    for idx in cache_anchor_indices:
        if 0 <= idx < len(mapped):
            indices_to_mark.add(idx)
            # Track anchor application separately from the tail so the
            # uplift metric reflects expert-work-injected breakpoints only.
            if idx < tail_start:
                record_anthropic_cache_anchor()

    for idx in sorted(indices_to_mark):
        _mark_message_cache_control(mapped[idx])

    return system_payload, mapped


def _mark_message_cache_control(message: dict[str, Any]) -> None:
    """Add ``cache_control`` to the last content block of a single
    outbound message. Plain-text content is upgraded to a block list so
    the marker has a place to live.
    """
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = [
            {"type": "text", "text": content, "cache_control": dict(_CACHE_CONTROL_EPHEMERAL)}
        ]
        return
    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last["cache_control"] = dict(_CACHE_CONTROL_EPHEMERAL)
        else:
            # ``content`` list with a non-dict tail entry — defensive
            # path; promote to a wrapper block carrying the marker so
            # the wire payload stays schema-valid.
            content[-1] = {
                "type": "text",
                "text": str(last),
                "cache_control": dict(_CACHE_CONTROL_EPHEMERAL),
            }


async def _to_anthropic_messages(
    messages: Sequence[BaseMessage], resolver: ImageResolver | None
) -> tuple[str | None, list[dict[str, Any]], list[int]]:
    """Split system content from the message list and map the rest.

    Anthropic places system prompts in a top-level ``system`` field; we
    concatenate any :class:`SystemMessage` instances we find rather
    than emitting an unsupported ``role: "system"`` message.

    Capability Uplift Sprint #8 (Mini-ADR U-7): tracks the indices
    (in the mapped list) of messages whose ``additional_kwargs`` carry
    ``"expert_work_cache_anchor": True`` so :func:`_apply_cache_control` can
    mark them with ``cache_control`` for prompt-cache prefix coverage.
    Returned as a side channel rather than baked into the mapped dicts
    so the wire payload stays Anthropic-clean.
    """
    system_parts: list[str] = []
    mapped: list[dict[str, Any]] = []
    cache_anchor_indices: list[int] = []

    for msg in messages:
        if isinstance(msg, SystemMessage):
            system_parts.append(_message_text(msg))
            continue
        if isinstance(msg, HumanMessage):
            mapped.append({"role": "user", "content": await _human_content(msg, resolver)})
        elif isinstance(msg, AIMessage):
            mapped.append(_ai_message_to_anthropic(msg))
        elif isinstance(msg, ToolMessage):
            mapped.append(_tool_message_to_anthropic(msg))
        else:
            # Unknown message subclass — fall back to a user message
            # with whatever text we can recover. Better than silently
            # dropping it.
            logger.warning(
                "anthropic_adapter.unknown_message_type type=%s",
                type(msg).__name__,
            )
            mapped.append({"role": "user", "content": _message_text(msg)})
        # Sprint #8 — note cache anchors AFTER the mapped append so the
        # index points at the freshly-added dict.
        extra = getattr(msg, "additional_kwargs", None)
        if isinstance(extra, dict) and extra.get("expert_work_cache_anchor"):
            cache_anchor_indices.append(len(mapped) - 1)

    system = "\n\n".join(p for p in system_parts if p) or None
    return system, mapped, cache_anchor_indices


def _ai_message_to_anthropic(msg: AIMessage) -> dict[str, Any]:
    tool_calls = list(getattr(msg, "tool_calls", None) or [])
    text = _message_text(msg)

    if not tool_calls:
        return {"role": "assistant", "content": text}

    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    for tc in tool_calls:
        content.append(
            {
                "type": "tool_use",
                "id": str(tc.get("id") or ""),
                "name": str(tc.get("name") or ""),
                "input": tc.get("args") or {},
            }
        )
    return {"role": "assistant", "content": content}


def _tool_message_to_anthropic(msg: ToolMessage) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": str(getattr(msg, "tool_call_id", "") or ""),
                "content": _message_text(msg),
            }
        ],
    }


def _message_text(msg: BaseMessage) -> str:
    """Stringify ``msg.content`` regardless of whether it's str or block list.

    LangChain allows ``content`` to be either ``str`` or
    ``list[ContentBlock]``; we flatten the list form by concatenating
    any block's ``"text"`` value so the adapter never sees a list at
    the wire boundary.
    """
    content = msg.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, Mapping):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


async def _human_content(
    msg: HumanMessage, resolver: ImageResolver | None
) -> str | list[dict[str, Any]]:
    """Map a ``HumanMessage`` to Anthropic ``content``.

    Plain text → a string. With ``image_ref`` blocks (J.6 Path A) → a
    block list whose images are resolved to base64 ``image`` blocks. A
    missing resolver drops the images with a warning so a text-only
    deployment never crashes on an image-bearing message.
    """
    text, image_refs = split_human_content(msg.content)
    if not image_refs:
        return text
    if resolver is None:
        logger.warning("anthropic_adapter.image_dropped_no_resolver count=%d", len(image_refs))
        return text
    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for ref in image_refs:
        resolved = await resolver.resolve(ref)
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": resolved.media_type,
                    "data": resolved.base64_data,
                },
            }
        )
    return blocks


def _structured_output_tool(spec: StructuredOutputSpec) -> dict[str, Any]:
    """Stream RT-1 — the single forced tool that carries the schema."""
    return {
        "name": spec.name,
        "description": "Return the final structured output for this request.",
        "input_schema": dict(spec.schema),
    }


def _extract_structured_response(message: AIMessage, spec: StructuredOutputSpec) -> AIMessage:
    """Stream RT-1 (tool_call path) — unwrap the forced tool call.

    The schema-carrying tool call is a wire mechanism, not a real tool
    invocation: leaking it as ``tool_calls`` would route the ReAct graph
    to the tools node. Its args are serialised back to JSON text so the
    router's validation loop sees the same shape as the native / prompt
    paths. A response without the forced call (a refusal) is returned
    as-is — its text will fail validation and trigger the correction
    retry (RT-ADR-1).
    """
    for call in message.tool_calls:
        if call.get("name") == spec.name:
            return message.model_copy(
                update={
                    "content": json.dumps(call.get("args") or {}, ensure_ascii=False),
                    "tool_calls": [],
                }
            )
    return message


def _to_anthropic_tool(spec: ToolSpec) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": spec.name,
        "description": spec.description,
        "input_schema": dict(spec.parameters) or {"type": "object", "properties": {}},
    }
    # Stream HX-13 — server-side tool search: a marked tool ships deferred
    # (the API retrieves and injects its schema on demand).
    if spec.defer_loading:
        payload["defer_loading"] = True
    return payload


def _from_anthropic_response(body: Mapping[str, Any]) -> AIMessage:
    """Decode Anthropic's content-blocks array into a LangChain AIMessage.

    Stream L.L1 — when the upstream returns cache token counters in
    ``usage`` (``cache_creation_input_tokens`` / ``cache_read_input_tokens``)
    they ride along on ``AIMessage.usage_metadata`` so dashboards /
    middleware (E.5 langfuse) can observe cache hit rate. The decoder
    is lenient: any of the fields may be missing on older API versions
    or non-cached requests.
    """
    blocks = body.get("content") or []
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
            elif block_type == "tool_use":
                tool_calls.append(
                    {
                        "id": str(block.get("id") or ""),
                        "name": str(block.get("name") or ""),
                        "args": dict(block.get("input") or {}),
                        "type": "tool_call",
                    }
                )

    usage_metadata = _extract_usage_metadata(body)
    if usage_metadata:
        return AIMessage(
            content="".join(text_parts),
            tool_calls=tool_calls,
            usage_metadata=usage_metadata,
        )
    return AIMessage(content="".join(text_parts), tool_calls=tool_calls)


def _extract_usage_metadata(body: Mapping[str, Any]) -> dict[str, Any] | None:
    """Stream L.L1 — pull cache-aware token counters out of the body.

    Anthropic returns ``usage`` with ``input_tokens`` /
    ``output_tokens`` plus the L1-specific
    ``cache_creation_input_tokens`` / ``cache_read_input_tokens`` when
    caching is active. The LangChain ``usage_metadata`` shape carries
    the standard counters in ``input_tokens`` / ``output_tokens`` /
    ``total_tokens``; cache counters land in
    ``input_token_details`` so downstream observability can split out
    the cached portion without crashing on older shapes.
    """
    usage_raw = body.get("usage")
    if not isinstance(usage_raw, Mapping):
        return None
    input_tokens = _coerce_int(usage_raw.get("input_tokens"))
    output_tokens = _coerce_int(usage_raw.get("output_tokens"))
    cache_creation = _coerce_int(usage_raw.get("cache_creation_input_tokens"))
    cache_read = _coerce_int(usage_raw.get("cache_read_input_tokens"))
    if (
        input_tokens is None
        and output_tokens is None
        and cache_creation is None
        and cache_read is None
    ):
        return None
    metadata: dict[str, Any] = {
        "input_tokens": input_tokens or 0,
        "output_tokens": output_tokens or 0,
        "total_tokens": (input_tokens or 0) + (output_tokens or 0),
    }
    cache_details: dict[str, int] = {}
    if cache_creation is not None:
        cache_details["cache_creation"] = cache_creation
    if cache_read is not None:
        cache_details["cache_read"] = cache_read
    if cache_details:
        metadata["input_token_details"] = cache_details
    return metadata


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):  # bool is a subclass of int — reject explicitly
        return None
    if isinstance(value, int):
        return value
    return None
