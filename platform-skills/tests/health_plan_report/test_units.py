"""A field whose unit the layout adds (or that has its own ``unit``) must not carry the unit
again in its text: 「约 400 kcal」 rendered as 「约 400 kcal kcal」 in a live replay."""

import pytest
from hpr.content import validate_content


def _block(sample: dict, kind: str) -> dict:
    return next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == kind)


def _unit_errors(sample: dict) -> list[str]:
    return [str(e) for e in validate_content(sample) if "单位" in e.message]


@pytest.mark.parametrize(
    ("kind", "setter", "suffix"),
    [
        ("meal_plan", lambda b: b["templates"][0]["meals"][0].update(kcal="约 400 kcal"), "kcal"),
        ("meal_plan", lambda b: b["templates"][0]["meals"][0].update(kcal="400千卡"), "kcal"),
        ("nutrition", lambda b: b.update(energy_kcal="1650 KCAL"), "energy_kcal"),
        ("nutrition", lambda b: b.update(energy_kcal="约1600大卡"), "energy_kcal"),
        ("nutrition", lambda b: b["macros"][0].update(grams="185g"), "grams"),
        ("nutrition", lambda b: b["macros"][0].update(grams="约 185 克"), "grams"),
        ("meal_plan", lambda b: b["templates"][0]["meals"][0].update(kcal="约400卡"), "kcal"),
        ("meal_plan", lambda b: b["templates"][0]["meals"][0].update(kcal="400 Cal"), "kcal"),
        ("nutrition", lambda b: b["macros"][0].update(grams="≥1.2 g/kg"), "grams"),
        ("nutrition", lambda b: b["macros"][0].update(grams="500 mg"), "grams"),
        ("nutrition", lambda b: b["macros"][0].update(grams="1.5 公斤"), "grams"),
        ("nutrition", lambda b: b["meals"][0].update(percent="30%"), "percent"),
        ("nutrition", lambda b: b["macros"][0].update(percent="45%"), "percent"),
        ("nutrition", lambda b: b["macros"][0].update(percent="45％"), "percent"),  # noqa: RUF001
    ],
)
def test_implicit_unit_repeated_is_rejected(sample, kind, setter, suffix):
    setter(_block(sample, kind))
    errs = _unit_errors(sample)
    assert len(errs) == 1 and errs[0].split(":")[0].endswith(suffix)


@pytest.mark.parametrize(
    ("kind", "field"),
    [("profile", "value"), ("goals", "target"), ("goals", "current")],
)
def test_explicit_unit_repeated_is_rejected(sample, kind, field):
    item = _block(sample, kind)["items"][0]
    item["unit"] = "mmol/L"
    item[field] = "6.4 MMOL/L"
    errs = _unit_errors(sample)
    assert len(errs) == 1 and errs[0].split(":")[0].endswith(f"items[0].{field}")


@pytest.mark.parametrize("text", ["约 400", "380–420", "400 左右", "不限"])  # noqa: RUF001
def test_plain_value_text_passes(sample, text):
    _block(sample, "meal_plan")["templates"][0]["meals"][0]["kcal"] = text
    nut = _block(sample, "nutrition")
    nut["energy_kcal"] = text
    nut["macros"][0]["grams"] = text
    assert _unit_errors(sample) == []


def test_unit_text_without_unit_field_passes(sample):
    item = _block(sample, "goals")["items"][0]
    item.pop("unit", None)
    item["target"] = "75.5 kg"  # no unit field: the value carries its own unit
    assert _unit_errors(sample) == []


def test_word_containing_g_is_not_a_gram_unit(sample):
    _block(sample, "nutrition")["macros"][0]["grams"] = "按 guideline 执行"
    assert _unit_errors(sample) == []


def test_message_tells_what_to_write(sample):
    _block(sample, "meal_plan")["templates"][0]["meals"][0]["kcal"] = "约 400 kcal"
    (err,) = [e for e in validate_content(sample) if "单位" in e.message]
    assert "约 400" in err.message and "kcal" in err.message


def test_latin_unit_matches_whole_words_only(sample):
    item = _block(sample, "profile")["items"][0]
    item["unit"] = "L"
    item["value"] = "见 LDL 说明"
    assert _unit_errors(sample) == []
    item["value"] = "1.2 L"
    assert len(_unit_errors(sample)) == 1


def test_no_misleading_example_when_the_bare_value_is_not_a_number(sample):
    _block(sample, "nutrition")["macros"][0]["grams"] = "≥1.2 g/kg"
    (err,) = [e for e in validate_content(sample) if "单位" in e.message]
    assert "例如" not in err.message
