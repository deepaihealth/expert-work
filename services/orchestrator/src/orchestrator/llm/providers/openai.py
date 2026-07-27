"""OpenAI Chat Completions adapter — Stream E.11.

Translates the orchestrator's :class:`BaseMessage` history +
:class:`ToolSpec` catalogue into OpenAI's Chat Completions wire format,
posts via :class:`OpenAIClient` (httpx in production, recording client
in tests), and maps the response back to a LangChain :class:`AIMessage`
with ``tool_calls`` populated.

Wire-format mapping (M0 minimal):

- ``SystemMessage`` → ``{"role": "system", "content": <text>}``. Stream
  RT-2 (RT-ADR-5): mid-conversation SystemMessages are coalesced into
  the leading system entry before mapping — strict OpenAI-compatible
  backends reject a non-leading ``system`` role with 400. See
  :mod:`orchestrator.llm.coalesce`.
- ``HumanMessage`` → ``{"role": "user", "content": <text>}``; when the
  message carries ``image_ref`` blocks (J.6 Path A) ``content`` becomes a
  block list with ``{"type": "image_url", ...}`` entries, the images
  resolved to data URIs via an :class:`ImageResolver`.
- ``AIMessage`` (text only) → ``{"role": "assistant", "content": <text>}``.
- ``AIMessage`` with ``tool_calls`` → ``{"role": "assistant", "content":
  null, "tool_calls": [{"id": ..., "type": "function", "function":
  {"name": ..., "arguments": <json-string>}}]}``.
- ``ToolMessage`` → ``{"role": "tool", "tool_call_id": ..., "content":
  <text>}``.

Differences from :mod:`anthropic` adapter (kept in sync with vendor docs):

- OpenAI puts ``system`` in the messages array, Anthropic at the top
  level — adapter responsibility.
- OpenAI ``arguments`` is a JSON-encoded string; we ``json.dumps`` the
  ``args`` dict on the way out and ``json.loads`` on the way in.

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
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, runtime_checkable

import httpx
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

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
    LLMDelta,
    OpenAIStreamAssembler,
    delta_from_openai_chunk,
)
from orchestrator.llm.structured_output import (
    StructuredOutputCapability,
    schema_instruction,
)
from orchestrator.multimodal import ImageResolver, split_human_content
from orchestrator.tools.registry import ToolSpec

logger = logging.getLogger(__name__)


_DEFAULT_BASE_URL = "https://api.openai.com"
DEFAULT_CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
_DEFAULT_TIMEOUT_S = 60.0
_ERROR_BODY_CHAR_CAP = 500

_SSE_SKIP = object()
_SSE_DONE = object()


def _build_request_body(
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    temperature: float | None,
    extra_body: dict[str, Any] | None,
    tool_choice: dict[str, Any] | None,
    response_format: dict[str, Any] | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"model": model, "messages": messages}
    if tools:
        body["tools"] = tools
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    if response_format is not None:
        body["response_format"] = response_format
    if temperature is not None:
        body["temperature"] = temperature
    if extra_body:
        body.update(extra_body)
    return body


def _parse_sse_line(line: str) -> Any:
    """Parse one SSE line into a JSON chunk, ``_SSE_DONE``, or ``_SSE_SKIP``.

    Blank lines, comment lines (``:`` keepalive), and non-``data:`` lines
    are skipped; ``data: [DONE]`` terminates; malformed JSON is skipped
    (lenient, like the non-streaming decoder)."""
    line = line.strip()
    if not line or line.startswith(":") or not line.startswith("data:"):
        return _SSE_SKIP
    data = line[len("data:") :].strip()
    if data == "[DONE]":
        return _SSE_DONE
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return _SSE_SKIP
    return parsed if isinstance(parsed, Mapping) else _SSE_SKIP


def _looks_billing(message: str) -> bool:
    low = message.lower()
    return "quota" in low or "billing" in low or "balance" in low


def _classify_stream_error(error: Mapping[str, Any]) -> LLMError:
    """Map an in-band SSE ``error`` object to an :class:`LLMError`.

    OpenAI-wire error events carry ``{message, type, code}``. Without an
    HTTP status we default to a retryable :class:`LLMServerError`; a
    billing/quota marker in the message escalates to key-level via the
    shared classifier (status 429 forces the marker inspection path)."""
    message = str(error.get("message") or "")
    if _looks_billing(message):
        return classify_http_error("openai", 429, message)
    return LLMServerError(f"openai stream error: {message}")


def _response_to_stream_chunks(response: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Synthesize a single streaming chunk from a whole non-streaming
    response body, so a :class:`RecordingOpenAIClient` primed with only
    ``response`` still yields coherent deltas when consumed via the
    streaming path (the router prefers ``stream()`` for streaming-capable
    providers). Tool calls get an ``index`` so multi-call responses
    reassemble correctly."""
    choices = response.get("choices") or []
    if not choices or not isinstance(choices[0], Mapping):
        return []
    first = choices[0]
    message = first.get("message")
    if not isinstance(message, Mapping):
        return []
    delta: dict[str, Any] = {}
    content = message.get("content")
    if content is not None:
        delta["content"] = content
    reasoning = message.get("reasoning_content")
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    raw_tcs = message.get("tool_calls")
    if isinstance(raw_tcs, list) and raw_tcs:
        delta["tool_calls"] = [
            {"index": i, "id": tc.get("id"), "type": tc.get("type"), "function": tc.get("function")}
            for i, tc in enumerate(raw_tcs)
            if isinstance(tc, Mapping)
        ]
    chunk: dict[str, Any] = {
        "choices": [{"delta": delta, "finish_reason": first.get("finish_reason")}]
    }
    for key in ("usage", "model", "system_fingerprint"):
        if key in response:
            chunk[key] = response[key]
    return [chunk]


@runtime_checkable
class OpenAIClient(Protocol):
    """Sized to the one Chat Completions endpoint we use.

    Both :class:`HTTPOpenAIClient` and :class:`RecordingOpenAIClient`
    implement this. Adapters raise :class:`LLMError` subclasses rather
    than letting transport exceptions leak.
    """

    async def chat_completions(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float | None = None,
        extra_body: dict[str, Any] | None = None,
        tool_choice: dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """POST ``/v1/chat/completions`` and return the parsed JSON body.

        ``extra_body`` (Stream CM-10, Mini-ADR CM-L3) merges vendor
        extension fields into the top-level request body — the thinking
        controls of the OpenAI-compatible vendors (``enable_thinking``,
        ``thinking``, ``reasoning_effort``) are all top-level
        non-standard fields, so one channel covers every vendor.

        ``response_format`` (Stream RT-1, native path) carries the
        ``{"type": "json_schema", ...}`` structured-output constraint;
        ``None`` omits the field entirely."""

    def stream_chat_completions(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float | None = None,
        extra_body: dict[str, Any] | None = None,
        tool_choice: dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[Mapping[str, Any]]:
        """Stream ``/v1/chat/completions`` (``stream=true``), yielding each
        parsed SSE JSON chunk and stopping at ``[DONE]``. An HTTP >= 400
        status raises the classified :class:`LLMError` before the first
        chunk; an in-band ``error`` event raises mid-stream (Stream L, P1)."""


@dataclass
class HTTPOpenAIClient:
    """Production :class:`OpenAIClient` — talks to the real API.

    ``transport`` is an optional injection point for tests; see
    :class:`~orchestrator.llm.providers.anthropic.HTTPAnthropicClient`
    for the rationale.

    ``chat_completions_path`` lets OpenAI-compatible vendors override the
    default ``/v1/chat/completions`` suffix — most (DeepSeek, Moonshot,
    DashScope compatible-mode) keep ``/v1/chat/completions``, but Zhipu
    GLM uses ``/api/paas/v4/chat/completions`` and Volcengine ARK
    (Doubao) uses ``/api/v3/chat/completions``. See
    :mod:`orchestrator.llm.providers.openai_compatible` for the
    pre-configured factory functions.
    """

    api_key: str
    base_url: str = _DEFAULT_BASE_URL
    timeout_s: float = _DEFAULT_TIMEOUT_S
    transport: httpx.AsyncBaseTransport | None = None
    chat_completions_path: str = DEFAULT_CHAT_COMPLETIONS_PATH
    #: Auth header name + value prefix. OpenAI and the compatible
    #: regional vendors use ``Authorization: Bearer <key>``; Azure
    #: OpenAI uses ``api-key: <key>`` (prefix empty).
    api_key_header: str = "authorization"
    api_key_prefix: str = "Bearer "
    #: 一期 Task 5 — process-level shared client. ``None`` falls back to the
    #: original per-call ``async with httpx.AsyncClient(...)`` behaviour
    #: (tests / eval CLI / not-yet-wired production paths). When injected it
    #: must NOT be closed here: it is owned by the control-plane lifespan and
    #: outlives every individual call.
    http: httpx.AsyncClient | None = None

    async def chat_completions(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float | None = None,
        extra_body: dict[str, Any] | None = None,
        tool_choice: dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        body = _build_request_body(
            model=model,
            messages=messages,
            tools=tools,
            temperature=temperature,
            extra_body=extra_body,
            tool_choice=tool_choice,
            response_format=response_format,
        )

        try:
            async with client_for(
                self.http, timeout=self.timeout_s, transport=self.transport
            ) as client:
                response = await client.post(
                    f"{self.base_url}{self.chat_completions_path}",
                    headers={
                        self.api_key_header: f"{self.api_key_prefix}{self.api_key}",
                        "content-type": "application/json",
                    },
                    json=body,
                    timeout=self.timeout_s,  # per-request — governs even when sharing a client
                )
        except httpx.HTTPError as exc:
            raise LLMNetworkError(f"openai: {exc}") from exc

        status = response.status_code
        if status >= 400:
            # Stream Y-MK — shared classifier splits account/key-level failures
            # (402 / quota / billing → LLMKeyUnavailableError, router tries a
            # sibling key) from plain 429 rate-limits, malformed 4xx, and 5xx.
            # 401 stays an LLMUnauthorizedError for the L.L8 OAuth refresh path.
            raise classify_http_error("openai", status, _truncate(response.text))

        data = response.json()
        if not isinstance(data, Mapping):
            raise LLMServerError(f"openai returned non-object body: {type(data).__name__}")
        return data

    async def stream_chat_completions(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float | None = None,
        extra_body: dict[str, Any] | None = None,
        tool_choice: dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[Mapping[str, Any]]:
        body = _build_request_body(
            model=model,
            messages=messages,
            tools=tools,
            temperature=temperature,
            extra_body=extra_body,
            tool_choice=tool_choice,
            response_format=response_format,
        )
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        # Disable httpx's read timeout on the stream — the router's
        # idle_timeout_s governs inter-chunk silence (Stream L, P1).
        timeout = httpx.Timeout(self.timeout_s, read=None)
        try:
            async with client_for(self.http, timeout=timeout, transport=self.transport) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}{self.chat_completions_path}",
                    headers={
                        self.api_key_header: f"{self.api_key_prefix}{self.api_key}",
                        "content-type": "application/json",
                    },
                    json=body,
                    timeout=timeout,  # per-request — the shared client's default must not win
                ) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        raise classify_http_error(
                            "openai", response.status_code, _truncate(response.text)
                        )
                    async for line in response.aiter_lines():
                        chunk = _parse_sse_line(line)
                        if chunk is _SSE_SKIP:
                            continue
                        if chunk is _SSE_DONE:
                            return
                        assert isinstance(chunk, Mapping)  # noqa: S101
                        error = chunk.get("error")
                        if isinstance(error, Mapping):
                            raise _classify_stream_error(error)
                        yield chunk
        except httpx.HTTPError as exc:
            raise LLMNetworkError(f"openai: {exc}") from exc


@dataclass
class RecordingOpenAIClient:
    """In-memory :class:`OpenAIClient` for dev / tests.

    Behaviour mirrors
    :class:`~orchestrator.llm.providers.anthropic.RecordingAnthropicClient`:
    canned response, recorded calls, optional injected exception.
    """

    response: Mapping[str, Any] = field(default_factory=dict)
    stream_chunks: list[Mapping[str, Any]] = field(default_factory=list)
    raise_with: BaseException | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def chat_completions(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float | None = None,
        extra_body: dict[str, Any] | None = None,
        tool_choice: dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "tools": tools,
                "temperature": temperature,
                "extra_body": extra_body,
                "tool_choice": tool_choice,
                "response_format": response_format,
            }
        )
        if self.raise_with is not None:
            raise self.raise_with
        return self.response

    async def stream_chat_completions(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float | None = None,
        extra_body: dict[str, Any] | None = None,
        tool_choice: dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[Mapping[str, Any]]:
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "tools": tools,
                "temperature": temperature,
                "extra_body": extra_body,
                "tool_choice": tool_choice,
                "response_format": response_format,
                "stream": True,
            }
        )
        if self.raise_with is not None:
            raise self.raise_with
        chunks = self.stream_chunks or _response_to_stream_chunks(self.response)
        for chunk in chunks:
            yield chunk


@dataclass
class OpenAIProvider:
    """:class:`LLMProvider` for OpenAI Chat Completions.

    ``temperature`` is the manifest's sampling temperature
    (``ModelSpec.temperature``). ``None`` omits it from the request so
    the provider applies its own default.
    """

    #: Stream RT-1 (RT-ADR-2) — stock OpenAI enforces schemas natively
    #: via ``response_format: {"type": "json_schema"}`` (strict).
    structured_output_capability: ClassVar[StructuredOutputCapability] = "native"

    client: OpenAIClient
    model: str
    temperature: float | None = None
    #: Resolves ``image_ref`` content blocks to bytes at call time (J.6
    #: Path A). ``None`` → image blocks are dropped with a warning.
    image_resolver: ImageResolver | None = None
    #: Stream CM-10 (Mini-ADR CM-L3) — pre-translated vendor thinking
    #: payload (``_thinking_payload``), merged into the request body.
    #: ``None`` (every untouched manifest) keeps the body byte-identical.
    thinking_payload: dict[str, Any] | None = None
    #: glm-only (2026-07-18) — vendor request params merged into ``extra_body``
    #: ONLY on the streaming path. glm's ``tool_stream: true`` makes it emit
    #: tool-call args incrementally instead of one silent batch, so the router's
    #: idle timer resets per fragment (else a large tool-call composes in
    #: silence past ``idle_timeout_s`` → spurious ``stream_idle_timeout``). Kept
    #: OFF the non-streaming ``complete`` path (structured output) where glm may
    #: reject it. ``None`` (every other agent/provider) → request byte-identical.
    stream_extra_body: dict[str, Any] | None = None
    #: Stream HX-13 (Mini-ADR HX-J4) — set after the allowed_tools
    #: constraint is rejected once: this provider instance falls back to
    #: the application tier for its remaining lifetime (restart retries).
    _allowed_tools_disabled: bool = field(default=False, init=False, repr=False)

    async def _prepare_request(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None,
        use_allowed: bool,
    ) -> dict[str, Any]:
        """Translate history + tools into ``chat_completions`` kwargs.
        Shared by ``complete`` and ``stream`` so both put the exact same
        request on the wire (only ``stream`` differs at the client)."""
        if output_schema is not None and self.structured_output_capability != "native":
            messages = [*messages, SystemMessage(content=schema_instruction(output_schema))]
        messages = coalesce_system_messages(messages)
        mapped = await _to_openai_messages(messages, self.image_resolver)
        response_format: dict[str, Any] | None = None
        if output_schema is not None and self.structured_output_capability == "native":
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": output_schema.name,
                    "schema": output_schema.schema,
                    "strict": output_schema.strict,
                },
            }
        tool_payload = [_to_openai_tool(spec) for spec in tools] if tools else None
        tool_choice: dict[str, Any] | None = None
        if use_allowed:
            tool_choice = {
                "type": "allowed_tools",
                "mode": "auto",
                "tools": [
                    {"type": "function", "function": {"name": spec.name}}
                    for spec in tools
                    if not spec.defer_loading
                ],
            }
        return {
            "model": self.model,
            "messages": mapped,
            "tools": tool_payload,
            "temperature": self.temperature,
            "extra_body": self.thinking_payload,
            "tool_choice": tool_choice,
            "response_format": response_format,
        }

    def _apply_stream_extra_body(self, request: dict[str, Any]) -> None:
        """Merge :attr:`stream_extra_body` into a prepared request's
        ``extra_body`` (stream path only; a no-op when ``None``)."""
        if self.stream_extra_body is not None:
            request["extra_body"] = {**(request["extra_body"] or {}), **self.stream_extra_body}

    async def complete(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
    ) -> AIMessage:
        use_allowed = any(spec.defer_loading for spec in tools) and not self._allowed_tools_disabled
        request = await self._prepare_request(
            messages=messages, tools=tools, output_schema=output_schema, use_allowed=use_allowed
        )
        try:
            body = await self.client.chat_completions(**request)
        except LLMClientError:
            if not use_allowed:
                raise
            self._allowed_tools_disabled = True
            disclosure_fallback_total.labels(provider="openai").inc()
            logger.warning("openai.allowed_tools_rejected — falling back to app tier")
            retry = await self._prepare_request(
                messages=messages, tools=tools, output_schema=output_schema, use_allowed=False
            )
            body = await self.client.chat_completions(**retry)
        return _from_openai_response(body)

    async def stream(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
    ) -> AsyncIterator[LLMDelta]:
        use_allowed = any(spec.defer_loading for spec in tools) and not self._allowed_tools_disabled
        request = await self._prepare_request(
            messages=messages, tools=tools, output_schema=output_schema, use_allowed=use_allowed
        )
        self._apply_stream_extra_body(request)
        try:
            async for chunk in self.client.stream_chat_completions(**request):
                yield delta_from_openai_chunk(chunk)
            return
        except LLMClientError:
            if not use_allowed:
                raise
            # HX-13 (Mini-ADR HX-J4) — allowed_tools rejected pre-stream.
            # Fail open: drop to the application tier and re-stream once.
            self._allowed_tools_disabled = True
            disclosure_fallback_total.labels(provider="openai").inc()
            logger.warning("openai.allowed_tools_rejected — falling back to app tier")
        retry = await self._prepare_request(
            messages=messages, tools=tools, output_schema=output_schema, use_allowed=False
        )
        self._apply_stream_extra_body(retry)
        async for chunk in self.client.stream_chat_completions(**retry):
            yield delta_from_openai_chunk(chunk)

    def new_stream_assembler(self) -> OpenAIStreamAssembler:
        return OpenAIStreamAssembler()


def _truncate(text: str) -> str:
    if len(text) <= _ERROR_BODY_CHAR_CAP:
        return text
    return text[:_ERROR_BODY_CHAR_CAP] + "...[truncated]"


async def _to_openai_messages(
    messages: Sequence[BaseMessage], resolver: ImageResolver | None
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            out.append({"role": "system", "content": _message_text(msg)})
        elif isinstance(msg, HumanMessage):
            out.append({"role": "user", "content": await _human_content(msg, resolver)})
        elif isinstance(msg, AIMessage):
            out.append(_ai_message_to_openai(msg))
        elif isinstance(msg, ToolMessage):
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": str(getattr(msg, "tool_call_id", "") or ""),
                    "content": _message_text(msg),
                }
            )
        else:
            logger.warning(
                "openai_adapter.unknown_message_type type=%s",
                type(msg).__name__,
            )
            out.append({"role": "user", "content": _message_text(msg)})
    return out


def _ai_message_to_openai(msg: AIMessage) -> dict[str, Any]:
    tool_calls = list(getattr(msg, "tool_calls", None) or [])
    text = _message_text(msg)

    if not tool_calls:
        return {"role": "assistant", "content": text}

    return {
        "role": "assistant",
        "content": text or None,
        "tool_calls": [
            {
                "id": str(tc.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(tc.get("name") or ""),
                    "arguments": json.dumps(tc.get("args") or {}),
                },
            }
            for tc in tool_calls
        ],
    }


def _message_text(msg: BaseMessage) -> str:
    """See :func:`orchestrator.llm.providers.anthropic._message_text`."""
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
    """Map a ``HumanMessage`` to OpenAI ``content``.

    Plain text → a string. With ``image_ref`` blocks (J.6 Path A) → a
    block list whose images are resolved to ``image_url`` data URIs. A
    missing resolver drops the images with a warning so a text-only
    deployment never crashes on an image-bearing message.
    """
    text, image_refs = split_human_content(msg.content)
    if not image_refs:
        return text
    if resolver is None:
        logger.warning("openai_adapter.image_dropped_no_resolver count=%d", len(image_refs))
        return text
    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for ref in image_refs:
        resolved = await resolver.resolve(ref)
        blocks.append({"type": "image_url", "image_url": {"url": resolved.data_uri}})
    return blocks


def _to_openai_tool(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": dict(spec.parameters) or {"type": "object", "properties": {}},
        },
    }


def _from_openai_response(body: Mapping[str, Any]) -> AIMessage:
    """Decode OpenAI choice[0].message into a LangChain AIMessage.

    Tolerant of empty / malformed responses — returns an empty
    :class:`AIMessage` rather than raising so the router can let the
    LLM "say nothing" gracefully. Real production errors come from
    :class:`LLMError` raised by the client, not from parse.
    """
    choices = body.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return AIMessage(content="")

    first = choices[0]
    if not isinstance(first, Mapping):
        return AIMessage(content="")
    message = first.get("message")
    if not isinstance(message, Mapping):
        return AIMessage(content="")

    raw_content = message.get("content")
    text = raw_content if isinstance(raw_content, str) else ""
    raw_tool_calls = message.get("tool_calls") or []

    tool_calls: list[dict[str, Any]] = []
    if isinstance(raw_tool_calls, list):
        for tc in raw_tool_calls:
            if not isinstance(tc, Mapping):
                continue
            fn = tc.get("function") or {}
            if not isinstance(fn, Mapping):
                continue
            tool_calls.append(
                {
                    "id": str(tc.get("id") or ""),
                    "name": str(fn.get("name") or ""),
                    "args": _parse_arguments(fn.get("arguments")),
                    "type": "tool_call",
                }
            )

    # Event-stream enrichment — surface the fields the OpenAI-compatible
    # vendors return but the M0 minimal decoder dropped:
    #   * ``usage_metadata`` (body.usage) — REQUIRED for token metering
    #     (TokenUsageMiddleware reads AIMessage.usage_metadata; without it
    #     every compat-vendor turn meters zero) + langfuse cost observability.
    #   * ``additional_kwargs.reasoning_content`` — the thinking trace
    #     (DeepSeek / Qwen / Doubao return ``message.reasoning_content``).
    #   * ``response_metadata`` — finish_reason / model / system_fingerprint.
    additional_kwargs: dict[str, Any] = {}
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        additional_kwargs["reasoning_content"] = reasoning

    return AIMessage(
        content=text,
        tool_calls=tool_calls,
        additional_kwargs=additional_kwargs,
        response_metadata=_extract_response_metadata(body, first),
        usage_metadata=_extract_usage_metadata(body),
    )


def _extract_usage_metadata(body: Mapping[str, Any]) -> dict[str, Any] | None:
    """Map OpenAI ``usage`` → the LangChain ``usage_metadata`` shape.

    OpenAI returns ``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``
    plus optional ``prompt_tokens_details.cached_tokens`` and
    ``completion_tokens_details.reasoning_tokens``. LangChain carries the
    standard counters in ``input_tokens`` / ``output_tokens`` / ``total_tokens``
    with cache/reasoning splits in ``input_token_details`` /
    ``output_token_details``. Lenient: ``None`` when no usable counter is
    present (older / streaming-only shapes).
    """
    usage_raw = body.get("usage")
    if not isinstance(usage_raw, Mapping):
        return None
    input_tokens = _coerce_int(usage_raw.get("prompt_tokens"))
    output_tokens = _coerce_int(usage_raw.get("completion_tokens"))
    total = _coerce_int(usage_raw.get("total_tokens"))
    if input_tokens is None and output_tokens is None and total is None:
        return None
    metadata: dict[str, Any] = {
        "input_tokens": input_tokens or 0,
        "output_tokens": output_tokens or 0,
        "total_tokens": total if total is not None else (input_tokens or 0) + (output_tokens or 0),
    }
    prompt_details = usage_raw.get("prompt_tokens_details")
    if isinstance(prompt_details, Mapping):
        cached = _coerce_int(prompt_details.get("cached_tokens"))
        if cached is not None:
            metadata["input_token_details"] = {"cache_read": cached}
    completion_details = usage_raw.get("completion_tokens_details")
    if isinstance(completion_details, Mapping):
        reasoning = _coerce_int(completion_details.get("reasoning_tokens"))
        if reasoning is not None:
            metadata["output_token_details"] = {"reasoning": reasoning}
    return metadata


def _extract_response_metadata(
    body: Mapping[str, Any], choice: Mapping[str, Any]
) -> dict[str, Any]:
    """finish_reason / model / system_fingerprint, omitting absent keys."""
    metadata: dict[str, Any] = {}
    finish_reason = choice.get("finish_reason")
    if isinstance(finish_reason, str) and finish_reason:
        metadata["finish_reason"] = finish_reason
    model = body.get("model")
    if isinstance(model, str) and model:
        metadata["model_name"] = model
    fingerprint = body.get("system_fingerprint")
    if isinstance(fingerprint, str) and fingerprint:
        metadata["system_fingerprint"] = fingerprint
    return metadata


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):  # bool is an int subclass — reject explicitly
        return None
    if isinstance(value, int):
        return value
    return None


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """OpenAI sends ``arguments`` as a JSON string; tolerate dict too."""
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}
