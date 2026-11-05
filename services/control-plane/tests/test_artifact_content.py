"""B-235 —— 选版规则(``choose_version``)的边界。"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from control_plane.api._artifact_content import (
    VersionExpired,
    VersionNotRetained,
    VersionUnknown,
    choose_version,
)
from expert_work.protocol import ArtifactVersion


def _v(artifact_id: UUID, version: int, *, copy: bool = True) -> ArtifactVersion:
    vid = uuid4()
    return ArtifactVersion(
        id=vid,
        artifact_id=artifact_id,
        tenant_id=uuid4(),
        user_id=uuid4(),
        version=version,
        path_in_workspace="a.txt",
        size_bytes=1 if copy else None,
        sha256="s" if copy else None,
        created_in_thread="t",
        snapshot_key=str(vid) if copy else None,
    )


def test_a_version_of_another_artifact_is_unknown() -> None:
    """``agent_key=None`` 的会话(老会话)按名字查最新版与查指定版是两次独立查询,同名多行
    时两次可能落在不同的产物上 —— 拿到的那一版不属于「最新版」那条产物,就当不存在。"""
    latest = _v(uuid4(), 3)
    other = _v(uuid4(), 2)

    with pytest.raises(VersionUnknown):
        choose_version(latest=latest, requested=2, pinned=other)


def test_same_artifact_rules() -> None:
    art = uuid4()
    latest = _v(art, 3, copy=False)
    assert choose_version(latest=latest, requested=None, pinned=None) is latest
    kept = _v(art, 2)
    assert choose_version(latest=latest, requested=2, pinned=kept) is kept
    assert choose_version(latest=latest, requested=3, pinned=latest) is latest
    with pytest.raises(VersionNotRetained):
        choose_version(latest=latest, requested=1, pinned=_v(art, 1, copy=False))
    with pytest.raises(VersionUnknown):
        choose_version(latest=latest, requested=9, pinned=None)


def test_a_missing_version_below_the_latest_has_expired() -> None:
    """B-63 —— 版本号从 1 连续分配,单独删掉某一版的只有留存作业:比最新版小、却没有版本行的
    那一版是超过保留期被清掉的;比最新版大的是从没有过。"""
    art = uuid4()
    latest = _v(art, 3)
    with pytest.raises(VersionExpired) as caught:
        choose_version(latest=latest, requested=2, pinned=None)
    assert (caught.value.requested, caught.value.latest) == (2, 3)
    with pytest.raises(VersionExpired):
        choose_version(latest=latest, requested=1, pinned=None)
    for never in (4, 0, -1):
        with pytest.raises(VersionUnknown):
            choose_version(latest=latest, requested=never, pinned=None)
