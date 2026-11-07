"""Layer 1: pptx template tooling edge cases (third review round).

Links of every kind to a dropped slide (shape click / hover, run click / mouse-over,
speaker notes), section-list element order, the --texts paging marker surviving a
truncated output, and mc:Ignorable prefixes that clash on duplicate.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import types
import zipfile
from pathlib import Path

import pptx
import pytest
from lxml import etree
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml import parse_xml
from pptx.oxml.ns import qn
from pptx.util import Cm


def _helpers():
    name = "ps_pptx_helpers"
    if name not in sys.modules:
        path = Path(__file__).with_name("_pptx_helpers.py")
        spec = importlib.util.spec_from_file_location(name, path)
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


H = _helpers()
scripts = H.scripts

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P14 = "http://schemas.microsoft.com/office/powerpoint/2010/main"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
DROP_THIRD = {"slides": [1, 2], "drop_unlisted": True}


def _titled_deck(path, n: int, extra=None) -> None:
    prs = pptx.Presentation()
    for i in range(n):
        prs.slides.add_slide(prs.slide_layouts[5]).shapes.title.text = f"第{i + 1}页"
    if extra:
        extra(prs)
    prs.save(str(path))


def _slide_parts(path) -> list[str]:
    names = zipfile.ZipFile(path).namelist()
    return sorted(n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))


def _link_xml(tag: str, rid: str) -> str:
    return (
        f'<a:{tag} xmlns:a="{A}" xmlns:r="{R_NS}" r:id="{rid}" action="ppaction://hlinksldjump"/>'
    )


def _shape_link(tag: str):
    """A text box on slide 1 whose shape-level link (cNvPr child) jumps to slide 3."""

    def hook(prs):
        tb = prs.slides[0].shapes.add_textbox(Cm(1), Cm(4), Cm(6), Cm(1))
        tb.text_frame.text = "跳转"
        rid = prs.slides[0].part.relate_to(prs.slides[2].part, RT.SLIDE)
        tb._element.find(".//" + qn("p:cNvPr")).append(parse_xml(_link_xml(tag, rid)))

    return hook


def _run_link(tag: str):
    """The title run on slide 1 carries a run-level link to slide 3."""

    def hook(prs):
        run = prs.slides[0].shapes.title.text_frame.paragraphs[0].runs[0]
        rid = prs.slides[0].part.relate_to(prs.slides[2].part, RT.SLIDE)
        run._r.get_or_add_rPr().append(parse_xml(_link_xml(tag, rid)))

    return hook


@pytest.mark.parametrize(
    "hook",
    [
        _shape_link("hlinkClick"),
        _shape_link("hlinkHover"),
        _run_link("hlinkClick"),
        _run_link("hlinkMouseOver"),
    ],
    ids=["shape_click", "shape_hover", "run_click", "run_mouse_over"],
)
def test_every_link_kind_to_a_dropped_slide_is_removed(scripts, tmp_path, hook):
    path = tmp_path / "links.pptx"
    _titled_deck(path, 3, hook)
    report, out = H.fill(scripts, tmp_path, path, DROP_THIRD)
    assert _slide_parts(out) == ["ppt/slides/slide1.xml", "ppt/slides/slide2.xml"]
    first = pptx.Presentation(str(out)).slides[0]
    assert not [r for r in first.part.rels.values() if r.reltype == RT.SLIDE]
    assert report["warnings"] == ["第 1 页有链接指向被删掉的页, 已去掉这个链接"]


def test_unremovable_reference_keeps_the_relationship_and_says_so(scripts, tmp_path):
    """A reference this tool does not know how to remove (here an r:embed in an
    extension) keeps the relationship; the warning must not claim the link is gone."""

    def hook(prs):
        rid = prs.slides[0].part.relate_to(prs.slides[2].part, RT.SLIDE)
        prs.slides[0]._element.append(
            parse_xml(
                f'<p:extLst xmlns:p="{P}"><p:ext uri="{{00000000-0000-0000-0000-0000000000AA}}">'
                f'<x:ref xmlns:x="urn:example:ext" xmlns:r="{R_NS}" r:embed="{rid}"/>'
                "</p:ext></p:extLst>"
            )
        )

    path = tmp_path / "ext.pptx"
    _titled_deck(path, 3, hook)
    report, _ = H.fill(scripts, tmp_path, path, DROP_THIRD)
    (warning,) = report["warnings"]
    assert "第 1 页" in warning and "已去掉" not in warning and "失效" in warning


def test_notes_link_to_a_dropped_slide_is_removed(scripts, tmp_path):
    def hook(prs):
        notes = prs.slides[0].notes_slide
        notes.notes_text_frame.text = "备注里跳到第三页"
        run = notes.notes_text_frame.paragraphs[0].runs[0]
        rid = notes.part.relate_to(prs.slides[2].part, RT.SLIDE)
        run._r.get_or_add_rPr().append(parse_xml(_link_xml("hlinkClick", rid)))

    path = tmp_path / "notes.pptx"
    _titled_deck(path, 3, hook)
    report, out = H.fill(scripts, tmp_path, path, DROP_THIRD)
    names = zipfile.ZipFile(out).namelist()
    assert sorted(n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)) == [
        "ppt/slides/slide1.xml",
        "ppt/slides/slide2.xml",
    ]
    first = pptx.Presentation(str(out)).slides[0]
    notes = first.notes_slide
    assert notes.notes_text_frame.text == "备注里跳到第三页"
    # a notes slide always points back at its own slide; nothing else may remain
    targets = [r.target_part for r in notes.part.rels.values() if r.reltype == RT.SLIDE]
    assert targets == [first.part]
    assert notes._element.find(".//" + qn("a:hlinkClick")) is None
    assert any("第 1 页" in w and "备注" in w for w in report["warnings"])


def test_rebuilt_sections_stay_before_the_section_list_extension(scripts, tmp_path):
    def hook(prs):
        ids = [s.get("id") for s in prs.slides._sldIdLst]
        root = prs.part._element
        ext_lst = root.find(qn("p:extLst"))
        if ext_lst is None:
            ext_lst = etree.SubElement(root, qn("p:extLst"))
        ext_lst.append(
            parse_xml(
                f'<p:ext xmlns:p="{P}" uri="{{521415D9-36F7-43E2-AB2F-B90AF26B5E84}}">'
                f'<p14:sectionLst xmlns:p14="{P14}">'
                '<p14:section name="A" id="{11111111-1111-1111-1111-111111111111}"><p14:sldIdLst>'
                f'<p14:sldId id="{ids[0]}"/><p14:sldId id="{ids[1]}"/></p14:sldIdLst></p14:section>'
                '<p14:section name="B" id="{22222222-2222-2222-2222-222222222222}"><p14:sldIdLst>'
                f'<p14:sldId id="{ids[2]}"/></p14:sldIdLst></p14:section>'
                "<p14:extLst/></p14:sectionLst></p:ext>"
            )
        )

    path = tmp_path / "sections.pptx"
    _titled_deck(path, 3, hook)
    _, out = H.fill(scripts, tmp_path, path, {"slides": [1, 3, 2]})
    section_list = next(pptx.Presentation(str(out)).part._element.iter(f"{{{P14}}}sectionLst"))
    tags = [child.tag.rsplit("}", 1)[-1] for child in section_list]
    assert tags == ["section", "section", "section", "extLst"]


def test_more_marker_survives_an_oversized_slide(scripts, tmp_path):
    """One slide alone can exceed the budget (--full, many long boxes). The output is
    then cut by the caller; "more" and the warning must come before the slides."""

    def hook(prs):
        for _ in range(60):
            tb = prs.slides[0].shapes.add_textbox(Cm(1), Cm(1), Cm(4), Cm(2))
            tb.text_frame.text = "长文字" * 120

    path = tmp_path / "big.pptx"
    _titled_deck(path, 3, hook)
    out = H.run(scripts, "inspect_template.py", path, "--texts", "--full").stdout
    assert len(out) > 20_000, "the case must really be oversized"
    head = out[:2_000]
    res = json.loads(out)
    assert res["more"] == "--slides 2-3" and '"more"' in head
    assert res["too_long"] == [1] and '"too_long"' in head
    assert out.index('"more"') < out.index('"slides"')
    assert [s["number"] for s in res["slides"]] == [1]


def _fake_slide(xml: str):
    root = parse_xml(xml)
    return types.SimpleNamespace(_element=root, part=types.SimpleNamespace(_element=root))


def test_ignorable_prefix_bound_elsewhere_on_the_copy_is_not_carried(scripts):
    spec = importlib.util.spec_from_file_location("ps_dup_slide", scripts / "duplicate_slide.py")
    dup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dup)
    src = _fake_slide(
        f'<p:sld xmlns:p="{P}" xmlns:mc="{MC}" xmlns:p14="{P14}" '
        'xmlns:x15="urn:example:x15" mc:Ignorable="p14 x15"><p:cSld><p:spTree/></p:cSld></p:sld>'
    )
    new = _fake_slide(
        f'<p:sld xmlns:p="{P}" xmlns:x15="urn:example:other"><p:cSld><p:spTree/></p:cSld></p:sld>'
    )
    dup._carry_ignorable(src, new)
    root = new._element
    assert root.get(f"{{{MC}}}Ignorable") == "p14", "the clashing prefix is left out"
    assert root.nsmap["p14"] == P14 and root.nsmap["x15"] == "urn:example:other"
    new_all = _fake_slide(f'<p:sld xmlns:p="{P}" xmlns:p14="urn:example:other"><p:cSld/></p:sld>')
    src_one = _fake_slide(
        f'<p:sld xmlns:p="{P}" xmlns:mc="{MC}" xmlns:p14="{P14}" mc:Ignorable="p14"/>'
    )
    dup._carry_ignorable(src_one, new_all)
    assert new_all._element.get(f"{{{MC}}}Ignorable") is None, "nothing left to ignore"
