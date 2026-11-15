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
