"""kb_text_norm.py:部首归一、引文投影、表格序列化、关键事实抽取(B-206 11-10 尺子修正)。
全部合成小例子。"""

from __future__ import annotations

import unicodedata

import pytest
from kb_cjk_radicals import RADICAL_TO_UNIFIED  # type: ignore[import-not-found]
from kb_text_norm import (  # type: ignore[import-not-found]
    fold_key,
    has_number,
    key_facts,
    normalize_cjk,
    quote_key,
    table_views,
    unit_numbers,
)

# U+2EDD(部首补充「食」)NFKC 归一不掉;U+2F08(康熙部首「人」)NFKC 能归一。
_SUPP_FOOD = "⻝"
_KANGXI_PERSON = "⼈"


def test_supplement_radicals_survive_nfkc_but_not_normalize_cjk() -> None:
    assert unicodedata.normalize("NFKC", _SUPP_FOOD) == _SUPP_FOOD  # 这就是删不掉的原因
    assert normalize_cjk(f"饮{_SUPP_FOOD}清淡") == "饮食清淡"
    assert normalize_cjk(f"成年{_KANGXI_PERSON}") == "成年人"


def test_the_mapping_covers_both_radical_blocks_only() -> None:
    assert all(0x2E80 <= cp <= 0x2FDF for cp in RADICAL_TO_UNIFIED)
    assert any(cp < 0x2F00 for cp in RADICAL_TO_UNIFIED)
    assert any(cp >= 0x2F00 for cp in RADICAL_TO_UNIFIED)
    # 映射的目标都是统一汉字(CJK Unified Ideographs 及其扩展区)
    assert all(
        "CJK UNIFIED IDEOGRAPH" in unicodedata.name(chr(v)) for v in RADICAL_TO_UNIFIED.values()
    )


def test_fold_key_matches_a_radical_spelling_with_the_plain_one() -> None:
    assert fold_key(f"饮{_SUPP_FOOD}清淡的成年人") == fold_key("饮食清淡的成年人")


def test_quote_key_drops_punctuation_and_spacing_but_keeps_number_signs() -> None:
    assert quote_key("每天 1~2 袋,含量≥5%。") == "每天1~2袋含量≥5%"
    assert quote_key("规格:10.5 g / 袋") == "规格10.5g袋"
    assert quote_key("型号 KX-305。") == "型号kx-305"
    # 句末的半角句点、列表前的短横不是数字的一部分,不留(否则「30分钟.」对不上「30分钟。」)
    assert quote_key("间隔30分钟.") == quote_key("间隔30分钟。")
    assert quote_key("- 每天1袋") == "每天1袋"
    # 留着的符号让只差一个符号的两句不相同
    assert quote_key("≥5%") != quote_key("≤5%")


def test_table_views_pair_headers_with_cells_by_row_and_by_column() -> None:
    table = (
        "说明\n| 品名 | 规格 | 用量 |\n| --- | --- | --- |\n"
        "| 甲款 | 10g | 每天1袋 |\n| 乙款 | 5g | 每天2袋 |\n"
    )
    views = [quote_key(v) for v in table_views(table)]
    assert "品名甲款规格10g用量每天1袋" in views  # 按行:表头 + 单元格
    assert any("规格10g5g" in v for v in views)  # 按列
    assert table_views("没有表格的一段话。") == []


def test_table_views_handle_a_chunk_without_a_header_row() -> None:
    views = [quote_key(v) for v in table_views("| 甲款 | 10g |\n| 乙款 | 5g |\n")]
    assert any("甲款乙款" in v for v in views)


def test_unit_numbers_pick_numbers_with_units() -> None:
    got = unit_numbers("每次 10 g,每天1~2袋,60℃以下温水,含量5%,5岁以上,第2章")
    assert got == ["10g", "2袋", "60c", "5%", "5岁"]


def test_has_number_does_not_match_inside_a_longer_number() -> None:
    assert has_number("5岁", quote_key("15岁以上")) is False
    assert has_number("5岁", quote_key("5岁以上"))
    assert has_number("0.5g", quote_key("每份10.5g")) is False


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("一盒 15 袋,每天 1 袋", "number", "15袋"),
        ("型号是 KX-305 的那款", "code", "kx-305"),
        ("说明书写的是「不宜空腹」", "quote", "不宜空腹"),
        ("证书编号 ZL201911234", "code", "zl201911234"),
    ],
)
def test_key_facts_are_extracted_by_generic_rules(text: str, kind: str, value: str) -> None:
    assert (kind, value) in key_facts(text)


def test_a_number_with_a_unit_is_not_also_a_code() -> None:
    assert key_facts("每次10g") == [("number", "10g")]


# ---------------------------------------------------------------------------
# 审查 M1 / M2 / L3:千分位、小数切出的假型号、边界、列表编号
# ---------------------------------------------------------------------------


def test_thousands_separators_are_dropped_before_extracting_numbers() -> None:
    assert unit_numbers("每粒含1,000毫克,每盒 2\uff0c500mg") == ["1000毫克", "2500mg"]
    assert has_number("1000毫克", quote_key("含 1,000 毫克"))
    assert key_facts("1,000毫克") == [("number", "1000毫克")]


def test_a_decimal_number_does_not_yield_a_fake_code() -> None:
    assert key_facts("每片1.5mg") == [("number", "1.5mg")]
    assert key_facts("规格 1.5mg/片,型号 KX-305") == [("number", "1.5mg"), ("code", "kx-305")]


def test_number_matching_has_a_trailing_boundary() -> None:
    assert has_number("10g", quote_key("容量10gb")) is False
    assert has_number("10g", quote_key("每份10g,"))


def test_list_numbering_is_not_read_as_a_decimal_point() -> None:
    assert quote_key("1. 5克装每盒") == "5克装每盒"
    assert quote_key("说明\n2. 每天1袋") == "说明每天1袋"
    assert quote_key("规格1.5克") == "规格1.5克"
