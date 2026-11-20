"""B-326 —— ``ArtifactShareLinkStore``:内存实现与 SQL 实现跑同一组用例。

* 链接 id 是 16 位 URL 安全随机串,每次都不同;
* ``mint_or_reuse``:同一 (对话, 版本 id) 还能用(没过期、剩余时间够、下载次数没用完、
  剩余时间不超过当前有效期)就复用同一条,否则新铸;别的对话 / 用户 / 租户各算各的;
* ``newest_for`` 一次查询按 (对话, 版本 id) 取最新一条(过期的也算);
* ``claim_download`` 原子计数,到上限就不再放行,``refund_download`` 还回一次、不低于 0;
* ``get_public`` / ``claim_download`` / ``refund_download`` 是跨租户的按 id 读写,SQL 实现
  不在 RLS 旁路作用域里就拒绝;
* ``delete_for_threads`` 只删这个租户这几段对话的;
* SQL:并发的 ``mint_or_reuse`` 只铸出一条;并发的 ``claim_download`` 恰好放行 ``max`` 次。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlArtifactStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.artifact_share import (
    LINK_ID_LENGTH,
    ArtifactShareLink,
    ArtifactShareLinkStore,
    InMemoryArtifactShareLinkStore,
    SqlArtifactShareLinkStore,
    new_link_id,
)
from expert_work.persistence.rls import RlsBypassRequiredError, bypass_rls_session

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_URLSAFE = re.compile(r"^[A-Za-z0-9_-]+$")
_TTL = timedelta(hours=2)


def test_new_link_id_is_16_urlsafe_chars_and_random() -> None:
    ids = {new_link_id() for _ in range(200)}
    assert len(ids) == 200
    for link_id in ids:
        assert len(link_id) == LINK_ID_LENGTH == 16
        assert _URLSAFE.match(link_id)


async def _mint(
    store: ArtifactShareLinkStore,
    *,
    tenant_id: UUID,
    user_id: UUID,
    thread_id: UUID,
    version_id: UUID,
    now: datetime | None = None,
    ttl: timedelta = _TTL,
    name: str = "report.pdf",
    version: int = 1,
    max_downloads: int = 20,
) -> ArtifactShareLink:
    return await store.mint_or_reuse(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key="agent-a",
        artifact_id=uuid4(),
        artifact_version_id=version_id,
        version=version,
        thread_id=thread_id,
        run_id=uuid4(),
        file_name=name,
        ttl=ttl,
        now=now or datetime.now(UTC),
        max_downloads=max_downloads,
    )


async def _contract(store: ArtifactShareLinkStore, version_id: UUID) -> None:
    tenant, user, thread = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    first = await _mint(
        store, tenant_id=tenant, user_id=user, thread_id=thread, version_id=version_id, now=now
    )
    assert len(first.id) == LINK_ID_LENGTH
    # The SQL store stamps with the later of ``now`` and the database clock.
    assert first.created_at >= now
    assert first.expires_at == first.created_at + _TTL
    assert first.file_name == "report.pdf"
    assert first.download_count == 0
    # Live → the same link, whoever asks in this conversation.
    again = await _mint(
        store,
        tenant_id=tenant,
        user_id=user,
        thread_id=thread,
        version_id=version_id,
        now=now + timedelta(minutes=5),
    )
    assert again.id == first.id
    # Another conversation / user / tenant never reuses it.
    others: list[tuple[UUID, UUID, UUID]] = [
        (tenant, user, uuid4()),
        (tenant, uuid4(), thread),
        (uuid4(), user, thread),
    ]
    for o_tenant, o_user, o_thread in others:
        other = await _mint(
            store,
            tenant_id=o_tenant,
            user_id=o_user,
            thread_id=o_thread,
            version_id=version_id,
            now=now,
        )
        assert other.id != first.id
    # Expired → a new one, same version and conversation.
    later = now + _TTL + timedelta(seconds=1)
    fresh = await _mint(
        store, tenant_id=tenant, user_id=user, thread_id=thread, version_id=version_id, now=later
    )
    assert fresh.id != first.id
    assert fresh.expires_at == later + _TTL

    found = await store.newest_for(
        tenant_id=tenant,
        user_id=user,
        keys=[(thread, version_id), (uuid4(), version_id), (thread, uuid4())],
    )
    assert found == {(thread, version_id): fresh}
    assert (
        await store.newest_for(tenant_id=tenant, user_id=uuid4(), keys=[(thread, version_id)]) == {}
    )
    assert (
        await store.newest_for(tenant_id=uuid4(), user_id=user, keys=[(thread, version_id)]) == {}
    )
    assert await store.newest_for(tenant_id=tenant, user_id=user, keys=[]) == {}

    async with bypass_rls_session():
        public = await store.get_public(link_id=fresh.id)
        assert public is not None
        assert public.id == fresh.id
        assert await store.get_public(link_id="unknown-id-00000") is None
        assert [
            await store.claim_download(link_id=fresh.id, max_downloads=2) for _ in range(3)
        ] == [True, True, False]
        await store.refund_download(link_id=fresh.id)
        assert await store.claim_download(link_id=fresh.id, max_downloads=2) is True
        for _ in range(4):
            await store.refund_download(link_id=fresh.id)
        floor = await store.get_public(link_id=fresh.id)
        assert floor is not None
        assert floor.download_count == 0
        await store.refund_download(link_id="unknown-id-00000")
        assert await store.claim_download(link_id="unknown-id-00000", max_downloads=2) is False
        # Nothing is deleted on access: the expired row is still there.
        expired = await store.get_public(link_id=first.id)
        assert expired is not None
        assert expired.is_expired(later)

    # Conversation purge: only that tenant's rows of those conversations go.
    assert await store.delete_for_threads(tenant_id=uuid4(), thread_ids=[thread]) == 0
    # This tenant's rows in the conversation: ``first``, ``fresh`` and the other user's.
    assert await store.delete_for_threads(tenant_id=tenant, thread_ids=[thread]) == 3
    assert await store.delete_for_threads(tenant_id=tenant, thread_ids=[]) == 0
    assert await store.newest_for(tenant_id=tenant, user_id=user, keys=[(thread, version_id)]) == {}


@pytest.mark.asyncio
async def test_in_memory_store_contract() -> None:
    await _contract(InMemoryArtifactShareLinkStore(), uuid4())


async def _purge_contract(store: ArtifactShareLinkStore) -> None:
    """L3 —— 清除用户 / 清除智能体时一并删链接(智能体清除可留下 ``keep_users`` 的)。"""
    tenant, other_tenant = uuid4(), uuid4()
    alice, bob = uuid4(), uuid4()

    async def mint(tenant_id: UUID, user_id: UUID, agent: str) -> None:
        await store.mint_or_reuse(
            tenant_id=tenant_id,
            user_id=user_id,
            agent_key=agent,
            artifact_id=uuid4(),
            artifact_version_id=uuid4(),
            version=1,
            thread_id=uuid4(),
            run_id=None,
            file_name="r.pdf",
            ttl=_TTL,
            now=datetime.now(UTC),
            max_downloads=20,
        )

    for user in (alice, bob):
        for agent in ("agent-a", "agent-b"):
            await mint(tenant, user, agent)
    await mint(other_tenant, alice, "agent-a")
    assert await store.delete_for_user(tenant_id=tenant, user_id=alice) == 2
    assert await store.delete_for_user(tenant_id=tenant, user_id=alice) == 0
    assert (
        await store.delete_for_agent(tenant_id=tenant, agent_key="agent-b", keep_users=[bob]) == 0
    )
    assert await store.delete_for_agent(tenant_id=tenant, agent_key="agent-b") == 1
    assert await store.delete_for_agent(tenant_id=tenant, agent_key="agent-a") == 1
    # The other tenant's row is untouched.
    assert await store.delete_for_user(tenant_id=other_tenant, user_id=alice) == 1


@pytest.mark.asyncio
async def test_in_memory_purges() -> None:
    await _purge_contract(InMemoryArtifactShareLinkStore())


def test_is_expired_boundary() -> None:
    now = datetime.now(UTC)
    link = ArtifactShareLink(
        id="x" * 16,
        tenant_id=uuid4(),
        user_id=uuid4(),
        agent_key="a",
        artifact_id=uuid4(),
        artifact_version_id=uuid4(),
        version=1,
        run_id=None,
        thread_id=uuid4(),
        created_at=now - timedelta(minutes=7),
        expires_at=now,
    )
    assert link.is_expired(now)
    assert not link.is_expired(now - timedelta(seconds=1))


def _link(*, now: datetime, left: timedelta, downloads: int = 0) -> ArtifactShareLink:
    return ArtifactShareLink(
        id="x" * 16,
        tenant_id=uuid4(),
        user_id=uuid4(),
        agent_key="a",
        artifact_id=uuid4(),
        artifact_version_id=uuid4(),
        version=1,
        run_id=None,
        thread_id=uuid4(),
        created_at=now - timedelta(minutes=1),
        expires_at=now + left,
        download_count=downloads,
    )


def test_reusable_needs_downloads_and_time_left_within_the_current_lifetime() -> None:
    """M2 —— 用完了次数、或快过期的链接不再发出去;有效期调短后,比新有效期还长的也不再发。"""
    now = datetime.now(UTC)
    two_h = timedelta(hours=2)
    assert _link(now=now, left=timedelta(hours=1)).reusable(now, ttl=two_h, max_downloads=20)
    # Downloads used up — or only one left (L2: a forward or a retry would use it up).
    assert not _link(now=now, left=timedelta(hours=1), downloads=20).reusable(
        now, ttl=two_h, max_downloads=20
    )
    assert not _link(now=now, left=timedelta(hours=1), downloads=19).reusable(
        now, ttl=two_h, max_downloads=20
    )
    assert _link(now=now, left=timedelta(hours=1), downloads=18).reusable(
        now, ttl=two_h, max_downloads=20
    )
    # A one-download platform still reuses an unused link.
    assert _link(now=now, left=timedelta(hours=1)).reusable(now, ttl=two_h, max_downloads=1)
    # About to expire: under min(5 min, ttl / 4) left.
    assert _link(now=now, left=timedelta(minutes=5)).reusable(now, ttl=two_h, max_downloads=20)
    assert not _link(now=now, left=timedelta(minutes=4, seconds=59)).reusable(
        now, ttl=two_h, max_downloads=20
    )
    # A short lifetime scales the margin down to a quarter of it.
    ten_min = timedelta(minutes=10)
    assert _link(now=now, left=timedelta(minutes=3)).reusable(now, ttl=ten_min, max_downloads=20)
    assert not _link(now=now, left=timedelta(minutes=2)).reusable(
        now, ttl=ten_min, max_downloads=20
    )
    # Longer to go than the current lifetime (it was lowered) → not reused; a minute of
    # clock slack is allowed (M2: concurrent readers' clocks).
    assert _link(now=now, left=timedelta(minutes=11)).reusable(now, ttl=ten_min, max_downloads=20)
    assert not _link(now=now, left=timedelta(minutes=11, seconds=1)).reusable(
        now, ttl=ten_min, max_downloads=20
    )
    assert not _link(now=now, left=timedelta(0)).reusable(now, ttl=two_h, max_downloads=20)


_CANNOT_SERVE = ["used_up", "about_to_expire", "lifetime_lowered"]


@pytest.mark.asyncio
@pytest.mark.parametrize("why", _CANNOT_SERVE)
async def test_mint_or_reuse_replaces_a_link_that_cannot_serve(why: str) -> None:
    await _replaces_a_link_that_cannot_serve(InMemoryArtifactShareLinkStore(), why)


@pytest.mark.asyncio
async def test_in_memory_newest_for_goes_by_created_at_not_insertion_order() -> None:
    store = InMemoryArtifactShareLinkStore()
    tenant, user, thread, version_id = uuid4(), uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    newer = await _mint(
        store, tenant_id=tenant, user_id=user, thread_id=thread, version_id=version_id, now=now
    )
    older = replace(newer, id="o" * 16, created_at=now - timedelta(hours=1))
    store.put(older)
    found = await store.newest_for(tenant_id=tenant, user_id=user, keys=[(thread, version_id)])
    assert found == {(thread, version_id): newer}


async def _replaces_a_link_that_cannot_serve(store: ArtifactShareLinkStore, why: str) -> None:
    tenant, user, thread, version_id = uuid4(), uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    first = await _mint(
        store, tenant_id=tenant, user_id=user, thread_id=thread, version_id=version_id, now=now
    )
    ask_at, ttl, max_downloads = now + timedelta(minutes=1), _TTL, 20
    if why == "used_up":
        # 19 of 20 served: one left is not enough to hand out (L2).
        async with bypass_rls_session():
            for _ in range(19):
                assert await store.claim_download(link_id=first.id, max_downloads=20)
    elif why == "about_to_expire":
        ask_at = first.expires_at - timedelta(minutes=4)
    else:
        ttl = timedelta(minutes=30)
        ask_at = now + timedelta(minutes=2)  # past the clock slack
    again = await _mint(
        store,
        tenant_id=tenant,
        user_id=user,
        thread_id=thread,
        version_id=version_id,
        now=ask_at,
        ttl=ttl,
        max_downloads=max_downloads,
    )
    assert again.id != first.id
    assert again.expires_at == ask_at + ttl


# --------------------------------------------------------------------- SQL


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
def sql_stores(
    postgres_container: PostgresContainer,
) -> Iterator[tuple[SqlArtifactShareLinkStore, SqlArtifactStore, Any]]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    factory = create_async_session_factory(engine)
    yield SqlArtifactShareLinkStore(factory), SqlArtifactStore(factory), engine


@pytest.fixture
async def sql_env(
    sql_stores: tuple[SqlArtifactShareLinkStore, SqlArtifactStore, Any],
) -> AsyncIterator[tuple[SqlArtifactShareLinkStore, SqlArtifactStore]]:
    links, artifacts, engine = sql_stores
    try:
        yield links, artifacts
    finally:
        await engine.dispose()


async def _version(artifacts: SqlArtifactStore, tenant: UUID, user: UUID) -> UUID:
    version = await artifacts.save_version(
        tenant_id=tenant,
        user_id=user,
        agent_key="agent-a",
        name=f"report-{uuid4().hex[:6]}.pdf",
        kind="document",
        path_in_workspace="report.pdf",
        created_in_thread="t-1",
    )
    return version.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sql_store_contract(
    sql_env: tuple[SqlArtifactShareLinkStore, SqlArtifactStore],
) -> None:
    links, artifacts = sql_env
    await _contract(links, await _version(artifacts, uuid4(), uuid4()))


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("why", _CANNOT_SERVE)
async def test_sql_mint_or_reuse_replaces_a_link_that_cannot_serve(
    sql_env: tuple[SqlArtifactShareLinkStore, SqlArtifactStore], why: str
) -> None:
    links, _ = sql_env
    await _replaces_a_link_that_cannot_serve(links, why)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sql_purges(
    sql_env: tuple[SqlArtifactShareLinkStore, SqlArtifactStore],
) -> None:
    links, _ = sql_env
    await _purge_contract(links)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sql_public_reads_require_bypass_scope(
    sql_env: tuple[SqlArtifactShareLinkStore, SqlArtifactStore],
) -> None:
    links, _ = sql_env
    with pytest.raises(RlsBypassRequiredError):
        await links.get_public(link_id="whatever-0000000")
    with pytest.raises(RlsBypassRequiredError):
        await links.claim_download(link_id="whatever-0000000", max_downloads=1)
    with pytest.raises(RlsBypassRequiredError):
        await links.refund_download(link_id="whatever-0000000")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sql_concurrent_claims_admit_exactly_max(
    sql_env: tuple[SqlArtifactShareLinkStore, SqlArtifactStore],
) -> None:
    links, _ = sql_env
    link = await _mint(
        links, tenant_id=uuid4(), user_id=uuid4(), thread_id=uuid4(), version_id=uuid4()
    )
    async with bypass_rls_session():
        results = await asyncio.gather(
            *(links.claim_download(link_id=link.id, max_downloads=5) for _ in range(25))
        )
        stored = await links.get_public(link_id=link.id)
    assert sum(results) == 5
    assert stored is not None
    assert stored.download_count == 5


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sql_concurrent_mints_make_one_live_link(
    sql_env: tuple[SqlArtifactShareLinkStore, SqlArtifactStore],
) -> None:
    links, _ = sql_env
    tenant, user, thread, version_id = uuid4(), uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    minted = await asyncio.gather(
        *(
            _mint(
                links,
                tenant_id=tenant,
                user_id=user,
                thread_id=thread,
                version_id=version_id,
                now=now,
            )
            for _ in range(10)
        )
    )
    assert len({link.id for link in minted}) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sql_round_trip_preserves_fields(
    sql_env: tuple[SqlArtifactShareLinkStore, SqlArtifactStore],
) -> None:
    links, artifacts = sql_env
    tenant, user, thread = uuid4(), uuid4(), uuid4()
    version_id = await _version(artifacts, tenant, user)
    created = await _mint(
        links,
        tenant_id=tenant,
        user_id=user,
        thread_id=thread,
        version_id=version_id,
        ttl=timedelta(minutes=7),
        name="7天血糖报告.pdf",
        version=3,
    )
    fetched = await links.newest_for(tenant_id=tenant, user_id=user, keys=[(thread, version_id)])
    assert fetched == {(thread, version_id): created}
    assert created.expires_at - created.created_at == timedelta(minutes=7)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sql_staggered_clocks_still_make_one_link(
    sql_env: tuple[SqlArtifactShareLinkStore, SqlArtifactStore],
) -> None:
    """M2 —— 并发的续链各自读的时钟前后差几秒(或在不同副本上):只铸一条。"""
    links, _ = sql_env
    tenant, user, thread, version_id = uuid4(), uuid4(), uuid4(), uuid4()
    base = datetime.now(UTC)
    minted = await asyncio.gather(
        *(
            _mint(
                links,
                tenant_id=tenant,
                user_id=user,
                thread_id=thread,
                version_id=version_id,
                now=base + timedelta(seconds=offset),
            )
            for offset in (9, 0, 5, 2, 7, 1, 8, 3, 6, 4)
        )
    )
    assert len({link.id for link in minted}) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sql_a_lagging_caller_clock_still_reuses(
    sql_env: tuple[SqlArtifactShareLinkStore, SqlArtifactStore],
) -> None:
    """M2 —— 调用方的时钟落后很多(读时钟之后等了很久的锁 / 慢的副本):以数据库时钟为准。"""
    links, _ = sql_env
    tenant, user, thread, version_id = uuid4(), uuid4(), uuid4(), uuid4()
    first = await _mint(
        links, tenant_id=tenant, user_id=user, thread_id=thread, version_id=version_id
    )
    lagging = await _mint(
        links,
        tenant_id=tenant,
        user_id=user,
        thread_id=thread,
        version_id=version_id,
        now=datetime.now(UTC) - timedelta(hours=2),
    )
    assert lagging.id == first.id
