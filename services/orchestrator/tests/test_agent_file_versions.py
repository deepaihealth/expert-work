"""B-236 第三层 —— ``/agent`` 文件的版本:运行结束时看一眼,改过的留一份。真落盘。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import shutil
import time
from collections.abc import Iterator
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
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore, _openat_dir
from orchestrator.tools.sandbox import WorkspacePermissionError
from orchestrator.tools.workspace_paths import AGENT_NOTES_FILE

_TENANT, _USER = uuid4(), uuid4()
_KEY = "plan-aaaaaaaa"
_C1 = UUID("11111111-1111-1111-1111-111111111111")


def _agent(root: Path) -> Path:
    return root / str(_TENANT) / str(_USER) / "agents" / _KEY


def _versions_root(root: Path) -> Path:
    return root / str(_TENANT) / ".agent-file-versions" / str(_USER)


_last_put_ns = 0


def _put(path: Path, text: str) -> Path:
    """写文件,并给一个**严格递增**的 mtime(≥ 现在,比上一次至少晚 1 ms)。review 17 —— 同样大小的
    连续重写在 mtime 粒度粗的文件系统上(容器的 overlay 之类)可能拿到同一个 mtime,被看成「没变」。"""
    global _last_put_ns
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _last_put_ns = max(time.time_ns(), _last_put_ns + 1_000_000)
    os.utime(path, ns=(_last_put_ns, _last_put_ns))
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


def _age_marker(marker: Path) -> None:
    """一个月前第一次确认不见、之后每周都有观察再确认过(review 16:内容 = 第一次的时间,mtime =
    最近一次确认)。"""
    now = time.time()
    marker.write_text(f"{now - 31 * 86_400:.0f}")
    os.utime(marker, (now, now))


def _put_v(path: Path, text: str, i: int) -> Path:
    """第 ``i`` 次重写:显式给一个不同的 mtime(review 17:mtime 粒度粗的文件系统上,同样大小的连续
    重写可能被看成「没变」)。"""
    _put(path, text)
    os.utime(path, ns=(1_700_000_000_000_000_000 + i * 10**9,) * 2)
    return path


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
    _age_marker(markers[0])

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


async def test_an_index_hidden_in_the_same_run_as_junk_keeps_its_newest_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """索引被藏起来、同一轮写垃圾:它的最新几份受保护(review 13 起索引不论在不在都留 3 份)。"""
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
    _age_marker(markers[0])

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
        _age_marker(marker)


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
        _put_v(f, f"good-v{i}", i)
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
        _put_v(agent / "gone.md", f"g{i}".ljust(1_000, "g"), i)
        _put_v(agent / "live.md", f"l{i}".ljust(1_000, "l"), i)
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
        _put_v(notes, f"GOOD index v{i}", i)
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


# ---------------------------------------------------------------------------
# review 15 —— 观察失败也撤旧标记、读不了 ≠ 不见、``.tmp-*`` 清理、账先记保守的、坏 ``path`` 就地修
# ---------------------------------------------------------------------------

_SAME_UID_PERMS = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root 绕过权限检查"
)


def _markers(root: Path) -> list[Path]:
    return list(_versions_root(root).rglob("missing"))


@contextlib.contextmanager
def _mode(path: Path, mode: int) -> Iterator[None]:
    os.chmod(path, mode)
    try:
        yield
    finally:
        os.chmod(path, 0o700)


async def _confirm_missing_then_restore(root: Path, f: Path) -> None:
    saved = f.read_bytes()
    f.unlink()
    await _observe(root)  # T0:确认不见,记标记
    assert len(_markers(root)) == 1
    f.write_bytes(saved)


@_SAME_UID_PERMS
async def test_an_unsearchable_subdir_neither_fails_the_observation_nor_hides_the_index(
    tmp_path: Path,
) -> None:
    """``tpl/`` 可读不可进(0644):``lstat`` 在里面报 EACCES —— 那一层没列全,观察照常完成,
    索引照常留版本,根目录的旧标记照常撤掉。"""
    agent = _agent(tmp_path)
    _put(agent / "tpl" / "t.md", "t")
    prefs = _put(agent / "prefs.md", "p1")
    notes = _put(agent / "AGENT_NOTES.md", "good index")
    await _observe(tmp_path)
    await _confirm_missing_then_restore(tmp_path, prefs)
    _put(notes, "edited index")
    with _mode(agent / "tpl", 0o644):
        report = await _observe(tmp_path)

    assert report.walk_incomplete is True
    assert _history(tmp_path)["AGENT_NOTES.md"] == ["good index", "edited index"]
    assert _markers(tmp_path) == []


@_SAME_UID_PERMS
async def test_an_unsearchable_root_clears_markers_and_judges_nothing_missing(
    tmp_path: Path,
) -> None:
    """根目录 0644:索引查不了(「不知道」,不是「不见」),别的文件也都「不知道」—— 旧标记撤掉,
    不新记任何标记。根目录进不去时索引也没法留版本(spec 里记着)。"""
    agent = _agent(tmp_path)
    prefs = _put(agent / "prefs.md", "p1")
    _put(agent / "AGENT_NOTES.md", "good index")
    await _observe(tmp_path)
    await _confirm_missing_then_restore(tmp_path, prefs)
    with _mode(agent, 0o644):
        report = await _observe(tmp_path)

    assert report.walk_incomplete is True
    assert _markers(tmp_path) == []


@_SAME_UID_PERMS
async def test_an_index_only_unsearchable_root_does_not_mark_the_index_missing(
    tmp_path: Path,
) -> None:
    """r6:根目录 0644、里面只有索引和点开头的名字 —— 根目录能「列全」,但索引查不了:索引是
    「不知道」,不能记「不见」。"""
    agent = _agent(tmp_path)
    _put(agent / "AGENT_NOTES.md", "i")
    (agent / ".cache").mkdir()
    await _observe(tmp_path)
    with _mode(agent, 0o644):
        await _observe(tmp_path)

    assert _markers(tmp_path) == []


@_SAME_UID_PERMS
async def test_an_unopenable_root_still_clears_old_markers(tmp_path: Path) -> None:
    """根目录 000:打不开、观察抛异常 —— 抛之前照样用空扫描收一次尾,旧标记撤掉。"""
    agent = _agent(tmp_path)
    prefs = _put(agent / "prefs.md", "p1")
    await _observe(tmp_path)
    await _confirm_missing_then_restore(tmp_path, prefs)
    with _mode(agent, 0o000), pytest.raises(WorkspacePermissionError):
        await _observe(tmp_path)

    assert _markers(tmp_path) == []
    assert _history(tmp_path)["prefs.md"] == ["p1"]


async def test_a_failing_walk_still_clears_old_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """遍历自己抛了异常(任何原因):抛之前用空扫描收一次尾 —— 只撤标记,不写、不删版本。"""
    agent = _agent(tmp_path)
    prefs = _put(agent / "prefs.md", "p1")
    await _observe(tmp_path)
    await _confirm_missing_then_restore(tmp_path, prefs)

    def boom(*args: Any) -> Any:
        raise OSError(5, "EIO")

    monkeypatch.setattr(versions_mod, "_files", boom)
    with pytest.raises(OSError):
        await _observe(tmp_path)

    assert _markers(tmp_path) == []
    assert _history(tmp_path)["prefs.md"] == ["p1"]


@_SAME_UID_PERMS
async def test_a_month_of_unsearchable_subdir_does_not_let_a_stale_marker_expire(
    tmp_path: Path,
) -> None:
    """r4 端到端:T0 确认不见 → 放回 + ``tpl/`` 0644 一个月 → 恢复权限、藏一次:旧标记早被撤掉,
    这次只是重新开始计时,整个历史都在。"""
    agent = _agent(tmp_path)
    _put(agent / "tpl" / "t.md", "t")
    prefs = agent / "prefs.md"
    _put(prefs, "good prefs v1")
    await _observe(tmp_path)
    _put(prefs, "good prefs v2 (longer)")
    await _observe(tmp_path)
    await _confirm_missing_then_restore(tmp_path, prefs)
    with _mode(agent / "tpl", 0o644):
        await _observe(tmp_path)
    _age_markers(tmp_path)
    prefs.unlink()

    report = await _observe(tmp_path)

    assert report.dropped == 0
    assert _history(tmp_path)["prefs.md"] == ["good prefs v1", "good prefs v2 (longer)"]


async def test_leftover_tmp_files_are_aged_by_ctime_not_by_a_copied_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review 16 —— 临时文件带着抄来的源文件 mtime(载荷定的:1970 或 2100):按 ctime 算,刚建的不删
    (另一个副本正在写),满 1 小时的删(哪怕 mtime 在将来)。"""
    _put(_agent(tmp_path) / "f.md", "f")
    await _observe(tmp_path)
    (file_dir,) = (_versions_root(tmp_path) / _KEY).iterdir()
    past, future = file_dir / ".tmp-past", file_dir / ".tmp-future"
    past.write_text("x" * 100)
    future.write_text("y")
    os.utime(past, (1_000_000_000, 1_000_000_000))
    os.utime(future, (4_102_444_800, 4_102_444_800))

    await _observe(tmp_path)
    assert past.exists() and future.exists()  # ctime 是刚才:正在写的不删

    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 2 * 3_600)
    await _observe(tmp_path)

    assert not past.exists() and not future.exists()


async def test_a_failing_prune_keeps_the_books_conservative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """p1:写下一份之后清理出错 —— 账已经按「多了这一份」记上,后面放不下的照样拦(``bytes``),
    这一份也不重复算成 skipped。"""
    import errno

    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 4_000)
    agent = _agent(tmp_path)
    for name, ch, mt in (("a.md", "a", 100), ("b.md", "b", 99)):
        f = _put(agent / name, ch * 1_000)
        os.utime(f, (mt, mt))
    await _observe(tmp_path)
    for name, ch, mt in (("a.md", "A", 300), ("b.md", "B", 299), ("c.md", "c", 298)):
        f = _put(agent / name, ch * 1_000)
        os.utime(f, (mt, mt))
    real = versions_mod._prune
    calls = 0

    def flaky(file_dir: int, scan: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.EIO, "EIO")
        real(file_dir, scan)

    monkeypatch.setattr(versions_mod, "_prune", flaky)

    report = await _observe(tmp_path)

    assert "bytes" in report.over_budget_reasons
    assert report.snapshotted + report.skipped == 3


async def test_enforce_sees_versions_another_replica_wrote_meanwhile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """p5:写的同时另一个副本也写下了版本:收回预算时重读一次,把它们也算进去。"""
    import hashlib

    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 4_000)
    agent = _agent(tmp_path)
    for ch in "ab":
        _put(agent / "a.md", ch * 1_000)
        _put(agent / "b.md", ch.upper() * 1_000)
        await _observe(tmp_path)
    real = versions_mod._write_pending

    def other_replica_writes_too(*args: Any) -> None:
        real(*args)
        d = _versions_root(tmp_path) / _KEY / hashlib.sha256(b"b.md").hexdigest()[:32]
        (d / ("v00000099-" + "0" * 64)).write_text("Z" * 1_000)

    monkeypatch.setattr(versions_mod, "_write_pending", other_replica_writes_too)
    _put(agent / "a.md", "d" * 1_000)

    await _observe(tmp_path)

    assert _agent_version_bytes(tmp_path) <= 4_000


async def test_a_torn_path_of_an_unchanged_file_is_repaired_in_place(tmp_path: Path) -> None:
    """p3:``path`` 坏了、文件一直没改(走不到写入那条修复):扫的时候看到了文件就就地修好;之后删掉
    照常按 30 天到期。"""
    f = _put(_agent(tmp_path) / "notes" / "f.md", "v1")
    await _observe(tmp_path)
    _put(f, "v2-longer")
    await _observe(tmp_path)
    (file_dir,) = (_versions_root(tmp_path) / _KEY).iterdir()
    (file_dir / "path").write_text("")

    await _observe(tmp_path)

    assert (file_dir / "path").read_text() == "notes/f.md"
    f.unlink()
    await _observe(tmp_path)
    _age_markers(tmp_path)
    report = await _observe(tmp_path)
    assert report.dropped == 1


async def test_an_index_dir_with_a_broken_path_keeps_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """索引的版本目录 ``path`` 坏了(索引也不在):按目录名认出是索引,仍留最新 3 份。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 10_000)
    agent = _agent(tmp_path)
    notes = agent / "AGENT_NOTES.md"
    for i in range(5):
        _put_v(notes, f"GOOD index v{i}".ljust(1_000, "."), i)
        await _observe(tmp_path)
    index_dir = _versions_root(tmp_path) / _KEY / hashlib.sha256(b"AGENT_NOTES.md").hexdigest()[:32]
    notes.unlink()
    (index_dir / "path").write_text("")
    for r, size in enumerate((3_500, 4_000)):  # 第二份要删到 2 份才放得下:不放
        _fill(agent, f"j{r}", [size])
        await _observe(tmp_path)

    assert len([p for p in index_dir.iterdir() if p.name.startswith("v")]) >= 3


async def test_a_broken_path_with_versions_is_unknown_and_keeps_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``path`` 坏了、源文件也没看到:「不知道」(按在算,留 2 份),不是「不见」(那只留 1 份)。"""
    monkeypatch.setattr(versions_mod, "MAX_AGENT_VERSION_BYTES", 10_000)
    agent = _agent(tmp_path)
    g = agent / "g.md"
    for i in range(4):
        _put_v(g, f"g{i}".ljust(1_000, "g"), i)
        await _observe(tmp_path)
    g_dir = _versions_root(tmp_path) / _KEY / hashlib.sha256(b"g.md").hexdigest()[:32]
    g.unlink()
    (g_dir / "path").write_text("")
    for r, size in enumerate((4_000, 4_500)):  # 第二份要把它删到 1 份才放得下:不放
        _fill(agent, f"j{r}", [size])
        await _observe(tmp_path)

    assert len([p for p in g_dir.iterdir() if p.name.startswith("v")]) >= 2


def _walk_incomplete_count() -> float:
    return float(
        versions_mod.agent_file_versions_total.labels(outcome="walk_incomplete")._value.get()
    )


@pytest.mark.parametrize("cause", ["depth", "unopenable"])
async def test_walk_incomplete_is_reported_logged_and_counted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    cause: str,
) -> None:
    """深度到顶、目录打不开:都报 ``walk_incomplete``,记日志、计数。"""
    agent = _agent(tmp_path)
    if cause == "depth":
        deep = agent
        for i in range(MAX_SEED_DEPTH + 2):
            deep = deep / f"d{i}"
        _put(deep / "x.txt", "x")
    else:
        _put(agent / "locked" / "x.txt", "x")
        real_openat = _openat_dir

        def refuse(dir_fd: int, name: str, *, create: bool) -> int:
            if name == "locked":
                raise PermissionError(13, "EACCES")
            return real_openat(dir_fd, name, create=create)

        monkeypatch.setattr(versions_mod, "_openat_dir", refuse)
    _put(agent / "f.md", "f")
    caplog.set_level("WARNING", logger=versions_mod.__name__)
    before = _walk_incomplete_count()

    await versions_mod._observe_logged(_store(tmp_path), _TENANT, _USER, _KEY)

    assert _walk_incomplete_count() == before + 1
    assert any("walk_incomplete" in r.getMessage() for r in caplog.records)


async def test_a_dir_that_fails_to_list_is_unlisted_and_the_observation_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """读目录本身出错(EIO 之类):这一层没列全、里面的文件「不知道」,观察照常完成。"""
    import errno

    agent = _agent(tmp_path)
    _put(agent / "sub" / "x.md", "x")
    _put(agent / "f.md", "f")
    await _observe(tmp_path)
    sub_ino = (agent / "sub").stat().st_ino
    real_scandir = os.scandir

    def failing(path: Any) -> Any:
        if isinstance(path, int) and os.fstat(path).st_ino == sub_ino:
            raise OSError(errno.EIO, "EIO")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", failing)
    report = await _observe(tmp_path)
    monkeypatch.setattr(os, "scandir", real_scandir)

    assert report.walk_incomplete is True
    assert _markers(tmp_path) == []


# ---------------------------------------------------------------------------
# review 16 —— 不观察的那段时间不算进 30 天、一个目录出错不连累别的、根目录的读错误、临时文件
# ---------------------------------------------------------------------------


async def test_an_unobserved_gap_restarts_the_thirty_days(tmp_path: Path) -> None:
    """repro_gap:T0 确认不见 → 文件被不走观察的路径放回、一个月没有观察 → T31 一次藏起来:
    上一次确认已经过了 7 天以上,从现在重新算 30 天,不会一下子删掉。"""
    agent = _agent(tmp_path)
    f = agent / "tpl" / "t.md"
    for i in range(3):
        _put_v(f, f"v{i}", i)
        await _observe(tmp_path)
    f.rename(agent / ".stash")
    await _observe(tmp_path)  # T0
    (marker,) = _markers(tmp_path)
    old = time.time() - 31 * 86_400
    marker.write_text(f"{old:.0f}")
    os.utime(marker, (old, old))  # 一个月里没有任何观察再确认过

    report = await _observe(tmp_path)  # T31:又不见

    assert report.dropped == 0
    assert _history(tmp_path)["tpl/t.md"] == ["v0", "v1", "v2"]
    assert float(marker.read_text()) > time.time() - 60  # 从现在重新算


async def test_a_torn_marker_restarts_the_thirty_days(tmp_path: Path) -> None:
    """标记内容写坏了(空的):当作重新开始,不删。"""
    gone = _put(_agent(tmp_path) / "gone.md", "g")
    await _observe(tmp_path)
    gone.unlink()
    await _observe(tmp_path)
    (marker,) = _markers(tmp_path)
    marker.write_text("")

    report = await _observe(tmp_path)

    assert report.dropped == 0
    assert float(marker.read_text()) > time.time() - 60


async def test_one_dirs_error_does_not_stop_the_sweep_of_later_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """repro_enospc 的形状(注入错误代替真把盘写满):扫到某个目录时出错(写标记 ENOSPC),后面目录里
    在着的文件的旧标记照样撤掉。"""
    import errno

    agent = _agent(tmp_path)
    for i in range(6):
        _put(agent / f"w{i}.md", f"w{i}")
    x = _put(agent / "x.md", "precious")
    await _observe(tmp_path)
    await _confirm_missing_then_restore(tmp_path, x)
    for i in range(6):
        (agent / f"w{i}.md").unlink()  # 这些要记「不见」:写标记时满盘
    real_open = os.open

    def enospc(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if isinstance(path, str) and "missing" in path and flags & os.O_CREAT:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    x_key = hashlib.sha256(b"x.md").hexdigest()[:32]
    agent_ino = (_versions_root(tmp_path) / _KEY).stat().st_ino
    real_listdir = os.listdir

    def x_last(path: Any = ".") -> list[str]:
        names = real_listdir(path)
        if isinstance(path, int) and os.fstat(path).st_ino == agent_ino:
            # review 17 —— 出错的目录一律排在 x.md 的前面:与真实列目录的顺序无关
            names = sorted(names, key=lambda n: n == x_key)
        return names

    monkeypatch.setattr(os, "listdir", x_last)
    monkeypatch.setattr(os, "open", enospc)
    await _observe(tmp_path)
    monkeypatch.setattr(os, "open", real_open)
    monkeypatch.setattr(os, "listdir", real_listdir)

    x_dir = _versions_root(tmp_path) / _KEY / x_key
    assert not (x_dir / "missing").exists()


@pytest.mark.parametrize("err", ["EMFILE", "EIO"])
async def test_a_non_enoent_error_opening_the_root_is_a_failure_that_still_sweeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, err: str
) -> None:
    """repro_rootopen:打开 agent 根出 EMFILE / EIO(存储层也收成「不存在」):不是「还没有根」——
    照样空扫描撤旧标记,再抛出去(计失败)。"""
    import errno

    agent = _agent(tmp_path)
    x = _put(agent / "x.md", "x")
    await _observe(tmp_path)
    await _confirm_missing_then_restore(tmp_path, x)
    real_open = os.open
    calls = 0

    def flaky(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal calls
        if path == _KEY and flags & os.O_DIRECTORY:
            calls += 1
            if calls == 1:  # 只有第一次:打开工作区里的 agent 根;版本区的同名目录照常
                raise OSError(getattr(errno, err), err)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", flaky)
    with pytest.raises(Exception, match="not found"):
        await _observe(tmp_path)
    monkeypatch.setattr(os, "open", real_open)

    assert _markers(tmp_path) == []


async def test_a_missing_agent_root_is_not_a_failure(tmp_path: Path) -> None:
    """agent 根真的还没建(ENOENT):什么都不做,不算失败。"""
    report = await _observe(tmp_path)

    assert report == versions_mod.ObserveReport()


async def test_a_failing_tmp_unlink_after_link_still_counts_the_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """probe_unlink_eio:版本已经 ``link`` 好,删临时文件出 EIO —— 这份照样算写下了、记进账里。"""
    import errno

    _put(_agent(tmp_path) / "a.md", "a" * 1_000)
    real_unlink = os.unlink

    def unlink(name: Any, *, dir_fd: int | None = None) -> None:
        if isinstance(name, str) and name.startswith(".tmp-") and "-path-" not in name:
            raise OSError(errno.EIO, "EIO")
        real_unlink(name, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", unlink)
    report = await _observe(tmp_path)
    monkeypatch.setattr(os, "unlink", real_unlink)

    assert report.snapshotted == 1 and report.skipped == 0
    assert _history(tmp_path)["a.md"] == ["a" * 1_000]


async def test_the_walk_error_wins_when_the_fallback_sweep_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """遍历抛了、兜底收尾也抛了:往外抛的是遍历的那个错误(兜底的只记日志)。"""
    _put(_agent(tmp_path) / "x.md", "x")

    def walk_fails(*args: Any) -> Any:
        raise KeyError("walk")

    def sweep_fails(*args: Any) -> None:
        raise OSError(5, "EIO")

    monkeypatch.setattr(versions_mod, "_files", walk_fails)
    monkeypatch.setattr(versions_mod, "_housekeep", sweep_fails)

    with pytest.raises(KeyError, match="walk"):
        await _observe(tmp_path)


async def test_a_dir_that_errors_still_gets_its_stale_marker_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一个版本目录自己列不出来(EIO):接住之后照样试着撤掉它的旧标记(它不是「确认不见」)。"""
    import errno

    agent = _agent(tmp_path)
    x = _put(agent / "x.md", "x")
    await _observe(tmp_path)
    await _confirm_missing_then_restore(tmp_path, x)
    x_dir = _versions_root(tmp_path) / _KEY / hashlib.sha256(b"x.md").hexdigest()[:32]
    x_ino = x_dir.stat().st_ino
    real_listdir = os.listdir

    def failing(path: Any = ".") -> list[str]:
        if isinstance(path, int) and os.fstat(path).st_ino == x_ino:
            raise OSError(errno.EIO, "EIO")
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", failing)
    await _observe(tmp_path)
    monkeypatch.setattr(os, "listdir", real_listdir)

    assert not (x_dir / "missing").exists()


# ---------------------------------------------------------------------------
# review 17 —— 标记的内容与 mtime 同一个 inode、不合理的时间、读 ``path`` 出错但文件在
# ---------------------------------------------------------------------------


async def _stale_marker(tmp_path: Path) -> Path:
    f = _agent(tmp_path) / "t.md"
    for i in range(3):
        _put_v(f, f"v{i}", i)
        await _observe(tmp_path)
    f.unlink()
    await _observe(tmp_path)  # T0
    (marker,) = _markers(tmp_path)
    old = time.time() - 31 * 86_400
    marker.write_text(f"{old:.0f}")
    os.utime(marker, (old, old))  # 一个月没有再确认过
    return marker


async def test_a_marker_rewritten_by_another_replica_mid_read_is_not_mixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review 17(test_r17_race2 的形状):读完旧标记的内容之后,另一个副本把它重写成新的 ——
    不能拼成「旧的开始时间 + 新的确认时间」而一下子到期。"""
    await _stale_marker(tmp_path)
    real = versions_mod._marker_first_missing

    def other_replica_rewrites(file_dir: int) -> Any:
        out = real(file_dir)
        versions_mod._atomic_write(file_dir, "missing", f"{time.time():.0f}".encode())
        return out

    monkeypatch.setattr(versions_mod, "_marker_first_missing", other_replica_rewrites)

    report = await _observe(tmp_path)

    assert report.dropped == 0
    assert _history(tmp_path)["t.md"] == ["v0", "v1", "v2"]


@pytest.mark.parametrize("content", [b"0", b"-inf", b"nan", b"1e400", b"1000000000"])
async def test_an_implausible_first_missing_time_restarts(tmp_path: Path, content: bytes) -> None:
    """review 17:内容不是有限数、或早于 2020 年(哪怕 mtime 是刚确认的):当作没有标记,重新开始。"""
    marker = await _stale_marker(tmp_path)
    marker.write_bytes(content)
    now = time.time()
    os.utime(marker, (now, now))

    report = await _observe(tmp_path)

    assert report.dropped == 0
    assert abs(float(marker.read_text()) - time.time()) < 60


async def test_a_future_first_missing_time_is_rewritten_to_now(tmp_path: Path) -> None:
    """review 17:内容在将来、mtime 是刚确认的:下一次确认把它改写成现在。"""
    marker = await _stale_marker(tmp_path)
    marker.write_text(f"{time.time() + 365 * 86_400:.0f}")
    now = time.time()
    os.utime(marker, (now, now))

    await _observe(tmp_path)

    assert abs(float(marker.read_text()) - time.time()) < 60


async def test_a_path_read_error_still_clears_the_marker_of_a_seen_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review 17:读 ``path`` 出 EIO、但源文件这次看到了(按目录名认得出):旧标记照样撤掉。"""
    import errno

    x = _put(_agent(tmp_path) / "x.md", "x")
    await _observe(tmp_path)
    await _confirm_missing_then_restore(tmp_path, x)
    real_open = os.open

    def flaky(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if path == "path" and dir_fd is not None:
            raise OSError(errno.EIO, "EIO")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", flaky)
    report = await _observe(tmp_path)
    monkeypatch.setattr(os, "open", real_open)

    assert report.dropped == 0
    assert _markers(tmp_path) == []
