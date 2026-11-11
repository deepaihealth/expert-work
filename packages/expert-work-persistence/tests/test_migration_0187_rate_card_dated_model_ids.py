"""迁移 0187 —— 价目表补 ``qwen3.8-max-0902`` 与 ``doubao-seed-2-1-pro-260915``。

同 0166 / 0169 的测法:在**补之前**的版本(0186)上灌数据,再升到 head。在 head 上灌数据再
断言测不出东西:那条 INSERT 早就跑过了。用容器里另建的库(共享库会被别的测试升 / 降级)。

* 没有价目行       → 两行补上,价格 = 2026-11-11 厂商控制台页面
* 已有人工定的价   → **不覆盖**
* 旧名             → 不插、不改
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
_DB = "migration_0187_test"
_BEFORE = "0186_b282_conv_dir_expiry"
_REVISION = "0187_rate_card_dated_ids"

_QWEN = "qwen/qwen3.8-max-0902"
_DOUBAO = "doubao/doubao-seed-2-1-pro-260915"
#: (input, output, cache creation, cache read) — micro-元 per million tokens.
_SEEDED = {
    _QWEN: (12_000_000, 36_000_000, 0, 1_500_000),
    _DOUBAO: (6_000_000, 30_000_000, 0, 1_200_000),
}
_MODELS = (
    "qwen3.8-max-0902",
    "qwen3.8-max",
    "doubao-seed-2-1-pro-260915",
    "doubao-seed-2-1-pro-260628",
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
    """A database of its own, migrated only as far as the version *before* 0187."""
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


def test_adds_the_console_prices_and_nothing_for_the_old_names(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), _REVISION)
    assert _prices(fresh_db) == _SEEDED


def test_keeps_prices_already_set(fresh_db: str) -> None:
    manual = (11_000_000, 33_000_000, 0, 1_000_000)
    old = (10_000_000, 30_000_000, 0, 0)
    _seed_price(fresh_db, "qwen", "qwen3.8-max-0902", manual)
    _seed_price(fresh_db, "qwen", "qwen3.8-max", old)
    command.upgrade(_alembic(fresh_db), _REVISION)
    assert _prices(fresh_db) == {
        _QWEN: manual,
        "qwen/qwen3.8-max": old,
        _DOUBAO: _SEEDED[_DOUBAO],
    }


def test_downgrade_removes_only_the_rows_it_seeded(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), _REVISION)
    # 运维在控制台把豆包那行改了价:降级不能删它。
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        conn.execute(
            """
            UPDATE model_rate_card SET output_per_mtok_micros = 28000000
             WHERE tenant_id IS NULL AND model = 'doubao-seed-2-1-pro-260915'
            """
        )
    command.downgrade(_alembic(fresh_db), _BEFORE)
    assert _prices(fresh_db) == {_DOUBAO: (6_000_000, 28_000_000, 0, 1_200_000)}
