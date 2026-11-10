"""kb_text_norm.py:部首归一、引文投影、表格序列化、关键事实抽取(B-206 11-10 尺子修正)。
全部合成小例子。"""

from __future__ import annotations

import unicodedata

import pytest
from kb_cjk_radicals import RADICAL_TO_UNIFIED  # type: ignore[import-not-found]
from kb_text_norm import (  # type: ignore[import-not-found]
    fact_positions,
    fold_key,
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


def test_a_number_is_not_found_inside_a_longer_number() -> None:
    assert ("number", "5岁") not in key_facts("15岁以上")
    assert ("number", "5岁") in key_facts("5岁以上")
    assert ("number", "0.5g") not in key_facts("每份10.5g")
    # 定位窗口只看前边界
    assert fact_positions("number", "5岁", quote_key("15岁以上")) == []
    assert fact_positions("number", "5g", quote_key("5g\u00d710袋")) == [0]


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("一盒 15 袋,每天 1 袋", "number", "15袋"),
        ("型号是 KX-305 的那款", "code", "kx-305"),
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
    assert unit_numbers("每粒含1,000毫克,每盒 2,500mg") == ["1000毫克", "2500mg"]
    assert key_facts("含 1,000 毫克") == [("number", "1000毫克")]
    assert key_facts("1,000毫克") == [("number", "1000毫克")]


def test_a_decimal_number_does_not_yield_a_fake_code() -> None:
    assert key_facts("每片1.5mg") == [("number", "1.5mg")]
    assert key_facts("规格 1.5mg/片,型号 KX-305") == [("number", "1.5mg"), ("code", "kx-305")]


def test_numbers_are_read_off_the_text_before_separators_are_dropped() -> None:
    # 审查第二轮 HIGH-1:在原文上抽,分隔符还在
    assert unit_numbers("每袋5g\u00d710袋") == ["5g", "10袋"]
    assert unit_numbers("每日1次1袋") == ["1次", "1袋"]
    assert unit_numbers("钙300mg,VD 5\u03bcg") == ["300mg", "5\u03bcg"]
    assert key_facts("型号KX-305,2盒") == [("number", "2盒"), ("code", "kx-305")]
    assert unit_numbers("容量10gb") == []


def test_list_numbering_is_not_read_as_a_decimal_point() -> None:
    assert quote_key("1. 5克装每盒") == "5克装每盒"
    assert quote_key("说明\n2. 每天1袋") == "说明每天1袋"
    assert quote_key("规格1.5克") == "规格1.5克"


def test_quoted_phrases_are_not_key_facts() -> None:
    # 审查第二轮 MEDIUM-1:引号是评委转述时加的,不是回答里的事实
    assert key_facts("说明书写的是「不宜空腹」") == []


def test_only_well_formed_half_width_thousands_are_merged() -> None:
    assert unit_numbers("共1,000,000袋") == ["1000000袋"]
    assert unit_numbers("每瓶1,000毫升") == ["1000毫升"]
    # 全角逗号是列举,不是千分位
    assert unit_numbers("规格100\uff0c200\uff0c500毫升") == ["500毫升"]
    assert unit_numbers("12,34克") == ["34克"]


def test_list_numbers_after_the_ideographic_comma_need_no_space() -> None:
    assert quote_key("用法\n2、每天1袋") == "用法每天1袋"
    assert unit_numbers("1、5岁以下儿童慎用") == ["5岁"]


def test_an_ellipsis_before_a_number_does_not_hide_it() -> None:
    assert unit_numbers("甲片每片……1.5mg") == ["1.5mg"]
    assert unit_numbers("每天...2袋") == ["2袋"]
