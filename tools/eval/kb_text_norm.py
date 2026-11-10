"""评测里比对文本用的归一(B-206 11-10 尺子修正):难负例删句、删后穷尽核对、判分引文核对都用这里。

* :func:`normalize_cjk`:先把 CJK 部首补充 / 康熙部首按 Unicode ``EquivalentUnifiedIdeograph``
  映射成统一汉字(数据见 ``kb_cjk_radicals.py``),再 NFKC。部首补充区块的字 NFKC 归一不掉,
  PDF / Office 抽出来的文本里常混着它们,锚就对不上、删不掉。
* :func:`fold_key`:同 ``kb_metrics.anchor_key``(去 HTML 标签,小写,只留字母与数字),多了部首归一。
  删句 / 删后残留核对用它。
* :func:`quote_key`:判分引文核对的投影(参考 WeKnora ``source_locator``):部首归一 + NFKC → 小写 →
  只留字母、数字和 ``.%≤≥<>±~-``(全角波浪号经 NFKC 成 ``~``)。``.`` 只在两个数字之间留(小数点),
  ``-`` / ``~`` 只在两个字母数字之间留(型号、范围)—— 句末的半角句点、列表前的短横不是内容。
* :func:`table_views`:Markdown 表格按行(表头 + 单元格成对)、按列(表头 + 这一列的单元格)序列化,
  引文常把表头和值连着抄,原文里它们隔着几行。
* :func:`unit_numbers` / :func:`key_facts`:带单位的数字、型号 / 编号类字符串 —— 通用规则,
  不针对任何一份资料;在保留分隔符的原文上抽,比集合。
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from kb_cjk_radicals import RADICAL_TO_UNIFIED  # type: ignore[import-not-found]

_RADICALS = str.maketrans({chr(k): chr(v) for k, v in RADICAL_TO_UNIFIED.items()})
_HTML_TAG = re.compile(r"</?[A-Za-z][^<>]{0,40}>")
#: 引文投影里留下的符号(≤ ≥ ±)。
_ALWAYS_KEEP = frozenset("%<>≤≥±")
#: 只在两侧都是字母数字时留(``.`` 只在两个数字之间)。
_JOINERS = frozenset(".-~")


#: 完整的千分位数字(``1,000`` / ``1,000,000``):只认半角逗号、在 NFKC 之前合并 ——
#: 全角逗号是列举(``100,200,500毫升`` 用全角逗号时是三个数),``12,34`` 也不合并。
_THOUSANDS = re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{3})+(?![\d,])")
#: 行首的列表编号(``1. `` / ``2、`` / ``3) ``,``、`` 后面的空白可有可无):不是小数点,
#: ``1. 5克装`` 不能读成 ``1.5克``。
_LIST_NUMBER = re.compile(r"(?m)^[ \t]*\d{1,2}(?:[.)][ \t]+|\u3001[ \t]*)")
_ELLIPSES = re.compile("[\u2026\u22ef]+")
_DOTS = re.compile(r"\.{3,}")
#: 任意位置的列表编号(引文常把几条列表压成一行抄);只用在引文的第二种比法上(见
#: :func:`quote_variants`),``第1、2条`` 这种正文按第一种比法照样对得上。
_INLINE_LIST_NUMBER = re.compile(r"(?<![0-9.])\d{1,2}(?:[.)]\s+|\u3001\s*)")


def normalize_cjk(text: str) -> str:
    """部首 → 统一汉字,再 NFKC。"""
    return unicodedata.normalize("NFKC", text.translate(_RADICALS))


def _numbers_ready(text: str) -> str:
    """合并完整的半角千分位(NFKC 之前),部首归一 + NFKC,去掉行首列表编号。"""
    merged = _THOUSANDS.sub(lambda m: m.group(0).replace(",", ""), text)
    # 省略号换成空格:NFKC 把「…」变成「...」,紧跟的数字会被当成小数的一部分
    folded = _DOTS.sub(" ", normalize_cjk(_ELLIPSES.sub(" ", merged)))
    return _LIST_NUMBER.sub("", folded)


def quote_variants(quote: str) -> list[str]:
    """引文的两种比法:原样;去掉任意位置的列表编号(``产品规格: 1. 5克装`` / ``5克装 2. 10克装``)。"""
    stripped = _INLINE_LIST_NUMBER.sub("", normalize_cjk(quote))
    return list(dict.fromkeys([quote, stripped]))


def _alnum(ch: str) -> bool:
    return unicodedata.category(ch)[0] in "LN"


def fold_key(text: str) -> str:
    """锚比对的归一形:去 HTML 标签、部首归一 + NFKC、小写,只留字母与数字。"""
    folded = normalize_cjk(_HTML_TAG.sub(" ", text)).lower()
    return "".join(ch for ch in folded if _alnum(ch))


def quote_key(text: str) -> str:
    """引文核对的投影(见模块说明)。"""
    folded = _numbers_ready(_HTML_TAG.sub(" ", text)).lower()
    kept = [ch for ch in folded if _alnum(ch) or ch in _ALWAYS_KEEP or ch in _JOINERS]
    out: list[str] = []
    for i, ch in enumerate(kept):
        if ch in _JOINERS:
            before = kept[i - 1] if i else ""
            after = kept[i + 1] if i + 1 < len(kept) else ""
            ok = (
                (before.isdigit() and after.isdigit())
                if ch == "."
                else (bool(before) and bool(after) and _alnum(before) and _alnum(after))
            )
            if not ok:
                continue
        out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# 表格
# ---------------------------------------------------------------------------

_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")


def _cells(line: str) -> list[str]:
    parts = line.strip().split("|")
    return [p.strip() for p in parts[1:-1]] if len(parts) >= 3 else []


def _table_blocks(text: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip().startswith("|"):
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(_SEPARATOR_CELL.match(c) for c in cells)


def table_views(text: str) -> list[str]:
    """``text`` 里每张 Markdown 表格按行(表头 + 单元格成对)与按列(表头 + 这一列)的序列化。
    切块常把表头切掉:没有表头时只按列拼。没有表格时空列表。"""
    views: list[str] = []
    for block in _table_blocks(text):
        rows = [_cells(line) for line in block]
        header: list[str] | None = None
        if len(rows) >= 2 and _is_separator(rows[1]):
            header, rows = rows[0], rows[2:]
        rows = [r for r in rows if r and not _is_separator(r)]
        if header is not None:
            # 单元格之间留空格:投影时消失,按原文抽数字时不把相邻单元格粘成一个数
            views += [" ".join(f"{h} {c}" for h, c in zip(header, r, strict=False)) for r in rows]
        width = max((len(r) for r in rows), default=0)
        for j in range(width):
            column = " ".join(r[j] for r in rows if j < len(r))
            title = header[j] if header is not None and j < len(header) else ""
            views.append(f"{title} {column}")
    return views


# ---------------------------------------------------------------------------
# 关键事实
# ---------------------------------------------------------------------------

#: 计量单位(通用说法,不含任何租户内容);长的写在前面。
_UNITS = (
    "毫克|微克|千克|公斤|克|毫升|升|分钟|小时|秒|天|日|周|个月|月|年|岁|次|袋|粒|片|瓶|盒|支|包|罐|"
    "条|元|倍|度|亿|万|斤|kcal|mmol|mg|μg|ug|kg|ml|cfu|iu|kj|cm|mm|g|l|%|°c"
)
_UNIT_NUMBER = re.compile(rf"(?<![0-9.])(\d+(?:\.\d+)?)\s*({_UNITS})(?![a-z])")
#: 型号 / 编号:前面不能紧挨字母数字或小数点(``1.5mg`` 切不出 ``5mg``)。
_CODE = re.compile(
    r"(?<![a-z0-9.])(?=[a-z0-9-]*[a-z])(?=[a-z0-9-]*[0-9])[a-z0-9][a-z0-9-]*[a-z0-9]"
)
_MIN_CODE = 3


def unit_numbers(text: str) -> list[str]:
    """带单位的数字,按 :func:`quote_key` 投影(``60℃`` → ``60c``),去重保序。"""
    folded = _numbers_ready(text).lower()
    found = [quote_key(m.group(1) + m.group(2)) for m in _UNIT_NUMBER.finditer(folded)]
    return list(dict.fromkeys(found))


def fact_positions(kind: str, value: str, key: str) -> list[int]:
    """投影后的 ``key`` 里 ``value`` 出现的位置,**只用来定位窗口**:「有没有这个事实」要在保留分隔符
    的原文上用 :func:`key_facts` 抽出来比集合(审查第二轮 HIGH-1:投影压掉了分隔符,「5g 乘号 10袋」
    投影成 ``5g10袋``,在投影上加尾边界会漏)。这里只加前边界:带单位的数字前面不能紧挨数字或
    小数点,型号前面不能紧挨字母数字、小数点或连字符。"""
    escaped = re.escape(value)
    if kind == "number":
        pattern = rf"(?<![0-9.]){escaped}"
    elif kind == "code":
        pattern = rf"(?<![a-z0-9.\-]){escaped}"
    else:
        pattern = escaped
    return [m.start() for m in re.finditer(pattern, key)]


def key_facts(text: str) -> list[tuple[str, str]]:
    """原文里的关键事实 ``(类别, 投影后的值)``:``number`` 带单位的数字、``code`` 型号 / 编号类
    字符串(字母数字混排、≥ 3 个字符,不含带单位的数字及其尾巴)。在原文上抽,分隔符还在,
    「5g 乘号 10袋」 抽出 ``5g`` 和 ``10袋``、``2.5mg`` 里抽不出 ``1.5mg``。
    引号里的短语不算(审查第二轮
    MEDIUM-1:引号常是评委转述时加的)。"""
    numbers = unit_numbers(text)
    folded = _numbers_ready(text).lower()
    codes = [
        m.group(0)
        for m in _CODE.finditer(folded)
        # 带单位的数字本身、或它的尾巴(``1.5mg`` 的 ``5mg``)不是型号
        if len(m.group(0)) >= _MIN_CODE
        and not any(n.endswith(quote_key(m.group(0))) for n in numbers)
    ]
    facts = [("number", n) for n in numbers]
    facts += [("code", quote_key(c)) for c in codes]
    return list(dict.fromkeys(facts))


__all__ = [
    "fact_positions",
    "fold_key",
    "key_facts",
    "normalize_cjk",
    "quote_key",
    "quote_variants",
    "table_views",
    "unit_numbers",
]
