"""Full style x variant combination matrix, plus abnormal-input tests (task 8).

Every combination must pass PPTX QA (missing text / overflow / out-of-bounds / contrast) and,
for block variants, keep every required text present in the rendered PDF HTML. Any failure here
means the renderer has a real bug: fix drawing/layout, never the test.
"""

from __future__ import annotations

import html as html_mod
import itertools
import re

import pytest
from hpr.catalog import VARIANTS
from hpr.content import required_texts
from hpr.measure import Measurer
from hpr.pdf_html import build_html
from hpr.ppt_draw import render_pptx
from hpr.ppt_layout import LayoutError
from hpr.qa import missing_texts, qa_pptx
from hpr.style import Layer, resolve
from hpr.theme import build_theme
from PIL import Image as PILImage

M = Measurer("/nonexistent")
COLORS = ["#0B4F5C", "浅蓝", "#111111", "墨绿"]
SCALES = ["compact", "standard", "large"]
DENSITY = ["compact", "standard", "airy"]
COVERS = ["band", "split", "minimal"]


@pytest.fixture
def base(tmp_path):
    PILImage.new("RGB", (640, 360), "#DDEEEE").save(tmp_path / "trend-note.png")
    return tmp_path


def _check(content, base, values, name="m.pptx"):
    style = resolve([Layer("x", values)], content).style
    out = base / name
    if out.exists():
        out.unlink()
    render_pptx(content, style, out, base, M)
    qa = qa_pptx(out, required_texts(content), build_theme(style, "pptx"), M)
    assert qa["status"] == "passed", (values, qa)


@pytest.mark.parametrize(
    ("color", "scale", "density", "cover"), list(itertools.product(COLORS, SCALES, DENSITY, COVERS))
)
def test_style_matrix_pptx(sample, base, color, scale, density, cover):
    _check(
        sample,
        base,
        {
            "color.primary": color,
            "type.scale": scale,
            "layout.density": density,
            "cover.variant": cover,
            "toc": "on",
        },
    )


@pytest.mark.parametrize(("kind", "variant"), [(k, v) for k, vs in VARIANTS.items() for v in vs])
def test_every_block_variant_pptx(sample, base, kind, variant):
    _check(sample, base, {f"blocks.{kind}.variant": variant})


@pytest.mark.parametrize(("kind", "variant"), [(k, v) for k, vs in VARIANTS.items() for v in vs])
def test_every_block_variant_html_keeps_text(sample, base, kind, variant):
    style = resolve([Layer("x", {f"blocks.{kind}.variant": variant})], sample).style
    html, _ = build_html(sample, style, build_theme(style, "pdf"), base)

    visible = html_mod.unescape(
        re.sub(r"<style>.*?</style>|<svg.*?</svg>|<[^>]+>", "", html, flags=re.S)
    )
    assert missing_texts(required_texts(sample), visible) == []


def test_hundred_row_table_spans_pages(sample, base):
    tbl = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "table")
    tbl["rows"] = [[f"第 {i} 天", "—", "—", "—"] for i in range(1, 101)]
    _check(sample, base, {})


def test_long_texts_everywhere(sample, base):
    long = "这是一段用于压力测试的较长说明文字，包含数字 123 和英文 words。" * 6  # noqa: RUF001
    for sec in sample["sections"]:
        for b in sec["blocks"]:
            if b["kind"] == "paragraph":
                b["text"] = long * 5
            if b["kind"] == "issues":
                for it in b["items"]:
                    it["evidence"] = long
    _check(sample, base, {"type.scale": "large"})


def test_one_section_one_paragraph(base):
    content = {
        "schema_version": "1",
        "title": "李先生健康管理方案",
        "generated_at": "2026-09-28",
        "client": {"name": "李先生"},
        "sections": [
            {"id": "s", "title": "说明", "blocks": [{"kind": "paragraph", "text": "一句话。"}]}
        ],
    }
    _check(content, base, {})


def test_bad_logo_bytes_warns(sample, base):
    (base / "bad.png").write_bytes(b"not a png")
    sample["brand"]["logo_path"] = "bad.png"
    style = resolve([], sample).style
    warnings = render_pptx(sample, style, base / "w.pptx", base, M)
    assert any("LOGO" in w for w in warnings)


def test_client_band_taller_than_a_page_is_an_error_with_its_path(sample, base):
    sample["client"]["facts"] = [{"label": f"项{i}", "value": "值" * 40} for i in range(60)]
    with pytest.raises(LayoutError, match=r"client\.facts"):
        render_pptx(sample, resolve([], sample).style, base / "f.pptx", base, M)


def test_unknown_variant_name_is_reported_not_fatal(sample, base):
    res = resolve([Layer("x", {"blocks.trend.variant": "雷达图"})], sample)
    entry = next(e for e in res.report if e["key"] == "blocks.trend.variant")
    assert entry["status"] == "out_of_range"
    _check(sample, base, {"blocks.trend.variant": "雷达图"})
