"""B-236 第三层 —— ``/agent`` 文件的版本:运行结束时看一眼,改过的留一份。真落盘。"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from orchestrator.tools import agent_file_versions as versions_mod
from orchestrator.tools.agent_file_versions import (
    MAX_VERSIONS_PER_FILE,
    NasAgentFileVersionStore,
    dispatch_observation,
)
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore

_TENANT, _USER = uuid4(), uuid4()
_KEY = "plan-aaaaaaaa"
_C1 = UUID("11111111-1111-1111-1111-111111111111")


def _agent(root: Path) -> Path:
    return root / str(_TENANT) / str(_USER) / "agents" / _KEY


def _versions_root(root: Path) -> Path:
    return root / str(_TENANT) / ".agent-file-versions" / str(_USER)


def _put(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _store(root: Path) -> NasAgentFileVersionStore:
    return NasAgentFileVersionStore(workspace=NasWorkspaceStore(root=str(root)))


def _history(root: Path) -> dict[str, list[str]]:
    """路径 → 各版本内容(按序号)。"""
    out: dict[str, list[str]] = {}
    base = _versions_root(root) / _KEY
    if not base.exists():
        return out
    for file_dir in base.iterdir():
        rel = (file_dir / "path").read_text()
        names = sorted(p.name for p in file_dir.iterdir() if p.name.startswith("v"))
        out[rel] = [(file_dir / n).read_text() for n in names]
    return out


async def _observe(root: Path) -> Any:
    return await _store(root).observe(tenant_id=_TENANT, user_id=_USER, agent_key=_KEY)


async def test_a_changed_file_is_snapshotted_and_an_unchanged_one_is_not(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    _put(agent / "AGENT_NOTES.md", "- tpl: /agent/tpl/t.md\n")
    tpl = _put(agent / "tpl" / "t.md", "v1")

    first = await _observe(tmp_path)
    assert first.snapshotted == 2
    assert await _observe(tmp_path) == versions_mod.ObserveReport(unchanged=2)

    _put(tpl, "v2")
    third = await _observe(tmp_path)
    assert (third.snapshotted, third.unchanged) == (1, 1)
    assert _history(tmp_path) == {
        "AGENT_NOTES.md": ["- tpl: /agent/tpl/t.md\n"],
        "tpl/t.md": ["v1", "v2"],
    }


async def test_a_touch_without_a_content_change_is_not_a_version(tmp_path: Path) -> None:
    tpl = _put(_agent(tmp_path) / "tpl" / "t.md", "same")
    await _observe(tmp_path)
    os.utime(tpl, ns=(1_800_000_000_000_000_000, 1_800_000_000_000_000_000))

    report = await _observe(tmp_path)

    assert (report.snapshotted, report.unchanged) == (0, 1)
    assert _history(tmp_path) == {"tpl/t.md": ["same"]}
    # 副本的 mtime 已对齐,下一次走快路径(不读内容)
    assert (await _observe(tmp_path)).unchanged == 1


async def test_only_the_last_twenty_versions_are_kept(tmp_path: Path) -> None:
    tpl = _agent(tmp_path) / "tpl" / "t.md"
    for i in range(MAX_VERSIONS_PER_FILE + 5):
        _put(tpl, f"v{i}")
        os.utime(tpl, ns=(i * 10**9 + 1, i * 10**9 + 1))
        await _observe(tmp_path)

    kept = _history(tmp_path)["tpl/t.md"]
    assert len(kept) == MAX_VERSIONS_PER_FILE
    assert kept[0] == "v5" and kept[-1] == f"v{MAX_VERSIONS_PER_FILE + 4}"


async def test_reserved_areas_dot_dirs_and_symlinks_are_not_versioned(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    outside = _put(tmp_path / "outside" / "secret.txt", "SECRET")
    for reserved in ("threads/c/work", "uploads", "inputs", ".tool_results", "skills", "shared"):
        _put(agent / reserved / "x.md", "r")
    _put(agent / ".hidden" / "x.md", "h")
    _put(agent / "tpl" / "t.md", "t")
    (agent / "tpl" / "leak.txt").symlink_to(outside)
    (agent / "linked").symlink_to(outside.parent)

    report = await _observe(tmp_path)

    assert _history(tmp_path) == {"tpl/t.md": ["t"]}
    assert report.skipped == 2


async def test_versions_live_outside_the_sandboxed_user_root(tmp_path: Path) -> None:
    _put(_agent(tmp_path) / "AGENT_NOTES.md", "x")
    await _observe(tmp_path)
    user_root = tmp_path / str(_TENANT) / str(_USER)
    assert _versions_root(tmp_path).exists()
    assert not any(".agent-file-versions" in str(p) for p in user_root.rglob("*"))


async def test_purging_the_user_removes_every_version(tmp_path: Path) -> None:
    _put(_agent(tmp_path) / "AGENT_NOTES.md", "a")
    _put(_agent(tmp_path) / "tpl" / "t.md", "b")
    await _observe(tmp_path)
    other_user = tmp_path / str(_TENANT) / ".agent-file-versions" / str(uuid4())
    other_user.mkdir()

    removed = await _store(tmp_path).delete_all_for_user(tenant_id=_TENANT, user_id=_USER)

    assert removed == 2
    assert not _versions_root(tmp_path).exists()
    assert other_user.exists()
    assert await _store(tmp_path).delete_all_for_user(tenant_id=_TENANT, user_id=_USER) == 0


async def test_an_agent_without_a_root_yet_observes_nothing(tmp_path: Path) -> None:
    assert await _observe(tmp_path) == versions_mod.ObserveReport()
    assert not (tmp_path / str(_TENANT) / ".agent-file-versions").exists()


# ---------------------------------------------------------------------------
# run 结束时的派发:只观察对话视图的主 run,失败不外抛
# ---------------------------------------------------------------------------


def _configurable(**extra: Any) -> dict[str, Any]:
    return {
        "tenant_id": str(_TENANT),
        "user_id": str(_USER),
        "agent_key": _KEY,
        "thread_id": str(_C1),
        "workspace_layout": "conversation",
        **extra,
    }


@pytest.mark.parametrize(
    "extra", [{"child_run": True}, {"workspace_layout": "agent_root"}, {"user_id": None}]
)
async def test_only_top_level_conversation_runs_are_observed(
    tmp_path: Path, extra: dict[str, Any]
) -> None:
    assert dispatch_observation(_store(tmp_path), _configurable(**extra)) is None
    assert dispatch_observation(None, _configurable()) is None


async def test_a_dispatched_observation_runs_and_a_failure_is_only_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _put(_agent(tmp_path) / "AGENT_NOTES.md", "a")
    task = dispatch_observation(_store(tmp_path), _configurable())
    assert task is not None
    await task
    assert _history(tmp_path) == {"AGENT_NOTES.md": ["a"]}

    def boom(*_args: Any) -> None:
        raise OSError(5, "EIO on /nas/客户甲/AGENT_NOTES.md")

    monkeypatch.setattr(versions_mod, "observe_sync", boom)
    with caplog.at_level("WARNING"):
        failing = dispatch_observation(_store(tmp_path), _configurable())
        assert failing is not None
        await asyncio.wait_for(failing, timeout=5)
    (record,) = [r for r in caplog.records if "agent_file_versions" in r.getMessage()]
    assert "OSError" in record.getMessage() and "客户甲" not in record.getMessage()


# ---------------------------------------------------------------------------
# review 7 / 10 —— 每个 agent 的字节预算、源文件不见满 30 天、删文件接口、清除之后不再建
# ---------------------------------------------------------------------------


def _version_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in _versions_root(root).rglob("v*") if p.is_file())


async def test_an_agents_versions_are_capped_oldest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """这个 agent 的合计超过预算:从最早写下的版本删起,跨文件 —— 最新的版本留着。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 10_000)
    agent = _agent(tmp_path)
    for i in range(6):
        for name in ("a.md", "b.md"):
            p = _put(agent / "tpl" / name, f"{name}{i}".ljust(1_000, "x"))
            os.utime(p, ns=(i * 10**9 + 1, i * 10**9 + 1))
        await _observe(tmp_path)
        await asyncio.sleep(0.01)  # 让各版本的 ctime 分得开

    assert _version_bytes(tmp_path) <= 10_000
    kept = _history(tmp_path)
    assert kept["tpl/a.md"][-1].startswith("a.md5") and kept["tpl/b.md"][-1].startswith("b.md5")
    assert not any(v.startswith(("a.md0", "b.md0")) for vs in kept.values() for v in vs)


async def test_versions_of_a_source_gone_for_thirty_days_are_dropped(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    keep = _put(agent / "tpl" / "keep.md", "k")
    gone = _put(agent / "tpl" / "gone.md", "g")
    await _observe(tmp_path)
    gone.unlink()

    await _observe(tmp_path)  # 第一次发现不见:记下,不删
    assert set(_history(tmp_path)) == {"tpl/keep.md", "tpl/gone.md"}
    markers = list(_versions_root(tmp_path).rglob("missing"))
    assert len(markers) == 1
    old = 1_000_000_000
    os.utime(markers[0], (old, old))

    report = await _observe(tmp_path)

    assert report.dropped == 1
    assert set(_history(tmp_path)) == {"tpl/keep.md"}
    assert keep.exists()


async def test_a_source_that_comes_back_clears_its_missing_mark(tmp_path: Path) -> None:
    f = _put(_agent(tmp_path) / "tpl" / "t.md", "t")
    await _observe(tmp_path)
    f.unlink()
    await _observe(tmp_path)
    _put(f, "t")
    await _observe(tmp_path)
    assert list(_versions_root(tmp_path).rglob("missing")) == []


async def test_deleting_a_file_through_the_api_removes_its_versions(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from control_plane.api._workspace_shared import drop_file_versions

    _put(_agent(tmp_path) / "tpl" / "t.md", "t")
    _put(_agent(tmp_path) / "tpl" / "u.md", "u")
    await _observe(tmp_path)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(agent_file_versions=_store(tmp_path)))
    )

    await drop_file_versions(
        request,  # type: ignore[arg-type]
        tenant_id=_TENANT,
        user_id=_USER,
        path=f"agents/{_KEY}/tpl/t.md",
    )

    assert set(_history(tmp_path)) == {"tpl/u.md"}
    for not_versioned in (
        f"agents/{_KEY}/threads/x/work/a.md",
        "flat.md",
        f"agents/{_KEY}/uploads/a",
    ):
        assert (
            await _store(tmp_path).delete_for_path(
                tenant_id=_TENANT, user_id=_USER, path=not_versioned
            )
            == 0
        )


async def test_a_run_ending_after_the_user_was_purged_writes_nothing(tmp_path: Path) -> None:
    from orchestrator.tools.nas_workspace_store import workspace_deleted_marker

    _put(_agent(tmp_path) / "AGENT_NOTES.md", "a")
    marker = workspace_deleted_marker(str(tmp_path), _TENANT, _USER)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()

    assert await _observe(tmp_path) == versions_mod.ObserveReport()
    assert not _versions_root(tmp_path).exists()


# ---------------------------------------------------------------------------
# review 8 / 10 —— 按 agent 隔离的预算、版本目录封顶、遍历不全不判「不见」
# ---------------------------------------------------------------------------


def _agent_dir(root: Path, key: str) -> Path:
    return root / str(_TENANT) / str(_USER) / "agents" / key


def _agent_version_bytes(root: Path, key: str = _KEY) -> int:
    base = _versions_root(root) / key
    return sum(p.stat().st_size for p in base.rglob("v*") if p.is_file())


def _version_count(root: Path, key: str) -> int:
    return len([p for p in (_versions_root(root) / key).rglob("v*") if p.is_file()])


async def _observe_key(root: Path, key: str) -> Any:
    return await _store(root).observe(tenant_id=_TENANT, user_id=_USER, agent_key=key)


async def test_another_agent_never_reduces_this_agents_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review 10 —— A 再大、再怎么换着名字写,B 的版本一份都不少(删只删 A 自己的)。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 10_000)
    notes = _agent_dir(tmp_path, "b-agent") / "AGENT_NOTES.md"
    for i in range(3):
        _put(notes, f"good notes v{i}")
        os.utime(notes, ns=(i * 10**9 + 1, i * 10**9 + 1))
        await _observe_key(tmp_path, "b-agent")
    bulky = _agent_dir(tmp_path, "a-agent")
    for r in range(8):
        for f in bulky.glob("*.bin"):
            f.unlink()
        for i in range(3):
            _put(bulky / f"r{r}-{i}.bin", "x" * 3_000)
        await _observe_key(tmp_path, "a-agent")
        assert _agent_version_bytes(tmp_path, "a-agent") <= 10_000

    assert _version_count(tmp_path, "b-agent") == 3
    for i in range(3, 5):
        _put(notes, f"good notes v{i}")
        os.utime(notes, ns=(i * 10**9 + 1, i * 10**9 + 1))
        await _observe_key(tmp_path, "b-agent")
    assert _version_count(tmp_path, "b-agent") == 5


async def test_the_store_stays_within_budget_under_churn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review 10 —— 每轮删掉旧文件、写新名字:不见了的版本先删,总量每轮都回到预算内。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 10_000)
    agent = _agent(tmp_path)
    for r in range(10):
        for f in agent.glob("j*"):
            f.unlink()
        for i in range(2):
            _put(agent / f"j{r}-{i}", f"{r}".ljust(3_000, "x"))
        await _observe(tmp_path)
        assert _agent_version_bytes(tmp_path) <= 10_000
        current = _history(tmp_path)
        assert all(f"j{r}-{i}" in current for i in range(2))  # 当前的文件都留住了


async def test_snapshotting_stops_when_the_newest_versions_alone_exceed_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review 10 —— 活文件最新那份加起来会超预算:不留新版本(记 over_budget),已有的一份不删。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 10_000)
    agent = _agent(tmp_path)
    a = _put(agent / "a.md", "1".ljust(4_000, "a"))
    os.utime(a, ns=(10**9, 10**9))
    await _observe(tmp_path)
    _put(a, "2".ljust(4_000, "a"))
    await _observe(tmp_path)
    _put(agent / "b.md", "b" * 7_000)

    report = await _observe(tmp_path)

    assert report.over_budget is True
    assert report.snapshotted == 0
    assert _history(tmp_path) == {"a.md": ["1".ljust(4_000, "a"), "2".ljust(4_000, "a")]}


async def test_skipped_files_leave_no_version_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review 10 —— 读预算不够跳过的文件不建版本目录(换着名字也绕不过目录数上限)。"""
    monkeypatch.setattr(versions_mod, "MAX_OBSERVE_READ_BYTES", 1_000)
    agent = _agent(tmp_path)
    big = _put(agent / "0big", "x" * 1_000)
    await _observe(tmp_path)
    now = time.time_ns()
    for r in range(1, 4):
        # 内容不变、mtime 变(且是最新的):每轮先把读预算用完
        os.utime(big, ns=(now + r * 10**9, now + r * 10**9))
        for f in agent.glob("r*"):
            f.unlink()
        for i in range(10):
            os.utime(_put(agent / f"r{r}-{i}", "y"), ns=(10**9, 10**9))  # 更旧:排在后面
        await _observe(tmp_path)

    dirs = list((_versions_root(tmp_path) / _KEY).iterdir())
    assert len(dirs) == 1
    assert all((d / "path").exists() and any(d.glob("v*")) for d in dirs)


async def test_orphan_version_dirs_are_swept_after_the_grace(tmp_path: Path) -> None:
    """review 10 —— 没有 ``path`` / 没有版本的目录(旧版本留下的)过了宽限期就删;新建的不动。"""
    _put(_agent(tmp_path) / "t.md", "t")
    await _observe(tmp_path)
    base = _versions_root(tmp_path) / _KEY
    old_empty, old_pathless, fresh = base / ("0" * 32), base / ("1" * 32), base / ("2" * 32)
    for d in (old_empty, old_pathless, fresh):
        d.mkdir()
    (old_empty / "path").write_text("x.md")
    (old_pathless / ("v00000001-" + "a" * 64)).write_text("v")
    for d in (old_empty, old_pathless):
        os.utime(d, (1_000_000_000, 1_000_000_000))

    await _observe(tmp_path)

    assert not old_empty.exists()
    assert not old_pathless.exists()
    assert fresh.exists()
    assert len(list(base.iterdir())) == 2  # t.md 的目录 + 刚建的那个


async def test_the_dir_cap_is_enforced_without_a_new_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review 10 —— 目录数上限每次观察都查,不只在写了新版本时。"""
    agent = _agent(tmp_path)
    for i in range(4):
        _put(agent / f"f{i}.md", f"{i}")
    await _observe(tmp_path)
    monkeypatch.setattr(versions_mod, "MAX_VERSION_DIRS_PER_AGENT", 2)

    report = await _observe(tmp_path)

    assert report.snapshotted == 0
    assert len(list((_versions_root(tmp_path) / _KEY).iterdir())) == 2


async def test_dirs_of_deleted_sources_go_before_stable_live_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review 9 —— 版本目录超了上限:先删源文件已不见的,不是先删一直没改的活文件。"""
    agent = _agent(tmp_path)
    _put(agent / "stable.md", "s")
    await _observe(tmp_path)
    gone = _put(agent / "gone.md", "g")
    await _observe(tmp_path)
    gone.unlink()
    await _observe(tmp_path)  # gone.md 记下「不见」(这一步刷新了它版本目录的 mtime)
    monkeypatch.setattr(versions_mod, "MAX_VERSION_DIRS_PER_AGENT", 2)
    _put(agent / "new.md", "n")
    await _observe(tmp_path)

    assert set(_history(tmp_path)) == {"stable.md", "new.md"}


async def test_version_dirs_per_agent_are_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """换着名字写空文件:字节上限管不住目录数,这道闸管。"""
    monkeypatch.setattr(versions_mod, "MAX_VERSION_DIRS_PER_AGENT", 5)
    agent = _agent(tmp_path)
    for i in range(12):
        for f in agent.glob("churn-*"):
            f.unlink()
        _put(agent / f"churn-{i}", "")
        await _observe(tmp_path)

    assert len(list((_versions_root(tmp_path) / _KEY).iterdir())) <= 5


async def test_an_unreadable_dir_does_not_start_the_missing_clock(tmp_path: Path) -> None:
    """``chmod 000`` 的目录里有什么不知道:这次遍历不完整,不给里面的文件记「不见」。"""
    sub = _agent(tmp_path) / "tpl"
    _put(sub / "t.md", "t")
    await _observe(tmp_path)
    sub.chmod(0)
    try:
        await _observe(tmp_path)
    finally:
        sub.chmod(0o700)
    assert list(_versions_root(tmp_path).rglob("missing")) == []


async def test_the_depth_limit_does_not_start_the_missing_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put(_agent(tmp_path) / "a" / "b" / "c.md", "c")
    await _observe(tmp_path)
    monkeypatch.setattr(versions_mod, "MAX_SEED_DEPTH", 2)
    await _observe(tmp_path)
    assert list(_versions_root(tmp_path).rglob("missing")) == []


async def test_a_deleted_file_keeps_its_newest_version_while_a_big_file_churns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review 11(probe_versions 的形状)—— 一次 run 删了 customers.md、同时大文件照常重写:
    这一次观察里 customers.md 最新那份还在(按 rank 删,大文件先让出自己的旧版本)。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 10_000)
    agent = _agent(tmp_path)
    customers = agent / "customers.md"
    for i in range(5):
        _put(customers, f"customer notes v{i}".ljust(200, "."))
        await _observe(tmp_path)
    big = agent / "data.xlsx"
    for r in range(8):
        _put(big, f"{r}".ljust(2_000, "x"))
        await _observe(tmp_path)
    customers.unlink()
    _put(big, "bad".ljust(2_000, "x"))

    await _observe(tmp_path)

    assert _agent_version_bytes(tmp_path) <= 10_000
    assert _history(tmp_path)["customers.md"][-1].startswith("customer notes v4")


async def test_the_index_keeps_its_good_version_through_a_bad_run_under_churn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review 11(probe_versions2 的形状)—— 大文件每轮重写把预算顶满,坏的一轮覆盖了索引:
    好的那几份(最新 3 份之内)都还在。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 10_000)
    agent = _agent(tmp_path)
    notes = agent / "AGENT_NOTES.md"
    for i in range(3):
        _put(notes, f"GOOD index v{i}".ljust(300, "."))
        await _observe(tmp_path)
    big = agent / "data.xlsx"
    for r in range(8):
        _put(big, f"{r}".ljust(4_000, "x"))
        await _observe(tmp_path)
    _put(notes, "BAD overwrite")
    _put(big, "bad".ljust(4_000, "x"))

    await _observe(tmp_path)

    kept = _history(tmp_path)["AGENT_NOTES.md"]
    assert kept[-1] == "BAD overwrite"
    assert [v[:12] for v in kept[-3:-1]] == ["GOOD index v", "GOOD index v"]
    assert kept[-2].startswith("GOOD index v2")


async def test_a_hostile_rename_of_the_index_does_not_evict_its_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review 11(exp_history_eviction 的形状)—— 一次 run 把索引改名成点开头的名字(遍历照样
    「完整」)再写一堆垃圾:这一次索引仍按活文件护着,最新几份都在。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 10_000)
    agent = _agent(tmp_path)
    notes = agent / "AGENT_NOTES.md"
    for i in range(5):
        _put(notes, f"good notes v{i}".ljust(100, "."))
        await _observe(tmp_path)
    static = agent / "static.bin"
    for i in range(2):
        _put(static, f"{i}".ljust(3_000, "s"))
        await _observe(tmp_path)
    notes.rename(agent / ".hidden")
    _put(agent / "junk.bin", "j" * 3_950)

    await _observe(tmp_path)

    assert _history(tmp_path)["AGENT_NOTES.md"][-1].startswith("good notes v4")


async def test_one_item_that_does_not_fit_does_not_block_later_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review 11 —— 放不下的那一份跳过,后面放得下的照写(之前整个停下,真改动跟着丢)。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 10_000)
    agent = _agent(tmp_path)
    a = _put(agent / "a.md", "a" * 6_000)
    os.utime(a, ns=(10**9, 10**9))
    x = _put(agent / "x.md", "good-0".ljust(1_000, "."))
    os.utime(x, ns=(2 * 10**9, 2 * 10**9))
    await _observe(tmp_path)
    now = time.time_ns()
    _put(x, "GOOD-1".ljust(1_000, "."))
    os.utime(x, ns=(now, now))
    b = _put(agent / "b.md", "b" * 4_000)  # 更新,排在前面,放不下
    os.utime(b, ns=(now + 10**9, now + 10**9))

    report = await _observe(tmp_path)

    assert report.over_budget is True
    assert [v[:6] for v in _history(tmp_path)["x.md"]] == ["good-0", "GOOD-1"]
    assert "b.md" not in _history(tmp_path)


async def test_another_agent_rewriting_one_file_never_touches_this_agents_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review 11(test_r11_isolation_probe 的加强版)—— A 反复重写一个文件,B 的版本一份不少。
    B 的旧版本 rank 比 A 的高:预算要是跨 agent 算,先删的就是 B 的。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 10_000)
    notes = _agent_dir(tmp_path, "b-agent") / "AGENT_NOTES.md"
    for i in range(6):
        _put(notes, f"good notes v{i}")
        os.utime(notes, ns=(i * 10**9 + 1, i * 10**9 + 1))
        await _observe_key(tmp_path, "b-agent")
    big = _agent_dir(tmp_path, "a-agent") / "big.bin"
    for r in range(8):
        _put(big, f"{r}".ljust(3_000, "x"))
        await _observe_key(tmp_path, "a-agent")

    assert _version_count(tmp_path, "b-agent") == 6


async def test_over_budget_is_logged_with_tenant_and_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """review 11 —— 运维要知道是谁:告警带租户与用户 id。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 1_000)
    _put(_agent(tmp_path) / "big.md", "b" * 5_000)
    caplog.set_level("WARNING", logger=versions_mod.__name__)

    await versions_mod._observe_logged(_store(tmp_path), _TENANT, _USER, _KEY)

    line = next(r.getMessage() for r in caplog.records if "over_budget" in r.getMessage())
    assert f"tenant_id={_TENANT}" in line and f"user_id={_USER}" in line


async def test_the_dir_cap_spares_fresh_empty_dirs_and_survives_a_racing_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review 11 —— 目录数封顶不删刚建、还没写进版本的目录(另一次观察正要写);删目录撞上
    ``ENOTEMPTY`` 只跳过那一个,整次观察照常结束。"""
    import errno

    agent = _agent(tmp_path)
    for i in range(3):
        _put(agent / f"f{i}.md", f"{i}")
    await _observe(tmp_path)
    fresh = _versions_root(tmp_path) / _KEY / ("f" * 32)
    fresh.mkdir()
    monkeypatch.setattr(versions_mod, "MAX_VERSION_DIRS_PER_AGENT", 3)

    await _observe(tmp_path)
    assert fresh.exists()  # 多出来的那个删的是别的,不是刚建的空目录

    def racing_rmtree(*args: Any, **kwargs: Any) -> None:
        raise OSError(errno.ENOTEMPTY, "Directory not empty")

    monkeypatch.setattr(versions_mod.shutil, "rmtree", racing_rmtree)
    _put(agent / "f9.md", "9")
    report = await _observe(tmp_path)  # 不抛
    assert report.snapshotted >= 1  # f9.md(还有上一次被封顶删掉、这次重新留的那个)


def test_the_budget_never_deletes_the_newest_version_of_a_live_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review 10 —— 预算被调低(或并发写入),活文件最新版本自己就超了:也不删,记 over_budget。"""
    agent = _agent(tmp_path)
    for name in ("a.md", "b.md"):
        _put(agent / name, name * 1_000)
    asyncio.run(_observe(tmp_path))
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 1_000)
    scan = versions_mod._Scan()
    fd = os.open(_versions_root(tmp_path) / _KEY, os.O_RDONLY | os.O_DIRECTORY)
    try:
        versions_mod._enforce_budget(fd, scan)
    finally:
        os.close(fd)

    assert scan.over_budget is True
    assert {k: len(v) for k, v in _history(tmp_path).items()} == {"a.md": 1, "b.md": 1}


# ---------------------------------------------------------------------------
# review 10 —— 读预算不被「只 touch」的文件饿死:索引第一个看,其余按 mtime 从新到旧
# ---------------------------------------------------------------------------


async def test_the_index_edit_is_versioned_although_touched_files_burn_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exp_empty_version_dirs 的形状:大文件每轮只 touch(读一遍才知道没变),索引照样留版本。"""
    monkeypatch.setattr(versions_mod, "MAX_OBSERVE_READ_BYTES", 2_000)
    agent = _agent(tmp_path)
    blobs = [_put(agent / name, name * 500) for name in ("0a", "0b")]  # 各 1000 字节
    notes = agent / versions_mod.AGENT_NOTES_FILE
    now = time.time_ns()
    for r in range(4):
        for blob in blobs:
            os.utime(blob, ns=(now + r * 10**9 + 5, now + r * 10**9 + 5))  # 比索引还新
        _put(notes, f"notes v{r}")
        os.utime(notes, ns=(now + r * 10**9, now + r * 10**9))
        await _observe(tmp_path)
        assert _history(tmp_path)[versions_mod.AGENT_NOTES_FILE][-1] == f"notes v{r}"


async def test_a_recent_edit_beats_older_touched_files_for_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """按 mtime 从新到旧读:刚改的文件先用预算,很早以前只被 touch 过的排在后面。"""
    monkeypatch.setattr(versions_mod, "MAX_OBSERVE_READ_BYTES", 2_000)
    agent = _agent(tmp_path)
    blobs = [_put(agent / name, name * 500) for name in ("0a", "0b")]
    tpl = _put(agent / "tpl" / "t.md", "v0")
    await _observe(tmp_path)  # 起点:三个都有一份
    for r in range(1, 3):
        for blob in blobs:
            os.utime(blob, ns=(r * 10**9, r * 10**9))  # 内容不变、mtime 变了,但是很早以前
        _put(tpl, f"v{r}")  # 真改动,mtime = 现在
        await _observe(tmp_path)
        assert _history(tmp_path)["tpl/t.md"][-1] == f"v{r}"


# ---------------------------------------------------------------------------
# review 11 —— 按 rank 删、每个文件公平:各条规则各钉一条
# ---------------------------------------------------------------------------


async def test_a_churning_big_file_gives_up_its_own_old_versions_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同 rank 才比大小:小文件的第 5 新与大文件的第 5 新相比,先删大的;小文件只让出比大文件
    rank 更高的那几份(不是「最早写下的先删」把小文件的历史整个冲掉)。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 10_000)
    agent = _agent(tmp_path)
    small = agent / "s.md"
    for i in range(6):
        _put(small, f"s{i}".ljust(100, "."))
        await _observe(tmp_path)
    big = agent / "big.bin"
    for r in range(10):
        _put(big, f"{r}".ljust(2_000, "x"))
        await _observe(tmp_path)
        assert _agent_version_bytes(tmp_path) <= 10_000

    assert len(_history(tmp_path)["s.md"]) == 5


async def test_a_live_file_keeps_two_versions_and_a_misfit_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """活文件最新 2 份受保护:新文件放进来要删到 1 份才放得下,就不放(记 over_budget)。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 10_000)
    agent = _agent(tmp_path)
    a = agent / "a.md"
    for i in range(3):
        _put(a, f"{i}".ljust(3_000, "a"))
        await _observe(tmp_path)
    _put(agent / "b.md", "b" * 4_500)

    report = await _observe(tmp_path)

    assert report.over_budget is True
    assert "b.md" not in _history(tmp_path)
    assert len(_history(tmp_path)["a.md"]) >= 2


async def test_the_live_index_keeps_three_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """活的索引最新 3 份受保护:新文件放进来要删到 2 份才放得下,就不放。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 10_000)
    agent = _agent(tmp_path)
    notes = agent / "AGENT_NOTES.md"
    for i in range(4):
        _put(notes, f"{i}".ljust(2_000, "n"))
        await _observe(tmp_path)
    _put(agent / "c.md", "c" * 4_500)

    await _observe(tmp_path)

    assert len(_history(tmp_path)["AGENT_NOTES.md"]) >= 3


async def test_the_longest_missing_file_loses_its_last_version_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """源文件已不见的最新那份最后才删,其中不见得最久的先删(不是先删大的)。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 10_000)
    agent = _agent(tmp_path)
    for name, size in (("m1.md", 1_000), ("m2.md", 2_000)):
        f = _put(agent / name, name[:2] * (size // 2))
        await _observe(tmp_path)
        f.unlink()
        await _observe(tmp_path)  # 记下「不见」
    base = _versions_root(tmp_path) / _KEY
    for d in base.iterdir():
        age = 7_200 if (d / "path").read_text() == "m1.md" else 3_600
        os.utime(d / "missing", (time.time() - age, time.time() - age))
    _put(agent / "live.bin", "l" * 7_500)

    await _observe(tmp_path)

    kept = _history(tmp_path)
    assert kept.get("m1.md", []) == []
    assert len(kept["m2.md"]) == 1
