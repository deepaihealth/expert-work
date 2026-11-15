"""B-300 重做(设计 ``docs/superpowers/specs/2026-11-15-b300-extraction-redesign-design.md`` §11)
评测的共用判法。纯函数,不调模型、不 import 平台包。

* 关键串(:func:`has_key`):去空白、小写、全角半角折叠后做包含;**纯数字的关键串按数字边界比**
  (「42」不撞「142」)—— 读数、数量类的场景靠它。
* 库里存下的行(:func:`stored`)与平台字段在不在(:func:`capability`):重做加的字段
  (``user_confirmed``、证据指针、事件日期、确认日期、切片起点)在老代码上没有,依赖它们的指标
  报「incomplete」,不当成 0 或失败(回归闸门要先在老代码上跑出基线)。
* 第二次提炼的空列表(:func:`pass2_empty`)与带编号的输入(:func:`numbered_lines`)。
* 判官提示词:内容「有据程度」(2602.01450 的五档,≥ 4 算对,§11.4;只判内容,来源读字段)与
  「是不是一次性事件」(§11.3「不是事件却被标了事件日期」)。措辞通用,不带领域词。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from b300_live import parse_extraction


@dataclass(frozen=True)
class JudgeItem:
    """送判官的一项(``b300_report`` 沿用这个名字)。"""

    key: str
    check: str
    prompt: str


#: 用户一方的来源(设计 §6.2:用户更正、用户说的、用户确认)。``user_corrected`` 不是列值(更正靠
#: 置信度 1.0),这里只为对得上平台的 ``USER_SIDE_ORIGINS``。
USER_SIDE = frozenset({"user_stated", "user_confirmed", "user_corrected"})

#: 重做加的平台字段(设计 §9.1 / §8)→ 记录里 ``capabilities`` 的键。
CAPABILITIES = ("user_confirmed", "evidence_pointer", "event_date", "confirmed_at", "slicing")

_NUMERIC = re.compile(r"^[0-9]+(?:[.,][0-9]+)*$")


def _norm(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).split()).lower()


def has_key(text: str, key: str) -> bool:
    """``key`` 出现在 ``text`` 里(规范化后);纯数字的 ``key`` 前后不能紧挨数字或小数点。"""
    if not key:
        return False
    body, needle = _norm(text), _norm(key)
    if _NUMERIC.match(needle):
        return re.search(rf"(?<![0-9.]){re.escape(needle)}(?![0-9]|\.[0-9])", body) is not None
    return needle in body


def capability(result: Mapping[str, Any], name: str) -> bool | None:
    """这条记录跑的代码有没有这个字段;老记录没记 = ``None``(不知道)。"""
    if name not in CAPABILITIES:
        raise KeyError(name)
    caps = result.get("capabilities")
    if not isinstance(caps, Mapping) or name not in caps:
        return None
    return bool(caps[name])


def stored(
    result: Mapping[str, Any],
    key: str,
    *,
    live_only: bool = False,
    origins: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """库里含 ``key`` 的行(默认含已失效的;``origins`` 给了就只要这些来源)。"""
    allowed = None if origins is None else set(origins)
    return [
        m
        for m in result.get("memories", [])
        if has_key(str(m.get("content", "")), key)
        and (not live_only or m.get("live"))
        and (allowed is None or m.get("origin") in allowed)
    ]


def round_calls(result: Mapping[str, Any], round_index: int) -> list[dict[str, Any]] | None:
    rounds = result.get("rounds", [])
    if round_index >= len(rounds):
        return None
    return list(rounds[round_index].get("calls", []))


def pass_memories(
    result: Mapping[str, Any], kind: str, round_index: int | None = None
) -> list[dict[str, Any]]:
    """某一次提炼(``pass1`` / ``pass2``)的产出;``round_index`` 给了就只看那一次写回。"""
    rounds = result.get("rounds", [])
    picked = rounds if round_index is None else rounds[round_index : round_index + 1]
    return [
        m
        for r in picked
        for c in r.get("calls", [])
        if c.get("kind") == kind
        for m in parse_extraction(str(c.get("reply", "")))
    ]


def pass2_empty(result: Mapping[str, Any], round_index: int) -> bool | None:
    """这一次写回里第二次提炼什么都没产出(返回空列表,或根本没调);没有这一轮 = ``None``。
    第 2 次尝试会对半分块、调两次第二次 —— 全空才算空。"""
    calls = round_calls(result, round_index)
    if calls is None:
        return None
    replies = [c for c in calls if c.get("kind") == "pass2"]
    return not any(parse_extraction(str(c.get("reply", ""))) for c in replies)


_LINE = re.compile(r"^\[(\d+)\] \[([a-z][a-z ]*)\] ?(.*)$")


def numbered_lines(text: str) -> dict[int, tuple[str, str]]:
    """重做后的提炼输入(``[n] [tag] ...``,设计 §5.1)→ {行号: (标签, 其余文字)};老输入没有编号。"""
    out: dict[int, tuple[str, str]] = {}
    for line in text.splitlines():
        match = _LINE.match(line.strip())
        if match:
            out[int(match.group(1))] = (match.group(2), match.group(3))
    return out


# --------------------------------------------------------------------------- 判官

#: 内容「有据程度」(2602.01450 的五档;§11.4:≥ 4 算对)。只判内容 —— 来源由程序读字段。
GROUNDED_WORDING = (
    "Rate how well a memory is supported by the two conversation lines on five levels: "
    "5 = everything it says is stated in the lines or follows directly from reading them "
    "together; 4 = supported, with only harmless rewording or obvious context added; 3 = partly "
    "supported, something is added, dropped or changed that matters; 2 = mostly not supported; "
    "1 = contradicts the lines or invents content. Keep any doubt the lines express: a memory "
    "that states as certain what the lines leave uncertain is at most level 3."
)
#: §11.3「不是事件却被标了事件日期」:判官判这条带事件日期的记忆是不是一次性的事。
EVENT_DATE_WORDING = (
    "A one-time event is a value that happened or was observed on one day, such as a reading, "
    "a count or a payment made. Plans, arrangements, preferences, states, identifiers, names "
    "and lasting facts are not one-time events, even when a date is written in them."
)


def _bullets(rows: Sequence[str]) -> str:
    return "\n".join(f"- {r}" for r in rows) or "- (none)"


def grounded_prompt(assistant_line: str, user_line: str, memories: Sequence[str]) -> str:
    return (
        f"Conversation lines:\n[assistant] {assistant_line}\n[user] {user_line}\n\n"
        f"{GROUNDED_WORDING} Question: does any memory below reach level 4 or 5? yes = it does. "
        "Give the level you chose in the reason.\n"
        f"Memories:\n{_bullets(memories)}"
    )


def confirmed_check(
    out: dict[str, Any],
    items: list[JudgeItem],
    record: Mapping[str, Any],
    *,
    metric: str,
    key: str,
    lines: tuple[str, str],
    extra_ok: bool = True,
) -> None:
    """存成 ``user_confirmed``(**读字段**)且判官判「有据程度 ≥ 4」(§11.3 / §11.4)。``lines`` =
    (助手那一行, 用户那一行)。平台没有这个来源值 → ``out["missing"][metric]``(incomplete);
    没存成用户确认、或 ``extra_ok`` 不成立 → ``False``(不送判官);否则 ``None`` + 一项待判。"""
    result = record["result"]
    if not capability(result, "user_confirmed"):
        out.setdefault("missing", {})[metric] = "user_confirmed"
        return
    rows = stored(result, key, live_only=True, origins={"user_confirmed"})
    if not rows or not extra_ok:
        out[metric] = False
        return
    out[metric] = None
    prompt = grounded_prompt(lines[0], lines[1], [str(m["content"]) for m in rows])
    items.append(JudgeItem(f"{record['key']}|{metric}", metric, prompt))


def event_date_prompt(content: str, occurred_on: str) -> str:
    return (
        f'A memory was stored with event date {occurred_on}:\n"{content}"\n\n{EVENT_DATE_WORDING} '
        "Question: is this memory NOT about a one-time event? yes = it is not."
    )
