"""B-302 PR 4 —— 结果重复检测,影子模式(spec §4.4、D2)。

规则逐条钉在 :mod:`orchestrator.graph_builder.outcome_repeat` 的纯函数上;最后两条走真图:
影子模式下模型看到的提示词、工具结果、路由与收尾和没有这项检测时逐字相同,只多一份
检查点里的账、一个计数器和一条轨迹 span。所有夹具都是合成的。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY

from expert_work.common.observability import init_tracing
from expert_work.common.spotlight import spotlight_untrusted
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import GraphRunner, ToolContext, ToolRegistry, build_react_graph
from orchestrator.graph_builder import builder as builder_module
from orchestrator.graph_builder.outcome_repeat import (
    FINGERPRINT_EDGE_CHARS,
    MIN_RESULT_BYTES,
    OUTCOME_REPEATS_KEY,
    OutcomeFlag,
    next_outcomes,
    outcome_repeat_update,
    result_fingerprint,
    strip_volatile,
)
from orchestrator.sse import PROMPT_INPUTS_KEY
from orchestrator.tools._guards import LOOP_TRIP_META_KEY
from orchestrator.tools.registry import ToolResult, ToolSpec

from .test_figure_block_injection import _RecordingLLM

#: 一段够长(去易变字段后 ≥ 64 字节)的合成结果正文。
_BODY = "line one of a synthetic tool result\nline two of the same result\nline three ends here\n"
_SPAN = "expert_work.orchestrator.outcome_repeat_shadow"


def _msg(content: str, call_id: str = "c1", name: str = "probe") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=call_id, name=name)


def _call(args: Mapping[str, Any], call_id: str = "c1", name: str = "probe") -> dict[str, Any]:
    return {"name": name, "args": dict(args), "id": call_id}


def _run(
    steps: Sequence[tuple[Mapping[str, Any], str]], name: str = "probe"
) -> list[list[OutcomeFlag]]:
    """把 ``(args, result)`` 一次一批地套上去,返回每一批的标记。"""
    ledger: Mapping[str, Any] = {}
    flags_per_step: list[list[OutcomeFlag]] = []
    for i, (args, result) in enumerate(steps):
        call_id = f"call-{i}"
        ledger, flags = next_outcomes(
            ledger, [(_call(args, call_id, name), _msg(result, call_id, name))]
        )
        flags_per_step.append(list(flags))
    return flags_per_step


# --- 指纹:去易变字段 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            "job 3f2b8c1e-9a7d-4e21-b0c4-5d6e7f809a1b done",
            "job 0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d done",
        ),
        ("at 2026-11-12T08:15:30.123Z", "at 2026-11-13 21:04:59+08:00"),
        ("clock 08:15:30.250", "clock 23:59:01"),
        ("digest 9f86d081884c7d659a2feaa0c55ad015", "digest a3f5c0e1b2d4968877665544332211ff"),
        ("epoch 1762934130 seen", "epoch 1762999999 seen"),
        ("epoch 1762934130123 seen", "epoch 1762934199999 seen"),
        ("took 152ms total", "took 7.5 s total"),
        ("took 3 sec", "took 12sec"),
        ("wrote /tmp/tmpa8x_2/out.txt ok", "wrote /tmp/run-77/other.bin ok"),
    ],
)
def test_results_that_differ_only_in_a_volatile_field_fingerprint_the_same(
    first: str, second: str
) -> None:
    a = result_fingerprint("probe", _msg(_BODY + first))
    b = result_fingerprint("probe", _msg(_BODY + second))
    assert a is not None
    assert a == b


def test_one_different_cjk_character_is_a_different_result() -> None:
    """守卫:咬「去易变字段时误删正文」。"""
    base = "第一行合成内容,用来凑足长度。第二行合成内容,继续凑足长度。第三行:数值为甲"
    assert result_fingerprint("probe", _msg(base)) != result_fingerprint(
        "probe", _msg(base[:-1] + "乙")
    )


def test_a_timestamp_right_after_cjk_text_is_volatile() -> None:
    """``\\b`` 按 ASCII 算:汉字与数字之间是边界,紧贴汉字的时间戳照样去掉。"""
    a = result_fingerprint("probe", _msg(_BODY + "完成于1762934130"))
    b = result_fingerprint("probe", _msg(_BODY + "完成于1762939999"))
    assert a == b


def test_digits_inside_a_word_are_not_volatile() -> None:
    """守卫:``\\b`` 按 ASCII 算 —— 标识符里的 10 位数字不是时间戳。"""
    assert strip_volatile("id1762934130x") == "id1762934130x"


def test_the_same_bytes_from_another_tool_are_a_different_outcome() -> None:
    assert result_fingerprint("probe", _msg(_BODY)) != result_fingerprint("other", _msg(_BODY))


def test_a_result_shorter_than_the_floor_after_stripping_is_not_counted() -> None:
    short = "x" * (MIN_RESULT_BYTES - 1)
    assert result_fingerprint("probe", _msg(short)) is None
    # 去掉易变字段之后才量长度:这一条原文 ≥ 64 字节,去掉后不够。
    padded = short[:-1] + " 3f2b8c1e-9a7d-4e21-b0c4-5d6e7f809a1b"
    assert len(padded.encode()) >= MIN_RESULT_BYTES
    assert result_fingerprint("probe", _msg(padded)) is None
    assert result_fingerprint("probe", _msg("x" * MIN_RESULT_BYTES)) is not None


def test_the_calls_own_id_is_volatile() -> None:
    """超长结果的落盘引用里带着本次调用的 id(``.tool_results/<run>/<call_id>-<tool>.txt``)。"""
    a = _msg(_BODY + "full copy: .tool_results/x/call_Zq81-probe.txt", call_id="call_Zq81")
    b = _msg(_BODY + "full copy: .tool_results/x/call_Mm42-probe.txt", call_id="call_Mm42")
    assert result_fingerprint("probe", a) == result_fingerprint("probe", b)


def test_the_call_id_is_only_removed_as_a_whole_token() -> None:
    """守卫:id 不能把正文里恰好含这几个字符的片段挖掉(「x9call42z」里的「call42」)。"""
    a = _msg(_BODY + "value x9call42z here", call_id="call42")
    b = _msg(_BODY + "value x9z here", call_id="call42")
    assert result_fingerprint("probe", a) != result_fingerprint("probe", b)


# --- 两种信号 ---------------------------------------------------------------------


def test_s1_counts_the_third_same_call_same_result_even_when_not_consecutive() -> None:
    """A、B、A、B、A(A、B 各自结果不同):第 3 次 A 才标;现有检测只认连续相同,认不出。"""
    a = ({"q": "alpha"}, _BODY + "result for alpha")
    b = ({"q": "beta"}, _BODY + "result for beta")
    flags = _run([a, b, a, b, a])
    assert flags[:4] == [[], [], [], []]
    assert flags[4] == [
        OutcomeFlag(signal="same_args", action="trip", tool="probe", occurrence=3, distinct_args=1)
    ]


def test_s1_normalises_arguments_like_loop_detection() -> None:
    flags = _run([({"q": "Alpha"}, _BODY), ({"q": " alpha "}, _BODY), ({"q": "ALPHA"}, _BODY)])
    assert [f.signal for f in flags[2]] == ["same_args"]


def test_s2_reminds_on_the_third_changed_argument_call_and_would_trip_from_the_fifth() -> None:
    steps = [({"q": f"variant {i}"}, _BODY) for i in range(6)]
    flags = _run(steps)
    assert flags[0] == []
    assert flags[1] == []
    assert flags[2] == [
        OutcomeFlag(
            signal="changed_args", action="remind", tool="probe", occurrence=3, distinct_args=3
        )
    ]
    assert [f.action for f in flags[3]] == ["remind"]
    assert flags[4] == [
        OutcomeFlag(
            signal="changed_args", action="trip", tool="probe", occurrence=5, distinct_args=5
        )
    ]
    assert [f.action for f in flags[5]] == ["trip"]


def test_s2_needs_two_different_arguments() -> None:
    """同一参数三次是 S1,不是 S2。"""
    flags = _run([({"q": "same"}, _BODY)] * 3)
    assert [f.signal for f in flags[2]] == ["same_args"]


def test_different_results_are_never_flagged() -> None:
    flags = _run([({"q": f"v{i}"}, _BODY + f"distinct tail {i}") for i in range(6)])
    assert all(step == [] for step in flags)


def test_short_results_never_reach_the_ledger() -> None:
    ledger, flags = next_outcomes({}, [(_call({"q": 1}), _msg("ok"))] * 5)
    assert ledger == {}
    assert flags == []


def test_calls_in_one_batch_count_in_call_order() -> None:
    batch = [(_call({"q": f"v{i}"}, f"c{i}"), _msg(_BODY, f"c{i}")) for i in range(3)]
    _, flags = next_outcomes({}, batch)
    assert [(f.signal, f.occurrence) for f in flags] == [("changed_args", 3)]


def test_the_previous_ledger_is_not_mutated() -> None:
    ledger, _ = next_outcomes({}, [(_call({"q": "a"}), _msg(_BODY))])
    snapshot = {k: dict(v) for k, v in ledger.items()}
    next_outcomes(ledger, [(_call({"q": "a"}), _msg(_BODY))])
    assert ledger == snapshot


def test_a_malformed_ledger_is_ignored() -> None:
    bad: dict[str, Any] = {"x": "not a mapping", "y": {"a": "two"}, "z": {"b": True}}
    ledger, flags = next_outcomes(bad, [(_call({"q": "a"}), _msg(_BODY))])
    assert flags == []
    assert all(isinstance(v, dict) for v in ledger.values())
    assert sum(sum(v.values()) for v in ledger.values()) == 1


# --- 记录:只有工具名、次数、布尔 --------------------------------------------------


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    exp = InMemorySpanExporter()
    init_tracing(
        service_name="test-outcome-repeat", env="test", span_processor=SimpleSpanProcessor(exp)
    )
    exp.clear()
    yield exp
    exp.clear()


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _flagged_state(steps: int) -> tuple[dict[str, Any], list[tuple[dict[str, Any], ToolMessage]]]:
    """``steps`` 次换参数、结果一样的调用,先套进账里,返回账与下一批。"""
    secret_args = [
        {"path": f"/data/secret-file-{i}.txt", "q": "private text"} for i in range(steps)
    ]
    ledger: Mapping[str, Any] = {}
    for i, args in enumerate(secret_args[:-1]):
        ledger, _ = next_outcomes(ledger, [(_call(args, f"c{i}"), _msg(_BODY, f"c{i}"))])
    last = steps - 1
    batch = [(_call(secret_args[last], f"c{last}"), _msg(_BODY, f"c{last}"))]
    return {OUTCOME_REPEATS_KEY: dict(ledger)}, batch


def test_flags_are_recorded_as_a_counter_a_span_and_a_log_without_content(
    exporter: InMemorySpanExporter, caplog: pytest.LogCaptureFixture
) -> None:
    state, batch = _flagged_state(5)
    metric = "expert_work_outcome_repeat_shadow_total"
    before = _sample(metric, signal="changed_args", action="trip", tool="probe")
    trips_before = _sample("expert_work_outcome_repeat_shadow_trips_total")
    errors_before = _sample("expert_work_outcome_repeat_shadow_errors_total")
    with caplog.at_level(logging.INFO, logger="orchestrator.graph_builder.outcome_repeat"):
        update = outcome_repeat_update(state, batch)
    assert set(update) == {OUTCOME_REPEATS_KEY}
    # 检测自己没出错(出错会被吞掉,只有这个计数说得出来)。
    assert _sample("expert_work_outcome_repeat_shadow_errors_total") == errors_before
    assert _sample(metric, signal="changed_args", action="trip", tool="probe") == before + 1
    assert _sample("expert_work_outcome_repeat_shadow_trips_total") == trips_before + 1

    spans = [s for s in exporter.get_finished_spans() if s.name == _SPAN]
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs["changed_args"] == 1
    assert attrs["same_args"] == 0
    assert attrs["would_trip"] is True
    assert tuple(attrs["tools"]) == ("probe",)
    assert attrs["max_occurrence"] == 5

    logs = [r.getMessage() for r in caplog.records if "outcome_repeat" in r.getMessage()]
    assert len(logs) == 1
    recorded = " ".join([*logs, *(str(v) for v in attrs.values())])
    for leaked in ("secret-file", "/data/", "private text", "synthetic tool result"):
        assert leaked not in recorded


def test_two_trips_in_one_batch_are_one_would_be_trip() -> None:
    """真开的时候每步至多计一次打转(同 loop-detection、B-268),影子的批级计数也按批。"""
    steps = [({"q": f"v{i}"}, _BODY) for i in range(4)]
    ledger: Mapping[str, Any] = {}
    for i, (args, result) in enumerate(steps):
        ledger, _ = next_outcomes(ledger, [(_call(args, f"c{i}"), _msg(result, f"c{i}"))])
    batch = [
        (_call({"q": "v8"}, "c8"), _msg(_BODY, "c8")),
        (_call({"q": "v9"}, "c9"), _msg(_BODY, "c9")),
    ]
    trips_before = _sample("expert_work_outcome_repeat_shadow_trips_total")
    outcome_repeat_update({OUTCOME_REPEATS_KEY: dict(ledger)}, batch)
    assert _sample("expert_work_outcome_repeat_shadow_trips_total") == trips_before + 1


def test_mcp_tool_names_collapse_to_their_server_in_the_metric_label() -> None:
    name = "mcp__srv__lookup"
    metric = "expert_work_outcome_repeat_shadow_total"
    before = _sample(metric, signal="changed_args", action="remind", tool="mcp:srv")
    ledger: Mapping[str, Any] = {}
    for i in range(2):
        ledger, _ = next_outcomes(
            ledger, [(_call({"q": i}, f"c{i}", name), _msg(_BODY, f"c{i}", name))]
        )
    outcome_repeat_update(
        {OUTCOME_REPEATS_KEY: dict(ledger)},
        [(_call({"q": 9}, "c9", name), _msg(_BODY, "c9", name))],
    )
    assert _sample(metric, signal="changed_args", action="remind", tool="mcp:srv") == before + 1


def test_nothing_new_writes_nothing() -> None:
    """没有可计的结果时 state 形状不变。"""
    assert outcome_repeat_update({}, [(_call({"q": 1}), _msg("ok"))]) == {}
    assert outcome_repeat_update({}, []) == {}


# --- 真图:影子模式什么都不改 -----------------------------------------------------


@dataclass
class _SameAnswer:
    """不管参数是什么都回同一段长结果的合成工具。"""

    name: str = "probe"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description="synthetic probe")

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        return ToolResult(content=_BODY + "same outcome every time")


def _probe(call_id: str, args: Mapping[str, Any]) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "probe", "args": dict(args), "id": call_id}])


def _script() -> list[AIMessage]:
    # 换参数 6 次(S2 第 3 次提醒、第 5 次起会计打转),再交替回到旧参数(S1)。
    changed = [_probe(f"c{i}", {"q": f"variant {i}"}) for i in range(6)]
    back = [_probe(f"r{i}", {"q": f"variant {i % 2}"}) for i in range(4)]
    return [*changed, *back]


def _shape(messages: Sequence[BaseMessage]) -> list[tuple[str, str, Any, str]]:
    return [
        (
            type(m).__name__,
            str(m.content),
            getattr(m, "tool_calls", None),
            str(getattr(m, "status", "")),
        )
        for m in messages
    ]


async def _run_graph() -> tuple[dict[str, Any], _RecordingLLM]:
    registry = ToolRegistry()
    registry.register(_SameAnswer())
    llm = _RecordingLLM(responses=_script())
    config: RunnableConfig = {
        "configurable": {
            "thread_id": str(uuid4()),
            "run_id": str(uuid4()),
            "tenant_id": str(uuid4()),
            "user_id": str(uuid4()),
            "agent_key": "synthetic-agent",
        }
    }
    async with make_checkpointer("memory") as cp:
        graph = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=registry)
        )
        final = await graph.ainvoke(
            {"messages": [HumanMessage(content="go")], "step_count": 0, "max_steps": 30},
            config=config,
        )
    return final, llm


async def test_shadow_mode_changes_nothing_the_model_sees_or_the_run_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4-shadow:同一脚本跑两遍,一遍关掉检测(钩子换成什么都不做),逐项比。"""
    metric = "expert_work_outcome_repeat_shadow_total"
    before = sum(
        _sample(metric, signal=s, action=a, tool="probe")
        for s in ("same_args", "changed_args")
        for a in ("remind", "trip")
    )
    errors_before = _sample("expert_work_outcome_repeat_shadow_errors_total")
    shadow_final, shadow_llm = await _run_graph()
    assert _sample("expert_work_outcome_repeat_shadow_errors_total") == errors_before
    after = sum(
        _sample(metric, signal=s, action=a, tool="probe")
        for s in ("same_args", "changed_args")
        for a in ("remind", "trip")
    )
    # 这条脚本确实触发了检测 —— 否则下面的「没有变化」不说明任何事。
    assert after - before >= 8
    assert shadow_final[OUTCOME_REPEATS_KEY]

    monkeypatch.setattr(builder_module, "outcome_repeat_update", lambda *_a, **_k: {})
    plain_final, plain_llm = await _run_graph()

    # 模型的输入:每一次调用的提示词逐条相同。
    assert shadow_llm.calls == plain_llm.calls == len(_script()) + 1
    assert [_shape(p) for p in shadow_llm.seen_prompts] == [
        _shape(p) for p in plain_llm.seen_prompts
    ]
    # 工具结果与历史:逐条相同;没有任何提醒文字。
    assert _shape(shadow_final["messages"]) == _shape(plain_final["messages"])
    assert not any(
        "arguments changed" in str(m.content) for p in shadow_llm.seen_prompts for m in p
    )
    # 路由与收尾:打转次数、出口原因、步数都一样。
    for key in ("loop_trip_count", "exit_reason", "step_count"):
        assert shadow_final.get(key) == plain_final.get(key)
    assert OUTCOME_REPEATS_KEY not in plain_final


# --- 审查修复(M2 / M3 / L1 / L3 / L6 / L9 / L10) ---------------------------------


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("took 7.5 s total", "took 12.25 s total"),
        ("at 2026-11-12 10:00:00 done", "at 2026-11-13 21:04:59 done"),
        ("at 2026-11-13 21:04 done", "at 2026-11-14 08:30 done"),
    ],
)
def test_spotlighted_results_with_spaced_volatile_fields_fingerprint_the_same(
    first: str, second: str
) -> None:
    """M2 —— 工具结果默认被 spotlight 包过:行内空白前插了 ▁,围栏 nonce 每次构建一换。
    先还原再去易变字段,带空格的写法和换了 nonce 的同一结果才认得出。"""
    a = _msg(spotlight_untrusted(_BODY + first, nonce="a1b2c3d4e5f6"))
    b = _msg(spotlight_untrusted(_BODY + second, nonce="0f9e8d7c6b5a"))
    assert result_fingerprint("probe", a) is not None
    assert result_fingerprint("probe", a) == result_fingerprint("probe", b)


def test_spotlighted_results_that_really_differ_stay_different() -> None:
    a = _msg(spotlight_untrusted(_BODY + "value 甲", nonce="a1b2c3d4e5f6"))
    b = _msg(spotlight_untrusted(_BODY + "value 乙", nonce="a1b2c3d4e5f6"))
    assert result_fingerprint("probe", a) != result_fingerprint("probe", b)


def test_a_lone_surrogate_does_not_raise() -> None:
    """M3 —— 孤立代理字符 utf-8 编码会抛;指纹照算。"""
    assert result_fingerprint("probe", _msg(_BODY + "\ud800 tail")) is not None


def test_list_content_with_mixed_key_types_does_not_raise() -> None:
    """M3 —— dict 键 int / str 混用时 ``json.dumps(sort_keys=True)`` 抛 TypeError;回退 ``str``。"""
    message = ToolMessage(
        content=[{"type": "text", "text": _BODY}, {1: "a", "b": 2}],
        tool_call_id="c1",
        name="probe",
    )
    assert result_fingerprint("probe", message) is not None


def test_any_internal_error_is_swallowed_counted_and_logged_by_type_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from orchestrator.graph_builder import outcome_repeat as module

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("secret detail from a tool result")

    monkeypatch.setattr(module, "next_outcomes", _boom)
    before = _sample("expert_work_outcome_repeat_shadow_errors_total")
    with caplog.at_level(logging.WARNING, logger="orchestrator.graph_builder.outcome_repeat"):
        assert outcome_repeat_update({}, [(_call({"q": 1}), _msg(_BODY))]) == {}
    assert _sample("expert_work_outcome_repeat_shadow_errors_total") == before + 1
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in logged
    assert "secret detail" not in logged


async def test_a_failing_hook_leaves_the_run_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """M3 —— 钩子里任何异常都不打断 run:同一脚本,检测内部抛错与检测关掉,逐项相同。"""
    from orchestrator.graph_builder import outcome_repeat as module

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "next_outcomes", _boom)
    failing_final, failing_llm = await _run_graph()
    monkeypatch.setattr(builder_module, "outcome_repeat_update", lambda *_a, **_k: {})
    plain_final, plain_llm = await _run_graph()
    assert failing_llm.calls == plain_llm.calls == len(_script()) + 1
    assert _shape(failing_final["messages"]) == _shape(plain_final["messages"])
    for key in ("loop_trip_count", "exit_reason", "step_count"):
        assert failing_final.get(key) == plain_final.get(key)
    assert OUTCOME_REPEATS_KEY not in failing_final


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("order 123456789012345678 shipped", "order 123456789012345679 shipped"),
        ("snowflake 1859273648102938475 id", "snowflake 1859273648102938476 id"),
        # 16 / 19 位纯数字是正文(卡号、雪花 ID):微秒 / 纳秒时间戳因此故意不去(LOW-3)。
        ("card 6222021234567890 ok", "card 6222021234567891 ok"),
        ("popular in the 1990s here", "popular in the 1980s here"),
        ("there are 100s of files", "there are 200s of files"),
        ("version 3 s build", "version 4 s build"),
    ],
)
def test_ordinary_numbers_are_not_volatile(first: str, second: str) -> None:
    """L1 —— 纯十进制长号码不是十六进制串;年代、数量、版本号后面的 s 不是耗时。"""
    assert result_fingerprint("probe", _msg(_BODY + first)) != result_fingerprint(
        "probe", _msg(_BODY + second)
    )


def test_fingerprinting_reads_only_the_head_and_tail_of_a_huge_result() -> None:
    """L6 —— 超大结果只取头尾各 :data:`FINGERPRINT_EDGE_CHARS` 个字符算指纹(不阻塞事件循环)。"""
    head = "h" * FINGERPRINT_EDGE_CHARS
    tail = "t" * FINGERPRINT_EDGE_CHARS
    a = _msg(head + "middle A " * 50_000 + tail)
    b = _msg(head + "middle B " * 50_000 + tail)
    assert result_fingerprint("probe", a) == result_fingerprint("probe", b)
    c = _msg("x" + head + "middle A " * 50_000 + tail)
    assert result_fingerprint("probe", a) != result_fingerprint("probe", c)
    d = _msg(head + "middle A " * 50_000 + tail + "x")
    assert result_fingerprint("probe", a) != result_fingerprint("probe", d)


def test_a_short_call_id_is_not_removed() -> None:
    """L9 —— 不足 6 个字符的 id 太容易撞上正文里的同样片段,不去。"""
    a = _msg(_BODY + "row c1 value", call_id="c1")
    b = _msg(_BODY + "row c2 value", call_id="c2")
    assert result_fingerprint("probe", a) != result_fingerprint("probe", b)


# --- 没真派发的结果不进账(L3 / L10):在钩子上记下交进来的是哪几条 --------------------


def _record_hook(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    real = builder_module.outcome_repeat_update

    def _recording(state: Any, calls: Any) -> dict[str, Any]:
        seen.extend(str(call.get("id")) for call, _message in calls)
        return real(state, calls)

    monkeypatch.setattr(builder_module, "outcome_repeat_update", _recording)
    return seen


@dataclass
class _Plain:
    name: str
    meta: Mapping[str, Any] | None = None

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description="synthetic")

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        return ToolResult(content=_BODY, meta=dict(self.meta) if self.meta else {})


async def _one_batch(
    registry: ToolRegistry,
    calls: list[dict[str, Any]],
    *,
    configurable: Mapping[str, Any] | None = None,
    **graph_kwargs: Any,
) -> dict[str, Any]:
    llm = _RecordingLLM(responses=[AIMessage(content="", tool_calls=calls)])
    async with make_checkpointer("memory") as cp:
        graph = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=registry, **graph_kwargs)
        )
        return await graph.ainvoke(
            {"messages": [HumanMessage(content="go")], "step_count": 0, "max_steps": 5},
            config={
                "configurable": {
                    "thread_id": str(uuid4()),
                    "run_id": str(uuid4()),
                    "tenant_id": str(uuid4()),
                    **(configurable or {}),
                }
            },
        )


async def test_a_refused_repeat_read_is_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """L3 —— B-268 拒绝的重复读取已经计进打转次数,影子不能再算一遍。"""
    seen = _record_hook(monkeypatch)
    registry = ToolRegistry()
    registry.register(_Plain("refused", meta={LOOP_TRIP_META_KEY: True}))
    registry.register(_Plain("probe"))
    await _one_batch(
        registry,
        [
            {"name": "refused", "args": {}, "id": "tc-refused"},
            {"name": "probe", "args": {}, "id": "tc-probe"},
        ],
    )
    assert seen == ["tc-probe"]


class _BlockOne:
    """前置中间件:拦下名叫 ``blocked`` 的工具。"""

    async def invoke(self, ctx: Any, terminal: Any) -> None:
        if ctx.payload.get("tool_name") == "blocked":
            raise PermissionError("blocked by policy")
        await terminal(ctx)


async def test_a_middleware_blocked_call_is_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _record_hook(monkeypatch)
    registry = ToolRegistry()
    registry.register(_Plain("blocked"))
    registry.register(_Plain("probe"))
    await _one_batch(
        registry,
        [
            {"name": "blocked", "args": {}, "id": "tc-blocked"},
            {"name": "probe", "args": {}, "id": "tc-probe"},
        ],
        before_tool_dispatch_chain=_BlockOne(),
    )
    assert seen == ["tc-probe"]


async def test_a_retyped_url_call_is_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _record_hook(monkeypatch)
    logo = "https://files.example.com/brand/cover-1726394851207.png"
    retyped = logo.replace("1726394851207", "17263948512077")
    registry = ToolRegistry()
    registry.register(_Plain("exec_python"))
    await _one_batch(
        registry,
        [
            {"name": "exec_python", "args": {"code": f"get('{retyped}')"}, "id": "tc-bad"},
            {"name": "exec_python", "args": {"code": "print(1)"}, "id": "tc-ok"},
        ],
        configurable={PROMPT_INPUTS_KEY: {"org_logo": logo}},
    )
    assert seen == ["tc-ok"]


async def test_calls_withheld_on_an_approval_continuation_are_not_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L10 —— 审批续跑时只有被批准的那条真跑;其余由平台作答(没执行),不进账。"""
    from .test_approval_only_approved_call import _call as approval_call
    from .test_approval_only_approved_call import _pause_then_resume

    seen = _record_hook(monkeypatch)
    turn = AIMessage(
        content="",
        tool_calls=[
            approval_call("lookup", {"q": 1}, "tc-0"),
            approval_call("send_email", {"to": "x"}, "tc-1"),
            approval_call("echo", {"q": 2}, "tc-2"),
        ],
    )
    out = await _pause_then_resume(
        turn, ["lookup", "send_email", "echo"], gated=frozenset({"send_email"})
    )
    assert out.tools["send_email"].seen == [{"to": "x"}]
    assert seen == ["tc-1"]
