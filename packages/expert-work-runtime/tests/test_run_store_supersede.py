"""P-1 —— agent_run 两列在 RunInfo / RunStore / RunManager 上的贯通(内存实现)。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunStore,
    RunInfo,
    RunManager,
    RunStatus,
)


def _info(
    *,
    run_id: UUID,
    tenant_id: UUID,
    thread_id: UUID,
    regenerated_from: UUID | None = None,
) -> RunInfo:
    now = datetime.now(UTC)
    return RunInfo(
        run_id=run_id,
        tenant_id=tenant_id,
        thread_id=thread_id,
        user_id=None,
        status=RunStatus.SUCCESS,
        on_disconnect=DisconnectMode.CONTINUE,
        is_resume=False,
        error=None,
        created_at=now,
        updated_at=now,
        finished_at=now,
        regenerated_from_run_id=regenerated_from,
    )


@pytest.mark.asyncio
async def test_mark_superseded_sets_link_and_is_tenant_scoped() -> None:
    store = InMemoryRunStore()
    tenant, thread, old, new = uuid4(), uuid4(), uuid4(), uuid4()
    await store.create(_info(run_id=old, tenant_id=tenant, thread_id=thread))
    other = await store.mark_superseded(run_id=old, tenant_id=uuid4(), superseded_by_run_id=new)
    assert other is False
    mine = await store.mark_superseded(run_id=old, tenant_id=tenant, superseded_by_run_id=new)
    assert mine is True
    row = await store.get(run_id=old, tenant_id=tenant)
    assert row is not None
    assert row.superseded_by_run_id == new


@pytest.mark.asyncio
async def test_manager_create_and_enqueue_persist_regenerated_from() -> None:
    store = InMemoryRunStore()
    manager = RunManager(store=store)
    tenant, thread, old = uuid4(), uuid4(), uuid4()
    r1, r2 = uuid4(), uuid4()
    await manager.create(run_id=r1, thread_id=thread, tenant_id=tenant, regenerated_from_run_id=old)
    await manager.enqueue(
        run_id=r2,
        thread_id=thread,
        tenant_id=tenant,
        enqueued_input={"input": "x"},
        regenerated_from_run_id=old,
    )
    for rid in (r1, r2):
        row = await store.get(run_id=rid, tenant_id=tenant)
        assert row is not None
        assert row.regenerated_from_run_id == old
    plain = uuid4()
    await manager.create(run_id=plain, thread_id=thread, tenant_id=tenant)
    plain_row = await store.get(run_id=plain, tenant_id=tenant)
    assert plain_row is not None
    assert plain_row.regenerated_from_run_id is None
