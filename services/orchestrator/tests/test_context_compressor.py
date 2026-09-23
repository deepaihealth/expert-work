"""Stream L.L2 — :class:`ContextCompressor` unit tests.

Pins the conflict-free invariants: head + tail messages survive the
compression, the summary lands as a SystemMessage between them, the
threshold gate fires only when estimated tokens cross
``context_window * threshold_pct``, and an unsummarisable overflow
raises :class:`ContextOverflowError`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from expert_work.common.conversation_channel import HIDE_FROM_UI
from orchestrator.context import (
    CompactionStats,
    ContextCompressor,
    ContextOverflowError,
    estimate_tokens,
    floor_head_keep_for_injection,
)
from orchestrator.context.compressor import (
    _SUMMARY_INPUT_CHAR_BUDGET,
    _SUMMARY_PER_MESSAGE_CHAR_CAP,
    _bound_text,
    _format_middle_for_summary,
)
from orchestrator.tools.registry import ToolSpec


@dataclass
class _ScriptedSummariser:
    """Records every summariser call and returns a deterministic body."""

    summary_text: str = "- bullet one\n- bullet two"
    calls: int = 0

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
    ) -> AIMessage:
        del messages, tools
        self.calls += 1
        return AIMessage(content=self.summary_text)


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------


def test_estimate_tokens_chars_div_four() -> None:
    """The token estimator divides total chars by 4 — the rule of
    thumb Hermes uses. Cheap, no dependency, conservative."""
    msgs = [HumanMessage(content="x" * 40)]
    assert estimate_tokens(msgs) == 10


def test_estimate_tokens_sums_across_messages() -> None:
    msgs = [
        SystemMessage(content="abcd"),  # 4 chars
        HumanMessage(content="efghij"),  # 6 chars
        AIMessage(content="klmnopqrst"),  # 10 chars
    ]
    # 20 chars / 4 = 5 tokens
    assert estimate_tokens(msgs) == 5


def test_estimate_tokens_flattens_content_block_list() -> None:
    """J.6 multimodal / L1 cache_control wrappers carry content as a
    block list; the estimator concatenates each block's ``text``."""
    msg = HumanMessage(
        content=[
            {"type": "text", "text": "first"},  # 5 chars
            {"type": "text", "text": " second"},  # 7 chars
        ]
    )
    assert estimate_tokens([msg]) == 3  # 12 chars // 4


def test_estimate_tokens_counts_non_text_blocks_via_repr() -> None:
    """Image / tool_use blocks contribute their stringified form so
    they still count toward the estimate (downstream payload size)."""
    msg = HumanMessage(
        content=[
            {"type": "text", "text": "hi"},
            {"type": "image", "source": {"data": "BASE64DATA"}},
        ]
    )
    # 2 chars + stringified image dict — exact value is implementation
    # dependent, but it must be more than just the text length.
    assert estimate_tokens([msg]) > estimate_tokens([HumanMessage(content="hi")])


# ---------------------------------------------------------------------------
# should_compress threshold gate
# ---------------------------------------------------------------------------


def test_should_compress_returns_true_at_threshold() -> None:
    """The gate uses ``>=`` so a prompt sized exactly at the threshold
    counts as needing compression — the upstream is more authoritative
    about the actual token count, so we lean conservative."""
    compressor = ContextCompressor(
        llm_caller=_ScriptedSummariser(),
        context_window=100,
        threshold_pct=0.5,
    )
    # 50 / 100 = 0.5 → at threshold (200 chars / 4 = 50 tokens).
    msgs = [HumanMessage(content="x" * 200)]
    assert compressor.should_compress(msgs) is True


def test_should_compress_returns_false_below_threshold() -> None:
    compressor = ContextCompressor(
        llm_caller=_ScriptedSummariser(),
        context_window=100,
        threshold_pct=0.5,
    )
    # 196 chars / 4 = 49 tokens → below 50.
    msgs = [HumanMessage(content="x" * 196)]
    assert compressor.should_compress(msgs) is False


# ---------------------------------------------------------------------------
# compress() one-pass behaviour
# ---------------------------------------------------------------------------


def _conversation(
    *, head: int, middle: int, tail: int, char_per_msg: int = 40
) -> list[BaseMessage]:
    """Build a flat conversation of HumanMessages — count controls the
    estimator's output without other content variance."""
    msgs: list[BaseMessage] = []
    for i in range(head + middle + tail):
        msgs.append(HumanMessage(content=f"msg-{i}-" + ("x" * (char_per_msg - 6))))
    return msgs


@pytest.mark.asyncio
async def test_compress_preserves_head_and_tail() -> None:
    """A successful pass keeps the first ``head_keep`` and last
    ``tail_keep`` messages intact; the middle is collapsed."""
    summariser = _ScriptedSummariser()
    compressor = ContextCompressor(
        llm_caller=summariser,
        context_window=280,  # headroom for the CM-7 reference-only preamble
        threshold_pct=0.5,
        head_keep=2,
        tail_keep=2,
    )
    # 20 messages x 80 chars = 1600 chars / 4 = 400 tokens; threshold
    # 140. After collapsing 16 middle messages into one summary the
    # estimate drops well under the threshold.
    msgs = _conversation(head=2, middle=16, tail=2, char_per_msg=80)
    out = await compressor.compress(msgs)

    # Head messages identical to original.
    assert out[0] is msgs[0]
    assert out[1] is msgs[1]
    # Tail messages identical to original.
    assert out[-1] is msgs[-1]
    assert out[-2] is msgs[-2]
    # One summary SystemMessage in between.
    assert isinstance(out[2], SystemMessage)
    assert "<context-summary>" in str(out[2].content)
    assert "bullet one" in str(out[2].content)
    assert summariser.calls == 1


@pytest.mark.asyncio
async def test_compress_preserves_leading_system_message_byte_stable() -> None:
    """Mini-ADR L-1: a leading SystemMessage stays out of the
    compression — head/tail accounting works on the non-system
    suffix. L1's byte-stable invariant survives compression."""
    summariser = _ScriptedSummariser()
    compressor = ContextCompressor(
        llm_caller=summariser,
        context_window=280,  # headroom for the CM-7 reference-only preamble
        threshold_pct=0.5,
        head_keep=1,
        tail_keep=1,
    )
    system_msg = SystemMessage(content="you are an editor")
    body = _conversation(head=1, middle=10, tail=1, char_per_msg=80)
    out = await compressor.compress([system_msg, *body])

    # First message is the SAME SystemMessage instance — never rewritten.
    assert out[0] is system_msg
    # Then the body's head (1 msg), summary, tail (1 msg).
    assert out[1] is body[0]
    assert isinstance(out[2], SystemMessage)
    assert "<context-summary>" in str(out[2].content)
    assert out[-1] is body[-1]


@pytest.mark.asyncio
async def test_compress_skipped_below_threshold() -> None:
    """When the input already fits under the threshold the compressor
    returns the original list verbatim and never invokes the
    summariser."""
    summariser = _ScriptedSummariser()
    compressor = ContextCompressor(
        llm_caller=summariser,
        context_window=1000,
        threshold_pct=0.9,
    )
    msgs = [HumanMessage(content="short")]  # well under threshold
    out = await compressor.compress(msgs)
    assert out == msgs
    assert summariser.calls == 0


@pytest.mark.asyncio
async def test_compress_summary_lands_between_head_and_tail() -> None:
    """The summary's position matters — head messages first, summary
    next, tail messages last. The order anchor lets the model treat
    the summary as a checkpoint."""
    summariser = _ScriptedSummariser(summary_text="- compressed bullet")
    compressor = ContextCompressor(
        llm_caller=summariser,
        # 600 tokens window x 0.5 = 300 tokens threshold. Head + tail
        # (5 msgs x 20 tok = 100 tok) leaves room for a summary
        # well under threshold.
        context_window=600,
        threshold_pct=0.5,
        head_keep=3,
        tail_keep=2,
    )
    msgs = _conversation(head=3, middle=10, tail=2, char_per_msg=80)
    out = await compressor.compress(msgs)

    # Sequence: 3 head + 1 summary + 2 tail = 6 items.
    assert len(out) == 6
    assert all(isinstance(m, HumanMessage) for m in out[:3])
    assert isinstance(out[3], SystemMessage)
    assert "compressed bullet" in str(out[3].content)
    assert all(isinstance(m, HumanMessage) for m in out[-2:])


# ---------------------------------------------------------------------------
# Overflow + max_passes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compress_raises_when_no_middle_to_summarise() -> None:
    """``head_keep + tail_keep`` covering the whole non-system slice
    leaves no middle to summarise — surfacing as overflow tells the
    operator the only knobs left are manifest-level."""

    @dataclass
    class _NeverCalled:
        async def __call__(
            self,
            *,
            messages: Sequence[BaseMessage],
            tools: Sequence[ToolSpec],
        ) -> AIMessage:
            del messages, tools
            msg = "summariser must not be invoked"
            raise AssertionError(msg)

    compressor = ContextCompressor(
        llm_caller=_NeverCalled(),
        context_window=100,
        threshold_pct=0.1,  # very low → forces compression attempt
        head_keep=5,
        tail_keep=5,
    )
    # 5 + 5 = 10; supply exactly 10 messages → no middle.
    msgs = _conversation(head=5, middle=0, tail=5, char_per_msg=200)
    with pytest.raises(ContextOverflowError) as exc_info:
        await compressor.compress(msgs)
    assert exc_info.value.passes == 0


@pytest.mark.asyncio
async def test_compress_raises_after_max_passes_when_summary_too_large() -> None:
    """A pathological summariser that itself returns a giant payload
    can't bring the estimate below threshold — after ``max_passes``
    attempts the compressor raises rather than looping forever."""

    @dataclass
    class _BloatedSummariser:
        big_content: str
        calls: int = 0

        async def __call__(
            self,
            *,
            messages: Sequence[BaseMessage],
            tools: Sequence[ToolSpec],
        ) -> AIMessage:
            del messages, tools
            self.calls += 1
            return AIMessage(content=self.big_content)

    # Summary is itself 1000 chars → 250 tokens. Threshold 50.
    summariser = _BloatedSummariser(big_content="x" * 1000)
    compressor = ContextCompressor(
        llm_caller=summariser,
        context_window=200,
        threshold_pct=0.25,  # threshold = 50 tokens
        head_keep=1,
        tail_keep=1,
        max_passes=2,
    )
    msgs = _conversation(head=1, middle=10, tail=1, char_per_msg=80)
    with pytest.raises(ContextOverflowError):
        await compressor.compress(msgs)
    # B-64 Task 7 回修第 4 轮 —— 第 2 遍的中段只剩第 1 遍的摘要,没有新东西可总结,
    # 按「中段已空」抛,不再用空的 NEW EVENTS 再调一次「更新」。原来这里断言的
    # ``calls == 2`` / ``passes == 2`` 钉的正是那次空更新。
    assert summariser.calls == 1


@pytest.mark.asyncio
async def test_compress_uses_minimum_one_pass() -> None:
    """``max_passes=1`` configuration: compressor runs exactly one
    pass and either returns it or raises."""
    summariser = _ScriptedSummariser(summary_text="- short summary")
    compressor = ContextCompressor(
        llm_caller=summariser,
        context_window=200,
        threshold_pct=0.5,
        head_keep=1,
        tail_keep=1,
        max_passes=1,
    )
    msgs = _conversation(head=1, middle=10, tail=1, char_per_msg=80)
    out = await compressor.compress(msgs)
    assert summariser.calls == 1
    # The middle is collapsed into a single SystemMessage.
    assert isinstance(out[1], SystemMessage)


# ---------------------------------------------------------------------------
# RT-ADR-6 — transient summariser failure skips the round; only three
# consecutive failed rounds escalate to ContextOverflowError
# ---------------------------------------------------------------------------


@dataclass
class _FlakySummariser:
    """Follows ``script`` per call — ``False`` raises, ``True`` returns a
    summary. Calls beyond the script succeed."""

    script: list[bool] = field(default_factory=list)
    calls: int = 0

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
    ) -> AIMessage:
        del messages, tools
        idx = self.calls
        self.calls += 1
        if idx < len(self.script) and not self.script[idx]:
            msg = "summariser upstream down"
            raise RuntimeError(msg)
        return AIMessage(content="- recovered summary")


def _flaky_compressor(summariser: _FlakySummariser) -> ContextCompressor:
    return ContextCompressor(
        llm_caller=summariser,
        context_window=200,
        threshold_pct=0.5,
        head_keep=1,
        tail_keep=1,
    )


@pytest.mark.asyncio
async def test_transient_summariser_failure_skips_round() -> None:
    """RT-ADR-6 — one summariser failure no longer fails the run: the
    round returns the messages uncompressed and the next turn retries."""
    summariser = _FlakySummariser(script=[False])
    compressor = _flaky_compressor(summariser)
    msgs = _conversation(head=1, middle=10, tail=1, char_per_msg=80)

    out = await compressor.compress(msgs, streak_key="thread-a")

    assert out == msgs  # unchanged — compression skipped, not failed
    assert summariser.calls == 1  # no retry within the same round


@pytest.mark.asyncio
async def test_three_consecutive_summariser_failures_raise_overflow() -> None:
    """RT-ADR-6 — the fail-hard backstop survives: a persistently
    broken summariser escalates on the third consecutive failed round
    of the SAME conversation instead of silently ballooning forever."""
    summariser = _FlakySummariser(script=[False, False, False])
    compressor = _flaky_compressor(summariser)
    msgs = _conversation(head=1, middle=10, tail=1, char_per_msg=80)

    # streak 1 / 2 — skipped
    assert await compressor.compress(msgs, streak_key="thread-a") == msgs
    assert await compressor.compress(msgs, streak_key="thread-a") == msgs
    with pytest.raises(ContextOverflowError):  # streak 3 — fail-hard
        await compressor.compress(msgs, streak_key="thread-a")


@pytest.mark.asyncio
async def test_summariser_success_resets_failure_streak() -> None:
    """A successful pass between failures resets the conversation's
    consecutive-failure count — only an unbroken streak escalates."""
    summariser = _FlakySummariser(script=[False, False, True, False, False])
    compressor = _flaky_compressor(summariser)
    msgs = _conversation(head=1, middle=10, tail=1, char_per_msg=80)

    await compressor.compress(msgs, streak_key="thread-a")  # fail — streak 1
    await compressor.compress(msgs, streak_key="thread-a")  # fail — streak 2
    await compressor.compress(msgs, streak_key="thread-a")  # success — bucket cleared
    # Two more failures only rebuild the streak to 2 — no overflow.
    assert await compressor.compress(msgs, streak_key="thread-a") == msgs
    assert await compressor.compress(msgs, streak_key="thread-a") == msgs


@pytest.mark.asyncio
async def test_failure_streaks_isolated_per_key() -> None:
    """The compressor instance is shared per (tenant, agent, version)
    across conversations — conversation A's two failures must not
    escalate conversation B's first wobble into a false overflow."""
    summariser = _FlakySummariser(script=[False, False, False, False])
    compressor = _flaky_compressor(summariser)
    msgs = _conversation(head=1, middle=10, tail=1, char_per_msg=80)

    # Conversation A fails twice — streak 2, still skipping.
    assert await compressor.compress(msgs, streak_key="thread-a") == msgs
    assert await compressor.compress(msgs, streak_key="thread-a") == msgs
    # Conversation B's FIRST failure: with a shared counter this would
    # be the third consecutive failure and raise — it must skip.
    assert await compressor.compress(msgs, streak_key="thread-b") == msgs
    # And A's streak is likewise untouched by B: its third failure raises.
    with pytest.raises(ContextOverflowError):
        await compressor.compress(msgs, streak_key="thread-a")


@pytest.mark.asyncio
async def test_none_streak_key_skips_without_escalation() -> None:
    """Without a conversation identity the compressor cannot count
    consecutive failures safely on the shared instance — every
    transient failure keeps the skip-once semantics and never
    escalates (empty-middle / max_passes fail-hard still apply)."""
    summariser = _FlakySummariser(script=[False] * 5)
    compressor = _flaky_compressor(summariser)
    msgs = _conversation(head=1, middle=10, tail=1, char_per_msg=80)

    for _ in range(5):
        assert await compressor.compress(msgs) == msgs


def test_failure_streak_map_bounded() -> None:
    """The per-conversation map is capped — the oldest-inserted entry
    is evicted once ``_MAX_STREAK_KEYS`` distinct keys accumulate."""
    from orchestrator.context.compressor import _MAX_STREAK_KEYS, _FailureStreaks

    streaks = _FailureStreaks()
    for i in range(_MAX_STREAK_KEYS):
        streaks.bump(f"t-{i}")
    assert len(streaks.counts) == _MAX_STREAK_KEYS

    streaks.bump("overflow")

    assert len(streaks.counts) == _MAX_STREAK_KEYS
    assert "t-0" not in streaks.counts  # oldest evicted
    assert streaks.counts["overflow"] == 1


# ---------------------------------------------------------------------------
# RT-ADR-10 — summariser prompt budget hardening
# ---------------------------------------------------------------------------


def test_bound_text_short_input_unchanged() -> None:
    assert _bound_text("short", 100) == "short"


def test_bound_text_keeps_head_two_thirds_and_tail() -> None:
    """deer-flow #3887 ``_bound_text`` shape: head gets 2/3 of the
    budget, the tail the remainder, with a visible elision marker."""
    text = "H" * 200 + "T" * 100
    out = _bound_text(text, 30)
    head = (30 * 2) // 3
    assert out.startswith("H" * head)
    assert out.endswith("T" * (30 - head))
    assert "truncated" in out


def test_format_middle_caps_single_oversized_message() -> None:
    """A single 3x-over-cap tool dump cannot monopolise the transcript."""
    big = "x" * (_SUMMARY_PER_MESSAGE_CHAR_CAP * 3)
    out = _format_middle_for_summary([HumanMessage(content=big)])
    # Small slack for the role prefix + elision marker.
    assert len(out) <= _SUMMARY_PER_MESSAGE_CHAR_CAP + 100
    assert "truncated" in out


def test_format_middle_skips_hidden_scaffolding() -> None:
    """B-67 —— 隐藏 HumanMessage(「本轮输入」段、恢复建议)不进摘要输入:摘要是持久文本,
    平台脚手架不该被写进去。"""
    out = _format_middle_for_summary(
        [
            HumanMessage(content="user ask"),
            HumanMessage(content="PLATFORM-INPUTS-BLOCK", additional_kwargs={HIDE_FROM_UI: True}),
            AIMessage(content="reply"),
        ]
    )
    assert out == "user: user ask\n\nassistant: reply"


def test_format_middle_enforces_total_budget() -> None:
    """Many under-cap messages still cannot exceed the total budget."""
    msgs = [HumanMessage(content=f"m{i}-" + "y" * 1500) for i in range(30)]  # ~45k chars
    out = _format_middle_for_summary(msgs)
    assert len(out) <= _SUMMARY_INPUT_CHAR_BUDGET + 100
    assert "truncated" in out


@pytest.mark.asyncio
async def test_update_mode_splits_budget_between_prior_and_events() -> None:
    """Update mode halves the input budget: PREVIOUS SUMMARY and NEW
    EVENTS each get at most half, so neither side can starve the other."""
    summariser = _RecordingSummariser()
    half = _SUMMARY_INPUT_CHAR_BUDGET // 2
    giant_prior = "<context-summary>\n" + "p" * (half * 3) + "\n</context-summary>"
    msgs: list[BaseMessage] = [
        HumanMessage(content="head-1"),
        HumanMessage(content="head-2"),
        SystemMessage(content=giant_prior),
        *_conversation(head=0, middle=14, tail=0, char_per_msg=3000),
        HumanMessage(content="tail-1"),
        HumanMessage(content="tail-2"),
    ]
    await _compressor(summariser).compress(msgs)

    user = str(summariser.prompts[0][1].content)
    prior_section, events_section = user.split("NEW EVENTS:")
    assert len(prior_section) <= half + 200
    assert len(events_section) <= half + 200
    assert "truncated" in prior_section
    assert "truncated" in events_section


# ---------------------------------------------------------------------------
# Tool / assistant mix preserved in the head/tail slices
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compress_keeps_tool_messages_in_tail_window() -> None:
    """A typical multi-step ReAct conversation has ToolMessage entries
    interleaved with AIMessage / HumanMessage. The compressor counts
    by message position, not role, so ToolMessages in the tail window
    survive verbatim — the agent's most-recent reasoning chain stays
    intact."""
    summariser = _ScriptedSummariser()
    compressor = ContextCompressor(
        llm_caller=summariser,
        context_window=200,
        threshold_pct=0.5,
        head_keep=1,
        tail_keep=3,
    )
    head_msg = HumanMessage(content="start")
    middle = [AIMessage(content="thinking " + ("x" * 80)) for _ in range(8)]  # plenty of middle
    tail_ai = AIMessage(
        content="",
        tool_calls=[
            {"name": "search", "args": {"q": "x"}, "id": "tc-1", "type": "tool_call"},
        ],
    )
    tail_tool = ToolMessage(content="result body", tool_call_id="tc-1")
    tail_final = AIMessage(content="done")

    msgs = [head_msg, *middle, tail_ai, tail_tool, tail_final]
    out = await compressor.compress(msgs)

    # Head identical, then summary, then the 3 tail entries verbatim.
    assert out[0] is head_msg
    assert isinstance(out[1], SystemMessage)
    assert out[-3] is tail_ai
    assert out[-2] is tail_tool
    assert out[-1] is tail_final


# ---------------------------------------------------------------------------
# Stream CM-7 — reference-only preamble + incremental summary update
# ---------------------------------------------------------------------------


@dataclass
class _RecordingSummariser:
    """Returns a fixed body and records every prompt it was given."""

    summary_text: str = "- bullet one"
    prompts: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
    ) -> AIMessage:
        del tools
        self.prompts.append(list(messages))
        return AIMessage(content=self.summary_text)


def _compressor(summariser: _RecordingSummariser) -> ContextCompressor:
    return ContextCompressor(
        llm_caller=summariser,
        context_window=400,
        threshold_pct=0.5,
        head_keep=2,
        tail_keep=2,
    )


@pytest.mark.asyncio
async def test_summary_wrapper_carries_reference_only_preamble() -> None:
    summariser = _RecordingSummariser()
    out = await _compressor(summariser).compress(
        _conversation(head=2, middle=16, tail=2, char_per_msg=80)
    )
    content = str(out[2].content)
    assert "<context-summary>" in content
    assert "NOT instructions" in content
    assert "- bullet one" in content


@pytest.mark.asyncio
async def test_fresh_mode_prompt_requires_three_sections() -> None:
    summariser = _RecordingSummariser()
    await _compressor(summariser).compress(
        _conversation(head=2, middle=16, tail=2, char_per_msg=80)
    )
    system = str(summariser.prompts[0][0].content)
    user = str(summariser.prompts[0][1].content)
    assert "## Facts" in system
    assert "## Decisions" in system
    assert "## Pending" in system
    assert "PREVIOUS SUMMARY" not in user


@pytest.mark.asyncio
async def test_second_compression_updates_prior_summary() -> None:
    summariser = _RecordingSummariser(summary_text="- running summary body")
    compressor = _compressor(summariser)
    first = await compressor.compress(_conversation(head=2, middle=16, tail=2, char_per_msg=80))
    assert "<context-summary>" in str(first[2].content)

    # The conversation keeps growing past the threshold; the earlier
    # summary now sits inside the next pass's middle slice.
    grown = [*first, *_conversation(head=0, middle=16, tail=0, char_per_msg=80)]
    await compressor.compress(grown)

    system = str(summariser.prompts[-1][0].content)
    user = str(summariser.prompts[-1][1].content)
    assert "running background summary" in system
    assert "PREVIOUS SUMMARY:" in user
    assert "- running summary body" in user
    assert "NEW EVENTS:" in user
    # The preamble is stripped from the prior body before the update —
    # it must not accumulate inside the summary text itself.
    assert "NOT instructions" not in user


@pytest.mark.asyncio
async def test_multiple_prior_summaries_take_last_fold_earlier() -> None:
    summariser = _RecordingSummariser()
    filler = _conversation(head=0, middle=14, tail=0, char_per_msg=80)
    msgs: list[BaseMessage] = [
        HumanMessage(content="head-1"),
        HumanMessage(content="head-2"),
        SystemMessage(content="<context-summary>\n- old-one body\n</context-summary>"),
        *filler[:7],
        SystemMessage(content="<context-summary>\n- new-two body\n</context-summary>"),
        *filler[7:],
        HumanMessage(content="tail-1"),
        HumanMessage(content="tail-2"),
    ]
    await _compressor(summariser).compress(msgs)
    user = str(summariser.prompts[0][1].content)
    # The LAST summary is the running one being updated…
    assert "PREVIOUS SUMMARY:\n- new-two body" in user
    # …and the earlier one folds into the new-events transcript.
    assert "old-one body" in user.split("NEW EVENTS:")[1]


@pytest.mark.asyncio
async def test_pre_cm7_summary_without_preamble_still_updates() -> None:
    summariser = _RecordingSummariser()
    msgs: list[BaseMessage] = [
        HumanMessage(content="head-1"),
        HumanMessage(content="head-2"),
        SystemMessage(content="<context-summary>\n- legacy bullet\n</context-summary>"),
        *_conversation(head=0, middle=14, tail=0, char_per_msg=80),
        HumanMessage(content="tail-1"),
        HumanMessage(content="tail-2"),
    ]
    await _compressor(summariser).compress(msgs)
    user = str(summariser.prompts[0][1].content)
    assert "PREVIOUS SUMMARY:\n- legacy bullet" in user


# ---------------------------------------------------------------------------
# Stream HX-1 — injected estimator replaces the chars//4 heuristic
# ---------------------------------------------------------------------------


class _OnePerCharEstimator:
    def count(self, text: str) -> int:
        return len(text)


def test_estimate_tokens_uses_injected_estimator() -> None:
    msgs = [HumanMessage(content="abcdefgh")]
    assert estimate_tokens(msgs) == 2  # legacy: 8 chars // 4
    assert estimate_tokens(msgs, estimator=_OnePerCharEstimator()) == 8


def test_should_compress_respects_injected_estimator() -> None:
    """With a 1-token-per-char estimator the same prompt crosses the
    threshold the chars//4 heuristic stays 4x under."""
    legacy = ContextCompressor(
        llm_caller=_ScriptedSummariser(),
        context_window=100,
        threshold_pct=0.5,
    )
    injected = ContextCompressor(
        llm_caller=_ScriptedSummariser(),
        context_window=100,
        threshold_pct=0.5,
        estimator=_OnePerCharEstimator(),
    )
    msgs = [HumanMessage(content="x" * 60)]  # 15 legacy tokens vs 60 injected
    assert legacy.should_compress(msgs) is False
    assert injected.should_compress(msgs) is True


# ---------------------------------------------------------------------------
# RT-2 PR-4 — compaction observability hook + tokens-saved counter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_compacted_fires_once_with_stats() -> None:
    """A real compression fires ``on_compacted`` exactly once with the
    pass count, before/after token estimates, and summary length."""
    summariser = _ScriptedSummariser()
    compressor = ContextCompressor(
        llm_caller=summariser,
        context_window=280,
        threshold_pct=0.5,
        head_keep=2,
        tail_keep=2,
    )
    msgs = _conversation(head=2, middle=16, tail=2, char_per_msg=80)
    before = estimate_tokens(msgs)

    seen: list[CompactionStats] = []

    async def _hook(stats: CompactionStats) -> None:
        seen.append(stats)

    out = await compressor.compress(msgs, on_compacted=_hook)

    assert len(seen) == 1
    stats = seen[0]
    assert stats.passes == 1
    assert stats.tokens_before == before
    assert stats.tokens_after == estimate_tokens(out)
    assert stats.tokens_after < stats.tokens_before
    assert stats.summary_chars > 0


@pytest.mark.asyncio
async def test_on_compacted_not_fired_below_threshold() -> None:
    """Input already under threshold: no pass runs, so no event fires."""
    compressor = ContextCompressor(
        llm_caller=_ScriptedSummariser(), context_window=1000, threshold_pct=0.9
    )
    seen: list[CompactionStats] = []

    async def _hook(stats: CompactionStats) -> None:
        seen.append(stats)

    await compressor.compress([HumanMessage(content="short")], on_compacted=_hook)
    assert seen == []


@pytest.mark.asyncio
async def test_on_compacted_not_fired_on_transient_skip() -> None:
    """A first-pass transient summariser failure skips compression — no
    summary landed, so the observability hook stays silent."""
    summariser = _FlakySummariser(script=[False])
    compressor = _flaky_compressor(summariser)
    msgs = _conversation(head=1, middle=10, tail=1, char_per_msg=80)
    seen: list[CompactionStats] = []

    async def _hook(stats: CompactionStats) -> None:
        seen.append(stats)

    out = await compressor.compress(msgs, on_compacted=_hook)
    assert out == msgs  # skipped, uncompressed
    assert seen == []


@pytest.mark.asyncio
async def test_tokens_saved_counter_increments_by_reclaimed_tokens() -> None:
    """The magnitude counter rises by exactly the estimated tokens the
    middle-summarisation removed from the prompt."""
    from prometheus_client import REGISTRY

    metric = "expert_work_cm_compressor_tokens_saved_total"
    compressor = ContextCompressor(
        llm_caller=_ScriptedSummariser(),
        context_window=280,
        threshold_pct=0.5,
        head_keep=2,
        tail_keep=2,
    )
    msgs = _conversation(head=2, middle=16, tail=2, char_per_msg=80)

    before = REGISTRY.get_sample_value(metric) or 0.0
    out = await compressor.compress(msgs)
    after = REGISTRY.get_sample_value(metric) or 0.0

    expected = estimate_tokens(msgs) - estimate_tokens(out)
    assert expected > 0
    assert after - before == pytest.approx(expected)


# ---------------------------------------------------------------------------
# RT-2 PR-4 (§8.4) — head_keep floor while per_session recall is active
# ---------------------------------------------------------------------------


def test_floor_head_keep_raises_zero_when_per_session_active() -> None:
    """head_keep=0 + per_session memory → floored to 1 so the messages[1]
    cache-anchor block survives compression."""
    assert floor_head_keep_for_injection(0, per_session_memory_active=True) == 1


def test_floor_head_keep_preserves_zero_without_per_session() -> None:
    """head_keep=0 is legit for a non-memory agent — never bricked."""
    assert floor_head_keep_for_injection(0, per_session_memory_active=False) == 0


def test_floor_head_keep_leaves_positive_values_untouched() -> None:
    assert floor_head_keep_for_injection(1, per_session_memory_active=True) == 1
    assert floor_head_keep_for_injection(4, per_session_memory_active=True) == 4
    assert floor_head_keep_for_injection(4, per_session_memory_active=False) == 4
