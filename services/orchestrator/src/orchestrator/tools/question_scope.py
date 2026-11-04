"""B-273 V1 —— ``ask_image(region="auto")`` 的问题是不是在问**整张图**(计数 / 合计 / 平均 /
最高最低 / 排名 / 哪些 / 趋势 / 差值)。

是的话不切条、直接整张读:切条之后每条只看到一截,整体类问题条上的答案在 E1 评测(11-03)里
55 次只对 1 次,而且 45 次是「只有一条说找到」,汇总规则兜不住。词表在 75 道测量题上 100% 准、
另 79 道查值题 0 误判 —— **部分是在同一批题上调出来的,偏乐观**,改后要用新写的题再验。

中英两套词;防误判(都只挡「字段名」式的用法):

* 引号里的词不算(表中「合计」一行 / the "Total" row);
* 合计 / 最高 / 平均这类词后面紧跟 一栏 / 一行 / 一列 / 字段 / column / row 不算;
* 「最X」只在后面是 的 / 是 / 为 / 值 / 标点 / 句末,或问题里有「哪」时才算 —— 「最高血压是多少」
  「最大摄氧量那一项」照样切条。

残留风险:不含这些词的整体问法(「谁的分数第一」「有没有超过 100 的」、其它语言)仍走切条,由
``tile_reading`` 的多条同值规则兜。中文写成 ``\\u`` 转义(与 ``tile_reading`` 同一规矩)。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final, Literal

CueKind = Literal["count", "aggregate"]

#: 量词:几次 / 几条 / 几个 / 几行 / 几项 / 几处 / 几人 / 几天 / 几位 / 几名 / 几份 / 几种 / 几组。
_CLASSIFIERS: Final = (
    "\u6b21\u6761\u4e2a\u884c\u9879\u5904\u4eba\u5929\u4f4d\u540d\u4efd\u79cd\u7ec4\u56de\u904d"
)
#: 「最高 / 最低 / 最大 / 最小 / 最多 / 最少」—— 只有它受「后面跟什么」那条防误判约束。
_SUPERLATIVE: Final = "\u6700[\u9ad8\u4f4e\u5927\u5c0f\u591a\u5c11]"


@dataclass(frozen=True)
class _Cue:
    kind: CueKind
    pattern: re.Pattern[str]
    #: 后面紧跟字段名(一栏 / 一行 / column ...)时不算。
    field_guard: bool = False
    superlative: bool = False


def _cue(
    kind: CueKind, pattern: str, *, field_guard: bool = False, superlative: bool = False
) -> _Cue:
    return _Cue(kind, re.compile(pattern), field_guard=field_guard, superlative=superlative)


_CUES: Final = (
    # 几次 / 多少条 / 一共 / 总共 / 共有 / 共计 / 总数 / 数量 / 统计一下|有|出
    _cue("count", f"几[{_CLASSIFIERS}]"),
    _cue("count", f"多少[{_CLASSIFIERS}]"),
    _cue("count", "\u4e00\u5171"),
    _cue("count", "\u603b\u5171"),
    _cue("count", "\u5171\u6709"),
    _cue("count", "\u5171\u8ba1"),
    _cue("count", "\u603b\u6570"),
    _cue("count", "\u6570\u91cf"),
    _cue("count", "\u7edf\u8ba1(?:\u4e00\u4e0b|\u6709|\u51fa)"),
    _cue("count", r"\bhow many\b"),
    _cue("count", r"\bhow often\b"),
    _cue("count", r"\bcount\b"),
    _cue("count", r"\bnumber of\b"),
    _cue("count", r"\bin total\b"),
    _cue("count", r"\baltogether\b"),
    # 合计 / 总计 / 总和 / 加起来 / 相加 / 平均 / 排名 / 哪些 / 趋势 / 差值 / 相差 / 最X / 哪个…最
    *(
        _cue("aggregate", word, field_guard=True)
        for word in (
            "\u5408\u8ba1",
            "\u603b\u8ba1",
            "\u603b\u548c",
            "\u52a0\u8d77\u6765",
            "\u76f8\u52a0",
            "\u5e73\u5747",
            "\u6392\u540d",
            "\u54ea\u4e9b",
            "\u8d8b\u52bf",
            "\u5dee\u503c",
            "\u76f8\u5dee",
            "\u54ea[\u4e2a\u4e00\u4f4d\u5929\u9879\u884c]\\S{0,8}\u6700",
            r"\btotal\b",
            r"\bsum\b",
            r"\baverage\b",
            r"\bmean\b",
            r"\bhighest\b",
            r"\blowest\b",
            r"\bmax(?:imum)?\b",
            r"\bmin(?:imum)?\b",
            r"\bmost\b",
            r"\bleast\b",
            r"\branked?\b",
            r"\bwhich\b.{0,40}\b(?:most|least)\b",
            r"\blist all\b",
            r"\badd up\b",
            r"\btrend\b",
        )
    ),
    _cue("aggregate", _SUPERLATIVE, field_guard=True, superlative=True),
)

#: 引号里的一段(「」『』“”、ASCII 双引号、前后不贴着字母数字的 ASCII 单引号 —— 撇号不是引号)。
_QUOTED: Final = re.compile(
    "\u300c[^\u300d]*\u300d|\u300e[^\u300f]*\u300f|\u201c[^\u201d]*\u201d"
    "|\"[^\"]*\"|(?<![a-z0-9])'[^']*'(?![a-z0-9])"
)
#: 词后面紧跟的字段名:值?一?栏|行|列|格、字段、column / row / field / cell。
_FIELD_AFTER: Final = re.compile(
    "^\\s*(?:\u503c?\\s*\u4e00?\\s*[\u680f\u884c\u5217\u683c]|\u5b57\u6bb5"
    r"|(?:column|row|field|cell)s?\b)"
)
#: 「最X」后面可以是:的 / 是 / 为 / 值 / 句末 / 标点。
_SUPERLATIVE_OK_AFTER: Final = re.compile(
    "^(?:\u7684|\u662f|\u4e3a|\u503c|$|[?,\u3002\u3001=!.;:])"
)
_WHICH: Final = "\u54ea"


def _cues(question: str) -> list[CueKind]:
    text = unicodedata.normalize("NFKC", question).casefold()
    quoted = [m.span() for m in _QUOTED.finditer(text)]
    kinds: list[CueKind] = []
    for cue in _CUES:
        for match in cue.pattern.finditer(text):
            if any(start <= match.start() < end for start, end in quoted):
                continue
            after = text[match.end() :]
            if cue.field_guard and _FIELD_AFTER.match(after):
                continue
            if cue.superlative and not _SUPERLATIVE_OK_AFTER.match(after) and _WHICH not in text:
                continue
            kinds.append(cue.kind)
    return kinds


def is_whole_image_question(question: str) -> bool:
    """问题像整体类问题(计数 / 合计 / 平均 / 最高最低 / 排名 / 哪些 / 趋势 / 差值)。"""
    return bool(_cues(question))


def is_count_question(question: str) -> bool:
    """问题像计数类问题(几次 / 一共 / how many ...)—— V2 用:条上报 0 当作没找到。"""
    return "count" in _cues(question)
