import pytest
from hpr.blocks import RenderError, block_to_prims, fmt, section_prims
from hpr.catalog import VARIANTS
from hpr.icons import ICONS, icon_for_section
from hpr.measure import SAFETY, Measurer
from hpr.prims import (
    CardGrid,
    Chart,
    Columns,
    KeyValue,
    Paragraph,
    SubHeading,
    Table,
    TimeBars,
    Timeline,
)
from hpr.style import Layer, contrast, resolve
from hpr.theme import build_theme, on_color


@pytest.fixture
def m():
    return Measurer(font_path="/nonexistent")  # 本地与 CI 用近似测量，确定性  # noqa: RUF003


def test_theme_scales_and_density():
    std = build_theme(resolve([]).style, "pptx")
    big = build_theme(resolve([Layer("x", {"type.scale": "large"})]).style, "pptx")
    small = build_theme(resolve([Layer("x", {"type.scale": "compact"})]).style, "pptx")
    assert (std.title, std.heading, std.body, std.caption) == (24, 16, 14, 11)
    assert big.body == std.body + 2
    assert small.body == 13 and small.body >= 12
    assert std.small == std.body - 1 and small.small == small.body - 1
    pdf = build_theme(resolve([]).style, "pdf")
    assert (pdf.title, pdf.heading, pdf.body, pdf.caption) == (20, 13, 10.5, 8.5)
    airy = build_theme(resolve([Layer("x", {"layout.density": "airy"})]).style, "pptx")
    assert airy.gap_l > std.gap_l


def test_theme_colors_readable():
    t = build_theme(resolve([Layer("x", {"color.background": "浅灰"})]).style, "pptx")
    assert contrast(t.ink, t.background) >= 7
    assert contrast(t.primary, t.background) >= 4.5
    assert contrast(on_color(t.primary), t.primary) >= 4.5


def test_wrap_preserves_text_and_respects_width(m):
    text = (
        "近 30 天空腹血糖有 9 天高于 6.1，建议先稳住餐后血糖，再谈减重速度。Walk 30 minutes daily."  # noqa: RUF001
    )
    lines = m.wrap(text, 120, 14)
    assert "".join(ln.text for ln in lines) == text
    assert all(m.width(ln.text, 14) <= 120 / SAFETY + 14 for ln in lines)


def test_wrap_keeps_hard_newlines(m):
    lines = m.wrap("第一行\n第二行", 500, 14)
    assert [ln.text for ln in lines] == ["第一行", "第二行"]
    assert [ln.ends_para for ln in lines] == [True, True]


def test_unbreakable_token_is_split_by_char(m):
    token = "A" * 80
    lines = m.wrap(token, 100, 14)
    assert len(lines) > 1
    assert "".join(ln.text for ln in lines) == token


def test_split_text_round_trip(m):
    text = "甲" * 30 + "\n" + "乙" * 30
    head, tail = m.split_text(text, 100, 14, 3)
    assert m.lines(head, 100, 14) == 3
    assert (head + "\n" + tail).replace("\n", "") == text.replace("\n", "")


def test_real_font_measurement_when_available():
    import os

    path = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
    if not os.path.exists(path):
        pytest.skip("CJK font only in sandbox image")
    real = Measurer(font_path=path)
    assert real.real
    assert 13 < real.width("中", 14) < 15


def test_fmt_numbers():
    assert fmt(6.4) == "6.4"
    assert fmt(26.0) == "26.0"
    assert fmt(398) == "398"
    assert fmt("约 400") == "约 400"


def test_every_kind_and_variant_maps(sample):
    blocks = {b["kind"]: b for s in sample["sections"] for b in s["blocks"]}
    for kind, variants in VARIANTS.items():
        for v in variants:
            prims = block_to_prims(blocks[kind], v, f"x.{kind}")
            assert prims, (kind, v)


def test_profile_cards_have_tags_and_bars(sample):
    prof = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "profile")
    [grid] = block_to_prims(prof, "cards", "p")
    assert isinstance(grid, CardGrid)
    assert grid.cols == 4
    first = grid.cards[0]
    assert first.tag.text == "高于参考范围"
    assert first.tag.tone == "out"
    assert first.bar is not None and first.bar.value == 6.4


def test_profile_without_range_has_tag_no_bar():
    blk = {"kind": "profile", "items": [{"name": "腰围", "value": "92 cm", "position": "above"}]}
    [grid] = block_to_prims(blk, "cards", "p")
    card = grid.cards[0]
    assert card.bar is None
    assert card.tag.text == "高于参考范围"


def test_profile_numeric_value_without_range_has_no_bar():
    blk = {
        "kind": "profile",
        "items": [{"name": "腰围", "value": 92, "unit": "cm", "position": "above"}],
    }
    [grid] = block_to_prims(blk, "cards", "p")
    card = grid.cards[0]
    assert card.bar is None
    assert card.tag.text == "高于参考范围"


def test_profile_numeric_value_with_only_low_has_no_bar():
    blk = {"kind": "profile", "items": [{"name": "腰围", "value": 92, "unit": "cm", "ref_low": 70}]}
    [grid] = block_to_prims(blk, "cards", "p")
    assert grid.cards[0].bar is None


def test_profile_table_variant(sample):
    prof = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "profile")
    [tbl] = block_to_prims(prof, "table", "p")
    assert isinstance(tbl, Table)
    assert tbl.columns[0] == "指标"
    assert tbl.rows[0][0] == "空腹血糖"


def test_trend_line_and_bar(sample):
    trend = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "trend")
    line = block_to_prims(trend, "line", "t")
    assert isinstance(line[0], SubHeading)
    chart = line[1]
    assert isinstance(chart, Chart) and chart.kind == "line"
    assert (chart.low, chart.high) == (4.4, 6.1)
    bar = block_to_prims(trend, "bar", "t")
    assert bar[1].low is None and bar[1].high is None
    assert any(isinstance(p, Paragraph) and "目标区间" in p.text for p in bar)


def test_donut_requires_numeric_percent():
    blk = {"kind": "nutrition", "macros": [{"name": "碳水", "percent": "约四成"}]}
    with pytest.raises(RenderError) as exc:
        block_to_prims(blk, "donut", "sections[3].blocks[0]")
    assert exc.value.path == "sections[3].blocks[0]"
    assert block_to_prims(blk, "table", "x")  # 表格版式可以


def test_diet_rules_columns_labels(sample):
    rules = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "diet_rules")
    prims = block_to_prims(rules, "columns", "d")
    cols = next(p for p in prims if isinstance(p, Columns))
    assert [c.title for c in cols.columns] == ["推荐", "限制", "避免"]
    assert any(isinstance(p, Table) for p in prims)


def test_sleep_timebar_and_phases_timeline(sample):
    sleep = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "sleep")
    assert isinstance(block_to_prims(sleep, "timebar", "s")[0], TimeBars)
    phases = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "phases")
    assert isinstance(block_to_prims(phases, "timeline", "p")[0], Timeline)


def test_kv_columns(sample):
    kv = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "kv")
    assert block_to_prims(kv, "two-column", "k")[0].cols == 2
    assert block_to_prims(kv, "one-column", "k")[0].cols == 1
    assert isinstance(block_to_prims(kv, "one-column", "k")[0], KeyValue)


def test_section_prims_follow_content_order_and_style(sample):
    style = resolve([Layer("x", {"blocks.meal_plan.variant": "table"})], sample).style
    out = section_prims(sample, style)
    assert [sec["id"] for sec, _ in out] == [s["id"] for s in sample["sections"]]
    diet_items = next(items for sec, items in out if sec["id"] == "diet")
    assert any(isinstance(p, Table) and "食物与用量" in p.columns for p, _ in diet_items)
    assert all(path.startswith("sections[") for _, items in out for _, path in items)


def test_icons_are_in_grid_and_every_section_has_one(sample):
    for name, polylines in ICONS.items():
        for pl in polylines:
            assert len(pl) >= 2, name
            assert all(0 <= x <= 24 and 0 <= y <= 24 for x, y in pl), name
    assert all(icon_for_section(s) in ICONS for s in sample["sections"])


def test_phases_table_keeps_every_text_when_two_focus_items_share_an_area():
    blk = {
        "kind": "phases",
        "items": [
            {
                "label": "第1阶段",
                "focus": [
                    {"area": "饮食", "text": "控糖"},
                    {"area": "饮食", "text": "晚餐控糖饮食"},
                    {"area": "运动", "text": "快走"},
                ],
            },
            {"label": "第2阶段", "focus": [{"area": "运动", "text": "慢跑"}]},
        ],
    }
    (tbl,) = block_to_prims(blk, "table", "p")
    assert tbl.columns == ("阶段", "饮食", "运动")
    assert tbl.rows[0][1].split("\n") == ["控糖", "晚餐控糖饮食"]  # both, in caller order
    assert tbl.rows[1][1] == "—"


def test_kind_table_keeps_a_column_the_caller_filled_with_dashes():
    blk = {
        "kind": "habits",
        "items": [{"name": "饮水", "current": "—"}, {"name": "步行", "current": "—"}],
    }
    (tbl,) = block_to_prims(blk, "table", "p")
    assert tbl.columns == ("习惯", "现状")  # caller's "—" is text; our empty columns still drop
    assert [r[1] for r in tbl.rows] == ["—", "—"]


def _gate(t):
    from hpr.qa import contrast_report

    return contrast_report(t)


def test_contrast_gate_fails_on_the_pre_fix_text_colours():
    import dataclasses

    from hpr.style import INK, mix
    from hpr.theme import OUT

    t = build_theme(resolve([]).style, "pptx")
    assert _gate(t)["ok"] is True
    fg = on_color(t.primary)
    old_cover_label = mix(t.primary, fg, 0.25)  # band cover fact labels / manager title
    assert _gate(dataclasses.replace(t, on_primary_soft=old_cover_label))["ok"] is False
    assert _gate(dataclasses.replace(t, muted=mix(INK, t.background, 0.40)))["ok"] is False
    assert _gate(dataclasses.replace(t, out_text=OUT))["ok"] is False  # 高于参考范围 tag


@pytest.mark.parametrize("primary", ["#0B4F5C", "浅蓝", "#111111", "墨绿", "蓝", "橙"])
@pytest.mark.parametrize("bg", ["白", "浅紫", "米色", "浅灰", "tint"])
@pytest.mark.parametrize("fmt", ["pptx", "pdf"])
def test_every_text_role_is_readable_on_its_surfaces(primary, bg, fmt):
    style = resolve([Layer("x", {"color.primary": primary, "color.background": bg})]).style
    t = build_theme(style, fmt)
    report = _gate(t)
    assert report["ok"] is True, report
    assert {
        "muted_on_background",
        "cover_label_on_primary",
        "out_text_on_background",
        "out_text_on_pale_out",
    } <= set(report)
