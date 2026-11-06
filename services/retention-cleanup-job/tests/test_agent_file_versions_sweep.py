"""B-284 —— 留存作业里的 ``/agent`` 文件历史版本到期:源文件不见满 30 天整个删、老版本按 90 天封顶。

真落盘:临时目录当 NAS 根,版本目录按 ``orchestrator.tools.agent_file_versions`` 的形状造。
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from retention_cleanup_job.agent_file_versions import (
    KEEP_NEWEST,
    sweep_agent_file_versions,
)

_KEY = "plan-aaaaaaaa"
_DAY = 86400


class _World:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.tenant, self.user = uuid4(), uuid4()

    def agent(self) -> Path:
        return self.root / str(self.tenant) / str(self.user) / "agents" / _KEY

    def source(self, rel: str) -> Path:
        path = self.agent() / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("src")
        return path

    def versions(
        self, rel: str, count: int = 1, user: UUID | None = None, dir_name: str | None = None
    ) -> Path:
        file_dir = (
            self.root
            / str(self.tenant)
            / ".agent-file-versions"
            / str(user or self.user)
            / _KEY
            / (dir_name or hashlib.sha256(rel.encode()).hexdigest()[:32])
        )
        file_dir.mkdir(parents=True)
        (file_dir / "path").write_text(rel)
        for i in range(count):
            (file_dir / f"v{i:08d}-{'0' * 64}").write_text(f"v{i}")
        return file_dir

    def marker(self, file_dir: Path, *, first_days_ago: float, last_days_ago: float) -> None:
        now = time.time()
        marker = file_dir / "missing"
        marker.write_text(f"{now - first_days_ago * _DAY:.0f}")
        last = now - last_days_ago * _DAY
        os.utime(marker, (last, last))

    def sweep(self, *, days_ahead: float = 0) -> object:
        return sweep_agent_file_versions(str(self.root), now=time.time() + days_ahead * _DAY)


@pytest.fixture
def world(tmp_path: Path) -> _World:
    return _World(tmp_path)


def test_a_live_source_clears_its_mark_and_keeps_its_versions(world: _World) -> None:
    world.source("tpl/a.md")
    file_dir = world.versions("tpl/a.md", 2)
    world.marker(file_dir, first_days_ago=40, last_days_ago=1)

    tally = world.sweep()

    assert file_dir.is_dir() and not (file_dir / "missing").exists()
    assert (tally.dirs_dropped, tally.failed) == (0, 0)  # type: ignore[attr-defined]


def test_a_newly_missing_source_is_marked_not_dropped(world: _World) -> None:
    world.agent().mkdir(parents=True)
    file_dir = world.versions("tpl/gone.md")

    tally = world.sweep()

    assert file_dir.is_dir()
    assert float((file_dir / "missing").read_text()) == pytest.approx(time.time(), abs=60)
    assert tally.dirs_dropped == 0  # type: ignore[attr-defined]


def test_a_source_missing_for_thirty_days_is_dropped_even_if_the_agent_is_idle(
    world: _World,
) -> None:
    """以前只有 run 结束时的观察会删:不再跑的 agent 的版本永远留着。现在留存作业每晚自己确认。"""
    world.source("keep.md")
    gone = world.versions("tpl/gone.md", 3)
    world.marker(gone, first_days_ago=31, last_days_ago=1)
    kept = world.versions("keep.md", 1)

    tally = world.sweep()

    assert not gone.exists()
    assert kept.is_dir()
    assert tally.dirs_dropped == 1  # type: ignore[attr-defined]


def test_a_stale_confirmation_restarts_the_thirty_days(world: _World) -> None:
    """上一次确认不见已经是 8 天前(中间没人看过,文件可能回来过):从这次重新算。"""
    world.agent().mkdir(parents=True)
    file_dir = world.versions("gone.md")
    world.marker(file_dir, first_days_ago=60, last_days_ago=8)

    tally = world.sweep()

    assert file_dir.is_dir()
    assert float((file_dir / "missing").read_text()) == pytest.approx(time.time(), abs=60)
    assert tally.dirs_dropped == 0  # type: ignore[attr-defined]


def test_a_symlink_on_the_source_path_means_unknown_and_nothing_moves(
    world: _World, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside")
    world.agent().mkdir(parents=True)
    os.symlink(outside, world.agent() / "tpl")
    file_dir = world.versions("tpl/gone.md")
    world.marker(file_dir, first_days_ago=40, last_days_ago=1)
    before = (file_dir / "missing").stat().st_mtime

    tally = world.sweep()

    assert file_dir.is_dir()
    assert (file_dir / "missing").stat().st_mtime == before
    assert tally.dirs_dropped == 0  # type: ignore[attr-defined]


def test_versions_older_than_ninety_days_beyond_the_newest_three_are_removed(
    world: _World,
) -> None:
    world.source("tpl/a.md")
    file_dir = world.versions("tpl/a.md", 6)

    young = world.sweep(days_ahead=10)
    assert young.versions_capped == 0  # type: ignore[attr-defined]

    old = world.sweep(days_ahead=91)

    names = sorted(p.name for p in file_dir.iterdir() if p.name.startswith("v"))
    assert len(names) == KEEP_NEWEST
    assert names == [f"v{i:08d}-{'0' * 64}" for i in (3, 4, 5)]
    assert old.versions_capped == 3  # type: ignore[attr-defined]


def test_a_soft_deleted_user_is_left_alone(world: _World) -> None:
    world.agent().mkdir(parents=True)
    file_dir = world.versions("gone.md")
    world.marker(file_dir, first_days_ago=40, last_days_ago=1)
    marker = world.root / str(world.tenant) / ".deleted" / str(world.user)
    marker.parent.mkdir(parents=True)
    marker.write_text("")

    tally = world.sweep()

    assert file_dir.is_dir()
    assert tally.dirs_dropped == 0  # type: ignore[attr-defined]


def test_one_unreadable_dir_is_counted_and_the_rest_still_run(world: _World) -> None:
    world.agent().mkdir(parents=True)
    bad = world.versions("bad.md")
    gone = world.versions("gone.md")
    world.marker(gone, first_days_ago=31, last_days_ago=1)
    bad.chmod(0)
    try:
        tally = world.sweep()
    finally:
        bad.chmod(0o700)

    assert not gone.exists()
    assert tally.failed == 1  # type: ignore[attr-defined]
    assert tally.needs_attention  # type: ignore[attr-defined]


def test_leftover_tmp_files_are_removed_after_an_hour(world: _World) -> None:
    """写到一半被杀留下的 ``.tmp-*``:满 1 小时(按 ctime)删掉,版本不动。"""
    world.source("tpl/a.md")
    file_dir = world.versions("tpl/a.md", 1)
    (file_dir / ".tmp-abc").write_text("half")

    world.sweep()
    assert (file_dir / ".tmp-abc").exists()

    world.sweep(days_ahead=0.05)  # 72 minutes later
    assert not (file_dir / ".tmp-abc").exists()
    assert (file_dir / f"v{0:08d}-{'0' * 64}").exists()


@pytest.mark.parametrize("rel", ["notes.md ", " notes.md", "tpl/ a .md "])
def test_a_live_source_whose_name_has_edge_spaces_is_not_missing(world: _World, rel: str) -> None:
    """M3 —— 文件名首尾的空格是合法的:``path`` 原样用,不 ``strip``;活文件不会被判成「不见」,
    满 30 天也不删它的历史。"""
    world.source(rel)
    file_dir = world.versions(rel)

    for day in (0, 31, 62):
        world.sweep(days_ahead=day)

    assert file_dir.is_dir()
    assert not (file_dir / "missing").exists()


def test_a_path_that_does_not_match_its_dir_name_is_unknown(world: _World) -> None:
    """M3 —— ``path`` 与目录名(源路径 sha256 前 32 位)对不上(写了一半 / 坏了)= 不知道:不记标记、
    不删,同 orchestrator 的 ``_valid_path``。"""
    file_dir = world.versions("gone.md", dir_name="0" * 32)

    for day in (0, 31, 62):
        world.sweep(days_ahead=day)

    assert file_dir.is_dir()
    assert not (file_dir / "missing").exists()


def test_a_deleted_source_whose_name_has_edge_spaces_still_ages_out(world: _World) -> None:
    """M3 —— 反过来:源文件真不见了,首尾带空格的路径照样按原样查出「不见」,满 30 天删掉历史
    (不会因为路径被改动而永远「不知道」)。"""
    world.source("kept.md")  # the agent root itself is there
    file_dir = world.versions(" gone.md ")
    world.marker(file_dir, first_days_ago=31, last_days_ago=1)

    world.sweep()

    assert not file_dir.exists()
