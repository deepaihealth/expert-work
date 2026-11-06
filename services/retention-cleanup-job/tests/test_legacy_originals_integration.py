"""B-281 —— 一次性清理迁移后留在 agent 根的上传原件:真库(登记表 + 会话 + 审计)+ 真文件。

谓词的逐条用例在 ``test_legacy_originals.py``(内存);这里证明三条查询(没删的文档行、老视图
会话、它们的 ``DOCUMENT_UPLOAD`` 审计)在 Postgres 上拼出来是对的,跨租户跑(RLS bypass),
``--apply`` 删之前的二次查库挡得住期间新登记的同名文件,命令行只打计数。
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
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
    SqlAuditLogStore,
    SqlThreadMetaStore,
    SqlUserUploadStore,
    create_async_engine_from_config,
)
from expert_work.protocol import AuditAction, AuditEntry, AuditResult, ThreadMeta
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
        self, tenant: UUID, user: UUID, *, old_view: bool = False, agent: str = "bot"
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
    # Stays: an old-view conversation still registers the same name.
    kept_row, _ = await db.migrated(tenant, user, "uploads/b.pdf")
    old = await db.thread(tenant, user, old_view=True)
    await db.register(tenant, user, old.thread_id, "uploads/b.pdf")
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


@pytest.mark.asyncio
async def test_apply_rechecks_the_registry_right_before_deleting(db: _Db) -> None:
    """A same-name upload registered in an old-view conversation after the plan was made (B-281:
    console uploads and copies register now) keeps the original."""
    tenant, user = uuid4(), uuid4()
    original, _ = await db.migrated(tenant, user, "uploads/a.pdf")
    cleanup = db.cleanup(apply=True)
    rows = await cleanup._live_uploads()
    metas = await cleanup._metas((r.tenant_id, r.thread_id) for r in rows)
    [candidate] = legacy_originals.plan(rows, [], metas, cleanup.tally)

    old = await db.thread(tenant, user, old_view=True)
    await db.register(tenant, user, old.thread_id, "uploads/a.pdf")
    await cleanup._one(candidate)

    assert (cleanup.tally.kept_changed_during_run, cleanup.tally.deleted) == (1, 0)
    assert original.exists()


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
