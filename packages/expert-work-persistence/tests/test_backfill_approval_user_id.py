"""迁移 0151 —— agent_approval.user_id 存量回填(X-15 ②)。

在**回填之前**的那个版本(0150)上灌数据,再升到 head,看回填有没有真的发生。
在 head 上灌数据再断言是测不出东西的:那条 UPDATE 早就跑过了。

三种行必须区分开,少一种这条测试就只证明了「UPDATE 语句能执行」:

* 审批单无主 + run 有主   → 回填(这是这条迁移存在的理由)
* 审批单无主 + run 也无主 → 保持 NULL(没有可回填的值,不能编一个)
* 审批单已有主           → **不覆盖**(写入侧修好之后的新行,值比 run 上的更准)

用容器里另建一个库,而不是共享那个 session 级容器的默认库 —— 别的测试会把它
升到 head,之后再 ``upgrade 0150`` 就是降级,拿不到「回填前」的状态。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "approval_backfill_test"
_BEFORE = "0150_agent_spec_draft"


def _sync_dsn(container: PostgresContainer, database: str | None = None) -> str:
    url = str(container.get_connection_url()).replace("+psycopg2", "")
    if database is None:
        return url
    base, _, _ = url.rpartition("/")
    return f"{base}/{database}"


@pytest.fixture
def fresh_db(postgres_container: PostgresContainer) -> Iterator[str]:
    """A database of its own, migrated only as far as the version *before*
    the backfill."""
    admin = _sync_dsn(postgres_container)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_DB}"')
        conn.execute(f'CREATE DATABASE "{_DB}"')

    dsn = _sync_dsn(postgres_container, _DB)
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    command.upgrade(cfg, _BEFORE)
    try:
        yield dsn
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')


def _seed_run(
    conn: psycopg.Connection, *, run_id: UUID, tenant: UUID, user_id: UUID | None
) -> UUID:
    thread_id = uuid4()
    now = datetime.now(UTC)
    conn.execute(
        """
        INSERT INTO agent_run (id, tenant_id, user_id, thread_id, status,
                               on_disconnect, created_at, updated_at)
        VALUES (%s, %s, %s, %s, 'paused', 'continue', %s, %s)
        """,
        (run_id, tenant, user_id, thread_id, now, now),
    )
    return thread_id


def _seed_approval(
    conn: psycopg.Connection,
    *,
    tenant: UUID,
    run_id: UUID,
    thread_id: UUID,
    user_id: UUID | None,
) -> UUID:
    approval_id = uuid4()
    now = datetime.now(UTC)
    conn.execute(
        """
        INSERT INTO agent_approval (id, tenant_id, user_id, run_id, thread_id,
                                    request_id, node, reason_kind, action_summary,
                                    proposed_args, requested_at, timeout_at)
        VALUES (%s, %s, %s, %s, %s, %s, 'tools', 'policy_gate', 'summary',
                '{}'::jsonb, %s, %s)
        """,
        (
            approval_id,
            tenant,
            user_id,
            run_id,
            thread_id,
            f"approval:{approval_id}",
            now,
            now + timedelta(hours=1),
        ),
    )
    return approval_id


def _upgrade_to_head(dsn: str) -> None:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    command.upgrade(cfg, "head")


def test_backfill_fills_only_the_ownerless_rows_it_can(fresh_db: str) -> None:
    tenant = uuid4()
    owner = uuid4()
    other_owner = uuid4()

    with psycopg.connect(fresh_db, autocommit=True) as conn:
        # ① 无主审批单,run 有主 —— 该被回填
        run_a = uuid4()
        thread_a = _seed_run(conn, run_id=run_a, tenant=tenant, user_id=owner)
        appr_a = _seed_approval(conn, tenant=tenant, run_id=run_a, thread_id=thread_a, user_id=None)

        # ② 无主审批单,run 也无主 —— 无值可填,保持 NULL
        run_b = uuid4()
        thread_b = _seed_run(conn, run_id=run_b, tenant=tenant, user_id=None)
        appr_b = _seed_approval(conn, tenant=tenant, run_id=run_b, thread_id=thread_b, user_id=None)

        # ③ 审批单已有主,且与 run 上的不同 —— 不许覆盖
        run_c = uuid4()
        thread_c = _seed_run(conn, run_id=run_c, tenant=tenant, user_id=owner)
        appr_c = _seed_approval(
            conn, tenant=tenant, run_id=run_c, thread_id=thread_c, user_id=other_owner
        )

    _upgrade_to_head(fresh_db)

    with psycopg.connect(fresh_db) as conn:
        got = dict(
            conn.execute(
                "SELECT id, user_id FROM agent_approval WHERE id = ANY(%s)",
                ([appr_a, appr_b, appr_c],),
            ).fetchall()
        )

    assert got[appr_a] == owner, "无主审批单没有从它的 run 拿回所有者"
    assert got[appr_b] is None, "run 自己都无主,不该编一个所有者出来"
    assert got[appr_c] == other_owner, "已有所有者的行被覆盖了"
