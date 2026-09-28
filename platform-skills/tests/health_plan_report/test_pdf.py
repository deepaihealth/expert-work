import base64
import importlib.util
import io
import json
import re
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

import pytest
from hpr import ppt_charts, theme
from hpr.blocks import RenderError
from hpr.content import required_texts
from hpr.pdf_guard import check_cover, check_footer, check_url, file_path, raise_for_failed_images
from hpr.pdf_html import build_html, css_string, render_pdf
from hpr.pdf_svg import bar_svg, cover_deco_svg, donut_svg, icon_svg, line_svg, timebar_svg
from hpr.ppt_layout import LayoutError
from hpr.prims import Chart
from hpr.qa import missing_texts
from hpr.style import Layer, resolve
from hpr.theme import build_theme
from PIL import Image as PILImage

RENDER = Path(__file__).resolve().parents[2] / "health-plan-report" / "scripts" / "render.py"
HAS_WEASY = importlib.util.find_spec("weasyprint") is not None


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "svg"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("style", "svg"):
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _visible_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    return "".join(p.parts)


@pytest.fixture
def base(tmp_path):
    PILImage.new("RGB", (640, 360), "#DDEEEE").save(tmp_path / "trend-note.png")
    return tmp_path


def _html(sample, base, layers=()):
    style = resolve(list(layers), sample).style
    return build_html(sample, style, build_theme(style, "pdf"), base)


def _cover(html: str) -> str:
    return html[html.index('<section class="cover') : html.index("</section>")]


def test_every_required_text_is_in_visible_html(sample, base):
    html, warnings = _html(sample, base)
    assert sample["period"]["label"] in required_texts(sample)
    assert missing_texts(required_texts(sample), _visible_text(html)) == []
    assert warnings == []


def test_cover_meta_includes_period_even_when_subtitle_omits_it(sample, base):
    sample["subtitle"] = "控糖与体重管理"
    cover = _cover(_html(sample, base)[0])
    assert '<div class="k">阶段</div>' in cover
    assert sample["period"]["label"] in _visible_text(cover)


def test_no_scripts_and_font_declared(sample, base):
    html, _ = _html(sample, base)
    assert "<script" not in html.lower()
    assert "Noto Sans CJK SC" in html


def test_page_number_and_footer_options(sample, base):
    html, _ = _html(sample, base)
    assert "counter(page)" in html and "counter(pages)" in html
    html2, _ = _html(
        sample, base, [Layer("x", {"footer.page_number": "关", "footer.align": "居中"})]
    )
    assert "counter(page)" not in html2
    assert "@bottom-center" in html2


@pytest.mark.parametrize("variant", ["band", "split", "minimal"])
def test_cover_variants(sample, base, variant):
    html, _ = _html(sample, base, [Layer("x", {"cover.variant": variant})])
    assert f'class="cover cover-{variant}' in html


@pytest.mark.parametrize("variant", ["band", "split", "minimal"])
def test_cover_text_is_laid_out_in_flow_not_at_fixed_offsets(sample, base, variant):
    html, _ = _html(sample, base, [Layer("x", {"cover.variant": variant})])
    css = html[html.index("<style>") : html.index("</style>")]
    for sel in (".cover h1", ".cover .sub", ".cover .meta"):
        rules = [m.group(1) for m in re.finditer(re.escape(sel) + r"\s*\{([^}]*)\}", css)]
        assert rules, sel
        fixed = [r for r in rules if "absolute" in r or re.search(r"(?<![-\w])(top|bottom)\s*:", r)]
        assert fixed == [], (sel, fixed)


def test_toc_rules(sample, base):
    assert 'class="toc"' in _html(sample, base)[0]  # 样例 9 个章节 ≥ 6
    assert 'class="toc"' not in _html(sample, base, [Layer("x", {"toc": "off"})])[0]


def test_css_string_escapes():
    assert css_string('a"b\\c\nd') == '"a\\22 b\\5C c\\A d"'


HOSTILE = '李先生</style><img src="http://x/y">\r\f\u2028方案'


def test_css_string_is_one_token_without_raw_markup_or_controls():
    out = css_string(HOSTILE)
    inner = out[1:-1]
    assert out[0] == out[-1] == '"' and '"' not in inner
    assert "<" not in out and ">" not in out
    assert not any(ord(c) < 0x20 or c in "\u2028\u2029\x7f" for c in out)


def test_hostile_title_and_org_cannot_inject_markup(sample, base):
    sample["title"] = HOSTILE
    sample["brand"]["org_name"] = HOSTILE
    html, _ = _html(sample, base)
    assert html.count("</style>") == 1
    assert '<img src="http' not in html
    assert not any(c in html for c in "\r\f\u2028")  # would garble the PDF text layer
    style = html[html.index("<style>") + len("<style>") : html.index("</style>")]
    assert "<" not in style


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("data:image/png;base64,AAAA", True),
        ("http://x/y.png", False),
        ("https://x/y.png", False),
        ("ftp://x/y.png", False),
        ("file://remote-host/etc/passwd", False),
    ],
)
def test_url_policy_schemes(tmp_path, url, ok):
    if ok:
        check_url(url, tmp_path)
    else:
        with pytest.raises(ValueError, match="不允许"):
            check_url(url, tmp_path)


def test_url_policy_files(tmp_path):
    base = tmp_path / "work"
    base.mkdir()
    inside = base / "a.png"
    outside = tmp_path / "secret.png"
    check_url(inside.as_uri(), base)
    for bad in (outside.as_uri(), (base / ".." / "secret.png").as_uri()):
        with pytest.raises(ValueError):
            check_url(bad, base)
    check_url(outside.as_uri(), base, frozenset({outside.resolve()}))


def test_chart_and_its_caption_stay_together(sample, base):
    html, _ = _html(sample, base)
    trend = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "trend")
    keep = html[html.index('<div class="keep">') :]
    assert (
        keep.index('<div class="chart">') < keep.index(trend["ref_text"]) < keep.index("</p></div>")
    )


def test_line_chart_labels_bounds_and_unit():
    t = build_theme(resolve([]).style, "pdf")
    line = Chart("line", ("09-01", "09-08"), (6.8, 6.2), "mmol/L", low=4.4, high=6.1)
    svg = line_svg(line, t)
    assert "目标下限 4.4" in svg and "目标上限 6.1" in svg and "mmol/L" in svg


def test_corrupt_image_is_a_block_error_not_a_crash(sample, base):
    noisy = PILImage.effect_noise((640, 360), 64).convert("RGB")
    noisy.save(base / "trend-note.png")
    data = (base / "trend-note.png").read_bytes()
    (base / "trend-note.png").write_bytes(data[: len(data) // 2])  # header fine, body truncated
    with pytest.raises(RenderError) as exc:
        render_pdf(sample, resolve([], sample).style, base / "x.pdf", base)
    assert exc.value.path.startswith("sections[") and "图片无法读取" in str(exc.value)
    assert not (base / "x.pdf").exists()


def test_user_text_is_escaped(sample, base):
    sample["sections"][0]["blocks"][0]["items"][0]["text"] = "<b>x</b> & y"
    html, _ = _html(sample, base)
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in html


def test_svgs_are_well_formed():
    t = build_theme(resolve([]).style, "pdf")
    line = Chart("line", ("09-01", "09-08", "09-15"), (6.8, 6.5, 6.2), "mmol/L", low=4.4, high=6.1)
    bar = Chart("bar", ("a", "b"), (1.0, 3.0))
    donut = Chart(
        "donut",
        ("碳水", "蛋白", "脂肪"),
        (45.0, 23.0, 32.0),
        legend=("a", "b", "c"),
        center="1650 kcal",
    )
    for svg in (
        line_svg(line, t),
        bar_svg(bar, t),
        donut_svg(donut, t),
        icon_svg("pulse", "#fff", 5),
        timebar_svg("23:00", "06:30", t.primary, t),
        cover_deco_svg(t, "#FFFFFF"),
    ):
        ET.fromstring(svg)  # noqa: S314 — our own generated SVG


def test_donut_palette_has_a_single_source():
    assert ppt_charts.donut_colors is theme.donut_colors
    t = build_theme(resolve([]).style, "pdf")
    donut = Chart("donut", ("a", "b"), (1.0, 1.0))
    assert all(c in donut_svg(donut, t) for c in theme.donut_colors(t)[:2])


def test_missing_logo_warns(sample, base):
    sample["brand"]["logo_path"] = "nope.png"
    _, warnings = _html(sample, base)
    assert any("LOGO" in w for w in warnings)


def _padded_logo(base: Path) -> None:
    im = PILImage.new("RGBA", (400, 300), (0, 0, 0, 0))
    im.paste((11, 79, 92, 255), (100, 120, 300, 180))  # visible 200x60 inside transparent padding
    im.save(base / "logo.png")


def _logo_srcs(cover: str) -> list[str]:
    return re.findall(r'<img src="([^"]+)"', cover)


def test_logo_is_trimmed_and_inlined_without_touching_disk(sample, base):
    _padded_logo(base)
    sample["brand"]["logo_path"] = "logo.png"
    before = sorted(p.name for p in base.iterdir())
    (src,) = _logo_srcs(_cover(_html(sample, base)[0]))
    assert src.startswith("data:image/png;base64,")
    with PILImage.open(io.BytesIO(base64.b64decode(src.split(",", 1)[1]))) as im:
        assert im.size == (200, 60)
    assert sorted(p.name for p in base.iterdir()) == before


@pytest.mark.parametrize(
    ("variant", "pos", "plated"),
    [("band", "top-right", True), ("split", "top-left", True), ("minimal", "top-right", False)],
)
def test_logo_on_dark_cover_sits_on_white_plate(sample, base, variant, pos, plated):
    _padded_logo(base)
    sample["brand"]["logo_path"] = "logo.png"
    layers = [Layer("x", {"cover.variant": variant, "brand.logo_position": pos})]
    cover = _cover(_html(sample, base, layers)[0])
    assert len(_logo_srcs(cover)) == 1
    assert ('class="plate"><img' in cover) is plated


class _Box:
    def __init__(self, tag, cls, y, h, x=0.0, w=100.0):
        self.element = ET.Element(tag, {"class": cls} if cls else {})
        self._r = (x, y, w, h)

    def border_box_x(self):
        return self._r[0]

    def border_box_y(self):
        return self._r[1]

    def border_width(self):
        return self._r[2]

    def border_height(self):
        return self._r[3]


class _PageBox:
    def __init__(self, boxes):
        self.boxes = boxes

    def descendants(self):
        return iter(self.boxes)

    def margin_height(self):
        return 1000.0


class _Doc:
    def __init__(self, *pages):
        self.pages = [type("P", (), {"_page_box": _PageBox(b)})() for b in pages]


def _cover_boxes(sub_y=260.0, meta_y=800.0):
    return [
        _Box("section", "cover cover-band", 0, 1000),
        _Box("h1", "", 150, 100),
        _Box("div", "sub", sub_y, 40),
        _Box("div", "meta", meta_y, 120),
    ]


def _foot_doc(bottom):
    cover = [_Box("section", "cover cover-band", 0, 1000)]
    return _Doc(cover, [_Box("div", "pfoot", bottom - 60, 60)], [_Box("div", "pfoot", 900, 40)])


def test_check_footer_accepts_footer_above_the_edge():
    check_footer(_foot_doc(975))


def test_check_footer_rejects_footer_running_into_the_edge():
    with pytest.raises(LayoutError) as exc:
        check_footer(_foot_doc(990))
    assert exc.value.path == "brand.disclaimer"


def test_check_cover_accepts_clean_layout():
    check_cover(_Doc(_cover_boxes(), [_Box("section", "toc", 0, 500)]))


@pytest.mark.parametrize(
    ("doc", "path"),
    [
        (_Doc(_cover_boxes(sub_y=200)), "title"),  # subtitle drawn over the title
        (_Doc(_cover_boxes(meta_y=950)), "client.facts"),  # meta runs past the page bottom
        (_Doc(_cover_boxes(), [_Box("section", "cover cover-band", 0, 80)]), "title"),  # spills
        (_Doc(_cover_boxes()[:3]), "title"),  # meta pushed off the fixed-height cover entirely
    ],
)
def test_check_cover_rejects_overlap_and_overflow(doc, path):
    with pytest.raises(LayoutError) as exc:
        check_cover(doc)
    assert exc.value.path == path


@pytest.mark.skipif(HAS_WEASY, reason="本地无 weasyprint 时才测这条报错路径")
def test_pdf_without_weasyprint_is_a_clear_error(sample, base):
    (base / "plan.json").write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
    res = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(RENDER),
            "--content",
            str(base / "plan.json"),
            "--format",
            "pdf",
            "--out-dir",
            str(base / "o"),
            "--basename",
            "x",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 1
    assert "weasyprint" in json.loads(res.stdout)["errors"][0]


def test_url_policy_allows_referenced_file_with_percent_in_name(tmp_path):
    base = tmp_path / "work"
    base.mkdir()
    outside = tmp_path / "b%41.png"  # a real file name containing "%41"
    check_url(outside.as_uri(), base, frozenset({outside.resolve()}))


def test_url_policy_refuses_single_encoded_traversal(tmp_path):
    base = tmp_path / "work"
    base.mkdir()
    for tail in ("/%2e%2e/secret.png", "/x%2f..%2f..%2fsecret.png"):
        with pytest.raises(ValueError):
            check_url(base.as_uri() + tail, base)


def test_url_policy_refuses_symlink_named_like_an_encoded_slash(tmp_path):
    base = tmp_path / "work"
    base.mkdir()
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "hostname").write_text("secret", encoding="utf-8")
    (base / "L%2fX").symlink_to(tmp_path / "etc")
    url = (base / "L%2fX" / "hostname").as_uri()
    assert "L%252fX" in url
    with pytest.raises(ValueError):
        check_url(url, base)


def test_policy_judges_the_path_that_is_actually_opened(tmp_path):
    base = tmp_path / "work"
    (base / "%2e%2e").mkdir(parents=True)
    (base / "%2e%2e" / "f.txt").write_text("inside", encoding="utf-8")
    (tmp_path / "f.txt").write_text("outside", encoding="utf-8")
    url = base.as_uri() + "/%252e%252e/f.txt"
    check_url(url, base)
    with urllib.request.urlopen(url) as fh:  # noqa: S310 — local file: URL on purpose
        opened = fh.read().decode()
    assert file_path(url).read_text(encoding="utf-8") == opened == "inside"


def test_failed_image_block_is_an_error_not_a_silent_drop(tmp_path):
    img = (tmp_path / "b%41.png").resolve()
    images = {img: "sections[2].blocks[1]"}
    raise_for_failed_images(["data:image/png;base64,AAAA"], images)  # the LOGO may degrade
    with pytest.raises(RenderError) as exc:
        raise_for_failed_images([img.as_uri()], images)
    assert exc.value.path == "sections[2].blocks[1]"


@pytest.mark.parametrize("kind", ["bomb-error", "bomb-warning"])
def test_decompression_bomb_image_is_a_block_error(base, sample, kind, huge_png, monkeypatch):
    if kind == "bomb-error":  # header declares 20000x20000; nothing is allocated
        (base / "trend-note.png").write_bytes(huge_png(20000, 20000))
    else:  # a real, decodable image just above a lowered pixel limit: warning, not error
        monkeypatch.setattr(PILImage, "MAX_IMAGE_PIXELS", 1000)
        PILImage.new("RGB", (40, 40), "#DDEEEE").save(base / "trend-note.png")
    with pytest.raises(RenderError) as exc:
        render_pdf(sample, resolve([], sample).style, base / "x.pdf", base)
    assert exc.value.path.startswith("sections[")
