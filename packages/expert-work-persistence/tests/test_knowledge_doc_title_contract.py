# ruff: noqa: RUF001 —— 用例是带中文全角字符的原文(全角本身就是被测输入)
"""文档名进检索文本 + 入库侧归一(B-175 波 2 PR2b)—— **两个后端跑同一组断言**。

* 段带上入库时的文档名(``doc_title``),存取往返不丢;
* 关键词索引里文档名单独一段(权重 A,正文 D):只在文档名里出现的词也能命中,
  但只按 :data:`KB_DOC_TITLE_HIT_WEIGHT` 记覆盖率,同样的词写在正文里的段排在前面;
* 写入侧与查询侧同一套归一:全角 / 半角互查,带空格与不带空格的编号互查;
* 改前入库的旧段(原样建索引、没有文档名)照常命中,分数与同样字的新段一致。

SQL 档标 ``integration``,本地跑前: export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    InMemoryKnowledgeStore,
    SqlKnowledgeStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.persistence.knowledge.base import KnowledgeStore
from expert_work.persistence.knowledge.text_search import (
    KB_DOC_TITLE_HIT_WEIGHT,
    KB_KEYWORD_PHRASE_BOOST,
    tokenize_for_search,
)
from expert_work.protocol import KnowledgeChunk, ScoredChunk

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "knowledge_doc_title_contract"


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
    async with _sql_store(request.getfixturevalue("postgres_container")) as sql_store:
        yield sql_store


@asynccontextmanager
async def _sql_store(container: PostgresContainer) -> AsyncIterator[SqlKnowledgeStore]:
    """A fresh migrated database in the test container, dropped afterwards."""
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


async def _seed(
    store: KnowledgeStore, docs: list[tuple[str | None, list[str]]]
) -> tuple[UUID, UUID]:
    """One base; one document per ``(doc_title, chunk texts)`` entry."""
    tenant = uuid4()
    kb_id = (await store.create_base(tenant_id=tenant, name="kb")).id
    for number, (doc_title, texts) in enumerate(docs):
        document = await store.upsert_document(
            tenant_id=tenant, kb_id=kb_id, filename=f"doc{number}.md", content=b"x"
        )
        chunks = [
            KnowledgeChunk(
                id=uuid4(),
                tenant_id=tenant,
                kb_id=kb_id,
                document_id=document.id,
                chunk_index=index,
                content=text,
                doc_title=doc_title,
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


async def test_doc_title_round_trips(store: KnowledgeStore) -> None:
    tenant, kb_id = await _seed(store, [("益休说明书", ["每日一袋"]), (None, ["旧段"])])
    hits = await store.search_scored(
        tenant_id=tenant,
        kb_ids=[kb_id],
        query_embedding=(1.0,) + (0.0,) * (EMBEDDING_DIM - 1),
        limit=10,
    )
    assert {hit.chunk.content: hit.chunk.doc_title for hit in hits} == {
        "每日一袋": "益休说明书",
        "旧段": None,
    }


async def test_a_word_only_in_the_doc_title_matches(store: KnowledgeStore) -> None:
    """A manual's body rarely names its product; the title does."""
    tenant, kb_id = await _seed(
        store, [("益休产品说明书", ["每日一袋,温水冲服"]), (None, ["无关"])]
    )
    assert await _contents(store, tenant, kb_id, "益休") == ["每日一袋,温水冲服"]


async def test_a_title_only_hit_counts_for_less_than_a_body_hit(store: KnowledgeStore) -> None:
    """The same word in the body outranks it in the title: a title-only hit
    adds ``KB_DOC_TITLE_HIT_WEIGHT`` of the unit's coverage, a body hit all
    of it (``ts_rank`` < 0.1 breaks ties only)."""
    tenant, kb_id = await _seed(
        store, [("益休产品说明书", ["每日一袋"]), ("晚安饮说明书", ["益休可以和晚安饮一起吃"])]
    )
    hits = await _search(store, tenant, kb_id, "益休")
    assert [hit.chunk.content for hit in hits] == ["益休可以和晚安饮一起吃", "每日一袋"]
    assert 1.0 <= hits[0].score < 1.1
    assert hits[1].score < 1.0  # below full coverage, whatever its ts_rank
    assert hits[1].score == pytest.approx(KB_DOC_TITLE_HIT_WEIGHT)  # no body word: ts_rank 0


async def test_title_and_body_words_add_up(store: KnowledgeStore) -> None:
    """A question naming the product and asking about the body's subject is
    covered by title + body together, above a chunk holding only the subject."""
    tenant, kb_id = await _seed(
        store, [("益休产品说明书", ["每日一袋,温水冲服"]), ("晚安饮说明书", ["每日两袋"])]
    )
    assert await _contents(store, tenant, kb_id, "益休每日几袋") == [
        "每日一袋,温水冲服",
        "每日两袋",
    ]


async def test_a_title_only_hit_counts_as_a_whole_chunk_for_idf(store: KnowledgeStore) -> None:
    """BM25's df counts a chunk holding the unit anywhere, title included —
    the title weight scales the chunk's coverage, not how common the unit is."""
    tenant, kb_id = await _seed(
        store, [("alpha guide", ["gamma"]), (None, ["beta delta"]), (None, ["alpha"])]
    )
    hits = await _search(store, tenant, kb_id, "alpha beta")
    idf_alpha = math.log(1 + (3 - 2 + 0.5) / (2 + 0.5))  # df = 2: the title and "alpha"
    idf_beta = math.log(1 + (3 - 1 + 0.5) / (1 + 0.5))
    title_only = next(hit for hit in hits if hit.chunk.content == "gamma")
    expected = KB_DOC_TITLE_HIT_WEIGHT * idf_alpha / (idf_alpha + idf_beta)
    assert title_only.score == pytest.approx(expected, abs=1e-6)  # no body word: ts_rank 0


async def test_identifier_in_the_doc_title_gets_the_identifier_boost(
    store: KnowledgeStore,
) -> None:
    """Patent certificates often carry the number in the file name only."""
    tenant, kb_id = await _seed(
        store, [("专利证书 ZL202410048761.8", ["发明名称:一种改善睡眠的组合物"]), (None, ["专利"])]
    )
    hits = await _search(store, tenant, kb_id, "ZL202410048761.8 的专利")
    assert hits[0].chunk.content == "发明名称:一种改善睡眠的组合物"
    assert hits[0].score >= KB_KEYWORD_PHRASE_BOOST


@pytest.mark.parametrize(
    ("query", "written"),
    [
        ("VC 含量", "维生素 ＶＣ 含量"),
        ("ＶＣ 含量", "维生素 VC 含量"),
        ("2型糖尿病", "２型糖尿病的饮食"),
        ("QXA-N7", "品种 ＱＸＡ－Ｎ７ 登记"),
    ],
)
async def test_halfwidth_and_fullwidth_find_each_other(
    store: KnowledgeStore, query: str, written: str
) -> None:
    tenant, kb_id = await _seed(store, [(None, [written, "无关"])])
    assert await _contents(store, tenant, kb_id, query) == [written]


@pytest.mark.parametrize(
    ("query", "written"),
    [
        ("ZL202410048761.8", "专利号 ZL 2024 1 0048761.8 授权"),
        ("ZL 2024 1 0048761.8", "专利号 ZL202410048761.8 授权"),
        ("ZL 2024 1 0048761.8", "专利号 ZL 2024 1 0048761.8 授权"),
        ("ZL2021116309138", "专利号:ZL 2021116309138"),
        ("ZL202410048761.8", "专利号 ZL 2024 1 0048761.8 2 页"),
        ("ZL202410048761.8", "ZL 2024 1 0048761.8 2025 3 1 授权"),
    ],
)
async def test_spaced_and_unspaced_numbers_find_each_other(
    store: KnowledgeStore, query: str, written: str
) -> None:
    tenant, kb_id = await _seed(store, [(None, [written, "专利 2024 年"])])
    hits = await _search(store, tenant, kb_id, query)
    assert [hit.chunk.content for hit in hits][:1] == [written]
    assert hits[0].score >= KB_KEYWORD_PHRASE_BOOST


async def test_numbers_in_a_spaced_run_stay_searchable_alone(store: KnowledgeStore) -> None:
    """Joining a spaced run adds a form; the numbers as written stay indexed."""
    written = "血糖 120 135 150 165 180"
    tenant, kb_id = await _seed(store, [(None, [written, "无关"])])
    assert await _contents(store, tenant, kb_id, "135") == [written]


@pytest.mark.integration
async def test_rows_indexed_before_normalising_still_match(
    postgres_container: PostgresContainer,
) -> None:
    """Rows indexed raw (before PR2b: no NFKC, no joined numbers, no title)
    keep matching through the query's raw forms, and score exactly like a new
    row of the same words where normalising changes nothing."""
    legacy = ["维生素 ＶＣ 含量", "专利号 ZL 2024 1 0048761.8 授权", "苹果 香蕉"]
    async with _sql_store(postgres_container) as store:
        tenant, kb_id = await _seed(store, [(None, [*legacy, "苹果 香蕉。"])])
        async with store._sf() as session:
            for written in legacy:
                await session.execute(
                    text(
                        "UPDATE knowledge_chunk SET content_tsv = to_tsvector('simple', :raw)"
                        " WHERE content = :content"
                    ),
                    {"raw": tokenize_for_search(written), "content": written},
                )
            await session.commit()
        assert await _contents(store, tenant, kb_id, "ＶＣ") == ["维生素 ＶＣ 含量"]
        hits = await _search(store, tenant, kb_id, "ZL 2024 1 0048761.8")
        assert [hit.chunk.content for hit in hits][:1] == [legacy[1]]
        assert hits[0].score >= KB_KEYWORD_PHRASE_BOOST
        fruit = await _search(store, tenant, kb_id, "苹果和香蕉")
        scores = {hit.chunk.content: hit.score for hit in fruit}
        assert scores["苹果 香蕉"] == pytest.approx(scores["苹果 香蕉。"])
