"""Integration tests for :class:`SqlAgentSpecStore` against a real Postgres.

Mirrors the ``InMemoryAgentSpecStore`` unit suite, run against the
Alembic schema. Each test uses a fresh ``tenant_id`` because the
testcontainers Postgres is shared across the session.
"""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.agent_spec import (
    DuplicateAgentSpecError,
    InMemoryAgentSpecStore,
    SqlAgentSpecStore,
)
from expert_work.persistence.models import AgentSpecRow
from expert_work.protocol import AgentSpec, AgentSpecStatus

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

_BASE_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "code-reviewer", "version": "1.0.0", "tenant": "platform-eng"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
        "system_prompt": {"template": "you are a reviewer"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}


def _spec(
    *, version: str = "1.0.0", name: str = "code-reviewer", display_name: str = ""
) -> AgentSpec:
    doc = deepcopy(_BASE_SPEC)
    doc["metadata"]["version"] = version
    doc["metadata"]["name"] = name
    doc["spec"]["display_name"] = display_name
    return AgentSpec.model_validate(doc)


def _sha() -> str:
    return "a" * 64


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


SqlStoreFixture = tuple[SqlAgentSpecStore, AsyncEngine]


@pytest.fixture
def sql_store(postgres_container: PostgresContainer) -> Iterator[SqlStoreFixture]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    yield SqlAgentSpecStore(create_async_session_factory(engine)), engine


@pytest.mark.asyncio
async def test_create_then_get_round_trip(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        record = await store.create(
            tenant_id=tenant, spec=_spec(), spec_sha256=_sha(), created_by="alice"
        )
        assert record.status is AgentSpecStatus.ACTIVE
        assert isinstance(record.id, UUID)

        fetched = await store.get(tenant_id=tenant, name="code-reviewer", version="1.0.0")
        assert fetched is not None
        assert fetched.id == record.id
        assert fetched.spec_sha256 == _sha()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_create_raises(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        await store.create(tenant_id=tenant, spec=_spec(), spec_sha256=_sha(), created_by="a")
        with pytest.raises(DuplicateAgentSpecError):
            await store.create(tenant_id=tenant, spec=_spec(), spec_sha256=_sha(), created_by="a")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_get_filters_by_tenant(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        owner, other = uuid4(), uuid4()
        await store.create(tenant_id=owner, spec=_spec(), spec_sha256=_sha(), created_by="a")
        assert await store.get(tenant_id=other, name="code-reviewer", version="1.0.0") is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_filters_by_name_newest_first(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        await store.create(
            tenant_id=tenant, spec=_spec(version="1.0.0"), spec_sha256=_sha(), created_by="a"
        )
        await store.create(
            tenant_id=tenant, spec=_spec(version="1.0.1"), spec_sha256=_sha(), created_by="a"
        )
        await store.create(
            tenant_id=tenant, spec=_spec(name="other"), spec_sha256=_sha(), created_by="a"
        )
        rows = await store.list_by_tenant(tenant_id=tenant, name="code-reviewer")
        assert [r.version for r in rows] == ["1.0.1", "1.0.0"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_spec_replaces_payload(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        await store.create(tenant_id=tenant, spec=_spec(), spec_sha256=_sha(), created_by="a")
        new_doc = deepcopy(_BASE_SPEC)
        new_doc["spec"]["system_prompt"]["template"] = "updated prompt"
        updated = await store.update_spec(
            tenant_id=tenant,
            name="code-reviewer",
            version="1.0.0",
            spec=AgentSpec.model_validate(new_doc),
            spec_sha256="b" * 64,
            updated_by="alice",
        )
        assert updated is not None
        assert updated.record.spec.spec.system_prompt.template == "updated prompt"
        assert updated.record.spec_sha256 == "b" * 64
        # Stream HX-5 -- create wrote revision 1, this update revision 2.
        assert updated.revision == 2
        assert updated.prev_sha256 == _sha()
        history = await store.list_revisions(
            tenant_id=tenant, name="code-reviewer", version="1.0.0"
        )
        assert [r.revision for r in history] == [2, 1]
        assert history[0].actor_id == "alice"
        assert history[1].actor_id == "a"

        # Same-sha update: recorded no-op, no new revision.
        noop = await store.update_spec(
            tenant_id=tenant,
            name="code-reviewer",
            version="1.0.0",
            spec=AgentSpec.model_validate(new_doc),
            spec_sha256="b" * 64,
            updated_by="alice",
        )
        assert noop is not None and noop.revision is None
        assert (
            len(await store.list_revisions(tenant_id=tenant, name="code-reviewer", version="1.0.0"))
            == 2
        )

        one = await store.get_revision(
            tenant_id=tenant, name="code-reviewer", version="1.0.0", revision=1
        )
        assert one is not None and one.spec_sha256 == _sha()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_spec_returns_none_when_missing(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        result = await store.update_spec(
            tenant_id=uuid4(),
            name="missing",
            version="9.9.9",
            spec=_spec(),
            spec_sha256=_sha(),
            updated_by="a",
        )
        assert result is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_soft_delete_hides_from_get(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        await store.create(tenant_id=tenant, spec=_spec(), spec_sha256=_sha(), created_by="a")
        deleted = await store.update_status(
            tenant_id=tenant,
            name="code-reviewer",
            version="1.0.0",
            status=AgentSpecStatus.DELETED,
        )
        assert deleted is not None and deleted.status is AgentSpecStatus.DELETED

        assert await store.get(tenant_id=tenant, name="code-reviewer", version="1.0.0") is None
        fetched = await store.get(
            tenant_id=tenant, name="code-reviewer", version="1.0.0", include_deleted=True
        )
        assert fetched is not None and fetched.status is AgentSpecStatus.DELETED
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Phase 3 (3.1) C-1 — list_distinct_active_by_tenant / count_distinct_active_by_tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_distinct_active_by_tenant_dedupes_before_paginating(
    sql_store: SqlStoreFixture,
) -> None:
    """C-1 的核心断言,真容器上验证 ``DISTINCT ON`` + 子查询确实把
    LIMIT/OFFSET 打在去重之后。一个 name 有多个 ACTIVE 版本行是常态
    (发新版本只 create,没有代码把旧版本降级)——分页若打在版本行上,
    ``limit=2 offset=0`` 会因为 alpha 占两个槽位而只剩 1 个去重后的结果,
    客户端按"不足一页即最后一页"的标准循环会静默漏掉 beta / gamma。"""
    store, engine = sql_store
    try:
        tenant = uuid4()
        await store.create(
            tenant_id=tenant,
            spec=_spec(name="alpha", version="1.0.0"),
            spec_sha256=_sha(),
            created_by="a",
        )
        await store.create(
            tenant_id=tenant,
            spec=_spec(name="alpha", version="1.0.1"),
            spec_sha256=_sha(),
            created_by="a",
        )
        await store.create(
            tenant_id=tenant,
            spec=_spec(name="beta", version="1.0.0"),
            spec_sha256=_sha(),
            created_by="a",
        )
        await store.create(
            tenant_id=tenant,
            spec=_spec(name="gamma", version="1.0.0"),
            spec_sha256=_sha(),
            created_by="a",
        )

        page0 = await store.list_distinct_active_by_tenant(tenant_id=tenant, limit=2, offset=0)
        assert [r.name for r in page0] == ["alpha", "beta"]

        page1 = await store.list_distinct_active_by_tenant(tenant_id=tenant, limit=2, offset=2)
        assert [r.name for r in page1] == ["gamma"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_distinct_active_by_tenant_keeps_newest_version_fields(
    sql_store: SqlStoreFixture,
) -> None:
    """去重保留的必须是 ``created_at`` 最新的那一行,用不同 ``display_name``
    标记两个版本,"取错版本"也会被逮到。"""
    store, engine = sql_store
    try:
        tenant = uuid4()
        await store.create(
            tenant_id=tenant,
            spec=_spec(name="alpha", version="1.0.0", display_name="v1 name"),
            spec_sha256=_sha(),
            created_by="a",
        )
        await store.create(
            tenant_id=tenant,
            spec=_spec(name="alpha", version="1.0.1", display_name="v2 name"),
            spec_sha256=_sha(),
            created_by="a",
        )

        rows = await store.list_distinct_active_by_tenant(tenant_id=tenant)

        assert len(rows) == 1
        assert rows[0].version == "1.0.1"
        assert rows[0].spec.spec.display_name == "v2 name"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_distinct_active_by_tenant_excludes_non_active_and_other_tenants(
    sql_store: SqlStoreFixture,
) -> None:
    store, engine = sql_store
    try:
        tenant, other = uuid4(), uuid4()
        await store.create(
            tenant_id=tenant, spec=_spec(name="alpha"), spec_sha256=_sha(), created_by="a"
        )
        await store.create(
            tenant_id=tenant, spec=_spec(name="beta"), spec_sha256=_sha(), created_by="a"
        )
        await store.update_status(
            tenant_id=tenant, name="beta", version="1.0.0", status=AgentSpecStatus.DEPRECATED
        )
        await store.create(
            tenant_id=other, spec=_spec(name="gamma"), spec_sha256=_sha(), created_by="a"
        )

        rows = await store.list_distinct_active_by_tenant(tenant_id=tenant)

        assert [r.name for r in rows] == ["alpha"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_count_distinct_active_by_tenant_sql(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        await store.create(
            tenant_id=tenant,
            spec=_spec(name="alpha", version="1.0.0"),
            spec_sha256=_sha(),
            created_by="a",
        )
        await store.create(
            tenant_id=tenant,
            spec=_spec(name="alpha", version="1.0.1"),
            spec_sha256=_sha(),
            created_by="a",
        )
        await store.create(
            tenant_id=tenant, spec=_spec(name="beta"), spec_sha256=_sha(), created_by="a"
        )

        # 两个 name(alpha 两个版本合一),不是三行。
        assert await store.count_distinct_active_by_tenant(tenant_id=tenant) == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_distinct_active_by_tenant_matches_the_in_memory_store(
    sql_store: SqlStoreFixture,
) -> None:
    """两个后端各写一遍去重 + 分页谓词,是本仓反复出问题的地方(SQL 的
    ``DISTINCT ON`` 与内存的逐条比较不是自动等价的)。同一组输入喂两边,
    多版本 + 单版本混在一起,断言完整翻页后的结果集相同。"""
    store, engine = sql_store
    try:
        tenant = uuid4()
        mem_store = InMemoryAgentSpecStore()
        layout = [
            ("alpha", "1.0.0", "v1"),
            ("alpha", "1.0.1", "v2"),
            ("beta", "1.0.0", "only"),
            ("gamma", "1.0.0", "only"),
        ]
        for name, version, display_name in layout:
            for backend in (store, mem_store):
                await backend.create(
                    tenant_id=tenant,
                    spec=_spec(name=name, version=version, display_name=display_name),
                    spec_sha256=_sha(),
                    created_by="a",
                )

        for limit, offset in ((2, 0), (2, 2), (100, 0)):
            sql_rows = await store.list_distinct_active_by_tenant(
                tenant_id=tenant, limit=limit, offset=offset
            )
            mem_rows = await mem_store.list_distinct_active_by_tenant(
                tenant_id=tenant, limit=limit, offset=offset
            )
            assert [r.name for r in sql_rows] == [r.name for r in mem_rows], (
                f"limit={limit} offset={offset} 两个后端结果不一致"
            )
            assert [r.version for r in sql_rows] == [r.version for r in mem_rows], (
                f"limit={limit} offset={offset} 两个后端选出的版本不一致"
            )

        assert await store.count_distinct_active_by_tenant(
            tenant_id=tenant
        ) == await mem_store.count_distinct_active_by_tenant(tenant_id=tenant)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_distinct_active_by_tenant_tie_break_matches_on_created_at_collision(
    sql_store: SqlStoreFixture,
) -> None:
    """复审二轮必修 1:tie-break 是承重代码,此前零覆盖。两个变异(内存侧
    tie-break 反向 / SQL 侧删掉 ``id.desc()``)都不会让任何既有测试变红,
    因为 ``test_..._matches_the_in_memory_store`` 从不制造 ``created_at``
    撞车,次级键子句没有测试咬得住。

    这里把三个版本的 ``created_at`` 压平到完全相同,并把 ``id`` 显式设成
    三个固定值(不用 store 各自随机生成的——SQL 用 ``gen_random_uuid()``、
    内存用 ``uuid4()``,两边独立随机不可能自然撞出同一个"赢家",测试必须
    自己控制这三个值才能断言"两边选的是同一行")。Postgres uuid 的
    bytewise 序与 Python ``UUID.__gt__`` 的 ``.int`` 序一致,是复审实测出
    来的事实——这条测试把它钉成断言,而不是继续靠"没人踩过"活着。
    """
    store, engine = sql_store
    try:
        tenant = uuid4()
        mem_store = InMemoryAgentSpecStore()

        # 固定三个 id(升序:低/中/高),两个后端都用同一组值——否则两边各自
        # 随机生成的 id 不可能保证选出同一个"赢家"。
        id_low, id_mid, id_high = sorted((uuid4(), uuid4(), uuid4()))
        same_created_at = datetime(2026, 1, 1, tzinfo=UTC)
        rows = [
            ("1.0.0", "v-low", id_low),
            ("1.0.1", "v-mid", id_mid),
            ("1.0.2", "v-high", id_high),
        ]

        for version, display_name, forced_id in rows:
            await store.create(
                tenant_id=tenant,
                spec=_spec(name="alpha", version=version, display_name=display_name),
                spec_sha256=_sha(),
                created_by="a",
            )
            async with store._sf() as session:
                await session.execute(
                    update(AgentSpecRow)
                    .where(
                        AgentSpecRow.tenant_id == tenant,
                        AgentSpecRow.name == "alpha",
                        AgentSpecRow.version == version,
                    )
                    .values(id=forced_id, created_at=same_created_at)
                )
                await session.commit()

            await mem_store.create(
                tenant_id=tenant,
                spec=_spec(name="alpha", version=version, display_name=display_name),
                spec_sha256=_sha(),
                created_by="a",
            )
            key = (tenant, "alpha", version)
            existing = mem_store._rows[key]
            mem_store._rows[key] = existing.model_copy(
                update={"id": forced_id, "created_at": same_created_at}
            )

        sql_rows = await store.list_distinct_active_by_tenant(tenant_id=tenant)
        mem_rows = await mem_store.list_distinct_active_by_tenant(tenant_id=tenant)

        assert len(sql_rows) == 1 and len(mem_rows) == 1
        # id 最大的那行(v-high / 1.0.2)必须是两个后端各自的赢家。
        assert sql_rows[0].id == id_high and sql_rows[0].version == "1.0.2"
        assert mem_rows[0].id == id_high and mem_rows[0].version == "1.0.2"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 未发布草稿 —— 与 test_in_memory_agent_spec_store 里的同名用例逐条对应。
# 两个实现的谓词必须同义;分裂过一次就再也没人查得出来是哪边错了。
# ---------------------------------------------------------------------------


def _drafted(template: str) -> AgentSpec:
    doc = deepcopy(_BASE_SPEC)
    doc["spec"]["system_prompt"]["template"] = template
    return AgentSpec.model_validate(doc)


@pytest.mark.asyncio
async def test_save_draft_leaves_the_live_spec_alone_sql(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        await store.create(tenant_id=tenant, spec=_spec(), spec_sha256=_sha(), created_by="a")
        saved = await store.save_draft(
            tenant_id=tenant,
            name="code-reviewer",
            version="1.0.0",
            spec=_drafted("drafted prompt"),
            spec_sha256="d" * 64,
            updated_by="bob",
        )
        assert saved is not None
        assert saved.draft is not None
        assert saved.draft.spec.spec.system_prompt.template == "drafted prompt"
        assert saved.draft.updated_by == "bob"
        assert saved.spec.spec.system_prompt.template == "you are a reviewer"
        assert saved.spec_sha256 == _sha()
        history = await store.list_revisions(
            tenant_id=tenant, name="code-reviewer", version="1.0.0"
        )
        assert len(history) == 1

        # 读回来也带着(``_row_to_draft`` 的往返)。
        fetched = await store.get(tenant_id=tenant, name="code-reviewer", version="1.0.0")
        assert fetched is not None and fetched.draft is not None
        assert fetched.draft.spec_sha256 == "d" * 64
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_publish_draft_promotes_and_clears_sql(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        await store.create(tenant_id=tenant, spec=_spec(), spec_sha256=_sha(), created_by="a")
        await store.save_draft(
            tenant_id=tenant,
            name="code-reviewer",
            version="1.0.0",
            spec=_drafted("drafted prompt"),
            spec_sha256="d" * 64,
            updated_by="bob",
        )
        result = await store.publish_draft(
            tenant_id=tenant, name="code-reviewer", version="1.0.0", updated_by="bob"
        )
        assert result is not None
        assert result.record.spec.spec.system_prompt.template == "drafted prompt"
        assert result.record.spec_sha256 == "d" * 64
        assert result.prev_sha256 == _sha()
        assert result.revision == 2
        assert result.record.draft is None

        fetched = await store.get(tenant_id=tenant, name="code-reviewer", version="1.0.0")
        assert fetched is not None and fetched.draft is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_publish_identical_draft_records_no_revision_sql(
    sql_store: SqlStoreFixture,
) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        await store.create(tenant_id=tenant, spec=_spec(), spec_sha256=_sha(), created_by="a")
        await store.save_draft(
            tenant_id=tenant,
            name="code-reviewer",
            version="1.0.0",
            spec=_spec(),
            spec_sha256=_sha(),
            updated_by="bob",
        )
        result = await store.publish_draft(
            tenant_id=tenant, name="code-reviewer", version="1.0.0", updated_by="bob"
        )
        assert result is not None
        assert result.revision is None
        assert result.record.draft is None
        history = await store.list_revisions(
            tenant_id=tenant, name="code-reviewer", version="1.0.0"
        )
        assert len(history) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_publish_without_a_draft_returns_none_sql(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        await store.create(tenant_id=tenant, spec=_spec(), spec_sha256=_sha(), created_by="a")
        assert (
            await store.publish_draft(
                tenant_id=tenant, name="code-reviewer", version="1.0.0", updated_by="bob"
            )
            is None
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_discard_draft_is_idempotent_sql(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        await store.create(tenant_id=tenant, spec=_spec(), spec_sha256=_sha(), created_by="a")
        first = await store.discard_draft(tenant_id=tenant, name="code-reviewer", version="1.0.0")
        assert first is not None and first.draft is None
        await store.save_draft(
            tenant_id=tenant,
            name="code-reviewer",
            version="1.0.0",
            spec=_drafted("x"),
            spec_sha256="d" * 64,
            updated_by="bob",
        )
        second = await store.discard_draft(tenant_id=tenant, name="code-reviewer", version="1.0.0")
        assert second is not None and second.draft is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_draft_methods_are_tenant_scoped_sql(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant, other = uuid4(), uuid4()
        await store.create(tenant_id=tenant, spec=_spec(), spec_sha256=_sha(), created_by="a")
        assert (
            await store.save_draft(
                tenant_id=other,
                name="code-reviewer",
                version="1.0.0",
                spec=_drafted("x"),
                spec_sha256="d" * 64,
                updated_by="mallory",
            )
            is None
        )
        assert (
            await store.discard_draft(tenant_id=other, name="code-reviewer", version="1.0.0")
            is None
        )
        assert (
            await store.publish_draft(
                tenant_id=other, name="code-reviewer", version="1.0.0", updated_by="mallory"
            )
            is None
        )
    finally:
        await engine.dispose()
