"""B-50 Task 12 —— 两条孤儿扫描的行为判据。

``threads/<thread_id>/`` 与 ``.tool_results/<run_id>/`` 同一套规矩
(spec §4.2):库里的行不在 **且** 超过宽限期才删;行还在的一律不动,
不管多老。

这里用真的 in-memory store,不用桩:扫描的正确性全押在「行在不在」这一个
谓词上,而桩最容易在这个谓词上撒谎(桩说「没有」,真 store 说「有」)。

``shared/`` 那两条是**不变式**,不是普通用例:那棵子树装的是搬迁时反推不出
归属的 legacy,按定义查不到登记行 —— 一旦被扫进来,就会被当成孤儿按 mtime
删掉,而且删的是用户唯一一份历史工作记忆。
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from expert_work.persistence.thread_meta import InMemoryThreadMetaStore
from expert_work.runtime.runs import DisconnectMode, RunInfo, RunStatus
from expert_work.runtime.runs.store import InMemoryRunStore
from retention_cleanup_job.orphan_threads import (
    sweep_orphan_thread_dirs,
    sweep_orphan_tool_results,
)

_TENANT = UUID("00000000-0000-0000-0000-0000000000aa")
_USER = UUID("00000000-0000-0000-0000-0000000000bb")
_KEY = "plan-aaaaaaaa"

#: 比宽限期(24h)老得多 —— 「过期」这一半判据必须成立,否则用例验的是
#: 「宽限期救了它」而不是「行还在救了它」。
_ANCIENT = time.time() - 400 * 86400


def _seed(base: Path, rel: str, *, aged: bool = True) -> Path:
    d = base / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "PLAN.md").write_bytes(b"p")
    if aged:
        for p in (d, d / "PLAN.md"):
            os.utime(p, (_ANCIENT, _ANCIENT))
    return d


@pytest.fixture
def user_root(tmp_path: Path) -> Path:
    return tmp_path / str(_TENANT) / str(_USER)


def _run(run_id: UUID, thread_id: UUID) -> RunInfo:
    now = datetime.now(UTC)
    return RunInfo(
        run_id=run_id,
        tenant_id=_TENANT,
        thread_id=thread_id,
        user_id=_USER,
        status=RunStatus.SUCCESS,
        on_disconnect=DisconnectMode.CONTINUE,
        is_resume=False,
        error=None,
        created_at=now,
        updated_at=now,
        finished_at=now,
        trace_id=None,
    )


async def _thread_store(*thread_ids: UUID) -> InMemoryThreadMetaStore:
    store = InMemoryThreadMetaStore()
    for tid in thread_ids:
        await store.create(
            thread_id=tid,
            tenant_id=_TENANT,
            created_by=str(_USER),
            user_id=_USER,
            agent_name="plan",
        )
    return store


# --------------------------------------------------------------- threads/


async def test_thread_dir_removed_under_the_agent_subtree(
    tmp_path: Path, user_root: Path
) -> None:
    """搬迁后的位置也扫得到、也删得掉 —— 枚举与删除必须认同一个 agent_key。

    枚举认了而删除没带 agent_key,表现是「每天扫到、每天删不掉」,
    返回值还会是 0,没有任何东西报错。
    """
    orphan = uuid4()
    scoped = _seed(user_root, f"agents/{_KEY}/threads/{orphan}")

    removed = await sweep_orphan_thread_dirs(str(tmp_path), await _thread_store())

    assert removed == 1
    assert not scoped.exists()


async def test_thread_dir_kept_while_the_row_exists(tmp_path: Path, user_root: Path) -> None:
    """行还在 → 一律不动,不管多老。"""
    live = uuid4()
    d = _seed(user_root, f"agents/{_KEY}/threads/{live}")

    removed = await sweep_orphan_thread_dirs(str(tmp_path), await _thread_store(live))

    assert removed == 0
    assert (d / "PLAN.md").exists()


async def test_shared_thread_dirs_are_never_touched(tmp_path: Path, user_root: Path) -> None:
    """``shared/`` 一个字节都不许动 —— 哪怕它形状对、行不在、而且老得过期。

    三个条件全中还必须活下来,这条才验到了「因为它在 shared/」而不是
    「因为它碰巧不满足某个条件」。
    """
    orphan = uuid4()
    d = _seed(user_root, f"shared/threads/{orphan}")
    _seed(user_root, "shared/style")

    removed = await sweep_orphan_thread_dirs(str(tmp_path), await _thread_store())

    assert removed == 0
    assert (d / "PLAN.md").exists()
    assert (user_root / "shared" / "style" / "PLAN.md").exists()


# --------------------------------------------------- .tool_results/


async def test_tool_results_dir_swept_when_run_is_gone(tmp_path: Path, user_root: Path) -> None:
    """孤儿 ``.tool_results/<run_id>/`` 过了宽限期才删。"""
    gone = uuid4()
    d = _seed(user_root, f"agents/{_KEY}/.tool_results/{gone}")

    removed = await sweep_orphan_tool_results(str(tmp_path), InMemoryRunStore())

    assert removed == 1
    assert not d.exists()


async def test_tool_results_dir_kept_while_run_row_exists(
    tmp_path: Path, user_root: Path
) -> None:
    """run 行还在 → 一律不动,不管多老(与 threads/ 同一条规矩)。"""
    run_id = uuid4()
    d = _seed(user_root, f"agents/{_KEY}/.tool_results/{run_id}")
    store = InMemoryRunStore()
    await store.create(_run(run_id, uuid4()))

    removed = await sweep_orphan_tool_results(str(tmp_path), store)

    assert removed == 0
    assert (d / "PLAN.md").exists()


async def test_tool_results_dir_within_grace_is_kept(tmp_path: Path, user_root: Path) -> None:
    """行不在但还新 —— 宽限期内不删。

    兜的是「留存清掉老 run 行的同一刻,一个引用它的 run 还在收尾」那个窗口。
    """
    gone = uuid4()
    d = _seed(user_root, f"agents/{_KEY}/.tool_results/{gone}", aged=False)

    removed = await sweep_orphan_tool_results(str(tmp_path), InMemoryRunStore())

    assert removed == 0
    assert (d / "PLAN.md").exists()


async def test_shared_tool_results_are_never_touched(tmp_path: Path, user_root: Path) -> None:
    """``shared/`` 下的溢出缓存同样不碰。"""
    gone = uuid4()
    d = _seed(user_root, f"shared/.tool_results/{gone}")

    removed = await sweep_orphan_tool_results(str(tmp_path), InMemoryRunStore())

    assert removed == 0
    assert (d / "PLAN.md").exists()


async def test_legacy_tool_results_at_the_user_root_are_swept(
    tmp_path: Path, user_root: Path
) -> None:
    """搬迁前的位置也要扫 —— 这批正是「从没被清过」的那 40 个目录。"""
    gone = uuid4()
    d = _seed(user_root, f".tool_results/{gone}")

    removed = await sweep_orphan_tool_results(str(tmp_path), InMemoryRunStore())

    assert removed == 1
    assert not d.exists()
