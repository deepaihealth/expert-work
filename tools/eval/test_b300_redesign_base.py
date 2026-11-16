"""B-300 重做(设计 2026-11-15 §11)评测的共用判法:关键串、库里存下的行、平台字段在不在、
第二次提炼的空列表、带编号的输入、判官的「有据程度」与「是不是一次性事件」提示词。离线。"""

from __future__ import annotations

import json
from typing import Any

import b300_redesign_base as base
import pytest


def _reply(*contents: str, basis: str | None = None) -> str:
    rows = []
    for c in contents:
        row: dict[str, Any] = {"kind": "fact", "content": c}
        if basis is not None:
            row["basis"] = basis
        rows.append(row)
    return json.dumps({"memories": rows}, ensure_ascii=False)


def test_numeric_keys_do_not_match_inside_longer_numbers() -> None:
    """数字的关键串按数字边界比:「42」不能撞上「142」或日期「2026-11-42」以外的数位。"""
    assert base.has_key("今天上午待处理 42 张", "42")
    assert not base.has_key("今天上午待处理 142 张", "42")
    assert not base.has_key("count 4210", "42")
    assert base.has_key("weight 71.2 kg", "71.2")
    assert not base.has_key("weight 171.25 kg", "71.2")
    # 非数字的关键串照旧:去空白、小写、全角半角折叠后做包含
    assert base.has_key("Dana  Whitfield handles it", "danawhitfield")
    assert base.has_key("型号 \uff2b\uff38\uff0d\uff13\uff10\uff15", "kx-305")  # 全角
    assert not base.has_key("anything", "")


def test_capability_reads_the_record_and_old_records_are_unknown() -> None:
    assert base.capability({"capabilities": {"user_confirmed": True}}, "user_confirmed") is True
    assert base.capability({"capabilities": {"user_confirmed": False}}, "user_confirmed") is False
    assert base.capability({}, "user_confirmed") is None
    with pytest.raises(KeyError):
        base.capability({}, "no-such-field")


def test_stored_rows_filter_by_key_liveness_and_origin() -> None:
    result = {
        "memories": [
            {"id": "1", "content": "x is 600", "origin": "user_confirmed", "live": True},
            {"id": "2", "content": "x is 600", "origin": "assistant_derived", "live": False},
            {"id": "3", "content": "other", "origin": "user_stated", "live": True},
        ]
    }
    assert [m["id"] for m in base.stored(result, "600")] == ["1", "2"]
    assert [m["id"] for m in base.stored(result, "600", live_only=True)] == ["1"]
    assert [m["id"] for m in base.stored(result, "600", origins=base.USER_SIDE)] == ["1"]


def test_user_side_includes_user_confirmed() -> None:
    assert {"user_stated", "user_confirmed", "user_corrected"} <= base.USER_SIDE
    assert "assistant_derived" not in base.USER_SIDE


def _rounds(*per_round: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "rounds": [
            {"after_turn": i, "calls": [{"kind": k, "reply": r} for k, r in calls]}
            for i, calls in enumerate(per_round)
        ]
    }


def test_pass2_empty_is_about_one_round() -> None:
    result = _rounds(
        [("pass1", _reply()), ("pass2", _reply("a"))],
        [("pass1", _reply()), ("pass2", _reply())],
    )
    assert base.pass2_empty(result, 0) is False
    assert base.pass2_empty(result, 1) is True


def test_pass2_empty_counts_a_missing_pass2_and_split_chunks() -> None:
    """没调第二次也算「第二次没产出」;第 2 次尝试对半分块后有两次第二次,全空才算空。"""
    assert base.pass2_empty(_rounds([("pass1", _reply("a"))]), 0) is True
    two = _rounds([("pass2", _reply()), ("pass2", _reply("b"))])
    assert base.pass2_empty(two, 0) is False
    assert base.pass2_empty(_rounds([("pass2", _reply()), ("pass2", _reply())]), 0) is True
    assert base.pass2_empty(_rounds([("pass2", _reply())]), 5) is None  # 没有这一轮


def test_numbered_lines_parse_the_new_input_and_old_input_has_none() -> None:
    text = (
        "[1] [user] [2026-03-02] 帮我看看\n[2] [assistant] [2026-03-02] 回程想订哪天?\n"
        "=== new ===\n[3] [user] [2026-03-02] 18号\n[4] [attached data] images: 1\n"
    )
    lines = base.numbered_lines(text)
    assert lines[3] == ("user", "[2026-03-02] 18号")
    assert lines[2][0] == "assistant" and lines[4][0] == "attached data"
    assert base.numbered_lines("[user] 18号\n[assistant] 好的") == {}


def test_grounded_prompt_carries_the_two_lines_the_scale_and_the_memories() -> None:
    prompt = base.grounded_prompt("What is the limit?", "600 calls", ["The limit is 600 calls."])
    head = prompt.split("Memories:\n")[0]
    assert "What is the limit?" in head and "600 calls" in head
    assert base.GROUNDED_WORDING in head and head.index(base.GROUNDED_WORDING) < head.index(
        "Question:"
    )
    assert "- The limit is 600 calls." in prompt
    # 判官只判内容:提示词不问来源 / 出处
    assert "source" not in base.GROUNDED_WORDING.lower()


def test_event_date_prompt_names_the_date_and_the_content() -> None:
    prompt = base.event_date_prompt("The meter read 18342.", "2026-09-01")
    assert "2026-09-01" in prompt and "The meter read 18342." in prompt
    assert base.EVENT_DATE_WORDING in prompt


def test_platform_text_has_no_domain_words() -> None:
    """判官措辞也是平台以外的文字,但照设计 §10 的词表自检:不带领域词。"""
    words = (
        "health medical patient blood glucose pressure weight doctor clinic coach customer "
        "client sales legal contract lawyer insurance"
    ).split()
    for text in (base.GROUNDED_WORDING, base.EVENT_DATE_WORDING):
        assert not [w for w in words if w in text.lower()], text


# ------------------------------------------------------------ 有据程度的证据(闸门 11-16)

_EVIDENCE = (
    "[1] [user] [2026-11-15] Let's complete the records.\n"
    "[2] [assistant] What is the reading?\n"
    "=== new ===\n"
    "[3] [user] [2026-11-16] 6.8\n"
    "[4] [assistant] OK, I have the reading as 6.8."
)


def test_grounded_wording_says_stamped_dates_and_shown_context_are_not_added() -> None:
    """闸门 11-16:判官只看两行、看不到发送日期与上文,把正当的日期和上文细节当成「多加的」。"""
    w = base.GROUNDED_WORDING
    assert "the day that line was sent" in w and '"=== new ==="' in w
    assert (
        "A date taken from a line's send date, and details stated in any shown line, context "
        "lines included, are not added content." in w
    )


def test_grounded_prompt_shows_the_extractor_evidence_when_given() -> None:
    memory = ["The reading was 6.8 on 2026-11-16."]
    prompt = base.grounded_prompt("q", "a", memory, evidence=_EVIDENCE)
    head = prompt.split("Memories:\n")[0]
    assert _EVIDENCE in head and "[2026-11-16]" in head and "=== new ===" in head
    assert head.index(_EVIDENCE) < head.index(base.GROUNDED_WORDING) < head.index("Question:")


def test_grounded_prompt_without_evidence_numbers_the_two_lines() -> None:
    prompt = base.grounded_prompt("What is the limit?", "600 calls", ["m"])
    assert "=== new ===\n[1] [assistant] What is the limit?\n[2] [user] 600 calls" in prompt


def test_confirmed_check_uses_the_pass2_input_of_the_round_that_wrote_the_row() -> None:
    other = "=== new ===\n[1] [user] [2026-11-14] earlier round"
    rows = [{"id": "r", "content": "the reading is 6.8", "origin": "user_confirmed", "live": True}]
    rounds: list[dict[str, Any]] = [
        {"calls": [{"kind": "pass2", "input": other, "reply": '{"memories": []}'}]},
        {
            "calls": [
                {
                    "kind": "pass2",
                    "input": _EVIDENCE,
                    "reply": '{"memories": [{"content": "the reading is 6.8"}]}',
                }
            ]
        },
        {"calls": [{"kind": "pass2", "input": other, "reply": '{"memories": []}'}]},
    ]
    caps = dict.fromkeys(base.CAPABILITIES, True)
    record = {"key": "k", "result": {"capabilities": caps, "memories": rows, "rounds": rounds}}
    out: dict[str, Any] = {}
    items: list[base.JudgeItem] = []
    base.confirmed_check(out, items, record, metric="m", key="6.8", lines=("q", "a"))
    (item,) = items
    assert _EVIDENCE in item.prompt and "earlier round" not in item.prompt
    # 记录里没有提炼输入(老记录)→ 退回两行
    for rnd in rounds:
        for c in rnd["calls"]:
            c.pop("input")
    items = []
    base.confirmed_check(out, items, record, metric="m", key="6.8", lines=("q", "a"))
    assert "[1] [assistant] q\n[2] [user] a" in items[0].prompt
