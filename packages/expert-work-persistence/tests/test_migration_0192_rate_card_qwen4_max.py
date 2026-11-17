"""迁移 0192 —— 价目表补 ``qwen / qwen4-max``。

同 0187 的测法:在**补之前**的版本(0191)上灌数据,再升到 0192。在 head 上灌数据再断言测不出
东西:那条 INSERT 早就跑过了。用容器里另建的库(共享库会被别的测试升 / 降级)。

* 没有价目行       → 补上,价格 = 2026-11-17 厂商模型页
* 已有人工定的价   → **不覆盖**
* 别的通义行       → 不动(0187 种的 ``qwen3.8-max-0902`` 原样)
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
_DB = "migration_0192_test"
_BEFORE = "0191_b308_credential_base_url"
_REVISION = "0192_rate_card_qwen4_max"

_QWEN4 = "qwen/qwen4-max"
#: (input, output, cache creation, cache read) — micro-元 per million tokens.
_SEEDED = (10_000_000, 30_000_000, 0, 500_000)
#: 0187 seeded this row; 0192 must leave it as it is.
_DATED = ("qwen/qwen3.8-max-0902", (12_000_000, 36_000_000, 0, 1_500_000))
_MODELS = ("qwen4-max", "qwen3.8-max-0902")


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
    """A database of its own, migrated only as far as the version *before* 0192."""
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


def test_adds_the_vendor_page_price_and_leaves_other_rows(fresh_db: str) -> None:
    assert _prices(fresh_db) == dict([_DATED])
    command.upgrade(_alembic(fresh_db), _REVISION)
    assert _prices(fresh_db) == {_QWEN4: _SEEDED, _DATED[0]: _DATED[1]}


def test_keeps_a_price_already_set(fresh_db: str) -> None:
    manual = (9_000_000, 27_000_000, 0, 400_000)
    _seed_price(fresh_db, "qwen", "qwen4-max", manual)
    command.upgrade(_alembic(fresh_db), _REVISION)
    assert _prices(fresh_db) == {_QWEN4: manual, _DATED[0]: _DATED[1]}


def test_downgrade_removes_only_the_row_it_seeded(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), _REVISION)
    command.downgrade(_alembic(fresh_db), _BEFORE)
    assert _prices(fresh_db) == dict([_DATED])


def test_downgrade_keeps_a_row_repriced_by_hand(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), _REVISION)
    # 运维在控制台把这行改了价:降级不能删它。
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        conn.execute(
            """
            UPDATE model_rate_card SET output_per_mtok_micros = 28000000
             WHERE tenant_id IS NULL AND provider = 'qwen' AND model = 'qwen4-max'
            """
        )
    command.downgrade(_alembic(fresh_db), _BEFORE)
    assert _prices(fresh_db) == {
        _QWEN4: (10_000_000, 28_000_000, 0, 500_000),
        _DATED[0]: _DATED[1],
    }
