"""Integration: 0131 grants ``retention_cleanup_worker`` sweep access.

删除接口卫生修复第 1 批 Task 6: 0131 补齐本批新增清扫表(``memory_item`` /
``user_workspace`` / ``tenant_user``)的 SELECT+DELETE 授权。照
``test_sql_app_user_role.py`` 的 ``has_table_privilege`` 断言先例写。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

pytestmark = pytest.mark.integration
ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _sync_dsn(c: PostgresContainer) -> str:
    url = str(c.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.mark.parametrize(
    "table_name", ["memory_item", "user_workspace", "tenant_user", "memory_writeback_job"]
)
def test_retention_cleanup_worker_has_sweep_grants(
    postgres_container: PostgresContainer, table_name: str
) -> None:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    engine = create_engine(_sync_dsn(postgres_container), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            has_select = conn.execute(
                text("SELECT has_table_privilege('retention_cleanup_worker', :t, 'SELECT')"),
                {"t": table_name},
            ).scalar()
            has_delete = conn.execute(
                text("SELECT has_table_privilege('retention_cleanup_worker', :t, 'DELETE')"),
                {"t": table_name},
            ).scalar()
            assert has_select is True, f"retention_cleanup_worker missing SELECT on {table_name}"
            assert has_delete is True, f"retention_cleanup_worker missing DELETE on {table_name}"
    finally:
        engine.dispose()
