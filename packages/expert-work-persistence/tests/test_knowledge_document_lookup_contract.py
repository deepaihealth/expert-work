"""Finding a document by its number or its name — both stores, one contract
(B-206 K-2: what ``knowledge_read`` resolves its ``document`` argument with).

``get_document_by_seq`` reads the tenant's document ``D{doc_seq}``;
``find_documents_by_name`` lists the documents of the given bases whose file
name or title holds a name, case-insensitively — exact matches first. The
memory run goes into unit CI; the SQL run is marked ``integration``.

Before running the SQL tests locally:
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
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
from expert_work.persistence.knowledge.base import KnowledgeStore

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "knowledge_document_lookup_contract"


async def _sql_store(request: pytest.FixtureRequest) -> AsyncIterator[KnowledgeStore]:
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


@pytest.fixture(params=["memory", pytest.param("sql", marks=pytest.mark.integration)])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[KnowledgeStore]:
    if request.param == "memory":
        yield InMemoryKnowledgeStore()
        return
    async for sql in _sql_store(request):
        yield sql


@dataclass(frozen=True)
class _Library:
    tenant: UUID
    other_tenant: UUID
    kb1: UUID
    kb2: UUID
    kb3: UUID
    #: file name -> (base, document id, doc_seq) for the tenant's documents
    docs: dict[tuple[UUID, str], tuple[UUID, int]]


async def _library(store: KnowledgeStore) -> _Library:
    tenant, other = uuid4(), uuid4()
    kb1 = (await store.create_base(tenant_id=tenant, name="kb1")).id
    kb2 = (await store.create_base(tenant_id=tenant, name="kb2")).id
    kb3 = (await store.create_base(tenant_id=tenant, name="kb3")).id
    other_kb = (await store.create_base(tenant_id=other, name="kb1")).id
    docs: dict[tuple[UUID, str], tuple[UUID, int]] = {}
    for kb_id, filename in (
        (kb1, "采购合同2024.pdf"),
        (kb1, "合同.docx"),
        (kb2, "合同.docx"),
        (kb1, "Report_Q1.md"),
        (kb1, "aXb.md"),
        (kb1, "100%达标.md"),
        (kb3, "合同.docx"),
    ):
        doc = await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename=filename)
        assert doc.doc_seq is not None
        docs[(kb_id, filename)] = (doc.id, doc.doc_seq)
    await store.upsert_document(tenant_id=other, kb_id=other_kb, filename="合同.docx")
    return _Library(tenant, other, kb1, kb2, kb3, docs)


async def _names(
    store: KnowledgeStore, lib: _Library, name: str, kb_ids: list[UUID], limit: int = 20
) -> list[tuple[UUID, str]]:
    found = await store.find_documents_by_name(
        tenant_id=lib.tenant, kb_ids=kb_ids, name=name, limit=limit
    )
    return [(doc.kb_id, doc.filename) for doc in found]


async def test_a_document_is_found_by_its_number_in_its_own_tenant_only(
    store: KnowledgeStore,
) -> None:
    lib = await _library(store)
    doc_id, seq = lib.docs[(lib.kb2, "合同.docx")]

    found = await store.get_document_by_seq(tenant_id=lib.tenant, doc_seq=seq)

    assert found is not None and found.id == doc_id and found.doc_seq == seq
    # The other tenant numbers its own documents from 1: its D1 is not ours.
    theirs = await store.get_document_by_seq(tenant_id=lib.other_tenant, doc_seq=1)
    assert theirs is not None and theirs.tenant_id == lib.other_tenant
    assert await store.get_document_by_seq(tenant_id=lib.other_tenant, doc_seq=seq) is None
    assert await store.get_document_by_seq(tenant_id=lib.tenant, doc_seq=999) is None
    assert await store.get_document_by_seq(tenant_id=uuid4(), doc_seq=1) is None


async def test_exact_file_names_come_first_across_the_given_bases_only(
    store: KnowledgeStore,
) -> None:
    lib = await _library(store)

    assert await _names(store, lib, "合同.docx", [lib.kb1, lib.kb2]) == [
        (lib.kb1, "合同.docx"),
        (lib.kb2, "合同.docx"),
    ]


async def test_a_title_match_is_exact_and_ranks_before_names_that_only_contain_it(
    store: KnowledgeStore,
) -> None:
    lib = await _library(store)

    # "合同" is the title of both 合同.docx (the file name without its
    # extension); 采购合同2024.pdf only contains it — and was uploaded first.
    assert await _names(store, lib, "合同", [lib.kb1, lib.kb2]) == [
        (lib.kb1, "合同.docx"),
        (lib.kb2, "合同.docx"),
        (lib.kb1, "采购合同2024.pdf"),
    ]
    assert await _names(store, lib, "合同", [lib.kb1, lib.kb2], limit=1) == [(lib.kb1, "合同.docx")]


async def test_names_match_without_regard_to_case(store: KnowledgeStore) -> None:
    lib = await _library(store)

    assert await _names(store, lib, "report_q1.MD", [lib.kb1]) == [(lib.kb1, "Report_Q1.md")]
    assert await _names(store, lib, "REPORT", [lib.kb1]) == [(lib.kb1, "Report_Q1.md")]


async def test_like_wildcards_in_a_name_are_literal(store: KnowledgeStore) -> None:
    lib = await _library(store)

    assert await _names(store, lib, "a_b", [lib.kb1]) == []
    assert await _names(store, lib, "%", [lib.kb1]) == [(lib.kb1, "100%达标.md")]
    assert await _names(store, lib, "_Q", [lib.kb1]) == [(lib.kb1, "Report_Q1.md")]


async def test_no_base_or_no_match_finds_nothing(store: KnowledgeStore) -> None:
    lib = await _library(store)

    assert await _names(store, lib, "合同", []) == []
    assert await _names(store, lib, "不存在", [lib.kb1, lib.kb2, lib.kb3]) == []
    assert (
        await store.find_documents_by_name(
            tenant_id=lib.other_tenant, kb_ids=[lib.kb1], name="合同", limit=20
        )
        == []
    )
