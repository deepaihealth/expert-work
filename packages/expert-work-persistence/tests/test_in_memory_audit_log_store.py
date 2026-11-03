"""Unit tests for :class:`InMemoryAuditLogStore`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AuditAction, AuditEntry, AuditQuery, AuditResult


def _entry(
    *,
    tenant_id: object | None = None,
    actor_id: str = "alice",
    action: AuditAction = AuditAction.MANIFEST_WRITE,
    resource_type: str = "manifest",
    resource_id: str | None = "demo@1",
    result: AuditResult = AuditResult.SUCCESS,
    details: dict[str, object] | None = None,
) -> AuditEntry:
    return AuditEntry(
        tenant_id=tenant_id or uuid4(),  # type: ignore[arg-type]
        actor_type="user",
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,  # type: ignore[arg-type]
        resource_id=resource_id,
        result=result,
        details=details or {},
    )


@pytest.mark.asyncio
async def test_append_assigns_id_and_timestamp() -> None:
    store = InMemoryAuditLogStore()
    entry = _entry()

    written = await store.append(entry)

    assert written.id == 1
    assert written.occurred_at is not None
    assert written.tenant_id == entry.tenant_id


@pytest.mark.asyncio
async def test_append_assigns_monotonic_ids() -> None:
    store = InMemoryAuditLogStore()
    a = await store.append(_entry())
    b = await store.append(_entry())
    c = await store.append(_entry())
    assert (a.id, b.id, c.id) == (1, 2, 3)


@pytest.mark.asyncio
async def test_get_by_id_tenant_isolated() -> None:
    store = InMemoryAuditLogStore()
    tenant_a, tenant_b = uuid4(), uuid4()
    written = await store.append(_entry(tenant_id=tenant_a))

    assert await store.get_by_id(written.id or 0, tenant_id=tenant_a) == written
    # Cross-tenant lookup returns None — never reveals existence.
    assert await store.get_by_id(written.id or 0, tenant_id=tenant_b) is None
    assert await store.get_by_id(9999, tenant_id=tenant_a) is None


@pytest.mark.asyncio
async def test_query_returns_newest_first() -> None:
    store = InMemoryAuditLogStore()
    tenant = uuid4()
    for _ in range(3):
        await store.append(_entry(tenant_id=tenant))

    page = await store.query(AuditQuery(tenant_id=tenant))
    assert [e.id for e in page.entries] == [3, 2, 1]
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_query_filters_by_tenant() -> None:
    store = InMemoryAuditLogStore()
    tenant_a, tenant_b = uuid4(), uuid4()
    await store.append(_entry(tenant_id=tenant_a))
    await store.append(_entry(tenant_id=tenant_b))
    await store.append(_entry(tenant_id=tenant_a))

    only_a = await store.query(AuditQuery(tenant_id=tenant_a))
    assert {e.tenant_id for e in only_a.entries} == {tenant_a}
    assert len(only_a.entries) == 2


@pytest.mark.asyncio
async def test_query_wildcard_tenant_returns_all() -> None:
    store = InMemoryAuditLogStore()
    tenant_a, tenant_b = uuid4(), uuid4()
    await store.append(_entry(tenant_id=tenant_a))
    await store.append(_entry(tenant_id=tenant_b))

    page = await store.query(AuditQuery(tenant_id="*"))
    assert {e.tenant_id for e in page.entries} == {tenant_a, tenant_b}


@pytest.mark.asyncio
async def test_query_compound_filters() -> None:
    store = InMemoryAuditLogStore()
    tenant = uuid4()
    await store.append(_entry(tenant_id=tenant, actor_id="alice", action=AuditAction.AUTH_LOGIN))
    await store.append(_entry(tenant_id=tenant, actor_id="bob", action=AuditAction.AUTH_LOGIN))
    await store.append(
        _entry(tenant_id=tenant, actor_id="alice", action=AuditAction.MANIFEST_WRITE)
    )

    alice_logins = await store.query(
        AuditQuery(tenant_id=tenant, actor_id="alice", action=AuditAction.AUTH_LOGIN)
    )
    assert [e.actor_id for e in alice_logins.entries] == ["alice"]
    assert alice_logins.entries[0].action == AuditAction.AUTH_LOGIN


@pytest.mark.asyncio
async def test_query_time_window() -> None:
    store = InMemoryAuditLogStore()
    tenant = uuid4()
    now = datetime.now(UTC)
    earlier = AuditEntry(
        tenant_id=tenant,
        actor_type="user",
        actor_id="alice",
        action=AuditAction.AUTH_LOGIN,
        resource_type="audit",
        result=AuditResult.SUCCESS,
        occurred_at=now - timedelta(days=10),
    )
    later = AuditEntry(
        tenant_id=tenant,
        actor_type="user",
        actor_id="alice",
        action=AuditAction.AUTH_LOGIN,
        resource_type="audit",
        result=AuditResult.SUCCESS,
        occurred_at=now,
    )
    await store.append(earlier)
    await store.append(later)

    window = await store.query(AuditQuery(tenant_id=tenant, from_ts=now - timedelta(days=1)))
    assert [e.actor_id for e in window.entries] == ["alice"]
    assert len(window.entries) == 1


@pytest.mark.asyncio
async def test_query_pagination_via_cursor() -> None:
    store = InMemoryAuditLogStore()
    tenant = uuid4()
    for _ in range(5):
        await store.append(_entry(tenant_id=tenant))

    page_one = await store.query(AuditQuery(tenant_id=tenant, limit=2))
    assert [e.id for e in page_one.entries] == [5, 4]
    assert page_one.next_cursor is not None

    page_two = await store.query(AuditQuery(tenant_id=tenant, limit=2, cursor=page_one.next_cursor))
    assert [e.id for e in page_two.entries] == [3, 2]
    assert page_two.next_cursor is not None

    page_three = await store.query(
        AuditQuery(tenant_id=tenant, limit=2, cursor=page_two.next_cursor)
    )
    assert [e.id for e in page_three.entries] == [1]
    assert page_three.next_cursor is None


@pytest.mark.asyncio
async def test_document_uploads_for_thread_is_targeted() -> None:
    """B-262 P3 review 3 —— 迁移按会话找它的上传审计:本租户、``document:upload``、成功的、
    ``details.thread_id`` 是这个会话、不早于 ``since``,按发生先后。"""
    store = InMemoryAuditLogStore()
    tenant, thread = uuid4(), uuid4()
    mine: dict[str, object] = {"thread_id": str(thread), "workspace_path": "uploads/a.pdf"}

    first = await store.append(
        _entry(tenant_id=tenant, action=AuditAction.DOCUMENT_UPLOAD, details=mine)
    )
    second = await store.append(
        _entry(tenant_id=tenant, action=AuditAction.DOCUMENT_UPLOAD, details=mine)
    )
    await store.append(
        _entry(
            tenant_id=tenant,
            action=AuditAction.DOCUMENT_UPLOAD,
            details={"thread_id": str(uuid4())},
        )
    )
    await store.append(_entry(tenant_id=uuid4(), action=AuditAction.DOCUMENT_UPLOAD, details=mine))
    await store.append(_entry(tenant_id=tenant, action=AuditAction.MANIFEST_WRITE, details=mine))
    await store.append(
        _entry(
            tenant_id=tenant,
            action=AuditAction.DOCUMENT_UPLOAD,
            details=mine,
            result=AuditResult.DENIED,
        )
    )

    got = await store.list_document_uploads_for_thread(tenant_id=tenant, thread_id=thread)
    assert [e.id for e in got] == [first.id, second.id]
    later = await store.list_document_uploads_for_thread(
        tenant_id=tenant, thread_id=thread, since=datetime.now(UTC) + timedelta(minutes=1)
    )
    assert later == []
