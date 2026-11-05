"""B-278 —— the retention job empties the purge holding area after 7 days."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from expert_work.persistence import purged_agents_dir
from expert_work.persistence.workspace import safe_remove
from retention_cleanup_job.job import RetentionCleanupJob


def _no_db() -> None:
    return None


def _park(root: Path, *, age_s: float) -> Path:
    tenant, user = uuid4(), uuid4()
    entry = purged_agents_dir(str(root), tenant, user) / (
        f"agent-0badc0de-{int(time.time() - age_s)}-{uuid4().hex[:8]}"
    )
    (entry / "deep" / "er").mkdir(parents=True)
    (entry / "deep" / "er" / "f.txt").write_text("x")
    return entry


def _job(root: Path) -> RetentionCleanupJob:
    kwargs: dict[str, Any] = {"db_session_factory": _no_db, "workspace_root": str(root)}
    return RetentionCleanupJob(**kwargs)


@pytest.mark.asyncio
async def test_old_quarantined_entries_are_removed_and_fresh_ones_kept(tmp_path: Path) -> None:
    old = _park(tmp_path, age_s=8 * 24 * 3600)
    fresh = _park(tmp_path, age_s=3600)

    job = _job(tmp_path)
    sweep = await job._sweep_purged_agents()

    assert (sweep.removed, sweep.failed) == (1, 0)
    assert "purged_agents" not in job._failed_passes
    assert not old.exists()
    assert fresh.exists()


@pytest.mark.asyncio
async def test_an_entry_that_will_not_go_is_counted_and_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stuck = _park(tmp_path, age_s=8 * 24 * 3600)

    def _never(dfd: int, name: str) -> bool:
        return False

    monkeypatch.setattr(safe_remove, "remove_tree_at", _never)

    job = _job(tmp_path)
    sweep = await job._sweep_purged_agents()

    assert (sweep.removed, sweep.failed) == (0, 1)
    assert "purged_agents" in job._failed_passes
    assert stuck.exists()
