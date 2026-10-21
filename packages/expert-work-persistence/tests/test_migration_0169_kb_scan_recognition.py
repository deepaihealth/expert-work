"""迁移 0169 —— B-209:价目表补两个 qwen flash 看图模型,识别缓存带租户隔离策略。

同 0166 的测法:在**补之前**的版本(0168)上灌数据,再升到 head。在 head 上灌数据再断言
测不出东西:那条 INSERT 早就跑过了。用容器里另建的库(共享库会被别的测试升 / 降级)。

* 没有价目行 → 两行补上,价格 = B-209 设计 §4.1(¥0.2 / ¥0.8、¥0.8 / ¥2.7 每百万 token)
* 已有人工定的价 → **不覆盖**
* ``knowledge_parse_cache`` 开了 RLS 且 FORCE(按租户隔离是设计 ⑩ 的拍板)
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "migration_0169_test"
_BEFORE = "0168_kb_rerank_score_threshold"


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


def _prices(dsn: str) -> dict[str, tuple[int, int, int, int]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            """
            SELECT provider || '/' || model, input_per_mtok_micros, output_per_mtok_micros,
                   cache_creation_per_mtok_micros, cache_read_per_mtok_micros
              FROM model_rate_card
             WHERE tenant_id IS NULL
               AND model IN ('qwen3.7-flash', 'qwen3.8-flash', 'glm-ocr', 'text-embedding-v4')
            """
        ).fetchall()
    return {row[0]: tuple(row[1:]) for row in rows}


def test_adds_the_documented_prices(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), "head")
    assert _prices(fresh_db) == {
        "qwen/qwen3.7-flash": (200_000, 800_000, 0, 0),
        "qwen/qwen3.8-flash": (800_000, 2_700_000, 0, 0),
        "glm/glm-ocr": (200_000, 200_000, 0, 0),
        "qwen/text-embedding-v4": (500_000, 0, 0, 0),
    }


def test_keeps_a_price_already_set(fresh_db: str) -> None:
    manual = (150_000, 600_000, 0, 30_000)
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO model_rate_card (tenant_id, provider, model, input_per_mtok_micros,
                                         output_per_mtok_micros, cache_creation_per_mtok_micros,
                                         cache_read_per_mtok_micros)
            VALUES (NULL, 'qwen', 'qwen3.7-flash', %s, %s, %s, %s)
            """,
            manual,
        )
    command.upgrade(_alembic(fresh_db), "head")
    prices = _prices(fresh_db)
    assert prices["qwen/qwen3.7-flash"] == manual
    assert prices["qwen/qwen3.8-flash"] == (800_000, 2_700_000, 0, 0)


def test_parse_cache_is_row_level_secured(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), "head")
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        row = conn.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = 'knowledge_parse_cache'"
        ).fetchone()
    assert row == (True, True)


def test_downgrade_round_trip(fresh_db: str) -> None:
    cfg = _alembic(fresh_db)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, _BEFORE)
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE tablename IN "
                "('knowledge_parse_cache', 'platform_recognition_config')"
            ).fetchall()
        }
    assert tables == set()
    command.upgrade(cfg, "head")
