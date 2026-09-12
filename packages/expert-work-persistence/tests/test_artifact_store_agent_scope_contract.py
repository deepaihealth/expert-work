"""``ArtifactStore`` 的 agent 维度契约 —— **两个后端跑同一组断言**。

仓库既有教训:「SQL ↔ 内存 store 谓词必须同义」。两份实现各写各的测试时,
漏掉一边的过滤条件是静默的 —— 单元测试全绿,真栈上 agent A 照旧看得见
agent B 的产物。所以这里参数化同一组用例跑两遍;内存档进单元 CI,
SQL 档标 ``integration``。

SQL 档在容器里**另建一个库**,不用那个 session 级共享的默认库:本文件的用例
会造出「同一 (tenant, user, name)、不同 agent_key」的两行,而那正是迁移 0154
的 ``downgrade()`` 重建三元组唯一索引时会撞的形状。共享库里留下这种行,别的
测试做 downgrade 往返(``test_sql_app_user_role`` / ``test_sql_user_upload_store``)
就会红 —— CI 上实际红过。

本地跑 SQL 档前: export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    InMemoryArtifactStore,
    SqlArtifactStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.artifact.base import ArtifactStore

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
#: 本文件专用的库 —— 见模块 docstring 为什么不能用共享的那个。
_DB = "artifact_agent_scope_contract"

_PLAN = "ai-health-plan-1a2b3c4d"
_SOP = "sop2-designer-5e6f7a8b"


@pytest.fixture(
    params=[
        "memory",
        pytest.param("sql", marks=pytest.mark.integration),
    ]
)
async def store(request: pytest.FixtureRequest) -> AsyncIterator[ArtifactStore]:
    if request.param == "memory":
        yield InMemoryArtifactStore()
        return

    container: PostgresContainer = request.getfixturevalue("postgres_container")
    admin = str(container.get_connection_url()).replace("+psycopg2", "")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{_DB}"')

    base, _, _ = admin.rpartition("/")
    dsn = f"{base}/{_DB}"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    command.upgrade(cfg, "head")
    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    )
    try:
        yield SqlArtifactStore(create_async_session_factory(engine))
    finally:
        await engine.dispose()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')


async def _save(
    store: ArtifactStore, *, tenant_id: UUID, user_id: UUID, agent_key: str, name: str
) -> None:
    await store.save_version(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=agent_key,
        name=name,
        kind="document",
        path_in_workspace=f"agents/{agent_key}/artifacts/{name}",
        created_in_thread=str(uuid4()),
    )


async def test_two_agents_same_name_are_two_artifacts(store: ArtifactStore) -> None:
    """核心不变式:同名不再合并。

    旧三元组键下这两次 save 走 ``ON CONFLICT DO UPDATE`` 合成一行、
    v1 的字节被 v2 覆盖 —— 这正是 B-50 要修的 bug。
    """
    tenant_id, user_id = uuid4(), uuid4()
    await _save(store, tenant_id=tenant_id, user_id=user_id, agent_key=_PLAN, name="报告.docx")
    await _save(store, tenant_id=tenant_id, user_id=user_id, agent_key=_SOP, name="报告.docx")

    rows = await store.list_for_user(tenant_id=tenant_id, user_id=user_id, agent_key=None)

    assert len(rows) == 2, "两个 agent 的同名产物合并成一行了"
    assert {r.agent_key for r in rows} == {_PLAN, _SOP}
    assert [r.latest_version for r in rows] == [1, 1], "版本号累加了 —— 说明走了合并分支"


async def test_same_agent_same_name_still_bumps_version(store: ArtifactStore) -> None:
    """收窄不能收过头:同一个 agent 重存同名,仍是同一行 + 版本 +1。"""
    tenant_id, user_id = uuid4(), uuid4()
    await _save(store, tenant_id=tenant_id, user_id=user_id, agent_key=_PLAN, name="报告.docx")
    await _save(store, tenant_id=tenant_id, user_id=user_id, agent_key=_PLAN, name="报告.docx")

    rows = await store.list_for_user(tenant_id=tenant_id, user_id=user_id, agent_key=None)

    assert len(rows) == 1
    assert rows[0].latest_version == 2


async def test_list_for_user_filters_by_agent_key(store: ArtifactStore) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    await _save(store, tenant_id=tenant_id, user_id=user_id, agent_key=_PLAN, name="计划.docx")
    await _save(store, tenant_id=tenant_id, user_id=user_id, agent_key=_SOP, name="评审.docx")

    only_plan = await store.list_for_user(tenant_id=tenant_id, user_id=user_id, agent_key=_PLAN)

    assert {a.name for a in only_plan} == {"计划.docx"}


async def test_list_for_user_with_none_returns_every_agent(store: ArtifactStore) -> None:
    """``agent_key=None`` = 不按 agent 过滤 —— 控制台与留存 job 要看全量。"""
    tenant_id, user_id = uuid4(), uuid4()
    await _save(store, tenant_id=tenant_id, user_id=user_id, agent_key=_PLAN, name="计划.docx")
    await _save(store, tenant_id=tenant_id, user_id=user_id, agent_key=_SOP, name="评审.docx")

    everything = await store.list_for_user(tenant_id=tenant_id, user_id=user_id, agent_key=None)

    assert {a.name for a in everything} == {"计划.docx", "评审.docx"}


async def test_get_latest_version_is_agent_scoped(store: ArtifactStore) -> None:
    """两个 agent 各有同名产物时,取的是自己那条的字节。"""
    tenant_id, user_id = uuid4(), uuid4()
    await _save(store, tenant_id=tenant_id, user_id=user_id, agent_key=_PLAN, name="报告.docx")
    await _save(store, tenant_id=tenant_id, user_id=user_id, agent_key=_SOP, name="报告.docx")

    version = await store.get_latest_version(
        tenant_id=tenant_id, user_id=user_id, agent_key=_SOP, name="报告.docx"
    )

    assert version is not None
    assert version.path_in_workspace == f"agents/{_SOP}/artifacts/报告.docx"


async def test_soft_delete_only_touches_its_own_agent(store: ArtifactStore) -> None:
    """删 agent A 的同名产物,agent B 的必须完好 —— 否则删一份连坐两份。"""
    tenant_id, user_id = uuid4(), uuid4()
    await _save(store, tenant_id=tenant_id, user_id=user_id, agent_key=_PLAN, name="报告.docx")
    await _save(store, tenant_id=tenant_id, user_id=user_id, agent_key=_SOP, name="报告.docx")

    hit = await store.soft_delete(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=_PLAN,
        name="报告.docx",
        now=datetime.now(UTC),
    )

    assert hit is True
    survivors = await store.list_for_user(tenant_id=tenant_id, user_id=user_id, agent_key=None)
    assert [a.agent_key for a in survivors] == [_SOP], "另一个 agent 的同名产物被连坐删了"


async def test_by_id_reads_and_deletes_are_unambiguous(store: ArtifactStore) -> None:
    """控制台按 id 寻址 —— name 在四元组键下不再是身份,id 永远是。"""
    tenant_id, user_id = uuid4(), uuid4()
    await _save(store, tenant_id=tenant_id, user_id=user_id, agent_key=_PLAN, name="报告.docx")
    await _save(store, tenant_id=tenant_id, user_id=user_id, agent_key=_SOP, name="报告.docx")
    rows = await store.list_for_user(tenant_id=tenant_id, user_id=user_id, agent_key=None)
    sop_row = next(a for a in rows if a.agent_key == _SOP)

    version = await store.get_latest_version_by_id(
        tenant_id=tenant_id, user_id=user_id, artifact_id=sop_row.id
    )
    assert version is not None
    assert version.path_in_workspace == f"agents/{_SOP}/artifacts/报告.docx"

    assert (
        await store.soft_delete_by_id(
            tenant_id=tenant_id, user_id=user_id, artifact_id=sop_row.id, now=datetime.now(UTC)
        )
        is True
    )
    survivors = await store.list_for_user(tenant_id=tenant_id, user_id=user_id, agent_key=None)
    assert [a.agent_key for a in survivors] == [_PLAN]


async def test_by_id_never_crosses_users(store: ArtifactStore) -> None:
    """按 id 寻址仍受 ``(tenant, user)`` 约束 —— id 是 UUID 不等于可以跨用户取。"""
    tenant_id, owner, stranger = uuid4(), uuid4(), uuid4()
    await _save(store, tenant_id=tenant_id, user_id=owner, agent_key=_PLAN, name="报告.docx")
    row = (await store.list_for_user(tenant_id=tenant_id, user_id=owner, agent_key=None))[0]

    assert (
        await store.get_latest_version_by_id(
            tenant_id=tenant_id, user_id=stranger, artifact_id=row.id
        )
        is None
    )
    assert (
        await store.soft_delete_by_id(
            tenant_id=tenant_id, user_id=stranger, artifact_id=row.id, now=datetime.now(UTC)
        )
        is False
    )


async def test_update_kind_and_list_versions_address_by_id(store: ArtifactStore) -> None:
    """``update_kind`` / ``list_versions`` 只有控制台在用,直接改成按 id。"""
    tenant_id, user_id = uuid4(), uuid4()
    await _save(store, tenant_id=tenant_id, user_id=user_id, agent_key=_PLAN, name="报告.docx")
    await _save(store, tenant_id=tenant_id, user_id=user_id, agent_key=_PLAN, name="报告.docx")
    row = (await store.list_for_user(tenant_id=tenant_id, user_id=user_id, agent_key=None))[0]

    updated = await store.update_kind(
        tenant_id=tenant_id, user_id=user_id, artifact_id=row.id, kind="code"
    )
    assert updated is not None and updated.kind == "code"

    versions = await store.list_versions(tenant_id=tenant_id, user_id=user_id, artifact_id=row.id)
    assert versions is not None
    assert [v.version for v in versions] == [2, 1]
