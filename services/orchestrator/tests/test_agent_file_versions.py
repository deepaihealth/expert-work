"""B-236 第三层 —— ``/agent`` 文件的版本:运行结束时看一眼,改过的留一份。真落盘。"""

from __future__ import annotations

import asyncio
import os
import shutil
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
from orchestrator.tools.conversation_seed import MAX_SEED_DEPTH, MAX_SEED_ENTRIES
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from orchestrator.tools.workspace_paths import AGENT_NOTES_FILE

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
    """review 10/13 —— 每轮删掉旧文件、写新名字:总量每轮都在预算内。已不见的文件在 30 天宽限期内
    留着最新那份(受保护),装不下的新版本就不留(记 over_budget),不删旧的腾地方。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 10_000)
    agent = _agent(tmp_path)
    refused = False
    for r in range(10):
        for f in agent.glob("j*"):
            f.unlink()
        for i in range(2):
            _put(agent / f"j{r}-{i}", f"{r}".ljust(3_000, "x"))
        report = await _observe(tmp_path)
        refused = refused or report.over_budget_reasons == ("bytes",)
        assert _agent_version_bytes(tmp_path) <= 10_000
    current = _history(tmp_path)
    assert refused
    assert [len(current[f"j0-{i}"]) for i in range(2)] == [1, 1]  # 删掉的文件最新那份还在


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
    """review 10/14 —— 一份版本都没有的目录过了宽限期就删,新建的不动;有版本、只是没有 ``path``
    的不是孤儿(「不知道」,留着)。"""
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
    assert old_pathless.exists()
    assert fresh.exists()
    assert len(list(base.iterdir())) == 3  # t.md 的目录 + 没 path 的 + 刚建的


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
        versions_mod._enforce_budget(fd, scan, versions_mod._usage(fd, scan))
    finally:
        os.close(fd)

    assert scan.over_reasons == {"still_over"}
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
    notes = agent / AGENT_NOTES_FILE
    now = time.time_ns()
    for r in range(4):
        for blob in blobs:
            os.utime(blob, ns=(now + r * 10**9 + 5, now + r * 10**9 + 5))  # 比索引还新
        _put(notes, f"notes v{r}")
        os.utime(notes, ns=(now + r * 10**9, now + r * 10**9))
        await _observe(tmp_path)
        assert _history(tmp_path)[AGENT_NOTES_FILE][-1] == f"notes v{r}"


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


# ---------------------------------------------------------------------------
# review 12 —— 目录数上限只挡新的不删旧的、清除竞态、标记只看这次扫描、看到了就撤「不见」
# ---------------------------------------------------------------------------


def _deleted_marker(root: Path) -> Path:
    from orchestrator.tools.nas_workspace_store import workspace_deleted_marker

    return Path(workspace_deleted_marker(str(root), _TENANT, _USER))


async def test_empty_files_at_the_dir_cap_never_evict_the_index_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exp_protected_eviction dircap 的形状:空文件把版本目录数顶满(字节预算挡不住),索引的历史
    一份不少;放不下的新文件不留,记 over_budget。"""
    monkeypatch.setattr(versions_mod, "MAX_VERSION_DIRS_PER_AGENT", 10)
    agent = _agent(tmp_path)
    notes = agent / "AGENT_NOTES.md"
    for r in range(1, 6):
        _put(notes, f"GOOD notes v{r}")
        _put(agent / "prefs.md", f"pref v{r}")
        await _observe(tmp_path)
    _put(notes, "BAD poisoned notes")
    await _observe(tmp_path)
    for r in range(7, 10):
        for i in range(6):
            _put(agent / f"{99 - r:02d}_{i:03d}", "")  # 名字排在前面、一直留着
        report = await _observe(tmp_path)

    assert report.over_budget is True
    assert len(list((_versions_root(tmp_path) / _KEY).iterdir())) == 10
    kept = _history(tmp_path)
    assert kept["AGENT_NOTES.md"][:5] == [f"GOOD notes v{r}" for r in range(1, 6)]
    assert len(kept["prefs.md"]) == 5


async def test_a_file_that_fails_to_drop_does_not_fail_the_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删版本目录撞上另一次观察同时往里写(``ENOTEMPTY``):只跳过那一个,这次观察照常结束。"""
    import errno

    _put(_agent(tmp_path) / "t.md", "t")
    await _observe(tmp_path)
    orphan = _versions_root(tmp_path) / _KEY / ("0" * 32)
    orphan.mkdir()
    os.utime(orphan, (1_000_000_000, 1_000_000_000))

    def racing_rmtree(*args: Any, **kwargs: Any) -> None:
        raise OSError(errno.ENOTEMPTY, "Directory not empty")

    monkeypatch.setattr(shutil, "rmtree", racing_rmtree)
    _put(_agent(tmp_path) / "u.md", "u")

    report = await _observe(tmp_path)

    assert report.snapshotted == 1
    assert orphan.exists()


async def test_a_restored_file_is_live_even_when_the_walk_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """probe12b C2 的形状:文件删了(记「不见」)又恢复,同时有一个太深的目录让遍历不完整 ——
    看到了就是在:撤掉标记,它按活文件留最新 2 份。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 10_000)
    agent = _agent(tmp_path)
    f = agent / "f.md"
    for i in range(3):
        _put(f, f"F-v{i}".ljust(1_500, "f"))
        await _observe(tmp_path)
    f.unlink()
    await _observe(tmp_path)  # 记下「不见」
    deep = agent
    for i in range(MAX_SEED_DEPTH + 2):
        deep = deep / f"d{i}"
    _put(deep / "x.txt", "x")
    _put(f, "F-PREV-STATE".ljust(1_500, "f"))
    await _observe(tmp_path)
    _put(f, "F-BAD".ljust(1_500, "f"))
    await _observe(tmp_path)
    big = agent / "big.bin"
    for r in range(4):
        _put(big, f"{r}".ljust(3_900, "b"))
        await _observe(tmp_path)

    kept = [v[:12] for v in _history(tmp_path)["f.md"]]
    assert kept[-2:] == ["F-PREV-STATE", "F-BAD".ljust(12, "f")]


async def test_a_hidden_index_survives_junk_regardless_of_clock_skew(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """索引被藏起来、同一轮写垃圾:它的最新几份受保护(review 13 起索引不论在不在都留 3 份),与
    ``missing`` 标记的时间、NAS 服务端时钟比本机慢多少都无关。"""
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
    real_since = versions_mod._missing_since

    def server_clock_behind(file_dir: int) -> float | None:
        since = real_since(file_dir)
        return None if since is None else since - 3_600  # NAS 服务端时钟比本机慢一小时

    monkeypatch.setattr(versions_mod, "_missing_since", server_clock_behind)
    notes.rename(agent / ".hidden")
    _put(agent / "junk.bin", "j" * 3_950)

    await _observe(tmp_path)

    assert _history(tmp_path)["AGENT_NOTES.md"][-1].startswith("good notes v4")


async def test_a_purge_landing_before_housekeeping_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """观察已经过了开头那次软删标记检查,清除才落下:收尾前再看一次,什么都不建。"""
    _put(_agent(tmp_path) / "AGENT_NOTES.md", "a")
    real_order = versions_mod._observe_order

    def purge_lands(found: Any) -> list[str]:
        marker = _deleted_marker(tmp_path)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return real_order(found)

    monkeypatch.setattr(versions_mod, "_observe_order", purge_lands)
    writes: list[int] = []
    real_write = versions_mod._write_pending

    def spy_write(*args: Any) -> None:
        writes.append(len(args[1]))
        real_write(*args)

    monkeypatch.setattr(versions_mod, "_write_pending", spy_write)

    await _observe(tmp_path)

    assert writes == []  # 一份都没写过(不是写了再删)
    assert not _versions_root(tmp_path).exists()


async def test_a_purge_landing_during_the_writes_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清除(标记 + 第二遍删除)落在写的过程中:最后一次写之后再看一次,这个用户的版本整个删掉。"""
    _put(_agent(tmp_path) / "AGENT_NOTES.md", "a")
    real_write = versions_mod._write_pending

    def purge_lands(*args: Any) -> None:
        real_write(*args)
        marker = _deleted_marker(tmp_path)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()

    monkeypatch.setattr(versions_mod, "_write_pending", purge_lands)

    await _observe(tmp_path)

    assert not _versions_root(tmp_path).exists()


async def test_observations_of_one_agent_take_turns_in_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一个(租户, 用户, agent)的两次观察在本进程里不重叠(各自按旧现状算预算会冲过预算)。"""
    import threading

    _put(_agent(tmp_path) / "t.md", "t")
    store = _store(tmp_path)
    active = 0
    peak = 0
    guard = threading.Lock()
    real = versions_mod.observe_sync

    def counting(*args: Any) -> Any:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        try:
            return real(*args)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(versions_mod, "observe_sync", counting)
    await asyncio.gather(
        *(store.observe(tenant_id=_TENANT, user_id=_USER, agent_key=_KEY) for _ in range(3))
    )

    assert peak == 1


# ---------------------------------------------------------------------------
# review 13 —— 受保护的版本只在自己到期时删;满了就不留新的。按目录判在不在、索引单独查、名字数封顶
# ---------------------------------------------------------------------------


def _fill(agent: Path, tag: str, sizes: list[int]) -> None:
    for j, size in enumerate(sizes):
        _put(agent / f"!{tag}_{j}", tag.ljust(size, "j"))  # 名字排在最前面


async def test_a_restored_index_behind_a_truncated_root_keeps_its_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1(r1 的形状):索引藏一轮(记了「不见」)再放回,前面塞 505 个目录让遍历在条目上限停下、
    够不到它,再用垃圾把预算顶满 —— 索引单独查过,算在,最新 3 份都在。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 10_000)
    agent = _agent(tmp_path)
    notes = agent / "AGENT_NOTES.md"
    for i in range(5):
        _put(notes, f"GOOD index v{i}".ljust(500, "."))
        await _observe(tmp_path)
    notes.rename(agent / ".bak")
    await _observe(tmp_path)
    (agent / ".bak").rename(notes)
    for i in range(505):
        (agent / f"0{i:04d}").mkdir()
    for r in range(4):
        _fill(agent, f"j{r}", [2_500])
        await _observe(tmp_path)

    kept = _history(tmp_path)["AGENT_NOTES.md"]
    assert len(kept) >= 3
    assert [v[:12] for v in kept[-3:]] == ["GOOD index v"] * 3


async def test_a_file_in_a_dir_that_was_not_fully_listed_is_budgeted_as_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1:没列全的目录里没看到的文件「不知道」—— 按在算(留 2 份);旧的「不见」标记撤掉
    (review 14:之后要重新确认不见、重新算 30 天)。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 10_000)
    agent = _agent(tmp_path)
    zz = agent / "zz.md"
    for i in range(3):
        _put(zz, f"zz v{i}".ljust(1_000, "."))
        await _observe(tmp_path)
    zz.rename(agent / ".zz")
    await _observe(tmp_path)  # 记下「不见」
    (agent / ".zz").rename(zz)
    for i in range(505):
        (agent / f"0{i:04d}").mkdir()  # 遍历在条目上限停下,够不到 zz.md
    for r in range(4):
        _fill(agent, f"j{r}", [2_500])
        await _observe(tmp_path)

    assert len(_history(tmp_path)["zz.md"]) == 2
    assert list(_versions_root(tmp_path).rglob("missing")) == []  # 「不知道」撤掉了旧标记


async def test_a_missing_file_keeps_its_protected_versions_through_the_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2(r2 的形状):索引改名藏起来(已不见、在宽限期内),别的文件把预算顶满:它最新 3 份一份
    不少,普通的已不见文件留最新 1 份;放不下的新版本不留(bytes)。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 10_000)
    agent = _agent(tmp_path)
    notes = agent / "AGENT_NOTES.md"
    for i in range(5):
        _put(notes, f"GOOD index v{i}".ljust(500, "."))
        await _observe(tmp_path)
    gone = _put(agent / "gone.md", "g".ljust(1_000, "g"))
    await _observe(tmp_path)
    notes.rename(agent / ".bak")
    gone.unlink()
    reports = []
    for r in range(6):
        _fill(agent, f"j{r}", [2_500])
        reports.append(await _observe(tmp_path))

    kept = _history(tmp_path)
    assert [v[:13] for v in kept["AGENT_NOTES.md"][-3:]] == [f"GOOD index v{i}" for i in (2, 3, 4)]
    assert len(kept["gone.md"]) == 1
    assert any(r.over_budget_reasons == ("bytes",) for r in reports)
    assert _agent_version_bytes(tmp_path) <= 10_000


async def test_a_hidden_index_keeps_exactly_its_newest_three_in_later_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L5:索引不见之后的几次观察里(宽限期内)它留最新 3 份 —— 不是 1 份,也不是全部。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 10_000)
    agent = _agent(tmp_path)
    notes = agent / "AGENT_NOTES.md"
    for i in range(5):
        _put(notes, f"GOOD index v{i}".ljust(1_000, "."))
        await _observe(tmp_path)
    notes.rename(agent / ".bak")
    await _observe(tmp_path)
    for r, size in enumerate((3_000, 3_000, 1_000)):
        _fill(agent, f"j{r}", [size])
        await _observe(tmp_path)
    _fill(agent, "j9", [500])
    last = await _observe(tmp_path)

    assert [v[:13] for v in _history(tmp_path)["AGENT_NOTES.md"]] == [
        f"GOOD index v{i}" for i in (2, 3, 4)
    ]
    assert last.over_budget_reasons == ("bytes",)


async def test_the_index_gets_its_dir_when_the_cap_is_full_of_deleted_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3:目录数被已不见文件的版本目录顶满(要等 30 天才腾出来):索引仍建得出版本目录;别的新文件
    不留(dir_cap)。"""
    monkeypatch.setattr(versions_mod, "MAX_VERSION_DIRS_PER_AGENT", 5)
    agent = _agent(tmp_path)
    for i in range(5):
        _put(agent / "z" / f"{i}", "")
    await _observe(tmp_path)
    for f in (agent / "z").iterdir():
        f.unlink()
    (agent / "z").rmdir()
    await _observe(tmp_path)
    _put(agent / "AGENT_NOTES.md", "GOOD index")
    _put(agent / "other.md", "o")

    report = await _observe(tmp_path)

    assert _history(tmp_path)["AGENT_NOTES.md"] == ["GOOD index"]
    assert "other.md" not in _history(tmp_path)
    assert report.over_budget_reasons == ("dir_cap",)


async def test_the_walk_reads_a_bounded_number_of_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4:点开头 / 非 UTF-8 的名字也算进名字数上限,不先把整个目录读进内存;到顶的目录没列全,
    里面没看到的文件算「不知道」,不记「不见」。"""
    monkeypatch.setattr(versions_mod, "MAX_OBSERVE_VISITS", 50)
    agent = _agent(tmp_path)
    _put(agent / "sub" / "x.md", "x")
    await _observe(tmp_path)
    fd = os.open(agent, os.O_RDONLY)
    try:
        for i in range(2_000):
            os.close(os.open(f".pad{i:05d}", os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd))
    finally:
        os.close(fd)
    read: list[int] = []
    real_listdir, real_scandir = os.listdir, os.scandir

    def counting_listdir(path: Any = ".") -> list[str]:
        names = real_listdir(path)
        if isinstance(path, int):
            read.append(len(names))
        return names

    class _CountingScandir:
        def __init__(self, path: Any) -> None:
            self._it = real_scandir(path)
            self._count = isinstance(path, int)

        def __enter__(self) -> _CountingScandir:
            return self

        def __exit__(self, *exc: object) -> None:
            self._it.close()

        def __iter__(self) -> _CountingScandir:
            return self

        def __next__(self) -> os.DirEntry[str]:
            entry = next(self._it)
            if self._count:
                read.append(1)
            return entry

    monkeypatch.setattr(os, "listdir", counting_listdir)
    monkeypatch.setattr(os, "scandir", _CountingScandir)

    await _observe(tmp_path)

    monkeypatch.setattr(os, "listdir", real_listdir)
    monkeypatch.setattr(os, "scandir", real_scandir)
    assert sum(read) < 200  # 版本目录那边几个名字 + 遍历最多 51 个
    assert list(_versions_root(tmp_path).rglob("missing")) == []


async def test_deleted_files_age_out_although_another_subtree_is_too_deep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L1:别的子树太深(遍历不完整)不再挡住列全了的目录里删掉的文件按 30 天到期。"""
    agent = _agent(tmp_path)
    gone = _put(agent / "gone.md", "g")
    await _observe(tmp_path)
    deep = agent
    for i in range(MAX_SEED_DEPTH + 2):
        deep = deep / f"d{i}"
    _put(deep / "x.txt", "x")
    gone.unlink()
    await _observe(tmp_path)
    markers = list(_versions_root(tmp_path).rglob("missing"))
    assert len(markers) == 1
    os.utime(markers[0], (1_000_000_000, 1_000_000_000))

    report = await _observe(tmp_path)

    assert report.dropped == 1
    assert "gone.md" not in _history(tmp_path)


async def test_the_index_is_observed_even_behind_the_entry_cap(tmp_path: Path) -> None:
    """L8:根目录里 505 个名字排在索引前面、遍历在条目上限停下 —— 索引单独查,改动照样留版本。"""
    agent = _agent(tmp_path)
    for i in range(505):
        (agent / f"0{i:04d}").mkdir(parents=True)
    _put(agent / "AGENT_NOTES.md", "GOOD index")

    await _observe(tmp_path)

    assert _history(tmp_path)["AGENT_NOTES.md"] == ["GOOD index"]


async def test_a_failing_housekeeping_still_runs_the_purge_post_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L2:收尾写了一部分之后抛异常、清除标记已落下 —— 写下的照样删掉。"""
    _put(_agent(tmp_path) / "AGENT_NOTES.md", "a")

    def purge_lands_then_fails(*args: Any) -> None:
        marker = _deleted_marker(tmp_path)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        raise OSError(5, "EIO")

    monkeypatch.setattr(versions_mod, "_enforce_budget", purge_lands_then_fails)

    with pytest.raises(OSError):
        await _observe(tmp_path)

    assert not _versions_root(tmp_path).exists()


async def test_a_cancelled_observation_keeps_the_lock_until_its_thread_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L4:观察的协程被取消,线程还在跑:锁要等线程跑完才放,下一次观察不和它重叠。"""
    import threading

    _put(_agent(tmp_path) / "t.md", "t")
    store = _store(tmp_path)
    active = 0
    peak = 0
    guard = threading.Lock()
    real = versions_mod.observe_sync

    def slow(*args: Any) -> Any:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.2)
        try:
            return real(*args)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(versions_mod, "observe_sync", slow)
    first = asyncio.create_task(store.observe(tenant_id=_TENANT, user_id=_USER, agent_key=_KEY))
    await asyncio.sleep(0.05)
    first.cancel()
    await store.observe(tenant_id=_TENANT, user_id=_USER, agent_key=_KEY)

    assert peak == 1
    with pytest.raises(asyncio.CancelledError):
        await first


# ---------------------------------------------------------------------------
# review 14 —— 「不知道」撤标记、``path`` 读错误 / 坏了不当孤儿且会修、遍历上限的边界
# ---------------------------------------------------------------------------


def _age_markers(root: Path) -> None:
    for marker in _versions_root(root).rglob("missing"):
        os.utime(marker, (1_000_000_000, 1_000_000_000))


def _dot_flood(agent: Path, n: int) -> None:
    fd = os.open(agent, os.O_RDONLY)
    try:
        for i in range(n):
            os.close(os.open(f".x{i:05d}", os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd))
    finally:
        os.close(fd)


async def test_a_stale_marker_does_not_survive_an_unknown_stretch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """r1 的形状:确认不见(T0 记标记)→ 放回 + 让遍历列不全(一个月都「不知道」)→ 藏一次:
    「不知道」那段撤掉了标记,这次只是重新开始计时,不会一下子把整个目录删掉。"""
    monkeypatch.setattr(versions_mod, "MAX_OBSERVE_VISITS", 50)
    agent = _agent(tmp_path)
    f = agent / "notes" / "f.md"
    for i in range(3):
        _put(f, f"good-v{i}")
        await _observe(tmp_path)
    stash = agent / ".stash"
    stash.mkdir()
    f.rename(stash / "f.md")
    await _observe(tmp_path)  # T0:确认不见
    (stash / "f.md").rename(f)
    _dot_flood(agent, 60)  # 根目录列不全:f.md「不知道」
    await _observe(tmp_path)
    _age_markers(tmp_path)  # 过了一个月
    for i in range(60):
        (agent / f".x{i:05d}").unlink()
    f.rename(stash / "f.md")

    report = await _observe(tmp_path)

    assert report.dropped == 0
    assert _history(tmp_path)["notes/f.md"] == [f"good-v{i}" for i in range(3)]


async def test_a_transient_error_reading_path_drops_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3:读 ``path`` 时一次 ESTALE / EIO:这次跳过那个目录 —— 不当孤儿删(哪怕它建了很久),也不动
    它的「不见」标记(不当成「不知道」去重新计时)。"""
    import errno

    f = _put(_agent(tmp_path) / "notes" / "f.md", "v1")
    gone = _put(_agent(tmp_path) / "gone.md", "g")
    await _observe(tmp_path)
    _put(f, "v2")
    gone.unlink()
    await _observe(tmp_path)  # gone.md 记下「不见」
    (marker,) = _versions_root(tmp_path).rglob("missing")
    os.utime(marker, (2_000_000_000, 2_000_000_000))
    for d in (_versions_root(tmp_path) / _KEY).iterdir():
        os.utime(d, (1_000_000_000, 1_000_000_000))
    real_open = os.open

    def flaky(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if path == "path" and dir_fd is not None:
            raise OSError(errno.ESTALE, "Stale file handle")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", flaky)
    report = await _observe(tmp_path)
    monkeypatch.setattr(os, "open", real_open)

    assert report.dropped == 0
    assert _history(tmp_path)["notes/f.md"] == ["v1", "v2"]
    assert marker.stat().st_mtime == 2_000_000_000  # 没被撤掉重记


async def test_a_torn_path_is_kept_and_repaired_on_the_next_write(tmp_path: Path) -> None:
    """P2:``path`` 是空的(写了一半):有版本的目录不当孤儿、不判不见;下一次写这个文件时原子地修好。"""
    f = _put(_agent(tmp_path) / "notes" / "f.md", "v1")
    await _observe(tmp_path)
    (file_dir,) = (_versions_root(tmp_path) / _KEY).iterdir()
    (file_dir / "path").write_text("")
    os.utime(file_dir, (1_000_000_000, 1_000_000_000))
    await _observe(tmp_path)
    _age_markers(tmp_path)
    first = await _observe(tmp_path)
    _put(f, "v2")

    second = await _observe(tmp_path)

    assert first.dropped == second.dropped == 0
    assert (file_dir / "path").read_text() == "notes/f.md"
    assert _history(tmp_path)["notes/f.md"] == ["v1", "v2"]


async def test_an_entry_cap_in_a_subdir_leaves_later_root_files_unknown(tmp_path: Path) -> None:
    """E7:条目上限在一个子目录里到顶,根目录后面的文件没处理到 —— 根目录没列全,它们「不知道」,
    标记过期也不删。"""
    agent = _agent(tmp_path)
    _put(agent / "b.md", "b")
    await _observe(tmp_path)
    for i in range(MAX_SEED_ENTRIES + 5):
        _put(agent / "a" / f"n{i:03d}", "")
    await _observe(tmp_path)
    _age_markers(tmp_path)

    report = await _observe(tmp_path)

    assert report.dropped == 0
    assert _history(tmp_path)["b.md"] == ["b"]


async def test_an_entry_cap_at_the_root_leaves_later_files_unknown(tmp_path: Path) -> None:
    """E8:根目录里 505 个名字排在前面、条目上限到顶:后面的文件「不知道」,标记过期也不删。"""
    agent = _agent(tmp_path)
    _put(agent / "zz.md", "z")
    await _observe(tmp_path)
    for i in range(MAX_SEED_ENTRIES + 5):
        (agent / f"0{i:04d}").mkdir()
    await _observe(tmp_path)
    _age_markers(tmp_path)

    report = await _observe(tmp_path)

    assert report.dropped == 0
    assert _history(tmp_path)["zz.md"] == ["z"]


async def test_a_dir_cut_by_the_name_cap_is_not_fully_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E9b:名字数上限在读 ``b/`` 的第一个名字时到顶 —— ``b/`` 没列全,``b/x.md`` 不判不见。"""
    agent = _agent(tmp_path)
    _put(agent / "b" / "x.md", "x")
    await _observe(tmp_path)
    for i in range(48):
        _put(agent / "a" / f"n{i:02d}", "n")
    monkeypatch.setattr(versions_mod, "MAX_OBSERVE_VISITS", 50)  # 根 2 + a 48 = 50;b 的第一个 51
    await _observe(tmp_path)
    _age_markers(tmp_path)

    report = await _observe(tmp_path)

    assert report.dropped == 0
    assert _history(tmp_path)["b/x.md"] == ["x"]


async def test_names_read_before_the_name_cap_are_still_processed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """名字数到顶时,已经读到的名字照常处理(只有那个目录算没列全),并报 ``walk_incomplete``。"""
    agent = _agent(tmp_path)
    for i in range(10):
        _put(agent / f"f{i}.md", f"{i}")
    monkeypatch.setattr(versions_mod, "MAX_OBSERVE_VISITS", 5)

    report = await _observe(tmp_path)

    assert report.snapshotted == 5
    assert report.walk_incomplete is True


async def test_a_name_gone_between_readdir_and_lstat_leaves_its_dir_unlisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4:目录里读到的名字到 ``lstat`` 时没了(正被来回改名):这一层算没列全,下面的文件不判不见、
    仍按在算(留 2 份)。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 2_600)
    agent = _agent(tmp_path)
    f = agent / "notes" / "f.md"
    _put(f, "A" * 1_000)
    await _observe(tmp_path)
    _put(f, "B" * 1_000)
    await _observe(tmp_path)
    _put(agent / "big.bin", "Z" * 500)
    real_lstat = os.lstat

    def racy(path: Any, *, dir_fd: int | None = None, **kw: Any) -> os.stat_result:
        if path == "notes" and dir_fd is not None:
            raise FileNotFoundError(path)
        return real_lstat(path, dir_fd=dir_fd, **kw)

    monkeypatch.setattr(os, "lstat", racy)
    report = await _observe(tmp_path)
    monkeypatch.setattr(os, "lstat", real_lstat)

    assert report.walk_incomplete is True
    assert _history(tmp_path)["notes/f.md"] == ["A" * 1_000, "B" * 1_000]
    assert list(_versions_root(tmp_path).rglob("missing")) == []


async def test_files_under_a_deleted_dir_age_out(tmp_path: Path) -> None:
    """M3:整个子目录删掉 —— 那一层列全了、子目录不在:下面的文件确认不见,满 30 天删掉。"""
    import shutil as _shutil

    agent = _agent(tmp_path)
    _put(agent / "z" / "a.md", "a")
    _put(agent / "keep.md", "k")
    await _observe(tmp_path)
    _shutil.rmtree(agent / "z")
    await _observe(tmp_path)
    _age_markers(tmp_path)

    report = await _observe(tmp_path)

    assert report.dropped == 1
    assert set(_history(tmp_path)) == {"keep.md"}


async def test_a_missing_file_keeps_exactly_one_and_a_live_file_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4 / M24:预算吃紧时,已不见的文件(宽限期内)正好留 1 份,在的文件正好留 2 份。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 10_000)
    agent = _agent(tmp_path)
    for i in range(3):
        _put(agent / "gone.md", f"g{i}".ljust(1_000, "g"))
        _put(agent / "live.md", f"l{i}".ljust(1_000, "l"))
        await _observe(tmp_path)
        await asyncio.sleep(0.01)
    (agent / "gone.md").unlink()
    _fill(agent, "j0", [3_000])
    await _observe(tmp_path)
    _fill(agent, "j1", [3_500])
    await _observe(tmp_path)

    kept = _history(tmp_path)
    assert [v[:2] for v in kept["gone.md"]] == ["g2"]
    assert [v[:2] for v in kept["live.md"]] == ["l1", "l2"]


async def test_a_hidden_index_behind_a_truncated_root_is_known_missing(
    tmp_path: Path,
) -> None:
    """M2:索引真不在了(单独查过),根目录又被截断 —— 照样确认不见、记标记(30 天后到期),
    留最新 3 份。"""
    agent = _agent(tmp_path)
    notes = agent / "AGENT_NOTES.md"
    for i in range(4):
        _put(notes, f"GOOD index v{i}")
        await _observe(tmp_path)
    notes.rename(agent / ".bak")
    for i in range(MAX_SEED_ENTRIES + 5):
        (agent / f"0{i:04d}").mkdir()

    report = await _observe(tmp_path)

    assert report.walk_incomplete is True
    assert len(list(_versions_root(tmp_path).rglob("missing"))) == 1
    assert len(_history(tmp_path)["AGENT_NOTES.md"]) == 4


async def test_both_limits_are_reported_when_both_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """字节预算与目录数上限同时放不下一份新版本:两个原因都记。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 1_000)
    monkeypatch.setattr(versions_mod, "MAX_VERSION_DIRS_PER_AGENT", 1)
    agent = _agent(tmp_path)
    _put(agent / "a.md", "a")
    await _observe(tmp_path)
    _put(agent / "b.md", "b" * 2_000)

    report = await _observe(tmp_path)

    assert report.over_budget_reasons == ("bytes", "dir_cap")


async def test_a_double_cancel_still_waits_and_the_thread_error_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """被取消两次也等线程跑完才放锁;线程的异常被取走、只记类型。"""
    import threading

    store = _store(tmp_path)
    active = 0
    peak = 0
    guard = threading.Lock()

    def slow_then_fail(*args: Any) -> Any:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.3)
        with guard:
            active -= 1
        raise OSError(5, "EIO")

    monkeypatch.setattr(versions_mod, "observe_sync", slow_then_fail)
    caplog.set_level("WARNING", logger=versions_mod.__name__)
    first = asyncio.create_task(store.observe(tenant_id=_TENANT, user_id=_USER, agent_key=_KEY))
    await asyncio.sleep(0.05)
    first.cancel()
    await asyncio.sleep(0.05)
    first.cancel()
    with pytest.raises(OSError):
        await store.observe(tenant_id=_TENANT, user_id=_USER, agent_key=_KEY)

    assert peak == 1
    with pytest.raises(asyncio.CancelledError):
        await first
    assert any(
        "observe_failed" in r.getMessage() and "OSError" in r.getMessage() for r in caplog.records
    )


async def test_a_dir_opened_after_the_name_cap_is_not_fully_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """名字数在读 ``a/`` 时就到顶了,之后才打开的 ``b/`` 一个名字都没读 —— 它没列全,``b/x.md``
    不判不见。"""
    agent = _agent(tmp_path)
    _put(agent / "b" / "x.md", "x")
    await _observe(tmp_path)
    for i in range(49):
        _put(agent / "a" / f"n{i:02d}", "n")
    monkeypatch.setattr(versions_mod, "MAX_OBSERVE_VISITS", 50)  # 根 2 + a 的第 49 个 = 51
    await _observe(tmp_path)
    _age_markers(tmp_path)

    report = await _observe(tmp_path)

    assert report.dropped == 0
    assert _history(tmp_path)["b/x.md"] == ["x"]
