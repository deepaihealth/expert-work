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
        real = conversation_dirs.remove_entry

        def _remove(root: str, parts: list[str], name: str, **kwargs: object) -> str:
            if name == str(stuck):
                raise NasEntryStuckError(13, "cannot remove or quarantine")
            return real(root, parts, name, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(conversation_dirs, "remove_entry", _remove)

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
    """用户已软删(``{tenant}/.deleted/{user}``):整棵树归 janitor 归档,这里不动,记一笔不再列。"""
    try:
        conv = await world.conversation(age_days=200)
        conv_dir = world.place(conv)
        marker = world.root / str(world.tenant) / ".deleted" / str(world.user)
        marker.parent.mkdir(parents=True)
        marker.write_text("")

        report = await world.job().run_once()

        assert conv_dir.exists()
        assert report.conversation_dirs_skipped_owner_deleted == 1
        assert world.cleaned_at(conv) is not None
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
