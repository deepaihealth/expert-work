"""Unit tests for the in-memory MCP connector catalog store — Stream W."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from expert_work.persistence.mcp_connector_catalog import (
    InMemoryMcpConnectorCatalogStore,
    McpConnectorCatalogAlreadyExistsError,
    McpConnectorCatalogNotFoundError,
)
from expert_work.protocol import (
    McpConnectorAuthField,
    McpConnectorAuthSchema,
    McpConnectorCatalogPatch,
    McpConnectorCatalogRecord,
    McpConnectorCatalogUpsert,
    TenantPlan,
)


def _bearer_schema() -> McpConnectorAuthSchema:
    return McpConnectorAuthSchema(
        fields=[
            McpConnectorAuthField(key="token", label="API Token", kind="secret"),
            McpConnectorAuthField(key="org", label="Organization", kind="param"),
        ]
    )


async def _make(store: InMemoryMcpConnectorCatalogStore, **over: Any) -> McpConnectorCatalogRecord:
    kwargs: dict[str, Any] = {
        "name": "github",
        "display_name": "GitHub",
        "transport": "streamable_http",
        "url_template": "https://api.github.com/{org}/mcp",
        "auth_type": "bearer",
        "auth_schema": _bearer_schema(),
        "required_tier": TenantPlan.PRO,
    }
    kwargs.update(over)
    return await store.create(upsert=McpConnectorCatalogUpsert(**kwargs), actor_id="sysadmin")


@pytest.mark.asyncio
async def test_create_then_get_by_id_round_trip() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    created = await _make(store)
    assert created.name == "github"
    assert created.tenant_id is None
    assert created.required_tier is TenantPlan.PRO
    assert created.updated_by == "sysadmin"
    fetched = await store.get_by_id(created.id)
    assert fetched is not None
    assert fetched.id == created.id


@pytest.mark.asyncio
async def test_auth_schema_round_trip() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    created = await _make(store)
    fetched = await store.get_by_id(created.id)
    assert fetched is not None
    assert [(f.key, f.kind) for f in fetched.auth_schema.fields] == [
        ("token", "secret"),
        ("org", "param"),
    ]
    assert [f.key for f in fetched.auth_schema.secret_fields()] == ["token"]


@pytest.mark.asyncio
async def test_oauth2_fields_round_trip() -> None:
    """Stream MCP-OAUTH (OA-1a): oauth_client_id / oauth_scopes survive the store."""
    store = InMemoryMcpConnectorCatalogStore()
    created = await _make(
        store,
        name="linear",
        transport="sse",
        url_template="https://mcp.linear.app/sse",
        auth_type="oauth2",
        auth_schema=McpConnectorAuthSchema(),
        oauth_client_id="expert-work-linear-app",
        oauth_scopes="read write",
    )
    fetched = await store.get_by_id(created.id)
    assert fetched is not None
    assert fetched.auth_type == "oauth2"
    assert fetched.oauth_client_id == "expert-work-linear-app"
    assert fetched.oauth_scopes == "read write"


@pytest.mark.asyncio
async def test_get_by_name() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    await _make(store)
    fetched = await store.get_by_name("github")
    assert fetched is not None and fetched.name == "github"
    assert await store.get_by_name("nope") is None


@pytest.mark.asyncio
async def test_get_absent_returns_none() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    assert await store.get_by_id(uuid4()) is None


@pytest.mark.asyncio
async def test_duplicate_name_rejected() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    await _make(store)
    with pytest.raises(McpConnectorCatalogAlreadyExistsError):
        await _make(store)


@pytest.mark.asyncio
async def test_list_sorted_and_category_filter() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    await _make(store, name="zeta", category="dev")
    await _make(store, name="alpha", category="dev")
    await _make(store, name="gamma", category="data")
    all_names = [r.name for r in await store.list()]
    assert all_names == ["alpha", "gamma", "zeta"]
    dev_names = [r.name for r in await store.list(category="dev")]
    assert dev_names == ["alpha", "zeta"]


@pytest.mark.asyncio
async def test_update_applies_partial_fields() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    created = await _make(store)
    updated = await store.update(
        catalog_id=created.id,
        patch=McpConnectorCatalogPatch(
            display_name="GitHub (Official)",
            required_tier=TenantPlan.ENTERPRISE,
            enabled=False,
        ),
    )
    assert updated.display_name == "GitHub (Official)"
    assert updated.required_tier is TenantPlan.ENTERPRISE
    assert updated.enabled is False
    # Unchanged fields preserved.
    assert updated.name == created.name
    assert updated.transport == created.transport
    assert updated.id == created.id
    assert updated.created_at == created.created_at
    assert updated.updated_at >= created.updated_at


@pytest.mark.asyncio
async def test_update_auth_schema_round_trip() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    created = await _make(store)
    new_schema = McpConnectorAuthSchema(
        fields=[McpConnectorAuthField(key="pat", label="PAT", kind="secret")]
    )
    updated = await store.update(
        catalog_id=created.id, patch=McpConnectorCatalogPatch(auth_schema=new_schema)
    )
    assert [f.key for f in updated.auth_schema.fields] == ["pat"]


@pytest.mark.asyncio
async def test_update_absent_raises() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    with pytest.raises(McpConnectorCatalogNotFoundError):
        await store.update(catalog_id=uuid4(), patch=McpConnectorCatalogPatch(enabled=False))


@pytest.mark.asyncio
async def test_delete_removes_row() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    created = await _make(store)
    await store.delete(created.id)
    assert await store.get_by_id(created.id) is None


@pytest.mark.asyncio
async def test_delete_absent_raises() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    with pytest.raises(McpConnectorCatalogNotFoundError):
        await store.delete(uuid4())


@pytest.mark.asyncio
async def test_list_empty_when_no_rows() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    assert await store.list() == []


@pytest.mark.asyncio
async def test_update_rejecting_cross_field_invalid_patch_persists_nothing() -> None:
    """A patch breaking the bearer↔secret-field invariant must raise and leave
    the stored row untouched (validate-before-commit, W-2 parity with SQL)."""
    store = InMemoryMcpConnectorCatalogStore()
    created = await _make(store)  # bearer + one secret field ("token")
    with pytest.raises(ValueError, match="exactly one secret field"):
        await store.update(
            catalog_id=created.id,
            patch=McpConnectorCatalogPatch(auth_schema=McpConnectorAuthSchema()),
        )
    after = await store.get_by_id(created.id)
    assert after is not None
    assert [f.key for f in after.auth_schema.fields] == ["token", "org"]


@pytest.mark.asyncio
async def test_platform_bearer_token_ref_round_trips() -> None:
    """A platform shared-bearer entry persists ``bearer_token_ref`` (no secret
    field) and round-trips through get/list; patch can re-set the ref."""
    store = InMemoryMcpConnectorCatalogStore()
    created = await store.create(
        upsert=McpConnectorCatalogUpsert(
            name="shared",
            display_name="Shared",
            transport="streamable_http",
            url_template="https://mcp.example.com/mcp",
            auth_type="bearer",
            bearer_token_ref="secret://expert-work/platform/mcp/shared/token",
            required_tier=TenantPlan.FREE,
        ),
        actor_id="sysadmin",
    )
    assert created.bearer_token_ref == "secret://expert-work/platform/mcp/shared/token"
    assert created.auth_schema.secret_fields() == []

    got = await store.get_by_id(created.id)
    assert got is not None
    assert got.bearer_token_ref == created.bearer_token_ref

    updated = await store.update(
        catalog_id=created.id,
        patch=McpConnectorCatalogPatch(
            bearer_token_ref="secret://expert-work/platform/mcp/shared/token-v2"
        ),
    )
    assert updated.bearer_token_ref is not None
    assert updated.bearer_token_ref.endswith("token-v2")


@pytest.mark.asyncio
async def test_get_by_ids_batches_and_skips_missing() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    a = await _make(store, name="a")
    b = await _make(store, name="b")
    missing = uuid4()
    got = await store.get_by_ids([a.id, b.id, missing])
    assert set(got) == {a.id, b.id}  # missing id absent
    assert got[a.id].name == "a"


@pytest.mark.asyncio
async def test_get_by_ids_empty_input_returns_empty() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    assert await store.get_by_ids([]) == {}


@pytest.mark.asyncio
async def test_update_with_previous_returns_before_and_after() -> None:
    """B-248: concurrent updates each see the previous one's result."""
    import asyncio

    store = InMemoryMcpConnectorCatalogStore()
    created = await _make(store)
    off = McpConnectorCatalogPatch(enabled=False)

    results = await asyncio.gather(
        *(store.update_with_previous(catalog_id=created.id, patch=off) for _ in range(3))
    )

    assert sorted(before.enabled for before, _ in results) == [False, False, True]
    assert all(after.enabled is False for _, after in results)
    on_before, on_after = await store.update_with_previous(
        catalog_id=created.id, patch=McpConnectorCatalogPatch(enabled=True)
    )
    assert (on_before.enabled, on_after.enabled) == (False, True)
