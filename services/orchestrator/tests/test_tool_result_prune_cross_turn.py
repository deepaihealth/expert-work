"""B-126 —— 跨轮无损清理:只看本轮开始前的历史,同一轮内逐字不变。

估算用 legacy ``chars // 4``(estimator=None),门槛取小值让几 KB 内容就能过线。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from expert_work.common.conversation_channel import HIDE_FROM_UI
from orchestrator.context import current_turn_start, prune_prior_turns
from orchestrator.tools.overflow import TOOL_RESULT_PATH_ARTIFACT_KEY

_BIG = "x" * 8000  # 2000 tokens via chars // 4


def _call(cid: str, name: str = "form_get_field_detail", args: dict | None = None) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": args or {"form_code": cid}, "id": cid, "type": "tool_call"}
        ],
    )


def _res(
    cid: str,
    content: object = None,
    *,
    persisted: bool = True,
    name: str = "form_get_field_detail",
    status: str = "success",
) -> ToolMessage:
    art = (
        {TOOL_RESULT_PATH_ARTIFACT_KEY: f".tool_results/run-1/{cid}-{name}.txt"}
        if persisted
        else None
    )
    return ToolMessage(
        content=f"{_BIG}#{cid}" if content is None else content,
        tool_call_id=cid,
        name=name,
        artifact=art,
        status=status,
    )


def _turn(tag: str, n: int, **kw: object) -> list[BaseMessage]:
    out: list[BaseMessage] = [HumanMessage(content=f"user {tag}")]
    for i in range(n):
        cid = f"{tag}-{i}"
        out += [_call(cid), _res(cid, **kw)]  # type: ignore[arg-type]
    out.append(AIMessage(content=f"answer {tag}"))
    return out


def _run(msgs: list[BaseMessage], **kw: int):
    params = {"recent_tool_results_kept": 1, "min_context_tokens": 100, "min_reclaim_tokens": 100}
    params.update(kw)
    return prune_prior_turns(msgs, **params)


def test_current_turn_start_is_last_real_human() -> None:
    msgs = [*_turn("a", 1), HumanMessage(content="now")]
    assert current_turn_start(msgs) == len(msgs) - 1


def test_hidden_human_is_not_a_turn_boundary() -> None:
    hidden = HumanMessage(content="<inputs/>", additional_kwargs={HIDE_FROM_UI: True})
    msgs = [*_turn("a", 1), HumanMessage(content="now"), hidden, _call("b-0"), _res("b-0")]
    assert current_turn_start(msgs) == len(_turn("a", 1))


def test_prior_turn_results_collapse_current_turn_untouched() -> None:
    msgs = [*_turn("a", 3), *_turn("b", 2)]
    r = _run(msgs)
    tools = [m for m in r.messages if isinstance(m, ToolMessage)]
    # 旧轮 a 的 3 条中最近 1 条受保护 → 清 2 条;本轮 b 的 2 条一律不动。
    assert r.pruned_count == 2
    assert tools[0].content.startswith("<tool-result-pruned>")
    assert tools[1].content.startswith("<tool-result-pruned>")
    assert tools[2].content.startswith(_BIG)
    assert all(t.content.startswith(_BIG) for t in tools[3:])
    assert r.reclaimed_tokens > 0


def test_stub_names_tool_args_and_recovery_path() -> None:
    r = _run([*_turn("a", 2), *_turn("b", 1)])
    stub = next(m for m in r.messages if isinstance(m, ToolMessage)).content
    assert "form_get_field_detail" in stub
    assert '"form_code": "a-0"' in stub
    assert ".tool_results/run-1/a-0-form_get_field_detail.txt" in stub


def test_view_is_identical_across_calls_in_a_turn() -> None:
    base = [*_turn("a", 3), HumanMessage(content="now"), _call("b-0"), _res("b-0")]
    grown = [*base, _call("b-1"), _res("b-1"), _call("b-2"), _res("b-2")]
    r1, r2 = _run(base), _run(grown)
    assert r1.messages == r2.messages[: len(r1.messages)]


def test_no_prior_turn_is_noop() -> None:
    msgs = _turn("a", 5)
    r = _run(msgs)
    assert r.pruned_count == 0 and r.messages == msgs


def test_under_min_context_is_noop() -> None:
    msgs = [*_turn("a", 3), *_turn("b", 1)]
    r = _run(msgs, min_context_tokens=10**9)
    assert r.pruned_count == 0 and r.messages == msgs


def test_under_min_reclaim_is_noop() -> None:
    msgs = [*_turn("a", 3), *_turn("b", 1)]
    r = _run(msgs, min_reclaim_tokens=10**9)
    assert r.pruned_count == 0 and r.messages == msgs


def test_min_context_counts_only_turn_start() -> None:
    # 本轮自己很大,但旧轮 + 本轮用户消息很小 → 不清(R2)。
    small_prior = [HumanMessage(content="hi"), _call("a-0"), _res("a-0", "tiny", persisted=True)]
    msgs = [
        *small_prior,
        HumanMessage(content="now"),
        *[m for i in range(5) for m in (_call(f"b-{i}"), _res(f"b-{i}"))],
    ]
    r = _run(msgs, min_context_tokens=1000)
    assert r.pruned_count == 0


def test_no_recovery_path_is_never_pruned() -> None:
    msgs = [*_turn("a", 3, persisted=False), *_turn("b", 1)]
    r = _run(msgs)
    assert r.pruned_count == 0


def test_error_results_are_never_pruned() -> None:
    msgs = [*_turn("a", 3, status="error"), *_turn("b", 1)]
    r = _run(msgs)
    assert r.pruned_count == 0


def test_non_string_content_untouched() -> None:
    prior = [
        HumanMessage(content="u"),
        _call("a-0"),
        _res("a-0", [{"type": "text", "text": _BIG}]),
        _call("a-1"),
        _res("a-1"),
    ]
    r = _run([*prior, *_turn("b", 1)], recent_tool_results_kept=0)
    tools = [m for m in r.messages if isinstance(m, ToolMessage)]
    assert isinstance(tools[0].content, list)


def test_assistant_messages_never_touched() -> None:
    msgs = [*_turn("a", 3), *_turn("b", 1)]
    r = _run(msgs)
    before = [m.content for m in msgs if isinstance(m, AIMessage)]
    after = [m.content for m in r.messages if isinstance(m, AIMessage)]
    assert before == after


def test_pairing_and_ids_preserved() -> None:
    msgs = [*_turn("a", 3), *_turn("b", 1)]
    r = _run(msgs)
    assert len(r.messages) == len(msgs)
    for a, b in zip(msgs, r.messages, strict=True):
        assert type(a) is type(b)
        if isinstance(a, ToolMessage):
            assert (a.tool_call_id, a.name, a.id) == (b.tool_call_id, b.name, b.id)


def test_idempotent() -> None:
    msgs = [*_turn("a", 3), *_turn("b", 1)]
    once = _run(msgs).messages
    twice = _run(once)
    assert twice.messages == once and twice.pruned_count == 0


def test_stubs_only_reference_paths_already_in_history() -> None:
    # R1 —— 占位引用的路径必须来自被清的那条消息自身(本对话历史),不引入外来路径。
    msgs = [*_turn("a", 3), *_turn("b", 1)]
    own_paths = {
        m.artifact[TOOL_RESULT_PATH_ARTIFACT_KEY]
        for m in msgs
        if isinstance(m, ToolMessage) and m.artifact
    }
    for m in _run(msgs).messages:
        if isinstance(m, ToolMessage) and m.content.startswith("<tool-result-pruned>"):
            assert any(p in m.content for p in own_paths)
