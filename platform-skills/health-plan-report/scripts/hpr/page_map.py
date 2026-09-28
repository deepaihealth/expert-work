"""Which pages each part of the report lands on, so a visual check opens the right page instead
of guessing (live replays spent looks on the wrong pages). Page numbers are 1-based, as the
viewer and ``read_page`` count them."""

from __future__ import annotations

import re
from typing import Any

from hpr.common import CLIENT_BAND_TITLE

_SECTION_PATH = re.compile(r"^sections\[(\d+)\]")
PDF_HEAD_PREFIX = "head-"  # a PDF section heading: id="head-<section anchor>"
PDF_END_PREFIX = "end-"  # the empty marker closing a PDF section: id="end-<section anchor>"
PDF_TOC_ID = "hpr-toc"


def _entries(content: dict, spans: dict[str, list[int]]) -> list[dict[str, Any]]:
    titles = {s["id"]: s["title"] for s in content["sections"]}
    out: list[dict[str, Any]] = []
    for key, pages in spans.items():
        if key in titles:
            out.append({"section": key, "title": titles[key], "pages": pages})
        else:
            out.append({"title": key, "pages": pages})
    return out


def pptx_page_map(
    content: dict, pages: list[Any], band_id: str, toc: bool, end: bool
) -> list[dict[str, Any]]:
    """``pages`` are the paginated body slides (each with ``section_id`` and ``placed``)."""
    ids = [s["id"] for s in content["sections"]]
    spans: dict[str, list[int]] = {"封面": [1]}
    if toc:
        spans["目录"] = [2]
    first = 2 + int(toc)
    for i, page in enumerate(pages):
        keys = [page.section_id]
        for pl in page.placed:
            m = _SECTION_PATH.match(pl.path)
            keys.append(ids[int(m.group(1))] if m else band_id)
        for key in dict.fromkeys(keys):
            spans.setdefault(CLIENT_BAND_TITLE if key == band_id else key, []).append(first + i)
    if end:
        spans["封底"] = [first + len(pages)]
    return _entries(content, spans)


def pdf_page_map(content: dict, document: Any, band_anchor: str) -> list[dict[str, Any]]:
    """``document`` is a rendered weasyprint Document. A section starts on the page that shows
    its heading (``head-<id>``) and ends on the page of its empty end marker (``end-<id>``).
    When a heading moves on with what follows it, weasyprint still leaves an empty fragment of
    the section and of the heading, anchors included, at the foot of the page before (seen in
    the real image) -- so a heading counts on the last page it is anchored on."""
    first: dict[str, int] = {}
    last: dict[str, int] = {}
    for n, page in enumerate(document.pages, start=1):
        for anchor in page.anchors:
            first.setdefault(anchor, n)
            last[anchor] = n
    spans: dict[str, list[int]] = {"封面": [1]}
    if PDF_TOC_ID in first:
        spans["目录"] = [first[PDF_TOC_ID]]
    parts = [(band_anchor, CLIENT_BAND_TITLE)] + [
        (f"sec-{s['id']}", s["id"]) for s in content["sections"]
    ]
    for anchor, key in parts:
        head = PDF_HEAD_PREFIX + anchor
        if head in last:
            start = last[head]
            end = max(first.get(PDF_END_PREFIX + anchor, start), start)
            spans[key] = list(range(start, end + 1))
    return _entries(content, spans)
