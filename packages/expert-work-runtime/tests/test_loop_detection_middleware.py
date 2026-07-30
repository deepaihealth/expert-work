"""Unit tests for :class:`LoopDetectionMiddleware` (Stream E.10.5).

Covers test matrix #37 (trip), #38 (no false positives), #39 (args
normalize) from PR #62's STREAM-E-DESIGN supplement.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from expert_work.runtime.middleware import (
    DEFAULT_REMINDER_TEXT,
    LoopDetectionMiddleware,
    Middleware,
    MiddlewareContext,
    clone_ai_message_with_tool_calls,
    fingerprint_tool_calls,
    normalize_args,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _tool_call(name: str, args: dict[str, object], call_id: str = "tc-1") -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _ai(
    tool_calls: list[dict[str, object]] | None = None,
    message_id: str | None = None,
) -> AIMessage:
    """Build an AIMessage with a stable id (so the loop-detect rewrite
    has something to replace via the add_messages reducer)."""
    msg = AIMessage(content="", tool_calls=tool_calls or [])
    if message_id is not None:
        msg.id = message_id
    return msg


async def _terminal(ctx: MiddlewareContext) -> None:
    ctx.payload["terminal_called"] = ctx.payload.get("terminal_called", 0) + 1


def _ctx(messages: list[BaseMessage]) -> MiddlewareContext:
    return MiddlewareContext(payload={"messages": list(messages)})


# ---------------------------------------------------------------------------
# Trip path (#37)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_identical_tool_calls_trip_detection() -> None:
    """Same `(name, args)` triple → tool_calls cleared + reminder appended."""
    mw = LoopDetectionMiddleware()
    tc = _tool_call("read_file", {"path": "/etc/passwd"})
    history: list[BaseMessage] = [
        HumanMessage(content="please read"),
        _ai(tool_calls=[tc], message_id="ai-1"),
        ToolMessage(content="denied", tool_call_id="tc-1"),
        _ai(tool_calls=[tc], message_id="ai-2"),
        ToolMessage(content="denied", tool_call_id="tc-1"),
        _ai(tool_calls=[tc], message_id="ai-3"),
    ]
    ctx = _ctx(history)
    await mw(ctx, _terminal)

    assert ctx.payload["terminal_called"] == 1
    new_messages = ctx.payload["messages"]
    assert len(new_messages) == 2
    cleaned, reminder = new_messages
    assert isinstance(cleaned, AIMessage)
    assert cleaned.id == "ai-3"  # most-recent AIMessage rewritten
    assert cleaned.tool_calls == []
    assert isinstance(reminder, HumanMessage)
    assert "Loop detected" in reminder.content
    # Spec 2026-07-30 Important#2 — not a real user turn: must not render as a
    # chat bubble, forward to third parties, or break history order-pairing.
    assert reminder.additional_kwargs.get("expert_work_hide_from_ui") is True


@pytest.mark.asyncio
async def test_window_skips_non_ai_messages_between_calls() -> None:
    """Only AIMessages with tool_calls count toward the window — interleaved
    ToolMessage replies don't break the detection."""
    mw = LoopDetectionMiddleware()
    tc = _tool_call("list_dir", {"path": "/var"})
    history: list[BaseMessage] = [
        _ai(tool_calls=[tc], message_id="ai-1"),
        ToolMessage(content="x", tool_call_id="tc-1"),
        HumanMessage(content="something"),  # human can interleave too
        _ai(tool_calls=[tc], message_id="ai-2"),
        ToolMessage(content="x", tool_call_id="tc-1"),
        _ai(tool_calls=[tc], message_id="ai-3"),
    ]
    ctx = _ctx(history)
    await mw(ctx, _terminal)
    assert isinstance(ctx.payload["messages"][0], AIMessage)
    assert ctx.payload["messages"][0].id == "ai-3"
    assert ctx.payload["messages"][0].tool_calls == []


# ---------------------------------------------------------------------------
# No-false-positive paths (#38)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_different_tools_no_trip() -> None:
    mw = LoopDetectionMiddleware()
    history: list[BaseMessage] = [
        _ai(tool_calls=[_tool_call("a", {})], message_id="ai-1"),
        _ai(tool_calls=[_tool_call("b", {})], message_id="ai-2"),
        _ai(tool_calls=[_tool_call("c", {})], message_id="ai-3"),
    ]
    ctx = _ctx(history)
    await mw(ctx, _terminal)
    # Untouched.
    assert ctx.payload["messages"] == history


@pytest.mark.asyncio
async def test_same_tool_different_args_no_trip() -> None:
    mw = LoopDetectionMiddleware()
    history: list[BaseMessage] = [
        _ai(tool_calls=[_tool_call("read_file", {"path": "/a"})], message_id="ai-1"),
        _ai(tool_calls=[_tool_call("read_file", {"path": "/b"})], message_id="ai-2"),
        _ai(tool_calls=[_tool_call("read_file", {"path": "/c"})], message_id="ai-3"),
    ]
    ctx = _ctx(history)
    await mw(ctx, _terminal)
    assert ctx.payload["messages"] == history


@pytest.mark.asyncio
async def test_only_two_ai_messages_below_window() -> None:
    mw = LoopDetectionMiddleware()
    tc = _tool_call("a", {"x": 1})
    history: list[BaseMessage] = [
        _ai(tool_calls=[tc], message_id="ai-1"),
        _ai(tool_calls=[tc], message_id="ai-2"),
    ]
    ctx = _ctx(history)
    await mw(ctx, _terminal)
    assert ctx.payload["messages"] == history


@pytest.mark.asyncio
async def test_one_different_call_in_window_breaks_loop() -> None:
    mw = LoopDetectionMiddleware()
    same = _tool_call("a", {})
    diff = _tool_call("b", {})
    history: list[BaseMessage] = [
        _ai(tool_calls=[same], message_id="ai-1"),
        _ai(tool_calls=[diff], message_id="ai-2"),
        _ai(tool_calls=[same], message_id="ai-3"),
    ]
    ctx = _ctx(history)
    await mw(ctx, _terminal)
    assert ctx.payload["messages"] == history


@pytest.mark.asyncio
async def test_empty_messages_safe_pass() -> None:
    mw = LoopDetectionMiddleware()
    ctx = _ctx([])
    await mw(ctx, _terminal)
    assert ctx.payload["terminal_called"] == 1
    assert ctx.payload["messages"] == []


@pytest.mark.asyncio
async def test_ai_without_tool_calls_does_not_count() -> None:
    """A final-answer AIMessage breaks any nascent loop counting."""
    mw = LoopDetectionMiddleware()
    tc = _tool_call("a", {})
    history: list[BaseMessage] = [
        _ai(tool_calls=[tc], message_id="ai-1"),
        _ai(tool_calls=[tc], message_id="ai-2"),
        _ai(tool_calls=None, message_id="ai-3"),  # plain text answer
    ]
    ctx = _ctx(history)
    await mw(ctx, _terminal)
    assert ctx.payload["messages"] == history


# ---------------------------------------------------------------------------
# Args normalisation (#39)
# ---------------------------------------------------------------------------


def test_normalize_args_key_order() -> None:
    a = {"a": 1, "b": 2}
    b = {"b": 2, "a": 1}
    assert normalize_args(a) == normalize_args(b)


def test_normalize_args_whitespace_and_case() -> None:
    assert normalize_args({"path": "/etc"}) == normalize_args({"path": "/etc "})
    assert normalize_args({"path": "/Data"}) == normalize_args({"path": "/data"})


def test_normalize_args_list_order_preserved() -> None:
    """Lists are semantically ordered for most APIs — don't sort them."""
    assert normalize_args(["a", "b"]) != normalize_args(["b", "a"])


def test_normalize_args_recursive() -> None:
    nested = {"outer": {"INNER": "  HelLo  "}}
    expected = {"outer": {"INNER": "hello"}}
    assert normalize_args(nested) == expected


def test_normalize_args_passthrough_scalar() -> None:
    assert normalize_args(42) == 42
    assert normalize_args(3.14) == 3.14
    assert normalize_args(None) is None
    assert normalize_args(True) is True


def test_fingerprint_same_for_normalised_equivalent_args() -> None:
    fp_a = fingerprint_tool_calls([{"name": "r", "args": {"path": "/Etc/Hosts", "limit": 10}}])
    fp_b = fingerprint_tool_calls([{"name": "r", "args": {"limit": 10, "path": "/etc/hosts "}}])
    assert fp_a == fp_b


def test_fingerprint_differs_on_meaningful_change() -> None:
    fp_a = fingerprint_tool_calls([{"name": "r", "args": {"path": "/a"}}])
    fp_b = fingerprint_tool_calls([{"name": "r", "args": {"path": "/b"}}])
    assert fp_a != fp_b


def test_fingerprint_empty_returns_empty_string() -> None:
    assert fingerprint_tool_calls([]) == ""


@pytest.mark.asyncio
async def test_args_normalize_trip_via_middleware() -> None:
    """Three calls with formally-different but semantically-same args still trip."""
    mw = LoopDetectionMiddleware()
    history: list[BaseMessage] = [
        _ai(tool_calls=[_tool_call("r", {"path": "/Data"})], message_id="ai-1"),
        _ai(tool_calls=[_tool_call("r", {"path": "/data "})], message_id="ai-2"),
        _ai(tool_calls=[_tool_call("r", {"path": "/data"})], message_id="ai-3"),
    ]
    ctx = _ctx(history)
    await mw(ctx, _terminal)
    assert len(ctx.payload["messages"]) == 2
    assert ctx.payload["messages"][0].tool_calls == []


# ---------------------------------------------------------------------------
# clone_ai_message_with_tool_calls helper
# ---------------------------------------------------------------------------


def test_clone_preserves_id_and_clears_tool_calls() -> None:
    original = _ai(tool_calls=[_tool_call("a", {"x": 1})], message_id="ai-stable")
    cloned = clone_ai_message_with_tool_calls(original, tool_calls=[])
    assert cloned.id == "ai-stable"
    assert cloned.tool_calls == []
    # Original untouched.
    assert original.tool_calls != []


def test_clone_also_clears_additional_kwargs_tool_calls() -> None:
    msg = AIMessage(
        content="x",
        tool_calls=[_tool_call("a", {})],
        additional_kwargs={"tool_calls": [{"raw": "provider-format"}]},
    )
    cloned = clone_ai_message_with_tool_calls(msg, tool_calls=[])
    assert cloned.additional_kwargs.get("tool_calls") == []


# ---------------------------------------------------------------------------
# Custom configuration + Protocol contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_window_size_two_trips_earlier() -> None:
    mw = LoopDetectionMiddleware(window_size=2)
    tc = _tool_call("a", {})
    history: list[BaseMessage] = [
        _ai(tool_calls=[tc], message_id="ai-1"),
        _ai(tool_calls=[tc], message_id="ai-2"),
    ]
    ctx = _ctx(history)
    await mw(ctx, _terminal)
    assert len(ctx.payload["messages"]) == 2
    assert ctx.payload["messages"][0].tool_calls == []


@pytest.mark.asyncio
async def test_custom_reminder_text_used() -> None:
    custom = "<system-reminder>switch strategies</system-reminder>"
    mw = LoopDetectionMiddleware(reminder_text=custom)
    tc = _tool_call("a", {})
    history: list[BaseMessage] = [_ai(tool_calls=[tc], message_id=f"ai-{i}") for i in range(3)]
    ctx = _ctx(history)
    await mw(ctx, _terminal)
    assert ctx.payload["messages"][1].content == custom


def test_default_reminder_mentions_loop() -> None:
    assert "Loop detected" in DEFAULT_REMINDER_TEXT


def test_satisfies_middleware_protocol() -> None:
    assert isinstance(LoopDetectionMiddleware(), Middleware)
