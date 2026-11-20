"""迁移 0196 —— 价目表补 ``doubao-seed-2-1-turbo-260628`` 与 ``doubao-seed-2-1-lite-260915``。

同 0192 的测法:在**补之前**的版本(0195)上灌数据,再升到 0196。在 head 上灌数据再断言测不出
东西:那条 INSERT 早就跑过了。用容器里另建的库(共享库会被别的测试升 / 降级)。

* 没有价目行       → 两行都补上,价格 = 2026-11-20 厂商控制台模型页
* 已有人工定的价   → **不覆盖**(另一行照补)
* 别的豆包行       → 不动(0187 种的 ``doubao-seed-2-1-pro-260915`` 原样)
* downgrade        → 只删价格与种的一字不差的行;人工改过价的留着
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
_DB = "migration_0196_test"
_BEFORE = "0195_rate_card_qwen38_cache"
_REVISION = "0196_rate_card_doubao_seed21"

_TURBO = "doubao/doubao-seed-2-1-turbo-260628"
_LITE = "doubao/doubao-seed-2-1-lite-260915"
#: (input, output, cache creation, cache read) — micro-元 per million tokens.
_SEEDED = {
    _TURBO: (3_000_000, 15_000_000, 0, 600_000),
    _LITE: (800_000, 2_700_000, 0, 160_000),
}
#: 0187 seeded this row; 0196 must leave it as it is.
_PRO = ("doubao/doubao-seed-2-1-pro-260915", (6_000_000, 30_000_000, 0, 1_200_000))
_MODELS = (
    "doubao-seed-2-1-turbo-260628",
    "doubao-seed-2-1-lite-260915",
    "doubao-seed-2-1-pro-260915",
)


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
    """A database of its own, migrated only as far as the version *before* 0196."""
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


def _seed_price(dsn: str, provider: str, model: str, price: tuple[int, int, int, int]) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO model_rate_card (tenant_id, provider, model, input_per_mtok_micros,
                                         output_per_mtok_micros, cache_creation_per_mtok_micros,
                                         cache_read_per_mtok_micros)
            VALUES (NULL, %s, %s, %s, %s, %s, %s)
            """,
            (provider, model, *price),
        )


def _prices(dsn: str) -> dict[str, tuple[int, int, int, int]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            """
            SELECT provider || '/' || model, input_per_mtok_micros, output_per_mtok_micros,
                   cache_creation_per_mtok_micros, cache_read_per_mtok_micros
              FROM model_rate_card
             WHERE tenant_id IS NULL AND model = ANY(%s)
            """,
            (list(_MODELS),),
        ).fetchall()
    return {row[0]: tuple(row[1:]) for row in rows}


def test_adds_the_console_prices_and_leaves_other_rows(fresh_db: str) -> None:
    assert _prices(fresh_db) == dict([_PRO])
    command.upgrade(_alembic(fresh_db), _REVISION)
    assert _prices(fresh_db) == {**_SEEDED, _PRO[0]: _PRO[1]}


def test_keeps_a_price_already_set(fresh_db: str) -> None:
    manual = (2_500_000, 12_000_000, 0, 500_000)
    _seed_price(fresh_db, "doubao", "doubao-seed-2-1-turbo-260628", manual)
    command.upgrade(_alembic(fresh_db), _REVISION)
    assert _prices(fresh_db) == {_TURBO: manual, _LITE: _SEEDED[_LITE], _PRO[0]: _PRO[1]}


def test_downgrade_removes_only_the_rows_it_seeded(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), _REVISION)
    command.downgrade(_alembic(fresh_db), _BEFORE)
    assert _prices(fresh_db) == dict([_PRO])


def test_downgrade_keeps_a_row_repriced_by_hand(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), _REVISION)
    # 运维在控制台把 lite 改了价:降级不能删它,turbo 照删。
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        conn.execute(
            """
            UPDATE model_rate_card SET output_per_mtok_micros = 2500000
             WHERE tenant_id IS NULL AND provider = 'doubao'
               AND model = 'doubao-seed-2-1-lite-260915'
            """
        )
    command.downgrade(_alembic(fresh_db), _BEFORE)
    assert _prices(fresh_db) == {
        _LITE: (800_000, 2_500_000, 0, 160_000),
        _PRO[0]: _PRO[1],
    }
