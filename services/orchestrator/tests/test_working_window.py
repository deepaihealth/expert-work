"""Stream CM-2 — :class:`WorkingWindow` + ``trim_to_recent_turns`` unit tests.

Pins the working-memory sliding-window invariants: the token gate is a
no-op under threshold, trimming keeps the first turn plus the most-recent N
turns, cuts land only on ``HumanMessage`` boundaries so no ToolCall↔ToolResult
pair is ever split (OpenClaw #1084), the leading ``SystemMessage`` block is
frozen, and the no-turn / few-turn / overlap edges no-op or de-dup cleanly.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from expert_work.common.conversation_channel import HIDE_FROM_UI
from orchestrator.context import TrimResult, WorkingWindow, trim_to_recent_turns

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _turn(idx: int, *, with_tool: bool = False) -> list[BaseMessage]:
    """One user turn: a HumanMessage, then (optionally) an AIMessage with a
    tool_call paired to its ToolMessage, then the assistant's reply."""
    msgs: list[BaseMessage] = [HumanMessage(content=f"user-{idx}")]
    if with_tool:
        call_id = f"call-{idx}"
        msgs.append(
            AIMessage(
                content="",
                tool_calls=[{"id": call_id, "name": "search", "args": {"q": str(idx)}}],
            )
        )
        msgs.append(ToolMessage(content=f"result-{idx}", tool_call_id=call_id))
    msgs.append(AIMessage(content=f"assistant-{idx}"))
    return msgs


def _conversation(
    n_turns: int, *, with_tool: bool = False, system: bool = True
) -> list[BaseMessage]:
    msgs: list[BaseMessage] = []
    if system:
        msgs.append(SystemMessage(content="system prompt"))
    for i in range(n_turns):
        msgs.extend(_turn(i, with_tool=with_tool))
    return msgs


def _user_payloads(messages: list[BaseMessage]) -> list[str]:
    return [str(m.content) for m in messages if isinstance(m, HumanMessage)]


def _assert_tool_pairs_intact(messages: list[BaseMessage]) -> None:
    """Every tool_use has a following tool_result and vice versa."""
    open_ids: set[str] = set()
    for m in messages:
        if isinstance(m, AIMessage) and m.tool_calls:
            for tc in m.tool_calls:
                open_ids.add(tc["id"])
        if isinstance(m, ToolMessage):
            # A tool_result must answer a tool_use seen earlier.
            assert m.tool_call_id in open_ids, f"dangling tool_result {m.tool_call_id}"
            open_ids.discard(m.tool_call_id)
    assert not open_ids, f"unanswered tool_use ids: {open_ids}"


# ---------------------------------------------------------------------------
# trim_to_recent_turns — pure trimming (token-unaware)
# ---------------------------------------------------------------------------


def test_trim_noop_when_within_budget() -> None:
    msgs = _conversation(3)
    result = trim_to_recent_turns(msgs, max_recent_turns=5, keep_first_turn=True)
    assert result.dropped_turns == 0
    assert result.messages == msgs


def test_trim_noop_when_no_human_message() -> None:
    msgs: list[BaseMessage] = [SystemMessage(content="s"), AIMessage(content="a")]
    result = trim_to_recent_turns(msgs, max_recent_turns=1, keep_first_turn=True)
    assert result.dropped_turns == 0
    assert result.messages == msgs


def test_trim_keeps_first_and_recent_turns() -> None:
    # 10 turns, keep first + most-recent 3 → drop turns 1..6 (six dropped).
    msgs = _conversation(10)
    result = trim_to_recent_turns(msgs, max_recent_turns=3, keep_first_turn=True)
    assert result.dropped_turns == 6
    payloads = _user_payloads(result.messages)
    assert payloads == ["user-0", "user-7", "user-8", "user-9"]
    # leading system frozen at the head.
    assert isinstance(result.messages[0], SystemMessage)


def test_trim_without_first_turn_keeps_only_recent() -> None:
    msgs = _conversation(10)
    result = trim_to_recent_turns(msgs, max_recent_turns=3, keep_first_turn=False)
    assert result.dropped_turns == 7
    assert _user_payloads(result.messages) == ["user-7", "user-8", "user-9"]


def test_trim_first_turn_overlaps_window_is_noop() -> None:
    # total == max_recent_turns + 1: keeping first turn + recent N covers all.
    msgs = _conversation(4)
    result = trim_to_recent_turns(msgs, max_recent_turns=3, keep_first_turn=True)
    assert result.dropped_turns == 0
    assert _user_payloads(result.messages) == ["user-0", "user-1", "user-2", "user-3"]


def test_trim_preserves_tool_call_pairs() -> None:
    # Every turn carries a tool_use/tool_result pair; trimming must not split one.
    msgs = _conversation(8, with_tool=True)
    result = trim_to_recent_turns(msgs, max_recent_turns=2, keep_first_turn=True)
    _assert_tool_pairs_intact(result.messages)
    # first turn + last 2 turns kept.
    assert _user_payloads(result.messages) == ["user-0", "user-6", "user-7"]


def test_trim_no_leading_system_still_cuts_on_human_boundary() -> None:
    msgs = _conversation(6, with_tool=True, system=False)
    result = trim_to_recent_turns(msgs, max_recent_turns=2, keep_first_turn=True)
    _assert_tool_pairs_intact(result.messages)
    assert isinstance(result.messages[0], HumanMessage)
    assert _user_payloads(result.messages) == ["user-0", "user-4", "user-5"]


# ---------------------------------------------------------------------------
# WorkingWindow — token gate
# ---------------------------------------------------------------------------


def _window(**kw: object) -> WorkingWindow:
    base: dict[str, object] = {"context_window": 1000, "threshold_pct": 0.7, "max_recent_turns": 2}
    base.update(kw)
    return WorkingWindow(**base)  # type: ignore[arg-type]


def test_should_trim_gate() -> None:
    window = _window()  # threshold = 700 tokens = 2800 chars
    small = [HumanMessage(content="hi")]
    big = [HumanMessage(content="x" * 4000)]
    assert window.should_trim(small) is False
    assert window.should_trim(big) is True


def test_apply_noop_under_threshold() -> None:
    window = _window(max_recent_turns=1)
    msgs = _conversation(5)  # short, well under 2800 chars
    result = window.apply(msgs)
    assert isinstance(result, TrimResult)
    assert result.dropped_turns == 0
    assert result.messages == msgs


def test_apply_trims_over_threshold() -> None:
    window = _window(max_recent_turns=2, keep_first_turn=True)
    # Make each turn large enough that 6 turns blow past 2800 chars.
    msgs: list[BaseMessage] = [SystemMessage(content="s")]
    for i in range(6):
        msgs.append(HumanMessage(content=f"user-{i} " + "y" * 600))
        msgs.append(AIMessage(content=f"assistant-{i}"))
    result = window.apply(msgs)
    assert result.dropped_turns == 3  # 6 - 2 - 1 (first kept)
    assert _user_payloads(result.messages)[0].startswith("user-0")
    assert _user_payloads(result.messages)[-1].startswith("user-5")
    assert len(_user_payloads(result.messages)) == 3


def test_apply_returns_new_list_does_not_mutate_input() -> None:
    window = _window(max_recent_turns=2)
    msgs = _conversation(8, with_tool=True)
    original = list(msgs)
    window.apply(msgs)
    assert msgs == original  # input untouched


# ---------------------------------------------------------------------------
# Stream HX-1 — injected estimator replaces the chars//4 heuristic
# ---------------------------------------------------------------------------


class _OnePerCharEstimator:
    def count(self, text: str) -> int:
        return len(text)


def test_should_trim_respects_injected_estimator() -> None:
    # threshold = 700 tokens; 1000 chars = 250 legacy tokens (no trim)
    # but 1000 injected tokens (trim).
    msgs = [HumanMessage(content="x" * 1000)]
    assert _window().should_trim(msgs) is False
    assert _window(estimator=_OnePerCharEstimator()).should_trim(msgs) is True


# ---------------------------------------------------------------------------
# B-67 — hidden HumanMessages (inputs block / advisories) never open a turn
# ---------------------------------------------------------------------------


def _hidden(text: str) -> HumanMessage:
    return HumanMessage(content=text, additional_kwargs={HIDE_FROM_UI: True})


def _jinja_conversation(n_turns: int) -> list[BaseMessage]:
    """Each turn = user message, hidden inputs block, a tool round, a hidden
    mid-turn advisory, the reply — the shapes ``build_run_graph_input`` and the
    recovery advisory actually produce."""
    msgs: list[BaseMessage] = [SystemMessage(content="system prompt")]
    for i in range(n_turns):
        call_id = f"call-{i}"
        msgs.extend(
            [
                HumanMessage(content=f"user-{i}"),
                _hidden(f"block-{i}"),
                AIMessage(
                    content="",
                    tool_calls=[{"id": call_id, "name": "search", "args": {"q": str(i)}}],
                ),
                ToolMessage(content=f"result-{i}", tool_call_id=call_id),
                _hidden(f"advisory-{i}"),
                AIMessage(content=f"assistant-{i}"),
            ]
        )
    return msgs


def _texts(messages: list[BaseMessage]) -> list[str]:
    return [str(m.content) for m in messages if isinstance(m, HumanMessage)]


def test_hidden_human_messages_stay_inside_the_turn_they_sit_in() -> None:
    result = trim_to_recent_turns(_jinja_conversation(3), max_recent_turns=1, keep_first_turn=False)
    # N=1 keeps the current user message together with its block and advisory.
    assert _texts(result.messages) == ["user-2", "block-2", "advisory-2"]
    assert result.dropped_turns == 2
    assert isinstance(result.messages[0], SystemMessage)
    _assert_tool_pairs_intact(result.messages)


def test_keep_first_turn_keeps_the_first_user_message_with_its_block() -> None:
    result = trim_to_recent_turns(_jinja_conversation(4), max_recent_turns=1, keep_first_turn=True)
    assert _texts(result.messages) == [
        "user-0",
        "block-0",
        "advisory-0",
        "user-3",
        "block-3",
        "advisory-3",
    ]
    assert result.dropped_turns == 2
    _assert_tool_pairs_intact(result.messages)


def test_only_user_messages_count_toward_the_turn_budget() -> None:
    msgs = _jinja_conversation(2)  # two user turns, six HumanMessages
    result = trim_to_recent_turns(msgs, max_recent_turns=2, keep_first_turn=False)
    assert result == TrimResult(messages=msgs, dropped_turns=0)
