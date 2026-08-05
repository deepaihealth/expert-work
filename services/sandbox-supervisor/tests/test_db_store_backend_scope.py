"""DbSandboxStore 的按后端限定谓词 —— 真 PG 集成测(PR-A Task 3)。

docker supervisor 与 AgentSandboxClient 共用 ``sandbox_instance`` 表;
supervisor 的两个集合查询原先不分后端:``list_idle_sessions`` 会把 E2B
热会话行交给 docker reaper(``docker stop`` 一个 E2B id、失败后销毁记账
把 agent 侧的行毁掉),``count_active_for_tenant`` 把 E2B 行算进 docker
配额。谓词 ``image_ref != AGENT_SANDBOX_IMAGE_REF``。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.models import SandboxInstanceRow
from expert_work.persistence.sandbox_instance_store import AGENT_SANDBOX_IMAGE_REF
from sandbox_supervisor.domain import SandboxRecord, SandboxState
from sandbox_supervisor.store import DbSandboxStore

pytestmark = pytest.mark.integration

ALEMBIC_INI = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "packages/expert-work-persistence/alembic.ini"
)


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
def store(postgres_container: PostgresContainer) -> Iterator[DbSandboxStore]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    factory = create_async_session_factory(engine)
    yield DbSandboxStore(factory)


async def _insert_docker_row(
    store: DbSandboxStore, *, tenant_id: UUID, last_used_at: datetime
) -> UUID:
    """插一条 docker-supervisor 真行 —— 走 ``DbSandboxStore.insert``,真实
    ``image_ref``/``node``,``last_used_at`` 拨旧到 idle 线外。"""
    row_id = uuid4()
    await store.insert(
        SandboxRecord(
            id=row_id,
            tenant_id=tenant_id,
            image_ref="registry.example.com/expert-work/sandbox:py312",
            node="dev-host-1",
            container_id="docker-cafe",
            state=SandboxState.IN_USE,
            thread_id="thread-1",
            cpu_quota=1.0,
            memory_mb=1024,
            pids_limit=128,
            timeout_s=300,
            created_at=last_used_at,
            acquired_at=last_used_at,
            last_used_at=last_used_at,
        )
    )
    return row_id


async def _insert_agent_row(
    store: DbSandboxStore, *, tenant_id: UUID, last_used_at: datetime
) -> UUID:
    """直插一条 agent 后端形状的行 —— 标记值/零值(同 Task 1 helper 的写法)。"""
    row_id = uuid4()
    async with store._sf() as session:
        session.add(
            SandboxInstanceRow(
                id=row_id,
                tenant_id=tenant_id,
                user_id=None,
                workspace_id=None,
                image_ref=AGENT_SANDBOX_IMAGE_REF,
                node=AGENT_SANDBOX_IMAGE_REF,
                container_id=None,
                state=SandboxState.IN_USE.value,
                thread_id=AGENT_SANDBOX_IMAGE_REF,
                cpu_quota=0,
                memory_mb=0,
                pids_limit=0,
                timeout_s=0,
                acquired_at=last_used_at,
                last_used_at=last_used_at,
            )
        )
        await session.commit()
    return row_id


@pytest.mark.asyncio
async def test_count_active_excludes_agent_sandbox_rows(store: DbSandboxStore) -> None:
    tenant_id = uuid4()
    old = datetime.now(UTC) - timedelta(seconds=3600)
    await _insert_docker_row(store, tenant_id=tenant_id, last_used_at=old)
    await _insert_agent_row(store, tenant_id=tenant_id, last_used_at=old)

    assert await store.count_active_for_tenant(tenant_id) == 1  # 只数 docker 行


@pytest.mark.asyncio
async def test_list_idle_sessions_excludes_agent_sandbox_rows(store: DbSandboxStore) -> None:
    tenant_id = uuid4()
    old = datetime.now(UTC) - timedelta(seconds=3600)
    docker_id = await _insert_docker_row(store, tenant_id=tenant_id, last_used_at=old)
    agent_id = await _insert_agent_row(store, tenant_id=tenant_id, last_used_at=old)

    idle = await store.list_idle_sessions(now=datetime.now(UTC), idle_ttl_s=60)

    # membership,不做整表相等 —— postgres_container 是 session 级容器,跨
    # 文件共享(Task 2 已确立此约定,见 test_sql_sandbox_instance_store.py)。
    idle_ids = {r.id for r in idle}
    assert docker_id in idle_ids  # E2B 行不进 docker reaper
    assert agent_id not in idle_ids
