"""迁移 0171 —— B-206 K-7b:重排分数下限记下它是在哪个重排模型下标定的。

同 0169 的测法:在**补之前**的版本上灌数据,再升到 head。用容器里另建的库(共享库会被别的
测试升 / 降级)。

* 已设下限的库 → 回填成平台配置行里的重排模型(``provider/model``)
* 没设下限的库 → 不回填(没有要绑定的东西)
* 没有平台配置行 / 配置行没开重排 → 不回填(标定模型未知,检索时按「换了模型」暂停)
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
_DB = "migration_0171_test"
_BEFORE = "0169_kb_scan_recognition"


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


def _base(dsn: str, name: str, threshold: float | None) -> UUID:
    base = uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO knowledge_base (id, tenant_id, name, rerank_score_threshold) "
            "VALUES (%s, %s, %s, %s)",
            (base, uuid4(), name, threshold),
        )
    return base


def _platform_rerank(dsn: str, provider: str | None, model: str | None) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO platform_embedding_config "
            "(id, embedding_provider, embedding_model, rerank_provider, rerank_model) "
            "VALUES ('singleton', 'qwen', 'text-embedding-v4', %s, %s)",
            (provider, model),
        )


def _recorded(dsn: str) -> dict[UUID, str | None]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute("SELECT id, rerank_threshold_model FROM knowledge_base").fetchall()
    return {row[0]: row[1] for row in rows}


def test_a_set_threshold_is_bound_to_the_platform_rerank_model(fresh_db: str) -> None:
    _platform_rerank(fresh_db, "qwen", "qwen3-vl-rerank")
    thresholded = _base(fresh_db, "with-threshold", 0.7)
    plain = _base(fresh_db, "without-threshold", None)

    command.upgrade(_alembic(fresh_db), "head")

    assert _recorded(fresh_db) == {thresholded: "qwen/qwen3-vl-rerank", plain: None}


@pytest.mark.parametrize(
    ("provider", "model"), [(None, None), ("", ""), ("qwen", ""), ("", "qwen3-vl-rerank")]
)
def test_no_platform_rerank_model_leaves_the_threshold_unbound(
    fresh_db: str, provider: str | None, model: str | None
) -> None:
    _platform_rerank(fresh_db, provider, model)
    thresholded = _base(fresh_db, "with-threshold", 0.7)

    command.upgrade(_alembic(fresh_db), "head")

    assert _recorded(fresh_db) == {thresholded: None}


def test_no_platform_config_row_leaves_the_threshold_unbound(fresh_db: str) -> None:
    thresholded = _base(fresh_db, "with-threshold", 0.7)

    command.upgrade(_alembic(fresh_db), "head")

    assert _recorded(fresh_db) == {thresholded: None}


def _has_column(dsn: str) -> bool:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'knowledge_base' AND column_name = 'rerank_threshold_model'"
        ).fetchone()
    return row == (1,)


def test_downgrade_round_trip(fresh_db: str) -> None:
    _platform_rerank(fresh_db, "qwen", "qwen3-vl-rerank")
    thresholded = _base(fresh_db, "with-threshold", 0.7)
    cfg = _alembic(fresh_db)
    command.upgrade(cfg, "head")
    assert _recorded(fresh_db) == {thresholded: "qwen/qwen3-vl-rerank"}
    command.downgrade(cfg, _BEFORE)
    assert _has_column(fresh_db) is False
    command.upgrade(cfg, "head")
    assert _has_column(fresh_db) is True
    assert _recorded(fresh_db) == {thresholded: "qwen/qwen3-vl-rerank"}  # backfilled again
