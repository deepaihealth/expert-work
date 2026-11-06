"""B-281 —— 一次性清理迁移后留在 agent 根的上传原件:真库(登记表 + 会话 + 审计)+ 真文件。

谓词的逐条用例在 ``test_legacy_originals.py``(内存);这里证明三条查询(没删的文档行、老视图
会话、它们的 ``DOCUMENT_UPLOAD`` 审计)在 Postgres 上拼出来是对的,跨租户跑(RLS bypass),
``--apply`` 删之前的二次查库挡得住期间新登记的同名文件,命令行只打计数。
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from testcontainers.postgres import PostgresContainer

from control_plane.api._workspace_shared import thread_workspace_path
from expert_work.persistence import (
    DatabaseConfig,
    SqlArtifactStore,
    SqlAuditLogStore,
    SqlThreadMetaStore,
    SqlUserUploadStore,
    create_async_engine_from_config,
)
from expert_work.protocol import AuditAction, AuditEntry, AuditResult, ThreadMeta
from expert_work.protocol.agent_key import sanitize_agent_key
from retention_cleanup_job import legacy_originals
from retention_cleanup_job.legacy_originals import LegacyOriginalsCleanup
from retention_cleanup_job.main import build_session_factory

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parents[3] / "packages/expert-work-persistence/alembic.ini"
_BODY = b"%PDF-1.4 migrated upload"


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


class _Db:
    def __init__(self, sf: async_sessionmaker[AsyncSession], admin_dsn: str, root: Path) -> None:
        self.sf = sf
        self.admin_dsn = admin_dsn
        self.root = root
        self.threads = SqlThreadMetaStore(sf)
        self.uploads = SqlUserUploadStore(sf)
        self.audit = SqlAuditLogStore(sf)

    def sql(self, stmt: str, **params: object) -> None:
        admin = create_engine(self.admin_dsn, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                conn.execute(text(stmt), params)
        finally:
            admin.dispose()

    async def thread(
        self, tenant: UUID, user: UUID | None, *, old_view: bool = False, agent: str = "bot"
    ) -> ThreadMeta:
        meta = await self.threads.create(
            thread_id=uuid4(), tenant_id=tenant, created_by="u", user_id=user, agent_name=agent
        )
        if old_view:
            self.sql(
                "UPDATE thread_meta SET workspace_layout = 'agent_root' WHERE thread_id = :t",
                t=meta.thread_id,
            )
            meta = meta.model_copy(update={"workspace_layout": "agent_root"})
        return meta

    def write(self, tenant: UUID, user: UUID, ref: str, meta: ThreadMeta, data: bytes) -> Path:
        target = self.root / str(tenant) / str(user) / thread_workspace_path(ref, meta)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    async def register(self, tenant: UUID, user: UUID, thread_id: UUID, ref: str) -> UUID:
        row = await self.uploads.insert(
            upload_id=uuid4(),
            tenant_id=tenant,
            user_id=user,
            thread_id=thread_id,
            kind="document",
            ref=ref,
            mime_type="application/pdf",
            size_bytes=len(_BODY),
            filename=ref.rsplit("/", 1)[-1],
            sha256=hashlib.sha256(_BODY).hexdigest(),
        )
        return row.id

    async def migrated(self, tenant: UUID, user: UUID, ref: str) -> tuple[Path, Path]:
        """An old conversation that uploaded ``ref`` and was migrated: original + copy + row."""
        meta = await self.thread(tenant, user)
        old = meta.model_copy(update={"workspace_layout": "agent_root"})
        original = self.write(tenant, user, ref, old, _BODY)
        copy = self.write(tenant, user, ref, meta, _BODY)
        await self.register(tenant, user, meta.thread_id, ref)
        return original, copy

    async def console_upload_audit(self, tenant: UUID, thread_id: UUID, ref: str) -> None:
        await self.audit.append(
            AuditEntry(
                tenant_id=tenant,
                actor_type="user",
                actor_id="console-user",
                action=AuditAction.DOCUMENT_UPLOAD,
                resource_type="user_workspace",
                resource_id=ref,
                result=AuditResult.SUCCESS,
                details={
                    "thread_id": str(thread_id),
                    "workspace_path": ref,
                    "file_size_bytes": len(_BODY),
                    "sha256": hashlib.sha256(_BODY).hexdigest(),
                },
            )
        )

    async def old_view_upload(
        self, tenant: UUID, user: UUID, meta: ThreadMeta, ref: str, *, register: bool
    ) -> Path:
        """An upload into an old-view conversation: the file in the agent root + its audit row (as
        every upload has), + a registry row when ``register`` (external upload; console uploads
        before B-281 had none). Content is unique per name."""
        data = _BODY + ref.rsplit("/", 1)[-1].encode()
        path = self.write(tenant, user, ref, meta, data)
        sha = hashlib.sha256(data).hexdigest()
        await self.audit.append(
            AuditEntry(
                tenant_id=tenant,
                actor_type="user",
                actor_id="u",
                action=AuditAction.DOCUMENT_UPLOAD,
                resource_type="user_workspace",
                resource_id=ref,
                result=AuditResult.SUCCESS,
                details={
                    "thread_id": str(meta.thread_id),
                    "workspace_path": ref,
                    "file_size_bytes": len(data),
                    "sha256": sha,
                },
            )
        )
        if register:
            await self.uploads.insert(
                upload_id=uuid4(),
                tenant_id=tenant,
                user_id=user,
                thread_id=meta.thread_id,
                kind="document",
                ref=ref,
                mime_type="application/pdf",
                size_bytes=len(data),
                filename=ref.rsplit("/", 1)[-1],
                sha256=sha,
            )
        return path

    async def artifact(
        self, tenant: UUID, user: UUID, path: Path, *, snapshot_key: str | None
    ) -> None:
        """An artifact version registered on that very workspace file."""
        await SqlArtifactStore(self.sf).save_version(
            tenant_id=tenant,
            user_id=user,
            agent_key=sanitize_agent_key("bot"),
            name=path.name,
            kind="document",
            path_in_workspace=str(path.relative_to(self.root / str(tenant) / str(user))),
            created_in_thread="t",
            snapshot_key=snapshot_key,
            # A copy kept since B-235 carries its digest (check constraint).
            size_bytes=path.stat().st_size if snapshot_key else None,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest() if snapshot_key else None,
        )

    def cleanup(self, *, apply: bool) -> LegacyOriginalsCleanup:
        return LegacyOriginalsCleanup(
            session_factory=self.sf,
            threads=self.threads,
            root=str(self.root),
            apply=apply,
        )


@pytest.fixture
async def db(postgres_container: PostgresContainer, tmp_path: Path) -> AsyncIterator[_Db]:
    admin_dsn = _sync_dsn(postgres_container)
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", admin_dsn)
    command.upgrade(cfg, "head")
    admin = create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            # The run is cross-tenant: start from an empty registry (the container is shared).
            conn.execute(text("TRUNCATE TABLE user_upload, thread_meta CASCADE"))
            conn.execute(text("TRUNCATE TABLE audit_log RESTART IDENTITY"))
    finally:
        admin.dispose()
    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    try:
        yield _Db(build_session_factory(engine), admin_dsn, tmp_path / "nas")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_dry_run_counts_then_apply_deletes_only_migrated_duplicates(db: _Db) -> None:
    tenant, user, other_tenant = uuid4(), uuid4(), uuid4()
    other_user = uuid4()

    # Goes: migrated duplicates, in two tenants (the run is cross-tenant).
    gone_a, copy_a = await db.migrated(tenant, user, "uploads/a.pdf")
    gone_t2, copy_t2 = await db.migrated(other_tenant, other_user, "uploads/a.pdf")
    # Stays: an old-view conversation still registers the same name (another user, so that
    # user's other originals are kept by the old-view rule, not this tenant user's).
    row_user = uuid4()
    kept_row, _ = await db.migrated(tenant, row_user, "uploads/b.pdf")
    old = await db.thread(tenant, row_user, old_view=True)
    await db.register(tenant, row_user, old.thread_id, "uploads/b.pdf")
    # Stays: an old-view conversation's console upload (audit only) names it — another user's
    # conversation of the same agent counts too (the bytes land in the uploader's workspace).
    kept_console, _ = await db.migrated(tenant, user, "uploads/c.pdf")
    console_thread = await db.thread(tenant, uuid4(), old_view=True)
    await db.console_upload_audit(tenant, console_thread.thread_id, "uploads/c.pdf")
    # ...but a console upload into a conversation-view conversation that has its own copy does
    # not keep anything (it sat in its own directory).
    gone_d, _ = await db.migrated(tenant, user, "uploads/d.pdf")
    new_thread = await db.thread(tenant, user)
    db.write(tenant, user, "uploads/d.pdf", new_thread, _BODY)
    await db.console_upload_audit(tenant, new_thread.thread_id, "uploads/d.pdf")
    # Stays: a conversation-view conversation not migrated yet (opened before B-262 P1) whose
    # console upload is only in the agent root.
    kept_unmigrated, _ = await db.migrated(tenant, user, "uploads/g.pdf")
    unmigrated = await db.thread(tenant, user)
    await db.console_upload_audit(tenant, unmigrated.thread_id, "uploads/g.pdf")
    # Stays: a live row whose conversation no longer exists, same name.
    kept_gone, _ = await db.migrated(tenant, user, "uploads/e.pdf")
    await db.register(tenant, user, uuid4(), "uploads/e.pdf")
    # Not even considered: the only row is soft-deleted (expired); nothing proves it migrated.
    expired_original, expired_copy = await db.migrated(tenant, user, "uploads/f.pdf")
    db.sql("UPDATE user_upload SET deleted_at = now() WHERE ref = 'uploads/f.pdf'")
    expired_copy.unlink()

    dry = await db.cleanup(apply=False).run()

    assert (dry.checked, dry.deletable, dry.deleted, dry.bytes) == (7, 3, 0, 3 * len(_BODY))
    assert (
        dry.kept_old_view_row,
        dry.kept_unregistered_upload,
        dry.kept_session_gone,
        dry.kept_copy_missing,
    ) == (1, 1, 1, 1)
    everything = (
        gone_a,
        gone_t2,
        gone_d,
        kept_row,
        kept_console,
        kept_gone,
        kept_unmigrated,
        expired_original,
    )
    assert all(p.exists() for p in everything)

    applied = await db.cleanup(apply=True).run()

    assert (applied.deletable, applied.deleted, applied.bytes) == (0, 3, 3 * len(_BODY))
    assert not gone_a.exists() and not gone_t2.exists() and not gone_d.exists()
    assert kept_row.exists() and kept_console.exists() and kept_gone.exists()
    assert kept_unmigrated.exists()
    assert expired_original.exists()
    assert copy_a.read_bytes() == _BODY and copy_t2.read_bytes() == _BODY

    again = await db.cleanup(apply=True).run()
    assert (again.deleted, again.no_original) == (0, 3)


@pytest.fixture
def change_before_delete(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Run ``change`` after an original passed every check and was verified, right before the
    re-check that precedes the unlink — what a concurrent writer can do in that window."""

    def install(change: Callable[[], Awaitable[None]]) -> None:
        real = LegacyOriginalsCleanup._finish

        async def finish(self: LegacyOriginalsCleanup, original: Any, verified: Any) -> str:
            await change()
            return await real(self, original, verified)

        monkeypatch.setattr(LegacyOriginalsCleanup, "_finish", finish)

    return install


@pytest.mark.asyncio
async def test_apply_rechecks_the_registry_right_before_deleting(
    db: _Db, change_before_delete: Any
) -> None:
    """A same-name upload registered in an old-view conversation meanwhile keeps the original."""
    tenant, user = uuid4(), uuid4()
    original, _ = await db.migrated(tenant, user, "uploads/a.pdf")

    async def change() -> None:
        # The old view's own row (not the conversation): caught by the registry re-check.
        old = await db.thread(tenant, uuid4(), old_view=True)
        await db.register(tenant, user, old.thread_id, "uploads/a.pdf")

    change_before_delete(change)
    tally = await db.cleanup(apply=True).run()

    assert (tally.kept_changed_during_run, tally.deleted) == (1, 0)
    assert original.exists()


@pytest.mark.asyncio
async def test_apply_rechecks_which_rows_point_at_it(db: _Db, change_before_delete: Any) -> None:
    """A new conversation-view row of the same name (with its own identical copy) registered
    meanwhile: still a candidate, but not the rows whose copies were verified — not this round."""
    tenant, user = uuid4(), uuid4()
    original, _ = await db.migrated(tenant, user, "uploads/a.pdf")

    async def change() -> None:
        newer = await db.thread(tenant, user)
        db.write(tenant, user, "uploads/a.pdf", newer, _BODY)
        await db.register(tenant, user, newer.thread_id, "uploads/a.pdf")

    change_before_delete(change)
    tally = await db.cleanup(apply=True).run()

    assert (tally.kept_changed_during_run, tally.deleted) == (1, 0)
    assert original.exists()


@pytest.mark.asyncio
async def test_apply_rechecks_old_view_conversations_right_before_deleting(
    db: _Db, change_before_delete: Any
) -> None:
    """The per-agent old-view check is cached within a run; the one right before the unlink is
    not."""
    tenant, user = uuid4(), uuid4()
    original, _ = await db.migrated(tenant, user, "uploads/a.pdf")

    async def change() -> None:
        await db.thread(tenant, None, old_view=True)

    change_before_delete(change)
    tally = await db.cleanup(apply=True).run()

    assert (tally.kept_changed_during_run, tally.deleted) == (1, 0)
    assert original.exists()


@pytest.mark.asyncio
async def test_apply_rechecks_artifact_versions_right_before_deleting(
    db: _Db, change_before_delete: Any
) -> None:
    tenant, user = uuid4(), uuid4()
    original, _ = await db.migrated(tenant, user, "uploads/a.pdf")

    async def change() -> None:
        await db.artifact(tenant, user, original, snapshot_key=None)

    change_before_delete(change)
    tally = await db.cleanup(apply=True).run()

    assert (tally.kept_changed_during_run, tally.deleted) == (1, 0)
    assert original.exists()


@pytest.mark.asyncio
async def test_old_view_conversations_and_snapshotless_artifacts_keep_originals(db: _Db) -> None:
    """Review F1 / F3: an old-view conversation of the same agent whose user is the workspace owner
    or nobody sees all of ``agents/<key>/uploads/`` (no row / audit / copy needed) → keep every
    original of that agent; one of another user (another workspace) doesn't. An artifact version
    without its own copy (before B-235) reads the file → keep."""
    tenant = uuid4()
    owner_old, null_old, other_old, other_agent_old = uuid4(), uuid4(), uuid4(), uuid4()
    artifact_user, snapshotted_user = uuid4(), uuid4()

    kept_owner, _ = await db.migrated(tenant, owner_old, "uploads/a.pdf")
    await db.thread(tenant, owner_old, old_view=True)
    kept_null, _ = await db.migrated(tenant, null_old, "uploads/a.pdf")
    await db.thread(tenant, None, old_view=True)  # shared by every user of the tenant's agent
    gone_other, _ = await db.migrated(tenant, other_old, "uploads/b.pdf")
    await db.thread(uuid4(), other_old, old_view=True)  # another tenant
    gone_other_agent, _ = await db.migrated(tenant, other_agent_old, "uploads/c.pdf")
    await db.thread(tenant, other_agent_old, old_view=True, agent="other-bot")
    kept_artifact, _ = await db.migrated(tenant, artifact_user, "uploads/d.pdf")
    await db.artifact(tenant, artifact_user, kept_artifact, snapshot_key=None)
    gone_snapshotted, _ = await db.migrated(tenant, snapshotted_user, "uploads/e.pdf")
    await db.artifact(tenant, snapshotted_user, gone_snapshotted, snapshot_key="snap/e.pdf")

    tally = await db.cleanup(apply=True).run()

    # The NULL-user old-view thread keeps the originals of *every* user of that agent in the
    # tenant (its /workspace is the agent root of whoever runs it).
    assert (tally.kept_old_view_conversation, tally.kept_artifact_version) == (6, 0)
    assert tally.deleted == 0

    db.sql("DELETE FROM thread_meta WHERE user_id IS NULL AND tenant_id = :t", t=tenant)
    tally = await db.cleanup(apply=True).run()

    assert (tally.kept_old_view_conversation, tally.kept_artifact_version) == (1, 1)
    assert tally.deleted == 4
    assert kept_owner.exists() and kept_artifact.exists()
    assert not kept_null.exists() and not gone_other.exists()
    assert not gone_other_agent.exists() and not gone_snapshotted.exists()


@pytest.mark.asyncio
async def test_migration_releases_originals_once_the_last_old_view_conversation_moves(
    db: _Db,
) -> None:
    """User decision (a): at the end of a successful conversation migration, the same predicate runs
    for that (tenant, user, agent) in the background. While another old-view conversation of the
    agent remains nothing goes; when the last one migrates, every migrated original with an
    identical copy goes — but not an audit-only one (decision b) nor one an artifact version without
    its own copy reads."""
    from control_plane.platform_migration import PlatformMigration, prepare_run_workspace
    from orchestrator.tools.locks import ProcessKeyedLock
    from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
    from retention_cleanup_job.legacy_originals import release_migrated_originals

    tenant, user = uuid4(), uuid4()
    db.root.mkdir(parents=True, exist_ok=True)
    x = await db.thread(tenant, user, old_view=True)
    y = await db.thread(tenant, user, old_view=True)
    x_pdf = await db.old_view_upload(tenant, user, x, "uploads/x.pdf", register=True)
    y_pdf = await db.old_view_upload(tenant, user, y, "uploads/y.pdf", register=True)
    console_only = await db.old_view_upload(tenant, user, x, "uploads/z.pdf", register=False)
    read_by_artifact = await db.old_view_upload(tenant, user, x, "uploads/w.pdf", register=True)
    await db.artifact(tenant, user, read_by_artifact, snapshot_key=None)
    migration = PlatformMigration(
        workspace_store=NasWorkspaceStore(root=str(db.root)),
        lock=ProcessKeyedLock(),
        threads=db.threads,
        uploads=db.uploads,
        runs=None,
        audit_store=db.audit,
        release_originals=functools.partial(
            release_migrated_originals, db.sf, db.threads, str(db.root)
        ),
    )

    async def migrate(meta: ThreadMeta) -> None:
        prepared = await prepare_run_workspace(
            migration,
            thread_id=meta.thread_id,
            tenant_id=tenant,
            workspace_layout="agent_root",
        )
        assert prepared.migrated is True
        await asyncio.gather(*migration._background)

    await migrate(x)
    assert x_pdf.exists() and y_pdf.exists()  # y is still in the old view

    await migrate(y)
    assert not x_pdf.exists() and not y_pdf.exists()
    assert console_only.exists() and read_by_artifact.exists()
    for meta, name in ((x, "x.pdf"), (y, "y.pdf"), (x, "z.pdf"), (x, "w.pdf")):
        conv = meta.model_copy(update={"workspace_layout": "conversation"})
        copy = db.root / str(tenant) / str(user) / thread_workspace_path(f"uploads/{name}", conv)
        assert copy.read_bytes() == _BODY + name.encode()


@pytest.mark.asyncio
async def test_the_migration_time_release_is_bounded(db: _Db) -> None:
    from retention_cleanup_job.legacy_originals import release_migrated_originals

    tenant, user = uuid4(), uuid4()
    first, _ = await db.migrated(tenant, user, "uploads/a.pdf")
    second, _ = await db.migrated(tenant, user, "uploads/b.pdf")

    tally = await release_migrated_originals(
        db.sf,
        db.threads,
        str(db.root),
        tenant_id=tenant,
        user_id=user,
        agent_name="bot",
        max_originals=1,
    )

    assert tally.deleted == 1
    assert first.exists() != second.exists()


@pytest.mark.asyncio
async def test_a_failing_release_does_not_fail_the_migration(db: _Db) -> None:
    from control_plane.platform_migration import PlatformMigration, prepare_run_workspace
    from orchestrator.tools.locks import ProcessKeyedLock
    from orchestrator.tools.nas_workspace_store import NasWorkspaceStore

    tenant, user = uuid4(), uuid4()
    db.root.mkdir(parents=True, exist_ok=True)
    x = await db.thread(tenant, user, old_view=True)
    await db.old_view_upload(tenant, user, x, "uploads/x.pdf", register=True)

    async def broken(**_: Any) -> Any:
        raise RuntimeError("boom")

    migration = PlatformMigration(
        workspace_store=NasWorkspaceStore(root=str(db.root)),
        lock=ProcessKeyedLock(),
        threads=db.threads,
        uploads=db.uploads,
        runs=None,
        audit_store=db.audit,
        release_originals=broken,
    )
    prepared = await prepare_run_workspace(
        migration, thread_id=x.thread_id, tenant_id=tenant, workspace_layout="agent_root"
    )
    assert prepared.migrated is True
    await asyncio.gather(*migration._background)  # the failure is logged, not raised


@pytest.mark.asyncio
async def test_the_command_prints_counts_only(
    db: _Db,
    postgres_container: PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tenant, user = uuid4(), uuid4()
    original, _ = await db.migrated(tenant, user, "uploads/secret-name.pdf")
    monkeypatch.setenv("EXPERT_WORK_RETENTION_DB_DSN", _async_dsn(postgres_container))
    monkeypatch.setenv("EXPERT_WORK_RETENTION_WORKSPACE_ROOT", str(db.root))
    monkeypatch.delenv("EXPERT_WORK_WORKSPACE_NAS_ROOT", raising=False)

    code = await legacy_originals._amain([])

    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("legacy_originals mode=dry-run checked=1 deletable=1 deleted=0 ")
    assert str(tenant) not in out and str(user) not in out and "secret-name" not in out
    assert original.exists()
