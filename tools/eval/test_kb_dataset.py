"""kb_dataset.py 的加载与校验:每条规则一条红绿(B-175 L1-T1)。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from kb_dataset import (  # type: ignore[import-not-found]
    SOURCES,
    KbDataset,
    KbDatasetError,
    KbExpected,
    check_corpus,
    load_dataset,
)

_ANCHOR = "常温密封保存即可"  # 8 字,刚好到下限


def _case(case_id: str = "faq-0001", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": case_id,
        "query": "益生菌需要放冰箱冷藏吗?",
        "source": "faq_xlsx",
        "tags": ["通用"],
        "expected": [{"document": "说明书A.docx", "anchor": _ANCHOR, "page": None}],
        "answer": "常温密封保存即可。",
    }
    base.update(overrides)
    return base


def _write(tmp_path: Path, cases: list[dict[str, Any]], **top: Any) -> Path:
    doc: dict[str, Any] = {
        "name": "unit_v1",
        "corpus": {"manifest": "~/kb-eval/x/manifest.csv", "tiers": ["A", "D-A"]},
        "cases": cases,
    }
    doc.update(top)
    path = tmp_path / "dataset.yaml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    return path


def test_load_valid_dataset_round_trip(tmp_path: Path) -> None:
    ds = load_dataset(
        _write(tmp_path, [_case(), _case("hard-0001", source="no_answer", expected=[])])
    )
    assert isinstance(ds, KbDataset)
    assert ds.name == "unit_v1"
    assert ds.corpus_manifest == "~/kb-eval/x/manifest.csv"
    assert ds.corpus_tiers == ("A", "D-A")
    assert [c.id for c in ds.cases] == ["faq-0001", "hard-0001"]
    first = ds.cases[0]
    assert first.query == "益生菌需要放冰箱冷藏吗?"
    assert first.source == "faq_xlsx"
    assert first.tags == ("通用",)
    assert first.expected == (KbExpected(document="说明书A.docx", anchor=_ANCHOR, page=None),)
    assert first.answer == "常温密封保存即可。"
    assert ds.cases[1].expected == ()
    assert ds.cases[1].answer is None or isinstance(ds.cases[1].answer, str)


def test_corpus_block_is_optional(tmp_path: Path) -> None:
    path = tmp_path / "no_corpus.yaml"
    path.write_text(yaml.safe_dump({"name": "n", "cases": [_case()]}, allow_unicode=True))
    ds = load_dataset(path)
    assert ds.corpus_manifest is None
    assert ds.corpus_tiers == ()


def test_sources_enum_is_the_documented_seven() -> None:
    assert SOURCES == frozenset(
        {"faq_xlsx", "faq_2026", "manual_qa", "hard_cross", "hard_exact", "hard_table", "no_answer"}
    )


@pytest.mark.parametrize("bad_query", [None, "", "   "])
def test_missing_or_empty_query_is_rejected(tmp_path: Path, bad_query: str | None) -> None:
    case = _case("faq-q")
    if bad_query is None:
        del case["query"]
    else:
        case["query"] = bad_query
    with pytest.raises(KbDatasetError, match="faq-q"):
        load_dataset(_write(tmp_path, [case]))


def test_unknown_source_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(KbDatasetError, match="faq-src") as exc:
        load_dataset(_write(tmp_path, [_case("faq-src", source="chat_log")]))
    assert "chat_log" in str(exc.value)


def test_anchor_shorter_than_eight_chars_is_rejected(tmp_path: Path) -> None:
    short = [{"document": "说明书A.docx", "anchor": "常温密封保存即", "page": None}]  # 7 字
    with pytest.raises(KbDatasetError, match="faq-anchor"):
        load_dataset(_write(tmp_path, [_case("faq-anchor", expected=short)]))


def test_anchor_of_exactly_eight_chars_is_accepted(tmp_path: Path) -> None:
    ds = load_dataset(_write(tmp_path, [_case("faq-anchor8")]))
    assert ds.cases[0].expected[0].anchor == _ANCHOR
    assert len(_ANCHOR) == 8


def test_whitespace_padding_does_not_count_toward_anchor_length(tmp_path: Path) -> None:
    padded = [{"document": "说明书A.docx", "anchor": "   短锚   ", "page": None}]
    with pytest.raises(KbDatasetError, match="faq-pad"):
        load_dataset(_write(tmp_path, [_case("faq-pad", expected=padded)]))


def test_duplicate_case_ids_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(KbDatasetError, match="faq-dup"):
        load_dataset(_write(tmp_path, [_case("faq-dup"), _case("faq-dup")]))


@pytest.mark.parametrize("bad_expected", ["说明书A.docx", {"document": "x"}, 3])
def test_expected_must_be_a_list(tmp_path: Path, bad_expected: Any) -> None:
    with pytest.raises(KbDatasetError, match="faq-exp"):
        load_dataset(_write(tmp_path, [_case("faq-exp", expected=bad_expected)]))


def test_missing_expected_key_is_rejected(tmp_path: Path) -> None:
    case = _case("faq-noexp")
    del case["expected"]
    with pytest.raises(KbDatasetError, match="faq-noexp"):
        load_dataset(_write(tmp_path, [case]))


def test_empty_expected_list_means_no_answer_and_is_valid(tmp_path: Path) -> None:
    ds = load_dataset(_write(tmp_path, [_case("na-0001", source="no_answer", expected=[])]))
    assert ds.cases[0].expected == ()


def test_expected_entry_needs_document_and_anchor(tmp_path: Path) -> None:
    with pytest.raises(KbDatasetError, match="faq-nodoc"):
        load_dataset(_write(tmp_path, [_case("faq-nodoc", expected=[{"anchor": _ANCHOR}])]))
    with pytest.raises(KbDatasetError, match="faq-noanchor"):
        load_dataset(_write(tmp_path, [_case("faq-noanchor", expected=[{"document": "a.docx"}])]))


@pytest.mark.parametrize("bad_page", ["3", 2.5, True])
def test_page_must_be_int_or_null(tmp_path: Path, bad_page: Any) -> None:
    exp = [{"document": "说明书A.docx", "anchor": _ANCHOR, "page": bad_page}]
    with pytest.raises(KbDatasetError, match="faq-page"):
        load_dataset(_write(tmp_path, [_case("faq-page", expected=exp)]))


def test_page_int_and_omitted_page_are_accepted(tmp_path: Path) -> None:
    exp = [
        {"document": "说明书A.docx", "anchor": _ANCHOR, "page": 12},
        {"document": "说明书B.docx", "anchor": _ANCHOR},
    ]
    ds = load_dataset(_write(tmp_path, [_case("faq-pageok", expected=exp)]))
    assert [e.page for e in ds.cases[0].expected] == [12, None]


def test_case_without_id_is_rejected_with_position(tmp_path: Path) -> None:
    case = _case()
    del case["id"]
    with pytest.raises(KbDatasetError, match="#0"):
        load_dataset(_write(tmp_path, [case]))


def test_cases_must_be_a_list(tmp_path: Path) -> None:
    path = tmp_path / "bad_cases.yaml"
    path.write_text(yaml.safe_dump({"name": "n", "cases": {"id": "x"}}), encoding="utf-8")
    with pytest.raises(KbDatasetError, match="cases"):
        load_dataset(path)


# ---------------------------------------------------------------------------
# check_corpus — 纯函数,只读 manifest.csv 的 path / tier 两列
# ---------------------------------------------------------------------------


def _manifest(tmp_path: Path) -> Path:
    path = tmp_path / "manifest.csv"
    rows = [
        "path,top_dir,ext,size,tier,pages,scanned_pages,text_chars,dup_group,version_marker,error",
        "说明书/说明书A.docx,说明书,docx,10,A,0,0,0,,,",
        "物料/折页.pdf,物料,pdf,10,D-A,2,0,100,,,",
        "扫描/扫描件.pdf,扫描,pdf,10,B,2,2,0,,,",
        "聊天记录/群聊.xlsx,聊天记录,xlsx,10,Q,0,0,0,,,",
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_check_corpus_accepts_documents_in_tier_a_and_d_a(tmp_path: Path) -> None:
    exp = [
        {"document": "说明书A.docx", "anchor": _ANCHOR},
        {"document": "折页.pdf", "anchor": _ANCHOR},
    ]
    ds = load_dataset(_write(tmp_path, [_case("ok", expected=exp)]))
    assert check_corpus(ds, _manifest(tmp_path)) == []


def test_check_corpus_reports_unknown_and_non_text_tier_documents(tmp_path: Path) -> None:
    exp = [
        {"document": "不存在.docx", "anchor": _ANCHOR},
        {"document": "扫描件.pdf", "anchor": _ANCHOR},  # B 层:有文件但不在 A / D-A
        {"document": "群聊.xlsx", "anchor": _ANCHOR},  # Q 层
    ]
    ds = load_dataset(_write(tmp_path, [_case("bad", expected=exp)]))
    problems = check_corpus(ds, _manifest(tmp_path))
    assert len(problems) == 3
    assert all(p.startswith("bad:") for p in problems)
    assert any("不存在.docx" in p for p in problems)
    assert any("扫描件.pdf" in p for p in problems)
    assert any("群聊.xlsx" in p for p in problems)


def test_check_corpus_ignores_no_answer_cases(tmp_path: Path) -> None:
    ds = load_dataset(_write(tmp_path, [_case("na", source="no_answer", expected=[])]))
    assert check_corpus(ds, _manifest(tmp_path)) == []


def test_match_defaults_to_any_and_rejects_unknown(tmp_path: Path) -> None:
    def write(match: object) -> Path:
        case: dict[str, object] = {
            "id": "c1",
            "query": "哪些产品含有 BGI-N3?",
            "source": "hard_cross",
            "expected": [{"document": "a.docx", "anchor": "动物双歧杆菌乳亚种BGI-N3"}],
        }
        if match is not None:
            case["match"] = match
        path = tmp_path / "d.yaml"
        path.write_text(
            yaml.safe_dump({"name": "d", "cases": [case]}, allow_unicode=True), encoding="utf-8"
        )
        return path

    default_path = write(None)
    assert load_dataset(default_path).cases[0].match == "any"
    all_path = write("all")
    assert load_dataset(all_path).cases[0].match == "all"
    bad_path = write("some")
    with pytest.raises(KbDatasetError, match="'match'"):
        load_dataset(bad_path)


_KB_DATASETS = Path(__file__).resolve().parent / "datasets" / "kb"


def test_committed_holdout_set_loads_and_stays_apart_from_the_main_set() -> None:
    """The holdout set must load like the main set, and share no case id or
    question with it — otherwise it is not held out."""
    main = load_dataset(_KB_DATASETS / "huada_v1.yaml")
    holdout = load_dataset(_KB_DATASETS / "huada_v1.holdout.yaml")
    assert len(holdout.cases) >= 20
    assert {c.id for c in main.cases}.isdisjoint({c.id for c in holdout.cases})
    assert {c.query for c in main.cases}.isdisjoint({c.query for c in holdout.cases})
    assert all("留出" in c.tags for c in holdout.cases)
