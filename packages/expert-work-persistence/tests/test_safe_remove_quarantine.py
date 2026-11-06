"""B-282 —— ``quarantine_entry`` / ``restore_quarantined``:改名挪进 / 挪出隔离区。

跨父目录改名一个目录要改写它自己的 ``..``,目录本身得可写;模型能把自己的目录 ``chmod 555``。
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from expert_work.persistence.workspace.safe_remove import (
    QUARANTINED,
    quarantine_entry,
    restore_quarantined,
)


@pytest.fixture
def tree(tmp_path: Path) -> Iterator[tuple[Path, UUID, UUID]]:
    tenant, user = uuid4(), uuid4()
    yield tmp_path, tenant, user
    for path in tmp_path.rglob("*"):  # let pytest clean up whatever is still read-only
        if path.is_dir() and not path.is_symlink():
            path.chmod(stat.S_IRWXU)


def _read_only_dir(base: Path) -> Path:
    target = base / "conv"
    (target / "work").mkdir(parents=True)
    (target / "work" / "note.md").write_text("note")
    target.chmod(0o555)
    return target


def test_a_read_only_directory_is_moved_into_quarantine(tree: tuple[Path, UUID, UUID]) -> None:
    root, tenant, user = tree
    parent = root / str(tenant) / str(user) / "threads"
    target = _read_only_dir(parent)

    outcome, parked = quarantine_entry(
        str(root), [str(tenant), str(user), "threads"], "conv", tenant_id=tenant, owner_id=user
    )

    assert outcome == QUARANTINED and parked is not None
    assert not target.exists()
    moved = root / str(tenant) / ".purged-agents" / str(user) / parked
    assert (moved / "work" / "note.md").read_text() == "note"


def test_a_read_only_directory_is_moved_back_out_of_quarantine(
    tree: tuple[Path, UUID, UUID],
) -> None:
    root, tenant, user = tree
    parent = root / str(tenant) / str(user) / "threads"
    parent.mkdir(parents=True)
    holding = root / str(tenant) / ".purged-agents" / str(user)
    _read_only_dir(holding).rename(holding / "conv-1700000000-0123abcd")

    restored = restore_quarantined(
        str(root),
        [str(tenant), str(user), "threads"],
        "conv",
        tenant_id=tenant,
        owner_id=user,
        parked="conv-1700000000-0123abcd",
    )

    assert restored
    assert (parent / "conv" / "work" / "note.md").read_text() == "note"
    assert not os.path.lexists(holding / "conv-1700000000-0123abcd")
