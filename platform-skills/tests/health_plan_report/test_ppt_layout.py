from pathlib import Path

import pytest
from hpr.blocks import section_prims
from hpr.measure import Measurer
from hpr.ppt_layout import (
    BODY_TOP,
    LayoutError,
    Page,
    make_ctx,
    measure,
    paginate,
    split,
)
from hpr.prims import Bullets, Card, CardGrid, Chart, Column, Columns, Paragraph, SubHeading, Table
from hpr.style import resolve
from hpr.theme import build_theme


def _ctx(content=None, style=None):
    style = style or resolve([]).style
    content = content or {"brand": {}}
    return make_ctx(content, build_theme(style, "pptx"), Measurer("/nonexistent"), Path("."))


def _one(prims, sid="s", title="章节"):
    sec = {"id": sid, "title": title, "blocks": [{"kind": "paragraph", "text": "x"}]}
    return (sec, [(p, f"sections[0].blocks[{i}]") for i, p in enumerate(prims)])


def _within_body(pages: list[Page], ctx) -> bool:
    return all(
        BODY_TOP - 0.01 <= pl.y and pl.y + pl.h <= ctx.body_bottom + 0.01
        for pg in pages
        for pl in pg.placed
    )


def test_minimal_content_one_page():
    ctx = _ctx()
    pages = paginate([_one([Paragraph("只有一句话。")])], ctx)
    assert len(pages) == 1
    assert pages[0].title == "章节" and not pages[0].continued


def test_small_sections_merge_onto_one_page():
    ctx = _ctx()
    a = _one([Paragraph("第一节。")], "a", "第一节")
    b = _one([Paragraph("第二节。")], "b", "第二节")
    pages = paginate([a, b], ctx)
    assert len(pages) == 1
    heads = [pl.prim for pl in pages[0].placed if isinstance(pl.prim, SubHeading)]
    assert heads == [SubHeading("第二节")]


def test_long_table_continues_with_header_repeated():
    ctx = _ctx()
    tbl = Table(("日期", "内容"), tuple((f"第 {i} 天", "记录空腹血糖与体重") for i in range(60)))
    pages = paginate([_one([tbl])], ctx)
    assert len(pages) >= 3
    assert pages[1].continued and pages[1].title == "章节（续）"  # noqa: RUF001
    pieces = [pl.prim for pg in pages for pl in pg.placed]
    assert all(isinstance(p, Table) and p.columns == ("日期", "内容") for p in pieces)
    assert sum(len(p.rows) for p in pieces) == 60
    assert _within_body(pages, ctx)


def test_paragraph_split_preserves_text():
    ctx = _ctx()
    text = "健康管理建议。" * 400
    pages = paginate([_one([Paragraph(text)])], ctx)
    assert len(pages) > 1
    joined = "".join(pl.prim.text for pg in pages for pl in pg.placed).replace("\n", "")
    assert joined == text
    assert _within_body(pages, ctx)


def test_atomic_too_tall_raises_with_path():
    ctx = _ctx()
    huge = Columns((Column("清单", tuple(f"条目 {i}" for i in range(200))),))
    with pytest.raises(LayoutError) as exc:
        paginate([_one([huge])], ctx)
    assert exc.value.path == "sections[0].blocks[0]"


def test_subheading_kept_with_next():
    ctx = _ctx()
    filler = Paragraph("填充文字。" * 150)
    prims = [filler, SubHeading("空腹血糖（mmol/L）"), Chart("line", ("a", "b"), (1.0, 2.0))]  # noqa: RUF001
    pages = paginate([_one(prims)], ctx)
    for pg in pages:
        last = pg.placed[-1].prim
        assert not isinstance(last, SubHeading)


def test_unbreakable_token_wraps_in_card_and_table():
    ctx = _ctx()
    token = "X" * 80
    grid = CardGrid((Card(title="编号", lines=(token,)),), cols=1)
    tbl = Table(("编号",), ((token,),))
    assert measure(grid, 400, ctx) > measure(
        CardGrid((Card(title="编号", lines=("短",)),), cols=1), 400, ctx
    )
    pages = paginate([_one([grid, tbl])], ctx)
    assert _within_body(pages, ctx)


def test_split_returns_none_when_nothing_fits():
    ctx = _ctx()
    head, tail = split(Bullets(("一条",)), 880, 1.0, ctx)
    assert head is None and tail == Bullets(("一条",))


def test_disclaimer_grows_footer_and_too_long_errors():
    short = _ctx({"brand": {"org_name": "示例健康管理中心", "disclaimer": "短声明。"}})
    long = _ctx({"brand": {"org_name": "示例健康管理中心", "disclaimer": "声明内容。" * 20}})
    assert long.body_bottom < short.body_bottom
    with pytest.raises(LayoutError) as exc:
        _ctx({"brand": {"disclaimer": "声明内容。" * 400}})
    assert exc.value.path == "brand.disclaimer"


def test_sample_paginates_within_body(sample):
    # 样例图片由 T5 测试现场生成；版式引擎测试不依赖它  # noqa: RUF003
    for sec in sample["sections"]:
        sec["blocks"] = [b for b in sec["blocks"] if b["kind"] != "image"]
    style = resolve([], sample).style
    ctx = _ctx(sample, style)
    pages = paginate(section_prims(sample, style), ctx)
    assert pages
    assert _within_body(pages, ctx)
    assert next(pg.section_id for pg in pages if not pg.continued) == sample["sections"][0]["id"]
