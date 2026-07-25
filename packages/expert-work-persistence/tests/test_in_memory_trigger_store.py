"""Unit tests for InMemoryTriggerStore — Stream J.10 (Mini-ADR J-26 / J-42)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import InMemoryTriggerRunStore, InMemoryTriggerStore
from expert_work.protocol import (
    TriggerKind,
    TriggerRecord,
    TriggerRunRecord,
    TriggerRunStatus,
)

_BASE = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)


def _record(
    *,
    trigger_id: UUID | None = None,
    tenant_id: UUID | None = None,
    agent_name: str = "reporter",
    name: str = "nightly",
    kind: TriggerKind = "cron",
    enabled: bool = True,
) -> TriggerRecord:
    config: dict[str, object] = {"expr": "0 9 * * *"} if kind == "cron" else {}
    return TriggerRecord(
        id=trigger_id or uuid4(),
        tenant_id=tenant_id or uuid4(),
        agent_name=agent_name,
        agent_version="1.0.0",
        name=name,
        kind=kind,
        config=config,
        enabled=enabled,
        source="api",
        created_at=_BASE,
        updated_at=_BASE,
    )


@pytest.mark.asyncio
async def test_create_then_get_round_trips() -> None:
    store = InMemoryTriggerStore()
    tid, tenant = uuid4(), uuid4()
    await store.create(_record(trigger_id=tid, tenant_id=tenant))

    fetched = await store.get(trigger_id=tid, tenant_id=tenant)
    assert fetched is not None
    assert fetched.id == tid
    assert fetched.kind == "cron"


@pytest.mark.asyncio
async def test_get_unknown_returns_none() -> None:
    store = InMemoryTriggerStore()
    assert await store.get(trigger_id=uuid4(), tenant_id=uuid4()) is None


@pytest.mark.asyncio
async def test_get_cross_tenant_returns_none() -> None:
    store = InMemoryTriggerStore()
    tid, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    await store.create(_record(trigger_id=tid, tenant_id=tenant_a))

    assert await store.get(trigger_id=tid, tenant_id=tenant_b) is None


@pytest.mark.asyncio
async def test_create_duplicate_name_per_agent_raises() -> None:
    store = InMemoryTriggerStore()
    tenant = uuid4()
    await store.create(_record(tenant_id=tenant, agent_name="reporter", name="nightly"))
    with pytest.raises(ValueError, match="already exists"):
        await store.create(_record(tenant_id=tenant, agent_name="reporter", name="nightly"))


@pytest.mark.asyncio
async def test_same_name_different_agent_is_allowed() -> None:
    """The uniqueness key is (tenant, agent, name) — not (tenant, name)."""
    store = InMemoryTriggerStore()
    tenant = uuid4()
    await store.create(_record(tenant_id=tenant, agent_name="reporter", name="nightly"))
    await store.create(_record(tenant_id=tenant, agent_name="auditor", name="nightly"))


@pytest.mark.asyncio
async def test_list_by_agent_filters() -> None:
    store = InMemoryTriggerStore()
    tenant = uuid4()
    await store.create(_record(tenant_id=tenant, agent_name="reporter", name="a"))
    await store.create(_record(tenant_id=tenant, agent_name="reporter", name="b"))
    await store.create(_record(tenant_id=tenant, agent_name="auditor", name="a"))

    listed = await store.list_by_agent(tenant_id=tenant, agent_name="reporter")
    assert {r.name for r in listed} == {"a", "b"}


@pytest.mark.asyncio
async def test_list_enabled_cron_is_cross_tenant_and_kind_filtered() -> None:
    store = InMemoryTriggerStore()
    await store.create(_record(tenant_id=uuid4(), name="c1", kind="cron", enabled=True))
    await store.create(_record(tenant_id=uuid4(), name="c2", kind="cron", enabled=True))
    await store.create(_record(tenant_id=uuid4(), name="c3", kind="cron", enabled=False))
    await store.create(_record(tenant_id=uuid4(), name="w1", kind="webhook"))

    listed = await store.list_enabled_cron()
    assert {r.name for r in listed} == {"c1", "c2"}


@pytest.mark.asyncio
async def test_update_replaces_row() -> None:
    store = InMemoryTriggerStore()
    tid, tenant = uuid4(), uuid4()
    await store.create(_record(trigger_id=tid, tenant_id=tenant, enabled=True))

    rec = await store.get(trigger_id=tid, tenant_id=tenant)
    assert rec is not None
    updated = await store.update(rec.model_copy(update={"enabled": False}))
    assert updated is True

    again = await store.get(trigger_id=tid, tenant_id=tenant)
    assert again is not None
    assert again.enabled is False


@pytest.mark.asyncio
async def test_update_cross_tenant_returns_false() -> None:
    store = InMemoryTriggerStore()
    tid, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    await store.create(_record(trigger_id=tid, tenant_id=tenant_a))

    rec = await store.get(trigger_id=tid, tenant_id=tenant_a)
    assert rec is not None
    impostor = rec.model_copy(update={"tenant_id": tenant_b})
    updated = await store.update(impostor)
    assert updated is False


@pytest.mark.asyncio
async def test_delete() -> None:
    store = InMemoryTriggerStore()
    tid, tenant = uuid4(), uuid4()
    await store.create(_record(trigger_id=tid, tenant_id=tenant))

    deleted = await store.delete(trigger_id=tid, tenant_id=tenant)
    assert deleted is True
    assert await store.get(trigger_id=tid, tenant_id=tenant) is None
    deleted_again = await store.delete(trigger_id=tid, tenant_id=tenant)
    assert deleted_again is False


@pytest.mark.asyncio
async def test_get_for_webhook_is_tenant_unscoped() -> None:
    """The webhook ingest path resolves a trigger by id alone."""
    store = InMemoryTriggerStore()
    tid, tenant = uuid4(), uuid4()
    await store.create(_record(trigger_id=tid, tenant_id=tenant, kind="webhook"))

    found = await store.get_for_webhook(trigger_id=tid)
    assert found is not None
    assert found.tenant_id == tenant
    assert await store.get_for_webhook(trigger_id=uuid4()) is None


# --- InMemoryTriggerRunStore ----------------------------------------------


def _run_record(
    *,
    run_record_id: UUID | None = None,
    tenant_id: UUID | None = None,
    trigger_id: UUID | None = None,
    status: TriggerRunStatus = TriggerRunStatus.FIRED,
    triggered_at: datetime = _BASE,
    next_retry_at: datetime | None = None,
) -> TriggerRunRecord:
    return TriggerRunRecord(
        id=run_record_id or uuid4(),
        tenant_id=tenant_id or uuid4(),
        trigger_id=trigger_id or uuid4(),
        run_id=uuid4(),
        status=status,
        attempt=1,
        next_retry_at=next_retry_at,
        triggered_at=triggered_at,
    )


@pytest.mark.asyncio
async def test_run_store_create_then_get() -> None:
    store = InMemoryTriggerRunStore()
    rid, tenant = uuid4(), uuid4()
    await store.create(_run_record(run_record_id=rid, tenant_id=tenant))

    fetched = await store.get(trigger_run_id=rid, tenant_id=tenant)
    assert fetched is not None
    assert fetched.id == rid
    assert fetched.status is TriggerRunStatus.FIRED


@pytest.mark.asyncio
async def test_run_store_get_unknown_and_cross_tenant() -> None:
    store = InMemoryTriggerRunStore()
    rid, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    await store.create(_run_record(run_record_id=rid, tenant_id=tenant_a))

    assert await store.get(trigger_run_id=uuid4(), tenant_id=tenant_a) is None
    assert await store.get(trigger_run_id=rid, tenant_id=tenant_b) is None


@pytest.mark.asyncio
async def test_run_store_create_duplicate_id_raises() -> None:
    store = InMemoryTriggerRunStore()
    rid = uuid4()
    await store.create(_run_record(run_record_id=rid))
    with pytest.raises(ValueError, match="already exists"):
        await store.create(_run_record(run_record_id=rid))


@pytest.mark.asyncio
async def test_run_store_update_transitions_status() -> None:
    store = InMemoryTriggerRunStore()
    rid, tenant = uuid4(), uuid4()
    await store.create(_run_record(run_record_id=rid, tenant_id=tenant))

    rec = await store.get(trigger_run_id=rid, tenant_id=tenant)
    assert rec is not None
    done = await store.update(rec.model_copy(update={"status": TriggerRunStatus.SUCCEEDED}))
    assert done is True

    again = await store.get(trigger_run_id=rid, tenant_id=tenant)
    assert again is not None
    assert again.status is TriggerRunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_run_store_update_cross_tenant_returns_false() -> None:
    store = InMemoryTriggerRunStore()
    rid, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    await store.create(_run_record(run_record_id=rid, tenant_id=tenant_a))

    rec = await store.get(trigger_run_id=rid, tenant_id=tenant_a)
    assert rec is not None
    miss = await store.update(rec.model_copy(update={"tenant_id": tenant_b}))
    assert miss is False


@pytest.mark.asyncio
async def test_run_store_list_by_trigger_filters_and_sorts() -> None:
    store = InMemoryTriggerRunStore()
    tenant, trigger_id = uuid4(), uuid4()
    await store.create(_run_record(tenant_id=tenant, trigger_id=trigger_id, triggered_at=_BASE))
    await store.create(
        _run_record(
            tenant_id=tenant,
            trigger_id=trigger_id,
            triggered_at=_BASE.replace(hour=13),
        )
    )
    await store.create(_run_record(tenant_id=tenant, trigger_id=uuid4()))

    listed = await store.list_by_trigger(trigger_id=trigger_id, tenant_id=tenant)
    assert len(listed) == 2
    # newest first
    assert listed[0].triggered_at > listed[1].triggered_at


@pytest.mark.asyncio
async def test_count_cron_by_tenant() -> None:
    store = InMemoryTriggerStore()
    tenant_a, tenant_b = uuid4(), uuid4()
    await store.create(_record(tenant_id=tenant_a, name="c1", kind="cron"))
    await store.create(_record(tenant_id=tenant_a, name="c2", kind="cron"))
    await store.create(_record(tenant_id=tenant_a, name="w1", kind="webhook"))
    await store.create(_record(tenant_id=tenant_b, name="c1", kind="cron"))

    assert await store.count_cron_by_tenant(tenant_id=tenant_a) == 2
    assert await store.count_cron_by_tenant(tenant_id=tenant_b) == 1
    assert await store.count_cron_by_tenant(tenant_id=uuid4()) == 0


@pytest.mark.asyncio
async def test_run_store_list_fired_is_status_filtered() -> None:
    store = InMemoryTriggerRunStore()
    await store.create(_run_record(status=TriggerRunStatus.FIRED))
    await store.create(_run_record(status=TriggerRunStatus.FIRED))
    await store.create(_run_record(status=TriggerRunStatus.SUCCEEDED))
    await store.create(_run_record(status=TriggerRunStatus.RETRYING))

    listed = await store.list_fired()
    assert len(listed) == 2
    assert all(r.status is TriggerRunStatus.FIRED for r in listed)


@pytest.mark.asyncio
async def test_run_store_list_due_retries() -> None:
    store = InMemoryTriggerRunStore()
    now = datetime(2026, 5, 22, 15, 0, 0, tzinfo=UTC)
    await store.create(
        _run_record(status=TriggerRunStatus.RETRYING, next_retry_at=now - timedelta(minutes=1))
    )
    await store.create(
        _run_record(status=TriggerRunStatus.RETRYING, next_retry_at=now + timedelta(hours=1))
    )
    await store.create(_run_record(status=TriggerRunStatus.FIRED))

    due = await store.list_due_retries(before=now)
    assert len(due) == 1
    assert due[0].status is TriggerRunStatus.RETRYING


@pytest.mark.asyncio
async def test_list_by_tenant_agent_version_filter() -> None:
    """Stream H.6 — agent_version narrows the admin list (Triggers tab)."""
    store = InMemoryTriggerStore()
    tenant = uuid4()
    r1 = _record(tenant_id=tenant, agent_name="reporter", name="t1")
    r2 = _record(tenant_id=tenant, agent_name="reporter", name="t2").model_copy(
        update={"agent_version": "2.0.0"}
    )
    await store.create(r1)
    await store.create(r2)

    v1 = await store.list_by_tenant(tenant_id=tenant, agent_name="reporter", agent_version="1.0.0")
    assert [t.name for t in v1] == ["t1"]

    # Version filter alone also works at store level (the 422 guard is API-layer).
    v2 = await store.list_by_tenant(tenant_id=tenant, agent_version="2.0.0")
    assert [t.name for t in v2] == ["t2"]

    # No filter → both (regression); cross-tenant variant same semantics.
    assert len(await store.list_by_tenant(tenant_id=tenant)) == 2
    cross = await store.list_all_tenants(agent_name="reporter", agent_version="2.0.0")
    assert [t.name for t in cross] == ["t2"]


# --- Stream 9.5 — CAS claim (multi-replica exactly-once) -------------------


@pytest.mark.asyncio
async def test_claim_cron_fire_wins_once_then_stale_loses() -> None:
    store = InMemoryTriggerStore()
    tid, tenant = uuid4(), uuid4()
    await store.create(_record(trigger_id=tid, tenant_id=tenant))  # last_fired_at=None
    fire_at = _BASE + timedelta(hours=1)

    # First instance claims the slot (expected=None matches a never-fired row).
    won = await store.claim_cron_fire(
        trigger_id=tid, tenant_id=tenant, expected_last_fired_at=None, new_last_fired_at=fire_at
    )
    assert won is True
    # A peer that still thinks last_fired_at is None loses — slot already taken.
    lost = await store.claim_cron_fire(
        trigger_id=tid, tenant_id=tenant, expected_last_fired_at=None, new_last_fired_at=fire_at
    )
    assert lost is False
    row = await store.get(trigger_id=tid, tenant_id=tenant)
    assert row is not None and row.last_fired_at == fire_at


@pytest.mark.asyncio
async def test_claim_cron_fire_cross_tenant_is_false() -> None:
    store = InMemoryTriggerStore()
    tid, tenant = uuid4(), uuid4()
    await store.create(_record(trigger_id=tid, tenant_id=tenant))
    assert (
        await store.claim_cron_fire(
            trigger_id=tid,
            tenant_id=uuid4(),
            expected_last_fired_at=None,
            new_last_fired_at=_BASE,
        )
        is False
    )


@pytest.mark.asyncio
async def test_claim_retry_wins_once_then_loses() -> None:
    store = InMemoryTriggerRunStore()
    rid, tenant = uuid4(), uuid4()
    await store.create(
        _run_record(run_record_id=rid, tenant_id=tenant, status=TriggerRunStatus.RETRYING)
    )

    first = await store.claim_retry(trigger_run_id=rid, tenant_id=tenant)
    second = await store.claim_retry(trigger_run_id=rid, tenant_id=tenant)
    assert first is True
    assert second is False  # already flipped out of retrying
    row = await store.get(trigger_run_id=rid, tenant_id=tenant)
    assert row is not None
    assert row.status is TriggerRunStatus.FIRED
    assert row.next_retry_at is None


@pytest.mark.asyncio
async def test_claim_retry_false_when_not_retrying_or_cross_tenant() -> None:
    store = InMemoryTriggerRunStore()
    rid, tenant = uuid4(), uuid4()
    await store.create(
        _run_record(run_record_id=rid, tenant_id=tenant, status=TriggerRunStatus.FIRED)
    )
    # Not in retrying → no claim; right row wrong tenant → no claim.
    assert await store.claim_retry(trigger_run_id=rid, tenant_id=tenant) is False
    assert await store.claim_retry(trigger_run_id=rid, tenant_id=uuid4()) is False


@pytest.mark.asyncio
async def test_memory_roundtrips_delivery_routing() -> None:
    store = InMemoryTriggerStore()
    tid, tenant, thread = uuid4(), uuid4(), uuid4()
    rec = _record(trigger_id=tid, tenant_id=tenant).model_copy(
        update={"originating_thread_id": thread, "context_mode": "reuse_thread"}
    )
    await store.create(rec)
    got = await store.get(trigger_id=tid, tenant_id=tenant)
    assert got is not None
    assert got.originating_thread_id == thread
    assert got.context_mode == "reuse_thread"


# --- Task 3 — user-scoped uniqueness (matches Task 1's partial unique indexes) --


@pytest.mark.asyncio
async def test_memory_two_users_same_name_allowed() -> None:
    store = InMemoryTriggerStore()
    tenant, u1, u2 = uuid4(), uuid4(), uuid4()
    await store.create(
        _record(trigger_id=uuid4(), tenant_id=tenant).model_copy(
            update={"user_id": u1, "name": "daily"}
        )
    )
    await store.create(
        _record(trigger_id=uuid4(), tenant_id=tenant).model_copy(
            update={"user_id": u2, "name": "daily"}
        )
    )  # 不放异常即通过


@pytest.mark.asyncio
async def test_memory_same_user_same_name_conflicts() -> None:
    store = InMemoryTriggerStore()
    tenant, u1 = uuid4(), uuid4()
    await store.create(
        _record(trigger_id=uuid4(), tenant_id=tenant).model_copy(
            update={"user_id": u1, "name": "daily"}
        )
    )
    with pytest.raises(ValueError):
        await store.create(
            _record(trigger_id=uuid4(), tenant_id=tenant).model_copy(
                update={"user_id": u1, "name": "daily"}
            )
        )


# --- Task 4 — list_by_user (per-user list, PR2 conversational tool + Spec 3) --


@pytest.mark.asyncio
async def test_memory_list_by_user_scopes() -> None:
    store = InMemoryTriggerStore()
    tenant, u1, u2 = uuid4(), uuid4(), uuid4()
    # b 的 created_at 比 a 晚,但先插入(与 created_at 顺序相反)—— 若
    # list_by_user 少排序、只按插入/字典顺序返回,下面按 created_at 升序的
    # 断言会失败。
    await store.create(
        _record(trigger_id=uuid4(), tenant_id=tenant).model_copy(
            update={"user_id": u1, "name": "b", "created_at": _BASE + timedelta(hours=1)}
        )
    )
    await store.create(
        _record(trigger_id=uuid4(), tenant_id=tenant).model_copy(
            update={"user_id": u1, "name": "a", "created_at": _BASE}
        )
    )
    await store.create(
        _record(trigger_id=uuid4(), tenant_id=tenant).model_copy(
            update={"user_id": u2, "name": "c"}
        )
    )
    got = await store.list_by_user(tenant_id=tenant, user_id=u1)
    assert [t.name for t in got] == ["a", "b"]  # created_at ascending, not insertion order


# --- Task 1 (PR4 hardening) — claim_reconcile CAS (fired → terminal) ------


@pytest.mark.asyncio
async def test_claim_reconcile_wins_from_fired() -> None:
    store = InMemoryTriggerRunStore()
    tenant = uuid4()
    row = _run_record(tenant_id=tenant)  # default status=FIRED
    await store.create(row)

    won = await store.claim_reconcile(row.model_copy(update={"status": TriggerRunStatus.SUCCEEDED}))
    assert won is True

    got = await store.get(trigger_run_id=row.id, tenant_id=tenant)
    assert got is not None
    assert got.status is TriggerRunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_claim_reconcile_loses_when_already_terminal() -> None:
    store = InMemoryTriggerRunStore()
    tenant = uuid4()
    row = _run_record(tenant_id=tenant)  # default status=FIRED
    await store.create(row)

    first = await store.claim_reconcile(
        row.model_copy(update={"status": TriggerRunStatus.SUCCEEDED})
    )
    second = await store.claim_reconcile(
        row.model_copy(update={"status": TriggerRunStatus.FAILED, "error": "x"})
    )
    assert first is True
    assert second is False

    got = await store.get(trigger_run_id=row.id, tenant_id=tenant)
    assert got is not None
    assert got.status is TriggerRunStatus.SUCCEEDED  # 输家不覆盖


@pytest.mark.asyncio
async def test_claim_reconcile_cross_tenant_miss() -> None:
    store = InMemoryTriggerRunStore()
    tenant = uuid4()
    row = _run_record(tenant_id=tenant)  # default status=FIRED
    await store.create(row)

    won = await store.claim_reconcile(
        row.model_copy(update={"tenant_id": uuid4(), "status": TriggerRunStatus.SUCCEEDED})
    )
    assert won is False


# --- PR4 §B — disable_for_agent (agent soft-delete cascade) ----------------


@pytest.mark.asyncio
async def test_disable_for_agent_scopes_by_name_version_tenant() -> None:
    store = InMemoryTriggerStore()
    ten_a, ten_b = uuid4(), uuid4()
    # 同租户:target@v1 enabled x2、target@v2 enabled x1、target@v1 已 disabled x1
    r1 = _record(tenant_id=ten_a, agent_name="target", name="t1").model_copy(
        update={"agent_version": "v1"}
    )
    r2 = _record(tenant_id=ten_a, agent_name="target", name="t2").model_copy(
        update={"agent_version": "v1"}
    )
    r_v2 = _record(tenant_id=ten_a, agent_name="target", name="t3").model_copy(
        update={"agent_version": "v2"}
    )
    r_off = _record(tenant_id=ten_a, agent_name="target", name="t4", enabled=False).model_copy(
        update={"agent_version": "v1"}
    )
    # 他租户:target@v1 enabled x1
    r_b = _record(tenant_id=ten_b, agent_name="target", name="t1").model_copy(
        update={"agent_version": "v1"}
    )
    for rec in (r1, r2, r_v2, r_off, r_b):
        await store.create(rec)

    n = await store.disable_for_agent(agent_name="target", agent_version="v1", tenant_id=ten_a)
    assert n == 2  # 已 disabled 的不计数

    async def _enabled(rec: TriggerRecord) -> bool:
        got = await store.get(trigger_id=rec.id, tenant_id=rec.tenant_id)
        assert got is not None
        return got.enabled

    assert await _enabled(r1) is False
    assert await _enabled(r2) is False
    assert await _enabled(r_v2) is True  # 同 name 他 version 不动(变异哨兵)
    assert await _enabled(r_b) is True  # 他租户不动
    assert await _enabled(r_off) is False  # 此前 disabled 的仍 disabled

    # 幂等重跑:全部已 disabled → 0
    again = await store.disable_for_agent(agent_name="target", agent_version="v1", tenant_id=ten_a)
    assert again == 0
