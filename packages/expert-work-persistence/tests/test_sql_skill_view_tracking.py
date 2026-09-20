"""Integration: ``skill_run_usage.outcome='viewed'`` on real Postgres — B-84.

在真 PG 上 (迁移 0159 已应用) 钉住四件事:

* ``viewed`` 是合法取值 —— 0159 把 CHECK 放宽了, 写得进去;
* ``skill_run_usage_window`` **过滤掉** ``viewed``, 回滚判定的样本一条不多
  (漏了会把健康版本自动 archive 掉);
* :meth:`SqlSkillStore.skill_view_gap` 能认出「绑了但这个 agent 没打开过」,
  **平台技能 (``tenant_id IS NULL``) 也算得出来** —— 这是本设计的全部意义:
  对接方 ai-health-plan 绑的 19 个技能 19/19 都是平台技能;
* 解析不到的名字落 ``untracked_names``, 不混进 ``unviewed``。

这个文件同时是 0159 能不能跟上 head 链的实证: fixture 走
``alembic upgrade head``。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlSkillStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.rls import build_rls_sessionmaker, current_tenant_id_var
from expert_work.protocol import SkillRunUsage, SkillStatus

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
APP_ROLE = "expert_work_app"
APP_PASSWORD = "expert_work_app_test_pw"  # test-only fixture password
_AGENT = "ai-health-plan"


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


def _rewrite_credentials(dsn: str, user: str, password: str) -> str:
    parsed = urlparse(dsn)
    new_netloc = f"{user}:{password}@{parsed.hostname}"
    if parsed.port is not None:
        new_netloc = f"{new_netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=new_netloc))


def _provision_app_role(sync_dsn: str) -> None:
    admin_engine = create_engine(sync_dsn, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
                {"role": APP_ROLE},
            ).first()
            if exists is None:
                conn.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}'"))
            conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
            conn.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE "
                    f"ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
                )
            )
            conn.execute(
                text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
            )
    finally:
        admin_engine.dispose()


@pytest.fixture
def stores(postgres_container: PostgresContainer) -> Iterator[tuple[SqlSkillStore, SqlSkillStore]]:
    """``(app_store, owner_store)``.

    ``app_store`` 连的是非 BYPASSRLS 的 ``expert_work_app``, 所以**写入侧真的受
    RLS 管** —— B-84 的 ``viewed`` 行必须在这种会话里写得进去, 那是这个设计相对
    「给 ``skill`` 加列」的关键好处。

    ``owner_store`` 连表 OWNER。合并视图的**读取**要同时看见 NULL-tenant 的
    ``skill`` 行与租户自有的 ``skill_run_usage`` 行, 这在任何单一租户 GUC 下都
    做不到 —— 与 ``list_skills_all_tenants`` / ``resolve_platform_by_name`` 同一
    条规矩 (``bypass_rls_session()``), 而这些表都是 ENABLE-only, OWNER 免疫。
    """
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    _provision_app_role(_sync_dsn(postgres_container))

    app_dsn = _rewrite_credentials(_async_dsn(postgres_container), APP_ROLE, APP_PASSWORD)
    app_engine = create_async_engine_from_config(DatabaseConfig(dsn=app_dsn))
    app_sf = build_rls_sessionmaker(create_async_session_factory(app_engine))

    owner_engine = create_async_engine_from_config(
        DatabaseConfig(dsn=_async_dsn(postgres_container))
    )
    owner_sf = build_rls_sessionmaker(create_async_session_factory(owner_engine))
    yield SqlSkillStore(app_sf), SqlSkillStore(owner_sf)


@pytest.fixture(autouse=True)
def reset_rls() -> Iterator[None]:
    tok = current_tenant_id_var.set(None)
    try:
        yield
    finally:
        current_tenant_id_var.reset(tok)


async def _seed_active(store: SqlSkillStore, tenant: UUID, name: str) -> UUID:
    sid = uuid4()
    await store.create_skill(skill_id=sid, tenant_id=tenant, name=name)
    await store.add_version(
        version_id=uuid4(), skill_id=sid, tenant_id=tenant, prompt_fragment="body"
    )
    await store.set_status(skill_id=sid, tenant_id=tenant, status=SkillStatus.ACTIVE)
    return sid


async def _record(
    store: SqlSkillStore,
    *,
    tenant: UUID,
    skill_id: UUID,
    outcome: str,
    agent: str = _AGENT,
    at: datetime | None = None,
) -> None:
    await store.record_skill_run_usage(
        usage=SkillRunUsage(
            id=uuid4(),
            tenant_id=tenant,
            skill_id=skill_id,
            skill_version=1,
            thread_id=uuid4(),
            agent_name=agent,
            outcome=outcome,  # type: ignore[arg-type]
            created_at=at or datetime.now(UTC),
        )
    )


@pytest.mark.asyncio
async def test_viewed_row_writes_under_a_plain_tenant_session(
    stores: tuple[SqlSkillStore, SqlSkillStore],
) -> None:
    """``viewed`` 行要能在**非 BYPASSRLS 的租户会话**里写进去。

    这是本设计相对「给 ``skill`` 加列」的关键好处: 证据行是消费方租户自有的,
    热路径不需要 bypass。顺带钉住 0159 真的放宽了 CHECK (没放宽这里会
    ``IntegrityError``)。
    """
    store, owner = stores
    tenant = uuid4()
    name = f"read-{uuid4().hex[:8]}"
    current_tenant_id_var.set(tenant)
    sid = await _seed_active(store, tenant, name)

    await _record(store, tenant=tenant, skill_id=sid, outcome="viewed")

    current_tenant_id_var.set(None)
    gap = await owner.skill_view_gap(
        tenant_id=tenant,
        agent_name=_AGENT,
        names=[name],
        viewed_before=datetime.now(UTC) - timedelta(days=1),
    )
    assert gap.unviewed == (), "刚写的 viewed 行必须让这个技能从盘点里消失"
    assert gap.untracked_names == ()


@pytest.mark.asyncio
async def test_run_usage_window_excludes_viewed_rows(
    stores: tuple[SqlSkillStore, SqlSkillStore],
) -> None:
    """回滚判定的样本一条 ``viewed`` 都不能有 —— 否则健康版本会被自动 archive。"""
    store, _owner = stores
    tenant = uuid4()
    current_tenant_id_var.set(tenant)
    sid = await _seed_active(store, tenant, f"gate-{uuid4().hex[:8]}")
    for outcome in ("success", "failed", "viewed"):
        await _record(store, tenant=tenant, skill_id=sid, outcome=outcome)

    rows = await store.skill_run_usage_window(
        skill_id=sid,
        skill_version=1,
        tenant_id=tenant,
        since=datetime.now(UTC) - timedelta(days=1),
    )
    assert sorted(r.outcome for r in rows) == ["failed", "success"]


@pytest.mark.asyncio
async def test_skill_view_gap_covers_platform_skills(
    stores: tuple[SqlSkillStore, SqlSkillStore],
) -> None:
    """平台技能 (``skill.tenant_id IS NULL``) 也要量得出来 —— 返工的全部意义。"""
    store, owner = stores
    tenant = uuid4()
    suffix = uuid4().hex[:8]
    read, unread = f"read-{suffix}", f"unread-{suffix}"
    read_id, unread_id = uuid4(), uuid4()
    await store.create_platform_skill(skill_id=read_id, name=read)
    await store.create_platform_skill(skill_id=unread_id, name=unread)

    current_tenant_id_var.set(tenant)
    await _record(store, tenant=tenant, skill_id=read_id, outcome="viewed")

    current_tenant_id_var.set(None)  # 合并视图: 以 OWNER 身份跨 NULL-tenant 读
    gap = await owner.skill_view_gap(
        tenant_id=tenant,
        agent_name=_AGENT,
        names=[read, unread, f"typo-{suffix}"],
        viewed_before=datetime.now(UTC) - timedelta(days=30),
    )
    assert [s.id for s in gap.unviewed] == [unread_id]
    assert gap.untracked_names == (f"typo-{suffix}",)


@pytest.mark.asyncio
async def test_skill_view_gap_is_per_agent(
    stores: tuple[SqlSkillStore, SqlSkillStore],
) -> None:
    """另一个 agent 打开过同一个平台技能, 不算这个 agent 读过。"""
    store, owner = stores
    tenant = uuid4()
    name = f"shared-{uuid4().hex[:8]}"
    sid = uuid4()
    await store.create_platform_skill(skill_id=sid, name=name)

    current_tenant_id_var.set(tenant)
    await _record(store, tenant=tenant, skill_id=sid, outcome="viewed", agent="sop2-designer")

    current_tenant_id_var.set(None)
    gap = await owner.skill_view_gap(
        tenant_id=tenant,
        agent_name=_AGENT,
        names=[name],
        viewed_before=datetime.now(UTC) - timedelta(days=30),
    )
    assert [s.id for s in gap.unviewed] == [sid]
