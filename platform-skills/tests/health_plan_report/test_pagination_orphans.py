"""Sparse pagination (live-fix defect 3): a split table keeps >= 2 rows on each side, a split
paragraph keeps >= 3 lines on each side, otherwise the element moves whole."""

from pathlib import Path

import pytest
from hpr.measure import Measurer
from hpr.ppt_layout import BODY_TOP, lh, make_ctx, paginate
from hpr.prims import Paragraph, SubHeading, Table
from hpr.style import Layer, resolve
from hpr.theme import build_theme

M = Measurer("/nonexistent")


def _ctx(scale):
    style = resolve([Layer("x", {"type.scale": scale})]).style
    return make_ctx({"brand": {}}, build_theme(style, "pptx"), M, Path("."))


def _filler(n: int) -> Paragraph:
    return Paragraph("\n".join(f"填充第{i}行" for i in range(n)))


def _pages(ctx, prims):
    sec = {"id": "s", "title": "章节", "blocks": []}
    return paginate([(sec, [(p, f"sections[0].blocks[{i}]") for i, p in enumerate(prims)])], ctx)


def _per_page(ctx) -> int:
    return int((ctx.body_bottom - BODY_TOP) // lh(ctx.theme.body))


TABLE = Table(("日期", "安排"), tuple((f"第 {i} 天", "快走 30 分钟") for i in range(6)))


@pytest.mark.parametrize("scale", ["standard", "large"])
def test_split_table_keeps_two_rows_on_each_side(scale):
    ctx = _ctx(scale)
    for n in range(1, _per_page(ctx)):
        pages = _pages(ctx, [_filler(n), TABLE])
        pieces = [pl.prim for pg in pages for pl in pg.placed if isinstance(pl.prim, Table)]
        assert sum(len(p.rows) for p in pieces) == len(TABLE.rows)
        if len(pieces) > 1:
            assert min(len(p.rows) for p in pieces) >= 2, (n, [len(p.rows) for p in pieces])


@pytest.mark.parametrize("scale", ["standard", "large"])
def test_split_paragraph_leaves_no_one_or_two_line_stub(scale):
    ctx = _ctx(scale)
    text = "\n".join(f"第{i}行内容" for i in range(8))
    for n in range(1, _per_page(ctx)):
        pages = _pages(ctx, [_filler(n), Paragraph(text)])
        pieces = [pl.prim.text for pg in pages for pl in pg.placed if pl.prim.text.startswith("第")]
        assert "\n".join(pieces) == text
        if len(pieces) > 1:
            assert min(p.count("\n") + 1 for p in pieces) >= 3, (n, pieces)


@pytest.mark.parametrize("scale", ["standard", "large"])
def test_heading_is_not_left_behind_when_its_table_moves(scale):
    ctx = _ctx(scale)
    for n in range(1, _per_page(ctx)):
        pages = _pages(ctx, [_filler(n), SubHeading("B 模板"), TABLE])
        for pg in pages:
            assert not isinstance(pg.placed[-1].prim, SubHeading), n


def test_table_taller_than_a_page_still_splits():
    ctx = _ctx("large")
    big = Table(("日期", "安排"), tuple((f"第 {i} 天", "快走") for i in range(80)))
    pages = _pages(ctx, [big])
    pieces = [pl.prim for pg in pages for pl in pg.placed]
    assert sum(len(p.rows) for p in pieces) == 80
    assert min(len(p.rows) for p in pieces) >= 2


def test_a_two_row_table_is_never_split():
    ctx = _ctx("large")
    two = Table(("日期", "安排"), (("周一", "快走"), ("周二", "快走")))
    for n in range(1, _per_page(ctx)):
        pages = _pages(ctx, [_filler(n), two])
        assert [pl.prim for pg in pages for pl in pg.placed if isinstance(pl.prim, Table)] == [two]


def test_stub_is_accepted_only_when_the_table_cannot_fit_otherwise():
    ctx = _ctx("large")
    per = _per_page(ctx)
    tall = "\n".join(["说明"] * int(per * 0.4))
    three = Table(("日期", "安排"), (("周一", tall), ("周二", tall), ("周三", tall)))
    pages = _pages(ctx, [three])
    pieces = [pl.prim for pg in pages for pl in pg.placed]
    assert sum(len(p.rows) for p in pieces) == 3 and len(pieces) >= 2
