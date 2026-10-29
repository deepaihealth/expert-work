"""B-235 —— 版本行带副本:``ArtifactStore`` 的新契约,**两个后端跑同一组断言**。

每次 ``save_artifact`` 都把那一刻的字节复制一份(副本以版本行 id 命名),版本行记
``snapshot_key`` + 当场算好的 ``size_bytes`` / ``sha256``。读侧要能按**版本号**取到某一
版(对话页取「这一轮登记的那一版」,而不是最新版),留存作业要能问「这个工作区路径还有
没有别的版本在用」和「这些副本还有没有版本行」。

SQL 档在容器里另建一个库(理由同 ``test_artifact_store_agent_scope_contract``)。
本地跑 SQL 档前: export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
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
from expert_work.persistence.rls import RlsBypassRequiredError, bypass_rls_var
from expert_work.protocol import ArtifactVersion

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "artifact_version_snapshot_contract"

_AGENT = "report-writer-1a2b3c4d"
_PATH = f"agents/{_AGENT}/report.pdf"


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
    store: ArtifactStore,
    *,
    tenant_id: UUID,
    user_id: UUID,
    agent_key: str = _AGENT,
    name: str = "report.pdf",
    path: str = _PATH,
    content: bytes | None = b"x",
) -> ArtifactVersion:
    """``content=None`` = 上线前的老写法(不带副本)。"""
    if content is None:
        return await store.save_version(
            tenant_id=tenant_id,
            user_id=user_id,
            agent_key=agent_key,
            name=name,
            kind="document",
            path_in_workspace=path,
            created_in_thread=str(uuid4()),
        )
    version_id = uuid4()
    return await store.save_version(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=agent_key,
        name=name,
        kind="document",
        path_in_workspace=path,
        created_in_thread=str(uuid4()),
        version_id=version_id,
        snapshot_key=str(version_id),
        size_bytes=len(content),
        sha256=f"sha-{content.decode()}",
    )


async def test_save_version_keeps_the_snapshot_fields(store: ArtifactStore) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    saved = await _save(store, tenant_id=tenant_id, user_id=user_id, content=b"AAA")

    assert saved.snapshot_key == str(saved.id), "副本键必须就是调用方给的版本行 id"
    assert (saved.size_bytes, saved.sha256) == (3, "sha-AAA")
    rows = await store.list_versions(
        tenant_id=tenant_id, user_id=user_id, artifact_id=saved.artifact_id
    )
    assert rows is not None and len(rows) == 1
    [listed] = rows
    assert (listed.id, listed.snapshot_key, listed.size_bytes, listed.sha256) == (
        saved.id,
        str(saved.id),
        3,
        "sha-AAA",
    )


async def test_legacy_save_has_no_snapshot(store: ArtifactStore) -> None:
    saved = await _save(store, tenant_id=uuid4(), user_id=uuid4(), content=None)

    assert saved.snapshot_key is None
    assert saved.size_bytes is None


async def test_get_version_by_id_returns_that_version_not_the_latest(
    store: ArtifactStore,
) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    first = await _save(store, tenant_id=tenant_id, user_id=user_id, content=b"AAA")
    await _save(store, tenant_id=tenant_id, user_id=user_id, content=b"BBB")

    got = await store.get_version_by_id(
        tenant_id=tenant_id, user_id=user_id, artifact_id=first.artifact_id, version=1
    )

    assert got is not None
    assert (got.id, got.sha256) == (first.id, "sha-AAA")


async def test_get_version_by_id_hides_other_user_deleted_and_unknown(
    store: ArtifactStore,
) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    saved = await _save(store, tenant_id=tenant_id, user_id=user_id)

    async def _get(**over: object) -> ArtifactVersion | None:
        args: dict[str, object] = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "artifact_id": saved.artifact_id,
            "version": 1,
        }
        args.update(over)
        return await store.get_version_by_id(**args)  # type: ignore[arg-type]

    assert await _get() is not None
    assert await _get(user_id=uuid4()) is None, "跨用户取到了别人的版本"
    assert await _get(tenant_id=uuid4()) is None, "跨租户取到了别人的版本"
    assert await _get(version=2) is None
    await store.soft_delete(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=_AGENT,
        name="report.pdf",
        now=datetime.now(UTC),
    )
    assert await _get() is None, "软删的产物仍能按版本取到"


async def test_get_version_by_id(store: ArtifactStore) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    first = await _save(store, tenant_id=tenant_id, user_id=user_id, content=b"AAA")
    await _save(store, tenant_id=tenant_id, user_id=user_id, content=b"BBB")

    got = await store.get_version_by_id(
        tenant_id=tenant_id, user_id=user_id, artifact_id=first.artifact_id, version=1
    )
    assert got is not None and got.id == first.id
    assert (
        await store.get_version_by_id(
            tenant_id=tenant_id, user_id=uuid4(), artifact_id=first.artifact_id, version=1
        )
        is None
    )


@pytest.fixture
def rls_bypass() -> Iterator[None]:
    """两个跨租户方法只给留存作业用,它整个 sweep 都在 RLS bypass 里跑。"""
    token = bypass_rls_var.set(True)
    try:
        yield
    finally:
        bypass_rls_var.reset(token)


@pytest.mark.usefixtures("rls_bypass")
async def test_paths_still_registered_ignores_the_excluded_versions(store: ArtifactStore) -> None:
    """留存作业删 v1 之前问:这个路径还有没有**别的**版本在用?"""
    tenant_id, user_id = uuid4(), uuid4()
    v1 = await _save(store, tenant_id=tenant_id, user_id=user_id)
    v2 = await _save(store, tenant_id=tenant_id, user_id=user_id)
    lonely = await _save(
        store, tenant_id=tenant_id, user_id=user_id, name="notes.md", path="agents/x/notes.md"
    )

    in_use = await store.paths_still_registered(
        tenant_id=tenant_id,
        user_id=user_id,
        paths=[_PATH, "agents/x/notes.md"],
        excluding_version_ids=[v1.id, lonely.id],
    )
    assert in_use == {_PATH}, "v2 还指着同一个路径,却被当成没人用了"

    gone = await store.paths_still_registered(
        tenant_id=tenant_id,
        user_id=user_id,
        paths=[_PATH],
        excluding_version_ids=[v1.id, v2.id],
    )
    assert gone == set()

    other_user = await store.paths_still_registered(
        tenant_id=tenant_id, user_id=uuid4(), paths=[_PATH], excluding_version_ids=[]
    )
    assert other_user == set(), "别的用户的同名路径不该算"


@pytest.mark.usefixtures("rls_bypass")
async def test_existing_version_ids(store: ArtifactStore) -> None:
    saved = await _save(store, tenant_id=uuid4(), user_id=uuid4())
    stray = uuid4()

    assert await store.existing_version_ids([saved.id, stray]) == {saved.id}
    assert await store.existing_version_ids([]) == set()


async def test_cross_tenant_lookups_refuse_to_run_without_the_rls_bypass(
    store: ArtifactStore,
) -> None:
    """强制 RLS 后,没声明 bypass 的会话这两个查询都会拿到空集 —— 孤儿清理会把每个租户
    满 24 小时的副本都当成孤儿删掉,同路径判断也会说「没人在用」。所以 SQL 档没 bypass
    就直接拒绝,不给一个看起来正常的空答案。内存档没有 RLS,不适用。"""
    if isinstance(store, InMemoryArtifactStore):
        pytest.skip("RLS only exists on the SQL store")
    saved = await _save(store, tenant_id=uuid4(), user_id=uuid4())
    with pytest.raises(RlsBypassRequiredError):
        await store.existing_version_ids([saved.id])
    with pytest.raises(RlsBypassRequiredError):
        await store.paths_still_registered(
            tenant_id=saved.tenant_id,
            user_id=saved.user_id,
            paths=[_PATH],
            excluding_version_ids=[],
        )
