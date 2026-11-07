"""Synthetic "already designed" decks for the template-fill tests (layer 1 and layer 2).

Generic content only. Each builder saves a deck and returns {role: address}, the
address fill_template.py / inspect_template.py are expected to use for that shape.
"""

from __future__ import annotations

import copy
import io

import pptx
from PIL import Image
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_AUTO_SIZE
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.opc.packuri import PackURI
from pptx.oxml import parse_xml
from pptx.oxml.ns import qn
from pptx.parts.slide import SlideLayoutPart, SlideMasterPart
from pptx.util import Cm, Pt

RED = RGBColor(0xC0, 0x10, 0x20)
BLUE = RGBColor(0x10, 0x30, 0xA0)


def _textbox(slide, left, top, width, height, runs, *, name=None):
    """runs: list of paragraphs, each a list of (text, size_pt | None, bold, color)."""
    tb = slide.shapes.add_textbox(Cm(left), Cm(top), Cm(width), Cm(height))
    if name:
        tb.name = name
    tf = tb.text_frame
    tf.word_wrap = True
    for i, para in enumerate(runs):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        for text, size, bold, color in para:
            r = p.add_run()
            r.text = text
            if size:
                r.font.size = Pt(size)
            r.font.bold = bold
            if color is not None:
                r.font.color.rgb = color
    return tb


def _addr(slide_no, *shapes) -> str:
    return f"{slide_no}:" + "/".join(str(s.shape_id) for s in shapes)


def build_designed_deck(path) -> dict[str, str]:
    prs = pptx.Presentation()
    prs.slide_width, prs.slide_height = 12192000, 6858000
    roles: dict[str, str] = {}

    # 1: title layout, placeholders only (sizes inherited from layout / master)
    s1 = prs.slides.add_slide(prs.slide_layouts[0])
    s1.shapes.title.text = "年度工作汇报"
    s1.placeholders[1].text = "2026 Q3 Review"
    roles["s1_title"], roles["s1_sub"] = _addr(1, s1.shapes.title), _addr(1, s1.placeholders[1])

    # 2: title + content, bullet levels 0/1/1
    s2 = prs.slides.add_slide(prs.slide_layouts[1])
    s2.shapes.title.text = "本期要点"
    body = s2.placeholders[1].text_frame
    body.text = "第一条要点"
    for text in ("细节 A", "细节 B"):
        p = body.add_paragraph()
        p.text, p.level = text, 1
    roles["s2_title"], roles["s2_body"] = _addr(2, s2.shapes.title), _addr(2, s2.placeholders[1])

    # 3: free text boxes, explicit formatting, mixed runs, autofit variants
    s3 = prs.slides.add_slide(prs.slide_layouts[6])
    head = _textbox(s3, 1, 0.5, 20, 2, [[("标题文字", 28, True, RED)]])
    head.text_frame.auto_size = MSO_AUTO_SIZE.NONE
    mixed = _textbox(
        s3,
        1,
        3,
        14,
        6,
        [
            [("小标题", 14, True, BLUE), ("附注", 10, False, None)],
            [("正文第二段", 12, False, None)],
        ],
    )
    cjk = _textbox(s3, 16, 3, 12, 2, [[("Q3 营收 Revenue 增长 12%", 16, False, None)]])
    small = _textbox(s3, 1, 10, 3, 1.2, [[("短", 18, False, None)]])
    small.text_frame.auto_size = MSO_AUTO_SIZE.NONE
    shrink = _textbox(s3, 16, 6, 8, 2, [[("会缩字的框", 20, False, None)]])
    body_pr = shrink.text_frame._txBody.find(qn("a:bodyPr"))
    for child in list(body_pr):
        body_pr.remove(child)
    body_pr.append(parse_xml(f'<a:normAutofit xmlns:a="{_A}" fontScale="70000"/>'))
    resize = _textbox(s3, 16, 9, 8, 1, [[("会长高的框", 14, False, None)]])
    resize.text_frame.auto_size = MSO_AUTO_SIZE.SHAPE_TO_FIT_TEXT
    nowrap = _textbox(s3, 26, 0.5, 5, 1, [[("单行", 12, False, None)]])
    nowrap.text_frame.word_wrap = False
    # 10 x 2 cm, 18 pt: inner height 49.5 pt = two 21.6 pt lines at single spacing
    spaced = _textbox(s3, 1, 13, 10, 2, [[("双倍行距", 18, False, None)]])
    spaced.text_frame.auto_size = MSO_AUTO_SIZE.NONE
    spaced.text_frame.paragraphs[0].line_spacing = 2.0
    gap = _textbox(s3, 12, 13, 10, 2, [[("段前留白", 18, False, None)]])
    gap.text_frame.auto_size = MSO_AUTO_SIZE.NONE
    gap.text_frame.paragraphs[0].space_before = Pt(20)
    deco = s3.shapes.add_shape(MSO_SHAPE.RECTANGLE, Cm(0), Cm(12), Cm(33.8), Cm(0.5))
    deco.name = "装饰条"
    for role, shape in (
        ("s3_head", head),
        ("s3_mixed", mixed),
        ("s3_cjk", cjk),
        ("s3_small", small),
        ("s3_shrink", shrink),
        ("s3_resize", resize),
        ("s3_nowrap", nowrap),
        ("s3_deco", deco),
        ("s3_spaced", spaced),
        ("s3_gap", gap),
    ):
        roles[role] = _addr(3, shape)

    # 4: groups, one nested, the outer group scaled x2 horizontally
    s4 = prs.slides.add_slide(prs.slide_layouts[6])
    outer = s4.shapes.add_group_shape()
    g_a = _textbox(outer, 1, 1, 6, 1.5, [[("组内一", 16, True, RED)]])
    inner = outer.shapes.add_group_shape()
    g_b = _textbox(inner, 1, 3, 6, 1.5, [[("嵌套组内", 14, False, BLUE)]])
    # pin the outer group's transform: child space (0,0)-(W,H) drawn at 2W x H on the slide
    xfrm = outer._element.grpSpPr.find(qn("a:xfrm"))
    width, height = Cm(16), Cm(6)
    for tag, x, y in (
        ("off", 0, 0),
        ("ext", 2 * width, height),
        ("chOff", 0, 0),
        ("chExt", width, height),
    ):
        el = xfrm.find(qn(f"a:{tag}"))
        el.set("cx" if tag in ("ext", "chExt") else "x", str(x))
        el.set("cy" if tag in ("ext", "chExt") else "y", str(y))
    lone = _textbox(s4, 20, 1, 6, 1.5, [[("组外", 16, False, None)]])
    roles["s4_outer"] = _addr(4, outer)
    roles["s4_g_a"] = _addr(4, outer, g_a)
    roles["s4_g_b"] = _addr(4, outer, inner, g_b)
    roles["s4_lone"] = _addr(4, lone)

    # 5: table 3x3, last row's first two cells merged
    s5 = prs.slides.add_slide(prs.slide_layouts[6])
    frame = s5.shapes.add_table(3, 3, Cm(1), Cm(1), Cm(18), Cm(3))
    tbl = frame.table
    for r in range(3):
        for c in range(3):
            run = tbl.cell(r, c).text_frame.paragraphs[0].add_run()
            run.text = f"格{r + 1}{c + 1}"
            run.font.size = Pt(12)
            run.font.bold = r == 0
    tbl.cell(2, 0).merge(tbl.cell(2, 1))
    roles["s5_table"] = _addr(5, frame)

    # 6: content whose text this tooling cannot edit, plus one caption box
    s6 = prs.slides.add_slide(prs.slide_layouts[6])
    data = CategoryChartData()
    data.categories = ["A", "B"]
    data.add_series("s1", (1, 2))
    chart = s6.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Cm(1), Cm(1), Cm(10), Cm(6), data)
    smart = parse_xml(
        f'<p:graphicFrame xmlns:p="{_P}" xmlns:a="{_A}">'
        '<p:nvGraphicFramePr><p:cNvPr id="90" name="示意图"/><p:cNvGraphicFramePr/><p:nvPr/>'
        '</p:nvGraphicFramePr><p:xfrm><a:off x="0" y="0"/><a:ext cx="100" cy="100"/></p:xfrm>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/diagram"/>'
        "</a:graphic></p:graphicFrame>"
    )
    s6.shapes._spTree.append(smart)
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 30, 30)).save(buf, "PNG")
    buf.seek(0)
    pic = s6.shapes.add_picture(buf, Cm(12), Cm(1))
    caption = _textbox(s6, 1, 8, 10, 1, [[("图注", 10, False, None)]])
    caption.text_frame.paragraphs[0].runs[0].hyperlink.address = "https://example.com/a"
    roles["s6_chart"], roles["s6_pic"] = _addr(6, chart), _addr(6, pic)
    roles["s6_smartart"], roles["s6_caption"] = "6:90", _addr(6, caption)
    prs.save(str(path))
    return roles


_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"


def _add_second_master(prs, prefix: str) -> None:
    """python-pptx cannot add a master: clone master 1 and its layouts (renamed) by hand."""
    m1 = prs.slide_masters[0].part
    pkg = prs.part.package
    m2 = SlideMasterPart(
        PackURI("/ppt/slideMasters/slideMaster2.xml"),
        m1.content_type,
        pkg,
        copy.deepcopy(m1._element),
    )
    next_id = 2147483648 + 1000
    for n, sid in enumerate(list(m2._element.find(qn("p:sldLayoutIdLst")))):
        old = m1.related_part(sid.get(qn("r:id")))
        lp = SlideLayoutPart(
            PackURI(f"/ppt/slideLayouts/slideLayout{100 + n}.xml"),
            old.content_type,
            pkg,
            copy.deepcopy(old._element),
        )
        lp._element.cSld.set("name", f"{prefix}{n}")
        lp.relate_to(m2, RT.SLIDE_MASTER)
        sid.set(qn("r:id"), m2.relate_to(lp, RT.SLIDE_LAYOUT))
        sid.set("id", str(next_id))
        next_id += 1
    m2.relate_to(m1.part_related_by(RT.THEME), RT.THEME)
    master_ids = prs.part._element.find(qn("p:sldMasterIdLst"))
    new = copy.deepcopy(master_ids[0])
    new.set("id", str(next_id))
    new.set(qn("r:id"), prs.part.relate_to(m2, RT.SLIDE_MASTER))
    master_ids.append(new)


def build_two_master_deck(path, tmp_path) -> dict[str, str]:
    """Slide 1 on master 0, slide 2 on master 1 (title-only layout); title text in each."""
    prs = pptx.Presentation()
    _add_second_master(prs, "第二母版-")
    mid = tmp_path / "two-master-mid.pptx"
    prs.save(str(mid))
    prs = pptx.Presentation(str(mid))
    s1 = prs.slides.add_slide(prs.slide_layouts[5])
    s1.shapes.title.text = "母版一的标题"
    s2 = prs.slides.add_slide(prs.slide_masters[1].slide_layouts[5])
    s2.shapes.title.text = "母版二的标题"
    prs.save(str(path))
    return {"m0_title": _addr(1, s1.shapes.title), "m1_title": _addr(2, s2.shapes.title)}
