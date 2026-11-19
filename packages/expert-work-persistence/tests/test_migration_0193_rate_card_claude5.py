"""迁移 0193 —— 价目表补 Claude 5 四个型号(B-320)。

同 0192 的测法:在**补之前**的版本(0192)上灌数据,再升到 0193。用容器里另建的库(共享库会被别的
测试升 / 降级)。

* 没有价目行       → 补上,价格 = Anthropic 美元标价乘迁移里的汇率(元)
* 已有人工定的价   → **不覆盖**
* 别的行           → 不动(0192 种的 ``qwen4-max`` 原样;旧 Claude 型号迁移不碰)
* downgrade        → 只删价格与种的一字不差的行;人工改过价的留着
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_MIGRATION = (
    Path(__file__).resolve().parent.parent / "migrations/versions/0193_rate_card_claude5.py"
)
_DB = "migration_0193_test"
_BEFORE = "0192_rate_card_qwen4_max"
_REVISION = "0193_rate_card_claude5"

#: Anthropic 官方标价(美元 / 百万 token):输入、输出、5 分钟缓存写入、缓存命中。
#: Haiku 5.5 是提示 ≤ 100K token 的那一档(价目表一行一个价,装不下分档)。
_USD = {
    "claude-fable-5-2": ("8", "40", "10", "0.20"),
    "claude-opus-5-5": ("4", "20", "5", "0.20"),
    "claude-sonnet-5-5": ("2", "10", "2.50", "0.10"),
    "claude-haiku-5-5": ("0.10", "0.50", "0.125", "0.01"),
}
_QWEN4 = ("qwen/qwen4-max", (10_000_000, 30_000_000, 0, 500_000))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0193", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _expected() -> dict[str, tuple[int, ...]]:
    rate = _load_migration()._CNY_PER_USD
    return {
        f"anthropic/{model}": tuple(int(Decimal(usd) * rate * 1_000_000) for usd in prices)
        for model, prices in _USD.items()
    }


def test_seeded_prices_are_the_usd_list_prices_times_the_one_rate() -> None:
    """价格只从两样东西算出来:官方美元标价 + 迁移里唯一的汇率常量。整数 micro-元,没有舍入。"""
    module = _load_migration()
    # 2026-11-17 人民币汇率中间价(用户 2026-11-19 拍板写死)。
    assert module._CNY_PER_USD == Decimal("6.7132")
    seeded = {f"{p}/{m}": (i, o, c, r) for p, m, i, o, c, r in module._PRICES}
    assert seeded == _expected()
    for prices in module._PRICES:
        assert all(isinstance(v, int) and v > 0 for v in prices[2:])


# --- 真 Postgres(testcontainers)-------------------------------------------------


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
    """A database of its own, migrated only as far as the version *before* 0193."""
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


def _prices(dsn: str) -> dict[str, tuple[int, ...]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            """
            SELECT provider || '/' || model, input_per_mtok_micros, output_per_mtok_micros,
                   cache_creation_per_mtok_micros, cache_read_per_mtok_micros
              FROM model_rate_card
             WHERE tenant_id IS NULL AND (provider = 'anthropic' OR model = 'qwen4-max')
            """
        ).fetchall()
    return {row[0]: tuple(row[1:]) for row in rows}


@pytest.mark.integration
def test_adds_the_four_prices_and_leaves_other_rows(fresh_db: str) -> None:
    assert _prices(fresh_db) == dict([_QWEN4])
    command.upgrade(_alembic(fresh_db), _REVISION)
    assert _prices(fresh_db) == {**_expected(), _QWEN4[0]: _QWEN4[1]}


@pytest.mark.integration
def test_keeps_a_price_already_set(fresh_db: str) -> None:
    manual = (50_000_000, 250_000_000, 60_000_000, 1_000_000)
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO model_rate_card (tenant_id, provider, model, input_per_mtok_micros,
                                         output_per_mtok_micros, cache_creation_per_mtok_micros,
                                         cache_read_per_mtok_micros)
            VALUES (NULL, 'anthropic', 'claude-opus-5-5', %s, %s, %s, %s)
            """,
            manual,
        )
    command.upgrade(_alembic(fresh_db), _REVISION)
    assert _prices(fresh_db)["anthropic/claude-opus-5-5"] == manual


@pytest.mark.integration
def test_downgrade_removes_only_the_rows_it_seeded(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), _REVISION)
    # 运维在控制台把一行改了价:降级不能删它。
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        conn.execute(
            """
            UPDATE model_rate_card SET output_per_mtok_micros = 1
             WHERE tenant_id IS NULL AND provider = 'anthropic' AND model = 'claude-haiku-5-5'
            """
        )
    command.downgrade(_alembic(fresh_db), _BEFORE)
    haiku = _expected()["anthropic/claude-haiku-5-5"]
    assert _prices(fresh_db) == {
        "anthropic/claude-haiku-5-5": (haiku[0], 1, haiku[2], haiku[3]),
        _QWEN4[0]: _QWEN4[1],
    }
