"""Inspect a .pptx: layouts of every slide master (with placeholders) and existing slides.

``prs.slide_layouts`` only covers the first master; institutional templates often carry
several, so layouts are listed per master: entry ``{"master": m, "index": i, ...}`` is
``prs.slide_masters[m].slide_layouts[i]`` (for ``master == 0`` also ``prs.slide_layouts[i]``).

``--texts`` lists instead, per existing slide, ``texts`` (every shape carrying text,
groups and table cells included, addressed for fill_template.py) and ``locked`` (charts,
SmartArt, pictures, ... whose text this tooling cannot edit), as compact JSON; the
``legend`` key explains the fields. ``--slides 3,5-7`` limits either output to those slides.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pptx
from _cli import emit_json, fail, resolve_io
from _pptx_text import (
    EMU_PER_CM,
    EMU_PER_PT,
    LATIN_PER_CJK,
    NO_TEXT_PLACEHOLDER,
    Target,
    estimate,
    para_props,
    paragraphs,
    paragraphs_text,
    walk_slide,
)
from pptx.oxml.ns import qn

LOCKED_KINDS = {"picture", "chart", "smartart", "ole", "graphic", "other", NO_TEXT_PLACEHOLDER}
PREVIEW_CHARS = 30
CELL_PREVIEW_CHARS = 12
LEGEND = (
    "texts: addr=fill_template.py 的地址(页:形状id[/组内id][@行,列]); box=[左,上,宽,高]厘米; "
    "kind 省略=文本框; text=开头预览(--full 看全文); paras=段数(1 段时省略); "
    "font_pt=字号(各段不同时逐段列出,"
    f"null=文件里没写死); cap=按当前字号估计约能放的汉字数(西文约 x{LATIN_PER_CJK}); "
    "autofit: shrink=溢出时缩字 resize=框随文字变高 off=不调整; nowrap=不自动换行; "
    "vertical=竖排; hidden=隐藏的形状; ambiguous=同页有重复 id, 这个地址填不了; "
    "error=这个形状读不了。表格 cells 逐行列出, null=被合并的格, col_chars=各列每行约几个汉字。"
    "locked: 文字改不了的形状(图表/SmartArt/图片/没有文字框的占位符等)。"
)


def _geometry(ph) -> dict:
    # inherited geometry: python-pptx raises KeyError for a few placeholder types it
    # cannot map to the master (see _pptx_text._base) -> same as "no fixed position"
    try:
        return {"left": ph.left, "top": ph.top, "width": ph.width, "height": ph.height}
    except KeyError:
        return dict.fromkeys(("left", "top", "width", "height"))


def _layout_info(master: int, index: int, layout) -> dict:
    placeholders = [
        {
            "idx": ph.placeholder_format.idx,
            "type": str(ph.placeholder_format.type),
            "name": ph.name,
            **_geometry(ph),
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


def _slide_info(number: int, slide, master_of: dict) -> dict:
    return {
        "number": number,
        "master": master_of.get(slide.slide_layout.slide_master.part.partname),
        "layout": slide.slide_layout.name,
        "shapes": [_shape_info(shape) for shape in slide.shapes],
    }


# ---------------------------------------------------------------- --texts


def _cm(box) -> list[float | int] | None:
    return None if box is None else [_num(round(v / EMU_PER_CM, 1)) for v in box]


def _clip(text: str, limit: int | None) -> str:
    return text if limit is None or len(text) <= limit else text[:limit] + "…"


def _num(v: float | None) -> float | int | None:
    return int(v) if v is not None and v == int(v) else v


def _sizes(t: Target) -> float | list | None:
    sizes = [_num(para_props(p, t.chain).size_pt) for p in paragraphs(t.tx_body)]
    return sizes[0] if len(set(sizes)) <= 1 else sizes


def _cap(t: Target) -> int | None:
    fit = estimate(t)
    return None if fit is None else fit.cap_chars


def _flags(t: Target, entry: dict) -> dict:
    for flag in ("vertical", "hidden", "ambiguous"):
        if getattr(t, flag):
            entry[flag] = True
    return entry


def _text_entry(t: Target, limit: int | None) -> dict:
    entry = {"addr": t.addr}
    if t.kind != "textbox":  # defaults are left out to keep the output small
        entry["kind"] = t.kind
    entry["box"] = _cm(t.box)
    entry["text"] = _clip(paragraphs_text(t.tx_body), limit)
    paras = len(t.tx_body.findall(qn("a:p")))
    if paras != 1:
        entry["paras"] = paras
    entry["font_pt"] = _sizes(t)
    entry["cap"] = _cap(t)
    if t.autofit:
        scaled = t.autofit == "shrink" and t.scale < 1
        entry["autofit"] = f"shrink:{round(t.scale * 100)}%" if scaled else t.autofit
    if not t.wrap:
        entry["nowrap"] = True
    return _flags(t, entry)


def _cell_size(c: Target) -> float | None:
    return _num(para_props(paragraphs(c.tx_body)[0], c.chain).size_pt)


def _table_entry(table: Target, cells: list[Target], limit: int | None) -> dict:
    rows = max(int(c.addr.rsplit("@", 1)[1].split(",")[0]) for c in cells)
    cols = len(cells) // rows
    grid = [cells[r * cols : (r + 1) * cols] for r in range(rows)]
    sample = grid[min(1, rows - 1)]  # first body row: header rows are often styled apart
    col_chars = []
    for c in sample:
        inner = (c.box[2] - c.insets[0] - c.insets[2]) / EMU_PER_PT
        col_chars.append(max(0, int(inner // (_cell_size(c) or 18.0))))
    sizes = {_cell_size(c) for c in cells if not c.merged}
    return _flags(
        table,
        {
            "addr": table.addr,
            "kind": "table",
            "box": _cm(table.box),
            "size": [rows, cols],
            "cells": [
                [None if c.merged else _clip(paragraphs_text(c.tx_body), limit) for c in row]
                for row in grid
            ],
            "font_pt": sizes.pop() if len(sizes) == 1 else sorted(sizes, key=lambda s: s or 0),
            "col_chars": col_chars,
        },
    )


def _entry(t: Target, targets: list[Target], full: bool) -> dict:
    if t.kind == "error":
        return {"addr": t.addr, "error": t.error}
    try:
        if t.kind == "table":
            cells = [c for c in targets if c.table == t.addr]
            return _table_entry(t, cells, None if full else CELL_PREVIEW_CHARS)
        return _text_entry(t, None if full else PREVIEW_CHARS)
    except Exception as exc:  # reported per shape, never swallowed
        return {"addr": t.addr, "error": f"{type(exc).__name__}: {exc}"[:160]}


def _text_info(targets: list[Target], full: bool) -> tuple[list[dict], list[dict]]:
    texts, locked = [], []
    for t in targets:
        if t.kind == "cell":
            continue
        if t.kind in LOCKED_KINDS:
            locked.append(_flags(t, {"addr": t.addr, "kind": t.kind, "box": _cm(t.box)}))
        elif t.kind in ("table", "error") or (t.tx_body is not None and t.listed):
            texts.append(_entry(t, targets, full))
    return texts, locked


def _texts_slide(number: int, slide, master_of: dict, default_style, full: bool) -> dict:
    texts, locked = _text_info(walk_slide(slide, number, default_style), full)
    return {
        "number": number,
        "master": master_of.get(slide.slide_layout.slide_master.part.partname),
        "layout": slide.slide_layout.name,
        "texts": texts,
        "locked": locked,
    }


def _pick_slides(spec: str | None, total: int) -> list[int]:
    if spec is None:
        return list(range(1, total + 1))
    picked: list[int] = []
    for part in spec.split(","):
        lo, _, hi = part.strip().partition("-")
        if not lo.isdigit() or (hi and not hi.isdigit()):
            fail(f"--slides 格式不对: {spec!r}, 应为 3,5-7")
        first, last = int(lo), int(hi or lo)
        if not 1 <= first <= last <= total:
            fail(f"--slides {part.strip()} 超出范围: 共 {total} 页")
        picked += [n for n in range(first, last + 1) if n not in picked]
    return sorted(picked)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    ap.add_argument(
        "--texts",
        action="store_true",
        help="只列已有页的 texts / locked(套已排好版的模板时用)",
    )
    ap.add_argument("--full", action="store_true", help="--texts 输出全文，不截断预览")  # noqa: RUF001
    ap.add_argument("--slides", help="只看这些页, 如 3,5-7")
    args = ap.parse_args()
    src, _ = resolve_io(args.input, None)
    prs = pptx.Presentation(str(src))
    masters = list(prs.slide_masters)
    master_of = {master.part.partname: m for m, master in enumerate(masters)}
    slides = list(prs.slides)
    numbers = _pick_slides(args.slides, len(slides))
    if not args.texts:
        emit_json(
            {
                "slide_size": [prs.slide_width, prs.slide_height],
                "layouts": [
                    _layout_info(m, i, layout)
                    for m, master in enumerate(masters)
                    for i, layout in enumerate(master.slide_layouts)
                ],
                "slides": [_slide_info(n, slides[n - 1], master_of) for n in numbers],
            }
        )
        return
    default_style = prs.part._element.find(qn("p:defaultTextStyle"))
    out = {
        "slide_size": [prs.slide_width, prs.slide_height],
        "legend": LEGEND,
        "slides": [
            _texts_slide(n, slides[n - 1], master_of, default_style, args.full) for n in numbers
        ],
    }
    print(json.dumps(out, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
