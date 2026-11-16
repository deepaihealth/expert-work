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


def test_grounded_wording_anchors_content_to_the_user_line_and_the_line_above() -> None:
    """闸门 11-16 + 审查 M2(设计 §11.4 / §5.3):内容要由那一条用户行与它正上方的助手行撑住;发送
    日期与别的用户行可以补时间与指代;只在别的助手行、活动行或工具结果里出现的事实算多加的。"""
    w = base.GROUNDED_WORDING
    assert "the day that line was sent" in w and '"=== new ==="' in w
    assert (
        "Content must be supported by the user line and the assistant line right above it. "
        "Send dates and other user lines may supply time and references. A fact that appears "
        "only in other assistant lines, activity lines or tool results counts as added." in w
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
    assert "The user line is line [2]." in prompt


def test_grounded_prompt_names_the_user_line_when_known() -> None:
    prompt = base.grounded_prompt("q", "a", ["m"], evidence=_EVIDENCE, user_line_no=3)
    head = prompt.split("Memories:\n")[0]
    assert "The user line is line [3]." in head
    assert head.index(_EVIDENCE) < head.index("The user line is line [3].")


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


def _record(rounds: list[dict[str, Any]], content: str) -> dict[str, Any]:
    rows = [{"id": "r", "content": content, "origin": "user_confirmed", "live": True}]
    caps = dict.fromkeys(base.CAPABILITIES, True)
    return {"key": "k", "result": {"capabilities": caps, "memories": rows, "rounds": rounds}}


def _round(
    input_text: str | None, made: list[dict[str, Any]], live_after: list[str]
) -> dict[str, Any]:
    call: dict[str, Any] = {"kind": "pass2", "reply": json.dumps({"memories": made})}
    if input_text is not None:
        call["input"] = input_text
    return {"calls": [call], "live_after": live_after}


def _judge(record: dict[str, Any]) -> base.JudgeItem:
    out: dict[str, Any] = {}
    items: list[base.JudgeItem] = []
    base.confirmed_check(out, items, record, metric="m", key="6.8", lines=("q", "a"))
    (item,) = items
    return item


def test_evidence_match_ignores_surrounding_whitespace_and_reads_the_turn() -> None:
    """审查 LOW:两边去掉首尾空白再比;命中的那一次产出里的 ``turn`` 作为「用户行是第几行」。"""
    made = [{"content": "  the reading is 6.8 \n", "turn": 3}]
    item = _judge(_record([_round(_EVIDENCE, made, ["the reading is 6.8"])], "the reading is 6.8"))
    assert item.evidence == "exact" and "The user line is line [3]." in item.prompt


def test_evidence_fallback_takes_the_first_round_where_the_text_became_live() -> None:
    """审查 LOW:产出里对不上(去重合并改写过)时,取这条正文**第一次**出现在有效记忆里的那一轮,
    不是最后一轮。"""
    first = "=== new ===\n[1] [user] [2026-11-15] first round"
    later = "=== new ===\n[1] [user] [2026-11-16] later round"
    rounds = [
        _round(first, [{"content": "other"}], ["the reading is 6.8"]),
        _round(later, [{"content": "other"}], ["the reading is 6.8"]),
    ]
    item = _judge(_record(rounds, "the reading is 6.8"))
    assert item.evidence == "fallback" and "first round" in item.prompt
    assert "later round" not in item.prompt


def test_evidence_two_lines_when_no_round_recorded_inputs() -> None:
    item = _judge(
        _record([_round(None, [{"content": "the reading is 6.8"}], [])], "the reading is 6.8")
    )
    assert item.evidence == "two-lines" and "[1] [assistant] q\n[2] [user] a" in item.prompt


def test_evidence_is_capped_and_says_what_was_dropped() -> None:
    """审查 LOW:证据有上限;超长的行截短、太早的上下文行丢掉,都写明丢了什么;分隔行以下的行都留着。"""
    long_line = "[1] [assistant] " + "x" * (base.EVIDENCE_LINE_CAP + 50)
    context = "\n".join(f"[{i}] [user] [2026-11-15] c{i}" for i in range(2, 400))
    text = f"{long_line}\n{context}\n=== new ===\n[400] [user] [2026-11-16] 6.8"
    capped = base.cap_evidence(text)
    assert len(capped) <= base.EVIDENCE_CAP + 200
    assert "=== new ===\n[400] [user] [2026-11-16] 6.8" in capped
    assert "earlier context lines dropped" in capped
    # 丢的是最早的上下文:离新段最近的那几行留着
    assert "[399] [user] [2026-11-15] c399" in capped and "] c2\n" not in capped
    short = (
        "[1] [assistant] " + "y" * (base.EVIDENCE_LINE_CAP + 50) + "\n=== new ===\n[2] [user] 6.8"
    )
    assert "characters cut]" in base.cap_evidence(short)
    assert base.cap_evidence(_EVIDENCE) == _EVIDENCE


def test_wording_digest_covers_how_the_evidence_is_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    """审查 LOW:证据的取法与上限算进判官措辞摘要。"""
    import b300_report as rep

    before = rep.a_judge_wording_digest()
    monkeypatch.setattr(base, "EVIDENCE_CAP", base.EVIDENCE_CAP + 1)
    assert rep.a_judge_wording_digest() != before


def test_copied_detail_from_another_assistant_line_is_outside_the_anchor_pair() -> None:
    """审查 M2(合成校准题的样子):细节只在别的助手行里,不在用户行与它正上方那一行里;判官题点名用户行,
    措辞说这种事实算多加的。"""
    evidence = (
        "[1] [assistant] The refund was issued to the original card on 2026-11-10.\n"
        "=== new ===\n"
        "[2] [user] [2026-11-16] Thanks.\n"
        "[3] [assistant] Please confirm: is the return number RT-48213?\n"
        "[4] [user] [2026-11-16] Yes."
    )
    memory = "The user confirmed return RT-48213, refunded to the original card on 2026-11-10."
    prompt = base.grounded_prompt("q", "a", [memory], evidence=evidence, user_line_no=4)
    lines = base.numbered_lines(evidence)
    anchor_pair = lines[3][1] + " " + lines[4][1]
    assert "2026-11-10" in lines[1][1] and "2026-11-10" not in anchor_pair
    assert "The user line is line [4]." in prompt
    assert "only in other assistant lines, activity lines or tool results counts as added" in prompt
