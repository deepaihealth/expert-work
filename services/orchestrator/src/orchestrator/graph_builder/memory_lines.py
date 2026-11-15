"""B-300 重做 PR X —— 提炼输入的行:编号、``=== new ===`` 标记、程序对 ``basis`` / ``turn`` /
``event_date`` 的核对与证据指针(设计 §4、§5.1、§5.4、§7.4)。

两次提炼各自一份带编号的输入。每行记着它来自哪条消息(id + 在整段对话里的序号)、发送时间、在不在
``=== new ===`` 之上(``context``,上下文行;切片是 PR S 的事,之前没有上下文行),以及在不在本次
写回负责的轮次里(``fresh``,交给去重合并的 ``in_new_segment``)。

程序从不因为指针不对而丢掉一条记忆,只会让标签变弱或指针留空(§4 末)。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import cast

from expert_work.persistence.memory.base import (
    ASSISTANT_DERIVED_MAX_CONFIDENCE,
    USER_CONFIRMED_MAX_CONFIDENCE,
)
from expert_work.protocol.memory_item import MemoryOrigin

#: 上下文行与新行的分界(附录 A / C 的原文)。
NEW_SEGMENT_MARK = "=== new ==="

USER_TAG = "[user]"
ASSISTANT_TAG = "[assistant]"

#: 第二次提炼里表示「用户对助手上一句的回答 / 同意」的 ``basis``(§5.2)。
_USER_BASES = frozenset({"user_answer", "user_agreement"})

_ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class ExtractionLine:
    """给提炼模型的一行(编号由 :func:`render_lines` 按位置加)。

    ``tag``:第二次的 ``[user]`` / ``[task]`` / ``[attached data]`` / ``[activity]`` /
    ``[assistant]``;第一次为空。``sent_day`` 只在要显示日期的行上有(按智能体时区);
    ``sent_at`` 是同一个发送时刻。"""

    text: str
    tag: str = ""
    sent_at: datetime | None = None
    sent_day: date | None = None
    message_id: str | None = None
    message_index: int | None = None
    #: 在本次写回负责的轮次里(切片之前 = 最后一轮)。
    fresh: bool = False
    #: 在 ``=== new ===`` 之上(PR S 的上下文行)。
    context: bool = False

    def body(self) -> str:
        parts = [self.tag, f"[{self.sent_day.isoformat()}]" if self.sent_day else "", self.text]
        return " ".join(part for part in parts if part)


def render_lines(lines: Sequence[ExtractionLine]) -> str:
    """``[n] 正文``,编号从 1 起、上下文行与新行连续;上下文行之后一行 ``=== new ===``。"""
    out: list[str] = []
    marked = False
    for number, line in enumerate(lines, start=1):
        if not line.context and not marked:
            out.append(NEW_SEGMENT_MARK)
            marked = True
        out.append(f"[{number}] {line.body()}")
    if not marked:
        out.append(NEW_SEGMENT_MARK)
    return "\n".join(out)


def cap_lines(lines: Sequence[ExtractionLine], cap: int) -> tuple[list[ExtractionLine], bool]:
    """B-300 D3 —— 总量上限:从最近的一行往前收,超了就停(更早的丢掉)。每行按带编号的样子量
    (编号按最宽的算),标记一行也算。返回 ``(留下的行, 是否超了)``。"""
    prefix = len(f"[{len(lines)}] ")
    used = len(NEW_SEGMENT_MARK)
    kept: list[ExtractionLine] = []
    for line in reversed(lines):
        used += 1 + prefix + len(line.body())
        if used > cap:
            return kept[::-1], True
        kept.append(line)
    return kept[::-1], False


def evidence_pointer(line: ExtractionLine | None) -> tuple[str | None, int | None]:
    """证据指针 ``(消息 id, 序号)``;序号只收非负整数(不知道就留空)。"""
    if line is None:
        return None, None
    index = line.message_index
    return line.message_id, index if index is not None and index >= 0 else None


@dataclass(frozen=True)
class Settled:
    """程序对一条提炼结果的核对结论(§5.4)。``checks`` 是要计数的事(降级原因、指针留空、
    事件日期置空)。"""

    origin: MemoryOrigin
    max_confidence: float | None
    evidence_message_id: str | None
    evidence_message_index: int | None
    event_date: date | None
    line_at: datetime | None
    in_new_segment: bool
    checks: tuple[str, ...] = ()


def _line_of(turn: object, lines: Sequence[ExtractionLine]) -> ExtractionLine | None:
    if isinstance(turn, bool) or not isinstance(turn, int) or not 1 <= turn <= len(lines):
        return None
    return lines[turn - 1]


def _event_date(claimed: object, line: ExtractionLine | None, today: date) -> date | None:
    """能解析成 ``YYYY-MM-DD``、不晚于那一行的发送日期(那一行没有日期时不查这一条),且不晚于
    智能体时区的「今天」(``turn`` 挂不上、那一行没日期时也查;审查 L3)。"""
    if not isinstance(claimed, str) or _ISO_DAY.fullmatch(claimed) is None:
        return None
    try:
        day = date.fromisoformat(claimed)
    except ValueError:  # 形状对、日子不存在(2 月 30 日)
        return None
    if day > today:
        return None
    if line is not None and line.sent_day is not None and day > line.sent_day:
        return None
    return day


def _downgrade_reason(turn: object, lines: Sequence[ExtractionLine]) -> str | None:
    """用户回答 / 同意挂不挂得上(§5.4):新段里的 ``[user]`` 行,上一行是 ``[assistant]``
    (上一行可以是上下文行)。挂不上 → 原因。"""
    line = _line_of(turn, lines)
    if line is None:
        return "out_of_range"
    if line.context:
        return "context_line"
    if line.tag != USER_TAG:
        return "not_user_line"
    position = cast(int, turn) - 1
    if position == 0 or lines[position - 1].tag != ASSISTANT_TAG:
        return "previous_not_assistant"
    return None


def _settle(
    origin: MemoryOrigin,
    max_confidence: float | None,
    turn: object,
    claimed_event_date: object,
    lines: Sequence[ExtractionLine],
    today: date,
    downgraded: tuple[str, ...] = (),
) -> Settled:
    line = _line_of(turn, lines)
    new_line = line if line is not None and not line.context else None
    pointer = evidence_pointer(new_line)
    event_date = _event_date(claimed_event_date, line, today)
    checks = (
        *downgraded,
        *(("pointer_missing",) if new_line is None else ()),
        *(("event_date_dropped",) if claimed_event_date is not None and event_date is None else ()),
    )
    return Settled(
        origin=origin,
        max_confidence=max_confidence,
        evidence_message_id=pointer[0],
        evidence_message_index=pointer[1],
        event_date=event_date,
        line_at=line.sent_at if line is not None else None,
        in_new_segment=new_line is not None and new_line.fresh,
        checks=checks,
    )


def settle_user_text(
    turn: object, claimed_event_date: object, lines: Sequence[ExtractionLine], *, today: date
) -> Settled:
    """第一次提炼:来源永远是 ``user_stated``(第一次只看得到用户的字),与 ``turn`` 对不对无关。"""
    return _settle("user_stated", None, turn, claimed_event_date, lines, today)


def settle_conversation(
    basis: object,
    turn: object,
    claimed_event_date: object,
    lines: Sequence[ExtractionLine],
    *,
    today: date,
) -> Settled:
    """第二次提炼:``basis`` 为用户回答 / 同意且挂得上 → ``user_confirmed``(≤ 0.8);其余一律
    ``assistant_derived``(≤ 0.6),挂不上的计一次降级与原因。"""
    downgraded: tuple[str, ...] = ()
    if basis in _USER_BASES:
        reason = _downgrade_reason(turn, lines)
        if reason is None:
            return _settle(
                "user_confirmed",
                USER_CONFIRMED_MAX_CONFIDENCE,
                turn,
                claimed_event_date,
                lines,
                today,
            )
        downgraded = (f"downgraded_{reason}",)
    return _settle(
        "assistant_derived",
        ASSISTANT_DERIVED_MAX_CONFIDENCE,
        turn,
        claimed_event_date,
        lines,
        today,
        downgraded,
    )
