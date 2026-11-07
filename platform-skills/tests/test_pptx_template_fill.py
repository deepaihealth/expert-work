"""Layer 1: pptx inspect_template.py (texts) + fill_template.py on synthetic designed decks.

Scripts run from the built package (``unpacked_skills``), as the agent runs them; decks
come from in_image/_pptx_fixtures.py so layer 2 builds the very same decks.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pptx
import pytest
from lxml import etree
from pptx.oxml.ns import qn

FIXTURES = Path(__file__).parent / "in_image" / "_pptx_fixtures.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


fx = _load("ps_pptx_fixtures", FIXTURES)


@pytest.fixture(scope="module")
def scripts(unpacked_skills, tmp_path_factory) -> Path:
    # a private copy: running the scripts writes __pycache__, which must not land in the
    # shared unpacked package that test_platform_checks scans file by file
    dst = tmp_path_factory.mktemp("pptx") / "scripts"
    shutil.copytree(unpacked_skills / "pptx" / "scripts", dst)
    return dst


@pytest.fixture(scope="module")
def text_mod(scripts):
    return _load("ps_pptx_text", scripts / "_pptx_text.py")


@pytest.fixture
def deck(tmp_path) -> tuple[Path, dict[str, str]]:
    path = tmp_path / "设计好的 模板.pptx"
    return path, fx.build_designed_deck(path)


def _run(scripts: Path, name: str, *args, expect: int = 0) -> subprocess.CompletedProcess:
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(scripts / name), *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == expect, proc.stdout + proc.stderr
    return proc


def _inspect(scripts, path, *flags) -> dict:
    return json.loads(_run(scripts, "inspect_template.py", path, *flags).stdout)


def _fill(scripts, tmp_path, src, spec, *, expect=0) -> tuple[dict | None, Path]:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "成品.pptx"
    proc = _run(scripts, "fill_template.py", src, spec_path, out, expect=expect)
    return (json.loads(proc.stdout) if proc.stdout.strip() else None), out


def _texts(result: dict) -> dict[str, dict]:
    return {t["addr"]: t for s in result["slides"] for t in s["texts"]}


def _shape_by_addr(prs, addr: str):
    slide_no, path = addr.split("@")[0].split(":")
    shapes = prs.slides[int(slide_no) - 1].shapes
    shape = None
    for sid in path.split("/"):
        shape = next(s for s in shapes if s.shape_id == int(sid))
        shapes = getattr(shape, "shapes", None)
    return shape


def _xml_by_id(prs) -> dict[tuple[int, str], bytes]:
    """Every shape element (any depth) by (slide number, cNvPr id) -> canonical bytes."""
    out = {}
    for n, slide in enumerate(prs.slides, start=1):
        tree = slide.shapes._spTree
        for c_nv in tree.iter(qn("p:cNvPr")):
            shape_el = c_nv.getparent().getparent()
            if shape_el is tree:
                continue
            out[(n, c_nv.get("id"))] = etree.tostring(shape_el, method="c14n")
    return out


# ------------------------------------------------------------------ inspect


def test_inspect_addresses_cover_every_structure(scripts, deck):
    path, roles = deck
    res = _inspect(scripts, path)
    texts = _texts(res)
    expect_kind = {
        "s1_title": "ph:ctrTitle#0",
        "s1_sub": "ph:subTitle#1",
        "s2_body": "ph:obj#1",
        "s3_head": "textbox",
        "s4_g_a": "textbox",
        "s4_g_b": "textbox",
        "s4_lone": "textbox",
        "s5_table": "table",
        "s6_caption": "textbox",
    }
    for role, kind in expect_kind.items():
        assert texts[roles[role]]["kind"] == kind, (role, texts.get(roles[role]))
    assert roles["s4_g_a"].count("/") == 1 and roles["s4_g_b"].count("/") == 2
    assert roles["s3_deco"] not in texts, "an empty decorative shape is not a text target"
    assert roles["s4_outer"] not in texts, "a group itself is not a text target"
    locked = {t["addr"]: t["kind"] for s in res["slides"] for t in s["locked"]}
    assert locked == {
        roles["s6_chart"]: "chart",
        roles["s6_smartart"]: "smartart",
        roles["s6_pic"]: "picture",
    }
    assert texts[roles["s1_title"]]["text"] == "年度工作汇报"
    assert texts[roles["s4_g_b"]]["text"] == "嵌套组内"


def test_inspect_keeps_legacy_layout_and_shape_output(scripts, deck):
    path, _ = deck
    res = _inspect(scripts, path)
    assert len(res["layouts"]) == 11 and {"master", "index", "name", "placeholders"} <= set(
        res["layouts"][0]
    )
    ph = res["layouts"][0]["placeholders"][0]
    assert {"idx", "type", "name", "left", "top", "width", "height"} <= set(ph)
    slide = res["slides"][0]
    assert slide["number"] == 1 and slide["master"] == 0 and slide["layout"] == "Title Slide"
    assert [set(s) for s in slide["shapes"]] == [{"name", "type", "has_text"}] * 2
    compact = _inspect(scripts, path, "--texts")
    assert "layouts" not in compact and "shapes" not in compact["slides"][0]
    assert compact["slides"][0]["texts"] == slide["texts"]


def test_inspect_table_cells_and_merges(scripts, deck):
    path, roles = deck
    table = _texts(_inspect(scripts, path))[roles["s5_table"]]
    assert table["size"] == [3, 3]
    assert table["cells"][0] == ["格11", "格12", "格13"]
    assert table["cells"][2][1] is None, "the merged-away cell is shown as null"
    assert table["font_pt"] == 12
    # 6 cm column = 170.1 pt, minus 2 x 7.2 pt margins = 155.7 pt; at 12 pt: 12 CJK chars
    assert table["col_chars"] == [12, 12, 12]


def test_inspect_font_sizes_autofit_and_wrap(scripts, deck):
    path, roles = deck
    texts = _texts(_inspect(scripts, path))
    assert texts[roles["s3_head"]]["font_pt"] == 28
    assert texts[roles["s3_mixed"]]["font_pt"] == [14, 12], "per-paragraph first-run sizes"
    assert texts[roles["s3_mixed"]]["paras"] == 2 and texts[roles["s3_mixed"]]["runs"] == 3
    # inherited, nothing set on the slide: master bodyStyle lvl1 / lvl2 = 32 / 28 pt
    assert texts[roles["s2_body"]]["font_pt"] == [32, 28, 28]
    assert texts[roles["s1_title"]]["font_pt"] == 44
    assert texts[roles["s3_shrink"]]["autofit"] == "shrink:70%"
    assert texts[roles["s3_resize"]]["autofit"] == "resize"
    assert texts[roles["s3_head"]]["autofit"] == "off"
    assert texts[roles["s3_nowrap"]].get("nowrap") is True
    assert "nowrap" not in texts[roles["s3_head"]]


def test_inspect_box_is_in_slide_space_for_group_children(scripts, deck):
    path, roles = deck
    texts = _texts(_inspect(scripts, path))
    # child (1, 1, 6, 1.5) cm inside a group drawn 2x wide
    assert texts[roles["s4_g_a"]]["box"] == [2.0, 1.0, 12.0, 1.5]
    assert texts[roles["s4_lone"]]["box"] == [20.0, 1.0, 6.0, 1.5]


def test_inspect_capacity_estimate(scripts, deck):
    path, roles = deck
    small = _texts(_inspect(scripts, path))[roles["s3_small"]]
    # 3 x 1.2 cm box, 18 pt: (85.0 - 14.4) / 18 = 3 chars per line, (34.0 - 7.2) / 21.6 = 1 line
    assert small["cap"] == 3


def test_inspect_preview_truncation_and_full(scripts, deck, tmp_path):
    path, roles = deck
    long_text = "很长的说明文字" * 10
    _, filled = _fill(scripts, tmp_path, path, {"fill": {roles["s3_mixed"]: long_text}})
    short = _texts(_inspect(scripts, filled))[roles["s3_mixed"]]["text"]
    assert short == long_text[:30] + "…"
    full = _texts(_inspect(scripts, filled, "--full"))[roles["s3_mixed"]]["text"]
    assert full == long_text


# ------------------------------------------------------------------ fill


def test_fill_changes_exactly_the_targets(scripts, deck, tmp_path):
    path, roles = deck
    spec = {
        roles["s1_title"]: "新的标题",
        roles["s3_head"]: "新标题文字",
        roles["s4_g_b"]: "嵌套组新字",
        roles["s5_table"] + "@2,3": "新格",
        roles["s3_cjk"]: "Revenue 营收 up 9%",
    }
    report, out = _fill(scripts, tmp_path, path, {"fill": spec})
    assert report["saved"] is True
    assert {r["addr"]: r["status"] for r in report["results"]} == dict.fromkeys(spec, "ok")
    assert report["changed_pages"] == [1, 3, 4, 5]
    before, after = pptx.Presentation(str(path)), pptx.Presentation(str(out))
    assert _shape_by_addr(after, roles["s1_title"]).text_frame.text == "新的标题"
    assert _shape_by_addr(after, roles["s4_g_b"]).text_frame.text == "嵌套组新字"
    assert _shape_by_addr(after, roles["s3_cjk"]).text_frame.text == "Revenue 营收 up 9%"
    cell = _shape_by_addr(after, roles["s5_table"]).table.cell(1, 2)
    assert cell.text_frame.text == "新格"
    assert _shape_by_addr(after, roles["s5_table"]).table.cell(1, 1).text_frame.text == "格22"
    # formatting of the first run survives
    run = _shape_by_addr(after, roles["s3_head"]).text_frame.paragraphs[0].runs[0]
    assert (run.font.size.pt, run.font.bold, run.font.color.rgb) == (28, True, fx.RED)
    nested = _shape_by_addr(after, roles["s4_g_b"]).text_frame.paragraphs[0].runs[0]
    assert (nested.font.size.pt, nested.font.color.rgb) == (14, fx.BLUE)
    assert cell.text_frame.paragraphs[0].runs[0].font.size.pt == 12
    # every other shape is byte-identical (canonical XML), groups/tables minus the target
    xml_before, xml_after = _xml_by_id(before), _xml_by_id(after)
    assert set(xml_before) == set(xml_after)
    touched = {
        (1, roles["s1_title"].split(":")[1]),
        (3, roles["s3_head"].split(":")[1]),
        (3, roles["s3_cjk"].split(":")[1]),
        *((4, sid) for sid in roles["s4_g_b"].split(":")[1].split("/")),  # target + its groups
        (5, roles["s5_table"].split(":")[1]),
    }
    changed = {k for k in xml_before if xml_before[k] != xml_after[k]}
    assert changed == touched


def test_fill_keeps_non_text_parts_of_the_target(scripts, deck, tmp_path):
    path, roles = deck
    _, out = _fill(scripts, tmp_path, path, {"fill": {roles["s3_shrink"]: "新字"}})
    old = _shape_by_addr(pptx.Presentation(str(path)), roles["s3_shrink"])._element
    new = _shape_by_addr(pptx.Presentation(str(out)), roles["s3_shrink"])._element
    for tag in ("p:nvSpPr", "p:spPr"):
        assert etree.tostring(old.find(qn(tag))) == etree.tostring(new.find(qn(tag)))
    for tag in ("a:bodyPr", "a:lstStyle"):
        path_ = qn("p:txBody") + "/" + qn(tag)
        assert etree.tostring(old.find(path_)) == etree.tostring(new.find(path_))


def test_fill_paragraphs_levels_and_mixed_runs(scripts, deck, tmp_path):
    path, roles = deck
    spec = {
        roles["s2_body"]: [
            {"text": "要点一", "level": 0},
            {"text": "细节", "level": 1},
            "细节二",
            {"text": "更深一层", "level": 2},
        ],
        roles["s3_mixed"]: ["新小标题", "新正文\n第二行"],
    }
    _, out = _fill(scripts, tmp_path, path, {"fill": spec})
    prs = pptx.Presentation(str(out))
    body = _shape_by_addr(prs, roles["s2_body"]).text_frame.paragraphs
    assert [p.text for p in body] == ["要点一", "细节", "细节二", "更深一层"]
    # explicit levels honoured (2 exists nowhere in the template); a paragraph without a
    # level reuses old paragraph i's format (level 1)
    assert [p.level for p in body] == [0, 1, 1, 2]
    # and the new level picks up its own inherited size (master bodyStyle lvl3 = 24 pt)
    sizes = _texts(_inspect(scripts, out))[roles["s2_body"]]["font_pt"]
    assert sizes == [32, 28, 28, 24]
    mixed = _shape_by_addr(prs, roles["s3_mixed"]).text_frame.paragraphs
    assert [len(p.runs) for p in mixed] == [1, 2]
    first = mixed[0].runs[0].font
    assert (first.size.pt, first.bold, first.color.rgb) == (14, True, fx.BLUE)
    assert mixed[1].runs[0].font.size.pt == 12
    assert mixed[1]._p.findall(qn("a:br")), "\\n inside a list item is a line break"


def test_fill_string_value_newlines_are_paragraphs(scripts, deck, tmp_path):
    path, roles = deck
    _, out = _fill(scripts, tmp_path, path, {"fill": {roles["s3_head"]: "第一段\n第二段"}})
    paras = _shape_by_addr(pptx.Presentation(str(out)), roles["s3_head"]).text_frame.paragraphs
    assert [p.text for p in paras] == ["第一段", "第二段"]
    assert all(p.runs[0].font.size.pt == 28 for p in paras)


def test_fill_reports_unknown_and_locked_addresses_and_saves_nothing(scripts, deck, tmp_path):
    path, roles = deck
    spec = {
        roles["s3_head"]: "fine",
        "3:999": "nope",
        roles["s6_chart"]: "x",
        roles["s6_smartart"]: "x",
        roles["s6_pic"]: "x",
        roles["s5_table"] + "@3,2": "merged",
        roles["s5_table"] + "@9,9": "nope",
    }
    report, out = _fill(scripts, tmp_path, path, {"fill": spec}, expect=1)
    assert report["saved"] is False
    assert {r["addr"]: r["status"] for r in report["results"]} == {
        roles["s3_head"]: "ok",
        "3:999": "not_found",
        roles["s6_chart"]: "locked:chart",
        roles["s6_smartart"]: "locked:smartart",
        roles["s6_pic"]: "locked:picture",
        roles["s5_table"] + "@3,2": "merged_cell",
        roles["s5_table"] + "@9,9": "not_found",
    }
    assert not out.exists()


@pytest.mark.parametrize(
    ("spec", "needle"),
    [
        ({"fill": {"标题": "x"}}, "格式不对"),
        ({"fill": {"99:2": "x"}}, "没有第 99 页"),
        ({"fill": {"1:2": 5}}, "字符串或非空列表"),
        ({"fill": {"1:2": [{"text": "x", "level": 12}]}}, "level"),
        ({"fill": {}, "slides": [1]}, "二选一"),
        ({"fil": {}}, "只能有"),
    ],
)
def test_fill_rejects_bad_specs(scripts, deck, tmp_path, spec, needle):
    path, _ = deck
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    proc = _run(scripts, "fill_template.py", path, spec_path, tmp_path / "o.pptx", expect=1)
    assert needle in proc.stderr


def test_overflow_estimate_flags_long_text_only(scripts, deck, tmp_path):
    path, roles = deck
    report, _ = _fill(scripts, tmp_path, path, {"fill": {roles["s3_small"]: "短句"}})
    assert report["overflow"] == []
    report, _ = _fill(
        scripts, tmp_path, path, {"fill": {roles["s3_small"]: "这是一段明显放不下的很长的文字"}}
    )
    (item,) = report["overflow"]
    assert item["addr"] == roles["s3_small"] and item["need_lines"] > item["fit_lines"]
    assert item["cap_chars"] == 3 and item["chars"] == 15 and item["font_pt"] == 18
    assert "估算" in report["overflow_note"]


def test_overflow_estimate_reflects_cjk_vs_latin_width(scripts, deck, tmp_path):
    path, roles = deck  # s3_head: 20 x 2 cm, 28 pt -> 19 CJK chars, one line
    report, _ = _fill(scripts, tmp_path, path, {"fill": {roles["s3_head"]: "a" * 30}})
    assert report["overflow"] == [], "30 Latin chars fit where 30 CJK chars do not"
    report, _ = _fill(scripts, tmp_path, path, {"fill": {roles["s3_head"]: "字" * 30}})
    assert [o["addr"] for o in report["overflow"]] == [roles["s3_head"]]


def test_overflow_in_table_cell_and_nowrap(scripts, deck, tmp_path):
    path, roles = deck
    cell = roles["s5_table"] + "@1,1"
    spec = {
        cell: "表格单元格里放了特别特别多的字" * 3,
        roles["s3_nowrap"]: "不换行的框放了很长一句话",
    }
    report, _ = _fill(scripts, tmp_path, path, {"fill": spec})
    by_addr = {o["addr"]: o for o in report["overflow"]}
    assert by_addr[cell]["autofit"] == "resize"
    assert by_addr[roles["s3_nowrap"]]["too_wide"] is True


def test_overflow_estimate_uses_line_and_paragraph_spacing(scripts, deck, tmp_path):
    path, roles = deck  # both boxes: two 18 pt lines at single spacing
    two_lines = ["第一行", "第二行"]
    spec = {roles["s3_spaced"]: two_lines, roles["s3_gap"]: two_lines, roles["s3_small"]: "短"}
    report, _ = _fill(scripts, tmp_path, path, {"fill": spec})
    by_addr = {o["addr"]: o for o in report["overflow"]}
    assert set(by_addr) == {roles["s3_spaced"], roles["s3_gap"]}
    assert (
        by_addr[roles["s3_spaced"]]["need_lines"],
        by_addr[roles["s3_spaced"]]["fit_lines"],
    ) == (2, 1)


def test_overflow_estimate_uses_stored_shrink_scale(scripts, deck, tmp_path):
    path, roles = deck  # 8 x 2 cm at 20 pt x 70% = 14 pt: 15 chars x 2 lines fit
    report, _ = _fill(scripts, tmp_path, path, {"fill": {roles["s3_shrink"]: "字" * 24}})
    assert report["overflow"] == []


def test_fill_drops_the_old_words_hyperlink(scripts, deck, tmp_path):
    path, roles = deck
    old = _shape_by_addr(pptx.Presentation(str(path)), roles["s6_caption"])
    assert old.text_frame.paragraphs[0].runs[0].hyperlink.address
    _, out = _fill(scripts, tmp_path, path, {"fill": {roles["s6_caption"]: "新图注"}})
    run = (
        _shape_by_addr(pptx.Presentation(str(out)), roles["s6_caption"])
        .text_frame.paragraphs[0]
        .runs[0]
    )
    assert run.text == "新图注" and run.hyperlink.address is None
    assert run.font.size.pt == 10


def test_slides_mode_duplicates_reorders_and_drops_only_when_asked(scripts, deck, tmp_path):
    path, roles = deck
    head = roles["s3_head"]
    slides = [{"from": 3, "fill": {head: "第一节"}}, 1, {"from": 3, "fill": {head: "第二节"}}]
    report, _ = _fill(scripts, tmp_path, path, {"slides": slides}, expect=1)
    assert report is None  # spec error: unlisted slides without drop_unlisted
    original = path.read_bytes()
    report, out = _fill(scripts, tmp_path, path, {"slides": slides, "drop_unlisted": True})
    assert path.read_bytes() == original, "the template file itself is never written"
    assert report["slides"] == [3, 1, 3] and report["changed_pages"] == [1, 3]
    prs = pptx.Presentation(str(out))
    assert len(prs.slides) == 3
    heads = [_shape_by_addr(prs, f"{n}:{head.split(':')[1]}").text_frame.text for n in (1, 3)]
    assert heads == ["第一节", "第二节"]
    assert prs.slides[1].shapes.title.text == "年度工作汇报"
    run = _shape_by_addr(prs, f"3:{head.split(':')[1]}").text_frame.paragraphs[0].runs[0]
    assert (run.font.size.pt, run.font.bold) == (28, True)


def test_slides_mode_address_must_match_its_source_slide(scripts, deck, tmp_path):
    path, roles = deck
    spec = {"slides": [{"from": 1, "fill": {roles["s3_head"]: "x"}}], "drop_unlisted": True}
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    proc = _run(scripts, "fill_template.py", path, spec_path, tmp_path / "o.pptx", expect=1)
    assert "不在第 1 页" in proc.stderr


def test_slides_mode_keeps_everything_when_all_listed(scripts, deck, tmp_path):
    path, _ = deck
    report, out = _fill(scripts, tmp_path, path, {"slides": [1, 2, 3, 4, 5, 6]})
    assert report["changed_pages"] == [] and len(pptx.Presentation(str(out)).slides) == 6


def test_multiple_masters(scripts, tmp_path):
    path = tmp_path / "双母版.pptx"
    roles = fx.build_two_master_deck(path, tmp_path)
    res = _inspect(scripts, path)
    assert [(s["number"], s["master"]) for s in res["slides"]] == [(1, 0), (2, 1)]
    texts = _texts(res)
    assert texts[roles["m1_title"]]["text"] == "母版二的标题"
    assert texts[roles["m1_title"]]["font_pt"] == 44, "size inherited through master 2"
    _, out = _fill(scripts, tmp_path, path, {"fill": {roles["m1_title"]: "改了"}})
    prs = pptx.Presentation(str(out))
    assert prs.slides[1].shapes.title.text == "改了"
    assert prs.slides[0].shapes.title.text == "母版一的标题"


def test_legacy_placeholder_type_without_known_base(scripts, tmp_path):
    """A slide placeholder whose layout placeholder has a type python-pptx cannot map to
    the master (sldImg), geometry inherited only: listed with box null, fillable."""
    prs = pptx.Presentation()
    layout = prs.slide_layouts[1]
    slide = prs.slides.add_slide(layout)
    body = slide.placeholders[1]
    body.text = "旧版占位符"
    layout_ph = layout.placeholders[1]._element
    layout_ph.find(qn("p:nvSpPr") + "/" + qn("p:nvPr") + "/" + qn("p:ph")).set("type", "sldImg")
    path = tmp_path / "legacy.pptx"
    prs.save(str(path))
    addr = f"1:{body.shape_id}"
    entry = _texts(_inspect(scripts, path))[addr]
    assert entry["box"] is None and entry["cap"] is None and entry["text"] == "旧版占位符"
    report, out = _fill(scripts, tmp_path, path, {"fill": {addr: "新字"}})
    assert report["results"] == [{"page": 1, "addr": addr, "status": "ok"}]
    assert pptx.Presentation(str(out)).slides[0].placeholders[1].text == "新字"


# ------------------------------------------------------------------ estimate unit


def test_wrap_lines_word_wrap_and_cjk(text_mod):
    assert text_mod.wrap_lines("字" * 10, 5) == 2
    assert text_mod.wrap_lines("字" * 11, 5) == 3
    # "hello world" at 0.5 em/char: "hello " fits in 4 em, "world" goes to line 2
    assert text_mod.wrap_lines("hello world", 4) == 2
    assert text_mod.wrap_lines("hello world", 7) == 1
    # a word longer than the line breaks inside itself
    assert text_mod.wrap_lines("a" * 20, 5.5) == 2
    assert text_mod.wrap_lines("", 5) == 1
