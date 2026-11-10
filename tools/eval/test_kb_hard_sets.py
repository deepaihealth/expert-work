"""kb_hard_sets.py:删答案句造难负例 + 近名干扰题(B-206 11-09)。合成小语料,全部离线。"""

from __future__ import annotations

import os
import pickle
from pathlib import Path

import pytest
import yaml
from kb_dataset import KbCase, KbExpected  # type: ignore[import-not-found]
from kb_hard_sets import (  # type: ignore[import-not-found]
    FINGERPRINT_DISTS,
    FINGERPRINT_MODULES,
    AnchorDeletingParser,
    ControlAnchorDeletedError,
    _parse_fresh,
    corpus_texts,
    delete_anchor_units,
    entity_names,
    hard_negative_cases,
    load_hard_negative_spec,
    near_misses,
    parser_fingerprint,
    pick_distractors,
    pick_hard_negatives,
    residue,
    spec_document,
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
    picked, skipped, _flagged = pick_hard_negatives(candidates, control, _TEXTS, n=5, seed=1)
    assert sorted(p.case.id for p in picked) == ["h1", "h4"]
    assert {p.case.id: p.documents for p in picked} == {"h1": ("a.md",), "h4": ("b.md",)}
    assert skipped == {"touches_control": 1, "match_all": 1}
    again, _, _ = pick_hard_negatives(candidates, control, _TEXTS, n=5, seed=1)
    assert [p.case.id for p in again] == [p.case.id for p in picked]
    one, _, _ = pick_hard_negatives(candidates, control, _TEXTS, n=1, seed=1)
    assert len(one) == 1


def test_pick_hard_negatives_skips_anchors_it_cannot_find() -> None:
    picked, skipped, _flagged = pick_hard_negatives(
        [_case("h9", "丙款", "c.md", "资料里根本没有的一句话")], [], _TEXTS, n=3, seed=1
    )
    assert picked == [] and skipped == {"anchor_not_found": 1}


def test_spec_round_trip_gives_no_answer_cases_with_a_judge_note(tmp_path: Path) -> None:
    picked, _, _ = pick_hard_negatives(
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
    assert spec.deleted == {"hn-h1": ("滤芯建议每6个月更换一次",)}


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
        ("青松", {"青柏", "白桦林", "青松"}, ["青柏"]),
        ("KX-305", {"KX-217", "KX-30", "KY-305"}, ["KX-217", "KY-305"]),
        ("甲一", {"甲一款", "乙二"}, []),  # 互相包含 = 同一个对象的长短名
    ],
)
def test_near_misses(name: str, others: set[str], expected: list[str]) -> None:
    assert near_misses(name, others) == expected


def test_pick_distractors_needs_the_entity_in_the_query_and_its_own_document() -> None:
    files = ["说明书:青松.docx", "说明书:青柏.docx", "说明书:白桦林.docx"]
    cases = [
        _case("d1", "青松每天用几次?", "说明书:青松.docx", "每日一次随餐服用"),
        _case("d2", "一天用几次?", "说明书:青松.docx", "每日一次随餐服用"),  # 问句没点名
        _case("d3", "白桦林怎么保存?", "说明书:白桦林.docx", "放在阴凉干燥处即可"),  # 没有近名
    ]
    picked = pick_distractors(cases, files, exclude_ids=set(), n=5, seed=1)
    assert [(p.case.id, p.entity, p.near) for p in picked] == [("d1", "青松", ("青柏",))]
    assert "「青柏」" in picked[0].note and "「青松」" in picked[0].note
    # 11-10:安到本题对象上的近名资料按「张冠李戴」记(整题编造),不再记 unsupported
    assert "misattributed" in picked[0].note and "unsupported" not in picked[0].note
    assert pick_distractors(cases, files, exclude_ids={"d1"}, n=5, seed=1) == []


# ---------------------------------------------------------------------------
# 11-09 评审:对照题的锚按「累计删句前后还在不在」核,不是按子串碰撞
# ---------------------------------------------------------------------------

_SHOP = "# 价目(合成)\n\n甲产品说明。乙产品售价一百元。丙产品重五斤。丁产品是蓝色的。\n"
_CONTROL = [_case("c1", "丙产品", "shop.md", "售价一百元。丙产品重五")]


@pytest.mark.parametrize(
    "anchor",
    [
        "乙产品售价",  # 只删掉对照锚的前半句:旧的子串核对看不出
        "一百元。丙产品",  # 跨两句整段删掉:旧的核对用换行拼句也看不出
    ],
)
def test_a_deletion_that_breaks_a_control_anchor_is_skipped(anchor: str) -> None:
    picked, skipped, _flagged = pick_hard_negatives(
        [_case("h1", "乙产品多少钱", "shop.md", anchor)],
        _CONTROL,
        {"shop.md": _SHOP},
        n=3,
        seed=1,
    )
    assert picked == [] and skipped == {"touches_control": 1}


def test_control_anchors_are_checked_against_the_cumulative_deletion() -> None:
    # 对照锚在文中出现两次;两道候选各删一处,单看都还剩一处,合起来就一处不剩
    text = "# 合成\n\n说明一:戊产品保修两年。说明二:戊产品保修两年。\n"
    control = [_case("c1", "戊", "w.md", "戊产品保修两年")]
    candidates = [
        _case("h1", "说明一", "w.md", "说明一戊产品保修"),
        _case("h2", "说明二", "w.md", "说明二戊产品保修"),
    ]
    picked, skipped, _flagged = pick_hard_negatives(
        candidates, control, {"w.md": text}, n=5, seed=1
    )
    assert len(picked) == 1 and skipped == {"touches_control": 1}


def test_an_anchor_that_normalises_to_nothing_deletes_nothing() -> None:
    picked, skipped, _flagged = pick_hard_negatives(
        [_case("h1", "?", "a.md", "——————————")], [], _TEXTS, n=3, seed=1
    )
    assert picked == [] and skipped == {"anchor_not_found": 1}


def test_the_parser_refuses_to_delete_a_guarded_control_anchor() -> None:
    parser = AnchorDeletingParser(anchors=("乙产品售价",), guard=("售价一百元。丙产品重五",))
    with pytest.raises(ControlAnchorDeletedError, match="售价一百元"):
        parser("shop.md", _SHOP.encode("utf-8"))
    # 护住的锚原本就不在这份文档里:照常删
    assert "乙产品" not in AnchorDeletingParser(anchors=("乙产品售价",), guard=("别的锚文本",))(
        "shop.md", _SHOP.encode("utf-8")
    )


def test_the_builder_reads_the_same_documents_the_eval_ingests(tmp_path: Path) -> None:
    """11-09 评审:旧版用 kb_build_dataset.parse_corpus,同名文件只留一份、超长 PDF 跳过,
    而评测建库两者都入 —— 选题时没看到的文档里可能正好有对照题的答案。"""
    import csv

    from kb_corpus_inventory import MANIFEST_FIELDS  # type: ignore[import-not-found]

    root = tmp_path / "corpus"
    for sub, text in (("a", "# 甲\n\n第一份。\n"), ("b", "# 乙\n\n第二份,同名。\n")):
        (root / sub).mkdir(parents=True)
        (root / sub / "x.md").write_text(text, encoding="utf-8")
    (root / "a" / "big.md").write_text("# 长文\n\n页数很多。\n", encoding="utf-8")
    (root / "a" / "copy.md").write_text("# 甲\n\n第一份。\n", encoding="utf-8")
    manifest = tmp_path / "manifest.csv"
    rows = [
        ("a/x.md", "", "0"),
        ("b/x.md", "", "0"),
        ("a/big.md", "", "999"),  # parse_corpus 按页数跳过的那种
        ("a/copy.md", "g1", "0"),
        ("a/x2.md", "g1", "0"),  # 与 copy.md 同一重复组:评测只入第一份
    ]
    with manifest.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(MANIFEST_FIELDS))
        writer.writeheader()
        for path, group, pages in rows:
            row = dict.fromkeys(MANIFEST_FIELDS, "")
            row.update(path=path, ext="md", size="10", tier="A", pages=pages, dup_group=group)
            writer.writerow(row)
    texts, failed = corpus_texts(
        manifest=manifest, corpus_root=root, tiers=("A",), cache_dir=tmp_path / "cache", workers=1
    )
    assert sorted(texts) == ["a/big.md", "a/copy.md", "a/x.md", "b/x.md"]
    assert "第二份" in texts["b/x.md"] and failed == []


@pytest.mark.parametrize(
    ("name", "others"),
    [("价目表", {"价格表"}), ("参数", {"参考"}), ("说明书", {"说明单"})],
)
def test_generic_document_words_are_not_near_misses(name: str, others: set[str]) -> None:
    # 文档类型词(价目表 / 参数 / 说明书 …)差一个字不是「另一个对象」
    assert near_misses(name, others) == []


def test_a_generic_word_is_not_the_near_miss_of_a_real_name() -> None:
    assert near_misses("图甲", {"图表", "图乙"}) == ["图乙"]
    # 自己是文档类型词:不当对象,邻近的名字再像也不算
    assert near_misses("价格表", {"价格甲"}) == []


def test_a_generic_word_in_the_question_is_not_taken_as_the_entity() -> None:
    files = ["价格表.xlsx", "价目表.xlsx"]
    cases = [_case("d1", "价格表里第一行是什么?", "价格表.xlsx", "第一行是表头说明文字")]
    assert pick_distractors(cases, files, exclude_ids=set(), n=5, seed=1) == []


def test_a_candidate_whose_answer_cannot_be_fully_deleted_is_skipped() -> None:
    # 11-09 复审:一处可删(同一句)、另一处跨 5 行(超过 MAX_WINDOW 句)删不掉 —— 选上了,
    # 评测建库后残留检查必停,重新生成还是同一份 spec
    text = "资料:甲乙丙丁戊。\n甲乙\n丙\n丁\n戊\n"
    picked, skipped, _flagged = pick_hard_negatives(
        [_case("h1", "?", "s.md", "甲乙丙丁戊")], [], {"s.md": text}, n=3, seed=1
    )
    assert picked == [] and skipped == {"not_deletable": 1}


def test_the_parse_cache_is_keyed_by_file_content_and_parser_version(tmp_path: Path) -> None:
    """11-09 复审后的真语料空跑:选题读的是 10-13 的解析缓存(只按路径做键),评测用今天的
    解析器,句子切法不同,删句删到了对照题的答案。缓存键要带文件内容和解析器版本。"""
    import csv

    from kb_corpus_inventory import MANIFEST_FIELDS

    root = tmp_path / "corpus"
    root.mkdir()
    doc = root / "a.md"
    doc.write_text("# 甲\n\n原来的内容。\n", encoding="utf-8")
    manifest = tmp_path / "manifest.csv"
    with manifest.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(MANIFEST_FIELDS))
        writer.writeheader()
        row = dict.fromkeys(MANIFEST_FIELDS, "")
        row.update(path="a.md", ext="md", size="10", tier="A", pages="0")
        writer.writerow(row)
    cache = tmp_path / "cache"

    def read(fingerprint: str) -> str:
        texts, _failed = corpus_texts(
            manifest=manifest,
            corpus_root=root,
            tiers=("A",),
            cache_dir=cache,
            workers=1,
            fingerprint=fingerprint,
        )
        return str(texts["a.md"])

    assert "原来的内容" in read("parser-v1")
    for cached in cache.glob("*"):
        cached.write_text("缓存里的旧解析", encoding="utf-8")
    assert read("parser-v1") == "缓存里的旧解析"  # 同一文件、同一解析器:用缓存
    assert "原来的内容" in read("parser-v2")  # 解析器变了:重新解析
    doc.write_text("# 甲\n\n改过的内容。\n", encoding="utf-8")
    assert "改过的内容" in read("parser-v1")  # 文件变了:重新解析


def test_the_parser_fingerprint_is_stable_and_names_the_parser() -> None:
    assert parser_fingerprint() == parser_fingerprint()
    assert len(parser_fingerprint()) == 16


def test_the_parse_cache_is_written_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replaced: list[tuple[str, str]] = []
    real = os.replace

    def spy(src: str | Path, dst: str | Path) -> None:
        replaced.append((str(src), str(dst)))
        real(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    doc = tmp_path / "a.md"
    doc.write_text("# 甲\n\n内容。\n", encoding="utf-8")
    assert "内容" in str(_parse_fresh(doc, tmp_path / "cache", "fp"))
    [(src, dst)] = replaced
    assert src.endswith(".tmp") and dst.endswith(".md")
    assert [p.name for p in (tmp_path / "cache").iterdir()] == [Path(dst).name]


def test_the_fingerprint_covers_only_the_parser_modules_and_libraries() -> None:
    assert FINGERPRINT_MODULES == ("parsing.py", "office_docx.py", "office_xlsx.py")
    assert set(FINGERPRINT_DISTS) == {
        "pymupdf",
        "pymupdf4llm",
        "markitdown",
        "openpyxl",
        "mammoth",
        "lxml",
    }


# ---------------------------------------------------------------------------
# 11-10:删后穷尽核对 —— 部首归一、锚不能再出现、参考答案的关键事实还在就剔除
# ---------------------------------------------------------------------------


def test_an_anchor_spelled_with_supplement_radicals_in_the_text_is_deleted() -> None:
    # U+2EDD 是部首补充区块的「食」:NFKC 归一不掉,旧的删句对不上、删不掉
    text = "# 合成\n\n适用:饮\u2edd清淡的成年人。其余说明照常。\n"
    deleted, dropped = delete_anchor_units(text, ["饮食清淡的成年人"])
    assert dropped == 1 and "清淡" not in deleted and "其余说明照常" in deleted
    assert residue(text, ["饮食清淡的成年人"]) == ["饮食清淡的成年人"]
    assert residue(deleted, ["饮食清淡的成年人"]) == []


def test_the_parser_guard_sees_a_control_anchor_spelled_with_radicals() -> None:
    text = "# 合成\n\n丙产品售价一百元,适合饮\u2edd清淡的人。\n"
    parser = AnchorDeletingParser(anchors=("丙产品售价",), guard=("饮食清淡的人",))
    with pytest.raises(ControlAnchorDeletedError):
        parser.apply(text, filename="x.md")


_FILTER = "# 甲款净水器说明书(合成)\n\n甲款净水器适合家庭使用。滤芯建议每6个月更换一次。\n"


def _answer_case(case_id: str, query: str, document: str, anchor: str, answer: str) -> KbCase:
    return KbCase(
        id=case_id,
        query=query,
        source="faq_xlsx",
        tags=(),
        expected=(KbExpected(document=document, anchor=anchor, page=None),),
        answer=answer,
    )


def test_a_candidate_whose_answer_fact_is_restated_elsewhere_is_excluded() -> None:
    texts = {
        "a.md": _FILTER,
        # 另一份文档换了说法把同一件事又写了一遍:删掉锚句,答案还在库里
        "faq.md": "# 常见问题(合成)\n\n问:滤芯多久换?答:建议每6个月更换一次滤芯。\n",
    }
    case = _answer_case("h1", "甲款滤芯多久换", "a.md", "滤芯建议每6个月更换一次", "每6个月")
    picked, skipped, flagged = pick_hard_negatives([case], [], texts, n=3, seed=1)
    assert picked == [] and flagged == [("h1", "fact_number")]
    assert skipped == {"answer_still_present": 1}


def test_the_same_number_in_an_unrelated_sentence_is_not_a_restated_answer() -> None:
    case = _answer_case("h1", "甲款滤芯多久换", "a.md", "滤芯建议每6个月更换一次", "每6个月")
    unrelated = {"a.md": _FILTER, "b.md": "# 丙款(合成)\n\n丙款净水器整机保修期为6个月。\n"}
    picked, _skipped, flagged = pick_hard_negatives([case], [], unrelated, n=3, seed=1)
    assert [p.case.id for p in picked] == ["h1"] and flagged == []
    # 同一个数在复述同一件事的句子里:才算答案还在
    restated = {**unrelated, "c.md": "# 合成\n\n滤芯的更换周期建议为每6个月一次。\n"}
    picked, _skipped, flagged = pick_hard_negatives([case], [], restated, n=3, seed=1)
    assert picked == [] and flagged == [("h1", "fact_number")]


def test_a_restated_fact_about_another_named_object_does_not_count() -> None:
    # 问句点了「甲款」:只有写到甲款的文档里还留着答案才算;乙款的同一句是另一个对象的事实
    texts = {
        "说明书:甲款.md": _FILTER,
        "说明书:乙款.md": "# 乙款(合成)\n\n乙款的滤芯建议每6个月更换一次哦。\n",
    }
    case = _answer_case(
        "h1", "甲款滤芯多久换", "说明书:甲款.md", "滤芯建议每6个月更换一次", "每6个月"
    )
    picked, _skipped, flagged = pick_hard_negatives([case], [], texts, n=3, seed=1)
    assert flagged == [] and [p.case.id for p in picked] == ["h1"]
    texts["说明书:乙款.md"] += "甲款同理,滤芯也是每6个月更换一次。\n"
    _picked, _skipped, flagged = pick_hard_negatives([case], [], texts, n=3, seed=1)
    assert flagged == [("h1", "fact_number")]


def test_codes_and_quoted_phrases_are_checked_too() -> None:
    texts = {
        "a.md": "# 合成\n\n甲款的型号是 KX-305,出厂编号另见铭牌。\n",
        "b.md": "# 目录(合成)\n\n甲款(型号 KX-305)在第三页。\n",
    }
    case = _answer_case("h1", "甲款型号", "a.md", "甲款的型号是KX-305", "KX-305")
    _picked, _skipped, flagged = pick_hard_negatives([case], [], texts, n=3, seed=1)
    assert flagged == [("h1", "fact_code")]


def test_suspects_can_be_kept_and_are_still_listed() -> None:
    texts = {
        "a.md": _FILTER,
        "faq.md": "# 常见问题(合成)\n\n问:滤芯多久换?答:建议每6个月更换一次滤芯。\n",
    }
    case = _answer_case("h1", "甲款滤芯多久换", "a.md", "滤芯建议每6个月更换一次", "每6个月")
    picked, skipped, flagged = pick_hard_negatives(
        [case], [], texts, n=3, seed=1, keep_suspects=True
    )
    assert [p.case.id for p in picked] == ["h1"] and skipped == {}
    assert flagged == [("h1", "fact_number")]


def test_the_spec_lists_excluded_cases_by_id_and_reason_only() -> None:
    texts = {
        "a.md": _FILTER,
        "faq.md": "# 常见问题(合成)\n\n问:滤芯多久换?答:建议每6个月更换一次滤芯。\n",
    }
    case = _answer_case("h1", "甲款滤芯多久换", "a.md", "滤芯建议每6个月更换一次", "每6个月")
    picked, skipped, flagged = pick_hard_negatives([case], [], texts, n=3, seed=1)
    spec = spec_document(
        picked, skipped, flagged, texts=texts, set_of={}, keep_suspects=False, header={"seed": 1}
    )
    assert spec["excluded"] == [{"id": "h1", "reason": "fact_number"}]
    assert spec["cases"] == [] and "suspects" not in spec


# ---------------------------------------------------------------------------
# 审查 M2:小数尾巴不是型号;型号前后都要有边界
# ---------------------------------------------------------------------------


def test_other_doses_do_not_count_as_the_answer_dose() -> None:
    anchor = "甲片每片含活性成分1.5mg"
    case = _answer_case("h1", "甲片每片多少", "a.md", anchor, "1.5mg")
    base = {"a.md": f"# 甲片(合成)\n\n{anchor}。\n"}
    others = {**base, "b.md": "# 乙片(合成)\n\n乙片每片含活性成分2.5mg,丙片每片含活性成分15mg。\n"}
    picked, _skipped, flagged = pick_hard_negatives([case], [], others, n=3, seed=1)
    assert flagged == [] and [p.case.id for p in picked] == ["h1"]
    same = {**base, "c.md": "# 合成\n\n甲片每片活性成分含量为1.5mg。\n"}
    _picked, _skipped, flagged = pick_hard_negatives([case], [], same, n=3, seed=1)
    assert flagged == [("h1", "fact_number")]


def test_a_code_inside_a_longer_code_is_not_the_same_code() -> None:
    case = _answer_case("h1", "甲款型号", "a.md", "甲款的型号是KX-305", "KX-305")
    texts = {
        "a.md": "# 合成\n\n甲款的型号是 KX-305,出厂编号另见铭牌。\n",
        "b.md": "# 目录(合成)\n\n甲款(型号 KX-3050)在第三页。\n",
    }
    _picked, _skipped, flagged = pick_hard_negatives([case], [], texts, n=3, seed=1)
    assert flagged == []
    texts["b.md"] += "甲款(型号 KX-305)在第五页。\n"
    _picked, _skipped, flagged = pick_hard_negatives([case], [], texts, n=3, seed=1)
    assert flagged == [("h1", "fact_code")]
