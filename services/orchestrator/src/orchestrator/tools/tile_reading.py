"""B-266 ``region="auto"`` —— 每块回答的解析、读数归一与保守汇总。

每块按提示词回一小段 JSON(``{"status", "label", "value", "unit"}``,所有厂商都只靠提示词,
不用各家的结构化输出:GLM-V 没有 ``response_format``,Qwen-VL 带图时会降级它)。实测(豆包,
2x3 块,24 题 x 2 次):94% 读对,236 块不含目标的没有一块报「找到」,288 次回答全是合法 JSON。

**解析阶梯**(每块一样):去 ``<think>…</think>`` 与代码围栏 → ``json.loads`` → 第一个配平的
``{…}`` → 字段校验与交叉核对 → 都不是 JSON 才走散文兜底(见 :func:`parse_tile_reply`)。

**汇总宁可报冲突,不可错合并**:读数先归一成带类型的 :class:`Reading`(比较符、数、范围、
升降标记、单位写法、定性等级),两两判「相容」;任意两个不相容就是冲突 —— **不投票**。单位只
统一写法(大小写、μ/µ/u、``x10^9/L`` 的几种写法),**从不换算**。

平台文字(提示词、给主模型的汇总)一律英文、零租户内容;中文词表写成 ``\\u`` 转义。
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Final, Literal

from orchestrator.tools.region_zoom import Box, Tile

Status = Literal["found", "not_found", "partial", "unparseable"]
ParseMode = Literal["json", "prose", "none"]
AutoOutcome = Literal["single", "conflict", "none"]

#: 每块的提示词在用户原问题后面追加这一段;问题本身原样不动。
TILE_INSTRUCTION: Final = (
    "This image is one tile cut from a larger image, so the answer may not be in it. "
    "Reply only with JSON, nothing else: "
    '{"status": "found|not_found|partial", '
    '"label": "<the row or field name printed next to the value, or empty>", '
    '"value": "<the value exactly as printed, keeping marks such as < > \u2191 \u2193 \u00b1 (-); '
    'empty unless found>", '
    '"unit": "<the unit as printed, or empty>"}. '
    'Use "not_found" if the requested item or its value is not visible in this tile. '
    'Use "partial" if the value is cut off by the edge of the tile. Do not guess.'
)

#: 交给主模型 / 记进 meta 的原文最多保留多少字符。
RAW_META_CHARS: Final = 200
#: 散文兜底:不超过这么多字符、带数字或定性词的回答才算一个值。
PROSE_VALUE_MAX_CHARS: Final = 40
_STATUSES: Final = frozenset({"found", "not_found", "partial"})


def tile_question(question: str) -> str:
    """一块的提示词:用户的问题原样,后面加 :data:`TILE_INSTRUCTION`。"""
    return f"{question}\n\n{TILE_INSTRUCTION}"


# ---------------------------------------------------------------------------
# 解析阶梯
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TileReply:
    """一块的回答解析之后的样子。``status == "unparseable"`` 的永远不算一个值。"""

    status: Status
    raw: str
    parse_mode: ParseMode
    label: str = ""
    value: str = ""
    unit: str = ""
    #: 交叉核对改判时记一句(``found_without_value`` / ``not_found_with_value`` ...)。
    note: str = ""


_THINK_RE: Final = re.compile(r"<think>.*?(?:</think>|\Z)", re.DOTALL | re.IGNORECASE)
_FENCE_RE: Final = re.compile(r"```(?:json|JSON)?")


def parse_tile_reply(text: str) -> TileReply:
    """一块的原始回答 → :class:`TileReply`(解析阶梯见模块 docstring)。"""
    raw = text.strip()
    cleaned = _FENCE_RE.sub("", _THINK_RE.sub("", raw)).strip()
    obj = _json_object(cleaned)
    if obj is not None:
        return _from_json(obj, raw)
    return _from_prose(cleaned, raw)


def _json_object(text: str) -> dict[str, object] | None:
    try:
        loaded = json.loads(text)
    except ValueError:
        loaded = None
    if isinstance(loaded, dict):
        return loaded
    candidate = _first_balanced_object(text)
    if candidate is None:
        return None
    try:
        loaded = json.loads(candidate)
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _first_balanced_object(text: str) -> str | None:
    """第一个配平的 ``{…}``(跳过字符串里的花括号);没有就 ``None``。"""
    start = text.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        start = text.find("{", start + 1)
    return None


def _field(obj: dict[str, object], key: str) -> str:
    value = obj.get(key)
    if value is None:
        return ""
    return str(value).strip()


def _from_json(obj: dict[str, object], raw: str) -> TileReply:
    status = _field(obj, "status").casefold()
    label, value, unit = _field(obj, "label"), _field(obj, "value"), _field(obj, "unit")
    if status not in _STATUSES:
        return TileReply(status="unparseable", raw=raw, parse_mode="json", note="bad_status")
    if status == "found" and (not value or _says_not_found(value)):
        note = "found_without_value" if not value else "found_with_not_found_value"
        return TileReply(status="not_found", raw=raw, parse_mode="json", label=label, note=note)
    if status == "not_found":
        note = "not_found_with_value" if value else ""
        return TileReply(status="not_found", raw=raw, parse_mode="json", label=label, note=note)
    return TileReply(
        status="found" if status == "found" else "partial",
        raw=raw,
        parse_mode="json",
        label=label,
        value=value if status == "found" else "",
        unit=unit if status == "found" else "",
    )


#: 散文兜底:回答里**任何位置**出现这个记号就是「没看到」。
_NOT_FOUND_TOKEN_RE: Final = re.compile(r"not[\s_]*found", re.IGNORECASE)
#: 散文兜底:**整句**(去掉标点、引号后)命中其中一条才算「没看到」。全是「看得见 / 有没有」
#: 的说法 —— 「未检出」「阴性」「无异常」这类是化验结果,一律不在这里。
_NOT_FOUND_LEXICON: Final = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?:\u672a\u627e\u5230|\u6ca1\u6709\u627e\u5230|\u627e\u4e0d\u5230).{0,20}",
        r"(?:\u56fe\u4e2d|\u8be5\u533a\u57df|\u6b64\u56fe\u5757|\u8be5\u56fe\u5757|\u672c\u56fe\u5757|\u56fe\u7247|\u8fd9\u5f20\u56fe)\u4e2d?(?:\u6ca1\u6709|\u672a)(?:\u663e\u793a|\u5305\u542b|\u51fa\u73b0|\u627e\u5230)?.{0,20}",
        r"(?:\u770b\u4e0d\u5230|\u770b\u4e0d\u6e05).{0,20}",
        r"\u65e0\u6cd5(?:\u8bc6\u522b|\u770b\u6e05|\u8bfb\u53d6|\u786e\u5b9a|\u627e\u5230).{0,20}",
        r".{0,20}(?:\u4e0d\u5b8c\u6574|\u88ab\u622a\u65ad|\u88ab\u88c1\u5207).{0,20}",
        r"(?:the )?(?:value |item )?(?:is )?not (?:visible|shown).{0,40}",
        r"(?:i )?(?:cannot|can ?t|can not) (?:see|read|find).{0,40}",
        r"n ?/? ?a",
        r"\u65e0|\u6ca1\u6709",
    )
)
_PUNCT_RE: Final = re.compile(
    r"[\s\"'`*_.,:;!?()\[\]\u3002\uff0c\uff1a\uff1b\uff01\uff1f\u201c\u201d\u2018\u2019\uff08\uff09\u3001]+"
)
#: 一个值得有的东西:数字,或者定性词 / 符号。
_QUALITATIVE_HINT_RE: Final = re.compile(
    r"\d|[+\-\u00b1]|\u9634\u6027|\u9633\u6027|\u672a\u68c0\u51fa"
    r"|\u672a\u89c1\u5f02\u5e38|\u65e0\u5f02\u5e38|\u6b63\u5e38|negative"
    r"|positive|not detected|trace",
    re.IGNORECASE,
)


def _flat(text: str) -> str:
    return _PUNCT_RE.sub(" ", unicodedata.normalize("NFKC", text)).strip().casefold()


def _says_not_found(text: str) -> bool:
    """散文里的「没看到 / 看不全」:``NOT_FOUND`` 记号在任何位置,或整句命中词表。"""
    if _NOT_FOUND_TOKEN_RE.search(text):
        return True
    flat = _flat(text)
    return bool(flat) and any(pattern.fullmatch(flat) for pattern in _NOT_FOUND_LEXICON)


def _from_prose(text: str, raw: str) -> TileReply:
    if not text:
        return TileReply(status="unparseable", raw=raw, parse_mode="none")
    if _says_not_found(text):
        return TileReply(status="not_found", raw=raw, parse_mode="prose")
    if len(text) <= PROSE_VALUE_MAX_CHARS and _QUALITATIVE_HINT_RE.search(text):
        return TileReply(status="found", raw=raw, parse_mode="prose", value=text)
    return TileReply(status="unparseable", raw=raw, parse_mode="none")


# ---------------------------------------------------------------------------
# 读数归一
# ---------------------------------------------------------------------------

ReadingKind = Literal["number", "comparator", "range", "qualitative", "text"]


@dataclass(frozen=True)
class Reading:
    """一个值归一之后的样子(只为判相容;给主模型看的仍是原文)。"""

    kind: ReadingKind
    numbers: tuple[Decimal, ...] = ()
    comparator: str = ""
    #: ``"up"`` / ``"down"`` / ``"mark"``(``*``)。
    flags: frozenset[str] = field(default_factory=frozenset)
    #: 统一写法后的单位;没有是 ``""``。
    unit: str = ""
    #: 定性等级:``neg`` / ``weak`` / ``pos`` / ``pos2`` / ``pos3`` / ``pos4``。
    qualitative: str = ""
    text: str = ""


_MINUS_RE: Final = re.compile("[\u2212\u2012\ufe63\uff0d]")
_RANGE_RE: Final = re.compile(r"(\d)\s*(?:~|\u301c|\u81f3|\u2013|\u2014|-)\s*(?=\d)")
_THOUSANDS_RE: Final = re.compile(r"(?<![\d.])\d{1,3}(?:,\d{3})+(?![\d,])")
_COMPARATORS: Final = (
    ("<=", "<="),
    (">=", ">="),
    ("\u2264", "<="),
    ("\u2265", ">="),
    ("<", "<"),
    (">", ">"),
    ("\u5c0f\u4e8e", "<"),
    ("\u4f4e\u4e8e", "<"),
    ("\u5927\u4e8e", ">"),
    ("\u9ad8\u4e8e", ">"),
)
_FLAG_TOKENS: Final = (
    ("\u2191", "up"),
    ("\u2193", "down"),
    ("\u504f\u9ad8", "up"),
    ("\u504f\u4f4e", "down"),
)
_MARK_RE: Final = re.compile(r"\*(?!\s*\d)")
_NUMBER: Final = r"[-+]?\d+(?:\.\d+)?"
_RANGE_VALUE_RE: Final = re.compile(rf"({_NUMBER})\s*~\s*({_NUMBER})(.*)", re.DOTALL)
_NUMBER_VALUE_RE: Final = re.compile(rf"({_NUMBER})(?:[eE]([-+]?\d+))?(.*)", re.DOTALL)
_SCIENTIFIC_TAIL_RE: Final = re.compile(r"\s*[\u00d7xX*]\s*10\s*[\^*]\s*([-+]?\d+)\s*")
_HL_FLAG_RE: Final = re.compile(r"\s*([HL])(?=\s|$)(.*)", re.DOTALL)
_QUALITATIVE_CLASSES: Final[dict[str, str]] = {
    "\u9634\u6027": "neg",
    "-": "neg",
    "(-)": "neg",
    "neg": "neg",
    "negative": "neg",
    "\u9633\u6027": "pos",
    "+": "pos",
    "(+)": "pos",
    "pos": "pos",
    "positive": "pos",
    "\u00b1": "weak",
    "(\u00b1)": "weak",
    "\u5f31\u9633\u6027": "weak",
    "++": "pos2",
    "(++)": "pos2",
    "+++": "pos3",
    "(+++)": "pos3",
    "++++": "pos4",
    "(++++)": "pos4",
}
_QUALITATIVE_SPLIT_RE: Final = re.compile(
    r"^(\u9634\u6027|\u9633\u6027|\u5f31\u9633\u6027)\s*(\([-+\u00b1]+\))$"
)


def canonical_text(text: str) -> str:
    """NFKC、统一减号、数字之间的范围分隔符统一成 ``~``、去掉千分位逗号。"""
    text = unicodedata.normalize("NFKC", text)
    text = _MINUS_RE.sub("-", text)
    text = _THOUSANDS_RE.sub(lambda m: m.group(0).replace(",", ""), text)
    return _RANGE_RE.sub(r"\1~", text).strip()


def normalize_unit(unit: str) -> str:
    """单位只统一写法:大小写、μ/µ/u、``x10^9/L`` 的几种写法、空白。**不换算。**"""
    text = unicodedata.normalize("NFKC", unit).strip()
    text = re.sub(r"^[\u00d7xX*]\s*(?=10)", "", text)
    text = re.sub(r"10\s*[\^*]\s*([-+]?\d+)", r"10^\1", text)
    text = text.replace("\u03bc", "u").replace("\u00b5", "u")
    return re.sub(r"\s+", "", text).casefold()


def parse_reading(value: str, unit: str = "") -> Reading:
    """原文的值(和单独给的单位)→ :class:`Reading`。"""
    text = canonical_text(value)
    flags: set[str] = set()
    for token, flag in _FLAG_TOKENS:
        if token in text:
            flags.add(flag)
            text = text.replace(token, " ")
    # ``*`` 是「异常」标记,但 ``10*9/L`` 里它是单位的一部分:后面跟数字的不算标记。
    if _MARK_RE.search(text):
        flags.add("mark")
        text = _MARK_RE.sub(" ", text)
    text = text.strip()
    comparator = ""
    for token, canonical in _COMPARATORS:
        if text.startswith(token):
            comparator, text = canonical, text[len(token) :].strip()
            break
    compact = re.sub(r"\s+", "", text).casefold()
    qualitative = _qualitative_class(compact)
    if qualitative and not comparator:
        return Reading(kind="qualitative", qualitative=qualitative, flags=frozenset(flags))
    reading = _numeric_reading(text, unit, comparator, flags)
    if reading is not None:
        return reading
    normalized = " ".join(text.split()).rstrip(".\u3002").casefold()
    return Reading(kind="text", text=f"{comparator}{normalized}", flags=frozenset(flags))


def _qualitative_class(compact: str) -> str:
    split = _QUALITATIVE_SPLIT_RE.match(compact)
    if split is not None:
        word = _QUALITATIVE_CLASSES.get(split.group(1), "")
        sign = _QUALITATIVE_CLASSES.get(split.group(2), "")
        return word if word and word == sign else ""
    return _QUALITATIVE_CLASSES.get(compact, "")


def _numeric_reading(text: str, unit: str, comparator: str, flags: set[str]) -> Reading | None:
    match = _RANGE_VALUE_RE.fullmatch(text)
    if match is not None and not comparator:
        numbers: tuple[Decimal, ...] = (_decimal(match.group(1)), _decimal(match.group(2)))
        rest = match.group(3)
        kind: ReadingKind = "range"
    else:
        match = _NUMBER_VALUE_RE.fullmatch(text)
        if match is None:
            return None
        number = _decimal(match.group(1))
        if match.group(2):
            number = number.scaleb(int(match.group(2)))
        rest = match.group(3)
        scientific = _SCIENTIFIC_TAIL_RE.fullmatch(rest)
        if scientific is not None:
            number, rest = number.scaleb(int(scientific.group(1))), ""
        numbers = (number,)
        kind = "comparator" if comparator else "number"
    hl = _HL_FLAG_RE.fullmatch(rest)
    if hl is not None:
        flags.add("up" if hl.group(1) == "H" else "down")
        rest = hl.group(2)
    return Reading(
        kind=kind,
        numbers=numbers,
        comparator=comparator,
        flags=frozenset(flags),
        unit=normalize_unit(rest.strip() or unit),
    )


def _decimal(text: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation:  # pragma: no cover —— 正则已保证是数
        return Decimal(0)


def compatible(a: Reading, b: Reading) -> tuple[bool, str]:
    """两个读数是不是同一个值;不是时带一句原因(给主模型的冲突说明用)。"""
    if a.kind != b.kind:
        if {a.kind, b.kind} == {"number", "comparator"}:
            return False, (
                "one reading has a comparator (< or >) and the other does not; "
                "a tile edge may have cut it off"
            )
        return False, ""
    if {"up", "down"} <= a.flags | b.flags:
        return False, "one reading is flagged high and the other low"
    if a.unit and b.unit and a.unit != b.unit:
        return False, "the units differ"
    if a.kind == "qualitative":
        return a.qualitative == b.qualitative, ""
    if a.kind == "text":
        return a.text == b.text, ""
    if a.comparator != b.comparator:
        return False, "the comparators differ"
    return a.numbers == b.numbers, ""


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TileResult:
    """一块看下来的结果:``reply`` 为 ``None`` 表示这一块的调用失败了(``error`` 是原因)。"""

    tile: Tile
    reply: TileReply | None
    error: str | None = None


@dataclass(frozen=True)
class AutoVerdict:
    outcome: AutoOutcome
    content: str


@dataclass
class _Group:
    members: list[TileResult]
    readings: list[Reading]


def _reply(result: TileResult) -> TileReply:
    if result.reply is None:  # pragma: no cover —— 只对有回答的块调用
        msg = f"tile {result.tile.label} has no reply"
        raise ValueError(msg)
    return result.reply


def _display(result: TileResult) -> str:
    reply = _reply(result)
    if reply.unit and normalize_unit(reply.unit) not in normalize_unit(reply.value):
        return f"{reply.value} {reply.unit}"
    return reply.value


def _completeness(result: TileResult) -> tuple[int, int, int]:
    reply = _reply(result)
    reading = parse_reading(reply.value, reply.unit)
    return (int(bool(reading.unit)), len(reading.flags), len(_display(result)))


def _label_key(label: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", label).split()).casefold()


def _group_values(found: Sequence[TileResult]) -> tuple[list[_Group], list[str]]:
    """逐个放进第一个与组内**每一个**读数都相容的组;放不进就另开一组。不投票。"""
    groups: list[_Group] = []
    reasons: list[str] = []
    for result in found:
        reply = _reply(result)
        reading = parse_reading(reply.value, reply.unit)
        for group in groups:
            checks = [compatible(reading, other) for other in group.readings]
            if all(ok for ok, _ in checks):
                group.members.append(result)
                group.readings.append(reading)
                break
            reasons.extend(reason for ok, reason in checks if not ok and reason)
        else:
            groups.append(_Group(members=[result], readings=[reading]))
    return groups, list(dict.fromkeys(reasons))


def _union_box(boxes: Sequence[Box]) -> Box:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _box_text(box: Box) -> str:
    return f"[{box[0]}, {box[1]}, {box[2]}, {box[3]}]"


def aggregate_tiles(results: Sequence[TileResult]) -> AutoVerdict:
    """各块的回答 → 单值 / 冲突 / 全无(spec §1)。失败的、读不懂的块各说一句,从不算作值。"""
    found = [r for r in results if r.reply is not None and r.reply.status == "found"]
    partial = [r for r in results if r.reply is not None and r.reply.status == "partial"]
    not_found = [r for r in results if r.reply is not None and r.reply.status == "not_found"]
    unreadable = [r for r in results if r.reply is not None and r.reply.status == "unparseable"]
    failed = [r for r in results if r.reply is None]
    groups, reasons = _group_values(found)
    if not groups:
        outcome: AutoOutcome = "none"
        content = (
            "region='auto': no tile could see the complete answer"
            f" ({len(not_found)} replied not_found). The text may be too small to read, or it is "
            "not in this image."
        )
        if partial:
            union = _union_box([r.tile.box for r in partial])
            names = ", ".join(r.tile.label for r in partial)
            content += (
                f" Tiles {names} saw it cut off at their edge, so it probably sits on a seam "
                f"between tiles: look again with region={_box_text(union)}."
            )
    elif len(groups) == 1:
        outcome = "single"
        content = _single_text(groups[0], len(not_found), len(partial))
    else:
        outcome = "conflict"
        content = _conflict_text(groups, reasons)
    content += _side_notes(unreadable, failed)
    return AutoVerdict(outcome=outcome, content=content)


def _single_text(group: _Group, not_found: int, partial: int) -> str:
    best = max(group.members, key=_completeness)
    labels = [r.reply.label for r in group.members if r.reply is not None and r.reply.label]
    label = max(set(labels), key=labels.count) if labels else ""
    variants = list(dict.fromkeys(_display(r) for r in group.members))
    parts = [f"read in tile {', '.join(r.tile.label for r in group.members)}"]
    if label:
        parts.insert(0, f'label "{label}"')
    parts.append(f"{not_found} tile(s) replied not_found")
    if partial:
        parts.append(f"{partial} saw it cut off")
    if len(variants) > 1:
        parts.append("written as " + " / ".join(f'"{v}"' for v in variants))
    return f"{_display(best)}\n\n(region='auto': {'; '.join(parts)}.)"


def _conflict_text(groups: Sequence[_Group], reasons: Sequence[str]) -> str:
    keys = {
        _label_key(r.reply.label)
        for group in groups
        for r in group.members
        if r.reply is not None and r.reply.label
    }
    if len(keys) > 1:
        why = "the labels differ, so these are probably different rows or columns"
    else:
        why = "same label, different readings"
    lines = []
    for group in groups:
        for result in group.members:
            reply = _reply(result)
            label = f' label "{reply.label}"' if reply.label else ""
            lines.append(
                f"- tile {result.tile.label} region={_box_text(result.tile.box)}{label}: "
                f'"{_display(result)}"'
            )
    detail = f" ({'; '.join(reasons)})" if reasons else ""
    return (
        f"region='auto': the tiles gave conflicting readings - {why}{detail}:\n"
        + "\n".join(lines)
        + f"\n{len(groups)} different readings; no reading is preferred by count. Decide which "
        "one answers the question, or look again with one of the regions above."
    )


def _side_notes(unreadable: Sequence[TileResult], failed: Sequence[TileResult]) -> str:
    notes = ""
    for result in unreadable:
        raw = result.reply.raw if result.reply is not None else ""
        notes += (
            f"\n(Tile {result.tile.label} replied in a form that could not be read, so it was not "
            f'counted: "{raw[:80]}")'
        )
    if failed:
        names = ", ".join(f"{r.tile.label} ({r.error})" for r in failed)
        notes += f"\n(These tiles failed and were not read: {names}.)"
    return notes


def tile_meta(result: TileResult) -> dict[str, object]:
    """meta 里一块的记录:位置、框、状态、原文(截断)、解析方式、label / value / unit。"""
    entry: dict[str, object] = {"tile": result.tile.label, "box": list(result.tile.box)}
    reply = result.reply
    if reply is None:
        entry["error"] = result.error
        return entry
    entry.update(
        {
            "status": reply.status,
            "raw": reply.raw[:RAW_META_CHARS],
            "parse_mode": reply.parse_mode,
            "label": reply.label,
            "value": reply.value,
            "unit": reply.unit,
        }
    )
    if reply.note:
        entry["note"] = reply.note
    return entry
