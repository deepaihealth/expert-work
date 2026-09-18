# ruff: noqa: RUF001 —— 断言里是平台贴给模型的中文全角标点
"""Unit tests for :class:`InMemoryThreadMessageStore` — conversation IA M4."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from expert_work.persistence import InMemoryThreadMessageStore, MessageTurn

_NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_sync_and_search_round_trip() -> None:
    store = InMemoryThreadMessageStore()
    tenant, thread = uuid4(), uuid4()
    await store.sync_thread(
        thread_id=thread,
        tenant_id=tenant,
        turns=[
            MessageTurn(seq=0, role="user", content="I was charged twice for 退款"),
            MessageTurn(seq=1, role="assistant", content="Refund case opened"),
        ],
        synced_at=_NOW,
    )

    # Case-insensitive substring, matches either role's content — incl. CJK.
    assert await store.search_thread_ids(tenant_id=tenant, q="CHARGED") == {thread}
    assert await store.search_thread_ids(tenant_id=tenant, q="退款") == {thread}
    assert await store.search_thread_ids(tenant_id=tenant, q="refund case") == {thread}
    assert await store.search_thread_ids(tenant_id=tenant, q="nothing here") == set()


@pytest.mark.asyncio
async def test_sync_is_idempotent_and_append_only() -> None:
    store = InMemoryThreadMessageStore()
    tenant, thread = uuid4(), uuid4()
    first = MessageTurn(seq=0, role="user", content="original")
    await store.sync_thread(thread_id=thread, tenant_id=tenant, turns=[first], synced_at=_NOW)
    # A re-sync never rewrites an existing seq (ON CONFLICT DO NOTHING
    # semantics) and appends the new tail.
    await store.sync_thread(
        thread_id=thread,
        tenant_id=tenant,
        turns=[
            MessageTurn(seq=0, role="user", content="MUTATED"),
            MessageTurn(seq=2, role="assistant", content="tail"),
        ],
        synced_at=_NOW,
    )
    assert await store.search_thread_ids(tenant_id=tenant, q="original") == {thread}
    assert await store.search_thread_ids(tenant_id=tenant, q="MUTATED") == set()
    assert await store.search_thread_ids(tenant_id=tenant, q="tail") == {thread}


@pytest.mark.asyncio
async def test_search_scopes_by_tenant() -> None:
    store = InMemoryThreadMessageStore()
    ten_a, ten_b = uuid4(), uuid4()
    thread_a, thread_b = uuid4(), uuid4()
    await store.sync_thread(
        thread_id=thread_a,
        tenant_id=ten_a,
        turns=[MessageTurn(seq=0, role="user", content="shared needle")],
        synced_at=_NOW,
    )
    await store.sync_thread(
        thread_id=thread_b,
        tenant_id=ten_b,
        turns=[MessageTurn(seq=0, role="user", content="shared needle")],
        synced_at=_NOW,
    )
    assert await store.search_thread_ids(tenant_id=ten_a, q="needle") == {thread_a}
    # Cross-tenant aggregate (system_admin browser) spans both.
    assert await store.search_thread_ids(tenant_id=None, q="needle") == {thread_a, thread_b}


@pytest.mark.asyncio
async def test_pending_is_noop_in_memory() -> None:
    # No thread_meta/agent_run tables to correlate against — the SQL
    # backend owns the real selection (see base docstring).
    store = InMemoryThreadMessageStore()
    assert await store.pending_thread_ids(limit=10) == []


@pytest.mark.asyncio
async def test_mark_superseded_updates_only_the_seq_range() -> None:
    store = InMemoryThreadMessageStore()
    thread, tenant, new_run = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    turns = [
        MessageTurn(seq=s, role="user" if s % 2 else "assistant", content=f"m{s}")
        for s in (1, 3, 5, 7)
    ]
    await store.sync_thread(thread_id=thread, tenant_id=tenant, turns=turns, synced_at=now)
    assert (
        await store.mark_superseded(
            thread_id=thread, tenant_id=tenant, seq_from=3, seq_to=6, superseded_by=new_run
        )
        == 2
    )
    assert (
        await store.mark_superseded(
            thread_id=thread, tenant_id=uuid4(), seq_from=0, seq_to=99, superseded_by=new_run
        )
        == 0
    )
    marked = {
        seq: turn.superseded_by for (tid, seq), (_t, turn) in store._turns.items() if tid == thread
    }
    assert marked == {1: None, 3: new_run, 5: new_run, 7: None}


# --------------------------------------------------------------------- B-73 ①


@pytest.mark.asyncio
async def test_platform_scaffolding_is_mirrored_but_not_searchable() -> None:
    """B-73 ① —— 镜像要**忠实**(审计看得见 B-67 的「本轮输入」段),搜索要**干净**。

    不加这道谓词的话,B-67 之后每一个 jinja 线程都带着同一段平台文案,搜「清单」
    「输入」或任意一个变量名会命中全部这类线程 —— 假阳性,不泄任何租户文本,
    但把对话浏览器的搜索淹掉。
    """
    store = InMemoryThreadMessageStore()
    tenant, thread = uuid4(), uuid4()
    await store.sync_thread(
        thread_id=thread,
        tenant_id=tenant,
        turns=[
            MessageTurn(seq=0, role="user", content="做一版随访方案"),
            MessageTurn(
                seq=1,
                role="user",
                content="[本轮输入]（平台自动生成）输入文件目录 $EXPERT_WORK_INPUTS_DIR",
                hidden=True,
            ),
            MessageTurn(seq=2, role="assistant", content="好的"),
        ],
        synced_at=_NOW,
    )
    # 行在表里(镜像忠实)……
    assert await store.search_thread_ids(tenant_id=tenant, q="随访") == {thread}
    # ……但搜不出来。
    assert await store.search_thread_ids(tenant_id=tenant, q="本轮输入") == set()
    assert await store.search_thread_ids(tenant_id=tenant, q="EXPERT_WORK_INPUTS_DIR") == set()


@pytest.mark.asyncio
async def test_a_reswept_row_gets_its_hidden_value_corrected() -> None:
    """存量行在 0156 之前没有这一列,默认 ``false`` 而不是真值。其余列在给定
    ``seq`` 上永不变(检查点只追加),所以照旧 DO NOTHING;``hidden`` 是唯一在
    冲突时也写的列 —— 否则那些行永远修不回来。"""
    store = InMemoryThreadMessageStore()
    tenant, thread = uuid4(), uuid4()
    stale = MessageTurn(seq=0, role="user", content="[本轮输入]（平台自动生成）")
    await store.sync_thread(thread_id=thread, tenant_id=tenant, turns=[stale], synced_at=_NOW)
    assert await store.search_thread_ids(tenant_id=tenant, q="本轮输入") == {thread}

    await store.sync_thread(
        thread_id=thread,
        tenant_id=tenant,
        turns=[MessageTurn(seq=0, role="user", content="[本轮输入]（平台自动生成）", hidden=True)],
        synced_at=_NOW,
    )
    assert await store.search_thread_ids(tenant_id=tenant, q="本轮输入") == set()
