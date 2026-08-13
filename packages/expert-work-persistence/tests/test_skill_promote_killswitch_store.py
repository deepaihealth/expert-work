"""Stream SE (SE-8-1) — promote-approval flow + kill-switch (in-memory backend).

Covers ``request/approve/reject/get/list_promote_requests`` (SE-A13b), the
persistent kill-switch ``get/set_kill_switch`` + ``is_evolution_halted``
(SE-A13c), and the new ``list_skills`` visibility / owner filters. SQL parity +
RLS live in the integration suite (``test_sql_skill_evolution_store.py``).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from expert_work.persistence.skill import InMemorySkillStore
from expert_work.persistence.skill.base import (
    DuplicatePromoteRequestError,
    PromoteRequestNotFoundError,
    SkillNotFoundError,
)


async def _agent_private_skill(
    store: InMemorySkillStore,
    tenant_id: UUID,
    *,
    name: str = "researcher",
    user_id: UUID | None = None,
) -> UUID:
    skill_id = uuid4()
    await store.create_skill(
        skill_id=skill_id,
        tenant_id=tenant_id,
        name=name,
        visibility="agent_private",
        created_by_user_id=user_id or uuid4(),
        created_by_agent_name="researcher",
    )
    await store.add_version(
        version_id=uuid4(),
        skill_id=skill_id,
        tenant_id=tenant_id,
        prompt_fragment="do the thing",
        authored_by="agent",
        evolution_origin="in_session",
    )
    return skill_id


# ── request_skill_promote ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_promote_creates_pending() -> None:
    store = InMemorySkillStore()
    tid = uuid4()
    sid = await _agent_private_skill(store, tid)
    req = await store.request_skill_promote(
        request_id=uuid4(),
        tenant_id=tid,
        skill_id=sid,
        skill_version=1,
        requested_by_user_id=uuid4(),
        requested_by_agent_name="researcher",
        reason="useful tenant-wide",
    )
    assert req.status == "pending"
    assert req.skill_id == sid and req.skill_version == 1
    assert req.reason == "useful tenant-wide"


@pytest.mark.asyncio
async def test_request_promote_unknown_skill_raises() -> None:
    store = InMemorySkillStore()
    with pytest.raises(SkillNotFoundError):
        await store.request_skill_promote(
            request_id=uuid4(), tenant_id=uuid4(), skill_id=uuid4(), skill_version=1
        )


@pytest.mark.asyncio
async def test_request_promote_duplicate_pending_raises() -> None:
    store = InMemorySkillStore()
    tid = uuid4()
    sid = await _agent_private_skill(store, tid)
    await store.request_skill_promote(
        request_id=uuid4(), tenant_id=tid, skill_id=sid, skill_version=1
    )
    with pytest.raises(DuplicatePromoteRequestError):
        await store.request_skill_promote(
            request_id=uuid4(), tenant_id=tid, skill_id=sid, skill_version=1
        )


# ── approve / reject ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_flips_visibility_and_status() -> None:
    store = InMemorySkillStore()
    tid, admin = uuid4(), uuid4()
    sid = await _agent_private_skill(store, tid)
    rid = uuid4()
    await store.request_skill_promote(request_id=rid, tenant_id=tid, skill_id=sid, skill_version=1)
    decided = await store.approve_skill_promote(
        request_id=rid, tenant_id=tid, decided_by_user_id=admin, decision_reason="ok"
    )
    assert decided.status == "approved"
    assert decided.decided_by_user_id == admin
    assert decided.decided_at is not None
    skill = await store.get_skill(skill_id=sid, tenant_id=tid)
    assert skill is not None and skill.visibility == "tenant"


@pytest.mark.asyncio
async def test_reject_keeps_agent_private() -> None:
    store = InMemorySkillStore()
    tid, admin = uuid4(), uuid4()
    sid = await _agent_private_skill(store, tid)
    rid = uuid4()
    await store.request_skill_promote(request_id=rid, tenant_id=tid, skill_id=sid, skill_version=1)
    decided = await store.reject_skill_promote(
        request_id=rid, tenant_id=tid, decided_by_user_id=admin, decision_reason="no"
    )
    assert decided.status == "rejected"
    skill = await store.get_skill(skill_id=sid, tenant_id=tid)
    assert skill is not None and skill.visibility == "agent_private"


@pytest.mark.asyncio
async def test_decide_unknown_request_raises() -> None:
    store = InMemorySkillStore()
    with pytest.raises(PromoteRequestNotFoundError):
        await store.approve_skill_promote(
            request_id=uuid4(), tenant_id=uuid4(), decided_by_user_id=uuid4()
        )


@pytest.mark.asyncio
async def test_decide_non_pending_raises() -> None:
    store = InMemorySkillStore()
    tid = uuid4()
    sid = await _agent_private_skill(store, tid)
    rid = uuid4()
    await store.request_skill_promote(request_id=rid, tenant_id=tid, skill_id=sid, skill_version=1)
    await store.approve_skill_promote(request_id=rid, tenant_id=tid, decided_by_user_id=uuid4())
    with pytest.raises(ValueError, match="not pending"):
        await store.reject_skill_promote(request_id=rid, tenant_id=tid, decided_by_user_id=uuid4())


@pytest.mark.asyncio
async def test_get_promote_request_hides_cross_tenant() -> None:
    store = InMemorySkillStore()
    tid, other = uuid4(), uuid4()
    sid = await _agent_private_skill(store, tid)
    rid = uuid4()
    await store.request_skill_promote(request_id=rid, tenant_id=tid, skill_id=sid, skill_version=1)
    assert await store.get_promote_request(request_id=rid, tenant_id=other) is None
    assert await store.get_promote_request(request_id=rid, tenant_id=tid) is not None


# ── list_promote_requests (review queue) ──────────────────────────────────


@pytest.mark.asyncio
async def test_list_promote_requests_filters_status_and_tenant() -> None:
    store = InMemorySkillStore()
    tid_a, tid_b = uuid4(), uuid4()
    sid_a = await _agent_private_skill(store, tid_a, name="a")
    sid_b = await _agent_private_skill(store, tid_b, name="b")
    ra, rb = uuid4(), uuid4()
    await store.request_skill_promote(
        request_id=ra, tenant_id=tid_a, skill_id=sid_a, skill_version=1
    )
    await store.request_skill_promote(
        request_id=rb, tenant_id=tid_b, skill_id=sid_b, skill_version=1
    )
    await store.reject_skill_promote(request_id=ra, tenant_id=tid_a, decided_by_user_id=uuid4())

    pending_a, _ = await store.list_promote_requests(tenant_id=tid_a, status="pending")
    assert pending_a == []
    rejected_a, _ = await store.list_promote_requests(tenant_id=tid_a, status="rejected")
    assert [r.id for r in rejected_a] == [ra]
    # cross-tenant isolation
    all_a, _ = await store.list_promote_requests(tenant_id=tid_a)
    assert all(r.tenant_id == tid_a for r in all_a)


@pytest.mark.asyncio
async def test_list_promote_requests_all_tenants_spans() -> None:
    store = InMemorySkillStore()
    tid_a, tid_b = uuid4(), uuid4()
    sid_a = await _agent_private_skill(store, tid_a, name="a")
    sid_b = await _agent_private_skill(store, tid_b, name="b")
    await store.request_skill_promote(
        request_id=uuid4(), tenant_id=tid_a, skill_id=sid_a, skill_version=1
    )
    await store.request_skill_promote(
        request_id=uuid4(), tenant_id=tid_b, skill_id=sid_b, skill_version=1
    )
    rows, _ = await store.list_promote_requests_all_tenants(status="pending")
    assert {r.tenant_id for r in rows} == {tid_a, tid_b}


# ── list_promote_requests skill_visibility filter (backlog task 8) ────────


@pytest.mark.asyncio
async def test_list_promote_requests_skill_visibility_filters() -> None:
    """``skill_visibility="tenant"`` excludes requests targeting an
    ``agent_private`` skill; the unfiltered call (admin path) still sees
    both."""
    store = InMemorySkillStore()
    tid = uuid4()
    sid_priv = await _agent_private_skill(store, tid, name="priv")
    sid_pub = uuid4()
    # default visibility=tenant
    await store.create_skill(skill_id=sid_pub, tenant_id=tid, name="pub")
    rid_priv, rid_pub = uuid4(), uuid4()
    await store.request_skill_promote(
        request_id=rid_priv, tenant_id=tid, skill_id=sid_priv, skill_version=1
    )
    await store.request_skill_promote(
        request_id=rid_pub, tenant_id=tid, skill_id=sid_pub, skill_version=1
    )

    filtered, _ = await store.list_promote_requests(tenant_id=tid, skill_visibility="tenant")
    assert [r.id for r in filtered] == [rid_pub]

    unfiltered, _ = await store.list_promote_requests(tenant_id=tid)
    assert {r.id for r in unfiltered} == {rid_priv, rid_pub}


@pytest.mark.asyncio
async def test_list_promote_requests_skill_visibility_pagination_not_short() -> None:
    """The filter must run before the keyset page is cut — a filtered page
    must never come back short of ``limit`` while more matching rows exist
    (the post-fetch-filter bug this store-layer approach avoids)."""
    store = InMemorySkillStore()
    tid = uuid4()
    tenant_request_ids: set[UUID] = set()
    # Interleave 3 agent_private + 3 tenant-visible skills, each with a
    # pending promote request, so a naive "filter after fetching a page"
    # implementation would visibly shrink a page below ``limit``.
    for i in range(3):
        sid_priv = await _agent_private_skill(store, tid, name=f"priv-{i}")
        rid = uuid4()
        await store.request_skill_promote(
            request_id=rid, tenant_id=tid, skill_id=sid_priv, skill_version=1
        )
        sid_pub = uuid4()
        await store.create_skill(skill_id=sid_pub, tenant_id=tid, name=f"pub-{i}")
        rid_pub = uuid4()
        await store.request_skill_promote(
            request_id=rid_pub, tenant_id=tid, skill_id=sid_pub, skill_version=1
        )
        tenant_request_ids.add(rid_pub)

    page1, cursor1 = await store.list_promote_requests(
        tenant_id=tid, skill_visibility="tenant", limit=2
    )
    assert len(page1) == 2, "page short of limit while more tenant-visible rows exist"
    assert all(r.id in tenant_request_ids for r in page1)
    assert cursor1 is not None

    page2, cursor2 = await store.list_promote_requests(
        tenant_id=tid, skill_visibility="tenant", limit=2, cursor=cursor1
    )
    assert len(page2) == 1, "second page should hold the one remaining tenant-visible row"
    assert cursor2 is None

    seen = {r.id for r in page1} | {r.id for r in page2}
    assert seen == tenant_request_ids


# ── list_skills filters (SE-8) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_skills_filters_visibility_and_owner() -> None:
    store = InMemorySkillStore()
    tid, user_id = uuid4(), uuid4()
    await _agent_private_skill(store, tid, name="mine", user_id=user_id)
    await _agent_private_skill(store, tid, name="other-agent", user_id=uuid4())
    await store.create_skill(skill_id=uuid4(), tenant_id=tid, name="shared")  # tenant-visible

    private, _ = await store.list_skills(tenant_id=tid, visibility="agent_private")
    assert {s.name for s in private} == {"mine", "other-agent"}

    tenant_vis, _ = await store.list_skills(tenant_id=tid, visibility="tenant")
    assert {s.name for s in tenant_vis} == {"shared"}

    mine, _ = await store.list_skills(
        tenant_id=tid, visibility="agent_private", created_by_user_id=user_id
    )
    assert {s.name for s in mine} == {"mine"}


# ── kill-switch (SE-A13c) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kill_switch_set_get_roundtrip() -> None:
    store = InMemorySkillStore()
    tid, admin = uuid4(), uuid4()
    sw = await store.set_kill_switch(
        switch_id=uuid4(),
        scope="tenant",
        tenant_id=tid,
        engaged=True,
        reason="runaway",
        actor_user_id=admin,
    )
    assert sw.engaged is True and sw.engaged_by_user_id == admin and sw.engaged_at is not None
    got = await store.get_kill_switch(scope="tenant", tenant_id=tid)
    assert got is not None and got.id == sw.id and got.engaged is True


@pytest.mark.asyncio
async def test_kill_switch_upsert_toggles_in_place() -> None:
    store = InMemorySkillStore()
    tid, admin = uuid4(), uuid4()
    first = await store.set_kill_switch(
        switch_id=uuid4(), scope="tenant", tenant_id=tid, engaged=True, actor_user_id=admin
    )
    released = await store.set_kill_switch(
        switch_id=uuid4(), scope="tenant", tenant_id=tid, engaged=False, actor_user_id=admin
    )
    # Same row updated, not a second one.
    assert released.id == first.id
    assert released.engaged is False and released.released_at is not None
    rows = [s for s in store._kill_switches if s.scope == "tenant" and s.tenant_id == tid]
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_is_evolution_halted_global_and_tenant() -> None:
    store = InMemorySkillStore()
    tid, other = uuid4(), uuid4()
    # Nothing engaged.
    assert await store.is_evolution_halted(tenant_id=tid) is False
    # Tenant switch only affects that tenant.
    await store.set_kill_switch(switch_id=uuid4(), scope="tenant", tenant_id=tid, engaged=True)
    assert await store.is_evolution_halted(tenant_id=tid) is True
    assert await store.is_evolution_halted(tenant_id=other) is False
    # Global switch halts everyone.
    await store.set_kill_switch(switch_id=uuid4(), scope="global", tenant_id=None, engaged=True)
    assert await store.is_evolution_halted(tenant_id=other) is True


@pytest.mark.asyncio
async def test_is_evolution_halted_ignores_released() -> None:
    store = InMemorySkillStore()
    tid = uuid4()
    await store.set_kill_switch(switch_id=uuid4(), scope="global", tenant_id=None, engaged=True)
    await store.set_kill_switch(switch_id=uuid4(), scope="global", tenant_id=None, engaged=False)
    assert await store.is_evolution_halted(tenant_id=tid) is False
