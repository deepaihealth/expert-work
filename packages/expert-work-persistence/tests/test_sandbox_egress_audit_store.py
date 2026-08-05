"""Tests for the read-side sandbox egress audit store (sandbox-egress Phase 3)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from expert_work.persistence.sandbox_egress_audit import (
    EgressAuditQuery,
    EgressAuditRecord,
    InMemorySandboxEgressAuditStore,
    decode_cursor,
    encode_cursor,
)

_T1 = UUID("11111111-1111-1111-1111-111111111111")
_T2 = UUID("22222222-2222-2222-2222-222222222222")


def _rec(
    row_id: int,
    *,
    tenant_id: UUID | None = _T1,
    agent_name: str = "a",
    verdict: str = "allowed",
    host: str = "api.openai.com",
) -> EgressAuditRecord:
    return EgressAuditRecord(
        id=row_id,
        tenant_id=tenant_id,
        agent_name=agent_name,
        agent_version="1.0.0",
        sandbox_id=f"sbx-{row_id}",
        target_host=host,
        target_port=443,
        verdict=verdict,
        bytes_up=10,
        bytes_down=20,
        duration_ms=5,
        error_msg=None,
        occurred_at=datetime.now(UTC),
    )


def test_cursor_round_trip() -> None:
    assert decode_cursor(encode_cursor(12345)) == 12345


def test_cursor_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="cursor"):
        decode_cursor("not-a-cursor!!")


async def test_query_newest_first_and_tenant_filter() -> None:
    store = InMemorySandboxEgressAuditStore()
    store.records = [_rec(1, tenant_id=_T1), _rec(2, tenant_id=_T2), _rec(3, tenant_id=_T1)]

    page = await store.query(EgressAuditQuery(tenant_id=_T1))
    assert [r.id for r in page.entries] == [3, 1]  # newest-first, T2 excluded


async def test_query_cross_tenant_wildcard_spans_all() -> None:
    store = InMemorySandboxEgressAuditStore()
    store.records = [_rec(1, tenant_id=_T1), _rec(2, tenant_id=_T2)]
    page = await store.query(EgressAuditQuery(tenant_id="*"))
    assert {r.id for r in page.entries} == {1, 2}


async def test_null_tenant_blocked_auth_only_in_cross_tenant_view() -> None:
    # audit-eval Phase 4 — a blocked_auth row has tenant_id=None (no trustworthy
    # tenant); it shows only in the cross-tenant ("*") view, never in a
    # specific-tenant query.
    store = InMemorySandboxEgressAuditStore()
    store.records = [
        _rec(1, tenant_id=None, verdict="blocked_auth"),
        _rec(2, tenant_id=_T1),
    ]
    specific = await store.query(EgressAuditQuery(tenant_id=_T1))
    assert [r.id for r in specific.entries] == [2]
    cross = await store.query(EgressAuditQuery(tenant_id="*"))
    assert {r.id for r in cross.entries} == {1, 2}


async def test_query_filters_verdict_and_host() -> None:
    store = InMemorySandboxEgressAuditStore()
    store.records = [
        _rec(1, verdict="allowed", host="api.openai.com"),
        _rec(2, verdict="blocked_allowlist", host="evil.com"),
        _rec(3, verdict="blocked_ssrf", host="api.openai.com"),
    ]
    by_verdict = await store.query(EgressAuditQuery(tenant_id=_T1, verdict="blocked_allowlist"))
    assert [r.id for r in by_verdict.entries] == [2]

    by_host = await store.query(EgressAuditQuery(tenant_id=_T1, target_host="api.openai.com"))
    assert {r.id for r in by_host.entries} == {1, 3}


async def test_query_paginates_with_cursor() -> None:
    store = InMemorySandboxEgressAuditStore()
    store.records = [_rec(i) for i in range(1, 6)]  # ids 1..5

    first = await store.query(EgressAuditQuery(tenant_id=_T1, limit=2))
    assert [r.id for r in first.entries] == [5, 4]
    assert first.next_cursor is not None

    second = await store.query(EgressAuditQuery(tenant_id=_T1, limit=2, cursor=first.next_cursor))
    assert [r.id for r in second.entries] == [3, 2]

    third = await store.query(EgressAuditQuery(tenant_id=_T1, limit=2, cursor=second.next_cursor))
    assert [r.id for r in third.entries] == [1]
    assert third.next_cursor is None  # last page


async def test_query_empty_store() -> None:
    store = InMemorySandboxEgressAuditStore()
    page = await store.query(EgressAuditQuery(tenant_id=uuid4()))
    assert page.entries == []
    assert page.next_cursor is None


async def test_count_by_verdict_since_excludes_allowed_and_out_of_window_rows() -> None:
    # 407 可观测 worker's incremental scan — Task 3 Step 1 + fix round 1
    # (double-count guard: the ``until`` upper bound is exclusive).
    store = InMemorySandboxEgressAuditStore()
    since = datetime.now(UTC) - timedelta(minutes=5)
    until = since + timedelta(minutes=5)
    store.records = [
        replace(_rec(1, verdict="blocked_auth"), occurred_at=since),  # lower bound: inclusive
        replace(_rec(2, verdict="blocked_auth"), occurred_at=since + timedelta(minutes=2)),
        replace(_rec(3, verdict="allowed"), occurred_at=since + timedelta(minutes=2)),
        # before the window:
        replace(_rec(4, verdict="blocked_ssrf"), occurred_at=since - timedelta(minutes=1)),
        # upper bound: exclusive, not counted (fix round 1 double-count guard):
        replace(_rec(5, verdict="blocked_ssrf"), occurred_at=until),
    ]

    counts = await store.count_by_verdict_since(since=since, until=until)
    assert counts == {"blocked_auth": 2}
