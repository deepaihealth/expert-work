# ruff: noqa: RUF001 —— 用例是带中文全角标点的真实问法(标点本身就是被测输入)
"""知识库关键词检索契约(B-175 波 2 PR1)—— **两个后端跑同一组断言**。

查询侧改成知识库专用的查询构造(去虚词与标点、任一词命中、编号按短语整体匹配并加权),
SQL 与内存两份 store 必须谓词同义:只在内存档上测会假绿(改前 SQL 是全 AND、内存是
任一词重叠,两边本来就不一致)。内存档进单元 CI,SQL 档标 ``integration``,在容器里
另建一个库。

本地跑 SQL 档前: export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    InMemoryKnowledgeStore,
    SqlKnowledgeStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.persistence.knowledge import memory as kb_memory
from expert_work.persistence.knowledge import sql as kb_sql
from expert_work.persistence.knowledge.base import KnowledgeStore
from expert_work.persistence.knowledge.text_search import (
    KB_BM25_B,
    KB_BM25_K1,
    KB_KEYWORD_PHRASE_BOOST,
    KB_KEYWORD_QUERY_PHRASE_BOOST,
    KbKeywordQuery,
)
from expert_work.protocol import KnowledgeChunk, ScoredChunk

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "knowledge_keyword_query_contract"


@pytest.fixture(
    params=[
        "memory",
        pytest.param("sql", marks=pytest.mark.integration),
    ]
)
async def store(request: pytest.FixtureRequest) -> AsyncIterator[KnowledgeStore]:
    if request.param == "memory":
        yield InMemoryKnowledgeStore()
        return

    container: PostgresContainer = request.getfixturevalue("postgres_container")
    admin = str(container.get_connection_url()).replace("+psycopg2", "")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{_DB}"')
    base, _, _ = admin.rpartition("/")
    dsn = f"{base}/{_DB}"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    command.upgrade(cfg, "head")
    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    )
    try:
        yield SqlKnowledgeStore(create_async_session_factory(engine))
    finally:
        await engine.dispose()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')


async def _seed(store: KnowledgeStore, texts: list[str]) -> tuple[UUID, UUID]:
    """One base, one document, one chunk per text (chunk_index = position)."""
    tenant = uuid4()
    kb_id = (await store.create_base(tenant_id=tenant, name="kb")).id
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename="a.md", content=b"a"
    )
    chunks = [
        KnowledgeChunk(
            id=uuid4(),
            tenant_id=tenant,
            kb_id=kb_id,
            document_id=document.id,
            chunk_index=index,
            content=text,
            embedding=(1.0,) + (0.0,) * (EMBEDDING_DIM - 1),
        )
        for index, text in enumerate(texts)
    ]
    await store.replace_chunks(tenant_id=tenant, document_id=document.id, chunks=chunks)
    return tenant, kb_id


async def _search(
    store: KnowledgeStore, tenant: UUID, kb_id: UUID, query: str
) -> list[ScoredChunk]:
    return await store.keyword_search_scored(
        tenant_id=tenant, kb_ids=[kb_id], query=query, limit=10
    )


async def _contents(store: KnowledgeStore, tenant: UUID, kb_id: UUID, query: str) -> list[str]:
    return [hit.chunk.content for hit in await _search(store, tenant, kb_id, query)]


def _idf(chunks_in_scope: int, chunks_with_unit: int) -> float:
    """BM25's idf (Lucene's form) — the weight of one query unit."""
    return math.log(1 + (chunks_in_scope - chunks_with_unit + 0.5) / (chunks_with_unit + 0.5))


def _bm25(units: list[tuple[float, float]], length: int, average_length: float) -> float:
    """The normalised BM25 a chunk scores (B-206 K-9) for ``units`` = (idf, tf)
    of every query unit: Σ idf·tf·(k1+1) / (tf + k1·(1 - b + b·dl/avgdl)),
    divided by its ceiling (k1+1)·Σ idf so it stays below 1."""
    norm = KB_BM25_K1 * (1 - KB_BM25_B + KB_BM25_B * length / average_length)
    total = sum(idf * tf * (KB_BM25_K1 + 1) / (tf + norm) for idf, tf in units if tf)
    return total / ((KB_BM25_K1 + 1) * sum(idf for idf, _ in units))


async def test_natural_language_question_finds_the_chunk(store: KnowledgeStore) -> None:
    """A long question full of function words and punctuation still hits the
    chunk that shares one content word (all-AND matching found nothing)."""
    tenant, kb_id = await _seed(store, ["编号：ZL201900001234.5", "公司简介"])
    hits = await _contents(store, tenant, kb_id, "QXA-N7这个品种的编号是多少？")
    assert hits == ["编号：ZL201900001234.5"]


async def test_identifier_matches_only_the_whole_identifier(store: KnowledgeStore) -> None:
    """``QXA-N7`` is one phrase: its fragments alone, or apart, do not match."""
    tenant, kb_id = await _seed(
        store, ["品种 QXA-N7 的登记编号", "含有 N7 成分", "QXA 集团简介", "QXA 和 N7 并列"]
    )
    assert await _contents(store, tenant, kb_id, "QXA-N7") == ["品种 QXA-N7 的登记编号"]


async def test_identifier_punctuation_takes_no_position(store: KnowledgeStore) -> None:
    """Postgres drops the joiner without counting a position, so the phrase
    for ``QXA-N7`` also matches ``QXA N7`` / ``QXA_N7``; the in-memory store
    must agree."""
    tenant, kb_id = await _seed(store, ["编号 QXA N7", "编号 QXA_N7", "QXA 编号 N7"])
    assert sorted(await _contents(store, tenant, kb_id, "QXA-N7")) == [
        "编号 QXA N7",
        "编号 QXA_N7",
    ]


async def test_identifier_chunk_ranks_above_scattered_words(store: KnowledgeStore) -> None:
    """In a mixed query the chunk holding the identifier comes first, ahead of
    a chunk that matches more of the plain words; its score carries the boost."""
    scattered = "野生 苹果 品种 编号 野生 苹果 品种 编号"
    tenant, kb_id = await _seed(store, [scattered, "QXA-N7 编号"])
    hits = await _search(store, tenant, kb_id, "野生苹果品种QXA-N7的编号是多少")
    assert [hit.chunk.content for hit in hits] == ["QXA-N7 编号", scattered]
    assert hits[0].score >= KB_KEYWORD_PHRASE_BOOST > hits[1].score > 0
    assert {hit.source for hit in hits} == {"keyword"}


async def test_function_words_alone_do_not_match(store: KnowledgeStore) -> None:
    tenant, kb_id = await _seed(store, ["是 的 多少", "价格 表"])
    assert await _contents(store, tenant, kb_id, "价格是多少？") == ["价格 表"]


async def test_more_distinct_words_outrank_one_repeated_word(store: KnowledgeStore) -> None:
    """A chunk covering three different query words beats one that repeats a
    single common query word five times."""
    repeated = "吃 饭 吃 菜 吃 肉 吃 鱼 吃 蛋"
    tenant, kb_id = await _seed(store, [repeated, "苹果 适合 老年 人群"])
    hits = await _contents(store, tenant, kb_id, "苹果适合哪些人群吃？")
    assert hits == ["苹果 适合 老年 人群", repeated]


async def test_covering_every_query_word_beats_repeating_some_of_them(
    store: KnowledgeStore,
) -> None:
    """The chunk holding all four query words once outranks one that repeats
    three of them ten times: BM25 saturates a repeated word (k1) and weighs
    the long chunk's words down (b)."""
    every_word = "苹果 适合 老年 人群 吃"
    three_words = " ".join(["苹果 适合 吃"] * 10)
    tenant, kb_id = await _seed(store, [three_words, every_word])
    hits = await _search(store, tenant, kb_id, "苹果适合哪些人群吃？")
    assert [hit.chunk.content for hit in hits] == [every_word, three_words]
    # No identifier, no verbatim match: the score is the normalised BM25.
    # 苹果 / 适合 / 吃 are in both chunks, 人群 only in ``every_word``; the
    # lengths are 5 and 30 tokens (average 17.5).
    common, rare = _idf(2, 2), _idf(2, 1)
    expected = [
        _bm25([(common, 1), (common, 1), (rare, 1), (common, 1)], 5, 17.5),
        _bm25([(common, 10), (common, 10), (rare, 0), (common, 10)], 30, 17.5),
    ]
    assert [hit.score for hit in hits] == pytest.approx(expected, abs=1e-6)


async def test_a_short_chunk_outranks_a_long_one_holding_the_same_words(
    store: KnowledgeStore,
) -> None:
    """BM25's length normalisation (B-206 K-9): of two chunks that each hold
    the query's words once, the shorter ranks first — before K-9 they tied and
    kept the scan order, which put the long one (inserted first) ahead."""
    long_chunk = "苹果 沙拉 " + " ".join(LONG_WORDS[:30])
    tenant, kb_id = await _seed(store, [long_chunk, "苹果 沙拉 配方"])
    assert await _contents(store, tenant, kb_id, "沙拉和苹果") == ["苹果 沙拉 配方", long_chunk]


async def test_the_keyword_score_is_normalised_bm25(store: KnowledgeStore) -> None:
    """tf = the unit's occurrences in the body, dl = the body's tokens (not its
    distinct words: ``苹果 苹果 沙拉`` is 3 long), avgdl and idf over the chunks
    in scope — the same number from both stores."""
    tenant, kb_id = await _seed(store, ["苹果 苹果 沙拉", "苹果 派 面包 牛奶 鸡蛋", "香蕉"])
    hits = await _search(store, tenant, kb_id, "沙拉和苹果")
    salad, apple = _idf(3, 1), _idf(3, 2)  # units in query order: 沙拉, 苹果
    assert [hit.chunk.content for hit in hits] == ["苹果 苹果 沙拉", "苹果 派 面包 牛奶 鸡蛋"]
    expected = [_bm25([(salad, 1), (apple, 2)], 3, 3.0), _bm25([(salad, 0), (apple, 1)], 5, 3.0)]
    assert [hit.score for hit in hits] == pytest.approx(expected, abs=1e-6)


async def test_rarer_word_outweighs_a_common_one(store: KnowledgeStore) -> None:
    """Of two chunks that each hold one of the two query words, the one whose
    word few chunks contain ranks first — although the other repeats its
    common word three times."""
    common_hit, rare_hit = "苹果 苹果 苹果 沙拉", "香蕉 沙拉"
    tenant, kb_id = await _seed(store, [common_hit, rare_hit, "苹果 甜点", "苹果 果汁", "苹果 派"])
    hits = await _contents(store, tenant, kb_id, "苹果和香蕉")
    assert hits[:2] == [rare_hit, common_hit]
    assert len(hits) == 5


async def test_verbatim_question_outranks_the_same_words_scattered(store: KnowledgeStore) -> None:
    """A chunk holding the question word for word (function words included,
    punctuation aside) outranks one with every content word reordered and
    repeated; a chunk of function words alone is still not a match."""
    verbatim = "问：为什么苹果比香蕉更甜？答：品种不同。"
    scattered = "香蕉 比 苹果 更甜 更甜 更甜"
    tenant, kb_id = await _seed(store, [scattered, verbatim, "为什么 为什么"])
    hits = await _search(store, tenant, kb_id, "为什么苹果比香蕉更甜？")
    assert [hit.chunk.content for hit in hits] == [verbatim, scattered]
    assert hits[0].score >= KB_KEYWORD_QUERY_PHRASE_BOOST > hits[1].score


async def test_question_phrase_never_adds_a_match(
    store: KnowledgeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verbatim-question bonus only reorders what the any-unit predicate
    matched: a chunk holding the question phrase but none of the units is not
    returned. The builder never yields such a query (the phrase's words
    include every term), so the stores get one directly."""
    forced = KbKeywordQuery(terms=("香蕉",), phrases=(), query_phrase="这个 是 什么")

    def _build(_query: str) -> KbKeywordQuery:
        return forced

    monkeypatch.setattr(kb_sql, "build_kb_keyword_query", _build)
    monkeypatch.setattr(kb_memory, "build_kb_keyword_query", _build)
    tenant, kb_id = await _seed(store, ["这个是什么", "香蕉 沙拉"])
    assert await _contents(store, tenant, kb_id, "这个是什么") == ["香蕉 沙拉"]


_WIDTHS = [
    pytest.param("hw", "hw", True, id="query-hw-chunk-hw"),
    pytest.param("fw", "fw", True, id="query-fw-chunk-fw"),
    pytest.param("fw", "hw", True, id="query-fw-chunk-hw"),
    pytest.param("hw", "fw", True, id="query-hw-chunk-fw"),
]
_TERM = {"hw": "VC", "fw": "ＶＣ"}
_IDENTIFIER = {"hw": "QXA-N7", "fw": "ＱＸＡ－Ｎ７"}
_QUESTION = {"hw": "2号苹果可以生吃吗？", "fw": "２号苹果可以生吃吗？"}


@pytest.mark.parametrize(("query_width", "chunk_width", "found"), _WIDTHS)
async def test_term_matches_across_widths(
    store: KnowledgeStore, query_width: str, chunk_width: str, found: bool
) -> None:
    """Both sides are NFKC-normalised (B-175 wave 2 PR2b; before it chunks
    were indexed as written and halfwidth never found fullwidth), so every
    width combination matches."""
    chunk = f"维生素 {_TERM[chunk_width]} 含量"
    tenant, kb_id = await _seed(store, [chunk])
    found_chunks = await _contents(store, tenant, kb_id, _TERM[query_width])
    assert found_chunks == ([chunk] if found else [])


@pytest.mark.parametrize(("query_width", "chunk_width", "found"), _WIDTHS)
async def test_identifier_matches_across_widths(
    store: KnowledgeStore, query_width: str, chunk_width: str, found: bool
) -> None:
    chunk = f"编号 {_IDENTIFIER[chunk_width]} 登记"
    tenant, kb_id = await _seed(store, [chunk])
    found_chunks = await _contents(store, tenant, kb_id, _IDENTIFIER[query_width])
    assert found_chunks == ([chunk] if found else [])


@pytest.mark.parametrize(("query_width", "chunk_width", "found"), _WIDTHS)
async def test_question_phrase_matches_across_widths(
    store: KnowledgeStore, query_width: str, chunk_width: str, found: bool
) -> None:
    chunk = f"问：{_QUESTION[chunk_width]}答：可以。"
    tenant, kb_id = await _seed(store, [chunk])
    hits = await _search(store, tenant, kb_id, _QUESTION[query_width])
    assert [hit.chunk.content for hit in hits] == [chunk]  # 号 / 苹果 / 生吃 match either way
    assert (hits[0].score >= KB_KEYWORD_QUERY_PHRASE_BOOST) is found


async def test_idf_weights_stay_positive_when_a_word_is_everywhere(store: KnowledgeStore) -> None:
    """BM25's idf stays usable at the extremes. When every chunk holds every
    query word (df = N) the +0.5 smoothing keeps each weight off zero, so the
    weights never sum to zero and every chunk gets the same positive score
    (this half cannot tell the sign of the weights apart). The +1 inside the
    log keeps the weight of a word found in every chunk above zero: it still
    counts, just for less than a rarer word (the second half)."""
    tenant, kb_id = await _seed(store, ["苹果 香蕉 沙拉", "香蕉 苹果 果汁", "苹果 派 香蕉"])
    hits = await _search(store, tenant, kb_id, "苹果和香蕉")
    assert len(hits) == 3
    everywhere = _idf(3, 3)
    expected = _bm25([(everywhere, 1), (everywhere, 1)], 3, 3.0)
    assert [hit.score for hit in hits] == pytest.approx([expected] * 3, abs=1e-6)
    assert expected > 0
    tenant, kb_id = await _seed(store, ["苹果 沙拉", "苹果 香蕉", "苹果 派"])
    hits = await _search(store, tenant, kb_id, "苹果和香蕉")
    assert hits[0].chunk.content == "苹果 香蕉"
    assert len(hits) == 3
    assert all(0 < hit.score < hits[0].score for hit in hits[1:])


async def test_identifier_chunk_first_even_for_a_long_query(store: KnowledgeStore) -> None:
    """The phrase boost outweighs any BM25 a chunk without the identifier can
    reach, however many words the query has (40 here, the other chunk
    repeating all of them)."""
    words = ["w" + chr(97 + i // 26) + chr(97 + i % 26) for i in range(40)]
    every_word_repeated = " ".join(words * 10)
    tenant, kb_id = await _seed(store, [every_word_repeated, "QXA-N7"])
    hits = await _search(store, tenant, kb_id, " ".join(words) + " QXA-N7")
    assert [hit.chunk.content for hit in hits] == ["QXA-N7", every_word_repeated]


async def test_sql_and_memory_rank_a_shared_fixture_identically(store: KnowledgeStore) -> None:
    """One expected order for both stores: identifier hits first, the verbatim
    question first among them, then BM25 (more of the query covered, rarer
    words weighing more, repeats counting with diminishing returns, shorter
    chunks ahead). Insertion order is deliberately not the expected order."""
    once = "苹果 适合 吃"
    repeated = "苹果 苹果 苹果 适合 适合 适合 吃 吃 吃"
    tenant, kb_id = await _seed(
        store,
        [
            "吃 吃 吃 吃 吃 吃 吃 吃",  # 1/5 covered
            once,  # 3/5, single occurrences
            "QXA-N7 品种",  # identifier, 1/5
            repeated,  # 3/5, each three times: above ``once`` despite its length
            "苹果 适合 人群 吃",  # 4/5
            "无关 内容",  # no match
            "QXA-N7 苹果 适合",  # identifier, 3/5
            "问：苹果QXA-N7适合哪些人群吃？",  # the question verbatim
        ],
    )
    hits = await _search(store, tenant, kb_id, "苹果QXA-N7适合哪些人群吃？")
    assert [hit.chunk.content for hit in hits] == [
        "问：苹果QXA-N7适合哪些人群吃？",
        "QXA-N7 苹果 适合",
        "QXA-N7 品种",
        "苹果 适合 人群 吃",
        repeated,
        once,
        "吃 吃 吃 吃 吃 吃 吃 吃",
    ]
    # Same scores too (values from Postgres): identifier 2.0 + question 1.5 +
    # the normalised BM25.
    expected = [3.869702, 2.260215, 2.139622, 0.372014, 0.245263, 0.218412, 0.109636]
    assert [hit.score for hit in hits] == pytest.approx(expected, abs=1e-5)


#: A ~800-character question: 200 distinct words, then an identifier.
LONG_WORDS = ["w" + chr(97 + i // 26) + chr(97 + i % 26) for i in range(200)]
LONG_QUESTION = " ".join(LONG_WORDS) + " QXA-N7"


async def test_long_question_is_capped_and_still_answers(store: KnowledgeStore) -> None:
    """A long question neither fails nor searches past the term cap: the
    identifier (phrases go first) and the earliest words are searched, a word
    beyond the cap is not."""
    tenant, kb_id = await _seed(
        store, ["QXA-N7 品种", f"{LONG_WORDS[0]} {LONG_WORDS[1]}", LONG_WORDS[150]]
    )
    assert len(LONG_QUESTION) > 600
    hits = await _contents(store, tenant, kb_id, LONG_QUESTION)
    assert hits == ["QXA-N7 品种", f"{LONG_WORDS[0]} {LONG_WORDS[1]}"]


@pytest.mark.parametrize("query", ["", "   ", "是什么？", "？！，。"])
async def test_nothing_searchable_returns_empty(store: KnowledgeStore, query: str) -> None:
    tenant, kb_id = await _seed(store, ["是 什么 ？ 的"])
    assert await _search(store, tenant, kb_id, query) == []


async def test_tsquery_metacharacters_are_inert(store: KnowledgeStore) -> None:
    """tsquery operators typed into the query neither raise nor change what
    matches."""
    tenant, kb_id = await _seed(store, ["品种 QXA-N7 的编号", "编号 公告", "无关 内容"])
    clean = await _contents(store, tenant, kb_id, "QXA-N7 编号")
    assert clean == ["品种 QXA-N7 的编号", "编号 公告"]
    hostile = "QXA-N7 & | ! ( ) : * <-> ' \" 编号:* | (!"
    assert await _contents(store, tenant, kb_id, hostile) == clean
    assert await _search(store, tenant, kb_id, "&|!():*<->''\"") == []


def _no_session() -> None:
    raise AssertionError("keyword search opened a database session")


@pytest.mark.parametrize("query", ["", "是什么？", "？！，。"])
async def test_sql_short_circuits_without_a_database_round_trip(query: str) -> None:
    store = SqlKnowledgeStore(cast(Any, _no_session))
    assert await store.keyword_search_scored(tenant_id=uuid4(), kb_ids=[uuid4()], query=query) == []
