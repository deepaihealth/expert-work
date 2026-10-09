"""B-168 B2 —— 写回节点在 run 里落任务行,真 Postgres 的租户 RLS ``WITH CHECK`` 放行。

``memory_writeback_job`` 是 FORCE RLS,``WITH CHECK (tenant_id = app.tenant_id)``(迁移
0161)。run 的入口不止一个,不是每个都设好了租户上下文;节点自己带上本 run 的租户。

testcontainers 的引导用户是超级用户、绕过一切策略,所以这里另建一个普通的 LOGIN 角色连库
(同 ``test_feedback_store_delete.py``)。对照组:不带租户上下文直接插,被策略拒掉 ——
证明策略在这个连接上是生效的,节点能插进去靠的是它自己设的作用域。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    InMemoryMemoryStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.memory import SqlMemoryWritebackJobStore
from expert_work.persistence.rls import build_rls_sessionmaker, current_tenant_id_var
from orchestrator import make_memory_writeback_node
from orchestrator.llm import FakeEmbedder

pytestmark = pytest.mark.integration

ALEMBIC_INI = (
    Path(__file__).resolve().parents[3] / "packages" / "expert-work-persistence" / "alembic.ini"
)
APP_ROLE = "expert_work_app_writeback_job"
APP_PASSWORD = "expert_work_app_writeback_job_pw"  # test-only fixture password


def _dsn(container: PostgresContainer, driver: str) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", f"+{driver}").replace(
        "postgresql://", f"postgresql+{driver}://", 1
    )


def _as_app_role(dsn: str) -> str:
    parsed = urlparse(dsn)
    netloc = f"{APP_ROLE}:{APP_PASSWORD}@{parsed.hostname}"
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _provision_app_role(sync_dsn: str) -> None:
    admin = create_engine(sync_dsn, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": APP_ROLE}
            ).first()
            if exists is None:
                # Module-level constants under our control — safe to interpolate.
                conn.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}'"))
            conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
            conn.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE "
                    f"ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
                )
            )
    finally:
        admin.dispose()


@pytest.fixture
async def app_role_jobs(
    postgres_container: PostgresContainer,
) -> AsyncIterator[SqlMemoryWritebackJobStore]:
    sync_dsn = _dsn(postgres_container, "psycopg")
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", sync_dsn)
    command.upgrade(cfg, "head")
    _provision_app_role(sync_dsn)
    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=_as_app_role(_dsn(postgres_container, "asyncpg")))
    )
    try:
        yield SqlMemoryWritebackJobStore(
            build_rls_sessionmaker(create_async_session_factory(engine))
        )
    finally:
        await engine.dispose()


async def _never_called(**_: Any) -> AIMessage:
    raise AssertionError("background mode must not call the memory model in the run")


@pytest.mark.asyncio
async def test_node_enqueue_passes_the_tenant_with_check_without_ambient_scope(
    app_role_jobs: SqlMemoryWritebackJobStore,
) -> None:
    tenant, user, run = uuid4(), uuid4(), uuid4()
    assert current_tenant_id_var.get() is None  # no entry point set a tenant

    # Control: the policy is live on this connection — an unscoped insert is refused.
    with pytest.raises(DBAPIError):
        await app_role_jobs.enqueue(
            tenant_id=tenant,
            user_id=user,
            agent_name="mem-agent",
            agent_version="1.0.0",
            thread_id=uuid4(),
            run_id=uuid4(),
            trace_id=None,
            message_count=1,
            now=datetime.now(UTC),
        )

    node = make_memory_writeback_node(
        memory_store=InMemoryMemoryStore(),
        embedder=FakeEmbedder(dim=8),
        llm_caller=_never_called,
        agent_name="mem-agent",
        agent_version="1.0.0",
        writeback_jobs=app_role_jobs,
    )
    out = await node(
        {  # type: ignore[arg-type]
            "messages": [SystemMessage(content="s"), HumanMessage(content="hi")],
            "step_count": 0,
            "max_steps": 5,
        },
        {
            "configurable": {
                "tenant_id": str(tenant),
                "user_id": str(user),
                "thread_id": str(uuid4()),
                "run_id": str(run),
                "checkpoint_map": {"": "ckpt-1"},
            }
        },
    )

    assert out["memory_writeback_queued"] is True
    token = current_tenant_id_var.set(tenant)
    try:
        job = await app_role_jobs.get_by_run(tenant_id=tenant, run_id=run)
    finally:
        current_tenant_id_var.reset(token)
    assert job is not None
    assert (job.checkpoint_id, job.message_count) == ("ckpt-1", 2)
