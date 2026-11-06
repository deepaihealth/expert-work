"""B-282 —— 闲置会话的工作目录到期清理,真 Postgres + 临时目录当 NAS 根。

每条用例自己造会话(``thread_meta`` 行 + 按需的 ``agent_run`` / 产物 / 租户配置),在临时
NAS 根下放好 ``agents/<key>/threads/<会话>/`` 与 agent 根的 ``.tool_results/<run>/``,跑整个
``run_once``(别的 pass 在这些表上什么都做不了),看文件与库。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlThreadMetaStore,
    create_async_engine_from_config,
)
from expert_work.protocol.agent_key import sanitize_agent_key
from retention_cleanup_job.job import RetentionCleanupJob
from retention_cleanup_job.main import build_session_factory

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parents[3] / "packages/expert-work-persistence/alembic.ini"
_AGENT = "Plan Bot"
_KEY = sanitize_agent_key(_AGENT)


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@dataclass
class _World:
    engine: AsyncEngine
    sf: async_sessionmaker[AsyncSession]
    admin_dsn: str
    root: Path
    tenant: UUID
    user: UUID

    def sql(self, statement: str, **params: object) -> list[tuple[object, ...]]:
        admin = create_engine(self.admin_dsn, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                result = conn.execute(text(statement), params)
                return [tuple(r) for r in result] if result.returns_rows else []
        finally:
            admin.dispose()

    async def conversation(
        self,
        *,
        age_days: float,
        layout: str = "conversation",
        tenant: UUID | None = None,
        agent: str | None = _AGENT,
    ) -> UUID:
        conv = uuid4()
        await SqlThreadMetaStore(self.sf).create(
            thread_id=conv,
            tenant_id=tenant or self.tenant,
            created_by="u",
            user_id=self.user,
            agent_name=agent,
        )
        self.sql(
            "UPDATE thread_meta SET created_at = now() - make_interval(secs => :s), "
            "updated_at = now() - make_interval(secs => :s), workspace_layout = :l "
            "WHERE thread_id = :t",
            s=age_days * 86400,
            l=layout,
            t=conv,
        )
        return conv

    def run(
        self, conv: UUID, *, age_days: float, status: str = "success", tenant: UUID | None = None
    ) -> UUID:
        run_id = uuid4()
        self.sql(
            "INSERT INTO agent_run (id, tenant_id, thread_id, status, on_disconnect, created_at, "
            " updated_at) VALUES (:r, :t, :c, :st, 'continue', now() - make_interval(secs => :s), "
            " now())",
            r=run_id,
            t=tenant or self.tenant,
            c=conv,
            st=status,
            s=age_days * 86400,
        )
        return run_id

    def tenant_days(self, tenant: UUID, days: int | None) -> None:
        self.sql(
            "INSERT INTO tenant_config (tenant_id, display_name, updated_by, "
            " conversation_workspace_retention_days) VALUES (:t, 't', 'test', :d)",
            t=tenant,
            d=days,
        )

    def user_dir(self, tenant: UUID | None = None) -> Path:
        return self.root / str(tenant or self.tenant) / str(self.user)

    def conv_dir(self, conv: UUID, tenant: UUID | None = None) -> Path:
        return self.user_dir(tenant) / "agents" / _KEY / "threads" / str(conv)

    def place(self, conv: UUID, tenant: UUID | None = None) -> Path:
        """对话目录的三样东西:``work/`` 里的产出、``PLAN.md`` 投影、开场复制标记。"""
        base = self.conv_dir(conv, tenant)
        (base / "work" / "report").mkdir(parents=True)
        (base / "work" / "report" / "draft.md").write_text("draft")
        (base / "PLAN.md").write_text("plan")
        (base / ".seeded").write_text("{}")
        return base

    def tool_results(self, run_id: UUID, *, agent: bool = True) -> Path:
        base = self.user_dir() / "agents" / _KEY if agent else self.user_dir()
        target = base / ".tool_results" / str(run_id)
        target.mkdir(parents=True)
        (target / "out.txt").write_text("overflow")
        return target

    def cleaned_at(self, conv: UUID) -> object:
        return self.sql(
            "SELECT workspace_cleaned_at FROM thread_meta WHERE thread_id = :t", t=conv
        )[0][0]

    def job(self, **kwargs: object) -> RetentionCleanupJob:
        return RetentionCleanupJob(
            db_session_factory=self.sf, workspace_root=str(self.root), **kwargs
        )  # type: ignore[arg-type]


@pytest.fixture
def world(postgres_container: PostgresContainer, tmp_path: Path) -> Iterator[_World]:
    admin_dsn = _sync_dsn(postgres_container)
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", admin_dsn)
    command.upgrade(cfg, "head")
    admin = create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(
                text(
                    "TRUNCATE TABLE thread_meta, agent_run, artifact_version, artifact, "
                    "tenant_config CASCADE"
                )
            )
    finally:
        admin.dispose()
    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    root = tmp_path / "nas"
    root.mkdir()
    yield _World(engine, build_session_factory(engine), admin_dsn, root, uuid4(), uuid4())


@pytest.mark.asyncio
async def test_an_idle_conversation_loses_its_work_dir_but_keeps_its_rows(world: _World) -> None:
    try:
        conv = await world.conversation(age_days=200)
        run_id = world.run(conv, age_days=100)
        conv_dir = world.place(conv)
        overflow = world.tool_results(run_id)
        before = world.sql("SELECT updated_at FROM thread_meta WHERE thread_id = :t", t=conv)

        report = await world.job().run_once()

        assert not conv_dir.exists()  # work/, PLAN.md and .seeded all gone
        assert not overflow.exists()
        assert (report.conversation_dirs_removed, report.conversation_tool_results_removed) == (
            1,
            1,
        )
        assert report.failed_passes == ()
        # DB rows stay; the cleanup stamp does not count as a change to the conversation.
        assert world.sql("SELECT count(*) FROM agent_run WHERE thread_id = :t", t=conv) == [(1,)]
        assert (
            world.sql("SELECT updated_at FROM thread_meta WHERE thread_id = :t", t=conv) == before
        )
        assert world.cleaned_at(conv) is not None
        # The agent root next to it is untouched.
        assert (world.user_dir() / "agents" / _KEY / "threads").is_dir()
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_a_recent_run_or_an_unfinished_run_keeps_the_dir(world: _World) -> None:
    """闲置看最后一次 run:10 天前跑过的留着;有没结束的 run(排队 / 运行 / 停在审批处)的
    不管多老都留着。"""
    try:
        recent = await world.conversation(age_days=200)
        world.run(recent, age_days=10)
        kept = {"recent": (recent, world.place(recent))}
        for status in ("queued", "running", "paused", "pending"):
            conv = await world.conversation(age_days=200)
            world.run(conv, age_days=150, status=status)
            kept[status] = (conv, world.place(conv))
        fresh = await world.conversation(age_days=40)  # no run; 40 days < 90
        kept["fresh"] = (fresh, world.place(fresh))

        report = await world.job().run_once()

        for name, (conv, path) in kept.items():
            assert path.exists(), name
            assert world.cleaned_at(conv) is None, name
        assert report.conversation_dirs_removed == 0
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_the_tenant_days_apply_to_that_tenant_only(world: _World) -> None:
    try:
        short = uuid4()
        world.tenant_days(short, 30)
        short_conv = await world.conversation(age_days=40, tenant=short)
        short_dir = world.place(short_conv, tenant=short)
        plain_conv = await world.conversation(age_days=40)
        plain_dir = world.place(plain_conv)

        await world.job().run_once()

        assert not short_dir.exists()
        assert plain_dir.exists()
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_a_file_a_live_artifact_still_reads_from_the_workspace_keeps_the_conversation(
    world: _World,
) -> None:
    """B-235 之前登记、仍是最新版的产物(没有副本)下载时读工作区里的文件:整段会话不动、计数;
    带副本的产物不挡;已删的产物不挡。"""
    try:
        protected = await world.conversation(age_days=200)
        protected_dir = world.place(protected)
        snapshotted = await world.conversation(age_days=200)
        snapshotted_dir = world.place(snapshotted)
        for conv, snapshot in ((protected, None), (snapshotted, "snap/key")):
            artifact_id = uuid4()
            path = f"agents/{_KEY}/threads/{conv}/work/report/draft.md"
            world.sql(
                "INSERT INTO artifact (id, tenant_id, user_id, agent_key, name, kind, "
                " latest_version) VALUES (:a, :t, :u, :k, :n, 'document', 2)",
                n=f"draft-{conv}.md",
                a=artifact_id,
                t=world.tenant,
                u=world.user,
                k=_KEY,
            )
            for version, snap in ((1, None), (2, snapshot)):
                world.sql(
                    "INSERT INTO artifact_version (id, artifact_id, tenant_id, user_id, version, "
                    " path_in_workspace, created_in_thread, snapshot_key, sha256, size_bytes) "
                    "VALUES (:i, :a, :t, :u, :v, :p, 'r', :s, :h, :z)",
                    i=uuid4(),
                    a=artifact_id,
                    t=world.tenant,
                    u=world.user,
                    v=version,
                    p=path,
                    s=snap,
                    h="0" * 64 if snap else None,
                    z=5 if snap else None,
                )

        report = await world.job().run_once()

        assert protected_dir.exists()
        assert (protected_dir / "work" / "report" / "draft.md").read_text() == "draft"
        assert world.cleaned_at(protected) is None
        assert not snapshotted_dir.exists()
        assert report.conversation_dirs_skipped_artifact == 1
        assert report.failed_passes == ()

        world.sql("UPDATE artifact SET deleted_at = now()")
        await world.job().run_once()
        assert not protected_dir.exists()
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_a_cleaned_conversation_is_not_picked_again_until_it_is_used(
    world: _World,
) -> None:
    """清过一次就记下;没有新 run 不再列它(不占批次)。会话又跑了之后重新算闲置。"""
    try:
        conv = await world.conversation(age_days=300)
        world.run(conv, age_days=200)
        world.place(conv)
        await world.job().run_once()
        first = world.cleaned_at(conv)

        again = await world.job().run_once()
        assert again.conversation_dirs_removed == 0
        assert world.cleaned_at(conv) == first

        # Resumed long ago, then idle again past the window → cleaned again.
        world.run(conv, age_days=95)
        world.sql(
            "UPDATE thread_meta SET workspace_cleaned_at = now() - interval '150 days' "
            "WHERE thread_id = :t",
            t=conv,
        )
        conv_dir = world.place(conv)
        third = await world.job().run_once()
        assert third.conversation_dirs_removed == 1
        assert not conv_dir.exists()
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_an_old_layout_conversation_only_loses_its_agent_root_tool_results(
    world: _World,
) -> None:
    """老视图会话的 ``threads/<会话>/`` 不在这一条的范围;它的 run 在 agent 根的
    ``.tool_results/<run>/`` 照删(没绑 agent 的在用户根)。"""
    try:
        old = await world.conversation(age_days=200, layout="agent_root")
        run_id = world.run(old, age_days=120)
        projection = world.conv_dir(old)
        projection.mkdir(parents=True)
        (projection / "PLAN.md").write_text("plan")
        overflow = world.tool_results(run_id)
        unbound = await world.conversation(age_days=200, layout="agent_root", agent=None)
        unbound_run = world.run(unbound, age_days=120)
        unbound_overflow = world.tool_results(unbound_run, agent=False)

        report = await world.job().run_once()

        assert projection.exists()
        assert not overflow.exists()
        assert not unbound_overflow.exists()
        assert report.conversation_tool_results_removed == 2
        assert report.conversation_dirs_removed == 0
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_a_symlink_on_the_way_is_never_followed(world: _World) -> None:
    try:
        conv = await world.conversation(age_days=200)
        outside = world.root.parent / "outside"
        (outside / str(conv) / "work").mkdir(parents=True)
        (outside / str(conv) / "work" / "keep.txt").write_text("keep")
        agent_dir = world.user_dir() / "agents" / _KEY
        agent_dir.mkdir(parents=True)
        os.symlink(outside, agent_dir / "threads")

        report = await world.job().run_once()

        assert (outside / str(conv) / "work" / "keep.txt").read_text() == "keep"
        assert report.conversation_dirs_skipped_unsafe == 1
        assert report.failed_passes == ()
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_a_failure_is_isolated_counted_and_needs_attention(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from expert_work.persistence.workspace.safe_remove import NasEntryStuckError
    from retention_cleanup_job import conversation_dirs

    try:
        stuck = await world.conversation(age_days=300)
        stuck_dir = world.place(stuck)
        fine = await world.conversation(age_days=200)
        fine_dir = world.place(fine)
        real = conversation_dirs.quarantine_entry

        def _park(root: str, parts: list[str], name: str, **kwargs: object) -> object:
            if name == str(stuck):
                raise NasEntryStuckError(13, "cannot quarantine")
            return real(root, parts, name, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(conversation_dirs, "quarantine_entry", _park)

        report = await world.job().run_once()

        assert stuck_dir.exists() and world.cleaned_at(stuck) is None  # retried tomorrow
        assert not fine_dir.exists()
        assert report.conversation_dirs_failed == 1
        assert report.failed_passes == ("conversation_dirs",)
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_one_run_handles_at_most_the_batch_size_oldest_first(world: _World) -> None:
    try:
        convs = [await world.conversation(age_days=age) for age in (300, 200, 150)]
        dirs = [world.place(c) for c in convs]

        report = await world.job(conversation_workspace_batch_size=2).run_once()

        assert report.conversation_dirs_removed == 2
        assert [d.exists() for d in dirs] == [False, False, True]
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_a_run_that_starts_after_listing_keeps_the_dir(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """列出闲置会话之后、删之前,这段会话又起了一个 run:不删、不记。"""
    from retention_cleanup_job import conversation_dirs

    try:
        conv = await world.conversation(age_days=200)
        conv_dir = world.place(conv)
        real = conversation_dirs.list_idle_conversations

        async def _list(*args: object, **kwargs: object) -> list[object]:
            listed = await real(*args, **kwargs)  # type: ignore[arg-type]
            world.run(conv, age_days=0, status="running")
            return listed  # type: ignore[return-value]

        monkeypatch.setattr(conversation_dirs, "list_idle_conversations", _list)

        report = await world.job().run_once()

        assert conv_dir.exists()
        assert world.cleaned_at(conv) is None
        assert report.conversation_dirs_skipped_active == 1
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_a_resumed_conversation_is_seeded_again_and_can_write(world: _World) -> None:
    """清完之后会话又来了一轮:没有 ``.seeded``,开场复制照常把 agent 根的基础目录复制进新的
    ``work/``,写文件照常。"""
    from orchestrator.tools.conversation_seed import seed_conversation
    from orchestrator.tools.locks import ProcessKeyedLock
    from orchestrator.tools.nas_workspace_store import NasWorkspaceStore

    try:
        conv = await world.conversation(age_days=200)
        conv_dir = world.place(conv)
        style = world.user_dir() / "agents" / _KEY / "style"
        style.mkdir(parents=True)
        (style / "anchor.md").write_text("anchor")
        await world.job().run_once()
        assert not conv_dir.exists()

        store = NasWorkspaceStore(root=str(world.root))
        report = await seed_conversation(
            store,
            tenant_id=world.tenant,
            user_id=world.user,
            agent_key=_KEY,
            conv_id=conv,
            lock=ProcessKeyedLock(),
        )

        assert report is not None
        assert (conv_dir / ".seeded").exists()
        assert (conv_dir / "work" / "style" / "anchor.md").read_text() == "anchor"
        await store.write_file(
            tenant_id=world.tenant,
            user_id=world.user,
            path=f"agents/{_KEY}/threads/{conv}/work/new.md",
            data=b"after resume",
        )
        assert (conv_dir / "work" / "new.md").read_bytes() == b"after resume"
    finally:
        await world.engine.dispose()


def test_the_done_line_carries_the_conversation_counts(caplog: pytest.LogCaptureFixture) -> None:
    from retention_cleanup_job.main import log_done
    from retention_cleanup_job.report import CleanupReport

    with caplog.at_level("INFO"):
        log_done(
            CleanupReport(
                conversation_dirs_removed=4,
                conversation_tool_results_removed=9,
                conversation_dirs_skipped_active=1,
                conversation_dirs_skipped_artifact=2,
                conversation_dirs_skipped_unsafe=3,
                conversation_dirs_skipped_owner_deleted=5,
                conversation_dirs_failed=6,
            )
        )

    line = next(
        r.getMessage() for r in caplog.records if "retention_cleanup_job.done" in r.getMessage()
    )
    for part in (
        "conversation_dirs_removed=4",
        "conversation_tool_results_removed=9",
        "conversation_dirs_skipped_active=1",
        "conversation_dirs_skipped_artifact=2",
        "conversation_dirs_skipped_unsafe=3",
        "conversation_dirs_skipped_owner_deleted=5",
        "conversation_dirs_failed=6",
    ):
        assert part in line, part


@pytest.mark.asyncio
async def test_a_soft_deleted_users_tree_is_left_to_the_archive(world: _World) -> None:
    """用户已软删(``{tenant}/.deleted/{user}``):整棵树归 janitor 归档,这里不动、不记清理时间。"""
    try:
        conv = await world.conversation(age_days=200)
        conv_dir = world.place(conv)
        marker = world.root / str(world.tenant) / ".deleted" / str(world.user)
        marker.parent.mkdir(parents=True)
        marker.write_text("")

        report = await world.job().run_once()

        assert conv_dir.exists()
        assert report.conversation_dirs_skipped_owner_deleted == 1
        assert world.cleaned_at(conv) is None  # not stamped: the user purge takes the rows
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_a_rendered_page_removed_with_its_run_becomes_a_text_note(world: _World) -> None:
    """agent 根 ``.tool_results/<run>/`` 里的渲染页随闲置会话清掉之后,老会话历史里那条图片引用
    不让模型调用失败:``resolve_message_images`` 换成「已不存在」的文字(B-64 的降级)。"""
    from expert_work.persistence import (
        RENDERED_FIGURE_DIR,
        RENDERED_FIGURE_PAGE_STEM,
        RENDERED_FIGURE_SHA_HEX_LEN,
        RENDERED_FIGURE_UNIT_PREFIX,
    )
    from orchestrator.multimodal import NasWorkspaceImageResolver, resolve_message_images

    try:
        conv = await world.conversation(age_days=200, layout="agent_root")
        run_id = world.run(conv, age_days=120)
        rel = "/".join(
            [
                "agents",
                _KEY,
                ".tool_results",
                str(run_id),
                RENDERED_FIGURE_DIR,
                "a" * RENDERED_FIGURE_SHA_HEX_LEN,
                "b" * RENDERED_FIGURE_SHA_HEX_LEN,
                f"{RENDERED_FIGURE_UNIT_PREFIX}3",
                f"{RENDERED_FIGURE_PAGE_STEM}-03.jpg",
            ]
        )
        page = world.user_dir() / rel
        page.parent.mkdir(parents=True)
        page.write_bytes(b"\xff\xd8\xff\xe0page")
        ref = f"expert_work://workspace/{world.tenant}/{world.user}/{rel}"
        resolver = NasWorkspaceImageResolver(root=world.root)
        assert not isinstance((await resolve_message_images([ref], resolver))[0], str)

        await world.job().run_once()

        assert not page.exists()
        (note,) = await resolve_message_images([ref], resolver)
        assert isinstance(note, str)
        assert "第 3 页" in note and "已不存在" in note
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_conversations_with_an_unfinished_run_do_not_take_a_batch_slot(
    world: _World,
) -> None:
    """有没结束的 run 的会话在列表这一步就排除(删前那次再查是兜并发的):不然它们每晚都排在
    最前面占满一批,别的会话永远轮不到。"""
    try:
        busy = await world.conversation(age_days=300)
        world.run(busy, age_days=200, status="running")
        busy_dir = world.place(busy)
        idle = await world.conversation(age_days=150)
        idle_dir = world.place(idle)

        report = await world.job(conversation_workspace_batch_size=1).run_once()

        assert busy_dir.exists()
        assert not idle_dir.exists()
        assert (report.conversation_dirs_removed, report.conversation_dirs_skipped_active) == (1, 0)
    finally:
        await world.engine.dispose()


# ---------------------------------------------------------------------------
# B-282 review —— 上传也是活动、持开场复制锁、run 目录里的产物、排序、竞态
# ---------------------------------------------------------------------------


def _upload(
    world: _World,
    conv: UUID,
    *,
    created_days: float,
    used_days: float | None = None,
    kind: str = "document",
    deleted: bool = False,
) -> None:
    world.sql(
        "INSERT INTO user_upload (id, tenant_id, user_id, thread_id, kind, ref, mime_type, "
        " size_bytes, filename, created_at, last_used_at, deleted_at) VALUES (:i, :t, :u, :c, "
        " :k, 'uploads/a.pdf', 'application/pdf', 1, 'a.pdf', "
        " now() - make_interval(secs => :cs), "
        " CASE WHEN CAST(:us AS float) IS NULL THEN NULL "
        "      ELSE now() - make_interval(secs => CAST(:us AS float)) END, "
        " CASE WHEN :d THEN now() ELSE NULL END)",
        i=uuid4(),
        t=world.tenant,
        u=world.user,
        c=conv,
        k=kind,
        cs=created_days * 86400,
        us=None if used_days is None else used_days * 86400,
        d=deleted,
    )


@pytest.mark.asyncio
async def test_a_recent_or_recently_used_upload_keeps_an_idle_conversation(
    world: _World,
) -> None:
    """新传了附件还没跑、或这份附件最近被别的会话引用 / 被下载(B-63 让登记行续命):会话目录里的
    ``work/uploads/<文件>`` 也得留着。删掉的附件、图片附件不算。"""
    try:
        convs = {}
        for name in ("fresh", "used", "deleted", "image"):
            conv = await world.conversation(age_days=200)
            world.run(conv, age_days=100)
            convs[name] = (conv, world.place(conv))
        _upload(world, convs["fresh"][0], created_days=5)
        _upload(world, convs["used"][0], created_days=200, used_days=5)
        _upload(world, convs["deleted"][0], created_days=5, deleted=True)
        _upload(world, convs["image"][0], created_days=5, kind="image")

        report = await world.job().run_once()

        assert convs["fresh"][1].exists()
        assert convs["used"][1].exists()
        assert not convs["deleted"][1].exists()
        assert not convs["image"][1].exists()
        assert report.conversation_dirs_removed == 2
    finally:
        await world.engine.dispose()


async def _upload_audit(world: _World, conv: UUID, *, age_days: float) -> None:
    from expert_work.persistence import SqlAuditLogStore, create_async_session_factory
    from expert_work.protocol import AuditAction, AuditEntry, AuditResult

    entry = await SqlAuditLogStore(create_async_session_factory(world.engine)).append(
        AuditEntry(
            tenant_id=world.tenant,
            actor_type="user",
            actor_id="console",
            action=AuditAction.DOCUMENT_UPLOAD,
            resource_type="user_workspace",
            resource_id="uploads/a.pdf",
            result=AuditResult.SUCCESS,
            details={"thread_id": str(conv), "workspace_path": "uploads/a.pdf"},
        )
    )
    world.sql(
        "UPDATE audit_log SET occurred_at = now() - make_interval(secs => :s) WHERE id = :i",
        s=age_days * 86400,
        i=entry.id,
    )


@pytest.mark.asyncio
async def test_a_console_upload_seen_only_in_the_audit_keeps_the_conversation(
    world: _World,
) -> None:
    """B-281 之前的控制台上传(与跨会话复制)没有登记行,只有 ``DOCUMENT_UPLOAD`` 审计。"""
    from expert_work.persistence import SqlAuditLogStore

    try:
        recent = await world.conversation(age_days=200)
        world.run(recent, age_days=100)
        recent_dir = world.place(recent)
        await _upload_audit(world, recent, age_days=5)
        old = await world.conversation(age_days=200)
        world.run(old, age_days=100)
        old_dir = world.place(old)
        await _upload_audit(world, old, age_days=100)

        report = await world.job(audit_log_store=SqlAuditLogStore(world.sf)).run_once()

        assert recent_dir.exists()
        assert not old_dir.exists()
        assert report.conversation_dirs_skipped_active == 1
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_the_sweep_waits_for_the_conversation_seed_lock(world: _World) -> None:
    """再查与删持开场复制那把锁:别人(开场复制)拿着它时不动这段会话。"""
    import asyncio

    import psycopg

    try:
        conv = await world.conversation(age_days=200)
        conv_dir = world.place(conv)
        dsn = world.admin_dsn.replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(dsn) as holder:
            holder.execute(
                "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                (8624, f"{world.tenant}:{conv}"),
            )
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(world.job().run_once(), timeout=3)
            assert conv_dir.exists()
            holder.rollback()

        await world.job().run_once()
        assert not conv_dir.exists()
    finally:
        await world.engine.dispose()


def test_the_lock_class_is_the_control_planes_seed_lock() -> None:
    from control_plane.advisory_locks import CONVERSATION_SEED_LOCK_CLASSID
    from retention_cleanup_job import conversation_dirs

    assert conversation_dirs.CONVERSATION_SEED_LOCK_CLASSID == CONVERSATION_SEED_LOCK_CLASSID


@pytest.mark.asyncio
async def test_a_run_dir_holding_a_file_an_artifact_reads_is_kept(world: _World) -> None:
    try:
        conv = await world.conversation(age_days=200, layout="agent_root")
        kept_run = world.run(conv, age_days=120)
        gone_run = world.run(conv, age_days=110)
        kept = world.tool_results(kept_run)
        gone = world.tool_results(gone_run)
        artifact = uuid4()
        world.sql(
            "INSERT INTO artifact (id, tenant_id, user_id, agent_key, name, kind, latest_version) "
            "VALUES (:a, :t, :u, :k, 'page.md', 'document', 1)",
            a=artifact,
            t=world.tenant,
            u=world.user,
            k=_KEY,
        )
        world.sql(
            "INSERT INTO artifact_version (id, artifact_id, tenant_id, user_id, version, "
            " path_in_workspace, created_in_thread) VALUES (:i, :a, :t, :u, 1, :p, 'r')",
            i=uuid4(),
            a=artifact,
            t=world.tenant,
            u=world.user,
            p=f"agents/{_KEY}/.tool_results/{kept_run}/out.txt",
        )

        report = await world.job().run_once()

        assert kept.exists()
        assert not gone.exists()
        assert report.conversation_tool_results_skipped_artifact == 1
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_protected_conversations_are_listed_last(world: _World) -> None:
    """产物保护的会话这一轮反正不删:排在最后,不占批次。"""
    try:
        protected = await world.conversation(age_days=300)
        world.place(protected)
        artifact = uuid4()
        world.sql(
            "INSERT INTO artifact (id, tenant_id, user_id, agent_key, name, kind, latest_version) "
            "VALUES (:a, :t, :u, :k, 'x.md', 'document', 1)",
            a=artifact,
            t=world.tenant,
            u=world.user,
            k=_KEY,
        )
        world.sql(
            "INSERT INTO artifact_version (id, artifact_id, tenant_id, user_id, version, "
            " path_in_workspace, created_in_thread) VALUES (:i, :a, :t, :u, 1, :p, 'r')",
            i=uuid4(),
            a=artifact,
            t=world.tenant,
            u=world.user,
            p=f"agents/{_KEY}/threads/{protected}/work/x.md",
        )
        newer = await world.conversation(age_days=150)
        newer_dir = world.place(newer)

        report = await world.job(conversation_workspace_batch_size=1).run_once()

        assert not newer_dir.exists()
        assert report.conversation_dirs_skipped_artifact == 0
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_a_run_that_started_and_finished_after_listing_keeps_the_dir(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from retention_cleanup_job import conversation_dirs

    try:
        conv = await world.conversation(age_days=200)
        world.run(conv, age_days=100)
        conv_dir = world.place(conv)
        real = conversation_dirs.list_idle_conversations

        async def _list(*args: object, **kwargs: object) -> list[object]:
            listed = await real(*args, **kwargs)  # type: ignore[arg-type]
            world.run(conv, age_days=0, status="success")
            return listed  # type: ignore[return-value]

        monkeypatch.setattr(conversation_dirs, "list_idle_conversations", _list)

        report = await world.job().run_once()

        assert conv_dir.exists()
        assert report.conversation_dirs_skipped_active == 1
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_a_run_created_while_deleting_is_counted_and_not_stamped(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from retention_cleanup_job import conversation_dirs

    try:
        conv = await world.conversation(age_days=200)
        conv_dir = world.place(conv)
        real = conversation_dirs._park

        def _park(*args: object, **kwargs: object) -> object:
            out = real(*args, **kwargs)  # type: ignore[arg-type]
            assert not conv_dir.exists()  # moved away ...
            world.run(conv, age_days=0, status="queued")
            return out

        monkeypatch.setattr(conversation_dirs, "_park", _park)

        report = await world.job().run_once()

        assert report.conversation_dirs_raced == 1
        assert world.cleaned_at(conv) is None
        # ... and put back: the run that started meanwhile finds its directory where it was.
        assert (conv_dir / "work" / "report" / "draft.md").read_text() == "draft"
        assert _quarantined(world) == []
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_only_the_idle_conversations_own_things_go(world: _World) -> None:
    """不变式(同 ``test_workspace_invariant`` 的口径):删一段闲置会话,只动它自己的
    ``threads/<会话>/`` 与它的 run 的 ``.tool_results/<run>/``;别的会话、agent 根的记忆 / 模板 /
    上传、别的 run 的结果都不动。"""
    try:
        idle = await world.conversation(age_days=200)
        idle_run = world.run(idle, age_days=100)
        world.place(idle)
        world.tool_results(idle_run)
        busy = await world.conversation(age_days=200)
        world.run(busy, age_days=1)
        world.place(busy)
        other_run = world.tool_results(uuid4())
        agent = world.user_dir() / "agents" / _KEY
        for rel in ("style/s.md", "MEMORY.md", "AGENT_NOTES.md", "uploads/u.pdf"):
            (agent / rel).parent.mkdir(parents=True, exist_ok=True)
            (agent / rel).write_text(rel)
        before = set(world.root.rglob("*"))

        await world.job().run_once()

        after = set(world.root.rglob("*"))
        gone = before - after
        idle_dir = world.conv_dir(idle)
        idle_results = agent / ".tool_results" / str(idle_run)
        assert gone and all(
            p == idle_dir or idle_dir in p.parents or p == idle_results or idle_results in p.parents
            for p in gone
        )
        assert other_run.exists()
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_the_cleanup_stamp_is_consumed_once_without_touching_updated_at(
    world: _World,
) -> None:
    """B-282 —— 起新 run 的入口把清理标记清回空(拿到一次说明);``updated_at`` 不动,之后这段
    会话按新的活动重新算闲置。"""
    from expert_work.persistence import SqlThreadMetaStore

    try:
        conv = await world.conversation(age_days=200)
        world.place(conv)
        await world.job().run_once()
        assert world.cleaned_at(conv) is not None
        before = world.sql("SELECT updated_at FROM thread_meta WHERE thread_id = :t", t=conv)
        threads = SqlThreadMetaStore(world.sf)

        first = await threads.consume_workspace_cleaned(conv, tenant_id=world.tenant)
        second = await threads.consume_workspace_cleaned(conv, tenant_id=world.tenant)
        other_tenant = await threads.consume_workspace_cleaned(conv, tenant_id=uuid4())

        assert (first, second, other_tenant) == (True, False, False)
        assert world.cleaned_at(conv) is None
        assert (
            world.sql("SELECT updated_at FROM thread_meta WHERE thread_id = :t", t=conv) == before
        )
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_a_conversation_with_a_recent_upload_does_not_take_a_batch_slot(
    world: _World,
) -> None:
    try:
        busy = await world.conversation(age_days=300)
        world.run(busy, age_days=200)
        _upload(world, busy, created_days=5)
        busy_dir = world.place(busy)
        idle = await world.conversation(age_days=150)
        idle_dir = world.place(idle)

        report = await world.job(conversation_workspace_batch_size=1).run_once()

        assert busy_dir.exists() and not idle_dir.exists()
        assert report.conversation_dirs_skipped_active == 0
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_an_upload_landing_after_listing_keeps_the_dir(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from retention_cleanup_job import conversation_dirs

    try:
        conv = await world.conversation(age_days=200)
        world.run(conv, age_days=100)
        conv_dir = world.place(conv)
        real = conversation_dirs.list_idle_conversations

        async def _list(*args: object, **kwargs: object) -> list[object]:
            listed = await real(*args, **kwargs)  # type: ignore[arg-type]
            _upload(world, conv, created_days=0)
            return listed  # type: ignore[return-value]

        monkeypatch.setattr(conversation_dirs, "list_idle_conversations", _list)

        report = await world.job().run_once()

        assert conv_dir.exists()
        assert report.conversation_dirs_skipped_active == 1
    finally:
        await world.engine.dispose()


def _quarantined(world: _World, tenant: UUID | None = None) -> list[str]:
    """``{tenant}/.purged-agents/{user}/`` 里挪进来的条目名。"""
    holding = world.root / str(tenant or world.tenant) / ".purged-agents" / str(world.user)
    return sorted(p.name for p in holding.iterdir()) if holding.is_dir() else []


@pytest.mark.asyncio
async def test_the_directory_is_moved_to_quarantine_and_later_writes_survive(
    world: _World,
) -> None:
    """M1 —— 持锁只做改名:目录整个挪进沙箱挂不到的隔离区(满 7 天由隔离区那一条删,在那之前能
    挪回),之后按原路径写的东西落进新目录,不会被这一轮删掉。"""
    try:
        conv = await world.conversation(age_days=200)
        run = world.run(conv, age_days=120)
        world.tool_results(run)
        conv_dir = world.place(conv)

        report = await world.job().run_once()

        assert not conv_dir.exists()
        parked = _quarantined(world)
        assert len(parked) == 2
        assert any(p.startswith(f"{conv}-") for p in parked)
        assert any(p.startswith(f"{run}-") for p in parked)
        moved = next(
            world.root / str(world.tenant) / ".purged-agents" / str(world.user) / p
            for p in parked
            if p.startswith(str(conv))
        )
        assert (moved / "work" / "report" / "draft.md").read_text() == "draft"
        assert (report.conversation_dirs_removed, report.conversation_tool_results_removed) == (
            1,
            1,
        )

        (conv_dir / "work").mkdir(parents=True)
        (conv_dir / "work" / "late.md").write_text("late")
        await world.job().run_once()
        assert (conv_dir / "work" / "late.md").read_text() == "late"
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_an_upload_landing_holds_the_lock_the_sweep_waits_for(world: _World) -> None:
    """M1 —— 上传落盘拿同一把锁的共享形式;落盘进行中,清理在锁外等着。"""
    import asyncio

    import psycopg

    try:
        conv = await world.conversation(age_days=200)
        conv_dir = world.place(conv)
        dsn = world.admin_dsn.replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(dsn) as holder:
            holder.execute(
                "SELECT pg_advisory_xact_lock_shared(%s, hashtext(%s))",
                (8624, f"{world.tenant}:{conv}"),
            )
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(world.job().run_once(), timeout=3)
            assert conv_dir.exists()
            holder.rollback()

        await world.job().run_once()
        assert not conv_dir.exists()
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_a_live_upload_row_keeps_the_dir_until_the_upload_itself_expires(
    world: _World,
) -> None:
    """M2 —— 租户上传保留 365 天、会话目录 90 天:登记行还活着(下载读的就是这段会话
    ``work/uploads/`` 里的文件),目录不清;B-63 让登记行到期(标 ``deleted_at``)之后才清。"""
    try:
        world.sql(
            "INSERT INTO tenant_config (tenant_id, display_name, updated_by, "
            " upload_retention_days) VALUES (:t, 't', 'test', 365)",
            t=world.tenant,
        )
        conv = await world.conversation(age_days=200)
        world.run(conv, age_days=200)
        conv_dir = world.place(conv)
        _upload(world, conv, created_days=200, used_days=200)

        first = await world.job().run_once()

        assert conv_dir.exists()
        assert first.conversation_dirs_removed == 0
        assert first.conversation_dirs_skipped_active == 0  # not even listed

        world.sql("UPDATE user_upload SET deleted_at = now() WHERE thread_id = :t", t=conv)
        await world.job().run_once()

        assert not conv_dir.exists()
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_a_soft_deleted_tenant_user_does_not_take_a_batch_slot(world: _World) -> None:
    """M4 —— 已软删的用户(``tenant_user.deleted_at``)在查询里就排除,永远不占批。"""
    try:
        gone = await world.conversation(age_days=300)
        gone_dir = world.place(gone)
        world.sql(
            "INSERT INTO tenant_user (id, tenant_id, subject_type, subject_id, deleted_at) "
            "VALUES (:u, :t, 'user', 'gone', now())",
            u=world.user,
            t=world.tenant,
        )
        other_user = uuid4()
        live = await world.conversation(age_days=200)
        world.sql("UPDATE thread_meta SET user_id = :u WHERE thread_id = :t", u=other_user, t=live)
        live_dir = world.root / str(world.tenant) / str(other_user) / "agents" / _KEY / "threads"
        (live_dir / str(live) / "work").mkdir(parents=True)

        report = await world.job(conversation_workspace_batch_size=1).run_once()

        assert gone_dir.exists()
        assert not (live_dir / str(live)).exists()
        assert report.conversation_dirs_removed == 1
        assert report.conversation_dirs_skipped_owner_deleted == 0
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_a_conversation_that_cannot_be_cleaned_goes_to_the_back_of_the_queue(
    world: _World, caplog: pytest.LogCaptureFixture
) -> None:
    """M4 —— 列到了却清不了(这里:审计里有近期上传)的会话记一笔尝试时间,下一轮排到没试过的
    后面;整批大半没清时打 warning。"""
    from expert_work.persistence import SqlAuditLogStore

    try:
        stuck = await world.conversation(age_days=300)
        world.run(stuck, age_days=250)
        stuck_dir = world.place(stuck)
        await _upload_audit(world, stuck, age_days=5)
        later = await world.conversation(age_days=200)
        later_dir = world.place(later)
        job_kwargs: dict[str, object] = {
            "audit_log_store": SqlAuditLogStore(world.sf),
            "conversation_workspace_batch_size": 1,
        }

        with caplog.at_level("WARNING"):
            first = await world.job(**job_kwargs).run_once()

        assert (first.conversation_dirs_skipped_active, first.conversation_dirs_removed) == (1, 0)
        assert later_dir.exists()
        assert world.sql(
            "SELECT workspace_clean_attempted_at IS NOT NULL, workspace_cleaned_at IS NULL "
            "FROM thread_meta WHERE thread_id = :t",
            t=stuck,
        ) == [(True, True)]
        assert "retention.conversation_dirs_mostly_skipped skipped=1 batch=1" in caplog.text

        second = await world.job(**job_kwargs).run_once()

        assert second.conversation_dirs_removed == 1
        assert not later_dir.exists() and stuck_dir.exists()
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_an_audited_upload_after_the_pass_started_keeps_the_dir(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L3 —— 持锁再查看审计看到「现在」,不是这一轮开始的时刻:开跑之后才落的上传也算。
    (这一轮的「开始」拨早一小时,上传审计记在真实的现在。)"""
    from datetime import timedelta

    from expert_work.persistence import SqlAuditLogStore
    from retention_cleanup_job import conversation_dirs

    try:
        conv = await world.conversation(age_days=200)
        world.run(conv, age_days=100)
        conv_dir = world.place(conv)
        await _upload_audit(world, conv, age_days=0)
        real = conversation_dirs.ConversationDirSweep.run

        async def _run(self: object, *, now: object) -> object:
            return await real(self, now=now - timedelta(hours=1))  # type: ignore[arg-type, operator]

        monkeypatch.setattr(conversation_dirs.ConversationDirSweep, "run", _run)

        report = await world.job(audit_log_store=SqlAuditLogStore(world.sf)).run_once()

        assert conv_dir.exists()
        assert report.conversation_dirs_skipped_active == 1
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_an_old_live_upload_row_appearing_after_listing_keeps_the_dir(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2 —— 持锁再查同样看没删的登记行(哪怕它的时间早就过了闲置线)。"""
    from retention_cleanup_job import conversation_dirs

    try:
        conv = await world.conversation(age_days=200)
        world.run(conv, age_days=150)
        conv_dir = world.place(conv)
        real = conversation_dirs.list_idle_conversations

        async def _list(*args: object, **kwargs: object) -> list[object]:
            listed = await real(*args, **kwargs)  # type: ignore[arg-type]
            _upload(world, conv, created_days=180, used_days=180)
            return listed  # type: ignore[return-value]

        monkeypatch.setattr(conversation_dirs, "list_idle_conversations", _list)

        report = await world.job().run_once()

        assert conv_dir.exists()
        assert report.conversation_dirs_skipped_active == 1
    finally:
        await world.engine.dispose()
