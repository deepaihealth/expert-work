"""Unit tests for the in-memory tenant MCP server store."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from expert_work.persistence.tenant_mcp_server import (
    InMemoryTenantMcpServerStore,
    TenantMcpServerAlreadyExistsError,
    TenantMcpServerNotFoundError,
)
from expert_work.protocol import TenantMcpServerPatch, TenantMcpServerRecord


async def _make(
    store: InMemoryTenantMcpServerStore, tenant_id: UUID, **over: Any
) -> TenantMcpServerRecord:
    kwargs: dict[str, Any] = {
        "tenant_id": tenant_id,
        "name": "github",
        "transport": "streamable_http",
        "url": "https://mcp.example.com/mcp",
        "auth_type": "none",
        "token_secret_ref": None,
        "timeout_s": 30.0,
        "created_by": "admin@acme",
    }
    kwargs.update(over)
    return await store.create(**kwargs)


@pytest.mark.asyncio
async def test_create_then_get_round_trip() -> None:
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    created = await _make(store, tid)
    assert created.name == "github"
    assert created.tenant_id == tid
    fetched = await store.get(tenant_id=tid, name="github")
    assert fetched is not None
    assert fetched.id == created.id


@pytest.mark.asyncio
async def test_get_absent_returns_none() -> None:
    store = InMemoryTenantMcpServerStore()
    assert await store.get(tenant_id=uuid4(), name="nope") is None


@pytest.mark.asyncio
async def test_duplicate_name_same_tenant_rejected() -> None:
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    await _make(store, tid)
    with pytest.raises(TenantMcpServerAlreadyExistsError):
        await _make(store, tid)


@pytest.mark.asyncio
async def test_same_name_different_tenant_ok() -> None:
    store = InMemoryTenantMcpServerStore()
    a, b = uuid4(), uuid4()
    await _make(store, a)
    await _make(store, b)  # no conflict
    assert (await store.get(tenant_id=a, name="github")) is not None
    assert (await store.get(tenant_id=b, name="github")) is not None


@pytest.mark.asyncio
async def test_list_for_tenant_sorted_and_scoped() -> None:
    store = InMemoryTenantMcpServerStore()
    a, b = uuid4(), uuid4()
    await _make(store, a, name="zeta")
    await _make(store, a, name="alpha")
    await _make(store, b, name="gamma")
    names = [r.name for r in await store.list_for_tenant(tenant_id=a)]
    assert names == ["alpha", "zeta"]


@pytest.mark.asyncio
async def test_list_all_tenants_ordered_name_then_tenant_id() -> None:
    """W4 review C-3 — the aggregate orders ``(name, tenant_id.int)``. The
    shared-name pair is created in *descending* ``tenant_id.int`` order so a
    dropped tenant_id tiebreak degrades to dict insertion order and the
    exact-order assertion goes red."""
    store = InMemoryTenantMcpServerStore()
    tid_lo, tid_hi = sorted((uuid4(), uuid4()), key=lambda u: u.int)
    await _make(store, tid_hi, name="beta")  # descending insertion for the tie
    await _make(store, tid_lo, name="beta")
    await _make(store, tid_hi, name="alpha")

    rows = await store.list_all_tenants()
    assert [(r.name, r.tenant_id) for r in rows] == [
        ("alpha", tid_hi),
        ("beta", tid_lo),
        ("beta", tid_hi),
    ]


@pytest.mark.asyncio
async def test_update_applies_partial_fields() -> None:
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    await _make(store, tid)
    original = await store.get(tenant_id=tid, name="github")
    assert original is not None
    updated = await store.update(
        tenant_id=tid,
        name="github",
        patch=TenantMcpServerPatch(url="https://new.example.com/mcp", enabled=False),
    )
    assert updated.url == "https://new.example.com/mcp"
    assert updated.enabled is False
    assert updated.updated_at >= updated.created_at
    assert updated.timeout_s == original.timeout_s
    assert updated.transport == original.transport
    assert updated.auth_type == original.auth_type
    assert updated.id == original.id
    assert updated.created_at == original.created_at


@pytest.mark.asyncio
async def test_update_absent_raises() -> None:
    store = InMemoryTenantMcpServerStore()
    with pytest.raises(TenantMcpServerNotFoundError):
        await store.update(
            tenant_id=uuid4(), name="nope", patch=TenantMcpServerPatch(enabled=False)
        )


@pytest.mark.asyncio
async def test_delete_removes_row() -> None:
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    await _make(store, tid)
    await store.delete(tenant_id=tid, name="github")
    assert await store.get(tenant_id=tid, name="github") is None


@pytest.mark.asyncio
async def test_delete_absent_raises() -> None:
    store = InMemoryTenantMcpServerStore()
    with pytest.raises(TenantMcpServerNotFoundError):
        await store.delete(tenant_id=uuid4(), name="nope")


@pytest.mark.asyncio
async def test_list_for_tenant_empty_when_no_rows() -> None:
    store = InMemoryTenantMcpServerStore()
    assert await store.list_for_tenant(tenant_id=uuid4()) == []


@pytest.mark.asyncio
async def test_create_without_catalog_id_defaults_none() -> None:
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    created = await _make(store, tid)
    assert created.catalog_id is None


@pytest.mark.asyncio
async def test_health_unset_until_probed() -> None:
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    created = await _make(store, tid)
    assert created.last_probe_at is None
    assert created.last_probe_status is None
    assert created.last_probe_error is None


@pytest.mark.asyncio
async def test_record_probe_result_ok_clears_error_and_keeps_updated_at() -> None:
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    created = await _make(store, tid)
    # First an error, then a recovery.
    at1 = datetime(2026, 6, 6, 10, 0, tzinfo=UTC)
    await store.record_probe_result(
        tenant_id=tid, name="github", status="error", probed_at=at1, error="MCP_PROBE_TIMEOUT"
    )
    at2 = datetime(2026, 6, 6, 11, 0, tzinfo=UTC)
    ok = await store.record_probe_result(
        tenant_id=tid, name="github", status="ok", probed_at=at2, error="ignored"
    )
    assert ok.last_probe_status == "ok"
    assert ok.last_probe_at == at2
    assert ok.last_probe_error is None  # ok clears any prior error
    assert ok.updated_at == created.updated_at  # a probe is not a config change


@pytest.mark.asyncio
async def test_record_probe_result_error_persists_message() -> None:
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    await _make(store, tid)
    at = datetime(2026, 6, 6, 12, 0, tzinfo=UTC)
    rec = await store.record_probe_result(
        tenant_id=tid, name="github", status="error", probed_at=at, error="MCP_PROBE_CONNECT"
    )
    assert rec.last_probe_status == "error"
    assert rec.last_probe_error == "MCP_PROBE_CONNECT"


@pytest.mark.asyncio
async def test_record_probe_result_absent_raises() -> None:
    store = InMemoryTenantMcpServerStore()
    with pytest.raises(TenantMcpServerNotFoundError):
        await store.record_probe_result(
            tenant_id=uuid4(), name="nope", status="ok", probed_at=datetime.now(tz=UTC)
        )


@pytest.mark.asyncio
async def test_create_with_catalog_id_round_trips() -> None:
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    catalog_id = uuid4()
    created = await _make(store, tid, catalog_id=catalog_id)
    assert created.catalog_id == catalog_id
    fetched = await store.get(tenant_id=tid, name="github")
    assert fetched is not None and fetched.catalog_id == catalog_id


@pytest.mark.asyncio
async def test_create_with_custom_headers_round_trips() -> None:
    """M1: header ref + names + sse_read_timeout persist and read back."""
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    created = await _make(
        store,
        tid,
        custom_headers_ref="secret://expert-work/tenant/x/mcp/github/headers",
        custom_header_names=["X-API-Key", "X-Org"],
        sse_read_timeout_s=120.0,
    )
    assert created.custom_header_names == ["X-API-Key", "X-Org"]
    assert created.sse_read_timeout_s == 120.0
    fetched = await store.get(tenant_id=tid, name="github")
    assert fetched is not None
    assert fetched.custom_headers_ref == "secret://expert-work/tenant/x/mcp/github/headers"
    assert fetched.custom_header_names == ["X-API-Key", "X-Org"]


@pytest.mark.asyncio
async def test_update_replaces_custom_headers() -> None:
    """M1: patching both ref+names replaces the header set; sse timeout updates."""
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    await _make(
        store,
        tid,
        custom_headers_ref="secret://expert-work/tenant/x/mcp/github/headers",
        custom_header_names=["X-Old"],
    )
    updated = await store.update(
        tenant_id=tid,
        name="github",
        patch=TenantMcpServerPatch(
            custom_headers_ref="secret://expert-work/tenant/x/mcp/github/headers",
            custom_header_names=["X-New", "X-API-Key"],
            sse_read_timeout_s=90.0,
        ),
    )
    assert updated.custom_header_names == ["X-New", "X-API-Key"]
    assert updated.sse_read_timeout_s == 90.0


@pytest.mark.asyncio
async def test_count_for_catalog_cross_tenant() -> None:
    """Platform-scope catalog method (MCP catalog delete-guard, Task 8 review
    I-1) counts across every tenant — no tenant_id predicate."""
    store = InMemoryTenantMcpServerStore()
    tid_1, tid_2 = uuid4(), uuid4()
    cat_a, cat_b = uuid4(), uuid4()

    await _make(store, tid_1, name="github", catalog_id=cat_a)
    await _make(store, tid_2, name="github", catalog_id=cat_a)
    await _make(store, tid_1, name="linear", catalog_id=cat_b)

    assert await store.count_for_catalog(catalog_id=cat_a) == 2
    assert await store.count_for_catalog(catalog_id=cat_b) == 1


@pytest.mark.asyncio
async def test_count_for_catalog_empty() -> None:
    store = InMemoryTenantMcpServerStore()
    assert await store.count_for_catalog(catalog_id=uuid4()) == 0
