"""DbSandboxStore 的按后端限定谓词 —— 真 PG 集成测(PR-A Task 3)。

docker supervisor 与 AgentSandboxClient 共用 ``sandbox_instance`` 表;
supervisor 的两个集合查询原先不分后端:``list_idle_sessions`` 会把 E2B
热会话行交给 docker reaper(``docker stop`` 一个 E2B id、失败后销毁记账
把 agent 侧的行毁掉),``count_active_for_tenant`` 把 E2B 行算进 docker
配额。谓词 ``image_ref != AGENT_SANDBOX_IMAGE_REF``。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

from expert_work.common.observability.log import ExpertWorkJsonFormatter
from expert_work.persistence import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
    rls,
)
from expert_work.persistence.models import SandboxInstanceRow
from expert_work.persistence.rls import bypass_rls_var, current_tenant_id_var
from expert_work.persistence.sandbox_instance_store import AGENT_SANDBOX_IMAGE_REF
from sandbox_supervisor import app as app_module
from sandbox_supervisor.domain import SandboxRecord, SandboxState
from sandbox_supervisor.store import DbSandboxStore

pytestmark = pytest.mark.integration

_RLS_LOGGER = "expert_work.persistence.rls"

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


@pytest.fixture
def rls_store(postgres_container: PostgresContainer) -> Iterator[DbSandboxStore]:
    """Same store, but over the factory **the supervisor process itself builds**
    (``app.build_session_factory``) — that is what B-45 is about."""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    yield DbSandboxStore(app_module.build_session_factory(engine))


@pytest.mark.asyncio
async def test_rls_detect_signal_is_attributable_in_this_process(
    rls_store: DbSandboxStore, caplog: pytest.LogCaptureFixture
) -> None:
    """B-45 —— listener 在 supervisor 进程真的装上了,归因指向 supervisor 自己
    的帧。supervisor 目前不给任何路径声明作用域(三分形态见
    ``test_rls_wiring.py`` 的模块注释),所以这是**生产真实形状**:

    * 信号存在 = ``build_rls_sessionmaker`` 真的被这个进程调过(``app.py``
      没接的时候 listener 在本进程不存在,一条都不会有);
    * ``rls_caller`` 指向 ``sandbox_supervisor.store`` = #1443 第二层还在。
      真 ORM 调用里 ``after_begin`` 跑在 ``greenlet_spawn`` 的 greenlet 上,
      普通帧链到 SQLAlchemy 就断了;不顺父 greenlet 的 ``gr_frame`` 走,这里
      只会拿到 ``<unknown>``;
    * ``rls_caller_outer`` 指向调用方(本测试函数)= 归因不止一层,能顺到
      是哪条 supervisor 路径开的会话;
    * 过一遍平台 JSON formatter 还带得出 ``rls_caller`` = #1443 第一层还在。
    """
    token_b = bypass_rls_var.set(False)
    token_t = current_tenant_id_var.set(None)
    rls._reset_signal_state()
    try:
        with caplog.at_level(logging.WARNING, logger=_RLS_LOGGER):
            await rls_store.count_active_for_tenant(uuid4())
    finally:
        rls._reset_signal_state()
        current_tenant_id_var.reset(token_t)
        bypass_rls_var.reset(token_b)

    records = [
        r for r in caplog.records if r.name == _RLS_LOGGER and r.message == "rls.would_fail_closed"
    ]
    assert records, "no Detect signal — build_rls_sessionmaker never ran in this process"
    caller = records[0].__dict__["rls_caller"]
    assert caller.startswith("sandbox_supervisor.store:"), caller
    assert caller.endswith(" count_active_for_tenant"), caller
    assert "sqlalchemy" not in caller
    outer = records[0].__dict__["rls_caller_outer"]
    assert outer is not None
    assert outer.endswith(" test_rls_detect_signal_is_attributable_in_this_process"), outer
    # 第一层:平台 JSON formatter 必须把结构化归因带到 stdout,不是丢掉。
    payload = json.loads(
        ExpertWorkJsonFormatter(service="sandbox_supervisor", env="dev").format(records[0])
    )
    assert payload["rls_caller"] == caller
