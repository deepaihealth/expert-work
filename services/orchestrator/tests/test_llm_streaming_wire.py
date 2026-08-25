from collections.abc import AsyncIterator
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from orchestrator.llm.providers._streaming import (
    LLMDelta,
    OpenAIStreamAssembler,
    StreamingLLMProvider,
    ToolCallChunk,
    delta_from_openai_chunk,
    supports_streaming,
)
from orchestrator.llm.providers.openai import _from_openai_response


def _chunk(delta: dict[str, Any], *, finish: str | None = None, **top: Any) -> dict[str, Any]:
    return {"choices": [{"delta": delta, "finish_reason": finish}], **top}


def _tc(
    *,
    index: int | None = None,
    id: str | None = None,
    name: str | None = None,
    args: str = "",
) -> dict[str, Any]:
    """One vendor ``delta.tool_calls[]`` entry.

    ``index=None`` OMITS the key entirely — the shape a vendor would put on
    the wire if it did not number its tool-call fragments. We have never
    captured such traffic; these are the defensive-path fixtures.
    """
    entry: dict[str, Any] = {}
    if index is not None:
        entry["index"] = index
    if id is not None:
        entry["id"] = id
    fn: dict[str, Any] = {}
    if name is not None:
        fn["name"] = name
    if args:
        fn["arguments"] = args
    entry["function"] = fn
    return entry


def _feed(asm: OpenAIStreamAssembler, *entries: dict[str, Any], finish: str | None = None) -> None:
    """Push ONE SSE chunk carrying ``entries`` into the assembler."""
    asm.add(delta_from_openai_chunk(_chunk({"tool_calls": list(entries)}, finish=finish)))


def _summary(msg: AIMessage) -> list[tuple[str, str, dict[str, Any]]]:
    return [(tc["id"], tc["name"], tc["args"]) for tc in msg.tool_calls]


def test_delta_content_and_progress() -> None:
    d = delta_from_openai_chunk(_chunk({"content": "Hel"}))
    assert d.content == "Hel"
    assert d.has_progress is True


def test_delta_role_only_is_not_progress() -> None:
    d = delta_from_openai_chunk(_chunk({"role": "assistant"}))
    assert d.content == ""
    assert d.reasoning == ""
    assert d.tool_calls == ()
    assert d.has_progress is False


def test_delta_reasoning_is_progress() -> None:
    d = delta_from_openai_chunk(_chunk({"reasoning_content": "thinking"}))
    assert d.reasoning == "thinking"
    assert d.has_progress is True


def test_delta_tool_call_fragment() -> None:
    d = delta_from_openai_chunk(
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "search", "arguments": '{"q":'},
                    }
                ]
            }
        )
    )
    expected_chunk = ToolCallChunk(index=0, id="call_1", name="search", args_fragment='{"q":')
    assert d.tool_calls == (expected_chunk,)
    assert d.has_progress is True


def test_delta_final_chunk_usage_and_finish() -> None:
    d = delta_from_openai_chunk(
        _chunk(
            {},
            finish="stop",
            usage={"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
            model="glm-5.2",
            system_fingerprint="fp_1",
        )
    )
    assert d.finish_reason == "stop"
    assert d.usage == {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}
    assert d.model == "glm-5.2"
    assert d.system_fingerprint == "fp_1"
    assert d.has_progress is False


def test_assembler_text_matches_non_streaming_decoder() -> None:
    # The regression guarantee: the same content assembled from deltas must
    # byte-equal the AIMessage the non-streaming decoder produces.
    body = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Hello world",
                    "reasoning_content": "let me think",
                },
                "finish_reason": "stop",
            }
        ],
        "model": "glm-5.2",
        "system_fingerprint": "fp_1",
        "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
    }
    expected = _from_openai_response(body)

    asm = OpenAIStreamAssembler()
    asm.add(delta_from_openai_chunk(_chunk({"role": "assistant"})))
    asm.add(delta_from_openai_chunk(_chunk({"reasoning_content": "let me think"})))
    asm.add(delta_from_openai_chunk(_chunk({"content": "Hello "})))
    asm.add(delta_from_openai_chunk(_chunk({"content": "world"})))
    asm.add(
        delta_from_openai_chunk(
            _chunk(
                {},
                finish="stop",
                model="glm-5.2",
                system_fingerprint="fp_1",
                usage={"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
            )
        )
    )
    got = asm.build()

    assert got.content == expected.content
    assert got.additional_kwargs == expected.additional_kwargs
    assert got.response_metadata == expected.response_metadata
    assert got.usage_metadata == expected.usage_metadata
    assert got.tool_calls == expected.tool_calls


def test_assembler_reassembles_tool_call_fragments() -> None:
    body = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "search", "arguments": '{"q": "hi"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    expected = _from_openai_response(body)

    asm = OpenAIStreamAssembler()
    asm.add(
        delta_from_openai_chunk(
            _chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "function": {"name": "search", "arguments": '{"q": '},
                        }
                    ]
                }
            )
        )
    )
    asm.add(
        delta_from_openai_chunk(
            _chunk(
                {"tool_calls": [{"index": 0, "function": {"arguments": '"hi"}'}}]},
                finish="tool_calls",
            )
        )
    )
    got = asm.build()
    assert got.tool_calls == expected.tool_calls


def test_assembler_interrupted_drops_incomplete_tool_call() -> None:
    # A tool-args fragment that never completed valid JSON must not become a
    # dispatchable tool call when the stream is interrupted mid-args.
    asm = OpenAIStreamAssembler()
    asm.add(delta_from_openai_chunk(_chunk({"content": "partial answer"})))
    asm.add(
        delta_from_openai_chunk(
            _chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "function": {"name": "search", "arguments": '{"q": '},
                        }
                    ]
                }
            )
        )
    )
    got = asm.build(interrupted=True)
    assert got.content == "partial answer"
    assert got.tool_calls == []
    assert got.response_metadata.get("finish_reason") == "stream_idle_timeout"


# --- missing ``index`` on tool-call fragments -------------------------------
# A vendor that omits ``delta.tool_calls[].index`` used to have every fragment
# folded onto slot 0, silently MERGING distinct calls into one (the first call
# disappeared and the concatenated arguments usually failed to parse). The
# assembler now opens a new slot instead. No user-visible error was ever
# raised for this, so these are the only guards.


def test_assembler_unindexed_tool_calls_do_not_collapse() -> None:
    asm = OpenAIStreamAssembler()
    _feed(asm, _tc(id="c0", name="search", args='{"q": "hi"}'))
    _feed(asm, _tc(id="c1", name="calc", args='{"n": 2}'), finish="tool_calls")
    got = asm.build()

    # Count first: "the two did not merge" is also true of an assembler that
    # dropped both, so the count has to be pinned before the contents.
    assert len(got.tool_calls) == 2
    assert _summary(got) == [
        ("c0", "search", {"q": "hi"}),
        ("c1", "calc", {"n": 2}),
    ]


def test_assembler_unindexed_tool_calls_in_one_chunk_do_not_collapse() -> None:
    # Same wire fact, batched shape: both calls in a single chunk.
    asm = OpenAIStreamAssembler()
    _feed(
        asm,
        _tc(id="c0", name="search", args='{"q": "hi"}'),
        _tc(id="c1", name="calc", args='{"n": 2}'),
        finish="tool_calls",
    )
    got = asm.build()

    assert len(got.tool_calls) == 2
    assert _summary(got) == [
        ("c0", "search", {"q": "hi"}),
        ("c1", "calc", {"n": 2}),
    ]


def test_assembler_unindexed_arg_fragments_continue_the_open_call() -> None:
    # Arguments-only fragments carry no id/name, so they must CONTINUE the
    # call opened by the last fragment — not open a call of their own.
    asm = OpenAIStreamAssembler()
    _feed(asm, _tc(id="c0", name="search", args='{"q": '))
    _feed(asm, _tc(args='"hi"'))
    _feed(asm, _tc(args="}"), finish="tool_calls")
    got = asm.build()

    assert len(got.tool_calls) == 1
    assert _summary(got) == [("c0", "search", {"q": "hi"})]


def test_assembler_mixed_indexed_then_unindexed_tool_calls() -> None:
    # An indexed slot and an unindexed one must never share a slot key.
    asm = OpenAIStreamAssembler()
    _feed(asm, _tc(index=0, id="c0", name="search", args='{"q": "hi"}'))
    _feed(asm, _tc(id="c1", name="calc", args='{"n": 2}'), finish="tool_calls")
    got = asm.build()

    assert len(got.tool_calls) == 2
    assert _summary(got) == [
        ("c0", "search", {"q": "hi"}),
        ("c1", "calc", {"n": 2}),
    ]


def test_assembler_index_on_head_fragment_only() -> None:
    # A vendor that numbers only the fragment carrying id/name: the untagged
    # tails must land on their own call, not on slot 0 or on a fresh slot.
    asm = OpenAIStreamAssembler()
    _feed(asm, _tc(index=0, id="c0", name="search", args='{"q": '))
    _feed(asm, _tc(args='"hi"}'))
    _feed(asm, _tc(index=1, id="c1", name="calc", args='{"n": '))
    _feed(asm, _tc(args="2}"), finish="tool_calls")
    got = asm.build()

    assert len(got.tool_calls) == 2
    assert _summary(got) == [
        ("c0", "search", {"q": "hi"}),
        ("c1", "calc", {"n": 2}),
    ]


# --- regression net: the indexed path (the shape the wire spec mandates) ----


@pytest.mark.parametrize(
    ("first_index", "second_index"),
    [
        (0, 1),  # ascending — the ordinary shape
        (1, 0),  # non-ascending — emission order is first-seen, not sorted
        (0, 7),  # sparse — gaps are not slots
    ],
    ids=["ascending", "non-ascending", "sparse"],
)
def test_assembler_indexed_tool_calls_unchanged(first_index: int, second_index: int) -> None:
    asm = OpenAIStreamAssembler()
    _feed(asm, _tc(index=first_index, id="c0", name="search", args='{"q": "hi"}'))
    _feed(asm, _tc(index=second_index, id="c1", name="calc", args='{"n": 2}'), finish="tool_calls")
    got = asm.build()

    assert len(got.tool_calls) == 2
    assert _summary(got) == [
        ("c0", "search", {"q": "hi"}),
        ("c1", "calc", {"n": 2}),
    ]


def test_assembler_indexed_fragments_interleave_by_index() -> None:
    # The load-bearing property of the indexed path: fragments route by index
    # even when two calls stream interleaved.
    asm = OpenAIStreamAssembler()
    _feed(asm, _tc(index=0, id="c0", name="search", args='{"q": '))
    _feed(asm, _tc(index=1, id="c1", name="calc", args='{"n": '))
    _feed(asm, _tc(index=0, args='"hi"}'))
    _feed(asm, _tc(index=1, args="2}"), finish="tool_calls")
    got = asm.build()

    assert len(got.tool_calls) == 2
    assert _summary(got) == [
        ("c0", "search", {"q": "hi"}),
        ("c1", "calc", {"n": 2}),
    ]


def test_delta_tool_call_index_presence_is_recorded() -> None:
    with_index = delta_from_openai_chunk(_chunk({"tool_calls": [_tc(index=0, id="c0")]}))
    assert with_index.tool_calls == (ToolCallChunk(index=0, id="c0"),)
    assert with_index.tool_calls[0].index_missing is False

    without = delta_from_openai_chunk(_chunk({"tool_calls": [_tc(id="c0")]}))
    assert without.tool_calls[0].index_missing is True
    # ``index`` keeps its 0 fallback — TokenSink keys a dedup map on it and is
    # deliberately outside this fix.
    assert without.tool_calls[0].index == 0


def test_supports_streaming_true_for_streaming_provider() -> None:
    class _Streamer:
        async def stream(self, **_: Any) -> AsyncIterator[LLMDelta]:
            if False:
                yield LLMDelta()

        def new_stream_assembler(self) -> OpenAIStreamAssembler:
            return OpenAIStreamAssembler()

    class _Wrapper:  # mimics RateLimitedProvider.inner unwrapping
        def __init__(self, inner: Any) -> None:
            self.inner = inner

    assert isinstance(_Streamer(), StreamingLLMProvider)
    assert supports_streaming(_Streamer()) is True
    assert supports_streaming(_Wrapper(_Streamer())) is True


def test_supports_streaming_false_for_plain_provider() -> None:
    class _Plain:
        async def complete(self, **_: Any) -> AIMessage:
            return AIMessage(content="")

    assert supports_streaming(_Plain()) is False


def test_supports_streaming_safe_on_self_referential_inner() -> None:
    class _SelfRef:
        def __init__(self) -> None:
            self.inner: Any = self  # points at itself

    assert supports_streaming(_SelfRef()) is False  # terminates, no hang, no stream()


def test_delta_missing_choices_is_empty_no_progress() -> None:
    d = delta_from_openai_chunk({})
    assert d.content == ""
    assert d.reasoning == ""
    assert d.tool_calls == ()
    assert d.finish_reason is None
    assert d.has_progress is False
