"""B-284 —— agent 根 B-236 之前的存量与冻结的 ``shared/``:真 Postgres + 临时 NAS 根。

切换时刻 ``T`` 钉在配置里(``agent_root_cutover``)。「T 之前」与「满天数」都看 ``max(mtime, ctime)``,
而测试里刚建的文件 ctime 就是现在、改不了。所以:

* agent 根这一步在「现在 + :data:`_LATER` 天」跑(``sweep_agent_roots`` 的 ``now``);用例里的
  「N 天前」都相对这个时刻(:meth:`_World.age` 把 mtime 拨到它之前 N 天,N < :data:`_LATER` 时
  mtime 在真实的将来,N ≥ :data:`_LATER` 时由 ctime 决定 —— 正好 :data:`_LATER` 天前),run 的时间
  同样相对它;
* 要算「T 之前」的用例把 ``T`` 钉在更远的将来,要算「T 之后」的钉在(真实的)一小时前。
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import DatabaseConfig, create_async_engine_from_config
from expert_work.protocol.agent_key import sanitize_agent_key
from retention_cleanup_job.job import RetentionCleanupJob
from retention_cleanup_job.main import build_session_factory

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parents[3] / "packages/expert-work-persistence/alembic.ini"
_AGENT = "Plan Bot"
_KEY = sanitize_agent_key(_AGENT)
_DAY = 86400.0
#: agent 根这一步「现在」比真实的现在晚这么多天(见模块头)。
_LATER = 100.0


def _dsn(container: PostgresContainer, driver: str) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", f"+{driver}").replace(
        "postgresql://", f"postgresql+{driver}://", 1
    )


@dataclass
class _World:
    engine: AsyncEngine
    sf: async_sessionmaker[AsyncSession]
    admin_dsn: str
    root: Path
    tenant: UUID
    user: UUID
    monkeypatch: pytest.MonkeyPatch
    #: agent 根这一步看到的「现在」(unix 秒)。
    now: float

    def sql(self, statement: str, **params: object) -> None:
        admin = create_engine(self.admin_dsn, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                conn.execute(text(statement), params)
        finally:
            admin.dispose()

    def thread(
        self, *, age_days: float, layout: str = "conversation", tenant: UUID | None = None
    ) -> UUID:
        conv = uuid4()
        self.sql(
            "INSERT INTO thread_meta (thread_id, tenant_id, user_id, created_by, status, "
            " agent_name, workspace_layout, created_at, updated_at) VALUES (:c, :t, :u, 'u', "
            " 'active', :a, :l, now() - make_interval(secs => :s), now())",
            c=conv,
            t=tenant or self.tenant,
            u=self.user,
            a=_AGENT,
            l=layout,
            s=(age_days - _LATER) * _DAY,
        )
        return conv

    def run(self, conv: UUID, *, age_days: float, tenant: UUID | None = None) -> None:
        self.sql(
            "INSERT INTO agent_run (id, tenant_id, thread_id, status, on_disconnect, created_at, "
            " updated_at) VALUES (:r, :t, :c, 'success', 'continue', "
            " now() - make_interval(secs => :s), now())",
            r=uuid4(),
            t=tenant or self.tenant,
            c=conv,
            s=(age_days - _LATER) * _DAY,
        )

    def register(self, rel: str) -> None:
        artifact = uuid4()
        self.sql(
            "INSERT INTO artifact (id, tenant_id, user_id, agent_key, name, kind, latest_version) "
            "VALUES (:a, :t, :u, :k, :n, 'document', 1)",
            a=artifact,
            t=self.tenant,
            u=self.user,
            k=_KEY,
            n=rel,
        )
        self.sql(
            "INSERT INTO artifact_version (id, artifact_id, tenant_id, user_id, version, "
            " path_in_workspace, created_in_thread) VALUES (:i, :a, :t, :u, 1, :p, 'r')",
            i=uuid4(),
            a=artifact,
            t=self.tenant,
            u=self.user,
            p=f"agents/{_KEY}/{rel}",
        )

    def agent(self, tenant: UUID | None = None) -> Path:
        return self.root / str(tenant or self.tenant) / str(self.user) / "agents" / _KEY

    def put(self, rel: str, *, age_days: float, tenant: UUID | None = None) -> Path:
        path = self.agent(tenant) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rel)
        self.age(path, age_days)
        return path

    def age(self, path: Path, age_days: float) -> None:
        """mtime 拨到「agent 根这一步的现在」之前 ``age_days`` 天(ctime 是真实的现在)。"""
        when = self.now - age_days * _DAY
        os.utime(path, (when, when), follow_symlinks=False)

    def job(self, *, now: float | None = None, **kwargs: object) -> RetentionCleanupJob:
        """agent 根这一步在 ``now``(默认 :attr:`now`)跑;别的 pass 照常用真实时间。"""
        from retention_cleanup_job import agent_roots
        from retention_cleanup_job import job as job_module

        self.monkeypatch.setattr(
            job_module,
            "sweep_agent_roots",
            partial(
                agent_roots.sweep_agent_roots,
                now=datetime.fromtimestamp(self.now if now is None else now, UTC),
            ),
        )
        return RetentionCleanupJob(  # type: ignore[arg-type]
            db_session_factory=self.sf, workspace_root=str(self.root), **kwargs
        )


@pytest.fixture
def world(
    postgres_container: PostgresContainer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[_World]:
    admin_dsn = _dsn(postgres_container, "psycopg")
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
    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=_dsn(postgres_container, "asyncpg"))
    )
    root = tmp_path / "nas"
    root.mkdir()
    yield _World(
        engine,
        build_session_factory(engine),
        admin_dsn,
        root,
        uuid4(),
        uuid4(),
        monkeypatch,
        time.time() + _LATER * _DAY,
    )


def _iso(delta_days: float) -> str:
    """一个带时区的 ISO 时刻,离现在 ``delta_days`` 天(正 = 将来)。"""
    return (datetime.now(UTC) + timedelta(days=delta_days)).isoformat()


#: 测试里的文件都是刚建的(ctime = 现在),mtime 最晚拨到 ``_LATER`` 天之后:要它们都算「T 之前」,
#: T 钉在比那还远的将来。
_FUTURE_T = _iso(2 * _LATER)


@pytest.mark.asyncio
async def test_nothing_is_touched_without_a_pinned_cutover(world: _World) -> None:
    """``T`` 不再从库里现算:库里有对话视图会话也不跑,只看配置。"""
    try:
        world.thread(age_days=400)
        old = world.put("old.txt", age_days=300)

        report = await world.job().run_once()

        assert old.exists()
        assert report.agent_root_files_removed == 0
        assert report.failed_passes == ()
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["20261201", "2026-12-01T00:00:00", "yesterday"])
async def test_a_malformed_cutover_disables_the_step_and_needs_attention(
    world: _World, raw: str, caplog: pytest.LogCaptureFixture
) -> None:
    try:
        old = world.put("old.txt", age_days=300)

        with caplog.at_level("INFO"):
            report = await world.job(agent_root_cutover=raw).run_once()

        assert old.exists()
        assert "agent_roots" in report.failed_passes
        assert (
            "retention.config_invalid env=EXPERT_WORK_RETENTION_AGENT_ROOT_CUTOVER" in caplog.text
        )
        assert "retention.agent_roots_gate cutover=off" in caplog.text
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_the_cutover_comes_from_config_not_from_conversations(world: _World) -> None:
    """清除最早的那段对话不会挪动 ``T``:库里有没有、最早是哪天的对话视图会话都不影响。"""
    try:
        first = world.thread(age_days=400)
        world.sql("DELETE FROM thread_meta WHERE thread_id = :t", t=first)
        expired = world.put("old.txt", age_days=100)

        report = await world.job(agent_root_cutover=_FUTURE_T).run_once()

        assert not expired.exists()
        assert report.agent_root_files_removed == 1
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_top_level_files_written_before_the_switch_expire(world: _World) -> None:
    """T 之前写的、没登记成产物的顶层文件,mtime 满 90 天删;其余一律不动。"""
    try:
        expired = world.put("old-report.txt", age_days=100)
        young = world.put("pre-switch.txt", age_days=40)
        artifact = world.put("final.docx", age_days=100)
        world.register("final.docx")
        memory = [
            world.put(name, age_days=100)
            for name in ("AGENT_NOTES.md", "MEMORY.md", "PLAN.md", "TODO.md")
        ]
        dot = world.put(".hidden", age_days=100)
        upload = world.put("uploads/u.pdf", age_days=100)
        world.age(upload.parent, 100)
        target = world.root.parent / "elsewhere.txt"
        target.write_text("keep")
        link = world.agent() / "link.txt"
        os.symlink(target, link)
        world.age(link, 100)

        report = await world.job(agent_root_cutover=_FUTURE_T).run_once()

        assert not expired.exists()
        for kept in (young, artifact, *memory, dot, upload, target):
            assert kept.exists(), kept.name
        assert os.path.lexists(link)
        assert report.agent_root_files_removed == 1
        assert report.agent_root_skipped_symlinks == 1
        assert report.failed_passes == ()
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_a_file_put_into_agent_after_the_switch_is_kept_even_with_an_old_mtime(
    world: _World,
) -> None:
    """C1 —— ``unzip`` / ``cp -p`` / ``touch -d``、开场复制都保留老 mtime:T 之后放进 ``/agent``
    的文件看 ctime(用户态改不了),不会被当成存量删掉。子目录同样。"""
    try:
        idle = world.thread(age_days=300, layout="agent_root")
        world.run(idle, age_days=100)
        restored = world.put("restored.md", age_days=200)  # ctime = now > T
        tpl = world.put("unzipped/a/b.md", age_days=200).parent.parent
        for path in (tpl / "a", tpl):
            world.age(path, 200)

        report = await world.job(agent_root_cutover=_iso(-1 / 24)).run_once()

        assert restored.exists()
        assert tpl.exists()
        assert (report.agent_root_files_removed, report.agent_root_dirs_removed) == (0, 0)
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_the_days_count_from_when_a_file_was_written_not_from_a_copied_mtime(
    world: _World,
) -> None:
    """``T`` 之前解压 / ``cp -p`` 出来的文件带着几年前的 mtime:天数也看 ``max(mtime, ctime)``,从
    写下那天(ctime)起满天数才删,不是第一晚就删。目录同样。"""
    try:
        idle = world.thread(age_days=300, layout="agent_root")
        world.run(idle, age_days=300)
        unzipped = world.put("unzipped.txt", age_days=1000)  # mtime from the archive
        tree = world.put("extracted/a.md", age_days=1000).parent
        world.age(tree, 1000)

        soon = await world.job(agent_root_cutover=_FUTURE_T, now=time.time() + 10 * _DAY).run_once()

        assert unzipped.exists() and tree.exists()
        assert (soon.agent_root_files_removed, soon.agent_root_dirs_removed) == (0, 0)

        later = await world.job(agent_root_cutover=_FUTURE_T).run_once()  # ctime now 100 days old

        assert not unzipped.exists() and not tree.exists()
        assert (later.agent_root_files_removed, later.agent_root_dirs_removed) == (1, 1)
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_template_dirs_from_before_the_switch_go_once_the_agent_is_idle(
    world: _World,
) -> None:
    try:
        idle_conv = world.thread(age_days=300, layout="agent_root")
        world.run(idle_conv, age_days=100)
        tpl = world.put("tpl/a/b.md", age_days=100).parent.parent
        for path in (tpl / "a", tpl):
            world.age(path, 100)
        mixed = world.put("mixed/old.md", age_days=100).parent
        world.put("mixed/new.md", age_days=5)
        with_artifact = world.put("reports/r.md", age_days=100).parent
        world.age(with_artifact, 100)
        world.register("reports/r.md")

        report = await world.job(agent_root_cutover=_FUTURE_T).run_once()

        assert not tpl.exists()
        assert mixed.exists()
        assert with_artifact.exists()
        assert report.agent_root_dirs_removed == 1
        assert not any(p.name.startswith(".retention-hold-") for p in world.agent().iterdir())
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_a_dir_that_changes_while_held_is_put_back(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2 —— 子目录先改名再复查:复查时发现有新东西(``T`` 之后),改回原名、不删。"""
    from retention_cleanup_job import agent_roots

    try:
        idle = world.thread(age_days=300, layout="agent_root")
        world.run(idle, age_days=100)
        tpl = world.put("tpl/t.md", age_days=100).parent
        world.age(tpl, 100)
        real = agent_roots._subtree_touched
        calls: list[bool] = []

        def _touched(path: Path, *, top_ctime: bool = True) -> float | None:
            calls.append(top_ctime)
            found = real(path, top_ctime=top_ctime)
            if found is None or top_ctime:
                return found
            return time.time() + 3 * _LATER * _DAY  # the re-walk sees a post-T entry

        monkeypatch.setattr(agent_roots, "_subtree_touched", _touched)

        report = await world.job(agent_root_cutover=_FUTURE_T).run_once()

        assert calls == [True, False]
        assert tpl.exists() and (tpl / "t.md").exists()
        assert report.agent_root_skipped_changed == 1
        assert not any(p.name.startswith(".retention-hold-") for p in world.agent().iterdir())
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_a_hold_dir_left_by_a_killed_run_is_finished_next_time(
    world: _World, caplog: pytest.LogCaptureFixture
) -> None:
    """M2 —— 改了名、没删完作业就被杀:下一轮按同样的判据删完;判据不成立(里面有新近的东西)
    就留着、打日志。"""
    try:
        idle = world.thread(age_days=300, layout="agent_root")
        world.run(idle, age_days=100)
        left = world.put(".retention-hold-dead/a/b.md", age_days=100).parent.parent
        for path in (left / "a", left):
            world.age(path, 100)
        busy = world.put(".retention-hold-busy/n.md", age_days=5).parent
        world.age(busy, 100)

        with caplog.at_level("WARNING"):
            report = await world.job(agent_root_cutover=_FUTURE_T).run_once()

        assert not left.exists()
        assert busy.exists() and (busy / "n.md").exists()
        assert report.agent_root_dirs_removed == 1
        assert "retention.agent_root_hold_left" in caplog.text
        assert ".retention-hold-busy" in caplog.text
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_only_the_pre_switch_stock_goes(world: _World) -> None:
    """不变式(``test_workspace_invariant`` 在没设切换时刻时的对应物):``T`` 钉在将来、所有东西都
    老,跑一次,「跑前 - 跑后」的路径差集逐字等于 agent 根顶层的存量 —— 平台目录、记忆文件、点开头的、
    用户根别处、``shared/``、别的租户(天数更长)、已软删用户,一个都不动。"""
    try:
        idle = world.thread(age_days=300, layout="agent_root")
        world.run(idle, age_days=100)
        long_tenant = uuid4()
        world.sql(
            "INSERT INTO tenant_config (tenant_id, display_name, updated_by, "
            " artifact_retention_days) VALUES (:t, 't', 'test', 365)",
            t=long_tenant,
        )
        gone_user = uuid4()
        user_root = world.root / str(world.tenant) / str(world.user)
        other_agent = user_root / "agents" / "other-agent"
        files = [
            world.agent() / "old.txt",
            world.agent() / "tpl" / "a.md",
            world.agent() / "AGENT_NOTES.md",
            world.agent() / "MEMORY.md",
            world.agent() / ".hidden",
            world.agent() / "threads" / str(idle) / "work" / "x.md",
            world.agent() / "uploads" / "u.pdf",
            world.agent() / "inputs" / "i.txt",
            world.agent() / "skills" / "s" / "SKILL.md",
            world.agent() / ".tool_results" / str(uuid4()) / "o.txt",
            other_agent / "stale.txt",
            other_agent / "recent.txt",
            user_root / "notes.txt",
            user_root / "shared" / "legacy.md",
            world.agent(long_tenant) / "old.txt",
            world.root / str(world.tenant) / str(gone_user) / "agents" / _KEY / "old.txt",
        ]
        for path in files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")
        (world.root / str(world.tenant) / ".deleted").mkdir()
        (world.root / str(world.tenant) / ".deleted" / str(gone_user)).write_text("")
        for path in world.root.rglob("*"):
            world.age(path, 100)
        world.age(other_agent / "recent.txt", 5)
        before = {str(p.relative_to(world.root)) for p in world.root.rglob("*")}

        report = await world.job(agent_root_cutover=_FUTURE_T).run_once()

        after = {str(p.relative_to(world.root)) for p in world.root.rglob("*")}
        base = f"{world.tenant}/{world.user}/agents"
        assert before - after == {
            f"{base}/{_KEY}/old.txt",
            f"{base}/{_KEY}/tpl",
            f"{base}/{_KEY}/tpl/a.md",
            f"{base}/other-agent/stale.txt",
        }
        assert after - before == set()
        assert report.failed_passes == ()
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_a_recently_used_agent_keeps_its_old_template_dirs(world: _World) -> None:
    try:
        conv = world.thread(age_days=300, layout="agent_root")
        world.run(conv, age_days=10)
        tpl = world.put("tpl/t.md", age_days=100).parent
        world.age(tpl, 100)

        await world.job(agent_root_cutover=_FUTURE_T).run_once()

        assert tpl.exists()
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_the_tenant_artifact_days_apply(world: _World) -> None:
    try:
        short = uuid4()
        world.sql(
            "INSERT INTO tenant_config (tenant_id, display_name, updated_by, "
            " artifact_retention_days) VALUES (:t, 't', 'test', 30)",
            t=short,
        )
        short_file = world.put("a.txt", age_days=40, tenant=short)
        plain_file = world.put("a.txt", age_days=40)

        await world.job(agent_root_cutover=_FUTURE_T).run_once()

        assert not short_file.exists()
        assert plain_file.exists()
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_shared_is_removed_only_after_the_configured_date(
    world: _World, caplog: pytest.LogCaptureFixture
) -> None:
    try:
        shared = world.root / str(world.tenant) / str(world.user) / "shared"
        (shared / "legacy").mkdir(parents=True)
        (shared / "legacy" / "x.md").write_text("x")

        unset = await world.job().run_once()
        empty = await world.job(shared_delete_after="").run_once()
        future = await world.job(shared_delete_after="2099-01-01").run_once()
        assert shared.exists()
        assert unset.shared_dirs_removed == empty.shared_dirs_removed == 0
        assert future.shared_dirs_removed == 0
        assert unset.failed_passes == empty.failed_passes == future.failed_passes == ()

        with caplog.at_level("INFO"):
            past = await world.job(shared_delete_after="2026-11-06").run_once()

        assert not shared.exists()
        assert past.shared_dirs_removed == 1
        assert (
            "retention.agent_roots_gate cutover=off shared_delete_after=2026-11-06" in caplog.text
        )
        assert (world.root / str(world.tenant) / str(world.user)).is_dir()
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["20261201", "2026/12/01", "2026-01-01", "1"])
async def test_a_malformed_shared_date_skips_only_shared_and_needs_attention(
    world: _World, raw: str
) -> None:
    """H2 —— 数字不会被当成 unix 时间(20261201 → 1970 → 立刻全删),写错的日期也不让作业起不来:
    只跳过 shared/ 这一步、记失败。早于功能上线的日期同样算写错。"""
    try:
        shared = world.root / str(world.tenant) / str(world.user) / "shared"
        shared.mkdir(parents=True)
        expired = world.put("old.txt", age_days=100)

        report = await world.job(shared_delete_after=raw, agent_root_cutover=_FUTURE_T).run_once()

        assert shared.exists()
        assert not expired.exists()  # the agent-root step still ran
        assert "agent_roots" in report.failed_passes
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_a_failure_is_isolated_counted_and_needs_attention(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from expert_work.persistence.workspace.safe_remove import NasEntryStuckError
    from retention_cleanup_job import agent_roots

    try:
        stuck = world.put("stuck.txt", age_days=100)
        fine = world.put("fine.txt", age_days=100)
        real = agent_roots.remove_entry

        def _remove(root: str, parts: list[str], name: str, **kwargs: object) -> str:
            if name == "stuck.txt":
                raise NasEntryStuckError(13, "cannot remove or quarantine")
            return real(root, parts, name, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(agent_roots, "remove_entry", _remove)

        report = await world.job(agent_root_cutover=_FUTURE_T).run_once()

        assert stuck.exists() and not fine.exists()
        assert report.agent_root_failed == 1
        assert "agent_roots" in report.failed_passes
    finally:
        await world.engine.dispose()


@pytest.mark.asyncio
async def test_a_subtree_too_big_to_judge_is_kept_and_counted(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from retention_cleanup_job import agent_roots

    try:
        idle = world.thread(age_days=300, layout="agent_root")
        world.run(idle, age_days=100)
        big = world.put("big/a.md", age_days=100).parent
        world.put("big/b.md", age_days=100)
        world.age(big, 100)
        monkeypatch.setattr(agent_roots, "MAX_SUBTREE_ENTRIES", 1)

        report = await world.job(agent_root_cutover=_FUTURE_T).run_once()

        assert big.exists()
        assert report.agent_root_skipped_unknown == 1
    finally:
        await world.engine.dispose()


def test_the_done_line_carries_the_agent_root_counts(caplog: pytest.LogCaptureFixture) -> None:
    from retention_cleanup_job.main import log_done
    from retention_cleanup_job.report import CleanupReport

    with caplog.at_level("INFO"):
        log_done(
            CleanupReport(
                agent_root_files_removed=1,
                agent_root_dirs_removed=2,
                shared_dirs_removed=3,
                agent_root_failed=4,
                agent_file_version_dirs_dropped=5,
                agent_file_versions_capped=6,
                agent_file_versions_failed=7,
                agent_root_skipped_unknown=8,
                agent_root_skipped_symlinks=9,
                agent_root_skipped_changed=10,
            )
        )
    line = next(
        r.getMessage() for r in caplog.records if "retention_cleanup_job.done" in r.getMessage()
    )
    for part in (
        "agent_root_files_removed=1",
        "agent_root_dirs_removed=2",
        "shared_dirs_removed=3",
        "agent_root_failed=4",
        "agent_file_version_dirs_dropped=5",
        "agent_file_versions_capped=6",
        "agent_file_versions_failed=7",
        "agent_root_skipped_unknown=8",
        "agent_root_skipped_symlinks=9",
        "agent_root_skipped_changed=10",
    ):
        assert part in line, part


def test_remove_entry_refuses_a_name_that_is_no_longer_the_same_inode(tmp_path: Path) -> None:
    """M2 —— 判定之后被换掉(重建同名文件 / 目录)就不删:``expect`` 核对 ``(st_dev, st_ino)``。"""
    from expert_work.persistence.workspace.safe_remove import CHANGED, REMOVED, remove_entry

    tenant, user = uuid4(), uuid4()
    base = tmp_path / str(tenant) / str(user)
    base.mkdir(parents=True)
    target = base / "f.txt"
    target.write_text("old")
    seen = os.lstat(target)
    target.unlink()
    target.write_text("new")
    replacement = os.lstat(target)
    if (replacement.st_dev, replacement.st_ino) == (seen.st_dev, seen.st_ino):
        pytest.skip("the filesystem reused the inode number")

    outcome = remove_entry(
        str(tmp_path),
        [str(tenant), str(user)],
        "f.txt",
        tenant_id=tenant,
        owner_id=user,
        expect=(seen.st_dev, seen.st_ino),
    )

    assert outcome == CHANGED
    assert target.read_text() == "new"
    assert (
        remove_entry(
            str(tmp_path),
            [str(tenant), str(user)],
            "f.txt",
            tenant_id=tenant,
            owner_id=user,
            expect=(replacement.st_dev, replacement.st_ino),
        )
        == REMOVED
    )
