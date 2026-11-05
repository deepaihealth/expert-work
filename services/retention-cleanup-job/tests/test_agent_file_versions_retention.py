"""B-236 —— 已删工作区到期销账时,``/agent`` 文件的历史版本一起删(它住在用户根旁边)。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from retention_cleanup_job.artifact_copies import remove_user_agent_file_versions
from retention_cleanup_job.workspace_files import UnsafeWorkspacePathError


def test_the_users_version_dir_is_removed_and_counted(tmp_path: Path) -> None:
    tenant, user, other = uuid4(), uuid4(), uuid4()
    base = tmp_path / str(tenant) / ".agent-file-versions"
    for who, n in ((user, 3), (other, 1)):
        file_dir = base / str(who) / "k" / ("a" * 32)
        file_dir.mkdir(parents=True)
        for i in range(n):
            (file_dir / f"v{i:08d}-{'0' * 64}").write_text("x")

    assert remove_user_agent_file_versions(str(tmp_path), tenant, user) == 3
    assert not (base / str(user)).exists()
    assert (base / str(other)).exists()
    assert remove_user_agent_file_versions(str(tmp_path), tenant, user) == 0


def test_a_symlinked_level_is_refused(tmp_path: Path) -> None:
    tenant, user = uuid4(), uuid4()
    outside = tmp_path / "outside"
    (outside / str(user)).mkdir(parents=True)
    (tmp_path / str(tenant)).mkdir()
    (tmp_path / str(tenant) / ".agent-file-versions").symlink_to(outside)

    with pytest.raises(UnsafeWorkspacePathError):
        remove_user_agent_file_versions(str(tmp_path), tenant, user)
    assert (outside / str(user)).exists()
