# ruff: noqa: RUF001 —— 用例是带中文全角标点的真实问法(标点本身就是被测输入)
"""Tests for ``tokenize_for_search`` — J.5 hybrid-search tokenization."""

from __future__ import annotations

import pytest

from expert_work.persistence.knowledge.text_search import (
    KB_QUERY_MAX_TERMS,
    KbKeywordQuery,
    build_kb_keyword_query,
    is_searchable_token,
    tokenize_for_search,
)


def test_segments_chinese_into_multiple_tokens() -> None:
    result = tokenize_for_search("知识检索系统")
    # jieba splits CJK text on word boundaries — more than one token.
    assert len(result.split()) > 1
    # No characters are lost in segmentation.
    assert "".join(result.split()) == "知识检索系统"


def test_english_tokens_lowercased() -> None:
    result = tokenize_for_search("Hello World")
    tokens = result.split()
    assert "hello" in tokens
    assert "world" in tokens
    assert "Hello" not in tokens


def test_mixed_language() -> None:
    result = tokenize_for_search("查询 PostgreSQL 数据库")
    assert "postgresql" in result.split()


def test_empty_and_whitespace_yield_empty() -> None:
    assert tokenize_for_search("") == ""
    assert tokenize_for_search("   ") == ""


# -- knowledge-base keyword query (B-175 wave 2 PR1) --------------------------


def test_kb_query_drops_function_words_and_punctuation() -> None:
    query = build_kb_keyword_query("苹果适合哪些人群吃？")
    assert query == KbKeywordQuery(
        terms=("苹果", "适合", "人群", "吃"),
        phrases=(),
        query_phrase="苹果 适合 哪些 人群 吃",
    )


def test_kb_query_keeps_identifier_as_one_phrase() -> None:
    query = build_kb_keyword_query("野生苹果品种QXA-N7的编号是多少？")
    assert query == KbKeywordQuery(
        terms=("野生", "苹果", "品种", "编号"),
        phrases=("qxa - n7",),
        query_phrase="野生 苹果 品种 qxa n7 的 编号 是 多少",
    )


def test_kb_query_identifier_fragments_are_not_standalone_terms() -> None:
    assert build_kb_keyword_query("QXA-N7") == KbKeywordQuery(
        terms=(), phrases=("qxa - n7",), query_phrase="qxa n7"
    )
    # A fragment that also stands on its own elsewhere in the query is still
    # searched there, as a plain term.
    assert build_kb_keyword_query("QXA-N7 和 N7 有什么区别") == KbKeywordQuery(
        terms=("n7", "区别"), phrases=("qxa - n7",), query_phrase="qxa n7 和 n7 有 什么 区别"
    )
    assert build_kb_keyword_query("QXA-N7 和 QXA 的区别") == KbKeywordQuery(
        terms=("qxa", "区别"), phrases=("qxa - n7",), query_phrase="qxa n7 和 qxa 的 区别"
    )


@pytest.mark.parametrize(
    ("raw", "phrase"),
    [
        ("QR07-2", "qr07 - 2"),
        ("KM-204", "km - 204"),
        ("ZL201900001234.5", "zl201900001234.5"),
        ("CN100000001A", "cn100000001a"),
        ("201800005678.9", "201800005678.9"),
        ("COVID-19", "covid - 19"),
        ("2024-10-14", "2024 - 10 - 14"),
        ("H2O", "h2o"),
        ("MP3", "mp3"),
    ],
)
def test_kb_query_recognises_identifier_shapes(raw: str, phrase: str) -> None:
    query = build_kb_keyword_query(f"{raw}的专利")
    assert query is not None
    assert query.phrases == (phrase,)
    assert query.terms == ("专利",)


def test_kb_query_drops_one_and_two_digit_numbers() -> None:
    assert build_kb_keyword_query("每天吃1~2包，30天") == KbKeywordQuery(
        terms=("每天", "吃", "包", "天"), phrases=(), query_phrase="每天 吃 1 2 包 30 天"
    )
    assert build_kb_keyword_query("每天吃100毫克") == KbKeywordQuery(
        terms=("每天", "吃", "100", "毫克"), phrases=(), query_phrase="每天 吃 100 毫克"
    )


def test_kb_query_drops_pronouns_but_keeps_single_content_characters() -> None:
    assert build_kb_keyword_query("我们适合吃吗") == KbKeywordQuery(
        terms=("适合", "吃"), phrases=(), query_phrase="我们 适合 吃 吗"
    )
    assert build_kb_keyword_query("他们和这些人那些东西") == KbKeywordQuery(
        terms=("人", "东西"), phrases=(), query_phrase="他们 和 这些 人 那些 东西"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "QXA\u2011N7",  # non-breaking hyphen
        "QXA\u2013N7",  # en dash
        "QXA\u2014N7",  # em dash
        "QXA\u2212N7",  # minus sign
        "QXA\ufe63N7",  # small hyphen-minus
    ],
)
def test_kb_query_normalises_dashes(raw: str) -> None:
    """Dashes are punctuation on both sides, so the raw text needs no form of
    its own."""
    assert build_kb_keyword_query(f"{raw}的专利") == KbKeywordQuery(
        terms=("专利",), phrases=("qxa - n7",), query_phrase="qxa n7 的 专利"
    )


def test_kb_query_keeps_the_raw_form_where_nfkc_changes_it() -> None:
    """Chunks are indexed as written (no NFKC), so a unit whose raw text NFKC
    changes keeps that raw form too — a fullwidth query still finds fullwidth
    text — while the NFKC form finds halfwidth text."""
    assert build_kb_keyword_query("ＱＸＡ－Ｎ７的专利") == KbKeywordQuery(
        terms=("专利",),
        phrases=("qxa - n7",),
        query_phrase="qxa n7 的 专利",
        raw_forms=(("qxa - n7", "ｑ ｘ ａ ｎ ７"),),
        raw_query_phrase="ｑ ｘ ａ ｎ ７ 的 专利",
    )
    assert build_kb_keyword_query("ＶＣ含量") == KbKeywordQuery(
        terms=("vc", "含量"),
        phrases=(),
        query_phrase="vc 含量",
        raw_forms=(("vc", "ｖ ｃ"),),
        raw_query_phrase="ｖ ｃ 含量",
    )
    # A one-word question has no question phrase, raw or not.
    single = build_kb_keyword_query("ＶＣ")
    assert single is not None
    assert (single.query_phrase, single.raw_query_phrase) == (None, None)


@pytest.mark.parametrize("raw", ["D3", "B12", "A4", "N7", "v2", "2g", "500mg", "3.5g"])
def test_kb_query_units_and_short_codes_are_not_identifiers(raw: str) -> None:
    query = build_kb_keyword_query(f"补充{raw}")
    assert query is not None
    assert query.phrases == ()
    assert "补充" in query.terms


def test_kb_query_caps_terms_keeping_phrases_first() -> None:
    words = ["w" + chr(97 + i // 26) + chr(97 + i % 26) for i in range(100)]
    query = build_kb_keyword_query(" ".join(words) + " QXA-N7 QR07-2")
    assert query is not None
    assert query.phrases == ("qxa - n7", "qr07 - 2")
    assert query.terms == tuple(words[: KB_QUERY_MAX_TERMS - 2])
    # 104 tokens: too long to be a verbatim question, so no question phrase.
    assert query.query_phrase is None


def test_kb_query_keeps_the_whole_question_as_one_phrase() -> None:
    """Function words stay in the question phrase (the write side indexed
    them), punctuation does not; a one-token question gets none."""
    query = build_kb_keyword_query("为什么苹果比香蕉更甜？")
    assert query is not None
    assert query.terms == ("苹果", "比", "香蕉", "更甜")
    assert query.query_phrase == "为什么 苹果 比 香蕉 更甜"
    single = build_kb_keyword_query("苹果？")
    assert single is not None
    assert single.query_phrase is None


def test_kb_query_lowercases_and_dedupes_terms() -> None:
    assert build_kb_keyword_query("PostgreSQL 和 postgresql 数据库") == KbKeywordQuery(
        terms=("postgresql", "数据库"), phrases=(), query_phrase="postgresql 和 postgresql 数据库"
    )


@pytest.mark.parametrize("raw", ["", "   ", "是什么？", "？！，。", "请问一下这个是不是"])
def test_kb_query_with_nothing_searchable_is_none(raw: str) -> None:
    assert build_kb_keyword_query(raw) is None


def test_kb_query_drops_tsquery_metacharacters() -> None:
    query = build_kb_keyword_query("a & b | !c (d) : * <-> 'e' \"f\" 专利")
    assert query == KbKeywordQuery(
        terms=("a", "b", "c", "d", "e", "f", "专利"), phrases=(), query_phrase="a b c d e f 专利"
    )
    assert build_kb_keyword_query("&|!():*<->''") is None


def test_is_searchable_token() -> None:
    assert is_searchable_token("n7")
    assert is_searchable_token("专利")
    assert not is_searchable_token("-")
    assert not is_searchable_token("？")
    assert not is_searchable_token("<->")


@pytest.mark.parametrize(
    ("token", "searchable"),
    [
        ("２", True),  # fullwidth digit
        ("Ⅱ", True),  # letter number
        ("〇", True),
        ("ｶﾞ", True),  # halfwidth katakana
        ("①", False),  # other numbers: Postgres' parser drops them
        ("½", False),
        ("²", False),
        ("₂", False),
        ("㎎", False),  # symbols
        ("℃", False),
    ],
)
def test_is_searchable_token_follows_the_postgres_parser(token: str, searchable: bool) -> None:
    assert is_searchable_token(token) is searchable
