# ruff: noqa: RUF001, RUF003 —— 用例是带全角字母与长破折号的原文(它们本身就是被测输入)
"""关键词检索的 BM25 文档长度(B-206 K-9,迁移 0187)—— 只有 SQL 档有的两件事。

* 迁移给存量段补 ``keyword_token_count``:从已存的关键词向量数正文(权重 D)的位置数,
  与写入侧(``kb_keyword_length``)对同一段写出的值相同;父段(没有关键词向量)留 NULL。
  做法:迁移到 head 后照常写段,降到 0186(删列)再升回 head(补列 + 回填),逐段对比。
* 发布滚动期间旧版 pod 写的段没有长度(NULL):按库里有长度的段的平均长度算 —— 不加分
  也不扣分,而不是整条查询报错或把它当成 0 长度排到最前。

本地跑前: export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
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
    SqlKnowledgeStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.persistence.knowledge.text_search import (
    KB_BM25_B,
    KB_BM25_K1,
    kb_keyword_length,
)
from expert_work.protocol import ChunkType, KnowledgeChunk, chunk_retrieval_text

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "knowledge_bm25_length"
_BEFORE = "0186_b282_conv_dir_expiry"

Fixture = tuple[SqlKnowledgeStore, Config]


@pytest.fixture
async def migrated(postgres_container: PostgresContainer) -> AsyncIterator[Fixture]:
    admin = str(postgres_container.get_connection_url()).replace("+psycopg2", "")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{_DB}"')
    dsn = f"{admin.rpartition('/')[0]}/{_DB}"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    command.upgrade(cfg, "head")
    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    )
    try:
        yield SqlKnowledgeStore(create_async_session_factory(engine)), cfg
    finally:
        await engine.dispose()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')


def _chunk(
    tenant: UUID, kb_id: UUID, document_id: UUID, index: int, content: str, **extra: object
) -> KnowledgeChunk:
    parent = extra.get("chunk_type") is ChunkType.PARENT
    return KnowledgeChunk(
        id=uuid4(),
        tenant_id=tenant,
        kb_id=kb_id,
        document_id=document_id,
        chunk_index=index,
        content=content,
        embedding=None if parent else (1.0,) + (0.0,) * (EMBEDDING_DIM - 1),
        **extra,  # type: ignore[arg-type]
    )


async def _seed(store: SqlKnowledgeStore, texts: list[str]) -> tuple[UUID, UUID]:
    tenant = uuid4()
    kb_id = (await store.create_base(tenant_id=tenant, name="kb")).id
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename="a.md", content=b"a"
    )
    chunks = [_chunk(tenant, kb_id, document.id, i, t) for i, t in enumerate(texts)]
    await store.replace_chunks(tenant_id=tenant, document_id=document.id, chunks=chunks)
    return tenant, kb_id


async def _lengths(store: SqlKnowledgeStore) -> dict[str, int | None]:
    async with store._sf() as session:
        rows = await session.execute(
            text("SELECT content, keyword_token_count FROM knowledge_chunk")
        )
        return {row.content: row.keyword_token_count for row in rows.all()}


#: Text shapes the write side treats specially: NFKC (fullwidth), dash folding
#: and identifiers (``QXA–N7``), spaced numbers joined onto an extra line,
#: punctuation (no position), a breadcrumb, mixed CJK / Latin, a repeated word.
_SHAPES = [
    "维生素 ＶＣ 含量,每日 100mg",
    "品种 QXA–N7 的登记编号:ZL 2024 1 0048761.8",
    "问:苹果适合哪些人群吃?答:老年人群。苹果 苹果",
    "Probiotic strain LGG® supports gut health (2 粒/日)",
]


async def test_the_backfill_counts_what_the_write_side_counts(migrated: Fixture) -> None:
    """Rows written at head get their length from the write side; dropping the
    column (0186) and upgrading again makes the migration backfill every row
    from its stored keyword vector — to the same numbers. Parent rows (no
    keyword vector) stay NULL, and the document title is not counted."""
    store, cfg = migrated
    tenant = uuid4()
    kb_id = (await store.create_base(tenant_id=tenant, name="kb")).id
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename="a.md", content=b"a"
    )
    parent = _chunk(tenant, kb_id, document.id, 0, "父段原文", chunk_type=ChunkType.PARENT)
    children = [
        _chunk(
            tenant,
            kb_id,
            document.id,
            index + 1,
            content,
            doc_title="产品说明书 益休",
            heading_path=("第一章", "用法"),
            parent_chunk_id=parent.id,
        )
        for index, content in enumerate(_SHAPES)
    ]
    await store.replace_chunks(
        tenant_id=tenant, document_id=document.id, chunks=[parent, *children]
    )
    written = await _lengths(store)
    expected = {
        content: kb_keyword_length(chunk_retrieval_text(("第一章", "用法"), content))
        for content in _SHAPES
    }
    assert written == {"父段原文": None, **expected}

    command.downgrade(cfg, _BEFORE)
    command.upgrade(cfg, "head")
    assert await _lengths(store) == written


async def test_a_row_without_a_length_counts_as_the_average(migrated: Fixture) -> None:
    """A row an older release wrote has no length: it scores as a row of the
    average length of those that have one (here (6 + 1) / 2 = 3.5) — the
    same as a row whose length is exactly that average."""
    store, _ = migrated
    tenant, kb_id = await _seed(store, ["苹果 沙拉", "苹果 派 面包 牛奶 鸡蛋 米饭", "香蕉"])
    async with store._sf() as session:
        await session.execute(
            text("UPDATE knowledge_chunk SET keyword_token_count = NULL WHERE content = :content"),
            {"content": "苹果 沙拉"},
        )
        await session.commit()
    hits = await store.keyword_search_scored(
        tenant_id=tenant, kb_ids=[kb_id], query="苹果", limit=10
    )
    scores = {hit.chunk.content: hit.score for hit in hits}
    idf = math.log(1 + (3 - 2 + 0.5) / (2 + 0.5))
    norm = KB_BM25_K1  # dl = avgdl: 1 - b + b
    expected = idf * (KB_BM25_K1 + 1) / (1 + norm) / ((KB_BM25_K1 + 1) * idf)
    assert scores["苹果 沙拉"] == pytest.approx(expected, abs=1e-6)
    # …and the other row still uses its own length against that average.
    long_norm = KB_BM25_K1 * (1 - KB_BM25_B + KB_BM25_B * 6 / 3.5)
    assert scores["苹果 派 面包 牛奶 鸡蛋 米饭"] == pytest.approx(1 / (1 + long_norm), abs=1e-6)
