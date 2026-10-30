"""B-236 第三层 —— ``/agent`` 文件的版本:运行结束时看一眼,改过的留一份。真落盘。"""

from __future__ import annotations

import asyncio
import os
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
# review 7 —— 用户合计上限、源文件不见满 30 天、删文件接口、清除之后不再建
# ---------------------------------------------------------------------------


def _version_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in _versions_root(root).rglob("v*") if p.is_file())


async def test_the_users_versions_are_capped_oldest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """合计超过上限:从最早写下的版本删起,跨文件 —— 最新的版本留着。"""
    monkeypatch.setattr(versions_mod, "MAX_USER_VERSION_BYTES", 10_000)
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
