"""Integration tests for SqlTriggerStore against a real Postgres — J.10."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlTriggerRunStore,
    SqlTriggerStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.protocol import (
    TriggerKind,
    TriggerRecord,
    TriggerRunRecord,
    TriggerRunStatus,
)

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

_BASE = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
def trigger_store(postgres_container: PostgresContainer) -> Iterator[SqlTriggerStore]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    yield SqlTriggerStore(create_async_session_factory(engine))


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
        webhook_secret_hash="sha256:abc" if kind == "webhook" else None,
        created_at=_BASE,
        updated_at=_BASE,
    )


@pytest.mark.asyncio
async def test_create_then_get_round_trips(trigger_store: SqlTriggerStore) -> None:
    tid, tenant = uuid4(), uuid4()
    await trigger_store.create(
        _record(trigger_id=tid, tenant_id=tenant, kind="webhook", name="hook")
    )

    fetched = await trigger_store.get(trigger_id=tid, tenant_id=tenant)
    assert fetched is not None
    assert fetched.id == tid
    assert fetched.kind == "webhook"
    assert fetched.webhook_secret_hash == "sha256:abc"
    assert fetched.source == "api"


@pytest.mark.asyncio
async def test_get_unknown_returns_none(trigger_store: SqlTriggerStore) -> None:
    assert await trigger_store.get(trigger_id=uuid4(), tenant_id=uuid4()) is None


@pytest.mark.asyncio
async def test_get_cross_tenant_returns_none(trigger_store: SqlTriggerStore) -> None:
    tid, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    await trigger_store.create(_record(trigger_id=tid, tenant_id=tenant_a))

    assert await trigger_store.get(trigger_id=tid, tenant_id=tenant_b) is None


@pytest.mark.asyncio
async def test_duplicate_name_per_agent_violates_unique(
    trigger_store: SqlTriggerStore,
) -> None:
    tenant = uuid4()
    await trigger_store.create(_record(tenant_id=tenant, agent_name="reporter", name="nightly"))
    with pytest.raises(IntegrityError):
        await trigger_store.create(_record(tenant_id=tenant, agent_name="reporter", name="nightly"))


@pytest.mark.asyncio
async def test_list_by_agent_filters(trigger_store: SqlTriggerStore) -> None:
    tenant = uuid4()
    await trigger_store.create(_record(tenant_id=tenant, agent_name="reporter", name="a"))
    await trigger_store.create(_record(tenant_id=tenant, agent_name="reporter", name="b"))
    await trigger_store.create(_record(tenant_id=tenant, agent_name="auditor", name="a"))

    listed = await trigger_store.list_by_agent(tenant_id=tenant, agent_name="reporter")
    assert {r.name for r in listed} == {"a", "b"}


@pytest.mark.asyncio
async def test_list_enabled_cron_cross_tenant_kind_filtered(
    trigger_store: SqlTriggerStore,
) -> None:
    await trigger_store.create(_record(tenant_id=uuid4(), name="c1", kind="cron", enabled=True))
    await trigger_store.create(_record(tenant_id=uuid4(), name="c3", kind="cron", enabled=False))
    await trigger_store.create(_record(tenant_id=uuid4(), name="w1", kind="webhook"))

    listed = await trigger_store.list_enabled_cron()
    names = {r.name for r in listed}
    assert "c1" in names
    assert "c3" not in names  # disabled
    assert "w1" not in names  # webhook


@pytest.mark.asyncio
async def test_update_replaces_mutable_fields(trigger_store: SqlTriggerStore) -> None:
    tid, tenant = uuid4(), uuid4()
    await trigger_store.create(_record(trigger_id=tid, tenant_id=tenant, enabled=True))

    rec = await trigger_store.get(trigger_id=tid, tenant_id=tenant)
    assert rec is not None
    fired_at = _BASE.replace(hour=9)
    updated = rec.model_copy(update={"enabled": False, "last_fired_at": fired_at})
    did_update = await trigger_store.update(updated)
    assert did_update is True

    again = await trigger_store.get(trigger_id=tid, tenant_id=tenant)
    assert again is not None
    assert again.enabled is False
    assert again.last_fired_at == fired_at


@pytest.mark.asyncio
async def test_update_unknown_returns_false(trigger_store: SqlTriggerStore) -> None:
    did_update = await trigger_store.update(_record())
    assert did_update is False


@pytest.mark.asyncio
async def test_delete(trigger_store: SqlTriggerStore) -> None:
    tid, tenant = uuid4(), uuid4()
    await trigger_store.create(_record(trigger_id=tid, tenant_id=tenant))

    deleted = await trigger_store.delete(trigger_id=tid, tenant_id=tenant)
    assert deleted is True
    assert await trigger_store.get(trigger_id=tid, tenant_id=tenant) is None
    deleted_again = await trigger_store.delete(trigger_id=tid, tenant_id=tenant)
    assert deleted_again is False


@pytest.mark.asyncio
async def test_get_for_webhook_is_tenant_unscoped(trigger_store: SqlTriggerStore) -> None:
    """The webhook ingest path resolves a trigger by id alone — no tenant."""
    tid, tenant = uuid4(), uuid4()
    await trigger_store.create(
        _record(trigger_id=tid, tenant_id=tenant, kind="webhook", name="hook")
    )

    found = await trigger_store.get_for_webhook(trigger_id=tid)
    assert found is not None
    assert found.tenant_id == tenant
    assert await trigger_store.get_for_webhook(trigger_id=uuid4()) is None


# --- SqlTriggerRunStore ---------------------------------------------------


@pytest.fixture
def trigger_run_store(postgres_container: PostgresContainer) -> Iterator[SqlTriggerRunStore]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    yield SqlTriggerRunStore(create_async_session_factory(engine))


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
async def test_run_store_create_then_get(trigger_run_store: SqlTriggerRunStore) -> None:
    rid, tenant = uuid4(), uuid4()
    await trigger_run_store.create(_run_record(run_record_id=rid, tenant_id=tenant))

    fetched = await trigger_run_store.get(trigger_run_id=rid, tenant_id=tenant)
    assert fetched is not None
    assert fetched.id == rid
    assert fetched.status is TriggerRunStatus.FIRED
    assert fetched.attempt == 1


@pytest.mark.asyncio
async def test_run_store_get_cross_tenant_returns_none(
    trigger_run_store: SqlTriggerRunStore,
) -> None:
    rid, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    await trigger_run_store.create(_run_record(run_record_id=rid, tenant_id=tenant_a))

    assert await trigger_run_store.get(trigger_run_id=rid, tenant_id=tenant_b) is None


@pytest.mark.asyncio
async def test_run_store_update_transitions_status(
    trigger_run_store: SqlTriggerRunStore,
) -> None:
    rid, tenant = uuid4(), uuid4()
    await trigger_run_store.create(_run_record(run_record_id=rid, tenant_id=tenant))

    rec = await trigger_run_store.get(trigger_run_id=rid, tenant_id=tenant)
    assert rec is not None
    done = await trigger_run_store.update(
        rec.model_copy(update={"status": TriggerRunStatus.FAILED, "error": "boom"})
    )
    assert done is True

    again = await trigger_run_store.get(trigger_run_id=rid, tenant_id=tenant)
    assert again is not None
    assert again.status is TriggerRunStatus.FAILED
    assert again.error == "boom"


@pytest.mark.asyncio
async def test_run_store_list_by_trigger(trigger_run_store: SqlTriggerRunStore) -> None:
    tenant, trigger_id = uuid4(), uuid4()
    await trigger_run_store.create(
        _run_record(tenant_id=tenant, trigger_id=trigger_id, triggered_at=_BASE)
    )
    await trigger_run_store.create(
        _run_record(tenant_id=tenant, trigger_id=trigger_id, triggered_at=_BASE.replace(hour=15))
    )
    await trigger_run_store.create(_run_record(tenant_id=tenant, trigger_id=uuid4()))

    listed = await trigger_run_store.list_by_trigger(trigger_id=trigger_id, tenant_id=tenant)
    assert len(listed) == 2
    assert listed[0].triggered_at > listed[1].triggered_at  # newest first


@pytest.mark.asyncio
async def test_count_cron_by_tenant(trigger_store: SqlTriggerStore) -> None:
    tenant_a, tenant_b = uuid4(), uuid4()
    await trigger_store.create(_record(tenant_id=tenant_a, name="c1", kind="cron"))
    await trigger_store.create(_record(tenant_id=tenant_a, name="c2", kind="cron"))
    await trigger_store.create(_record(tenant_id=tenant_a, name="w1", kind="webhook"))
    await trigger_store.create(_record(tenant_id=tenant_b, name="c1", kind="cron"))

    assert await trigger_store.count_cron_by_tenant(tenant_id=tenant_a) == 2
    assert await trigger_store.count_cron_by_tenant(tenant_id=tenant_b) == 1


@pytest.mark.asyncio
async def test_run_store_list_fired(trigger_run_store: SqlTriggerRunStore) -> None:
    await trigger_run_store.create(_run_record(status=TriggerRunStatus.FIRED))
    await trigger_run_store.create(_run_record(status=TriggerRunStatus.SUCCEEDED))
    await trigger_run_store.create(_run_record(status=TriggerRunStatus.RETRYING))

    listed = await trigger_run_store.list_fired()
    assert all(r.status is TriggerRunStatus.FIRED for r in listed)
    assert len(listed) >= 1


@pytest.mark.asyncio
async def test_run_store_list_due_retries(trigger_run_store: SqlTriggerRunStore) -> None:
    now = datetime(2026, 5, 22, 15, 0, 0, tzinfo=UTC)
    due = _run_record(status=TriggerRunStatus.RETRYING, next_retry_at=now - timedelta(minutes=1))
    not_due = _run_record(status=TriggerRunStatus.RETRYING, next_retry_at=now + timedelta(hours=1))
    await trigger_run_store.create(due)
    await trigger_run_store.create(not_due)

    listed = await trigger_run_store.list_due_retries(before=now)
    ids = {r.id for r in listed}
    assert due.id in ids
    assert not_due.id not in ids


# --- Stream 9.5 — CAS claim exactly-once under real concurrency -----------


@pytest.mark.asyncio
async def test_claim_cron_fire_concurrent_wins_exactly_one(
    trigger_store: SqlTriggerStore,
) -> None:
    """16 schedulers race the same due cron slot — the CAS on ``last_fired_at``
    (IS NOT DISTINCT FROM the read value) lets exactly one win."""
    tid, tenant = uuid4(), uuid4()
    await trigger_store.create(_record(trigger_id=tid, tenant_id=tenant))  # last_fired_at=None
    fire_at = _BASE + timedelta(hours=1)

    async def _claim() -> bool:
        return await trigger_store.claim_cron_fire(
            trigger_id=tid,
            tenant_id=tenant,
            expected_last_fired_at=None,
            new_last_fired_at=fire_at,
        )

    results = await asyncio.gather(*[_claim() for _ in range(16)])
    assert sum(1 for won in results if won) == 1

    row = await trigger_store.get(trigger_id=tid, tenant_id=tenant)
    assert row is not None
    assert row.last_fired_at == fire_at


@pytest.mark.asyncio
async def test_claim_retry_concurrent_wins_exactly_one(
    trigger_run_store: SqlTriggerRunStore,
) -> None:
    """16 schedulers race the same retrying firing — the CAS ``retrying`` →
    ``fired`` lets exactly one re-fire."""
    rid, tenant = uuid4(), uuid4()
    await trigger_run_store.create(
        _run_record(run_record_id=rid, tenant_id=tenant, status=TriggerRunStatus.RETRYING)
    )

    async def _claim() -> bool:
        return await trigger_run_store.claim_retry(trigger_run_id=rid, tenant_id=tenant)

    results = await asyncio.gather(*[_claim() for _ in range(16)])
    assert sum(1 for won in results if won) == 1

    row = await trigger_run_store.get(trigger_run_id=rid, tenant_id=tenant)
    assert row is not None
    assert row.status is TriggerRunStatus.FIRED
    assert row.next_retry_at is None


@pytest.mark.asyncio
async def test_create_roundtrips_delivery_routing(trigger_store: SqlTriggerStore) -> None:
    tid, tenant, thread = uuid4(), uuid4(), uuid4()
    rec = _record(trigger_id=tid, tenant_id=tenant).model_copy(
        update={"originating_thread_id": thread, "context_mode": "reuse_thread"}
    )
    await trigger_store.create(rec)
    got = await trigger_store.get(trigger_id=tid, tenant_id=tenant)
    assert got is not None
    assert got.originating_thread_id == thread
    assert got.context_mode == "reuse_thread"


@pytest.mark.asyncio
async def test_create_defaults_context_mode(trigger_store: SqlTriggerStore) -> None:
    tid, tenant = uuid4(), uuid4()
    await trigger_store.create(_record(trigger_id=tid, tenant_id=tenant))
    got = await trigger_store.get(trigger_id=tid, tenant_id=tenant)
    assert got is not None
    assert got.originating_thread_id is None
    assert got.context_mode == "fresh_thread_per_run"


# --- Task 3 — user-scoped uniqueness (Task 1's partial unique indexes) ----


@pytest.mark.asyncio
async def test_two_users_same_name_allowed(trigger_store: SqlTriggerStore) -> None:
    tenant, u1, u2 = uuid4(), uuid4(), uuid4()
    r1 = _record(trigger_id=uuid4(), tenant_id=tenant).model_copy(
        update={"user_id": u1, "name": "daily"}
    )
    r2 = _record(trigger_id=uuid4(), tenant_id=tenant).model_copy(
        update={"user_id": u2, "name": "daily"}
    )
    await trigger_store.create(r1)
    await trigger_store.create(r2)  # 不同 user 同名 —— 放行
    assert (await trigger_store.get(trigger_id=r2.id, tenant_id=tenant)) is not None


@pytest.mark.asyncio
async def test_same_user_same_name_conflicts(trigger_store: SqlTriggerStore) -> None:
    from sqlalchemy.exc import IntegrityError

    tenant, u1 = uuid4(), uuid4()
    r1 = _record(trigger_id=uuid4(), tenant_id=tenant).model_copy(
        update={"user_id": u1, "name": "daily"}
    )
    r2 = _record(trigger_id=uuid4(), tenant_id=tenant).model_copy(
        update={"user_id": u1, "name": "daily"}
    )
    await trigger_store.create(r1)
    with pytest.raises(IntegrityError):
        await trigger_store.create(r2)


# --- Task 4 — list_by_user (per-user list, PR2 conversational tool + Spec 3) --


@pytest.mark.asyncio
async def test_list_by_user_scopes_ordered_and_filters_agent(
    trigger_store: SqlTriggerStore,
) -> None:
    tenant, u1, u2 = uuid4(), uuid4(), uuid4()
    r1 = _record(trigger_id=uuid4(), tenant_id=tenant, agent_name="reporter", name="a").model_copy(
        update={"user_id": u1, "created_at": _BASE}
    )
    r2 = _record(trigger_id=uuid4(), tenant_id=tenant, agent_name="auditor", name="b").model_copy(
        update={"user_id": u1, "created_at": _BASE.replace(hour=13)}
    )
    r3 = _record(trigger_id=uuid4(), tenant_id=tenant, agent_name="reporter", name="c").model_copy(
        update={"user_id": u2}
    )
    # r2(created_at 更晚)先插、r1 后插 —— 与 created_at 顺序相反地插入,
    # 这样若 list_by_user 缺 ORDER BY(只靠插入/物理顺序凑巧对上)断言会失败。
    await trigger_store.create(r2)
    await trigger_store.create(r1)
    await trigger_store.create(r3)

    got = await trigger_store.list_by_user(tenant_id=tenant, user_id=u1)
    assert [t.name for t in got] == ["a", "b"]  # tenant+user scoped, created_at ascending

    filtered = await trigger_store.list_by_user(tenant_id=tenant, user_id=u1, agent_name="reporter")
    assert [t.name for t in filtered] == ["a"]


# --- Task 1 (PR4 hardening) — claim_reconcile CAS (fired → terminal) ------


@pytest.mark.asyncio
async def test_claim_reconcile_wins_then_loses(trigger_run_store: SqlTriggerRunStore) -> None:
    rid, tenant = uuid4(), uuid4()
    row = _run_record(run_record_id=rid, tenant_id=tenant)  # default status=FIRED
    await trigger_run_store.create(row)

    won = await trigger_run_store.claim_reconcile(
        row.model_copy(update={"status": TriggerRunStatus.SUCCEEDED})
    )
    lost = await trigger_run_store.claim_reconcile(
        row.model_copy(update={"status": TriggerRunStatus.FAILED, "error": "x"})
    )
    assert won is True
    assert lost is False  # already moved off `fired` — the UPDATE matches no row

    got = await trigger_run_store.get(trigger_run_id=rid, tenant_id=tenant)
    assert got is not None
    assert got.status is TriggerRunStatus.SUCCEEDED  # loser did not overwrite


@pytest.mark.asyncio
async def test_claim_reconcile_false_when_not_fired(trigger_run_store: SqlTriggerRunStore) -> None:
    rid, tenant = uuid4(), uuid4()
    row = _run_record(run_record_id=rid, tenant_id=tenant)  # starts FIRED
    await trigger_run_store.create(row)
    # Row moved off `fired` via the ordinary (non-CAS) update — e.g. the
    # scheduler already routed it to a retry before this stale claim arrives.
    await trigger_run_store.update(row.model_copy(update={"status": TriggerRunStatus.RETRYING}))

    won = await trigger_run_store.claim_reconcile(
        row.model_copy(update={"status": TriggerRunStatus.SUCCEEDED})
    )
    assert won is False

    got = await trigger_run_store.get(trigger_run_id=rid, tenant_id=tenant)
    assert got is not None
    assert got.status is TriggerRunStatus.RETRYING


# --- PR4 §B — disable_for_agent (agent soft-delete cascade) ----------------


@pytest.mark.asyncio
async def test_disable_for_agent_scopes_by_name_version_tenant(
    trigger_store: SqlTriggerStore,
) -> None:
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
        await trigger_store.create(rec)

    n = await trigger_store.disable_for_agent(
        agent_name="target", agent_version="v1", tenant_id=ten_a
    )
    assert n == 2  # 已 disabled 的不计数

    async def _enabled(rec: TriggerRecord) -> bool:
        got = await trigger_store.get(trigger_id=rec.id, tenant_id=rec.tenant_id)
        assert got is not None
        return got.enabled

    assert await _enabled(r1) is False
    assert await _enabled(r2) is False
    assert await _enabled(r_v2) is True  # 同 name 他 version 不动(变异哨兵)
    assert await _enabled(r_b) is True  # 他租户不动
    assert await _enabled(r_off) is False  # 此前 disabled 的仍 disabled

    # 幂等重跑:全部已 disabled → 0
    again = await trigger_store.disable_for_agent(
        agent_name="target", agent_version="v1", tenant_id=ten_a
    )
    assert again == 0
