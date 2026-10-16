"""迁移 0166 —— 价目表补 ``deepseek-flash``,价格照抄 ``deepseek-v4-flash``。

在**补之前**的版本(0165)上灌价目行,再升到 head,看补得对不对。在 head 上灌数据再断言
测不出东西:那条 INSERT 早就跑过了。三种起点各开一个库:

* 有旧名的价、没有新名 → 新名补上,四个价一字不差(这是这条迁移存在的理由)
* 新名已有人工定的价   → **不覆盖**
* 旧名没有价           → 不补(没有可抄的价,不能编一个)

用容器里另建的库,而不是共享那个 session 级容器的默认库 —— 别的测试会把它升到 head,
之后再 ``upgrade 0165`` 就是降级,拿不到「补之前」的状态。
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
_DB = "rate_card_deepseek_flash_test"
_BEFORE = "0165_knowledge_model_v2"
_OLD_PRICE = (2_000_000, 8_000_000, 0, 40_000)


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
    """A database of its own, migrated only as far as the version *before* 0166."""
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


def _seed_price(conn: psycopg.Connection, model: str, price: tuple[int, int, int, int]) -> None:
    conn.execute(
        """
        INSERT INTO model_rate_card (tenant_id, provider, model, input_per_mtok_micros,
                                     output_per_mtok_micros, cache_creation_per_mtok_micros,
                                     cache_read_per_mtok_micros)
        VALUES (NULL, 'deepseek', %s, %s, %s, %s, %s)
        """,
        (model, *price),
    )


def _flash_prices(dsn: str) -> list[tuple[int, int, int, int]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            """
            SELECT input_per_mtok_micros, output_per_mtok_micros,
                   cache_creation_per_mtok_micros, cache_read_per_mtok_micros
              FROM model_rate_card
             WHERE tenant_id IS NULL AND provider = 'deepseek' AND model = 'deepseek-flash'
            """
        ).fetchall()
    return [tuple(r) for r in rows]


def test_copies_the_old_flash_price(fresh_db: str) -> None:
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        _seed_price(conn, "deepseek-v4-flash", _OLD_PRICE)
    command.upgrade(_alembic(fresh_db), "head")
    assert _flash_prices(fresh_db) == [_OLD_PRICE]


def test_keeps_a_price_already_set_for_the_new_name(fresh_db: str) -> None:
    manual = (1_000_000, 4_000_000, 0, 20_000)
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        _seed_price(conn, "deepseek-v4-flash", _OLD_PRICE)
        _seed_price(conn, "deepseek-flash", manual)
    command.upgrade(_alembic(fresh_db), "head")
    assert _flash_prices(fresh_db) == [manual]


def test_invents_no_price_when_the_old_name_has_none(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), "head")
    assert _flash_prices(fresh_db) == []
