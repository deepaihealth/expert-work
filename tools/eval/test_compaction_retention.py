"""B-165 —— 压缩保留率评测的夹具与判分(不调模型)。

模型那一段只能在测试集群里跑;这里钉住的是「量的东西对不对」:针摆在该摆的地方、只出现在该出现
的角色里、尺寸落在设计的档位、判分按位置与角色记得对。夹具或判分错了,评测会给出看似确定、
实际无关的数字。
"""

from __future__ import annotations

from collections.abc import Sequence

from compaction_retention import (
    FIXTURES,
    HEAD_KEEP,
    TAIL_KEEP,
    Fixture,
    Trial,
    bucket_of,
    build_fixture,
    format_middle_pre_b141,
    needle_code,
    render_table,
    score_text,
)
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from orchestrator.context.compressor import (
    _SUMMARY_INPUT_CHAR_BUDGET,
    _format_middle_for_summary,
    _split,
)


def _middle(fx: Fixture) -> list[BaseMessage]:
    return _split(fx.messages, head_keep=HEAD_KEEP, tail_keep=TAIL_KEEP).middle


def _where(code: str, messages: Sequence[BaseMessage]) -> list[str]:
    """``code`` 出现在哪些位置:``<role>`` 表示正文,``args`` 表示工具调用参数。"""
    hits: list[str] = []
    for msg in messages:
        if code in str(msg.content):
            hits.append(type(msg).__name__)
        if isinstance(msg, AIMessage) and any(code in str(c["args"]) for c in msg.tool_calls):
            hits.append("args")
    return hits


def test_fixture_is_deterministic() -> None:
    a, b = build_fixture("F1", FIXTURES["F1"]), build_fixture("F1", FIXTURES["F1"])
    assert [m.model_dump() for m in a.messages] == [m.model_dump() for m in b.messages]


def test_needles_spread_five_to_ninety_five_percent_with_five_in_mid() -> None:
    fx = build_fixture("F1", FIXTURES["F1"])
    positions = [n.position for n in fx.needles]
    assert len(positions) == 12
    assert len({n.code for n in fx.needles}) == 12
    assert 0.04 <= positions[0] <= 0.06
    assert 0.94 <= positions[-1] <= 0.96
    assert sum(bucket_of(p) == "mid" for p in positions) == 5
    kinds = [n.kind for n in fx.needles]
    assert (
        kinds.count("user"),
        kinds.count("assistant"),
        kinds.count("tool"),
        kinds.count("args"),
    ) == (
        5,
        3,
        2,
        2,
    )


def test_each_needle_lives_only_in_its_role_inside_the_middle() -> None:
    """args 针只在 write_file 参数里(正文里没有 —— 否则测不出「看得到参数」);tool 针在读文件
    结果的前 1,000 字符内(按 1,500 上限截时一定留在头部);开头 / 末尾不含任何针。"""
    for name, steps in FIXTURES.items():
        fx = build_fixture(name, steps)
        middle = _middle(fx)
        expected = {"user": "HumanMessage", "assistant": "AIMessage", "tool": "ToolMessage"}
        for n in fx.needles:
            assert _where(n.code, fx.messages) == _where(n.code, middle)
            if n.kind == "args":
                assert _where(n.code, middle) == ["args"]
                call = next(
                    c
                    for m in middle
                    if isinstance(m, AIMessage)
                    for c in m.tool_calls
                    if n.code in str(c["args"])
                )
                assert call["name"] == "write_file"
            else:
                assert _where(n.code, middle) == [expected[n.kind]]
            if n.kind == "tool":
                result = next(
                    m for m in middle if isinstance(m, ToolMessage) and n.code in str(m.content)
                )
                assert str(result.content).index(n.code) < 1_000


def test_progress_line_last_stage_near_85_percent() -> None:
    fx = build_fixture("F1", FIXTURES["F1"])
    middle = _middle(fx)
    idx = next(i for i, m in enumerate(middle) if fx.next_stage in str(m.content))
    assert fx.last_stage in str(middle[idx].content)
    assert 0.80 <= idx / len(middle) <= 0.90
    assert sum(fx.next_stage in str(m.content) for m in fx.messages) == 1


def test_fixture_sizes_hit_their_design_points() -> None:
    """F1 截完约 14 万(在 16 万预算内,不触发收起);F2 约 30 万(强制走收起)。"""
    f1 = _format_middle_for_summary(_middle(build_fixture("F1", FIXTURES["F1"])), char_budget=10**9)
    f2 = _format_middle_for_summary(_middle(build_fixture("F2", FIXTURES["F2"])), char_budget=10**9)
    assert 130_000 <= len(f1) <= _SUMMARY_INPUT_CHAR_BUDGET
    assert 280_000 <= len(f2) <= 320_000


def test_score_text_buckets_kinds_and_progress() -> None:
    fx = build_fixture("F1", FIXTURES["F1"])
    mid = [n for n in fx.needles if bucket_of(n.position) == "mid"]
    text = " ".join(n.code for n in mid) + f" {fx.last_stage}"
    score = score_text(text, fx)
    assert score.found == 5
    assert score.by_bucket == {"early": (0, 4), "mid": (5, 5), "late": (0, 3)}
    assert sum(found for found, _ in score.by_kind.values()) == 5
    assert score.by_kind["args"][1] == 2
    assert score.progress_ok is False  # 只有最后完成的阶段、没有下一阶段
    assert score_text(text + f" {fx.next_stage}", fx).progress_ok is True


def test_pre_b141_arm_keeps_its_24k_budget_and_loses_the_middle() -> None:
    """A 组拷贝自 880b5a46:总长 24k(加省略标记),中段的中间整块丢掉。"""
    for name, steps in FIXTURES.items():
        fx = build_fixture(name, steps)
        out = format_middle_pre_b141(_middle(fx))
        assert len(out) <= 24_000 + 60
        assert score_text(out, fx).by_bucket["mid"][0] == 0


def test_current_arm_input_sees_every_needle_on_f1() -> None:
    """第 0 层:B 组(当前代码)在 F1 上,12 根针全部进了摘要模型的输入。"""
    fx = build_fixture("F1", FIXTURES["F1"])
    assert score_text(_format_middle_for_summary(_middle(fx)), fx).found == 12


def test_render_table_one_row_per_fixture_arm_and_token_totals() -> None:
    fx = build_fixture("F1", FIXTURES["F1"])
    s = score_text(needle_code(0), fx)
    trials = [
        Trial("F1", arm, rep, 100, s, 50, s, 1_000, 200, "m")
        for arm in ("A", "B")
        for rep in range(2)
    ]
    table = render_table(trials)
    rows = [line for line in table.splitlines() if line.startswith("F1")]
    assert len(rows) == 2
    assert "total tokens: input 4,000 / output 800" in table
