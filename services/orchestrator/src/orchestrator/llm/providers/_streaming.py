"""Streaming wire model for OpenAI Chat Completions SSE — Stream L (P1).

Normalizes the vendor's ``stream=true`` SSE chunks into transport- and
router-agnostic :class:`LLMDelta` objects, and re-assembles a delta
sequence into a :class:`AIMessage` via the SAME decoder the non-streaming
path uses (:func:`orchestrator.llm.providers.openai._from_openai_response`)
— so an assembled message is byte-identical to today's whole-response
result for the same vendor content.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from langchain_core.messages import AIMessage, BaseMessage

from expert_work.protocol import StructuredOutputSpec
from orchestrator.tools.registry import ToolSpec


@dataclass(frozen=True)
class ToolCallChunk:
    """One OpenAI streaming tool-call fragment, keyed by ``index``.

    ``id`` and ``name`` arrive once (first fragment for that index);
    ``args_fragment`` accumulates across fragments into the full JSON
    argument string.

    ``index_missing`` records that the vendor sent NO ``index`` on this
    fragment — ``index`` then holds the 0 fallback, which is a usable
    dedup key but NOT a slot identity (two unindexed calls would share
    it). :class:`OpenAIStreamAssembler` consults the flag rather than
    ``index`` when deciding which call a fragment belongs to.
    """

    index: int
    id: str | None = None
    name: str | None = None
    args_fragment: str = ""
    index_missing: bool = False


@dataclass(frozen=True)
class LLMDelta:
    """One normalized SSE chunk. ``has_progress`` marks a chunk that
    carries real generation (content / reasoning / a tool-call fragment)
    — the router's first-token detection ignores role-only / usage-only
    chunks."""

    content: str = ""
    reasoning: str = ""
    tool_calls: tuple[ToolCallChunk, ...] = ()
    finish_reason: str | None = None
    usage: Mapping[str, Any] | None = None
    model: str | None = None
    system_fingerprint: str | None = None

    @property
    def has_progress(self) -> bool:
        return bool(self.content or self.reasoning or self.tool_calls)


def delta_from_openai_chunk(chunk: Mapping[str, Any]) -> LLMDelta:
    """Map one parsed OpenAI streaming chunk to an :class:`LLMDelta`.

    Tolerant of missing keys — malformed chunks degrade to an empty
    (no-progress) delta rather than raising, mirroring the lenient
    non-streaming decoder.
    """
    choices = chunk.get("choices") or []
    delta: Mapping[str, Any] = {}
    finish_reason: str | None = None
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        raw_delta = choices[0].get("delta")
        if isinstance(raw_delta, Mapping):
            delta = raw_delta
        fr = choices[0].get("finish_reason")
        if isinstance(fr, str) and fr:
            finish_reason = fr

    content = delta.get("content")
    reasoning = delta.get("reasoning_content")
    tool_calls: list[ToolCallChunk] = []
    raw_tcs = delta.get("tool_calls")
    if isinstance(raw_tcs, list):
        for tc in raw_tcs:
            if not isinstance(tc, Mapping):
                continue
            fn_raw = tc.get("function")
            fn: Mapping[str, Any] = fn_raw if isinstance(fn_raw, Mapping) else {}
            raw_index = tc.get("index")
            idx: int | None = raw_index if isinstance(raw_index, int) else None
            tool_calls.append(
                ToolCallChunk(
                    index=idx if idx is not None else 0,
                    id=str(tc["id"]) if tc.get("id") else None,
                    name=str(fn["name"]) if fn.get("name") else None,
                    args_fragment=str(fn.get("arguments") or ""),
                    index_missing=idx is None,
                )
            )

    usage = chunk.get("usage")
    model = chunk.get("model")
    fingerprint = chunk.get("system_fingerprint")
    return LLMDelta(
        content=content if isinstance(content, str) else "",
        reasoning=reasoning if isinstance(reasoning, str) else "",
        tool_calls=tuple(tool_calls),
        finish_reason=finish_reason,
        usage=usage if isinstance(usage, Mapping) else None,
        model=model if isinstance(model, str) and model else None,
        system_fingerprint=fingerprint if isinstance(fingerprint, str) and fingerprint else None,
    )


class _ToolAcc:
    __slots__ = ("args", "id", "name")

    def __init__(self) -> None:
        self.id: str | None = None
        self.name: str | None = None
        self.args: list[str] = []


#: One accumulator slot in :class:`OpenAIStreamAssembler`. Vendor-supplied
#: ``index`` values live in the ``"i"`` namespace, slots synthesized for
#: fragments that arrived WITHOUT an ``index`` live in ``"s"`` — the two can
#: never collide, so an unindexed call cannot land on an indexed one's slot.
_SlotKey = tuple[str, int]


class OpenAIStreamAssembler:
    """Accumulate :class:`LLMDelta` chunks into a synthetic non-streaming
    body, then decode with the shared
    :func:`~orchestrator.llm.providers.openai._from_openai_response` so the
    result is byte-identical to the whole-response path."""

    def __init__(self) -> None:
        self._content: list[str] = []
        self._reasoning: list[str] = []
        self._tools: dict[_SlotKey, _ToolAcc] = {}
        self._tool_order: list[_SlotKey] = []
        self._last_slot: _SlotKey | None = None
        self._next_synthetic = 0
        self._usage: Mapping[str, Any] | None = None
        self._model: str | None = None
        self._fingerprint: str | None = None
        self._finish: str | None = None

    def _slot_for(self, tc: ToolCallChunk) -> _SlotKey:
        """Which accumulator this fragment belongs to.

        A fragment carrying ``index`` — what the OpenAI Chat Completions
        wire defines, so the overwhelmingly common case — takes the first
        branch: one slot per index, byte-for-byte the behavior that
        existed before this method, so interleaved fragments still route
        by index.

        ``index`` is spec-mandated, but we have never captured vendor SSE
        to confirm that every compat vendor actually sends it, so its
        absence is handled defensively. Arrival order on the wire is then
        the only signal left: a fragment carrying ``id``/``name`` OPENS a
        call, and an arguments-only fragment CONTINUES the one most
        recently touched. Folding them all onto index 0 instead merged
        distinct calls into one — silently, since the concatenated
        arguments then fail to parse and decode to ``{}``.
        """
        if not tc.index_missing:
            return ("i", tc.index)
        if self._last_slot is None or tc.id is not None or tc.name is not None:
            slot = ("s", self._next_synthetic)
            self._next_synthetic += 1
            return slot
        return self._last_slot

    def add(self, delta: LLMDelta) -> None:
        if delta.content:
            self._content.append(delta.content)
        if delta.reasoning:
            self._reasoning.append(delta.reasoning)
        for tc in delta.tool_calls:
            slot = self._slot_for(tc)
            self._last_slot = slot
            acc = self._tools.get(slot)
            if acc is None:
                acc = _ToolAcc()
                self._tools[slot] = acc
                self._tool_order.append(slot)
            if tc.id is not None:
                acc.id = tc.id
            if tc.name is not None:
                acc.name = tc.name
            if tc.args_fragment:
                acc.args.append(tc.args_fragment)
        if delta.usage is not None:
            self._usage = delta.usage
        if delta.model is not None:
            self._model = delta.model
        if delta.system_fingerprint is not None:
            self._fingerprint = delta.system_fingerprint
        if delta.finish_reason is not None:
            self._finish = delta.finish_reason

    def build(self, *, interrupted: bool = False) -> AIMessage:
        # Deferred import breaks the openai <-> _streaming import cycle.
        from orchestrator.llm.providers.openai import _from_openai_response

        content = "".join(self._content)
        tool_calls: list[dict[str, Any]] = []
        for slot in self._tool_order:
            acc = self._tools[slot]
            args_str = "".join(acc.args)
            if interrupted and not _is_valid_json_object(args_str):
                # A tool call whose arguments never completed cannot be
                # safely dispatched — drop it from the interrupted result.
                continue
            tool_calls.append(
                {
                    "id": acc.id or "",
                    "type": "function",
                    "function": {"name": acc.name or "", "arguments": args_str},
                }
            )

        message: dict[str, Any] = {"role": "assistant"}
        message["content"] = content if content or not tool_calls else None
        if self._reasoning:
            message["reasoning_content"] = "".join(self._reasoning)
        if tool_calls:
            message["tool_calls"] = tool_calls

        finish = self._finish or ("stream_idle_timeout" if interrupted else None)
        choice: dict[str, Any] = {"message": message, "finish_reason": finish}
        body: dict[str, Any] = {"choices": [choice]}
        if self._model:
            body["model"] = self._model
        if self._fingerprint:
            body["system_fingerprint"] = self._fingerprint
        if self._usage is not None:
            body["usage"] = self._usage
        return _from_openai_response(body)


def _is_valid_json_object(raw: str) -> bool:
    if not raw:
        return False
    try:
        return isinstance(json.loads(raw), dict)
    except json.JSONDecodeError:
        return False


def delta_from_anthropic_event(event: Mapping[str, Any]) -> LLMDelta:
    """Map one Anthropic Messages SSE event to an :class:`LLMDelta`.
    Lenient: unknown / structural events (content_block_stop, message_stop,
    ping) yield an empty (no-progress) delta."""
    etype = event.get("type")
    if etype == "message_start":
        message = event.get("message")
        message = message if isinstance(message, Mapping) else {}
        usage = message.get("usage")
        model = message.get("model")
        return LLMDelta(
            usage=usage if isinstance(usage, Mapping) else None,
            model=model if isinstance(model, str) and model else None,
        )
    if etype == "content_block_start":
        cb = event.get("content_block")
        cb = cb if isinstance(cb, Mapping) else {}
        if cb.get("type") == "tool_use":
            idx = event.get("index")
            return LLMDelta(
                tool_calls=(
                    ToolCallChunk(
                        index=idx if isinstance(idx, int) else 0,
                        id=str(cb["id"]) if cb.get("id") else None,
                        name=str(cb["name"]) if cb.get("name") else None,
                    ),
                )
            )
        return LLMDelta()
    if etype == "content_block_delta":
        delta = event.get("delta")
        delta = delta if isinstance(delta, Mapping) else {}
        dtype = delta.get("type")
        if dtype == "text_delta":
            text = delta.get("text")
            return LLMDelta(content=text if isinstance(text, str) else "")
        if dtype == "thinking_delta":
            th = delta.get("thinking")
            return LLMDelta(reasoning=th if isinstance(th, str) else "")
        if dtype == "input_json_delta":
            idx = event.get("index")
            pj = delta.get("partial_json")
            return LLMDelta(
                tool_calls=(
                    ToolCallChunk(
                        index=idx if isinstance(idx, int) else 0,
                        args_fragment=pj if isinstance(pj, str) else "",
                    ),
                )
            )
        return LLMDelta()
    if etype == "message_delta":
        delta = event.get("delta")
        delta = delta if isinstance(delta, Mapping) else {}
        stop = delta.get("stop_reason")
        usage = event.get("usage")
        return LLMDelta(
            finish_reason=stop if isinstance(stop, str) and stop else None,
            usage=usage if isinstance(usage, Mapping) else None,
        )
    return LLMDelta()


class AnthropicStreamAssembler:
    """Accumulate Anthropic :class:`LLMDelta` chunks into a synthetic
    Messages response body, then decode with the shared
    :func:`~orchestrator.llm.providers.anthropic._from_anthropic_response`
    (byte-identical to the non-streaming path). ``thinking`` is dropped
    (the decoder ignores it); usage from message_start + message_delta is
    merged."""

    def __init__(self) -> None:
        self._content: list[str] = []
        self._tools: dict[int, _ToolAcc] = {}
        self._tool_order: list[int] = []
        self._usage: dict[str, Any] = {}
        self._finish: str | None = None

    def add(self, delta: LLMDelta) -> None:
        if delta.content:
            self._content.append(delta.content)
        # reasoning intentionally dropped from the final message
        for tc in delta.tool_calls:
            acc = self._tools.get(tc.index)
            if acc is None:
                acc = _ToolAcc()
                self._tools[tc.index] = acc
                self._tool_order.append(tc.index)
            if tc.id is not None:
                acc.id = tc.id
            if tc.name is not None:
                acc.name = tc.name
            if tc.args_fragment:
                acc.args.append(tc.args_fragment)
        if delta.usage is not None:
            self._usage.update(delta.usage)  # MERGE (input from start, output from delta)
        if delta.finish_reason is not None:
            self._finish = delta.finish_reason

    def build(self, *, interrupted: bool = False) -> AIMessage:
        from orchestrator.llm.providers.anthropic import _from_anthropic_response

        blocks: list[dict[str, Any]] = []
        text = "".join(self._content)
        if text:
            blocks.append({"type": "text", "text": text})
        for idx in self._tool_order:
            acc = self._tools[idx]
            args_str = "".join(acc.args)
            if interrupted and not _is_valid_json_object(args_str):
                continue
            input_obj = json.loads(args_str) if _is_valid_json_object(args_str) else {}
            blocks.append(
                {"type": "tool_use", "id": acc.id or "", "name": acc.name or "", "input": input_obj}
            )
        body: dict[str, Any] = {"content": blocks}
        if self._usage:
            body["usage"] = self._usage
        return _from_anthropic_response(body)


@runtime_checkable
class StreamAssembler(Protocol):
    """Accumulates provider deltas into a final :class:`AIMessage`. Each
    provider supplies its own (OpenAI-wire vs Anthropic wire assemble
    differently) via ``StreamingLLMProvider.new_stream_assembler``."""

    def add(self, delta: LLMDelta) -> None:
        """Accumulate one delta into the pending message."""

    def build(self, *, interrupted: bool = False) -> AIMessage:
        """Finalize the accumulated deltas into an ``AIMessage``."""


@runtime_checkable
class StreamingLLMProvider(Protocol):
    """A provider that can yield :class:`LLMDelta` chunks. The router
    drives its idle-timeout over this; providers without it use the
    legacy single-deadline ``complete()`` path."""

    def stream(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
    ) -> AsyncIterator[LLMDelta]:
        """Yield normalized :class:`LLMDelta` chunks from the provider."""

    def new_stream_assembler(self) -> StreamAssembler:
        """A fresh assembler for one stream drain (provider-specific)."""


def supports_streaming(provider: object) -> bool:
    """Whether the innermost provider can stream.

    Unwraps admission/wrapper layers that expose ``.inner`` (e.g.
    :class:`RateLimitedProvider`) so the check reflects the real adapter,
    not the wrapper. Avoids importing ``RateLimitedProvider`` here (that
    module imports the router) — a duck-typed ``.inner`` walk keeps this
    module dependency-light.
    """
    seen: set[int] = set()
    p: Any = provider
    while hasattr(p, "inner") and id(p) not in seen:
        seen.add(id(p))
        p = p.inner
    return isinstance(p, StreamingLLMProvider)
