import colorsys
import json
import subprocess
import sys
from pathlib import Path

import pytest
from hpr.style import (
    COLOR_NAMES,
    NOT_APPLIED,
    Layer,
    contrast,
    flatten,
    load_layer,
    luminance,
    resolve,
    variant_for,
)

SCRIPT = Path(__file__).resolve().parents[2] / "health-plan-report" / "scripts" / "resolve_style.py"


def _entry(res, key, layer=None):
    return next(e for e in res.report if e["key"] == key and (layer is None or e["layer"] == layer))


def test_defaults_are_direction_a():
    s = resolve([]).style
    assert s["color.primary"] == "#0B4F5C"
    assert s["color.accent"] == "#E8A33D"
    assert s["color.background"] == "#FFFFFF"
    assert s["type.scale"] == "standard"
    assert s["cover.variant"] == "band"
    assert s["output.formats"] == "pptx"


def test_higher_layer_wins_and_lower_is_overridden():
    res = resolve(
        [Layer("本次", {"color.primary": "墨绿"}), Layer("个人", {"color.primary": "#1E3A8A"})]
    )
    assert res.style["color.primary"] == "#1F4D3A"
    assert _entry(res, "color.primary", "本次")["status"] == "applied"
    assert _entry(res, "color.primary", "个人")["status"] == "overridden"


def test_chinese_synonyms():
    res = resolve(
        [Layer("x", {"brand.logo_position": "右上角", "footer.align": "居中", "toc": "不要"})]
    )
    assert res.style["brand.logo_position"] == "top-right"
    assert res.style["footer.align"] == "center"
    assert res.style["toc"] == "off"


def test_color_suffix_se_is_tolerated():
    assert resolve([Layer("x", {"color.primary": "深蓝色"})]).style["color.primary"] == "#1E3A8A"


def test_out_of_range_does_not_block_lower_layer():
    res = resolve([Layer("本次", {"type.scale": "巨大"}), Layer("个人", {"type.scale": "large"})])
    assert res.style["type.scale"] == "large"
    assert _entry(res, "type.scale", "本次")["status"] == "out_of_range"
    assert _entry(res, "type.scale", "个人")["status"] == "applied"


def test_unknown_key_reported():
    res = resolve([Layer("x", {"background.image": "sea.png"})])
    assert _entry(res, "background.image")["status"] == "out_of_range"


def test_brand_keys_are_locked():
    res = resolve([Layer("x", {"brand.org_name": "别的机构", "brand.disclaimer": "无"})])
    assert _entry(res, "brand.org_name")["status"] == "brand_locked"
    assert "brand.org_name" not in res.style


def test_dark_background_is_lightened():
    res = resolve([Layer("x", {"color.background": "#123456"})])
    bg = res.style["color.background"]
    assert luminance(bg) >= 0.80
    assert _entry(res, "color.background")["status"] == "adjusted"


def test_background_keywords():
    assert (
        resolve([Layer("x", {"color.background": "浅灰"})]).style["color.background"] == "#F5F7F8"
    )
    tint = resolve([Layer("x", {"color.background": "tint"})]).style["color.background"]
    assert luminance(tint) >= 0.80
    assert tint != "#FFFFFF"


def test_tint_background_report_value_is_final_hex():
    res = resolve([Layer("x", {"color.background": "tint"})])
    entry = _entry(res, "color.background")
    assert entry["value"] == res.style["color.background"]
    assert entry["value"].startswith("#")
    assert entry["status"] == "applied"


def test_low_contrast_primary_is_darkened():
    res = resolve([Layer("x", {"color.primary": "浅蓝"})])
    p = res.style["color.primary"]
    assert contrast(p, res.style["color.background"]) >= 4.5
    assert _entry(res, "color.primary")["status"] == "adjusted"


def _hls(hex_: str) -> tuple[float, float, float]:
    return colorsys.rgb_to_hls(*(int(hex_[i : i + 2], 16) / 255 for i in (1, 3, 5)))


@pytest.mark.parametrize("name", ["浅蓝", "浅绿", "浅紫", "米色", "橙"])
def test_darkened_primary_keeps_its_hue_instead_of_turning_grey(name):
    src = COLOR_NAMES[name]
    p = resolve([Layer("x", {"color.primary": name})]).style["color.primary"]
    assert contrast(p, "#FFFFFF") >= 4.5
    (h0, _, s0), (h1, _, s1) = _hls(src), _hls(p)
    assert min(abs(h1 - h0), 1 - abs(h1 - h0)) * 360 <= 5
    assert s1 >= min(0.5, s0 - 0.05)  # mixing toward black left 浅蓝 at S≈0.06 (grey)


def test_light_blue_darkens_to_the_nearest_readable_blue():
    res = resolve([Layer("x", {"color.primary": "浅蓝"})])
    assert res.style["color.primary"] == "#1E78D1"
    assert "#1E78D1" in _entry(res, "color.primary")["note"]


def test_relative_words_move_one_step_from_lower_layer():
    res = resolve(
        [Layer("本次", {"type.scale": "大一点"}), Layer("个人", {"type.scale": "compact"})]
    )
    assert res.style["type.scale"] == "standard"
    res2 = resolve([Layer("本次", {"layout.density": "紧凑一点"})])
    assert res2.style["layout.density"] == "compact"
    res3 = resolve(
        [Layer("本次", {"type.scale": "大一点"}), Layer("个人", {"type.scale": "large"})]
    )
    assert res3.style["type.scale"] == "large"
    assert _entry(res3, "type.scale", "本次")["status"] == "out_of_range"


def test_nested_layer_is_flattened(tmp_path):
    p = tmp_path / "l.json"
    p.write_text(
        json.dumps({"color": {"primary": "深蓝"}, "type.scale": "large"}), encoding="utf-8"
    )
    layer = load_layer(p)
    assert layer.values == {"color.primary": "深蓝", "type.scale": "large"}
    assert flatten({"a": {"b": {"c": 1}}}) == {"a.b.c": 1}


def test_non_object_layer_rejected(tmp_path):
    p = tmp_path / "l.json"
    p.write_text("[1]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON 对象"):
        load_layer(p)
    p.write_text("{oops", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON"):
        load_layer(p)


def test_variant_keys_by_kind_section_and_block(sample):
    layers = [
        Layer(
            "x",
            {
                "blocks.meal_plan.variant": "表格",
                "sections.profile.variant": "table",
                "sections.diet.2.variant": "cards",
            },
        )
    ]
    res = resolve(layers, sample)
    s = res.style
    assert variant_for(s, "meal_plan", "diet", None, 2) == "cards"
    assert variant_for(s, "meal_plan", "other", None, 1) == "table"
    assert variant_for(s, "profile", "profile", None, 1) == "table"
    assert (
        variant_for(s, "issues", "profile", None, 2) == "cards"
    )  # issues 支持 cards 但不支持 table
    assert variant_for(s, "trend", "trend", None, 1) == "line"


def test_variant_targets_validated_against_content(sample):
    res = resolve(
        [Layer("x", {"sections.nope.variant": "table", "blocks.trend.variant": "table"})], sample
    )
    assert _entry(res, "sections.nope.variant")["status"] == "out_of_range"
    assert _entry(res, "blocks.trend.variant")["status"] == "out_of_range"


def test_section_variant_unsupported_by_every_block_is_out_of_range(sample):
    res = resolve([Layer("x", {"sections.trend.variant": "donut"})], sample)
    assert _entry(res, "sections.trend.variant")["status"] == "out_of_range"


def test_not_applied_statuses():
    assert set(NOT_APPLIED) == {"adjusted", "out_of_range", "brand_locked"}


def test_cli_resolve_and_merge(tmp_path):
    layer = tmp_path / "a.json"
    layer.write_text(json.dumps({"color.primary": "深蓝"}), encoding="utf-8")
    out = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), "--style", str(layer)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["style"]["color.primary"] == "#1E3A8A"

    personal = tmp_path / "report-style" / "personal.json"
    ok = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(SCRIPT),
            "--merge-into",
            str(personal),
            "--set",
            "type.scale=大号",
            "--set",
            "footer.page_number=false",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0, ok.stderr
    assert json.loads(personal.read_text(encoding="utf-8")) == {
        "footer.page_number": False,
        "type.scale": "大号",
    }

    before = personal.read_bytes()
    bad = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), "--merge-into", str(personal), "--set", "type.scale=巨大"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 1
    assert personal.read_bytes() == before

    unset = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), "--merge-into", str(personal), "--unset", "type.scale"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert unset.returncode == 0
    assert json.loads(personal.read_text(encoding="utf-8")) == {"footer.page_number": False}
