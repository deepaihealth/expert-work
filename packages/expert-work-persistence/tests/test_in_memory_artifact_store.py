"""Unit tests for InMemoryArtifactStore — Stream J.9 contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import InMemoryArtifactStore

#: B-50 —— 这些用例是单 agent 场景;``agent_key`` 现在是必传参数。
_AGENT_KEY = "test-agent-0badc0de"


async def _only_id(store: object, tenant_id: UUID, user_id: UUID, name: str) -> UUID:
    """B-50 —— ``update_kind`` / ``list_versions`` 改按 ``artifact_id`` 寻址
    (四元组键之后 name 不是身份)。测试里先按 name 找到那一行的 id。"""
    rows = await store.list_for_user(  # type: ignore[attr-defined]
        tenant_id=tenant_id, user_id=user_id, agent_key=None, include_deleted=True
    )
    return UUID(str(next(a.id for a in rows if a.name == name)))


@pytest.mark.asyncio
async def test_save_version_creates_artifact_at_version_one() -> None:
    store = InMemoryArtifactStore()
    tenant_id, user_id = uuid4(), uuid4()

    version = await store.save_version(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=_AGENT_KEY,
        name="report.md",
        kind="document",
        path_in_workspace="report.md",
        created_in_thread="t-1",
    )
    assert version.version == 1
    assert version.tenant_id == tenant_id
    assert version.size_bytes is None
    assert version.sha256 is None

    artifacts = await store.list_for_user(
        tenant_id=tenant_id, user_id=user_id, agent_key=_AGENT_KEY
    )
    assert len(artifacts) == 1
    assert artifacts[0].name == "report.md"
    assert artifacts[0].kind == "document"
    assert artifacts[0].latest_version == 1


@pytest.mark.asyncio
async def test_save_version_appends_new_version_for_same_name() -> None:
    store = InMemoryArtifactStore()
    tenant_id, user_id = uuid4(), uuid4()

    v1 = await store.save_version(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=_AGENT_KEY,
        name="report.md",
        kind="document",
        path_in_workspace="report.md",
        created_in_thread="t-1",
    )
    v2 = await store.save_version(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=_AGENT_KEY,
        name="report.md",
        kind="document",
        path_in_workspace="report.md",
        created_in_thread="t-2",
    )
    assert (v1.version, v2.version) == (1, 2)
    assert v1.artifact_id == v2.artifact_id

    artifacts = await store.list_for_user(
        tenant_id=tenant_id, user_id=user_id, agent_key=_AGENT_KEY
    )
    assert len(artifacts) == 1
    assert artifacts[0].latest_version == 2


@pytest.mark.asyncio
async def test_save_version_keeps_original_kind() -> None:
    # A later save never changes an existing artifact's kind.
    store = InMemoryArtifactStore()
    tenant_id, user_id = uuid4(), uuid4()

    await store.save_version(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=_AGENT_KEY,
        name="x",
        kind="document",
        path_in_workspace="x",
        created_in_thread="t-1",
    )
    await store.save_version(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=_AGENT_KEY,
        name="x",
        kind="code",
        path_in_workspace="x",
        created_in_thread="t-2",
    )
    artifacts = await store.list_for_user(
        tenant_id=tenant_id, user_id=user_id, agent_key=_AGENT_KEY
    )
    assert artifacts[0].kind == "document"


@pytest.mark.asyncio
async def test_list_for_user_filters_by_tenant_and_user() -> None:
    store = InMemoryArtifactStore()
    tenant_a, tenant_b = uuid4(), uuid4()
    user_x, user_y = uuid4(), uuid4()

    await store.save_version(
        tenant_id=tenant_a,
        user_id=user_x,
        agent_key=_AGENT_KEY,
        name="a",
        kind="data",
        path_in_workspace="a",
        created_in_thread="t",
    )
    # Same name, different user / tenant → separate artifacts.
    await store.save_version(
        tenant_id=tenant_a,
        user_id=user_y,
        agent_key=_AGENT_KEY,
        name="a",
        kind="data",
        path_in_workspace="a",
        created_in_thread="t",
    )
    await store.save_version(
        tenant_id=tenant_b,
        user_id=user_x,
        agent_key=_AGENT_KEY,
        name="a",
        kind="data",
        path_in_workspace="a",
        created_in_thread="t",
    )

    assert (
        len(await store.list_for_user(tenant_id=tenant_a, user_id=user_x, agent_key=_AGENT_KEY))
        == 1
    )
    assert (
        len(await store.list_for_user(tenant_id=tenant_a, user_id=user_y, agent_key=_AGENT_KEY))
        == 1
    )
    assert await store.list_for_user(tenant_id=uuid4(), user_id=user_x, agent_key=_AGENT_KEY) == []


@pytest.mark.asyncio
async def test_get_latest_version_returns_newest_revision() -> None:
    store = InMemoryArtifactStore()
    tenant_id, user_id = uuid4(), uuid4()
    await store.save_version(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=_AGENT_KEY,
        name="report.md",
        kind="document",
        path_in_workspace="v1.md",
        created_in_thread="t-1",
    )
    await store.save_version(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=_AGENT_KEY,
        name="report.md",
        kind="document",
        path_in_workspace="v2.md",
        created_in_thread="t-2",
    )
    latest = await store.get_latest_version(
        tenant_id=tenant_id, user_id=user_id, agent_key=_AGENT_KEY, name="report.md"
    )
    assert latest is not None
    assert latest.version == 2
    assert latest.path_in_workspace == "v2.md"


@pytest.mark.asyncio
async def test_get_latest_version_unknown_name_returns_none() -> None:
    store = InMemoryArtifactStore()
    assert (
        await store.get_latest_version(
            tenant_id=uuid4(), user_id=uuid4(), agent_key=_AGENT_KEY, name="missing"
        )
        is None
    )


@pytest.mark.asyncio
async def test_soft_delete_hides_from_list_and_get() -> None:
    store = InMemoryArtifactStore()
    tenant_id, user_id = uuid4(), uuid4()
    await store.save_version(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=_AGENT_KEY,
        name="report.md",
        kind="document",
        path_in_workspace="report.md",
        created_in_thread="t-1",
    )
    now = datetime.now(UTC)
    hit = await store.soft_delete(
        tenant_id=tenant_id, user_id=user_id, agent_key=_AGENT_KEY, name="report.md", now=now
    )
    assert hit is True
    # Default list hides; include_deleted=True reveals.
    assert (
        await store.list_for_user(tenant_id=tenant_id, user_id=user_id, agent_key=_AGENT_KEY) == []
    )
    deleted = await store.list_for_user(
        tenant_id=tenant_id, user_id=user_id, agent_key=_AGENT_KEY, include_deleted=True
    )
    assert len(deleted) == 1
    assert deleted[0].deleted_at == now
    # get_latest_version hides soft-deleted (404-equivalent).
    assert (
        await store.get_latest_version(
            tenant_id=tenant_id, user_id=user_id, agent_key=_AGENT_KEY, name="report.md"
        )
        is None
    )


@pytest.mark.asyncio
async def test_soft_delete_idempotent_and_cross_user_safe() -> None:
    store = InMemoryArtifactStore()
    tenant_id, user_x, user_y = uuid4(), uuid4(), uuid4()
    await store.save_version(
        tenant_id=tenant_id,
        user_id=user_x,
        agent_key=_AGENT_KEY,
        name="report.md",
        kind="document",
        path_in_workspace="report.md",
        created_in_thread="t-1",
    )
    now = datetime.now(UTC)
    # Cross-user soft-delete misses (same 404 hiding rule).
    miss_cross_user = await store.soft_delete(
        tenant_id=tenant_id, user_id=user_y, agent_key=_AGENT_KEY, name="report.md", now=now
    )
    assert miss_cross_user is False
    # First soft-delete hits.
    assert await store.soft_delete(
        tenant_id=tenant_id, user_id=user_x, agent_key=_AGENT_KEY, name="report.md", now=now
    )
    # Second soft-delete misses (idempotent).
    assert not await store.soft_delete(
        tenant_id=tenant_id, user_id=user_x, agent_key=_AGENT_KEY, name="report.md", now=now
    )


@pytest.mark.asyncio
async def test_save_version_undeletes_soft_deleted_row() -> None:
    """Mini-ADR J-25 — a re-save on a soft-deleted name un-deletes it."""
    store = InMemoryArtifactStore()
    tenant_id, user_id = uuid4(), uuid4()
    await store.save_version(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=_AGENT_KEY,
        name="report.md",
        kind="document",
        path_in_workspace="v1.md",
        created_in_thread="t-1",
    )
    await store.soft_delete(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=_AGENT_KEY,
        name="report.md",
        now=datetime.now(UTC),
    )
    # Re-save un-deletes; latest_version bumps to 2.
    v2 = await store.save_version(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=_AGENT_KEY,
        name="report.md",
        kind="document",
        path_in_workspace="v2.md",
        created_in_thread="t-2",
    )
    assert v2.version == 2
    artifacts = await store.list_for_user(
        tenant_id=tenant_id, user_id=user_id, agent_key=_AGENT_KEY
    )
    assert len(artifacts) == 1
    assert artifacts[0].deleted_at is None
    assert artifacts[0].latest_version == 2


@pytest.mark.asyncio
async def test_list_expired_returns_soft_deleted_past_horizon() -> None:
    store = InMemoryArtifactStore()
    tenant_id, user_id = uuid4(), uuid4()
    # Two artifacts; soft-delete both at different times.
    for name in ("old.md", "new.md"):
        await store.save_version(
            tenant_id=tenant_id,
            user_id=user_id,
            agent_key=_AGENT_KEY,
            name=name,
            kind="document",
            path_in_workspace=name,
            created_in_thread="t",
        )
    old_time = datetime.now(UTC) - timedelta(days=70)
    recent_time = datetime.now(UTC) - timedelta(days=10)
    await store.soft_delete(
        tenant_id=tenant_id, user_id=user_id, agent_key=_AGENT_KEY, name="old.md", now=old_time
    )
    await store.soft_delete(
        tenant_id=tenant_id, user_id=user_id, agent_key=_AGENT_KEY, name="new.md", now=recent_time
    )
    # Cutoff 60 days ago → only ``old.md`` is past horizon.
    cutoff = datetime.now(UTC) - timedelta(days=60)
    expired = await store.list_expired(before=cutoff)
    assert [a.name for a in expired] == ["old.md"]


@pytest.mark.asyncio
async def test_list_active_past_retention_picks_stale_active_only() -> None:
    store = InMemoryArtifactStore()
    tenant_id, user_id = uuid4(), uuid4()
    v = await store.save_version(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=_AGENT_KEY,
        name="stale.md",
        kind="document",
        path_in_workspace="stale.md",
        created_in_thread="t-old",
    )
    # Hand-tune updated_at into the past.
    artifacts = await store.list_for_user(
        tenant_id=tenant_id, user_id=user_id, agent_key=_AGENT_KEY
    )
    stale = artifacts[0]
    backdated = stale.model_copy(update={"updated_at": datetime.now(UTC) - timedelta(days=100)})
    store._artifacts[stale.id] = backdated
    # B-63 —— the row's clock is its versions' last use: backdate the version too.
    store._versions = [
        x.model_copy(update={"created_at": datetime.now(UTC) - timedelta(days=100)})
        if x.id == v.id
        else x
        for x in store._versions
    ]
    # Also create a fresh active row.
    await store.save_version(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=_AGENT_KEY,
        name="fresh.md",
        kind="document",
        path_in_workspace="fresh.md",
        created_in_thread="t-new",
    )
    rows = await store.list_active_past_retention(now=datetime.now(UTC), default_days=90)
    assert [a.name for a in rows] == ["stale.md"]
    assert v.version == 1  # unchanged sanity


@pytest.mark.asyncio
async def test_hard_delete_removes_artifact_and_versions() -> None:
    store = InMemoryArtifactStore()
    tenant_id, user_id = uuid4(), uuid4()
    v = await store.save_version(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=_AGENT_KEY,
        name="report.md",
        kind="document",
        path_in_workspace="v1.md",
        created_in_thread="t-1",
    )
    await store.save_version(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=_AGENT_KEY,
        name="report.md",
        kind="document",
        path_in_workspace="v2.md",
        created_in_thread="t-2",
    )
    artifacts = await store.list_for_user(
        tenant_id=tenant_id, user_id=user_id, agent_key=_AGENT_KEY
    )
    assert len(artifacts) == 1
    artifact_id = artifacts[0].id
    removed = await store.hard_delete(artifact_ids=[artifact_id])
    assert removed == 1
    # Subsequent list returns empty; versions table also cleared.
    assert (
        await store.list_for_user(tenant_id=tenant_id, user_id=user_id, agent_key=_AGENT_KEY) == []
    )
    assert (
        await store.list_for_user(
            tenant_id=tenant_id, user_id=user_id, agent_key=_AGENT_KEY, include_deleted=True
        )
        == []
    )
    assert v.version == 1  # sanity


@pytest.mark.asyncio
async def test_hard_delete_empty_input_is_zero() -> None:
    store = InMemoryArtifactStore()
    assert await store.hard_delete(artifact_ids=[]) == 0


@pytest.mark.asyncio
async def test_update_kind_returns_updated_row() -> None:
    store = InMemoryArtifactStore()
    tenant_id, user_id = uuid4(), uuid4()
    await store.save_version(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=_AGENT_KEY,
        name="report.md",
        kind="document",
        path_in_workspace="report.md",
        created_in_thread="t-1",
    )
    updated = await store.update_kind(
        tenant_id=tenant_id,
        user_id=user_id,
        artifact_id=await _only_id(store, tenant_id, user_id, "report.md"),
        kind="code",
    )
    assert updated is not None
    assert updated.kind == "code"


@pytest.mark.asyncio
async def test_update_kind_hides_soft_deleted_and_cross_user() -> None:
    store = InMemoryArtifactStore()
    tenant_id, user_x, user_y = uuid4(), uuid4(), uuid4()
    await store.save_version(
        tenant_id=tenant_id,
        user_id=user_x,
        agent_key=_AGENT_KEY,
        name="report.md",
        kind="document",
        path_in_workspace="report.md",
        created_in_thread="t-1",
    )
    artifact_id = await _only_id(store, tenant_id, user_x, "report.md")
    # Cross-user → None:id 是真的,但不属于 user_y。
    assert (
        await store.update_kind(
            tenant_id=tenant_id, user_id=user_y, artifact_id=artifact_id, kind="code"
        )
        is None
    )
    # Soft-delete then try → None (hidden).
    await store.soft_delete(
        tenant_id=tenant_id,
        user_id=user_x,
        agent_key=_AGENT_KEY,
        name="report.md",
        now=datetime.now(UTC),
    )
    assert (
        await store.update_kind(
            tenant_id=tenant_id, user_id=user_x, artifact_id=artifact_id, kind="code"
        )
        is None
    )


@pytest.mark.asyncio
async def test_list_versions_returns_desc_or_none() -> None:
    store = InMemoryArtifactStore()
    tenant_id, user_id = uuid4(), uuid4()
    await store.save_version(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=_AGENT_KEY,
        name="report.md",
        kind="document",
        path_in_workspace="v1.md",
        created_in_thread="t-1",
    )
    await store.save_version(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=_AGENT_KEY,
        name="report.md",
        kind="document",
        path_in_workspace="v2.md",
        created_in_thread="t-2",
    )
    artifact_id = await _only_id(store, tenant_id, user_id, "report.md")
    versions = await store.list_versions(
        tenant_id=tenant_id, user_id=user_id, artifact_id=artifact_id
    )
    assert versions is not None
    assert [v.version for v in versions] == [2, 1]
    # Unknown id → None.
    assert (
        await store.list_versions(tenant_id=tenant_id, user_id=user_id, artifact_id=uuid4()) is None
    )
    # Soft-deleted → None.
    await store.soft_delete(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=_AGENT_KEY,
        name="report.md",
        now=datetime.now(UTC),
    )
    assert (
        await store.list_versions(tenant_id=tenant_id, user_id=user_id, artifact_id=artifact_id)
        is None
    )


@pytest.mark.asyncio
async def test_set_version_digest_backfills_size_and_sha() -> None:
    store = InMemoryArtifactStore()
    tenant_id, user_id = uuid4(), uuid4()
    version = await store.save_version(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=_AGENT_KEY,
        name="data.bin",
        kind="data",
        path_in_workspace="data.bin",
        created_in_thread="t-1",
    )
    assert version.size_bytes is None

    await store.set_version_digest(version_id=version.id, size_bytes=128, sha256="abc123")

    latest = await store.get_latest_version(
        tenant_id=tenant_id, user_id=user_id, agent_key=_AGENT_KEY, name="data.bin"
    )
    assert latest is not None
    assert latest.size_bytes == 128
    assert latest.sha256 == "abc123"


# ---------------------------------------------------------------------------
# 留存链 B-28 —— 按版本 90 天:list_versions_expired / delete_versions /
# mark_expired_if_versionless / list_versions_by_artifact
# ---------------------------------------------------------------------------


async def _seed_versions(store: InMemoryArtifactStore, *, name: str, ages_days: list[int]) -> UUID:
    tenant_id, user_id = uuid4(), uuid4()
    artifact_id: UUID | None = None
    for i, age in enumerate(ages_days, start=1):
        v = await store.save_version(
            tenant_id=tenant_id,
            user_id=user_id,
            agent_key=_AGENT_KEY,
            name=name,
            kind="document",
            path_in_workspace=f"{name}.v{i}",
            created_in_thread="t",
        )
        artifact_id = v.artifact_id
        backdated = datetime.now(UTC) - timedelta(days=age)
        store._versions = [
            x.model_copy(update={"created_at": backdated}) if x.id == v.id else x
            for x in store._versions
        ]
    assert artifact_id is not None
    return artifact_id


@pytest.mark.asyncio
async def test_list_versions_expired_is_per_version_oldest_first() -> None:
    store = InMemoryArtifactStore()
    a = await _seed_versions(store, name="a.md", ages_days=[120, 100, 5])
    b = await _seed_versions(store, name="b.md", ages_days=[3])
    now = datetime.now(UTC)

    expired = await store.list_versions_expired(now=now, default_days=90)

    assert [(e.version.artifact_id, e.version.version) for e in expired] == [(a, 1), (a, 2)]
    assert b not in {e.version.artifact_id for e in expired}
    assert {e.retention_days for e in expired} == {90}
    assert await store.list_versions_expired(now=now, default_days=90, limit=1) == expired[:1]


@pytest.mark.asyncio
async def test_delete_versions_then_mark_expired_only_when_none_left() -> None:
    store = InMemoryArtifactStore()
    a = await _seed_versions(store, name="a.md", ages_days=[120, 5])
    now = datetime.now(UTC)
    versions = await store.list_versions_by_artifact(artifact_id=a)
    assert [v.version for v in versions] == [2, 1]

    assert await store.delete_versions(version_ids=[versions[1].id]) == 1
    assert await store.delete_versions(version_ids=[versions[1].id]) == 0  # idempotent
    assert await store.mark_expired_if_versionless(artifact_id=a, now=now) is False
    assert [v.version for v in await store.list_versions_by_artifact(artifact_id=a)] == [2]

    assert await store.delete_versions(version_ids=[versions[0].id]) == 1
    assert await store.mark_expired_if_versionless(artifact_id=a, now=now) is True
    assert await store.mark_expired_if_versionless(artifact_id=a, now=now) is False  # already
    assert await store.mark_expired_if_versionless(artifact_id=uuid4(), now=now) is False

    row = next(iter(store._artifacts.values()))
    assert row.deleted_at == now
    # list_versions hides the soft-deleted parent; list_versions_by_artifact does not.
    assert (
        await store.list_versions(tenant_id=row.tenant_id, user_id=row.user_id, artifact_id=a)
        is None
    )
    assert await store.list_versions_by_artifact(artifact_id=a) == []


@pytest.mark.asyncio
async def test_list_versions_expired_orders_same_timestamp_by_version() -> None:
    """Same tiebreak as the SQL store: identical ``created_at`` → ``version``
    ascending, never the (random) row id. Ids are forced into reverse
    version order so an ``id`` tiebreak is deterministically wrong."""
    store = InMemoryArtifactStore()
    a = await _seed_versions(store, name="tie.md", ages_days=[100, 100, 100])
    reversed_ids = [
        UUID("ffffffff-0000-4000-8000-000000000001"),
        UUID("88888888-0000-4000-8000-000000000002"),
        UUID("00000000-0000-4000-8000-000000000003"),
    ]
    same_ts = datetime.now(UTC) - timedelta(days=100)
    store._versions = [
        v.model_copy(update={"id": reversed_ids[v.version - 1], "created_at": same_ts})
        for v in store._versions
    ]
    expired = await store.list_versions_expired(now=datetime.now(UTC), default_days=90)

    assert [(e.version.artifact_id, e.version.version) for e in expired] == [
        (a, 1),
        (a, 2),
        (a, 3),
    ]


# ---------------------------------------------------------------------------
# B-63 —— 按最后使用到期:旧版本按自己的时钟,最新版与产物行按产物的时钟(各版本最晚的)
# ---------------------------------------------------------------------------


def _used(store: InMemoryArtifactStore, artifact_id: UUID, version: int, days_ago: int) -> None:
    at = datetime.now(UTC) - timedelta(days=days_ago)
    store._versions = [
        x.model_copy(update={"last_used_at": at})
        if x.artifact_id == artifact_id and x.version == version
        else x
        for x in store._versions
    ]


@pytest.mark.asyncio
async def test_a_recently_used_old_version_keeps_itself_and_the_latest() -> None:
    """v1 200 天前登记、10 天前还下载过;v2(最新)120 天前登记、没人用过。v1 自己没到期;
    v2 是最新版,按整条产物最晚的时钟(v1 的使用)也没到期;产物行同样没到期。"""
    store = InMemoryArtifactStore()
    a = await _seed_versions(store, name="kept.md", ages_days=[200, 120])
    _used(store, a, 1, days_ago=10)
    now = datetime.now(UTC)

    assert await store.list_versions_expired(now=now, default_days=90) == []
    assert await store.list_active_past_retention(now=now, default_days=90) == []


@pytest.mark.asyncio
async def test_an_unused_old_version_expires_while_a_used_latest_keeps_the_row() -> None:
    store = InMemoryArtifactStore()
    a = await _seed_versions(store, name="mixed.md", ages_days=[200, 120])
    _used(store, a, 2, days_ago=10)
    now = datetime.now(UTC)

    expired = await store.list_versions_expired(now=now, default_days=90)
    assert [(e.version.artifact_id, e.version.version) for e in expired] == [(a, 1)]
    assert await store.list_active_past_retention(now=now, default_days=90) == []


@pytest.mark.asyncio
async def test_the_row_and_its_latest_version_expire_together() -> None:
    store = InMemoryArtifactStore()
    a = await _seed_versions(store, name="gone.md", ages_days=[200, 120])
    _used(store, a, 1, days_ago=95)
    now = datetime.now(UTC)

    expired = await store.list_versions_expired(now=now, default_days=90)
    assert [(e.version.artifact_id, e.version.version) for e in expired] == [(a, 1), (a, 2)]
    assert [r.id for r in await store.list_active_past_retention(now=now, default_days=90)] == [a]


@pytest.mark.asyncio
async def test_touch_version_writes_at_most_once_a_day() -> None:
    store = InMemoryArtifactStore()
    a = await _seed_versions(store, name="t.md", ages_days=[5])
    version = (await store.list_versions_by_artifact(artifact_id=a))[0]
    now = datetime.now(UTC)

    assert await store.touch_version(version_id=version.id, now=now) is True
    assert await store.touch_version(version_id=version.id, now=now + timedelta(hours=23)) is False
    after = (await store.list_versions_by_artifact(artifact_id=a))[0]
    assert after.last_used_at == now
    next_day = now + timedelta(days=1, minutes=1)
    assert await store.touch_version(version_id=version.id, now=next_day) is True
    assert await store.touch_version(version_id=uuid4(), now=next_day) is False
