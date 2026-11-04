"""B-273 V1 —— ``ask_image(region="auto")`` 的问题是不是在问**整张图**(计数 / 合计 / 平均 /
最高最低 / 排名 / 哪些 / 趋势 / 差值)。

是的话以整张读为准(横条照样并行读,只用来标不确定,见 ``tile_reading.decide_whole_question``):
切条之后每条只看到一截,整体类问题条上的答案在 E1 评测(11-03)里 55 次只对 1 次,而且 45 次是
「只有一条说找到」,汇总规则兜不住。词表在 75 道测量题上 100% 准、另 79 道查值题 0 误判 ——
**部分是在同一批题上调出来的,偏乐观**,改后要用新写的题再验。

中英两套词;防误判(都只挡「字段名 / 测量项名」式的用法,规则是通用的,不列具体项目):

* 引号里的词不算(表中「合计」一行 / the "Total" row);
* 词后面紧跟 一栏 / 一行 / 一列 / 图 / 表 / 字段 / column / row 不算(「趋势图标题」);
* 「最X」只在后面是 的 / 是 / 为 / 值 / 标点 / 句末,或问题里有「哪」时才算 ——「最高血压是多少」
  「最大摄氧量那一项」照样切条;「最X值」「平均值」前面紧贴着一个名称(「收缩压最高值」)是那一项
  的名字,也不算;
* 「平均」后面紧跟名称(「平均红细胞体积」是测量项)不算,只认 平均每 / 平均是 / 平均值 / 的平均;
  「数量」不当线索(「白细胞数量」「药品的数量」都是读一个值);
* 英文:count / total / mean / min / max / most 只在像是在要求汇总时才算(count of、the total?、
  mean of、maximum value、which … most),作为名称的一部分(platelet count、total cholesterol、
  mean corpuscular volume、minimum payment due、most recent)不算;``number of`` 只认
  the / a / total number of(phone number of 不算);数字后面的 min 是分钟。

残留风险:不含这些词的整体问法(「谁的分数第一」「有没有超过 100 的」、其它语言)仍走切条,由
``tile_reading`` 的多条同值规则兜。中文写成 ``\\u`` 转义(与 ``tile_reading`` 同一规矩)。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

#: 量词:几次 / 几条 / 几个 / 几行 / 几项 / 几处 / 几人 / 几天 / 几位 / 几名 / 几份 / 几种 / 几组
#: / 几回 / 几遍。
_CLASSIFIERS: Final = (
    "\u6b21\u6761\u4e2a\u884c\u9879\u5904\u4eba\u5929\u4f4d\u540d\u4efd\u79cd\u7ec4\u56de\u904d"
)
_WHICH_ZH: Final = "\u54ea"
_WHICH_EN: Final = re.compile(r"\b(?:which|who)\b")

#: 词后面紧跟的字段 / 对象名:值?一?栏|行|列|格|图|表、字段、column / row / field / cell。
_FIELD_AFTER: Final = re.compile(
    r"^\s*(?:\u503c?\s*\u4e00?\s*[\u680f\u884c\u5217\u683c\u56fe\u8868]|\u5b57\u6bb5|(?:column|row|field|cell)s?\b)"
)
#: 引号里的一段(「」『』“”、ASCII 双引号、前后不贴着字母数字的 ASCII 单引号 —— 撇号不是引号)。
_QUOTED: Final = re.compile(
    "\u300c[^\u300d]*\u300d|\u300e[^\u300f]*\u300f|\u201c[^\u201d]*\u201d"
    "|\"[^\"]*\"|(?<![a-z0-9])'[^']*'(?![a-z0-9])"
)
_SUPERLATIVE_OK_AFTER: Final = re.compile(
    "^(?:\u7684|\u662f|\u4e3a|\u503c|$|[?,\u3002\u3001=!.;:])"
)
_AVERAGE_OK_AFTER: Final = re.compile(
    "^(?:\u6bcf|\u662f|\u4e3a|\u503c|\u6570|\u5206|\u7684|\u591a\u5c11|$|[?,\u3002\u3001=!.;:])"
)
_AVERAGE_OK_BEFORE: Final = "\u7684\u6c42\u7b97"
#: 「X值」前面可以是:句首 / 的 / 中 / 里 / 非文字字符;紧贴着别的字就是某一项的名字。
_VALUE_OK_BEFORE: Final = "\u7684\u4e2d\u91cc"

Accept = Callable[[str, re.Match[str]], bool]


def _followed_by(pattern: str, *, or_which: bool = False) -> Accept:
    """这个英文词后面紧跟 ``pattern``(或句末 / 标点),或问题里有 which / who 时才算。"""
    after_re = re.compile(rf"^\s*(?:$|[?.!,;:]|{pattern})")

    def accept(text: str, match: re.Match[str]) -> bool:
        if or_which and _WHICH_EN.search(text):
            return True
        return after_re.match(text[match.end() :]) is not None

    return accept


def _not_a_named_value(text: str, match: re.Match[str]) -> bool:
    """「最高值」「平均值」前面紧贴着一个名称(「收缩压最高值」)= 那一项的名字,不是在求最值。"""
    if match.start() == 0:
        return True
    before = text[match.start() - 1]
    return before in _VALUE_OK_BEFORE or not before.isalnum()


def _superlative_ok(text: str, match: re.Match[str]) -> bool:
    if _WHICH_ZH in text:
        return True
    after = text[match.end() :]
    if after.startswith("\u503c"):
        return _not_a_named_value(text, match)
    return _SUPERLATIVE_OK_AFTER.match(after) is not None


def _average_ok(text: str, match: re.Match[str]) -> bool:
    after = text[match.end() :]
    if after.startswith("\u503c"):
        return _not_a_named_value(text, match)
    before = text[match.start() - 1] if match.start() else ""
    return _AVERAGE_OK_AFTER.match(after) is not None or (
        bool(before) and before in _AVERAGE_OK_BEFORE
    )


def _count_ok(text: str, match: re.Match[str]) -> bool:
    """count 是动词或「count of」才算;「platelet count」是测量项名。"""
    if not text[: match.start()].strip():
        return True
    after = text[match.end() :]
    return re.match(r"^\s+(?:of|the|how|all|every|each|up|them|these|those)\b", after) is not None


def _min_max_ok(text: str, match: re.Match[str]) -> bool:
    """数字后面的 min 是分钟;「minimum payment due」是名称。"""
    if re.search(r"\d\s*$", text[: match.start()]):
        return False
    return _followed_by(r"of\b|value\b|in\b|among\b|across\b", or_which=True)(text, match)


@dataclass(frozen=True)
class _Cue:
    pattern: re.Pattern[str]
    #: 后面紧跟字段 / 对象名(一栏 / 图 / column ...)时不算。
    field_guard: bool = False
    #: 额外条件:返回 ``False`` = 这一处是名称的一部分,不是线索。
    accept: Accept | None = None


def _cue(pattern: str, *, field_guard: bool = False, accept: Accept | None = None) -> _Cue:
    return _Cue(re.compile(pattern), field_guard=field_guard, accept=accept)


_CUES: Final = (
    # 几次 / 多少条 / 一共 / 总共 / 共有 / 共计 / 总数 / 统计一下|有|出
    _cue(f"几[{_CLASSIFIERS}]"),
    _cue(f"多少[{_CLASSIFIERS}]"),
    _cue("\u4e00\u5171"),
    _cue("\u603b\u5171"),
    _cue("\u5171\u6709"),
    _cue("\u5171\u8ba1"),
    _cue("\u603b\u6570"),
    _cue("\u7edf\u8ba1(?:\u4e00\u4e0b|\u6709|\u51fa)"),
    _cue(r"\bhow many\b"),
    _cue(r"\bhow often\b"),
    _cue(r"\bcount\b", accept=_count_ok),
    _cue(r"(?:^|\b(?:the|a|total)\s+)number of\b"),
    _cue(r"\bin total\b"),
    _cue(r"\baltogether\b"),
    # 合计 / 总计 / 总和 / 加起来 / 相加 / 排名 / 哪些 / 趋势 / 差值 / 相差 / 哪个…最
    *(
        _cue(word, field_guard=True)
        for word in (
            "\u5408\u8ba1",
            "\u603b\u8ba1",
            "\u603b\u548c",
            "\u52a0\u8d77\u6765",
            "\u76f8\u52a0",
            "\u6392\u540d",
            "\u54ea\u4e9b",
            "\u8d8b\u52bf",
            "\u5dee\u503c",
            "\u76f8\u5dee",
            r"\u54ea[\u4e2a\u4e00\u4f4d\u5929\u9879\u884c]\S{0,8}\u6700",
            r"\bsum\b",
            r"\baverage\b",
            r"\bhighest\b",
            r"\blowest\b",
            r"\branked?\b",
            r"\bwhich\b.{0,40}\b(?:most|least)\b",
            r"\blist all\b",
            r"\badd up\b",
            r"\btrend\b",
        )
    ),
    _cue("\u5e73\u5747", field_guard=True, accept=_average_ok),
    _cue("\u6700[\u9ad8\u4f4e\u5927\u5c0f\u591a\u5c11]", field_guard=True, accept=_superlative_ok),
    _cue(
        r"\btotal\b",
        field_guard=True,
        accept=_followed_by(r"of\b|for\b|number\b|count\b|across\b|in\b"),
    ),
    _cue(r"\bmean\b", field_guard=True, accept=_followed_by(r"of\b|value\b")),
    _cue(r"\b(?:max|min)(?:imum)?\b", field_guard=True, accept=_min_max_ok),
    _cue(
        r"\b(?:most|least)\b",
        field_guard=True,
        accept=_followed_by(r"of\b|in\b|among\b|common\b|frequent|often\b|times\b", or_which=True),
    ),
)


def _hits(text: str, cue: _Cue, quoted: list[tuple[int, int]]) -> bool:
    for match in cue.pattern.finditer(text):
        if any(start <= match.start() < end for start, end in quoted):
            continue
        if cue.field_guard and _FIELD_AFTER.match(text[match.end() :]):
            continue
        if cue.accept is not None and not cue.accept(text, match):
            continue
        return True
    return False


def is_whole_image_question(question: str) -> bool:
    """问题像整体类问题(计数 / 合计 / 平均 / 最高最低 / 排名 / 哪些 / 趋势 / 差值)。"""
    text = unicodedata.normalize("NFKC", question).casefold()
    quoted = [m.span() for m in _QUOTED.finditer(text)]
    return any(_hits(text, cue, quoted) for cue in _CUES)
