"""Sprint #7 (Mini-ADRs U-33 / U-37 / U-40) — InMemoryMemoryStore
consolidator interface contracts.

Covers the 7 new methods + the ``retrieve()`` default-WHERE filter:

* ``consolidator_distinct_tenant_ids`` returns tenants with transients
* ``distinct_users`` returns per-tenant users with transients
* ``list_transient`` returns oldest-first within an age window
* ``vector_neighbors`` returns cosine-bounded neighbors
* ``write_consolidated`` writes parent + links sources back atomically
* ``list_purge_candidates`` enforces 3 guards (age / unused / unreviewed)
* ``mark_reviewed`` stamps last_reviewed_at
* ``archive`` raises NotImplementedError (Sprint #7 reserves M2-C)
* ``retrieve`` skips raw transient with ``consolidated_into`` set + skips
  ``status='archived'``
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from expert_work.persistence import InMemoryMemoryStore
from expert_work.persistence.memory.hash import hash_content
from expert_work.protocol import MemoryItem

_NOW = datetime.now(UTC)


def _item(
    *,
    tenant: object,
    user: object,
    content: str,
    embedding: tuple[float, ...] = (1.0, 0.0),
    status: str = "transient",
    consolidated_into: object = None,
    last_reviewed_at: object = None,
    created_at: datetime | None = None,
    last_used_at: datetime | None = None,
    access_count: int = 0,
) -> MemoryItem:
    created = created_at or _NOW
    return MemoryItem(
        id=uuid4(),
        tenant_id=tenant,  # type: ignore[arg-type]
        user_id=user,  # type: ignore[arg-type]
        kind="fact",
        content=content,
        embedding=embedding,
        content_hash=hash_content(content),
        created_at=created,
        last_used_at=last_used_at or created,
        status=status,  # type: ignore[arg-type]
        consolidated_into=consolidated_into,  # type: ignore[arg-type]
        last_reviewed_at=last_reviewed_at,  # type: ignore[arg-type]
        access_count=access_count,
    )


@pytest.mark.asyncio
async def test_consolidator_distinct_tenant_ids_returns_only_with_transient() -> None:
    store = InMemoryMemoryStore()
    t1, t2 = uuid4(), uuid4()
    u = uuid4()
    await store.write([_item(tenant=t1, user=u, content="a")])
    store._rows.append(_item(tenant=t2, user=u, content="b", status="consolidated"))
    tenants = await store.consolidator_distinct_tenant_ids()
    assert t1 in tenants
    assert t2 not in tenants


@pytest.mark.asyncio
async def test_distinct_users_skips_consolidated() -> None:
    store = InMemoryMemoryStore()
    t, u1, u2 = uuid4(), uuid4(), uuid4()
    await store.write([_item(tenant=t, user=u1, content="x")])
    store._rows.append(_item(tenant=t, user=u2, content="y", status="consolidated"))
    users = await store.distinct_users(tenant_id=t)
    assert u1 in users
    assert u2 not in users


@pytest.mark.asyncio
async def test_list_transient_skips_consolidated_and_old() -> None:
    store = InMemoryMemoryStore()
    t, u = uuid4(), uuid4()
    fresh = _item(tenant=t, user=u, content="fresh", created_at=_NOW)
    old = _item(tenant=t, user=u, content="old", created_at=_NOW - timedelta(days=45))
    linked = _item(
        tenant=t,
        user=u,
        content="linked",
        consolidated_into=uuid4(),
    )
    store._rows.extend([fresh, old, linked])
    result = await store.list_transient(tenant_id=t, user_id=u, max_age_days=30, limit=10)
    contents = [r.content for r in result]
    assert "fresh" in contents
    assert "old" not in contents
    assert "linked" not in contents


@pytest.mark.asyncio
async def test_vector_neighbors_respects_cosine_max() -> None:
    store = InMemoryMemoryStore()
    t, u = uuid4(), uuid4()
    a = _item(tenant=t, user=u, content="a", embedding=(1.0, 0.0))
    b_close = _item(tenant=t, user=u, content="b", embedding=(0.99, 0.01))
    c_far = _item(tenant=t, user=u, content="c", embedding=(0.0, 1.0))
    store._rows.extend([a, b_close, c_far])
    neighbors = await store.vector_neighbors(
        tenant_id=t, user_id=u, embedding=(1.0, 0.0), cosine_max=0.1, limit=10
    )
    contents = [n.content for n in neighbors]
    assert "a" in contents
    assert "b" in contents
    assert "c" not in contents


@pytest.mark.asyncio
async def test_write_consolidated_links_sources_atomically() -> None:
    store = InMemoryMemoryStore()
    t, u = uuid4(), uuid4()
    s1 = _item(tenant=t, user=u, content="source 1")
    s2 = _item(tenant=t, user=u, content="source 2")
    store._rows.extend([s1, s2])
    parent = await store.write_consolidated(
        tenant_id=t,
        user_id=u,
        content="consolidated summary",
        embedding=(0.5, 0.5),
        source_ids=[s1.id, s2.id],
        source_hashes={s.id: hash_content(s.content) for s in (s1, s2)},
    )
    assert parent.status == "consolidated"
    assert set(parent.consolidated_from) == {s1.id, s2.id}
    # Sources now point back to parent.
    rows_by_id = {r.id: r for r in store._rows}
    assert rows_by_id[s1.id].consolidated_into == parent.id
    assert rows_by_id[s2.id].consolidated_into == parent.id


@pytest.mark.asyncio
async def test_write_consolidated_aborts_when_source_already_consolidated() -> None:
    store = InMemoryMemoryStore()
    t, u = uuid4(), uuid4()
    s1 = _item(tenant=t, user=u, content="source", consolidated_into=uuid4())
    store._rows.append(s1)
    with pytest.raises(RuntimeError, match=r"already consolidated_into"):
        await store.write_consolidated(
            tenant_id=t,
            user_id=u,
            content="summary",
            embedding=(0.5, 0.5),
            source_ids=[s1.id],
            source_hashes={s1.id: hash_content(s1.content)},
        )


@pytest.mark.asyncio
async def test_list_purge_candidates_respects_three_guards() -> None:
    store = InMemoryMemoryStore()
    t, u = uuid4(), uuid4()
    too_young = _item(
        tenant=t,
        user=u,
        content="young",
        created_at=_NOW - timedelta(days=15),
    )
    used = _item(
        tenant=t,
        user=u,
        content="used",
        created_at=_NOW - timedelta(days=60),
        last_used_at=_NOW - timedelta(days=5),
        access_count=1,
    )
    reviewed = _item(
        tenant=t,
        user=u,
        content="reviewed",
        created_at=_NOW - timedelta(days=60),
        last_reviewed_at=_NOW - timedelta(days=10),
    )
    eligible = _item(
        tenant=t,
        user=u,
        content="eligible",
        created_at=_NOW - timedelta(days=60),
    )
    store._rows.extend([too_young, used, reviewed, eligible])
    result = await store.list_purge_candidates(tenant_id=t, user_id=u, min_age_days=30, limit=10)
    contents = [r.content for r in result]
    assert contents == ["eligible"]


@pytest.mark.asyncio
async def test_list_purge_candidates_never_used_guard_uses_access_count() -> None:
    """P5a Task 6 — the never-used guard must be exact (``access_count ==
    0``), not the old ``last_used_at <= created_at + 1 minute``
    approximation. A row hit a few times right after creation and never
    touched again still has ``access_count > 0``: the old heuristic would
    misread its stale-but-early ``last_used_at`` as "never used" and
    wrongly admit it to the purge pool (this is the bug being fixed —
    high-frequency-but-early-hit memories used to leak into purge
    candidates)."""
    store = InMemoryMemoryStore()
    t, u = uuid4(), uuid4()
    created = _NOW - timedelta(days=60)
    accessed_early = _item(
        tenant=t,
        user=u,
        content="accessed_early",
        created_at=created,
        last_used_at=created + timedelta(seconds=30),
        access_count=3,
    )
    store._rows.append(accessed_early)
    result = await store.list_purge_candidates(tenant_id=t, user_id=u, min_age_days=30, limit=10)
    assert result == []


@pytest.mark.asyncio
async def test_mark_reviewed_stamps_timestamp() -> None:
    store = InMemoryMemoryStore()
    t, u = uuid4(), uuid4()
    item = _item(tenant=t, user=u, content="x")
    store._rows.append(item)
    ok = await store.mark_reviewed(tenant_id=t, user_id=u, memory_id=item.id)
    assert ok is True
    assert store._rows[-1].last_reviewed_at is not None
    assert store._rows[-1].last_reviewed_at >= _NOW


@pytest.mark.asyncio
async def test_archive_raises_not_implemented() -> None:
    store = InMemoryMemoryStore()
    with pytest.raises(NotImplementedError, match=r"reserved for M2-C"):
        await store.archive(
            tenant_id=uuid4(),
            user_id=uuid4(),
            memory_id=uuid4(),
        )


@pytest.mark.asyncio
async def test_retrieve_skips_consolidated_into_and_archived() -> None:
    store = InMemoryMemoryStore()
    t, u = uuid4(), uuid4()
    raw_linked = _item(
        tenant=t,
        user=u,
        content="raw_linked",
        consolidated_into=uuid4(),
    )
    raw_free = _item(tenant=t, user=u, content="raw_free")
    consolidated = _item(tenant=t, user=u, content="consolidated", status="consolidated")
    archived = _item(tenant=t, user=u, content="archived", status="archived")
    store._rows.extend([raw_linked, raw_free, consolidated, archived])
    hits = await store.retrieve(tenant_id=t, user_id=u, query_embedding=(1.0, 0.0), limit=10)
    contents = {h.content for h in hits}
    assert "raw_free" in contents
    assert "consolidated" in contents
    assert "raw_linked" not in contents
    assert "archived" not in contents


# ---------------------------------------------------------------------------
# Stream HX-2 (Mini-ADR HX-B3) — review flag lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_for_review_targets_thread_transients_only() -> None:
    store = InMemoryMemoryStore()
    t, u = uuid4(), uuid4()
    thread = "thread-a"
    hit = _item(tenant=t, user=u, content="from thread a").model_copy(
        update={"source_thread_id": thread}
    )
    consolidated = _item(tenant=t, user=u, content="parent", status="consolidated").model_copy(
        update={"source_thread_id": thread}
    )
    other = _item(tenant=t, user=u, content="other thread").model_copy(
        update={"source_thread_id": "thread-b"}
    )
    await store.write([hit, consolidated, other])

    flagged = await store.flag_for_review(tenant_id=t, user_id=u, source_thread_id=thread)

    assert flagged == 1  # consolidated parent + other thread untouched
    listed = await store.list_review_flagged(tenant_id=t, user_id=u, limit=10)
    assert [i.id for i in listed] == [hit.id]


@pytest.mark.asyncio
async def test_flag_reopens_previously_reviewed_item() -> None:
    """A user 👎 re-opens even an item the consolidator already deemed
    durable — unlike purge candidates, flagged listing ignores
    ``last_reviewed_at``."""
    store = InMemoryMemoryStore()
    t, u = uuid4(), uuid4()
    item = _item(tenant=t, user=u, content="reviewed durable", last_reviewed_at=_NOW).model_copy(
        update={"source_thread_id": "thread-x"}
    )
    await store.write([item])

    assert await store.flag_for_review(tenant_id=t, user_id=u, source_thread_id="thread-x") == 1
    listed = await store.list_review_flagged(tenant_id=t, user_id=u, limit=10)
    assert [i.id for i in listed] == [item.id]


@pytest.mark.asyncio
async def test_mark_reviewed_clears_review_flag() -> None:
    store = InMemoryMemoryStore()
    t, u = uuid4(), uuid4()
    item = _item(tenant=t, user=u, content="flagged").model_copy(
        update={"source_thread_id": "thread-y"}
    )
    await store.write([item])
    await store.flag_for_review(tenant_id=t, user_id=u, source_thread_id="thread-y")

    ok = await store.mark_reviewed(tenant_id=t, user_id=u, memory_id=item.id)

    assert ok is True
    assert await store.list_review_flagged(tenant_id=t, user_id=u, limit=10) == []
