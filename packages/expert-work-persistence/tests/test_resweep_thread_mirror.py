"""迁移 0157 —— 清空镜像水位逼 sweep 重扫(B-73 ①)。

在**清空之前**的那个版本(0156)上灌数据,再升到 head。在 head 上灌数据再断言
是测不出东西的:那条 DELETE 早就跑过了。

两件事必须一起断言,少一件这条测试就不成立:

* 水位行**没了** —— 这是这条迁移存在的理由(没有水位行 = 排进 backfill 队列,
  见 ``test_sql_thread_message_store.py::test_pending_selection_backfill_and_activity_sql``)
* 镜像行**一行不少、内容不变** —— 「让搜索干净」也可以靠删 ``thread_message`` 做到,
  那会把审计要看的忠实记录一起删掉。这条断言是两种改法的分水岭。

用容器里另建一个库,而不是共享那个 session 级容器的默认库 —— 别的测试会把它
升到 head,之后再 ``upgrade 0156`` 就是降级,拿不到「清空前」的状态。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "thread_mirror_resweep_test"
_BEFORE = "0156_thread_message_hidden"


def _sync_dsn(container: PostgresContainer, database: str | None = None) -> str:
    url = str(container.get_connection_url()).replace("+psycopg2", "")
    if database is None:
        return url
    base, _, _ = url.rpartition("/")
    return f"{base}/{database}"


@pytest.fixture
def fresh_db(postgres_container: PostgresContainer) -> Iterator[str]:
    """A database of its own, migrated only as far as the version *before*
    the re-sweep."""
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


def _seed_mirrored_thread(conn: psycopg.Connection, *, tenant: UUID) -> UUID:
    """One thread with two mirrored rows and a watermark that says 'all synced'."""
    thread_id = uuid4()
    now = datetime.now(UTC)
    conn.execute(
        """
        INSERT INTO thread_meta (thread_id, tenant_id, created_by, status,
                                 created_at, updated_at)
        VALUES (%s, %s, 'probe', 'active', %s, %s)
        """,
        (thread_id, tenant, now, now),
    )
    for seq, (role, content) in enumerate(
        [("user", "帮我排一下这周的随访"), ("assistant", "<recovery-advisory>工具失败了</recovery-advisory>")]
    ):
        conn.execute(
            """
            INSERT INTO thread_message (thread_id, seq, tenant_id, role, content, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (thread_id, seq, tenant, role, content, now),
        )
    conn.execute(
        """
        INSERT INTO thread_message_sync (thread_id, tenant_id, synced_at, message_count)
        VALUES (%s, %s, %s, 2)
        """,
        (thread_id, tenant, now),
    )
    return thread_id


def _upgrade_to_head(dsn: str) -> None:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    command.upgrade(cfg, "head")


def test_resweep_drops_watermarks_and_keeps_the_mirror(fresh_db: str) -> None:
    tenant = uuid4()
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        thread_a = _seed_mirrored_thread(conn, tenant=tenant)
        thread_b = _seed_mirrored_thread(conn, tenant=tenant)

    _upgrade_to_head(fresh_db)

    with psycopg.connect(fresh_db) as conn:
        watermarks = conn.execute(
            "SELECT count(*) FROM thread_message_sync WHERE thread_id = ANY(%s)",
            ([thread_a, thread_b],),
        ).fetchone()
        mirrored = conn.execute(
            """
            SELECT thread_id, seq, content FROM thread_message
            WHERE thread_id = ANY(%s) ORDER BY thread_id, seq
            """,
            ([thread_a, thread_b],),
        ).fetchall()

    assert watermarks is not None
    assert watermarks[0] == 0, "水位行还在 —— 这两条线程不会被 sweep 重选,hidden 永远停在默认值"
    assert len(mirrored) == 4, "镜像行被删了 —— 审计要看的忠实记录不能拿来换一个干净的搜索"
    assert [row[2] for row in mirrored].count(
        "<recovery-advisory>工具失败了</recovery-advisory>"
    ) == 2, "镜像内容被改写了"
