"""B-268 —— 本轮的 ``read_page`` 读取账:重看回执、超上限预告、熔断。

评测(E2,11-03,合成纯图片表格 PDF):旧的 3 槽窗口让 4 页 / 6 页文档按「最早那页退出 →
重看」严格轮转到步数上限,写到某页时它已不在视野里,数字就编出来。窗口本身的放宽在
``graph_builder.figure_block``;这里是 ``read_page`` 那一侧:

* 账记在 ``state["figure_reads"]``(工具可写的通道,读的一侧逐条校验),每条是一次渲染:
  ``{"run", "doc", "unit", "region", "pages"}``。**按轮计**(第 7 点):``run`` 是这一轮的首段
  run(审批续跑段是新 run_id,计数要接着算 —— ``ToolContext.turn_root_run_id``);主代的
  ``viewed_figures`` 跨轮累积,下一轮的正常重看不能被上一轮的次数挡住 —— 通道的 reducer
  (``state._merge_figure_reads``)只留最新那一轮的条目,别的轮的条目读的时候也不算。
* 同一 ``(文档, 编号, 局部)`` 本轮已渲 :data:`FIGURE_READS_PER_ITEM` 次,再读就不渲;同一文档
  的整页本轮渲染次数到 ``2 x 页数 + 3`` 不再渲;局部图另有每份文档
  :data:`FIGURE_CROPS_PER_DOCUMENT` 张的预算(B-266 的放大局部要能用)。一次调用里只拒超了的
  编号,其余照渲;**整次都没渲**才计入防循环次数(``LOOP_TRIP_META_KEY``),
  ``DEFAULT_LOOP_TRIPS_PER_RUN`` 次后 run 收尾。依据:正常 run 每页最多重看 1 次,E2 里最后
  核对才到第 3 次。
* 页数取自渲染片段回的 ``pages``(``pdfinfo``,拿不到就没有);没有页数就不按整页总数拒。
* 给模型的话一律用 ``read_page`` 参数的叫法 ``unit``(docx 的 unit 是图清单编号,不是页号)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

#: 正在读的那份文档同时可见的整页数上限(``figure_block`` 的整页道)。
FIGURE_PAGES_IN_VIEW: Final = 6
#: 别的文档的整页只分旧窗口(3 页)里正在读的那份没占用的位置 —— 只有正在读的那份放宽。
FIGURE_OTHER_PAGES_IN_VIEW: Final = 3
#: 局部图(``read_page(region=…)``)单独一条道的上限,只挤局部图、不挤整页。
FIGURE_CROPS_IN_VIEW: Final = 2
#: 同一 ``(文档, 编号, 局部)`` 本轮最多渲几次;再读就拒绝。
FIGURE_READS_PER_ITEM: Final = 3
#: 同一文档本轮整页渲染次数上限 = ``页数 x _DOC_RENDERS_PER_PAGE + _DOC_RENDERS_SLACK``。
_DOC_RENDERS_PER_PAGE: Final = 2
_DOC_RENDERS_SLACK: Final = 3
#: 同一文档本轮局部图上限。12 = 一页密集文档按横条扫一遍(``region="auto"`` 最多 8 条)再加
#: 4 张定点放大;一页扫描件「整页 + 4 张局部」远在其内。E2 里轮流挤整页的那两次是 16 / 17 张。
FIGURE_CROPS_PER_DOCUMENT: Final = 12

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
    """一次渲染:模型传的编号、局部框(整页是 ``None``)。"""

    unit: int
    region: Box | None = None


@dataclass(frozen=True)
class ReadPlan:
    """这次调用渲哪些编号;``refused`` 是没渲的那些的说明(平台文字,``None`` = 都渲)。"""

    units: tuple[int, ...]
    refused: str | None = None


@dataclass(frozen=True)
class _Entry:
    doc: str
    read: FigureRead
    pages: int | None


def ledger_entries(
    run_id: UUID, doc_sha: str, reads: Sequence[FigureRead], pages: int | None
) -> list[dict[str, Any]]:
    """这次调用要追加进 ``figure_reads`` 的条目,一次渲染一条;``run_id`` 是这一轮的首段。"""
    return [
        {
            "run": str(run_id),
            "doc": doc_sha,
            "unit": read.unit,
            "region": list(read.region) if read.region is not None else None,
            "pages": pages,
        }
        for read in reads
    ]


def _plain_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse_entry(raw: object, run: str) -> _Entry | None:
    """一条账目;不是本轮的、形状不对的都当没有(通道工具可写,不可信)。"""
    if not isinstance(raw, Mapping) or raw.get("run") != run:
        return None
    doc, unit = raw.get("doc"), _plain_int(raw.get("unit"))
    if not isinstance(doc, str) or unit is None:
        return None
    region_raw = raw.get("region")
    region: Box | None = None
    if region_raw is not None:
        ints = [_plain_int(v) for v in region_raw] if isinstance(region_raw, list) else []
        box = [v for v in ints if v is not None]
        if len(ints) != 4 or len(box) != 4:
            return None
        region = (box[0], box[1], box[2], box[3])
    return _Entry(doc=doc, read=FigureRead(unit, region), pages=_plain_int(raw.get("pages")))


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


def _ranges(units: Sequence[int]) -> str:
    """``[1, 2, 3, 5]`` → ``"1-3, 5"``(先去重排序)。"""
    ordered = sorted(set(units))
    runs: list[tuple[int, int]] = []
    for unit in ordered:
        if runs and unit == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], unit)
        else:
            runs.append((unit, unit))
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def _units(units: Sequence[int]) -> str:
    """``unit 3`` / ``units 1-2, 5``。"""
    return f"unit {units[0]}" if len(set(units)) == 1 else f"units {_ranges(units)}"


def _describe(read: FigureRead) -> str:
    if read.region is None:
        return f"unit {read.unit}"
    return f"the region {list(read.region)} of unit {read.unit}"


def _times(entries: Sequence[_Entry], read: FigureRead) -> int:
    return sum(1 for e in entries if e.read == read)


def _plan_pages(entries: Sequence[_Entry], units: Sequence[int]) -> ReadPlan:
    whole = [e for e in entries if e.read.region is None]
    pages = _known_pages(entries)
    left = (
        None if pages is None else _DOC_RENDERS_PER_PAGE * pages + _DOC_RENDERS_SLACK - len(whole)
    )
    allowed: list[int] = []
    repeated: list[int] = []
    over: list[int] = []
    for unit in units:
        if _times(whole, FigureRead(unit)) >= FIGURE_READS_PER_ITEM:
            repeated.append(unit)
        elif left is not None and len(allowed) >= left:
            over.append(unit)
        else:
            allowed.append(unit)
    notes: list[str] = []
    if repeated:
        notes.append(
            f"Not rendered: {_units(repeated)} - already read {FIGURE_READS_PER_ITEM} times in "
            "this task."
        )
    if over:
        notes.append(
            f"Not rendered: {_units(over)} - this document's pages were already rendered "
            f"{len(whole)} times in this task, the most allowed for its {pages} pages."
        )
    return ReadPlan(tuple(allowed), " ".join([*notes, _REFUSAL_TAIL]) if notes else None)


def _plan_crop(entries: Sequence[_Entry], unit: int, region: Box) -> ReadPlan:
    crops = [e for e in entries if e.read.region is not None]
    read = FigureRead(unit, region)
    if _times(crops, read) >= FIGURE_READS_PER_ITEM:
        why = f"already read {FIGURE_READS_PER_ITEM} times in this task"
    elif len(crops) >= FIGURE_CROPS_PER_DOCUMENT:
        why = (
            f"{len(crops)} zoomed regions of this document were already rendered in this task, "
            "the most allowed"
        )
    else:
        return ReadPlan((unit,))
    return ReadPlan((), f"Not rendered: {_describe(read)} - {why}. {_REFUSAL_TAIL}")


def plan_reads(
    ledger: Sequence[object],
    *,
    run_id: UUID | None,
    doc_sha: str,
    units: Sequence[int],
    region: Box | None,
) -> ReadPlan:
    """渲染之前按账决定这次渲哪些编号(``region`` 时恰好一个编号);超了的编号不渲、写进
    ``refused``。``units`` 为空 = 整次都没渲(调用方计一次防循环)。"""
    entries = _doc_entries(ledger, run_id, doc_sha)
    if region is None:
        return _plan_pages(entries, units)
    return _plan_crop(entries, units[0], region)


def _in_view_sentence(entries: Sequence[_Entry], reads: Sequence[FigureRead]) -> str | None:
    """本轮读过的这份文档的整页里,最新的 :data:`FIGURE_PAGES_IN_VIEW` 个编号 —— 刚读完它就是
    正在读的那份,整页窗口(``figure_block``)里一定有它们。"""
    reads_so_far = [*(e.read for e in entries), *reads]
    newest_first = [r.unit for r in reversed(reads_so_far) if r.region is None]
    in_view = list(dict.fromkeys(newest_first))[:FIGURE_PAGES_IN_VIEW]
    if not in_view:
        return None
    if len(in_view) == 1:
        return f"Unit {in_view[0]} of this document is in view now; no need to fetch it again."
    return (
        f"Units {_ranges(in_view)} of this document are all in view now; "
        "no need to fetch them again."
    )


def _receipt(entries: Sequence[_Entry], reads: Sequence[FigureRead]) -> str | None:
    """重看回执(第 3 点):这次读的里面本轮已经读过的那些,第几次读。"""
    seen = [(read, _times(entries, read) + 1) for read in dict.fromkeys(reads)]
    seen = [(read, nth) for read, nth in seen if nth > 1]
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
