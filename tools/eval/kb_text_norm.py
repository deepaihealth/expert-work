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
* :func:`unit_numbers` / :func:`key_facts`:带单位的数字、型号 / 编号类字符串、引号里的短语 ——
  通用规则,不针对任何一份资料。
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


def normalize_cjk(text: str) -> str:
    """部首 → 统一汉字,再 NFKC。"""
    return unicodedata.normalize("NFKC", text.translate(_RADICALS))


def _alnum(ch: str) -> bool:
    return unicodedata.category(ch)[0] in "LN"


def fold_key(text: str) -> str:
    """锚比对的归一形:去 HTML 标签、部首归一 + NFKC、小写,只留字母与数字。"""
    folded = normalize_cjk(_HTML_TAG.sub(" ", text)).lower()
    return "".join(ch for ch in folded if _alnum(ch))


def quote_key(text: str) -> str:
    """引文核对的投影(见模块说明)。"""
    folded = normalize_cjk(_HTML_TAG.sub(" ", text)).lower()
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
            views += ["".join(f"{h}{c}" for h, c in zip(header, r, strict=False)) for r in rows]
        width = max((len(r) for r in rows), default=0)
        for j in range(width):
            column = "".join(r[j] for r in rows if j < len(r))
            views.append((header[j] if header is not None and j < len(header) else "") + column)
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
_CODE = re.compile(r"(?<![a-z0-9])(?=[a-z0-9-]*[a-z])(?=[a-z0-9-]*[0-9])[a-z0-9][a-z0-9-]*[a-z0-9]")
#: 「」 “” "" 『』 里的短语。
_QUOTED = re.compile('[「“"『]([^」”"』\\n]{2,40})[」”"』]')
_MIN_CODE = 3


def unit_numbers(text: str) -> list[str]:
    """带单位的数字,按 :func:`quote_key` 投影(``60℃`` → ``60c``),去重保序。"""
    folded = normalize_cjk(text).lower()
    found = [quote_key(m.group(1) + m.group(2)) for m in _UNIT_NUMBER.finditer(folded)]
    return list(dict.fromkeys(found))


def has_number(token: str, key: str) -> bool:
    """投影后的 ``key`` 里有没有 ``token``(前面不能紧挨数字或小数点:``5岁`` 不算在 ``15岁`` 里)。"""
    return re.search(rf"(?<![0-9.]){re.escape(token)}", key) is not None


def key_facts(text: str) -> list[tuple[str, str]]:
    """参考答案里的关键事实 ``(类别, 投影后的值)``:``number`` 带单位的数字、``code`` 型号 / 编号类
    字符串(字母数字混排、≥ 3 个字符,不含已算作带单位数字的)、``quote`` 引号里的短语。"""
    numbers = unit_numbers(text)
    folded = normalize_cjk(text).lower()
    codes = [
        m.group(0)
        for m in _CODE.finditer(folded)
        if len(m.group(0)) >= _MIN_CODE and quote_key(m.group(0)) not in numbers
    ]
    quotes = [quote_key(m.group(1)) for m in _QUOTED.finditer(normalize_cjk(text))]
    facts = [("number", n) for n in numbers]
    facts += [("code", quote_key(c)) for c in codes]
    facts += [("quote", q) for q in quotes if q]
    return list(dict.fromkeys(facts))


__all__ = [
    "fold_key",
    "has_number",
    "key_facts",
    "normalize_cjk",
    "quote_key",
    "table_views",
    "unit_numbers",
]
