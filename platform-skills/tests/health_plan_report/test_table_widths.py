"""Table column widths: every column fits its longest unbreakable token (live-fix defect 2)."""

import re

import pytest
from hpr.common import column_widths, nobreak_tokens
from hpr.measure import SAFETY, Measurer
from hpr.pdf_html import PDF_BODY_W, build_html
from hpr.ppt_layout import BODY_W, make_ctx, table_geometry
from hpr.prims import Table
from hpr.style import Layer, resolve
from hpr.theme import build_theme
from PIL import Image as PILImage

M = Measurer("/nonexistent")
FOODS = "杂粮饭（大米＋糙米）75 g、清蒸鲈鱼 100 g、清炒西兰花 200 g、橄榄油 10 g、紫菜蛋花汤 1 碗"  # noqa: RUF001
MEAL = Table(
    ("日期", "餐次", "时间", "热量", "食物与用量"),
    (
        ("周一", "早餐", "7:30", "420 kcal", FOODS),
        ("周二", "午餐", "12:00", "560 kcal", FOODS * 2),
        ("周三", "加餐（下午）", "15:30", "100 kcal", FOODS),  # noqa: RUF001
    ),
)


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("周一", ["周一"]),
        ("早餐", ["早餐"]),
        ("7:30", ["7:30"]),
        ("12:00", ["12:00"]),
        ("420 kcal", ["420 kcal"]),
        ("前 2 周", ["前", "2 周"]),
        ("每次 30 分钟", ["每次", "30 分钟"]),
        ("睡 7 小时", ["睡", "7 小时"]),
        ("7:30 早餐", ["7:30", "早餐"]),
        ("3 组动作", ["3 组", "动", "作"]),
        ("加餐（下午）", ["加餐", "（下午）"]),  # noqa: RUF001
        ("女 / 48 岁（1978-01）", ["女", "/", "48 岁", "（1978-01）"]),  # noqa: RUF001
    ],
)
def test_nobreak_tokens(text, tokens):
    assert nobreak_tokens(text) == tokens


def _fits_tokens(tbl: Table, col_w: list[float], pad: float, size: float) -> list[str]:
    bad = []
    for i, cw in enumerate(col_w):
        cells = [tbl.columns[i]] + [r[i] for r in tbl.rows]
        for c in cells:
            for tok in nobreak_tokens(c):
                if M.width(tok, size, True) * SAFETY > cw - 2 * pad + 0.01:
                    bad.append(f"{i}:{tok}")
    return bad


@pytest.mark.parametrize("scale", ["compact", "standard", "large"])
def test_ppt_table_columns_fit_their_longest_token(scale):
    style = resolve([Layer("x", {"type.scale": scale})]).style
    ctx = make_ctx({"brand": {}}, build_theme(style, "pptx"), M, __import__("pathlib").Path("."))
    geo = table_geometry(MEAL, BODY_W, ctx)
    assert abs(sum(geo.col_w) - BODY_W) < 0.5
    assert _fits_tokens(MEAL, geo.col_w, geo.pad_x, ctx.theme.small) == []
    # short cells stay on one line; the long text column gets the rest
    for i, cell in enumerate(MEAL.rows[0][:4]):
        assert M.lines(cell, geo.col_w[i] - 2 * geo.pad_x, ctx.theme.small) == 1, cell
    assert geo.col_w[4] == max(geo.col_w)


def _colgroup_pt(html: str) -> list[list[float]]:
    groups = re.findall(r"<colgroup>(.*?)</colgroup>", html)
    return [
        [float(w) * PDF_BODY_W / 100 for w in re.findall(r"width:([\d.]+)%", g)] for g in groups
    ]


@pytest.mark.parametrize("scale", ["compact", "standard", "large"])
def test_pdf_tables_have_fixed_token_fitting_colgroups(sample, tmp_path, scale):
    PILImage.new("RGB", (640, 360), "#DDEEEE").save(tmp_path / "trend-note.png")
    blk = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "meal_plan")
    blk["templates"][0]["meals"].append(
        {
            "name": "加餐（下午）",  # noqa: RUF001
            "time": "15:30",
            "foods": [{"name": FOODS, "amount": "1 份"}],
            "kcal": 100,
        }
    )
    layers = [Layer("x", {"type.scale": scale, "blocks.meal_plan.variant": "table"})]
    style = resolve(layers, sample).style
    t = build_theme(style, "pdf")
    html, _ = build_html(sample, style, t, tmp_path)
    assert "table-layout: fixed" in html
    tables = re.findall(r"<table[^>]*>(.*?)</table>", html)
    groups = _colgroup_pt(html)
    assert tables and len(groups) == len(tables)
    pad = 2.2 / 25.4 * 72
    for tb, widths in zip(tables, groups, strict=True):
        assert abs(sum(widths) - PDF_BODY_W) < 0.5
        head = re.findall(r"<th>(.*?)</th>", tb)
        rows = [
            re.findall(r"<td[^>]*>(.*?)</td>", r) for r in re.findall(r"<tr>(.*?)</tr>", tb)[1:]
        ]
        tbl = Table(tuple(head), tuple(tuple(r) for r in rows))
        assert _fits_tokens(tbl, widths, pad, t.small) == [], head


def test_column_widths_honour_tokens_when_every_column_wraps():
    long = "说明文字" * 30
    cols = ("甲", "乙", "丙")
    rows = ((long, "见 2026-09-28T07:30", long),)
    widths = column_widths(cols, rows, 300.0, lambda _i, s: M.width(s, 10) + 4)
    assert abs(sum(widths) - 300.0) < 1e-6
    assert widths[1] >= M.width("2026-09-28T07:30", 10) + 4
