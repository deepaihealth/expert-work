"""Inspect a .pptx: layouts of every slide master (with placeholders) and existing slides.

``prs.slide_layouts`` only covers the first master; institutional templates often carry
several, so layouts are listed per master: entry ``{"master": m, "index": i, ...}`` is
``prs.slide_masters[m].slide_layouts[i]`` (for ``master == 0`` also ``prs.slide_layouts[i]``).

Each slide also lists ``texts`` (every shape carrying text, groups and table cells
included, addressed for fill_template.py) and ``locked`` (charts, SmartArt, pictures,
... whose text this tooling cannot edit). The ``legend`` key explains the fields.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pptx
from _cli import emit_json, resolve_io
from _pptx_text import (
    EMU_PER_CM,
    EMU_PER_PT,
    LATIN_PER_CJK,
    Target,
    estimate,
    para_props,
    paragraphs_text,
    run_count,
    walk_slide,
)
from pptx.oxml.ns import qn

LOCKED_KINDS = {"picture", "chart", "smartart", "ole", "graphic", "other"}
PREVIEW_CHARS = 30
CELL_PREVIEW_CHARS = 12
LEGEND = (
    "texts: addr=fill_template.py 的地址(页:形状id[/组内id][@行,列]); box=[左,上,宽,高]厘米; "
    "text=开头预览(--full 看全文); paras/runs=段数/文字段数; font_pt=字号(各段不同时逐段列出,"
    f"null=文件里没写死); cap=按当前字号估计约能放的汉字数(西文约 x{LATIN_PER_CJK}); "
    "autofit: shrink=溢出时缩字 resize=框随文字变高 off=不调整; nowrap=不自动换行; "
    "表格 cells 逐行列出, null=被合并的格, col_chars=各列每行约几个汉字。"
    "locked: 文字改不了的形状(图表/SmartArt/图片等)。"
)


def _layout_info(master: int, index: int, layout) -> dict:
    placeholders = [
        {
            "idx": ph.placeholder_format.idx,
            "type": str(ph.placeholder_format.type),
            "name": ph.name,
            "left": ph.left,
            "top": ph.top,
            "width": ph.width,
            "height": ph.height,
        }
        for ph in layout.placeholders
    ]
    return {"master": master, "index": index, "name": layout.name, "placeholders": placeholders}


def _shape_info(shape) -> dict:
    shape_type = shape.shape_type
    has_text = bool(shape.has_text_frame and shape.text_frame.text)
    return {
        "name": shape.name,
        "type": "unknown" if shape_type is None else str(shape_type),
        "has_text": has_text,
    }


def _cm(box) -> list[float] | None:
    return None if box is None else [round(v / EMU_PER_CM, 1) for v in box]


def _clip(text: str, limit: int | None) -> str:
    return text if limit is None or len(text) <= limit else text[:limit] + "…"


def _num(v: float | None) -> float | int | None:
    return int(v) if v is not None and v == int(v) else v


def _sizes(t: Target) -> float | list | None:
    sizes = [_num(para_props(p, t.chain).size_pt) for p in t.tx_body.findall(qn("a:p"))]
    return sizes[0] if len(set(sizes)) <= 1 else sizes


def _text_entry(t: Target, limit: int | None) -> dict:
    entry = {
        "addr": t.addr,
        "name": t.name,
        "kind": t.kind,
        "box": _cm(t.box),
        "text": _clip(paragraphs_text(t.tx_body), limit),
        "paras": len(t.tx_body.findall(qn("a:p"))),
        "runs": run_count(t.tx_body),
        "font_pt": _sizes(t),
    }
    fit = estimate(t)
    entry["cap"] = None if fit is None else fit.cap_chars
    if t.autofit:
        scaled = t.autofit == "shrink" and t.scale < 1
        entry["autofit"] = f"shrink:{round(t.scale * 100)}%" if scaled else t.autofit
    if not t.wrap:
        entry["nowrap"] = True
    return entry


def _table_entry(table: Target, cells: list[Target], limit: int | None) -> dict:
    rows = max(int(c.addr.rsplit("@", 1)[1].split(",")[0]) for c in cells)
    cols = len(cells) // rows
    grid = [cells[r * cols : (r + 1) * cols] for r in range(rows)]
    sample = grid[min(1, rows - 1)]  # first body row: header rows are often styled apart
    col_chars = []
    for c in sample:
        size = para_props(c.tx_body.find(qn("a:p")), c.chain).size_pt or 18.0
        inner = (c.box[2] - c.insets[0] - c.insets[2]) / EMU_PER_PT
        col_chars.append(max(0, int(inner // size)))
    sizes = {
        _num(para_props(c.tx_body.find(qn("a:p")), c.chain).size_pt) for c in cells if not c.merged
    }
    return {
        "addr": table.addr,
        "name": table.name,
        "kind": "table",
        "box": _cm(table.box),
        "size": [rows, cols],
        "cells": [
            [None if c.merged else _clip(paragraphs_text(c.tx_body), limit) for c in row]
            for row in grid
        ],
        "font_pt": sizes.pop() if len(sizes) == 1 else sorted(sizes, key=lambda s: s or 0),
        "col_chars": col_chars,
    }


def _text_info(targets: list[Target], full: bool) -> tuple[list[dict], list[dict]]:
    texts, locked = [], []
    for t in targets:
        if t.kind == "table":
            cells = [c for c in targets if c.table == t.addr]
            texts.append(_table_entry(t, cells, None if full else CELL_PREVIEW_CHARS))
        elif t.kind == "cell":
            continue
        elif t.kind in LOCKED_KINDS:
            locked.append({"addr": t.addr, "name": t.name, "kind": t.kind, "box": _cm(t.box)})
        elif t.tx_body is not None and t.listed:
            texts.append(_text_entry(t, None if full else PREVIEW_CHARS))
    return texts, locked


def _slide_info(number: int, slide, master_of: dict, default_style, args) -> dict:
    texts, locked = _text_info(walk_slide(slide, number, default_style), args.full)
    info = {
        "number": number,
        "master": master_of.get(slide.slide_layout.slide_master.part.partname),
        "layout": slide.slide_layout.name,
    }
    if not args.texts:
        info["shapes"] = [_shape_info(shape) for shape in slide.shapes]
    return {**info, "texts": texts, "locked": locked}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    ap.add_argument("--full", action="store_true", help="输出全文，不截断预览")  # noqa: RUF001
    ap.add_argument(
        "--texts",
        action="store_true",
        help="只列已有页的 texts / locked(套已排好版的模板时用)",
    )
    args = ap.parse_args()
    src, _ = resolve_io(args.input, None)
    prs = pptx.Presentation(str(src))
    masters = list(prs.slide_masters)
    master_of = {master.part.partname: m for m, master in enumerate(masters)}
    default_style = prs.part._element.find(qn("p:defaultTextStyle"))
    out = {"slide_size": [prs.slide_width, prs.slide_height], "legend": LEGEND}
    if not args.texts:
        out["layouts"] = [
            _layout_info(m, i, layout)
            for m, master in enumerate(masters)
            for i, layout in enumerate(master.slide_layouts)
        ]
    out["slides"] = [
        _slide_info(i, slide, master_of, default_style, args)
        for i, slide in enumerate(prs.slides, start=1)
    ]
    emit_json(out)


if __name__ == "__main__":
    main()
