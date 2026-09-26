"""Inspect a .pptx: layouts of every slide master (with placeholders) and existing slides.

``prs.slide_layouts`` only covers the first master; institutional templates often carry
several, so layouts are listed per master: entry ``{"master": m, "index": i, ...}`` is
``prs.slide_masters[m].slide_layouts[i]`` (for ``master == 0`` also ``prs.slide_layouts[i]``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pptx
from _cli import emit_json, resolve_io


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


def _slide_info(number: int, slide, master_of: dict) -> dict:
    return {
        "number": number,
        "master": master_of.get(slide.slide_layout.slide_master.part.partname),
        "layout": slide.slide_layout.name,
        "shapes": [_shape_info(shape) for shape in slide.shapes],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    args = ap.parse_args()
    src, _ = resolve_io(args.input, None)
    prs = pptx.Presentation(str(src))
    masters = list(prs.slide_masters)
    master_of = {master.part.partname: m for m, master in enumerate(masters)}
    emit_json(
        {
            "slide_size": [prs.slide_width, prs.slide_height],
            "layouts": [
                _layout_info(m, i, layout)
                for m, master in enumerate(masters)
                for i, layout in enumerate(master.slide_layouts)
            ],
            "slides": [
                _slide_info(i, slide, master_of) for i, slide in enumerate(prs.slides, start=1)
            ],
        }
    )


if __name__ == "__main__":
    main()
