"""B2 worker 可观测性 — 帧构建纯函数单测."""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from expert_work.common.spotlight import DATAMARK_GLYPH, spotlight_untrusted, unspotlight
from orchestrator.tools._worker_events import (
    WORKER_ARGS_EXCERPT,
    WORKER_CONTENT_EXCERPT,
    WORKER_RESULT_EXCERPT,
    WorkerIdentity,
    build_worker_end_frame,
    build_worker_start_frame,
    build_worker_update_frame,
)

_IDENT = WorkerIdentity(
    worker_id="w-1",
    parent_worker_id=None,
    parent_tool_call_id="call-1",
    label="spawn_worker",
    agent_ref="dynamic:research",
    depth=1,
)


def test_start_frame_envelope_and_task_excerpt() -> None:
    frame = build_worker_start_frame(_IDENT, wseq=0, task="t" * 600, role="research", max_steps=32)
    assert frame["worker_id"] == "w-1"
    assert frame["parent_worker_id"] is None
    assert frame["parent_tool_call_id"] == "call-1"
    assert frame["label"] == "spawn_worker"
    assert frame["agent_ref"] == "dynamic:research"
    assert frame["depth"] == 1
    assert frame["kind"] == "start"
    assert frame["wseq"] == 0
    assert frame["data"]["role"] == "research"
    assert frame["data"]["max_steps"] == 32
    # 500 字 + "…"
    assert len(frame["data"]["task_excerpt"]) == WORKER_CONTENT_EXCERPT + 1
    assert frame["data"]["task_excerpt"].endswith("…")


def test_update_frame_summarizes_ai_and_tool_messages() -> None:
    writes = {
        "step_count": 3,
        "messages": [
            AIMessage(
                content="x" * 600,
                tool_calls=[
                    {
                        "name": "http_request",
                        "args": {"url": "https://e.com", "body": "b" * 300},
                        "id": "tc-1",
                    }
                ],
            ),
            ToolMessage(content="r" * 600, tool_call_id="tc-1", name="http_request"),
        ],
        "plan": {"goal": "dropped"},
    }
    frame = build_worker_update_frame(_IDENT, wseq=1, node="agent", writes=writes, duration_ms=42)
    data = frame["data"]
    assert frame["kind"] == "update"
    assert data["node"] == "agent"
    assert data["step_count"] == 3
    assert data["_duration_ms"] == 42
    assert "plan" not in data  # 非消息类 writes 丢弃
    ai, tool = data["messages"]
    assert ai["type"] == "ai"
    assert len(ai["content_excerpt"]) == WORKER_CONTENT_EXCERPT + 1
    assert ai["tool_calls"][0]["name"] == "http_request"
    assert len(ai["tool_calls"][0]["args_excerpt"]) == WORKER_ARGS_EXCERPT + 1
    assert tool["type"] == "tool"
    assert tool["name"] == "http_request"
    assert len(tool["tool_result_excerpt"]) == WORKER_RESULT_EXCERPT + 1


def test_update_frame_unspotlights_tool_result_excerpt() -> None:
    # B-25 — a worker's ToolMessage content arrives spotlighted (datamarked +
    # nonce-fenced, builder._invoke_tool). The frame leaves the platform via
    # the external SSE/items surface, so the excerpt must carry the tool's
    # words, not the internal anti-injection wrapping.
    original = "搜索结果 有效 line-two"
    msg = ToolMessage(
        content=spotlight_untrusted(original, nonce="0ce9b28d"),
        tool_call_id="tc-1",
        name="http_request",
    )
    frame = build_worker_update_frame(
        _IDENT, wseq=1, node="tools", writes={"messages": [msg]}, duration_ms=5
    )
    (tool,) = frame["data"]["messages"]
    assert tool["tool_result_excerpt"] == original
    assert "«" not in tool["tool_result_excerpt"]
    assert DATAMARK_GLYPH not in tool["tool_result_excerpt"]
    assert "0ce9b28d" not in tool["tool_result_excerpt"]


def test_update_frame_unspotlights_before_truncating() -> None:
    # B-25 — order matters: truncating first would cut the fence in half and
    # unspotlight's marker regex would no longer match, leaking a partial
    # fence + every glyph. Long content must be restored, THEN excerpted.
    original = "w " * 600  # datamark touches every gap; wrapped length ≫ excerpt budget
    msg = ToolMessage(
        content=spotlight_untrusted(original, nonce="deadbeef1234"),
        tool_call_id="tc-1",
        name="http_request",
    )
    frame = build_worker_update_frame(
        _IDENT, wseq=1, node="tools", writes={"messages": [msg]}, duration_ms=5
    )
    (tool,) = frame["data"]["messages"]
    excerpt = tool["tool_result_excerpt"]
    assert len(excerpt) == WORKER_RESULT_EXCERPT + 1  # 500 + "…"
    assert (
        excerpt
        == unspotlight(spotlight_untrusted(original, nonce="deadbeef1234"))[:WORKER_RESULT_EXCERPT]
        + "…"
    )
    assert "«" not in excerpt
    assert "UNTRUSTED" not in excerpt
    assert DATAMARK_GLYPH not in excerpt


def test_update_frame_unspotlights_exec_artifact_streams() -> None:
    # B-25 — the exec artifact's stdout/stderr also cross the external
    # surface; if a marked rendering ever lands there, the frame must still
    # ship clean text (unspotlight is a no-op on already-clean streams).
    marked_out = spotlight_untrusted("ok done", nonce="n-exec")
    marked_err = spotlight_untrusted("boom failed", nonce="n-exec")
    msg = ToolMessage(
        content=spotlight_untrusted("stdout:\nok done\n\nexit_code: 0", nonce="n-exec"),
        tool_call_id="tc-1",
        name="exec_python",
        artifact={
            "exit_code": 0,
            "timed_out": False,
            "truncated": False,
            "stdout": marked_out,
            "stderr": marked_err,
        },
    )
    frame = build_worker_update_frame(
        _IDENT, wseq=1, node="tools", writes={"messages": [msg]}, duration_ms=5
    )
    (tool,) = frame["data"]["messages"]
    exec_summary = tool["exec"]
    assert exec_summary["stdout_excerpt"] == "ok done"
    assert exec_summary["stderr_excerpt"] == "boom failed"
    assert "«" not in tool["tool_result_excerpt"]
    assert DATAMARK_GLYPH not in tool["tool_result_excerpt"]
    assert "n-exec" not in tool["tool_result_excerpt"]


def test_update_frame_accepts_single_message_and_generic_type() -> None:
    frame = build_worker_update_frame(
        _IDENT,
        wseq=0,
        node="agent",
        writes={"messages": SystemMessage(content="hi")},
        duration_ms=1,
    )
    (msg,) = frame["data"]["messages"]
    assert msg["type"] == "system"
    assert msg["content_excerpt"] == "hi"


def test_update_frame_no_step_count_key_when_absent() -> None:
    frame = build_worker_update_frame(
        _IDENT, wseq=0, node="tools", writes={"messages": []}, duration_ms=5
    )
    assert "step_count" not in frame["data"]
    assert frame["data"]["messages"] == []


def test_update_frame_carries_exec_artifact_summary() -> None:
    # PR-D — a worker's exec_python/bash result keeps its structured fields
    # (excerpted to the frame's summary budget); the content excerpt loses its
    # line structure to datamarking (B-25 unspotlight recovers words, not layout).
    msg = ToolMessage(
        content="stdout:▁ 1▁ exit_code:▁ 0",
        tool_call_id="tc-1",
        name="exec_python",
        artifact={
            "exit_code": 0,
            "timed_out": False,
            "truncated": False,
            "stdout": "1\n" * 600,
            "stderr": "",
        },
    )
    frame = build_worker_update_frame(
        _IDENT, wseq=1, node="tools", writes={"messages": [msg]}, duration_ms=5
    )
    (tool,) = frame["data"]["messages"]
    # B-25 — the datamarked content excerpt leaves the frame restored, not raw.
    assert tool["tool_result_excerpt"] == "stdout: 1 exit_code: 0"
    exec_summary = tool["exec"]
    assert exec_summary["exit_code"] == 0
    assert exec_summary["timed_out"] is False
    assert exec_summary["stdout_excerpt"].startswith("1\n")
    assert len(exec_summary["stdout_excerpt"]) == WORKER_RESULT_EXCERPT + 1  # +1 = "…"
    assert exec_summary["stderr_excerpt"] == ""


def test_update_frame_excerpts_a_long_stderr_artifact() -> None:
    # Final-review fix wave — mutation coverage. The pre-existing
    # exec-artifact test's stderr is empty, so a mutant that drops the
    # ``_excerpt(...)`` wrapper around ``stderr_excerpt`` (leaving the raw
    # ``artifact["stderr"]`` un-truncated) survives; a >500-char stderr kills
    # it.
    msg = ToolMessage(
        content="stdout:▁ exit_code:▁ 1",
        tool_call_id="tc-3",
        name="bash",
        artifact={
            "exit_code": 1,
            "timed_out": False,
            "truncated": False,
            "stdout": "",
            "stderr": "e" * 600,
        },
    )
    frame = build_worker_update_frame(
        _IDENT, wseq=1, node="tools", writes={"messages": [msg]}, duration_ms=5
    )
    (tool,) = frame["data"]["messages"]
    assert len(tool["exec"]["stderr_excerpt"]) == WORKER_RESULT_EXCERPT + 1


def test_update_frame_ignores_bool_exit_code() -> None:
    # Final-review fix wave — mutation coverage. ``bool`` is a subclass of
    # ``int`` in Python, so ``isinstance(True, int)`` is True; the artifact
    # reader must explicitly exclude bools via
    # ``not isinstance(exit_code, bool)``, or a bogus artifact shape with
    # ``exit_code: True`` would grow an ``exec`` key.
    msg = ToolMessage(
        content="ok",
        tool_call_id="tc-4",
        name="exec_python",
        artifact={"exit_code": True, "timed_out": False, "stdout": "", "stderr": ""},
    )
    frame = build_worker_update_frame(
        _IDENT, wseq=1, node="tools", writes={"messages": [msg]}, duration_ms=5
    )
    (tool,) = frame["data"]["messages"]
    assert "exec" not in tool


def test_update_frame_ignores_non_exec_artifact() -> None:
    # manage_task-style artifacts (no exit_code) must not grow an exec key.
    msg = ToolMessage(
        content="ok",
        tool_call_id="tc-2",
        name="manage_task",
        artifact={"trigger_id": "t1", "action": "create"},
    )
    frame = build_worker_update_frame(
        _IDENT, wseq=1, node="tools", writes={"messages": [msg]}, duration_ms=5
    )
    (tool,) = frame["data"]["messages"]
    assert "exec" not in tool


def test_end_frame_summary() -> None:
    frame = build_worker_end_frame(
        _IDENT,
        wseq=9,
        outcome="max_steps",
        iteration_used=32,
        llm_call_count=16,
        wall_clock_ms=1234,
    )
    assert frame["kind"] == "end"
    assert frame["data"] == {
        "outcome": "max_steps",
        "iteration_used": 32,
        "llm_call_count": 16,
        "wall_clock_ms": 1234,
    }


def test_frames_are_json_safe() -> None:
    writes = {"messages": [AIMessage(content="ok")], "step_count": 1}
    for frame in (
        build_worker_start_frame(_IDENT, wseq=0, task="t", role=None, max_steps=8),
        build_worker_update_frame(_IDENT, wseq=1, node="agent", writes=writes, duration_ms=0),
        build_worker_end_frame(
            _IDENT, wseq=2, outcome="success", iteration_used=1, llm_call_count=1, wall_clock_ms=10
        ),
    ):
        json.dumps(frame)  # 不抛 = JSON-safe


def test_end_frame_carries_worker_token_usage() -> None:
    """end 帧必须带 worker 这一轮烧掉的 token。

    没有它,父侧的每个消费者都只能看到主线的消耗:``turn_summary.ts`` 的
    第一行就是 ``if (evt.event !== "updates") continue;``,worker 事件整个
    跳过。线上实例(run f562fa69):对话页显示 175,137 tok,而
    ``token_usage`` 里同一 trace 下 ``sop2-designer-worker`` 另有 69 次调用
    共 3,317,974 —— 界面少报 19 倍。

    形状与 langchain 的 ``AIMessage.usage_metadata`` 同构,前端因此能直接
    复用现成的 ``usageFromMetadata`` 解析,不必为 worker 另写一套。
    """
    frame = build_worker_end_frame(
        _IDENT,
        wseq=9,
        outcome="success",
        iteration_used=49,
        llm_call_count=49,
        wall_clock_ms=933_000,
        usage={
            "input_tokens": 3_200_000,
            "output_tokens": 117_974,
            "total_tokens": 3_317_974,
            "input_token_details": {"cache_read": 1_000, "cache_creation": 2_000},
            "output_token_details": {"reasoning": 4_000},
        },
    )
    assert frame["kind"] == "end"
    usage = frame["data"]["usage"]
    assert usage["input_tokens"] == 3_200_000
    assert usage["output_tokens"] == 117_974
    assert usage["total_tokens"] == 3_317_974
    assert usage["input_token_details"]["cache_read"] == 1_000
    assert usage["output_token_details"]["reasoning"] == 4_000
    # 既有四字段不能被挤掉。
    assert frame["data"]["iteration_used"] == 49
    assert frame["data"]["wall_clock_ms"] == 933_000


def test_end_frame_usage_absent_stays_absent() -> None:
    """拿不到 usage 时不得编一个 0 —— 「没报」和「真的是 0」必须分得开。

    ``usage_metadata`` 是提供商可选字段(部分 provider / 缓存命中路径不报)。
    塞一个零值会让消费者把「未知」当成「免费」。
    """
    frame = build_worker_end_frame(
        _IDENT, wseq=1, outcome="success", iteration_used=2, llm_call_count=2, wall_clock_ms=10
    )
    assert "usage" not in frame["data"]


def test_usage_of_sums_ai_messages_and_reports_none_when_unreported() -> None:
    """``_usage_of`` 是 end 帧 usage 的来源——从 worker 自己的消息里求和。

    provider 只在部分消息上报 ``usage_metadata`` 是常态(缓存命中、流式尾
    帧),求和必须跳过没报的那些而不是崩掉;一条都没报要回 ``None``,让
    「未知」和「真的是 0」分得开。
    """
    from orchestrator.tools._child_run import _usage_of

    msgs = [
        AIMessage(
            content="a",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 10,
                "total_tokens": 110,
                "input_token_details": {"cache_read": 40, "cache_creation": 5},
                "output_token_details": {"reasoning": 3},
            },
        ),
        ToolMessage(content="t", tool_call_id="tc-1"),
        AIMessage(content="b"),  # provider 没报 usage 的那种
        AIMessage(
            content="c",
            usage_metadata={
                "input_tokens": 200,
                "output_tokens": 20,
                "total_tokens": 220,
                "input_token_details": {"cache_read": 60, "cache_creation": 0},
                "output_token_details": {"reasoning": 7},
            },
        ),
    ]
    usage = _usage_of(msgs)
    assert usage is not None
    assert usage["input_tokens"] == 300
    assert usage["output_tokens"] == 30
    assert usage["total_tokens"] == 330
    assert usage["input_token_details"] == {"cache_read": 100, "cache_creation": 5}
    assert usage["output_token_details"] == {"reasoning": 10}

    assert _usage_of([AIMessage(content="x"), SystemMessage(content="s")]) is None
    assert _usage_of([]) is None


def test_a_workers_account_excludes_what_came_back_from_its_child() -> None:
    """B-41 —— 嵌套委派的另一半不变式:**父 worker 的账不含孙 worker 的 token**。

    整棵树只被数一次,靠的是两件事同时成立:

    1. 孙 worker 的 end 帧也直达父 run 的 bridge(``_child_run`` 把
       ``worker_event_sink`` 向下透传),消费者累加所有 end 帧即得整棵树;
    2. **而每个 worker 的 end 帧只装它自己那份** —— 子代跑在自己的线程上,
       回到父这边只是一条 ``ToolMessage``,不带 ``usage_metadata``。

    少了第 2 条就是双计(和过程条耗时那次一模一样:同一段时间既进工具行又进
    subagent 行,44m23s 反超 23m45s 的总墙钟)。少了第 1 条则是漏计。

    这条钉的是第 2 条。它防的是一次很自然的「改进」:为了让父帧自包含,把子
    帧的 usage 也并进父帧 —— 那正好把不变式打破。素材因此**刻意**把孙的
    token 摆在几个顺手就会被读到的位置(正文、``additional_kwargs``、
    ``response_metadata``),只有严格只认 ``usage_metadata`` 的实现才会绿。

    第 1 条是前端属性,钉在
    ``turn_summary.test.ts`` 的 "counts a nested worker and its parent once each"。
    """
    from orchestrator.tools._child_run import _usage_of

    child_tokens = {"input_tokens": 3_000_000, "output_tokens": 300_000, "total_tokens": 3_300_000}
    parent_messages = [
        AIMessage(
            content="先派个孙 worker",
            usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
        ),
        # 孙 worker 的结果回到父这边的形态:一条工具消息。
        ToolMessage(
            content=f"worker 完成,用了 {child_tokens['total_tokens']} tokens",
            tool_call_id="tc-child",
            additional_kwargs={"usage": dict(child_tokens)},
            response_metadata={"token_usage": dict(child_tokens)},
        ),
        AIMessage(
            content="汇总",
            usage_metadata={"input_tokens": 200, "output_tokens": 20, "total_tokens": 220},
        ),
    ]

    usage = _usage_of(parent_messages)

    assert usage is not None
    # 父自己的两次调用:110 + 220。孙的 3_300_000 一分都不能进来。
    assert usage["total_tokens"] == 330
    assert usage["input_tokens"] == 300
    assert usage["output_tokens"] == 30
