"""迁移 0174 —— B-206 K-3:切块新默认 400 / 0,文档记切块参数指纹。

还停在旧默认 512 / 64 的库改成 400 / 0、调过参数的库不动;之后不带参数建的库拿到 400 / 0;
``knowledge_document.chunk_params_hash`` 可空、可写;降级删列、默认值回 512 / 64,再升回来。
用容器里另建的库(共享库会被别的测试升 / 降级)。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "migration_0174_test"
_BEFORE = "0173_kb_semantic_break_cache"


def _sync_dsn(container: PostgresContainer, database: str | None = None) -> str:
    url = str(container.get_connection_url()).replace("+psycopg2", "")
    if database is None:
        return url
    base, _, _ = url.rpartition("/")
    return f"{base}/{database}"


def _alembic(dsn: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    return cfg


@pytest.fixture
def fresh_db(postgres_container: PostgresContainer) -> Iterator[str]:
    admin = _sync_dsn(postgres_container)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_DB}"')
        conn.execute(f'CREATE DATABASE "{_DB}"')
    dsn = _sync_dsn(postgres_container, _DB)
    command.upgrade(_alembic(dsn), _BEFORE)
    try:
        yield dsn
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')


def _base(dsn: str, name: str, sizes: tuple[int, int] | None = None) -> UUID:
    base = uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        if sizes is None:
            conn.execute(
                "INSERT INTO knowledge_base (id, tenant_id, name) VALUES (%s, %s, %s)",
                (base, uuid4(), name),
            )
        else:
            conn.execute(
                "INSERT INTO knowledge_base "
                "(id, tenant_id, name, chunk_max_tokens, chunk_overlap_tokens) "
                "VALUES (%s, %s, %s, %s, %s)",
                (base, uuid4(), name, *sizes),
            )
    return base


def _sizes(dsn: str, base: UUID) -> tuple[int, int]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT chunk_max_tokens, chunk_overlap_tokens FROM knowledge_base WHERE id = %s",
            (base,),
        ).fetchone()
    assert row is not None
    return row[0], row[1]


def _has_hash_column(dsn: str) -> bool:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'knowledge_document' AND column_name = 'chunk_params_hash'"
        ).fetchone()
    return row is not None


def test_untouched_bases_move_to_the_new_defaults_and_tuned_ones_stay(fresh_db: str) -> None:
    untouched = _base(fresh_db, "untouched")  # the old server default: 512 / 64
    console = _base(fresh_db, "console", (512, 64))  # what the console used to send
    tuned = _base(fresh_db, "tuned", (256, 16))
    half = _base(fresh_db, "half", (512, 0))
    assert _sizes(fresh_db, untouched) == (512, 64)
    command.upgrade(_alembic(fresh_db), "head")
    assert _sizes(fresh_db, untouched) == (400, 0)
    assert _sizes(fresh_db, console) == (400, 0)
    assert _sizes(fresh_db, tuned) == (256, 16)
    assert _sizes(fresh_db, half) == (512, 0)
    assert _sizes(fresh_db, _base(fresh_db, "new")) == (400, 0)


def test_documents_take_a_chunk_params_hash(fresh_db: str) -> None:
    assert not _has_hash_column(fresh_db)
    command.upgrade(_alembic(fresh_db), "head")
    base = _base(fresh_db, "kb")
    document = uuid4()
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        tenant = conn.execute("SELECT tenant_id FROM knowledge_base WHERE id = %s", (base,))
        tenant_id = tenant.fetchone()[0]  # type: ignore[index]
        conn.execute(
            "INSERT INTO knowledge_document (id, tenant_id, kb_id, filename, status) "
            "VALUES (%s, %s, %s, 'a.md', 'ready')",
            (document, tenant_id, base),
        )
        stored = conn.execute(
            "SELECT chunk_params_hash FROM knowledge_document WHERE id = %s", (document,)
        ).fetchone()
        assert stored == (None,)
        conn.execute(
            "UPDATE knowledge_document SET chunk_params_hash = 'abc' WHERE id = %s", (document,)
        )


def test_the_downgrade_restores_the_old_defaults_and_upgrades_again(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), "head")
    command.downgrade(_alembic(fresh_db), _BEFORE)
    assert not _has_hash_column(fresh_db)
    assert _sizes(fresh_db, _base(fresh_db, "after-downgrade")) == (512, 64)
    command.upgrade(_alembic(fresh_db), "head")
    assert _has_hash_column(fresh_db)
