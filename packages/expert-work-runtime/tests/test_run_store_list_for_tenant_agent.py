"""``RunStore.list_for_tenant(agent_name=...)`` —— 阶段 3 (3.2)。

run 行上没有 agent,绑定在 ``thread_meta`` 上,所以这是个 join ——
和同目录 ``test_run_store_list_running_for_agent.py`` 测的是同一层机制,
helper 也照抄那份。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from expert_work.persistence.thread_meta import InMemoryThreadMetaStore
from expert_work.runtime.runs import DisconnectMode, InMemoryRunStore, RunInfo, RunStatus

_BASE = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


def _info(
    *,
    run_id: UUID,
    tenant_id: UUID,
    thread_id: UUID,
    user_id: UUID | None = None,
    status: RunStatus = RunStatus.SUCCESS,
) -> RunInfo:
    return RunInfo(
        run_id=run_id,
        tenant_id=tenant_id,
        thread_id=thread_id,
        user_id=user_id,
        status=status,
        on_disconnect=DisconnectMode.CANCEL,
        is_resume=False,
        error=None,
        created_at=_BASE,
        updated_at=_BASE,
        finished_at=None,
    )


async def _seed_thread(
    threads: InMemoryThreadMetaStore,
    *,
    thread_id: UUID,
    tenant_id: UUID,
    agent_name: str,
) -> None:
    # ``created_by`` 是必填的(thread_meta/base.py:48)。
    await threads.create(
        thread_id=thread_id,
        tenant_id=tenant_id,
        created_by="seed",
        agent_name=agent_name,
    )


@pytest.mark.asyncio
async def test_filters_by_agent_name() -> None:
    """这个过滤必须穿过 thread_meta 那一层,不然对外的 run 列表只能靠
    API 层先查 thread 再过滤 —— 分页会失准。"""
    threads = InMemoryThreadMetaStore()
    store = InMemoryRunStore(thread_meta_store=threads)
    tenant_id = uuid4()
    t_alpha, t_beta = uuid4(), uuid4()
    await _seed_thread(threads, thread_id=t_alpha, tenant_id=tenant_id, agent_name="alpha")
    await _seed_thread(threads, thread_id=t_beta, tenant_id=tenant_id, agent_name="beta")

    run_alpha = uuid4()
    await store.create(_info(run_id=run_alpha, tenant_id=tenant_id, thread_id=t_alpha))
    await store.create(_info(run_id=uuid4(), tenant_id=tenant_id, thread_id=t_beta))

    rows = await store.list_for_tenant(tenant_id=tenant_id, agent_name="alpha")

    assert [r.run_id for r in rows] == [run_alpha]


@pytest.mark.asyncio
async def test_agent_name_none_keeps_every_agent() -> None:
    """不传 ``agent_name`` 的既有调用方行为必须一个字不变。"""
    threads = InMemoryThreadMetaStore()
    store = InMemoryRunStore(thread_meta_store=threads)
    tenant_id = uuid4()
    t_alpha, t_beta = uuid4(), uuid4()
    await _seed_thread(threads, thread_id=t_alpha, tenant_id=tenant_id, agent_name="alpha")
    await _seed_thread(threads, thread_id=t_beta, tenant_id=tenant_id, agent_name="beta")
    await store.create(_info(run_id=uuid4(), tenant_id=tenant_id, thread_id=t_alpha))
    await store.create(_info(run_id=uuid4(), tenant_id=tenant_id, thread_id=t_beta))

    assert len(await store.list_for_tenant(tenant_id=tenant_id)) == 2


@pytest.mark.asyncio
async def test_agent_name_without_thread_store_returns_empty() -> None:
    """没接 ``thread_meta_store`` 时,agent 过滤无从判断 —— 返回空,
    而不是**静默忽略过滤条件**把全部 run 都吐出来。后者会让一个配错的
    实例把别的 agent 的 run 漏给第三方。同 ``list_running_for_agent``
    的既有处理(它在同样情况下返回 ``[]``)。"""
    store = InMemoryRunStore()  # 没有 thread_meta_store
    tenant_id, thread_id = uuid4(), uuid4()
    await store.create(_info(run_id=uuid4(), tenant_id=tenant_id, thread_id=thread_id))

    assert await store.list_for_tenant(tenant_id=tenant_id, agent_name="alpha") == []
    # 不带过滤时照常返回 —— 证明上面那条空结果来自过滤,不是 store 坏了
    assert len(await store.list_for_tenant(tenant_id=tenant_id)) == 1
