"""kb_hard_sets.py:删答案句造难负例 + 近名干扰题(B-206 11-09)。合成小语料,全部离线。"""

from __future__ import annotations

import pickle
from pathlib import Path

import pytest
import yaml
from kb_dataset import KbCase, KbExpected  # type: ignore[import-not-found]
from kb_hard_sets import (  # type: ignore[import-not-found]
    AnchorDeletingParser,
    delete_anchor_units,
    entity_names,
    hard_negative_cases,
    load_hard_negative_spec,
    near_misses,
    pick_distractors,
    pick_hard_negatives,
    residue,
    split_units,
)

_DOC_A = (
    "# 甲款净水器说明书(合成)\n\n"
    "甲款净水器适合家庭使用。滤芯建议每 6 个月更换一次。更换时请先关闭进水阀。\n\n"
    "| 项目 | 数值 |\n| --- | --- |\n| 额定流量 | 每分钟 1.8 升 |\n| 保修 | 整机 2 年 |\n"
)
_DOC_B = "# 乙款净水器说明书(合成)\n\n乙款净水器适合办公室使用。滤芯建议每 9 个月更换一次。\n"


def _case(case_id: str, query: str, document: str, anchor: str, match: str = "any") -> KbCase:
    return KbCase(
        id=case_id,
        query=query,
        source="faq_xlsx",
        tags=(),
        expected=(KbExpected(document=document, anchor=anchor, page=None),),
        answer=None,
        match=match,
    )


# ---------------------------------------------------------------------------
# 删句
# ---------------------------------------------------------------------------


def test_units_rebuild_the_text_exactly() -> None:
    assert "".join(split_units(_DOC_A)) == _DOC_A


def test_deleting_an_anchor_removes_only_its_sentence_and_keeps_the_context() -> None:
    text, dropped = delete_anchor_units(_DOC_A, ["滤芯建议每6个月更换一次"])
    assert dropped == 1
    assert "6 个月" not in text
    # 相关上下文还在:同段的前后句、文档其余部分
    assert "甲款净水器适合家庭使用。" in text and "更换时请先关闭进水阀。" in text
    assert "额定流量" in text


def test_a_table_row_is_one_unit() -> None:
    text, dropped = delete_anchor_units(_DOC_A, ["额定流量每分钟1.8升"])
    assert dropped == 1
    assert "1.8" not in text and "| 保修 | 整机 2 年 |" in text


def test_an_anchor_across_two_sentences_drops_just_those_two() -> None:
    text, dropped = delete_anchor_units(_DOC_A, ["更换一次。更换时请先关闭"])
    assert dropped == 2
    assert "甲款净水器适合家庭使用。" in text
    assert residue(text, ["更换一次。更换时请先关闭"]) == []


def test_every_occurrence_is_removed_and_residue_reports_what_is_left() -> None:
    doubled = _DOC_A + "\n再说一遍:滤芯建议每 6 个月更换一次。\n"
    text, dropped = delete_anchor_units(doubled, ["滤芯建议每6个月更换一次"])
    assert dropped == 2
    assert residue(text, ["滤芯建议每6个月更换一次", "甲款净水器适合家庭使用"]) == [
        "甲款净水器适合家庭使用"
    ]


def test_a_missing_anchor_changes_nothing() -> None:
    assert delete_anchor_units(_DOC_A, ["说明书里没有的一句话"]) == (_DOC_A, 0)


def test_the_parser_wrapper_is_picklable_and_deletes_after_parsing() -> None:
    parser = AnchorDeletingParser(anchors=("滤芯建议每6个月更换一次",))
    clone = pickle.loads(pickle.dumps(parser))  # noqa: S301 - our own object, built here
    out = clone("a.md", _DOC_A.encode("utf-8"))
    assert "6 个月" not in out and "甲款净水器适合家庭使用" in out


# ---------------------------------------------------------------------------
# 选难负例
# ---------------------------------------------------------------------------

_TEXTS = {"a.md": _DOC_A, "b.md": _DOC_B}


def test_pick_hard_negatives_keeps_every_control_anchor_and_is_deterministic() -> None:
    control = [_case("c1", "甲款流量多少", "a.md", "额定流量每分钟1.8升")]
    candidates = [
        _case("h1", "甲款滤芯多久换", "a.md", "滤芯建议每6个月更换一次"),
        # 删它会连带删掉对照题的锚所在的表格行 → 不能选
        _case("h2", "甲款流量单位", "a.md", "每分钟1.8升"),
        # 「找全」题删一处删不干净,不选
        _case("h3", "哪些款有滤芯", "a.md", "滤芯建议每6个月", match="all"),
        _case("h4", "乙款滤芯多久换", "b.md", "滤芯建议每9个月更换一次"),
    ]
    picked, skipped = pick_hard_negatives(candidates, control, _TEXTS, n=5, seed=1)
    assert sorted(p.case.id for p in picked) == ["h1", "h4"]
    assert {p.case.id: p.documents for p in picked} == {"h1": ("a.md",), "h4": ("b.md",)}
    assert skipped == {"touches_control": 1, "match_all": 1}
    again, _ = pick_hard_negatives(candidates, control, _TEXTS, n=5, seed=1)
    assert [p.case.id for p in again] == [p.case.id for p in picked]
    one, _ = pick_hard_negatives(candidates, control, _TEXTS, n=1, seed=1)
    assert len(one) == 1


def test_pick_hard_negatives_skips_anchors_it_cannot_find() -> None:
    picked, skipped = pick_hard_negatives(
        [_case("h9", "丙款", "c.md", "资料里根本没有的一句话")], [], _TEXTS, n=3, seed=1
    )
    assert picked == [] and skipped == {"anchor_not_found": 1}


def test_spec_round_trip_gives_no_answer_cases_with_a_judge_note(tmp_path: Path) -> None:
    picked, _ = pick_hard_negatives(
        [_case("h1", "甲款滤芯多久换", "a.md", "滤芯建议每6个月更换一次")], [], _TEXTS, n=1, seed=1
    )
    path = tmp_path / "hn.yaml"
    path.write_text(
        yaml.safe_dump(
            {"seed": 1, "cases": [p.to_spec("main") for p in picked]}, allow_unicode=True
        ),
        encoding="utf-8",
    )
    spec = load_hard_negative_spec(path)
    assert spec.anchors == ("滤芯建议每6个月更换一次",)
    [(label, case)] = hard_negative_cases(spec)
    assert label == "hard_negative"
    assert case.id == "hn-h1" and case.query == "甲款滤芯多久换"
    assert case.answer_label == "no_answer" and case.expected == ()
    assert "滤芯建议每6个月更换一次" in spec.notes["hn-h1"]


def test_spec_with_a_bad_entry_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "hn.yaml"
    path.write_text(yaml.safe_dump({"cases": [{"id": "x"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="hard-negative spec"):
        load_hard_negative_spec(path)


# ---------------------------------------------------------------------------
# 近名干扰
# ---------------------------------------------------------------------------


def test_entity_names_come_from_document_names() -> None:
    names = entity_names(["产品说明书:甲一.docx", "规格/KX-305 参数.md", "2026 价目表.xlsx"])
    assert {"甲一", "KX-305", "产品说明书", "参数", "价目表"} <= names
    assert "2026" not in names


@pytest.mark.parametrize(
    ("name", "others", "expected"),
    [
        ("益休", {"益舒", "益家人", "益休"}, ["益舒"]),
        ("KX-305", {"KX-217", "KX-30", "KY-305"}, ["KX-217", "KY-305"]),
        ("甲一", {"甲一款", "乙二"}, []),  # 互相包含 = 同一个对象的长短名
    ],
)
def test_near_misses(name: str, others: set[str], expected: list[str]) -> None:
    assert near_misses(name, others) == expected


def test_pick_distractors_needs_the_entity_in_the_query_and_its_own_document() -> None:
    files = ["说明书:益休.docx", "说明书:益舒.docx", "说明书:益家人.docx"]
    cases = [
        _case("d1", "益休每天吃几袋?", "说明书:益休.docx", "每日两袋温水冲服"),
        _case("d2", "一天吃几袋?", "说明书:益休.docx", "每日两袋温水冲服"),  # 问句没点名
        _case("d3", "益家人怎么保存?", "说明书:益家人.docx", "常温避光保存即可"),  # 没有近名
    ]
    picked = pick_distractors(cases, files, exclude_ids=set(), n=5, seed=1)
    assert [(p.case.id, p.entity, p.near) for p in picked] == [("d1", "益休", ("益舒",))]
    assert "「益舒」" in picked[0].note and "「益休」" in picked[0].note
    assert pick_distractors(cases, files, exclude_ids={"d1"}, n=5, seed=1) == []
