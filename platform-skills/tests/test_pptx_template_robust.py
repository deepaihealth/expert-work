"""Layer 1: pptx template tooling on awkward real-world deck shapes (review fixes).

Slide-level design kept on duplicate, sections / custom shows after restructuring,
duplicate shape ids, malformed or unusual XML that must not abort inspection, spec
validation, overflow fail-safes, output size.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import re
import sys
import zipfile
from pathlib import Path

import pptx
import pytest
from lxml import etree
from PIL import Image
from pptx.enum.text import MSO_AUTO_SIZE
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml import parse_xml
from pptx.oxml.ns import qn
from pptx.util import Cm, Pt


def _helpers():
    name = "ps_pptx_helpers"
    if name not in sys.modules:
        path = Path(__file__).with_name("_pptx_helpers.py")
        spec = importlib.util.spec_from_file_location(name, path)
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


H = _helpers()
fx = H.fx
scripts, text_mod, deck = H.scripts, H.text_mod, H.deck

P14 = "http://schemas.microsoft.com/office/powerpoint/2010/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"


def _one_box_deck(path, text="hello", **box) -> str:
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    tb = slide.shapes.add_textbox(Cm(1), Cm(1), Cm(box.get("w", 10)), Cm(box.get("h", 2)))
    tb.text_frame.text = text
    prs.save(str(path))
    return f"1:{tb.shape_id}"


def _edit(path, fn) -> None:
    prs = pptx.Presentation(str(path))
    fn(prs)
    prs.save(str(path))


def _box_el(prs):
    return next(s for s in prs.slides[0].shapes if s.has_text_frame)._element


# ------------------------------------------------------------------ H1 slide-level design


def _design_deck(path) -> str:
    """Slide 1: picture background, master shapes hidden, hidden slide, named, transition."""
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    tb = slide.shapes.add_textbox(Cm(1), Cm(1), Cm(10), Cm(2))
    tb.text_frame.text = "正文"
    # push the image's rId past what the copy will number it, so an un-remapped r:embed
    # dangles instead of matching by coincidence
    spare = [
        slide.part.relate_to(f"https://example.com/{n}", RT.HYPERLINK, is_external=True)
        for n in range(3)
    ]
    buf = io.BytesIO()
    Image.new("RGB", (32, 18), (20, 60, 120)).save(buf, "PNG")
    buf.seek(0)
    _, rid = slide.part.get_or_add_image_part(buf)
    for spare_rid in spare:
        slide.part.drop_rel(spare_rid)
    sld = slide._element
    sld.cSld.set("name", "设计页")
    sld.cSld.insert(
        0,
        parse_xml(
            f'<p:bg xmlns:p="{fx._P}" xmlns:a="{fx._A}" xmlns:r="{R_NS}"><p:bgPr>'
            f'<a:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch>'
            "</a:blipFill><a:effectLst/></p:bgPr></p:bg>"
        ),
    )
    sld.set("showMasterSp", "0")
    sld.set("show", "0")
    sld.append(parse_xml(f'<p:transition xmlns:p="{fx._P}" spd="slow"><p:fade/></p:transition>'))
    prs.slides.add_slide(prs.slide_layouts[6])
    prs.save(str(path))
    return f"1:{tb.shape_id}"


def _slide_minus_shapes(slide) -> bytes:
    """Slide XML without spTree, every r:id replaced by the part it points to."""
    el = copy.deepcopy(slide._element)
    el.cSld.remove(el.cSld.find(qn("p:spTree")))
    for node in el.iter():
        for attr, val in list(node.attrib.items()):
            if attr.startswith(f"{{{R_NS}}}"):
                node.set(attr, str(slide.part.related_part(val).partname).split("/")[-1][:5])
    return etree.tostring(el, method="c14n")


def test_duplicate_slide_keeps_background_transition_and_flags(scripts, tmp_path):
    path = tmp_path / "design.pptx"
    _design_deck(path)
    out = tmp_path / "dup.pptx"
    H.run(scripts, "duplicate_slide.py", path, out, "--index", "1")
    prs = pptx.Presentation(str(out))
    original, copy_ = prs.slides[0], prs.slides[1]
    assert _slide_minus_shapes(copy_) == _slide_minus_shapes(original)
    assert copy_._element.get("showMasterSp") == "0" and copy_._element.get("show") == "0"
    assert copy_._element.find(qn("p:transition")) is not None
    blip = copy_._element.cSld.find(qn("p:bg")).find(".//" + qn("a:blip"))
    assert copy_.part.related_part(blip.get(qn("r:embed"))).blob  # remapped, not dangling


def test_fill_slides_mode_duplicate_keeps_slide_design(scripts, tmp_path):
    path = tmp_path / "design.pptx"
    addr = _design_deck(path)
    spec = {"slides": [1, {"from": 1, "fill": {addr: "第二份"}}, 2]}
    _, out = H.fill(scripts, tmp_path, path, spec)
    prs = pptx.Presentation(str(out))
    assert _slide_minus_shapes(prs.slides[1]) == _slide_minus_shapes(prs.slides[0])


def test_fill_slides_mode_reports_shared_chart_parts(scripts, deck, tmp_path):
    path, roles = deck
    spec = {"slides": [1, 2, 3, 4, 5, 6, {"from": 6, "fill": {roles["s6_caption"]: "副本"}}]}
    report, _ = H.fill(scripts, tmp_path, path, spec)
    assert report["shared_parts_warning"] == ["图表"]


# ------------------------------------------------------------------ M1 sections, shows, links


def _add_sections(prs, groups: list[tuple[str, list[int]]]) -> None:
    ids = [sid.get("id") for sid in prs.slides._sldIdLst]
    ext_lst = prs.part._element.find(qn("p:extLst"))
    if ext_lst is None:
        ext_lst = etree.SubElement(prs.part._element, qn("p:extLst"))
    ext = etree.SubElement(ext_lst, qn("p:ext"), uri="{521415D9-36F7-43E2-AB2F-B90AF26B5E84}")
    lst = etree.SubElement(ext, f"{{{P14}}}sectionLst", nsmap={"p14": P14})
    for n, (name, members) in enumerate(groups):
        sec = etree.SubElement(
            lst, f"{{{P14}}}section", name=name, id=f"{{0000000{n}-0000-0000-0000-000000000000}}"
        )
        sl = etree.SubElement(sec, f"{{{P14}}}sldIdLst")
        for m in members:
            etree.SubElement(sl, f"{{{P14}}}sldId", id=ids[m - 1])


def _sections(prs) -> list[tuple[str, list[str]]]:
    return [
        (sec.get("name"), [s.get("id") for s in sec.iter(f"{{{P14}}}sldId")])
        for sec in prs.part._element.iter(f"{{{P14}}}section")
    ]


def _titled_deck(path, n: int, extra=None) -> None:
    prs = pptx.Presentation()
    for i in range(n):
        prs.slides.add_slide(prs.slide_layouts[5]).shapes.title.text = f"第{i + 1}页"
    if extra:
        extra(prs)
    prs.save(str(path))


def test_sections_follow_drop_duplicate_and_order(scripts, tmp_path):
    path = tmp_path / "sections.pptx"
    _titled_deck(path, 4, lambda prs: _add_sections(prs, [("甲", [1, 2]), ("乙", [3, 4])]))
    spec = {"slides": [1, {"from": 1, "fill": {}}, 2, 4], "drop_unlisted": True}
    report, out = H.fill(scripts, tmp_path, path, spec)
    prs = pptx.Presentation(str(out))
    final = [sid.get("id") for sid in prs.slides._sldIdLst]
    sections = _sections(prs)
    assert [name for name, _ in sections] == ["甲", "乙"]
    assert sections[0][1] == final[:3] and sections[1][1] == final[3:]
    assert "warnings" not in report


def test_interleaving_reorder_splits_sections_into_runs(scripts, tmp_path):
    path = tmp_path / "sections.pptx"
    _titled_deck(path, 4, lambda prs: _add_sections(prs, [("甲", [1, 2]), ("乙", [3, 4])]))
    before = [
        sec.get("id")
        for sec in pptx.Presentation(str(path)).part._element.iter(f"{{{P14}}}section")
    ]
    report, out = H.fill(scripts, tmp_path, path, {"slides": [1, 3, 2, 4]})
    prs = pptx.Presentation(str(out))
    final = [sid.get("id") for sid in prs.slides._sldIdLst]
    sections = _sections(prs)
    assert [name for name, _ in sections] == ["甲", "乙", "甲", "乙"]
    assert [ids for _, ids in sections] == [[f] for f in final]
    guids = [sec.get("id") for sec in prs.part._element.iter(f"{{{P14}}}section")]
    assert guids[:2] == before and len(set(guids)) == 4
    assert all(re.fullmatch(r"\{[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}\}", g) for g in guids)
    assert "warnings" not in report


def test_drop_removes_slide_from_custom_show_and_package(scripts, tmp_path):
    path = tmp_path / "show.pptx"

    def add_show(prs):
        rid = prs.slides._sldIdLst[2].rId
        prs.part._element.insert(
            list(prs.part._element).index(prs.part._element.find(qn("p:notesSz"))) + 1,
            parse_xml(
                f'<p:custShowLst xmlns:p="{fx._P}" xmlns:r="{R_NS}">'
                '<p:custShow name="精简版" id="0">'
                f'<p:sldLst><p:sld r:id="{rid}"/></p:sldLst></p:custShow></p:custShowLst>'
            ),
        )

    _titled_deck(path, 3, add_show)
    report, out = H.fill(scripts, tmp_path, path, {"slides": [1, 2], "drop_unlisted": True})
    names = zipfile.ZipFile(out).namelist()
    assert sorted(n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)) == [
        "ppt/slides/slide1.xml",
        "ppt/slides/slide2.xml",
    ]
    prs = pptx.Presentation(str(out))
    assert prs.part._element.find(".//" + qn("p:sld")) is None
    assert any("精简版" in w for w in report["warnings"])


def _slide_parts(path) -> list[str]:
    names = zipfile.ZipFile(path).namelist()
    return sorted(n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))


def test_drop_removes_shape_click_jump_to_the_dropped_slide(scripts, tmp_path):
    path = tmp_path / "jump.pptx"

    def jump(prs):
        tb = prs.slides[0].shapes.add_textbox(Cm(1), Cm(4), Cm(6), Cm(1))
        tb.text_frame.text = "跳转"
        tb.click_action.target_slide = prs.slides[2]

    _titled_deck(path, 3, jump)
    report, out = H.fill(scripts, tmp_path, path, {"slides": [1, 2], "drop_unlisted": True})
    assert _slide_parts(out) == ["ppt/slides/slide1.xml", "ppt/slides/slide2.xml"]
    first = pptx.Presentation(str(out)).slides[0]
    assert first._element.find(".//" + qn("a:hlinkClick")) is None
    assert not [r for r in first.part.rels.values() if r.reltype.endswith("/slide")]
    assert any("第 1 页" in w and "链接" in w for w in report["warnings"])


def test_drop_warns_when_a_kept_slide_still_links_to_it(scripts, tmp_path):
    path = tmp_path / "link.pptx"

    def link(prs):
        run = prs.slides[0].shapes.title.text_frame.paragraphs[0].runs[0]
        run.hyperlink.address = None
        rid = prs.slides[0].part.relate_to(
            prs.slides[2].part, pptx.opc.constants.RELATIONSHIP_TYPE.SLIDE
        )
        run._r.get_or_add_rPr().append(
            parse_xml(
                f'<a:hlinkClick xmlns:a="{fx._A}" xmlns:r="{R_NS}" r:id="{rid}" action="ppaction://hlinksldjump"/>'
            )
        )

    _titled_deck(path, 3, link)
    report, out = H.fill(scripts, tmp_path, path, {"slides": [1, 2], "drop_unlisted": True})
    assert any("第 1 页" in w and "链接" in w for w in report["warnings"])
    assert _slide_parts(out) == ["ppt/slides/slide1.xml", "ppt/slides/slide2.xml"]
    run = pptx.Presentation(str(out)).slides[0].shapes.title.text_frame.paragraphs[0].runs[0]
    assert run._r.find(".//" + qn("a:hlinkClick")) is None


# ------------------------------------------------------------------ M2 duplicate shape ids


def test_duplicate_shape_ids_are_ambiguous_and_never_filled(scripts, tmp_path):
    path = tmp_path / "dupid.pptx"
    addr = _one_box_deck(path, "AAA")

    def clone(prs):
        el = copy.deepcopy(_box_el(prs))
        el.find(".//" + qn("a:t")).text = "BBB"
        prs.slides[0].shapes._spTree.append(el)

    _edit(path, clone)
    entries = [t for t in H.inspect(scripts, path)["slides"][0]["texts"] if t["addr"] == addr]
    assert len(entries) == 2 and all(e.get("ambiguous") is True for e in entries)
    report, out = H.fill(scripts, tmp_path, path, {"fill": {addr: "NEW"}}, expect=1)
    assert report["results"] == [{"page": 1, "addr": addr, "status": "ambiguous_id"}]
    assert not out.exists()


# ------------------------------------------------------------------ H2 crash cases + isolation


def _empty_body(prs):
    body = _box_el(prs).find(qn("p:txBody"))
    for p in body.findall(qn("a:p")):
        body.remove(p)


def _pct_fontscale(prs):
    bp = _box_el(prs).find(qn("p:txBody") + "/" + qn("a:bodyPr"))
    for c in list(bp):
        bp.remove(c)
    bp.append(bp.makeelement(qn("a:normAutofit"), {"fontScale": "62.5%"}))


def _line_spacing(val):
    def mut(prs):
        p = _box_el(prs).find(".//" + qn("a:p"))
        ppr = p.find(qn("a:pPr"))
        if ppr is None:
            ppr = p.makeelement(qn("a:pPr"), {})
            p.insert(0, ppr)
        ppr.insert(0, parse_xml(f'<a:lnSpc xmlns:a="{fx._A}"><a:spcPct val="{val}"/></a:lnSpc>'))

    return mut


@pytest.mark.parametrize(
    "mutate",
    [_empty_body, _pct_fontscale, _line_spacing("150%"), _line_spacing("0")],
    ids=[
        "txbody_without_paragraphs",
        "fontscale_percent_string",
        "lnspc_percent_string",
        "lnspc_zero",
    ],
)
def test_unusual_text_bodies_inspect_and_fill(scripts, tmp_path, mutate):
    path = tmp_path / "odd.pptx"
    addr = _one_box_deck(path)
    _edit(path, mutate)
    entry = H.texts(H.inspect(scripts, path))[addr]
    assert "error" not in entry
    report, out = H.fill(scripts, tmp_path, path, {"fill": {addr: "新的文字"}})
    assert report["results"][0]["status"] == "ok" and report["overflow_unknown"] == []
    assert H.shape_by_addr(pptx.Presentation(str(out)), addr).text_frame.text == "新的文字"


def test_table_cells_without_text_body_list_empty_and_fill(scripts, tmp_path):
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    frame = slide.shapes.add_table(2, 2, Cm(1), Cm(1), Cm(10), Cm(3))
    for row in frame.table.rows:
        for cell in row.cells:
            cell._tc.remove(cell._tc.find(qn("a:txBody")))
    path = tmp_path / "bare-table.pptx"
    prs.save(str(path))
    table = H.texts(H.inspect(scripts, path))[f"1:{frame.shape_id}"]
    assert table["cells"] == [["", ""], ["", ""]]
    _, out = H.fill(scripts, tmp_path, path, {"fill": {f"1:{frame.shape_id}@2,1": "补上"}})
    cell = H.shape_by_addr(pptx.Presentation(str(out)), f"1:{frame.shape_id}").table.cell(1, 0)
    assert cell.text_frame.text == "补上"


def test_one_broken_shape_does_not_hide_the_rest(scripts, tmp_path):
    path = tmp_path / "broken.pptx"
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    good = slide.shapes.add_textbox(Cm(1), Cm(1), Cm(10), Cm(2))
    good.text_frame.text = "好的"
    bad_body = slide.shapes.add_textbox(Cm(1), Cm(4), Cm(10), Cm(2))
    bad_body.text_frame.text = "坏的框"
    bad_body.text_frame._txBody.find(qn("a:bodyPr")).set("lIns", "abc")
    bad_run = slide.shapes.add_textbox(Cm(1), Cm(7), Cm(10), Cm(2))
    bad_run.text_frame.text = "坏的字号"
    bad_run.text_frame.paragraphs[0].runs[0]._r.get_or_add_rPr().set("sz", "big")
    prs.save(str(path))
    entries = H.texts(H.inspect(scripts, path))
    assert entries[f"1:{good.shape_id}"]["text"] == "好的"
    for broken in (bad_body, bad_run):
        assert "error" in entries[f"1:{broken.shape_id}"], entries
    spec = {"fill": {f"1:{good.shape_id}": "新", f"1:{bad_body.shape_id}": "x"}}
    report, _ = H.fill(scripts, tmp_path, path, spec, expect=1)
    statuses = {r["addr"]: r["status"] for r in report["results"]}
    assert statuses[f"1:{good.shape_id}"] == "ok"
    assert statuses[f"1:{bad_body.shape_id}"].startswith("error")
    # the run-level fault only breaks the estimate: fill works, overflow unknown
    report, _ = H.fill(scripts, tmp_path, path, {"fill": {f"1:{bad_run.shape_id}": "新"}})
    assert report["overflow_unknown"] == [f"1:{bad_run.shape_id}"]


def test_placeholder_without_text_body_and_hidden_shapes(scripts, tmp_path):
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    body = slide.placeholders[1]
    body._element.remove(body._element.find(qn("p:txBody")))
    slide.shapes.title.text = "标题"
    slide.shapes.title._element.find(".//" + qn("p:cNvPr")).set("hidden", "1")
    path = tmp_path / "ph.pptx"
    prs.save(str(path))
    res = H.inspect(scripts, path)["slides"][0]
    locked = {t["addr"]: t["kind"] for t in res["locked"]}
    assert locked == {f"1:{body.shape_id}": "placeholder_without_text"}
    assert H.texts({"slides": [res]})[f"1:{slide.shapes.title.shape_id}"]["hidden"] is True
    report, _ = H.fill(scripts, tmp_path, path, {"fill": {f"1:{body.shape_id}": "x"}}, expect=1)
    assert report["results"][0]["status"] == "locked:placeholder_without_text"


def test_inspect_default_mode_keeps_working_on_broken_shapes(scripts, tmp_path):
    path = tmp_path / "broken.pptx"
    addr = _one_box_deck(path)
    _edit(path, _pct_fontscale)
    res = H.inspect(scripts, path, legacy=True)
    assert set(res["slides"][0]) == {"number", "master", "layout", "shapes"}
    assert addr  # default mode never builds text entries, so text faults cannot abort it


# ------------------------------------------------------------------ M3 overflow fail-safes


def test_overflow_unknown_when_geometry_is_missing(scripts, tmp_path):
    prs = pptx.Presentation()
    layout = prs.slide_layouts[1]
    slide = prs.slides.add_slide(layout)
    body = slide.placeholders[1]
    body.text = "x"
    ph = layout.placeholders[1]._element
    ph.find(qn("p:nvSpPr") + "/" + qn("p:nvPr") + "/" + qn("p:ph")).set("type", "sldImg")
    path = tmp_path / "nogeo.pptx"
    prs.save(str(path))
    addr = f"1:{body.shape_id}"
    report, _ = H.fill(scripts, tmp_path, path, {"fill": {addr: "字" * 500}})
    assert report["overflow"] == [] and report["overflow_unknown"] == [addr]


def test_ambiguous_width_punctuation_counts_full_width(text_mod):
    assert text_mod.text_em("“”—…") == 4.0
    assert text_mod.text_em("Привет") == 3.0, "letters stay proportional"


@pytest.mark.parametrize("width", [2.0, 3.0, 4.0, 5.5, 7.0, 9.0])
def test_non_latin_alphabets_wrap_by_word(text_mod, width):
    # same word lengths in Latin, Cyrillic, Greek, Hebrew and Vietnamese: same line count
    latin = text_mod.wrap_lines("abcdef abc abc abcd abcdefg", width)
    for text in (
        "Привет мир как дела сегодня",
        "αβγδεζ αβγ αβγ αβγδ αβγδεζη",
        "שלומות שלו שלו שלום שלומותי",
        "Chương mày hay biết nghiệpp",
    ):
        assert text_mod.wrap_lines(text, width) == latin, text


def test_near_full_box_is_reported(scripts, deck, tmp_path):
    path, roles = deck  # nested-group box: 2 lines of 14 pt use 95% of its height
    report, _ = H.fill(scripts, tmp_path, path, {"fill": {roles["s4_g_b"]: "字" * 30}})
    assert report["overflow"] == [], "near-full is not overflow"
    (item,) = report["near"]
    assert item["addr"] == roles["s4_g_b"] and item["need_lines"] <= item["fit_lines"]
    report, _ = H.fill(scripts, tmp_path, path, {"fill": {roles["s4_g_b"]: "字" * 10}})
    assert report["overflow"] == [] and report["near"] == []


def test_vertical_text_swaps_width_and_height(scripts, tmp_path):
    path = tmp_path / "vert.pptx"
    addr = _one_box_deck(path, "a", w=15, h=2)

    def vert(prs):
        tb = _box_el(prs)
        tb.find(qn("p:txBody") + "/" + qn("a:bodyPr")).set("vert", "eaVert")
        tb.find(".//" + qn("a:r")).get_or_add_rPr().set("sz", "2800")

    _edit(path, vert)
    # horizontally 14 chars at 28 pt fit one line; stacked vertically the 2 cm box holds
    # 1 char per column and ~12 columns
    report, _ = H.fill(scripts, tmp_path, path, {"fill": {addr: "字" * 14}})
    assert [o["addr"] for o in report["overflow"]] == [addr]
    assert H.texts(H.inspect(scripts, path))[addr]["vertical"] is True


# ------------------------------------------------------------------ M4 output size


def test_slides_filter_and_compact_json(scripts, deck):
    path, _ = deck
    proc = H.run(scripts, "inspect_template.py", path, "--texts", "--slides", "2,4-5")
    assert ", " not in proc.stdout.split('"legend"')[0] and '": ' not in proc.stdout
    res = json.loads(proc.stdout)
    assert [s["number"] for s in res["slides"]] == [2, 4, 5]
    legacy = H.inspect(scripts, path, "--slides", "3", legacy=True)
    assert [s["number"] for s in legacy["slides"]] == [3] and "layouts" in legacy
    H.run(scripts, "inspect_template.py", path, "--texts", "--slides", "9", expect=1)


def test_texts_output_fits_the_exec_output_cap(scripts, deck, tmp_path):
    path, _ = deck
    assert len(H.run(scripts, "inspect_template.py", path, "--texts").stdout) < 20_000
    prs = pptx.Presentation()
    for s in range(10):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        for b in range(15):
            tb = slide.shapes.add_textbox(Cm(1 + b), Cm(1 + b % 5), Cm(6), Cm(1.5))
            tb.text_frame.text = f"第{s + 1}页说明文字第{b + 1}条,内容较长需要截断显示"
            tb.text_frame.paragraphs[0].runs[0].font.size = Pt(14)
    big = tmp_path / "ten.pptx"
    prs.save(str(big))
    assert len(H.run(scripts, "inspect_template.py", big, "--texts").stdout) < 20_000


def _dense_deck(path) -> None:
    prs = pptx.Presentation()
    for s in range(6):
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        slide.shapes.title.text = f"第{s + 1}页标题"
        for b in range(40):
            tb = slide.shapes.add_textbox(Cm(b % 8 * 4), Cm(3 + b // 8 * 3), Cm(4), Cm(2))
            tb.text_frame.text = f"第{s + 1}页第{b + 1}条说明文字,内容较长需要截断"
            tb.text_frame.auto_size = MSO_AUTO_SIZE.NONE
            tb.text_frame.paragraphs[0].runs[0].font.size = Pt(12)
    prs.save(str(path))


def test_texts_pages_itself_under_the_output_cap(scripts, tmp_path):
    path = tmp_path / "dense.pptx"
    _dense_deck(path)
    seen, flags, rounds = [], ["--texts"], 0
    while True:
        out = H.run(scripts, "inspect_template.py", path, *flags).stdout
        assert len(out) < 20_000, len(out)
        res = json.loads(out)
        seen += [s["number"] for s in res["slides"]]
        rounds += 1
        if "more" not in res:
            break
        assert res["more"].startswith("--slides ")
        flags = ["--texts", *res["more"].split()]
    assert seen == [1, 2, 3, 4, 5, 6] and rounds > 1
    assert all(len(s["texts"]) == 41 for s in [res["slides"][-1]])


# ------------------------------------------------------------------ M5 spec validation


@pytest.mark.parametrize(
    ("value", "needle"),
    [
        ("坏\x07字", "控制字符"),
        ([{"text": "x", "level": True}], "level"),
        ([{"text": "x", "level": 1.0}], "level"),
    ],
    ids=["bell_char", "bool_level", "float_level"],
)
def test_fill_rejects_bad_values_without_traceback(scripts, tmp_path, value, needle):
    path = tmp_path / "one.pptx"
    addr = _one_box_deck(path)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({"fill": {addr: value}}), encoding="utf-8")
    proc = H.run(scripts, "fill_template.py", path, spec_path, tmp_path / "o.pptx", expect=1)
    assert needle in proc.stderr and "Traceback" not in proc.stderr


def test_fill_rejects_duplicate_json_keys(scripts, tmp_path):
    path = tmp_path / "one.pptx"
    addr = _one_box_deck(path)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(f'{{"fill": {{"{addr}": "一", "{addr}": "二"}}}}', encoding="utf-8")
    proc = H.run(scripts, "fill_template.py", path, spec_path, tmp_path / "o.pptx", expect=1)
    assert "重复" in proc.stderr and addr in proc.stderr


def test_carriage_returns_are_newlines(scripts, tmp_path):
    path = tmp_path / "one.pptx"
    addr = _one_box_deck(path)
    _, out = H.fill(scripts, tmp_path, path, {"fill": {addr: "一\r\n二\r三"}})
    paras = H.shape_by_addr(pptx.Presentation(str(out)), addr).text_frame.paragraphs
    assert [p.text for p in paras] == ["一", "二", "三"]
    _, out = H.fill(scripts, tmp_path, path, {"fill": {addr: ["上\r\n下"]}})
    paras = H.shape_by_addr(pptx.Presentation(str(out)), addr).text_frame.paragraphs
    assert len(paras) == 1 and paras[0]._p.findall(qn("a:br"))


def test_vertical_tab_is_a_line_break(scripts, tmp_path):
    path = tmp_path / "one.pptx"
    addr = _one_box_deck(path)
    _, out = H.fill(scripts, tmp_path, path, {"fill": {addr: "上\v下\n第二段"}})
    paras = H.shape_by_addr(pptx.Presentation(str(out)), addr).text_frame.paragraphs
    assert len(paras) == 2 and paras[0]._p.findall(qn("a:br"))


# ------------------------------------------------------------------ low: replacement details


def test_level_change_drops_copied_indent(scripts, tmp_path):
    path = tmp_path / "indent.pptx"
    addr = _one_box_deck(path)

    def indent(prs):
        p = _box_el(prs).find(".//" + qn("a:p"))
        ppr = p.makeelement(qn("a:pPr"), {"marL": "457200", "indent": "-228600"})
        p.insert(0, ppr)

    _edit(path, indent)
    _, out = H.fill(
        scripts, tmp_path, path, {"fill": {addr: ["同级", {"text": "下一级", "level": 1}]}}
    )
    p0, p1 = (
        p._p.find(qn("a:pPr"))
        for p in H.shape_by_addr(pptx.Presentation(str(out)), addr).text_frame.paragraphs
    )
    assert p0.get("marL") == "457200", "same level keeps the template indent"
    assert p1.get("lvl") == "1" and p1.get("marL") is None and p1.get("indent") is None


def test_first_run_format_is_the_actual_first_run(scripts, tmp_path):
    path = tmp_path / "runs.pptx"
    addr = _one_box_deck(path)

    def runs(prs):
        p = _box_el(prs).find(".//" + qn("a:p"))
        first = p.find(qn("a:r"))
        rpr = first.find(qn("a:rPr"))
        if rpr is not None:
            first.remove(rpr)
        second = parse_xml(
            f'<a:r xmlns:a="{fx._A}"><a:rPr lang="zh-CN" sz="4000"/><a:t>大</a:t></a:r>'
        )
        first.addnext(second)

    _edit(path, runs)
    _, out = H.fill(scripts, tmp_path, path, {"fill": {addr: "新"}})
    run = H.shape_by_addr(pptx.Presentation(str(out)), addr).text_frame.paragraphs[0].runs[0]
    assert run.font.size is None, "the first run had no size; the second run's 40 pt must not leak"


def test_copied_run_format_drops_spellcheck_flags(scripts, tmp_path):
    path = tmp_path / "flags.pptx"
    addr = _one_box_deck(path)
    _edit(
        path,
        lambda prs: (
            _box_el(prs)
            .find(".//" + qn("a:r"))
            .get_or_add_rPr()
            .attrib.update({"err": "1", "dirty": "0", "sz": "2000"})
        ),
    )
    _, out = H.fill(scripts, tmp_path, path, {"fill": {addr: "新"}})
    rpr = (
        H.shape_by_addr(pptx.Presentation(str(out)), addr)
        .text_frame.paragraphs[0]
        .runs[0]
        ._r.find(qn("a:rPr"))
    )
    assert rpr.get("sz") == "2000" and rpr.get("err") is None and rpr.get("dirty") is None


def test_paragraph_alternate_content_is_removed(scripts, tmp_path):
    path = tmp_path / "math.pptx"
    addr = _one_box_deck(path)
    alt = (
        f'<mc:AlternateContent xmlns:mc="{MC}" xmlns:a14="http://schemas.microsoft.com/office/drawing/2010/main">'
        '<mc:Choice Requires="a14"><a14:m/></mc:Choice><mc:Fallback/></mc:AlternateContent>'
    )
    _edit(path, lambda prs: _box_el(prs).find(".//" + qn("a:p")).insert(0, parse_xml(alt)))
    _, out = H.fill(scripts, tmp_path, path, {"fill": {addr: "新"}})
    el = H.shape_by_addr(pptx.Presentation(str(out)), addr)._element
    assert el.find(".//" + f"{{{MC}}}AlternateContent") is None


# ------------------------------------------------------------------ round 2: mc:Ignorable


def _alternate_content_deck(path, tmp_path) -> str:
    """Slide 1 written the way PowerPoint does: p14 declared on the root, mc:Ignorable,
    and the transition inside mc:AlternateContent."""
    base = tmp_path / "mc-base.pptx"
    addr = _one_box_deck(base)
    with zipfile.ZipFile(base) as zin, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "ppt/slides/slide1.xml":
                xml = data.decode()
                xml = xml.replace(
                    "<p:sld ",
                    f'<p:sld xmlns:mc="{MC}" xmlns:p14="{P14}" mc:Ignorable="p14" ',
                    1,
                )
                xml = xml.replace(
                    "</p:sld>",
                    '<mc:AlternateContent><mc:Choice Requires="p14">'
                    '<p:transition spd="slow" p14:dur="2000"><p14:prism/></p:transition>'
                    '</mc:Choice><mc:Fallback><p:transition spd="slow"><p:fade/></p:transition>'
                    "</mc:Fallback></mc:AlternateContent></p:sld>",
                )
                data = xml.encode()
            zout.writestr(item, data)
    return addr


def test_duplicate_keeps_ignorable_with_its_namespace(scripts, tmp_path):
    path = tmp_path / "mc.pptx"
    addr = _alternate_content_deck(path, tmp_path)
    report, out = H.fill(
        scripts, tmp_path, path, {"slides": [1, {"from": 1, "fill": {addr: "副本"}}]}
    )
    assert report["changed_pages"] == [2]
    with zipfile.ZipFile(out) as z:
        roots = [etree.fromstring(z.read(n)) for n in _slide_parts(out)]
    for root in roots:
        assert root.get(f"{{{MC}}}Ignorable") == "p14"
        assert root.nsmap.get("p14") == P14, "the ignorable prefix is declared on the root"
        assert root.find(f"{{{MC}}}AlternateContent") is not None
    prs = pptx.Presentation(str(out))
    texts = [H.shape_by_addr(prs, f"{n}:{addr.split(':')[1]}").text_frame.text for n in (1, 2)]
    assert texts == ["hello", "副本"], "the duplicate's shapes are still the ones being filled"
