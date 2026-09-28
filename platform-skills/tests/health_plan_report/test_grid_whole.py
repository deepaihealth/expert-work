"""A card grid is one topic: when it fits on a page it is never split across pages (live
replay: five goal cards, three at the foot of one page and two on the next). Only a grid
taller than a whole page splits between rows."""

from pathlib import Path

import pytest
from hpr.measure import Measurer
from hpr.ppt_layout import BODY_TOP, BODY_W, lh, make_ctx, measure, paginate
from hpr.prims import Card, CardGrid, Paragraph, SubHeading
from hpr.style import Layer, resolve
from hpr.theme import build_theme

M = Measurer("/nonexistent")


def _ctx(scale):
    style = resolve([Layer("x", {"type.scale": scale})]).style
    return make_ctx({"brand": {}}, build_theme(style, "pptx"), M, Path("."))


def _grid(n: int) -> CardGrid:
    return CardGrid(
        tuple(
            Card(title=f"目标{i}", value="72.5 → 71.0", unit="kg", lines=("第 2 周末",))
            for i in range(n)
        ),
        3,
    )


def _pages(ctx, prims):
    sec = {"id": "s", "title": "章节", "blocks": []}
    return paginate([(sec, [(p, f"sections[0].blocks[{i}]") for i, p in enumerate(prims)])], ctx)


def _grid_pages(pages) -> list[int]:
    return [i for i, pg in enumerate(pages) for pl in pg.placed if isinstance(pl.prim, CardGrid)]


@pytest.mark.parametrize("scale", ["standard", "large"])
def test_grid_that_fits_a_page_is_never_split(scale):
    ctx = _ctx(scale)
    grid = _grid(5)
    per_page = int((ctx.body_bottom - BODY_TOP) // lh(ctx.theme.body))
    assert measure(grid, BODY_W, ctx) <= ctx.body_bottom - BODY_TOP
    for n in range(1, per_page):
        filler = Paragraph("\n".join(f"填充第{i}行" for i in range(n)))
        pages = _pages(ctx, [filler, SubHeading("阶段目标"), grid])
        on = _grid_pages(pages)
        assert len(on) == 1, (n, on)  # one piece, on one page
        heading = [
            i for i, pg in enumerate(pages) for pl in pg.placed if isinstance(pl.prim, SubHeading)
        ]
        assert heading == on, (n, heading, on)  # the heading travels with it


@pytest.mark.parametrize("scale", ["standard", "large"])
def test_grid_taller_than_a_page_still_splits_between_rows(scale):
    ctx = _ctx(scale)
    grid = _grid(30)
    assert measure(grid, BODY_W, ctx) > ctx.body_bottom - BODY_TOP
    pieces = [pl.prim for pg in _pages(ctx, [grid]) for pl in pg.placed]
    assert len(pieces) > 1 and sum(len(p.cards) for p in pieces) == 30
