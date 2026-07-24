"""Integration: 0134 存量孤儿 trigger_run / webhook_delivery 清理.

删除接口卫生修复第 3 批 Task 5:``trigger_run.trigger_id`` 与
``webhook_delivery.endpoint_id`` 历史上无 FK、删父也不级联(端点接线由
PR3 Task 4 负责),存量孤儿行需一次性迁移清理 —— 0134 DELETE 所有指向
不存在 ``agent_trigger`` / ``webhook_endpoint`` 的子行。

测试策略说明(同 ``test_role_binding_orphan_cleanup.py``):
``postgres_container`` 是 session 级共享容器,无法保证本文件是第一个把
容器迁到 head 的测试 —— 0134 是一次性数据清理迁移,若容器早已在空数据
上跑过它,之后插入的孤儿永远不会被清理,"迁到中间版本再 upgrade" 的写法
是脆的。故先全量 upgrade 到 head 保证 schema 就绪,手工插入孤儿数据,再
直接执行迁移里的 DELETE SQL 本体做等价断言。

与 0132 测试的差别:DELETE SQL 不在测试里复制一份,而是经 alembic
``ScriptDirectory`` 加载 0134 迁移模块、直接执行其模块级常量 —— 单一
事实源,迁移文件里的 SQL 变异(见下)测试必然看得见,也不存在两处文本
漂移的可能。

变异自验(手工执行,未固化进 CI):把 0134 迁移文件里两条 DELETE 的
``NOT EXISTS`` 改成 ``EXISTS`` 重跑 —— 测试变红("有主"行被误删,
``live_run_id`` / ``live_delivery_id`` 的保留断言失败);改回后复绿。
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

_REVISION = "0134_orphan_run_delivery_cleanup"


def _sync_dsn(c: PostgresContainer) -> str:
    url = str(c.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def test_orphan_trigger_run_and_webhook_delivery_removed_parented_kept(
    postgres_container: PostgresContainer,
) -> None:
    """孤儿 trigger_run / webhook_delivery 被删;有主行保留."""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    # RED gate: raises if migration 0134 does not exist yet.
    migration = ScriptDirectory.from_config(cfg).get_revision(_REVISION).module

    tenant_id = uuid4()
    trigger_id = uuid4()
    endpoint_id = uuid4()
    live_run_id = uuid4()
    orphan_run_id = uuid4()
    live_delivery_id = uuid4()
    orphan_delivery_id = uuid4()

    engine = create_engine(_sync_dsn(postgres_container), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            # Live parent trigger + its trigger_run — must survive.
            conn.execute(
                text(
                    "INSERT INTO agent_trigger "
                    "(id, tenant_id, agent_name, agent_version, name, kind, source,"
                    " created_at, updated_at) "
                    "VALUES (:id, :tid, 'test-agent', '1', :name, 'cron', 'api', now(), now())"
                ),
                {"id": trigger_id, "tid": tenant_id, "name": f"trig-{uuid4().hex[:8]}"},
            )
            conn.execute(
                text(
                    "INSERT INTO trigger_run (id, tenant_id, trigger_id, triggered_at) "
                    "VALUES (:id, :tid, :trig, now())"
                ),
                {"id": live_run_id, "tid": tenant_id, "trig": trigger_id},
            )
            # Orphan trigger_run — points at a trigger that never existed.
            conn.execute(
                text(
                    "INSERT INTO trigger_run (id, tenant_id, trigger_id, triggered_at) "
                    "VALUES (:id, :tid, :trig, now())"
                ),
                {"id": orphan_run_id, "tid": tenant_id, "trig": uuid4()},
            )
            # Live endpoint + its webhook_delivery — must survive.
            conn.execute(
                text(
                    "INSERT INTO webhook_endpoint "
                    "(id, tenant_id, name, url, created_at, updated_at) "
                    "VALUES (:id, :tid, :name, 'https://example.com/hook', now(), now())"
                ),
                {"id": endpoint_id, "tid": tenant_id, "name": f"ep-{uuid4().hex[:8]}"},
            )
            conn.execute(
                text(
                    "INSERT INTO webhook_delivery "
                    "(id, tenant_id, endpoint_id, event_id, event_type, created_at, updated_at) "
                    "VALUES (:id, :tid, :ep, :eid, 'run.completed', now(), now())"
                ),
                {
                    "id": live_delivery_id,
                    "tid": tenant_id,
                    "ep": endpoint_id,
                    "eid": f"evt-{uuid4().hex}",
                },
            )
            # Orphan webhook_delivery — points at an endpoint that never existed.
            conn.execute(
                text(
                    "INSERT INTO webhook_delivery "
                    "(id, tenant_id, endpoint_id, event_id, event_type, created_at, updated_at) "
                    "VALUES (:id, :tid, :ep, :eid, 'run.completed', now(), now())"
                ),
                {
                    "id": orphan_delivery_id,
                    "tid": tenant_id,
                    "ep": uuid4(),
                    "eid": f"evt-{uuid4().hex}",
                },
            )

            # Execute the migration's DELETE bodies — single source of truth.
            conn.execute(text(migration._DELETE_ORPHAN_TRIGGER_RUN_SQL))
            conn.execute(text(migration._DELETE_ORPHAN_WEBHOOK_DELIVERY_SQL))

            remaining_runs = {
                row[0]
                for row in conn.execute(
                    text("SELECT id FROM trigger_run WHERE id = ANY(:ids)"),
                    {"ids": [live_run_id, orphan_run_id]},
                )
            }
            remaining_deliveries = {
                row[0]
                for row in conn.execute(
                    text("SELECT id FROM webhook_delivery WHERE id = ANY(:ids)"),
                    {"ids": [live_delivery_id, orphan_delivery_id]},
                )
            }
    finally:
        engine.dispose()

    assert orphan_run_id not in remaining_runs, "orphan trigger_run survived cleanup"
    assert live_run_id in remaining_runs, "parented trigger_run was wrongly deleted"
    assert orphan_delivery_id not in remaining_deliveries, (
        "orphan webhook_delivery survived cleanup"
    )
    assert live_delivery_id in remaining_deliveries, "parented webhook_delivery was wrongly deleted"
