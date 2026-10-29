"""B-235 —— 留存作业处理产物版本副本。

每次 ``save_artifact`` 都在 ``{root}/{tenant}/.artifact-versions/{user}/{version_id}`` 留一份
那一刻的字节。这里钉:

* 版本到期 → 副本跟着版本行一起删;副本删不掉 → 行留着明天再试(登记行是副本唯一的索引);
* 同一路径还有别的版本在用时**不删**工作区文件(同名重登记的各版本指着同一个文件,删老
  版本时新版本还在用它 —— B-235 之前就有的问题,这里一并修掉);
* 软删到期的硬删、已删工作区的 90 天销账,都把副本一起清;
* 孤儿副本(找不到版本行,如登记失败后没删掉、清除用户时没删掉)满 24 小时删掉,
  ``.tmp-`` 残件同样;不是 UUID 名字的东西不碰。
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from expert_work.persistence import (
    ArtifactStore,
    DatabaseConfig,
    InMemoryArtifactStore,
    InMemoryUserWorkspaceStore,
    SqlArtifactStore,
    artifact_snapshot_dir,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.rls import RlsBypassRequiredError, bypass_rls_var
from expert_work.protocol import ArtifactVersion
from retention_cleanup_job.artifact_copies import sweep_orphan_copies
from retention_cleanup_job.job import RetentionCleanupJob

_AGENT = "writer-0badc0de"
_OLD = datetime.now(UTC) - timedelta(days=100)
_FRESH = datetime.now(UTC) - timedelta(days=1)


class _World:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.artifacts = InMemoryArtifactStore()
        self.tenant = uuid4()
        self.user = uuid4()

    def job(self, **overrides: Any) -> RetentionCleanupJob:
        kwargs: dict[str, Any] = {
            "db_session_factory": lambda: None,
            "artifact_store": self.artifacts,
            "workspace_root": str(self.root),
            "artifact_retention_days": 90,
        }
        kwargs.update(overrides)
        return RetentionCleanupJob(**kwargs)

    def workspace_file(self, rel: str, data: bytes = b"x") -> Path:
        target = self.root / str(self.tenant) / str(self.user) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def copies(self, user: UUID | None = None) -> Path:
        return artifact_snapshot_dir(str(self.root), self.tenant, user or self.user)

    async def save(
        self, *, path: str, created_at: datetime, name: str = "report.txt", copy: bool = True
    ) -> ArtifactVersion:
        version_id = uuid4()
        version = await self.artifacts.save_version(
            tenant_id=self.tenant,
            user_id=self.user,
            agent_key=_AGENT,
            name=name,
            kind="document",
            path_in_workspace=path,
            created_in_thread="t",
            version_id=version_id if copy else None,
            snapshot_key=str(version_id) if copy else None,
            size_bytes=1 if copy else None,
            sha256="s" if copy else None,
        )
        self.artifacts._versions = [  # backdate: the in-memory store stamps now()
            v.model_copy(update={"created_at": created_at}) if v.id == version.id else v
            for v in self.artifacts._versions
        ]
        if copy:
            self.copies().mkdir(parents=True, exist_ok=True)
            (self.copies() / str(version_id)).write_bytes(b"copy")
        return version

    async def versions(self, name: str = "report.txt") -> list[ArtifactVersion]:
        rows = await self.artifacts.list_for_user(
            tenant_id=self.tenant, user_id=self.user, agent_key=_AGENT, include_deleted=True
        )
        artifact = next(a for a in rows if a.name == name)
        return await self.artifacts.list_versions_by_artifact(artifact_id=artifact.id)


@pytest.fixture
def world(tmp_path: Path) -> _World:
    return _World(tmp_path)


async def test_an_expired_version_takes_its_copy_with_it(world: _World) -> None:
    old = await world.save(path="agents/w/a.txt", created_at=_OLD, name="a.txt")
    fresh = await world.save(path="agents/w/b.txt", created_at=_FRESH, name="b.txt")
    world.workspace_file("agents/w/a.txt")

    report = await world.job()._sweep_artifact_versions()

    assert not (world.copies() / str(old.id)).exists(), "到期版本的副本没删"
    assert (world.copies() / str(fresh.id)).exists()
    assert report[0] == 1


async def test_a_copy_that_cannot_be_removed_keeps_its_row(world: _World) -> None:
    old = await world.save(path="agents/w/a.txt", created_at=_OLD)
    copy = world.copies() / str(old.id)
    copy.unlink()
    copy.mkdir()  # 不是普通文件 —— 删不掉,也不该去删

    deleted, _files, _expired = await world.job()._sweep_artifact_versions()

    assert deleted == 0
    assert [v.id for v in await world.versions()] == [old.id], "副本还在,登记行却被删了"


async def test_the_workspace_file_stays_while_a_newer_version_points_at_it(world: _World) -> None:
    """同名重登记的 v1(过期)与 v2(新)指着同一个文件:删 v1 不许删掉 v2 还在用的文件。"""
    shared = "agents/w/report.txt"
    v1 = await world.save(path=shared, created_at=_OLD)
    v2 = await world.save(path=shared, created_at=_FRESH)
    target = world.workspace_file(shared)

    deleted, files_removed, _ = await world.job()._sweep_artifact_versions()

    assert deleted == 1
    assert target.exists(), "删老版本时把新版本还在用的工作区文件删了"
    assert files_removed == 0
    assert not (world.copies() / str(v1.id)).exists()
    assert (world.copies() / str(v2.id)).exists()


async def test_the_workspace_file_goes_once_no_version_points_at_it(world: _World) -> None:
    shared = "agents/w/report.txt"
    await world.save(path=shared, created_at=_OLD)
    await world.save(path=shared, created_at=_OLD)
    target = world.workspace_file(shared)

    deleted, files_removed, expired = await world.job()._sweep_artifact_versions()

    assert (deleted, files_removed, expired) == (2, 1, 1)
    assert not target.exists()
    assert list(world.copies().iterdir()) == []


async def test_a_pre_release_version_without_copy_still_expires(world: _World) -> None:
    await world.save(path="agents/w/a.txt", created_at=_OLD, copy=False)
    target = world.workspace_file("agents/w/a.txt")

    deleted, files_removed, _ = await world.job()._sweep_artifact_versions()

    assert (deleted, files_removed) == (1, 1)
    assert not target.exists()


async def test_hard_delete_after_grace_removes_the_remaining_copies(world: _World) -> None:
    fresh = await world.save(path="agents/w/a.txt", created_at=_FRESH)
    rows = await world.artifacts.list_for_user(
        tenant_id=world.tenant, user_id=world.user, agent_key=_AGENT
    )
    await world.artifacts.soft_delete(
        tenant_id=world.tenant,
        user_id=world.user,
        agent_key=_AGENT,
        name=rows[0].name,
        now=datetime.now(UTC) - timedelta(days=200),
    )

    _soft, hard = await world.job()._sweep_artifacts()

    assert hard == 1
    assert not (world.copies() / str(fresh.id)).exists(), "硬删了产物却留下了它的副本"


async def test_workspace_hard_delete_removes_the_users_copy_directory(world: _World) -> None:
    workspaces = InMemoryUserWorkspaceStore()
    ws = await workspaces.resolve(tenant_id=world.tenant, user_id=world.user)
    await workspaces.soft_delete(workspace_id=ws.id, now=datetime.now(UTC) - timedelta(days=200))
    await workspaces.mark_archived(workspace_id=ws.id, archived_object_key="k")
    await world.save(path="agents/w/a.txt", created_at=_FRESH)
    other = uuid4()
    world.copies(other).mkdir(parents=True)
    (world.copies(other) / str(uuid4())).write_bytes(b"keep")

    hard, _pending = await world.job(workspace_store=workspaces)._sweep_workspaces()

    assert hard == 1
    assert not world.copies().exists(), "销账后这个用户的副本目录还在"
    assert len(list(world.copies(other).iterdir())) == 1


def _age(path: Path, hours: float) -> None:
    stamp = time.time() - hours * 3600
    os.utime(path, (stamp, stamp))


async def test_orphan_copies_are_removed_after_a_day(world: _World) -> None:
    kept = await world.save(path="agents/w/a.txt", created_at=_FRESH)
    d = world.copies()
    orphan_old = d / str(uuid4())
    orphan_young = d / str(uuid4())
    tmp_old = d / f"{uuid4()}.tmp-{uuid4().hex}"
    tmp_young = d / f"{uuid4()}.tmp-{uuid4().hex}"
    stranger = d / "README"
    for p in (orphan_old, orphan_young, tmp_old, tmp_young, stranger):
        p.write_bytes(b"x")
    for p in (orphan_old, tmp_old, stranger, d / str(kept.id)):
        _age(p, 25)
    _age(orphan_young, 2)
    _age(tmp_young, 2)

    removed = await sweep_orphan_copies(str(world.root), world.artifacts)

    assert removed == 2
    left = {p.name for p in d.iterdir()}
    assert left == {str(kept.id), orphan_young.name, tmp_young.name, "README"}


async def test_orphan_sweep_ignores_a_symlinked_copy_directory(world: _World) -> None:
    outside = world.root / "outside"
    outside.mkdir()
    victim = outside / str(uuid4())
    victim.write_bytes(b"not ours")
    _age(victim, 48)
    world.copies().parent.mkdir(parents=True)
    world.copies().symlink_to(outside)

    assert await sweep_orphan_copies(str(world.root), world.artifacts) == 0
    assert victim.exists()


async def test_a_kept_row_keeps_its_copy(world: _World) -> None:
    """工作区文件删不掉 → 行留着明天再试;那这一版的副本也必须还在,否则留下的行
    指着一个已经没了的副本,下载 404。"""
    old = await world.save(path="agents/w/a.txt", created_at=_OLD)
    (world.root / str(world.tenant) / str(world.user) / "agents/w/a.txt").mkdir(parents=True)

    deleted, _files, _expired = await world.job()._sweep_artifact_versions()

    assert deleted == 0
    assert [v.id for v in await world.versions()] == [old.id]
    assert (world.copies() / str(old.id)).exists(), "行留着,副本却先删了"


# --------------------------------------------------------------------------- RLS 闸(SQL)

_ALEMBIC_INI = (
    Path(__file__).resolve().parents[3] / "packages" / "expert-work-persistence" / "alembic.ini"
)
_DB = "retention_artifact_copies_rls"


@pytest.fixture
async def sql_artifacts(postgres_container: Any) -> AsyncIterator[ArtifactStore]:
    admin = str(postgres_container.get_connection_url()).replace("+psycopg2", "")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{_DB}"')
    base, _, _ = admin.rpartition("/")
    dsn = f"{base}/{_DB}"
    cfg = Config(str(_ALEMBIC_INI))
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


@pytest.mark.integration
async def test_orphan_sweep_without_the_rls_bypass_deletes_nothing(
    world: _World, sql_artifacts: ArtifactStore
) -> None:
    """强制 RLS 且没声明 bypass 时,「哪些副本还有版本行」会答「一个都没有」—— 那样每个租户
    满 24 小时的副本都会被当成孤儿删掉。必须拒绝运行,一个文件都不删。"""
    d = world.copies()
    d.mkdir(parents=True)
    copy = d / str(uuid4())
    copy.write_bytes(b"x")
    _age(copy, 48)

    with pytest.raises(RlsBypassRequiredError):
        await sweep_orphan_copies(str(world.root), sql_artifacts)

    assert copy.exists()


class _ExplodingStore(InMemoryArtifactStore):
    async def existing_version_ids(self, ids: Any) -> set[UUID]:
        raise RuntimeError("db down")


async def test_a_failing_orphan_pass_does_not_skip_the_later_passes(world: _World) -> None:
    """孤儿副本这一 pass 查库失败:记日志、计 0,后面的 pass 照跑(今天的上传 / 线程 /
    记忆 / 工作区清理不能因为它整个跳过)。"""
    stray = world.copies() / str(uuid4())
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"x")
    _age(stray, 48)
    job = world.job(artifact_store=_ExplodingStore())
    later: list[str] = []

    async def _zero() -> int:
        return 0

    async def _audit_log() -> tuple[int, dict[str, int]]:
        return 0, {}

    async def _memory() -> int:
        later.append("memory")
        return 0

    for name in (
        "_count_unacked_past_retention",
        "_delete_event_log",
        "_delete_sandbox_egress_audit",
        "_delete_finished_memory_writeback_jobs",
        "_delete_finished_knowledge_tasks",
        "_delete_orphaned_knowledge_revisions",
        "_delete_expired_knowledge_retrieval_logs",
        "_delete_expired_jwt_blacklist",
    ):
        setattr(job, name, _zero)
    job._delete_audit_log = _audit_log  # type: ignore[method-assign]
    job._sweep_memory = _memory  # type: ignore[method-assign]

    report = await job.run_once()

    assert later == ["memory"], "孤儿副本 pass 一失败,后面的 pass 都没跑"
    assert report.orphan_artifact_copies_removed == 0
    assert stray.exists()


# --------------------------------------------------------------------------- 删之前的两道把关


def _orphans(world: _World, n: int) -> list[Path]:
    d = world.copies()
    d.mkdir(parents=True, exist_ok=True)
    paths = []
    for _ in range(n):
        p = d / str(uuid4())
        p.write_bytes(b"x")
        _age(p, 48)
        paths.append(p)
    return paths


async def test_a_mass_of_orphans_is_not_deleted(world: _World) -> None:
    """60 个满 24 小时的副本、一个版本行都找不到 —— 更像是「库里看不见行」(角色没有
    BYPASSRLS、连错了库)而不是真有 60 个孤儿。删除不可逆,停下来记日志,等人看。"""
    paths = _orphans(world, 60)

    assert await sweep_orphan_copies(str(world.root), world.artifacts) == 0
    assert all(p.exists() for p in paths)


async def test_orphans_that_are_a_minority_are_deleted(world: _World) -> None:
    """同样 60 个孤儿,但它们只占少数(还有 100 个副本有版本行)—— 照常清。"""
    for i in range(100):
        await world.save(path=f"agents/w/{i}.txt", created_at=_FRESH, name=f"{i}.txt")
    for kept in world.copies().iterdir():
        _age(kept, 48)
    paths = _orphans(world, 60)

    assert await sweep_orphan_copies(str(world.root), world.artifacts) == 60
    assert not any(p.exists() for p in paths)
    assert len(list(world.copies().iterdir())) == 100


class _BlindStore(InMemoryArtifactStore):
    """库连接的角色没有 BYPASSRLS:声明了 bypass 也看不见别的租户的行。"""

    async def bypasses_row_security(self) -> bool:
        return False


async def test_a_role_that_cannot_see_every_row_deletes_nothing(world: _World) -> None:
    paths = _orphans(world, 3)

    assert await sweep_orphan_copies(str(world.root), _BlindStore()) == 0
    assert all(p.exists() for p in paths)


_NOBYPASS_ROLE = "b235_artifact_nobypass"
_NOBYPASS_PW = "b235-test-only-pw"  # test-only fixture password


@pytest.fixture
async def nobypass_artifacts(postgres_container: Any) -> AsyncIterator[ArtifactStore]:
    """连库角色没有 BYPASSRLS 的 SQL 档 —— ``bypass_rls_var`` 只是不发 GUC,RLS 照样生效。"""
    admin = str(postgres_container.get_connection_url()).replace("+psycopg2", "")
    db = _DB + "_nobypass"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{db}"')
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (_NOBYPASS_ROLE,))
        if exists.fetchone() is None:
            conn.execute(
                f"CREATE ROLE {_NOBYPASS_ROLE} LOGIN NOBYPASSRLS PASSWORD '{_NOBYPASS_PW}'"
            )
    base, _, _ = admin.rpartition("/")
    dsn = f"{base}/{db}"
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    command.upgrade(cfg, "head")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f"GRANT USAGE ON SCHEMA public TO {_NOBYPASS_ROLE}")
        conn.execute(f"GRANT SELECT ON artifact_version, artifact TO {_NOBYPASS_ROLE}")
    scheme, rest = dsn.split("://", 1)
    host = rest.split("@", 1)[1]
    role_dsn = f"postgresql+asyncpg://{_NOBYPASS_ROLE}:{_NOBYPASS_PW}@{host}"
    del scheme
    engine = create_async_engine_from_config(DatabaseConfig(dsn=role_dsn))
    try:
        yield SqlArtifactStore(create_async_session_factory(engine))
    finally:
        await engine.dispose()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')


@pytest.mark.integration
async def test_a_db_role_without_bypassrls_deletes_nothing(
    world: _World, nobypass_artifacts: ArtifactStore
) -> None:
    """声明了 bypass(``bypass_rls_var``)但连库角色没有 BYPASSRLS:强制 RLS 下「哪些副本
    还有版本行」会答「一个都没有」。必须先问库这个角色看不看得见所有行,看不见就不删。"""
    paths = _orphans(world, 3)
    token = bypass_rls_var.set(True)
    try:
        assert await nobypass_artifacts.bypasses_row_security() is False
        assert await sweep_orphan_copies(str(world.root), nobypass_artifacts) == 0
    finally:
        bypass_rls_var.reset(token)
    assert all(p.exists() for p in paths)


@pytest.mark.integration
async def test_the_test_superuser_does_see_every_row(sql_artifacts: ArtifactStore) -> None:
    assert await sql_artifacts.bypasses_row_security() is True
