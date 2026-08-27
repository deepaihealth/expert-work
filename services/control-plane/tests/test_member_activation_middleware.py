"""Unit tests for ``MemberActivationMiddleware`` — 2026-08-27 拍板「登录过就算」.

The invited→active promotion moved off the run-create endpoint (Stream R R-8)
onto the request path: any request carrying the user's verified JWT counts.
Tested against the in-memory member/user stores through a minimal ASGI stack.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from control_plane.member_activation import MemberActivationMiddleware
from expert_work.persistence import InMemoryTenantMemberStore
from expert_work.persistence.tenant_user import InMemoryTenantUserStore
from expert_work.protocol import Principal


def _app(
    *,
    member_repo: InMemoryTenantMemberStore | None,
    users: InMemoryTenantUserStore | None,
    principal: Principal | None,
) -> Starlette:
    async def ok(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    class _SeedPrincipal:
        """Outer layer standing in for AuthMiddleware: stamps the principal."""

        def __init__(self, app: Any) -> None:
            self._app = app

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http" and principal is not None:
                scope.setdefault("state", {})["principal"] = principal
            await self._app(scope, receive, send)

    app = Starlette(routes=[Route("/", ok)])
    app.add_middleware(MemberActivationMiddleware, member_repo=member_repo, users=users)
    app.add_middleware(_SeedPrincipal)
    return app


async def _get(app: Starlette) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/")
        assert resp.status_code == 200


def _user_principal(subject_id: str, tenant: Any) -> Principal:
    return Principal(
        subject_id=subject_id, subject_type="user", tenant_id=tenant, roles=("operator",)
    )


@pytest.mark.asyncio
async def test_invited_member_activated_on_any_authenticated_request() -> None:
    store = InMemoryTenantMemberStore()
    users = InMemoryTenantUserStore()
    tenant, kc_id = uuid4(), str(uuid4())
    member = await store.create(
        tenant_id=tenant, email="e@co.com", role="operator", invited_by="admin"
    )
    await store.set_keycloak_user_id(member_id=member.id, tenant_id=tenant, keycloak_user_id=kc_id)

    await _get(_app(member_repo=store, users=users, principal=_user_principal(kc_id, tenant)))

    got = await store.get(tenant_id=tenant, member_id=member.id)
    assert got is not None and got.status == "active"
    # subject_id back-filled with the resolved tenant_user.id (Mini-ADR R-6)
    user = await users.resolve(tenant_id=tenant, subject_type="user", subject_id=kc_id)
    assert got.subject_id == user.id


@pytest.mark.asyncio
async def test_memo_skips_store_after_definitive_outcome() -> None:
    store = InMemoryTenantMemberStore()
    users = InMemoryTenantUserStore()
    tenant, kc_id = uuid4(), str(uuid4())
    member = await store.create(
        tenant_id=tenant, email="e@co.com", role="operator", invited_by="admin"
    )
    await store.set_keycloak_user_id(member_id=member.id, tenant_id=tenant, keycloak_user_id=kc_id)

    lookups = 0
    orig = store.get_by_keycloak_user_id

    async def _counting(**kwargs: Any) -> Any:
        nonlocal lookups
        lookups += 1
        return await orig(**kwargs)

    store.get_by_keycloak_user_id = _counting  # type: ignore[method-assign]
    app = _app(member_repo=store, users=users, principal=_user_principal(kc_id, tenant))
    await _get(app)
    await _get(app)
    await _get(app)
    assert lookups == 1  # first request settles it; memo skips the rest


@pytest.mark.asyncio
async def test_machine_principal_skipped() -> None:
    store = InMemoryTenantMemberStore()
    users = InMemoryTenantUserStore()
    machine = Principal(
        subject_id=str(uuid4()),
        subject_type="service_account",
        tenant_id=uuid4(),
        roles=("operator",),
    )
    lookups = 0
    orig = store.get_by_keycloak_user_id

    async def _counting(**kwargs: Any) -> Any:
        nonlocal lookups
        lookups += 1
        return await orig(**kwargs)

    store.get_by_keycloak_user_id = _counting  # type: ignore[method-assign]
    await _get(_app(member_repo=store, users=users, principal=machine))
    assert lookups == 0


@pytest.mark.asyncio
async def test_no_roster_row_is_definitive_noop() -> None:
    # e.g. the bootstrap admin — never invited; memoed after one lookup.
    store = InMemoryTenantMemberStore()
    users = InMemoryTenantUserStore()
    principal = _user_principal(str(uuid4()), uuid4())
    await _get(_app(member_repo=store, users=users, principal=principal))


@pytest.mark.asyncio
async def test_store_error_never_blocks_the_request_and_retries() -> None:
    store = InMemoryTenantMemberStore()
    users = InMemoryTenantUserStore()
    tenant, kc_id = uuid4(), str(uuid4())
    member = await store.create(
        tenant_id=tenant, email="e@co.com", role="operator", invited_by="admin"
    )
    await store.set_keycloak_user_id(member_id=member.id, tenant_id=tenant, keycloak_user_id=kc_id)

    calls = 0
    orig = store.get_by_keycloak_user_id

    async def _flaky(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("db hiccup")
        return await orig(**kwargs)

    store.get_by_keycloak_user_id = _flaky  # type: ignore[method-assign]
    app = _app(member_repo=store, users=users, principal=_user_principal(kc_id, tenant))
    await _get(app)  # error swallowed, request still 200
    await _get(app)  # retried (no memo on error) → activates
    got = await store.get(tenant_id=tenant, member_id=member.id)
    assert got is not None and got.status == "active"


@pytest.mark.asyncio
async def test_missing_stores_noop() -> None:
    await _get(_app(member_repo=None, users=None, principal=_user_principal(str(uuid4()), uuid4())))
    await _get(_app(member_repo=None, users=None, principal=None))
