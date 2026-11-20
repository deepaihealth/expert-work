"""迁移 0195 —— 价目表 ``qwen / qwen3.8-max`` 的缓存命中价 2.4 → 厂商标价 1.5。

同 0192 的测法:在改之前的版本(0194)上灌数据,再升到 0195;用容器里另建的库。

* 手工填的错价 2.4      → 改成 1.5,别的列不动
* 人工按别的口径改过的价 → **不覆盖**
* 没有这一行             → 不插
* 带日期名 0902          → 不动
* downgrade             → 只把 1.5 的这一行改回 2.4
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
_DB = "migration_0195_test"
_BEFORE = "0194_b326_artifact_share_links"
_REVISION = "0195_rate_card_qwen38_cache"

_MAX = "qwen/qwen3.8-max"
#: (input, output, cache creation, cache read) — micro-元 per million tokens.
_WRONG = (12_000_000, 36_000_000, 0, 2_400_000)
_FIXED = (12_000_000, 36_000_000, 0, 1_500_000)
#: 0187 seeded this row (already 1.5); 0195 must leave it as it is.
_DATED = ("qwen/qwen3.8-max-0902", (12_000_000, 36_000_000, 0, 1_500_000))
_MODELS = ("qwen3.8-max", "qwen3.8-max-0902")


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
    """A database of its own, migrated only as far as the version *before* 0195."""
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


def _with_wrong_row(dsn: str) -> None:
    _seed_price(dsn, "qwen", "qwen3.8-max", _WRONG)


def test_fixes_the_hand_entered_cache_read_price(fresh_db: str) -> None:
    _with_wrong_row(fresh_db)
    assert _prices(fresh_db) == {_MAX: _WRONG, _DATED[0]: _DATED[1]}
    command.upgrade(_alembic(fresh_db), _REVISION)
    assert _prices(fresh_db) == {_MAX: _FIXED, _DATED[0]: _DATED[1]}


def test_keeps_a_price_set_another_way(fresh_db: str) -> None:
    manual = (13_000_000, 36_000_000, 0, 2_400_000)
    _seed_price(fresh_db, "qwen", "qwen3.8-max", manual)
    command.upgrade(_alembic(fresh_db), _REVISION)
    assert _prices(fresh_db) == {_MAX: manual, _DATED[0]: _DATED[1]}


def test_does_not_insert_a_missing_row(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), _REVISION)
    assert _prices(fresh_db) == dict([_DATED])


def test_downgrade_restores_the_previous_price(fresh_db: str) -> None:
    _with_wrong_row(fresh_db)
    command.upgrade(_alembic(fresh_db), _REVISION)
    command.downgrade(_alembic(fresh_db), _BEFORE)
    assert _prices(fresh_db) == {_MAX: _WRONG, _DATED[0]: _DATED[1]}


def test_keeps_a_different_cache_read_price(fresh_db: str) -> None:
    manual = (12_000_000, 36_000_000, 0, 2_000_000)
    _seed_price(fresh_db, "qwen", "qwen3.8-max", manual)
    command.upgrade(_alembic(fresh_db), _REVISION)
    assert _prices(fresh_db) == {_MAX: manual, _DATED[0]: _DATED[1]}


@pytest.mark.parametrize(
    "manual",
    [(12_000_000, 36_000_000, 0, 1_200_000), (13_000_000, 36_000_000, 0, 1_500_000)],
)
def test_downgrade_keeps_a_price_set_another_way(
    fresh_db: str, manual: tuple[int, int, int, int]
) -> None:
    _seed_price(fresh_db, "qwen", "qwen3.8-max", manual)
    command.upgrade(_alembic(fresh_db), _REVISION)
    command.downgrade(_alembic(fresh_db), _BEFORE)
    assert _prices(fresh_db) == {_MAX: manual, _DATED[0]: _DATED[1]}


def _updated_at(dsn: str) -> object:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            """
            SELECT updated_at FROM model_rate_card
             WHERE tenant_id IS NULL AND provider = 'qwen' AND model = 'qwen3.8-max'
            """
        ).fetchone()
    assert row is not None
    return row[0]


def test_the_fix_stamps_updated_at(fresh_db: str) -> None:
    _with_wrong_row(fresh_db)
    before = _updated_at(fresh_db)
    command.upgrade(_alembic(fresh_db), _REVISION)
    assert _updated_at(fresh_db) > before  # type: ignore[operator]
