"""B-236 第二层 —— 对话开场把 agent 根的基础目录复制进本对话的 ``/workspace``。

真落盘:临时目录当 NAS 根,读写都走真的 :class:`NasWorkspaceStore` 与复制模块。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from orchestrator.graph_builder.conversation_seed import make_conversation_seed_node
from orchestrator.tools import conversation_seed as seed_mod
from orchestrator.tools.conversation_seed import (
    MAX_SEED_BYTES,
    SEED_MARKER,
    SEED_TMP_PREFIX,
    seed_conversation,
)
from orchestrator.tools.locks import ProcessKeyedLock
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from orchestrator.tools.workspace_store import WorkspaceFileEntry
from orchestrator.tools.workspace_tree import render_conversation_block

_TENANT, _USER = uuid4(), uuid4()
_KEY = "plan-aaaaaaaa"
_C1 = UUID("11111111-1111-1111-1111-111111111111")


def _agent(root: Path) -> Path:
    return root / str(_TENANT) / str(_USER) / "agents" / _KEY


def _conv(root: Path, conv: UUID = _C1) -> Path:
    return _agent(root) / "threads" / str(conv)


def _work(root: Path, conv: UUID = _C1) -> Path:
    return _conv(root, conv) / "work"


def _put(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _base_tree(root: Path) -> None:
    agent = _agent(root)
    _put(agent / "style" / "s.md", "anchor")
    _put(agent / "style" / "deep" / "a" / "b.txt", "deep")
    _put(agent / "tpl" / "t.md", "tpl")
    _put(agent / "AGENT_NOTES.md", "root file — never copied")
    for reserved in ("uploads", "inputs", ".tool_results", "skills", "shared", ".hidden"):
        _put(agent / reserved / "r.txt")


async def _seed(root: Path, conv: UUID = _C1, lock: Any = None) -> seed_mod.SeedReport | None:
    return await seed_conversation(
        NasWorkspaceStore(root=str(root)),
        tenant_id=_TENANT,
        user_id=_USER,
        agent_key=_KEY,
        conv_id=conv,
        lock=lock or ProcessKeyedLock(),
    )


def _leftover_tmp(root: Path) -> list[str]:
    return [p.name for p in _conv(root).iterdir() if p.name.startswith(SEED_TMP_PREFIX)]


async def test_base_dirs_are_copied_once_with_their_mtimes(tmp_path: Path) -> None:
    _base_tree(tmp_path)
    src = _agent(tmp_path) / "style" / "s.md"
    os.utime(src, ns=(1_700_000_000_000_000_000, 1_700_000_000_123_456_789))

    report = await _seed(tmp_path)

    assert report is not None
    assert report.copied == ("style", "tpl")
    work = _work(tmp_path)
    assert sorted(p.name for p in work.iterdir()) == ["style", "tpl"]
    assert (work / "style" / "deep" / "a" / "b.txt").read_text() == "deep"
    assert (work / "style" / "s.md").stat().st_mtime_ns == src.stat().st_mtime_ns
    assert (work / "style" / "s.md").stat().st_mode & 0o777 == 0o600
    marker = json.loads((_conv(tmp_path) / SEED_MARKER).read_text())
    assert marker["copied"] == ["style", "tpl"]
    assert _leftover_tmp(tmp_path) == []


async def test_a_second_run_does_nothing_and_never_overwrites(tmp_path: Path) -> None:
    """幂等:标记在 → 不复制;本对话改过的副本、之后才出现在 agent 根的目录都不动。"""
    _base_tree(tmp_path)
    assert await _seed(tmp_path) is not None
    _put(_work(tmp_path) / "style" / "s.md", "edited here")
    _put(_agent(tmp_path) / "later" / "l.md")

    assert await _seed(tmp_path) is None
    assert (_work(tmp_path) / "style" / "s.md").read_text() == "edited here"
    assert not (_work(tmp_path) / "later").exists()


async def test_names_already_in_the_conversation_are_never_overwritten(tmp_path: Path) -> None:
    _base_tree(tmp_path)
    _put(_work(tmp_path) / "style" / "mine.md", "mine")

    report = await _seed(tmp_path)

    assert report is not None and report.skipped_existing == ("style",)
    assert sorted(p.name for p in (_work(tmp_path) / "style").iterdir()) == ["mine.md"]


async def test_concurrent_runs_of_one_conversation_copy_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两个 run 同时开始:只有一个去复制,另一个拿到锁后看到标记,什么都不做。复制故意放慢,
    让两边一定同时越过「还没有标记」那一眼。"""
    _base_tree(tmp_path)
    real = seed_mod._measure

    def slow_measure(*args: Any, **kwargs: Any) -> None:
        time.sleep(0.2)
        real(*args, **kwargs)

    monkeypatch.setattr(seed_mod, "_measure", slow_measure)
    lock = ProcessKeyedLock()

    reports = await asyncio.gather(_seed(tmp_path, lock=lock), _seed(tmp_path, lock=lock))

    assert sum(r is not None for r in reports) == 1
    assert sorted(p.name for p in _work(tmp_path).iterdir()) == ["style", "tpl"]
    assert _leftover_tmp(tmp_path) == []


async def test_symlinks_in_the_agent_root_are_not_followed(tmp_path: Path) -> None:
    """agent 根里种下的符号链接(一级目录本身、目录里的文件、目录里的子目录)一律不跟:
    链接指向的内容不进本对话,跳过的条目计数。"""
    outside = tmp_path / "outside"
    _put(outside / "secret.txt", "SECRET")
    agent = _agent(tmp_path)
    _put(agent / "style" / "s.md", "anchor")
    (agent / "linked").symlink_to(outside)
    (agent / "style" / "leak.txt").symlink_to(outside / "secret.txt")
    (agent / "style" / "leakdir").symlink_to(outside)
    os.mkfifo(agent / "style" / "pipe")

    report = await _seed(tmp_path)

    assert report is not None
    assert report.copied == ("style",)
    assert report.skipped_entries == 4  # linked/、leak.txt、leakdir、pipe
    work = _work(tmp_path)
    assert sorted(p.name for p in (work / "style").iterdir()) == ["s.md"]
    assert not (work / "linked").exists()
    for path in work.rglob("*"):
        assert not path.is_symlink()
        assert "SECRET" not in (path.read_text() if path.is_file() else "")


async def test_a_dir_over_the_cap_is_skipped_whole_and_reported(tmp_path: Path) -> None:
    """超出 20 MB 的目录整个不复制(稀疏文件:只看大小,一个字节都不读),其余照常;没复制的
    目录进 ``seed_skipped_dirs``,快照段里写明它只在 ``/agent/<目录>/``。"""
    _base_tree(tmp_path)
    big = _put(_agent(tmp_path) / "big" / "video.bin", "")
    os.truncate(big, MAX_SEED_BYTES + 1)
    node = make_conversation_seed_node(
        store=NasWorkspaceStore(root=str(tmp_path)), lock=ProcessKeyedLock()
    )

    update = await node({"messages": []}, _config())  # type: ignore[typeddict-item]

    assert update == {"seed_skipped_dirs": ["big"]}
    assert sorted(p.name for p in _work(tmp_path).iterdir()) == ["style", "tpl"]
    assert _leftover_tmp(tmp_path) == []
    block = render_conversation_block(
        [], [WorkspaceFileEntry(path="big/video.bin", size=1, mtime=None)], seed_skipped=["big"]
    )
    assert "use in place: /agent/big/" in block


async def test_the_cap_is_shared_by_the_whole_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """额度是整个对话的:前面的目录用掉的,后面的目录不能再用。"""
    monkeypatch.setattr(seed_mod, "MAX_SEED_ENTRIES", 4)
    agent = _agent(tmp_path)
    _put(agent / "a" / "1.md")
    _put(agent / "a" / "2.md")  # a/ = 3 条(目录本身 + 2 个文件)
    _put(agent / "b" / "1.md")  # b/ = 2 条,剩 1 条放不下

    report = await _seed(tmp_path)

    assert report is not None
    assert (report.copied, report.skipped_too_large) == (("a",), ("b",))


async def test_a_failed_seed_writes_no_marker_and_the_next_run_finishes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复制中途失败:不写标记、不留临时目录;下一次 run 再做,已经搬进来的目录不重复。"""
    _base_tree(tmp_path)
    real = seed_mod._seed_one

    def fail_on_tpl(agent_fd: int, tmp_fd: int, work_fd: int, name: str, budget: Any) -> None:
        if name == "tpl":
            raise OSError(5, "EIO")
        real(agent_fd, tmp_fd, work_fd, name, budget)

    monkeypatch.setattr(seed_mod, "_seed_one", fail_on_tpl)
    with pytest.raises(OSError):
        await _seed(tmp_path)
    assert not (_conv(tmp_path) / SEED_MARKER).exists()
    assert _leftover_tmp(tmp_path) == []
    assert sorted(p.name for p in _work(tmp_path).iterdir()) == ["style"]

    monkeypatch.setattr(seed_mod, "_seed_one", real)
    report = await _seed(tmp_path)

    assert report is not None
    assert (report.copied, report.skipped_existing) == (("tpl",), ("style",))
    assert (_conv(tmp_path) / SEED_MARKER).exists()


async def test_a_name_that_is_not_utf8_is_skipped_and_counted(tmp_path: Path) -> None:
    _base_tree(tmp_path)
    bad = os.fsencode(_agent(tmp_path)) + b"/\xd7\xca"
    try:
        os.mkdir(bad)
    except OSError as exc:  # macOS APFS 拒绝非 UTF-8 文件名
        pytest.skip(f"filesystem refuses a non-UTF-8 name: {exc}")

    report = await _seed(tmp_path)

    assert report is not None and report.copied == ("style", "tpl")
    assert report.skipped_entries == 1


def _config(**extra: Any) -> dict[str, Any]:
    return {
        "configurable": {
            "tenant_id": str(_TENANT),
            "user_id": str(_USER),
            "agent_key": _KEY,
            "thread_id": str(_C1),
            "workspace_layout": "conversation",
            **extra,
        }
    }


@pytest.mark.parametrize(
    "extra",
    [{"child_run": True}, {"workspace_layout": "agent_root"}, {"agent_key": ""}],
    ids=["child_run", "legacy_view", "unbound"],
)
async def test_the_node_only_seeds_a_top_level_conversation_view(
    tmp_path: Path, extra: dict[str, Any]
) -> None:
    _base_tree(tmp_path)
    node = make_conversation_seed_node(
        store=NasWorkspaceStore(root=str(tmp_path)), lock=ProcessKeyedLock()
    )

    assert await node({"messages": []}, _config(**extra)) == {}  # type: ignore[typeddict-item]
    assert not _conv(tmp_path).exists()


async def test_a_failing_seed_never_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _base_tree(tmp_path)

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(5, "EIO on /nas/客户甲/style")

    monkeypatch.setattr(seed_mod, "seed_conversation_sync", boom)
    node = make_conversation_seed_node(
        store=NasWorkspaceStore(root=str(tmp_path)), lock=ProcessKeyedLock()
    )
    with caplog.at_level("WARNING"):
        assert await node({"messages": []}, _config()) == {}  # type: ignore[typeddict-item]
    (record,) = [r for r in caplog.records if "conversation_seed" in r.getMessage()]
    assert "OSError" in record.getMessage()
    assert "客户甲" not in record.getMessage()


async def test_the_lock_is_keyed_by_conversation_and_released(tmp_path: Path) -> None:
    lock = ProcessKeyedLock()
    _base_tree(tmp_path)
    await _seed(tmp_path, lock=lock)
    await _seed(tmp_path, conv=uuid4(), lock=lock)
    assert lock._locks == {} and lock._holders == {}
