"""Unit tests for InMemoryThreadMetaStore — Repository contract."""

from __future__ import annotations

from uuid import uuid4

import pytest

from expert_work.persistence import InMemoryThreadMetaStore
from expert_work.protocol import ThreadStatus


@pytest.mark.asyncio
async def test_create_and_get_round_trip() -> None:
    store = InMemoryThreadMetaStore()
    thread_id, tenant_id = uuid4(), uuid4()

    created = await store.create(
        thread_id=thread_id,
        tenant_id=tenant_id,
        created_by="user-1",
        agent_name="demo",
        agent_version="0.1.0",
    )
    assert created.thread_id == thread_id
    assert created.status == ThreadStatus.ACTIVE
    assert created.agent_version == "0.1.0"

    fetched = await store.get(thread_id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.created_by == "user-1"


@pytest.mark.asyncio
async def test_get_filters_by_tenant() -> None:
    store = InMemoryThreadMetaStore()
    thread_id, owner, other = uuid4(), uuid4(), uuid4()
    await store.create(thread_id=thread_id, tenant_id=owner, created_by="x")

    assert await store.get(thread_id, tenant_id=other) is None
    assert await store.get(thread_id, tenant_id=owner) is not None


@pytest.mark.asyncio
async def test_user_id_round_trip_and_list_filter() -> None:
    store = InMemoryThreadMetaStore()
    tenant_id, user_a, user_b = uuid4(), uuid4(), uuid4()

    t_a = await store.create(thread_id=uuid4(), tenant_id=tenant_id, created_by="x", user_id=user_a)
    assert t_a.user_id == user_a
    await store.create(thread_id=uuid4(), tenant_id=tenant_id, created_by="x", user_id=user_b)
    # A thread with no owner (machine-triggered) keeps user_id None.
    unowned = await store.create(thread_id=uuid4(), tenant_id=tenant_id, created_by="x")
    assert unowned.user_id is None

    only_a = await store.list_by_tenant(tenant_id, user_id=user_a)
    assert [m.user_id for m in only_a] == [user_a]
    # No user filter → all three threads.
    assert len(await store.list_by_tenant(tenant_id)) == 3


@pytest.mark.asyncio
async def test_create_rejects_duplicate_thread_id() -> None:
    store = InMemoryThreadMetaStore()
    thread_id, tenant_id = uuid4(), uuid4()
    await store.create(thread_id=thread_id, tenant_id=tenant_id, created_by="x")
    with pytest.raises(ValueError, match="already exists"):
        await store.create(thread_id=thread_id, tenant_id=tenant_id, created_by="x")


@pytest.mark.asyncio
async def test_list_by_tenant_pagination_and_status_filter() -> None:
    store = InMemoryThreadMetaStore()
    tenant_id = uuid4()
    threads = [uuid4() for _ in range(5)]
    for t in threads:
        await store.create(thread_id=t, tenant_id=tenant_id, created_by="x")

    await store.update_status(threads[0], ThreadStatus.COMPLETED, tenant_id=tenant_id)

    all_active = await store.list_by_tenant(tenant_id, status=ThreadStatus.ACTIVE)
    assert len(all_active) == 4

    page = await store.list_by_tenant(tenant_id, limit=2, offset=0)
    assert len(page) == 2


@pytest.mark.asyncio
async def test_update_status_returns_true_only_on_match() -> None:
    store = InMemoryThreadMetaStore()
    thread_id, owner, other = uuid4(), uuid4(), uuid4()
    await store.create(thread_id=thread_id, tenant_id=owner, created_by="x")

    # Bind awaited mutations before asserting; `assert await foo()` is stripped
    # under `python -O` and the side effect would disappear silently.
    owner_match = await store.update_status(thread_id, ThreadStatus.PAUSED, tenant_id=owner)
    assert owner_match is True
    other_mismatch = await store.update_status(thread_id, ThreadStatus.PAUSED, tenant_id=other)
    assert other_mismatch is False
    missing = await store.update_status(uuid4(), ThreadStatus.PAUSED, tenant_id=owner)
    assert missing is False


@pytest.mark.asyncio
async def test_check_access_and_delete() -> None:
    store = InMemoryThreadMetaStore()
    thread_id, owner, other = uuid4(), uuid4(), uuid4()
    await store.create(thread_id=thread_id, tenant_id=owner, created_by="x")

    owner_access = await store.check_access(thread_id, owner)
    assert owner_access is True
    other_access = await store.check_access(thread_id, other)
    assert other_access is False

    wrong_tenant_delete = await store.delete(thread_id, tenant_id=other)
    assert wrong_tenant_delete is False
    still_accessible = await store.check_access(thread_id, owner)
    assert still_accessible is True

    owner_delete = await store.delete(thread_id, tenant_id=owner)
    assert owner_delete is True
    gone = await store.check_access(thread_id, owner)
    assert gone is False


# ---------------------------------------------------------------------------
# Stream H.6 (Mini-ADR H-10) — agent_name / agent_version list filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_by_tenant_agent_filters() -> None:
    store = InMemoryThreadMetaStore()
    tenant_id = uuid4()
    await store.create(
        thread_id=uuid4(),
        tenant_id=tenant_id,
        created_by="x",
        agent_name="reporter",
        agent_version="1.0.0",
    )
    await store.create(
        thread_id=uuid4(),
        tenant_id=tenant_id,
        created_by="x",
        agent_name="reporter",
        agent_version="2.0.0",
    )
    await store.create(
        thread_id=uuid4(),
        tenant_id=tenant_id,
        created_by="x",
        agent_name="scribe",
        agent_version="1.0.0",
    )
    # Machine thread with no agent binding stays out of any agent filter.
    await store.create(thread_id=uuid4(), tenant_id=tenant_id, created_by="x")

    by_name = await store.list_by_tenant(tenant_id, agent_name="reporter")
    assert {m.agent_version for m in by_name} == {"1.0.0", "2.0.0"}

    by_name_version = await store.list_by_tenant(
        tenant_id, agent_name="reporter", agent_version="2.0.0"
    )
    assert [m.agent_version for m in by_name_version] == ["2.0.0"]

    assert await store.list_by_tenant(tenant_id, agent_name="ghost") == []
    # No filter → all four threads (regression).
    assert len(await store.list_by_tenant(tenant_id)) == 4


@pytest.mark.asyncio
async def test_list_all_tenants_agent_filters() -> None:
    store = InMemoryThreadMetaStore()
    await store.create(thread_id=uuid4(), tenant_id=uuid4(), created_by="x", agent_name="reporter")
    await store.create(thread_id=uuid4(), tenant_id=uuid4(), created_by="x", agent_name="scribe")

    cross = await store.list_all_tenants(agent_name="reporter")
    assert [m.agent_name for m in cross] == ["reporter"]
    assert len(await store.list_all_tenants()) == 2


# ---------------------------------------------------------------------------
# Session-history uplift — title (auto/rename), search, archive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_title_round_trip_and_tenant_scope() -> None:
    store = InMemoryThreadMetaStore()
    thread_id, owner, other = uuid4(), uuid4(), uuid4()
    created = await store.create(thread_id=thread_id, tenant_id=owner, created_by="x")
    assert created.title is None

    wrong = await store.update_title(thread_id, "hijack", tenant_id=other)
    assert wrong is False
    ok = await store.update_title(thread_id, "帮我写季度报告", tenant_id=owner)
    assert ok is True

    fetched = await store.get(thread_id, tenant_id=owner)
    assert fetched is not None
    assert fetched.title == "帮我写季度报告"

    missing = await store.update_title(uuid4(), "ghost", tenant_id=owner)
    assert missing is False


@pytest.mark.asyncio
async def test_list_q_matches_title_case_insensitive() -> None:
    store = InMemoryThreadMetaStore()
    tenant_id = uuid4()
    t_report = await store.create(thread_id=uuid4(), tenant_id=tenant_id, created_by="x")
    t_weather = await store.create(thread_id=uuid4(), tenant_id=tenant_id, created_by="x")
    await store.update_title(t_report.thread_id, "Quarterly Report", tenant_id=tenant_id)
    await store.update_title(t_weather.thread_id, "今天天气", tenant_id=tenant_id)
    # Untitled thread never matches a search.
    await store.create(thread_id=uuid4(), tenant_id=tenant_id, created_by="x")

    hit = await store.list_by_tenant(tenant_id, q="report")
    assert [m.thread_id for m in hit] == [t_report.thread_id]
    assert await store.list_by_tenant(tenant_id, q="天气") == [
        await store.get(t_weather.thread_id, tenant_id=tenant_id)
    ]
    assert await store.list_by_tenant(tenant_id, q="nonexistent") == []


@pytest.mark.asyncio
async def test_archived_excluded_by_default_but_reachable() -> None:
    store = InMemoryThreadMetaStore()
    tenant_id = uuid4()
    keep = await store.create(thread_id=uuid4(), tenant_id=tenant_id, created_by="x")
    archived = await store.create(thread_id=uuid4(), tenant_id=tenant_id, created_by="x")
    await store.update_status(archived.thread_id, ThreadStatus.ARCHIVED, tenant_id=tenant_id)

    default = await store.list_by_tenant(tenant_id)
    assert [m.thread_id for m in default] == [keep.thread_id]

    with_archived = await store.list_by_tenant(tenant_id, include_archived=True)
    assert {m.thread_id for m in with_archived} == {keep.thread_id, archived.thread_id}

    only_archived = await store.list_by_tenant(tenant_id, status=ThreadStatus.ARCHIVED)
    assert [m.thread_id for m in only_archived] == [archived.thread_id]

    # Cross-tenant listing applies the same default exclusion.
    cross_default = await store.list_all_tenants()
    assert archived.thread_id not in {m.thread_id for m in cross_default}
    cross_all = await store.list_all_tenants(include_archived=True)
    assert archived.thread_id in {m.thread_id for m in cross_all}


# ---------------------------------------------------------------------------
# Conversation browser pager — count mirrors the list filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_mirrors_list_filters() -> None:
    store = InMemoryThreadMetaStore()
    tenant_id, user_a = uuid4(), uuid4()
    for i in range(3):
        await store.create(
            thread_id=uuid4(),
            tenant_id=tenant_id,
            created_by="x",
            user_id=user_a if i < 2 else None,
            agent_name="alpha" if i < 2 else "beta",
        )
    other_tenant_thread = await store.create(
        thread_id=uuid4(), tenant_id=uuid4(), created_by="x", agent_name="alpha"
    )

    assert await store.count_by_tenant(tenant_id) == 3
    assert await store.count_by_tenant(tenant_id, agent_name="alpha") == 2
    assert await store.count_by_tenant(tenant_id, user_id=user_a) == 2
    # Count is unaffected by what a page would slice.
    page = await store.list_by_tenant(tenant_id, limit=1)
    assert len(page) == 1
    assert await store.count_by_tenant(tenant_id) == 3

    # thread_ids narrowing composes; the empty set short-circuits to 0.
    assert await store.count_by_tenant(tenant_id, thread_ids={page[0].thread_id}) == 1
    assert await store.count_by_tenant(tenant_id, thread_ids=set()) == 0

    # Cross-tenant count spans tenants and honours the user filter (N-4 fix).
    assert await store.count_all_tenants() == 4
    assert await store.count_all_tenants(user_id=user_a) == 2
    assert other_tenant_thread.tenant_id != tenant_id


@pytest.mark.asyncio
async def test_list_all_tenants_filters_by_user() -> None:
    """N-4 fix — the cross-tenant list narrows by user_id in the store, so
    the browser's member filter (and its total) is exact, not a post-filter."""
    store = InMemoryThreadMetaStore()
    user = uuid4()
    mine = await store.create(thread_id=uuid4(), tenant_id=uuid4(), created_by="x", user_id=user)
    await store.create(thread_id=uuid4(), tenant_id=uuid4(), created_by="x", user_id=uuid4())

    got = await store.list_all_tenants(user_id=user)
    assert [m.thread_id for m in got] == [mine.thread_id]


# ---------------------------------------------------------------------------
# get_many — batch of get(), keyed by thread_id. Replaces a per-id get() loop
# in the run-list merged view (avoids an N+1 over the page's threads).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_many_returns_owned_subset_keyed_by_thread_id() -> None:
    store = InMemoryThreadMetaStore()
    tenant = uuid4()
    t1, t2 = uuid4(), uuid4()
    await store.create(thread_id=t1, tenant_id=tenant, created_by="x")
    await store.create(thread_id=t2, tenant_id=tenant, created_by="x")
    missing = uuid4()
    got = await store.get_many([t1, t2, missing], tenant_id=tenant)
    assert set(got) == {t1, t2}
    assert got[t1].thread_id == t1


@pytest.mark.asyncio
async def test_get_many_is_tenant_scoped() -> None:
    store = InMemoryThreadMetaStore()
    owner, other = uuid4(), uuid4()
    tid = uuid4()
    await store.create(thread_id=tid, tenant_id=owner, created_by="x")
    # Cross-tenant id is absent — never reveals cross-tenant existence.
    assert await store.get_many([tid], tenant_id=other) == {}


@pytest.mark.asyncio
async def test_get_many_empty_input_returns_empty() -> None:
    store = InMemoryThreadMetaStore()
    assert await store.get_many([], tenant_id=uuid4()) == {}


@pytest.mark.asyncio
async def test_list_agent_names_for_user_is_distinct_and_sorted() -> None:
    """B-50 PR4 —— 「这个用户用过哪些 agent」。

    ``sanitize_agent_key`` 带 sha256 后缀、单向,所以对外要把工作区里的
    ``agents/<key>/`` 和产物行的 ``agent_key`` 还原成 ``agent_code``,只能靠
    这张反查表。取值域必须是**这个用户实际用过的**那些 agent —— 正好是 key
    可能出现的那个集合。
    """
    store = InMemoryThreadMetaStore()
    tenant, user = uuid4(), uuid4()
    for agent in ("plan", "designer", "plan", "plan"):
        await store.create(
            thread_id=uuid4(), tenant_id=tenant, created_by="x", user_id=user, agent_name=agent
        )

    assert await store.list_agent_names_for_user(tenant_id=tenant, user_id=user) == [
        "designer",
        "plan",
    ]


@pytest.mark.asyncio
async def test_list_agent_names_for_user_excludes_other_users_and_tenants() -> None:
    store = InMemoryThreadMetaStore()
    tenant, other_tenant = uuid4(), uuid4()
    user, other_user = uuid4(), uuid4()
    await store.create(
        thread_id=uuid4(), tenant_id=tenant, created_by="x", user_id=user, agent_name="mine"
    )
    await store.create(
        thread_id=uuid4(),
        tenant_id=tenant,
        created_by="x",
        user_id=other_user,
        agent_name="other-user",
    )
    await store.create(
        thread_id=uuid4(),
        tenant_id=other_tenant,
        created_by="x",
        user_id=user,
        agent_name="other-tenant",
    )

    assert await store.list_agent_names_for_user(tenant_id=tenant, user_id=user) == ["mine"]


@pytest.mark.asyncio
async def test_list_agent_names_for_user_skips_threads_with_no_agent() -> None:
    """``agent_name`` 可空(``create`` 的默认值)—— 空的不进结果,也不变成 ``None`` 条目。"""
    store = InMemoryThreadMetaStore()
    tenant, user = uuid4(), uuid4()
    await store.create(thread_id=uuid4(), tenant_id=tenant, created_by="x", user_id=user)
    await store.create(
        thread_id=uuid4(), tenant_id=tenant, created_by="x", user_id=user, agent_name="real"
    )

    assert await store.list_agent_names_for_user(tenant_id=tenant, user_id=user) == ["real"]


@pytest.mark.asyncio
async def test_list_agent_names_for_user_includes_archived_threads() -> None:
    """归档不等于「这个 agent 没写过东西」—— 它的文件还在工作区里躺着。

    ``list_by_tenant`` 默认排除 ARCHIVED,这个方法**不能**跟着排除:跟着排
    会让归档会话那个 agent 的文件在 ``scope=user`` 里整片消失。
    """
    store = InMemoryThreadMetaStore()
    tenant, user = uuid4(), uuid4()
    thread_id = uuid4()
    await store.create(
        thread_id=thread_id, tenant_id=tenant, created_by="x", user_id=user, agent_name="archived"
    )
    await store.update_status(thread_id, ThreadStatus.ARCHIVED, tenant_id=tenant)

    assert await store.list_agent_names_for_user(tenant_id=tenant, user_id=user) == ["archived"]
