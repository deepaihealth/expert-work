"""B-268 —— 本 run 的 ``read_page`` 读取账:重看回执、超上限预告、熔断。

评测(E2,11-03,合成纯图片表格 PDF):旧的 3 槽窗口让 4 页 / 6 页文档按「最早那页退出 →
重看」严格轮转到步数上限,写到某页时它已不在视野里,数字就编出来。窗口本身的放宽在
``graph_builder.figure_block``;这里是 ``read_page`` 那一侧:

* 账记在 ``state["figure_reads"]``(工具可写的通道,读的一侧逐条校验),每条是一次渲染:
  ``{"run", "doc", "unit", "page", "region", "pages"}``。**按 run 计**(第 7 点):主代的
  ``viewed_figures`` 跨轮累积,后续轮次的正常重看不能被上一轮的次数挡住 —— 通道的 reducer
  (``state._merge_figure_reads``)只留最新那个 run 的条目,别的 run 的条目读的时候也不算。
* 同一 ``(文档, 编号, 局部)`` 本 run 已渲 :data:`FIGURE_READS_PER_ITEM` 次,再读就不渲、
  回拒绝文字;同一文档本 run 渲染次数超过 ``2 x 页数 + 3`` 同样处理。拒绝计入防循环次数
  (``LOOP_TRIP_META_KEY``),``DEFAULT_LOOP_TRIPS_PER_RUN`` 次后 run 收尾。依据:正常 run
  每页最多重看 1 次,E2 里最后核对才到第 3 次。
* 页数取自渲染片段回的 ``pages``(``pdfinfo``,拿不到就没有);没有页数就只按单页规则。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

#: 正在读的那份文档同时可见的整页数上限(``figure_block`` 的整页道)。
FIGURE_PAGES_IN_VIEW: Final = 6
#: 局部图(``read_page(region=…)``)单独一条道的上限,只挤局部图、不挤整页。
FIGURE_CROPS_IN_VIEW: Final = 2
#: 同一 ``(文档, 编号, 局部)`` 本 run 最多渲几次;再读就拒绝。
FIGURE_READS_PER_ITEM: Final = 3
#: 同一文档本 run 渲染次数上限 = ``页数 x _DOC_RENDERS_PER_PAGE + _DOC_RENDERS_SLACK``。
_DOC_RENDERS_PER_PAGE: Final = 2
_DOC_RENDERS_SLACK: Final = 3

_REFUSAL_TAIL = (
    "Finish the task from what you have already written down; "
    "if data is really missing, say in your answer what is missing."
)
_THINKING_NOTE = (
    "Note: numbers noted in your thinking are not kept to the next step — write the numbers "
    "you need into your reply, or record them in a workspace file with write_file, and rely "
    "on the file afterwards."
)

Box = tuple[int, int, int, int]


@dataclass(frozen=True)
class FigureRead:
    """一次渲染:模型传的编号、真实页号、局部框(整页是 ``None``)。"""

    unit: int
    page: int
    region: Box | None = None


@dataclass(frozen=True)
class _Entry:
    doc: str
    read: FigureRead
    pages: int | None


def ledger_entries(
    run_id: UUID, doc_sha: str, reads: Sequence[FigureRead], pages: int | None
) -> list[dict[str, Any]]:
    """这次调用要追加进 ``figure_reads`` 的条目,一次渲染一条。"""
    return [
        {
            "run": str(run_id),
            "doc": doc_sha,
            "unit": read.unit,
            "page": read.page,
            "region": list(read.region) if read.region is not None else None,
            "pages": pages,
        }
        for read in reads
    ]


def _plain_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse_entry(raw: object, run: str) -> _Entry | None:
    """一条账目;不是本 run 的、形状不对的都当没有(通道工具可写,不可信)。"""
    if not isinstance(raw, Mapping) or raw.get("run") != run:
        return None
    doc, unit, page = raw.get("doc"), _plain_int(raw.get("unit")), _plain_int(raw.get("page"))
    if not isinstance(doc, str) or unit is None or page is None:
        return None
    region_raw = raw.get("region")
    region: Box | None = None
    if region_raw is not None:
        ints = [_plain_int(v) for v in region_raw] if isinstance(region_raw, list) else []
        box = [v for v in ints if v is not None]
        if len(ints) != 4 or len(box) != 4:
            return None
        region = (box[0], box[1], box[2], box[3])
    return _Entry(doc=doc, read=FigureRead(unit, page, region), pages=_plain_int(raw.get("pages")))


def _doc_entries(ledger: Sequence[object], run_id: UUID | None, doc_sha: str) -> list[_Entry]:
    if run_id is None:
        return []
    parsed = (_parse_entry(raw, str(run_id)) for raw in ledger)
    return [e for e in parsed if e is not None and e.doc == doc_sha]


def _known_pages(entries: Sequence[_Entry], pages: int | None = None) -> int | None:
    if pages is not None:
        return pages
    known = [e.pages for e in entries if e.pages is not None and e.pages > 0]
    return known[-1] if known else None


def _describe(read: FigureRead) -> str:
    if read.region is None:
        return f"page {read.page}"
    return f"region {list(read.region)} of page {read.page}"


def read_refusal(
    ledger: Sequence[object],
    *,
    run_id: UUID | None,
    doc_sha: str,
    units: Sequence[int],
    region: Box | None,
) -> str | None:
    """这次 ``read_page`` 该不该拒;该拒就是给模型的那段话。渲染之前判,只看账。"""
    entries = _doc_entries(ledger, run_id, doc_sha)
    pages = _known_pages(entries)
    if pages is not None and len(entries) + len(units) > (
        _DOC_RENDERS_PER_PAGE * pages + _DOC_RENDERS_SLACK
    ):
        return (
            f"This document has already been rendered {len(entries)} times in this task, "
            f"so it is not rendered again. {_REFUSAL_TAIL}"
        )
    for unit in units:
        same = [e.read for e in entries if e.read.unit == unit and e.read.region == region]
        if len(same) >= FIGURE_READS_PER_ITEM:
            what = _describe(same[-1])
            return (
                f"{what[0].upper()}{what[1:]} has already been read {len(same)} times in this "
                f"task, so it is not rendered again. {_REFUSAL_TAIL}"
            )
    return None


def _page_ranges(pages: Sequence[int]) -> str:
    """``[1, 2, 3, 5]`` → ``"1-3, 5"``。"""
    ordered = sorted(set(pages))
    runs: list[tuple[int, int]] = []
    for page in ordered:
        if runs and page == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], page)
        else:
            runs.append((page, page))
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def _in_view_sentence(entries: Sequence[_Entry], reads: Sequence[FigureRead]) -> str | None:
    """本 run 读过的这份文档的整页里,最新的 :data:`FIGURE_PAGES_IN_VIEW` 页 —— 正在读的文档
    的整页窗口(``figure_block``)里一定有它们。"""
    reads_so_far = [*(e.read for e in entries), *reads]
    newest_first = [r.page for r in reversed(reads_so_far) if r.region is None]
    in_view = list(dict.fromkeys(newest_first))[:FIGURE_PAGES_IN_VIEW]
    if not in_view:
        return None
    if len(in_view) == 1:
        return f"Page {in_view[0]} of this document is in view now; no need to fetch it again."
    return (
        f"Pages {_page_ranges(in_view)} of this document are all in view now; "
        "no need to fetch them again."
    )


def _receipt(entries: Sequence[_Entry], reads: Sequence[FigureRead]) -> str | None:
    """重看回执(第 3 点):这次读的里面本 run 已经读过的那些,第几次读。"""
    seen: list[tuple[FigureRead, int]] = []
    for read in reads:
        before = sum(1 for e in entries if (e.read.unit, e.read.region) == (read.unit, read.region))
        if before:
            seen.append((read, before + 1))
    if not seen:
        return None
    if len(seen) == 1:
        read, nth = seen[0]
        head = f"You already viewed {_describe(read)} in this task (read #{nth})."
    else:
        items = "; ".join(f"{_describe(read)} (read #{nth})" for read, nth in seen)
        head = f"You already viewed {items} in this task."
    parts = [head, _in_view_sentence(entries, reads), _THINKING_NOTE]
    return " ".join(p for p in parts if p)


def read_notice(
    ledger: Sequence[object],
    *,
    run_id: UUID | None,
    doc_sha: str,
    reads: Sequence[FigureRead],
    pages: int | None = None,
) -> str | None:
    """渲染成功之后附在回执后面的平台文字:重看回执(第 3 点)+ 超上限预告(第 5 点)。

    只给能直接看图的模型(``read_page`` 的 ``inline`` 投递)—— 「在你眼前」对别的投递是假话。
    """
    entries = _doc_entries(ledger, run_id, doc_sha)
    parts: list[str] = []
    total = _known_pages(entries, pages)
    if not entries and total is not None and total > FIGURE_PAGES_IN_VIEW:
        parts.append(
            f"This document has {total} pages, more than the {FIGURE_PAGES_IN_VIEW} pages that "
            "can be in view at once. Write the numbers into a workspace file as you read, then "
            "calculate from the file."
        )
    receipt = _receipt(entries, reads)
    if receipt is not None:
        parts.append(receipt)
    return " ".join(parts) or None
