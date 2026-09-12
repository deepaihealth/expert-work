"""迁移 0154 —— artifact 加 agent_key、回填、唯一键换四元组(B-50 工作区分层)。

在**回填之前**那个版本(0153)上灌数据,再升到 head,看回填有没有真的发生。
在 head 上灌数据再断言测不出东西:那条 UPDATE 早就跑过了。

回填链**比字段名长一跳**:``artifact_version.created_in_thread`` 存的是
``run_id`` 不是 thread_id(``tools/artifact.py`` 写的是 ``str(ctx.run_id)``,
字段名与内容不符)。所以是
``created_in_thread`` → ``agent_run.id`` → ``agent_run.thread_id``
→ ``thread_meta.agent_name``。少一跳就永远填不上,而且不会报错 —— 只是
所有行都留空串,看起来像「这批数据推不出来」。

四种行必须分开造,少一种这条测试就只证明了「UPDATE 语句能执行」:

* 链完整                       → 回填成 ``sanitize_agent_key(agent_name)``
* ``created_in_thread`` 不是 UUID(回落常量 ``_FALLBACK_THREAD_ID``) → 留空串
* 是 UUID 但 ``agent_run`` 行已删                                    → 留空串
* 一条 artifact 被两个 agent 先后写过                                → 归**最早**那个

用容器里另建一个库,而不是共享那个 session 级容器的默认库 —— 别的测试会把它
升到 head,之后再 ``upgrade 0153`` 就是降级,拿不到「回填前」的状态。

本地跑前: export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
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

from expert_work.protocol.agent_key import sanitize_agent_key

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "artifact_agent_key_test"
_BEFORE = "0153_agent_run_supersede"


def _sync_dsn(container: PostgresContainer, database: str | None = None) -> str:
    url = str(container.get_connection_url()).replace("+psycopg2", "")
    if database is None:
        return url
    base, _, _ = url.rpartition("/")
    return f"{base}/{database}"


@pytest.fixture
def fresh_db(postgres_container: PostgresContainer) -> Iterator[str]:
    """A database of its own, migrated only as far as the version *before* 0154."""
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


def _upgrade_to_head(dsn: str) -> None:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    command.upgrade(cfg, "head")


def _seed_run(
    conn: psycopg.Connection, *, run_id: UUID, tenant: UUID, user_id: UUID, agent_name: str
) -> None:
    """一条完整的反推链:agent_run → thread_meta(带 agent_name)。"""
    thread_id = uuid4()
    now = datetime.now(UTC)
    conn.execute(
        """
        INSERT INTO thread_meta (thread_id, tenant_id, user_id, created_by, agent_name,
                                 agent_version, created_at, updated_at)
        VALUES (%s, %s, %s, 'migration-test', %s, '1.0.0', %s, %s)
        """,
        (thread_id, tenant, user_id, agent_name, now, now),
    )
    conn.execute(
        """
        INSERT INTO agent_run (id, tenant_id, user_id, thread_id, status,
                               on_disconnect, created_at, updated_at)
        VALUES (%s, %s, %s, %s, 'success', 'continue', %s, %s)
        """,
        (run_id, tenant, user_id, thread_id, now, now),
    )


def _seed_artifact(
    conn: psycopg.Connection, *, tenant: UUID, user_id: UUID, name: str, created_in_thread: str
) -> UUID:
    """0154 之前形状的一条 artifact + 它的 v1(那时还没有 agent_key 列)。"""
    artifact_id = uuid4()
    now = datetime.now(UTC)
    conn.execute(
        """
        INSERT INTO artifact (id, tenant_id, user_id, name, kind, latest_version,
                              created_at, updated_at)
        VALUES (%s, %s, %s, %s, 'document', 1, %s, %s)
        """,
        (artifact_id, tenant, user_id, name, now, now),
    )
    conn.execute(
        """
        INSERT INTO artifact_version (id, artifact_id, tenant_id, user_id, version,
                                      path_in_workspace, created_in_thread, created_at)
        VALUES (%s, %s, %s, %s, 1, %s, %s, %s)
        """,
        (uuid4(), artifact_id, tenant, user_id, name, created_in_thread, now),
    )
    return artifact_id


def _append_version(
    conn: psycopg.Connection,
    *,
    artifact_id: UUID,
    tenant: UUID,
    user_id: UUID,
    version: int,
    name: str,
    created_in_thread: str,
) -> None:
    """0154 之前 ON CONFLICT 合并的形态:第二个 agent 在同一条 artifact 上追加版本。"""
    conn.execute(
        """
        INSERT INTO artifact_version (id, artifact_id, tenant_id, user_id, version,
                                      path_in_workspace, created_in_thread, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            uuid4(),
            artifact_id,
            tenant,
            user_id,
            version,
            name,
            created_in_thread,
            datetime.now(UTC),
        ),
    )
    conn.execute("UPDATE artifact SET latest_version = %s WHERE id = %s", (version, artifact_id))


def _agent_keys(dsn: str, artifact_ids: list[UUID]) -> dict[UUID, str]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT id, agent_key FROM artifact WHERE id = ANY(%s)", (artifact_ids,)
        ).fetchall()
    return {row[0]: row[1] for row in rows}


def test_backfill_resolves_agent_through_run_then_thread(fresh_db: str) -> None:
    """链完整的行回填成真 key;两种断链的行留空串,不猜。"""
    tenant, user_id = uuid4(), uuid4()
    run_id = uuid4()

    with psycopg.connect(fresh_db, autocommit=True) as conn:
        _seed_run(conn, run_id=run_id, tenant=tenant, user_id=user_id, agent_name="ai-health-plan")
        # ① 链完整
        resolved = _seed_artifact(
            conn, tenant=tenant, user_id=user_id, name="报告.docx", created_in_thread=str(run_id)
        )
        # ② created_in_thread 不是 UUID —— SaveArtifactTool 的回落常量
        not_a_uuid = _seed_artifact(
            conn, tenant=tenant, user_id=user_id, name="a.docx", created_in_thread="artifact-tool"
        )
        # ③ 是 UUID 但 agent_run 行已经不在了
        run_gone = _seed_artifact(
            conn, tenant=tenant, user_id=user_id, name="b.docx", created_in_thread=str(uuid4())
        )

    _upgrade_to_head(fresh_db)

    got = _agent_keys(fresh_db, [resolved, not_a_uuid, run_gone])
    assert got[resolved] == sanitize_agent_key("ai-health-plan"), (
        "链完整的行没回填 —— 大概率漏了 created_in_thread → agent_run.id 这一跳"
    )
    assert got[not_a_uuid] == "", "created_in_thread 不是 UUID,不该编一个归属出来"
    assert got[run_gone] == "", "agent_run 行已删,不该编一个归属出来"


def test_backfill_picks_the_earliest_version_owner(fresh_db: str) -> None:
    """一条 artifact 被两个 agent 先后写过时,归**第一个**建它的。

    判给最后写的那个,会让历史上第一个 agent 的产物凭空改姓 —— 而且它在新
    四元组键下本来就会分出自己的行,没有任何理由把旧行让出去。
    """
    tenant, user_id = uuid4(), uuid4()
    plan_run, sop_run = uuid4(), uuid4()

    with psycopg.connect(fresh_db, autocommit=True) as conn:
        _seed_run(
            conn, run_id=plan_run, tenant=tenant, user_id=user_id, agent_name="ai-health-plan"
        )
        _seed_run(conn, run_id=sop_run, tenant=tenant, user_id=user_id, agent_name="sop2-designer")
        artifact_id = _seed_artifact(
            conn, tenant=tenant, user_id=user_id, name="报告.docx", created_in_thread=str(plan_run)
        )
        _append_version(
            conn,
            artifact_id=artifact_id,
            tenant=tenant,
            user_id=user_id,
            version=2,
            name="报告.docx",
            created_in_thread=str(sop_run),
        )

    _upgrade_to_head(fresh_db)

    assert _agent_keys(fresh_db, [artifact_id])[artifact_id] == sanitize_agent_key(
        "ai-health-plan"
    ), "归属判给了最后写的那个 agent —— 第一个 agent 的产物凭空改姓了"


def test_unique_key_is_now_four_columns(fresh_db: str) -> None:
    """核心不变式:同一用户下两个 agent 的同名产物,是两条独立行。

    旧三元组键会让第二条 INSERT 撞唯一约束(生产代码走 ON CONFLICT DO UPDATE
    把它们合并成一行,旧字节被覆盖 —— 这正是 B-50 要修的 bug)。
    """
    tenant, user_id = uuid4(), uuid4()
    _upgrade_to_head(fresh_db)

    now = datetime.now(UTC)
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        for key in ("ai-health-plan-1a2b3c4d", "sop2-designer-5e6f7a8b"):
            conn.execute(
                """
                INSERT INTO artifact (id, tenant_id, user_id, agent_key, name, kind,
                                      latest_version, created_at, updated_at)
                VALUES (%s, %s, %s, %s, '报告.docx', 'document', 1, %s, %s)
                """,
                (uuid4(), tenant, user_id, key, now, now),
            )
        count = conn.execute(
            "SELECT count(*) FROM artifact WHERE tenant_id = %s AND user_id = %s",
            (tenant, user_id),
        ).fetchone()
    assert count is not None and count[0] == 2, "两个 agent 的同名产物没分成两行"


def test_same_agent_same_name_still_collides(fresh_db: str) -> None:
    """收窄不能收过头:同一个 agent 的同名产物仍然撞唯一键(生产代码靠它走 upsert)。"""
    tenant, user_id = uuid4(), uuid4()
    _upgrade_to_head(fresh_db)

    now = datetime.now(UTC)
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO artifact (id, tenant_id, user_id, agent_key, name, kind,
                                  latest_version, created_at, updated_at)
            VALUES (%s, %s, %s, 'ai-health-plan-1a2b3c4d', '报告.docx', 'document', 1, %s, %s)
            """,
            (uuid4(), tenant, user_id, now, now),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                """
                INSERT INTO artifact (id, tenant_id, user_id, agent_key, name, kind,
                                      latest_version, created_at, updated_at)
                VALUES (%s, %s, %s, 'ai-health-plan-1a2b3c4d', '报告.docx', 'document', 1, %s, %s)
                """,
                (uuid4(), tenant, user_id, now, now),
            )
